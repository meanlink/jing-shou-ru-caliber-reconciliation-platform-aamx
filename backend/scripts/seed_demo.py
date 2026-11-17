import copy, time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, ReconciliationCase
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services.reconciliation import compute_merge_in_transaction
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(ReconciliationCase).where(ReconciliationCase.name=='九月两侧校账示例')):
            # Demo case: finance fixes a refund, the channel fixes a visit count on
            # another row; both changes merge cell by cell against the same base.
            admin=db.scalar(select(User).where(User.role=='admin'))
            base=copy.deepcopy(d.records)
            finance=copy.deepcopy(base)
            channel=copy.deepcopy(base)
            finance[0]['refunds']=round(finance[0]['refunds']+12.5,2)
            channel[1]['visits']=int(channel[1]['visits'])+5
            case=ReconciliationCase(name='九月两侧校账示例',source_dataset_id=d.id,base_records=base,finance_records=finance,channel_records=channel,base_checksum=d.checksum,finance_revision=1,channel_revision=1,case_version=0,status='open',created_by=admin.id,created_at=time.time(),updated_at=time.time())
            db.add(case);db.flush()
            compute_merge_in_transaction(db,case,admin)
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,ReconciliationCase]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
