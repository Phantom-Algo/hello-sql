"""选择性估算与代价公式：逐行覆盖估算表、夹取、树高与退化输入。

估算表里的每一行在这里都有对应用例；"等代价与空表取顺序扫描"这类决策属于
选路器，在 test_access_path.py 中断言，本文件只锁定支撑决策的数值关系。
"""

from __future__ import annotations

import pytest

from contracts.ast import SqlType
from contracts.storage import ColumnStats
from runner.physical.cost_model import (
    INDEX_FANOUT,
    RANGE_DEFAULT_SELECTIVITY,
    equality_selectivity,
    index_cost,
    range_selectivity,
    seq_cost,
    tree_height,
)
from runner.physical.requests import IndexRange


def _stats(
    distinct_count: int,
    minimum: object,
    maximum: object,
) -> ColumnStats:
    return ColumnStats(
        name="id",
        distinct_count=distinct_count,
        min_value=minimum,  # type: ignore[arg-type]
        max_value=maximum,  # type: ignore[arg-type]
    )


def _range(
    lower: object,
    upper: object,
    *,
    lower_inclusive: bool = True,
    upper_inclusive: bool = True,
) -> IndexRange:
    return IndexRange(
        "id",
        lower,  # type: ignore[arg-type]
        upper,  # type: ignore[arg-type]
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
    )


# ---------- 等值选择性 ----------


def test_equality_uses_one_over_distinct_count() -> None:
    assert equality_selectivity(_stats(50, 0, 100), 25, 1000) == pytest.approx(0.02)


def test_equality_below_min_or_above_max_is_zero() -> None:
    stats = _stats(50, 10, 20)

    assert equality_selectivity(stats, 9, 1000) == 0.0
    assert equality_selectivity(stats, 21, 1000) == 0.0


def test_equality_on_both_endpoints_is_inside_bounds() -> None:
    stats = _stats(50, 10, 20)

    assert equality_selectivity(stats, 10, 1000) == pytest.approx(0.02)
    assert equality_selectivity(stats, 20, 1000) == pytest.approx(0.02)


def test_equality_with_zero_distinct_count_is_unestimable_when_rows_exist() -> None:
    assert equality_selectivity(_stats(0, None, None), 5, 1000) is None


def test_equality_on_empty_table_is_estimated_as_zero() -> None:
    # 空表不是退化：任何键都不可能命中，选择性是有效估算
    assert equality_selectivity(_stats(0, None, None), 5, 0) == 0.0
    assert equality_selectivity(None, 5, 0) == 0.0


def test_equality_without_column_stats_is_unestimable_when_rows_exist() -> None:
    assert equality_selectivity(None, 5, 1000) is None


def test_equality_without_endpoints_keeps_one_over_distinct_count() -> None:
    # 端点缺失只是无法判定越界，不使估算失效
    assert equality_selectivity(_stats(4, None, None), 5, 1000) == pytest.approx(0.25)


def test_equality_selectivity_is_clamped_to_one() -> None:
    assert equality_selectivity(_stats(1, 0, 10), 5, 1000) == 1.0


def test_equality_with_incomparable_endpoints_does_not_raise() -> None:
    assert equality_selectivity(_stats(4, "a", "z"), 5, 1000) == pytest.approx(0.25)


# ---------- 区间选择性 ----------


def test_double_sided_numeric_range_uses_endpoint_ratio() -> None:
    stats = _stats(100, 0, 100)

    assert range_selectivity(stats, SqlType.INT, _range(20, 40)) == pytest.approx(0.2)


def test_single_sided_range_takes_min_or_max_as_the_other_end() -> None:
    stats = _stats(100, 0, 100)

    assert range_selectivity(stats, SqlType.INT, _range(None, 50)) == pytest.approx(0.5)
    assert range_selectivity(stats, SqlType.INT, _range(50, None)) == pytest.approx(0.5)


def test_range_selectivity_is_clamped_to_unit_interval() -> None:
    stats = _stats(100, 0, 100)

    assert range_selectivity(stats, SqlType.INT, _range(200, None)) == 0.0
    assert range_selectivity(stats, SqlType.INT, _range(None, 200)) == 1.0


def test_range_on_single_valued_column_depends_on_coverage() -> None:
    stats = _stats(1, 7, 7)

    assert range_selectivity(stats, SqlType.INT, _range(None, 7)) == 1.0
    assert range_selectivity(stats, SqlType.INT, _range(7, None)) == 1.0
    assert range_selectivity(stats, SqlType.INT, _range(None, 7, upper_inclusive=False)) == 0.0
    assert range_selectivity(stats, SqlType.INT, _range(7, None, lower_inclusive=False)) == 0.0


def test_range_on_single_valued_column_outside_the_value_is_zero() -> None:
    stats = _stats(1, 7, 7)

    assert range_selectivity(stats, SqlType.INT, _range(None, 6)) == 0.0
    assert range_selectivity(stats, SqlType.INT, _range(8, None)) == 0.0


@pytest.mark.parametrize("missing", ["min", "max"])
def test_numeric_range_without_endpoints_uses_default(missing: str) -> None:
    minimum = None if missing == "min" else 0
    maximum = None if missing == "max" else 100
    stats = _stats(100, minimum, maximum)

    assert (
        range_selectivity(stats, SqlType.INT, _range(20, 40))
        == RANGE_DEFAULT_SELECTIVITY
    )


@pytest.mark.parametrize("kind", [SqlType.TEXT, SqlType.BOOLEAN])
def test_non_numeric_range_uses_default(kind: SqlType) -> None:
    stats = _stats(2, False, True)

    assert (
        range_selectivity(stats, kind, IndexRange("active", None, True))
        == RANGE_DEFAULT_SELECTIVITY
    )


def test_range_without_column_stats_uses_default() -> None:
    assert (
        range_selectivity(None, SqlType.INT, _range(20, 40))
        == RANGE_DEFAULT_SELECTIVITY
    )


def test_range_with_incomparable_endpoints_uses_default() -> None:
    stats = _stats(4, "a", "z")

    assert (
        range_selectivity(stats, SqlType.INT, _range(20, 40))
        == RANGE_DEFAULT_SELECTIVITY
    )


# ---------- 代价公式 ----------


@pytest.mark.parametrize(
    ("row_count", "expected"),
    [
        (0, 1),                    # 空表：树高取下界 1
        (1, 1),                    # N ≤ 1：只有根页
        (INDEX_FANOUT, 2),         # 恰好装满一层
        (INDEX_FANOUT + 1, 3),     # 溢出一层
        (INDEX_FANOUT**2, 3),
        (INDEX_FANOUT**2 + 1, 4),
    ],
)
def test_tree_height_steps_per_fanout(row_count: int, expected: int) -> None:
    assert tree_height(row_count) == expected


def test_tree_height_is_monotonic() -> None:
    heights = [tree_height(rows) for rows in range(0, 500)]

    assert heights == sorted(heights)


def test_seq_cost_counts_one_page_read_per_data_page() -> None:
    assert seq_cost(0) == 0.0
    assert seq_cost(12) == 12.0


def test_index_cost_is_tree_height_plus_one_read_per_hit_row() -> None:
    # 300 行 → 树高 3，命中 5 行 → 3 + 5 × 1.0
    assert index_cost(300, 5.0) == pytest.approx(8.0)


def test_empty_table_makes_sequence_scan_cheaper() -> None:
    # 空表：C_seq = 0，C_index = H(0) + 0 = 1，索引永远不会胜出
    assert seq_cost(0) < index_cost(0, 0.0)


def test_equal_cost_has_no_index_advantage() -> None:
    # 判定是 C_index < C_seq：等代价时索引不占优，由选路器取顺序扫描
    assert index_cost(1000, 100.0) == pytest.approx(seq_cost(103))
    assert not (index_cost(1000, 100.0) < seq_cost(103))
