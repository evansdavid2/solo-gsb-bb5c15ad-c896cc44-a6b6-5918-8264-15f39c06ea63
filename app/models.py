"""Pydantic 请求模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class NodeInput(BaseModel):
    name: str = Field(..., min_length=1, description="节点名，批次内唯一")
    node_type: str = Field("service", description="节点类型，如 service / config / job")
    estimated_seconds: int | None = Field(
        None, ge=1, description="预估工期（正整数，秒）；不传视为 0，用于关键路径分析"
    )
    max_attempts: int = Field(
        1, ge=1, le=10, description="最大尝试次数（含首次，1～10）；1 表示不自动重试，旧批次兼容为 1"
    )
    retry_delay_seconds: int = Field(
        0, ge=0, le=3600, description="失败后自动重试的延迟（0～3600 秒），到期前节点不进入 next_nodes"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class EdgeInput(BaseModel):
    # to_node 依赖 from_node：from_node 必须先执行
    from_node: str = Field(..., min_length=1, description="前置节点")
    to_node: str = Field(..., min_length=1, description="后置节点（依赖前置）")


class CreateReleaseRequest(BaseModel):
    version: str = Field(..., min_length=1, description="发布版本号，如 2026.09.20-001")
    nodes: list[NodeInput] = Field(..., min_length=1, description="服务节点列表")
    dependencies: list[EdgeInput] = Field(default_factory=list, description="依赖边 from->to")


class ReportRequest(BaseModel):
    node: str = Field(..., min_length=1, description="回报的节点名")
    status: str = Field(..., pattern="^(success|failed)$", description="成功或失败")
    version: str = Field(..., min_length=1, description="回报时携带的版本号，必须与批次版本一致")
    error_code: str | None = Field(None, description="失败错误码（失败时可选）")
    error_message: str | None = Field(None, description="失败原因（失败时可选）")
