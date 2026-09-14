"""选择性估算与代价公式（纯函数 + 带标定依据的常量）。

代价单位是**预计的数据页读取次数**，与主指标同口径。建模假设：

1. 索引节点页与数据页同价（同一个 Buffer Pool）；
2. 顺序扫描读满 page_count 个数据页，每页一次；
3. 索引探测按树高层数计页读，顶层常驻缓存这一优惠不单独建模；
4. 每次回表按一次数据页读取计，不建模键序与物理顺序的相关性；
5. 溢出链页、空闲页、页 0 不计，与 page_count 的口径一致。

两条已知近似都朝"偏向顺序扫描"的方向偏：没有直方图时均匀分布假设会高估偏斜
数据的命中行数；基数来自有界采样、可能偏低，使 1/D 偏大。命中行数估大只会让
索引显得更贵，代价模型允许把索引估贵，不允许估便宜。
"""

from __future__ import annotations

from contracts.ast import SqlType, Value
from contracts.storage import ColumnStats

from runner.physical.requests import IndexRange


INDEX_FANOUT = 100
"""索引节点扇出估计。

按 4KB 页估算：叶条目 = 8B rid + 8B 数值键 + 8B 槽 ≈ 24B，单叶页约 168 条；
内节点条目 = 4B 子页 + 8B rid + 8B 键 + 8B 槽 ≈ 28B，单节点约 145 个子页。
取 100 作为保守下界，使 TEXT 键（键更长、扇出更小）也在同一量级内。
标定记录：待与 bench 一起用实测树高反解后回填。
"""

REF_PAGE_COST = 1.0
"""每次回表的预计数据页读取数。

取 1.0 表示"每命中一行按一次数据页读计"，不假设缓存命中与物理聚簇带来的折减。
标定记录：待与 bench 一起用（索引模式页读 − 索引节点页读）/ 命中行数反解后回填。
"""

RANGE_DEFAULT_SELECTIVITY = 1 / 3
"""缺少可用端点或非数值列时的范围默认选择性。

沿用 System R 对范围谓词的经典默认值 1/3：在没有任何分布信息时给出一个中性
偏保守的估计。
"""


def clamp(value: float, low: float, high: float) -> float:
    """把估算值夹到 [low, high]。"""
    return max(low, min(high, value))


def tree_height(row_count: int) -> int:
    """按扇出估算 B+ 树高度：N ≤ 1 时为 1，之后每 F 倍行数加一层。"""
    height = 1
    capacity = 1
    while capacity < row_count:
        capacity *= INDEX_FANOUT
        height += 1
    return height


def seq_cost(page_count: int) -> float:
    """顺序扫描代价：读满全部数据页，每页一次。"""
    return float(page_count)


def index_cost(row_count: int, hit_rows: float) -> float:
    """索引访问代价：树高次节点页读取 + 每命中一行一次回表读。"""
    return tree_height(row_count) + hit_rows * REF_PAGE_COST


def equality_selectivity(
    stats: ColumnStats | None,
    value: Value,
    row_count: int,
) -> float | None:
    """等值选择性：取 1/D，键越界给 0。

    返回 None 表示统计退化到无法估算（表里有行但该列基数为 0），该列不产生
    候选。空表不是退化：任何键都不可能命中，选择性 0 是有效估算。
    """
    if stats is None or stats.distinct_count <= 0:
        return 0.0 if row_count == 0 else None
    if _within_bounds(value, stats.min_value, stats.max_value) is False:
        return 0.0
    return clamp(1.0 / stats.distinct_count, 0.0, 1.0)


def range_selectivity(
    stats: ColumnStats | None,
    column_type: SqlType,
    request: IndexRange,
) -> float:
    """区间选择性：数值列按端点占比估算，其余情形一律给默认选择性。

    端点占比只在"min 与 max 之间存在均匀分布"的假设下成立；端点缺失、类型
    不是数值、端点不可比都退回默认值，而不是让估算失效。
    """
    if column_type is not SqlType.INT and column_type is not SqlType.REAL:
        return RANGE_DEFAULT_SELECTIVITY
    if stats is None or stats.min_value is None or stats.max_value is None:
        return RANGE_DEFAULT_SELECTIVITY
    try:
        lower = stats.min_value if request.lower is None else request.lower
        upper = stats.max_value if request.upper is None else request.upper
        span = stats.max_value - stats.min_value
        width = upper - lower
    except TypeError:
        return RANGE_DEFAULT_SELECTIVITY
    if span <= 0:
        # 单值列：区间是否覆盖该值决定全命中或全不命中
        return 1.0 if _covers(request, stats.max_value) else 0.0
    return clamp(width / span, 0.0, 1.0)


def _within_bounds(
    value: Value,
    minimum: Value | None,
    maximum: Value | None,
) -> bool | None:
    """等值键是否落在 [min, max] 内；端点缺失或不可比时返回 None（不判定越界）。"""
    if minimum is None or maximum is None:
        return None
    try:
        return minimum <= value <= maximum
    except TypeError:
        return None


def _covers(request: IndexRange, value: Value) -> bool:
    """区间是否覆盖单个值；端点开闭生效，端点不可比时按不覆盖处理。"""
    try:
        if request.lower is not None and (
            request.lower > value
            or (request.lower == value and not request.lower_inclusive)
        ):
            return False
        if request.upper is not None and (
            request.upper < value
            or (request.upper == value and not request.upper_inclusive)
        ):
            return False
    except TypeError:
        return False
    return True
