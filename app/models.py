"""API 请求/响应模型与业务校验。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_tz(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("时间必须携带时区，统一按 UTC 解释（如 2026-01-01T00:00:00Z）")
    return v.astimezone(timezone.utc)


class Reading(BaseModel):
    """一条累计电量读数。"""

    time: datetime = Field(description="读数时刻（UTC，ISO 8601）")
    value: float = Field(ge=0, description="累计电量读数（kWh）")

    _normalize_time = field_validator("time")(_require_tz)


class Meter(BaseModel):
    """一只表：总表（total）或分表（sub）。"""

    meter_id: str = Field(min_length=1, description="表计标识")
    role: Literal["total", "sub"] = Field(description="total=总表，sub=分表")
    max_power_kw: float | None = Field(
        default=None, gt=0, description="功率上限（kW），仅分表需要"
    )
    readings: list[Reading] = Field(
        min_length=2, description="累计读数序列，须覆盖观测期首尾，中间可缺测"
    )

    @model_validator(mode="after")
    def _check_meter(self) -> "Meter":
        if self.role == "sub" and self.max_power_kw is None:
            raise ValueError(f"分表 {self.meter_id} 必须提供 max_power_kw")
        if self.role == "total" and self.max_power_kw is not None:
            raise ValueError(f"总表 {self.meter_id} 不应提供 max_power_kw")
        times = [r.time for r in self.readings]
        if len(set(times)) != len(times):
            raise ValueError(f"表 {self.meter_id} 存在重复的读数时刻")
        return self


class Query(BaseModel):
    """推算请求：指定分表与起止时刻。"""

    meter_id: str = Field(description="待推算的分表 meter_id")
    start: datetime = Field(description="起始时刻（UTC）")
    end: datetime = Field(description="结束时刻（UTC）")

    _normalize_start = field_validator("start")(_require_tz)
    _normalize_end = field_validator("end")(_require_tz)


def validate_meter_batch(meters: list[Meter]) -> None:
    """跨表的整批结构校验（发布与即时推算共用）。"""
    totals = [m for m in meters if m.role == "total"]
    subs = [m for m in meters if m.role == "sub"]
    if len(totals) != 1:
        raise ValueError(f"必须且只能有 1 个总表，当前 {len(totals)} 个")
    if not 2 <= len(subs) <= 6:
        raise ValueError(f"分表数量须为 2~6 个，当前 {len(subs)} 个")
    ids = [m.meter_id for m in meters]
    if len(set(ids)) != len(ids):
        raise ValueError("meter_id 存在重复")

    # 观测期：所有表首读数时刻的最小值、末读数时刻的最大值；
    # 每只表都必须恰好覆盖观测期首尾。
    t0 = min(min(r.time for r in m.readings) for m in meters)
    t1 = max(max(r.time for r in m.readings) for m in meters)
    if not t0 < t1:
        raise ValueError("观测期起点必须早于终点")
    for m in meters:
        first = min(r.time for r in m.readings)
        last = max(r.time for r in m.readings)
        if first != t0 or last != t1:
            raise ValueError(
                f"表 {m.meter_id} 缺少观测期首尾读数：观测期为 "
                f"[{t0.isoformat()}, {t1.isoformat()}]，该表读数范围为 "
                f"[{first.isoformat()}, {last.isoformat()}]"
            )


def validate_query_against_meters(meters: list[Meter], q: Query) -> None:
    """查询窗口与整批读数的关系校验。"""
    subs = {m.meter_id for m in meters if m.role == "sub"}
    t0 = min(min(r.time for r in m.readings) for m in meters)
    t1 = max(max(r.time for r in m.readings) for m in meters)
    if q.meter_id not in subs:
        raise ValueError(f"查询对象 {q.meter_id} 必须是某个分表")
    if not (t0 <= q.start < q.end <= t1):
        raise ValueError(
            f"查询起止时刻须满足 观测期起点 <= start < end <= 观测期终点，"
            f"观测期为 [{t0.isoformat()}, {t1.isoformat()}]"
        )


class MeterBatch(BaseModel):
    """按结算期发布的整批电表读数（与 /infer 入参中的 meters 同构）。"""

    meters: list[Meter] = Field(description="1 个总表 + 2~6 个分表的完整批次")

    @model_validator(mode="after")
    def _check_batch(self) -> "MeterBatch":
        validate_meter_batch(self.meters)
        return self


class InferRequest(BaseModel):
    meters: list[Meter] = Field(description="1 个总表 + 2~6 个分表")
    query: Query

    @model_validator(mode="after")
    def _check_request(self) -> "InferRequest":
        validate_meter_batch(self.meters)
        validate_query_against_meters(self.meters, self.query)
        return self


class InferResponse(BaseModel):
    status: Literal["ok", "incompatible"] = Field(
        description="ok=推算成功；incompatible=读数不相容（无可行解）"
    )
    meter_id: str
    start: datetime
    end: datetime
    min_kwh: float | None = Field(description="该时段可行最小耗电量（kWh）")
    max_kwh: float | None = Field(description="该时段可行最大耗电量（kWh）")
    is_determined: bool | None = Field(
        description="min 与 max 是否一致（电量可被唯一确定，无需补抄）"
    )
    message: str
    diagnostics: list[str] = Field(
        default_factory=list, description="读数不相容时的原因说明"
    )


# ---------------------------------------------------------------------------
# 按结算期发布
# ---------------------------------------------------------------------------


class PublishRequest(MeterBatch):
    """按结算期发布整批读数的请求。"""

    expected_version: int = Field(
        ge=0,
        description="预期当前正式版本号（首次发布该结算期时传 0）；"
        "与服务端当前版本一致才允许提交",
    )
    request_key: str = Field(
        min_length=1,
        max_length=128,
        description="客户端请求键（幂等键）：同结算期同键同内容重传返回原结果；"
        "同键异内容拒绝",
    )


class PublishResponse(BaseModel):
    """发布成功的响应（也是同键重传时回放的原始发布结果）。"""

    status: Literal["published", "replayed"] = Field(
        description="published=本次原子发布成功；replayed=同键重传，回放原发布结果"
    )
    period: str = Field(description="结算期标识")
    version: int = Field(description="发布后的正式版本号（从 1 开始递增）")
    request_key: str
    content_hash: str = Field(description="批次内容的规范化 SHA-256 摘要")
    meters: list[Meter] = Field(description="已发布的整批读数")
    published_at: datetime = Field(description="首次发布时间（UTC）")
    message: str


# ---------------------------------------------------------------------------
# 按结算期推算（使用已发布的正式版本）
# ---------------------------------------------------------------------------


class PeriodInferRequest(BaseModel):
    """对已发布结算期的推算请求（query 结构与 /infer 一致）。"""

    query: Query

    @model_validator(mode="after")
    def _check_request(self) -> "PeriodInferRequest":
        # 与已发布读数的关系校验在路由层完成（需要从存储读取该期数据）
        return self


class PeriodInferResponse(InferResponse):
    """按结算期推算的响应：推算结果 + 整次推算所使用的同一版读数版本号。"""

    period: str
    version: int = Field(description="本次推算读取的正式读数版本号")


class PeriodSummary(BaseModel):
    """单个结算期的当前状态。"""

    period: str
    current_version: int = Field(description="当前正式版本号；0 表示尚无正式读数")
    meter_count: int = Field(description="当前版本包含的表计数量（无版本时为 0）")
    published_at: datetime | None = Field(
        default=None, description="当前版本发布时间（UTC）"
    )


class VersionInfo(BaseModel):
    """某结算期的一个历史版本。"""

    version: int
    content_hash: str
    request_key: str
    published_at: datetime
    meter_count: int
