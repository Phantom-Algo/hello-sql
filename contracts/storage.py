"""契约 V3.1：Storage 共享数据形状与公开接口协议。

BaseDatabaseServer 和 BaseStorage 只约定 B 向 C 暴露的方法；具体实现、
文件格式与内部状态均由 storage/ 维护。

B 的表数据与系统目录都必须通过 4KB 页式存储、Buffer Pool 和统一记录
编解码链路持久化。V2 不再允许以独立 JSON 文件作为权威系统目录。

V3 新增索引与统计能力：
- BaseStorage 增加 create_index / drop_index / list_indexes /
  statistics / index_lookup / index_range 六个方法；
- 共享形状增加 IndexInfo / ColumnStats / TableStats。
索引文件布局、索引维护时机与统计采集方式均属 B 的内部实现，不进契约；
契约只约束可观察语义：索引与数据一致、索引查找与 scan 的行形状一致、
统计不得早于该表最近一次已完成的写操作。

比较语义属 C：C 把 SQL 比较运算符翻译成键值或键区间后再调用 B，B 的索引
接口不接收操作符，只负责"按列键序"返回命中行。跨类型的值归纳（例如
INT 列收到 1.0 时由 C 先归一为 1）同样由 C 完成，B 只按列类型收值。

红线：
- B 不解析 SQL，不知道 SELECT / WHERE 是什么；
- 表名、列名统一小写（由 A 转换），B 不做大小写处理；
- DatabaseServer 接收数据目录并创建默认库，connect() 返回绑定目标库的
  BaseStorage 实现；进程重启后数据必须完整可读。
- 系统目录是 B 的内部特殊表，不暴露为普通 SQL 表；list_tables() 只返回
  用户表，describe() 仍是 C 获取用户表 Schema 的唯一接口。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from contracts.ast import ColumnDef, Value


RowId = int
"""B 返回的行把手：只保证“本次运行内、从 scan 拿到后、
到 update_row / delete_row 调用前”有效；重启后以重新 scan 为准。"""


Row = tuple[RowId, tuple[Value, ...]]
"""一行 = (row_id, 按建表顺序的值元组)。"""


@dataclass(frozen=True)
class TableInfo:
    """表结构：describe 的返回格式，由 B 构造、C 读取。"""

    name: str
    columns: tuple[ColumnDef, ...]   # 建表顺序，只读


@dataclass(frozen=True)
class IndexInfo:
    """索引元数据：list_indexes 的返回格式，由 B 构造、C 读取。

    本轮只支持单列、非唯一索引，因此不提供 unique 字段；将来做 UNIQUE
    或多列索引时需要升契约版本再增加字段。
    """

    name: str
    table: str
    column: str


@dataclass(frozen=True)
class ColumnStats:
    """列级统计：供 C 估算选择性与代价。

    字段精度不同，契约 3.1（D47）起明确区分：

    - ``min_value`` / ``max_value`` 是**精确边界**：分别等于表中该列的最小值
      与最大值；空表（或该列尚无数据）时为 None。C 可以依赖它做"键越界即
      0 行"的推论，B 必须保证它不窄于真实范围。
    - ``distinct_count`` 是**近似值**：由 B 的采集策略（有界采样）决定，
      可能低于真实基数；C 不得假设其精确，只能当作估算输入。
    - 空表时 ``distinct_count`` 为 0、``min_value`` / ``max_value`` 为 None。
    """

    name: str
    distinct_count: int
    min_value: Value | None
    max_value: Value | None


@dataclass(frozen=True)
class TableStats:
    """表级统计：statistics 的返回格式，由 B 构造、C 读取。

    page_count 只计数据页，不含页 0、空闲页与溢出链页——这是代价估算
    可直接使用的口径，属契约定义而非实现细节。

    columns 必须按建表列顺序**完整**包含该表的全部用户列，不得省略任何
    列：C 的估算器按列名取统计，缺列会造成静默的退化估算。空表按
    row_count=0、page_count=0 返回，列统计的 distinct_count 为 0、
    min_value / max_value 为 None。
    """

    table: str
    row_count: int
    page_count: int
    columns: tuple[ColumnStats, ...]


class BaseStorage(Protocol):
    """绑定单个数据库的表级存储接口。"""

    def create_table(
        self,
        name: str,
        columns: Sequence[ColumnDef],
    ) -> None: ...

    def drop_table(self, name: str) -> None: ...

    def list_tables(self) -> list[str]: ...

    def describe(self, name: str) -> TableInfo: ...

    def insert(self, name: str, values: Sequence[Value]) -> RowId: ...

    def scan(self, name: str) -> Iterator[Row]: ...

    def update_row(
        self,
        name: str,
        row_id: RowId,
        values: Sequence[Value],
    ) -> None: ...

    def delete_row(self, name: str, row_id: RowId) -> None: ...

    # ---- V3：索引与统计 ----

    def create_index(self, name: str, table: str, column: str) -> None: ...

    def drop_index(self, name: str) -> None: ...

    def list_indexes(self, table: str | None = None) -> list[IndexInfo]: ...

    def statistics(self, table: str) -> TableStats: ...

    def index_lookup(self, table: str, column: str, key: Value) -> Iterator[Row]: ...

    def index_range(
        self,
        table: str,
        column: str,
        lower: Value | None,
        upper: Value | None,
        *,
        lower_inclusive: bool = True,
        upper_inclusive: bool = True,
    ) -> Iterator[Row]: ...


class BaseDatabaseServer(Protocol):
    """管理数据库并创建 BaseStorage 连接的库级接口。"""

    def create_database(self, name: str) -> None: ...

    def drop_database(self, name: str) -> None: ...

    def list_databases(self) -> list[str]: ...

    def has_database(self, name: str) -> bool: ...

    def connect(self, name: str) -> BaseStorage: ...
