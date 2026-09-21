"""FastAPI 入口：依赖图发布 / 回滚 / 重试 / 查询 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import config, service
from .database import init_db
from .errors import APIError
from .models import CreateReleaseRequest, ReportRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时完成 SQLite 初始化（目录不存在会自动创建，重复执行幂等）
    init_db()
    yield


app = FastAPI(
    title="依赖图发布回滚 API",
    description="客户端提交服务节点及依赖关系，按拓扑层级生成执行计划，"
                "支持节点成功/失败回报、失败仅回滚本批次已成功节点、从断点重试。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/releases", status_code=201, tags=["releases"])
def create_release(payload: CreateReleaseRequest) -> dict:
    """创建带版本号的发布批次；循环依赖返回 422 cycle_detected。"""
    return service.create_release(payload)


@app.get("/api/releases", tags=["releases"])
def list_releases(limit: int = 50) -> dict:
    """查询最近的发布批次列表。"""
    return {"items": service.list_releases(limit=min(max(limit, 1), 200))}


@app.get("/api/releases/{release_id}", tags=["releases"])
def get_release_status(release_id: str) -> dict:
    """查询发布状态：每个节点的状态、进度、当前可回报节点。"""
    return service.get_release(release_id)


@app.get("/api/releases/{release_id}/plan", tags=["releases"])
def get_release_plan(
    release_id: str,
    deadline_seconds: int | None = Query(
        None, ge=0,
        description="可选截止工期（秒，非负整数）；仅做可行性分析，不改变批次状态",
    ),
) -> dict:
    """查询拓扑分层执行计划与关键路径（CPM）分析。

    返回最短总工期、各节点最早/最晚开始时间、total_slack、全部零余量关键节点；
    critical_path 在并列最长路径中取节点名序列字典序最小者。
    deadline_seconds 小于最短总工期时仍返回完整分析，feasible=false 并给出缺口秒数。
    """
    return service.get_release_plan(release_id, deadline_seconds)


@app.post("/api/releases/{release_id}/report", tags=["releases"])
def report_node(release_id: str, payload: ReportRequest) -> dict:
    """回报节点执行结果。

    - 跨版本回报 → 409 version_mismatch
    - 前置节点未全部成功就回报 → 409 out_of_order
    - 节点处于自动重试等待期（next_retry_at 未到期）→ 409 retry_not_due，状态不变
    - status=failed 且 attempt_count < max_attempts → 原子递增尝试次数，节点恢复 pending
      并安排 next_retry_at，不触发批次回滚（result=retry_scheduled）
    - status=failed 且达到 max_attempts 上限 → 逆拓扑回滚本批次已成功节点，返回可重试断点
      （旧批次 max_attempts 缺省为 1，首次失败即回滚）
    """
    return service.report_node(release_id, payload)


@app.post("/api/releases/{release_id}/retry", tags=["releases"])
def retry_release(release_id: str) -> dict:
    """从断点续发：重置 failed/rolled_back 节点，从最低待执行层级继续。"""
    return service.retry_release(release_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)
