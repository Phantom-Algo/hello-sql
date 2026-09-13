"""HELLO-SQL 交互会话、命令调度与智能补全。

本模块只调用 Runner 公开会话能力，不访问 compiler 或 storage 的具体
实现。SQL 输入交给 Runner；``/inspect`` 仅向可选 inspector 请求最近
快照并渲染，绝不重新执行用户语句。
"""

from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import TYPE_CHECKING, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.sql import SqlLexer

from contracts.errors import E_BAD_ARG, SqlError
from contracts.result import QueryResult, ScriptResult
from runner.terminal.render import HELP_ITEMS, TerminalRenderer, safe_text
from UI.inspection import (
    InspectionModule,
    InspectionSnapshot,
    format_inspection_text,
)

if TYPE_CHECKING:
    from runner.runner import Runner


KEYWORDS = (
    "CREATE", "DATABASE", "DROP", "USE", "TABLE", "INSERT", "INTO", "VALUES",
    "SELECT", "FROM", "WHERE", "UPDATE", "SET", "DELETE", "AND", "OR", "NOT",
    "INNER", "JOIN", "ON", "AS", "INT", "TEXT", "REAL", "BOOLEAN", "TRUE", "FALSE",
)
COMMANDS = (
    "/help", "/databases", "/tables", "/describe", "/file",
    "/stop-on-error", "/inspect", "/clear", "/quit",
)
STYLE = Style.from_dict({
    "prompt": "bold #64d9c3",
    "rule": "#424955",
    "bottom-toolbar": "bg:default #9299a6",
    "completion-menu.completion.current": "bg:#383653 #ffffff",
    "auto-suggestion": "#697181",
    "pygments.keyword": "bold #8bb5fa",
    "pygments.literal.string": "#e5c07b",
    "pygments.literal.number": "#e5c07b",
})


class SqlCompleter(Completer):
    """根据当前输入上下文生成 SQL、终端命令和元数据补全。"""

    def __init__(self, runner: Runner) -> None:
        """保存用于动态读取数据库名和表名的 Runner。"""

        self.runner = runner

    def get_completions(self, document, complete_event):
        """根据光标前单词返回不重复的候选项。

        ``/inspect `` 之后只提供 ALL/A/B/C；USE 和表名上下文从
        Runner 动态读取。元数据读取失败会被隔离，不会打断用户编辑。
        """

        before = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)
        prefix = before[:len(before) - len(word)].strip().upper()
        candidates = list(COMMANDS if before.lstrip().startswith("/") else KEYWORDS)
        try:
            if before.lstrip().lower().startswith("/inspect "):
                candidates = [item.value for item in InspectionModule]
            elif prefix.endswith("USE") or prefix.endswith("DATABASE"):
                candidates = self.runner.list_databases()
            elif prefix.endswith(("FROM", "INTO", "UPDATE", "TABLE", "/DESCRIBE")):
                candidates = self.runner.list_tables()
            elif not before.lstrip().startswith("/"):
                candidates += self.runner.list_tables()
        except SqlError:
            # 补全失败不妨碍继续编辑和执行 SQL。
            pass
        for candidate in sorted(set(candidates)):
            if candidate.lower().startswith(word.lower()):
                yield Completion(candidate, start_position=-len(word))


class TerminalSession:
    """管理一个持久 Runner 会话的输入、命令、输出和错误恢复。"""

    def __init__(
        self, runner: Runner, *, data_dir: Path | None = None,
        plain: bool = False, history: bool = True, stop_on_error: bool = True,
    ) -> None:
        """初始化交互状态并根据 TTY/用户选项决定渲染模式。

        Args:
            runner: 保存当前数据库和可选 inspector 的运行会话。
            data_dir: 可选历史文件所在目录。
            plain: 是否强制纯文本模式。
            history: 是否使用持久输入历史。
            stop_on_error: 多语句脚本遇错时是否停止。
        """

        self.runner = runner
        self.data_dir = data_dir
        self.interactive = not plain and sys.stdin.isatty() and sys.stdout.isatty()
        self.history_enabled = history
        self.stop_on_error = stop_on_error
        self.renderer = TerminalRenderer()

    def _make_prompt(self) -> PromptSession:
        """构建带历史、SQL 高亮、补全和多行键位的提示器。"""

        bindings = KeyBindings()

        @bindings.add("enter")
        def _execute_buffer(event) -> None:
            """将 Enter 绑定为提交当前完整缓冲区。"""

            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _insert_newline(event) -> None:
            """将 Alt+Enter 绑定为在当前光标处插入换行。"""

            event.current_buffer.insert_text("\n")

        history = InMemoryHistory()
        if self.history_enabled and self.data_dir is not None:
            try:
                history_path = self.data_dir / ".hello_sql_history"
                # 在会话开始时发现无法写入的情况，避免每次提交输入时失败。
                with history_path.open("a", encoding="utf-8"):
                    pass
                history = FileHistory(str(history_path))
            except OSError:
                self.renderer.console.print("历史文件不可写，本次仅保留内存历史。", style="yellow")
        return PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=SqlCompleter(self.runner),
            complete_while_typing=False,
            multiline=True,
            key_bindings=bindings,
            lexer=PygmentsLexer(SqlLexer),
            style=STYLE,
            bottom_toolbar=lambda: [
                ("class:rule", "─" * self.renderer.console.width + "\n"),
                ("", "Enter 执行 · Alt+Enter 换行 · Tab 补全 · ↑↓ 历史 · Ctrl+D 退出"),
            ],
            reserve_space_for_menu=3,
        )

    def _result(self, result: QueryResult, elapsed: float | None = None) -> None:
        """按交互或纯文本模式渲染一个 QueryResult。"""

        if self.interactive:
            self.renderer.result(result, elapsed)
        else:
            self.runner._print_result(result)

    def _script_result(self, result: ScriptResult) -> bool:
        """渲染脚本结果，返回其中是否至少有一条失败。"""
        if self.interactive:
            self.renderer.script_result(result)
        else:
            for statement in result.statements:
                if statement.result is not None:
                    self.runner._print_result(statement.result)
                else:
                    assert statement.error is not None
                    print(f"[{statement.error.code}] {safe_text(statement.error.message)}")
        return any(statement.error is not None for statement in result.statements)

    def _notice(self, message: str) -> None:
        """显示不代表执行失败的辅助提示。

        交互模式使用与界面一致的弱化颜色，纯文本模式使用普通
        ``print``，便于管道和自动化测试读取。
        """

        if self.interactive:
            self.renderer.console.print(message, style="#9299a6")
        else:
            print(message)

    def _inspect(self, module: InspectionModule) -> None:
        """读取并显示最近 SQL 的指定模块快照。

        Args:
            module: ALL、A、B 或 C 筛选。

        本方法不调用 Runner.execute。未启用 inspector 或尚无 SQL 记录时，
        只给出提示并返回，不把这类界面状态算作 SQL 失败。
        """

        inspector = self.runner.inspector
        if inspector is None:
            self._notice("当前会话未启用查询追踪。")
            return
        value = inspector.latest(module.value)
        if value is None:
            self._notice("暂无可查看的 SQL 追踪，请先执行一条语句。")
            return
        snapshot = cast(InspectionSnapshot, value)
        if self.interactive:
            url, opened = inspector.open_view(module.value)
            self.renderer.inspection(snapshot)
            if opened:
                self._notice(f"已在默认浏览器打开查看器：{url}")
            else:
                self._notice(f"无法自动打开浏览器，请手动访问：{url}")
        else:
            print(format_inspection_text(snapshot))

    def _command(self, sql: str) -> tuple[bool, bool]:
        """返回（是否为界面命令，命令执行是否失败）。"""
        if not sql.startswith("/"):
            return False, False
        try:
            parts = shlex.split(sql)
        except ValueError as error:
            raise SqlError(E_BAD_ARG, f"命令参数无效：{error}") from None
        if not parts:
            return False, False
        command = parts[0].lower()
        if command == "/inspect":
            valid_arity = len(parts) in (1, 2)
        else:
            expected = 2 if command in ("/describe", "/file", "/stop-on-error") else 1
            valid_arity = len(parts) == expected
        if command not in COMMANDS or not valid_arity:
            raise SqlError(E_BAD_ARG, "未知命令或参数不正确，请输入 /help")
        if command == "/help":
            if self.interactive:
                self.renderer.help()
            else:
                for key, description in HELP_ITEMS:
                    print(f"{key}\t{description}")
        elif command == "/clear":
            if self.interactive:
                self.renderer.console.clear()
        elif command == "/databases":
            self._result(QueryResult(columns=("database",), rows=tuple((name,) for name in sorted(self.runner.list_databases()))))
        elif command == "/tables":
            self._result(QueryResult(columns=("table",), rows=tuple((name,) for name in sorted(self.runner.list_tables()))))
        elif command == "/describe":
            info = self.runner.describe_table(parts[1].lower())
            self._result(QueryResult(columns=("column", "type"), rows=tuple((col.name, col.type.value) for col in info.columns)))
        elif command == "/file":
            result = self.runner.execute_file(
                Path(parts[1]).expanduser(),
                stop_on_error=self.stop_on_error,
            )
            return True, self._script_result(result)
        elif command == "/inspect":
            try:
                module = InspectionModule.parse(parts[1] if len(parts) == 2 else None)
            except ValueError:
                raise SqlError(E_BAD_ARG, "/inspect 只接受 A、B、C 或 ALL") from None
            self._inspect(module)
        elif command == "/stop-on-error":
            value = parts[1].lower()
            if value not in ("on", "off"):
                raise SqlError(E_BAD_ARG, "/stop-on-error 只接受 on 或 off")
            self.stop_on_error = value == "on"
            message = f"stop-on-error = {value}"
            if self.interactive:
                self.renderer.console.print(message, style="#9299a6")
            else:
                print(message)
        return True, False

    def _execute_input(self, sql: str) -> bool:
        """执行一个完整输入缓冲区，返回是否出现 SQL 错误。"""
        recognized, failed = self._command(sql)
        if recognized:
            return failed
        result = self.runner.execute_script(sql, stop_on_error=self.stop_on_error)
        return self._script_result(result)

    def run(self) -> int:
        """运行提示循环，直到用户退出或输入流结束。

        交互终端中 SQL 错误会渲染后继续；管道模式会记录失败并
        在最终返回非零退出码。Ctrl+C 只取消尚未提交的编辑内容。
        """

        prompt = None
        if self.interactive:
            self.renderer.welcome(self.runner.current_database, self.data_dir)
            prompt = self._make_prompt()
        failed = False
        while True:
            try:
                if prompt is not None:
                    sql = prompt.prompt([("class:prompt", f"{self.runner.current_database} ❯ ")])
                else:
                    sql = input(f"{self.runner.current_database}> " if sys.stdin.isatty() else "")
            except EOFError:
                return int(failed)
            except KeyboardInterrupt:
                # 这里只拦截编辑阶段，绝不声称能回滚已开始执行的 DML。
                continue
            sql = sql.strip()
            if not sql:
                continue
            if sql.lower().rstrip(";") in ("/quit", "quit", "exit", "\\q"):
                return int(failed)
            try:
                input_failed = self._execute_input(sql)
                failed = failed or (input_failed and not sys.stdin.isatty())
            except SqlError as error:
                # 交互纠错后仍可正常退出；管道输入出现错误则返回非零退出码。
                failed = failed or not sys.stdin.isatty()
                if self.interactive:
                    self.renderer.error(error)
                else:
                    print(f"[{error.code}] {safe_text(error.message)}")
