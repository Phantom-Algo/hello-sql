"""优化器等价性测试（V3-T5）：优化开 / 关两种模式下结果逐条一致。

这是 F5 的一线验收，分三层：
1. golden 全量：V1 的 37 条样例参数化跑双模式；
2. V2 用例：JOIN、BOOLEAN、别名等既有能力跑双模式；
3. 随机小表：随机数据与随机谓词，专抓「规则组合起来才出错」的情况——
   单条规则各自正确、叠加后漏行是本设计最大的残余风险。

双模式对照之外，另加专项断言锁住语义边界（零列来源的行数、恒假与空表的表头、
DML 计划不被改写）。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import SqlError
from contracts.result import QueryResult
from runner import Runner
from runner.logical_plan import LogicalPlanBuilder
from runner.logical_plan.optimizer import LogicalOptimizer
from storage import DatabaseServer
from tests.golden_sql import GOLDEN_SQL


# ---------- 双模式对照辅助 ----------


def _runner(data_dir: Path) -> Runner:
    return Runner(DatabaseServer(data_dir), parse, parse_script=parse_script)


def _snapshot(result: QueryResult) -> tuple:
    """结果的等价性快照：列、行、影响行数逐项一致。"""
    return (result.columns, result.rows, result.affected_rows)


def _run(data_dir: Path, statements: list[str], *, optimize: bool) -> list[tuple]:
    """在独立数据目录上按序执行语句，返回逐条快照；错误取错误码与消息。"""
    runner = _runner(data_dir)
    outcomes: list[tuple] = []
    for sql in statements:
        try:
            outcomes.append(_snapshot(runner.execute(sql, optimize=optimize)))
        except SqlError as error:
            outcomes.append((error.code, error.message))
    return outcomes


def _assert_equivalent(tmp_path: Path, statements: list[str]) -> None:
    """同一批语句在两种模式下逐条比对。"""
    off = _run(tmp_path / "off", statements, optimize=False)
    on = _run(tmp_path / "on", statements, optimize=True)
    assert len(on) == len(off)
    for sql, on_outcome, off_outcome in zip(statements, on, off):
        assert on_outcome == off_outcome, sql


def _plan_of(runner: Runner, sql: str):
    """用 Runner 的会话存储构建语句的逻辑计划。"""
    return LogicalPlanBuilder(runner.describe_table).build(parse(sql))


# ---------- 1. golden 全量 ----------


def test_golden_sql_is_equivalent_in_both_modes(tmp_path: Path) -> None:
    statements = [case["sql"] for case in GOLDEN_SQL]

    _assert_equivalent(tmp_path, statements)


# ---------- 2. V2 用例 ----------

_V2_SETUP = """
CREATE TABLE users  (id INT, name TEXT, active BOOLEAN, email TEXT);
CREATE TABLE orders (id INT, user_id INT, total REAL, cancelled BOOLEAN, notes TEXT);
INSERT INTO users  VALUES (1, 'alice', TRUE,  'a@x');
INSERT INTO users  VALUES (2, 'bob',   TRUE,  'b@x');
INSERT INTO users  VALUES (3, 'carol', FALSE, 'c@x');
INSERT INTO orders VALUES (101, 1,  30.5, FALSE, 'n1');
INSERT INTO orders VALUES (102, 1,  10,   TRUE,  'n2');
INSERT INTO orders VALUES (103, 2,  50,   FALSE, 'n3');
INSERT INTO orders VALUES (104, 3,  99,   FALSE, 'n4');
INSERT INTO orders VALUES (105, 99, 5,    FALSE, 'n5');
"""

_V2_QUERIES = [
    "SELECT * FROM users;",
    "SELECT name FROM users WHERE id = 2;",
    "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total > 20 AND u.active;",
    "SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id;",
    "SELECT * FROM users u JOIN orders o ON u.id = o.user_id WHERE u.active;",
    "SELECT name FROM users WHERE active;",
    "SELECT name FROM users WHERE NOT active;",
    "SELECT name FROM users WHERE NOT (active AND id = 1);",
    "SELECT name FROM users WHERE id = 1 OR id = 3;",
    "SELECT name FROM users WHERE (id = 1 OR id = 2) AND active;",
    "SELECT name FROM users WHERE TRUE;",
    "SELECT name FROM users WHERE 1 = 1;",
    "SELECT name FROM users WHERE id = 99;",
    "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE NOT (o.cancelled);",
    "SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id WHERE u.id = 1 OR o.total = 10;",
    "SELECT u.name, o.notes FROM users u JOIN orders o ON u.id = o.user_id WHERE o.cancelled = FALSE AND u.active = TRUE;",
    "SELECT u.email FROM users u JOIN orders o ON u.active WHERE o.cancelled;",
    "SELECT name FROM users WHERE id < 3 AND name <> 'bob';",
    "SELECT name FROM users WHERE id >= 2;",
    "SELECT u.name, u.id FROM users u JOIN orders o ON u.id = o.user_id WHERE u.id > 1 AND o.total < 100;",
    "SELECT email FROM users WHERE active AND NOT (email = 'a@x');",
    "SELECT a.name, b.name FROM users a JOIN users b ON a.id = b.id WHERE a.active;",
    "SELECT a.email FROM users a JOIN users b ON a.active AND b.active;",
    # ON 里的单侧条件：与写在 WHERE 等价，但要经过规则 4 的 ON 下推路径
    "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id AND u.active;",
    "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id AND o.total > 20;",
    "SELECT u.id FROM users u JOIN orders o ON u.active;",
    "SELECT u.id, o.id FROM users u JOIN orders o ON u.active AND o.cancelled;",
    "SELECT u.name FROM users u JOIN orders o ON u.active WHERE o.total > 5;",
    "SELECT a.name FROM users a JOIN users b ON a.id = b.id AND a.active AND b.active;",
    "SELECT u.id FROM users u JOIN orders o ON u.active AND TRUE;",
    "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id AND NOT u.active;",
]


def test_v2_queries_are_equivalent_in_both_modes(tmp_path: Path) -> None:
    _assert_equivalent(tmp_path, [_V2_SETUP, *_V2_QUERIES])


def test_join_query_returns_expected_rows_without_pushdown(tmp_path: Path) -> None:
    """不下推也正确：优化关闭时的结果本身就是正确的基准（V3 §7.5）。"""
    runner = _runner(tmp_path / "plain")
    runner.execute_script(_V2_SETUP)
    sql = (
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id "
        "WHERE o.total > 20 AND u.active;"
    )

    result = runner.execute(sql, optimize=False)

    assert result.columns == ("u.name", "o.total")
    assert set(result.rows) == {("alice", 30.5), ("bob", 50.0)}


def test_optimizer_actually_rewrites_these_queries(tmp_path: Path) -> None:
    """护栏：上面的对照必须真的在比对「优化过的计划」，而不是两次同样的执行。"""
    runner = _runner(tmp_path / "rewrite")
    runner.execute_script(_V2_SETUP)
    optimizer = LogicalOptimizer()
    queries = [
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total > 20 AND u.active;",
        "SELECT name FROM users WHERE id = 2;",
        "SELECT name FROM users WHERE NOT (active AND id = 1);",
    ]

    for sql in queries:
        log = optimizer.optimize(_plan_of(runner, sql))
        assert log.optimized != log.original, sql
        assert log.applications, sql


# ---------- 3. 专项风险用例 ----------


def test_zero_column_source_keeps_row_count(tmp_path: Path) -> None:
    """零列来源：orders 一列都不被引用，但它决定 JOIN 的产生行数（2 个 active 用户 × 5 张订单）。"""
    runner = _runner(tmp_path / "zero_column")
    runner.execute_script(_V2_SETUP)
    sql = "SELECT u.id FROM users u JOIN orders o ON u.active;"

    optimized = runner.execute(sql, optimize=True)
    plain = runner.execute(sql, optimize=False)

    assert len(optimized.rows) == len(plain.rows) == 10


def test_false_predicate_and_empty_tables_keep_header(tmp_path: Path) -> None:
    """恒假谓词与空表：两种模式都返回「有表头、零行」。"""
    setup = """
    CREATE TABLE users (id INT, name TEXT, active BOOLEAN, email TEXT);
    CREATE TABLE empty_table (id INT, name TEXT);
    INSERT INTO users VALUES (1, 'alice', TRUE, 'a@x');
    """
    queries = [
        "SELECT * FROM users WHERE FALSE;",
        "SELECT name FROM users WHERE 1 = 2;",
        "SELECT id, name FROM empty_table;",
        "SELECT * FROM empty_table WHERE id = 1;",
        "SELECT id FROM users WHERE id = 99;",
        "SELECT u.id FROM users u JOIN empty_table e ON u.active;",
    ]
    runner_on = _runner(tmp_path / "on")
    runner_off = _runner(tmp_path / "off")
    runner_on.execute_script(setup, optimize=True)
    runner_off.execute_script(setup, optimize=False)

    for sql in queries:
        with_subtest = runner_on.execute(sql, optimize=True)
        without = runner_off.execute(sql, optimize=False)
        assert with_subtest == without, sql
        assert with_subtest.rows == (), sql
        assert with_subtest.columns, sql


def test_false_predicate_over_join_returns_no_rows(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "false_join")
    runner.execute_script(_V2_SETUP)
    sql = "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE u.id = 1 AND FALSE;"

    optimized = runner.execute(sql, optimize=True)

    assert optimized.columns == ("u.name", "o.total")
    assert optimized.rows == ()


def test_dml_plans_are_not_rewritten_by_the_switch(tmp_path: Path) -> None:
    """UPDATE / DELETE 的 child 用输出行做整行替换，列序不能被改写（§3.3）。"""
    runner = _runner(tmp_path / "dml")
    runner.execute_script(_V2_SETUP)
    optimizer = LogicalOptimizer()

    for sql in (
        "UPDATE users SET name = 'zed' WHERE id = 1;",
        "DELETE FROM users WHERE active AND id > 1;",
    ):
        plan = _plan_of(runner, sql)
        log = optimizer.optimize(plan)
        assert log.optimized is plan, sql
        assert log.applications == (), sql


def test_dml_results_are_equivalent_in_both_modes(tmp_path: Path) -> None:
    statements = [
        _V2_SETUP,
        "UPDATE users SET name = 'zed', active = FALSE WHERE id = 1;",
        "SELECT * FROM users;",
        "DELETE FROM users WHERE active = TRUE;",
        "SELECT id, name, active FROM users;",
        "UPDATE users SET email = 'x@y' WHERE FALSE;",
        "DELETE FROM orders WHERE user_id = 99;",
        "SELECT id, total FROM orders;",
    ]

    _assert_equivalent(tmp_path, statements)


# ---------- 4. 随机小表 ----------


def _bool(rng: random.Random) -> str:
    return "TRUE" if rng.random() < 0.5 else "FALSE"


def _atom(rng: random.Random, qualifiers: tuple[str, ...]) -> str:
    """一个不可再分的布尔原子：布尔列，或整型列与字面量的比较。"""
    qualifier = rng.choice(qualifiers)
    column = rng.choice(("id", "x", "flag"))
    if column == "flag":
        return f"{qualifier}.flag"
    op = rng.choice(("=", "<>", "<", "<=", ">", ">="))
    return f"{qualifier}.{column} {op} {rng.randrange(0, 5)}"


def _condition(rng: random.Random, qualifiers: tuple[str, ...], depth: int = 0) -> str:
    """随机谓词：比较、布尔列、AND / OR / NOT 的组合。"""
    roll = rng.random()
    if depth < 2 and roll < 0.3:
        op = rng.choice((" AND ", " OR "))
        return (
            f"({_condition(rng, qualifiers, depth + 1)}"
            f"{op}{_condition(rng, qualifiers, depth + 1)})"
        )
    if depth < 2 and roll < 0.45:
        return f"NOT ({_condition(rng, qualifiers, depth + 1)})"
    return _atom(rng, qualifiers)


def _cross_on(rng: random.Random) -> str:
    """跨两侧的整型比较。"""
    return f"a.{rng.choice(('id', 'x'))} = b.{rng.choice(('id', 'x'))}"


def _single_side_on(rng: random.Random) -> str:
    """只引用一侧的条件：ON 下推的候选。"""
    qualifier = rng.choice(("a", "b"))
    roll = rng.random()
    if roll < 0.3:
        return f"{qualifier}.flag"
    if roll < 0.5:
        return f"NOT {qualifier}.flag"
    op = rng.choice(("=", "<>", "<", "<=", ">", ">="))
    return f"{qualifier}.{rng.choice(('id', 'x'))} {op} {rng.randrange(0, 4)}"


def _on_condition(rng: random.Random) -> str:
    """JOIN 的 ON：单侧条件、跨侧比较，以及两者的 AND 组合。

    单侧项走的是规则 4 的 ON 下推路径，AND 组合则同时覆盖「推走单侧、留下跨侧」。
    """
    roll = rng.random()
    if roll < 0.25:
        return _single_side_on(rng)
    if roll < 0.35:
        return rng.choice(("TRUE", "1 = 1", "FALSE"))
    cross = _cross_on(rng)
    if roll < 0.55:
        return cross
    if roll < 0.75:
        return f"{cross} AND {_single_side_on(rng)}"
    return f"({_atom(rng, ('a',))} OR {_atom(rng, ('b',))})"


def _random_statements(rng: random.Random) -> list[str]:
    """随机建三张小表、随机插数据、随机生成查询。"""
    statements = [
        f"CREATE TABLE {table} (id INT, x INT, flag BOOLEAN);"
        for table in ("a", "b", "c")
    ]
    for table in ("a", "b", "c"):
        for row in range(rng.randrange(0, 5)):
            statements.append(
                f"INSERT INTO {table} VALUES "
                f"({row}, {rng.randrange(0, 4)}, {_bool(rng)});"
            )
    for _ in range(12):
        statements.append(_random_query(rng))
    return statements


def _random_query(rng: random.Random) -> str:
    if rng.random() < 0.25:
        return f"SELECT * FROM a WHERE {_condition(rng, ('a',))};"
    if rng.random() < 0.3:
        # 三表左深链：连续两层下推与裁剪都要正确
        select = rng.choice(("*", "a.id", "c.id", "a.x, b.x, c.x", "a.id, c.flag"))
        sql = (
            f"SELECT {select} FROM a JOIN b ON {_on_condition(rng)}"
            f" JOIN c ON {_on_condition(rng)}"
        )
        qualifiers = ("a", "b", "c")
    else:
        if rng.random() < 0.3:
            # 只取一侧的列：覆盖「另一侧需求为空」的裁剪边界
            select = rng.choice(("a.id", "a.x", "b.id", "b.x, a.flag"))
        else:
            select = rng.choice(("*", "a.id, b.id", "a.x, b.x", "a.flag, b.flag", "b.id"))
        sql = f"SELECT {select} FROM a JOIN b ON {_on_condition(rng)}"
        qualifiers = ("a", "b")
    roll = rng.random()
    if roll < 0.8:
        sql += f" WHERE {_condition(rng, qualifiers)}"
    elif roll < 0.9:
        # 常量谓词：覆盖折叠 + 恒真恒假 + 冗余节点消除
        sql += rng.choice((" WHERE (1 = 1)", " WHERE (1 = 2)", " WHERE TRUE", " WHERE FALSE"))
    return sql + ";"


@pytest.mark.parametrize("seed", range(12))
def test_random_small_tables_are_equivalent(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    statements = _random_statements(rng)

    _assert_equivalent(tmp_path, statements)
