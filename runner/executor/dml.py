"""DML 执行器：消费 INSERT / UPDATE / DELETE 三种计划，返回影响行数。

UPDATE / DELETE 不重复实现扫描与过滤，其 child 通过 DQL 的
build_row_executor 构建为 SeqScanExecutor / FilterExecutor。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, assert_never

from contracts.result import QueryResult
from runner.executor.base import RowExecutor, StatementExecutor
from runner.executor.context import ExecutionContext
from runner.executor.dql import build_row_executor
from runner.logical_plan.expressions import BoundAssignment, BoundLiteral
from runner.logical_plan.plans import LogicalDelete, LogicalInsert, LogicalUpdate
from runner.trace_hooks import trace_runner_operation


@dataclass(frozen=True, slots=True)
class InsertExecutor(StatementExecutor):
    """插入单行：值已由 Builder 完成数量校验与类型规范化。"""

    table: str
    values: tuple[BoundLiteral, ...]

    @trace_runner_operation("runtime", "insert.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """取出绑定字面量的 Python 值写入目标表，成功时报告影响一行。"""

        context.storage.insert(
            self.table,
            tuple(value.value for value in self.values),
        )
        return QueryResult(affected_rows=1)


@dataclass(frozen=True, slots=True)
class UpdateExecutor(StatementExecutor):
    """整行替换：先物化命中行再写入，避免在 scan 迭代期间修改表。"""

    table: str
    assignments: tuple[BoundAssignment, ...]
    child: RowExecutor

    @trace_runner_operation("runtime", "update.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """先物化所有匹配行，再按绑定列位置整行替换并返回影响行数。"""

        # 必须先完全消费 child，结束 Storage.scan() 迭代后再开始写
        matched_rows = tuple(self.child.rows(context))

        for row in matched_rows:
            values = list(row.values)
            # assignments 已按列索引排列且完成 last-write-wins
            for assignment in self.assignments:
                values[assignment.column.index] = assignment.value.value
            context.storage.update_row(
                self.table,
                row.row_id,
                tuple(values),
            )

        return QueryResult(affected_rows=len(matched_rows))


@dataclass(frozen=True, slots=True)
class DeleteExecutor(StatementExecutor):
    """删除命中行：物化阶段只需保留 row_id。"""

    table: str
    child: RowExecutor

    @trace_runner_operation("runtime", "delete.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """先物化匹配 row_id，再逐一删除，避免在 Storage.scan 迭代期间改页。"""

        row_ids = tuple(
            row.row_id
            for row in self.child.rows(context)
        )

        for row_id in row_ids:
            context.storage.delete_row(self.table, row_id)

        return QueryResult(affected_rows=len(row_ids))


# ---------- 构建 ----------


DmlPlan: TypeAlias = LogicalInsert | LogicalUpdate | LogicalDelete


def build_dml_executor(plan: DmlPlan) -> StatementExecutor:
    """把 DML 逻辑计划转换为语句级执行器。"""
    match plan:
        case LogicalInsert():
            return InsertExecutor(
                table=plan.table,
                values=plan.values,
            )
        case LogicalUpdate():
            return UpdateExecutor(
                table=plan.table,
                assignments=plan.assignments,
                child=build_row_executor(plan.child),
            )
        case LogicalDelete():
            return DeleteExecutor(
                table=plan.table,
                child=build_row_executor(plan.child),
            )
        case _:
            assert_never(plan)
