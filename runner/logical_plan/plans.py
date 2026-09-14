from __future__ import annotations

from dataclasses import dataclass

from contracts.ast import ColumnDef, JoinType
from contracts.errors import E_COLUMN_NOT_FOUND, E_TYPE_MISMATCH, E_VALUE_COUNT, SqlError

from runner.logical_plan.base import (
    EMPTY_SCHEMA,
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
    join_schema,
)
from runner.logical_plan.expressions import (
    BoundArith,
    BoundAssignment,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    LogicOp,
    require_boolean,
)

# ---------- 查询节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalScan(LogicalPlan):
    """读取一张表：叶子节点，输出 Schema 为表的完整 Schema。

    只声明“读哪张表”，物理计划可以把它映射为 SeqScan/IndexScan。
    alias 不参与名称解析（解析依据是 Schema 列上的 qualifier），只让计划树自描述：
    自连接的两侧打印为 LogicalScan[users AS u1] / LogicalScan[users AS u2]。
    """

    table: str
    schema: LogicalSchema
    alias: str | None = None

    @property
    def qualifier(self) -> str:
        """名称绑定时使用的限定符：别名优先，否则使用表名。"""
        return self.alias or self.table

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema


@dataclass(frozen=True, slots=True)
class LogicalFilter(LogicalPlan):
    """按谓词过滤 child 输出行；输出 Schema 与 child 相同。

    谓词顶层恒为 BoundLogical(AND) 且 terms 非空；其中的列引用都必须能在
    child 的输出 Schema 中按 index 定位。
    """

    predicate: BoundExpr
    child: LogicalPlan

    def __post_init__(self) -> None:
        if (
            not isinstance(self.predicate, BoundLogical)
            or self.predicate.op is not LogicOp.AND
            or not self.predicate.terms
        ):
            raise SqlError(
                E_TYPE_MISMATCH,
                "filter predicate must be a non-empty AND conjunction",
            )
        _check_expr_columns(self.predicate, self.child.output_schema)

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return self.child.output_schema


@dataclass(frozen=True, slots=True)
class LogicalProjection(LogicalPlan):
    """按 SELECT 书写顺序投影：输出 Schema 依 columns 重新编号，允许重复列。

    SELECT name, name FROM users 在 V1 中合法，所以输出列不去重；`SELECT *`
    由 Builder 展开为全列后仍保留本节点，使所有 SELECT 都有一致的根。

    output_names 是结果表头，与 columns 等长且一一对应：“是否限定”由 SQL 书写
    决定、无法从列上推导，因此显式保存：限定投影与 JOIN 的 SELECT * 取
    `限定符.列名`，其余取原列名。
    """

    columns: tuple[BoundColumnRef, ...]
    output_names: tuple[str, ...]
    child: LogicalPlan

    def __post_init__(self) -> None:
        # 先校验等长：否则 output_schema 按 output_names 取名会抛 IndexError
        if len(self.output_names) != len(self.columns):
            raise SqlError(
                E_TYPE_MISMATCH,
                f"projection output name count: {len(self.output_names)} != "
                f"{len(self.columns)}",
            )
        schema = self.child.output_schema
        for col in self.columns:
            _check_column(col.column, schema)

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        # 输出列沿用来源的 table/qualifier/type，表头取 output_names，位置从 0 重新编号
        return LogicalSchema(
            tuple(
                LogicalColumn(
                    table=col.column.table,
                    qualifier=col.column.qualifier,
                    name=self.output_names[i],
                    index=i,
                    type=col.column.type,
                )
                for i, col in enumerate(self.columns)
            )
        )


@dataclass(frozen=True, slots=True)
class LogicalJoin(LogicalPlan):
    """INNER JOIN：左右子树按左列在前、右列在后的顺序拼接。

    不变式：
    - schema 恒等于 join_schema(left, right)；左右限定符相交时 join_schema 抛
      E_DUP_TABLE_ALIAS，因此本节点是重复别名的兜底检查点；
    - on 顶层恒为 BoundLogical(AND) 且 terms 非空，每个 conjunct 结果为 BOOLEAN；
    - on 中的列引用都必须在 schema 中按 index 定位。
    链式 JOIN 按书写顺序构造左深树，某个 on 只绑定在“左侧累积 Schema + 当前右表”上。
    """

    left: LogicalPlan
    right: LogicalPlan
    on: BoundExpr
    schema: LogicalSchema
    kind: JoinType = JoinType.INNER

    def __post_init__(self) -> None:
        # 先校验 Schema 契约：这样后续按 index 定位 on 中的列时，诊断信息才可信
        if self.schema != join_schema(self.left.output_schema, self.right.output_schema):
            raise SqlError(
                E_TYPE_MISMATCH,
                "join schema must equal join_schema(left.output_schema, "
                "right.output_schema)",
            )
        if (
            not isinstance(self.on, BoundLogical)
            or self.on.op is not LogicOp.AND
            or not self.on.terms
        ):
            raise SqlError(
                E_TYPE_MISMATCH,
                "join predicate must be a non-empty AND conjunction",
            )
        _check_expr_columns(self.on, self.schema)
        for term in self.on.terms:
            require_boolean(term, "JOIN ON")

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.left, self.right)

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema


@dataclass(frozen=True, slots=True)
class LogicalEmpty(LogicalPlan):
    """恒零行叶子：规则证明某个子树必然零行后用它替换该子树。

    两条硬要求：
    - schema 必须携带且与被替换子树同构：上层节点的列引用仍按旧的 index 编写，
      _check_column 会按 schema.columns[col.index] 校验；不带同构 Schema 时
      `SELECT * FROM t WHERE FALSE` 会在构造 Projection 时报错，而不是返回
      “有表头、零行”；
    - 它同时是可执行节点（EmptyExecutor）。把 Empty 沿树向上塌陷（JOIN 任一侧为
      Empty → 整个 JOIN 为 Empty）是优化规则的职责，不是本节点的语义：规则没跑到
      那一步时，计划照常执行且结果正确。
    """

    schema: LogicalSchema

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return self.schema


# ---------- 数据库命令节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalCreateDatabase(LogicalPlan):
    """创建数据库：叶子节点，存在性由执行阶段交给 DatabaseServer。"""

    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropDatabase(LogicalPlan):
    """删除数据库：叶子节点，当前库保护和存在性由执行阶段处理。"""

    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalUseDatabase(LogicalPlan):
    """切换当前数据库：叶子节点，由执行阶段更新 Runner 会话状态。"""

    name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


# ---------- 表 DDL 与 DML 节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalCreateTable(LogicalPlan):
    """建表：叶子节点，列信息原样保留（是否已存在由执行阶段交给 Storage）。"""

    table: str
    columns: tuple[ColumnDef, ...]

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropTable(LogicalPlan):
    """删表：叶子节点；表是否存在由执行阶段交给 Storage 检查。"""

    table: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalInsert(LogicalPlan):
    """单行插入：值已绑定为 BoundLiteral，数量与类型必须与表 Schema 一致。"""

    table: str
    table_schema: LogicalSchema
    values: tuple[BoundLiteral, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.table_schema.columns):
            raise SqlError(
                E_VALUE_COUNT,
                f"insert value count: {len(self.values)} != "
                f"{len(self.table_schema.columns)}",
            )
        for value, col in zip(self.values, self.table_schema.columns):
            if value.type != col.type:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"value {value.value!r} is not a valid {col.type.value} "
                    f"value for column {col.name}",
                )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalUpdate(LogicalPlan):
    """整行替换前的一次 UPDATE：child 先找出命中行，执行期收集 row_id 后写表。

    目标表必须与 child 底层的 LogicalScan 一致；assignments
    按 Builder 排好的顺序（last-write-wins 后按列序号排列）保存。
    """

    table: str
    assignments: tuple[BoundAssignment, ...]
    child: LogicalPlan

    def __post_init__(self) -> None:
        if _scan_under(self.child).table != self.table:
            raise SqlError(
                E_TYPE_MISMATCH,
                f"update target {self.table!r} does not match its scan",
            )
        schema = self.child.output_schema
        for assignment in self.assignments:
            _check_column(assignment.column, schema)
            if assignment.value.type != assignment.column.type:
                raise SqlError(
                    E_TYPE_MISMATCH,
                    f"assignment value type mismatch for column {assignment.column.name}",
                )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDelete(LogicalPlan):
    """删除命中行：与 LogicalUpdate 同构，child 先找行、执行期收集后删除。"""

    table: str
    child: LogicalPlan

    def __post_init__(self) -> None:
        if _scan_under(self.child).table != self.table:
            raise SqlError(
                E_TYPE_MISMATCH,
                f"delete target {self.table!r} does not match its scan",
            )

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return (self.child,)

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


# ---------- 索引 DDL 节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalCreateIndex(LogicalPlan):
    """建索引：叶子节点，目标表与列在绑定期已确认存在。

    是否与既有索引重名属 Storage 的权威状态，本节点不做判定。
    """

    index_name: str
    table: str
    column: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropIndex(LogicalPlan):
    """删索引：叶子节点，只带索引名（索引名在库内唯一，不需要表名）。"""

    index_name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


# ---------- 不变式校验辅助 ----------


def _scan_under(plan: LogicalPlan) -> LogicalScan:
    """沿一元链向下找到底层的 LogicalScan（只允许 Scan / Filter）。"""
    while isinstance(plan, LogicalFilter):
        plan = plan.child
    if isinstance(plan, LogicalScan):
        return plan
    raise SqlError(
        E_TYPE_MISMATCH, "UPDATE/DELETE child must be Scan or Filter over a Scan"
    )


def _check_column(column: LogicalColumn, schema: LogicalSchema) -> None:
    """校验一个列引用能在 schema 中按 index 定位，且限定符、表名、列名、类型一致。"""
    if not 0 <= column.index < len(schema.columns):
        raise SqlError(E_COLUMN_NOT_FOUND, f"column not found: {column.name}")
    expect = schema.columns[column.index]
    if (
        expect.table != column.table
        or expect.qualifier != column.qualifier
        or expect.name != column.name
        or expect.type != column.type
    ):
        raise SqlError(
            E_TYPE_MISMATCH,
            f"bound column mismatch: {column.qualifier}.{column.name}@"
            f"{column.index}({column.type.value}) vs schema {expect.qualifier}."
            f"{expect.name}({expect.type.value})",
        )


def _check_expr_columns(expr: BoundExpr, schema: LogicalSchema) -> None:
    """递归校验表达式中所有列引用都能在 schema 中定位。"""
    match expr:
        case BoundColumnRef():
            _check_column(expr.column, schema)
        case BoundComparison():
            _check_expr_columns(expr.left, schema)
            _check_expr_columns(expr.right, schema)
        case BoundLogical():
            for term in expr.terms:
                _check_expr_columns(term, schema)
        case BoundUnaryNot():
            _check_expr_columns(expr.operand, schema)
        case BoundArith():
            _check_expr_columns(expr.left, schema)
            _check_expr_columns(expr.right, schema)
        case BoundCast():
            _check_expr_columns(expr.expr, schema)
        case BoundLiteral():
            pass
