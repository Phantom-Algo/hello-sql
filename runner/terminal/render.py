"""HELLO-SQL 的 Rich 渲染、pyfiglet 标题与追踪概览。

欢迎页的 HELLO-SQL 艺术字完全由 pyfiglet 第三方字体生成，不使用
手工符号堆叠。查询结果和 ``/inspect`` 概览共用蓝紫粉强调色、
方框与弱化辅助文字，与已确定的暗色终端样式保持一致。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import TYPE_CHECKING

from pyfiglet import Figlet
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from contracts.errors import SqlError
from contracts.result import QueryResult, ScriptResult

if TYPE_CHECKING:
    from UI.inspection import InspectionSnapshot


ACCENT = "#64d9c3"
MUTED = "#9299a6"
OWNER_STYLES = {"A": "#65a8ff", "B": "#f17cc2", "C": "#9b8cff", "SHARED": ACCENT}
STATUS_STYLES = {
    "SUCCESS": "#95df88",
    "FAILED": "#ff777f",
    "SKIPPED": MUTED,
    "DISABLED": "#e5c07b",
    "PENDING": "#e5c07b",
    "RUNNING": ACCENT,
}


def safe_text(value: object) -> str:
    """将记录里的换行、ESC 等控制字符显示为字面量，保持表格/终端完整。"""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: repr(m[0])[1:-1], str(value))


@lru_cache(maxsize=8)
def title_art(width: int) -> str:
    """根据终端宽度选择 pyfiglet 字体并缓存艺术字。

    优先使用立体 ``ansi_shadow``，宽度不足时退化为 ``small``，极窄
    终端才返回普通标题。函数不手工修改字形。
    """

    # 字形完全来自 pyfiglet 的字体资源，宽度不够时换用紧凑字体。
    for font in ("ansi_shadow", "small"):
        art = Figlet(font=font, width=1000).renderText("HELLO-SQL").rstrip()
        if max(map(len, art.splitlines()), default=0) <= width:
            return art
    return "HELLO-SQL"


def gradient_title(width: int) -> Text:
    """将 pyfiglet 艺术字映射为蓝、紫、粉三段真彩渐变。"""

    art = title_art(width)
    longest = max(map(len, art.splitlines()), default=1)
    stops = ((59, 149, 255), (175, 117, 244), (255, 110, 161))
    result = Text(no_wrap=True)
    for row, line in enumerate(art.splitlines()):
        if row:
            result.append("\n")
        for column, char in enumerate(line):
            position = column / max(longest - 1, 1) * 2
            segment = min(int(position), 1)
            mix = position - segment
            rgb = tuple(round(a + (b - a) * mix) for a, b in zip(stops[segment], stops[segment + 1]))
            result.append(char, style="#{:02x}{:02x}{:02x}".format(*rgb))
    return result


class TerminalRenderer:
    """集中实现欢迎页、SQL 结果、错误、帮助和追踪概览。"""

    def __init__(self, console: Console | None = None) -> None:
        """使用调用方 Console 或创建禁止自动高亮的默认 Console。"""

        self.console = console if console is not None else Console(highlight=False)

    def welcome(self, database: str, data_dir: Path | None) -> None:
        """按终端宽度渲染响应式艺术字、会话卡片和快速入口。"""

        width = self.console.width
        session = Text(
            f">_ hello-sql\n\n当前数据库  {safe_text(database)}\n运行模式    本地\n"
            f"数据目录\n{safe_text(data_dir) if data_dir else '由调用方管理'}",
            overflow="fold",
        )
        session.stylize("bold #b09cf7", 0, 12)
        info = Panel(session, border_style="#9b8cdd", padding=(0, 1))
        self.console.print()
        if width >= 108:
            header = Table.grid(padding=(0, 3), expand=True)
            header.add_column(width=width - 39)
            header.add_column(width=36)
            header.add_row(
                Group(gradient_title(width - 39), Text("一个轻量级 SQL 数据库", style=MUTED)),
                info,
            )
            self.console.print(header)
        else:
            self.console.print(gradient_title(width))
            self.console.print(Text("一个轻量级 SQL 数据库", style=MUTED))
            self.console.print(info)
        self.console.print()
        self.console.print(Text("快速开始  /help 帮助 · /tables 查看表 · /inspect 查看最近流程", style=MUTED))
        self.console.rule(style="#424955")

    def result(self, result: QueryResult, elapsed: float | None = None) -> None:
        """将 SELECT 行集渲染为方框表格，将 DDL/DML 渲染为影响行摘要。"""

        suffix = f" · {elapsed:.3f}s" if elapsed is not None else ""
        if result.columns is not None and result.rows is not None:
            table = Table(box=box.SQUARE, border_style=ACCENT, header_style="bold", highlight=False)
            for index, column in enumerate(result.columns):
                numeric = bool(result.rows) and all(
                    isinstance(row[index], (int, float)) for row in result.rows
                )
                table.add_column(Text(safe_text(column)), justify="right" if numeric else "left", overflow="fold")
            for row in result.rows:
                table.add_row(*(Text(safe_text(value)) for value in row))
            self.console.print(table)
            count = len(result.rows)
            self.console.print(Text(f"{count} row{'s' if count != 1 else ''}{suffix}", style=MUTED))
        else:
            count = result.affected_rows or 0
            self.console.print(Text(f"✓ {count} row{'s' if count != 1 else ''} affected{suffix}", style="#95df88"))
        self.console.print()

    def error(self, error: SqlError, elapsed: float | None = None) -> None:
        """以字面文本渲染 SQL 错误码、消息和可选耗时。"""

        suffix = f" · {elapsed:.3f}s" if elapsed is not None else ""
        self.console.print(
            Text(
                f"[{error.code}] {safe_text(error.message)}{suffix}",
                style="#ff777f",
            )
        )
        self.console.print()

    def script_result(self, result: ScriptResult) -> None:
        """按源码顺序展示脚本内每条语句的位置、结果和耗时。"""
        total = len(result.statements)
        for index, statement in enumerate(result.statements, start=1):
            span = statement.span
            self.console.print(
                Text(
                    f"#{index}/{total}  "
                    f"{span.start_line}:{span.start_col}-"
                    f"{span.end_line}:{span.end_col}",
                    style=MUTED,
                )
            )
            if statement.result is not None:
                self.result(statement.result, statement.elapsed_ms / 1000)
            else:
                assert statement.error is not None
                self.error(statement.error, statement.elapsed_ms / 1000)
        if result.stopped_early:
            self.console.print(Text("遇到错误，已停止执行后续语句。", style="#ffb86c"))
            self.console.print()

    def inspection(self, snapshot: InspectionSnapshot) -> None:
        """按蓝紫粉暗色 TUI 风格渲染最近查询的阶段概览。

        顶部卡片展示查询编号、数据库、脚本位置、筛选和 SQL；下方
        流水线表以全局 sequence 排序，为 A/B/C 使用独立颜色，并展示
        状态、事件数和耗时。本方法只读取 ``InspectionSnapshot``，不调用
        任何 Parser、Storage 或 Executor。
        """

        trace = snapshot.trace
        header = Text()
        header.append(f"QUERY #{trace.query_number:04d}", style="bold #b09cf7")
        header.append(f"   {safe_text(trace.trace_id)}\n", style=MUTED)
        header.append("数据库  ", style=MUTED)
        header.append(f"{safe_text(trace.database)}   ", style="bold")
        header.append("状态  ", style=MUTED)
        header.append(
            trace.status.value,
            style=f"bold {STATUS_STYLES.get(trace.status.value, MUTED)}",
        )
        header.append("   筛选  ", style=MUTED)
        header.append(snapshot.module.value, style="bold #64d9c3")
        header.append(
            f"   语句 {trace.statement_index}/{trace.statement_count}\n",
            style=MUTED,
        )
        header.append("SQL  ", style=MUTED)
        header.append(safe_text(trace.sql), style="#e6e9ef")
        self.console.print(
            Panel(
                header,
                title="[bold #64d9c3]>_ QUERY INSPECTOR[/]",
                border_style="#6f63a8",
                padding=(1, 2),
            )
        )

        table = Table(
            box=box.SQUARE,
            border_style="#424955",
            header_style="bold #c9c3ec",
            title=f"PIPELINE  ·  {len(snapshot.stages)} STAGES",
            title_style="bold #9299a6",
            expand=True,
            highlight=False,
        )
        table.add_column("#", justify="right", width=3, style=MUTED)
        table.add_column("模块", justify="center", width=5)
        table.add_column("阶段", ratio=2, overflow="fold")
        table.add_column("状态", width=10)
        table.add_column("事件", justify="right", width=6)
        table.add_column("耗时", justify="right", width=11)
        for stage in snapshot.stages:
            owner = stage.owner.value
            owner_style = OWNER_STYLES.get(owner, ACCENT)
            status_style = STATUS_STYLES.get(stage.status.value, MUTED)
            table.add_row(
                str(stage.sequence),
                Text(f"● {owner}", style=f"bold {owner_style}"),
                Text(stage.name, style=owner_style),
                Text(stage.status.value, style=status_style),
                str(len(stage.events)),
                f"{stage.elapsed_ms:.3f} ms",
            )
        self.console.print(table)
        self.console.print(
            Text(
                "筛选：/inspect A · /inspect B · /inspect C · /inspect ALL",
                style=MUTED,
            )
        )
        self.console.print()

    def help(self) -> None:
        """渲染终端命令、SQL 能力和编辑快捷键帮助。"""

        table = Table(box=box.SIMPLE, border_style=ACCENT, title="hello-sql 帮助", highlight=False)
        table.add_column("命令 / 快捷键", style=ACCENT)
        table.add_column("说明")
        for command, description in HELP_ITEMS:
            table.add_row(command, description)
        self.console.print(table)
        self.console.print(Text(
            "SQL：CREATE/DROP DATABASE、USE、CREATE/DROP TABLE、INSERT、SELECT、UPDATE、DELETE\n"
            "类型：INT / TEXT / REAL / BOOLEAN；支持 AND / OR / NOT 与 INNER JOIN。\n"
            "Enter 执行当前缓冲区，Alt+Enter 换行；可一次执行多条 SQL。",
            style=MUTED,
        ))


HELP_ITEMS = (
    ("/help", "查看帮助"),
    ("/databases", "查看数据库"),
    ("/tables", "查看当前库的表"),
    ("/describe 表名", "查看表结构"),
    ("/file 路径", "按 UTF-8 执行 SQL 文件"),
    ("/stop-on-error on|off", "设置脚本遇错停止或继续"),
    ("/inspect [ALL|A|B|C]", "查看最近 SQL 的全链路或指定模块"),
    ("/clear", "清理屏幕"),
    ("/quit、quit、exit", "退出程序"),
    ("Tab / ↑↓", "补全 / 浏览输入历史"),
    ("Enter / Alt+Enter", "执行当前缓冲区 / 插入换行"),
    ("Ctrl+C / Ctrl+D", "清空尚未提交的输入 / 空输入时退出"),
)
