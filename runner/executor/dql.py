"""DQL 执行器：把 Projection/Filter/Join/Scan 计划树转换为拉取式行流水线。

- SeqScanExecutor：读取 Storage 的整表行，按 source_indexes 投影到声明的列；
- FilterExecutor：按谓词过滤，命中行原样下传；
- NestedLoopJoinExecutor：拼接左右行并按 ON 谓词筛选；
- ProjectionExecutor：按投影列重排行值；
- EmptyExecutor：恒零行，不访问 Storage；
- SelectExecutor：把行流水线物化为 QueryResult。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from contracts.errors import E_COLUMN_NOT_FOUND, SqlError
from contracts.result import QueryResult
from runner.executor.base import RowExecutor, StatementExecutor
from runner.executor.context import ExecutionContext
from runner.executor.row import ExecRow
from runner.logical_plan.base import LogicalPlan, LogicalSchema
from runner.logical_plan.builder import DescribeTable
from runner.logical_plan.expressions import BoundColumnRef, BoundExpr, eval_expr
from runner.logical_plan.plans import (
    LogicalEmpty,
    LogicalFilter,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
)
from runner.trace_hooks import trace_runner_operation


@dataclass(frozen=True, slots=True)
class SeqScanExecutor(RowExecutor):
    """顺序扫描一张表，按 source_indexes 从 Storage 的整行中取出声明输出列。

    source_indexes[i] 是第 i 个输出列在 storage.scan() 行元组中的位置，
    是执行期下标，与 LogicalColumn.index（裁剪后元组中的位置）是两套下标。
    未裁剪时它是 (0, 1, ..., n-1)，输出与整行逐值相同。

    例如：
    users(id, name, age, gender)，但是裁剪后只剩下 name 与 gender
    但是由于 storage.scan() 返回的是整行数据，因此需要通过 source_indexes 将原始数据映射为在执行器树中要输入的数据
    在本例子中，source_indexes 应该为 (1, 3)，表示第 0 个位置的数据映射到原始数据的第 1 个，即 name，gender 同理。
    """

    table: str
    schema: LogicalSchema
    source_indexes: tuple[int, ...]

    @property
    def output_schema(self) -> LogicalSchema:
        """返回物理表的完整绑定 Schema，其顺序与 Storage 行值一致。"""

        return self.schema

    @trace_runner_operation("runtime", "seq_scan.rows")
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        for row_id, values in context.storage.scan(self.table):
            yield ExecRow(
                row_id=row_id,
                values=tuple(values[index] for index in self.source_indexes),
            )


@dataclass(frozen=True, slots=True)
class EmptyExecutor(RowExecutor):
    """恒零行、零副作用：不访问 Storage，只承载输出 Schema 供表头使用。"""

    schema: LogicalSchema

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema

    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        return iter(())


@dataclass(frozen=True, slots=True)
class FilterExecutor(RowExecutor):
    """按谓词过滤 child 输出行，输出 Schema 与 child 相同。"""

    predicate: BoundExpr
    child: RowExecutor

    @property
    def output_schema(self) -> LogicalSchema:
        """过滤不改变列集与位置，因此直接返回子算子 Schema。"""

        return self.child.output_schema

    @trace_runner_operation("runtime", "filter.rows")
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        """拉取子算子行并求值已绑定谓词，仅下传结果为真的行。"""

        # 命中行原样下传，列位置与 row_id 都不变；AND 短路由 eval_expr 负责
        for row in self.child.rows(context):
            if eval_expr(self.predicate, row.values):
                yield row


@dataclass(frozen=True, slots=True)
class NestedLoopJoinExecutor(RowExecutor):
    """INNER JOIN 行算子：左侧流式拉取，右侧在每次执行时物化一次。

    左右行的 values 按 Schema 约定直接拼接，ON 中的列索引因此可以
    直接作用于拼接行。JOIN 只出现在 SELECT 计划中，输出行沿用左行的
    row_id 仅为了继续通过通用行流水线，不会被解释为 JOIN 结果的物理行标识。
    """

    left: RowExecutor
    right: RowExecutor
    on: BoundExpr
    schema: LogicalSchema

    @property
    def output_schema(self) -> LogicalSchema:
        """返回左右 Schema 按列顺序合并后的 JOIN 输出结构。"""

        return self.schema

    @trace_runner_operation("runtime", "nested_loop_join.rows")
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        """物化右侧一次，对每个左行比较 ON，惰性产出匹配的拼接行。"""

        # 右侧只执行一次，避免为每个左行重复扫描 Storage。
        right_rows = tuple(self.right.rows(context))
        for left_row in self.left.rows(context):
            for right_row in right_rows:
                values = left_row.values + right_row.values
                if eval_expr(self.on, values):
                    yield ExecRow(row_id=left_row.row_id, values=values)


@dataclass(frozen=True, slots=True)
class ProjectionExecutor(RowExecutor):
    """按 SELECT 书写顺序重排行值，输出 Schema 的列索引已重新编号。"""

    columns: tuple[BoundColumnRef, ...]
    child: RowExecutor
    schema: LogicalSchema

    @property
    def output_schema(self) -> LogicalSchema:
        """返回已按 SELECT 列表重新排列和编号的输出 Schema。"""

        return self.schema

    @trace_runner_operation("runtime", "projection.rows")
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        """按绑定列索引投影每条子行，保留 SQL 书写顺序和重复列。"""

        # 逐项按 columns 顺序读取 child 的行值，保留重复列与书写顺序
        for row in self.child.rows(context):
            values = tuple(
                row.values[column.column.index]
                for column in self.columns
            )
            yield ExecRow(row_id=row.row_id, values=values)


@dataclass(frozen=True, slots=True)
class SelectExecutor(StatementExecutor):
    """SELECT 语句级执行器：把根算子的行流水线物化为 QueryResult。"""

    root: RowExecutor

    @trace_runner_operation("runtime", "select.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """完全消费根行流，构造表头与行元组均不可缺省的 SELECT 结果。"""

        return QueryResult(
            columns=tuple(
                column.name
                for column in self.root.output_schema.columns
            ),
            rows=tuple(
                row.values
                for row in self.root.rows(context)
            ),
            affected_rows=None,
        )


# ---------- 构建 ----------


def build_row_executor(
    plan: LogicalPlan,
    describe_table: DescribeTable,
) -> RowExecutor:
    """把逻辑计划递归转换为行执行器；UPDATE/DELETE 也用它构建 Scan/Filter child。

    describe_table 用于在构建期取表完整列序，定位裁剪后 Scan 的输出列。
    """
    match plan:
        case LogicalScan():
            return SeqScanExecutor(
                table=plan.table,
                schema=plan.output_schema,
                source_indexes=_scan_source_indexes(
                    plan.table, plan.output_schema, describe_table
                ),
            )
        case LogicalEmpty():
            return EmptyExecutor(schema=plan.output_schema)
        case LogicalFilter():
            return FilterExecutor(
                predicate=plan.predicate,
                child=build_row_executor(plan.child, describe_table),
            )
        case LogicalJoin():
            return NestedLoopJoinExecutor(
                left=build_row_executor(plan.left, describe_table),
                right=build_row_executor(plan.right, describe_table),
                on=plan.on,
                schema=plan.output_schema,
            )
        case LogicalProjection():
            return ProjectionExecutor(
                columns=plan.columns,
                child=build_row_executor(plan.child, describe_table),
                schema=plan.output_schema,
            )
        case _:
            raise TypeError(
                f"plan cannot produce rows: {type(plan).__name__}"
            )


def build_select_executor(
    plan: LogicalProjection,
    describe_table: DescribeTable,
) -> SelectExecutor:
    """SELECT 语句级执行器的构建入口。"""
    return SelectExecutor(root=build_row_executor(plan, describe_table))


def _scan_source_indexes(
    table: str,
    schema: LogicalSchema,
    describe_table: DescribeTable,
) -> tuple[int, ...]:
    """按表完整列序定位每个输出列在 storage.scan() 行元组中的位置。

    裁剪后的 Scan.schema 是表 Schema 的子序列，两者的列名一致，因此按列名反查
    即可；列名在表内唯一（存储层拒绝重名列）。
    """
    positions = {
        column.name: index
        for index, column in enumerate(describe_table(table).columns)
    }
    try:
        return tuple(positions[column.name] for column in schema.columns)
    except KeyError as missing:
        raise SqlError(
            E_COLUMN_NOT_FOUND,
            f"column not found in table {table}: {missing.args[0]}",
        ) from None
