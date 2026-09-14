"""A 模块的 Lexer、Parser、AST 与 SourceSpan 旁路追踪器。

本模块使用现有编译器完成一次真实解析，同时把中间结果
转换为 ``TraceEvent`` 和 ``StageTrace``。它不修改 SQL 语义，不为了
画图重新解析 SQL，也不让 ``compiler`` 反向依赖 ``UI``。普通
``compiler.parse`` 和 ``compiler.parse_script`` 因此保持零额外开销。

Parser 追踪通过子类包装现有规则，记录真实的调用顺序、递归
深度、Token 游标变化、返回值和错误。AST 快照则按前序遍历生成。
遍历器面向所有 contracts.ast dataclass，因此 V3 的 CreateIndexStmt 与
DropIndexStmt 无需新增追踪契约即可自动产生真实节点、字段和路径。
现有契约只定义语句级 SourceSpan，所以本模块只给 AST 根节点关联
语句范围，不伪造子节点精确位置。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from functools import wraps
from time import perf_counter
from typing import Callable, Sequence, TypeVar, cast

from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.tokens import Token, TokenType
from contracts.ast import ParsedStatement, Script, SourceSpan, Statement
from contracts.errors import ParseError
from UI.trace_models import StageTrace, TraceEvent, TraceOwner, TraceStatus


_T = TypeVar("_T")

# 序号与 UI/README.md 的全链路表一致：1 留给 C 的 REPL 接收阶段，
# A 占用 2 至 5，之后 B/C 可从 6 继续追加绑定、计划、执行与存储。
_LEXER_SEQUENCE = 2
_PARSER_SEQUENCE = 3
_AST_SEQUENCE = 4
_SPAN_SEQUENCE = 5

# peek 不移动游标且调用极频繁，不记录可避免无效事件淹没规则。
_TRACED_METHODS = frozenset(
    {
        "advance",
        "expect",
        "parse",
        "parse_script",
        "parse_statement",
        "parse_identifier",
        "parse_value",
        "_expect_single_statement_end",
        "_build_parsed_statement",
    }
)


@dataclass(frozen=True, slots=True)
class _ParserRuleGuide:
    """使用适合教学界面的文字描述一条 Parser 规则。

    ``title`` 是界面显示的动作名，``purpose`` 说明具体职责，
    ``level`` 则区分主要语法节点与游标管理细节。该类仅保存
    展示元数据，不参与 Parser 分派，也不修改公共追踪契约。
    """

    title: str
    purpose: str
    level: str = "semantic"


# 每条已追踪 Parser 规则都拥有稳定、可读的中文说明。
# 映射放在适配器旁边便于答辩前审核教学文本，同时避免将
# 界面职责反向写入 ``compiler/parser.py``。
_PARSER_RULE_GUIDES: dict[str, _ParserRuleGuide] = {
    "parse": _ParserRuleGuide("解析单条 SQL", "消费一条完整 SQL，并拒绝结束位置之后的多余内容。"),
    "parse_script": _ParserRuleGuide("解析 SQL 脚本", "按顺序消费完整词法单元流，并保留每条语句的源码范围。"),
    "parse_statement": _ParserRuleGuide("判断语句类型", "根据开头关键字选择 CREATE、DROP、USE、INSERT、SELECT、UPDATE 或 DELETE 文法。"),
    "_expect_single_statement_end": _ParserRuleGuide("检查语句边界", "接受一个可选分号后要求到达 EOF，防止单语句入口隐藏额外 SQL。"),
    "_build_parsed_statement": _ParserRuleGuide("关联原文与源码范围", "把抽象语法树、SQL 原文片段及全局行列范围组合成 ParsedStatement。"),
    "_parse_create_statement": _ParserRuleGuide("分派 CREATE 语句", "区分 CREATE DATABASE、CREATE TABLE 与 CREATE INDEX。"),
    "_parse_create_database_statement": _ParserRuleGuide("构建 CREATE DATABASE", "读取数据库名称并生成 CreateDatabaseStmt 节点。"),
    "_parse_create_table_statement": _ParserRuleGuide("构建 CREATE TABLE", "读取表名与有序列定义并生成 CreateTableStmt 节点。"),
    "_parse_create_index_statement": _ParserRuleGuide("构建 CREATE INDEX", "读取索引名、目标表和单个索引列并生成 CreateIndexStmt 节点。"),
    "_parse_drop_statement": _ParserRuleGuide("分派 DROP 语句", "区分 DROP DATABASE、DROP TABLE 与 DROP INDEX。"),
    "_parse_drop_database_statement": _ParserRuleGuide("构建 DROP DATABASE", "读取数据库名称并生成 DropDatabaseStmt 节点。"),
    "_parse_drop_table_statement": _ParserRuleGuide("构建 DROP TABLE", "读取表名并生成 DropTableStmt 节点。"),
    "_parse_drop_index_statement": _ParserRuleGuide("构建 DROP INDEX", "读取索引名并生成 DropIndexStmt 节点。"),
    "_parse_use_database_statement": _ParserRuleGuide("构建 USE", "读取目标数据库名称并生成 UseDatabaseStmt 节点。"),
    "_parse_insert_statement": _ParserRuleGuide("构建 INSERT", "读取目标表与 VALUES 列表并生成 InsertStmt 节点。"),
    "_parse_insert_values": _ParserRuleGuide("读取插入值列表", "把 VALUES 中逗号分隔的 SQL 字面量转换为有序 Python 值。"),
    "_parse_select_statement": _ParserRuleGuide("构建 SELECT", "组合投影列、FROM 数据源、有序 JOIN 子句与可选 WHERE 表达式。"),
    "_parse_select_list": _ParserRuleGuide("解析投影列表", "识别 SELECT *，或按顺序解析普通列与限定列。"),
    "_parse_table_reference": _ParserRuleGuide("解析表引用", "读取表名和可选的显式或隐式别名，并统一规范化名称。"),
    "_parse_join_clauses": _ParserRuleGuide("收集 JOIN 子句", "按源码顺序读取连续的 INNER JOIN，直到不再出现 JOIN。"),
    "_parse_join_clause": _ParserRuleGuide("构建 INNER JOIN", "读取右表及 ON 条件并生成一个 JoinClause 节点。"),
    "_parse_optional_where": _ParserRuleGuide("检查 WHERE 子句", "存在 WHERE 时构建过滤表达式，否则保留为无过滤条件。"),
    "_parse_expression": _ParserRuleGuide("解析逻辑表达式", "进入优先级链，使括号、比较、NOT、AND、OR 形成正确的表达式树。"),
    "_parse_or_expression": _ParserRuleGuide("组合 OR 表达式", "把 AND 层表达式按从左到右组合为 OR 节点。"),
    "_parse_and_expression": _ParserRuleGuide("组合 AND 表达式", "先于 OR，把 NOT 层表达式按从左到右组合为 AND 节点。"),
    "_parse_not_expression": _ParserRuleGuide("处理 NOT", "让 NOT 优先作用于紧随其后的表达式，再交给 AND 与 OR。"),
    "_parse_predicate": _ParserRuleGuide("构建谓词", "解析括号表达式，或将两个通用操作数组合成比较表达式。"),
    "_parse_comparison_operator": _ParserRuleGuide("读取比较运算符", "接受 =、<>、<、<=、> 或 >= 并返回规范化运算符。"),
    "_parse_scalar": _ParserRuleGuide("解析比较操作数", "把当前位置的词法单元转换为 Column 或 Literal 操作数。"),
    "_parse_column_reference": _ParserRuleGuide("构建列引用", "读取“列名”或“限定符.列名”，并生成规范化 Column 节点。"),
    "_parse_update_statement": _ParserRuleGuide("构建 UPDATE", "读取目标表、赋值列表及可选过滤条件并生成 UpdateStmt。"),
    "_parse_assignments": _ParserRuleGuide("收集赋值项", "按源码顺序读取 SET 后逗号分隔的全部赋值。"),
    "_parse_assignment": _ParserRuleGuide("构建一项赋值", "读取“列名 = 字面量”并生成一个 Assignment 节点。"),
    "_parse_delete_statement": _ParserRuleGuide("构建 DELETE", "读取目标表与可选过滤条件并生成 DeleteStmt。"),
    "_parse_column_definitions": _ParserRuleGuide("收集列定义", "按声明顺序读取 CREATE TABLE 中的全部列定义。"),
    "_parse_column_definition": _ParserRuleGuide("构建一项列定义", "读取列名与 SQL 类型并生成 ColumnDef 节点。"),
    "_parse_sql_type": _ParserRuleGuide("解析 SQL 类型", "把 INT、TEXT、REAL 或 BOOLEAN 映射为对应 SqlType。"),
    "parse_value": _ParserRuleGuide("转换 SQL 字面量", "把整数、实数、字符串、TRUE 或 FALSE 原文转换为 Python 值。"),
    "parse_identifier": _ParserRuleGuide("读取并规范化标识符", "消费一个标识符，并在进入 AST 前转为小写。", "technical"),
    "expect": _ParserRuleGuide("校验预期词法单元", "确认当前词法单元属于语法规则期望的类别，然后消费它。", "technical"),
    "advance": _ParserRuleGuide("推进词法单元游标", "返回当前词法单元，并将语法分析器游标移动到下一个位置。", "technical"),
}


class CompilerTraceMode(str, Enum):
    """区分单语句兼容入口与多语句脚本入口。

    ``SINGLE`` 与 ``compiler.parse`` 一致，只接受一条 SQL；
    ``SCRIPT`` 与 ``compiler.parse_script`` 一致，直接消费完整 Token 流。
    """

    SINGLE = "single"
    SCRIPT = "script"


@dataclass(frozen=True, slots=True)
class CompilerTraceResult:
    """一次 A 模块编译的业务结果和可视化阶段。

    成功时 ``statements`` 保存带原文范围的结果；失败时 ``error``
    保存原始 ParseError，已完成阶段仍可查看，下游阶段标记 SKIPPED。
    C 可先发布追踪，再通过 ``raise_for_error`` 保持原有异常行为。
    """

    mode: CompilerTraceMode
    source: str
    statements: Script
    stages: tuple[StageTrace, ...]
    error: ParseError | None = None

    def __post_init__(self) -> None:
        """验证模式、结果类型、阶段顺序和错误状态相互一致。

        这些检查使矛盾数据在进入 TraceHub 前就暴露，前端不必再
        猜测“是否成功”或重新排序。
        """

        if not isinstance(self.mode, CompilerTraceMode):
            raise TypeError("mode must be CompilerTraceMode")
        if not isinstance(self.source, str):
            raise TypeError("source must be str")
        statements = tuple(self.statements)
        stages = tuple(self.stages)
        if any(not isinstance(item, ParsedStatement) for item in statements):
            raise TypeError("statements must contain ParsedStatement")
        if any(not isinstance(stage, StageTrace) for stage in stages):
            raise TypeError("stages must contain StageTrace")
        if any(a.sequence >= b.sequence for a, b in zip(stages, stages[1:])):
            raise ValueError("stages must be strictly ordered")
        single_result_is_invalid = (
            self.mode is CompilerTraceMode.SINGLE
            and self.error is None
            and len(statements) != 1
        )
        if single_result_is_invalid:
            raise ValueError("successful single parse requires one statement")
        if self.error is not None and not isinstance(self.error, ParseError):
            raise TypeError("error must be ParseError or None")
        has_failed_stage = any(stage.status is TraceStatus.FAILED for stage in stages)
        if (self.error is not None) != has_failed_stage:
            raise ValueError("error and failed stage must agree")
        object.__setattr__(self, "statements", statements)
        object.__setattr__(self, "stages", stages)

    @property
    def succeeded(self) -> bool:
        """返回 Lexer 和 Parser 是否都已成功。

        属性只读取已保存错误，不重新执行或遍历编译阶段。
        """

        return self.error is None

    def raise_for_error(self) -> None:
        """若编译失败，重新抛出本次产生的原始 ParseError。

        成功时本方法无副作用；失败时保留 E_SYNTAX、行列及消息。
        """

        if self.error is not None:
            raise self.error

    def require_statement(self) -> Statement:
        """取得成功 SINGLE 追踪中的唯一 AST Statement。

        Returns:
            单语句 Parser 实际构建的 AST。

        Raises:
            ParseError: 本次编译失败时抛出。
            ValueError: 结果来自 SCRIPT 模式时抛出。
        """

        self.raise_for_error()
        if self.mode is not CompilerTraceMode.SINGLE:
            raise ValueError("require_statement is only valid in single mode")
        return self.statements[0].statement


@dataclass(slots=True)
class _ParserEventDraft:
    """Parser 规则进入时占位、退出时补全的内部草稿。

    外层递归规则比内层规则更晚返回；若退出时才追加事件，
    界面就会倒放过程。草稿允许进入时确定序号，退出时再补结果。
    """

    sequence: int
    rule: str
    depth: int
    start_index: int
    started_at: float
    arguments: object
    end_index: int | None = None
    result: object = None
    elapsed_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None


def _parser_rule_guide(rule: str) -> _ParserRuleGuide:
    """返回已记录 Parser 方法的稳定教学元数据。

    当前语法规则均在上方显式列出。若后续新增 ``_parse_*`` 方法，
    回退逻辑会继续生成可读事件并指出尚未分类的规则，从而保持向前兼容。
    """

    guide = _PARSER_RULE_GUIDES.get(rule)
    if guide is not None:
        return guide
    readable = rule.removeprefix("_parse_").replace("_", " ")
    return _ParserRuleGuide(
        f"执行语法规则 {readable}",
        "消费当前语法片段并把解析结果交给上层规则。",
    )


def _token_at(tokens: Sequence[Token], index: int) -> Token:
    """安全返回 Parser 游标处的 Token，避免追踪界面产生越界异常。

    合法编译流始终包含 EOF。失败规则的最终游标可能已位于流边界，
    因此这里只对读取下标做防御性限制，不会改动 Parser 的真实游标。
    """

    if not tokens:
        raise ValueError("Parser tracing requires a non-empty Token stream")
    return tokens[min(max(index, 0), len(tokens) - 1)]


def _token_label(token: Token) -> str:
    """将一个 Token 格式化为简短且可对应源码的事件文本。

    EOF 没有原始词素；其他 Token 同时显示类别与用户实际书写内容，
    便于学生将步骤说明与 SQL 原文逐项对应。
    """

    if token.type is TokenType.EOF:
        return "EOF"
    return f'{token.type.name}({token.lexeme!r})'


def _consumed_token_summary(consumed: Sequence[Token]) -> str:
    """概括一条规则真实消费的 Token，避免事件卡片被长文本淹没。

    数量不超过四个时全部显示；更长的语法片段只展示前三个与总数，
    完整列表仍保留在可选开启的原始快照中。
    """

    visible = [token for token in consumed if token.type is not TokenType.EOF]
    if not visible:
        return "未消费新的源码词法单元"
    labels = [_token_label(token) for token in visible]
    if len(labels) <= 4:
        return "消费 " + " → ".join(labels)
    return "消费 " + " → ".join(labels[:3]) + f" …（共 {len(labels)} 个）"


def _result_summary(result: object) -> str | None:
    """从可序列化快照中提取简洁的语义结果。

    AST 数据类使用 ``node_type`` 与 ``fields`` 表示，序列显示项数，
    Parser 的原生结果显示值和类型。大型嵌套结构不写入常驻卡片。
    """

    if isinstance(result, dict) and isinstance(result.get("node_type"), str):
        return f"生成 {result['node_type']}"
    if isinstance(result, list):
        return f"得到 {len(result)} 项有序结果"
    if result is None:
        return None
    if isinstance(result, (str, bool, int, float)):
        return f"得到 {result!r}（{type(result).__name__}）"
    return None


def _parser_event_description(
    draft: _ParserEventDraft,
    guide: _ParserRuleGuide,
    tokens: Sequence[Token],
    consumed: Sequence[Token],
) -> str:
    """根据规则职责、Token 流与返回结果生成具体说明。

    成功文本同时说明语法职责与可观察产物；失败文本则标明出错 Token
    和编译错误。递归深度属于实现元数据，因此不占用主说明，仍可在
    ``metrics`` 和浏览器悬停提示中查看。
    """

    current = _token_at(tokens, draft.start_index)
    if draft.error_message is not None:
        code = draft.error_code or "E_SYNTAX"
        return (
            f"{guide.purpose}处理到 {_token_label(current)} 时失败；"
            f"{code}：{draft.error_message}"
        )

    consumed_text = _consumed_token_summary(consumed)
    if draft.rule == "advance":
        return f"取出 {_token_label(current)} 并把游标从 #{draft.start_index} 推进到 #{draft.end_index}。"
    if draft.rule == "expect":
        return f"{_token_label(current)} 符合当前文法期望，已校验并消费。"
    if draft.rule == "parse_identifier":
        return f"读取标识符 {current.lexeme!r}，规范化后得到 {draft.result!r}。"
    if draft.rule == "parse_value":
        return (
            f"将字面量 {current.lexeme!r} 转换为 Python 值 "
            f"{draft.result!r}（{type(draft.result).__name__}）。"
        )

    result_text = _result_summary(draft.result)
    suffix = f"；{result_text}" if result_text else ""
    return f"{guide.purpose}{consumed_text}{suffix}。"


class _TracingParser(Parser):
    """只用于观察入口、不复制任何 SQL 文法的 Parser 子类。

    该类动态包装基类的 parse 规则、expect 和 advance，因此可视化
    结果来自正式 Parser 的真实调用，而不是根据 AST 倒推的演示数据。
    """

    def __init__(self, tokens: list[Token]) -> None:
        """初始化 Parser Token 游标、调用草稿和递归深度。

        Args:
            tokens: 本次 Lexer 产生、以 EOF 结尾的完整 Token 列表。
        """

        super().__init__(tokens)
        self._trace_drafts: list[_ParserEventDraft] = []
        self._trace_depth = 0

    def __getattribute__(self, name: str) -> object:
        """访问属性时，只为指定 Parser 操作返回追踪包装器。

        ``_parse_`` 开头的规则自动追踪；显式集合补充公开规则与
        Token 消费操作。除此之外所有属性保持基类原行为。
        """

        attribute = super().__getattribute__(name)
        is_parser_operation = name in _TRACED_METHODS or name.startswith("_parse_")
        if not is_parser_operation or not callable(attribute):
            return attribute
        method = cast(Callable[..., object], attribute)

        @wraps(method)
        def traced_call(*args: object, **kwargs: object) -> object:
            """原样调用基类方法，同时委托记录器补全事件草稿。

            包装器不解释参数、不自行移动 Token 游标，也不吞掉异常；
            因此加入观察前后，基类 Parser 的返回值与错误语义保持一致。
            """

            recorder = cast(
                Callable[
                    [str, Callable[..., object], tuple[object, ...], dict[str, object]],
                    object,
                ],
                super(_TracingParser, self).__getattribute__("_record_rule_call"),
            )
            return recorder(name, method, args, kwargs)

        return traced_call

    def _record_rule_call(
        self,
        rule: str,
        method: Callable[..., _T],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> _T:
        """记录单次规则的入口顺序、游标、返回值、错误和耗时。

        Args:
            rule: Parser 方法名。
            method: 基类的原始绑定方法。
            args: 不含 self 的位置参数。
            kwargs: 关键字参数。

        Returns:
            原始方法的返回值。

        Raises:
            Exception: 原方法的异常被记录后原样向上传播。
        """

        drafts = cast(list[_ParserEventDraft], super().__getattribute__("_trace_drafts"))
        depth = cast(int, super().__getattribute__("_trace_depth"))
        start_index = cast(int, super().__getattribute__("_index"))
        draft = _ParserEventDraft(
            len(drafts) + 1,
            rule,
            depth,
            start_index,
            perf_counter(),
            {"args": _snapshot(args), "kwargs": _snapshot(kwargs)},
        )
        drafts.append(draft)
        super().__setattr__("_trace_depth", depth + 1)
        try:
            result = method(*args, **kwargs)
        except Exception as error:
            draft.error_code = getattr(error, "code", type(error).__name__)
            draft.error_message = getattr(error, "message", str(error))
            raise
        else:
            draft.result = _snapshot(result)
            return result
        finally:
            draft.end_index = cast(int, super().__getattribute__("_index"))
            draft.elapsed_ms = (perf_counter() - draft.started_at) * 1000
            super().__setattr__("_trace_depth", depth)

    def build_events(self) -> tuple[TraceEvent, ...]:
        """按规则进入顺序将草稿固化为不可变 TraceEvent。

        Returns:
            包含递归深度、前后游标及消费 Token 的事件元组。
        """

        drafts = cast(list[_ParserEventDraft], super().__getattribute__("_trace_drafts"))
        tokens = cast(tuple[Token, ...], super().__getattribute__("_tokens"))
        events: list[TraceEvent] = []
        for draft in drafts:
            end = draft.end_index if draft.end_index is not None else draft.start_index
            failed = draft.error_message is not None
            consumed = tokens[draft.start_index:end]
            guide = _parser_rule_guide(draft.rule)
            span = _token_range_span(tokens, draft.start_index, end)
            if failed and span is None and draft.start_index < len(tokens):
                span = _token_span(tokens[draft.start_index])
            events.append(
                TraceEvent(
                    event_id=f"parser.rule.{draft.sequence:04d}",
                    sequence=draft.sequence,
                    action=guide.title,
                    description=_parser_event_description(
                        draft,
                        guide,
                        tokens,
                        consumed,
                    ),
                    input_snapshot={
                        "rule": draft.rule,
                        "rule_title": guide.title,
                        "detail_level": guide.level,
                        "depth": draft.depth,
                        "cursor_before": draft.start_index,
                        "current_token": _token_snapshot(
                            _token_at(tokens, draft.start_index)
                        ),
                        "arguments": draft.arguments,
                    },
                    output_snapshot={
                        "status": "failed" if failed else "success",
                        "cursor_after": end,
                        "consumed_tokens": [_token_snapshot(token) for token in consumed],
                        "result": draft.result,
                        "error_code": draft.error_code,
                        "error_message": draft.error_message,
                    },
                    metrics={"depth": draft.depth, "consumed_token_count": len(consumed)},
                    source_span=span,
                    elapsed_ms=draft.elapsed_ms,
                )
            )
        return tuple(events)


def trace_parse(sql: str) -> CompilerTraceResult:
    """按 ``compiler.parse`` 的单语句规则编译并返回 A 追踪。

    函数不直接抛 ParseError，以便 C 先将失败过程放入 TraceHub。
    需要保持原 parse 行为时，调用结果的 ``require_statement``。
    """

    return _compile(sql, CompilerTraceMode.SINGLE)


def trace_parse_script(sql: str) -> CompilerTraceResult:
    """按 ``compiler.parse_script`` 规则编译完整脚本并返回 A 追踪。

    Lexer 只扫描一次，Parser 消费同一 Token 流，不使用
    ``split(';')``；多行 SourceSpan 因此始终是完整脚本的全局坐标。
    """

    return _compile(sql, CompilerTraceMode.SCRIPT)


def _compile(sql: str, mode: CompilerTraceMode) -> CompilerTraceResult:
    """在同一次编译中组装四个阶段，并在上游失败时跳过下游。

    Args:
        sql: SQL 原文。
        mode: 单语句或脚本模式。

    Returns:
        成功产物或 ParseError，以及四个状态明确的阶段。
    """

    if not isinstance(sql, str):
        raise TypeError("sql must be str")
    if not isinstance(mode, CompilerTraceMode):
        raise TypeError("mode must be CompilerTraceMode")

    started = perf_counter()
    try:
        tokens = Lexer(sql).tokenize()
    except ParseError as error:
        elapsed = (perf_counter() - started) * 1000
        stages = (
            _failed_lexer_stage(sql, error, elapsed),
            _skipped("a.parser", _PARSER_SEQUENCE, "Parser", "Lexer 失败，没有完整 Token 流。"),
            _skipped("a.ast", _AST_SEQUENCE, "AST", "Parser 未运行，无法构建 AST。"),
            _skipped("a.source_span", _SPAN_SEQUENCE, "SourceSpan", "Parser 未运行，无语句范围。"),
        )
        return CompilerTraceResult(mode, sql, (), stages, error)

    lexer_elapsed = (perf_counter() - started) * 1000
    lexer_stage = _lexer_stage(sql, tokens, lexer_elapsed)
    parser = _TracingParser(tokens)
    started = perf_counter()
    try:
        if mode is CompilerTraceMode.SCRIPT:
            statements = parser.parse_script(sql)
        else:
            statement = parser.parse()
            statements = (_single_parsed_statement(sql, tokens, statement),)
    except ParseError as error:
        elapsed = (perf_counter() - started) * 1000
        stages = (
            lexer_stage,
            _parser_stage(sql, tokens, parser.build_events(), elapsed, error, ()),
            _skipped("a.ast", _AST_SEQUENCE, "AST", "Parser 失败，不发布不完整 AST。"),
            _skipped("a.source_span", _SPAN_SEQUENCE, "SourceSpan", "Parser 失败，不发布范围。"),
        )
        return CompilerTraceResult(mode, sql, (), stages, error)

    elapsed = (perf_counter() - started) * 1000
    stages = (
        lexer_stage,
        _parser_stage(
            sql,
            tokens,
            parser.build_events(),
            elapsed,
            None,
            statements,
        ),
        _ast_stage(statements),
        _source_span_stage(sql, statements),
    )
    return CompilerTraceResult(mode, sql, statements, stages)


def _single_parsed_statement(
    source: str,
    tokens: list[Token],
    statement: Statement,
) -> ParsedStatement:
    """为成功 ``Parser.parse`` 结果补齐原文与语句级 SourceSpan。

    单语句完整性已由 Parser 验证，因此可用第一个与最后一个
    非 EOF Token 复用 ``ParsedStatement`` 的闭区间契约。
    """

    actual = [token for token in tokens if token.type is not TokenType.EOF]
    if not actual:
        raise ValueError("successful parse requires a source token")
    span = _token_range_span(tuple(actual), 0, len(actual))
    assert span is not None
    return ParsedStatement(
        statement,
        source[actual[0].start_offset:actual[-1].end_offset],
        span,
    )


_TOKEN_ROLES: dict[TokenType, str] = {
    TokenType.KW_CREATE: "开始一条对象创建语句",
    TokenType.KW_DROP: "开始一条对象删除语句",
    TokenType.KW_SELECT: "开始一条查询并引出投影列表",
    TokenType.KW_INSERT: "开始一条数据插入语句",
    TokenType.KW_UPDATE: "开始一条数据更新语句",
    TokenType.KW_DELETE: "开始一条数据删除语句",
    TokenType.KW_FROM: "引出 SELECT 的主表数据源",
    TokenType.KW_WHERE: "引出语句的过滤表达式",
    TokenType.KW_JOIN: "引出一个 INNER JOIN 数据源",
    TokenType.KW_ON: "引出 JOIN 的连接条件",
    TokenType.KW_NOT: "表示优先于 AND 和 OR 的逻辑非",
    TokenType.KW_AND: "连接两个优先于 OR 的布尔条件",
    TokenType.KW_OR: "连接两个最低优先级的布尔条件",
    TokenType.KW_TRUE: "表示将转换为 Python True 的布尔字面量",
    TokenType.KW_FALSE: "表示将转换为 Python False 的布尔字面量",
    TokenType.DOT: "分隔表限定符与列名，例如 u.id",
    TokenType.COMMA: "分隔同一语法列表中相邻的项",
    TokenType.LPAREN: "开始列表或提高内部表达式优先级",
    TokenType.RPAREN: "结束列表或括号表达式",
    TokenType.SEMICOLON: "标记当前 SQL 语句的显式结束",
    TokenType.STAR: "表示 SELECT 中选取表的全部列",
}


def _lexer_token_description(token: Token) -> str:
    """说明一个真实 Token 的词法类别及其对下游的作用。

    说明只依据 ``TokenType`` 与原始 lexeme 生成，同时回答“识别了什么”
    与“它为什么重要”；需要结合上下文的语法决策仍由 Parser 负责。
    """

    if token.type is TokenType.EOF:
        return "扫描到输入末尾，追加零长度 EOF 哨兵供语法分析器检查完整性。"
    role = _TOKEN_ROLES.get(token.type)
    if role is not None:
        return f"识别源码 {token.lexeme!r} 为 {token.type.name}：{role}。"
    if token.type is TokenType.IDENTIFIER:
        return f"识别 {token.lexeme!r} 为标识符；其表名、列名或别名职责由语法分析器根据位置确定。"
    if token.type in {
        TokenType.INTEGER_LITERAL,
        TokenType.REAL_LITERAL,
        TokenType.STRING_LITERAL,
    }:
        return f"识别 {token.lexeme!r} 为 {token.type.name}；语法分析器会将它转换为 Python 原生值。"
    if token.type.name.startswith("KW_"):
        return f"识别 {token.lexeme!r} 为 SQL 保留关键字 {token.type.name}，供语法分析器选择对应文法。"
    return f"识别 {token.lexeme!r} 为 {token.type.name}，供语法分析器检查语法结构。"


def _lexer_stage(sql: str, tokens: list[Token], elapsed_ms: float) -> StageTrace:
    """将成功 Lexer 输出转为逐 Token 可查看的阶段。

    普通 Token 保存精确行列和字符偏移；EOF 为零长度哨兵，不伪造
    非空 SourceSpan。现有 Lexer 只能提供整体耗时，所以真实总耗时
    保存在 StageTrace，不把平均值伪装成单个 Token 的实测耗时。
    """

    events = tuple(
        TraceEvent(
            f"lexer.token.{index:04d}",
            index,
            f"识别 {token.type.name}",
            _lexer_token_description(token),
            {
                "lexeme": token.lexeme,
                "start_offset": token.start_offset,
                "end_offset": token.end_offset,
            },
            {"token": _token_snapshot(token)},
            {"character_count": token.end_offset - token.start_offset},
            _token_span(token),
            0.0,
        )
        for index, token in enumerate(tokens, start=1)
    )
    return StageTrace(
        "a.lexer", _LEXER_SEQUENCE, TraceOwner.A, "Lexer",
        "从左到右扫描 SQL，生成带位置和偏移的 Token 流。",
        TraceStatus.SUCCESS,
        "SQL 原始文本", "按源码顺序排列、以 EOF 结束的 Token 流",
        {"sql": sql, "character_count": len(sql)}, events,
        {"token_count": len(tokens), "tokens": [_token_snapshot(token) for token in tokens]},
        {"token_count": len(tokens), "source_token_count": len(tokens) - 1},
        None, elapsed_ms,
    )


def _failed_lexer_stage(sql: str, error: ParseError, elapsed_ms: float) -> StageTrace:
    """构建一个保留精确错误坐标的 Lexer 失败阶段。

    Lexer 不公开抛错前的半成品列表，本适配器不进行第二次扫描，
    因此只记录真实可得的错误位置与原始消息。
    """

    span = SourceSpan(error.line, error.col, error.line, error.col)
    event = TraceEvent(
        "lexer.error.0001", 1, "Lexer 报告词法错误",
        "扫描在此位置终止，未产生完整 Token 流。",
        {"line": error.line, "column": error.col},
        {"error_code": error.code, "message": error.message},
        {}, span, elapsed_ms,
    )
    return StageTrace(
        "a.lexer", _LEXER_SEQUENCE, TraceOwner.A, "Lexer",
        "Lexer 扫描到无法组成 Token 的内容。", TraceStatus.FAILED,
        "str", "list[Token] ending with EOF", {"sql": sql}, (event,),
        {"token_stream_available": False}, {"character_count": len(sql)}, span,
        elapsed_ms, error.code, error.message,
    )


def _parser_stage(
    sql: str,
    tokens: list[Token],
    events: tuple[TraceEvent, ...],
    elapsed_ms: float,
    error: ParseError | None,
    statements: Script,
) -> StageTrace:
    """组装 Parser 的真实规则事件、输入 Token 与最终状态。

    Args:
        sql: 完整原文。
        tokens: Lexer 本次生成的 Token。
        events: 按进入顺序固化的规则调用。
        elapsed_ms: Parser 总耗时。
        error: 可选语法错误。
        statements: 成功时的语句结果。
    """

    span = (
        SourceSpan(error.line, error.col, error.line, error.col)
        if error
        else _statements_span(statements)
    )
    return StageTrace(
        "a.parser", _PARSER_SEQUENCE, TraceOwner.A, "Parser",
        "通过递归下降规则消费 Token，并按优先级构建语句结构。",
        TraceStatus.FAILED if error else TraceStatus.SUCCESS,
        "Lexer 生成的完整 Token 流", "单条 Statement 或带原文范围的 Script AST",
        {"sql": sql, "tokens": [_token_snapshot(token) for token in tokens]}, events,
        {
            "statement_count": len(statements),
            "statement_types": [
                type(item.statement).__name__ for item in statements
            ],
        },
        {"rule_call_count": len(events), "token_count": len(tokens)}, span, elapsed_ms,
        error.code if error else None, error.message if error else None,
    )


def _ast_stage(statements: Script) -> StageTrace:
    """将同一次 Parser 产生的 AST 转为树快照和前序节点事件。

    遍历逻辑只判断对象是否为 dataclass，不维护节点类型白名单，所以新增的
    CreateIndexStmt、DropIndexStmt 会与 V1/V2 节点走相同路径自动进入快照。
    根节点事件使用语句级 SourceSpan；子节点暂无契约位置，所以只展示稳定
    字段路径，不伪造范围。
    """

    started = perf_counter()
    events: list[TraceEvent] = []
    trees: list[object] = []
    for index, parsed in enumerate(statements, start=1):
        trees.append(_snapshot(parsed.statement))
        _walk_ast(
            parsed.statement,
            f"statements[{index - 1}].statement",
            index,
            parsed.span,
            events,
            True,
        )
    elapsed = (perf_counter() - started) * 1000
    return StageTrace(
        "a.ast", _AST_SEQUENCE, TraceOwner.A, "AST",
        "展示 Parser 实际构建的语句、索引、表、列和表达式节点树。",
        TraceStatus.SUCCESS, "Parser 生成的 Statement / Script", "可视化 AST 节点树",
        {"statement_count": len(statements)}, tuple(events),
        {"statement_count": len(statements), "trees": trees},
        {"statement_count": len(statements), "node_count": len(events)},
        _statements_span(statements), elapsed,
    )


def _ast_node_description(value: object, statement_index: int) -> str:
    """说明一个 AST 数据类节点的真实业务含义。

    文本使用节点的强类型字段，而不是只展示遍历路径。这样卡片能直接
    说明 Parser 构建了什么，完整路径与快照则继续供联动和原始数据检查使用。
    """

    kind = type(value).__name__
    if kind == "SelectStmt":
        columns = getattr(value, "columns", None)
        projection = "全部列" if columns is None else f"{len(columns)} 个投影列"
        table = getattr(getattr(value, "table", None), "name", "?")
        joins = len(getattr(value, "joins", ()))
        where = "包含 WHERE 条件" if getattr(value, "where", None) is not None else "无 WHERE 条件"
        return f"第 {statement_index} 条语句的查询根节点：从 {table} 读取{projection}，包含 {joins} 个 JOIN，{where}。"
    if kind == "TableRef":
        name = getattr(value, "name", "?")
        alias = getattr(value, "alias", None)
        return f"数据源表 {name}" + (f" 使用别名 {alias}。" if alias else " 未使用别名。")
    if kind == "Column":
        name = getattr(value, "name", "?")
        qualifier = getattr(value, "qualifier", None)
        qualified = f"{qualifier}.{name}" if qualifier else name
        return f"列引用 {qualified}；限定符用于 JOIN 或多表场景的名称绑定。"
    if kind == "Literal":
        literal = getattr(value, "value", None)
        return f"已将 SQL 字面量转换为 Python 值 {literal!r}（{type(literal).__name__}）。"
    if kind == "Cmp":
        return f"比较节点使用 {getattr(value, 'op', '?')} 运算符连接左右操作数。"
    if kind in {"And", "Or"}:
        return f"逻辑 {kind.upper()} 节点按语法分析优先级连接左右条件子树。"
    if kind == "Not":
        return "逻辑 NOT 节点优先对其操作数子树取反。"
    if kind == "JoinClause":
        right = getattr(getattr(value, "right", None), "name", "?")
        return f"INNER JOIN 节点将右表 {right} 与主表按 ON 表达式连接。"
    if kind == "CreateTableStmt":
        return f"建表语句根节点：创建表 {getattr(value, 'table', '?')}，定义 {len(getattr(value, 'columns', ()))} 列。"
    if kind == "ColumnDef":
        sql_type = getattr(getattr(value, "type", None), "value", "?")
        return f"列定义 {getattr(value, 'name', '?')} 使用 {sql_type} 类型。"
    if kind == "CreateIndexStmt":
        return (
            f"创建索引 {getattr(value, 'index_name', '?')}，目标为 "
            f"{getattr(value, 'table', '?')}.{getattr(value, 'column', '?')}。"
        )
    if kind == "DropIndexStmt":
        return f"删除索引 {getattr(value, 'index_name', '?')}。"
    if kind.endswith("Stmt"):
        return f"第 {statement_index} 条语句的 {kind} AST 根节点，保存后续绑定与执行所需字段。"
    return f"{kind} 保存语法分析器已确认的语法结构，并作为父节点的有类型子节点。"


def _walk_ast(
    value: object,
    path: str,
    statement_index: int,
    root_span: SourceSpan,
    events: list[TraceEvent],
    is_root: bool,
) -> None:
    """前序遍历 AST dataclass，为每个真实节点追加事件。

    标量和枚举作为父节点字段展示，不创建虚假节点；序列按原始下标递归。
    CREATE/DROP INDEX 的名称字段因此保留在根节点快照中，而不会被伪造成
    子 AST。函数只读取 frozen AST，不会修改业务结果或公共追踪契约。
    """

    if is_dataclass(value) and not isinstance(value, type):
        sequence = len(events) + 1
        events.append(
            TraceEvent(
                f"ast.node.{sequence:04d}", sequence, f"生成 {type(value).__name__} 节点",
                _ast_node_description(value, statement_index),
                {"path": path, "statement_index": statement_index},
                {"node": _snapshot(value)}, {"field_count": len(fields(value))},
                root_span if is_root else None,
            )
        )
        for item in fields(value):
            _walk_ast(
                getattr(value, item.name),
                f"{path}.{item.name}",
                statement_index,
                root_span,
                events,
                False,
            )
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _walk_ast(
                item,
                f"{path}[{index}]",
                statement_index,
                root_span,
                events,
                False,
            )


def _source_span_stage(sql: str, statements: Script) -> StageTrace:
    """直接展示 ParsedStatement 中的原文与全局一基闭区间。

    坐标来自本次 Parser 产物，不对 SQL 做分割或第二次位置计算。
    """

    started = perf_counter()
    events = tuple(
        TraceEvent(
            f"source-span.statement.{index:04d}", index, f"确定第 {index} 条语句范围",
            (
                f"该语句原文位于第 {parsed.span.start_line} 行第 "
                f"{parsed.span.start_col} 列至第 {parsed.span.end_line} 行第 "
                f"{parsed.span.end_col} 列，共 {len(parsed.sql)} 个字符；"
                "该范围用于错误定位与界面点击联动。"
            ),
            {"full_source_character_count": len(sql)},
            {
                "statement_index": index,
                "sql": parsed.sql,
                "span": _span_snapshot(parsed.span),
            },
            {"statement_character_count": len(parsed.sql)}, parsed.span,
        )
        for index, parsed in enumerate(statements, start=1)
    )
    elapsed = (perf_counter() - started) * 1000
    output = [
        {
            "statement_index": index,
            "sql": parsed.sql,
            "span": _span_snapshot(parsed.span),
        }
        for index, parsed in enumerate(statements, start=1)
    ]
    return StageTrace(
        "a.source_span", _SPAN_SEQUENCE, TraceOwner.A, "SourceSpan",
        "将每条 AST 映射回完整 SQL 脚本中的原文与全局行列。",
        TraceStatus.SUCCESS, "带原文的 ParsedStatement 列表", "每条语句的 SQL 原文与全局 SourceSpan",
        {"full_source": sql, "statement_count": len(statements)}, events,
        {"statements": output}, {"statement_count": len(statements)},
        _statements_span(statements), elapsed,
    )


def _skipped(stage_id: str, sequence: int, name: str, description: str) -> StageTrace:
    """构建一个因上游失败而没有运行的阶段。

    Args:
        stage_id: 稳定阶段 ID。
        sequence: 全链路排序值。
        name: 界面展示名称。
        description: 真实跳过原因。
    """

    return StageTrace(
        stage_id,
        sequence,
        TraceOwner.A,
        name,
        description,
        TraceStatus.SKIPPED,
    )


def _token_snapshot(token: Token) -> dict[str, object]:
    """把 Token 转换为与编译器对象解耦的 JSON 快照。

    快照同时保留 Token 类别、用户原始写法、一基行列与零基
    半开字符偏移，便于界面联动高亮 SQL 原文。
    """

    return {
        "type": token.type.name,
        "lexeme": token.lexeme,
        "line": token.position.line,
        "column": token.position.column,
        "start_offset": token.start_offset,
        "end_offset": token.end_offset,
    }


def _token_span(token: Token) -> SourceSpan | None:
    """将非空 Token 转为一基闭区间。

    当前契约不允许 Token 跨行，因此终止列可由 lexeme 长度
    精确计算。EOF 不占原文字符，一基闭区间无法表示其零长度，
    所以返回 ``None``。
    """

    if not token.lexeme:
        return None
    return SourceSpan(
        token.position.line,
        token.position.column,
        token.position.line,
        token.position.column + len(token.lexeme) - 1,
    )


def _token_range_span(
    tokens: tuple[Token, ...],
    start: int,
    end: int,
) -> SourceSpan | None:
    """将 Parser 实际消费的 Token 半开下标范围合并为闭区间。

    空范围和 EOF-only 范围返回 None，避免把未消费的后续 Token
    错误地标记为某个可选规则的源码。
    """

    consumed = [
        token
        for token in tokens[start:end]
        if token.type is not TokenType.EOF and token.lexeme
    ]
    if not consumed:
        return None
    last = _token_span(consumed[-1])
    assert last is not None
    return SourceSpan(
        consumed[0].position.line,
        consumed[0].position.column,
        last.end_line,
        last.end_col,
    )


def _span_snapshot(span: SourceSpan) -> dict[str, int]:
    """把 SourceSpan 转换为前端可直接消费的普通字典。

    字段名保留契约的起止语义，界面不需要导入 AST dataclass，
    也不需要猜测坐标是否从零开始。
    """

    return {
        "start_line": span.start_line,
        "start_col": span.start_col,
        "end_line": span.end_line,
        "end_col": span.end_col,
    }


def _statements_span(statements: Script) -> SourceSpan | None:
    """使用首条起点和末条终点合并脚本阶段总范围。

    Parser 已保证语句按源码顺序排列，因此不必重新扫描 SQL。
    空脚本不占据任何语句范围，返回 ``None``。
    """

    if not statements:
        return None
    return SourceSpan(
        statements[0].span.start_line,
        statements[0].span.start_col,
        statements[-1].span.end_line,
        statements[-1].span.end_col,
    )


def _snapshot(value: object) -> object:
    """递归把 AST、Token、枚举和序列转换为只含 JSON 兼容值的快照。

    dataclass 保留具体节点类型和字段名，不支持的内部对象最后
    使用 repr 展示，不将可调用或可变的业务对象泄漏给界面。
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Token):
        return _token_snapshot(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "node_type": type(value).__name__,
            "fields": {item.name: _snapshot(getattr(value, item.name)) for item in fields(value)},
        }
    if isinstance(value, dict):
        return {str(key): _snapshot(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_snapshot(item) for item in value]
    return repr(value)


__all__ = ["CompilerTraceMode", "CompilerTraceResult", "trace_parse", "trace_parse_script"]
