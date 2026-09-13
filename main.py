"""hello-sql 命令入口，也是 compiler / storage / runner 的装配层。"""

from __future__ import annotations

import argparse
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

from compiler import parse, parse_script
from contracts.errors import SqlError
from contracts.result import ScriptResult
from runner import Runner
from storage import DatabaseServer
from UI import QueryInspector


def build_parser() -> argparse.ArgumentParser:
    """构建 HELLO-SQL 命令行参数解析器。

    本函数只声明参数、帮助和版本信息，不创建数据目录，也不启动
    数据库。因此 ``--help`` 和 ``--version`` 可以在任意目录安全运行。
    """

    parser = argparse.ArgumentParser(description="hello-sql · 轻量级本地 SQL 数据库")
    parser.add_argument("-D", "--database", default="main", help="初始数据库（默认 main）")
    parser.add_argument(
        "--data-dir", type=Path,
        help="数据目录（默认 HELLO_SQL_DATA_DIR 或 ~/.hello-sql/data）",
    )
    parser.add_argument("--plain", action="store_true", help="使用纯文本交互，关闭艺术字和颜色")
    parser.add_argument("--no-history", action="store_true", help="不读取或保存磁盘输入历史")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("-e", "--execute", metavar="SQL", help="执行 SQL 或多语句脚本后退出")
    execution.add_argument("-f", "--file", type=Path, metavar="PATH", help="按 UTF-8 执行 SQL 文件后退出")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="多语句执行时记录错误并继续",
    )
    try:
        app_version = version("hello-sql")
    except PackageNotFoundError:
        app_version = "1.1.0 (source)"
    parser.add_argument("--version", action="version", version=f"hello-sql {app_version}")
    return parser


def resolve_data_dir(argument: Path | None) -> Path:
    """按显式参数、环境变量、用户默认值的顺序解析数据目录。

    Args:
        argument: ``--data-dir`` 传入的可选路径。

    Returns:
        已扩展用户目录并转为绝对路径的数据根目录。
    """

    configured = os.environ.get("HELLO_SQL_DATA_DIR")
    path = argument if argument is not None else (
        Path(configured) if configured else Path.home() / ".hello-sql" / "data"
    )
    return path.expanduser().resolve()


def print_script_result(runner: Runner, result: ScriptResult, *, rich: bool) -> int:
    """展示脚本汇总结果，有任一语句失败时返回非零退出码。"""
    if rich:
        from runner.terminal.render import TerminalRenderer

        TerminalRenderer().script_result(result)
    else:
        from runner.terminal.render import safe_text

        for statement in result.statements:
            if statement.result is not None:
                runner._print_result(statement.result)
            else:
                assert statement.error is not None
                print(
                    f"[{statement.error.code}] {safe_text(statement.error.message)}",
                    file=sys.stderr,
                )
    return int(any(statement.error is not None for statement in result.statements))


def main(argv: list[str] | None = None) -> int:
    """装配存储、运行器和可视化追踪，然后执行选定模式。

    交互模式、``-e`` 和 ``-f`` 都复用同一个 ``QueryInspector``。
    B 路由器注入 DatabaseServer，C 路由器和编排器注入 Runner，
    从而让 ``/inspect`` 能读到与实际 SQL 执行一致的完整记录。

    Returns:
        成功为 0，SQL/输入错误为 1，键盘中断为 130。
    """

    args = build_parser().parse_args(argv)
    try:
        data_dir = resolve_data_dir(args.data_dir)
        inspector = QueryInspector()
        server = DatabaseServer(data_dir, trace_sink=inspector.storage_router)
        runner = Runner(
            server=server,
            parse=parse,
            parse_script=parse_script,
            current_database=args.database.lower(),
            trace_sink=inspector.execution_router,
            inspector=inspector,
        )
        if args.execute is not None:
            result = runner.execute_script(
                args.execute,
                stop_on_error=not args.continue_on_error,
            )
            return print_script_result(
                runner,
                result,
                rich=sys.stdout.isatty() and not args.plain,
            )
        if args.file is not None:
            result = runner.execute_file(
                args.file.expanduser(),
                stop_on_error=not args.continue_on_error,
            )
            return print_script_result(
                runner,
                result,
                rich=sys.stdout.isatty() and not args.plain,
            )
        return runner.repl(
            data_dir=data_dir,
            plain=args.plain,
            history=not args.no_history,
            stop_on_error=not args.continue_on_error,
        )
    except SqlError as error:
        # Literal text: do not interpret error contents as terminal markup.
        from runner.terminal.render import safe_text
        print(f"[{error.code}] {safe_text(error.message)}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"hello-sql: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n操作被中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
