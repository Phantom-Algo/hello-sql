"""F5 逻辑优化器驱动：按固定顺序反复应用规则，直到计划不再变化或达到迭代上限。

日志是优化器的返回值而非全局状态：optimize() 同时返回计划与产生它的记录，
不可能出现「日志与实际执行计划不一致」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from runner.logical_plan.base import LogicalPlan
from runner.logical_plan.optimizer.rules import (
    RuleResult,
    flatten_and_merge_filters,
    fold_constants,
    normalize_booleans,
    prune_and_eliminate,
    push_join_predicates,
)
from runner.logical_plan.plans import (
    LogicalEmpty,
    LogicalFilter,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
)


@dataclass(frozen=True, slots=True)
class Rule:
    """一条优化规则：名字是稳定的诊断标识（改名视为接口变更）。"""

    name: str
    apply: Callable[[LogicalPlan], RuleResult]


@dataclass(frozen=True, slots=True)
class RuleApplication:
    """一次规则命中：规则自述的单行摘要与计划形态的前后对照。"""

    rule: str
    summary: str
    plan_before: str
    plan_after: str


@dataclass(frozen=True, slots=True)
class OptimizationLog:
    """一条语句的一次优化记录。"""

    original: LogicalPlan
    optimized: LogicalPlan
    applications: tuple[RuleApplication, ...]
    rounds: int
    hit_limit: bool


# 迭代上限：防御性兜底，触顶只影响性能、不影响正确性（规则都是单调的）
MAX_ROUNDS = 8

# 规则顺序由依赖方向决定：先消掉字面量噪音，再拆出可下推的 conjunct，
# 然后做布尔规范化，最后才下推与裁剪（裁剪放最后，只做一次）。
DEFAULT_RULES = (
    Rule("fold_constants", fold_constants),
    Rule("flatten_and_merge_filters", flatten_and_merge_filters),
    Rule("normalize_booleans", normalize_booleans),
    Rule("push_join_predicates", push_join_predicates),
    Rule("prune_and_eliminate", prune_and_eliminate),
)

class LogicalOptimizer:
    """规则型逻辑优化器：一组 LogicalPlan → LogicalPlan 的纯变换 + 不动点驱动。"""

    def __init__(
        self,
        rules: tuple[Rule, ...] = DEFAULT_RULES,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self._rules = tuple(rules)
        self._max_rounds = max_rounds

    def optimize(self, plan: LogicalPlan) -> OptimizationLog:
        """反复按固定顺序应用规则，直到计划不再变化或达到迭代上限。

        只优化 SELECT 计划树：UPDATE / DELETE 的 child 用输出行做整行替换，
        列序一旦被改写就会错位写入，因此 DML 与 DDL 的计划原样返回。
        """
        if not isinstance(plan, LogicalProjection):
            return OptimizationLog(
                original=plan,
                optimized=plan,
                applications=(),
                rounds=0,
                hit_limit=False,
            )

        current = plan
        applications: list[RuleApplication] = []
        rounds = 0
        hit_limit = False
        while True:
            if rounds == self._max_rounds:
                # 上限触顶不抛错：记录日志并返回当前计划，正确性不受影响
                hit_limit = True
                break
            rounds += 1
            current, changed = self._run_round(current, applications)
            if not changed:
                break
        return OptimizationLog(
            original=plan,
            optimized=current,
            applications=tuple(applications),
            rounds=rounds,
            hit_limit=hit_limit,
        )

    def _run_round(
        self,
        plan: LogicalPlan,
        applications: list[RuleApplication],
    ) -> tuple[LogicalPlan, bool]:
        """跑一轮全部规则：单条规则内部跑到自身不动点或本轮无变化。"""
        changed = False
        for rule in self._rules:
            for _ in range(self._max_rounds):
                result = rule.apply(plan)
                if result.plan == plan:
                    break
                applications.append(
                    RuleApplication(
                        rule=rule.name,
                        summary=result.summary,
                        plan_before=render_plan(plan),
                        plan_after=render_plan(result.plan),
                    )
                )
                plan = result.plan
                changed = True
        return plan, changed


def render_plan(plan: LogicalPlan) -> str:
    """紧凑单行表示，供日志与基准对比表使用（完整树用计划自身的渲染接口）。

    Scan 带上输出列数，裁剪造成的形态变化才在单行里可见。
    """
    match plan:
        case LogicalScan():
            return f"Scan({plan.table}×{len(plan.output_schema.columns)})"
        case LogicalEmpty():
            return "Empty"
        case LogicalFilter():
            return f"Filter -> {render_plan(plan.child)}"
        case LogicalProjection():
            return f"Projection -> {render_plan(plan.child)}"
        case LogicalJoin():
            return f"Join({render_plan(plan.left)}, {render_plan(plan.right)})"
        case _:
            return type(plan).__name__


