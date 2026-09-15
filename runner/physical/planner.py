"""物理选路：为每个 Scan 决定访问路径，并记录决策理由与两个代价估计。

规划期只读：本模块只消费契约里的数据类型（表结构回调、统计、索引清单），对
Storage 的实际调用由执行器完成，因此规划本身不产生 I/O。每次构建新建一个
PhysicalPlanner，它按表缓存索引清单与统计，缓存范围天然限定在单条语句内——
写入会让统计失效，跨语句缓存是错的。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from contracts.errors import E_BAD_ARG, SqlError
from contracts.storage import IndexInfo, TableStats

from runner.logical_plan.builder import DescribeTable
from runner.logical_plan.expressions import BoundExpr
from runner.logical_plan.plans import LogicalScan
from runner.physical import cost_model
from runner.physical.requests import (
    IndexCandidate,
    IndexLookup,
    IndexRequest,
    translate,
)
from runner.trace_hooks import RunnerTraceSink, trace_runner_operation


PhysicalMode = Literal["auto", "seq", "index"]
"""物理模式取值：auto 按代价选路，seq 强制顺序扫描，index 强制索引访问。"""

PHYSICAL_MODES: tuple[PhysicalMode, ...] = ("auto", "seq", "index")

StatisticsOfTable = Callable[[str], TableStats]
"""读取一张表统计的回调；与 describe 一样必须是动态回调，USE 换库后要取新连接。"""

ListIndexesOfTable = Callable[[str], tuple[IndexInfo, ...]]
"""读取一张表索引清单的回调；同样必须是动态回调。"""


def validate_physical_mode(mode: str) -> PhysicalMode:
    """校验物理模式取值；非法取值不做"当作 auto"的容错，直接抛 E_BAD_ARG。"""
    if mode not in PHYSICAL_MODES:
        raise SqlError(E_BAD_ARG, f"unknown physical mode: {mode!r}")
    return cast(PhysicalMode, mode)


# 理由码：测试与 bench 报告的稳定标识，解释性文字只进追踪事件。
FORCED_SEQ = "FORCED_SEQ"
"""强制顺序扫描。"""

FORCED_INDEX = "FORCED_INDEX"
"""强制索引访问。"""

NO_PREDICATE = "NO_PREDICATE"
"""没有可翻译的比较条件。"""

NO_MATCHING_INDEX = "NO_MATCHING_INDEX"
"""有可翻译条件，但目标列上没有索引。"""

NO_STATS = "NO_STATS"
"""有索引，但统计退化到无法估算。"""

SEQ_CHEAPER = "SEQ_CHEAPER"
"""代价比较取顺序扫描（含等代价）。"""

INDEX_EQUALITY = "INDEX_EQUALITY"
"""等值索引访问胜出。"""

INDEX_RANGE = "INDEX_RANGE"
"""区间索引访问胜出。"""


@dataclass(frozen=True, slots=True)
class AccessPath:
    """一次 Scan 的物理访问决策。requests 为空即顺序扫描，不再单独存路径种类。"""

    table: str
    reason: str
    requests: tuple[IndexRequest, ...] = ()
    column: str | None = None
    selectivity: float | None = None
    estimated_rows: float | None = None
    seq_cost: float | None = None
    index_cost: float | None = None


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Executor 构建期的只读输入：表结构查询与选路器。"""

    describe: DescribeTable
    planner: PhysicalPlanner


@dataclass(frozen=True, slots=True)
class PricedCandidate:
    """已定价的索引候选：选择性、命中行数与索引访问代价。"""

    candidate: IndexCandidate
    selectivity: float
    estimated_rows: float
    cost: float


class PhysicalPlanner:
    """单次构建内的选路器：翻译谓词、估算代价、决定访问路径。

    physical 的三种取值决定决策分支：seq 只构造路径不读元数据；index 只翻译
    谓词、不判断索引是否存在；auto 读索引清单与统计后做代价比较。无论哪种
    模式，索引是否存在最终都由 Storage 判定。
    """

    def __init__(
        self,
        *,
        statistics: StatisticsOfTable,
        list_indexes: ListIndexesOfTable,
        physical: PhysicalMode = "auto",
        trace_sink: RunnerTraceSink | None = None,
    ) -> None:
        self._statistics = statistics
        self._list_indexes = list_indexes
        self._physical = validate_physical_mode(physical)
        self._trace_sink = trace_sink
        self._paths: list[AccessPath] = []
        self._indexes: dict[str, tuple[IndexInfo, ...]] = {}
        self._stats: dict[str, TableStats] = {}

    @property
    def physical(self) -> PhysicalMode:
        """本次构建使用的物理模式。"""
        return self._physical

    @property
    def paths(self) -> tuple[AccessPath, ...]:
        """本次构建已产生的选路记录，按规划顺序排列。"""
        return tuple(self._paths)

    @trace_runner_operation("executor", "plan_access")
    def plan_scan(
        self,
        scan: LogicalScan,
        conjuncts: tuple[BoundExpr, ...],
    ) -> AccessPath:
        """为一次 Scan 选路：翻译 conjuncts，按物理模式决定访问路径。"""
        candidates = _candidates(scan.table, conjuncts)
        if self._physical == "seq":
            path = AccessPath(table=scan.table, reason=FORCED_SEQ)
        elif self._physical == "index":
            path = self._forced_index(scan.table, candidates)
        else:
            path = self._auto(scan.table, candidates)
        self._paths.append(path)
        return path

    def _forced_index(
        self,
        table: str,
        candidates: tuple[IndexCandidate, ...],
    ) -> AccessPath:
        """强制索引：不读统计，等值候选优先，全部候选按优先级交给执行器。

        C 不看索引清单，因此"首选候选没有索引"只能由执行器按序回退解决。
        """
        if not candidates:
            return AccessPath(table=table, reason=NO_PREDICATE)
        ordered = sorted(
            candidates,
            key=lambda item: (0 if item.is_lookup else 1, item.column.index),
        )
        return AccessPath(
            table=table,
            reason=FORCED_INDEX,
            requests=tuple(item.request for item in ordered),
            column=ordered[0].column.name,
        )

    def _auto(
        self,
        table: str,
        candidates: tuple[IndexCandidate, ...],
    ) -> AccessPath:
        """代价选路：任何退化都落到顺序扫描，不抛错。"""
        if not candidates:
            return AccessPath(table=table, reason=NO_PREDICATE)
        matched = self._matching_candidates(table, candidates)
        if not matched:
            return AccessPath(table=table, reason=NO_MATCHING_INDEX)
        stats = self._table_stats(table)
        if stats.row_count > 0 and stats.page_count == 0:
            # 统计不一致：顺序扫描无法定价，只有退化一种选择
            return AccessPath(table=table, reason=NO_STATS)
        priced = _price_candidates(matched, stats)
        if not priced:
            # 候选列的基数全为 0：等值选择性不可估算，该列不产生候选
            return AccessPath(
                table=table,
                reason=NO_STATS,
                seq_cost=cost_model.seq_cost(stats.page_count),
            )
        ordered = tuple(sorted(priced, key=_priced_order))
        best = ordered[0]
        seq = cost_model.seq_cost(stats.page_count)
        if seq <= best.cost:
            # 等代价取顺序扫描，保证确定性与可复现；仍记录落选候选的估算供解释
            return AccessPath(
                table=table,
                reason=SEQ_CHEAPER,
                column=best.candidate.column.name,
                selectivity=best.selectivity,
                estimated_rows=best.estimated_rows,
                seq_cost=seq,
                index_cost=best.cost,
            )
        return AccessPath(
            table=table,
            reason=INDEX_EQUALITY if best.candidate.is_lookup else INDEX_RANGE,
            requests=tuple(item.candidate.request for item in ordered),
            column=best.candidate.column.name,
            selectivity=best.selectivity,
            estimated_rows=best.estimated_rows,
            seq_cost=seq,
            index_cost=best.cost,
        )

    def _matching_candidates(
        self,
        table: str,
        candidates: tuple[IndexCandidate, ...],
    ) -> tuple[IndexCandidate, ...]:
        """筛出目标列上有索引的候选；索引清单按表缓存，每表最多取一次。"""
        indexes = self._index_list(table)
        if not indexes:
            return ()
        available = {info.column for info in indexes}
        return tuple(
            candidate for candidate in candidates if candidate.column.name in available
        )

    def _index_list(self, table: str) -> tuple[IndexInfo, ...]:
        """取表的索引清单；缓存限定在本次构建内。"""
        cached = self._indexes.get(table)
        if cached is None:
            cached = tuple(self._list_indexes(table))
            self._indexes[table] = cached
        return cached

    def _table_stats(self, table: str) -> TableStats:
        """取表的统计；缓存限定在本次构建内。"""
        cached = self._stats.get(table)
        if cached is None:
            cached = self._statistics(table)
            self._stats[table] = cached
        return cached


def _candidates(
    table: str,
    conjuncts: tuple[BoundExpr, ...],
) -> tuple[IndexCandidate, ...]:
    """翻译全部 conjuncts，只保留可翻译的候选（顺序即谓词书写顺序）。"""
    return tuple(
        translated
        for translated in (translate(conjunct, table) for conjunct in conjuncts)
        if isinstance(translated, IndexCandidate)
    )


def _price_candidates(
    matched: tuple[IndexCandidate, ...],
    stats: TableStats,
) -> tuple[PricedCandidate, ...]:
    """逐候选估算选择性与索引代价；统计退化到无法估算的候选被剔除。"""
    columns = {column.name: column for column in stats.columns}
    priced: list[PricedCandidate] = []
    for candidate in matched:
        column_stats = columns.get(candidate.column.name)
        request = candidate.request
        if isinstance(request, IndexLookup):
            selectivity = cost_model.equality_selectivity(
                column_stats, request.key, stats.row_count
            )
        else:
            selectivity = cost_model.range_selectivity(
                column_stats, candidate.column.type, request
            )
        if selectivity is None:
            continue
        hit_rows = stats.row_count * selectivity
        priced.append(
            PricedCandidate(
                candidate=candidate,
                selectivity=selectivity,
                estimated_rows=hit_rows,
                cost=cost_model.index_cost(stats.row_count, hit_rows),
            )
        )
    return tuple(priced)


def _priced_order(item: PricedCandidate) -> tuple[float, float, int]:
    """候选排序：代价小者优先，其次选择性小者，最后列位置靠前者。

    位置取自 LogicalScan.schema，不依赖索引清单的返回顺序，保证结果确定。
    """
    return (item.cost, item.selectivity, item.candidate.column.index)


__all__ = [
    "AccessPath",
    "BuildContext",
    "FORCED_INDEX",
    "FORCED_SEQ",
    "INDEX_EQUALITY",
    "INDEX_RANGE",
    "ListIndexesOfTable",
    "NO_MATCHING_INDEX",
    "NO_PREDICATE",
    "NO_STATS",
    "PHYSICAL_MODES",
    "PhysicalMode",
    "PhysicalPlanner",
    "SEQ_CHEAPER",
    "StatisticsOfTable",
    "validate_physical_mode",
]
