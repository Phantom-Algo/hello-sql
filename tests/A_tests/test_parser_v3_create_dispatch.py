"""V3 ``CREATE`` 二级语句分派测试。

本文件只验收步骤三的分派责任：Parser 消费 ``CREATE`` 后看到
``INDEX`` 时，必须调用 ``_parse_create_index_statement``，而不再将它
当作未知标识符。本测试使用可观测的替身方法隔离语句体实现，
完整 CREATE INDEX 语法与 AST 内容由专门的解析测试覆盖。
"""

from __future__ import annotations

import pytest

from compiler.lexer import tokenize
from compiler.parser import Parser
from compiler.tokens import TokenType
from contracts.ast import CreateIndexStmt
from contracts.errors import ParseError


# 该测试用替身函数记录入口 Token，以直接证明 INDEX 分支被选中。
def test_create_index_is_dispatched_to_dedicated_parser(monkeypatch) -> None:
    """确认 ``CREATE INDEX`` 到达专用解析函数且保持正确 Token 边界。

    替身函数进入时应当看到 ``KW_INDEX``，这证明上层只消费了
    ``CREATE``；替身再消费 ``INDEX`` 并返回一个可识别的 AST，
    用于证明 ``parse_statement`` 没有覆盖或丢失专用函数的返回值。

    Args:
        monkeypatch: pytest 提供的临时属性替换工具，用例结束后自动恢复原方法。
    """
    observed_tokens: list[TokenType] = []
    expected = CreateIndexStmt(index_name="idx_users_id", table="users", column="id")

    def fake_create_index_parser(parser: Parser) -> CreateIndexStmt:
        """记录分派入口，并模拟专用解析方法的 AST 返回值。"""
        observed_tokens.append(parser.peek().type)
        parser.expect(TokenType.KW_INDEX)
        return expected

    monkeypatch.setattr(
        Parser,
        "_parse_create_index_statement",
        fake_create_index_parser,
    )
    parser = Parser(tokenize("CREATE INDEX idx_users_id ON users (id)"))

    statement = parser.parse_statement()

    assert observed_tokens == [TokenType.KW_INDEX]
    assert statement == expected
    assert parser.peek().lexeme == "idx_users_id"


# 该测试锁定 CREATE 的错误提示，确保新增 INDEX 后三种合法目标均被列出。
def test_unknown_create_target_lists_all_supported_targets() -> None:
    """确认未知 CREATE 目标的语法错误同时提示 DATABASE、TABLE 和 INDEX。"""
    parser = Parser(tokenize("CREATE VIEW users"))

    with pytest.raises(ParseError) as error_info:
        parser.parse_statement()

    assert "expected DATABASE, TABLE, or INDEX after CREATE" in str(error_info.value)
