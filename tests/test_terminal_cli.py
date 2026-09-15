"""CLI 真实进程持久化、TUI 渲染与交互边界回归测试。"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.cells import cell_len
from rich.console import Console

from compiler import parse, parse_script
from contracts.errors import E_BAD_ARG, SqlError
from contracts.result import QueryResult
from main import main, resolve_data_dir
from runner import Runner
from runner.terminal.render import TerminalRenderer, gradient_title, title_art
from runner.terminal.session import SqlCompleter, TerminalSession
from storage import DatabaseServer
from UI import QueryInspector


ROOT = Path(__file__).resolve().parents[1]


def run_cli(tmp_path, *args, sql=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--data-dir", str(tmp_path / "db"), *args],
        input=sql, text=True, capture_output=True, cwd=cwd or tmp_path, timeout=15,
    )


def test_cli_persistence_across_processes_and_working_directories(tmp_path):
    written = run_cli(tmp_path, sql="CREATE TABLE users (id INT, name TEXT);\nINSERT INTO users VALUES (1, '张三');\n/quit\n")
    assert written.returncode == 0, written.stderr
    other = tmp_path / "other"
    other.mkdir()
    read = run_cli(tmp_path, "-e", "SELECT * FROM users;", cwd=other)
    assert read.returncode == 0, read.stderr
    assert read.stdout == "id\tname\n1\t张三\n"
    assert "\x1b" not in read.stdout
    assert not (other / "data").exists()


def test_batch_error_exit_code_and_continue(tmp_path):
    result = run_cli(tmp_path, sql="SELEC * FROM t;\nCREATE TABLE t (id INT);\n/tables\n")
    assert result.returncode == 1
    assert "[E_SYNTAX]" in result.stdout
    assert "table\nt\n" in result.stdout
    failed = run_cli(tmp_path, "-e", "SELECT * FROM missing;")
    assert failed.returncode == 1
    assert "[E_TABLE_NOT_FOUND]" in failed.stderr
    assert "Traceback" not in failed.stderr


def test_metadata_commands_follow_use(tmp_path):
    result = run_cli(tmp_path, sql=(
        "CREATE DATABASE shop;\nUSE shop;\nCREATE TABLE goods (id INT, name TEXT);\n"
        "/tables\n/describe goods\nUSE main;\n/tables\n/databases\nexit;\n"
    ))
    assert result.returncode == 0, result.stderr
    assert "table\ngoods\n" in result.stdout
    assert "column\ttype\nid\tINT\nname\tTEXT\n" in result.stdout
    assert "database\nmain\nshop\n" in result.stdout
    assert result.stdout.count("goods") == 1


def test_path_priority_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.delenv("HELLO_SQL_DATA_DIR", raising=False)
    expected = tmp_path / "user" / ".hello-sql" / "data"
    assert resolve_data_dir(None) == expected
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir(None) == expected
    monkeypatch.setenv("HELLO_SQL_DATA_DIR", str(tmp_path / "configured"))
    assert resolve_data_dir(None) == tmp_path / "configured"
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_help_and_version_do_not_create_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HELLO_SQL_DATA_DIR", str(tmp_path / "never-created"))
    for argument in ("--help", "--version"):
        with pytest.raises(SystemExit) as caught:
            main([argument])
        assert caught.value.code == 0
    assert "hello-sql" in capsys.readouterr().out
    assert not (tmp_path / "never-created").exists()


def test_completion_tracks_current_database(tmp_path):
    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    runner.execute("CREATE TABLE local_table (id INT)")
    runner.execute("CREATE DATABASE shop")
    completer = SqlCompleter(runner)

    def choices(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert choices("SELECT * FROM lo") == ["local_table"]
    assert choices("USE sh") == ["shop"]
    runner.execute("USE shop")
    assert choices("SELECT * FROM lo") == []
    assert choices("/ta") == ["/tables"]


def test_inspect_completion_offers_only_supported_module_filters(tmp_path):
    """``/inspect`` 参数位应只提示 ALL/A/B/C 四个稳定筛选值。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    completer = SqlCompleter(runner)
    choices = [
        item.text
        for item in completer.get_completions(
            Document("/inspect "),
            CompleteEvent(),
        )
    ]

    assert choices == ["A", "ALL", "B", "C"]


def test_plain_inspect_command_filters_latest_trace_without_reexecution(
    tmp_path,
    capsys,
):
    """纯文本 ``/inspect A`` 应只输出 A 阶段且不新增追踪。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    runner.execute("CREATE TABLE inspected (id INT);")
    session = TerminalSession(runner, plain=True, history=False)
    count_before = len(inspector.hub)

    assert session._command("/inspect A") == (True, False)
    output = capsys.readouterr().out
    assert "module=A" in output
    assert "A Lexer" in output
    assert "B Catalog" not in output
    assert "C Runtime" not in output
    assert len(inspector.hub) == count_before


def test_inspect_before_first_query_and_invalid_filter_are_friendly(tmp_path, capsys):
    """无历史时给出提示，无效模块则使用 E_BAD_ARG 明确拒绝。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    session = TerminalSession(runner, plain=True, history=False)

    assert session._command("/inspect") == (True, False)
    assert "暂无可查看" in capsys.readouterr().out
    with pytest.raises(SqlError) as caught:
        session._command("/inspect D")
    assert caught.value.code == E_BAD_ARG


def test_interactive_inspect_opens_local_view_and_renders_tui_summary(tmp_path):
    """交互 ``/inspect C`` 应打开本地窗口并同时保留 Rich 阶段摘要。"""

    inspector = QueryInspector()
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    runner.execute("CREATE TABLE browser_demo (id INT);")
    session = TerminalSession(runner, history=False)
    session.interactive = True
    stream = io.StringIO()
    session.renderer = TerminalRenderer(
        Console(file=stream, width=120, color_system=None)
    )

    with patch.object(
        inspector,
        "open_view",
        return_value=("http://127.0.0.1:43123/?module=C", True),
    ) as opener:
        assert session._command("/inspect C") == (True, False)

    opener.assert_called_once_with("C")
    output = stream.getvalue()
    assert "QUERY INSPECTOR" in output
    assert "Binder" in output and "Runtime" in output
    assert "Lexer" not in output and "Catalog" not in output
    assert "127.0.0.1:43123" in output


@pytest.mark.parametrize("width", [24, 40, 80, 108, 120, 160])
def test_welcome_fits_terminal_width(width):
    stream = io.StringIO()
    renderer = TerminalRenderer(Console(file=stream, width=width, color_system=None))
    renderer.welcome("main", Path("/tmp/测试目录/hello-sql/data"))
    assert all(cell_len(line) <= width for line in stream.getvalue().splitlines())
    assert max(map(cell_len, title_art(width).splitlines())) <= width


def test_tables_preserve_unicode_markup_and_empty_headers():
    stream = io.StringIO()
    renderer = TerminalRenderer(Console(file=stream, width=100, color_system=None))
    renderer.result(QueryResult(columns=("name",), rows=(("张三 [red] ",), ("\x1b[2J\n",))))
    renderer.result(QueryResult(columns=("empty_column",), rows=()))
    output = stream.getvalue()
    assert "张三 [red]" in output
    assert "\\x1b[2J\\n" in output
    assert "\x1b" not in output
    assert "empty_column" in output and "0 rows" in output


def test_art_is_rendered_in_true_color():
    stream = io.StringIO()
    console = Console(file=stream, width=120, force_terminal=True, color_system="truecolor", no_color=False)
    console.print(gradient_title(100))
    assert "\x1b[38;2;" in stream.getvalue()
    assert "#3b95ff" in str(gradient_title(100).spans[0].style)


def test_interactive_ctrl_c_error_recovery_and_updated_prompt(tmp_path):
    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    session = TerminalSession(runner, data_dir=tmp_path, history=False)
    session.interactive = True
    stream = io.StringIO()
    session.renderer = TerminalRenderer(Console(file=stream, width=120, color_system=None))
    answers = iter([KeyboardInterrupt(), "SELEC", "CREATE DATABASE shop", "USE shop", EOFError()])
    prompts = []

    class FakePrompt:
        def prompt(self, message):
            prompts.append(message[0][1])
            value = next(answers)
            if isinstance(value, BaseException):
                raise value
            return value

    with patch.object(session, "_make_prompt", return_value=FakePrompt()), patch("sys.stdin.isatty", return_value=True):
        assert session.run() == 0
    assert prompts[-1] == "shop ❯ "
    assert "[E_SYNTAX]" in stream.getvalue()
    assert not (tmp_path / ".hello_sql_history").exists()


def test_installed_command_can_run_outside_repository(tmp_path):
    # 此项目按 README 安装后，入口必须不依赖 cwd/PYTHONPATH。
    command = Path(sys.executable).parent / ("hello-sql.exe" if os.name == "nt" else "hello-sql")
    if not command.exists():
        pytest.skip("需要先安装应用以测试 console_scripts")
    result = subprocess.run([str(command), "--version"], cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("hello-sql ")


def test_cli_execute_accepts_multiple_statements(tmp_path):
    result = run_cli(
        tmp_path,
        "-e",
        "CREATE TABLE flags (id INT, enabled BOOLEAN); "
        "INSERT INTO flags VALUES (1, TRUE); "
        "SELECT * FROM flags WHERE enabled;",
    )

    assert result.returncode == 0, result.stderr
    assert "id\tenabled\n1\tTrue\n" in result.stdout


def test_cli_and_terminal_command_execute_utf8_sql_file(tmp_path):
    cli_file = tmp_path / "cli demo.sql"
    cli_file.write_text(
        "CREATE TABLE cli_notes (id INT, body TEXT);\n"
        "INSERT INTO cli_notes VALUES (1, '你好');\n"
        "SELECT *\nFROM cli_notes;\n",
        encoding="utf-8",
    )
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    cli = run_cli(cli_root, "-f", str(cli_file))
    assert cli.returncode == 0, cli.stderr
    assert "id\tbody\n1\t你好\n" in cli.stdout

    tui_file = tmp_path / "tui demo.sql"
    tui_file.write_text(
        "CREATE TABLE tui_notes (id INT, body TEXT);\n"
        "INSERT INTO tui_notes VALUES (2, '文件执行');\n"
        "SELECT * FROM tui_notes;\n",
        encoding="utf-8",
    )
    terminal_root = tmp_path / "terminal"
    terminal_root.mkdir()
    terminal = run_cli(
        terminal_root,
        sql=f'/file "{tui_file}"\n/quit\n',
    )
    assert terminal.returncode == 0, terminal.stderr
    assert "id\tbody\n2\t文件执行\n" in terminal.stdout


def test_terminal_continue_on_error_executes_later_file_statements(tmp_path):
    sql_file = tmp_path / "continue.sql"
    sql_file.write_text(
        "CREATE TABLE values_table (id INT);\n"
        "INSERT INTO values_table VALUES ('bad');\n"
        "INSERT INTO values_table VALUES (7);\n"
        "SELECT * FROM values_table;\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        sql=f'/stop-on-error off\n/file "{sql_file}"\n/quit\n',
    )

    assert result.returncode == 1
    assert "stop-on-error = off" in result.stdout
    assert "[E_TYPE_MISMATCH]" in result.stdout
    assert "id\n7\n" in result.stdout


def test_interactive_prompt_executes_multiline_multistatement_buffer(tmp_path):
    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    session = TerminalSession(runner, data_dir=tmp_path, history=False)
    session.interactive = True
    stream = io.StringIO()
    session.renderer = TerminalRenderer(Console(file=stream, width=120, color_system=None))
    script = (
        "CREATE TABLE entries (id INT, enabled BOOLEAN);\n"
        "INSERT INTO entries VALUES (1, TRUE);\n"
        "SELECT *\nFROM entries\nWHERE enabled;"
    )
    answers = iter([script, EOFError()])

    class FakePrompt:
        def prompt(self, message):
            value = next(answers)
            if isinstance(value, BaseException):
                raise value
            return value

    with (
        patch.object(session, "_make_prompt", return_value=FakePrompt()),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert session.run() == 0

    assert runner.execute("SELECT * FROM entries;").rows == ((1, True),)
    output = stream.getvalue()
    assert "#1/3" in output
    assert "#3/3" in output


def setup_indexed_table(tmp_path):
    """在 CLI 数据目录里建一张带索引的表，返回建表过程的结果。"""

    setup = run_cli(
        tmp_path,
        sql=(
            "CREATE TABLE items (id INT, note TEXT);\n"
            "CREATE INDEX items_id ON items (id);\n"
            "INSERT INTO items VALUES (1, 'a');\n"
            "/quit\n"
        ),
    )
    assert setup.returncode == 0, setup.stderr
    return setup


def prepare_indexed_table(tmp_path, sql="SELECT * FROM items;"):
    """建表后按给定 SQL 再跑一次，返回可直接断言的 CLI 结果。"""

    setup_indexed_table(tmp_path)
    return run_cli(tmp_path, sql=f"{sql}\n/quit\n")


def test_physical_command_reports_and_switches_mode(tmp_path, capsys):
    """``/physical`` 无参显示当前值，带参切换；非法取值以 E_BAD_ARG 拒绝。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    session = TerminalSession(runner, plain=True, history=False)
    assert session.physical == "auto"

    assert session._command("/physical") == (True, False)
    assert "physical = auto" in capsys.readouterr().out

    assert session._command("/physical index") == (True, False)
    assert session.physical == "index"
    assert "physical = index" in capsys.readouterr().out

    with pytest.raises(SqlError) as caught:
        session._command("/physical bogus")
    assert caught.value.code == E_BAD_ARG

    with pytest.raises(SqlError) as caught:
        session._command("/physical index extra")
    assert caught.value.code == E_BAD_ARG


def test_terminal_session_honors_initial_and_changed_physical_mode(tmp_path, capsys):
    """会话初值与 ``/physical`` 都要真正作用到语句执行上，而不是只改显示。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    runner.execute("CREATE TABLE items (id INT, note TEXT)")
    runner.execute("CREATE INDEX items_id ON items (id)")
    runner.execute("INSERT INTO items VALUES (1, 'a')")

    forced = TerminalSession(runner, plain=True, history=False, physical="index")
    # 无谓词查询在 index 模式下必须报错，证明初值真的生效
    assert forced._execute_input("SELECT * FROM items;") is True
    assert "[E_BAD_ARG]" in capsys.readouterr().out

    switching = TerminalSession(runner, plain=True, history=False)
    assert switching._execute_input("SELECT * FROM items;") is False
    assert "[E_BAD_ARG]" not in capsys.readouterr().out
    switching._command("/physical index")
    assert switching._execute_input("SELECT * FROM items;") is True
    assert "[E_BAD_ARG]" in capsys.readouterr().out


def test_invalid_initial_physical_mode_is_rejected(tmp_path):
    """非法初值应在进入终端前就失败，不让整场会话静默跑错模式。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    with pytest.raises(SqlError) as caught:
        TerminalSession(runner, plain=True, history=False, physical="fast")
    assert caught.value.code == E_BAD_ARG


def test_physical_completion_offers_only_supported_modes(tmp_path):
    """``/physical`` 参数位应只提示 auto/seq/index 三个合法值。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    completer = SqlCompleter(runner)

    def choices(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert choices("/physical ") == ["auto", "index", "seq"]
    assert choices("/physical s") == ["seq"]


def test_prompt_shows_forced_mode_but_stays_clean_for_auto(tmp_path):
    """强制模式下提示符带模式标记，auto 模式保持原有格式。"""

    runner = Runner(DatabaseServer(tmp_path), parse, parse_script=parse_script)
    auto_session = TerminalSession(runner, plain=True, history=False)
    forced_session = TerminalSession(runner, plain=True, history=False, physical="seq")

    assert auto_session._prompt_label("❯") == "main ❯ "
    assert auto_session._prompt_label(">") == "main> "
    assert forced_session._prompt_label("❯") == "main [seq] ❯ "
    assert forced_session._prompt_label(">") == "main [seq]> "


def test_file_command_inherits_session_physical_mode(tmp_path):
    """``/file`` 必须沿用会话模式，否则强制模式会被文件执行绕过。"""

    sql_file = tmp_path / "no_predicate.sql"
    sql_file.write_text("SELECT * FROM items;\n", encoding="utf-8")
    setup_indexed_table(tmp_path)

    result = run_cli(
        tmp_path,
        sql=f'/physical index\n/file "{sql_file}"\n/quit\n',
    )

    assert result.returncode == 1
    assert "physical = index" in result.stdout
    assert "[E_BAD_ARG]" in result.stdout


def test_cli_physical_flag_reaches_interactive_session(tmp_path):
    """``--physical`` 在 TUI 下不再被静默忽略。"""

    refused = prepare_indexed_table(tmp_path)
    assert refused.returncode == 0, refused.stderr
    assert "id\tnote\n1\ta\n" in refused.stdout

    forced = run_cli(tmp_path, "--physical", "index", sql="SELECT * FROM items;\n/quit\n")
    assert forced.returncode == 1
    assert "[E_BAD_ARG]" in forced.stdout

    pushable = run_cli(
        tmp_path,
        "--physical",
        "index",
        sql="SELECT * FROM items WHERE id = 1;\n/quit\n",
    )
    assert pushable.returncode == 0, pushable.stderr
    assert "id\tnote\n1\ta\n" in pushable.stdout
