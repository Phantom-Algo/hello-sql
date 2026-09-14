"""页式系统表自举模块（V2 D20/D21；V3 D30 增加索引表）。

本模块只依赖 pager / engine / cache 这些既有基础设施，不反向依赖门面。

V3 起每库有三张内置系统表：__sys_tables / __sys_columns / __sys_indexes。
三张都是**普通表文件**（magic HSQL），不是索引文件（HSIX）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from contracts.ast import ColumnDef, SqlType

from storage.cache import BufferPool
from storage.constants import (
    SYS_COLUMNS_FILE_NAME,
    SYS_INDEXES_FILE_NAME,
    SYS_TABLES_FILE_NAME,
)
from storage.engine import TableEngine
from storage.pager import create_table_file


# 内置系统表 Schema（D20）：启动时不查询 Catalog，直接按本常量打开系统表。
SYS_TABLES_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("table_id", SqlType.INT),
    ColumnDef("table_name", SqlType.TEXT),
    ColumnDef("file_name", SqlType.TEXT),
)

SYS_COLUMNS_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("table_id", SqlType.INT),
    ColumnDef("ordinal", SqlType.INT),
    ColumnDef("column_name", SqlType.TEXT),
    ColumnDef("column_type", SqlType.TEXT),
)

# V3 D30：索引元数据。file_name 恒为 f"{index_name}.idx"，加载时校验；
# 不存 ordinal 而存列名：列名在表内唯一，且加载时用 __sys_columns 校验存在性。
SYS_INDEXES_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("index_name", SqlType.TEXT),
    ColumnDef("table_id", SqlType.INT),
    ColumnDef("column_name", SqlType.TEXT),
    ColumnDef("file_name", SqlType.TEXT),
)


@dataclass(frozen=True)
class SystemTablePaths:
    """三张系统表的文件路径。

    用具名 dataclass 而非三元组：三个元素同类型，位置解包极易写反。
    """

    tables: Path
    columns: Path
    indexes: Path

    def all(self) -> tuple[Path, Path, Path]:
        """按固定顺序返回全部路径，供批量创建/清理使用。"""
        return self.tables, self.columns, self.indexes


@dataclass(frozen=True)
class SystemTables:
    """三张系统表的行级访问器（V3 D30 增加 indexes）。"""

    tables: TableEngine
    columns: TableEngine
    indexes: TableEngine


def system_table_paths(db_dir: str | Path) -> SystemTablePaths:
    """返回三张系统表的文件路径。"""
    root = Path(db_dir)
    return SystemTablePaths(
        tables=root / SYS_TABLES_FILE_NAME,
        columns=root / SYS_COLUMNS_FILE_NAME,
        indexes=root / SYS_INDEXES_FILE_NAME,
    )


def create_empty_system_catalog(db_dir: str | Path) -> None:
    """在空库目录中创建三张页式系统表文件（各自只有页 0）。"""
    for path in system_table_paths(db_dir).all():
        create_table_file(path)


def open_system_tables(db_dir: str | Path, pool: BufferPool) -> SystemTables:
    """按内置 Schema 打开三张系统表，返回可扫描的表引擎集合。"""
    paths = system_table_paths(db_dir)
    return SystemTables(
        tables=TableEngine(paths.tables, SYS_TABLES_COLUMNS, pool),
        columns=TableEngine(paths.columns, SYS_COLUMNS_COLUMNS, pool),
        indexes=TableEngine(paths.indexes, SYS_INDEXES_COLUMNS, pool),
    )
