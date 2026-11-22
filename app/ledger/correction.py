import csv
from dataclasses import dataclass
from io import StringIO
from time import perf_counter

from app.faults import LedgerError
from app.job import MixingJob
from app.ledger.identity import docket, revision_id
from app.receipts.clock import timepoint
from app.receipts.contract import COLUMNS
from app.receipts.packet import checksums
from app.receipts.units import milligrams

TARE_FIELDS = ('container', 'version', 'start', 'end', 'mass', 'unit')
META_FIELDS = ('reason', 'note', 'parent', 'key', 'correction')


def _invalid(message, code='CORRECTION_INVALID'):
    return LedgerError(code, message)


@dataclass(frozen=True)
class Change:
    container: str
    version: str
    mass: str | None
    unit: str | None
    start: str | None
    end: str | None


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f'皮重更正的 {field} 为空')
    value = value.strip()
    if '\n' in value or '\r' in value:
        raise _invalid(f'皮重更正的 {field} 不得跨行')
    return value


def _mass_text(value):
    if isinstance(value, bool):
        raise _invalid('皮重更正的 mass 不合法')
    if isinstance(value, (int, float)):
        value = str(value)
    return _text(value, 'mass')


def _time_text(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f'皮重更正的 {field} 为空')
    value = value.strip()
    try:
        timepoint(value)
    except ValueError:
        raise _invalid(f'皮重更正的 {field} 不是带偏移的时间') from None
    return value


def _one(row):
    if not isinstance(row, dict):
        raise _invalid('tare 须为对象')
    unknown = set(row) - set(TARE_FIELDS)
    if unknown:
        raise _invalid('皮重更正含未知字段：' + '、'.join(sorted(unknown)))
    container = _text(row.get('container'), 'container')
    version = _text(row.get('version'), 'version')
    mass = None if row.get('mass') is None else _mass_text(row.get('mass'))
    unit = None if row.get('unit') is None else _text(row.get('unit'), 'unit')
    start = _time_text(row.get('start'), 'start')
    end = _time_text(row.get('end'), 'end')
    if mass is None and start is None:
        raise _invalid('皮重更正至少须改写 mass/unit 或生效区间')
    if (start is None) != (end is None):
        raise _invalid('补报新皮重须同时提供 start 与 end')
    if start is not None and not timepoint(start) < timepoint(end):
        raise _invalid('皮重更正区间须 start < end')
    if mass is not None:
        try:
            milligrams(mass, unit or 'g')
        except ValueError as error:
            if unit is not None:
                raise _invalid(f'皮重更正重量无效（{error}）') from None
    return Change(container, version, mass, unit, start, end)


def parse(payload):
    if not isinstance(payload, dict):
        raise _invalid('皮重更正需提供 JSON 对象')
    reason = payload.get('reason', payload.get('note', ''))
    if reason is None:
        reason = ''
    if not isinstance(reason, str) or len(reason.strip()) > 500:
        raise _invalid('皮重更正的原因不合法')
    rows = payload.get('tare', payload.get('tare_rows'))
    if rows is not None and 'correction' in payload:
        raise _invalid('一次更正只能提交一种 tare 结构')
    if rows is None:
        rows = payload.get('correction')
    if rows is None:
        rows = {key: value for key, value in payload.items() if key not in META_FIELDS}
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise _invalid('需提供至少一条皮重更正')
    changes = [_one(row) for row in rows]
    seen = set()
    for change in changes:
        pair = (change.container, change.version)
        if pair in seen:
            raise _invalid(f'容器 {change.container} 的版本 {change.version} 在一次更正中重复')
        seen.add(pair)
    return changes, reason.strip()


def _spans(text):
    """按 scan 的行号口径列出每条记录的起止物理行，不改变原件。"""
    reader = csv.reader(StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    try:
        header = next(reader, None)
        records = []
        while True:
            start = reader.line_num + 1
            raw = next(reader, None)
            if raw is None:
                break
            records.append((start, reader.line_num, raw))
    except csv.Error:
        raise _invalid('皮重原件引号错误，无法更正') from None
    if header is None:
        raise _invalid('皮重原件缺少表头，无法更正')
    return header, records


def _lines(text):
    """切分物理行，行宽与 csv 的 \\r、\\n、\\r\\n 口径一致。"""
    lines = []
    index = 0
    while index < len(text):
        stop = index
        while stop < len(text) and text[stop] not in '\r\n':
            stop += 1
        if stop < len(text):
            if text[stop] == '\r' and text.startswith('\r\n', stop):
                stop += 2
            else:
                stop += 1
        lines.append(text[index:stop])
        index = stop
    return lines


def _ending(line):
    if line.endswith('\r\n'):
        return '\r\n'
    if line.endswith('\r'):
        return '\r'
    return '\n'


def _render(cells, ending='\n'):
    buffer = StringIO()
    csv.writer(buffer, lineterminator=ending).writerow(cells)
    return buffer.getvalue()


def _append(text, cells):
    lines = _lines(text)
    endings = [_ending(line) for line in lines if line.endswith(('\n', '\r'))]
    ending = endings[0] if endings else '\n'
    gap = '' if not text or text.endswith(('\n', '\r')) else ending
    return text + gap + _render(cells, ending)


def _resolved(change, original):
    merged = dict(original)
    merged.update(container=change.container, version=change.version)
    if change.mass is not None:
        merged['mass'] = change.mass
    if change.unit is not None:
        merged['unit'] = change.unit
    if change.start is not None:
        merged.update(start=change.start, end=change.end)
    start, end = merged.get('start'), merged.get('end')
    try:
        if not start or not end or not timepoint(start) < timepoint(end):
            raise ValueError('TARE_RANGE')
        if not merged.get('unit'):
            raise ValueError('BAD_UNIT')
        tare_mg = milligrams(merged['mass'], merged['unit'])
    except (KeyError, ValueError) as error:
        raise _invalid(f'更正后的皮重行无效（{error}）') from None
    return merged, start, end, tare_mg


def _canonical(merged, start, end):
    """规范化够定位到实物与生效窗口的字段；原因与书写格式不进键。"""
    return {'container': merged['container'].strip(), 'version': merged['version'].strip(),
            'start': timepoint(start).isoformat(), 'end': timepoint(end).isoformat(),
            'tare_mg': milligrams(merged['mass'], merged['unit'])}


def _original_canonical(original, columns):
    """原行能规范化时给出可比值；坏行无法规范化则视为必然改变。"""
    try:
        merged = dict(zip(columns, original, strict=True))
        start, end = merged.get('start'), merged.get('end')
        if not start or not end:
            return None
        return _canonical(merged, start, end)
    except (KeyError, ValueError, TypeError):
        return None


def apply(parent_sheets, changes):
    text = parent_sheets['tares']
    header, records = _spans(text)
    columns = [name.strip() for name in COLUMNS['tares'].split(',')]
    if [cell.strip() for cell in header] != columns:
        # scan 允许换列；这里按表头实际顺序写回，保留原件列序。
        columns = [cell.strip() for cell in header]
    try:
        container_at, version_at = header.index('container'), header.index('version')
    except ValueError:
        raise _invalid('皮重原件缺少 container 或 version 列，无法更正') from None
    staged = {}       # 替换目标行号 -> (原行, 写回单元格)
    appended = []     # 补报的新版本，按提交顺序追加
    evidence = []
    canonical = []
    changed = False
    for change in changes:
        matches = [(start, end, raw) for start, end, raw in records
                   if len(raw) == len(header)
                   and raw[container_at].strip() == change.container
                   and raw[version_at].strip() == change.version]
        if len(matches) > 1:
            raise _invalid(f'容器 {change.container} 的皮重版本 {change.version} 不唯一，无法定位原件')
        if matches:
            start, end, raw = matches[0]
            if start in staged:
                raise _invalid(f'皮重原件第 {start} 行被重复更正')
            merged, resolved_start, resolved_end, _ = _resolved(change, dict(zip(columns, raw, strict=True)))
            merged_canonical = _canonical(merged, resolved_start, resolved_end)
            if merged_canonical == _original_canonical(raw, columns):
                # 未改动任何物理值：不进证据、不进修订键，避免同一更正换个写法重签。
                continue
            cells = [merged.get(name, '') for name in columns]
            staged[start] = (end, raw, cells)
            changed = True
            evidence.append({'action': 'replace', 'container': change.container, 'version': change.version,
                             'line': start, 'before': list(raw), 'after': list(cells)})
        else:
            if change.start is None:
                raise _invalid(f'未找到容器 {change.container} 的皮重版本 {change.version}；补报新版本须提供 start 与 end')
            merged, resolved_start, resolved_end, _ = _resolved(change, {})
            cells = [merged.get(name, '') for name in columns]
            appended.append(cells)
            merged_canonical = _canonical(merged, resolved_start, resolved_end)
            changed = True
            evidence.append({'action': 'append', 'container': change.container, 'version': change.version,
                             'line': None, 'before': None, 'after': list(cells)})
        canonical.append(merged_canonical)
    lines = _lines(text)
    out = list(lines)
    for start, (end, _, cells) in sorted(staged.items(), reverse=True):
        out[start - 1:end] = [_render(cells, _ending(lines[start - 1]))]
    result = ''.join(out)
    for cells in appended:
        result = _append(result, cells)
    sheets = dict(parent_sheets) | {'tares': result}
    canonical.sort(key=lambda item: (item['container'], item['version'], item['start'], item['end'], item['tare_mg']))
    return sheets, evidence, canonical, changed


def _by_ticket(snapshot):
    return {row['data']['ticket']: row for row in snapshot['recovery']['rows']}


def _recalc(base, snapshot, pots):
    before, after = _by_ticket(base), _by_ticket(snapshot)
    sections = []
    for pot in pots:
        changes = []
        for ticket, row in sorted(after.items(), key=lambda item: (item[1]['at'], item[1]['line'])):
            if row['data']['pot'] != pot:
                continue
            old = before.get(ticket)
            if old is not None and old['net_mg'] == row['net_mg'] and old['parts_mg'] == row['parts_mg']:
                continue
            changes.append({'ticket': ticket, 'action': row['data']['action'], 'line': row['line'],
                            'net_before_mg': None if old is None else old['net_mg'],
                            'net_after_mg': row['net_mg'],
                            'parts_before_mg': None if old is None else old['parts_mg'],
                            'parts_after_mg': row['parts_mg'],
                            'stock_before_mg': None if old is None else old['after_mg'],
                            'stock_after_mg': row['after_mg']})
        if changes:
            sections.append({'pot': pot, 'tickets': changes})
    return sections


def _affected(base, snapshot, containers):
    before = {row['data']['ticket']: row for row in base['tares']['rows']}
    after = {row['data']['ticket']: row for row in snapshot['tares']['rows']}
    lost = sorted(set(before) - set(after))
    if lost:
        raise _invalid(f'更正后 {len(lost)} 张原已计价称量票无法计价（{", ".join(lost[:6])}），旧收据保持有效',
                       'CORRECTION_UNSTABLE')
    prior = {(row['data']['ticket'], row['code']) for row in base['recovery']['isolated']}
    fresh = [row for row in snapshot['recovery']['isolated']
             if (row['data']['ticket'], row['code']) not in prior]
    if fresh:
        tickets = ', '.join(row['data']['ticket'] for row in fresh[:6])
        raise _invalid(f'更正后出现新的回桶分摊隔离（{tickets}），旧收据保持有效', 'CORRECTION_UNSTABLE')
    if any(line['check_mg'] != 0 for line in snapshot['balance']['lines']):
        raise _invalid('更正后成分重量不平，旧收据保持有效', 'CORRECTION_UNSTABLE')
    tickets = []
    for ticket in sorted(after):
        was, now = before.get(ticket), after[ticket]
        if was is not None and was['net_mg'] == now['net_mg']:
            continue
        if was is None and now['data']['container'] not in containers:
            continue
        tickets.append({'ticket': ticket, 'pot': now['data']['pot'],
                        'container': now['data']['container'], 'action': now['data']['action'],
                        'tare_before_mg': None if was is None else was['tare_mg'],
                        'tare_after_mg': now['tare_mg'],
                        'net_before_mg': None if was is None else was['net_mg'],
                        'net_after_mg': now['net_mg']})
    pots = sorted({row['pot'] for row in tickets})
    return {'tickets': tickets, 'pots': pots, 'recalc': _recalc(base, snapshot, pots),
            'totals_before': base['balance']['totals'], 'totals_after': snapshot['balance']['totals']}


def effective(book, parent_key, payload):
    """解析并定位原件，但不落盘：供工作台预览更正影响。"""
    parent = book.require(parent_key)
    changes, reason = parse(payload)
    files, evidence, canonical, changed = apply(parent['sheets'], changes)
    return parent, changes, reason, files, evidence, canonical, changed


def correct(book, parent_key, payload):
    started = perf_counter()
    parent, changes, reason, files, evidence, canonical, changed = effective(book, parent_key, payload)
    if not changed:
        # 皮重物理值未变（可能只是单位写法不同）：不另签发一版，直接复用当前版。
        return parent, True
    key = revision_id(parent_key, canonical)
    existing = book.find(key)
    if existing:
        return existing, True
    containers = {change.container for change in changes}
    try:
        job = MixingJob(files)
        snapshot = job.snapshot()
    except LedgerError as error:
        raise _invalid(f'按更正皮重重算失败（{error}），旧收据保持有效',
                       'CORRECTION_UNSTABLE') from None
    except Exception as error:  # noqa: BLE001 - 任何重算异常都不得签发残缺收据
        raise _invalid(f'按更正皮重重算失败（{type(error).__name__}），旧收据保持有效',
                       'CORRECTION_UNSTABLE') from None
    try:
        affected = _affected(parent['snapshot'], snapshot, containers)
    except LedgerError as error:
        # 重算结果不能替代旧收据；旧留档未被触碰，仍可下载复核。
        raise _invalid(str(error), 'CORRECTION_UNSTABLE') from None
    except Exception as error:  # noqa: BLE001
        raise _invalid(f'按更正皮重重算失败（{type(error).__name__}），旧收据保持有效',
                       'CORRECTION_UNSTABLE') from None
    previous = parent['manifest']
    root = previous.get('root', parent_key)
    manifest = docket(job.job, key, previous['source'], checksums(files), started,
                      revision=book.next_revision(parent_key), root=root,
                      correction={'parent': parent_key, 'root': root, 'reason': reason,
                                  'changes': evidence, 'affected': affected})
    packet = {'manifest': manifest, 'sheets': files, 'snapshot': snapshot}
    return book.write(key, packet)
