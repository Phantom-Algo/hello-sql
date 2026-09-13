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
from UI import InspectionViewer, QueryInspector


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
    """

    inspector = _inspector_with_trace(tmp_path)
    viewer = InspectionViewer(inspector.latest)
    root = viewer.start()
    try:
        with urlopen(root, timeout=3) as response:
            page = response.read().decode("utf-8")
        assert "HELLO-SQL" in page
        assert "Processing stages" in page
        assert 'data-view="nodes"' in page
        assert 'data-view="tokens"' in page
        assert 'data-view="pages"' in page
        assert 'id="node-stage-tabs"' in page
        assert 'id="node-tree"' in page
        with urlopen(f"{root}app.css", timeout=3) as response:
            stylesheet = response.read().decode("utf-8")
        assert ".empty-state[hidden] { display: none; }" in stylesheet
        assert ".tree-children::before" in stylesheet
        assert ".tree-node" in stylesheet
        with urlopen(f"{root}app.js", timeout=3) as response:
            script = response.read().decode("utf-8")
        assert "function buildNodeForest(nodes)" in script
        assert "function nodeTreeBranch(" in script

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
