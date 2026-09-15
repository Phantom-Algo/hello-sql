"""契约 V3.1：错误码与异常（三个模块共用）。

归属规则（谁抛什么，写在谁的错误上）：
- E_SYNTAX：A 抛（ParseError）；
- 表 / 列 / 个数 / 类型类错误：C 在语义检查时抛；
  B 在自己的方法边界做同样的防御性检查时也抛同码；
- E_ROW_NOT_FOUND、E_STORAGE：B 抛；
- 数据库层：E_DATABASE_NOT_FOUND / E_DATABASE_EXISTS 由 B（DatabaseServer）抛；
  E_DATABASE_IN_USE 由 C 抛（正在使用的库）或 B 抛（默认库 main）。
- E_BAD_ARG：B 在公开方法边界上拒绝非法库名 / 表名时抛；
  正常 SQL 路径由 A 的标识符规则保证不会触发。
- 限定列、别名和布尔表达式错误由 C 在名称绑定或类型绑定阶段抛；
- SQL 文件无法读取由 C 的脚本执行入口抛 E_INPUT_FILE。
- 索引不存在（删索引、索引查找找不到对应索引、physical="index" 但无可用
  索引）与建索引重名由 B 抛 E_INDEX_NOT_FOUND / E_INDEX_EXISTS；索引列
  不存在复用 E_COLUMN_NOT_FOUND。索引存在性一律由 B 判定，C 不维护第二份
  判断；C 只在强制索引模式下无法为该查询构造索引请求时抛 E_BAD_ARG。

REPL 捕获 SqlError 后打印 [错误码] 消息，然后回到提示符。
"""

from __future__ import annotations


# 错误码全集：字符串值本身即契约，不要改动。
E_SYNTAX = "E_SYNTAX"
E_TABLE_NOT_FOUND = "E_TABLE_NOT_FOUND"
E_TABLE_EXISTS = "E_TABLE_EXISTS"
E_COLUMN_NOT_FOUND = "E_COLUMN_NOT_FOUND"
E_AMBIGUOUS_COLUMN = "E_AMBIGUOUS_COLUMN"
E_TABLE_QUALIFIER_NOT_FOUND = "E_TABLE_QUALIFIER_NOT_FOUND"
E_DUP_TABLE_ALIAS = "E_DUP_TABLE_ALIAS"
E_DUP_COLUMN = "E_DUP_COLUMN"
E_VALUE_COUNT = "E_VALUE_COUNT"
E_TYPE_MISMATCH = "E_TYPE_MISMATCH"
E_BOOLEAN_REQUIRED = "E_BOOLEAN_REQUIRED"
E_ROW_NOT_FOUND = "E_ROW_NOT_FOUND"
E_STORAGE = "E_STORAGE"

# 数据库层
E_DATABASE_NOT_FOUND = "E_DATABASE_NOT_FOUND"
E_DATABASE_EXISTS = "E_DATABASE_EXISTS"
E_DATABASE_IN_USE = "E_DATABASE_IN_USE"

# 参数非法（库名 / 表名不符合 [a-z_][a-z0-9_]*，或为空）
E_BAD_ARG = "E_BAD_ARG"

# 多语句与 SQL 文件执行
E_INPUT_FILE = "E_INPUT_FILE"

# 索引（V3）
E_INDEX_EXISTS = "E_INDEX_EXISTS"
E_INDEX_NOT_FOUND = "E_INDEX_NOT_FOUND"


class SqlError(Exception):
    """所有可预期错误的基类。message 建议用英文，便于测试稳定。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class ParseError(SqlError):
    """语法错误：A 唯一允许抛的异常，带行列号。"""

    def __init__(self, line: int, col: int, message: str) -> None:
        self.line = line
        self.col = col
        super().__init__(E_SYNTAX, f"syntax error at line {line}, col {col}: {message}")
