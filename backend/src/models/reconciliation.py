from typing import Any
from sqlalchemy import String, Double, JSON, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class ReconciliationCase(Base):
    """A reconciliation copy built from one immutable source ledger.

    ``base_records`` is the common ledger both sides started from and is never
    edited.  ``finance_records`` and ``channel_records`` are the two fully
    versioned side copies.
    """

    __tablename__ = 'reconciliation_cases'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    source_dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    base_records: Mapped[list] = mapped_column(JSON)
    finance_records: Mapped[list] = mapped_column(JSON)
    channel_records: Mapped[list] = mapped_column(JSON)
    base_checksum: Mapped[str] = mapped_column(String(64))
    finance_revision: Mapped[int] = mapped_column(Integer, default=0)
    channel_revision: Mapped[int] = mapped_column(Integer, default=0)
    case_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default='open')
    published_dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    published_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class ReconciliationMerge(Base):
    """A durable merge run.  Rows left in ``running`` are resumed on restart."""

    __tablename__ = 'reconciliation_merges'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_cases.id'))
    status: Mapped[str] = mapped_column(String(20), default='running')
    finance_revision: Mapped[int] = mapped_column(Integer, default=0)
    channel_revision: Mapped[int] = mapped_column(Integer, default=0)
    case_version: Mapped[int] = mapped_column(Integer, default=0)
    records: Mapped[list] = mapped_column(JSON, default=list)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(64), default='')
    records_checksum: Mapped[str] = mapped_column(String(64), default='')
    applied_judgements: Mapped[list] = mapped_column(JSON, default=list)
    stale_judgements: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(String(1000), default='')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class ReconciliationAdjudication(Base):
    """Owner decision for one conflicting cell, or for a delete/modify row."""

    __tablename__ = 'reconciliation_adjudications'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_cases.id'))
    row_day: Mapped[str] = mapped_column(String(40))
    row_channel: Mapped[str] = mapped_column(String(80))
    field: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    base_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    finance_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    channel_value: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    resolution: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(500), default='')
    status: Mapped[str] = mapped_column(String(20), default='active')
    case_version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    voided_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    void_reason: Mapped[str] = mapped_column(String(300), default='')


class ReconciliationPublish(Base):
    """Idempotent publish receipt; unique key + unique batch name stop duplicates."""

    __tablename__ = 'reconciliation_publishes'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_cases.id'))
    merge_id: Mapped[int] = mapped_column(ForeignKey('reconciliation_merges.id'))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    status: Mapped[str] = mapped_column(String(20), default='completed')
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
