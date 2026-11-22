import json
import os
import re
from tempfile import NamedTemporaryFile

from app.faults import LedgerError
from app.ledger.identity import encoded


class Bookshelf:
    def __init__(self, directory):
        self.directory=directory
        directory.mkdir(parents=True,exist_ok=True)

    def path(self,key):
        if not re.fullmatch('[a-f0-9]{64}',key):
            raise LedgerError('BOOK_UNKNOWN','找不到该收据留档',404)
        return self.directory/f'{key}.receipt'

    def find(self,key):
        try:
            return json.loads(self.path(key).read_bytes())
        except FileNotFoundError:
            return None

    def require(self,key):
        item=self.find(key)
        if item is None:
            raise LedgerError('BOOK_UNKNOWN','找不到该收据留档',404)
        return item

    def write(self,key,payload):
        target=self.path(key)
        with NamedTemporaryFile(dir=self.directory, prefix='.draft-') as temp:
            temp.write(encoded(payload))
            temp.flush()
            os.fsync(temp.fileno())
            try:
                os.link(temp.name,target)
            except FileExistsError:
                return self.require(key),True
            descriptor=os.open(self.directory,os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return payload,False

    def listing(self):
        kept=sorted(self.directory.glob('*.receipt'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        return [self.require(path.stem)['manifest'] for path in kept[:60]]

    def lineage(self,key):
        """同一封存工单的全部修订，根单在前，按修订号与签发次序排列。"""
        root=self.require(key)
        root_key=root['manifest'].get('root',root['manifest']['key'])
        family=[item['manifest']|{'revision':item['manifest'].get('revision',1),
                                  'root':item['manifest'].get('root',item['manifest']['key'])}
                for item in (self.require(p.stem) for p in self.directory.glob('*.receipt'))
                if item['manifest'].get('root',item['manifest']['key'])==root_key]
        family.sort(key=lambda m:(m['revision'],m['issued_at'],m['key']))
        return family

    def next_revision(self,key):
        """新修订在整条沿革中的序号；并发误差只影响编号，不影响收据内容。"""
        return max((m['revision'] for m in self.lineage(key)),default=1)+1
