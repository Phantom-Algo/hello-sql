"""AST/计划/Executor 节点、Lexer Token、存储页与 SQL 联动索引测试。

测试通过真实查询产生追踪，然后只读取 ``InspectionSnapshot.to_dict``。
关键断言保证联动层没有重新执行 SQL，且模块筛选、多语句全局偏移、
双向 ID 与物理页事件保持一致。
"""

from __future__ import annotations

from compiler import parse, parse_script
from runner import Runner
from storage import DatabaseServer
from UI import QueryInspector, build_linkage


def _select_trace(tmp_path) -> tuple[QueryInspector, dict[str, object]]:
    """创建表和一行数据，再返回真实 SELECT 的 Inspector 与 ALL 快照字典。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    runner.execute_script(
        "CREATE TABLE users (id INT, enabled BOOLEAN); "
        "INSERT INTO users VALUES (1, TRUE);"
    )
    runner.execute(
        "SELECT u.id FROM users AS u WHERE u.enabled = TRUE;"
    )
    snapshot = inspector.latest("ALL")
    assert snapshot is not None
    return inspector, snapshot.to_dict()


def test_linkage_uses_real_token_offsets_and_builds_bidirectional_node_links(tmp_path):
    """Token 偏移应对应原文，Column 节点与 Token 应拥有双向 ID。"""

    _, data = _select_trace(tmp_path)
    linkage = data["linkage"]
    source = linkage["source"]
    tokens = linkage["tokens"]
    nodes = linkage["nodes"]

    assert "SELECT u.id" in source
    assert [token["lexeme"] for token in tokens[:4]] == ["SELECT", "u", ".", "id"]
    for token in tokens:
        assert source[token["start_offset"]:token["end_offset"]] == token["lexeme"]

    column = next(
        node
        for node in nodes
        if node["stage_id"] == "a.ast"
        and node["kind"] == "Column"
        and node["snapshot"]["fields"]["name"] == "id"
    )
    linked_lexemes = {
        token["lexeme"]
        for token in tokens
        if token["token_id"] in column["token_ids"]
    }
    assert {"u", "id"} <= linked_lexemes
    assert all(
        column["node_id"] in token["node_ids"]
        for token in tokens
        if token["token_id"] in column["token_ids"]
    )


def test_physical_pages_merge_cache_pager_events_and_link_back_to_table(tmp_path):
    """同一物理页的 Cache/Pager 事件应合并，并反向关联 users Token/节点。"""

    _, data = _select_trace(tmp_path)
    linkage = data["linkage"]
    pages = linkage["pages"]
    assert {page["page_number"] for page in pages} >= {0, 1}

    data_page = next(page for page in pages if page["page_number"] == 1)
    assert data_page["file_name"] == "users.table"
    assert data_page["table"] == "users"
    assert {event["stage_id"] for event in data_page["events"]} >= {
        "b.cache",
        "b.pager",
    }
    users_token = next(
        token for token in linkage["tokens"] if token["lexeme"] == "users"
    )
    assert data_page["page_id"] in users_token["page_ids"]
    assert data_page["token_ids"] == [users_token["token_id"]]
    assert data_page["node_ids"]


def test_module_filter_keeps_source_tokens_but_limits_nodes_and_pages(tmp_path):
    """A/B/C 筛选应隐藏其他模块对象，同时保留 SQL Token 锚点。"""

    inspector, _ = _select_trace(tmp_path)
    a = inspector.latest("A").to_dict()["linkage"]
    b = inspector.latest("B").to_dict()["linkage"]
    c = inspector.latest("C").to_dict()["linkage"]

    assert a["tokens"] and {node["stage_id"] for node in a["nodes"]} == {"a.ast"}
    assert a["pages"] == []
    assert b["tokens"] and b["nodes"] == [] and b["pages"]
    assert c["tokens"] and c["pages"] == []
    assert {node["stage_id"] for node in c["nodes"]} == {
        "c.logical_plan",
        "c.executor",
    }


def test_multistatement_linkage_keeps_full_source_and_only_active_tokens(tmp_path):
    """脚本最后语句应使用全局偏移，但 Token 列表只包含当前语句。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    source = "CREATE TABLE flags (id INT);\nSELECT * FROM flags;"
    runner.execute_script(source)
    snapshot = inspector.latest()
    assert snapshot is not None

    linkage = build_linkage(snapshot)
    assert linkage["source"] == source
    assert linkage["tokens"][0]["lexeme"] == "SELECT"
    assert linkage["tokens"][0]["start_offset"] == source.index("SELECT")
    assert all(token["source_span"]["start_line"] == 2 for token in linkage["tokens"])


def test_building_linkage_does_not_publish_or_replace_trace(tmp_path):
    """重复构建联动索引不应增加查询编号或替换 TraceHub 对象。"""

    inspector, _ = _select_trace(tmp_path)
    snapshot = inspector.latest()
    assert snapshot is not None
    latest_before = inspector.hub.latest()
    count_before = len(inspector.hub)

    first = build_linkage(snapshot)
    second = build_linkage(snapshot)

    assert first == second
    assert len(inspector.hub) == count_before
    assert inspector.hub.latest() is latest_before
