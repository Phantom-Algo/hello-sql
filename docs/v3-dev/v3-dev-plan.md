# hello-sql V3 设计文档（优化与索引）

> 版本：v3.0-draft（2026-09-12）
> 定位：V3 的公共设计依据。本文只定义**功能范围、公共契约与数据流**；
> 各模块的内部实现方案（索引文件布局、统计采集方式、代价公式与参数）
> 由对应负责人自行设计，并在各自的设计文档中定稿，本文不做规定。
> 上游：[contract-v1.md](../contract-v1.md)（V1.1 历史冻结稿）、
> [v2-dev-plan.md](../v2-dev/v2-dev-plan.md)（V2）、`contracts` 3.0。
> 与全链路追踪的关系：追踪 / Inspector 窗口由其他同学实现，本文不重复建设，
> 只要求三个模块按其既有追踪口径上报事件。

## 1. 目标

V2 让 hello-sql 能跑通复杂 SQL；V3 让它**自己挑最省的路走，并且拿得出证据**。

1. 规则型逻辑优化器把计划规范成可分析形态；
2. 索引 + 统计 + 代价选路，使查询在有索引时更快、在索引不划算时主动放弃；
3. 一套可复现的基准，用数据证明升级后比升级前更有效。

## 2. 范围

### 2.1 本版要做

| 编号 | 功能 | 归属 | 一句话定义 |
|---|---|---|---|
| F1 | 索引 DDL 文法 | A | 支持 `CREATE INDEX` / `DROP INDEX` 的解析与 AST |
| F2 | B+ 树单列索引 | B | 建索引、删索引、列出索引，并与表数据保持一致 |
| F3 | 索引查找能力 | B | 按（表，列，键值或键区间）返回匹配行；不接收操作符 |
| F4 | 统计信息 | B | 返回表的行数、数据页数与列级统计 |
| F5 | 规则型逻辑优化器 | C | 在不改变结果的前提下改写逻辑计划，可整体开关 |
| F6 | 代价选路与强制模式 | C | 依统计与索引清单选择物理路径，并支持强制指定 |
| F7 | 索引感知的谓词下推 | C | 把可下推的条件交给索引访问，其余留在上层求值 |
| F8 | 基准与对比报告 | bench（B） | 三种模式跑同一批查询，产出可复现的对比数据 |

### 2.2 本版不做

- UNIQUE 索引、多列组合索引、索引覆盖扫描；
- ANALYZE 语句（统计自动维护，不新增 SQL 文法）；
- JOIN 重排、代价驱动的 JOIN 算法选择；
- WAL、崩溃恢复、事务；
- NULL、ORDER BY、GROUP BY、聚合。

## 3. 已拍板决策

| 编号 | 主题 | 结论 |
|---|---|---|
| DV3-01 | 契约版本 | `contracts.__version__` 升为 `3.0`，写入 V3 设计文档 |
| DV3-02 | 新增错误码 | 只加 `E_INDEX_EXISTS`、`E_INDEX_NOT_FOUND` |
| DV3-03 | 索引列不存在 | 复用既有 `E_COLUMN_NOT_FOUND`，不新增错误码 |
| DV3-04 | 强制物理模式 | `Runner.execute(..., physical="auto"|"seq"|"index")` 作为正式 API，默认 `auto` |
| DV3-05 | bench 的装配地位 | 允许 `bench/` 与 `main.py` 一样同时 import 三家；其余模块红线不变 |
| DV3-06 | UNIQUE | 本轮 `CREATE UNIQUE INDEX` 直接报 `E_SYNTAX`，AST 不预留字段 |
| DV3-07 | 统计可用性 | 任何存在的表都返回 `TableStats`；仅表不存在报 `E_TABLE_NOT_FOUND` |
| DV3-08 | 空表语义 | 返回 `row_count=0`、`page_count=0`、列 `distinct_count=0`、`min/max=None` |
| DV3-09 | `page_count` 定义 | 只计数据页，不含页 0、空闲页、溢出链页 |
| DV3-10 | bench 负责人 | 由你负责；实现放在最后，允许先搭空壳 |
| DV3-11 | 索引接口不带运算符 | B 只提供 `index_lookup` / `index_range`（键值 / 键区间）；比较语义与值归纳由 C 负责，避免 B 重实现一套比较规则 |
| DV3-12 | 统计字段精度（契约 3.1） | `ColumnStats.min_value` / `max_value` 是**精确边界**（B 保证，C 可据此推出"越界即 0 行"）；`distinct_count` 仍是近似值（B 的采样策略决定，C 不得假设精确） |

## 4. 公共契约

### 4.1 `contracts/ast.py`（A 维护）

```python
@dataclass(frozen=True)
class CreateIndexStmt:
    index_name: str
    table: str
    column: str


@dataclass(frozen=True)
class DropIndexStmt:
    index_name: str
```

`Statement` 联合类型加入这两个节点。索引名、表名、列名沿用现有标识符规则，
进入 AST 前统一小写。

### 4.2 `contracts/storage.py`（B 实现，C 只读消费）

```python
@dataclass(frozen=True)
class IndexInfo:
    name: str
    table: str
    column: str


@dataclass(frozen=True)
class ColumnStats:
    name: str
    distinct_count: int
    min_value: Value | None
    max_value: Value | None


@dataclass(frozen=True)
class TableStats:
    table: str
    row_count: int
    page_count: int
    columns: tuple[ColumnStats, ...]
```

`BaseStorage` 协议新增六个方法：

```python
def create_index(self, name: str, table: str, column: str) -> None: ...
def drop_index(self, name: str) -> None: ...
def list_indexes(self, table: str | None = None) -> list[IndexInfo]: ...
def statistics(self, table: str) -> TableStats: ...
def index_lookup(self, table: str, column: str, key: Value) -> Iterator[Row]: ...
def index_range(
    self,
    table: str,
    column: str,
    lower: Value | None,
    upper: Value | None,
    *,
    lower_inclusive: bool = True,
    upper_inclusive: bool = True,
) -> Iterator[Row]: ...
```

契约级语义：

- 索引名在**同一数据库内唯一**；与表名、列名共用标识符规则。
- 六个新方法对表名沿用既有规则：非法标识符与 `__sys_` 前缀 → `E_BAD_ARG`；
  `statistics` / `index_lookup` / `index_range` 只针对用户表。
- 索引查找返回的 `Row` 形状必须与 `scan()` 完全一致（`row_id` + 按建表列顺序的值），使 C 侧复用同一条 ExecRow 管线。
- **比较语义属 C，不属 B**：B 的索引接口不接收操作符。C 负责把比较运算符翻译成键值或键区间，并负责把值归纳到列的类型（例如 INT 列收到 `1.0` 时先归一为 `1`）；B 只按列类型收值，类型不符沿用 `E_TYPE_MISMATCH`。
- `index_range` 的 `lower` / `upper` 为 `None` 表示该侧无界，默认闭区间；端点只允许按关键字传入，避免位置参数写错。
- **索引键序必须与 C 的比较语义一致**：等值查找能命中的键，必须是 C 认为"相等"的值；区间查找返回的键，必须落在 C 所理解的区间内。
- `index_lookup` / `index_range` 在（表，列）上没有对应索引时 → `E_INDEX_NOT_FOUND`。
- `list_indexes()` 不带表名时返回该库全部索引，顺序稳定。
- `TableStats.columns` 必须按建表列顺序**完整**包含该表的全部用户列；缺列会让 C 静默退化估算，属契约违反。
- **字段精度不同（契约 3.1 / DV3-12）**：`min_value` / `max_value` 是**精确边界**——
  B 必须保证它不窄于真实范围，C 才可以直接用"键越界 ⇒ 命中 0 行"这一推论；
  `distinct_count` 仍是**近似值**（B 的有界采样决定），C 只能当估算输入。
  这条区分的由来：采样前缀曾让大表的 `max` 远低于真实值，C 据此把"其实命中
  几十行"的区间估成 0 行并选中索引，实测比顺序扫描慢 18 倍。

### 4.3 `contracts/errors.py`

```text
E_INDEX_EXISTS     建索引时索引名已存在（B 抛）
E_INDEX_NOT_FOUND  索引不存在：删索引、索引查找找不到对应索引、
                   physical="index" 但无可用索引（B 抛）
```

其余错误沿用既有归属：表不存在 `E_TABLE_NOT_FOUND`，索引列不存在
`E_COLUMN_NOT_FOUND`，非法标识符 `E_BAD_ARG`，`UNIQUE` 报 `E_SYNTAX`。

### 4.4 C 的公开 API（不进 contracts，但属于跨模块可见行为）

```python
def execute(
    self,
    sql: str,
    *,
    physical: Literal["auto", "seq", "index"] = "auto",
) -> QueryResult: ...
```

默认参数保证既有调用零影响。`execute_script` / `execute_file` 提供同名关键字参数
并透传给每条语句，供基准工具统一强制物理模式。

### 4.5 追踪契约

不新增任何追踪契约。A/B/C 只向其既有的 `TraceSink` 口径上报事件：

- A：Token、Parser 过程、AST、SourceSpan；
- B：Catalog 查询、页读写、缓存命中、记录编解码、索引读写；
- C：绑定、逻辑计划、优化规则、选路结果、执行统计。

优化器阶段从当前实现的 `OFF` 变为有内容，是 V3 对追踪器的唯一增量。

### 4.6 新增方法的失败码与责任方

"谁抛"遵循既有约定：**C 能预检的语义错误由 C 抛，B 在自己的方法边界同码防御；
只有 B 才是权威的事实（如索引是否已存在）由 B 抛。**

| 方法 | 失败码 | 谁抛 | 触发条件 |
|---|---|---|---|
| `create_index` | `E_BAD_ARG` | B（边界） | 索引名 / 表名 / 列名非法或以 `__sys_` 开头 |
| `create_index` | `E_TABLE_NOT_FOUND` | C 预检，B 边界同码 | 目标表不存在 |
| `create_index` | `E_COLUMN_NOT_FOUND` | C 预检，B 边界同码 | 目标列不存在 |
| `create_index` | `E_INDEX_EXISTS` | **B** | 索引名已存在；只有 B 能原子判定，C 不预检 |
| `drop_index` | `E_BAD_ARG` | B（边界） | 索引名非法 |
| `drop_index` | `E_INDEX_NOT_FOUND` | **B** | 索引不存在 |
| `list_indexes` | `E_BAD_ARG` | B（边界） | 表名非法或以 `__sys_` 开头 |
| `list_indexes` | `E_TABLE_NOT_FOUND` | B | 传入了表名但该表不存在 |
| `statistics` | `E_BAD_ARG` | B（边界） | 表名非法或以 `__sys_` 开头 |
| `statistics` | `E_TABLE_NOT_FOUND` | B 边界（C 通常已由 `describe` 预检） | 表不存在 |
| `index_lookup` / `index_range` | `E_INDEX_NOT_FOUND` | **B** | 该（表，列）上没有索引 |
| `index_lookup` / `index_range` | `E_TYPE_MISMATCH` | B | 键值类型与列类型不符（C 应先完成值归纳） |
| `index_lookup` / `index_range` | `E_TABLE_NOT_FOUND` | B 边界同码 | 表不存在 |
| `index_lookup` / `index_range` | `E_BAD_ARG` | B（边界） | 表名或列名非法 |

**索引存在性一律由 B 判定**：C 不维护第二份"有没有索引"的判断。`E_INDEX_EXISTS`
与 `E_INDEX_NOT_FOUND` 只可能来自 B。

## 5. 数据流与交接

### 5.1 建索引

```text
SQL: CREATE INDEX idx_events_id ON events (id)
  → A: tokenize / parse → CreateIndexStmt(index_name, table, column)   [A → C]
  → C: describe(events) 校验表存在、列存在与类型                        [C → B]
  → C: 计划与执行（表/列/重名等语义校验在 C 与 B 两侧按既有分工完成）
  → B: create_index(name, table, column)                              [C → B]
  → 成功返回 None；失败抛 E_INDEX_EXISTS / E_TABLE_NOT_FOUND /
    E_COLUMN_NOT_FOUND / E_BAD_ARG
```

### 5.2 带索引的查询

```text
SQL: SELECT * FROM events WHERE id = 4242 AND amount > 100
  → A: SelectStmt                                                      [A → C]
  → C: describe(events) → TableInfo                                    [B → C]
       statistics(events) → TableStats                                 [B → C]
       list_indexes(events) → tuple[IndexInfo, ...]                    [B → C]
  → C: 规则优化（F5）→ 代价选路（F6）→ 谓词下推（F7）
  → C: 若选中索引路径，把 id = 4242 翻译成键值，调用
       index_lookup(events, id, 4242)                                   [C → B]
  → B: 返回 Row 迭代器                                                  [B → C]
  → C: 残余条件求值 → Projection → QueryResult
```

关键点：**B 只负责"返回满足索引条件的行"，残余条件仍在 C 侧求值。**
B 不认识 SQL 表达式，红线不破。

### 5.3 强制物理模式

```text
Runner.execute(sql, physical="seq")     → 跳过选路，强制顺序扫描
Runner.execute(sql, physical="index")   → 跳过选路，强制索引访问
Runner.execute(sql, physical="auto")    → 依统计与索引清单自行选择（默认）
```

强制模式只影响选路，不影响结果正确性。判定机制如下：

- **C 在强制模式下不判断索引是否存在**：只要能把谓词翻译成（表，列，键值或
  键区间），就直接调用 B 的索引接口；有索引则执行，没有则由 **B 抛
  `E_INDEX_NOT_FOUND`**。`list_indexes` 只服务 `auto` 模式的选路，不参与强制
  模式的判定，因此索引错误的归属在所有路径下完全一致。
- **唯一由 C 抛的情形**：整条查询没有任何可翻译成索引访问的条件（例如
  `SELECT * FROM t`，或谓词所在列无法形成键值 / 键区间），C 无法构造索引请求，
  按调用参数对该查询无效处理，抛 `E_BAD_ARG`。
- **不得静默退化为顺序扫描**——否则基准会把"没走索引"误判成"走了索引"，
  对比数据失去意义。

### 5.4 交接点汇总

| 交接 | 携带数据 | 契约位置 |
|---|---|---|
| A → C | `CreateIndexStmt` / `DropIndexStmt` / `SelectStmt` 等 AST | `contracts/ast.py` |
| C → B | `describe` / `scan` / `insert` / `update_row` / `delete_row` | `contracts/storage.py` |
| C → B | `create_index` / `drop_index` / `list_indexes` / `statistics` / `index_lookup` / `index_range` | `contracts/storage.py` |
| B → C | `TableInfo` / `Row` / `RowId` / `IndexInfo` / `TableStats` / `ColumnStats` | `contracts/storage.py` |
| C → 追踪器 | 计划、规则日志、选路结果、执行统计 | 追踪器既有契约 |
| bench → 仓库 | 对比表与报告 | `docs/v3-dev/benchmark-report.md` |

## 6. 模块分工

| 模块 | 新增内容 | 对外公开接口 | 消费什么 | 明确不做 | 依赖 |
|---|---|---|---|---|---|
| A 编译 | F1：索引 DDL 文法与 AST | `parse` / `parse_script` | 无 | 不查表、不查列、不认识索引存储 | 无，契约冻结后即可开工 |
| B 存储 | F2/F3/F4：索引、索引查找、统计 | `BaseStorage` 新增 6 方法 | 表名、列名、索引名、键值、键区间 | 不认识 SQL 与比较运算符、不做选路 | 契约冻结后即可开工 |
| C 运行 | F5/F6/F7：优化器、选路、下推、执行器 | `Runner.execute(..., physical=...)` | `describe` / `statistics` / `list_indexes` / `index_lookup` / `index_range` | 不碰索引文件格式、不直接 import storage 实现 | 需要 A 的 AST 与 B 的接口 |
| bench | F8：基准与报告 | 三家公开入口 | 全部 | 不绕过公开接口、不直接读写内部结构 | 全部就绪（可先搭空壳） |

三方互不 import 的红线保持不变；装配层为 `main.py` 与 `bench/`。

## 7. 契约级语义要求（不规定实现方式）

以下每条都是可测的对外语义；里面怎么写由负责人定。

### 7.1 统计信息

- 契约要求：任何**存在**的表都返回 `TableStats`；表不存在报 `E_TABLE_NOT_FOUND`。
- 空表按 DV3-08 返回零值；`page_count` 按 DV3-09 只计数据页。
- 契约要求：规划期调用只读，不产生写盘；返回的统计不得早于该表最近一次已完成的写操作。
- 契约要求（3.1）：`min_value` / `max_value` 精确；`distinct_count` 近似。

> 备注（给 B）：采集与更新策略（增量 / 惰性 / 采样）归你定；请在设计文档中写明
> 一致性口径，因为 C 会据此选路。
> B 的落地口径（D47/D48）：极值精确——插入 O(1) 拓宽，删空极值持有者才在下次
> `statistics()` 做一趟只读全扫重建基线（进程内一次性）；基数仍有界采样。
> **因此 `statistics()` 的首次调用可能读满该表的数据页**，规划期虽仍只读，
> 但基准工具必须按 §7.4 的口径预热后再计数。
> 备注（给 C）：估算器需要能处理空表、`distinct_count=0`、`min=max` 三种退化输入。

### 7.2 索引与索引扫描

- 契约要求：任何完成的写入之后，索引与表数据保持一致；删表时索引一并清理。
- 契约要求：索引查找的结果与 `scan` 过滤后一致，且 `Row` 形状相同。
- 契约要求：索引键序与 C 的比较语义一致；C 负责值归纳，B 只按列类型收值。

> 备注（给 B）：索引文件布局、页复用、维护时机（同步 / 延迟）由你定；
> 契约只承诺"一致"与"形状相同"这两条可测语义。

### 7.3 逻辑优化器

- 契约要求：优化前后 `QueryResult` 逐条等价。
- 契约要求：优化器可整体开关，用于基准的"无优化器"基线。
- 契约要求：规则的应用过程可解释（供追踪器展示）。

> 备注（给 C）：规则集合与顺序由你定，V2 计划书 §5.2 的候选清单可直接参考。

### 7.4 代价选路

- 契约要求：`auto` 下选择结果可解释；拿不到统计或没有可用索引时安全退化为顺序扫描。
- 契约要求：`seq` / `index` 强制模式只影响选路，不影响结果正确性。

> 备注（给 C）：代价模型的形式、参数、阈值与标定方法由你设计并在你的设计文档中定稿，
> 本文不预设。只提一条：若把参数固化为常量，请在注释中指向标定依据。

### 7.5 谓词下推

- 契约要求：下推是优化而非语义改变——不下推也必须结果正确。
- 契约要求：C 负责把比较运算符翻译成键值或键区间后再调用 B；B 的接口不出现操作符。

> 备注（给 C）：哪些谓词"可下推"由你定；请用测试兜住"不下推不漏行"。

## 8. 验收标准

| 编号 | 用例 | 验证点 |
|---|---|---|
| V3-T1 | 索引 DDL | 建/删/列出；重名 `E_INDEX_EXISTS`；不存在 `E_INDEX_NOT_FOUND`；列不存在 `E_COLUMN_NOT_FOUND`；`CREATE UNIQUE INDEX` 报 `E_SYNTAX` |
| V3-T2 | 索引一致性 | insert/update/delete 之后索引与表数据一致；删表后索引消失 |
| V3-T3 | 索引查找语义 | `index_lookup` / `index_range` 结果与 `scan` 过滤结果逐行一致；`Row` 形状相同；`None` 端点表示无界；无索引报 `E_INDEX_NOT_FOUND` |
| V3-T4 | 统计语义 | 有/无表；空表零值；`page_count` 口径；`columns` 完整且按建表列顺序；DML 后统计不早于最近一次写入 |
| V3-T5 | 优化等价 | 同一批查询在优化器开 / 关两种模式下结果逐条一致 |
| V3-T6 | 选路正确 | 高选择性走索引、低选择性放弃索引；无统计、无索引时安全退化 |
| V3-T7 | 强制模式 | `seq` / `index` / `auto` 三种模式结果逐条一致 |
| V3-T8 | 基准报告 | 三模式对比表产出，含页读取次数与加速比；报告文件进仓库 |
| V3-T9 | 回归 | V1 37 条 golden 与 V2 全部用例不倒退 |

等价性的责任划分：**模块内的"优化开 / 关结果一致"由 C 主责**（V3-T5、V3-T7）；
**bench 只做端到端复核**（V3-T8），不承担等价性的一线责任——避免出现"两边都
以为对方测了"。

## 9. 里程碑与依赖顺序

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| M0 | 契约冻结：本文经三方确认，`contracts` 升 3.0 | ✅ 已落地：`contracts/` 四个文件同步完成，形状由 `tests/test_v3_contract.py` 锁定（12 例）；V3 的 SQL 级 golden 随各功能落地分批追加 |
| M1 | A：索引 DDL；B：索引与统计（两条并行） | 各自单元测试通过；可直接调用公开接口验证 |
| M2 | C：规则优化器（不依赖索引的部分） | 优化等价测试通过；可整体开关 |
| M3 | C：代价选路 + 下推 + 索引执行器 + `physical` 开关 | 选路与强制模式测试通过；无索引/无统计可退化 |
| M4 | 集成 + 基准标定与报告 | 三模式结果一致；对比报告产出 |
| M5 | 文档与演示收口 | README / 契约文档 / 演示 SQL 更新 |

## 10. 契约变更流程

沿用既有约定：提议人写清"现状 → 问题 → 新条文"，同步 golden；
AST 变更至少 A+C 同意，Storage 变更至少 B+C 同意，文法 / 类型 / 错误码
需要三方同意；同意后本文升版本号，禁止悄悄改共享文件。

## 11. 风险与提醒

- 索引与数据一致性是最大风险点，insert / update / delete 三条路径都要覆盖。
- 下推若判断过宽会漏行；必须保留"不下推也正确"的测试。
- 空表与退化统计容易触发除零，C 的估算器要显式处理。
- 基准的主指标用确定性的页读取 / 缓存计数，耗时只作辅证；CI 断言不要用墙钟阈值。
- 统计基线会读满数据页并顺带预热 B 的 rid→页 映射，使 `auto`（会调
  `statistics`）与强制 `index`（不调）在同一进程里的成本不对称；基准报告
  必须写清这条口径，并保留 `api-*` 这类不经规划器的纯存储列。
- 追踪器在优化器阶段目前显示 `OFF`；V3 落地后由其自动填实，无需新建追踪契约。

## 12. 遗留

- UNIQUE 索引与多列组合索引：下一版再议，需再次升契约（AST 加字段 + 新错误码）。
- ANALYZE 语句：若统计变为显式重算再考虑加入文法。
- B 侧 V3.x（D49）：页定位与写入成本，拆成三条独立落地：
  - **D49b 布局遍历只付一次**（已落地）：`scan` / `_locate` / `_find_page_for`
    改用已增量维护的布局缓存，不再每次读遍整表判定页类型。稳态扫描从 2× 数据页
    降到 1×，插入与行定位也各少付一遍。首次访问仍要建一次基线（不可免除）。
  - **D49c 插入页提示**（已落地）：记住"比它小的活动页都已满"的下界，纯追加
    负载下每行只读一页；任何腾出空间的写入都把提示回退，落点语义与改动前一致。
  - **D49a 回表 O(1)**（待做）：索引叶条目携带物理页号（或首次访问建整表映射），
    消除冷进程的逐页探测；需升索引文件版本并定义旧索引重建策略。
  三条都不改契约、不改错误码；验收方式统一为同一份 bench 的 before/after 页读对比。
