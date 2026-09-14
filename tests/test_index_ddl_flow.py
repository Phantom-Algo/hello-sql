"""索引 DDL 的 SQL 级端到端测试：真实 Parser + Runner + Storage。

本文件是 F1 在 C 侧的验收：只从 SQL 文本出发，用公开的 list_indexes
观察索引状态，不读存储内部结构；错误只断言错误码，文本不进契约。
建索引后再跑 SELECT 只用于确认「建索引不影响既有查询结果」，索引选路
与查找语义属 F6/F7。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import (
    E_BAD_ARG,
    E_COLUMN_NOT_FOUND,
    E_INDEX_EXISTS,
    E_INDEX_NOT_FOUND,
    E_SYNTAX,
    E_TABLE_NOT_FOUND,
    SqlError,
)
from runner import Runner
from storage import DatabaseServer


SETUP_SQL = """
CREATE TABLE events (id INT, amount REAL, tag TEXT);
INSERT INTO events VALUES (1, 1.5, 'a');
"""


def _session(tmp_path: Path) -> tuple[DatabaseServer, Runner]:
    """建一个含 events 表与一行数据的会话，返回（server, runner）。"""
    server = DatabaseServer(tmp_path)
    runner = Runner(server, parse, parse_script=parse_script)
    runner.execute_script(SETUP_SQL)
    return server, runner


def _index_names(server: DatabaseServer, table: str = "events") -> list[str]:
    """从当前库读出某表的索引名，按 list_indexes 的稳定顺序返回。"""
    return [info.name for info in server.connect("main").list_indexes(table)]


def test_create_index_is_visible_through_list_indexes(tmp_path: Path) -> None:
    server, runner = _session(tmp_path)
    before = runner.execute("SELECT id, tag FROM events")

    result = runner.execute("CREATE INDEX idx_events_id ON events (id)")

    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None
    indexes = server.connect("main").list_indexes("events")
    assert [(info.name, info.column) for info in indexes] == [("idx_events_id", "id")]
    assert runner.execute("SELECT id, tag FROM events").rows == before.rows


def test_drop_index_removes_it(tmp_path: Path) -> None:
    server, runner = _session(tmp_path)
    runner.execute("CREATE INDEX idx_events_id ON events (id)")

    result = runner.execute("DROP INDEX idx_events_id")

    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None
    assert server.connect("main").list_indexes("events") == []


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("CREATE INDEX i ON missing (id)", E_TABLE_NOT_FOUND),
        ("CREATE INDEX i ON __sys_t (id)", E_BAD_ARG),
        ("CREATE INDEX i ON events (nope)", E_COLUMN_NOT_FOUND),
        ("CREATE INDEX __sys_i ON events (id)", E_BAD_ARG),
        ("DROP INDEX __sys_i", E_BAD_ARG),
        ("DROP INDEX nope", E_INDEX_NOT_FOUND),
        ("CREATE UNIQUE INDEX i ON events (id)", E_SYNTAX),
    ],
)
def test_index_ddl_error_codes(tmp_path: Path, sql: str, code: str) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as error:
        runner.execute(sql)

    assert error.value.code == code


def test_duplicate_index_is_rejected_twice_over(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    runner.execute("CREATE INDEX idx_events_id ON events (id)")

    # 同名索引与同（表，列）的第二个索引都由 Storage 判定重名
    for sql in (
        "CREATE INDEX idx_events_id ON events (id)",
        "CREATE INDEX idx_events_amount ON events (id)",
    ):
        with pytest.raises(SqlError) as error:
            runner.execute(sql)
        assert error.value.code == E_INDEX_EXISTS


def test_index_ddl_is_unaffected_by_optimize_switch(tmp_path: Path) -> None:
    server, runner = _session(tmp_path)

    off = runner.execute(
        "CREATE INDEX idx_events_off ON events (id)", optimize=False
    )
    on = runner.execute("CREATE INDEX idx_events_on ON events (amount)")

    assert off.affected_rows == 0 and on.affected_rows == 0
    assert sorted(_index_names(server)) == ["idx_events_off", "idx_events_on"]
    # DDL 不是 Projection 根，优化器原样返回，不产生任何规则应用
    assert runner.last_optimization_log is not None
    assert runner.last_optimization_log.applications == ()


def test_index_names_are_scoped_to_the_current_database(tmp_path: Path) -> None:
    server, runner = _session(tmp_path)
    runner.execute("CREATE INDEX idx_events_id ON events (id)")

    runner.execute_script(
        "CREATE DATABASE other; USE other;"
        "CREATE TABLE events (id INT);"
    )

    # main 库中的同名索引在 other 中不可见，删除时报不存在
    with pytest.raises(SqlError) as error:
        runner.execute("DROP INDEX idx_events_id")
    assert error.value.code == E_INDEX_NOT_FOUND

    # 索引名只在所属数据库内唯一，因此 other 中可建同名索引
    created_in_other = runner.execute("CREATE INDEX idx_events_id ON events (id)")
    assert created_in_other.affected_rows == 0
    assert [info.name for info in server.connect("other").list_indexes()] == [
        "idx_events_id"
    ]

    runner.execute("USE main")
    assert _index_names(server) == ["idx_events_id"]


def test_index_ddl_runs_inside_a_script(tmp_path: Path) -> None:
    server, runner = _session(tmp_path)

    result = runner.execute_script(
        "CREATE INDEX idx_events_id ON events (id); DROP INDEX idx_events_id;"
    )

    assert result.stopped_early is False
    assert len(result.statements) == 2
    assert all(statement.error is None for statement in result.statements)
    assert [statement.result.affected_rows for statement in result.statements] == [0, 0]
    assert server.connect("main").list_indexes("events") == []
