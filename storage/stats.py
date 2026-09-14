"""表级统计（V3 D37/D38/D41）。

契约（`contracts.storage.TableStats`）要求：

- 任何存在的表都返回统计；空表返回零值与空的 min/max；
- `page_count` 只计数据页（不含页 0、空闲页、溢出链页）；
- `columns` 必须按建表列顺序**完整**包含全部用户列；
- 规划期调用**只读**、不产生写盘；
- 返回值不得早于该表最近一次已完成的写操作。

实现分工：

- **行数与数据页数**由 `engine` 增量维护（一次惰性基线 + 写路径更新），
  因此这里是 O(1) 读取，不会退化成全表扫描；
- **列级统计**采用有界采样（D41：最多 `STATS_SAMPLE_PAGES` 个活动数据页），
  结果缓存在内存中，任何写入使其失效（D38：统计一律不落盘）。

采样值是**近似值**，契约明确允许（`ColumnStats` 文档注明
"取值是否精确由 B 的采集策略决定"）。
"""

from __future__ import annotations

from typing import Sequence

from contracts.ast import ColumnDef
from contracts.storage import ColumnStats, TableStats
from storage.constants import STATS_SAMPLE_PAGES
from storage.engine import TableEngine


class TableStatsProvider:
    """一张表的统计提供者：行数/页数实时读引擎，列级统计采样并缓存。"""

    def __init__(
        self,
        engine: TableEngine,
        columns: Sequence[ColumnDef],
        table: str,
    ) -> None:
        self._engine = engine
        self._columns = tuple(columns)
        self._table = table
        self._column_stats: tuple[ColumnStats, ...] | None = None

    def invalidate(self) -> None:
        """写入后调用：丢弃列级统计缓存，下次快照重新采样。"""
        self._column_stats = None

    def snapshot(self) -> TableStats:
        """返回当前统计；列级统计按需采样并缓存。"""
        if self._column_stats is None:
            self._column_stats = self._sample_columns()
        return TableStats(
            table=self._table,
            row_count=self._engine.row_count(),
            page_count=self._engine.data_page_count(),
            columns=self._column_stats,
        )

    def _sample_columns(self) -> tuple[ColumnStats, ...]:
        """采样最多 `STATS_SAMPLE_PAGES` 个数据页，统计每列的基数与极值。"""
        distinct: list[set] = [set() for _ in self._columns]
        minimum: list[object] = [None] * len(self._columns)
        maximum: list[object] = [None] * len(self._columns)

        for _row_id, values in self._engine.sample_rows(STATS_SAMPLE_PAGES):
            for index, value in enumerate(values):
                if index >= len(distinct):
                    break
                distinct[index].add(value)
                if minimum[index] is None or value < minimum[index]:
                    minimum[index] = value
                if maximum[index] is None or value > maximum[index]:
                    maximum[index] = value

        return tuple(
            ColumnStats(
                name=column.name,
                distinct_count=len(distinct[index]),
                min_value=minimum[index],
                max_value=maximum[index],
            )
            for index, column in enumerate(self._columns)
        )
