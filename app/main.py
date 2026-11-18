"""FastAPI 入口：排污口清单同步 API。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
交互文档：/docs（Swagger UI）、/redoc
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .database import init_db
from .models import (
    ChangeListResponse,
    ChangeRequest,
    ChangeSubmitResponse,
    BatchStatusResponse,
    DimStateDetailResponse,
    DimStateListResponse,
    DimensionEnableResponse,
    HealthResponse,
    OpenBatchRequest,
    OpenBatchResponse,
    OutletListResponse,
    PublishRequest,
    PublishResponse,
    SampleAttributionRequest,
    SampleAttributionResponse,
    ShardSubmitRequest,
    ShardSubmitResponse,
    StatsResponse,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="排污口清单同步 API",
    version="1.1.0",
    description=(
        "环保数据员提交监管系统排污口全量清单（分片）与连续变更；"
        "巡查端按区查询现役排口。发布切换需片齐、变更无缺号且序号不倒退。"
        "可回溯排口维表按状态号留存每次已生效增量与整批换版的完整清单，"
        "支持水样按采样当时的辖区与启停情况归档回溯。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def service_error_handler(_: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 兜底：未预期错误（含 SQLite 写入失败）返回 500；service 层事务已回滚，旧版状态不变
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": str(exc), "details": {}},
    )


# ---------- 批次与分片 ----------

@app.post("/batches", response_model=OpenBatchResponse, status_code=201, tags=["批次/分片"],
          summary="开批：声明片数和来源截止序号")
def open_batch(req: OpenBatchRequest) -> dict:
    return service.open_batch(req)


@app.get("/batches/current", response_model=BatchStatusResponse, tags=["批次/分片"],
         summary="查看当前批次进度（待补片号、待补序号）")
def current_batch() -> dict:
    return service.current_batch_status()


@app.post("/batches/{batch_id}/shards", response_model=ShardSubmitResponse,
          status_code=201, tags=["批次/分片"],
          summary="分片提交排口号、区号、启停状态（同片号重试幂等）")
def submit_shard(batch_id: int, req: ShardSubmitRequest) -> dict:
    return service.submit_shard(batch_id, req)


@app.post("/batches/{batch_id}/publish", response_model=PublishResponse, tags=["批次/分片"],
          summary="提交切换：指定追平序号，片齐且无缺号才发布新清单")
def publish(batch_id: int, req: PublishRequest) -> dict:
    return service.publish_batch(batch_id, req)


@app.post("/batches/{batch_id}/abort", status_code=200, tags=["批次/分片"],
          summary="废弃进行中的批次（发布失败后放弃该批，不影响现役版本）")
def abort_batch(batch_id: int) -> dict:
    return service.abort_batch(batch_id)


# ---------- 变更 ----------

@app.post("/changes", response_model=ChangeSubmitResponse, status_code=201, tags=["变更"],
          summary="按连续序号提交新增/迁区/停用变更（全量上传期间同样生效）")
def submit_change(req: ChangeRequest) -> dict:
    return service.submit_change(req)


@app.get("/changes", response_model=ChangeListResponse, tags=["变更"],
         summary="变更流查看：含缺号列表与断点信息")
def list_changes(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return service.list_changes(limit, offset)


# ---------- 巡查端查询 ----------

@app.get("/outlets", response_model=OutletListResponse, tags=["查询"],
         summary="按区查询现役排口（取当前已发布版本的同一已提交状态）")
def list_outlets(
    district: str | None = Query(default=None, description="区号，缺省返回全部区"),
    active_only: bool = Query(default=False, description="仅返回启用排口"),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return service.list_outlets(district, active_only, limit, offset)


@app.get("/stats", response_model=StatsResponse, tags=["查询"],
         summary="按区计数（与清单取同一已提交状态）")
def stats() -> dict:
    return service.stats()


# ---------- 可回溯排口维表 ----------

@app.post("/dimension/enable", response_model=DimensionEnableResponse, tags=["排口维表"],
          summary="启用维表：记录现役清单的初始状态号（幂等，重复启用不重拍快照）")
def enable_dimension() -> dict:
    return service.enable_dimension()


@app.get("/dimension/states", response_model=DimStateListResponse, tags=["排口维表"],
         summary="状态号流水：查看每个状态的来源（初始/增量/换版）与清单规模")
def list_dim_states(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return service.list_dim_states(limit, offset)


@app.get("/dimension/states/{state_no}", response_model=DimStateDetailResponse,
         tags=["排口维表"], summary="查看某个状态号对应的完整清单快照（辖区与启停）")
def get_dim_state(state_no: int) -> dict:
    return service.get_dim_state(state_no)


@app.post("/samples/attribute", response_model=SampleAttributionResponse, tags=["排口维表"],
          summary="水样归属回溯：按采样时取得的状态号返回当时辖区与启停，及无法归属原因")
def attribute_samples(req: SampleAttributionRequest) -> dict:
    return service.attribute_samples([item.model_dump() for item in req.samples])


@app.get("/health", response_model=HealthResponse, tags=["系统"], summary="健康检查与版本水位")
def health() -> dict:
    return service.health()
