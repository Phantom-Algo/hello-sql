"""编译模块的 Token（词法单元）基础定义。

本文件对应第 1 个实现步骤：定义词法分析器与语法分析器之间
传递的数据结构，但不在这里扫描 SQL 文本。后续的 ``lexer.py`` 负责把
SQL 字符串转换成 Token 序列，``parser.py`` 负责读取这些 Token 并构建
contracts.ast 中规定的 AST。

设计约定：
1. 每个 Token 都保留 SQL 源码中的原始文本（``lexeme``），以便报错时
   显示用户实际输入的内容；
2. 每个 Token 都保存起始位置（``position``），供 ParseError 精确定位；
3. 每个 Token 都保存零基、左闭右开的源码字符偏移 ``[start_offset,
   end_offset)``，后续可直接用 Python 切片恢复原文；
4. 行号与列号都从 1 开始计数。例如输入第一个字符的位置为 (1, 1)；
5. 关键字通过专用 TokenType 表示。这样 parser 可以清楚地区分关键字与
   普通标识符，也能拒绝把保留字作为库名、表名或列名。
6. V3 在本层只新增 ``INDEX`` 保留字，供后续 parser 区分
   ``CREATE INDEX`` / ``DROP INDEX`` 与已有数据库、数据表 DDL。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    """SQL 词法分析阶段可产生的全部 Token 类型。

    TokenType 只描述“这段文本在语法上是什么类别”，不保存文本内容。
    例如 ``users`` 与 ``orders`` 都是 IDENTIFIER，但各自的实际文字保存
    在 Token.lexeme 中。关键字采用 ``KW_`` 前缀，避免它们与普通标识符、
    Python 名称或 SQL 类型名称混淆。
    """

    # ---------- 输入结束 ----------
    # EOF 不是用户输入的字符；lexer 在扫描完全部 SQL 后补充它，
    # 使得 parser 能够可靠地检查“一次 parse 只能有一条完整语句”。
    EOF = auto()

    # ---------- 普通标识符与字面量 ----------
    # IDENTIFIER 对应尚未被识别为保留字的名称，例如 users、user_id、_tmp。
    IDENTIFIER = auto()
    # INTEGER_LITERAL 保存形如 18 或 -18 的整数字面量。
    INTEGER_LITERAL = auto()
    # REAL_LITERAL 保存形如 18.5 或 -0.5 的小数字面量。
    REAL_LITERAL = auto()
    # STRING_LITERAL 保存单引号字符串；lexeme 保留原始 SQL 片段，值的转义
    # 处理由 lexer 在后续实现中完成。
    STRING_LITERAL = auto()

    # ---------- SQL 语句关键字 ----------
    KW_CREATE = auto()
    KW_DATABASE = auto()
    KW_DROP = auto()
    KW_USE = auto()
    KW_TABLE = auto()
    KW_INSERT = auto()
    KW_INTO = auto()
    KW_VALUES = auto()
    KW_SELECT = auto()
    KW_FROM = auto()
    KW_WHERE = auto()
    KW_UPDATE = auto()
    KW_SET = auto()
    KW_DELETE = auto()

    # V3 单列索引 DDL 关键字。Token 层只负责把 INDEX 与普通
    # 标识符区分开；CREATE/DROP INDEX 的句法结构将由 parser 后续实现。
    KW_INDEX = auto()

    # V2 逻辑表达式关键字。AND、OR、NOT 在 parser 中将按 NOT > AND > OR
    # 的优先级构建 AST；Token 层只负责将它们与普通标识符区分开。
    KW_AND = auto()
    KW_OR = auto()
    KW_NOT = auto()

    # V2 表引用与 INNER JOIN 关键字。AS 用于显式表别名；INNER 可省略，
    # JOIN 和 ON 分别标记连接子句及其连接条件。
    KW_AS = auto()
    KW_INNER = auto()
    KW_JOIN = auto()
    KW_ON = auto()

    # ---------- SQL 布尔字面量关键字 ----------
    # TRUE 与 FALSE 在后续 parser 步骤中会转换为 Python bool。
    KW_TRUE = auto()
    KW_FALSE = auto()

    # ---------- SQL 类型关键字 ----------
    # 类型关键字只会出现在 CREATE TABLE 的列定义中。
    KW_INT = auto()
    KW_TEXT = auto()
    KW_REAL = auto()
    KW_BOOLEAN = auto()

    # ---------- 分隔符 ----------
    LPAREN = auto()       # (
    RPAREN = auto()       # )
    COMMA = auto()        # ,
    # DOT 用于分隔限定符与列名，例如 u.id；它不属于实数字面量内部的小数点。
    DOT = auto()          # .
    # STAR 只用于 SELECT *，表示“按建表顺序选择全部列”，不是乘法运算符。
    # V1 SQL 不支持算术表达式，因此 lexer 不为它赋予其他含义。
    STAR = auto()         # *
    SEMICOLON = auto()    # ;

    # ---------- 比较运算符 ----------
    EQ = auto()           # =
    NE = auto()           # <>
    LT = auto()           # <
    LE = auto()           # <=
    GT = auto()           # >
    GE = auto()           # >=


@dataclass(frozen=True)
class SourcePosition:
    """表示 SQL 文本中一个字符的起始位置。

    ``line`` 与 ``column`` 都采用从 1 开始的计数方式，符合编辑器和用户
    阅读错误消息时的习惯。Token 记录的是其第一个字符的位置：例如
    ``SELECT`` 位于输入开头时的位置为 ``SourcePosition(1, 1)``。

    该类保持不可变（frozen），保证 lexer 生成一个 Token 后，其错误位置
    不会在 parser 运行期间被意外修改。
    """

    line: int
    column: int


@dataclass(frozen=True)
class Token:
    """词法分析器交给语法分析器的一个最小、完整的词法单元。

    Attributes:
        type: Token 的类别，决定 parser 应如何解释该单元。
        lexeme: 该 Token 在原 SQL 中对应的原始文本。例如整数 ``18`` 的
            lexeme 为 ``"18"``，字符串 ``'alice'`` 的 lexeme 包含引号。
        position: Token 第一个字符在 SQL 输入中的行列位置。parser 检测到
            缺少关键字、非法 token 或多余内容时，使用此位置构造 ParseError。
        start_offset: Token 第一个字符在完整 SQL 字符串中的零基字符偏移。
        end_offset: Token 最后一个字符之后的零基字符偏移。该字段与
            start_offset 组成左闭右开的范围，可直接执行
            ``source[start_offset:end_offset]`` 得到 lexeme。

    Token 不直接保存最终 AST 值。例如 ``INTEGER_LITERAL`` 的 ``"18"``
    将由 parser 转为 int 18；这样既保留了精确错误上下文，也使词法阶段
    只专注于识别文本边界。偏移量按 Python 字符而非 UTF-8 字节计数；EOF
    的两个偏移量均等于完整 SQL 字符串长度。
    """

    type: TokenType
    lexeme: str
    position: SourcePosition
    start_offset: int
    end_offset: int


# 本映射是 lexer 识别保留字的唯一数据来源。键统一为大写，因为 SQL
# 关键字大小写不敏感；lexer 应在比较时使用 identifier.upper()，但普通
# 标识符本身仍需在进入 AST 时由 parser/lexer 统一转为小写。
KEYWORDS: dict[str, TokenType] = {
    "CREATE": TokenType.KW_CREATE,
    "DATABASE": TokenType.KW_DATABASE,
    "DROP": TokenType.KW_DROP,
    "USE": TokenType.KW_USE,
    "TABLE": TokenType.KW_TABLE,
    "INSERT": TokenType.KW_INSERT,
    "INTO": TokenType.KW_INTO,
    "VALUES": TokenType.KW_VALUES,
    "SELECT": TokenType.KW_SELECT,
    "FROM": TokenType.KW_FROM,
    "WHERE": TokenType.KW_WHERE,
    "UPDATE": TokenType.KW_UPDATE,
    "SET": TokenType.KW_SET,
    "DELETE": TokenType.KW_DELETE,
    # INDEX 的键保持大写；lexer 会先对用户输入执行 upper()，
    # 因此 INDEX、index 与 Index 都会稳定生成同一个专用 Token。
    "INDEX": TokenType.KW_INDEX,
    "AND": TokenType.KW_AND,
    "OR": TokenType.KW_OR,
    "NOT": TokenType.KW_NOT,
    "AS": TokenType.KW_AS,
    "INNER": TokenType.KW_INNER,
    "JOIN": TokenType.KW_JOIN,
    "ON": TokenType.KW_ON,
    "TRUE": TokenType.KW_TRUE,
    "FALSE": TokenType.KW_FALSE,
    "INT": TokenType.KW_INT,
    "TEXT": TokenType.KW_TEXT,
    "REAL": TokenType.KW_REAL,
    "BOOLEAN": TokenType.KW_BOOLEAN,
}
