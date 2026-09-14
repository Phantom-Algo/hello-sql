"""F5 逻辑优化的五条规则：LogicalPlan → LogicalPlan 的纯变换。

规则只做树改写：不 import storage、不执行计划、不读数据、不原地修改输入
（节点都是 frozen dataclass，改写即构造新节点、复用未变子树）。每条规则都是
整树递归重写，规则内部自证作用范围，驱动层不需要理解规则结构。

结果等价是第一约束，形态规范是第二约束：任何做不到等价的改写都不做。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from contracts.ast import JoinType, SqlType
from contracts.errors import E_TYPE_MISMATCH, SqlError

from runner.logical_plan.base import (
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
    join_schema,
)
from runner.logical_plan.expressions import (
    BoundArith,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
    LogicOp,
    cast_value,
    cmp_eval,
)
from runner.logical_plan.plans import (
    LogicalEmpty,
    LogicalFilter,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """一次规则应用的产物：改写后的计划 + 本规则自述的单行摘要。

    摘要由规则在改写过程中统计命中得出（不靠驱动层反推），因此能说清
    「做了什么改写、改了多少处」。
    """

    plan: LogicalPlan
    summary: str


def _summarize_hits(parts: list[str]) -> str:
    """把命中片段拼成单行摘要；无命中时给一句可读的说明。"""
    return "；".join(parts) if parts else "无命中"


# ---------- 列身份 ----------


class ColumnKey(NamedTuple):
    """列的身份键：裁剪需求按它去重与会合，不含随行布局变化的 index。"""

    table: str
    qualifier: str
    name: str
    type: SqlType


def column_key(column: LogicalColumn) -> ColumnKey:
    """取列的身份键。"""
    return ColumnKey(column.table, column.qualifier, column.name, column.type)


# ---------- 表达式改写 ----------


def rewrite_expr(
    expr: BoundExpr,
    rewrite: Callable[[BoundExpr], BoundExpr],
) -> BoundExpr:
    """自底向上重写表达式：先重建子表达式，再把 rewrite 作用到重建后的节点。"""
    match expr:
        case BoundColumnRef():
            rebuilt: BoundExpr = expr
        case BoundLiteral():
            rebuilt = expr
        case BoundComparison():
            rebuilt = BoundComparison(
                rewrite_expr(expr.left, rewrite),
                expr.op,
                rewrite_expr(expr.right, rewrite),
            )
        case BoundLogical():
            rebuilt = BoundLogical(
                expr.op, tuple(rewrite_expr(term, rewrite) for term in expr.terms)
            )
        case BoundUnaryNot():
            rebuilt = BoundUnaryNot(rewrite_expr(expr.operand, rewrite))
        case BoundArith():
            rebuilt = BoundArith(
                rewrite_expr(expr.left, rewrite), expr.op, rewrite_expr(expr.right, rewrite)
            )
        case BoundCast():
            rebuilt = BoundCast(rewrite_expr(expr.expr, rewrite), expr.target)
        case _:
            rebuilt = expr
    return rewrite(rebuilt)


def map_columns(
    expr: BoundExpr,
    transform: Callable[[LogicalColumn], LogicalColumn],
) -> BoundExpr:
    """重建表达式，对每个列引用应用 transform（只改列，不改结构）。"""
    match expr:
        case BoundColumnRef():
            return BoundColumnRef(transform(expr.column))
        case BoundComparison():
            return BoundComparison(
                map_columns(expr.left, transform),
                expr.op,
                map_columns(expr.right, transform),
            )
        case BoundLogical():
            return BoundLogical(
                expr.op, tuple(map_columns(term, transform) for term in expr.terms)
            )
        case BoundUnaryNot():
            return BoundUnaryNot(map_columns(expr.operand, transform))
        case BoundArith():
            return BoundArith(
                map_columns(expr.left, transform), expr.op, map_columns(expr.right, transform)
            )
        case BoundCast():
            return BoundCast(map_columns(expr.expr, transform), expr.target)
        case _:
            return expr


def expr_columns(expr: BoundExpr) -> tuple[LogicalColumn, ...]:
    """收集表达式中全部列引用，保持出现顺序。"""
    columns: list[LogicalColumn] = []
    _collect_columns(expr, columns)
    return tuple(columns)


def _collect_columns(expr: BoundExpr, columns: list[LogicalColumn]) -> None:
    """把表达式中的列引用按出现顺序追加到 columns。"""
    match expr:
        case BoundColumnRef():
            columns.append(expr.column)
        case BoundComparison():
            _collect_columns(expr.left, columns)
            _collect_columns(expr.right, columns)
        case BoundLogical():
            for term in expr.terms:
                _collect_columns(term, columns)
        case BoundUnaryNot():
            _collect_columns(expr.operand, columns)
        case BoundArith():
            _collect_columns(expr.left, columns)
            _collect_columns(expr.right, columns)
        case BoundCast():
            _collect_columns(expr.expr, columns)
        case _:
            pass


def expr_qualifiers(expr: BoundExpr) -> set[str]:
    """收集表达式中全部列引用的限定符集合。"""
    return {column.qualifier for column in expr_columns(expr)}


def remap_column(column: LogicalColumn, mapping: Mapping[int, int]) -> LogicalColumn:
    """按「旧 index → 新 index」的映射重塑列引用，只改 index，其余字段不动。

    映射缺失说明该引用指向已被裁掉的列，属实现错误：宁可当场报错，也不静默读错列。
    """
    try:
        index = mapping[column.index]
    except KeyError:
        raise SqlError(
            E_TYPE_MISMATCH,
            f"column mapping missing: {column.qualifier}.{column.name}@{column.index}",
        ) from None
    if index == column.index:
        return column
    return LogicalColumn(column.table, column.qualifier, column.name, index, column.type)


def remap_expr(expr: BoundExpr, mapping: Mapping[int, int]) -> BoundExpr:
    """按「旧 index → 新 index」的映射改写表达式中全部列引用。"""
    return map_columns(expr, lambda column: remap_column(column, mapping))


# ---------- 树遍历 ----------


def map_plan(
    plan: LogicalPlan,
    node_rewrite: Callable[[LogicalPlan], LogicalPlan],
) -> LogicalPlan:
    """整树自底向上重写：先重写子节点，再对重建后的本节点应用 node_rewrite。

    自底向上保证父节点看得到子节点的改写结果——Filter 合并、多层下推、裁剪
    都依赖「子节点已落定」这个前提。
    """
    children = tuple(map_plan(child, node_rewrite) for child in plan.children)
    return node_rewrite(with_children(plan, children))


def with_children(plan: LogicalPlan, children: tuple[LogicalPlan, ...]) -> LogicalPlan:
    """用新的子节点重建本节点；子节点未变时原样返回，避免无谓重建。"""
    if not children or children == plan.children:
        return plan
    match plan:
        case LogicalFilter():
            return LogicalFilter(plan.predicate, children[0])
        case LogicalProjection():
            return LogicalProjection(plan.columns, plan.output_names, children[0])
        case LogicalJoin():
            return LogicalJoin(children[0], children[1], plan.on, plan.schema, plan.kind)
        case _:
            return plan


def _flatten_layer(expr: BoundExpr, op: LogicOp) -> tuple[BoundExpr, ...]:
    """把同层同操作符的逻辑链递归展平为单一层，顺序不变。"""
    if isinstance(expr, BoundLogical) and expr.op is op:
        terms: list[BoundExpr] = []
        for term in expr.terms:
            terms.extend(_flatten_layer(term, op))
        return tuple(terms)
    return (expr,)


def _flatten_and(expr: BoundExpr) -> tuple[BoundExpr, ...]:
    """展平 AND 链。"""
    return _flatten_layer(expr, LogicOp.AND)


def _flatten_or(expr: BoundExpr) -> tuple[BoundExpr, ...]:
    """展平 OR 链。"""
    return _flatten_layer(expr, LogicOp.OR)


def _is_bool_literal(expr: BoundExpr, value: bool) -> bool:
    """判断表达式是否为指定的布尔字面量（TRUE / FALSE）。"""
    return (
        isinstance(expr, BoundLiteral)
        and expr.type is SqlType.BOOLEAN
        and expr.value == value
    )


def _bool_literal(value: bool) -> BoundLiteral:
    """构造布尔字面量。"""
    return BoundLiteral(value, SqlType.BOOLEAN)


def _dedup(terms: tuple[BoundExpr, ...]) -> tuple[BoundExpr, ...]:
    """同层按结构相等去重，保持首次出现顺序（布尔幂等律）。"""
    return tuple(dict.fromkeys(terms))


# ---------- 规则 1：常量折叠 ----------


def fold_constants(plan: LogicalPlan) -> RuleResult:
    """规则 1：常量折叠——所有操作数均为字面量的可求值节点当场求值。"""
    folded = 0

    def fold(node: BoundExpr) -> BoundExpr:
        nonlocal folded
        rewritten = fold_expr(node)
        if rewritten != node:
            folded += 1
        return rewritten

    def node_rewrite(node: LogicalPlan) -> LogicalPlan:
        match node:
            case LogicalFilter():
                return LogicalFilter(rewrite_expr(node.predicate, fold), node.child)
            case LogicalJoin():
                return LogicalJoin(
                    node.left,
                    node.right,
                    rewrite_expr(node.on, fold),
                    node.schema,
                    node.kind,
                )
            case _:
                return node

    rewritten = map_plan(plan, node_rewrite)
    parts = [f"折叠 {folded} 处常量表达式"] if folded else []
    return RuleResult(rewritten, _summarize_hits(parts))


def fold_expr(node: BoundExpr) -> BoundExpr:
    """对单个表达式节点做常量折叠（子节点已折叠）。

    求值直接复用运行期的 cmp_eval / cast_value：绑定期已完成类型协调，
    折叠结果与运行期逐行求值由同一个函数产生，不引入新的语义规则。
    """
    match node:
        case BoundComparison(left=BoundLiteral(), right=BoundLiteral()):
            return _bool_literal(cmp_eval(node.op, node.left.value, node.right.value))
        case BoundUnaryNot(operand=BoundLiteral()):
            return _bool_literal(not node.operand.value)
        case BoundCast(expr=BoundLiteral(), target=target):
            return BoundLiteral(cast_value(node.expr.value, target), target)
        case _:
            return node


# ---------- 规则 2：AND 展平与 Filter 合并 ----------


def flatten_and_merge_filters(plan: LogicalPlan) -> RuleResult:
    """规则 2：AND 链展平 + 相邻 Filter 合并为一个（上层谓词在前）。"""
    flattened = 0
    merged = 0

    def node_rewrite(node: LogicalPlan) -> LogicalPlan:
        nonlocal flattened, merged
        match node:
            case LogicalFilter(
                predicate=BoundLogical(op=LogicOp.AND) as predicate,
                child=LogicalFilter() as inner,
            ):
                terms = _flatten_and(predicate) + _flatten_and(inner.predicate)
                flattened += 1
                merged += 1
                return LogicalFilter(BoundLogical(LogicOp.AND, terms), inner.child)
            case LogicalFilter(predicate=BoundLogical(op=LogicOp.AND) as predicate):
                terms = _flatten_and(predicate)
                if terms == predicate.terms:
                    return node
                flattened += 1
                return LogicalFilter(BoundLogical(LogicOp.AND, terms), node.child)
            case LogicalJoin(on=BoundLogical(op=LogicOp.AND) as on):
                terms = _flatten_and(on)
                if terms == on.terms:
                    return node
                flattened += 1
                return LogicalJoin(
                    node.left,
                    node.right,
                    BoundLogical(LogicOp.AND, terms),
                    node.schema,
                    node.kind,
                )
            case _:
                return node

    rewritten = map_plan(plan, node_rewrite)
    parts = []
    if flattened:
        parts.append(f"展平 {flattened} 处 AND 链")
    if merged:
        parts.append(f"合并 {merged} 处相邻 Filter")
    return RuleResult(rewritten, _summarize_hits(parts))


# ---------- 规则 3：布尔规范化 ----------

# 比较运算符取反表：二值逻辑下与 NOT 逐值等价（V3 无 NULL，即无 UNKNOWN）
_NEGATED_OP = {
    ComparisonOp.EQ: ComparisonOp.NE,
    ComparisonOp.NE: ComparisonOp.EQ,
    ComparisonOp.LT: ComparisonOp.GE,
    ComparisonOp.GE: ComparisonOp.LT,
    ComparisonOp.LE: ComparisonOp.GT,
    ComparisonOp.GT: ComparisonOp.LE,
}


def normalize_booleans(plan: LogicalPlan) -> RuleResult:
    """规则 3：布尔规范化——NOT 下推、恒真恒假化简、同层去重。"""
    hits: dict[str, int] = {}

    def node_rewrite(node: LogicalPlan) -> LogicalPlan:
        match node:
            case LogicalFilter():
                return LogicalFilter(
                    _normalize_conjunction(node.predicate, hits), node.child
                )
            case LogicalJoin():
                return LogicalJoin(
                    node.left,
                    node.right,
                    _normalize_conjunction(node.on, hits),
                    node.schema,
                    node.kind,
                )
            case _:
                return node

    rewritten = map_plan(plan, node_rewrite)
    parts = []
    if hits.get("NOT 下推"):
        parts.append(f"下推 {hits['NOT 下推']} 处 NOT")
    if hits.get("真值化简"):
        parts.append(f"化简 {hits['真值化简']} 处常量项")
    if hits.get("同层去重"):
        parts.append(f"去除 {hits['同层去重']} 项重复")
    return RuleResult(rewritten, _summarize_hits(parts))


def _hit(hits: dict[str, int], label: str, amount: int = 1) -> None:
    """累加命中计数；零增量不记录，避免摘要里出现 0 值。"""
    if amount:
        hits[label] = hits.get(label, 0) + amount


def _normalize_conjunction(
    predicate: BoundLogical,
    hits: dict[str, int],
) -> BoundLogical:
    """规范化顶层 AND 信封：逐项规范化后展平、去 TRUE、短路 FALSE、同层去重。

    信封必须保留（LogicalFilter / LogicalJoin 都要求顶层为非空 AND）：整体化为常量
    TRUE / FALSE 时同样只留单元素信封，冗余节点消除由裁剪规则负责，绝不构造空 AND。
    """
    terms: list[BoundExpr] = []
    for term in predicate.terms:
        terms.extend(_flatten_and(simplify_expr(term, hits)))
    if any(_is_bool_literal(term, False) for term in terms):
        _hit(hits, "真值化简", len(terms))
        return BoundLogical(LogicOp.AND, (_bool_literal(False),))
    without_constants = tuple(t for t in terms if not _is_bool_literal(t, True))
    _hit(hits, "真值化简", len(terms) - len(without_constants))
    kept = _dedup(without_constants)
    _hit(hits, "同层去重", len(without_constants) - len(kept))
    return BoundLogical(LogicOp.AND, kept or (_bool_literal(True),))


def simplify_expr(expr: BoundExpr, hits: dict[str, int]) -> BoundExpr:
    """自底向上化简布尔表达式（NOT 下推 + 真值化简 + 同层去重）。"""
    return rewrite_expr(expr, lambda node: _simplify_node(node, hits))


def _simplify_node(node: BoundExpr, hits: dict[str, int]) -> BoundExpr:
    match node:
        case BoundUnaryNot(operand=BoundLiteral()):
            _hit(hits, "真值化简")
            return _bool_literal(not node.operand.value)
        case BoundUnaryNot(operand=BoundUnaryNot()):
            _hit(hits, "NOT 下推")
            return node.operand.operand
        case BoundUnaryNot(operand=BoundComparison()):
            _hit(hits, "NOT 下推")
            # 双侧均为字面量时交由常量折叠收尾
            return fold_expr(
                BoundComparison(
                    node.operand.left, _NEGATED_OP[node.operand.op], node.operand.right
                )
            )
        case BoundUnaryNot(operand=BoundLogical(op=LogicOp.AND)):
            _hit(hits, "NOT 下推")
            return simplify_expr(
                BoundLogical(
                    LogicOp.OR, tuple(BoundUnaryNot(term) for term in node.operand.terms)
                ),
                hits,
            )
        case BoundUnaryNot(operand=BoundLogical(op=LogicOp.OR)):
            _hit(hits, "NOT 下推")
            return simplify_expr(
                BoundLogical(
                    LogicOp.AND, tuple(BoundUnaryNot(term) for term in node.operand.terms)
                ),
                hits,
            )
        case BoundLogical():
            return _simplify_logical(node, hits)
        case _:
            return node


def _simplify_logical(node: BoundLogical, hits: dict[str, int]) -> BoundExpr:
    """AND / OR 的真值化简与去重；结果可能是更短的链或单个项。"""
    terms = _flatten_and(node) if node.op is LogicOp.AND else _flatten_or(node)
    if node.op is LogicOp.AND:
        if any(_is_bool_literal(term, False) for term in terms):
            _hit(hits, "真值化简", len(terms))
            return _bool_literal(False)
        without_constants = tuple(t for t in terms if not _is_bool_literal(t, True))
    else:
        if any(_is_bool_literal(term, True) for term in terms):
            _hit(hits, "真值化简", len(terms))
            return _bool_literal(True)
        without_constants = tuple(t for t in terms if not _is_bool_literal(t, False))
    _hit(hits, "真值化简", len(terms) - len(without_constants))
    kept = _dedup(without_constants)
    _hit(hits, "同层去重", len(without_constants) - len(kept))
    if not kept:
        # 全部项都被常量吸收：AND 的项全为 TRUE、OR 的项全为 FALSE
        return _bool_literal(node.op is LogicOp.AND)
    if len(kept) == 1:
        return kept[0]
    return BoundLogical(node.op, kept)


# ---------- 规则 4：JOIN 谓词下推 ----------


def push_join_predicates(plan: LogicalPlan) -> RuleResult:
    """规则 4：把只引用 JOIN 单侧的 conjunct 下推到对应子树。

    conjunct 有两个来源，都处理：

    - JOIN 之上的 ``LogicalFilter``（WHERE 条件）：拆完若已无 conjunct 则删除该 Filter；
    - ``LogicalJoin.on``：单侧项下推到对应子树，ON 只留跨侧项。ON 必须保持非空 AND
      信封，因此全部项都推走时留一个 TRUE 占位。

    只处理 INNER JOIN：有外表补 NULL 语义的连接不允许把条件下推，否则会漏行。
    当前只有 INNER 可构造，模式匹配显式写出是为了让这条前提在代码里可见，
    将来引入 OUTER 时不会被静默继承。
    """
    where_left = 0
    where_right = 0
    on_left = 0
    on_right = 0

    def node_rewrite(node: LogicalPlan) -> LogicalPlan:
        nonlocal where_left, where_right, on_left, on_right
        match node:
            # ON 里的单侧项：先处理 JOIN 自身（map_plan 自底向上，子节点已落定）
            case LogicalJoin(kind=JoinType.INNER) as join:
                new_join, left_terms, right_terms, keep = _push_single_side(
                    join, join.on.terms
                )
                if new_join is join:
                    return node
                on_left += len(left_terms)
                on_right += len(right_terms)
                new_on = BoundLogical(LogicOp.AND, keep or (_bool_literal(True),))
                return LogicalJoin(
                    new_join.left,
                    new_join.right,
                    new_on,
                    new_join.schema,
                    new_join.kind,
                )
            # WHERE 条件：Filter 拆空后自行消失，条件全部落到 JOIN 之下
            case LogicalFilter(
                predicate=predicate, child=LogicalJoin(kind=JoinType.INNER) as join
            ):
                new_join, left_terms, right_terms, keep = _push_single_side(
                    join, predicate.terms
                )
                if new_join is join:
                    return node
                where_left += len(left_terms)
                where_right += len(right_terms)
                if not keep:
                    return new_join
                return LogicalFilter(BoundLogical(LogicOp.AND, keep), new_join)
            case _:
                return node

    rewritten = map_plan(plan, node_rewrite)
    parts = []
    if where_left:
        parts.append(f"下推 {where_left} 项到左子树")
    if where_right:
        parts.append(f"下推 {where_right} 项到右子树")
    if on_left:
        parts.append(f"下推 {on_left} 项 ON 到左子树")
    if on_right:
        parts.append(f"下推 {on_right} 项 ON 到右子树")
    return RuleResult(rewritten, _summarize_hits(parts))


def _push_single_side(
    join: LogicalJoin,
    terms: tuple[BoundExpr, ...],
) -> tuple[LogicalJoin, tuple[BoundExpr, ...], tuple[BoundExpr, ...], tuple[BoundExpr, ...]]:
    """把只引用单侧的 conjunct 挂到对应子树，返回新 JOIN 与拆分后的三组项。

    无可下推项时原样返回 join（调用方据此判断本节点未被改写）。左子树的行就是
    拼接行的前缀，位置不变；右侧必须按左子树列数平移 index。
    """
    left_terms, right_terms, keep = _split_by_side(terms, join)
    if not left_terms and not right_terms:
        return join, left_terms, right_terms, keep
    left = _merge_filter(join.left, left_terms) if left_terms else join.left
    right = join.right
    if right_terms:
        right = _merge_filter(join.right, _shift_to_right(right_terms, join))
    return (
        LogicalJoin(left, right, join.on, join.schema, join.kind),
        left_terms,
        right_terms,
        keep,
    )


def _split_by_side(
    terms: tuple[BoundExpr, ...],
    join: LogicalJoin,
) -> tuple[tuple[BoundExpr, ...], tuple[BoundExpr, ...], tuple[BoundExpr, ...]]:
    """按 conjunct 引用的限定符拆分：只引用左 / 只引用右 / 无法拆分。

    用 qualifier 而非 table：别名场景下 SQL 可见的限定符是别名，且 join_schema 保证
    两侧限定符不相交，判定不会两侧同时成立。OR 节点按整体并集判定，不拆开判断。
    不引用任何列的 conjunct 不视为「只引用单侧」，留在原处（Filter 之上或 ON 之内）。
    """
    left_qualifiers = set(join.left.output_schema.qualifiers)
    right_qualifiers = set(join.right.output_schema.qualifiers)
    left_terms: list[BoundExpr] = []
    right_terms: list[BoundExpr] = []
    keep: list[BoundExpr] = []
    for term in terms:
        qualifiers = expr_qualifiers(term)
        if qualifiers and qualifiers <= left_qualifiers:
            left_terms.append(term)
        elif qualifiers and qualifiers <= right_qualifiers:
            right_terms.append(term)
        else:
            keep.append(term)
    return tuple(left_terms), tuple(right_terms), tuple(keep)


def _merge_filter(child: LogicalPlan, terms: tuple[BoundExpr, ...]) -> LogicalPlan:
    """把 conjunct 以 Filter 形式挂到 child 上；child 已是 Filter 时合并（新谓词在前）。"""
    if isinstance(child, LogicalFilter):
        return LogicalFilter(
            BoundLogical(LogicOp.AND, terms + child.predicate.terms), child.child
        )
    return LogicalFilter(BoundLogical(LogicOp.AND, terms), child)


def _shift_to_right(
    terms: tuple[BoundExpr, ...],
    join: LogicalJoin,
) -> tuple[BoundExpr, ...]:
    """把 conjunct 的列引用从拼接行换算到右子树行：index 减去左子树列数。

    左子树的行就是拼接行的前缀，位置不变；右侧必须显式做减法，漏做会静默读错列。
    """
    shift = len(join.left.output_schema.columns)
    width = len(join.right.output_schema.columns)
    mapping = {index: index - shift for index in range(shift, shift + width)}
    return tuple(remap_expr(term, mapping) for term in terms)


# ---------- 规则 5：投影裁剪与冗余节点消除 ----------


def prune_and_eliminate(plan: LogicalPlan) -> RuleResult:
    """规则 5：自顶向下算出每个节点必须输出哪些列，按需重建 Schema 与列引用，
    并删除化简后失去意义的节点。

    两个阶段（映射方向决定顺序不可颠倒）：
    1. 自顶向下传播「父节点要求本节点输出哪些列」；
    2. 自底向上重建各节点的新 Schema 与「旧 index → 新 index」映射，再用子节点的
       映射改写父节点自身的谓词、ON 与投影列。
    """
    hits: dict[str, int] = {}
    needs: dict[int, frozenset[ColumnKey]] = {}
    _propagate(plan, frozenset(), needs)
    pruned, _ = _rebuild(plan, needs, hits)
    parts = []
    if hits.get("裁剪来源"):
        parts.append(
            f"裁剪 {hits['裁剪来源']} 处来源（列 -{hits['裁掉列']}）"
        )
    if hits.get("删除冗余 Filter"):
        parts.append(f"删除 {hits['删除冗余 Filter']} 处恒真 Filter")
    if hits.get("替换为 Empty"):
        parts.append(f"替换 {hits['替换为 Empty']} 处为 Empty")
    return RuleResult(pruned, _summarize_hits(parts))


def _propagate(
    plan: LogicalPlan,
    need: frozenset[ColumnKey],
    needs: dict[int, frozenset[ColumnKey]],
) -> None:
    """阶段 1：把需求自顶向下铺到每个节点上。"""
    needs[id(plan)] = need
    for child, child_need in zip(plan.children, _children_needs(plan, need)):
        _propagate(child, child_need, needs)


def _children_needs(
    plan: LogicalPlan,
    need: frozenset[ColumnKey],
) -> tuple[frozenset[ColumnKey], ...]:
    """把父节点需求翻译成对每个子节点的需求。"""
    match plan:
        case LogicalProjection():
            # 投影承载最终列顺序与表头语义，需求由自身 columns 给出
            return (frozenset(column_key(col.column) for col in plan.columns),)
        case LogicalFilter():
            return (need | frozenset(column_key(col) for col in expr_columns(plan.predicate)),)
        case LogicalJoin():
            return (_side_need(plan, plan.left, need), _side_need(plan, plan.right, need))
        case _:
            return ()


def _side_need(join: LogicalJoin, side: LogicalPlan, need: frozenset[ColumnKey]) -> frozenset[ColumnKey]:
    """一侧的需求：ON 中属于该侧的列 ∪ 父需求中属于该侧的列。"""
    qualifiers = set(side.output_schema.qualifiers)
    from_on = (
        column_key(column)
        for column in expr_columns(join.on)
        if column.qualifier in qualifiers
    )
    from_parent = (key for key in need if key.qualifier in qualifiers)
    return frozenset(from_on) | frozenset(from_parent)


def _rebuild(
    plan: LogicalPlan,
    needs: dict[int, frozenset[ColumnKey]],
    hits: dict[str, int],
) -> tuple[LogicalPlan, dict[int, int]]:
    """阶段 2：重建节点并返回本节点「旧 index → 新 index」的输出映射。"""
    need = needs[id(plan)]
    rebuilt = tuple(_rebuild(child, needs, hits) for child in plan.children)
    match plan:
        case LogicalScan():
            schema, mapping = _prune_schema(plan.schema, need, hits)
            if schema is plan.schema:
                return plan, mapping
            return LogicalScan(plan.table, schema, plan.alias), mapping
        case LogicalEmpty():
            return plan, _identity_mapping(plan.schema)
        case LogicalFilter():
            child, child_mapping = rebuilt[0]
            return _rebuild_filter(plan.predicate, child, child_mapping, hits)
        case LogicalProjection():
            child, child_mapping = rebuilt[0]
            # 投影列按 child 的映射改写（它们求值时吃的是 child 的输出行）
            columns = tuple(
                BoundColumnRef(remap_column(col.column, child_mapping))
                for col in plan.columns
            )
            node = LogicalProjection(columns, plan.output_names, child)
            return node, _identity_mapping(node.output_schema)
        case LogicalJoin():
            (left, _), (right, _) = rebuilt
            return _rebuild_join(plan, left, right, hits)
        case _:
            return plan, _identity_mapping(plan.output_schema)


def _rebuild_filter(
    predicate: BoundExpr,
    child: LogicalPlan,
    child_mapping: dict[int, int],
    hits: dict[str, int],
) -> tuple[LogicalPlan, dict[int, int]]:
    """重写 Filter 的谓词并做冗余消除。

    Filter 形状不变，因此它自身的输出映射就是 child 的映射。
    """
    new_predicate = remap_expr(predicate, child_mapping)
    truth = _constant_truth(new_predicate)
    if truth is True:
        _hit(hits, "删除冗余 Filter")
        return child, child_mapping
    if truth is False or isinstance(child, LogicalEmpty):
        _hit(hits, "替换为 Empty")
        return LogicalEmpty(child.output_schema), child_mapping
    return LogicalFilter(new_predicate, child), child_mapping


def _rebuild_join(
    plan: LogicalJoin,
    left: LogicalPlan,
    right: LogicalPlan,
    hits: dict[str, int],
) -> tuple[LogicalPlan, dict[int, int]]:
    """重写 JOIN 的 ON 并做冗余消除（INNER JOIN 的零行传染）。"""
    schema = join_schema(left.output_schema, right.output_schema)
    mapping = _join_mapping(plan.schema, schema)
    if plan.kind is JoinType.INNER and (
        isinstance(left, LogicalEmpty) or isinstance(right, LogicalEmpty)
    ):
        _hit(hits, "替换为 Empty")
        return LogicalEmpty(schema), mapping
    return (
        LogicalJoin(left, right, remap_expr(plan.on, mapping), schema, plan.kind),
        mapping,
    )


def _constant_truth(predicate: BoundExpr) -> bool | None:
    """AND 信封的常量真值：含 FALSE 项为 False，全部为 TRUE 项为 True，否则 None。"""
    terms = _flatten_and(predicate)
    if any(_is_bool_literal(term, False) for term in terms):
        return False
    if all(_is_bool_literal(term, True) for term in terms):
        return True
    return None


def _prune_schema(
    schema: LogicalSchema,
    need: frozenset[ColumnKey],
    hits: dict[str, int],
) -> tuple[LogicalSchema, dict[int, int]]:
    """按需求把 Schema 裁成子序列并重新编号，返回新 Schema 与旧 → 新映射。

    需求集合与自身 Schema 的交集为空时保留裁剪前 Schema 的首列：零列 Scan 的行数
    仍然有意义（它决定上层 JOIN 的产生行数），换成零行会改变结果。
    """
    kept = [column for column in schema.columns if column_key(column) in need]
    if not kept:
        if not schema.columns:
            return schema, {}
        kept = [schema.columns[0]]
    if len(kept) == len(schema.columns):
        return schema, _identity_mapping(schema)
    _hit(hits, "裁剪来源")
    _hit(hits, "裁掉列", len(schema.columns) - len(kept))
    mapping: dict[int, int] = {}
    columns: list[LogicalColumn] = []
    for index, column in enumerate(kept):
        mapping[column.index] = index
        columns.append(
            LogicalColumn(column.table, column.qualifier, column.name, index, column.type)
        )
    return LogicalSchema(tuple(columns)), mapping


def _join_mapping(old: LogicalSchema, new: LogicalSchema) -> dict[int, int]:
    """旧拼接行 → 新拼接行的 index 映射：按 (qualifier, name) 匹配同名列。

    (qualifier, name) 在同一 Schema 内唯一（存储层拒绝重名列，自连接两侧限定符不同），
    找不到的列即被裁掉的列，不产生映射条目。
    """
    positions = {(column.qualifier, column.name): column.index for column in new.columns}
    mapping: dict[int, int] = {}
    for column in old.columns:
        index = positions.get((column.qualifier, column.name))
        if index is not None:
            mapping[column.index] = index
    return mapping


def _identity_mapping(schema: LogicalSchema) -> dict[int, int]:
    """输出列未被裁剪时的恒等映射。"""
    return {column.index: column.index for column in schema.columns}
