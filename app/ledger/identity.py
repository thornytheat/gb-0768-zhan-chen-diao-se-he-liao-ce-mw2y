from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter

from app.receipts.packet import checksums
from app.settings import EDITION


def encoded(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()


def seal_id(files):
    hashes=checksums(files)
    return sha256(encoded({'edition':EDITION,'files':hashes})).hexdigest(),hashes


def revision_id(parent_key, change):
    # 更正留档键由父留档与规范化后的皮重差异决定：重试同一更正恒得同键。
    return sha256(encoded({'edition':EDITION,'parent':parent_key,'correction':change})).hexdigest()


def docket(job, key, source, hashes, started, revision=1, root=None, correction=None):
    manifest={'key':key,'order':job.order,'source':source,'edition':EDITION,'files':hashes,
              'revision':revision,'root':root or key,
              'issued_at':datetime.now(UTC).isoformat(timespec='seconds'), 'elapsed_ms':round((perf_counter()-started)*1000),
              'start':job.start.isoformat(),'end':job.end.isoformat(),'cutoff':job.cutoff.isoformat()}
    if correction is not None:
        # 原件摘要保持可复核；更正收据另登记修订链与差异证据。
        manifest['correction']=correction
    return manifest
