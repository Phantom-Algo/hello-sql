"""Executor 树统一构建入口。"""

from __future__ import annotations

from runner.executor.base import StatementExecutor
from runner.executor.ddl import build_ddl_executor
from runner.executor.dml import build_dml_executor
from runner.executor.dql import build_select_executor
from runner.logical_plan.base import LogicalPlan
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalInsert,
    LogicalProjection,
    LogicalUpdate,
    LogicalUseDatabase,
)
from runner.trace_hooks import RunnerTraceSink, trace_runner_operation


class ExecutorTreeBuilder:
    """把语句级 LogicalPlan 根节点转换为 StatementExecutor。

    本类只负责识别**根节点**所属的语句类别，具体 Executor 的构造由各执行模块负责。
    """

    def __init__(self, trace_sink: RunnerTraceSink | None = None) -> None:
        """创建 Executor 树构建器并保存可选 C 追踪回调。

        Args:
            trace_sink: 接收构建输入、最终 Executor 树和失败的字典回调。
                不提供时构建路径与原实现相同。
        """

        self._trace_sink = trace_sink

    @trace_runner_operation("executor", "build_executor_tree")
    def build(self, plan: LogicalPlan) -> StatementExecutor:
        """按逻辑根节点分派 DQL、DML 或 DDL，构建一棵可执行树。"""
        match plan:
            case LogicalProjection():
                return build_select_executor(plan)
            case LogicalInsert() | LogicalUpdate() | LogicalDelete():
                return build_dml_executor(plan)
            case (
                LogicalCreateDatabase()
                | LogicalDropDatabase()
                | LogicalUseDatabase()
                | LogicalCreateTable()
                | LogicalDropTable()
            ):
                return build_ddl_executor(plan)
            case _:
                raise TypeError(
                    "unsupported statement plan: "
                    f"{type(plan).__name__}"
                )
