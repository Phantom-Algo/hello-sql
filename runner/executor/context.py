"""Executor 每次调用共享的会话上下文。

本模块只保存 Server、当前 Storage、库名与可选 C 事件回调；
它不执行计划，也不构造 UI 模型。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.storage import BaseDatabaseServer, BaseStorage
from runner.trace_hooks import RunnerTraceSink


@dataclass(slots=True)
class ExecutionContext:
    """Executor 共享的执行环境、会话状态与可选追踪出口。

    行执行器是不可变计划投影，不持有 Storage 或 UI 状态。每次执行
    时从本上下文获取当前连接和可选字典回调，从而保持职责边界。
    """

    server: BaseDatabaseServer
    """DatabaseServer：建库 / 删库 / 连接数据库。"""

    storage: BaseStorage
    """当前数据库的表级操作连接（Storage）。"""

    current_database: str
    """当前数据库名，用于删库时的会话检查。"""

    trace_sink: RunnerTraceSink | None = None
    """可选 C 运行事件出口；未启用时为 None。"""
