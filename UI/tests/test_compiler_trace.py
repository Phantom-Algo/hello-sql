"""验证 A 追踪的真实编译结果、全局位置、AST 快照与失败链路。"""

from __future__ import annotations

import pytest

from compiler import parse, parse_script
from contracts.ast import SourceSpan
from contracts.errors import ParseError
from UI import CompilerTraceMode, TraceStatus, trace_parse, trace_parse_script


def _stage(result: object, stage_id: str):
    """按稳定 stage_id 从一次编译追踪中取得唯一阶段，简化测试断言。"""

    return next(stage for stage in result.stages if stage.stage_id == stage_id)


def test_single_trace_matches_parse_and_has_four_successful_stages() -> None:
    """单语句追踪应与正式 parse 产生相同 AST，且四个阶段全部成功。"""

    sql = (
        "SELECT u.id FROM users u INNER JOIN orders o "
        "ON u.id = o.user_id WHERE NOT u.enabled = FALSE;"
    )
    result = trace_parse(sql)

    assert result.succeeded
    assert result.mode is CompilerTraceMode.SINGLE
    assert result.require_statement() == parse(sql)
    assert [stage.stage_id for stage in result.stages] == [
        "a.lexer", "a.parser", "a.ast", "a.source_span"
    ]
    assert [stage.sequence for stage in result.stages] == [2, 3, 4, 5]
    assert all(stage.status is TraceStatus.SUCCESS for stage in result.stages)


def test_lexer_events_keep_token_positions_offsets_and_eof() -> None:
    """Lexer 事件应保存 Token 精确坐标、半开偏移和零长度 EOF 哨兵。"""

    lexer = _stage(trace_parse("\n SELECT u.id FROM users u;"), "a.lexer")
    select_event = lexer.events[0]
    dot_event = next(event for event in lexer.events if event.action == "识别 DOT")
    eof_event = lexer.events[-1]

    assert select_event.output_snapshot["token"] == {
        "type": "KW_SELECT",
        "lexeme": "SELECT",
        "line": 2,
        "column": 2,
        "start_offset": 2,
        "end_offset": 8,
    }
    assert select_event.source_span == SourceSpan(2, 2, 2, 7)
    assert dot_event.output_snapshot["token"]["lexeme"] == "."
    assert eof_event.output_snapshot["token"]["type"] == "EOF"
    assert eof_event.source_span is None


def test_parser_events_show_real_rule_order_depth_and_consumption() -> None:
    """Parser 事件应按进入顺序展示递归规则、深度和实际消费 Token。"""

    parser = _stage(
        trace_parse("SELECT id FROM users WHERE NOT active = TRUE;"),
        "a.parser",
    )
    rules = [event.input_snapshot["rule"] for event in parser.events]

    assert rules[:3] == ["parse", "parse_statement", "_parse_select_statement"]
    assert "_parse_not_expression" in rules
    assert "_parse_comparison_operator" in rules
    assert max(event.metrics["depth"] for event in parser.events) > 2
    assert parser.events[0].metrics["consumed_token_count"] > 0


def test_ast_stage_contains_v2_nodes_and_does_not_invent_child_spans() -> None:
    """AST 事件应展示 V2 节点，且只有语句根关联现有契约的 SourceSpan。"""

    result = trace_parse("SELECT u.id FROM users u WHERE u.enabled = TRUE;")
    ast = _stage(result, "a.ast")
    actions = [event.action for event in ast.events]

    assert actions[0] == "生成 SelectStmt 节点"
    assert "生成 TableRef 节点" in actions
    assert "生成 Column 节点" in actions
    assert "生成 Cmp 节点" in actions
    assert "生成 Literal 节点" in actions
    assert ast.events[0].source_span == result.statements[0].span
    assert all(event.source_span is None for event in ast.events[1:])
    assert ast.output_snapshot["trees"][0]["node_type"] == "SelectStmt"


def test_script_trace_preserves_original_sql_and_global_spans() -> None:
    """脚本追踪应等于正式 parse_script，并保留第二行的全局行列位置。"""

    sql = "CREATE DATABASE shop;\n  USE Shop;"
    result = trace_parse_script(sql)
    spans = _stage(result, "a.source_span")

    assert result.succeeded
    assert result.mode is CompilerTraceMode.SCRIPT
    assert result.statements == parse_script(sql)
    assert [item.sql for item in result.statements] == [
        "CREATE DATABASE shop;", "USE Shop;"
    ]
    assert result.statements[1].span == SourceSpan(2, 3, 2, 11)
    assert spans.events[1].output_snapshot["sql"] == "USE Shop;"


def test_lexical_error_fails_lexer_and_skips_downstream() -> None:
    """非法字符应使 Lexer 失败并令 Parser、AST、SourceSpan 全部跳过。"""

    result = trace_parse("SELECT @ FROM users;")

    assert not result.succeeded
    assert [stage.status for stage in result.stages] == [
        TraceStatus.FAILED, TraceStatus.SKIPPED,
        TraceStatus.SKIPPED, TraceStatus.SKIPPED,
    ]
    assert result.error is not None
    assert (result.error.line, result.error.col) == (1, 8)
    assert result.stages[0].source_span == SourceSpan(1, 8, 1, 8)


def test_syntax_error_keeps_lexer_and_failed_parser_rule_chain() -> None:
    """语法错误应保留成功 Token 和失败规则链，只跳过 AST 与 SourceSpan。"""

    result = trace_parse("SELECT FROM users;")
    parser = _stage(result, "a.parser")

    assert [stage.status for stage in result.stages] == [
        TraceStatus.SUCCESS, TraceStatus.FAILED,
        TraceStatus.SKIPPED, TraceStatus.SKIPPED,
    ]
    assert parser.error_code == "E_SYNTAX"
    assert any(event.output_snapshot["status"] == "failed" for event in parser.events)
    assert parser.source_span == SourceSpan(1, 8, 1, 8)


def test_single_rejects_multiple_statements_while_script_accepts() -> None:
    """两个新入口必须分别保留 parse 和 parse_script 的语句数量语义。"""

    sql = "CREATE DATABASE shop; USE shop;"

    assert not trace_parse(sql).succeeded
    assert len(trace_parse_script(sql).statements) == 2


def test_raise_for_error_restores_original_exception_contract() -> None:
    """C 发布失败追踪后仍应能向原调用方抛出同一个 E_SYNTAX 对象。"""

    result = trace_parse("SELECT FROM users;")

    with pytest.raises(ParseError) as captured:
        result.raise_for_error()
    assert captured.value is result.error
    assert captured.value.code == "E_SYNTAX"


def test_empty_script_is_a_successful_zero_statement_trace() -> None:
    """空脚本应成功，并生成零 AST 节点和零 SourceSpan 事件。"""

    result = trace_parse_script(" \n\t")

    assert result.succeeded
    assert result.statements == ()
    assert all(stage.status is TraceStatus.SUCCESS for stage in result.stages)
    assert _stage(result, "a.ast").metrics["node_count"] == 0
    assert _stage(result, "a.source_span").events == ()


# 两个参数分别锁定 V3 的创建与删除索引节点，证明 AST 追踪没有类型白名单。
@pytest.mark.parametrize(
    ("sql", "node_type", "expected_fields", "parser_rule"),
    [
        (
            "CREATE INDEX idx_users_id ON users (id);",
            "CreateIndexStmt",
            {"index_name": "idx_users_id", "table": "users", "column": "id"},
            "_parse_create_index_statement",
        ),
        (
            "DROP INDEX idx_users_id;",
            "DropIndexStmt",
            {"index_name": "idx_users_id"},
            "_parse_drop_index_statement",
        ),
    ],
)
def test_index_ddl_enters_parser_and_ast_trace_stages(
    sql: str,
    node_type: str,
    expected_fields: dict[str, str],
    parser_rule: str,
) -> None:
    """验证索引 DDL 的真实 Parser 规则、AST 节点和字段都进入 A 追踪。

    测试同时查看 Parser 的规则事件和 AST 的前序事件，而不是只比较最终
    parse 结果。这样可以证明答辩界面展示的数据来自本次真实编译过程，并且
    CreateIndexStmt/DropIndexStmt 由通用 dataclass 快照器保存完整字段。

    Args:
        sql: 当前需要追踪的完整索引 DDL。
        node_type: AST 快照预期保存的具体 dataclass 类型名。
        expected_fields: 查看器节点详情中应出现的规范化字段。
        parser_rule: Parser 阶段应记录的索引专用规则名称。
    """
    result = trace_parse(sql)
    parser_stage = _stage(result, "a.parser")
    ast_stage = _stage(result, "a.ast")

    assert result.succeeded
    assert parser_rule in [
        event.input_snapshot["rule"] for event in parser_stage.events
    ]
    assert len(ast_stage.events) == 1
    assert ast_stage.events[0].action == f"生成 {node_type} 节点"
    assert ast_stage.events[0].source_span == result.statements[0].span
    assert ast_stage.output_snapshot["trees"] == (
        {"node_type": node_type, "fields": expected_fields},
    )


# 该测试确认脚本模式会按原顺序为两种索引语句各生成一棵 AST 根树。
def test_index_script_trace_contains_create_and_drop_ast_forest() -> None:
    """验证 CREATE/DROP INDEX 多语句追踪形成有序的两棵 AST 树。

    trace_parse_script 只扫描一次完整输入；AST 阶段应保存两个根节点事件，
    并让第二条 DROP INDEX 的 SourceSpan 继续使用完整脚本中的第 2 行位置。
    """
    result = trace_parse_script(
        "CREATE INDEX idx ON users (id);\n"
        "DROP INDEX idx;"
    )
    ast_stage = _stage(result, "a.ast")

    assert result.succeeded
    assert [event.action for event in ast_stage.events] == [
        "生成 CreateIndexStmt 节点",
        "生成 DropIndexStmt 节点",
    ]
    assert [tree["node_type"] for tree in ast_stage.output_snapshot["trees"]] == [
        "CreateIndexStmt",
        "DropIndexStmt",
    ]
    assert result.statements[1].span == SourceSpan(2, 1, 2, 15)
