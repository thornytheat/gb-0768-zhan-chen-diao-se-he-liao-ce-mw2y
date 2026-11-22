from flask import Blueprint,render_template,request

from app.api import bookshelf
from app.faults import LedgerError,REASONS
from app.job import MixingJob
from app.ledger.signoff import certify
from app.receipts.clock import studio_label
from app.receipts.packet import read_sample
from app.receipts.units import grams
from app.settings import EDITION

LINKS={'weighings':'称量原件','tares':'容器皮重','lots':'颜料批号','recovery':'回桶分料',
       'balance':'库存核料','history':'工单留档','receipt':'证据签发'}
ACTIONS={'OPEN':'期初','ISSUE':'领用','APPLY':'上件','LOSS':'损耗','RETURN':'回桶'}
CORRECTION_ACTIONS={'replace':'改写', 'append':'补报'}
pages=Blueprint('studio',__name__)


@pages.get('/')
@pages.get('/studio/<screen>')
def desk(screen='overview'):
    if screen!='overview' and screen not in LINKS:
        raise LedgerError('STAGE_ABSENT','没有这项核料工序',404)
    sample=request.args.get('sample','dirty')
    key=request.args.get('key')
    receipt=None
    lineage=[]
    correction=None
    if key:
        record=bookshelf().require(key)
        data,meta=record['snapshot'],record['manifest']
        sample=meta['source'] if meta['source']!='upload' else sample
        if screen=='receipt':
            receipt=certify(record)
        lineage=bookshelf().lineage(key)
        correction=meta.get('correction')
    else:
        job=MixingJob(read_sample(sample))
        data,meta=job.snapshot(),{'order':job.job.order,'edition':EDITION,'cutoff':job.job.cutoff.isoformat()}
    history=bookshelf().listing() if screen=='history' else []
    rejects=[]
    if screen=='weighings':
        rejects=data[screen]['isolated']+[row for item in data[screen]['sheets'] for row in item['isolated']]
    if screen=='tares':
        rejects=data[screen]['isolated']+data[screen]['invalid_versions']
    if screen=='lots':
        rejects=data[screen]['isolated']+data[screen]['invalid_catalog']
    if screen=='recovery':
        rejects=data[screen]['isolated']
    if screen=='balance':
        rejects=data[screen]['invalid_receipts']
    can_correct=False
    tare_choices={}
    if key and screen=='tares':
        # 票行本身只有秤单列；生效皮重版本挂在 tares 证据源上，隔离版本另有原行。
        sources=[source for row in data['tares']['rows']+data['tares']['isolated'] for source in row.get('tares',[])]
        sources+=data['tares']['invalid_versions']
        for row in sources:
            container=row['data'].get('container','').strip()
            version=row['data'].get('version','').strip()
            if container and version:
                tare_choices.setdefault(container,[])
                if version not in tare_choices[container]:
                    tare_choices[container].append(version)
        can_correct=bool(tare_choices)
    return render_template('base.html',screen=screen,links=LINKS,sample=sample,key=key,data=data,meta=meta,
                           receipt=receipt,history=history,labels=REASONS,actions=ACTIONS,
                           correction_actions=CORRECTION_ACTIONS,grams=grams,studio_label=studio_label,
                           rejects=rejects,lineage=lineage,correction=correction,can_correct=can_correct,
                           tare_choices=tare_choices)
