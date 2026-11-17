"""Three-way cell merge for finance / channel reconciliation copies.

The common ledger (``base_records``) is immutable.  Both sides edit their own
complete copy; the merge compares each cell against the base so that changes on
different fields of the same row can be combined, while same-cell edits and
delete/modify collisions become adjudication items for the business owner.
"""
import copy
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import (
    Dataset,
    ReconciliationAdjudication,
    ReconciliationCase,
    ReconciliationMerge,
    ReconciliationPublish,
)
from src.services.business import DEFAULT_EXPRESSION, analyze, validate_ledger
from src.utils.serialization import checksum

COLUMN_KEYS = ["day", "channel", "currency", "gross", "refunds", "discount", "recognized_net", "visits"]
KEY_FIELDS = ("day", "channel")
NUMERIC_FIELDS = ("gross", "refunds", "discount", "recognized_net", "visits")


def key_of(row):
    return (str(row.get("day", "")).strip(), str(row.get("channel", "")).strip())


def row_key_text(key):
    return {"day": key[0], "channel": key[1]}


def index_rows(records):
    return {key_of(row): row for row in records}


def copy_records(records):
    return copy.deepcopy(list(records))


def side_records(case, side):
    return case.finance_records if side == "finance" else case.channel_records


def side_revision(case, side):
    return case.finance_revision if side == "finance" else case.channel_revision


def load_case(db, case_id, lock=False):
    case = db.get(ReconciliationCase, case_id)
    if case is None:
        raise HTTPException(404, "对账副本不存在")
    if lock:
        db.refresh(case, with_for_update=True)
    return case


def normalise_row(row):
    """Cast contract fields so string inputs from the UI merge cleanly."""
    clean = {}
    for key in COLUMN_KEYS:
        value = row.get(key)
        if value in (None, ""):
            raise ValueError(f"字段 {key} 不能为空")
        if key in NUMERIC_FIELDS:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"字段 {key} 必须为数值") from None
            if key == "visits" and number.is_integer():
                clean[key] = int(number)
            else:
                clean[key] = number
        else:
            clean[key] = str(value).strip()
    return clean


def normalise_copy(records):
    """Structural validation for an editable copy; full validity is checked on publish."""
    if not isinstance(records, list) or not records:
        raise HTTPException(422, "副本账本不能为空")
    normalised, seen = [], set()
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            raise HTTPException(422, f"第 {index + 1} 行不是有效记录")
        try:
            clean = normalise_row(row)
        except ValueError as exc:
            raise HTTPException(422, f"第 {index + 1} 行：{exc}") from None
        key = key_of(clean)
        if not all(key):
            raise HTTPException(422, f"第 {index + 1} 行日期和渠道不能为空")
        if key in seen:
            raise HTTPException(422, f"第 {index + 1} 行自然键重复：{key[0]} / {key[1]}")
        seen.add(key)
        normalised.append(clean)
    return normalised


def _cell_conflict(key, field, base_value, finance_value, channel_value):
    return {
        "row": row_key_text(key),
        "field": field,
        "kind": "cell",
        "base": base_value,
        "finance": finance_value,
        "channel": channel_value,
    }


def _row_conflict(key, base_value, finance_value, channel_value):
    return {
        "row": row_key_text(key),
        "field": None,
        "kind": "delete_modify",
        "base": base_value,
        "finance": finance_value,
        "channel": channel_value,
    }


def _active_judgements(db, case_id):
    return list(
        db.scalars(
            select(ReconciliationAdjudication)
            .where(
                ReconciliationAdjudication.case_id == case_id,
                ReconciliationAdjudication.status == "active",
            )
            .order_by(ReconciliationAdjudication.id)
        )
    )


def _judgement_index(judgements):
    cells, rows = {}, {}
    for item in judgements:
        if item.kind == "cell":
            cells[(item.row_day, item.row_channel, item.field)] = item
        else:
            rows[(item.row_day, item.row_channel)] = item
    return cells, rows


def _same_snapshot(item, base_value, finance_value, channel_value):
    """A judgement only applies while the snapshots it was made against survive."""
    return (
        item.base_value == base_value
        and item.finance_value == finance_value
        and item.channel_value == channel_value
    )


def compute_merge(case, judgements):
    """Return (records, conflicts, stats, applied_ids, stale_ids)."""
    base = index_rows(case.base_records)
    finance = index_rows(case.finance_records)
    channel = index_rows(case.channel_records)
    cell_j, row_j = _judgement_index(judgements)

    keys = list(dict.fromkeys([*base.keys(), *finance.keys(), *channel.keys()]))
    merged, conflicts = [], []
    applied, stale = [], []
    stats = {
        "rows_base": len(base),
        "rows_finance": len(finance),
        "rows_channel": len(channel),
        "cells_from_finance": 0,
        "cells_from_channel": 0,
        "cells_adjudicated": 0,
        "rows_added": 0,
        "rows_deleted": 0,
        "conflicts": 0,
    }

    for key in sorted(keys):
        b = base.get(key)
        f = finance.get(key)
        c = channel.get(key)

        if b is None:
            if f is not None and c is not None:
                resolved, row_conflicts = _merge_new_row(key, f, c, cell_j, applied, stale)
                if resolved is not None:
                    stats["rows_added"] += 1
                    merged.append(resolved)
                conflicts.extend(row_conflicts)
                continue
            if f is not None:
                stats["rows_added"] += 1
                stats["cells_from_finance"] += len(COLUMN_KEYS)
                merged.append(copy.deepcopy(f))
            else:
                stats["rows_added"] += 1
                stats["cells_from_channel"] += len(COLUMN_KEYS)
                merged.append(copy.deepcopy(c))
            continue

        if f is None and c is None:
            stats["rows_deleted"] += 1
            continue

        if f is None or c is None:
            kept_side = "finance" if f is not None else "channel"
            kept_row = f if f is not None else c
            if kept_row == b:
                # the other side deleted a row nobody modified: deletion stands
                stats["rows_deleted"] += 1
                continue
            guess = row_j.get(key)
            if guess is not None and _same_snapshot(
                guess, copy.deepcopy(b), copy.deepcopy(f), copy.deepcopy(c)
            ):
                applied.append(guess.id)
                if guess.resolution != kept_side:
                    stats["rows_deleted"] += 1
                    continue
                stats["cells_adjudicated"] += len(COLUMN_KEYS)
                merged.append(copy.deepcopy(kept_row))
                continue
            if guess is not None:
                stale.append(guess.id)
            conflicts.append(
                _row_conflict(key, copy.deepcopy(b), copy.deepcopy(f), copy.deepcopy(c))
            )
            merged.append(copy.deepcopy(b))  # placeholder until the owner decides
            continue

        resolved_row = {}
        for field in COLUMN_KEYS:
            bv, fv, cv = b.get(field), f.get(field), c.get(field)
            if field in KEY_FIELDS:
                resolved_row[field] = fv
                continue
            if fv == bv and cv == bv:
                resolved_row[field] = bv
            elif fv == bv:
                resolved_row[field] = cv
                stats["cells_from_channel"] += 1
            elif cv == bv:
                resolved_row[field] = fv
                stats["cells_from_finance"] += 1
            elif fv == cv:
                resolved_row[field] = fv
            else:
                guess = cell_j.get((key[0], key[1], field))
                if guess is not None and _same_snapshot(guess, bv, fv, cv):
                    applied.append(guess.id)
                    stats["cells_adjudicated"] += 1
                    resolved_row[field] = fv if guess.resolution == "finance" else cv
                else:
                    if guess is not None:
                        stale.append(guess.id)
                    conflicts.append(_cell_conflict(key, field, bv, fv, cv))
                    resolved_row[field] = bv  # placeholder until the owner decides
        merged.append(resolved_row)

    conflicts.sort(key=lambda item: (item["row"]["day"], item["row"]["channel"], item["field"] or ""))
    stats["conflicts"] = len(conflicts)
    return merged, conflicts, stats, sorted(set(applied)), sorted(set(stale))


def _merge_new_row(key, f, c, cell_j, applied, stale):
    resolved, conflicts = {}, []
    for field in COLUMN_KEYS:
        fv, cv = f.get(field), c.get(field)
        if fv == cv:
            resolved[field] = fv
            continue
        guess = cell_j.get((key[0], key[1], field))
        if guess is not None and _same_snapshot(guess, None, fv, cv):
            applied.append(guess.id)
            resolved[field] = fv if guess.resolution == "finance" else cv
        else:
            if guess is not None:
                stale.append(guess.id)
            conflicts.append(_cell_conflict(key, field, None, fv, cv))
            resolved[field] = fv
    return resolved, conflicts


def merge_digest(records, conflicts):
    return checksum({"records": records, "conflicts": conflicts})


def _new_merge(case, user):
    return ReconciliationMerge(
        case_id=case.id,
        status="running",
        finance_revision=case.finance_revision,
        channel_revision=case.channel_revision,
        case_version=case.case_version,
        records=[],
        conflicts=[],
        stats={},
        digest="",
        records_checksum="",
        applied_judgements=[],
        stale_judgements=[],
        error="",
        created_by=user.id,
        created_at=time.time(),
    )


def _run_merge(db, case, merge):
    """Execute a merge row committed as ``running`` (durable resume point)."""
    if (
        merge.finance_revision != case.finance_revision
        or merge.channel_revision != case.channel_revision
        or merge.case_version != case.case_version
    ):
        merge.status = "failed"
        merge.error = "合并期间副本或裁决已更新，本次合并快照过期；过期合并拒绝发布，请重新合并"
        merge.finished_at = time.time()
        return merge
    judgements = _active_judgements(db, case.id)
    records, conflicts, stats, applied, stale = compute_merge(case, judgements)
    for item in judgements:
        if item.id in stale:
            # The decision no longer describes the current snapshot: retire it so
            # it cannot silently influence a later merge.
            item.status = "voided"
            item.voided_at = time.time()
            item.void_reason = "副本内容已变化，原裁决与新快照不符，已失效"
    db.flush()
    merge.records = records
    merge.conflicts = conflicts
    merge.stats = stats
    merge.digest = merge_digest(records, conflicts)
    merge.records_checksum = checksum(records)
    merge.applied_judgements = applied
    merge.stale_judgements = stale
    merge.status = "completed"
    merge.finished_at = time.time()
    merge.error = ""
    if case.status != "closed":
        case.status = "open" if conflicts else "ready"
    return merge


def start_merge(db, case_id, user):
    """Create or resume a merge for the current revisions (retry never duplicates)."""
    case = load_case(db, case_id, lock=True)
    if case.status == "closed":
        raise HTTPException(409, "该对账已结案，不能再次合并")
    latest = db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case.id)
        .order_by(ReconciliationMerge.id.desc())
    )
    if (
        latest
        and latest.finance_revision == case.finance_revision
        and latest.channel_revision == case.channel_revision
        and latest.case_version == case.case_version
    ):
        if latest.status == "running":
            return _run_merge(db, case, latest)
        if latest.status == "completed":
            return latest
        # A failed row for the same revisions stays as an audit trail; a retry
        # creates a fresh merge rather than reusing an unpublishable snapshot.
    merge = _new_merge(case, user)
    db.add(merge)
    # Commit the running row before computing: a crash mid-merge leaves a
    # durable resume point instead of a half-written result.
    db.commit()
    return _run_merge(db, case, merge)


def compute_merge_in_transaction(db, case, user):
    """Merge inside the caller's transaction (used by the idempotent demo seed)."""
    merge = _new_merge(case, user)
    db.add(merge)
    db.flush()
    return _run_merge(db, case, merge)


def resume_incomplete_merges(db, case_id=None):
    """Recover merges interrupted mid-flight; stale snapshots fail instead of merging late."""
    query = select(ReconciliationMerge).where(ReconciliationMerge.status == "running")
    if case_id is not None:
        query = query.where(ReconciliationMerge.case_id == case_id)
    pending = list(db.scalars(query.order_by(ReconciliationMerge.id)))
    resumed = []
    for merge in pending:
        case = db.get(ReconciliationCase, merge.case_id)
        if case is None:
            merge.status = "failed"
            merge.error = "对账副本已不存在"
            merge.finished_at = time.time()
        else:
            _run_merge(db, case, merge)
        resumed.append(merge.id)
    if pending:
        db.commit()
    return resumed


def _side_value_before(item, old_rows):
    key = (item.row_day, item.row_channel)
    row = old_rows.get(key)
    if row is None:
        return None
    return row.get(item.field) if item.kind == "cell" else copy.deepcopy(row)


def _side_value_after(item, new_rows):
    key = (item.row_day, item.row_channel)
    row = new_rows.get(key)
    if row is None:
        return None
    return row.get(item.field) if item.kind == "cell" else copy.deepcopy(row)


def void_stale_judgements(db, case, side, old_rows, new_rows):
    """Copy edits void adjudications whose edited-side value actually moved."""
    snapshot_attr = "finance_value" if side == "finance" else "channel_value"
    voided = []
    for item in _active_judgements(db, case.id):
        before = _side_value_before(item, old_rows)
        after = _side_value_after(item, new_rows)
        if before == after:
            continue
        if getattr(item, snapshot_attr) == after:
            continue  # reverted exactly to the adjudicated value: decision still describes it
        item.status = "voided"
        item.voided_at = time.time()
        item.void_reason = f"{'财务' if side == 'finance' else '渠道'}副本再次修改，原裁决失效"
        voided.append(item.id)
    return voided


def touch_copy(db, case_id, side, records, expected_revision, user):
    """Replace one side's complete copy and void adjudications it invalidates."""
    if side not in ("finance", "channel"):
        raise HTTPException(422, "只能编辑财务或渠道副本")
    case = load_case(db, case_id, lock=True)
    if case.status == "closed":
        raise HTTPException(409, "该对账已结案，原账与副本不再接受修改")
    clean = normalise_copy(records)
    revision = side_revision(case, side)
    if expected_revision is not None and expected_revision != revision:
        raise HTTPException(409, "该副本已被其他人更新，请刷新后再提交")
    old_rows = index_rows(side_records(case, side))
    new_rows = index_rows(clean)
    if side == "finance":
        case.finance_records = clean
        case.finance_revision = revision + 1
    else:
        case.channel_records = clean
        case.channel_revision = revision + 1
    case.case_version += 1
    case.status = "open"
    case.updated_at = time.time()
    db.flush()
    voided = void_stale_judgements(db, case, side, old_rows, new_rows)
    db.flush()
    if voided:
        # A voided decision must not linger as if it still applied: recompute the
        # merge at the new revisions so the affected conflicts reappear.
        merge = _new_merge(case, user)
        db.add(merge)
        db.flush()
        _run_merge(db, case, merge)
        db.flush()
    return case, voided


def adjudicate(db, case_id, payload, user):
    """Record an owner decision for a currently open conflict and re-merge."""
    case = load_case(db, case_id, lock=True)
    if case.status == "closed":
        raise HTTPException(409, "该对账已结案，不能再裁决")
    latest = db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case.id, ReconciliationMerge.status == "completed")
        .order_by(ReconciliationMerge.id.desc())
    )
    if (
        not latest
        or latest.finance_revision != case.finance_revision
        or latest.channel_revision != case.channel_revision
        or latest.case_version != case.case_version
    ):
        raise HTTPException(409, "当前副本尚无最新合并结果，请先执行合并再裁决")
    target = next(
        (
            item
            for item in latest.conflicts
            if item["row"]["day"] == payload.row_day
            and item["row"]["channel"] == payload.row_channel
            and (item["field"] or None) == (payload.field or None)
        ),
        None,
    )
    if not target:
        raise HTTPException(409, "该冲突已不存在，请刷新后重新查看待裁决项")
    if payload.resolution not in ("finance", "channel"):
        raise HTTPException(422, "裁决只能选择财务侧或渠道侧版本")
    existing = db.scalar(
        select(ReconciliationAdjudication).where(
            ReconciliationAdjudication.case_id == case.id,
            ReconciliationAdjudication.row_day == payload.row_day,
            ReconciliationAdjudication.row_channel == payload.row_channel,
            ReconciliationAdjudication.field == payload.field,
            ReconciliationAdjudication.status == "active",
        )
    )
    if existing:
        existing.status = "voided"
        existing.voided_at = time.time()
        existing.void_reason = "负责人重新裁决"
        db.flush()
    item = ReconciliationAdjudication(
        case_id=case.id,
        row_day=payload.row_day,
        row_channel=payload.row_channel,
        field=payload.field,
        kind=target["kind"],
        base_value=target["base"],
        finance_value=target["finance"],
        channel_value=target["channel"],
        resolution=payload.resolution,
        note=payload.note or "",
        status="active",
        case_version=case.case_version,
        created_by=user.id,
        created_at=time.time(),
    )
    db.add(item)
    db.flush()
    merge = _new_merge(case, user)
    db.add(merge)
    db.flush()
    _run_merge(db, case, merge)
    return item, merge


def revision_key(case):
    return (case.finance_revision, case.channel_revision, case.case_version)


def _publish_key(case, merge):
    return f"case:{case.id}:merge:{merge.id}:digest:{merge.digest}"


def latest_completed_merge(db, case_id):
    return db.scalar(
        select(ReconciliationMerge)
        .where(ReconciliationMerge.case_id == case_id, ReconciliationMerge.status == "completed")
        .order_by(ReconciliationMerge.id.desc())
    )


def publish(db, case_id, merge_id, user):
    """All-or-nothing close + analyzable batch creation, idempotent on retry."""
    case = load_case(db, case_id, lock=True)
    if case.status == "closed":
        receipt = db.scalar(
            select(ReconciliationPublish)
            .where(ReconciliationPublish.case_id == case.id, ReconciliationPublish.status == "completed")
            .order_by(ReconciliationPublish.id.desc())
        )
        if receipt:
            return receipt, db.get(Dataset, receipt.dataset_id), True
        raise HTTPException(409, "该对账已结案，但没有可追溯的发布记录")
    if merge_id:
        merge = db.get(ReconciliationMerge, merge_id)
        if not merge or merge.case_id != case.id:
            raise HTTPException(409, "请先执行合并，再发布")
        if (
            merge.finance_revision != case.finance_revision
            or merge.channel_revision != case.channel_revision
            or merge.case_version != case.case_version
        ):
            raise HTTPException(409, "合并结果已过期（副本或裁决在此后发生变化），过期合并拒绝发布，请重新合并")
        if merge.status != "completed":
            raise HTTPException(409, "合并尚未完成，不能发布")
    else:
        # Publish/reconcile race: always evaluate the complete current versions
        # of both side copies instead of an outdated merged snapshot.
        merge = latest_completed_merge(db, case.id)
        if (
            not merge
            or merge.finance_revision != case.finance_revision
            or merge.channel_revision != case.channel_revision
            or merge.case_version != case.case_version
        ):
            merge = _new_merge(case, user)
            db.add(merge)
            db.flush()
            _run_merge(db, case, merge)
            if merge.status != "completed":
                raise HTTPException(409, merge.error or "合并未能完成，不能发布")
    if merge.conflicts:
        raise HTTPException(409, f"仍有 {len(merge.conflicts)} 项待裁决，不能发布")

    key = _publish_key(case, merge)
    receipt = db.scalar(
        select(ReconciliationPublish).where(ReconciliationPublish.idempotency_key == key)
    )
    if receipt:
        return receipt, db.get(Dataset, receipt.dataset_id), True

    records = copy.deepcopy(merge.records)
    issues = validate_ledger(records)
    if issues:
        raise HTTPException(409, "合并后全账无效，拒绝发布：" + "；".join(issues[:5]))
    try:
        analyze(
            records,
            {"expression": DEFAULT_EXPRESSION, "group_by": "channel", "zero_policy": "reject"},
        )
    except ValueError as exc:
        raise HTTPException(409, f"合并后账本不可分析，拒绝发布：{exc}") from None

    name = f"{case.name} · 对账合并 v{case.case_version}"
    dataset = db.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        dataset = Dataset(
            name=name,
            source=f"reconciliation:{case.id}:merge:{merge.id}",
            records=records,
            checksum=checksum(records),
            created_at=time.time(),
        )
        db.add(dataset)
        db.flush()
    receipt = ReconciliationPublish(
        case_id=case.id,
        merge_id=merge.id,
        dataset_id=dataset.id,
        status="completed",
        idempotency_key=key,
        created_by=user.id,
        created_at=time.time(),
    )
    db.add(receipt)
    case.status = "closed"
    case.published_dataset_id = dataset.id
    case.published_at = time.time()
    case.updated_at = time.time()
    try:
        db.flush()
    except IntegrityError:
        # A concurrent publish won the unique key or batch name: replay its receipt
        # instead of creating a second batch.
        db.rollback()
        existing = db.scalar(
            select(ReconciliationPublish).where(
                ReconciliationPublish.case_id == case_id,
                ReconciliationPublish.status == "completed",
            ).order_by(ReconciliationPublish.id.desc())
        )
        if existing:
            return existing, db.get(Dataset, existing.dataset_id), True
        raise HTTPException(409, "发布并发冲突，请刷新后重试") from None
    return receipt, dataset, False
