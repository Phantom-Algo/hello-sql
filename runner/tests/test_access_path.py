"""选路层：理由码全覆盖、规划期零额外 I/O、候选排序与按表缓存。

元数据一律用假回调注入：需要断言"这条路径不读元数据"时换成"被调用就失败"的
哨兵，把调用次数变成断言而不是观察值。
"""

from __future__ import annotations

import pytest

from contracts.ast import SqlType
from contracts.errors import E_BAD_ARG, SqlError
from contracts.storage import ColumnStats, IndexInfo, TableStats
from runner.logical_plan.base import LogicalColumn, LogicalSchema
from runner.logical_plan.expressions import (
    BoundColumnRef,
    BoundComparison,
    BoundLiteral,
    ComparisonOp,
)
from runner.logical_plan.plans import LogicalScan
from runner.physical.planner import (
    FORCED_INDEX,
    FORCED_SEQ,
    INDEX_EQUALITY,
    INDEX_RANGE,
    NO_MATCHING_INDEX,
    NO_PREDICATE,
    NO_STATS,
    PHYSICAL_MODES,
    SEQ_CHEAPER,
    AccessPath,
    PhysicalPlanner,
    validate_physical_mode,
)
from runner.physical.requests import IndexLookup, IndexRange


TABLE = "items"

# 基准数据：1000 行占 100 个数据页，树高 3；等值命中 1 行时代价 4 < 100
ID_STATS = ColumnStats("id", 1000, 0, 999)
NAME_STATS = ColumnStats("name", 2, "a", "b")
AMOUNT_STATS = ColumnStats("amount", 1000, 0.0, 999.0)


def _schema() -> LogicalSchema:
    """items(id INT, name TEXT, amount REAL)，index 与建表列序一致。"""
    return LogicalSchema(
        (
            LogicalColumn.of(TABLE, "id", 0, SqlType.INT),
            LogicalColumn.of(TABLE, "name", 1, SqlType.TEXT),
            LogicalColumn.of(TABLE, "amount", 2, SqlType.REAL),
        )
    )


def _scan() -> LogicalScan:
    return LogicalScan(table=TABLE, schema=_schema())


def _column(name: str) -> LogicalColumn:
    return _schema().resolve(name)


def _stats(
    *,
    rows: int = 1000,
    pages: int = 100,
    columns: tuple[ColumnStats, ...] = (ID_STATS, NAME_STATS, AMOUNT_STATS),
) -> TableStats:
    return TableStats(table=TABLE, row_count=rows, page_count=pages, columns=columns)


def _index(column: str) -> IndexInfo:
    return IndexInfo(name=f"idx_{TABLE}_{column}", table=TABLE, column=column)


def _equality(name: str, value: object) -> BoundComparison:
    column = _column(name)
    return BoundComparison(
        BoundColumnRef(column),
        ComparisonOp.EQ,
        BoundLiteral(value, column.type),  # type: ignore[arg-type]
    )


def _range_term(name: str, op: ComparisonOp, value: object) -> BoundComparison:
    column = _column(name)
    return BoundComparison(
        BoundColumnRef(column),
        op,
        BoundLiteral(value, column.type),  # type: ignore[arg-type]
    )


class _Metadata:
    """假元数据源：按表给出统计与索引清单，并记录调用次序。"""

    def __init__(
        self,
        *,
        statistics: dict[str, TableStats] | None = None,
        indexes: dict[str, tuple[IndexInfo, ...]] | None = None,
    ) -> None:
        self.stats = dict(statistics or {})
        self.indexes = dict(indexes or {})
        self.calls: list[tuple[str, str]] = []

    def statistics(self, table: str) -> TableStats:
        self.calls.append(("statistics", table))
        return self.stats[table]

    def list_indexes(self, table: str) -> tuple[IndexInfo, ...]:
        self.calls.append(("list_indexes", table))
        return tuple(self.indexes.get(table, ()))


def _forbidden(operation: str):
    """哨兵回调：被调用即失败，用于断言某条路径不读元数据。"""

    def callback(table: str) -> object:
        raise AssertionError(f"{operation} called: {table}")

    return callback


def _metadata(**kwargs: object) -> _Metadata:
    """带默认统计与索引的假元数据源。"""
    parameters = {"statistics": {TABLE: _stats()}, "indexes": {TABLE: (_index("id"),)}}
    parameters.update(kwargs)
    return _Metadata(**parameters)  # type: ignore[arg-type]


def _planner(
    metadata: _Metadata,
    physical: str = "auto",
) -> PhysicalPlanner:
    return PhysicalPlanner(
        statistics=metadata.statistics,
        list_indexes=metadata.list_indexes,
        physical=physical,  # type: ignore[arg-type]
    )


# ---------- 强制模式：跳过选路与元数据 ----------


def test_seq_mode_forces_sequence_scan_without_reading_metadata() -> None:
    planner = PhysicalPlanner(
        statistics=_forbidden("statistics"),  # type: ignore[arg-type]
        list_indexes=_forbidden("list_indexes"),  # type: ignore[arg-type]
        physical="seq",
    )

    path = planner.plan_scan(_scan(), (_equality("id", 5),))

    assert path == AccessPath(table=TABLE, reason=FORCED_SEQ)
    assert path.requests == ()


def test_seq_mode_survives_an_untranslatable_predicate() -> None:
    planner = PhysicalPlanner(
        statistics=_forbidden("statistics"),  # type: ignore[arg-type]
        list_indexes=_forbidden("list_indexes"),  # type: ignore[arg-type]
        physical="seq",
    )

    path = planner.plan_scan(_scan(), (_range_term("id", ComparisonOp.NE, 5),))

    assert path.reason == FORCED_SEQ


def test_index_mode_forces_index_without_reading_metadata() -> None:
    planner = PhysicalPlanner(
        statistics=_forbidden("statistics"),  # type: ignore[arg-type]
        list_indexes=_forbidden("list_indexes"),  # type: ignore[arg-type]
        physical="index",
    )

    path = planner.plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == FORCED_INDEX
    assert path.requests == (IndexLookup("id", 5),)
    assert path.column == "id"
    # 本模式没有读统计，两个代价估计都不可得
    assert path.seq_cost is None and path.index_cost is None


def test_index_mode_without_candidates_keeps_empty_requests() -> None:
    planner = PhysicalPlanner(
        statistics=_forbidden("statistics"),  # type: ignore[arg-type]
        list_indexes=_forbidden("list_indexes"),  # type: ignore[arg-type]
        physical="index",
    )

    path = planner.plan_scan(_scan(), ())

    assert path == AccessPath(table=TABLE, reason=NO_PREDICATE)


def test_index_mode_orders_equality_before_range() -> None:
    planner = _planner(_metadata(indexes={}, statistics={}), physical="index")
    terms = (_range_term("id", ComparisonOp.GT, 5), _equality("name", "x"))

    path = planner.plan_scan(_scan(), terms)

    assert path.column == "name"
    assert path.requests == (
        IndexLookup("name", "x"),
        IndexRange("id", 5, None, lower_inclusive=False),
    )


def test_index_mode_breaks_ties_by_column_position() -> None:
    planner = _planner(_metadata(indexes={}, statistics={}), physical="index")
    terms = (_equality("amount", 1.5), _equality("id", 5))

    path = planner.plan_scan(_scan(), terms)

    assert path.column == "id"
    assert path.requests == (IndexLookup("id", 5), IndexLookup("amount", 1.5))


# ---------- auto：退化路径 ----------


def test_auto_without_predicate_returns_no_predicate() -> None:
    metadata = _metadata()

    path = _planner(metadata).plan_scan(_scan(), ())

    assert path == AccessPath(table=TABLE, reason=NO_PREDICATE)
    assert metadata.calls == []


def test_auto_without_translatable_predicate_returns_no_predicate() -> None:
    metadata = _metadata()

    path = _planner(metadata).plan_scan(_scan(), (BoundLiteral(True, SqlType.BOOLEAN),))

    assert path.reason == NO_PREDICATE
    assert metadata.calls == []


def test_auto_without_any_index_stops_before_statistics() -> None:
    metadata = _metadata(indexes={TABLE: ()})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == NO_MATCHING_INDEX
    assert metadata.calls == [("list_indexes", TABLE)]


def test_auto_with_index_on_another_column_stops_before_statistics() -> None:
    metadata = _metadata(indexes={TABLE: (_index("name"),)})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == NO_MATCHING_INDEX
    assert metadata.calls == [("list_indexes", TABLE)]


def test_auto_ignores_untranslatable_conjuncts_next_to_translatable_ones() -> None:
    metadata = _metadata()

    path = _planner(metadata).plan_scan(
        _scan(), (_range_term("id", ComparisonOp.NE, 5), _equality("id", 5))
    )

    assert path.reason == INDEX_EQUALITY
    assert path.requests == (IndexLookup("id", 5),)


def test_auto_with_zero_distinct_count_degrades_to_no_stats() -> None:
    columns = (ColumnStats("id", 0, None, None),)
    metadata = _metadata(statistics={TABLE: _stats(columns=columns)})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == NO_STATS
    assert path.requests == ()
    # 统计读过，顺序扫描的代价可得；索引代价因选择性不可估算而缺失
    assert path.seq_cost == 100.0
    assert path.index_cost is None


def test_auto_with_inconsistent_page_count_degrades_to_no_stats() -> None:
    metadata = _metadata(statistics={TABLE: _stats(rows=1000, pages=0)})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == NO_STATS
    assert path.seq_cost is None and path.index_cost is None


def test_auto_range_candidate_survives_degenerate_distinct_count() -> None:
    # 区间条件不依赖基数：端点缺失改用默认选择性，仍是一次有效估算
    columns = (ColumnStats("id", 0, None, None),)
    metadata = _metadata(statistics={TABLE: _stats(columns=columns)})

    path = _planner(metadata).plan_scan(_scan(), (_range_term("id", ComparisonOp.GT, 5),))

    assert path.reason == SEQ_CHEAPER
    assert path.selectivity == pytest.approx(1 / 3)


# ---------- auto：代价比较 ----------


def test_auto_chooses_index_for_selective_equality() -> None:
    path = _planner(_metadata()).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == INDEX_EQUALITY
    assert path.column == "id"
    assert path.selectivity == pytest.approx(0.001)
    assert path.estimated_rows == pytest.approx(1.0)
    assert path.seq_cost == pytest.approx(100.0)
    assert path.index_cost == pytest.approx(4.0)
    assert path.requests == (IndexLookup("id", 5),)


def test_auto_chooses_index_for_selective_range() -> None:
    path = _planner(_metadata()).plan_scan(
        _scan(), (_range_term("id", ComparisonOp.GT, 990),)
    )

    assert path.reason == INDEX_RANGE
    assert path.index_cost is not None and path.index_cost < 100.0
    assert path.requests == (IndexRange("id", 990, None, lower_inclusive=False),)


def test_auto_gives_up_the_index_for_low_selectivity() -> None:
    columns = (ColumnStats("id", 10, 0, 999),)
    metadata = _metadata(statistics={TABLE: _stats(columns=columns)})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == SEQ_CHEAPER
    # 落选候选的估算仍被记录，便于解释"为什么不走索引"
    assert path.column == "id"
    assert path.estimated_rows == pytest.approx(100.0)
    assert path.index_cost == pytest.approx(103.0)
    assert path.requests == ()


def test_auto_prefers_sequence_scan_on_equal_cost() -> None:
    columns = (ColumnStats("id", 10, 0, 999),)
    metadata = _metadata(statistics={TABLE: _stats(pages=103, columns=columns)})

    path = _planner(metadata).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == SEQ_CHEAPER
    assert path.index_cost == pytest.approx(path.seq_cost)  # type: ignore[arg-type]


def test_auto_orders_requests_by_cost() -> None:
    metadata = _metadata(indexes={TABLE: (_index("id"), _index("name"))})

    path = _planner(metadata).plan_scan(
        _scan(), (_range_term("name", ComparisonOp.GT, "a"), _equality("id", 5))
    )

    # name 是 TEXT：区间取默认选择性（1/3，约 333 行）→ 比等值命中 1 行贵得多
    assert path.reason == INDEX_EQUALITY
    assert path.requests == (
        IndexLookup("id", 5),
        IndexRange("name", "a", None, lower_inclusive=False),
    )


def test_auto_breaks_cost_ties_by_column_position() -> None:
    # 两列的基数与端点范围都相同 → 代价与选择性相同，取 schema 中靠前的 id
    columns = (ColumnStats("id", 1000, 0, 999), ColumnStats("name", 1000, "a", "z"))
    metadata = _metadata(
        indexes={TABLE: (_index("id"), _index("name"))},
        statistics={TABLE: _stats(columns=columns)},
    )

    path = _planner(metadata).plan_scan(
        _scan(), (_equality("name", "m"), _equality("id", 5))
    )

    assert path.column == "id"
    assert path.requests == (IndexLookup("id", 5), IndexLookup("name", "m"))


def test_auto_metadata_is_read_once_per_table() -> None:
    metadata = _metadata()
    planner = _planner(metadata)

    planner.plan_scan(_scan(), (_equality("id", 5),))
    planner.plan_scan(_scan(), (_equality("id", 6),))

    assert metadata.calls == [("list_indexes", TABLE), ("statistics", TABLE)]


def test_paths_accumulate_in_planning_order() -> None:
    planner = _planner(_metadata())

    first = planner.plan_scan(_scan(), (_equality("id", 5),))
    second = planner.plan_scan(_scan(), ())

    assert planner.paths == (first, second)
    assert [path.reason for path in planner.paths] == [INDEX_EQUALITY, NO_PREDICATE]


def test_plan_scan_reports_one_trace_event_per_decision() -> None:
    events: list[dict[str, object]] = []
    metadata = _metadata()
    planner = PhysicalPlanner(
        statistics=metadata.statistics,
        list_indexes=metadata.list_indexes,
        trace_sink=events.append,
    )

    path = planner.plan_scan(_scan(), (_equality("id", 5),))

    assert [event["component"] for event in events] == ["executor"]
    assert [event["operation"] for event in events] == ["plan_access"]
    assert events[0]["status"] == "success"
    assert events[0]["result"] == path


def test_plan_scan_reports_no_event_without_a_sink() -> None:
    # 没有追踪回调时选路照常完成，不产生任何观察开销
    path = _planner(_metadata()).plan_scan(_scan(), (_equality("id", 5),))

    assert path.reason == INDEX_EQUALITY


# ---------- 物理模式取值 ----------


def test_physical_modes_are_the_three_literals() -> None:
    assert PHYSICAL_MODES == ("auto", "seq", "index")
    for mode in PHYSICAL_MODES:
        assert validate_physical_mode(mode) == mode


def test_unknown_physical_mode_is_rejected() -> None:
    with pytest.raises(SqlError) as info:
        _planner(_metadata(), physical="fast")

    assert info.value.code == E_BAD_ARG


def test_unknown_physical_mode_is_not_treated_as_auto() -> None:
    with pytest.raises(SqlError) as info:
        validate_physical_mode("")

    assert info.value.code == E_BAD_ARG
