"""V3 DROP 二级语句分派测试。

本文件只验收步骤五的分派责任：Parser 消费 DROP 后看到 INDEX 时，必须
调用 _parse_drop_index_statement，而不能将 INDEX 当作普通标识符或未知
DROP 目标。测试使用可观测替身隔离语句体，只检查分支选择、Token 边界和
返回值传递；完整索引名解析与 AST 内容由专门的 DROP INDEX 测试覆盖。
"""

from __future__ import annotations

import pytest

from compiler.lexer import tokenize
from compiler.parser import Parser
from compiler.tokens import TokenType
from contracts.ast import DropIndexStmt
from contracts.errors import ParseError


# 该测试通过替身记录专用方法入口处的 Token，直接证明 INDEX 分支已被选中。
def test_drop_index_is_dispatched_to_dedicated_parser(monkeypatch) -> None:
    """确认 DROP INDEX 到达专用解析方法，并保持正确的 Token 消费边界。

    替身方法进入时必须看到 KW_INDEX，这证明 _parse_drop_statement 只消费了
    DROP。替身随后消费 INDEX 并返回固定 AST，用于验证上层分派不会修改或
    丢失专用方法的返回结果。

    Args:
        monkeypatch: pytest 提供的临时属性替换工具，用例结束后恢复原方法。
    """
    observed_tokens: list[TokenType] = []
    expected = DropIndexStmt(index_name="idx_users_id")

    def fake_drop_index_parser(parser: Parser) -> DropIndexStmt:
        """记录分派入口，并模拟专用索引删除解析方法的 AST 返回值。"""
        observed_tokens.append(parser.peek().type)
        parser.expect(TokenType.KW_INDEX)
        return expected

    monkeypatch.setattr(
        Parser,
        "_parse_drop_index_statement",
        fake_drop_index_parser,
    )
    parser = Parser(tokenize("DROP INDEX idx_users_id"))

    statement = parser.parse_statement()

    assert observed_tokens == [TokenType.KW_INDEX]
    assert statement == expected
    assert parser.peek().lexeme == "idx_users_id"


# 该测试锁定 DROP 的错误提示，确保新增 INDEX 后三种合法目标均被列出。
def test_unknown_drop_target_lists_all_supported_targets() -> None:
    """确认未知 DROP 目标会报告语法错误并列出 DATABASE、TABLE 与 INDEX。"""
    parser = Parser(tokenize("DROP VIEW users"))

    with pytest.raises(ParseError) as error_info:
        parser.parse_statement()

    assert "expected DATABASE, TABLE, or INDEX after DROP" in str(error_info.value)
