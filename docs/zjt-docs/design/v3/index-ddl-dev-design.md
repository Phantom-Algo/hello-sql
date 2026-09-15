# 索引 DDL 的 C 侧设计（V3 · CREATE INDEX / DROP INDEX）

> 版本：v1.0（2026-09-15）
> 归属：C（运行层）。本文只覆盖 F1 在 C 侧的落地：逻辑计划、绑定期校验、执行器与错误归属。
> 上游：[v3-dev-plan.md](../../../v3-dev/v3-dev-plan.md) §4.1 / §4.6 / §5.1、`contracts` 3.0。
> F6 选路与 F7 索引查找不在本文范围。

## 1. 目标与边界

把 `CREATE INDEX` / `DROP INDEX` 从 AST 接到 `BaseStorage.create_index` / `drop_index`，使 v3 计划 §4.6 的错误归属表逐条可测。

本轮做四件事：

1. 新增计划节点 `LogicalCreateIndex` / `LogicalDropIndex`；
2. `LogicalPlanBuilder` 的两个分派分支，并在绑定期确认目标表与列存在；
3. 新增两个 DDL 执行器，接入 `build_ddl_executor` 与 `ExecutorTreeBuilder`；
4. 计划级单测与 SQL 级端到端测试。

本轮不做：UNIQUE 与多列索引（A 直接报 `E_SYNTAX`，C 不可达）；`SHOW INDEXES` 之类新文法；索引的选路与使用（F6/F7）；索引文件格式、维护时机与统计（B）。

`contracts/`、`compiler/`、`storage/` 三个目录一行不改，C 只通过契约方法调用 B。

## 2. 现状与缺口

| 侧 | 现状 | 证据 |
|---|---|---|
| A | 已能解析两种语句，名称一律小写、保留字被拒、`UNIQUE` 报 `E_SYNTAX` | `compiler/parser.py:475`、`compiler/parser.py:540`、`tests/A_tests/test_parser_v3_index_errors.py:27` |
| 契约 | AST 与 Storage 方法已冻结 | `contracts/ast.py:216`、`contracts/ast.py:229`、`contracts/storage.py:132` |
| B | 六个方法已实现：重名 `E_INDEX_EXISTS`、同（表，列）二次建索引也 `E_INDEX_EXISTS`、索引名 `__sys_` 前缀 `E_BAD_ARG` | `storage/__init__.py:583`、`storage/__init__.py:631`、`storage/__init__.py:93` |
| C | **缺口**：`CreateIndexStmt` / `DropIndexStmt` 落到 `assert_never(statement)`，一执行就是 `AssertionError` | `runner/logical_plan/builder.py:108` |

缺口只有这一处：语句能解析、能走到 C，但计划节点与执行器都不存在，绑定期直接落到 `assert_never`。

## 3. 计划节点

插入位置：`runner/logical_plan/plans.py` 的 `LogicalDelete` 之后、`# ---------- 不变式校验辅助 ----------` 之前。

```python
# ---------- 索引 DDL 节点 ----------


@dataclass(frozen=True, slots=True)
class LogicalCreateIndex(LogicalPlan):
    """建索引：叶子节点，目标表与列在绑定期已确认存在。

    是否与既有索引重名属 Storage 的权威状态，本节点不做判定。
    """

    index_name: str
    table: str
    column: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA


@dataclass(frozen=True, slots=True)
class LogicalDropIndex(LogicalPlan):
    """删索引：叶子节点，只带索引名（索引名在库内唯一，不需要表名）。"""

    index_name: str

    @property
    def children(self) -> tuple[LogicalPlan, ...]:
        return ()

    @property
    def output_schema(self) -> LogicalSchema:
        return EMPTY_SCHEMA
```

三点设计取舍：

- **字段与 AST 一一对应，不内嵌 AST 对象**：计划节点是"已绑定的语义"，携带 `CreateIndexStmt` 会把 AST 类型引进执行器与日志；后续若要给节点补充绑定结果（例如列在表中的位置），也只有拆成字段才能加。
- **`children` 为空、`output_schema` 为 `EMPTY_SCHEMA`**：与 `LogicalCreateTable` / `LogicalDropTable` 完全一致；`EMPTY_SCHEMA` 已由 `runner/logical_plan/base.py` 导入进 plans.py，无需新增 import。
- **节点不校验重名**：索引存在性只有 B 能权威判定（v3 计划 §4.6），C 侧放第二份判断会与之漂移。

同时把两个节点补进包导出：`runner/logical_plan/__init__.py` 的 plans import 块（`runner/logical_plan/__init__.py:42`）与 `__all__`（`runner/logical_plan/__init__.py:58`）各加两行，保持该包"计划节点全部可从 `runner.logical_plan` 导入"的既有约定。

## 4. 绑定期校验

### 4.1 分派

插入位置：`runner/logical_plan/builder.py` 的 `build()` 中，紧随 `case DropTableStmt()`（`runner/logical_plan/builder.py:97`）。

```python
            case CreateIndexStmt():
                return self._build_create_index(statement)
            case DropIndexStmt():
                return LogicalDropIndex(index_name=statement.index_name)
```

`DROP INDEX` 不做任何校验，因此直接构造节点，不额外拆方法：C 无法知道索引是否存在，构造删除请求以外的判断都属 B。

### 4.2 建索引的表/列校验

插入位置：`runner/logical_plan/builder.py` 私有构建方法区，紧随 `_build_delete`（`runner/logical_plan/builder.py:298`）。

```python
    def _build_create_index(
        self, statement: CreateIndexStmt
    ) -> LogicalCreateIndex:
        """建索引前确认目标列存在。

        表名合法性与表是否存在由 describe 判定，C 不重复实现标识符规则；
        索引重名不在此判定，那是只有 Storage 才权威的事实。
        """
        table_info = self._describe_table(statement.table)
        if not any(
            column.name == statement.column for column in table_info.columns
        ):
            raise SqlError(
                E_COLUMN_NOT_FOUND,
                f"column not found in table {statement.table}: {statement.column}",
            )
        return LogicalCreateIndex(
            index_name=statement.index_name,
            table=statement.table,
            column=statement.column,
        )
```

校验放在绑定期而非执行器，理由是三条：

1. **与 SELECT 同一条错误通道**：`_build_source` 同样在绑定期用 `describe` 做语义校验，索引 DDL 走同一路径后，追踪器的 `logical_plan` 阶段能统一显示"绑定失败"，不必为 DDL 单独设计执行期错误。
2. **执行器保持零判断**：既有 `CreateTableExecutor` / `DropTableExecutor` 都只做一次 B 调用，新执行器照此办理；读代码时"哪些检查在 C 侧"只需看绑定期。
3. **契约分工的直接落地**：表不存在与列不存在的预检归 C、索引存在性归 B，两边边界清楚。

`E_COLUMN_NOT_FOUND` 的消息文本由 C 决定（沿用 `_scan_source_indexes` 的 `column not found in table {table}: {column}` 措辞）；除错误码外的文本不进契约，测试只断言错误码。

### 4.3 需要补的 import

`runner/logical_plan/builder.py`：

```python
from contracts.ast import (
    ...
    CreateIndexStmt,
    DropIndexStmt,
    ...
)
from contracts.errors import E_COLUMN_NOT_FOUND, E_DUP_TABLE_ALIAS, E_VALUE_COUNT, SqlError
from runner.logical_plan.plans import (
    ...
    LogicalCreateIndex,
    LogicalDropIndex,
    ...
)
```

## 5. 执行器与两处分派

### 5.1 执行器

插入位置：`runner/executor/ddl.py` 的 `DropTableExecutor` 之后；模块文档首行同步改为"消费七种命令计划"，并补一行索引级说明。

```python
# ---------- 索引 DDL ----------


@dataclass(frozen=True, slots=True)
class CreateIndexExecutor(StatementExecutor):
    """建索引：目标表与列已在绑定期确认，重名由 Storage 判定。"""

    index_name: str
    table: str
    column: str

    @trace_runner_operation("runtime", "create_index.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """在当前数据库的目标表上建立单列索引，错误码原样传播。"""

        context.storage.create_index(self.index_name, self.table, self.column)
        return _ddl_success()


@dataclass(frozen=True, slots=True)
class DropIndexExecutor(StatementExecutor):
    """删索引：C 不预检索引是否存在。"""

    index_name: str

    @trace_runner_operation("runtime", "drop_index.execute")
    def execute(self, context: ExecutionContext) -> QueryResult:
        """删除当前数据库中的索引，E_INDEX_NOT_FOUND 原样传播。"""

        context.storage.drop_index(self.index_name)
        return _ddl_success()
```

`context.storage` 是当前数据库的连接，因此 `USE` 之后的索引 DDL 自然作用于新库，无需额外代码。两个执行器都不吞异常：`SqlError` 由 `Runner._execute_statement` 抛给调用方，`execute_script` 的 `stop_on_error` 逻辑不变。

### 5.2 DDL 构建入口

`runner/executor/ddl.py` 的 `DdlPlan` 与 `build_ddl_executor` 同步扩展：

```python
DdlPlan: TypeAlias = (
    LogicalCreateDatabase
    | LogicalDropDatabase
    | LogicalUseDatabase
    | LogicalCreateTable
    | LogicalDropTable
    | LogicalCreateIndex
    | LogicalDropIndex
)


def build_ddl_executor(plan: DdlPlan) -> StatementExecutor:
    """把 DDL 逻辑计划转换为语句级执行器。"""
    match plan:
        ...
        case LogicalCreateIndex():
            return CreateIndexExecutor(plan.index_name, plan.table, plan.column)
        case LogicalDropIndex():
            return DropIndexExecutor(plan.index_name)
        case _:
            assert_never(plan)
```

import 列表（`runner/executor/ddl.py:17`）补 `LogicalCreateIndex` / `LogicalDropIndex`。

### 5.3 ExecutorTreeBuilder

`runner/executor/builder.py` 的 DDL 分派元组补两个成员（`runner/executor/builder.py:75`），import 列表（`runner/executor/builder.py:14`）补同名两项：

```python
            case (
                LogicalCreateDatabase()
                | LogicalDropDatabase()
                | LogicalUseDatabase()
                | LogicalCreateTable()
                | LogicalDropTable()
                | LogicalCreateIndex()
                | LogicalDropIndex()
            ):
                return build_ddl_executor(plan)
```

**`_CATALOG_WRITING_PLANS` 不加入索引 DDL**（`runner/executor/builder.py:32`）：该缓存是 `(库名, 表名) → TableInfo`，索引的增删不改变任何表的列结构，清空缓存只会让后续语句多做一次无谓的 `describe`。F6 若引入索引清单缓存，失效责任落在 F6 自己的缓存上，与这里的表结构缓存无耦合。

### 5.4 优化器

不需要改任何一行。`LogicalOptimizer.optimize()` 只对 `LogicalProjection` 根跑规则，其余根原样返回（`runner/logical_plan/optimizer/optimizer.py:89`），因此新节点既不会被规则改写，也不会因 `optimize` 取值不同而改变行为；`render_plan` 的兜底分支（`runner/logical_plan/optimizer/optimizer.py:160`）把它们显示为类名。这与"`optimize` 只作用于 SELECT"的口径一致。

## 6. C 明确不做的三项检查

写下来是为了避免实现时"顺手再加一层"：

1. **索引是否存在**（`E_INDEX_EXISTS` / `E_INDEX_NOT_FOUND`）：只来自 B。C 不调用 `list_indexes` 做前置判断，也不在 `DROP INDEX` 前查一次。
2. **索引名与表名的合法性**：非法字符由 A 的标识符规则挡在 AST 之外；`__sys_` 保留前缀在 SQL 路径上可达（`parse_identifier` 只做小写化，`compiler/parser.py:210`），由 B 的边界抛 `E_BAD_ARG`（已实测：`CREATE INDEX __sys_i ON events (id)` → `E_BAD_ARG`）。C 侧再实现一套规则只会产生两套判定。
3. **索引键类型是否受支持**：B 支持 `INT / REAL / TEXT / BOOLEAN` 四种（`storage/index.py:78`），与 `SqlType` 全集相同，没有 C 可预检的类型。

## 7. 错误矩阵

`events(id INT)` 已存在时，各语句的最终行为（错误码已实测确认）：

| 语句 | 错误码 | 抛出处 | C 的动作 |
|---|---|---|---|
| `CREATE INDEX i ON events (id)` | — | — | `describe` 通过 → `create_index` |
| `CREATE INDEX i ON missing (id)` | `E_TABLE_NOT_FOUND` | B（`describe`，C 调用） | 不二次判定 |
| `CREATE INDEX i ON __sys_t (id)` | `E_BAD_ARG` | B（`describe`，C 调用） | 不实现标识符规则 |
| `CREATE INDEX i ON events (nope)` | `E_COLUMN_NOT_FOUND` | **C** | 计划不构建 |
| `CREATE INDEX i ON events (id)`，`i` 已存在 | `E_INDEX_EXISTS` | **B** | 不预检 |
| `CREATE INDEX j ON events (id)`，（表，列）已有索引 | `E_INDEX_EXISTS` | **B** | 不预检 |
| `CREATE INDEX __sys_i ON events (id)` | `E_BAD_ARG` | **B** | 不校验标识符 |
| `DROP INDEX i` | — | — | 直接调用 |
| `DROP INDEX nope` | `E_INDEX_NOT_FOUND` | **B** | 不预检 |
| `DROP INDEX __sys_i` | `E_BAD_ARG` | **B** | 不校验标识符 |
| `CREATE UNIQUE INDEX i ON events (id)` | `E_SYNTAX` | **A** | 不可达 |

成功一律返回 `QueryResult(affected_rows=0)`，`columns` 与 `rows` 均为 `None`，与既有 DDL 一致（`runner/executor/ddl.py:27`）。

## 8. 调用序列

```text
SQL: CREATE INDEX idx_events_id ON events (id)
  → A: parse → CreateIndexStmt(index_name="idx_events_id", table="events", column="id")
  → C: LogicalPlanBuilder.build → _build_create_index
         describe("events")          表名非法 E_BAD_ARG / 表不存在 E_TABLE_NOT_FOUND
         列不在 TableInfo.columns    C 抛 E_COLUMN_NOT_FOUND
  → C: LogicalCreateIndex(index_name, table, column)
  → C: LogicalOptimizer.optimize    非 Projection 根，原样返回
  → C: ExecutorTreeBuilder.build → build_ddl_executor → CreateIndexExecutor
  → C: CreateIndexExecutor.execute → context.storage.create_index(...)
  → B: 建索引文件 → 全表灌数据 → 登记系统表     重名 / 同列二次建索引 → E_INDEX_EXISTS
  → C: QueryResult(affected_rows=0)

SQL: DROP INDEX idx_events_id
  → A: parse → DropIndexStmt(index_name="idx_events_id")
  → C: LogicalDropIndex(index_name)
  → C: DropIndexExecutor → context.storage.drop_index(...)
  → B: 摘登记 → 丢缓存 → 删文件                 不存在 → E_INDEX_NOT_FOUND
  → C: QueryResult(affected_rows=0)
```

## 9. 测试方案

### 9.1 计划级：`runner/tests/test_index_ddl_plans.py`

用假的 describe 回调（照 `runner/tests/test_executor_builder.py` 的 `FakeCatalog` 风格）隔开 Storage，覆盖捆绑与分派：

| 用例 | 断言 |
|---|---|
| `test_build_returns_logical_create_index` | 节点类型与三个字段与 AST 一致 |
| `test_build_rejects_missing_column_before_plan` | 抛 `E_COLUMN_NOT_FOUND`，且 describe 被调用一次 |
| `test_build_drop_index_does_not_touch_catalog` | `describe` 调用记录为空 |
| `test_executor_builder_maps_index_ddl_to_executors` | `ExecutorTreeBuilder.build` 返回 `CreateIndexExecutor` / `DropIndexExecutor`，字段正确 |
| `test_create_index_executor_calls_storage_once` | 假 Storage 记录到一次 `create_index(name, table, column)`，返回 `affected_rows == 0` |
| `test_optimizer_passes_index_ddl_through` | `applications == ()`、`rounds == 0`、`optimized is plan` |

执行器用例的关键代码（其余照此写）：

```python
class FakeStorage:
    """只记录索引 DDL 调用，其余契约方法不实现。"""

    def __init__(self) -> None:
        self.index_calls: list[tuple[str, str, str]] = []

    def create_index(self, name: str, table: str, column: str) -> None:
        self.index_calls.append((name, table, column))


def test_create_index_executor_calls_storage_once():
    storage = FakeStorage()
    executor = CreateIndexExecutor("idx_a", "events", "id")

    result = executor.execute(_context(storage))  # 只填 storage 的 ExecutionContext

    assert storage.index_calls == [("idx_a", "events", "id")]
    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None
```

### 9.2 SQL 级：`tests/test_index_ddl_flow.py`

装配 `DatabaseServer` + `Runner`（与 `tests/test_optimizer_equivalence.py` 相同），索引状态用公开的 `list_indexes` 断言，不读存储内部结构：

```python
def _session(tmp_path):
    """建一个含 events 表与一行数据的会话，返回（server, runner）。"""
    server = DatabaseServer(tmp_path)
    runner = Runner(server, parse, parse_script=parse_script)
    runner.execute_script(
        "CREATE TABLE events (id INT, amount REAL, tag TEXT);"
        "INSERT INTO events VALUES (1, 1.5, 'a');"
    )
    return server, runner


def test_create_index_is_visible_through_list_indexes(tmp_path):
    server, runner = _session(tmp_path)
    before = runner.execute("SELECT id, tag FROM events")

    result = runner.execute("CREATE INDEX idx_events_id ON events (id)")

    assert result.affected_rows == 0
    assert result.columns is None and result.rows is None
    indexes = server.connect("main").list_indexes("events")
    assert [(info.name, info.column) for info in indexes] == [("idx_events_id", "id")]
    assert runner.execute("SELECT id, tag FROM events").rows == before.rows
```

用例清单：

| 用例 | 断言 |
|---|---|
| `test_create_index_is_visible_through_list_indexes` | 建索引成功、结果为空结果、`list_indexes` 可见，且随后的 `SELECT` 与建索引前逐行一致 |
| `test_drop_index_removes_it` | 删后 `list_indexes("events") == []` |
| `test_index_ddl_error_codes`（参数化） | §7 表中除成功外的每一行：表不存在 / 表名保留前缀 / 列不存在 / `__sys_` 索引名（建与删）/ 索引不存在 / `UNIQUE` |
| `test_duplicate_index_is_rejected_twice_over` | 同名索引与同（表，列）的第二个索引都报 `E_INDEX_EXISTS` |
| `test_index_ddl_is_unaffected_by_optimize_switch` | `optimize=False` 与 `True` 各建一个索引，两者都存在；DDL 的 `last_optimization_log.applications == ()` |
| `test_index_names_are_scoped_to_the_current_database` | `USE other` 后同名索引可建，旧库索引在 `other` 中 `DROP` 报 `E_INDEX_NOT_FOUND` |
| `test_index_ddl_runs_inside_a_script` | `execute_script("CREATE INDEX ...; DROP INDEX ...;")` 两条都成功，`stopped_early is False` |

索引建好后再跑一条 `SELECT` 属 F7 的验收范围，这里只保证"建索引不影响既有查询结果"，因此把它并入第一条用例。

### 9.3 不新增的测试

- 不改 `tests/golden_sql.py`：那是 V1 的 37 条基线，索引 DDL 由 V3 用例覆盖。
- 不重复 B 的用例：索引与数据一致性、删表级联清理索引、索引查找语义分别是 B 的 V3-T2 / V3-T3，C 侧只在 9.2 补一条端到端 `SELECT`。

## 10. 实施顺序

| 步 | 内容 | 退出条件 |
|---|---|---|
| 1 | `plans.py` 两个节点 + `runner/logical_plan/__init__.py` 导出 | `runner/tests/test_logical_plans.py` 不回归 |
| 2 | `builder.py` 分派与 `_build_create_index` | 9.1 的绑定用例通过 |
| 3 | `ddl.py` 执行器 + `DdlPlan` / `build_ddl_executor` + `executor/builder.py` 分派 | 9.1 全绿 |
| 4 | `tests/test_index_ddl_flow.py` | 9.2 全绿 |
| 5 | 全量回归 | `pytest -q` 不倒退（当前基线 1047 通过） |

## 11. 验收标准

- V3-T1 中属 C 的条目全部通过：建 / 删成功、`list_indexes` 可见、重名 `E_INDEX_EXISTS`、不存在 `E_INDEX_NOT_FOUND`、列不存在 `E_COLUMN_NOT_FOUND`、`CREATE UNIQUE INDEX` 报 `E_SYNTAX`；
- 两种语句的成功结果都是 `affected_rows=0` 且无表头；
- `optimize` 开 / 关下索引 DDL 的行为与最终索引状态一致，`DROP INDEX` 的 `describe` 调用次数为 0；
- 全量回归不倒退，`tests/golden_sql.py` 一字未改；
- 只新增 C 侧内容，`contracts/` / `compiler/` / `storage/` 无改动。
