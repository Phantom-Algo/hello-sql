"""索引请求形状与谓词翻译（纯计算层，不 import storage）。

翻译器只回答"这个条件能否正确表达成键值或键区间"，不回答"下推后语义是否仍然
等价"：索引访问只把行集换成子集，完整谓词仍由上层 Filter 逐行求值，因此翻译
规则只需要保证"索引返回的行集是命中集的超集"，即不遗漏任何命中行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from contracts.ast import SqlType, Value

from runner.logical_plan.base import LogicalColumn
from runner.logical_plan.expressions import (
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
)


@dataclass(frozen=True, slots=True)
class IndexLookup:
    """等值索引请求：按列上的一个键取值。"""

    column: str
    key: Value


@dataclass(frozen=True, slots=True)
class IndexRange:
    """范围索引请求：端点 None 表示该侧无界，开闭语义由端点标志给出。"""

    column: str
    lower: Value | None
    upper: Value | None
    lower_inclusive: bool = True
    upper_inclusive: bool = True


IndexRequest: TypeAlias = IndexLookup | IndexRange


# 不可翻译的理由码：供追踪与测试断言，不作为 AccessPath.reason 的取值。
NE_UNSUPPORTED = "NE_UNSUPPORTED"
"""col <> k：需要把两侧区间合并成两次索引访问，本版不做。"""

CAST_UNSAFE = "CAST_UNSAFE"
"""一侧是类型提升产生的 CAST，且字面量无法无损归一回列的类型。"""

NOT_COMPARISON = "NOT_COMPARISON"
"""不是比较：裸布尔列、布尔字面量等没有键值可交给索引，留在上层求值。"""

NOT_BARE_COLUMN = "NOT_BARE_COLUMN"
"""没有"本表裸列对字面量"的形态：列在表达式内、两侧都是列、另一侧不是字面量。"""

NOT_CONJUNCT = "NOT_CONJUNCT"
"""conjunct 顶层是 OR 或 NOT：其分支不能单独作为过滤条件交给索引。"""


@dataclass(frozen=True, slots=True)
class Unsupported:
    """不可翻译的判定结果：只带稳定理由码，不参与执行。"""

    reason: str


@dataclass(frozen=True, slots=True)
class IndexCandidate:
    """一个可翻译 conjunct 的索引候选：目标列与语义等价的索引请求。"""

    column: LogicalColumn
    request: IndexRequest

    @property
    def is_lookup(self) -> bool:
        """等值候选为 True，区间候选为 False（候选排序与理由码都要用它）。"""
        return isinstance(self.request, IndexLookup)


TranslationResult: TypeAlias = IndexCandidate | Unsupported

# 字面量在左时的镜像翻转：k < col 等价于 col > k，等值两侧对称，<> 不翻译。
_MIRRORED_OP = {
    ComparisonOp.LT: ComparisonOp.GT,
    ComparisonOp.LE: ComparisonOp.GE,
    ComparisonOp.GT: ComparisonOp.LT,
    ComparisonOp.GE: ComparisonOp.LE,
}


def translate(conjunct: BoundExpr, table: str) -> TranslationResult:
    """把单个 conjunct 翻译成索引候选；不可翻译时返回理由码。

    table 是被规划的扫描表：只有属于该表的列才可能用它的索引取值。绑定完成
    后比较两侧类型已经一致，因此字面量的值经无损归纳后可直接作为索引键。
    """
    if isinstance(conjunct, (BoundLogical, BoundUnaryNot)):
        return Unsupported(NOT_CONJUNCT)
    if not isinstance(conjunct, BoundComparison):
        return Unsupported(NOT_COMPARISON)
    if conjunct.op is ComparisonOp.NE:
        return Unsupported(NE_UNSUPPORTED)

    matched = _match_sides(conjunct.left, conjunct.right, table)
    mirrored = False
    if isinstance(matched, Unsupported):
        # 字面量在左时按镜像翻转，等价关系见 _MIRRORED_OP
        mirrored_match = _match_sides(conjunct.right, conjunct.left, table)
        if isinstance(mirrored_match, Unsupported):
            # 两个方向都不可翻译：优先报更具体的理由，NOT_BARE_COLUMN 是兜底
            if matched.reason == NOT_BARE_COLUMN:
                return mirrored_match
            return matched
        matched = mirrored_match
        mirrored = True
    column, key = matched
    return IndexCandidate(column, _request(column, conjunct.op, key, mirrored))


def _match_sides(
    column_side: BoundExpr,
    value_side: BoundExpr,
    table: str,
) -> tuple[LogicalColumn, Value] | Unsupported:
    """按"索引列对字面量"的形态取出列与键值：列在给定一侧，另一侧必须是字面量。"""
    column = _column_side(column_side, table)
    if isinstance(column, Unsupported):
        return column
    return _key_value(column, value_side)


def _column_side(expr: BoundExpr, table: str) -> LogicalColumn | Unsupported:
    """识别可作为索引键的列：本表裸列，或数值提升留下的 CAST(本表 INT 列)。

    只有 INT→REAL 这一种提升是可达的，其余 CAST 形态没有等价键值，一律归为
    不可归一的 CAST，交给调用方按 CAST_UNSAFE 处理。
    """
    if isinstance(expr, BoundColumnRef):
        if expr.column.table == table:
            return expr.column
        return Unsupported(NOT_BARE_COLUMN)
    if isinstance(expr, BoundCast):
        if isinstance(expr.expr, BoundColumnRef):
            column = expr.expr.column
            if (
                column.table == table
                and column.type is SqlType.INT
                and expr.target is SqlType.REAL
            ):
                return column
            return Unsupported(
                CAST_UNSAFE if column.table == table else NOT_BARE_COLUMN
            )
        return Unsupported(CAST_UNSAFE)
    return Unsupported(NOT_BARE_COLUMN)


def _key_value(
    column: LogicalColumn,
    value_side: BoundExpr,
) -> tuple[LogicalColumn, Value] | Unsupported:
    """把字面量归一到列的类型；无法无损归一即不可翻译。

    INT 列与 REAL 字面量比较时，绑定期把列包成 CAST(col AS REAL)、字面量留成
    float：整数值可以无损回到 INT（1.0 等价于 1），非整数值没有等价写法，不做
    截断或进位，直接判为不可翻译。
    """
    if not isinstance(value_side, BoundLiteral):
        return Unsupported(NOT_BARE_COLUMN)
    if value_side.type is column.type:
        return column, value_side.value
    if (
        column.type is SqlType.INT
        and value_side.type is SqlType.REAL
        and isinstance(value_side.value, float)
        and value_side.value.is_integer()
    ):
        return column, int(value_side.value)
    return Unsupported(CAST_UNSAFE)


def _request(
    column: LogicalColumn,
    op: ComparisonOp,
    key: Value,
    mirrored: bool,
) -> IndexRequest:
    """按运算符构造索引请求；镜像形态先翻转成"列在左"的等价运算符。"""
    if op is ComparisonOp.EQ:
        return IndexLookup(column.name, key)
    if mirrored:
        op = _MIRRORED_OP[op]
    match op:
        case ComparisonOp.LT:
            return IndexRange(column.name, None, key, upper_inclusive=False)
        case ComparisonOp.LE:
            return IndexRange(column.name, None, key)
        case ComparisonOp.GT:
            return IndexRange(column.name, key, None, lower_inclusive=False)
        case ComparisonOp.GE:
            return IndexRange(column.name, key, None)
        case _:
            raise AssertionError(f"unexpected comparison op: {op}")
