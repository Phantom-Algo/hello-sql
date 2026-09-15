"""基准场景：同一条查询的 SQL 形态与存储级形态。

每个场景同时给出

- `sql`：交给 `Runner.execute(..., physical=...)` 的端到端语句；
- 存储级谓词（列、种类、键或区间）：供 `api-seq` / `api-index` 直接调用
  `scan` / `index_lookup` / `index_range`。

两者语义必须一致，报告里的行多重集校验就靠这一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from contracts.ast import Value

from bench.dataset import DatasetSpec


Kind = Literal["lookup", "range"]


@dataclass(frozen=True, slots=True)
class Scenario:
    """一个场景：SQL + 等价的存储级谓词。"""

    name: str
    sql: str
    column: str
    kind: Kind
    expectation: str
    key: Value | None = None
    lower: Value | None = None
    upper: Value | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    has_index: bool = True
    """目标列上有无索引：False 的场景只用于强制 `index` 的错误矩阵。"""

    def matches(self, values: tuple[Value, ...], position: int) -> bool:
        """存储级的等价过滤：与 SQL 谓词逐字对应。"""

        value = values[position]
        if self.kind == "lookup":
            return value == self.key
        if self.lower is not None and (
            value < self.lower
            or (value == self.lower and not self.lower_inclusive)
        ):
            return False
        if self.upper is not None and (
            value > self.upper
            or (value == self.upper and not self.upper_inclusive)
        ):
            return False
        return True


def build_scenarios(spec: DatasetSpec) -> tuple[Scenario, ...]:
    """按数据集规模生成场景；边界值都用相对比例，换行数不用改用例。"""

    table = spec.table
    rows = spec.rows
    return (
        Scenario(
            name="point_hit",
            sql=f"SELECT * FROM {table} WHERE id = {rows // 2};",
            column="id",
            kind="lookup",
            key=rows // 2,
            expectation="高选择性点查：应走索引，且只回表一行",
        ),
        Scenario(
            name="point_miss",
            sql=f"SELECT * FROM {table} WHERE id = {rows * 2};",
            column="id",
            kind="lookup",
            key=rows * 2,
            expectation="键真实越界：估算 0 行，索引返回空集",
        ),
        Scenario(
            name="range_selective",
            sql=f"SELECT * FROM {table} WHERE id > {rows - 100};",
            column="id",
            kind="range",
            lower=rows - 100,
            lower_inclusive=False,
            expectation="窄区间：命中约 100 行，接近索引与顺序扫描的分水岭",
        ),
        Scenario(
            name="range_wide",
            sql=f"SELECT * FROM {table} WHERE id > {rows // 4};",
            column="id",
            kind="range",
            lower=rows // 4,
            lower_inclusive=False,
            expectation="宽区间：命中四分之三，代价模型应放弃索引",
        ),
        Scenario(
            name="dup_lookup",
            sql=f"SELECT * FROM {table} WHERE amount = 5;",
            column="amount",
            kind="lookup",
            key=5,
            expectation="低基数列等值：命中多行，用于对照回表成本",
        ),
        Scenario(
            name="no_index_column",
            sql=f"SELECT * FROM {table} WHERE name = 'name-000000';",
            column="name",
            kind="lookup",
            key="name-000000",
            has_index=False,
            expectation="目标列没有索引：强制 index 时由 B 抛 E_INDEX_NOT_FOUND",
        ),
    )
