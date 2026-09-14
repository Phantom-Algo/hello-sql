"""V3 索引 DDL 的 parse 与 parse_script 兼容性测试。

本文件验证新增 CREATE INDEX、DROP INDEX 不改变编译模块的两个稳定公开入口：
parse 仍只返回一条 Statement；parse_script 仍在同一 Token 流上按顺序构建
ParsedStatement，并精确保留每条 SQL 原文和全局 SourceSpan。字符串字面量
中的分号与索引 DDL 放在同一脚本中测试，用行为证明实现没有退化为 split。
"""

from __future__ import annotations

from compiler import parse, parse_script
from contracts.ast import CreateIndexStmt, DropIndexStmt, InsertStmt, SourceSpan


# 该测试锁定题目给出的单语句入口，确保可选分号不会改变索引 AST。
def test_parse_accepts_one_create_index_statement() -> None:
    """验证 parse 能直接返回规范化的 CreateIndexStmt。

    单语句入口应继续消费至多一个结束分号并要求随后为 EOF；它不能因为新增
    parse_script 或索引语法而改变原有返回类型和结束规则。
    """
    statement = parse("CREATE INDEX idx ON users (id);")

    assert statement == CreateIndexStmt(
        index_name="idx",
        table="users",
        column="id",
    )


# 该测试锁定两条索引 DDL 的 AST 顺序、原始文本和完整脚本全局范围。
def test_parse_script_preserves_index_sql_and_global_spans() -> None:
    """验证多行索引脚本产生两个有序且可定位的 ParsedStatement。

    源码首行故意留空，使第一条语句从第 2 行开始；第二条语句必须继续使用
    第 3 行，而不能在每次调用语句解析器时把位置重置为第 1 行。结束分号属于
    对应语句，因此同时包含在 sql 字段和一基闭区间 SourceSpan 中。
    """
    source = (
        "\n"
        "CREATE INDEX idx ON users (id);\n"
        "DROP INDEX idx;\n"
    )

    script = parse_script(source)

    assert [item.statement for item in script] == [
        CreateIndexStmt(index_name="idx", table="users", column="id"),
        DropIndexStmt(index_name="idx"),
    ]
    assert [item.sql for item in script] == [
        "CREATE INDEX idx ON users (id);",
        "DROP INDEX idx;",
    ]
    assert [item.span for item in script] == [
        SourceSpan(start_line=2, start_col=1, end_line=2, end_col=31),
        SourceSpan(start_line=3, start_col=1, end_line=3, end_col=15),
    ]


# 该测试将字符串内分号与索引 DDL 混合，行为上证明脚本没有使用 split 分割。
def test_parse_script_keeps_string_semicolon_before_index_ddl() -> None:
    """验证字符串分号不会切断语句，后续索引 DDL 仍按顺序解析。

    如果实现使用 split(';')，字符串值 before;after 会被错误拆成两段，脚本
    无法得到三个 AST。当前实现只让 Lexer 产生的 SEMICOLON Token 结束语句，
    因而字符串 Token 内部的分号保持为普通值字符。
    """
    source = (
        "INSERT INTO logs VALUES ('before;after');\n"
        "CREATE INDEX idx ON logs (id);\n"
        "DROP INDEX idx;"
    )

    script = parse_script(source)

    assert [item.statement for item in script] == [
        InsertStmt(table="logs", values=("before;after",)),
        CreateIndexStmt(index_name="idx", table="logs", column="id"),
        DropIndexStmt(index_name="idx"),
    ]
