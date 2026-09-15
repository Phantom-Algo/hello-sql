"""表级统计（V3 D37/D38/D41 + M7 的 D47/D48）。

契约（`contracts.storage.TableStats`）要求：

- 任何存在的表都返回统计；空表返回零值与空的 min/max；
- `page_count` 只计数据页（不含页 0、空闲页、溢出链页）；
- `columns` 必须按建表列顺序**完整**包含全部用户列；
- 规划期调用**只读**、不产生写盘；
- 返回值不得早于该表最近一次已完成的写操作；
- 契约 3.1 起：`min_value` / `max_value` 是**精确边界**（D47），
  `distinct_count` 仍是近似值（D48）。

实现分工：

- **行数与数据页数**由 `engine` 增量维护（一次惰性基线 + 写路径更新），
  因此这里是 O(1) 读取；
- **极值**精确维护（D47）：插入只可能拓宽极值 → O(列数) 就地更新；
  删掉 / 改掉**极值持有者**会让极值不确定 → 下次快照做一趟**只读**精确全扫。
  因此"首次快照"（或极值被破坏后）最多一次全表扫描，之后是 O(1)；
- **基数**有界采样（D41/D48）：最多 `STATS_SAMPLE_PAGES` 个**均匀间隔**页，
  结果缓存在内存中，任何写入使其失效（D38：统计一律不落盘）。

为什么极值必须精确：C 的选择性估算把 `min/max` 当硬边界（越界即 0 行）。
采样前缀会让单调插入的大表报出偏低的 `max`，把"其实命中 99 行"的区间
估成 0 行，进而选出远比顺序扫描更差的索引路径。
"""

from __future__ import annotations

from typing import Sequence

from contracts.ast import ColumnDef, Value
from contracts.storage import ColumnStats, TableStats
from storage.constants import STATS_SAMPLE_PAGES
from storage.engine import TableEngine


class _Extreme:
    """一列的精确极值，以及"有多少行正好持有这个极值"（D47）。

    计数是必要的：低基数列（例如全表同一个状态值）里删掉任意一行都会碰到
    `min == max`，只比数值会让每次删除都触发一次全表重算。有了计数，只有
    **删空极值持有者**时才转不确定，重算次数因此与真实语义对齐。

    不变式：`minimum is None` ⇔ 该列暂无数据（`min_count == max_count == 0`）；
    单值列满足 `minimum == maximum` 且 `min_count == max_count`。
    """

    __slots__ = ("minimum", "maximum", "min_count", "max_count")

    def __init__(self) -> None:
        self.minimum: Value | None = None
        self.maximum: Value | None = None
        self.min_count = 0
        self.max_count = 0

    def widen(self, value: Value) -> None:
        """并入一个值：插入只可能拓宽极值，O(1)。"""

        if self.minimum is None:
            self.minimum = self.maximum = value
            self.min_count = self.max_count = 1
            return
        if value < self.minimum:
            self.minimum, self.min_count = value, 1
        elif value == self.minimum:
            self.min_count += 1
        if value > self.maximum:
            self.maximum, self.max_count = value, 1
        elif value == self.maximum:
            self.max_count += 1

    def remove(self, value: Value) -> None:
        """移走一个值：只有它正好持有极值时计数才递减。"""

        if value == self.minimum:
            self.min_count -= 1
        if value == self.maximum:
            self.max_count -= 1

    @property
    def exhausted(self) -> bool:
        """极值持有者是否已被删空（含"整列已无数据"的 0 行情形）。"""

        return self.min_count <= 0 or self.max_count <= 0

    def clear(self) -> None:
        """重置为空列的确定状态（0 行时使用，不需要扫描）。"""

        self.minimum = self.maximum = None
        self.min_count = self.max_count = 0


class TableStatsProvider:
    """一张表的统计提供者：行数/页数实时读引擎，极值精确、基数采样。"""

    def __init__(
        self,
        engine: TableEngine,
        columns: Sequence[ColumnDef],
        table: str,
    ) -> None:
        self._engine = engine
        self._columns = tuple(columns)
        self._table = table
        self._extrema = [_Extreme() for _ in self._columns]
        self._extrema_certain = False
        self._distinct: tuple[int, ...] | None = None

    def invalidate(self) -> None:
        """结构性失效（重建、清空等）：极值与基数都要重新计算。"""

        self._extrema = [_Extreme() for _ in self._columns]
        self._extrema_certain = False
        self._distinct = None

    # ---- 写路径钩子（全部 O(列数)，不读页） ----

    def note_insert(self, values: Sequence[Value]) -> None:
        """追加一行：只可能拓宽极值（D47）。"""

        if self._extrema_certain:
            for index, value in enumerate(values):
                if index >= len(self._extrema):
                    break
                self._extrema[index].widen(value)
        self._distinct = None

    def note_update(
        self,
        old_values: Sequence[Value],
        new_values: Sequence[Value],
    ) -> None:
        """整行替换：先移走旧值，仍确定时再并入新值。

        旧值被移走后极值仍确定（还有其它行持有它）才并入新值；一旦转不确定，
        新值不再并入——下次快照的全扫会把最终状态算对。
        """

        if self._extrema_certain:
            self._remove(old_values)
            if self._extrema_certain:
                for index, value in enumerate(new_values):
                    if index >= len(self._extrema):
                        break
                    self._extrema[index].widen(value)
        self._distinct = None

    def note_delete(self, old_values: Sequence[Value]) -> None:
        """删除一行：只有删空极值持有者时才需要重算。"""

        if self._extrema_certain:
            self._remove(old_values)
        self._distinct = None

    def _remove(self, values: Sequence[Value]) -> None:
        """把这一行的值从各列极值计数里移除；删空即转不确定，下次精确重算。"""

        for index, value in enumerate(values):
            if index >= len(self._extrema):
                break
            self._extrema[index].remove(value)
        if any(item.exhausted for item in self._extrema):
            self._extrema_certain = False

    # ---- 快照 ----

    def snapshot(self) -> TableStats:
        """返回当前统计；极值按需精确重算，基数按需采样并缓存。"""

        row_count = self._engine.row_count()
        page_count = self._engine.data_page_count()
        if row_count == 0:
            # 空表的极值是确定的零值（DV3-08），不需要扫描。
            for item in self._extrema:
                item.clear()
            self._extrema_certain = True
        elif not self._extrema_certain:
            self._extrema = self._scan_extrema()
            self._extrema_certain = True
        if self._distinct is None:
            self._distinct = self._sample_distinct(row_count)
        return TableStats(
            table=self._table,
            row_count=row_count,
            page_count=page_count,
            columns=tuple(
                ColumnStats(
                    name=column.name,
                    distinct_count=self._distinct[index],
                    min_value=self._extrema[index].minimum,
                    max_value=self._extrema[index].maximum,
                )
                for index, column in enumerate(self._columns)
            ),
        )

    def _scan_extrema(self) -> list[_Extreme]:
        """一趟只读全扫，算出每列的精确极值与极值持有计数（D47）。

        只在"极值不确定"时调用：首次快照，或极值持有者被删 / 改之后。
        读的是解码后的行，所以溢出行与普通行一视同仁；不写盘、不 mark_dirty。
        """

        extrema = [_Extreme() for _ in self._columns]
        for _row_id, values in self._engine.scan():
            for index, value in enumerate(values):
                if index >= len(extrema):
                    break
                extrema[index].widen(value)
        return extrema

    def _sample_distinct(self, row_count: int) -> tuple[int, ...]:
        """均匀间隔采样最多 `STATS_SAMPLE_PAGES` 页，统计每列的近似基数。"""

        if row_count == 0:
            return tuple(0 for _ in self._columns)
        distinct: list[set] = [set() for _ in self._columns]
        for _row_id, values in self._engine.sample_rows(STATS_SAMPLE_PAGES):
            for index, value in enumerate(values):
                if index >= len(distinct):
                    break
                distinct[index].add(value)
        return tuple(len(values) for values in distinct)
