"""模块 A 的语法分析器基础设施。

本文件已实现全部 V1.1 语句的语法层工作，并按 V2 计划逐步扩展。本阶段在
原有 Token 游标、标识符标准化、DDL、INSERT、SELECT、WHERE、UPDATE 和
DELETE 基础上，增加 BOOLEAN、Column/TableRef、表别名、INNER JOIN、通用
标量、逻辑表达式优先级以及带原文范围的多语句解析。Parser 的职责到构建
AST 为止，所有语义检查和执行均由 runner 与 storage 模块完成。

Parser 的输入是 lexer.tokenize() 生成、且以 EOF 结尾的 Token 序列。它的
输出最终会是 contracts.ast 中定义的 Statement。所有语法层的失败统一通过
contracts.errors.ParseError 报告，因此调用者可以稳定获得 E_SYNTAX 与行列号。

V3 在共享契约中新增 CreateIndexStmt 与 DropIndexStmt。本模块显式
导入这两种 AST 节点，使索引 DDL 分派与构建只依赖 contracts.ast
的公共类型。CREATE 二级分派已能区分 DATABASE、TABLE 与 INDEX，
并由独立方法将索引名、表名和列名组装成 CreateIndexStmt。DROP
二级分派同样能区分 DATABASE、TABLE 与 INDEX，并由专用方法读取索引名、
完成小写化后构建 DropIndexStmt。Parser
仍不判断索引、表或列是否真实存在，这些语义检查由 C/B 完成。
"""

from __future__ import annotations

from collections.abc import Sequence

from compiler.tokens import Token, TokenType
from contracts.ast import (
    And,
    Assignment,
    Cmp,
    Column,
    ColumnDef,
    CreateDatabaseStmt,
    CreateIndexStmt,
    CreateTableStmt,
    DropDatabaseStmt,
    DropIndexStmt,
    DropTableStmt,
    DeleteStmt,
    Expr,
    InsertStmt,
    JoinClause,
    JoinType,
    Literal,
    Not,
    Or,
    ParsedStatement,
    Script,
    SelectStmt,
    ScalarExpr,
    SourceSpan,
    SqlType,
    Statement,
    TableRef,
    UseDatabaseStmt,
    UpdateStmt,
    Value,
)
from contracts.errors import ParseError


# 此映射定义 CREATE TABLE 中类型关键字与 AST SqlType 枚举的固定对应关系。
# Parser 使用它完成语法层转换；值是否匹配列类型仍属于 C/B 的职责。
_SQL_TYPE_TOKENS: dict[TokenType, SqlType] = {
    TokenType.KW_INT: SqlType.INT,
    TokenType.KW_TEXT: SqlType.TEXT,
    TokenType.KW_REAL: SqlType.REAL,
    TokenType.KW_BOOLEAN: SqlType.BOOLEAN,
}


# 此元组集中列出 SQL 字面量 Token，供 parse_value 与通用标量解析共同使用。
# 保持单一数据来源可以避免新增值类型时两个解析入口支持范围不一致。
_VALUE_TOKEN_TYPES: tuple[TokenType, ...] = (
    TokenType.INTEGER_LITERAL,
    TokenType.REAL_LITERAL,
    TokenType.STRING_LITERAL,
    TokenType.KW_TRUE,
    TokenType.KW_FALSE,
)


# 此映射定义 WHERE 中比较运算符 Token 与 AST Cmp.op 字符串的固定对应关系。
# 只包含契约允许的六种运算符；运算结果的真假判断由 C 在执行时完成。
_COMPARISON_OPERATORS: dict[TokenType, str] = {
    TokenType.EQ: "=",
    TokenType.NE: "<>",
    TokenType.LT: "<",
    TokenType.LE: "<=",
    TokenType.GT: ">",
    TokenType.GE: ">=",
}


# 此集合列出 V2 不支持的 JOIN 修饰词，防止隐式别名逻辑误吞 LEFT 等单词。
# 它们后接 JOIN 时会留在 Token 流中，并最终由单语句完整性检查报告 E_SYNTAX。
_UNSUPPORTED_JOIN_MODIFIERS = frozenset(
    {"left", "right", "full", "cross", "natural"}
)


class Parser:
    """消费 Token 序列、维护当前游标并提供语法解析通用操作。

    Parser 不保存 SQL 原始字符串，也不扫描字符；其唯一输入是 Lexer 已生成的
    Token 序列。这种职责拆分使词法规则和文法规则彼此独立：lexer 负责把
    ``SELECT`` 识别为 ``KW_SELECT``，parser 负责判断它在当前位置是否符合
    文法要求。

    Args:
        tokens: 必须以 TokenType.EOF 结束的 Token 序列。Lexer 始终满足这个
            前提；若其他代码手动构造不完整序列，则视为 Parser 的使用错误。
    """

    # 此构造函数固定 Token 序列，并把读取游标放在第一个 Token 上。
    def __init__(self, tokens: Sequence[Token]) -> None:
        """初始化语法分析状态，并验证 Token 序列的基本完整性。

        将输入转为 tuple 可避免外部代码在解析过程中修改 Token 列表，导致游标位置与 Token 内容不一致。
        EOF 是 parser 判断输入结束的唯一哨兵，
        因此缺失 EOF 属于内部调用错误，而不是用户 SQL 的语法错误。

        Raises:
            ValueError: tokens 为空，或最后一个 Token 不是 EOF 时抛出。
        """
        self._tokens = tuple(tokens)
        if not self._tokens:
            raise ValueError("parser requires a non-empty token sequence")
        if self._tokens[-1].type is not TokenType.EOF:
            raise ValueError("parser token sequence must end with EOF")
        self._index = 0

    # 此公共方法查看当前或后续 Token，但不移动 parser 的读取游标。
    def peek(self, offset: int = 0) -> Token:
        """返回当前 Token 之后 offset 个位置的 Token，且不消费任何 Token。

        ``peek()`` 等同于 ``peek(0)``，常用于判断当前语句以哪个关键字开头；
        ``peek(1)`` 常用于区分 ``CREATE DATABASE`` 与 ``CREATE TABLE``。
        当请求位置超过 EOF 时，持续返回最后一个 EOF Token，使后续解析方法
        能将“输入意外结束”报告为 ParseError，而不是抛出数组越界异常。

        Args:
            offset: 相对于当前游标的非负偏移量。

        Raises:
            ValueError: offset 为负数时抛出，避免向后查看已消费的语法上下文。
        """
        if offset < 0:
            raise ValueError("peek offset must not be negative")
        target_index = self._index + offset
        if target_index >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[target_index]

    # 此公共方法消费当前 Token，并将读取游标移动到下一个 Token。
    def advance(self) -> Token:
        """返回当前 Token，并在非 EOF 时向前移动一个位置。

        EOF 不会被越过：即使重复调用 advance()，也持续返回最后一个 EOF Token。
        该设计使 parser 在处理不完整输入时保持稳定，所有错误仍可使用 EOF 的
        源码位置构造 ParseError。
        """
        token = self.peek()
        if token.type is not TokenType.EOF:
            self._index += 1
        return token

    # 此公共方法断言当前位置必须是指定类型，并在成功时消费该 Token。
    def expect(
        self,
        expected: TokenType | tuple[TokenType, ...],
        description: str | None = None,
    ) -> Token:
        """校验当前 Token 类型并消费它；不匹配时抛出带位置的 ParseError。

        解析具体文法时应优先使用本方法，例如 ``expect(TokenType.KW_FROM)``。
        它把“检查类型、生成错误信息、移动游标”统一封装，避免每个语句解析
        函数自行实现不同风格的错误处理。多个允许类型可通过 tuple 传入。

        Args:
            expected: 允许出现的一个 TokenType，或由多个 TokenType 组成的 tuple。
            description: 可选的人类可读说明；提供后将用于错误信息中的 expected
                部分，例如 ``"a column type"``。

        Returns:
            已经被消费的、类型符合要求的 Token。

        Raises:
            ParseError: 当前 Token 类型不在 expected 中时抛出，位置指向当前
                Token 的起始位置；若当前为 EOF，则指向输入结束位置。
            ValueError: expected 为空 tuple 时抛出，表示 Parser 内部调用错误。
        """
        expected_types = (expected,) if isinstance(expected, TokenType) else expected
        if not expected_types:
            raise ValueError("expected token types must not be empty")

        current = self.peek()
        if current.type in expected_types:
            return self.advance()

        expected_text = description or self._format_expected_types(expected_types)
        actual_text = self._format_actual_token(current)
        raise ParseError(
            current.position.line,
            current.position.column,
            f"expected {expected_text}, found {actual_text}",
        )

    # 此公共方法读取一个非保留字标识符，并转换为 AST 规定的小写名称。
    def parse_identifier(self) -> str:
        """消费一个 IDENTIFIER Token，并返回其小写形式。

        Lexer 已将保留字标记为 KW_* 类型，因此 ``parse_identifier`` 只接受普通 IDENTIFIER；
        这保证 CREATE TABLE select (...) 之类的输入不会把保留字错误地当成表名或列名。
        返回值统一小写，满足 AST 对 database、table、column 名称的冻结不变式。

        Returns:
            小写的标识符文本，例如 ``"Users"`` 返回 ``"users"``。

        Raises:
            ParseError: 当前 Token 不是普通标识符时抛出。
        """
        token = self.expect(TokenType.IDENTIFIER, "an identifier")
        return token.lexeme.lower()

    # 此公共方法读取一个 SQL 字面量，并转换成 contracts.ast.Value 原生值。
    def parse_value(self) -> Value:
        """消费数字、字符串或布尔 Token，并返回对应的 Python 原生值。

        AST 中禁止把数字保留为字符串，所以 ``"18"`` 必须转换为 int 18，
        ``"18.5"`` 必须转换为 float 18.5。字符串 Token 保留原始引号和转义，
        因此通过 ``_decode_string_literal`` 去除外层单引号，并把 ``''`` 恢复为
        单个单引号。V2 的 ``TRUE`` 和 ``FALSE`` 分别转换为严格的 Python
        ``True`` 与 ``False``。值是否符合某张表列的 SQL 类型不由 A 判断，
        属于 C/B。

        Returns:
            int、float、str 或 bool，符合 contracts.ast.Value 的类型别名。

        Raises:
            ParseError: 当前 Token 不是字面量时抛出。若手工构造了格式错误的
                STRING_LITERAL Token，也会在当前位置抛出。
        """
        token = self.peek()
        if token.type is TokenType.INTEGER_LITERAL:
            self.advance()
            return int(token.lexeme)
        if token.type is TokenType.REAL_LITERAL:
            self.advance()
            return float(token.lexeme)
        if token.type is TokenType.STRING_LITERAL:
            self.advance()
            return self._decode_string_literal(token)
        if token.type is TokenType.KW_TRUE:
            self.advance()
            return True
        if token.type is TokenType.KW_FALSE:
            self.advance()
            return False

        expected_text = self._format_expected_types(_VALUE_TOKEN_TYPES)
        raise ParseError(
            token.position.line,
            token.position.column,
            f"expected {expected_text}, found {self._format_actual_token(token)}",
        )

    # 此公共方法解析一条完整 SQL 输入，并保证末尾没有第二条语句或多余内容。
    def parse(self) -> Statement:
        """解析一条完整 SQL 语句，允许末尾至多存在一个分号。

        V3 继续保留 V1.1/V2 的单语句行为，并加入索引 DDL。本方法先解析语句主体，再通过
        ``_expect_single_statement_end`` 检查可选分号和 EOF，因此第二条语句
        与连续分号仍会报告 E_SYNTAX。

        Returns:
            当前已实现语句对应的 AST Statement。

        Raises:
            ParseError: 输入为空、语句类型不受支持、语法结构不完整，或语句后
                仍存在第二个分号、第二条语句或其他多余内容时抛出。
        """
        statement = self.parse_statement()
        self._expect_single_statement_end()
        return statement

    # 此内部方法落实 parse 的单语句结束规则，并拒绝第二条语句或多余分号。
    def _expect_single_statement_end(self) -> None:
        """消费至多一个结束分号，并强制要求下一个 Token 为 EOF。

        该函数将单语句结束规则集中封装：parse 只能处理一条语句，而
        parse_script 才能在分号后继续解析。多个连续分号不会被静默跳过。

        Raises:
            ParseError: 可选分号后仍存在另一条语句或其他 Token 时抛出。
        """
        if self.peek().type is TokenType.SEMICOLON:
            self.advance()
        self.expect(TokenType.EOF, "end of input after one SQL statement")

    # 此公共方法连续消费完整 Token 流，并为每条 SQL 构建带原文范围的结果。
    def parse_script(self, source: str) -> Script:
        """解析零条或多条分号分隔的 SQL，返回有序 ParsedStatement 元组。

        本方法直接在 Lexer 生成的同一 Token 流上循环调用 parse_statement，
        不对 source 使用字符串分割，因此字符串字面量中的分号仍属于原 Token。
        语句之间必须存在分号，只有最后一条语句可以省略分号。每条语句使用
        Token 的零基半开偏移截取原文，再转换为契约要求的一基闭区间 span。
        CREATE INDEX 与 DROP INDEX 不设置特殊分支，因此其原文和范围与所有
        V1/V2 语句遵循同一套全局定位规则。

        Args:
            source: 生成当前 Parser Token 序列的完整原始 SQL 文本。

        Returns:
            按源码顺序排列的 ParsedStatement 元组；空白输入返回空元组。

        Raises:
            ParseError: 语句本身非法、出现空语句，或相邻语句之间缺少分号时
                抛出；错误位置始终使用完整脚本的全局行列号。
            ValueError: source 长度与 Token 流记录的 EOF 偏移不一致时抛出，
                表示调用方没有传入生成该 Token 流的同一份源码。
        """
        eof_token = self._tokens[-1]
        if eof_token.end_offset != len(source):
            raise ValueError("parser source does not match token stream length")

        parsed_statements: list[ParsedStatement] = []
        while self.peek().type is not TokenType.EOF:
            start_token = self.peek()
            statement = self.parse_statement()
            end_token = self._tokens[self._index - 1]

            if self.peek().type is TokenType.SEMICOLON:
                end_token = self.advance()
            elif self.peek().type is not TokenType.EOF:
                current = self.peek()
                raise ParseError(
                    current.position.line,
                    current.position.column,
                    "expected ';' between SQL statements or end of input, "
                    f"found {self._format_actual_token(current)}",
                )

            parsed_statements.append(
                self._build_parsed_statement(
                    statement=statement,
                    source=source,
                    start_token=start_token,
                    end_token=end_token,
                )
            )

        return tuple(parsed_statements)

    # 此内部方法根据首尾 Token 同时生成语句原文和一基闭区间 SourceSpan。
    @staticmethod
    def _build_parsed_statement(
        statement: Statement,
        source: str,
        start_token: Token,
        end_token: Token,
    ) -> ParsedStatement:
        """将已解析 AST 与其连续源码范围组装为 ParsedStatement。

        Token 保存的是完整输入中的零基半开字符偏移和一基起始行列。本方法用
        ``source[start:end]`` 保留用户原始大小写及语句内部空白，再把末 Token
        的起始列加上 lexeme 长度减一，得到 SourceSpan 所需的一基闭区间终点。
        结束分号被消费时 end_token 就是分号，因此原文与 span 都自然包含它。

        Args:
            statement: parse_statement 已构造完成的 AST。
            source: 生成当前 Token 流的完整 SQL 脚本。
            start_token: 当前语句第一个非空白 Token。
            end_token: 当前语句最后一个 Token，可能是结束分号。

        Returns:
            AST、原始 SQL 和全局 SourceSpan 完全对应的 ParsedStatement。

        Raises:
            ValueError: 首尾 Token 范围无效，或 end_token 没有实际源码字符时
                抛出，表示 Parser 内部调用违反了本方法前置条件。
        """
        if (
            start_token.start_offset >= end_token.end_offset
            or not end_token.lexeme
        ):
            raise ValueError("parsed statement requires a non-empty token range")

        return ParsedStatement(
            statement=statement,
            sql=source[start_token.start_offset : end_token.end_offset],
            span=SourceSpan(
                start_line=start_token.position.line,
                start_col=start_token.position.column,
                end_line=end_token.position.line,
                end_col=end_token.position.column + len(end_token.lexeme) - 1,
            ),
        )

    # 此公共方法根据语句第一个关键字分派到相应的具体语句解析方法。
    def parse_statement(self) -> Statement:
        """解析当前游标处的一条 SQL 语句主体，但不处理末尾分号和 EOF。

        当前阶段已分派 V1.1 的全部九类语句：CREATE、DROP、USE、INSERT、
        SELECT、UPDATE、DELETE。CREATE 会在第二个关键字处继续区分
        DATABASE、TABLE 与 V3 INDEX；DROP 当前继续区分 DATABASE 与
        TABLE，其他关键字各有唯一的语句解析函数。

        Returns:
            已构造的任意 V1.1 Statement AST 节点。

        Raises:
            ParseError: 当前 Token 不是本阶段支持的语句起始关键字时抛出。
        """
        current = self.peek()
        if current.type is TokenType.KW_CREATE:
            return self._parse_create_statement()
        if current.type is TokenType.KW_DROP:
            return self._parse_drop_statement()
        if current.type is TokenType.KW_USE:
            return self._parse_use_database_statement()
        if current.type is TokenType.KW_INSERT:
            return self._parse_insert_statement()
        if current.type is TokenType.KW_SELECT:
            return self._parse_select_statement()
        if current.type is TokenType.KW_UPDATE:
            return self._parse_update_statement()
        if current.type is TokenType.KW_DELETE:
            return self._parse_delete_statement()

        raise ParseError(
            current.position.line,
            current.position.column,
            "expected CREATE, DROP, USE, INSERT, SELECT, UPDATE, or DELETE, "
            f"found {self._format_actual_token(current)}",
        )

    # 此内部方法在消费 CREATE 后，识别目标是数据库、表还是索引。
    def _parse_create_statement(
        self,
    ) -> CreateDatabaseStmt | CreateTableStmt | CreateIndexStmt:
        """解析 CREATE 二级关键字并分派到对应的专用解析方法。

        方法先消费 ``CREATE``，再查看下一个 Token：``DATABASE``、
        ``TABLE`` 和 ``INDEX`` 分别进入各自的专用函数。该方法只依据
        SQL 语法选择 AST 分支，不检查同名数据库、表或索引是否已经
        存在；这些状态相关检查必须由 C/B 完成。

        Returns:
            被选中的专用解析方法构建的 CreateDatabaseStmt、
            CreateTableStmt 或 CreateIndexStmt。

        Raises:
            ParseError: CREATE 后缺少 DATABASE/TABLE/INDEX，或选中分支的
                后续结构不符合相应文法时抛出。
        """
        self.expect(TokenType.KW_CREATE)
        target = self.peek()
        if target.type is TokenType.KW_DATABASE:
            return self._parse_create_database_statement()
        if target.type is TokenType.KW_TABLE:
            return self._parse_create_table_statement()
        if target.type is TokenType.KW_INDEX:
            return self._parse_create_index_statement()

        raise ParseError(
            target.position.line,
            target.position.column,
            "expected DATABASE, TABLE, or INDEX after CREATE, "
            f"found {self._format_actual_token(target)}",
        )

    # 此专用方法解析单列 CREATE INDEX 语句体并构建共享 AST。
    def _parse_create_index_statement(self) -> CreateIndexStmt:
        """解析 ``INDEX index_name ON table_name (column_name)`` 并返回 AST。

        调用本方法时，上层已经消费 ``CREATE``，当前 Token 必须是
        ``INDEX``。方法按固定文法顺序消费索引名、``ON``、表名、
        左括号、单个列名与右括号。三个名称都通过 ``parse_identifier``
        读取，因此会统一转换为小写，同时拒绝把 SQL 保留字当作名称。
        V3 只支持单列、非 UNIQUE 索引；索引是否重名、表和列是否存在
        属于语义层，不在本方法中访问 Catalog 或 Storage。

        Returns:
            完整、名称已小写化的 CreateIndexStmt。

        Raises:
            ParseError: 任一必需关键字、标识符或括号缺失，或出现
                不支持的多列结构时抛出；位置指向第一个不符合预期的 Token。
        """
        self.expect(TokenType.KW_INDEX)
        index_name = self.parse_identifier()
        self.expect(TokenType.KW_ON, "ON after CREATE INDEX name")
        table = self.parse_identifier()
        self.expect(TokenType.LPAREN, "'(' before CREATE INDEX column")
        column = self.parse_identifier()
        self.expect(TokenType.RPAREN, "')' after CREATE INDEX column")
        return CreateIndexStmt(
            index_name=index_name,
            table=table,
            column=column,
        )

    # 此内部方法在消费 DROP 后，识别目标是数据库、表还是索引。
    def _parse_drop_statement(
        self,
    ) -> DropDatabaseStmt | DropTableStmt | DropIndexStmt:
        """解析 DROP 二级关键字并分派到对应的专用解析方法。

        DROP 后的第二个关键字决定 AST 分支：DATABASE、TABLE 与 INDEX
        分别进入对应的专用解析方法。Parser 只验证语法形式，不能也不应判断
        数据库是否为 main、是否正在使用，或表和索引是否真实存在。

        Returns:
            专用解析方法构建的 DropDatabaseStmt、DropTableStmt 或
            DropIndexStmt。

        Raises:
            ParseError: DROP 后缺少 DATABASE/TABLE/INDEX，或选中分支的
                后续结构不符合相应文法时抛出。
        """
        self.expect(TokenType.KW_DROP)
        target = self.peek()
        if target.type is TokenType.KW_DATABASE:
            return self._parse_drop_database_statement()
        if target.type is TokenType.KW_TABLE:
            return self._parse_drop_table_statement()
        if target.type is TokenType.KW_INDEX:
            return self._parse_drop_index_statement()

        raise ParseError(
            target.position.line,
            target.position.column,
            "expected DATABASE, TABLE, or INDEX after DROP, "
            f"found {self._format_actual_token(target)}",
        )

    # 此专用方法读取 DROP INDEX 的索引名并构建共享 AST。
    def _parse_drop_index_statement(self) -> DropIndexStmt:
        """解析 INDEX index_name，并返回索引名已标准化的 DropIndexStmt。

        调用本方法时，上层已经消费 DROP，当前 Token 必须是 INDEX。方法先
        消费该关键字，再通过 parse_identifier 读取唯一需要的索引名；该公共
        标识符入口会统一转换为小写，并拒绝 SQL 保留字。V3 规定索引名在同一
        数据库中唯一，因此 DROP INDEX 不需要表名或列名。索引是否真实存在
        属于 B 的权威状态，由存储边界返回 E_INDEX_NOT_FOUND，A 不访问
        Catalog 或 Storage。

        Returns:
            仅包含小写索引名的 DropIndexStmt。

        Raises:
            ParseError: INDEX 后缺少普通标识符，或出现保留字等不合法名称时
                抛出；错误位置指向第一个不符合预期的 Token。
        """
        self.expect(TokenType.KW_INDEX)
        return DropIndexStmt(index_name=self.parse_identifier())

    # 此内部方法解析 CREATE DATABASE 的名称，并直接构建其 AST 节点。
    def _parse_create_database_statement(self) -> CreateDatabaseStmt:
        """解析已消费 CREATE 后的 ``DATABASE id``，返回 CreateDatabaseStmt。

        ``parse_identifier`` 统一将库名转小写，因此 ``CREATE DATABASE Shop``
        产出的 AST 为 ``CreateDatabaseStmt(name=\"shop\")``。重名库的
        E_DATABASE_EXISTS 不是词法或语法错误，必须留给 DatabaseServer。
        """
        self.expect(TokenType.KW_DATABASE)
        return CreateDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 DROP DATABASE 的名称，并直接构建其 AST 节点。
    def _parse_drop_database_statement(self) -> DropDatabaseStmt:
        """解析已消费 DROP 后的 ``DATABASE id``，返回 DropDatabaseStmt。

        例如 ``DROP DATABASE Shop`` 会产生 name 为 ``\"shop\"`` 的 AST。
        默认库 main 能否删除、当前库能否删除及库是否存在均属于运行/存储层，
        因此本方法只消费语法所需的关键字和标识符。
        """
        self.expect(TokenType.KW_DATABASE)
        return DropDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 USE id；正式文法中 USE 后不能再出现 DATABASE 关键字。
    def _parse_use_database_statement(self) -> UseDatabaseStmt:
        """解析 ``USE id``，返回 UseDatabaseStmt。

        V1.1 的正式文法和 golden 用例规定形式是 ``USE shop``，而不是
        ``USE DATABASE shop``。因此本方法在 USE 后直接调用 parse_identifier；
        若遇到保留字 DATABASE，会正确报告 E_SYNTAX。
        """
        self.expect(TokenType.KW_USE)
        return UseDatabaseStmt(name=self.parse_identifier())

    # 此内部方法解析 CREATE TABLE 的表名和一组有序列定义。
    def _parse_create_table_statement(self) -> CreateTableStmt:
        """解析 ``TABLE id '(' colDef (',' colDef)* ')'``，返回 CreateTableStmt。

        列定义必须至少有一项，并按 SQL 书写顺序保存在 AST 中；这个顺序将来
        决定 B 的物理存储列顺序和 SELECT * 的展开顺序。A 不检查列名是否重复：
        g18 要求重复列在 B.create_table 边界以 E_DUP_COLUMN 报告。
        """
        self.expect(TokenType.KW_TABLE)
        table_name = self.parse_identifier()
        columns = self._parse_column_definitions()
        return CreateTableStmt(table=table_name, columns=columns)

    # 此内部方法解析 DROP TABLE 的表名，并直接构建其 AST 节点。
    def _parse_drop_table_statement(self) -> DropTableStmt:
        """解析已消费 DROP 后的 ``TABLE id``，返回 DropTableStmt。

        本方法只负责将表名标准化并放入 AST；表是否存在、是否需要删除文件等
        运行期问题不属于 A。``DROP TABLE users`` 的表名始终输出为 ``\"users\"``。
        """
        self.expect(TokenType.KW_TABLE)
        return DropTableStmt(table=self.parse_identifier())

    # 此内部方法解析 INSERT INTO ... VALUES (...)，并构建 InsertStmt 节点。
    def _parse_insert_statement(self) -> InsertStmt:
        """解析 ``INSERT INTO id VALUES '(' value (',' value)* ')'``。

        INSERT 的 values 至少要有一个字面量；每个字面量通过 parse_value 转为
        int、float、str 或 bool，并按书写顺序放入不可变元组。A 不知道目标表
        的列数或列类型，因此“值数量不匹配”和“值类型不匹配”都不能在此处
        判断，必须把完整 InsertStmt 交给 C/B 处理。

        Returns:
            包含小写表名与原生 Python 值元组的 InsertStmt。

        Raises:
            ParseError: 缺少 INTO、表名、VALUES、括号、字面量、逗号，或使用
                非法值类型时抛出。
        """
        self.expect(TokenType.KW_INSERT)
        self.expect(TokenType.KW_INTO, "INTO after INSERT")
        table_name = self.parse_identifier()
        self.expect(TokenType.KW_VALUES, "VALUES after INSERT table name")
        values = self._parse_insert_values()
        return InsertStmt(table=table_name, values=values)

    # 此内部方法解析 INSERT 的圆括号值列表，并保证列表至少包含一个字面量。
    def _parse_insert_values(self) -> tuple[Value, ...]:
        """解析 ``'(' value (',' value)* ')'``，返回按输入顺序排列的值元组。

        文法中 value 没有方括号，因此空列表 ``VALUES ()`` 不合法。本方法在
        左括号后立刻调用 parse_value，从而在右括号位置得到准确的语法错误。
        逗号后也必须再出现一个 value，因此尾逗号同样被拒绝。

        Returns:
            至少包含一个 int、float、str 或 bool 的 tuple。
        """
        self.expect(TokenType.LPAREN, "'(' after VALUES")
        values = [self.parse_value()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            values.append(self.parse_value())

        self.expect(TokenType.RPAREN, "')' after INSERT values")
        return tuple(values)

    # 此内部方法解析 SELECT 的投影、FROM、JOIN 和可选 WHERE，并构建 AST。
    def _parse_select_statement(self) -> SelectStmt:
        """解析 ``SELECT ... FROM tableRef { joinClause } [WHERE expr]``。

        SELECT 的投影列表可以是 ``*``，也可以是一个或多个普通/限定列引用；
        显式列按原书写顺序保留，并允许重复。FROM 由 TableRef 保存表名及
        可选别名；随后按源码顺序保存零个或多个 INNER JOIN。WHERE 存在时
        构建完整 Expr。A 仅记录结构，不判断表列是否存在或 ON 的结果类型。

        Returns:
            包含 Column、FROM TableRef、有序 JoinClause 和可选 WHERE 的节点。

        Raises:
            ParseError: SELECT、FROM、JOIN、ON 或 WHERE 结构不完整时抛出。
        """
        self.expect(TokenType.KW_SELECT)
        columns = self._parse_select_list()
        self.expect(TokenType.KW_FROM, "FROM after SELECT list")
        table = self._parse_table_reference()
        joins = self._parse_join_clauses()
        where = self._parse_optional_where()
        return SelectStmt(
            columns=columns,
            table=table,
            where=where,
            joins=joins,
        )

    # 此内部方法解析 FROM/JOIN 的表名及可选别名，并构建 V2 TableRef 节点。
    def _parse_table_reference(self) -> TableRef:
        """解析 ``id [AS id | id]``，返回名称均已小写的 TableRef。

        首先读取必需的基础表名。其后若出现 AS，则强制读取一个显式别名；
        若直接出现普通 IDENTIFIER，则将其作为隐式别名。WHERE 等保留字、
        分号或 EOF 不会被误识别为别名。表名和别名都通过 parse_identifier
        统一转为小写；别名是否重复、表是否存在仍由 C 负责检查。

        Returns:
            名称已规范化，并带有可选 alias 的 TableRef。

        Raises:
            ParseError: 缺少表名，或 AS 后缺少普通标识符别名时抛出。
        """
        table_name = self.parse_identifier()
        alias: str | None = None

        if self.peek().type is TokenType.KW_AS:
            self.advance()
            alias = self.parse_identifier()
        elif self.peek().type is TokenType.IDENTIFIER:
            possible_alias = self.peek()
            is_unsupported_join = (
                possible_alias.lexeme.lower() in _UNSUPPORTED_JOIN_MODIFIERS
                and self.peek(1).type is TokenType.KW_JOIN
            )
            if not is_unsupported_join:
                alias = self.parse_identifier()

        return TableRef(name=table_name, alias=alias)

    # 此内部方法连续解析 SELECT 中零个或多个 JOIN，并保持源码书写顺序。
    def _parse_join_clauses(self) -> tuple[JoinClause, ...]:
        """解析 ``{ [INNER] JOIN tableRef ON expr }``，返回有序 JOIN 元组。

        当当前 Token 不是 INNER 或 JOIN 时返回空元组；否则重复调用单项 JOIN
        解析函数。列表按 SQL 从左到右追加，C 可据此按左结合语义构建逻辑计划。

        Returns:
            按源码顺序排列的 JoinClause 元组；没有 JOIN 时返回空元组。

        Raises:
            ParseError: 任意一项 JOIN 的表引用、ON 或表达式不完整时抛出。
        """
        joins: list[JoinClause] = []
        while self.peek().type in (TokenType.KW_INNER, TokenType.KW_JOIN):
            joins.append(self._parse_join_clause())
        return tuple(joins)

    # 此内部方法解析一项可省略 INNER 的连接子句，并构建 JoinClause AST。
    def _parse_join_clause(self) -> JoinClause:
        """解析 ``[INNER] JOIN tableRef ON expr``，返回 INNER JoinClause。

        显式 INNER 出现时必须紧跟 JOIN；省略 INNER 时直接以 JOIN 开始。右表
        复用表引用解析，因此支持 AS 和隐式别名；ON 条件复用完整表达式入口，
        因此支持括号、比较、NOT、AND 与 OR。V2 的连接类型固定为 INNER。

        Returns:
            包含右侧 TableRef、ON Expr 和 JoinType.INNER 的 JoinClause。

        Raises:
            ParseError: INNER 后缺少 JOIN、缺少右表、ON 或连接条件时抛出。
        """
        if self.peek().type is TokenType.KW_INNER:
            self.advance()
            self.expect(TokenType.KW_JOIN, "JOIN after INNER")
        else:
            self.expect(TokenType.KW_JOIN)

        right_table = self._parse_table_reference()
        self.expect(TokenType.KW_ON, "ON after JOIN table reference")
        condition = self._parse_expression()
        return JoinClause(
            right=right_table,
            on=condition,
            kind=JoinType.INNER,
        )

    # 此内部方法解析 SELECT 的星号或显式列引用列表，并保留其书写顺序。
    def _parse_select_list(self) -> tuple[Column, ...] | None:
        """解析 ``'*'`` 或 ``columnRef (',' columnRef)*``，返回投影列。

        AST 契约约定 SELECT * 必须用 columns=None 表示，不能把星号保存为
        ``('*',)``。显式列列表允许普通列和 ``u.id`` 形式的限定列，也允许
        重复。A 只保留语法顺序和限定符，C 再执行名称绑定与投影。

        Returns:
            SELECT * 时返回 None；显式列列表时返回至少一个 Column 的 tuple。

        Raises:
            ParseError: SELECT 后没有 * 或标识符、逗号后缺少列名，或 * 后仍有
                逗号/额外列名时，会由本方法或后续 FROM 检查抛出。
        """
        if self.peek().type is TokenType.STAR:
            self.advance()
            return None

        columns = [self._parse_column_reference()]
        while self.peek().type is TokenType.COMMA:
            self.advance()
            columns.append(self._parse_column_reference())
        return tuple(columns)

    # 此内部方法解析普通列或“限定符.列名”，并构建标准 Column AST 节点。
    def _parse_column_reference(self) -> Column:
        """解析 ``id`` 或 ``id '.' id``，返回名称均已小写的 Column。

        第一个标识符后没有点号时，它就是列名，qualifier 为 None；存在点号时，
        第一个标识符成为表名或别名限定符，点号后的第二个标识符成为列名。
        A 不验证限定符和列是否存在，也不判断未限定列是否存在歧义。

        Returns:
            一个普通列或带 qualifier 的不可变 Column 节点。

        Raises:
            ParseError: 缺少首个标识符，或点号后缺少列名时抛出。
        """
        first_identifier = self.parse_identifier()
        if self.peek().type is not TokenType.DOT:
            return Column(name=first_identifier)

        self.advance()
        column_name = self.parse_identifier()
        return Column(name=column_name, qualifier=first_identifier)

    # 此内部方法把列引用或 SQL 字面量统一解析为 ScalarExpr 操作数。
    def _parse_scalar(self) -> ScalarExpr:
        """解析 ``columnRef`` 或 ``value``，返回 Column 或 Literal。

        IDENTIFIER 开头的输入交给列引用解析；数字、字符串和布尔 Token 交给
        parse_value 转换后包装为 Literal。该方法只区分语法形状，不做列绑定、
        类型推导或布尔上下文检查，这些均由 C 在获得 AST 后完成。

        Returns:
            Column 或 Literal 类型的通用标量操作数。

        Raises:
            ParseError: 当前 Token 既不是列引用开头，也不是受支持字面量时抛出。
        """
        token = self.peek()
        if token.type is TokenType.IDENTIFIER:
            return self._parse_column_reference()
        if token.type in _VALUE_TOKEN_TYPES:
            return Literal(value=self.parse_value())

        raise ParseError(
            token.position.line,
            token.position.column,
            "expected a column reference or literal, "
            f"found {self._format_actual_token(token)}",
        )

    # 此内部方法识别可选 WHERE，并在存在时把条件子句交给完整表达式解析。
    def _parse_optional_where(self) -> Expr | None:
        """解析可选的 ``WHERE cond``；没有 WHERE 时返回 None。

        SELECT、UPDATE、DELETE 共用同一套 WHERE 文法，因此将可选入口集中在
        一个方法中，保证三类语句产生相同的表达式 AST。A 不执行条件判断，
        只记录 Column、Literal、Cmp、Not、And 和 Or 的嵌套关系并交给 C。

        Returns:
            不存在 WHERE 时返回 None；存在时返回完整 Expr 表达式树。
        """
        if self.peek().type is not TokenType.KW_WHERE:
            return None
        self.advance()
        return self._parse_expression()

    # 此内部方法作为表达式统一入口，从最低优先级的 OR 层开始递归下降。
    def _parse_expression(self) -> Expr:
        """解析一个完整 V2 表达式，并返回其优先级结构对应的 AST。

        入口选择 OR 层，是因为递归下降解析器应从最低优先级开始：OR 的每个
        操作数先交给更高优先级的 AND、NOT、predicate 和比较层完成。这样 AST
        的嵌套结构天然表达 ``括号 > 比较 > NOT > AND > OR``，无需后期重排。

        Returns:
            Column、Literal、Cmp、Not、And 或 Or 组成的完整 Expr。

        Raises:
            ParseError: 当前 Token 不能作为表达式开头，或子表达式不完整时抛出。
        """
        return self._parse_or_expression()

    # 此内部方法解析最低优先级 OR，并把连续 OR 构造成左结合表达式树。
    def _parse_or_expression(self) -> Expr:
        """解析 ``andExpr { OR andExpr }``，返回左结合的 Or AST。

        每个 OR 操作数先由 _parse_and_expression 完整解析，因此 AND 自动比 OR
        结合得更紧。例如 ``a OR b AND c`` 会生成 ``Or(a, And(b, c))``。

        Returns:
            没有 OR 时返回单个高优先级表达式；否则返回左结合 Or 树。

        Raises:
            ParseError: OR 后缺少合法的 AND 层表达式时抛出。
        """
        expression = self._parse_and_expression()

        while self.peek().type is TokenType.KW_OR:
            self.advance()
            expression = Or(
                left=expression,
                right=self._parse_and_expression(),
            )

        return expression

    # 此内部方法解析 AND 优先级层，并把连续 AND 构造成左结合表达式树。
    def _parse_and_expression(self) -> Expr:
        """解析 ``notExpr { AND notExpr }``，返回左结合的 And AST。

        每个 AND 操作数先解析 NOT 层，因此 NOT 比 AND 优先；本方法的结果又
        作为 OR 的操作数，因此 AND 同时比 OR 优先。连续 AND 按源码顺序左结合。

        Returns:
            没有 AND 时返回单个 NOT 层表达式；否则返回左结合 And 树。

        Raises:
            ParseError: AND 后缺少合法的 NOT 层表达式时抛出。
        """
        expression = self._parse_not_expression()

        while self.peek().type is TokenType.KW_AND:
            self.advance()
            expression = And(
                left=expression,
                right=self._parse_not_expression(),
            )

        return expression

    # 此内部方法递归解析一元 NOT，使其优先于 AND/OR 并允许连续取反。
    def _parse_not_expression(self) -> Expr:
        """解析 ``NOT notExpr`` 或 predicate，返回 Not 或更高优先级表达式。

        遇到 NOT 时递归调用自身，因此 ``NOT NOT active`` 会生成嵌套 Not；
        没有 NOT 时进入 predicate 层。predicate 会先完成比较，所以
        ``NOT age = 18`` 的结果是 ``Not(Cmp(...))`` 而不是比较一个 Not 节点。

        Returns:
            Not 节点，或未带 NOT 的 predicate 表达式。

        Raises:
            ParseError: NOT 后缺少另一个 NOT 或合法 predicate 时抛出。
        """
        if self.peek().type is TokenType.KW_NOT:
            self.advance()
            return Not(operand=self._parse_not_expression())
        return self._parse_predicate()

    # 此内部方法解析括号表达式，或解析标量及其可选比较后半段。
    def _parse_predicate(self) -> Expr:
        """解析 ``'(' expr ')'`` 或 ``scalar [comparisonOp scalar]``。

        括号内递归调用完整表达式入口，因此括号能够覆盖 AND、OR 等默认
        优先级；括号本身不生成 AST 节点。非括号分支允许比较两侧均为任意
        ScalarExpr，也允许裸列或裸字面量直接作为 Expr 交给 C 做类型检查。

        比较两侧都通过 _parse_scalar 读取，因此支持列与列、列与字面量、
        字面量与列以及字面量与字面量。比较运算符是可选的，所以裸列和裸
        字面量也能作为 Expr；它们是否适用于 WHERE 由 C 做类型检查。

        Returns:
            去除语法括号后的内部 Expr、一个 ScalarExpr，或一个 Cmp 节点。

        Raises:
            ParseError: 括号不闭合、缺少左侧标量，或比较后缺少右侧标量时抛出。
        """
        if self.peek().type is TokenType.LPAREN:
            self.advance()
            expression = self._parse_expression()
            self.expect(TokenType.RPAREN, "')' after parenthesized expression")
            return expression

        left = self._parse_scalar()
        if self.peek().type not in _COMPARISON_OPERATORS:
            return left

        operator = self._parse_comparison_operator()
        right = self._parse_scalar()
        return Cmp(left=left, op=operator, right=right)

    # 此内部方法将当前比较运算符 Token 转换为 AST Cmp.op 所需的字符串。
    def _parse_comparison_operator(self) -> str:
        """消费 ``= <> < <= > >=`` 中的一种比较运算符，返回其原 SQL 写法。

        Lexer 已经保证多字符运算符不会被拆开；本方法只需用映射验证它是否属于
        契约允许的六种类型。运算符的类型兼容性和实际比较结果由 C 负责。

        Returns:
            六种契约比较符之一。

        Raises:
            ParseError: 当前 Token 不是允许的比较运算符时抛出。
        """
        token = self.peek()
        operator = _COMPARISON_OPERATORS.get(token.type)
        if operator is None:
            raise ParseError(
                token.position.line,
                token.position.column,
                "expected =, <>, <, <=, >, or >=, "
                f"found {self._format_actual_token(token)}",
            )
        self.advance()
        return operator

    # 此内部方法解析 UPDATE 的表名、SET 赋值列表和可选 WHERE，并构建 UpdateStmt。
    def _parse_update_statement(self) -> UpdateStmt:
        """解析 ``UPDATE id SET assign (',' assign)* [WHERE cond]``。

        UPDATE 至少包含一项赋值；可选 WHERE 与 SELECT、DELETE 使用同一个
        表达式解析方法。A 只保留赋值的 SQL 书写顺序，不能也不应查找表、列或
        行。C 在执行前校验列和值，并落实“同列重复赋值时后者覆盖前者”。

        Returns:
            包含小写表名、赋值元组和可选 Expr 的 UpdateStmt。

        Raises:
            ParseError: 缺少表名、SET、赋值、逗号后的赋值，或 WHERE 文法错误
                时抛出。
        """
        self.expect(TokenType.KW_UPDATE)
        table_name = self.parse_identifier()
        self.expect(TokenType.KW_SET, "SET after UPDATE table name")
        assignments = self._parse_assignments()
        where = self._parse_optional_where()
        return UpdateStmt(table=table_name, assignments=assignments, where=where)

    # 此内部方法解析 UPDATE SET 后至少一项赋值和其逗号分隔关系。
    def _parse_assignments(self) -> tuple[Assignment, ...]:
        """解析 ``assign (',' assign)*``，返回按 SQL 顺序排列的 Assignment 元组。

        文法要求至少一项赋值，因此 UPDATE ... SET WHERE ... 会在 WHERE 处
        报错。重复列名不在 A 去重：例如 ``SET age=1, age=2`` 必须保留两项，
        由 C 按顺序执行“后者覆盖前者”的已冻结语义。

        Returns:
            至少包含一个 Assignment 的 tuple。
        """
        assignments = [self._parse_assignment()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            assignments.append(self._parse_assignment())

        return tuple(assignments)

    # 此内部方法解析一项“列名 = 字面量”赋值，并构建 Assignment AST 节点。
    def _parse_assignment(self) -> Assignment:
        """解析 ``id '=' value``，返回列名已小写、值已原生化的 Assignment。

        Assignment 使用单独的 ``=``，不接受 WHERE 比较中的 <、>= 等运算符。
        A 不检查目标列是否存在，也不检查值是否符合列类型；这些均依赖 C/B 的
        schema 和存储边界。

        Returns:
            一个 Assignment AST 节点。

        Raises:
            ParseError: 缺少列名、等号或字面量时抛出。
        """
        column_name = self.parse_identifier()
        self.expect(TokenType.EQ, "'=' in UPDATE assignment")
        return Assignment(column=column_name, value=self.parse_value())

    # 此内部方法解析 DELETE 的 FROM、表名和可选 WHERE，并构建 DeleteStmt。
    def _parse_delete_statement(self) -> DeleteStmt:
        """解析 ``DELETE FROM id [WHERE cond]``，返回 DeleteStmt。

        没有 WHERE 的 DELETE 在语法上合法，where 字段为 None，执行时由 C
        解释为删除当前表的全部行。带 WHERE 时只构建条件 AST；行扫描、匹配
        row_id 收集和逐行删除均属于 C 与 B 的协作边界。

        Returns:
            包含小写表名和可选 Expr 的 DeleteStmt。

        Raises:
            ParseError: 缺少 FROM、表名，或 WHERE 条件不符合 V1 文法时抛出。
        """
        self.expect(TokenType.KW_DELETE)
        self.expect(TokenType.KW_FROM, "FROM after DELETE")
        table_name = self.parse_identifier()
        where = self._parse_optional_where()
        return DeleteStmt(table=table_name, where=where)

    # 此内部方法解析 CREATE TABLE 圆括号中的至少一个列定义及其逗号分隔关系。
    def _parse_column_definitions(self) -> tuple[ColumnDef, ...]:
        """解析 ``'(' colDef (',' colDef)* ')'``，并返回不可变的列定义元组。

        此方法在读取左括号后立即要求一个列定义，因此空列列表 ``()`` 会在
        右括号处报语法错误。每逗号后必须再有一项列定义，故尾逗号
        ``(id INT,)`` 同样会报错。重复列名保持原样交给 B 校验。

        Returns:
            按输入顺序排列、至少包含一个 ColumnDef 的 tuple。
        """
        self.expect(TokenType.LPAREN, "'(' after table name")
        columns = [self._parse_column_definition()]

        while self.peek().type is TokenType.COMMA:
            self.advance()
            columns.append(self._parse_column_definition())

        self.expect(TokenType.RPAREN, "')' after column definitions")
        return tuple(columns)

    # 此内部方法解析单个“列名 + SQL 类型”结构，并构建 ColumnDef。
    def _parse_column_definition(self) -> ColumnDef:
        """解析 ``id type``，并返回一个 ColumnDef AST 节点。

        列名经 parse_identifier 统一转为小写，类型经 parse_sql_type 转为
        SqlType 枚举。该方法只验证 CREATE TABLE 的局部语法，不检查与前面
        列名是否重复，也不产生任何存储元数据。
        """
        column_name = self.parse_identifier()
        column_type = self._parse_sql_type()
        return ColumnDef(name=column_name, type=column_type)

    # 此内部方法把四种 SQL 类型关键字映射为 contracts.ast.SqlType。
    def _parse_sql_type(self) -> SqlType:
        """消费一个 SQL 类型关键字，并返回对应的 SqlType 枚举成员。

        V1.1 的 INT、TEXT、REAL 与 V2 的 BOOLEAN 都在固定映射中。遇到其他
        关键字、标识符或 EOF 时，方法使用当前 Token 的位置抛出 ParseError；
        这样 CREATE TABLE 的列类型错误能准确定位到错误位置。

        Returns:
            SqlType.INT、SqlType.TEXT、SqlType.REAL 或 SqlType.BOOLEAN。

        Raises:
            ParseError: 当前 Token 不是四种受支持类型关键字之一时抛出。
        """
        token = self.peek()
        sql_type = _SQL_TYPE_TOKENS.get(token.type)
        if sql_type is None:
            raise ParseError(
                token.position.line,
                token.position.column,
                "expected INT, TEXT, REAL, or BOOLEAN, "
                f"found {self._format_actual_token(token)}",
            )
        self.advance()
        return sql_type

    # 此内部方法将 lexer 已验证过的单引号字符串转换为最终的 Python str。
    def _decode_string_literal(self, token: Token) -> str:
        """移除字符串的外层单引号，并将 SQL 的 ``''`` 转义还原为 ``'``。

        Lexer 正常生成的 STRING_LITERAL 一定具有首尾单引号且不跨行。本方法仍
        做防御性检查，避免未来单元测试或其他模块手工传入错误 Token 时泄漏
        ValueError。此检查失败仍按语法错误报告，位置指向字符串开头。

        Args:
            token: 类型应为 STRING_LITERAL 的 Token。

        Returns:
            不含外层引号、已完成单引号反转义的 Python 字符串。

        Raises:
            ParseError: token 的文本不具有合法字符串外层引号时抛出。
        """
        lexeme = token.lexeme
        if len(lexeme) < 2 or not lexeme.startswith("'") or not lexeme.endswith("'"):
            raise ParseError(
                token.position.line,
                token.position.column,
                "invalid string literal token",
            )
        return lexeme[1:-1].replace("''", "'")

    # 此内部方法把一组 TokenType 格式化为适合错误消息阅读的文本。
    @staticmethod
    def _format_expected_types(expected_types: tuple[TokenType, ...]) -> str:
        """将允许的 Token 类型转换为错误消息中的 expected 描述。

        关键字显示为其 SQL 写法，例如 KW_FROM 显示为 ``FROM``；其他 Token
        显示为小写的类型名。多个候选项使用 ``or`` 连接，使报错能直接说明
        当前位置可接受哪些 token。
        """
        return " or ".join(Parser._format_token_type(token_type) for token_type in expected_types)

    # 此内部方法把实际遇到的 Token 格式化为适合错误消息阅读的文本。
    @staticmethod
    def _format_actual_token(token: Token) -> str:
        """生成错误消息中的 found 描述，并保留实际 token 文本。

        EOF 没有原始文本，因此显示为 ``end of input``。其他 Token 同时显示
        类型和 lexeme，例如 ``identifier 'users'``，便于用户定位错误原因。
        """
        if token.type is TokenType.EOF:
            return "end of input"
        return f"{Parser._format_token_type(token.type)} {token.lexeme!r}"

    # 此内部方法将 TokenType 转成 SQL 风格、稳定且易读的名称。
    @staticmethod
    def _format_token_type(token_type: TokenType) -> str:
        """将内部 TokenType 名称转换为面向用户的错误消息文本。

        KW_ 前缀的枚举值代表 SQL 保留字，应显示为大写 SQL 拼写；其余类型
        使用小写枚举名，例如 INTEGER_LITERAL 显示为 ``integer_literal``。
        该方法只负责展示，不影响 parser 的类型比较逻辑。
        """
        if token_type.name.startswith("KW_"):
            return token_type.name.removeprefix("KW_")
        return token_type.name.lower()


__all__ = ["Parser"]
