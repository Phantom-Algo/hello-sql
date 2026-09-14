"""逻辑优化器测试：五条规则的改写形状、裁剪后的列 index、迭代驱动与日志。

计划形状断言用结构相等或节点类型，不写完整树的字符串快照——后者每次节点
字段调整都要批量改测试，收益不抵维护成本。
"""

from __future__ import annotations

import unittest

from contracts.ast import And, Column, Cmp, Expr, Literal, Not, Or, SqlType
from runner.executor.context import ExecutionContext
from runner.executor.dql import build_select_executor
from runner.logical_plan import (
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
    LogicOp,
    LogicalColumn,
    LogicalDelete,
    LogicalEmpty,
    LogicalFilter,
    LogicalJoin,
    LogicalPlan,
    LogicalProjection,
    LogicalScan,
    LogicalSchema,
    bind_conjunction,
    join_schema,
)
from runner.logical_plan.optimizer import (
    DEFAULT_RULES,
    MAX_ROUNDS,
    LogicalOptimizer,
    Rule,
    RuleResult,
    render_plan,
)
from runner.logical_plan.optimizer.rules import (
    flatten_and_merge_filters,
    fold_constants,
    fold_expr,
    normalize_booleans,
    prune_and_eliminate,
    push_join_predicates,
)


# ---------- 计划构造辅助 ----------


def _users(alias: str | None = None) -> LogicalSchema:
    """构造 users(id INT, name TEXT, active BOOLEAN)；给了别名则限定符为别名。"""
    return LogicalSchema(
        (
            LogicalColumn.of("users", "id", 0, SqlType.INT, alias),
            LogicalColumn.of("users", "name", 1, SqlType.TEXT, alias),
            LogicalColumn.of("users", "active", 2, SqlType.BOOLEAN, alias),
        )
    )


def _orders(alias: str | None = None) -> LogicalSchema:
    """构造 orders(id INT, user_id INT, total REAL, cancelled BOOLEAN, notes TEXT)。"""
    return LogicalSchema(
        (
            LogicalColumn.of("orders", "id", 0, SqlType.INT, alias),
            LogicalColumn.of("orders", "user_id", 1, SqlType.INT, alias),
            LogicalColumn.of("orders", "total", 2, SqlType.REAL, alias),
            LogicalColumn.of("orders", "cancelled", 3, SqlType.BOOLEAN, alias),
            LogicalColumn.of("orders", "notes", 4, SqlType.TEXT, alias),
        )
    )


def _scan(
    schema: LogicalSchema,
    table: str = "users",
    alias: str | None = None,
) -> LogicalScan:
    return LogicalScan(table=table, schema=schema, alias=alias)


def _column_ref(
    schema: LogicalSchema,
    name: str,
    qualifier: str | None = None,
) -> BoundColumnRef:
    return BoundColumnRef(schema.resolve(name, qualifier))


def _literal_cmp(left: object, op: str, right: object) -> Cmp:
    """字面量 vs 字面量的比较。"""
    return Cmp(Literal(left), op, Literal(right))  # type: ignore[arg-type]


def _column_cmp(name: str, op: str, value: object, qualifier: str | None = None) -> Cmp:
    """列 vs 字面量的比较。"""
    return Cmp(Column(name, qualifier), op, Literal(value))  # type: ignore[arg-type]


def _predicate(node: Expr, schema: LogicalSchema) -> BoundLogical:
    """按绑定期路径绑定 WHERE / ON，得到顶层 AND 信封。"""
    return bind_conjunction(node, schema)


def _filter(predicate: BoundLogical, child: LogicalPlan) -> LogicalFilter:
    return LogicalFilter(predicate=predicate, child=child)


def _projection(
    columns: tuple[BoundColumnRef, ...],
    names: tuple[str, ...],
    child: LogicalPlan,
) -> LogicalProjection:
    return LogicalProjection(columns=columns, output_names=names, child=child)


def _join(left: LogicalPlan, right: LogicalPlan, on: Expr) -> LogicalJoin:
    """按 Builder 的路径构造 JOIN：ON 绑定在拼接 Schema 上。"""
    merged = join_schema(left.output_schema, right.output_schema)
    return LogicalJoin(
        left=left,
        right=right,
        on=_predicate(on, merged),
        schema=merged,
    )


def _users_orders_join() -> LogicalJoin:
    """users AS u JOIN orders AS o ON u.id = o.user_id（拼接行共 8 列）。"""
    return _join(
        _scan(_users("u"), alias="u"),
        _scan(_orders("o"), table="orders", alias="o"),
        Cmp(Column("id", "u"), "=", Column("user_id", "o")),
    )


class FoldConstantsTest(unittest.TestCase):
    """规则 1：字面量比较、NOT、CAST 在计划里当场求值。"""

    def test_literal_comparison_folds_for_all_operators(self) -> None:
        cases = [
            ("=", 1, 1, True),
            ("=", 1, 2, False),
            ("<>", 1, 2, True),
            ("<", 1, 2, True),
            ("<=", 2, 2, True),
            (">", 2, 1, True),
            (">=", 1, 2, False),
        ]
        for op, left, right, expected in cases:
            with self.subTest(op=op, left=left, right=right):
                schema = _users()
                plan = _filter(
                    _predicate(_literal_cmp(left, op, right), schema), _scan(schema)
                )

                folded = fold_constants(plan).plan

                self.assertEqual(
                    folded.predicate,
                    BoundLogical(
                        LogicOp.AND, (BoundLiteral(expected, SqlType.BOOLEAN),)
                    ),
                )

    def test_numeric_promotion_uses_runtime_evaluation(self) -> None:
        # 绑定已完成 INT -> REAL 提升，折叠复用 cmp_eval，结果与运行期一致
        schema = _users()
        plan = _filter(_predicate(_literal_cmp(1, "=", 1.0), schema), _scan(schema))

        folded = fold_constants(plan).plan

        self.assertEqual(folded.predicate.terms[0], BoundLiteral(True, SqlType.BOOLEAN))

    def test_boolean_literal_negation_folds(self) -> None:
        for value, expected in ((True, False), (False, True)):
            with self.subTest(value=value):
                schema = _users()
                plan = _filter(_predicate(Not(Literal(value)), schema), _scan(schema))

                folded = fold_constants(plan).plan

                self.assertEqual(
                    folded.predicate.terms[0], BoundLiteral(expected, SqlType.BOOLEAN)
                )

    def test_cast_literal_folds_to_target_type(self) -> None:
        self.assertEqual(
            fold_expr(BoundCast(BoundLiteral(1, SqlType.INT), SqlType.REAL)),
            BoundLiteral(1.0, SqlType.REAL),
        )

    def test_column_comparison_is_left_alone(self) -> None:
        schema = _users()
        plan = _filter(_predicate(_column_cmp("id", "=", 1), schema), _scan(schema))
        original = plan.predicate.terms[0]

        folded = fold_constants(plan).plan

        self.assertEqual(folded.predicate.terms[0], original)

    def test_literal_inside_nested_term_folds(self) -> None:
        # 折叠是整树递归，不只作用于顶层 conjunct
        schema = _users()
        plan = _filter(
            _predicate(Or(_literal_cmp(1, "=", 1), _column_cmp("id", "=", 2)), schema),
            _scan(schema),
        )

        folded = fold_constants(plan).plan

        or_term = folded.predicate.terms[0]
        self.assertIsInstance(or_term, BoundLogical)
        self.assertEqual(or_term.terms[0], BoundLiteral(True, SqlType.BOOLEAN))  # type: ignore[union-attr]


class FlattenAndMergeFiltersTest(unittest.TestCase):
    """规则 2：AND 展平与相邻 Filter 合并。"""

    def test_nested_and_is_flattened_in_order(self) -> None:
        schema = _users()
        first = _predicate(_column_cmp("id", "=", 1), schema).terms[0]
        second = _predicate(_column_cmp("name", "=", "x"), schema).terms[0]
        third = _predicate(_column_cmp("active", "=", True), schema).terms[0]
        nested = BoundLogical(
            LogicOp.AND, (first, BoundLogical(LogicOp.AND, (second, third)))
        )
        plan = _filter(nested, _scan(schema))

        flattened = flatten_and_merge_filters(plan).plan

        self.assertEqual(
            flattened.predicate, BoundLogical(LogicOp.AND, (first, second, third))
        )

    def test_adjacent_filters_merge_with_upper_predicate_first(self) -> None:
        schema = _users()
        upper = _predicate(_column_cmp("id", "=", 1), schema)
        lower = _predicate(_column_cmp("name", "=", "x"), schema)
        plan = _filter(upper, _filter(lower, _scan(schema)))

        merged = flatten_and_merge_filters(plan).plan

        self.assertIsInstance(merged, LogicalFilter)
        self.assertEqual(
            merged.predicate, BoundLogical(LogicOp.AND, (upper.terms[0], lower.terms[0]))
        )
        self.assertIsInstance(merged.child, LogicalScan)

    def test_chain_of_filters_collapses_in_one_pass(self) -> None:
        schema = _users()
        first = _predicate(_column_cmp("id", "=", 1), schema).terms[0]
        second = _predicate(_column_cmp("name", "=", "x"), schema).terms[0]
        third = _predicate(_column_cmp("active", "=", True), schema).terms[0]
        plan = _filter(
            BoundLogical(LogicOp.AND, (first,)),
            _filter(
                BoundLogical(LogicOp.AND, (second,)),
                _filter(BoundLogical(LogicOp.AND, (third,)), _scan(schema)),
            ),
        )

        merged = flatten_and_merge_filters(plan).plan

        self.assertEqual(
            merged.predicate, BoundLogical(LogicOp.AND, (first, second, third))
        )

    def test_join_on_is_flattened(self) -> None:
        join = _users_orders_join()
        term = join.on.terms[0]
        nested_on = BoundLogical(
            LogicOp.AND, (term, BoundLogical(LogicOp.AND, (term,)))
        )
        plan = LogicalJoin(join.left, join.right, nested_on, join.schema)

        flattened = flatten_and_merge_filters(plan).plan

        self.assertEqual(flattened.on, BoundLogical(LogicOp.AND, (term, term)))


class NormalizeBooleansTest(unittest.TestCase):
    """规则 3：NOT 下推、恒真恒假化简、同层去重。"""

    def _normalized(self, node: Expr, schema: LogicalSchema) -> BoundLogical:
        return normalize_booleans(
            _filter(_predicate(node, schema), _scan(schema))
        ).plan.predicate

    def test_not_over_and_pushes_down_to_or(self) -> None:
        schema = _users()

        predicate = self._normalized(
            Not(And(_column_cmp("id", "=", 1), _column_cmp("name", "=", "x"))), schema
        )

        or_term = predicate.terms[0]
        self.assertIsInstance(or_term, BoundLogical)
        self.assertEqual(or_term.op, LogicOp.OR)  # type: ignore[union-attr]
        # 每个子项继续下推：NOT(比较) 翻转为取反后的比较
        self.assertEqual(
            [term.op for term in or_term.terms],  # type: ignore[union-attr]
            [ComparisonOp.NE, ComparisonOp.NE],
        )

    def test_not_over_or_pushes_down_to_and(self) -> None:
        schema = _users()

        predicate = self._normalized(
            Not(Or(_column_cmp("id", "=", 1), _column_cmp("id", "=", 2))), schema
        )

        # 下推结果被顶层信封展平为两个 conjunct
        self.assertEqual(len(predicate.terms), 2)
        self.assertEqual(
            [term.op for term in predicate.terms], [ComparisonOp.NE, ComparisonOp.NE]
        )

    def test_double_negation_cancels(self) -> None:
        schema = _users()
        bound = _predicate(_column_cmp("id", "=", 1), schema).terms[0]

        predicate = self._normalized(Not(Not(_column_cmp("id", "=", 1))), schema)

        self.assertEqual(predicate.terms[0], bound)

    def test_comparison_negation_flips_operator(self) -> None:
        cases = [
            ("=", ComparisonOp.NE),
            ("<>", ComparisonOp.EQ),
            ("<", ComparisonOp.GE),
            (">=", ComparisonOp.LT),
            ("<=", ComparisonOp.GT),
            (">", ComparisonOp.LE),
        ]
        for op, expected in cases:
            with self.subTest(op=op):
                schema = _users()

                predicate = self._normalized(Not(_column_cmp("id", op, 1)), schema)

                term = predicate.terms[0]
                self.assertIsInstance(term, BoundComparison)
                self.assertEqual(term.op, expected)  # type: ignore[union-attr]
                self.assertEqual(  # type: ignore[union-attr]
                    term.left, _column_ref(schema, "id")
                )

    def test_negated_literal_comparison_folds(self) -> None:
        schema = _users()

        predicate = self._normalized(Not(_literal_cmp(1, "=", 2)), schema)

        self.assertEqual(
            predicate.terms[0], BoundLiteral(True, SqlType.BOOLEAN)
        )

    def test_same_layer_duplicates_are_removed(self) -> None:
        schema = _users()
        term = _predicate(_column_cmp("id", "=", 1), schema).terms[0]
        other = _predicate(_column_cmp("id", "=", 2), schema).terms[0]

        predicate = self._normalized(
            And(_column_cmp("id", "=", 1), And(_column_cmp("id", "=", 1), _column_cmp("id", "=", 2))),
            schema,
        )

        self.assertEqual(predicate, BoundLogical(LogicOp.AND, (term, other)))

    def test_true_and_xs_keeps_xs(self) -> None:
        schema = _users()
        term = _predicate(_column_cmp("id", "=", 1), schema).terms[0]

        predicate = self._normalized(
            And(Literal(True), _column_cmp("id", "=", 1)), schema
        )

        self.assertEqual(predicate, BoundLogical(LogicOp.AND, (term,)))

    def test_false_and_xs_collapses_to_false(self) -> None:
        schema = _users()

        predicate = self._normalized(
            And(Literal(False), _column_cmp("id", "=", 1)), schema
        )

        self.assertEqual(
            predicate,
            BoundLogical(LogicOp.AND, (BoundLiteral(False, SqlType.BOOLEAN),)),
        )

    def test_true_or_xs_collapses_to_true(self) -> None:
        schema = _users()

        predicate = self._normalized(
            Or(Literal(True), _column_cmp("id", "=", 1)), schema
        )

        self.assertEqual(
            predicate,
            BoundLogical(LogicOp.AND, (BoundLiteral(True, SqlType.BOOLEAN),)),
        )

    def test_false_or_xs_keeps_xs(self) -> None:
        schema = _users()
        term = _predicate(_column_cmp("id", "=", 1), schema).terms[0]

        predicate = self._normalized(Or(Literal(False), _column_cmp("id", "=", 1)), schema)

        self.assertEqual(predicate, BoundLogical(LogicOp.AND, (term,)))

    def test_envelope_is_preserved_for_constant_predicates(self) -> None:
        # 顶层信封必须是非空 AND：常量 TRUE 只留单元素信封，绝不构造空 AND
        schema = _users()

        predicate = self._normalized(And(Literal(True), Literal(True)), schema)

        self.assertEqual(
            predicate,
            BoundLogical(LogicOp.AND, (BoundLiteral(True, SqlType.BOOLEAN),)),
        )


class PushJoinPredicatesTest(unittest.TestCase):
    """规则 4：把 WHERE（Filter）与 ON 里只引用单侧的 conjunct 下推到对应子树。"""

    def test_left_only_conjunct_is_pushed(self) -> None:
        join = _users_orders_join()
        predicate = _predicate(Column("active", "u"), join.schema)
        plan = _filter(predicate, join)

        pushed = push_join_predicates(plan).plan

        self.assertIsInstance(pushed, LogicalJoin)
        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertEqual(pushed.left.predicate.terms, predicate.terms)  # type: ignore[union-attr]
        self.assertIsInstance(pushed.right, LogicalScan)

    def test_right_only_conjunct_is_pushed_with_index_shift(self) -> None:
        join = _users_orders_join()
        # o.total 在 8 列拼接行中的位置是 5（左子树 3 列 + orders 内位置 2）
        plan = _filter(
            _predicate(Cmp(Column("total", "o"), ">", Literal(20.0)), join.schema),
            join,
        )

        pushed = push_join_predicates(plan).plan

        right = pushed.right
        self.assertIsInstance(right, LogicalFilter)
        term = right.predicate.terms[0]  # type: ignore[union-attr]
        self.assertIsInstance(term, BoundComparison)
        self.assertEqual(term.left.column.index, 2)  # type: ignore[union-attr]
        self.assertEqual(right.child.schema.columns[2].name, "total")  # type: ignore[union-attr]

    def test_cross_side_conjunct_stays_above_join(self) -> None:
        join = _users_orders_join()
        on_term = join.on.terms[0]
        plan = _filter(BoundLogical(LogicOp.AND, (on_term,)), join)

        pushed = push_join_predicates(plan).plan

        self.assertIsInstance(pushed, LogicalFilter)
        self.assertEqual(pushed.predicate.terms, (on_term,))
        self.assertIsInstance(pushed.child, LogicalJoin)

    def test_or_referencing_both_sides_stays_above_join(self) -> None:
        join = _users_orders_join()
        predicate = _predicate(
            Or(Column("active", "u"), Column("cancelled", "o")), join.schema
        )
        plan = _filter(predicate, join)

        pushed = push_join_predicates(plan).plan

        self.assertIsInstance(pushed, LogicalFilter)
        self.assertEqual(pushed.predicate.terms, predicate.terms)

    def test_filter_is_deleted_when_all_conjuncts_move_down(self) -> None:
        join = _users_orders_join()
        left_term = _predicate(Column("active", "u"), join.schema).terms[0]
        right_term = _predicate(Column("cancelled", "o"), join.schema).terms[0]
        plan = _filter(BoundLogical(LogicOp.AND, (left_term, right_term)), join)

        pushed = push_join_predicates(plan).plan

        self.assertIsInstance(pushed, LogicalJoin)
        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertIsInstance(pushed.right, LogicalFilter)

    def test_pushed_conjunct_merges_with_existing_filter(self) -> None:
        join = _users_orders_join()
        inner = _predicate(_column_cmp("name", "=", "x", "u"), join.left.output_schema)
        existing_filter = _filter(inner, join.left)
        outer_term = _predicate(Column("active", "u"), join.schema).terms[0]
        plan = _filter(
            BoundLogical(LogicOp.AND, (outer_term,)),
            LogicalJoin(existing_filter, join.right, join.on, join.schema),
        )

        pushed = push_join_predicates(plan).plan

        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertEqual(  # type: ignore[union-attr]
            pushed.left.predicate.terms, (outer_term,) + inner.terms
        )
        self.assertIsInstance(pushed.left.child, LogicalScan)  # type: ignore[union-attr]

    def test_on_conjunct_referencing_left_only_is_pushed(self) -> None:
        join = _users_orders_join()
        on = _join(
            join.left,
            join.right,
            And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Column("active", "u"),
            ),
        )

        pushed = push_join_predicates(on).plan

        self.assertIsInstance(pushed, LogicalJoin)
        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertEqual(pushed.left.predicate.terms, (on.on.terms[1],))  # type: ignore[union-attr]
        # ON 只留跨侧项，占位信封不出现
        self.assertEqual(pushed.on, BoundLogical(LogicOp.AND, (on.on.terms[0],)))
        self.assertIsInstance(pushed.right, LogicalScan)

    def test_on_conjunct_referencing_right_only_is_pushed_with_index_shift(self) -> None:
        join = _users_orders_join()
        # o.total 在 8 列拼接行中的位置是 5，落到 orders 行内应回到 2
        on = _join(
            join.left,
            join.right,
            And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Cmp(Column("total", "o"), ">", Literal(20.0)),
            ),
        )

        pushed = push_join_predicates(on).plan

        self.assertIsInstance(pushed.right, LogicalFilter)
        term = pushed.right.predicate.terms[0]  # type: ignore[union-attr]
        self.assertIsInstance(term, BoundComparison)
        self.assertEqual(term.left.column.name, "total")  # type: ignore[union-attr]
        self.assertEqual(term.left.column.index, 2)  # type: ignore[union-attr]
        self.assertIsInstance(pushed.left, LogicalScan)

    def test_on_keeps_true_envelope_when_every_term_is_pushed(self) -> None:
        # ON 必须保持非空 AND 信封：全部项都推走时留一个 TRUE 占位
        join = _users_orders_join()
        on = _join(
            join.left,
            join.right,
            And(Column("active", "u"), Column("cancelled", "o")),
        )

        pushed = push_join_predicates(on).plan

        self.assertEqual(
            pushed.on,
            BoundLogical(LogicOp.AND, (BoundLiteral(True, SqlType.BOOLEAN),)),
        )
        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertIsInstance(pushed.right, LogicalFilter)
        self.assertEqual(pushed.schema, on.schema)

    def test_cross_side_and_or_on_terms_are_left_alone(self) -> None:
        join = _users_orders_join()
        plan = _join(
            join.left,
            join.right,
            And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Or(Column("active", "u"), Column("cancelled", "o")),
            ),
        )

        result = push_join_predicates(plan)

        self.assertEqual(result.plan, plan)
        self.assertEqual(result.summary, "无命中")

    def test_on_pushdown_merges_with_existing_filter(self) -> None:
        join = _users_orders_join()
        inner = _predicate(_column_cmp("name", "=", "x", "u"), join.left.output_schema)
        on = _join(
            _filter(inner, join.left),
            join.right,
            And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Column("active", "u"),
            ),
        )

        pushed = push_join_predicates(on).plan

        self.assertIsInstance(pushed.left, LogicalFilter)
        self.assertEqual(  # type: ignore[union-attr]
            pushed.left.predicate.terms, (on.on.terms[1],) + inner.terms
        )
        self.assertIsInstance(pushed.left.child, LogicalScan)  # type: ignore[union-attr]

    def test_where_and_on_pushdown_are_reported_separately(self) -> None:
        join = _users_orders_join()
        on = _join(
            join.left,
            join.right,
            And(
                Cmp(Column("id", "u"), "=", Column("user_id", "o")),
                Column("active", "u"),
            ),
        )
        plan = _filter(
            _predicate(Column("cancelled", "o"), on.schema), on
        )

        result = push_join_predicates(plan)

        self.assertEqual(
            result.summary,
            "下推 1 项到右子树；下推 1 项 ON 到左子树",
        )


class PruneAndEliminateTest(unittest.TestCase):
    """规则 5：需求传播、Scan 子序列重建、index 重映射与冗余节点消除。"""

    def test_scan_is_pruned_to_needed_columns(self) -> None:
        join = _users_orders_join()
        plan = _projection((_column_ref(join.schema, "name", "u"),), ("u.name",), join)

        pruned = prune_and_eliminate(plan).plan

        left, right = pruned.child.left, pruned.child.right  # type: ignore[union-attr]
        self.assertEqual([column.name for column in left.schema.columns], ["id", "name"])
        self.assertEqual([column.name for column in right.schema.columns], ["user_id"])
        # 输出列 index 从 0 连续编号
        self.assertEqual([column.index for column in left.schema.columns], [0, 1])
        self.assertEqual([column.index for column in right.schema.columns], [0])

    def test_projection_and_join_references_are_remapped(self) -> None:
        join = _users_orders_join()
        plan = _projection((_column_ref(join.schema, "name", "u"),), ("u.name",), join)

        pruned = prune_and_eliminate(plan).plan

        # 投影列按 child（Join）的映射改写：u.name 仍在 1 号位置
        self.assertEqual(pruned.columns[0].column.index, 1)
        # ON 按拼接行映射改写：o.user_id 从 5 号位置移到 2 号位置
        on_term = pruned.child.on.terms[0]  # type: ignore[union-attr]
        self.assertEqual(on_term.left.column.index, 0)
        self.assertEqual(on_term.right.column.index, 2)
        self.assertEqual(
            pruned.child.schema.columns[on_term.right.column.index].name,  # type: ignore[union-attr]
            "user_id",
        )

    def test_zero_column_source_keeps_first_column(self) -> None:
        # orders 一列都不被引用，但不能被裁到零列：它决定 JOIN 的产生行数
        join = _join(
            _scan(_users("u"), alias="u"),
            _scan(_orders("o"), table="orders", alias="o"),
            Column("active", "u"),
        )
        plan = _projection((_column_ref(join.schema, "id", "u"),), ("u.id",), join)

        pruned = prune_and_eliminate(plan).plan

        right = pruned.child.right  # type: ignore[union-attr]
        self.assertIsInstance(right, LogicalScan)
        # 需求为空的来源保留裁剪前 Schema 的首列
        self.assertEqual([column.name for column in right.schema.columns], ["id"])
        # 左侧只被 ON 与投影各引用一列，同样按需求裁剪
        self.assertEqual(
            [column.name for column in pruned.child.left.schema.columns],  # type: ignore[union-attr]
            ["id", "active"],
        )

    def test_trivially_true_filter_is_deleted(self) -> None:
        schema = _users()
        plan = _projection(
            (_column_ref(schema, "id"),),
            ("id",),
            _filter(_predicate(Literal(True), schema), _scan(schema)),
        )

        pruned = prune_and_eliminate(plan).plan

        self.assertIsInstance(pruned.child, LogicalScan)

    def test_trivially_false_filter_becomes_empty(self) -> None:
        schema = _users()
        plan = _projection(
            (_column_ref(schema, "id"),),
            ("id",),
            _filter(_predicate(Literal(False), schema), _scan(schema)),
        )

        pruned = prune_and_eliminate(plan).plan

        self.assertIsInstance(pruned.child, LogicalEmpty)
        # Empty 携带与被替换子树同构的 Schema，上层引用才继续有效
        self.assertEqual(
            [column.name for column in pruned.child.schema.columns], ["id"]  # type: ignore[union-attr]
        )

    def test_filter_over_empty_becomes_empty(self) -> None:
        schema = _users()
        predicate = _predicate(_column_cmp("id", "=", 1), schema)
        plan = _projection(
            (_column_ref(schema, "id"),),
            ("id",),
            _filter(predicate, LogicalEmpty(schema)),
        )

        pruned = prune_and_eliminate(plan).plan

        self.assertIsInstance(pruned.child, LogicalEmpty)

    def test_join_with_empty_side_becomes_empty(self) -> None:
        left = LogicalEmpty(_users("u"))
        right = _scan(_orders("o"), table="orders", alias="o")
        merged = join_schema(left.output_schema, right.output_schema)
        join = LogicalJoin(
            left=left,
            right=right,
            on=_predicate(Cmp(Column("id", "u"), "=", Column("user_id", "o")), merged),
            schema=merged,
        )
        plan = _projection((_column_ref(merged, "name", "u"),), ("u.name",), join)

        pruned = prune_and_eliminate(plan).plan

        self.assertIsInstance(pruned.child, LogicalEmpty)

    def test_projection_over_empty_is_kept(self) -> None:
        # 0 行的结果集仍需正确的列数与表头
        schema = _users()
        plan = _projection(
            (_column_ref(schema, "id"), _column_ref(schema, "name")),
            ("id", "name"),
            LogicalEmpty(schema),
        )

        pruned = prune_and_eliminate(plan).plan

        self.assertIsInstance(pruned, LogicalProjection)
        self.assertEqual(pruned.output_names, ("id", "name"))
        self.assertIsInstance(pruned.child, LogicalEmpty)

    def test_scan_is_reused_when_nothing_is_dropped(self) -> None:
        schema = _users()
        scan = _scan(schema)
        plan = _projection(
            tuple(BoundColumnRef(column) for column in schema.columns),
            tuple(column.name for column in schema.columns),
            scan,
        )

        pruned = prune_and_eliminate(plan).plan

        self.assertIs(pruned.child, scan)


class RuleSummaryTest(unittest.TestCase):
    """摘要由规则自述：说清做了什么改写、改了多少处，驱动层只做搬运。"""

    def test_fold_summary_counts_folded_expressions(self) -> None:
        schema = _users()
        plan = _filter(
            _predicate(
                And(_literal_cmp(1, "=", 1), _column_cmp("id", "=", 2)), schema
            ),
            _scan(schema),
        )

        result = fold_constants(plan)

        self.assertEqual(result.summary, "折叠 1 处常量表达式")

    def test_flatten_summary_counts_flattened_layers(self) -> None:
        schema = _users()
        first = _predicate(_column_cmp("id", "=", 1), schema).terms[0]
        second = _predicate(_column_cmp("name", "=", "x"), schema).terms[0]
        plan = _filter(
            BoundLogical(LogicOp.AND, (first, BoundLogical(LogicOp.AND, (second,)))),
            _scan(schema),
        )

        result = flatten_and_merge_filters(plan)

        self.assertEqual(result.summary, "展平 1 处 AND 链")

    def test_flatten_summary_counts_merged_filters(self) -> None:
        schema = _users()
        upper = _predicate(_column_cmp("id", "=", 1), schema)
        lower = _predicate(_column_cmp("name", "=", "x"), schema)
        plan = _filter(upper, _filter(lower, _scan(schema)))

        result = flatten_and_merge_filters(plan)

        self.assertEqual(result.summary, "展平 1 处 AND 链；合并 1 处相邻 Filter")

    def test_normalize_summary_reports_not_pushdown_and_dedup(self) -> None:
        schema = _users()
        plan = _filter(
            _predicate(
                And(Not(_column_cmp("id", "=", 1)), _column_cmp("name", "=", "x")),
                schema,
            ),
            _scan(schema),
        )

        result = normalize_booleans(plan)

        self.assertEqual(result.summary, "下推 1 处 NOT")

    def test_pushdown_summary_reports_both_sides(self) -> None:
        join = _users_orders_join()
        predicate = _predicate(
            And(Column("active", "u"), Column("cancelled", "o")), join.schema
        )

        result = push_join_predicates(_filter(predicate, join))

        self.assertEqual(result.summary, "下推 1 项到左子树；下推 1 项到右子树")

    def test_prune_summary_reports_pruned_columns_and_eliminations(self) -> None:
        join = _users_orders_join()
        plan = _projection((_column_ref(join.schema, "name", "u"),), ("u.name",), join)

        result = prune_and_eliminate(plan)

        self.assertEqual(result.summary, "裁剪 2 处来源（列 -5）")

    def test_summary_is_no_hit_when_nothing_changes(self) -> None:
        schema = _users()
        plan = _projection(
            tuple(BoundColumnRef(column) for column in schema.columns),
            tuple(column.name for column in schema.columns),
            _scan(schema),
        )

        result = fold_constants(plan)

        self.assertEqual(result.summary, "无命中")
        self.assertEqual(result.plan, plan)


class LogicalOptimizerTest(unittest.TestCase):
    """驱动：迭代上限、日志内容、非查询计划原样保留。"""

    @staticmethod
    def _filtered_join_plan() -> LogicalProjection:
        join = _users_orders_join()
        return _projection(
            (_column_ref(join.schema, "name", "u"),),
            ("u.name",),
            _filter(_predicate(Column("active", "u"), join.schema), join),
        )

    def test_select_plan_reaches_fixpoint(self) -> None:
        plan = self._filtered_join_plan()

        log = LogicalOptimizer().optimize(plan)

        self.assertLessEqual(log.rounds, MAX_ROUNDS)
        self.assertFalse(log.hit_limit)
        self.assertEqual(log.original, plan)
        # 不动点：再跑一次不应产生任何改写
        self.assertEqual(LogicalOptimizer().optimize(log.optimized).applications, ())

    def test_plan_without_optimization_opportunity_logs_nothing(self) -> None:
        schema = _users()
        plan = _projection(
            tuple(BoundColumnRef(column) for column in schema.columns),
            tuple(column.name for column in schema.columns),
            _scan(schema),
        )

        log = LogicalOptimizer().optimize(plan)

        self.assertEqual(log.applications, ())
        self.assertEqual(log.rounds, 1)
        self.assertEqual(log.optimized, plan)

    def test_non_query_plan_is_returned_untouched(self) -> None:
        plan = LogicalDelete(table="users", child=_scan(_users()))

        log = LogicalOptimizer().optimize(plan)

        self.assertIs(log.optimized, plan)
        self.assertEqual(log.applications, ())
        self.assertEqual(log.rounds, 0)

    def test_applications_carry_rule_name_and_single_line_plans(self) -> None:
        log = LogicalOptimizer().optimize(self._filtered_join_plan())
        known_rules = {rule.name for rule in DEFAULT_RULES}

        self.assertTrue(log.applications)
        for application in log.applications:
            with self.subTest(rule=application.rule, summary=application.summary):
                self.assertIn(application.rule, known_rules)
                self.assertTrue(application.summary)
                self.assertNotIn("\n", application.plan_before)
                self.assertNotIn("\n", application.plan_after)

    def test_applications_are_recorded_only_when_plan_changes(self) -> None:
        log = LogicalOptimizer().optimize(self._filtered_join_plan())

        for application in log.applications:
            with self.subTest(rule=application.rule):
                self.assertNotEqual(application.plan_before, application.plan_after)

    def test_log_reports_pushdown_and_pruning(self) -> None:
        log = LogicalOptimizer().optimize(self._filtered_join_plan())
        rules = {application.rule for application in log.applications}

        self.assertIn("push_join_predicates", rules)
        self.assertIn("prune_and_eliminate", rules)

    def test_iteration_limit_stops_without_raising(self) -> None:
        # 构造一条永远会改动计划的规则，验证触顶时只记录日志、不抛错
        def always_change(plan: LogicalPlan) -> RuleResult:
            assert isinstance(plan, LogicalProjection)
            return RuleResult(
                LogicalProjection(
                    plan.columns + plan.columns[:1],
                    plan.output_names + plan.output_names[:1],
                    plan.child,
                ),
                "追加一列",
            )

        schema = _users()
        plan = _projection((_column_ref(schema, "id"),), ("id",), _scan(schema))
        optimizer = LogicalOptimizer(
            rules=(Rule("always_change", always_change),), max_rounds=3
        )

        log = optimizer.optimize(plan)

        self.assertTrue(log.hit_limit)
        self.assertEqual(log.rounds, 3)

    def test_render_plan_is_compact_single_line(self) -> None:
        join = _users_orders_join()
        plan = _projection((_column_ref(join.schema, "name", "u"),), ("u.name",), join)

        rendered = render_plan(plan)

        self.assertNotIn("\n", rendered)
        self.assertTrue(rendered.startswith("Projection -> Join("))
        self.assertIn("Scan(users×3)", rendered)


class EmptyExecutorTest(unittest.TestCase):
    """LogicalEmpty 的执行侧：零行、零副作用、不访问 Storage。"""

    class _NoStorage:
        """任何属性访问都视为失败：EmptyExecutor 不应接触 Storage。"""

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"storage accessed: {name}")

    @staticmethod
    def _forbidden_describe(table: str) -> object:
        raise AssertionError(f"describe called: {table}")

    def test_empty_plan_returns_header_and_no_rows(self) -> None:
        schema = _users()
        plan = _projection(
            (_column_ref(schema, "id"), _column_ref(schema, "name")),
            ("id", "name"),
            LogicalEmpty(schema),
        )
        executor = build_select_executor(plan, self._forbidden_describe)  # type: ignore[arg-type]
        context = ExecutionContext(
            server=self._NoStorage(),  # type: ignore[arg-type]
            storage=self._NoStorage(),  # type: ignore[arg-type]
            current_database="main",
        )

        result = executor.execute(context)

        self.assertEqual(result.columns, ("id", "name"))
        self.assertEqual(result.rows, ())


if __name__ == "__main__":
    unittest.main()
