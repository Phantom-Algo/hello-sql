"""ExecutorTreeBuilder 的表结构缓存：按 (数据库, 表名) 命中，目录改写时失效。"""

from __future__ import annotations

import unittest

from contracts.ast import Column, ColumnDef, Cmp, SqlType
from contracts.storage import TableInfo
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.dql import SeqScanExecutor
from runner.logical_plan import (
    BoundColumnRef,
    LogicalColumn,
    LogicalSchema,
    bind_conjunction,
    join_schema,
)
from runner.logical_plan.plans import (
    LogicalCreateTable,
    LogicalDropTable,
    LogicalJoin,
    LogicalProjection,
    LogicalScan,
    LogicalUseDatabase,
)


class FakeCatalog:
    """按库保存表结构并记录 describe 调用；database 可切换以模拟 USE 的效果。"""

    def __init__(self, database: str = "main") -> None:
        self.database = database
        self.schemas: dict[tuple[str, str], tuple[ColumnDef, ...]] = {}
        self.calls: list[tuple[str, str]] = []

    def add_table(self, database: str, table: str, *columns: ColumnDef) -> None:
        self.schemas[(database, table)] = tuple(columns)

    def current_database(self) -> str:
        return self.database

    def describe(self, table: str) -> TableInfo:
        self.calls.append((self.database, table))
        return TableInfo(name=table, columns=self.schemas[(self.database, table)])


def _schema(
    table: str,
    columns: tuple[ColumnDef, ...],
    alias: str | None = None,
) -> LogicalSchema:
    """按建表列序建立 Schema，index 与列序一致。"""
    return LogicalSchema(
        tuple(
            LogicalColumn.of(table, column.name, index, column.type, alias)
            for index, column in enumerate(columns)
        )
    )


def _select(
    table: str,
    columns: tuple[ColumnDef, ...],
    names: tuple[str, ...],
    alias: str | None = None,
) -> LogicalProjection:
    """构造 `SELECT names FROM table [AS alias]`；Scan 只输出被引用的列。

    这是优化器裁剪后的形态：Scan.schema 是表结构的子序列，index 从 0 重排，因此
    构建期必须反查表完整列序才能知道每一列在物理行中的位置。
    """
    kept = tuple(column for column in columns if column.name in names)
    schema = _schema(table, kept, alias)
    scan = LogicalScan(table=table, schema=schema, alias=alias)
    refs = tuple(BoundColumnRef(schema.resolve(name, alias)) for name in names)
    return LogicalProjection(columns=refs, output_names=names, child=scan)


def _self_join(table: str, columns: tuple[ColumnDef, ...]) -> LogicalProjection:
    """构造 `SELECT a.id FROM table a JOIN table b ON a.id = b.id`：同表被扫两次。"""
    left_schema = _schema(table, columns, "a")
    right_schema = _schema(table, columns, "b")
    merged = join_schema(left_schema, right_schema)
    join = LogicalJoin(
        left=LogicalScan(table=table, schema=left_schema, alias="a"),
        right=LogicalScan(table=table, schema=right_schema, alias="b"),
        on=bind_conjunction(
            Cmp(Column("id", "a"), "=", Column("id", "b")), merged
        ),
        schema=merged,
    )
    column = merged.resolve("id", "a")
    return LogicalProjection(
        columns=(BoundColumnRef(column),), output_names=("a.id",), child=join
    )


def _scan_source_indexes(executor: object) -> tuple[int, ...]:
    """从构建结果里取出 Scan 执行器的 physical 取值位置，用于验证结构来源。"""
    assert isinstance(executor, SeqScanExecutor)
    return executor.source_indexes


class ExecutorTreeBuilderCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = FakeCatalog()
        self.builder = ExecutorTreeBuilder(
            self.catalog.describe, self.catalog.current_database
        )

    def test_repeated_statements_reuse_cached_schema(self) -> None:
        columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        self.catalog.add_table("main", "users", *columns)
        plan = _select("users", columns, ("name",))

        first = self.builder.build(plan)
        second = self.builder.build(plan)

        self.assertEqual(self.catalog.calls, [("main", "users")])
        self.assertEqual(_scan_source_indexes(first.root.child), (1,))
        self.assertEqual(_scan_source_indexes(second.root.child), (1,))

    def test_two_scans_of_one_table_share_one_lookup(self) -> None:
        columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        self.catalog.add_table("main", "users", *columns)

        executor = self.builder.build(_self_join("users", columns))

        # 同一条语句里自连接扫了两次同一张表，但表结构只取一次
        self.assertEqual(self.catalog.calls, [("main", "users")])
        join = executor.root.child
        self.assertEqual(_scan_source_indexes(join.left), (0, 1))
        self.assertEqual(_scan_source_indexes(join.right), (0, 1))

    def test_same_table_name_in_another_database_is_a_separate_entry(self) -> None:
        main_columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        shop_columns = (ColumnDef("name", SqlType.TEXT), ColumnDef("id", SqlType.INT))
        self.catalog.add_table("main", "users", *main_columns)
        self.catalog.add_table("shop", "users", *shop_columns)

        in_main = self.builder.build(_select("users", main_columns, ("name",)))
        self.catalog.database = "shop"
        in_shop = self.builder.build(_select("users", shop_columns, ("name",)))

        # 库名进键：两张同名表各取一次，且各自按自己库的列序定位
        self.assertEqual(
            self.catalog.calls, [("main", "users"), ("shop", "users")]
        )
        self.assertEqual(_scan_source_indexes(in_main.root.child), (1,))
        self.assertEqual(_scan_source_indexes(in_shop.root.child), (0,))

    def test_use_database_keeps_cached_entries(self) -> None:
        columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        self.catalog.add_table("main", "users", *columns)
        self.catalog.add_table("shop", "users", *columns)
        plan = _select("users", columns, ("name",))

        self.builder.build(plan)
        self.builder.build(LogicalUseDatabase("shop"))
        self.catalog.database = "shop"
        self.builder.build(plan)
        self.builder.build(LogicalUseDatabase("main"))
        self.catalog.database = "main"
        self.builder.build(plan)

        # USE 不改写目录：切回原库仍命中切走之前的缓存条目
        self.assertEqual(
            self.catalog.calls, [("main", "users"), ("shop", "users")]
        )

    def test_create_table_invalidates_cache(self) -> None:
        columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        self.catalog.add_table("main", "users", *columns)
        plan = _select("users", columns, ("name",))

        self.builder.build(plan)
        self.builder.build(
            LogicalCreateTable("orders", (ColumnDef("id", SqlType.INT),))
        )
        self.builder.build(plan)

        self.assertEqual(
            self.catalog.calls,
            [("main", "users"), ("main", "users")],
        )

    def test_drop_table_invalidates_cache(self) -> None:
        columns = (ColumnDef("id", SqlType.INT), ColumnDef("name", SqlType.TEXT))
        self.catalog.add_table("main", "users", *columns)
        plan = _select("users", columns, ("name",))

        self.builder.build(plan)
        self.builder.build(LogicalDropTable("users"))
        self.builder.build(plan)

        self.assertEqual(
            self.catalog.calls,
            [("main", "users"), ("main", "users")],
        )


if __name__ == "__main__":
    unittest.main()
