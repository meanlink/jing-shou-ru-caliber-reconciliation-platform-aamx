from pydantic import BaseModel, Field


class CaseInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    finance_records: list[dict] | None = Field(default=None, min_length=1, max_length=5000)
    channel_records: list[dict] | None = Field(default=None, min_length=1, max_length=5000)


class CopyInput(BaseModel):
    side: str = Field(pattern='^(finance|channel)$')
    records: list[dict] = Field(min_length=1, max_length=5000)
    expected_revision: int | None = None


class MergeInput(BaseModel):
    pass


class AdjudicationInput(BaseModel):
    row_day: str = Field(max_length=40)
    row_channel: str = Field(max_length=80)
    field: str | None = Field(default=None, max_length=40)
    resolution: str = Field(pattern='^(finance|channel)$')
    note: str = Field(default='', max_length=500)


class PublishInput(BaseModel):
    merge_id: int | None = None
