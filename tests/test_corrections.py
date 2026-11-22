from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from app.ledger.identity import encoded
from app.main import create_app


@pytest.fixture
def sealed(client):
    response = client.post('/api/books', json={'sample': 'dirty'})
    assert response.status_code == 200
    return response.json['key']


def _nets(record):
    return {row['data']['ticket']: row['net_mg'] for row in record['snapshot']['tares']['rows']}


def _correction(client, key, rows, reason='补测'):
    return client.post(f'/api/books/{key}/corrections', json={'tare': rows, 'reason': reason})


def _record(client, key):
    return client.get('/api/export/' + key).json


def _replace(container='TIN-A', version='TA1', mass='22', unit='g'):
    return {'container': container, 'version': version, 'mass': mass, 'unit': unit}


def test_correcting_signed_tare_keeps_original(client, sealed):
    before = client.get('/api/books/' + sealed).data
    response = _correction(client, sealed, _replace())
    assert response.status_code == 200
    manifest = response.json['manifest']
    # 旧收据一字未动，仍可查回与下载。
    assert client.get('/api/books/' + sealed).data == before
    assert client.get('/api/export/' + sealed).status_code == 200
    assert (manifest['revision'], manifest['root'], manifest['correction']['parent']) == (2, sealed, sealed)


def test_correction_recomputes_same_pot_usage_and_return(client, sealed):
    corrected = _correction(client, sealed, _replace()).json
    affected = corrected['manifest']['correction']['affected']
    # 同罐 A12 后续每张票的净重都按新皮重重算。
    assert [(t['ticket'], t['net_before_mg'], t['net_after_mg']) for t in affected['tickets']] == [
        ('0001', 100000, 98000), ('0002', 200000, 198000), ('0003', 120000, 118000), ('0004', 30000, 28000),
        ('0005', 120000, 118000), ('0012', 10000, 8000), ('0014', 180000, 178000), ('0016', 5000, 3000),
    ]
    # 回桶票 0005 的成分分摊按新净重 118 g 重算，且仍按批号守恒。
    section = next(s for s in affected['recalc'] if s['pot'] == 'A12')
    assert section['tickets'][-1]['ticket'] == '0005'
    assert section['tickets'][-1]['parts_after_mg'] == {'R-017': 39067, 'W-041': 78933}
    assert sum(section['tickets'][-1]['parts_after_mg'].values()) == 118000
    assert affected['totals_after']['returned_mg'] == 144010


def test_corrected_receipt_has_new_net_and_balanced_ledger(client, sealed):
    key = _correction(client, sealed, _replace()).json['key']
    record = _record(client, key)
    assert _nets(record)['0005'] == 118000
    assert record['snapshot']['balance']['totals']['returned_mg'] == 144010
    assert all(line['check_mg'] == 0 for line in record['snapshot']['balance']['lines'])


def test_original_is_reachable_from_correction(client, sealed):
    key = _correction(client, sealed, _replace()).json['key']
    lineage = client.get(f'/api/books/{key}/revisions').json['revisions']
    assert [(m['revision'], m['key']) for m in lineage] == [(1, sealed), (2, key)]
    page = client.get('/studio/receipt?key=' + key).get_data(as_text=True)
    assert '/api/export/' + sealed in page and '本版皮重更正' in page and '同罐后续重算' in page


def test_retrying_same_correction_does_not_issue_twice(client, sealed):
    first = _correction(client, sealed, _replace()).json
    again = _correction(client, sealed, _replace(mass='22000', unit='mg'), reason='补测').json
    # 书写格式与原因不同，但物理皮重相同，仍视为同一更正。
    assert (again['key'], again['reused']) == (first['key'], True)
    assert len(client.get('/api/books').json['books']) == 2


def test_same_correction_is_stable_across_restart(tmp_path, sealed):
    first = create_app(tmp_path).test_client()
    key = first.post(f'/api/books/{sealed}/corrections', json={'tare': [_replace()]}).json['key']
    again = create_app(tmp_path).test_client().post(
        f'/api/books/{sealed}/corrections', json={'tare': [_replace()], 'reason': '另一班补录'})
    assert (again.json['key'], again.json['reused']) == (key, True)


def test_failed_recompute_keeps_usable_old_version(client, sealed):
    # 把 TA1 整体挪到 12:00 之后，会让原已计价的 0001-0004 无生效皮重，重算必须失败。
    rows = [{'container': 'TIN-A', 'version': 'TA1', 'mass': '20', 'unit': 'g',
             'start': '2026-11-08T14:00:00+08:00', 'end': '2026-11-08T18:00:00+08:00'}]
    response = _correction(client, sealed, rows)
    assert (response.status_code, response.json['code']) == (422, 'CORRECTION_UNSTABLE')
    assert client.get('/api/books/' + sealed).status_code == 200
    assert client.get('/api/export/' + sealed).status_code == 200
    assert len(client.get('/api/books').json['books']) == 1


def test_overlap_correction_rejected_without_partial_record(client, sealed):
    rows = [{'container': 'TIN-A', 'version': 'TA9', 'mass': '9', 'unit': 'g',
             'start': '2026-11-08T11:00:00+08:00', 'end': '2026-11-08T13:00:00+08:00'}]
    response = _correction(client, sealed, rows)
    assert (response.status_code, response.json['code']) == (422, 'CORRECTION_UNSTABLE')
    assert [item.get('revision', 1) for item in client.get('/api/books').json['books']] == [1]


def test_replacing_window_and_appending_new_version(client, sealed):
    rows = [{'container': 'TIN-A', 'version': 'TA1', 'mass': '20', 'unit': 'g',
             'start': '2026-11-08T08:00:00+08:00', 'end': '2026-11-08T12:00:00+08:00'},
            {'container': 'TIN-A', 'version': 'TA2', 'mass': '21', 'unit': 'g',
             'start': '2026-11-08T12:00:00+08:00', 'end': '2026-11-08T18:00:00+08:00'}]
    response = _correction(client, sealed, rows)
    assert response.status_code == 200
    record = _record(client, response.json['key'])
    # 12:00 及之后出库的 0005、0014 用新版本 21 g；之前沿用 20 g。
    assert _nets(record)['0005'] == 119000 and _nets(record)['0014'] == 179000
    assert [c['action'] for c in response.json['correction']['changes']] == ['replace', 'append']


def test_unknown_version_needs_explicit_window(client, sealed):
    missing = _correction(client, sealed, _replace(version='TX', mass='9'))
    assert missing.json['code'] == 'CORRECTION_INVALID'
    ok = _correction(client, sealed, _replace(version='TX', mass='9') | {
        'start': '2026-11-08T18:00:00+08:00', 'end': '2026-11-08T20:00:00+08:00'})
    assert ok.status_code == 200


def test_bad_correction_payload_is_rejected(client, sealed):
    payloads = [
        None, {}, {'tare': [{'container': 'TIN-A'}]},
        {'tare': [_replace(mass='NULL')]},
        {'tare': [_replace(unit='stone')]},
        {'tare': [_replace() | {'start': '2026-11-08T08:00:00+08:00'}]},
        {'tare': [{'container': 'TIN-A', 'version': 'TA1', 'mass': '9', 'unit': 'g', 'bogus': '1'}]},
        {'tare': [_replace(), _replace()]},
    ]
    for payload in payloads:
        response = client.post(f'/api/books/{sealed}/corrections', json=payload)
        assert (response.status_code, response.json['code']) == (422, 'CORRECTION_INVALID'), payload


def test_correction_requires_known_parent(client):
    assert _correction(client, 'unknown', _replace()).json['code'] == 'BOOK_UNKNOWN'


def test_preview_does_not_issue(client, sealed):
    response = client.post(f'/api/books/{sealed}/corrections/preview', json={'tare': [_replace()]})
    assert response.status_code == 200 and response.json['nets']['0005'] == 118000
    assert len(client.get('/api/books').json['books']) == 1


def test_corrected_export_rehashes(client, sealed):
    key = _correction(client, sealed, _replace()).json['key']
    document = client.get('/api/export/' + key).json
    root = {name: document[name] for name in ('manifest', 'sheets', 'snapshot')}
    assert sha256(encoded(root)).hexdigest() == document['sha256']
    assert document['manifest']['files']['tares'] == sha256(document['sheets']['tares'].encode()).hexdigest()


def test_correction_leaves_parent_and_other_sheets_untouched(client, sealed):
    original = _record(client, sealed)
    key = _correction(client, sealed, _replace()).json['key']
    corrected = _record(client, key)
    assert _record(client, sealed) == original
    for name in ('job', 'weights', 'lots', 'claims'):
        assert corrected['manifest']['files'][name] == original['manifest']['files'][name]
    assert corrected['manifest']['files']['tares'] != original['manifest']['files']['tares']
    # 未改动的皮重行与原件逐字节一致。
    assert corrected['sheets']['tares'].count('\n') == original['sheets']['tares'].count('\n')


def test_concurrent_identical_correction_issues_once(client, sealed):
    def submit(_):
        return _correction(client, sealed, _replace()).json

    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(submit, range(4)))
    assert len({r['key'] for r in results}) == 1
    assert sum(r['reused'] for r in results) >= 3
    assert len(client.get('/api/books').json['books']) == 2


def test_noop_correction_reuses_current_version(client, sealed):
    # 同一物理皮重换个单位写法重交：不签发新一版。
    same = _correction(client, sealed, _replace(mass='20000', unit='mg'))
    assert (same.json['key'], same.json['reused'], same.json['manifest']['revision']) == (sealed, True, 1)
    assert len(client.get('/api/books').json['books']) == 1


def test_partially_redundant_correction_keys_only_real_changes(client, sealed):
    rows = [{'container': 'TIN-A', 'version': 'TA1', 'mass': '20', 'unit': 'g'},
            {'container': 'TIN-B', 'version': 'TB1', 'mass': '12', 'unit': 'g'}]
    response = _correction(client, sealed, rows)
    assert response.status_code == 200
    # 未变的 TA1 不写进证据，也不参与修订键。
    assert [(c['action'], c['container']) for c in response.json['correction']['changes']] == [('replace', 'TIN-B')]
    only_b = _correction(client, sealed, {'container': 'TIN-B', 'version': 'TB1', 'mass': '12', 'unit': 'g'})
    assert only_b.json['key'] == response.json['key']


def test_newly_priced_ticket_renders_in_correction(client, sealed):
    # 0010 原本皮重重叠被隔离，缩窄 TB2 区间后恢复计价；收据页要能显示这一类「无前值」。
    rows = [{'container': 'TIN-B', 'version': 'TB2', 'start': '2026-11-08T14:00:00+08:00',
             'end': '2026-11-08T15:00:00+08:00'}]
    response = _correction(client, sealed, rows)
    assert response.status_code == 200
    affected = response.json['correction']['affected']
    assert [(t['ticket'], t['net_before_mg'], t['net_after_mg']) for t in affected['tickets']] == [('0010', None, 3000)]
    page = client.get('/studio/receipt?key=' + response.json['key'])
    assert page.status_code == 200 and 'B08 / 0010' in page.get_data(as_text=True)


def test_sheet_formatting_is_preserved(client, sealed):
    from app.ledger.correction import apply, parse
    source = ('\ufeffmass,unit,container,version,start,end\r\n'
              '20,g,TIN-A,TA1,2026-11-08T08:00:00+08:00,2026-11-08T18:00:00+08:00\r\n'
              '10,g,TIN-B,TB1,2026-11-08T08:00:00+08:00,2026-11-08T18:00:00+08:00\r\n')
    changes, _ = parse({'tare': {'container': 'TIN-A', 'version': 'TA1', 'mass': '22', 'unit': 'g'}})
    sheets, evidence, _, changed = apply({'tares': source}, changes)
    result = sheets['tares']
    # BOM、CRLF 与换列顺序都保留，仅目标单元格改写。
    assert result.startswith('\ufeffmass,unit,container,version,start,end\r\n')
    assert '22,g,TIN-A,TA1' in result and '10,g,TIN-B,TB1' in result
    assert result.count('\r\n') == 3 and changed


def test_lineage_lists_revision_chain(client, sealed):
    second = _correction(client, sealed, _replace()).json['key']
    third = _correction(client, second, _replace(container='TIN-B', version='TB1', mass='12')).json['key']
    family = client.get(f'/api/books/{third}/revisions').json['revisions']
    assert [(m['revision'], m['key']) for m in family] == [(1, sealed), (2, second), (3, third)]
    assert [m['correction']['parent'] for m in family[1:]] == [sealed, second]
