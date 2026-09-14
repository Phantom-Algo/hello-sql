"""模块 A：SQL 编译层的唯一公开入口。

外部模块通过以下两个稳定入口使用 A：

    from compiler import parse
    statement = parse("SELECT * FROM users;")

    from compiler import parse_script
    statements = parse_script("CREATE DATABASE shop; USE shop;")

``parse`` 依次协调 lexer 和 parser：先将 SQL 字符串切分为带位置的 Token，
再将 Token 按 V1.1 文法、V2 BOOLEAN/Column/TableRef/INNER JOIN/逻辑表达式，
以及 V3 CREATE INDEX/DROP INDEX 语法构建为 contracts.ast.Statement。A 的职责在
AST 产生时结束；它绝不查询数据库、访问磁盘、调用 Storage，也不执行条件。

本包只导出 parse 和 parse_script，避免调用方依赖 Lexer、Parser 内部实现。
支持 CREATE/DROP DATABASE、USE、CREATE/DROP TABLE、CREATE/DROP INDEX、
INSERT、SELECT、UPDATE、DELETE。错误统一抛出带行列号的
contracts.errors.ParseError。
"""

from __future__ import annotations

from compiler.lexer import tokenize
from compiler.parser import Parser
from contracts.ast import Script, Statement


# 此函数是模块 A 唯一面向 runner、main.py 和测试代码的稳定公开 API。
def parse(sql: str) -> Statement:
    """把一条完整 SQL 文本编译为契约规定的 AST Statement。

    编译过程固定分为两个内部阶段：

    1. ``tokenize(sql)``：词法分析，识别关键字、标识符、字面量、运算符，
       并为每个 Token 记录从 1 开始的行列位置和零基源码偏移；
    2. ``Parser(tokens).parse()``：语法分析，验证当前已支持的文法、将标识符
       统一转小写、将字面量转换为 Python 原生值，并构建对应的 AST 数据类。

    本函数不进行任何语义或运行时检查：数据库/表/列是否存在、INSERT 的值
    数量和类型是否匹配、WHERE 条件是否命中行，均是 runner 与 storage 的职责。
    输入只能包含一条 SQL，结尾允许至多一个分号。

    Args:
        sql: 待编译的一条 SQL 文本。关键字大小写不敏感；数据库名、表名、
            列名和索引名会在 AST 中统一保存为小写。

    Returns:
        contracts.ast.Statement 联合类型中的一个具体 AST 节点。

    Raises:
        ParseError: SQL 含非法字符、未闭合字符串、文法错误、多条语句，或
            不属于当前支持子集时抛出。异常携带 E_SYNTAX 及精确行列号。
    """
    tokens = tokenize(sql)
    return Parser(tokens).parse()


# 此函数是模块 A 面向多语句执行器和 SQL 文件入口的稳定脚本解析 API。
def parse_script(sql: str) -> Script:
    """把完整 SQL 脚本解析为带原文及全局范围的有序语句元组。

    Lexer 只运行一次，Parser 随后连续消费同一个 Token 流；实现不会调用
    ``split(';')``，所以 ``INSERT ... VALUES ('a;b')`` 中的分号不会切断语句。
    各语句之间必须以分号分隔，末条分号可省略，空白输入返回空元组。
    V3 的 CREATE INDEX 与 DROP INDEX 和既有语句使用完全相同的 Token 游标、
    原文切片及 SourceSpan 构建流程，不存在索引 DDL 专用的字符串分割路径。

    Args:
        sql: 可能包含零条、一条或多条语句的完整 SQL 源码。

    Returns:
        按源码顺序排列的 ParsedStatement 元组，每项包含 AST、原文和 SourceSpan。

    Raises:
        ParseError: 任一语句非法、出现空语句或相邻语句缺少分号时抛出。
    """
    tokens = tokenize(sql)
    return Parser(tokens).parse_script(sql)


__all__ = ["parse", "parse_script"]
