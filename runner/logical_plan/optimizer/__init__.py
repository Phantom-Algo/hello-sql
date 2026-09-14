"""逻辑优化器包：规则集、驱动与日志。"""

from runner.logical_plan.optimizer.optimizer import (
    DEFAULT_RULES,
    MAX_ROUNDS,
    LogicalOptimizer,
    OptimizationLog,
    Rule,
    RuleApplication,
    render_plan,
)
from runner.logical_plan.optimizer.rules import (
    ColumnKey,
    RuleResult,
    column_key,
    expr_columns,
    expr_qualifiers,
    flatten_and_merge_filters,
    fold_constants,
    normalize_booleans,
    prune_and_eliminate,
    push_join_predicates,
    remap_column,
    remap_expr,
)

__all__ = [
    "DEFAULT_RULES",
    "MAX_ROUNDS",
    "LogicalOptimizer",
    "OptimizationLog",
    "Rule",
    "RuleApplication",
    "render_plan",
    # 规则
    "RuleResult",
    "flatten_and_merge_filters",
    "fold_constants",
    "normalize_booleans",
    "prune_and_eliminate",
    "push_join_predicates",
    # 列引用辅助
    "ColumnKey",
    "column_key",
    "expr_columns",
    "expr_qualifiers",
    "remap_column",
    "remap_expr",
]
