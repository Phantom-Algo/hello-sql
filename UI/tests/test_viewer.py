"""HELLO-SQL 本地查看器的资源、JSON API 与生命周期测试。

测试只访问服务器绑定的 ``127.0.0.1`` 随机端口，不连接外网。
浏览器打开调用使用 mock，验证跨平台调用语义而不弹出真实窗口。
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import pytest

from compiler import parse, parse_script
from runner import Runner
from storage import DatabaseServer
from UI import (
    InspectionModule,
    InspectionSnapshot,
    InspectionViewer,
    QueryInspector,
    QueryTrace,
    TraceStatus,
    trace_parse,
)


def _inspector_with_trace(tmp_path) -> QueryInspector:
    """执行一条真实 DDL 并返回含完整 A/B/C 记录的 Inspector。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    runner.execute("CREATE TABLE visual_demo (id INT, enabled BOOLEAN);")
    return inspector


def _compiler_only_snapshot(sql: str) -> InspectionSnapshot:
    """把一次成功 A 编译追踪包装成查看器可读取的只读快照。

    该辅助函数只做 A 阶段编译，因此 A 的 AST 可视化可以脱离运行、
    存储阶段独立验收。它只使用 trace_parse 已产生的真实四阶段记录
    构造 QueryTrace，不补造运行、存储事件，也不修改公共追踪契约。

    Args:
        sql: 需要在查看器 NODES 面板中验证的单条索引 DDL。

    Returns:
        仅包含 A 阶段、可直接交给 InspectionViewer 的 InspectionSnapshot。
    """
    compiled = trace_parse(sql)
    assert compiled.succeeded
    parsed = compiled.statements[0]
    trace = QueryTrace(
        trace_id="trace-index-visual",
        query_number=1,
        sql=parsed.sql,
        database="main",
        status=TraceStatus.SUCCESS,
        stages=compiled.stages,
        source_span=parsed.span,
        result_summary={"kind": "compiler-only-verification"},
    )
    return InspectionSnapshot(trace, InspectionModule.A, trace.stages)


def _read_json(url: str) -> dict[str, object]:
    """读取本机查看器 URL 并解码为 UTF-8 JSON 字典。"""

    with urlopen(url, timeout=3) as response:
        assert response.headers["Cache-Control"] == "no-store"
        return json.loads(response.read().decode("utf-8"))


def test_viewer_serves_packaged_page_and_filtered_trace_api(tmp_path):
    """查看器应提供完整资源，API 应只返回指定模块阶段。

    除了验证 HTML 和追踪 JSON，本用例还固定阶段详情的隐藏契约。
    JavaScript 选中阶段后会为 ``empty-detail`` 设置 ``hidden``；CSS
    必须显式将该状态设为 ``display: none``，避免 ``.empty-state``
    的 grid 样式覆盖浏览器默认隐藏规则并把真实详情挤到下方。

    新版首屏默认展示节点树，并将原始 JSON 放进统一的
    ``debug-only`` 区域。顶部 ``debug-toggle`` 默认处于关闭状态，用户
    需要时才显示快照；节点、词法单元、页的摘要和关联关系则始终保留。桌面
    端处理流水线使用独立纵向滚动，避免十四个阶段撑长整页形成大片留白。
    """

    inspector = _inspector_with_trace(tmp_path)
    viewer = InspectionViewer(inspector.latest)
    root = viewer.start()
    try:
        with urlopen(root, timeout=3) as response:
            page = response.read().decode("utf-8")
        assert "HELLO-SQL" in page
        assert "处理阶段" in page
        assert "查询流程检查器" in page
        assert "原始数据" in page
        assert "AST / Logical Plan / Executor Tree" in page
        assert "Lexer 结果" in page
        assert "Buffer Cache / Pager / Storage Engine" in page
        for obsolete_english_label in (
            "QUERY FLOW INSPECTOR",
            "RAW DATA",
            "Processing stages",
            "FLOW EXPLORER",
            "Query structure",
            "SOURCE LINK",
            "RESET LINK",
            "TREE VISUALIZER",
            "PHYSICAL PAGES",
            "PAGE OPERATIONS",
        ):
            assert obsolete_english_label not in page
        assert 'data-view="nodes"' in page
        assert 'data-view="tokens"' in page
        assert 'data-view="pages"' in page
        assert 'id="node-stage-tabs"' in page
        assert 'id="node-tree"' in page
        assert '<div id="stage-pane" class="entity-pane" hidden>' in page
        assert '<div id="nodes-pane" class="entity-pane">' in page
        assert 'id="node-inspector"' in page
        assert 'id="token-inspector"' in page
        assert 'id="page-inspector"' in page
        assert 'id="debug-toggle"' in page
        assert 'id="node-facts"' in page
        assert 'id="token-facts"' in page
        assert 'id="page-facts"' in page
        assert 'id="event-toolbar"' in page
        assert 'id="technical-events-toggle"' in page
        assert "展开底层步骤" in page
        assert page.count('class="debug-only"') == 3
        assert page.count("debug-only") == 4
        assert 'class="raw-data-panel debug-only"' in page
        with urlopen(f"{root}app.css", timeout=3) as response:
            stylesheet = response.read().decode("utf-8")
        assert ".empty-state[hidden] { display: none; }" in stylesheet
        assert ".debug-only { display: none !important; }" in stylesheet
        assert "body.show-debug .debug-only { display: block !important; }" in stylesheet
        assert "height: clamp(580px, 68vh, 760px);" in stylesheet
        assert "overflow-y: auto;" in stylesheet
        assert "scrollbar-gutter: stable;" in stylesheet
        assert ".tree-children::before" in stylesheet
        assert ".tree-node" in stylesheet
        assert ".event-toolbar[hidden] { display: none; }" in stylesheet
        assert ".event-technical" in stylesheet
        with urlopen(f"{root}app.js", timeout=3) as response:
            script = response.read().decode("utf-8")
        assert "function buildNodeForest(nodes)" in script
        assert "function nodeTreeBranch(" in script
        assert 'view: "nodes"' in script
        assert 'state.view = "nodes"' in script
        assert "function setDebugVisibility(enabled)" in script
        assert "function toggleDebugVisibility()" in script
        assert "function renderFacts(containerId, facts)" in script
        assert "function statusText(status)" in script
        assert "function stageDisplayName(stage)" in script
        assert '"a.lexer": "Lexer"' in script
        assert '"a.parser": "Parser"' in script
        assert '"a.source_span": "SourceSpan"' in script
        assert '"c.logical_plan": "Logical Plan"' in script
        assert "function eventActionText(action)" in script
        assert 'SUCCESS: "成功"' in script
        assert '"runtime.select.execute": "执行查询"' in script
        assert "function eventDetailLevel(event)" in script
        assert "function renderStageEvents(stage)" in script
        assert "function toggleTechnicalEvents()" in script
        assert "setDebugVisibility(false)" in script
        assert 'node.kind === "CreateIndexStmt"' in script
        assert 'node.kind === "DropIndexStmt"' in script

        payload = _read_json(f"{root}api/trace?module=A")
        assert payload["module"] == "A"
        assert payload["stages"]
        assert {stage["owner"] for stage in payload["stages"]} == {"A"}
        assert payload["linkage"]["tokens"]
        assert payload["linkage"]["nodes"]
        assert payload["linkage"]["pages"] == []
        assert viewer.start() == root
    finally:
        viewer.close()
    assert not viewer.running


def test_viewer_rejects_unknown_filter_and_reports_empty_hub():
    """未知模块应返回 400，尚无查询的 Hub 应返回 404。"""

    inspector = QueryInspector()
    viewer = InspectionViewer(inspector.latest)
    root = viewer.start()
    try:
        with pytest.raises(HTTPError) as invalid:
            urlopen(f"{root}api/trace?module=D", timeout=3)
        assert invalid.value.code == 400
        with pytest.raises(HTTPError) as empty:
            urlopen(f"{root}api/trace?module=ALL", timeout=3)
        assert empty.value.code == 404
    finally:
        viewer.close()


def test_query_inspector_lazily_opens_and_closes_cross_platform_view(tmp_path):
    """首次 open_view 应调用默认浏览器，close_view 后可安全重建。"""

    inspector = _inspector_with_trace(tmp_path)
    with patch("UI.viewer.webbrowser.open", return_value=True) as opener:
        first_url, opened = inspector.open_view("b")
        second_url, _ = inspector.open_view("C")
    assert opened
    assert "module=B" in first_url
    assert "module=C" in second_url
    assert first_url.split("?", 1)[0] == second_url.split("?", 1)[0]
    assert opener.call_count == 2

    inspector.close_view()
    with patch("UI.viewer.webbrowser.open", return_value=False):
        rebuilt_url, rebuilt_opened = inspector.open_view("ALL")
    assert not rebuilt_opened
    assert "module=ALL" in rebuilt_url
    inspector.close_view()


# 两个用例通过查看器真实 JSON API 验证创建和删除索引节点均可供网页树渲染。
@pytest.mark.parametrize(
    ("sql", "node_type", "expected_fields"),
    [
        (
            "CREATE INDEX idx_users_id ON users (id);",
            "CreateIndexStmt",
            {"index_name": "idx_users_id", "table": "users", "column": "id"},
        ),
        (
            "DROP INDEX idx_users_id;",
            "DropIndexStmt",
            {"index_name": "idx_users_id"},
        ),
    ],
)
def test_viewer_exposes_index_ast_nodes_to_inspect_tree(
    sql: str,
    node_type: str,
    expected_fields: dict[str, str],
) -> None:
    """验证 /inspect A 的 linkage 包含可点击的索引 AST 根节点。

    浏览器树只消费 API 的 linkage.nodes，因此本测试启动真实本机查看器、读取
    module=A JSON，并检查节点类型、阶段、父子关系和完整快照字段。只要这些
    数据存在，通用 renderNodeTree 就会按 a.ast 树入口渲染节点卡片；前端的
    nodeSummary 再为 CREATE/DROP INDEX 提供人类可读摘要。

    Args:
        sql: 当前查看器快照中的索引 DDL 原文。
        node_type: 树节点应显示的 AST 类型名称。
        expected_fields: 点击节点后详情面板应显示的 AST 字段。
    """
    snapshot = _compiler_only_snapshot(sql)
    viewer = InspectionViewer(lambda module: snapshot)
    root = viewer.start()
    try:
        payload = _read_json(f"{root}api/trace?module=A")
        ast_nodes = [
            node
            for node in payload["linkage"]["nodes"]
            if node["stage_id"] == "a.ast"
        ]

        assert len(ast_nodes) == 1
        assert ast_nodes[0]["kind"] == node_type
        assert ast_nodes[0]["parent_id"] is None
        assert ast_nodes[0]["snapshot"] == {
            "node_type": node_type,
            "fields": expected_fields,
        }
        assert ast_nodes[0]["token_ids"]
    finally:
        viewer.close()
