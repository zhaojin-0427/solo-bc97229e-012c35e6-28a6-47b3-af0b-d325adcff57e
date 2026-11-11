"""发布与按期间推算的业务逻辑。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import status

from .models import (
    InferResponse,
    Meter,
    PeriodInferResponse,
    PublishRequest,
    PublishResponse,
    Query,
    validate_query_against_meters,
)
from .solver import MeterSeries, check_batch_feasible, solve_window
from . import storage


class PublishError(Exception):
    """发布被业务规则拒绝；路由层映射为对应 HTTP 状态码与错误体。"""

    def __init__(self, http_status: int, code: str, message: str, **extra):
        self.http_status = http_status
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)

    def body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                **self.extra,
            }
        }


# ---------------------------------------------------------------------------
# 通用转换
# ---------------------------------------------------------------------------


def to_ms(t: datetime) -> int:
    return round(t.timestamp() * 1000)


def meters_to_series(meters: list[Meter]) -> list[MeterSeries]:
    series = []
    for m in meters:
        ordered = sorted(m.readings, key=lambda r: r.time)
        series.append(
            MeterSeries(
                meter_id=m.meter_id,
                role=m.role,
                max_power_kw=m.max_power_kw,
                times_ms=[to_ms(r.time) for r in ordered],
                values=[r.value for r in ordered],
            )
        )
    return series


def canonical_content_hash(meters: list[Meter]) -> str:
    """批次内容的规范化 SHA-256（时间已统一归一化为 UTC，与输入时区写法无关）。"""
    payload = json.dumps(
        [m.model_dump(mode="json") for m in meters],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_infer(meters: list[Meter], query: Query) -> InferResponse:
    """沿用既有求解器对一组已校验读数做推算（/infer 与按期间推算共用）。"""
    result = solve_window(
        meters_to_series(meters),
        query_meter_id=query.meter_id,
        q_start_ms=to_ms(query.start),
        q_end_ms=to_ms(query.end),
    )
    if not result.feasible:
        return InferResponse(
            status="incompatible",
            meter_id=query.meter_id,
            start=query.start,
            end=query.end,
            min_kwh=None,
            max_kwh=None,
            is_determined=None,
            message="读数不相容：现有读数在“总表=分表之和、分表耗电非负且受功率上限约束”下无可行解",
            diagnostics=result.diagnostics
            or ["约束联立后无可行解（未定位到单一原因，请核查各表读数）"],
        )

    is_determined = (result.max_kwh - result.min_kwh) <= 1e-6 * max(
        1.0, abs(result.max_kwh)
    )
    if is_determined:
        message = "该时段电量可被现有读数唯一确定，可直接结算，无需补抄"
    else:
        message = "该时段电量只能确定到可行区间，请结合结算精度要求判断是否需要补抄"
    return InferResponse(
        status="ok",
        meter_id=query.meter_id,
        start=query.start,
        end=query.end,
        min_kwh=result.min_kwh,
        max_kwh=result.max_kwh,
        is_determined=is_determined,
        message=message,
        diagnostics=[],
    )


# ---------------------------------------------------------------------------
# 按结算期发布
# ---------------------------------------------------------------------------


def _stored_replay(row) -> tuple[int, dict]:
    """幂等重放：返回原始 HTTP 状态码与原始响应体（不随当前版本变化）。"""
    return row["http_status"], json.loads(row["response_json"])


def _replay_or_conflict(
    row, content_hash: str
) -> tuple[int, dict] | None:
    """锁内/锁外共用的终局处理：同内容重放原结果，异内容抛键冲突。"""
    stored_status, stored_body = _stored_replay(row)
    if row["content_hash"] != content_hash:
        raise PublishError(
            status.HTTP_409_CONFLICT,
            "key_content_conflict",
            f"结算期 {row['period']} 的请求键 {row['request_key']} "
            "已用于不同内容的请求，同一请求键不得重复用于不同批次",
            period=row["period"],
            request_key=row["request_key"],
        )
    if stored_status == status.HTTP_200_OK:
        stored_body = dict(stored_body)
        stored_body["status"] = "replayed"
    return stored_status, stored_body


def publish_batch(period: str, req: PublishRequest) -> tuple[int, PublishResponse | dict]:
    """发布一个完整批次。

    流程：
      1) 规范化摘要 + 最佳努力的快速幂等读（无锁）；
      2) 在写事务之外运行既有校验/求解器做整批物理相容性检查（不阻塞查询）；
      3) 单条 BEGIN IMMEDIATE 事务内完成幂等键、版本匹配与原子替换+版本递增。
    """
    content_hash = canonical_content_hash(req.meters)

    # 1) 快速路径：已终局的请求直接回放原结果（响应丢失/已有新版也能取回）
    with storage.read_tx() as conn:
        row = storage.get_idempotency_row(conn, period, req.request_key)
    if row is not None:
        return _replay_or_conflict(row, content_hash)

    # 2) 求解（写事务之外，旧版读数在此期间持续可查）
    feasibility = check_batch_feasible(meters_to_series(req.meters))
    if not feasibility.feasible:
        error_body = PublishError(
            status.HTTP_409_CONFLICT,
            "incompatible_batch",
            "整批读数未通过物理相容性检查，拒绝发布，正式读数保持旧版本不变",
            period=period,
            diagnostics=feasibility.diagnostics
            or ["约束联立后无可行解（未定位到单一原因，请核查各表读数）"],
        ).body()
        with storage.write_tx() as conn:
            row = storage.get_idempotency_row(conn, period, req.request_key)
            if row is not None:  # 并发下已被其他请求终局
                return _replay_or_conflict(row, content_hash)
            storage.insert_idempotency(
                conn, period, req.request_key, content_hash,
                status.HTTP_409_CONFLICT, error_body,
            )
        return status.HTTP_409_CONFLICT, error_body

    # 3) 原子发布（单写事务：版本匹配检查 + 替换 + 递增 + 幂等落盘）
    with storage.write_tx() as conn:
        row = storage.get_idempotency_row(conn, period, req.request_key)
        if row is not None:  # 并发下同键请求已终局
            return _replay_or_conflict(row, content_hash)

        period_row = storage.get_period_row(conn, period)
        current_version = period_row["current_version"] if period_row else 0
        if req.expected_version != current_version:
            error_body = PublishError(
                status.HTTP_409_CONFLICT,
                "version_conflict",
                f"预期版本号 {req.expected_version} 与当前正式版本号 "
                f"{current_version} 不匹配，拒绝发布，正式读数保持版本 "
                f"{current_version} 不变",
                period=period,
                expected_version=req.expected_version,
                current_version=current_version,
            ).body()
            storage.insert_idempotency(
                conn, period, req.request_key, content_hash,
                status.HTTP_409_CONFLICT, error_body,
            )
            return status.HTTP_409_CONFLICT, error_body

        new_version = current_version + 1
        published_at = datetime.now(timezone.utc)
        meters_payload = [m.model_dump(mode="json") for m in req.meters]
        storage.upsert_period(conn, period, new_version)
        storage.insert_version(
            conn, period, new_version, content_hash, req.request_key,
            published_at.isoformat(), meters_payload,
        )
        success_body = PublishResponse(
            status="published",
            period=period,
            version=new_version,
            request_key=req.request_key,
            content_hash=content_hash,
            meters=req.meters,
            published_at=published_at,
            message=(
                f"结算期 {period} 整批读数已原子发布，正式版本由 "
                f"{current_version} 递增至 {new_version}"
            ),
        ).model_dump(mode="json")
        storage.insert_idempotency(
            conn, period, req.request_key, content_hash,
            status.HTTP_200_OK, success_body,
        )
        return status.HTTP_200_OK, PublishResponse(**success_body)


# ---------------------------------------------------------------------------
# 按结算期推算
# ---------------------------------------------------------------------------


class PeriodNotFound(Exception):
    def __init__(self, period: str):
        self.period = period
        super().__init__(f"结算期 {period} 尚无已发布的正式读数")


def infer_published(period: str, query: Query) -> PeriodInferResponse:
    """对指定结算期的当前正式版本读数推算；整次推算使用同一版读数。"""
    with storage.read_tx() as conn:  # 单一只读事务取一致快照
        period_row = storage.get_period_row(conn, period)
        if period_row is None or period_row["current_version"] == 0:
            raise PeriodNotFound(period)
        version = period_row["current_version"]
        version_row = storage.get_version_row(conn, period, version)
        meters_payload = json.loads(version_row["meters_json"])

    meters = [Meter(**m) for m in meters_payload]
    validate_query_against_meters(meters, query)
    base = run_infer(meters, query)
    return PeriodInferResponse(
        **base.model_dump(), period=period, version=version
    )


# ---------------------------------------------------------------------------
# 结算期状态查询
# ---------------------------------------------------------------------------


def list_period_summaries() -> list[dict]:
    with storage.read_tx() as conn:
        rows = storage.list_periods(conn)
    summaries = []
    for r in rows:
        meter_count = 0
        if r["meters_json"]:
            meter_count = len(json.loads(r["meters_json"]))
        summaries.append(
            {
                "period": r["period"],
                "current_version": r["current_version"],
                "meter_count": meter_count,
                "published_at": r["published_at"],
            }
        )
    return summaries


def get_period_summary(period: str) -> dict:
    with storage.read_tx() as conn:
        period_row = storage.get_period_row(conn, period)
        if period_row is None:
            raise PeriodNotFound(period)
        version_row = storage.get_version_row(
            conn, period, period_row["current_version"]
        )
        meter_count = (
            len(json.loads(version_row["meters_json"])) if version_row else 0
        )
        return {
            "period": period,
            "current_version": period_row["current_version"],
            "meter_count": meter_count,
            "published_at": version_row["published_at"] if version_row else None,
        }


def list_period_versions(period: str) -> list[dict]:
    with storage.read_tx() as conn:
        if storage.get_period_row(conn, period) is None:
            raise PeriodNotFound(period)
        rows = storage.list_versions(conn, period)
    return [
        {
            "version": r["version"],
            "content_hash": r["content_hash"],
            "request_key": r["request_key"],
            "published_at": r["published_at"],
            "meter_count": len(json.loads(r["meters_json"])),
        }
        for r in rows
    ]
