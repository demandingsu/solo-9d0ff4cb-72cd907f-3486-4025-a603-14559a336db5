"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ChangeType = Literal["add", "move", "deactivate"]


# ---------- 请求 ----------

class OpenBatchRequest(BaseModel):
    total_shards: int = Field(gt=0, description="本批全量清单分片总数")
    source_cutoff_seq: int = Field(ge=0, description="来源系统截止变更序号（快照基线）")


class OutletItem(BaseModel):
    outlet_no: str = Field(min_length=1, description="排口号")
    district: str = Field(min_length=1, description="区号")
    active: bool = Field(description="启停状态：true=启用，false=停用")


class ShardSubmitRequest(BaseModel):
    shard_no: int = Field(ge=1, description="片号，从 1 开始")
    items: list[OutletItem] = Field(description="本片排口记录")


class ChangeRequest(BaseModel):
    seq: int = Field(ge=1, description="连续变更序号，从 1 开始")
    change_type: ChangeType
    outlet_no: str = Field(min_length=1)
    district: Optional[str] = Field(default=None, description="move/add 时必填的目标/所属区号")
    active: Optional[bool] = Field(default=None, description="add 时必填的启停状态")


class PublishRequest(BaseModel):
    catch_up_seq: int = Field(ge=0, description="追平序号：发布后清单需覆盖到此变更序号")


# ---------- 可回溯排口维表 ----------

class SampleAttributionItem(BaseModel):
    sample_no: str = Field(min_length=1, description="水样号")
    outlet_no: str = Field(min_length=1, description="采样排口号")
    state_no: int = Field(ge=1, description="采样当时取得的状态号（现役查询返回值）")


class SampleAttributionRequest(BaseModel):
    samples: list[SampleAttributionItem] = Field(min_length=1, max_length=1000)


# ---------- 响应 ----------

class OpenBatchResponse(BaseModel):
    batch_id: int
    total_shards: int
    source_cutoff_seq: int
    status: str


class ShardSubmitResponse(BaseModel):
    batch_id: int
    shard_no: int
    idempotent: bool = Field(description="true=重试命中，内容未重复生效")
    item_count: int
    received_shards: int
    total_shards: int


class ChangeSubmitResponse(BaseModel):
    seq: int
    idempotent: bool
    applied: bool = Field(description="是否已落到现役清单（旧版）")
    blocked: bool
    block_reason: Optional[str] = None
    expected_next_seq: int
    last_served_seq: int
    current_state_no: Optional[int] = Field(
        default=None, description="维表启用后的当前状态号；阻断/暂挂不产生新状态时维持原值"
    )


class PublishResponse(BaseModel):
    batch_id: int
    version: int
    catch_up_seq: int
    outlet_count: int
    active_count: int
    changes_applied: int
    current_state_no: Optional[int] = Field(
        default=None, description="维表启用后，本次换版生成的状态号"
    )


class OutletOut(BaseModel):
    outlet_no: str
    district: str
    active: bool
    version: int


class OutletListResponse(BaseModel):
    version: int
    state_no: Optional[int] = Field(default=None, description="维表启用后的当前状态号，采样时记录此值用于回溯")
    district: Optional[str]
    active_only: bool
    count: int
    items: list[OutletOut]


class DistrictCount(BaseModel):
    district: str
    total: int
    active: int
    inactive: int


class StatsResponse(BaseModel):
    version: int
    state_no: Optional[int] = None
    last_served_seq: int
    total: int
    active: int
    inactive: int
    districts: list[DistrictCount]


class BatchStatusResponse(BaseModel):
    batch_id: int
    status: str
    total_shards: int
    source_cutoff_seq: int
    received_shards: list[int]
    missing_shards: list[int]
    changes_pending: list[int] = Field(description="截止序号之后至追平目标之间待补的变更序号（按当前最大已知序号推断）")
    next_expected_seq: int
    blocked: bool
    block_reason: Optional[str] = None
    last_served_seq: int


class ChangeOut(BaseModel):
    seq: int
    change_type: str
    outlet_no: str
    district: Optional[str]
    active: Optional[bool]
    applied: bool
    blocked: bool
    block_reason: Optional[str]


class ChangeListResponse(BaseModel):
    items: list[ChangeOut]
    missing_seqs: list[int]
    next_expected_seq: int
    last_served_seq: int


class HealthResponse(BaseModel):
    status: str
    version: int
    last_served_seq: int
    dimension_enabled: bool = False
    current_state_no: Optional[int] = None


# ---------- 维表响应 ----------

class DimensionEnableResponse(BaseModel):
    enabled: bool
    idempotent: bool = Field(description="true=维表此前已启用，未重复拍初始快照")
    state_no: int = Field(description="初始状态号（首次启用为 1）")
    source_version: int
    outlet_count: int
    active_count: int


class DimStateOut(BaseModel):
    state_no: int
    source: str
    ref_seq: Optional[int]
    ref_version: int
    outlet_count: int
    active_count: int
    created_at: str


class DimStateListResponse(BaseModel):
    enabled: bool
    current_state_no: Optional[int]
    count: int
    items: list[DimStateOut]


class DimOutletOut(BaseModel):
    outlet_no: str
    district: str
    active: bool


class DimStateDetailResponse(BaseModel):
    state_no: int
    source: str
    ref_seq: Optional[int]
    ref_version: int
    outlet_count: int
    active_count: int
    created_at: str
    items: list[DimOutletOut]


class SampleAttributionResult(BaseModel):
    sample_no: str
    outlet_no: str
    state_no: int
    attributable: bool
    district: Optional[str] = Field(default=None, description="采样当时归属区号；无法归属时为 null")
    active: Optional[bool] = Field(default=None, description="采样当时启停情况；无法归属时为 null")
    reason: Optional[str] = Field(default=None, description="无法归属的原因码；可归属时为 null")
    reason_message: Optional[str] = None


class SampleAttributionResponse(BaseModel):
    enabled: bool
    current_state_no: Optional[int]
    count: int
    attributed_count: int
    unattributable_count: int
    results: list[SampleAttributionResult]
