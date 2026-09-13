"""HELLO-SQL 全链路可视化的五类场景验收测试。

本模块不使用伪造的 AST、LogicalPlan 或存储回调，而是组装真实
Compiler、Runner、DatabaseServer 和 QueryInspector。因此每个断言都在验证
用户从 REPL 实际执行 SQL 后，``/inspect`` 能看到的同一份追踪数据。

验收矩阵包含五类必要路径：

* 成功：带别名、BOOLEAN、INNER JOIN 和 WHERE 的查询走完 A/B/C；
* 语法错误：Lexer 成功而 Parser 失败，AST 与 SourceSpan 阶段跳过；
* 语义错误：A 正常产生 AST，C 在 Binder 报告未知列；
* 存储错误：故意损坏临时表文件，验证 B 和 C 保留真实失败链；
* 多语句：验证全局 SourceSpan、逐条编号、继续策略与停止策略。

测试只断言稳定的错误码、阶段状态、结果和源码位置，不断言
具体耗时，避免答辩机器性能差异导致偶发失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import (
    E_COLUMN_NOT_FOUND,
    E_STORAGE,
    E_SYNTAX,
    E_VALUE_COUNT,
    ParseError,
    SqlError,
)
from runner import Runner
from storage import DatabaseServer
from storage.constants import PAGE_SIZE, TABLE_FILE_SUFFIX
from UI import QueryInspector, QueryTrace, StageTrace, TraceStatus


def _build_traced_runner(data_dir: Path) -> tuple[Runner, QueryInspector]:
    """在独立临时数据目录中组装开启 A/B/C 追踪的真实 Runner。

    Args:
        data_dir: pytest 为当前用例提供的临时数据根目录。

    Returns:
        ``(runner, inspector)``。Runner 负责真实解析和执行 SQL，
        Inspector 保存该次执行产生的不可变可视化快照。

    Notes:
        StorageTraceRouter 和 ExecutionTraceRouter 分别注入 B、C 模块。
        二者必须与 QueryInspector 来自同一实例，否则追踪会被
        分散到不同上下文，无法代表同一条查询。
    """

    inspector = QueryInspector(capacity=30)
    server = DatabaseServer(data_dir, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    return runner, inspector


def _require_stage(trace: QueryTrace, stage_id: str) -> StageTrace:
    """按稳定 ``stage_id`` 取得唯一阶段，并在缺失或重复时立即失败。

    Args:
        trace: 一条已发布的完整 QueryTrace。
        stage_id: 要查找的公开阶段标识，例如 ``c.binding``。

    Returns:
        与标识匹配的唯一 StageTrace。

    这个辅助函数同时固定“每条追踪中阶段 ID 唯一”的界面契约，
    避免各个用例重复编写遍历逻辑。
    """

    matches = tuple(stage for stage in trace.stages if stage.stage_id == stage_id)
    assert len(matches) == 1, f"expected one stage {stage_id!r}, got {len(matches)}"
    return matches[0]


def _oldest_first(inspector: QueryInspector) -> tuple[QueryTrace, ...]:
    """把 TraceHub 的“新到旧”快照转换为 SQL 脚本的源码执行顺序。

    Args:
        inspector: 保存当前会话追踪的 QueryInspector。

    Returns:
        按 query_number 从小到大排列的 QueryTrace 元组。

    TraceHub.recent() 面向界面的“最近记录”列表，因此默认最新优先；
    多语句验收需要与源码顺序对齐，故在这里集中反转一次。
    """

    return tuple(reversed(inspector.hub.recent()))


def test_successful_join_exposes_complete_pipeline_result_and_linkage(tmp_path: Path):
    """成功 JOIN 应走完 14 个阶段，并同时产生结果、节点和数据页。

    用例先在真实存储中建立 users/orders 数据，再执行一条包含
    表别名、限定列、INNER JOIN、BOOLEAN 和 WHERE 的 SELECT。这是
    成功路径的综合样例，能证明 A 的 Token/AST、B 的页操作以及
    C 的计划/Executor 来自同一次执行。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    runner.execute_script(
        "CREATE TABLE users (id INT, enabled BOOLEAN);\n"
        "CREATE TABLE orders (user_id INT, total INT);\n"
        "INSERT INTO users VALUES (1, TRUE);\n"
        "INSERT INTO users VALUES (2, FALSE);\n"
        "INSERT INTO orders VALUES (1, 80);\n"
        "INSERT INTO orders VALUES (2, 5);"
    )
    sql = (
        "SELECT u.id, o.total FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id "
        "WHERE u.enabled = TRUE;"
    )

    result = runner.execute(sql)

    assert result.columns == ("u.id", "o.total")
    assert result.rows == ((1, 80),)
    snapshot = inspector.latest("ALL")
    assert snapshot is not None
    trace = snapshot.trace
    assert trace.status is TraceStatus.SUCCESS
    assert trace.error_code is None
    assert [stage.sequence for stage in trace.stages] == list(range(1, 15))
    assert all(
        stage.status is TraceStatus.SUCCESS
        for stage in trace.stages
        if stage.stage_id != "c.optimizer"
    )
    assert _require_stage(trace, "c.optimizer").status is TraceStatus.DISABLED
    assert trace.result_summary["row_count"] == 1

    payload = snapshot.to_dict()
    linkage = payload["linkage"]
    assert linkage["source"] == sql
    assert "INNER" in {token["lexeme"] for token in linkage["tokens"]}
    assert {"a.ast", "c.logical_plan", "c.executor"} <= {
        node["stage_id"] for node in linkage["nodes"]
    }
    assert {"users", "orders"} <= {
        page["table"] for page in linkage["pages"] if page["table"] is not None
    }


def test_syntax_error_stops_at_parser_and_remains_serializable(tmp_path: Path):
    """语法错误应保留 Token 和精确位置，但不得伪造 AST、B 或 C 阶段。

    ``SELECT FROM users`` 可以被 Lexer 完整切分，但 SELECT 列表
    缺失，所以 Parser 应在 FROM 所在的第 1 行第 8 列失败。
    QueryInspector 仍需发布可 JSON 化的 FAILED 快照，然后向调用方
    原样重新抛出 ParseError。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    sql = "SELECT FROM users;"

    with pytest.raises(ParseError) as caught:
        runner.execute(sql)

    assert caught.value.code == E_SYNTAX
    assert (caught.value.line, caught.value.col) == (1, 8)
    trace = inspector.hub.latest()
    assert trace is not None
    assert trace.status is TraceStatus.FAILED
    assert trace.error_code == E_SYNTAX
    assert [stage.stage_id for stage in trace.stages] == [
        "c.repl",
        "a.lexer",
        "a.parser",
        "a.ast",
        "a.source_span",
    ]
    assert _require_stage(trace, "a.lexer").status is TraceStatus.SUCCESS
    assert _require_stage(trace, "a.parser").status is TraceStatus.FAILED
    assert _require_stage(trace, "a.parser").error_code == E_SYNTAX
    assert _require_stage(trace, "a.ast").status is TraceStatus.SKIPPED
    assert _require_stage(trace, "a.source_span").status is TraceStatus.SKIPPED

    snapshot = inspector.latest("ALL")
    assert snapshot is not None
    payload = snapshot.to_dict()
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == E_SYNTAX
    assert payload["source_span"]["start_col"] == 8
    assert payload["linkage"]["source"] == sql
    assert [token["lexeme"] for token in payload["linkage"]["tokens"][:2]] == [
        "SELECT",
        "FROM",
    ]
    assert payload["linkage"]["nodes"] == []
    assert payload["linkage"]["pages"] == []


def test_semantic_error_is_distinguished_from_parser_and_runtime(tmp_path: Path):
    """未知列应在 Binder 中失败，并明确与语法错误、运行错误分层。

    表 users 真实存在，但投影列 missing 不在 Schema 中。因此
    A 的四个编译阶段必须全部成功，B/Catalog 可以返回表结构，
    C/Binder 和 Logical Plan 记录 ``E_COLUMN_NOT_FOUND``，而
    Executor/Runtime 因计划未建立只能标记为 ``SKIPPED``。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    runner.execute("CREATE TABLE users (id INT);")

    with pytest.raises(SqlError) as caught:
        runner.execute("SELECT missing FROM users;")

    assert caught.value.code == E_COLUMN_NOT_FOUND
    trace = inspector.hub.latest()
    assert trace is not None
    assert trace.status is TraceStatus.FAILED
    assert trace.error_code == E_COLUMN_NOT_FOUND
    assert all(
        _require_stage(trace, stage_id).status is TraceStatus.SUCCESS
        for stage_id in ("a.lexer", "a.parser", "a.ast", "a.source_span")
    )
    assert _require_stage(trace, "b.catalog").status is TraceStatus.SUCCESS
    assert _require_stage(trace, "c.binding").status is TraceStatus.FAILED
    assert _require_stage(trace, "c.binding").error_code == E_COLUMN_NOT_FOUND
    assert _require_stage(trace, "c.logical_plan").status is TraceStatus.FAILED
    assert _require_stage(trace, "c.executor").status is TraceStatus.SKIPPED
    assert _require_stage(trace, "c.runtime").status is TraceStatus.SKIPPED

    snapshot = inspector.latest("C")
    assert snapshot is not None
    payload = snapshot.to_dict()
    assert payload["module"] == "C"
    assert payload["error_code"] == E_COLUMN_NOT_FOUND
    assert {stage["owner"] for stage in payload["stages"]} == {"C"}


def test_corrupt_table_file_preserves_storage_and_runtime_failure_chain(
    tmp_path: Path,
):
    """非整页表文件应产生 E_STORAGE，并保留 B 到 C 的失败传播路径。

    用例先通过公开 SQL 正常建表和写行，再仅对 pytest 临时目录
    中的 ``users.table`` 追加一字节。这会精确破坏“文件长度是
    PAGE_SIZE 整数倍”的存储不变量，不依赖权限、空间或操作系统
    偶然性。SELECT 的绑定和 Executor 建树仍应成功，真正失败点必须
    出现在 Pager/Engine 和消费行的 Runtime。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    runner.execute("CREATE TABLE users (id INT);")
    runner.execute("INSERT INTO users VALUES (1);")
    table_path = tmp_path / "main" / f"users{TABLE_FILE_SUFFIX}"
    assert table_path.stat().st_size % PAGE_SIZE == 0

    # 只污染当前用例的临时表文件，以触发真实 Pager 校验分支。
    with table_path.open("ab") as stream:
        stream.write(b"\x00")
    assert table_path.stat().st_size % PAGE_SIZE == 1

    with pytest.raises(SqlError) as caught:
        runner.execute("SELECT * FROM users;")

    assert caught.value.code == E_STORAGE
    trace = inspector.hub.latest()
    assert trace is not None
    assert trace.status is TraceStatus.FAILED
    assert trace.error_code == E_STORAGE
    assert _require_stage(trace, "c.binding").status is TraceStatus.SUCCESS
    assert _require_stage(trace, "c.logical_plan").status is TraceStatus.SUCCESS
    assert _require_stage(trace, "c.executor").status is TraceStatus.SUCCESS
    for stage_id in ("b.pager", "b.engine", "c.runtime"):
        stage = _require_stage(trace, stage_id)
        assert stage.status is TraceStatus.FAILED
        assert stage.error_code == E_STORAGE
        assert stage.events
    assert trace.result_summary == {"kind": "error", "phase": "execute"}


def test_multistatement_continue_mode_keeps_each_result_trace_and_global_span(
    tmp_path: Path,
):
    """多语句继续模式应为失败前后每条语句保留独立追踪。

    第 3 条 INSERT 故意少一个 BOOLEAN 值，从而产生稳定的
    ``E_VALUE_COUNT`` 语义错误。``stop_on_error=False`` 要求第 4 条
    INSERT 和第 5 条 SELECT 仍然执行。除了 ScriptResult，本用例还
    核对 QueryTrace 编号、脚本下标、全局行列坐标和最后语句的
    Token 原文联动。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    source = (
        "CREATE TABLE items (id INT, enabled BOOLEAN);\n"
        "INSERT INTO items VALUES (1, TRUE);\n"
        "INSERT INTO items VALUES (2);\n"
        "INSERT INTO items VALUES (3, FALSE);\n"
        "SELECT * FROM items;"
    )

    result = runner.execute_script(source, stop_on_error=False)

    assert result.stopped_early is False
    assert len(result.statements) == 5
    assert [
        item.error.code if item.error is not None else None
        for item in result.statements
    ] == [None, None, E_VALUE_COUNT, None, None]
    assert result.statements[-1].result is not None
    assert result.statements[-1].result.rows == ((1, True), (3, False))

    traces = _oldest_first(inspector)
    assert [trace.query_number for trace in traces] == [1, 2, 3, 4, 5]
    assert [trace.statement_index for trace in traces] == [1, 2, 3, 4, 5]
    assert [trace.statement_count for trace in traces] == [5, 5, 5, 5, 5]
    assert [trace.status for trace in traces] == [
        TraceStatus.SUCCESS,
        TraceStatus.SUCCESS,
        TraceStatus.FAILED,
        TraceStatus.SUCCESS,
        TraceStatus.SUCCESS,
    ]
    assert traces[2].error_code == E_VALUE_COUNT
    assert [trace.source_span.start_line for trace in traces] == [1, 2, 3, 4, 5]

    snapshot = inspector.latest("ALL")
    assert snapshot is not None
    linkage = snapshot.to_dict()["linkage"]
    assert linkage["source"] == source
    assert linkage["tokens"][0]["lexeme"] == "SELECT"
    assert linkage["tokens"][0]["start_offset"] == source.index("SELECT")
    assert all(
        token["source_span"]["start_line"] == 5 for token in linkage["tokens"]
    )


def test_multistatement_stop_mode_does_not_publish_unexecuted_traces(tmp_path: Path):
    """多语句停止模式应在首个语义错误后保留未执行语句的空白。

    脚本在第 2 条 INSERT 产生 ``E_VALUE_COUNT``，默认
    ``stop_on_error=True`` 会返回 ``stopped_early=True``。第 3 条
    SELECT 既没有业务结果，也不能发布 QueryTrace，否则界面会误导
    用户认为该语句已被执行。已发布记录仍保留原脚本的
    ``statement_count=3``，便于解释“停在第 2/3 条”。
    """

    runner, inspector = _build_traced_runner(tmp_path)
    source = (
        "CREATE TABLE flags (id INT, enabled BOOLEAN);\n"
        "INSERT INTO flags VALUES (1);\n"
        "SELECT * FROM flags;"
    )

    result = runner.execute_script(source)

    assert result.stopped_early is True
    assert len(result.statements) == 2
    assert result.statements[1].error is not None
    assert result.statements[1].error.code == E_VALUE_COUNT
    traces = _oldest_first(inspector)
    assert [trace.statement_index for trace in traces] == [1, 2]
    assert [trace.statement_count for trace in traces] == [3, 3]
    assert [trace.status for trace in traces] == [
        TraceStatus.SUCCESS,
        TraceStatus.FAILED,
    ]
    assert all("SELECT" not in trace.sql for trace in traces)
