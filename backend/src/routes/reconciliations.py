import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func

from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import (
    Dataset,
    ReconciliationAdjudication,
    ReconciliationCase,
    ReconciliationMerge,
    ReconciliationPublish,
)
from src.schemas.reconciliation import (
    AdjudicationInput,
    CaseInput,
    CopyInput,
    PublishInput,
)
from src.services import reconciliation as service
from src.services.audit import record

router = APIRouter(prefix='/reconciliations')


def _merge_stale(case, merge):
    return bool(
        merge
        and (
            merge.finance_revision != case.finance_revision
            or merge.channel_revision != case.channel_revision
            or merge.case_version != case.case_version
        )
    )


def _display_status(case, merge):
    """One business-facing state: closed / conflict / stale / pending publish."""
    if case.status == 'closed':
        return 'closed'
    stale = _merge_stale(case, merge)
    if not merge or stale:
        return 'stale'
    if merge.status == 'running':
        return 'merging'
    if merge.status != 'completed':
        return 'stale'
    return 'conflict' if merge.conflicts else 'ready'


def _case_dict(case, merge=None):
    pending = list(merge.conflicts or []) if merge and merge.status == 'completed' and not _merge_stale(case, merge) else []
    return {
        'id': case.id,
        'name': case.name,
        'source_dataset_id': case.source_dataset_id,
        'base_checksum': case.base_checksum,
        'finance_revision': case.finance_revision,
        'channel_revision': case.channel_revision,
        'case_version': case.case_version,
        'status': case.status,
        'display_status': _display_status(case, merge),
        'merge_stale': _merge_stale(case, merge),
        'pending_conflicts': len(pending),
        'published_dataset_id': case.published_dataset_id,
        'created_at': case.created_at,
        'updated_at': case.updated_at,
        'published_at': case.published_at,
        'base_rows': len(case.base_records),
        'finance_rows': len(case.finance_records),
        'channel_rows': len(case.channel_records),
    }


def _merge_dict(merge):
    return {
        'id': merge.id,
        'case_id': merge.case_id,
        'status': merge.status,
        'finance_revision': merge.finance_revision,
        'channel_revision': merge.channel_revision,
        'case_version': merge.case_version,
        'stats': merge.stats,
        'digest': merge.digest,
        'records_checksum': merge.records_checksum,
        'stale_judgements': merge.stale_judgements,
        'applied_judgements': merge.applied_judgements,
        'error': merge.error,
        'created_at': merge.created_at,
        'finished_at': merge.finished_at,
    }


def _adjudication_dict(item):
    return {
        'id': item.id,
        'case_id': item.case_id,
        'row': {'day': item.row_day, 'channel': item.row_channel},
        'field': item.field,
        'kind': item.kind,
        'base': item.base_value,
        'finance': item.finance_value,
        'channel': item.channel_value,
        'resolution': item.resolution,
        'note': item.note,
        'status': item.status,
        'created_at': item.created_at,
        'voided_at': item.voided_at,
        'void_reason': item.void_reason,
    }


def _publish_dict(receipt, dataset):
    return {
        'id': receipt.id,
        'case_id': receipt.case_id,
        'merge_id': receipt.merge_id,
        'dataset_id': receipt.dataset_id,
        'batch_name': dataset.name if dataset else '',
        'batch_checksum': dataset.checksum if dataset else '',
        'batch_rows': len(dataset.records) if dataset else 0,
        'created_at': receipt.created_at,
    }


@router.get('')
def listing(db=Depends(get_db), user=Depends(current_user)):
    latest = {}
    for merge in db.scalars(select(ReconciliationMerge).order_by(ReconciliationMerge.id)):
        latest[merge.case_id] = merge
    return [
        _case_dict(case, latest.get(case.id))
        for case in db.scalars(select(ReconciliationCase).order_by(ReconciliationCase.id.desc()))
    ]


@router.post('', status_code=201)
def create_case(data: CaseInput, db=Depends(get_db), user=Depends(can_write)):
    source = db.get(Dataset, data.dataset_id)
    if not source:
        raise HTTPException(404, '源账批次不存在')
    base = service.copy_records(source.records)
    finance = service.normalise_copy(data.finance_records) if data.finance_records else service.copy_records(base)
    channel = service.normalise_copy(data.channel_records) if data.channel_records else service.copy_records(base)
    case = ReconciliationCase(
        name=data.name.strip(),
        source_dataset_id=source.id,
        base_records=base,
        finance_records=finance,
        channel_records=channel,
        base_checksum=source.checksum,
        finance_revision=1,
        channel_revision=1,
        case_version=0,
        status='open',
        created_by=user.id,
        created_at=time.time(),
        updated_at=time.time(),
    )
    db.add(case)
    db.flush()
    record(db, user.email, 'reconciliation.create', {'id': case.id, 'source_dataset': source.id, 'rows': len(base)})
    db.commit()
    return _case_dict(case)


@router.get('/{case_id}')
def detail(case_id: int, db=Depends(get_db), user=Depends(current_user)):
    case = service.load_case(db, case_id)
    source = db.get(Dataset, case.source_dataset_id)
    merge = db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case.id)
        .order_by(ReconciliationMerge.id.desc())
    )
    judgements = list(
        db.scalars(
            select(ReconciliationAdjudication)
            .where(ReconciliationAdjudication.case_id == case.id)
            .order_by(ReconciliationAdjudication.id.desc())
        )
    )
    receipt = db.scalar(
        select(ReconciliationPublish)
        .where(ReconciliationPublish.case_id == case.id, ReconciliationPublish.status == 'completed')
        .order_by(ReconciliationPublish.id.desc())
    )
    dataset = db.get(Dataset, receipt.dataset_id) if receipt else None
    merge_stale = _merge_stale(case, merge)
    return {
        **_case_dict(case, merge),
        'source': {
            'id': source.id,
            'name': source.name,
            'checksum': source.checksum,
            'rows': len(source.records),
            'created_at': source.created_at,
        }
        if source
        else None,
        'base_records': case.base_records,
        'finance_records': case.finance_records,
        'channel_records': case.channel_records,
        'merge': _merge_dict(merge) if merge else None,
        'merge_stale': merge_stale,
        'pending_conflicts': len((merge.conflicts or []) if merge and merge.status == 'completed' and not merge_stale else []),
        'conflicts': (merge.conflicts or []) if merge and merge.status == 'completed' and not merge_stale else [],
        'adjudications': [_adjudication_dict(item) for item in judgements],
        'publish': _publish_dict(receipt, dataset) if receipt else None,
    }


@router.put('/{case_id}/copies')
def update_copy(case_id: int, data: CopyInput, db=Depends(get_db), user=Depends(can_write)):
    case, voided = service.touch_copy(db, case_id, data.side, data.records, data.expected_revision, user)
    record(
        db,
        user.email,
        'reconciliation.copy.edit',
        {'id': case.id, 'side': data.side, 'revision': service.side_revision(case, data.side), 'voided_judgements': voided},
    )
    db.commit()
    merge_row = db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case.id)
        .order_by(ReconciliationMerge.id.desc())
    )
    return {
        **_case_dict(case, merge_row),
        'voided_judgements': voided,
        'merge': _merge_dict(merge_row) if merge_row else None,
        'finance_records': case.finance_records,
        'channel_records': case.channel_records,
    }


@router.post('/{case_id}/merge', status_code=201)
def merge(case_id: int, db=Depends(get_db), user=Depends(can_write)):
    merge_row = service.start_merge(db, case_id, user)
    case = service.load_case(db, case_id)
    if merge_row.status == 'failed':
        record(db, user.email, 'reconciliation.merge.failed', {'id': case.id, 'merge': merge_row.id, 'error': merge_row.error})
        db.commit()
        raise HTTPException(409, merge_row.error or '合并未能完成')
    if merge_row.conflicts:
        case.status = 'open'
    db.flush()
    record(db, user.email, 'reconciliation.merge', {'id': case.id, 'merge': merge_row.id, 'conflicts': len(merge_row.conflicts or []), 'status': merge_row.status})
    db.commit()
    return {
        'merge': _merge_dict(merge_row),
        'conflicts': merge_row.conflicts or [],
        'stats': merge_row.stats or {},
        'case': _case_dict(case, merge_row),
    }


@router.post('/{case_id}/adjudications', status_code=201)
def adjudicate(case_id: int, data: AdjudicationInput, db=Depends(get_db), user=Depends(admin_only)):
    item, merge_row = service.adjudicate(db, case_id, data, user)
    record(
        db,
        user.email,
        'reconciliation.adjudicate',
        {'id': case_id, 'row': {'day': item.row_day, 'channel': item.row_channel}, 'field': item.field, 'resolution': item.resolution},
    )
    db.commit()
    return {
        'adjudication': _adjudication_dict(item),
        'merge': _merge_dict(merge_row),
        'conflicts': merge_row.conflicts or [],
        'case': _case_dict(service.load_case(db, case_id), merge_row),
    }


@router.post('/{case_id}/publish', status_code=201)
def publish(case_id: int, data: PublishInput, db=Depends(get_db), user=Depends(admin_only)):
    receipt, dataset, replayed = service.publish(db, case_id, data.merge_id, user)
    if not replayed:
        record(
            db,
            user.email,
            'reconciliation.publish',
            {'id': case_id, 'merge': receipt.merge_id, 'dataset': dataset.id, 'checksum': dataset.checksum},
        )
    db.commit()
    case = service.load_case(db, case_id)
    final_merge = db.get(ReconciliationMerge, receipt.merge_id)
    return {
        'publish': _publish_dict(receipt, dataset),
        'case': _case_dict(case, final_merge),
        'replayed': replayed,
    }


@router.post('/{case_id}/resume', status_code=201)
def resume(case_id: int, db=Depends(get_db), user=Depends(can_write)):
    service.load_case(db, case_id)
    resumed = service.resume_incomplete_merges(db, case_id)
    latest = db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case_id)
        .order_by(ReconciliationMerge.id.desc())
    )
    if latest:
        record(db, user.email, 'reconciliation.resume', {'id': case_id, 'merge': latest.id, 'resumed': resumed})
        db.commit()
    return {
        'resumed': resumed,
        'merge': _merge_dict(latest) if latest else None,
        'conflicts': (latest.conflicts or []) if latest else [],
    }
