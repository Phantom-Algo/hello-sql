"""索引 DDL 的 C 侧计划级测试：绑定期校验、执行器分派与优化器直通。

本组用例用假的 describe 回调与只记录索引调用的假 Storage 隔开 B，
因此断言的是 C 自身的契约：绑定期只确认表与列存在，索引存在性一律不问。
"""

from __future__ import annotations

from typing import cast

import pytest

from contracts.ast import ColumnDef, CreateIndexStmt, DropIndexStmt, SqlType
from contracts.errors import E_COLUMN_NOT_FOUND, SqlError
from contracts.storage import BaseDatabaseServer, BaseStorage, TableInfo
from runner.executor.base import StatementExecutor
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.context import ExecutionContext
from runner.executor.ddl import CreateIndexExecutor, DropIndexExecutor
from runner.logical_plan import LogicalPlanBuilder
from runner.logical_plan.optimizer import LogicalOptimizer
from runner.logical_plan.plans import LogicalCreateIndex, LogicalDropIndex

EVENTS_COLUMNS = (
    ColumnDef("id", SqlType.INT),
    ColumnDef("amount", SqlType.REAL),
    ColumnDef("tag", SqlType.TEXT),
)


class FakeCatalog:
    """按表名保存列定义并记录 describe 调用，模拟当前数据库的目录。"""

    def __init__(self) -> None:
        self.schemas: dict[str, tuple[ColumnDef, ...]] = {"events": EVENTS_COLUMNS}
        self.calls: list[str] = []

    def describe(self, table: str) -> TableInfo:
        self.calls.append(table)
        return TableInfo(name=table, columns=self.schemas[table])


class FakeStorage:
    """只记录索引 DDL 调用，其余契约方法不实现。"""

    def __init__(self) -> None:
        self.index_calls: list[tuple[str, str, str]] = []

    def create_index(self, name: str, table: str, column: str) -> None:
        self.index_calls.append((name, table, column))

    def drop_index(self, name: str) -> None:
        self.index_calls.append(("DROP", name, ""))


def _context(storage: FakeStorage) -> ExecutionContext:
    """只填 storage 的 ExecutionContext：索引 DDL 执行器不触碰 server 与会话库名。"""
    return ExecutionContext(
        server=cast(BaseDatabaseServer, None),
        storage=cast(BaseStorage, storage),
        current_database="main",
    )


def _forbidden_metadata(table: str) -> object:
    """哨兵：DDL 语句没有 Scan，选路不该读统计或索引清单。"""
    raise AssertionError(f"metadata read: {table}")


def _builder(catalog: FakeCatalog) -> LogicalPlanBuilder:
    return LogicalPlanBuilder(catalog.describe)


# ---------- 绑定期 ----------


def test_build_returns_logical_create_index() -> None:
    catalog = FakeCatalog()

    plan = _builder(catalog).build(
        CreateIndexStmt(index_name="idx_events_id", table="events", column="id")
    )

    assert isinstance(plan, LogicalCreateIndex)
    assert (plan.index_name, plan.table, plan.column) == (
        "idx_events_id",
        "events",
        "id",
    )
    assert plan.children == ()
    assert plan.output_schema.columns == ()


def test_build_rejects_missing_column_before_plan() -> None:
    catalog = FakeCatalog()

    with pytest.raises(SqlError) as error:
        _builder(catalog).build(
            CreateIndexStmt(index_name="idx_events_x", table="events", column="nope")
        )

    assert error.value.code == E_COLUMN_NOT_FOUND
    assert catalog.calls == ["events"]


def test_build_drop_index_does_not_touch_catalog() -> None:
    catalog = FakeCatalog()

    plan = _builder(catalog).build(DropIndexStmt(index_name="idx_events_id"))

    assert isinstance(plan, LogicalDropIndex)
    assert plan.index_name == "idx_events_id"
    assert catalog.calls == []


# ---------- 执行器分派 ----------


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (LogicalCreateIndex("idx_a", "events", "id"), CreateIndexExecutor),
        (LogicalDropIndex("idx_a"), DropIndexExecutor),
    ],
)
def test_executor_builder_maps_index_ddl_to_executors(
    plan: LogicalCreateIndex | LogicalDropIndex,
    expected: type[StatementExecutor],
) -> None:
    catalog = FakeCatalog()
    builder = ExecutorTreeBuilder(
        catalog.describe,
        lambda: "main",
        statistics=_forbidden_metadata,  # type: ignore[arg-type]
        list_indexes=_forbidden_metadata,  # type: ignore[arg-type]
    )

    executor = builder.build(plan)

    assert isinstance(executor, expected)
    # 索引 DDL 不进表结构缓存键，构建期不需要读目录
    assert catalog.calls == []
    # 不含 Scan 的语句没有选路对象，规划期不读任何元数据
    assert builder.last_access_paths == ()


def test_create_index_executor_calls_storage_once() -> None:
    storage = FakeStorage()
    executor = CreateIndexExecutor("idx_a", "events", "id")

    result = executor.execute(_context(storage))

    assert storage.index_calls == [("idx_a", "events", "id")]
    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None


def test_drop_index_executor_calls_storage_once() -> None:
    storage = FakeStorage()
    executor = DropIndexExecutor("idx_a")

    result = executor.execute(_context(storage))

    assert storage.index_calls == [("DROP", "idx_a", "")]
    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None


# ---------- 优化器 ----------


@pytest.mark.parametrize(
    "plan",
    [LogicalCreateIndex("idx_a", "events", "id"), LogicalDropIndex("idx_a")],
)
def test_optimizer_passes_index_ddl_through(
    plan: LogicalCreateIndex | LogicalDropIndex,
) -> None:
    log = LogicalOptimizer().optimize(plan)

    assert log.applications == ()
    assert log.rounds == 0
    assert log.optimized is plan
