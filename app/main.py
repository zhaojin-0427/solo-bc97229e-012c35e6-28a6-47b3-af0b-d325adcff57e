"""园区分表电量推算 API。

结合总表与各分表的累计读数、分表功率上限，用线性规划求指定分表在
指定时段内可行耗电量的精确最小/最大值，用于判断结算分界时刻缺测的
分表电量是否足以确定、是否需要补抄。

新增按结算期的整批发布与查询能力：
- ``POST /api/v1/periods/{period}/publish``：整批物理相容 + 预期版本号匹配时
  原子替换正式读数并递增版本（乐观并发 + 幂等键）；
- ``POST /api/v1/periods/{period}/infer``：对该期当前正式版本做推算，
  整次推算使用同一版读数，响应附版本号。
数据与请求结果落在本地 SQLite（Docker 卷持久化），重启后保留。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Path, Request
from fastapi.responses import JSONResponse

from . import service, storage
from .models import (
    InferRequest,
    InferResponse,
    PeriodInferRequest,
    PeriodInferResponse,
    PeriodSummary,
    PublishRequest,
    PublishResponse,
    VersionInfo,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = storage.get_conn()  # 启动即建库建表并启用 WAL
    conn.close()
    yield


app = FastAPI(
    title="园区分表电量推算 API",
    description=(
        "输入采集系统导出的 1 个总表与 2~6 个分表的累计电量读数（UTC）"
        "及各分表功率上限，基于线性规划求指定分表在指定起止时刻之间"
        "可行耗电量的最小值与最大值（精确边界，非线性插值）。"
        "约束：任意时段总表耗电 = 全部分表之和；分表耗电非负且不超功率上限；"
        "不考虑损耗、换表与回零。读数不相容时返回 status=incompatible。\n\n"
        "结算期发布：整批读数通过物理相容性检查且预期版本号匹配时，原子替换"
        "正式读数并递增版本；同版本并发提交最多一个成功。请求键保证同键同内容"
        "重传回放原发布结果，同键异内容拒绝。数据在本地 SQLite WAL 持久化，重启保留。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 交互式文档示例（Swagger UI /docs 中可直接 Try it out 演示）
# ---------------------------------------------------------------------------

# 场景：观测期 2026-01-01 00:00~04:00（UTC），总表逐小时有读数，
# 两个分表只有观测期首尾读数（中间缺测），功率上限均 20 kW。
# 总表逐小时耗电：10 / 40 / 30 / 20 kWh，合计 100 kWh。
# 分表 sub_a 全期 60 kWh，sub_b 全期 40 kWh。
_EXAMPLE_METERS = {
    "meters": [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 1000.0},
                {"time": "2026-01-01T01:00:00Z", "value": 1010.0},
                {"time": "2026-01-01T02:00:00Z", "value": 1050.0},
                {"time": "2026-01-01T03:00:00Z", "value": 1080.0},
                {"time": "2026-01-01T04:00:00Z", "value": 1100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 500.0},
                {"time": "2026-01-01T04:00:00Z", "value": 560.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 200.0},
                {"time": "2026-01-01T04:00:00Z", "value": 240.0},
            ],
        },
    ]
}

EXAMPLE_DETERMINED = {
    **_EXAMPLE_METERS,
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T01:00:00Z",
        "end": "2026-01-01T02:00:00Z",
    },
}

EXAMPLE_RANGE = {
    **_EXAMPLE_METERS,
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T01:00:00Z",
    },
}

EXAMPLE_INCOMPATIBLE = {
    "meters": [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 60.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 50.0},
            ],
        },
    ],
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T04:00:00Z",
    },
}

# 发布示例：首次发布 expected_version=0，请求键由客户端生成
EXAMPLE_PUBLISH_FIRST = {
    **_EXAMPLE_METERS,
    "expected_version": 0,
    "request_key": "bulk-2026-01-upload-0001",
}

# 第二批（同一结算期的下一版）：expected_version=1
_EXAMPLE_METERS_V2 = {
    "meters": [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 1000.0},
                {"time": "2026-01-01T01:00:00Z", "value": 1010.0},
                {"time": "2026-01-01T02:00:00Z", "value": 1050.0},
                {"time": "2026-01-01T03:00:00Z", "value": 1080.0},
                {"time": "2026-01-01T04:00:00Z", "value": 1100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 500.0},
                {"time": "2026-01-01T04:00:00Z", "value": 560.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 200.0},
                {"time": "2026-01-01T04:00:00Z", "value": 240.0},
            ],
        },
    ],
}

EXAMPLE_PUBLISH_NEXT = {
    **_EXAMPLE_METERS_V2,
    "expected_version": 1,
    "request_key": "bulk-2026-01-upload-0002",
}

EXAMPLE_PUBLISH_INCOMPATIBLE = {
    "meters": EXAMPLE_INCOMPATIBLE["meters"],
    "expected_version": 0,
    "request_key": "bulk-2026-01-upload-bad",
}

EXAMPLE_PUBLISH_BAD_VERSION = {
    **_EXAMPLE_METERS,
    "expected_version": 99,
    "request_key": "bulk-2026-01-upload-wrongver",
}

EXAMPLE_PERIOD_INFER = {
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T01:00:00Z",
        "end": "2026-01-01T02:00:00Z",
    }
}

PERIOD_PATH = Path(
    ...,
    min_length=1,
    max_length=64,
    pattern=r"^[A-Za-z0-9._-]+$",
    description="结算期标识（字母、数字、点、下划线、短横线）",
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "园区分表电量推算 API", "docs": "/docs", "health": "/health"}


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------


@app.exception_handler(service.PublishError)
def _publish_error_handler(_: Request, exc: service.PublishError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.body())


@app.exception_handler(service.PeriodNotFound)
def _period_not_found_handler(_: Request, exc: service.PeriodNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "period_not_found",
                "message": f"结算期 {exc.period} 尚无已发布的正式读数",
                "period": exc.period,
            }
        },
    )


@app.exception_handler(ValueError)
def _value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    """路由层业务校验失败（如查询窗口越界）统一映射为 422，与入参校验一致。"""
    return JSONResponse(
        status_code=422,
        content={"detail": [{"type": "value_error", "msg": str(exc)}]},
    )


# ---------------------------------------------------------------------------
# 原有接口：即时推算（不落库、无版本）
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/infer",
    response_model=InferResponse,
    summary="推算指定分表在指定时段的可行电量区间",
)
def infer(
    req: InferRequest = Body(
        openapi_examples={
            "电量可被唯一确定（无需补抄）": {
                "summary": "总表逐小时有读数，01:00~02:00 总表耗电 40 kWh，"
                "两个分表功率上限均 20 kW，sub_a 该时段被唯一确定为 20 kWh",
                "value": EXAMPLE_DETERMINED,
            },
            "电量只能落在区间内（需判断是否补抄）": {
                "summary": "同一组读数，查询 00:00~01:00，"
                "sub_a 该时段可行范围为 [0, 10] kWh",
                "value": EXAMPLE_RANGE,
            },
            "读数不相容": {
                "summary": "总表全期 100 kWh，但两个分表合计 110 kWh，"
                "约束无可行解",
                "value": EXAMPLE_INCOMPATIBLE,
            },
        }
    ),
) -> InferResponse:
    return service.run_infer(req.meters, req.query)


# ---------------------------------------------------------------------------
# 按结算期发布
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/periods/{period}/publish",
    response_model=PublishResponse,
    responses={409: {"description": "版本不匹配 / 批次不相容 / 同键异内容"}},
    summary="按结算期原子发布整批电表读数",
)
def publish(
    period: str = PERIOD_PATH,
    req: PublishRequest = Body(
        openapi_examples={
            "首次发布（expected_version=0）": {
                "summary": "结算期 2026-01 的首次整批发布，预期当前版本为 0",
                "value": EXAMPLE_PUBLISH_FIRST,
            },
            "发布下一版（expected_version=1）": {
                "summary": "已存在版本 1 时，预期版本传 1，成功后原子替换为版本 2",
                "value": EXAMPLE_PUBLISH_NEXT,
            },
            "不相容批次被拒绝": {
                "summary": "总表 100 kWh 与分表合计 110 kWh 不平衡，"
                "拒绝发布且旧版保持不变",
                "value": EXAMPLE_PUBLISH_INCOMPATIBLE,
            },
            "预期版本号不匹配": {
                "summary": "当前版本并非 99，返回 409 version_conflict，旧版保留",
                "value": EXAMPLE_PUBLISH_BAD_VERSION,
            },
        }
    ),
) -> JSONResponse:
    http_status, body = service.publish_batch(period, req)
    if isinstance(body, PublishResponse):
        body = body.model_dump(mode="json")
    return JSONResponse(status_code=http_status, content=body)


# ---------------------------------------------------------------------------
# 按结算期推算（读取已发布的正式版本）
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/periods/{period}/infer",
    response_model=PeriodInferResponse,
    responses={404: {"description": "该结算期尚无已发布读数"}},
    summary="基于结算期当前正式版本读数推算（整次推算同版，响应附版本号）",
)
def period_infer(period: str = PERIOD_PATH, req: PeriodInferRequest = Body(
    openapi_examples={
        "推算已发布结算期": {
            "summary": "对结算期 2026-01 的当前正式版本查询 sub_a 的 "
            "01:00~02:00 电量，响应携带所用版本号",
            "value": EXAMPLE_PERIOD_INFER,
        }
    },
)) -> PeriodInferResponse:
    return service.infer_published(period, req.query)


# ---------------------------------------------------------------------------
# 结算期状态查询
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/periods",
    response_model=list[PeriodSummary],
    summary="列出所有结算期及其当前正式版本",
)
def list_periods() -> list[dict]:
    return service.list_period_summaries()


@app.get(
    "/api/v1/periods/{period}",
    response_model=PeriodSummary,
    responses={404: {"description": "结算期不存在"}},
    summary="查询单个结算期的当前版本状态",
)
def get_period(period: str = PERIOD_PATH) -> dict:
    return service.get_period_summary(period)


@app.get(
    "/api/v1/periods/{period}/versions",
    response_model=list[VersionInfo],
    responses={404: {"description": "结算期不存在"}},
    summary="列出结算期的历史版本（版本号严格递增、不重复）",
)
def get_period_versions(period: str = PERIOD_PATH) -> list[dict]:
    return service.list_period_versions(period)
