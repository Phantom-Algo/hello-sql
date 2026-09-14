"""LogicalPlan 包：对外导出稳定类型（base / expressions / plans / builder）。

- base.py：LogicalColumn、LogicalSchema、LogicalPlan 基类；
- expressions.py：绑定表达式（Bound* 节点、bind_expr、求值）；
- plans.py：计划节点（Scan/Filter/Projection/Join 与 DDL/DML）；
- builder.py：Statement -> 计划树；
"""

from runner.logical_plan.base import (
    EMPTY_SCHEMA,
    LogicalColumn,
    LogicalPlan,
    LogicalSchema,
    join_schema,
)
from runner.logical_plan.builder import DescribeTable, LogicalPlanBuilder
from runner.logical_plan.expressions import (
    ArithOp,
    BoundArith,
    BoundAssignment,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundExpr,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
    LogicOp,
    arith_eval,
    bind_conjunction,
    bind_expr,
    bind_literal,
    cast_value,
    cmp_eval,
    coerce,
    deduce_type,
    eval_expr,
    normalize_literal,
    require_boolean,
)
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropTable,
    LogicalEmpty,
    LogicalFilter,
    LogicalInsert,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
    LogicalUpdate,
    LogicalUseDatabase,
)

__all__ = [
    # base
    "EMPTY_SCHEMA",
    "LogicalColumn",
    "LogicalPlan",
    "LogicalSchema",
    "join_schema",
    # builder
    "DescribeTable",
    "LogicalPlanBuilder",
    # expressions
    "ArithOp",
    "BoundArith",
    "BoundAssignment",
    "BoundCast",
    "BoundColumnRef",
    "BoundComparison",
    "BoundExpr",
    "BoundLiteral",
    "BoundLogical",
    "BoundUnaryNot",
    "ComparisonOp",
    "LogicOp",
    "arith_eval",
    "bind_conjunction",
    "bind_expr",
    "bind_literal",
    "cast_value",
    "cmp_eval",
    "coerce",
    "deduce_type",
    "eval_expr",
    "normalize_literal",
    "require_boolean",
    # plans
    "LogicalCreateDatabase",
    "LogicalCreateTable",
    "LogicalDelete",
    "LogicalDropDatabase",
    "LogicalDropTable",
    "LogicalEmpty",
    "LogicalFilter",
    "LogicalInsert",
    "LogicalJoin",
    "LogicalProjection",
    "LogicalScan",
    "LogicalUpdate",
    "LogicalUseDatabase",
]
