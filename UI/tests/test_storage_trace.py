"""验证 B 四层追踪的真实调用、错误传播、缓存指标和上下文隔离。"""

from __future__ import annotations

import json

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer
from storage.pager import create_table_file, read_page
from UI import QueryTrace, StorageTraceRouter, TraceStatus


def _stage(stages: tuple[object, ...], stage_id: str):
    """按稳定 ID 取得 B 的唯一阶段，集中测试中的查询样板。"""

    return next(stage for stage in stages if stage.stage_id == stage_id)


def _server_with_table(tmp_path, router: StorageTraceRouter):
    """创建注入 Router 的服务器以及包含一行数据的 users 表。"""

    server = DatabaseServer(tmp_path, trace_sink=router)
    storage = server.connect("main")
    storage.create_table(
        "users",
        (
            ColumnDef("id", SqlType.INT),
            ColumnDef("enabled", SqlType.BOOLEAN),
        ),
    )
    storage.insert("users", (1, True))
    return server, storage


def test_full_flow_builds_four_successful_stages(tmp_path) -> None:
    """建表和 CRUD 应同时产生 Catalog、Cache、Pager、Engine 四层事件。"""

    router = StorageTraceRouter()
    with router.capture() as collector:
        _server, storage = _server_with_table(tmp_path, router)
        rows = list(storage.scan("users"))
        storage.update_row("users", rows[0][0], (2, False))
        storage.delete_row("users", rows[0][0])

    stages = collector.build_stages()
    assert [stage.stage_id for stage in stages] == [
        "b.catalog", "b.cache", "b.pager", "b.engine"
    ]
    assert [stage.sequence for stage in stages] == [6, 7, 8, 9]
    assert all(stage.status is TraceStatus.SUCCESS for stage in stages)
    assert all(stage.events for stage in stages)
    assert collector.operation_count == sum(len(stage.events) for stage in stages)


def test_cache_events_distinguish_hits_and_misses(tmp_path) -> None:
    """重复读同一页应分别产生真实 miss 和后续 hit 结论。"""

    router = StorageTraceRouter()
    server = DatabaseServer(tmp_path, trace_sink=router)
    table_path = tmp_path / "cache.table"
    create_table_file(table_path)

    with router.capture() as collector:
        read_page(server._pool, table_path, 0)
        read_page(pool=server._pool, file_path=table_path, page_no=0)

    cache = _stage(collector.build_stages(), "b.cache")
    accesses = [
        event for event in cache.events if event.action == "cache.get_page"
    ]
    assert [event.output_snapshot["cache_outcome"] for event in accesses] == [
        "miss", "hit"
    ]
    assert accesses[0].metrics["cache_deltas"]["misses"] == 1
    assert accesses[1].metrics["cache_deltas"]["hits"] == 1


def test_page_snapshots_are_bounded_instead_of_copying_full_pages(tmp_path) -> None:
    """页事件只应保留长度和十六字节预览，不把整页塞入历史。"""

    router = StorageTraceRouter()
    server = DatabaseServer(tmp_path, trace_sink=router)
    table_path = tmp_path / "bounded.table"
    create_table_file(table_path)

    with router.capture() as collector:
        page = read_page(server._pool, table_path, 0)

    assert len(page) == 4096
    pager = _stage(collector.build_stages(), "b.pager")
    read_event = next(
        event for event in pager.events if event.action == "pager.read_page"
    )
    snapshot = read_event.output_snapshot["result"]
    assert snapshot["byte_length"] == 4096
    assert len(snapshot["hex_preview"]) == 32
    assert snapshot["preview_truncated"] is True


def test_catalog_failure_is_visible_and_other_components_are_skipped(tmp_path) -> None:
    """查询不存在的表时应保留错误码，并标注未运行的存储层。"""

    router = StorageTraceRouter()
    server = DatabaseServer(tmp_path, trace_sink=router)
    storage = server.connect("main")

    with router.capture() as collector:
        with pytest.raises(SqlError) as caught:
            storage.describe("missing")

    assert caught.value.code == E_TABLE_NOT_FOUND
    stages = collector.build_stages()
    catalog = _stage(stages, "b.catalog")
    assert catalog.status is TraceStatus.FAILED
    assert catalog.error_code == E_TABLE_NOT_FOUND
    assert catalog.events[-1].output_snapshot["error_code"] == E_TABLE_NOT_FOUND
    assert all(
        _stage(stages, stage_id).status is TraceStatus.SKIPPED
        for stage_id in ("b.cache", "b.pager", "b.engine")
    )


def test_scan_is_recorded_only_after_the_iterator_runs(tmp_path) -> None:
    """Engine.scan 是惰性生成器，必须迭代后才能记录真实结果。"""

    router = StorageTraceRouter()
    server, storage = _server_with_table(tmp_path, router)

    with router.capture() as collector:
        rows = storage.scan("users")
        assert not any(
            event.action == "engine.scan"
            for event in _stage(collector.build_stages(), "b.engine").events
        )
        assert list(rows) == [(1, (1, True))]

    engine = _stage(collector.build_stages(), "b.engine")
    scan = next(event for event in engine.events if event.action == "engine.scan")
    assert scan.output_snapshot["result"] == {"yielded": 1}
    assert server.cache_stats["hits"] >= 0


def test_broken_trace_sink_cannot_change_storage_results(tmp_path) -> None:
    """观察者自身抛错时，BufferPool 必须隔离异常并保留业务语义。"""

    def broken_sink(_payload: dict[str, object]) -> None:
        """模拟界面收集器内部编程错误。"""

        raise RuntimeError("viewer failed")

    server = DatabaseServer(tmp_path, trace_sink=broken_sink)
    storage = server.connect("main")
    storage.create_table("safe", (ColumnDef("id", SqlType.INT),))
    row_id = storage.insert("safe", (7,))

    assert list(storage.scan("safe")) == [(row_id, (7,))]


def test_router_isolates_nested_queries_and_ignores_outside_calls(tmp_path) -> None:
    """Router 应把内外层查询隔离，且不收集 capture 之外的操作。"""

    router = StorageTraceRouter()
    server = DatabaseServer(tmp_path, trace_sink=router)
    storage = server.connect("main")
    storage.list_tables()

    with router.capture() as outer:
        storage.list_tables()
        with router.capture() as inner:
            storage.list_tables()
        storage.list_tables()
    storage.list_tables()

    assert outer.operation_count == 2
    assert inner.operation_count == 1
    assert all(
        event.action == "catalog.names"
        for event in _stage(outer.build_stages(), "b.catalog").events
    )


def test_b_stages_can_be_embedded_in_query_trace_json(tmp_path) -> None:
    """B 的四阶段应可直接并入公共 QueryTrace 并序列化为 JSON。"""

    router = StorageTraceRouter()
    with router.capture() as collector:
        _server, storage = _server_with_table(tmp_path, router)
        list(storage.scan("users"))

    trace = QueryTrace(
        trace_id="query-000001",
        query_number=1,
        sql="SELECT * FROM users;",
        database="main",
        status=TraceStatus.SUCCESS,
        stages=collector.build_stages(),
    )
    decoded = json.loads(trace.to_json())
    assert [stage["stage_id"] for stage in decoded["stages"]] == [
        "b.catalog", "b.cache", "b.pager", "b.engine"
    ]
    assert decoded["stages"][1]["events"][0]["action"].startswith("cache.")
