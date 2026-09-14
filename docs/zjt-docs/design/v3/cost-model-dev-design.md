# 代价选路与强制物理模式开发设计（V3 · C 侧 F6/F7）

## 1. 文档目标

本文定稿 C 模块 V3 的 F6（代价选路与强制模式）与 F7 的索引侧部分（谓词 → 索引请求的翻译）：
在 Executor 树构建期读 `statistics()` 与 `list_indexes()`，为每个 `LogicalScan` 选出一条物理路径，
并把 `physical` 开关的三模式语义落到代码上。V3 要求代价公式、参数与标定方法由 C 自行定稿，
因此本文是这部分的一手依据。

本轮改动落点：

```text
1. runner/physical/__init__.py       # 包导出
2. runner/physical/requests.py       # IndexLookup / IndexRange / IndexRequest + 谓词翻译
3. runner/physical/cost_model.py     # 选择性估算 + 代价公式 + 带标定依据的常量
4. runner/physical/planner.py        # PhysicalPlanner + AccessPath + 理由码 + BuildContext
5. runner/executor/dql.py            # IndexScanExecutor + 构建期选路接入
6. runner/executor/dml.py            # 构建签名随 BuildContext 调整（行为不变）
7. runner/executor/builder.py        # build(plan, *, physical=...)
8. runner/runner.py                  # execute / execute_script / execute_file 透传 physical
9. main.py                           # --physical 命令行透传
```

## 2. 范围与非目标

### 2.1 本轮做

| 项 | 内容 |
|---|---|
| 谓词翻译 | `BoundComparison` → `IndexLookup` / `IndexRange`，含镜像翻转、开闭区间、无损值归纳 |
| 选择性估算 | 等值取 `1/distinct_count`，范围取区间占比，全部夹到 `[0,1]`，退化输入显式处理 |
| 代价模型 | `seq ≈ page_count`；`index ≈ 树高 + 命中行数 × 回表代价`，单位是预计数据页读取数 |
| 选路位置 | Executor 树构建期，每个 `LogicalScan` 一次局部决策 |
| 强制模式 | `seq` / `index` / `auto` 三模式与三条例外语义 |
| 可解释性 | 每次决策产出一条 `AccessPath` 记录（理由码 + 两个代价估计），并上报既有追踪口径 |
| 执行器 | 新增 `IndexScanExecutor`，行形状与 `SeqScanExecutor` 完全一致 |

### 2.2 本轮不做

- 不做 JOIN 顺序重排、不做 JOIN 算法选择（V3 明文非目标）：选路是每个 Scan 的局部决策；
- 不做索引合并（index intersection）、不做索引覆盖扫描（V3 明文非目标）：一次扫描最多用一个索引，命中行一律回表；
- 不做直方图与多列统计（B 的 `ColumnStats` 只有基数与极值）；
- 不做基于运行期反馈的自适应选路：代价估算只在构建期算一次；
- 不新增错误码：C 只抛既有的 `E_BAD_ARG`，索引存在性一律由 B 判定；
- 不改优化器规则、不改 `contracts`。

## 3. 现状与约束

### 3.1 现有执行链与插入点

```text
SQL → LogicalPlanBuilder.build → LogicalPlan → LogicalOptimizer.optimize → LogicalPlan
    → ExecutorTreeBuilder.build(plan) → StatementExecutor → execute(ExecutionContext)
```

选路落在最后一步：`build()` 时表结构已经确定、行形状（`source_indexes`）也在这一步计算，
把访问路径一并决定，不需要再引入一层物理计划 IR。理由是现有链路里 Executor 树就是物理计划的载体
（V1/V2 没有独立物理计划层），另建一层 IR 只会重复描述 Schema 与行形状；而"选路结果可解释"这一条
由 `AccessPath` 记录承担，与 V3 对可解释性的要求一致。

### 3.2 选路能拿到的契约输入

| 输入 | 来源 | 用途 |
|---|---|---|
| `TableStats.row_count` / `page_count` | `statistics(table)` | 命中行数与顺序扫描代价 |
| `ColumnStats.distinct_count` / `min_value` / `max_value` | 同上 | 选择性估算 |
| `IndexInfo(table, column)` | `list_indexes(table)` | `auto` 模式下判断哪些列可用索引 |
| `LogicalColumn.type` | 绑定结果 | 值归纳与区间算术的可行性判断 |
| `LogicalColumn.name` / `table` | 绑定结果 | 定位统计项与索引列 |

拿不到的东西必须正面承认：**契约不暴露 B+ 树的树高**（`storage` 内部把 height 写在索引文件页 0，
不进契约），因此树高只能由 C 用行数按扇出估算；契约同样没有数据分布直方图，
范围选择性只能建立在"min/max 区间内均匀分布"这一假设上。两条限制都写进 §7 的公式与 §16 的风险。

### 3.3 与优化器的正交关系

`optimize` 决定逻辑计划形态，`physical` 决定同一个 Scan 走哪条路径，两者互不推导。
它们的实际交汇点是**条件是否已经落到单个 Scan 之上**：

| optimize | physical | 单表查询 | JOIN 查询 |
|---|---|---|---|
| True | auto / index | 优化后 `Filter → Scan`，有候选 | 单侧 WHERE 已被下推到 `Filter → Scan`，有候选 |
| False | auto / index | Builder 原形 `Filter → Scan`，有候选 | WHERE 停在 Join 之上的 Filter 里，无候选 |

选路只认 `Filter⁺ → Scan` 这一相邻形态，不自己搬运谓词——把条件放到扫描位置是 F5 的职责。
`optimize=False` 的 JOIN 因此没有候选位置：`auto` 退化为顺序扫描（结果正确），
`physical="index"` 报 `E_BAD_ARG`（§8.2）。这是分工的直接后果；bench 若要同时测"无优化器"与
"强制索引"，把这两条轴分开跑，或把查询写成单表形式。

## 4. 设计原则

### 4.1 索引只做缩小行集，正确性由 Filter 兜底

下推后**不删除**任何谓词：`FilterExecutor` 照旧对每一行求值完整谓词，索引访问只负责把行集换成一个子集。
由此得到两条工程结论：

- 索引多返回行不会引起错误结果（Filter 会拦住）；
- 翻译规则只需要保证"少返回行"不会发生，即索引返回的子集必须是命中集（即正确结果集）的超集。
  （由 B 的 V3-T3 对照测试与本文 §13 的对照测试共同兜住）。

这样翻译器只需要回答"这个条件能否正确表达成键值/键区间"，不必回答"下推后语义是否仍然等价"。

### 4.2 选路是每个 Scan 的局部决策

同一语句里多个 Scan 各自定价、各自决定，不做跨表比较。这与"不做 JOIN 重排"的范围一致，
也让"每次执行的页读"成为可比单位：`NestedLoopJoinExecutor` 对右侧只物化一次，
每个 Scan 在一次语句执行中只被拉取一次，按单次执行的页读定价即可。

### 4.3 退化必须显式，方向必须保守

空表、`distinct_count=0`、`min == max`、端点缺失、键值越界都各有明确规定（§7.5），
且所有近似都朝"偏向顺序扫描"的方向偏：采样基数偏低 → 选择率偏高 → 命中行数偏高 → 索引显得更贵。
代价模型允许把索引估贵（少走索引，结果仍正确），不允许把索引估便宜（可能选到更慢的路径）。

### 4.4 强制模式只跳过选路，不改变判定归属

`seq` / `index` 都不做代价比较，也不改变"谁抛错"的分工：索引是否存在永远由 B 判定，
C 只在"无法为该查询构造任何索引请求"时抛 `E_BAD_ARG`。

## 5. 模块与接口

### 5.1 新增模块

```text
runner/physical/requests.py     # 索引请求形状与谓词翻译（纯函数，不 import storage）
runner/physical/cost_model.py   # 选择性估算与代价公式（纯函数 + 标定常量）
runner/physical/planner.py      # PhysicalPlanner、AccessPath、BuildContext、理由码
```

`runner/physical/` 只消费契约里的数据类型（`TableStats` / `IndexInfo` / `LogicalColumn`），
对 Storage 的实际调用留在执行器里，保持"规划不产生 I/O"的边界。

### 5.2 构建上下文与签名

`ExecutorTreeBuilder` 的构造期回调由两个扩到四个：新增 `statistics` 与 `list_indexes`，
与既有 `describe_table` 一样是**动态回调**（USE 换库后必须取新连接上的元数据，不能在建 Runner 时固化）。

```python
# runner/executor/builder.py
class ExecutorTreeBuilder:
    def __init__(
        self,
        describe_table: DescribeTable,
        current_database: CurrentDatabase,
        *,
        statistics: StatisticsOfTable,      # Callable[[str], TableStats]
        list_indexes: ListIndexesOfTable,   # Callable[[str], tuple[IndexInfo, ...]]
        trace_sink: RunnerTraceSink | None = None,
    ) -> None: ...

    @trace_runner_operation("executor", "build_executor_tree")
    def build(self, plan: LogicalPlan, *, physical: PhysicalMode = "auto") -> StatementExecutor: ...
```

- `statistics` / `list_indexes` 是**必填关键字参数**：漏传时构造期即报错，不会出现"忘了接线于是全部走顺序扫描"的静默退化；
- 每次 `build()` 新建一个 `PhysicalPlanner`，它的一次性缓存（统计、索引清单）因此天然限定在单条语句内——
  写操作会让统计失效，跨语句缓存是错的；表结构缓存仍留在 `ExecutorTreeBuilder` 上跨语句存活。

行执行器的构建入口把 `describe_table` 收进上下文对象，第二参数统一改为 `BuildContext`：

```python
# runner/physical/planner.py
@dataclass(frozen=True, slots=True)
class BuildContext:
    """Executor 构建期的只读输入：表结构查询与选路器。"""
    describe: DescribeTable
    planner: PhysicalPlanner


# runner/executor/dql.py / dml.py
def build_row_executor(plan: LogicalPlan, context: BuildContext) -> RowExecutor: ...
def build_select_executor(plan: LogicalProjection, context: BuildContext) -> SelectExecutor: ...
def build_dml_executor(plan: DmlPlan, context: BuildContext) -> StatementExecutor: ...
```

必填参数而非 `planner=None` 缺省：缺省值会让接线错误变成静默的顺序扫描。
代价是三处既有调用点要同步改（`runner/executor/builder.py` 两处、`runner/tests/test_logical_optimizer.py` 一处），
测试侧用"被调用就失败"的哨兵回调构造上下文，正好把"这条路径不该读元数据"变成断言。

### 5.3 选路结果 AccessPath

```python
# runner/physical/planner.py
@dataclass(frozen=True, slots=True)
class AccessPath:
    """一次 Scan 的物理访问决策。requests 为空即顺序扫描，不再单独存路径种类。"""
    table: str
    reason: str                                  # 稳定理由码（§6.6）
    requests: tuple[IndexRequest, ...] = ()      # 索引路径的候选请求，按优先级排列
    column: str | None = None                    # 首选候选的列名
    selectivity: float | None = None
    estimated_rows: float | None = None
    seq_cost: float | None = None
    index_cost: float | None = None
```

路径种类由 `requests` 是否为空表达，避免"种类说走索引、请求却是空的"这种非法状态。
`PhysicalPlanner` 在本次构建内累积每条记录：

```python
class PhysicalPlanner:
    def __init__(self, *, statistics, list_indexes, physical="auto", trace_sink=None) -> None: ...

    @trace_runner_operation("executor", "plan_access")
    def plan_scan(self, scan: LogicalScan, conjuncts: tuple[BoundExpr, ...]) -> AccessPath: ...

    @property
    def paths(self) -> tuple[AccessPath, ...]: ...
```

`ExecutorTreeBuilder.build()` 在树构建完成后做一次语句级判定：
`physical == "index"` 且 `paths` 非空（语句确实含扫描）且所有 `requests` 都为空 → 抛 `E_BAD_ARG`。
`paths` 为空表示语句没有扫描（DDL、INSERT、USE 等），此时物理模式无作用对象，正常执行。

## 6. 谓词翻译（F7）

### 6.1 可翻译形态

一个 conjunct 可翻译，当且仅当它是 `BoundComparison`，两侧恰好是**一个裸列引用与一个字面量**，
且列属于被规划的这张表：

```text
LogicalScan(table=t) ← Filter(… col <op> literal …)
                     ↑ col.table == t
```

绑定完成后两侧类型已经一致（`coerce` 会做数值提升或包 `BoundCast`），因此字面量的值可以直接作为键值；
唯一需要 C 补做的是契约点名的**值归纳**（见 §6.3）。

### 6.2 运算符翻译表

列在左侧：

| 谓词 | 索引请求 | 端点 |
|---|---|---|
| `col = k` | `IndexLookup(col, k)` | — |
| `col <> k` | 不翻译 | 需要双区间合并，本轮不做 |
| `col < k` | `IndexRange(col, None, k)` | `upper_inclusive=False` |
| `col <= k` | `IndexRange(col, None, k)` | `upper_inclusive=True` |
| `col > k` | `IndexRange(col, k, None)` | `lower_inclusive=False` |
| `col >= k` | `IndexRange(col, k, None)` | `lower_inclusive=True` |

字面量在左侧时按镜像翻转（`k < col` ≡ `col > k`，`k <= col` ≡ `col >= k`，`k = col` ≡ `col = k`；
`k <> col` 同样不翻译）。区间端点只按关键字传入，映射到 B 的 `lower_inclusive` / `upper_inclusive`，
**不引入 ±ε 之类的端点微调**：契约已经提供开闭区间，语义可以逐位对齐。

### 6.3 值归纳

绑定期的数值提升会留下两种需要处理的形态：

- `INT 列 = 1.0`：绑定后列被包成 `BoundCast(col, REAL)`，字面量是 `1.0`；
- `INT 列 = 1.5`：同上，但没有等价写法。

规则是**只做无损归一**：比较的一侧是 `BoundCast(裸列, T)`、另一侧字面量 `v` 时，
若 `T` 是 REAL、裸列是 INT 且 `v` 是实际上的整数值（虽然表现形式上存在小数零，通过 `float.is_integer()` 进行判断），把 `v` 归一回 INT 后照常翻译；
不满足即视为不可翻译（理由 `CAST_UNSAFE`）。这样既满足契约"INT 列收到 1.0 时先归一为 1"的要求，
也不必为"截断还是进位"发明新语义（之所以不考虑将 v 直接进位或截断，是因为实现的复杂性代价高）。`REAL` 列的 INT 字面量在绑定期已被重塑为 `float`，无需额外处理。

### 6.4 不可翻译清单

| 形态 | 理由码 | 说明 |
|---|---|---|
| `col <> k` | `NE_UNSUPPORTED` | 双区间需要合并成两次索引访问，本轮不做 |
| 含 `BoundCast` 且无法无损归一（即左列为 BoundCast(裸列, REAL)，右侧常量为非整数的小数，此时无法做到无损归一） | `CAST_UNSAFE` | 见 §6.3 |
| 裸布尔列（`WHERE active`） | `NOT_COMPARISON` | 只翻译比较运算符，不为布尔列单开一条路径 |
| 列在 `BoundArith` / 嵌套表达式内 | `NOT_BARE_COLUMN` | V3 无可达输入，防御性判定 |
| `OR` 分支内、`NOT` 下的条件 | `NOT_CONJUNCT` | 顶层为 OR/NOT 的项留在上层求值 |

不翻译不等于错误：该条件继续由上层 `FilterExecutor` 求值，结果不受影响。

### 6.5 索引请求形状

```python
# runner/physical/requests.py
@dataclass(frozen=True, slots=True)
class IndexLookup:
    column: str
    key: Value


@dataclass(frozen=True, slots=True)
class IndexRange:
    column: str
    lower: Value | None
    upper: Value | None
    lower_inclusive: bool = True
    upper_inclusive: bool = True


IndexRequest: TypeAlias = IndexLookup | IndexRange
```

### 6.6 理由码

```text
FORCED_SEQ          强制顺序扫描
FORCED_INDEX        强制索引访问
NO_PREDICATE        没有可翻译的比较条件
NO_MATCHING_INDEX   有可翻译条件，但目标列上没有索引
NO_STATS            有索引，但统计退化到无法估算
SEQ_CHEAPER         代价比较取顺序扫描（含等代价）
INDEX_EQUALITY      等值索引访问胜出
INDEX_RANGE         区间索引访问胜出
```

理由码是测试与 bench 报告的稳定标识；`AccessPath.reason` 之外的解释性文字只进追踪事件，不进断言。

## 7. 代价模型（F6）

### 7.1 定价单位与假设

代价单位是**预计的数据页读取次数**，与 V3 规定的主指标（确定性的页读/缓存计数）同口径。假设：

1. 索引节点页与数据页同价（同一个 Buffer Pool）；
2. 顺序扫描读满 `page_count` 个数据页，每页一次；
3. 索引探测按树高层数计页读，顶层节点常驻缓存这一优惠不单独建模（树高 ≤ 4，量级远小于回表项）；
4. 每次回表按一次数据页读取计（`REF_PAGE_COST`），不建模索引键序与物理顺序的相关性；
5. 溢出链页、空闲页、页 0 不计（与 `page_count` 的口径一致，两侧同等忽略）。

### 7.2 选择性估算

记号：`N = row_count`，`P = page_count`，`D = distinct_count`，`min` / `max` 取自 `ColumnStats`。

| 情形 | 选择性 |
|---|---|
| `col = k`，`D > 0`，`min ≤ k ≤ max` | `1 / D` |
| `col = k`，`k < min` 或 `k > max` | `0` |
| 数值列区间，`min`、`max` 存在且 `max > min` | `clamp((上界 − 下界) / (max − min), 0, 1)` |
| 单侧区间 `col < k` / `col <= k` | 上界取 `k`、下界取 `min` |
| 单侧区间 `col > k` / `col >= k` | 下界取 `k`、上界取 `max` |
| 数值列区间，`max == min` | 区间覆盖该值 → `1`，否则 `0` |
| 数值列区间，`min` 或 `max` 缺失 | `RANGE_DEFAULT_SELECTIVITY` |
| 非数值列（TEXT / BOOLEAN）的区间 | `RANGE_DEFAULT_SELECTIVITY` |
| `col = k`，`D == 0` 且 `N > 0` | 退化，不产生候选（§7.5） |

两条已知近似及其偏差方向：

- **均匀分布假设**：只有 `min`/`max` 没有直方图，偏斜数据上会高估命中行数 → 偏向顺序扫描；
- **采样基数**：`distinct_count` 来自有界采样（B 侧最多 16 个数据页），可能低估基数 →
  `1/D` 偏大 → 命中行数偏大 → 同样偏向顺序扫描。

两条偏差都落在保守一侧，符合 §4.3。

### 7.3 代价公式

```text
H(N)    = 1                                    , N ≤ 1        # 树高估计
        = ceil(log_F(N)) + 1                   , N > 1        # F = INDEX_FANOUT

C_seq   = P
C_index = H(N) + N × sel × REF_PAGE_COST
```

判定：`C_index < C_seq` 取索引，否则顺序扫描（**等代价取顺序扫描**，保证确定性与可复现）。
索引胜出的条件可以化简成 `sel × N × REF_PAGE_COST + H < P`，即"命中行数要明显少于表的数据页数"——
这与"每命中一行回表读一页"的物理事实一致，也让临界点随行宽自然变化（行越宽、每页行数越少，索引越容易胜出）。

树高由行数按扇出估算，`INDEX_FANOUT` 的取值依据见 §7.4；契约不暴露 B 的真实树高，公式只用契约数据。

### 7.4 常量与标定

全部常量集中在 `runner/physical/cost_model.py`，每条标注标定依据：

```python
INDEX_FANOUT = 100
"""索引节点扇出估计。

按 4KB 页估算：叶条目 = 8B rid + 8B 数值键 + 8B 槽 ≈ 24B，单叶页约 168 条；
内节点条目 = 4B 子页 + 8B rid + 8B 键 + 8B 槽 ≈ 28B，单节点约 145 个子页。
取 100 作为保守下界，使 TEXT 键（键更长、扇出更小）也在同一量级内。
标定记录：<数据集与实测树高，落地时回填>。
"""

REF_PAGE_COST = 1.0
"""每次回表的预计数据页读取数。

取 1.0 表示"每命中一行按一次数据页读计"，不假设缓存命中与物理聚簇带来的折减。
标定记录：<bench 实测（索引模式页读 − 索引页读）/ 命中行数，落地时回填>。
"""

RANGE_DEFAULT_SELECTIVITY = 1 / 3
"""缺少可用端点或非数值列时的范围默认选择性。

沿用 System R 对范围谓词的经典默认值 1/3：在没有任何分布信息时给出一个中性偏保守的估计。
"""
```

**标定方法**（在 M4 与 bench 一起完成，不阻塞功能落地）：

1. bench 在固定数据集上用 `seq` / `index` 两种模式跑同一批查询，取 B 的缓存 misses 作为页读次数；
2. 计数前先对目标表调用一次 `statistics()` 预热：统计采集自身会读页（B 侧采样最多 16 页），
   不预热会把采样成本记到第一个跑的模式头上，三模式就不可比了；
3. 用实测值反解 `REF_PAGE_COST = (index 模式页读 − 索引节点页读) / 命中行数`，
   用实测树高反解 `INDEX_FANOUT`；
4. 把标定后的数值与测量条件（表规模、行宽、缓存容量、命中率）回填到上面的常量注释里；
5. 判定标准：V3-T6 的两个方向都成立（高选择性走索引、低选择性放弃索引），
   且模型给出的临界点与实测页读的临界点在同一量级。

常量只影响临界点，不影响正确性，因此允许先落初始值、后回填标定结果。

### 7.5 退化输入处理

| 输入 | 处理 | 结果 |
|---|---|---|
| 空表（`N = 0`） | `C_seq = 0`，`C_index = H(0) + 0 = 1` | `auto` 取顺序扫描；强制 `index` 照常调 B |
| `D = 0` 且 `N > 0`（采样为空/统计不一致） | 等值选择性不可估算，该列不产生候选 | `auto` 退化为顺序扫描，理由 `NO_STATS` |
| `min` / `max` 缺失 | 区间算术不可用，改用默认选择性 | 仍是有效估算，不退化 |
| `max == min` | 区间宽度为 0，按"是否覆盖该值"给 0 或 1 | 有效估算 |
| 等值键越界 | 选择性 0，`C_index = H` | 索引胜出，真实结果也必然是 0 行 |
| `P = 0` 且 `N > 0`（统计不一致） | 顺序扫描代价无法定价 | `auto` 退化为顺序扫描，理由 `NO_STATS` |
| `statistics()` / `list_indexes()` 抛 `SqlError` | **不捕获** | 表存在性已由绑定期的 `describe` 保证，此处再抛错说明契约被违反，应当暴露 |

### 7.6 决策流程

```text
plan_scan(scan, conjuncts):
  1. 翻译 conjuncts → 候选（列，请求，选择性，理由）        # 纯计算，不读 Storage
  2. physical == "seq"    → 直接返回空 requests（FORCED_SEQ）
  3. physical == "index"  → 见 §8.2
  4. auto：
     a. 候选为空                    → NO_PREDICATE
     b. list_indexes(table)（首次访问该表才调用）
        —— 无任何索引                → NO_MATCHING_INDEX
        —— 候选列都无索引            → NO_MATCHING_INDEX
     c. statistics(table)（首次访问该表才调用）
        —— 统计退化                  → NO_STATS
     d. 逐候选算 C_index，取最小；与 C_seq 比较
        —— C_seq ≤ min(C_index)      → SEQ_CHEAPER
        —— 否则                      → INDEX_EQUALITY / INDEX_RANGE
```

候选之间的排序与并列打破规则：

1. 代价小者优先（`auto`）；`index` 模式下不读统计，按"等值优先于区间"排序；
2. 代价相同取选择性小者；
3. 仍相同取列在 `LogicalScan.schema` 中位置靠前者（确定性，不依赖 `list_indexes` 的返回顺序）。

一条语句里出现多次同一张表的扫描时，`statistics` 与 `list_indexes` 的按表缓存让每次构建只取一次元数据。

## 8. 强制物理模式

`physical="seq"` / `"index"` 都**跳过代价比较**，只保留路径的构造与判定归属。取值非法（不在三个字面量内）
在 `Runner` 入口抛 `E_BAD_ARG`，不做"未知取值当 auto"的容错。

### 8.1 `seq`

- 所有 Scan 一律 `SeqScanExecutor`；
- 不调用 `statistics`、不调用 `list_indexes`：强制顺序扫描不需要任何元数据，规划期零额外 Storage 调用；
- 任何语句都不会因此报错（包括无 WHERE 的查询、DDL、INSERT）。

### 8.2 `index`

三条例外严格执行：

1. **C 不判断索引是否存在**：只要能把谓词翻译成（表，列，键值或键区间），就直接把请求交给 B；
   有索引则执行，没有则由 **B 抛 `E_INDEX_NOT_FOUND`**。`list_indexes` 在本模式下**一次都不调用**。
2. **唯一由 C 抛的情形**：整条查询（语句内所有扫描）都构造不出任何索引请求 → 抛 `E_BAD_ARG`
   （消息形如 `physical="index" requires at least one index-eligible predicate`）。
3. **不得静默退化为顺序扫描**：本模式下不存在"代价不合适所以改走顺序扫描"的分支。

判定粒度是"可翻译的扫描位置"，不是"语句里的每一个扫描"：

- 至少一个扫描位置有候选 → 每个有候选的位置都走索引（每个位置取优先级最高的候选，
  并把全部候选按序交给执行器，见 §9）；没有候选的位置照旧顺序扫描——那里根本没有键值可交给 B；
- 零个候选位置 → `E_BAD_ARG`；
- 语句不含任何扫描（DDL、INSERT、USE）→ 物理模式无作用对象，正常执行。
  这条保证 bench 用 `execute_script(..., physical="index")` 跑"建表 + 插入 + 查询"整段脚本时，
  建表与插入不会因为"没有可索引的谓词"而失败。

**候选回退**：排序在前的候选未必有索引，而 C 又不看索引清单。若只认首选候选，
`WHERE amount > 100 AND id = 5`（索引只建在 `amount` 上）会以 `E_INDEX_NOT_FOUND` 失败，
这会把"这张表有可用索引"误报成"没有可用索引"。因此执行器按优先级逐个把候选交给 B，
遇到 `E_INDEX_NOT_FOUND` 就试下一个（B 在调用点同步抛出，没有行被消费，回退是干净的），
全部候选都没有索引时把 B 的错误原样上抛。C 始终没有判断过索引是否存在，只是多问了一次 B。

### 8.3 `auto`

- 只有代价比较，没有强制；任何退化都落到顺序扫描，不抛 `E_BAD_ARG`；
- 规划期只读：`list_indexes` 仅在存在可翻译候选时调用，`statistics` 仅在存在"候选 × 索引"匹配时调用；
- 表上没有索引时完全不读统计，这条路径与今天的行为、与 `seq` 模式的页读完全同价。

### 8.4 错误矩阵

| 场景 | `auto` | `seq` | `index` |
|---|---|---|---|
| 无 WHERE 的 SELECT | 顺序扫描 | 顺序扫描 | `E_BAD_ARG`（C 抛） |
| WHERE 列有索引、选择性高 | 索引访问 | 顺序扫描 | 索引访问（B 无索引则 `E_INDEX_NOT_FOUND`） |
| WHERE 列无索引 | 顺序扫描 | 顺序扫描 | 调 B → `E_INDEX_NOT_FOUND`（B 抛） |
| 谓词不可翻译（`<>`、CAST、裸布尔、OR 内条件） | 顺序扫描 | 顺序扫描 | `E_BAD_ARG`，但同层另有可翻译项时用那一项 |
| 统计退化 | 顺序扫描（`NO_STATS`） | 顺序扫描 | 索引访问（不读统计） |
| 空表 | 顺序扫描 | 顺序扫描 | 调 B（不因空表跳过） |
| DDL / INSERT / USE | 正常执行 | 正常执行 | 正常执行 |
| JOIN + `optimize=False` | 顺序扫描 | 顺序扫描 | `E_BAD_ARG`（无单侧 Filter，§3.3） |
| `physical` 取值非法 | `E_BAD_ARG` | `E_BAD_ARG` | `E_BAD_ARG` |

### 8.5 结果等价性

强制模式只影响路径，不影响结果集：`FilterExecutor` 保留完整谓词，索引只缩小行集。
需要显式声明的一点是**行序**：索引按 B 的键序返回行，与 `scan()` 的行序不同，
而 V3 没有 `ORDER BY`，无 `ORDER BY` 的查询不承诺行序。
因此三个模式的比对基准是**行多重集合**（V3-T7），不是逐位置相等的行元组；
`auto` 与 `seq` 之间、以及无索引场景下，实测行序与今天完全一致。

## 9. 索引执行器

```python
# runner/executor/dql.py
@dataclass(frozen=True, slots=True)
class IndexScanExecutor(RowExecutor):
    """按索引请求取行：行形状与 SeqScanExecutor 完全一致。"""
    table: str
    schema: LogicalSchema
    source_indexes: tuple[int, ...]
    requests: tuple[IndexRequest, ...]     # 候选请求，按优先级排列

    @property
    def output_schema(self) -> LogicalSchema: ...

    @trace_runner_operation("runtime", "index_scan.rows")
    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        for row_id, values in self._fetch(context):
            yield ExecRow(row_id=row_id, values=tuple(values[i] for i in self.source_indexes))

    def _fetch(self, context: ExecutionContext) -> Iterator[Row]:
        """按优先级尝试候选请求；只有 E_INDEX_NOT_FOUND 才回退，其余错误直接上抛。"""
```

要点：

- `source_indexes` 与 `SeqScanExecutor` 共用同一个计算函数（按列名反查表完整列序），
  因此 B 的 `Row`（`row_id` + 建表列序的值）能直接复用同一条 ExecRow 管线；
- `row_id` 是 B 的物理行把手，UPDATE / DELETE 的索引扫描路径可以原样把它交给 `update_row` / `delete_row`；
- 只捕获 `E_INDEX_NOT_FOUND` 做候选回退，`E_TYPE_MISMATCH` 等错误立即上抛——类型归纳是 C 的责任，
  归一出错必须暴露，不能被回退掩盖；
- 行是惰性产出的，B 的 `E_INDEX_NOT_FOUND` 在第一次拉取时抛出，仍在 `SelectExecutor.execute` 之内，
  对调用方表现为一条 `SqlError`；测试只断言错误码，不锁定时序。

构建分支：`build_row_executor` 命中 `Filter → Scan` 形态时先向选路器要一条 `AccessPath`，
再按 `requests` 是否为空构造 `IndexScanExecutor` 或 `SeqScanExecutor`：

```python
case LogicalFilter():
    terms, scan, inner_filters = _split_filter_chain(plan)   # Filter* → Scan 才成立
    if scan is None:
        return FilterExecutor(plan.predicate, build_row_executor(plan.child, context))
    path = context.planner.plan_scan(scan, terms)
    executor = build_scan_executor(scan, path, context)
    for layer in reversed(inner_filters):        # 被展开的内层 Filter 原样重建
        executor = FilterExecutor(layer.predicate, executor)
    return FilterExecutor(plan.predicate, executor)
```

`_split_filter_chain` 沿 `LogicalFilter` 链向下收集全部 conjuncts，只在其末端是 `LogicalScan` 时生效；
优化器的 Filter 合并规则让链式 Filter 实际上不会出现，这里展开是为了让"候选看得到全部条件"这一性质
不依赖优化器是否开启。内层与外层的谓词都保留，符合 §4.1。

## 10. 规划期只读调用的时机与缓存

| 调用 | 时机 | 缓存范围 |
|---|---|---|
| `describe(table)` | 既有：构建期定位 `source_indexes` | `ExecutorTreeBuilder` 跨语句（按 (库, 表)） |
| `list_indexes(table)` | `auto` 且该表有可翻译候选 | 本次 `build()`（按表） |
| `statistics(table)` | `auto` 且候选列上有索引 | 本次 `build()`（按表） |

跨语句缓存统计是错的：写入会让 B 的列级统计失效，C 缓存会拿到过期基数。把缓存绑在 `PhysicalPlanner`
实例上（每次 `build()` 新建）就自然满足这条。规划期只读、不写盘由 B 的契约保证，C 侧只需要不越界。

## 11. API 与入口透传

```python
# runner/runner.py
PhysicalMode = Literal["auto", "seq", "index"]

def execute(self, sql: str, *, optimize: bool = True, physical: PhysicalMode = "auto") -> QueryResult: ...
def execute_script(self, sql: str, *, stop_on_error=True, optimize=True, physical="auto") -> ScriptResult: ...
def execute_file(self, path, *, stop_on_error=True, optimize=True, physical="auto") -> ScriptResult: ...

def _execute_statement(self, statement, *, optimize=True, physical="auto") -> QueryResult: ...
```

- 默认值保证既有调用零影响；
- `physical` 在 `execute` / `execute_script` / `execute_file` 入口先校验一次，非法取值在跑第一条语句之前就报错；
- `Runner.last_access_paths: tuple[AccessPath, ...]` 与 `last_optimization_log` 同构：最近一条语句的
  选路记录，每条语句执行前重置；JOIN 一条语句可能有多条（每个 Scan 一条）；
- `main.py` 新增 `--physical {auto,seq,index}`（默认 `auto`），透传给 `-e` / `-f` 两个入口；
  REPL 交互路径不加快捷命令，固定 `auto`（V3 没有把强制模式做成会话状态）。

**Inspector 路径的接线**：`Runner.execute` 启用 inspector 时由编排器代为执行，
协议需要同步扩展成 `execute(runner, sql, *, physical="auto")`，UI 侧的 `_run_statement` 把 `physical`
透传给 `runner._execute_statement`。这是一次签名同步，属集成项；在 UI 跟进前，
bench 与强制模式的验证一律走非 inspector 路径，避免"UI 下 physical 被忽略"产生错误结论。

## 12. 追踪与可解释性

沿用既有 C 追踪口径（不新增契约）：`PhysicalPlanner.plan_scan` 用
`trace_runner_operation("executor", "plan_access")` 装饰，每个 Scan 上报一条事件，
载荷里带输入计划与该 Scan 的 `AccessPath`（理由码、两个代价估计、选择性、候选请求）。
组件名落在既有的 `executor` 上，不新增第五个组件。

`AccessPath` 同时是 bench 的取证材料：报告里的"这一行走了索引"由理由码与候选列表证明，
不靠行序或耗时推断。

## 13. 测试方案

### 13.1 纯函数层

`runner/tests/test_index_requests.py`

- 六种运算符 × 列在左/字面量在左，断言请求类型、端点、开闭标志；
- `<>`、裸布尔列、`NOT` 下条件、`OR` 分支条件不可翻译；
- `BoundCast` 列的三种情形：可无损归一（`1.0 → 1`）、不可归一（`1.5`）、非数值目标；
- TEXT / BOOLEAN 键的翻译与区间。

`runner/tests/test_cost_model.py`

- §7.2 表格逐行覆盖：等值、越界键、单侧区间、双侧区间、`max == min`、端点缺失、非数值列；
- 夹取到 `[0,1]` 的边界；
- 树高公式在 `N = 0 / 1 / F / F+1` 各点的取值；
- 等代价取顺序扫描；空表取顺序扫描。

### 13.2 选路层（假统计 + 哨兵回调）

`runner/tests/test_access_path.py`

- `auto` 的理由码全覆盖：`NO_PREDICATE` / `NO_MATCHING_INDEX` / `NO_STATS` / `SEQ_CHEAPER` /
  `INDEX_EQUALITY` / `INDEX_RANGE`；
- 零额外 I/O 的断言：`seq` 模式下两个元数据回调都是"被调用即失败"的哨兵；
  无索引时 `statistics` 不被调用；无候选时 `list_indexes` 不被调用；
- 候选排序与并列打破规则的确定性；
- 同一张表被扫两次时元数据只取一次。

### 13.3 执行器层

`runner/tests/test_index_scan_executor.py`

- 手工构造请求，断言行形状、`source_indexes` 映射与 `row_id` 透传；
- 候选回退：首选请求抛 `E_INDEX_NOT_FOUND` → 用第二个候选成功；
- 全部候选无索引 → 上抛 B 的错误；
- `E_TYPE_MISMATCH` 不被回退吞掉。

### 13.4 SQL 级（V3-T6 / V3-T7）

`tests/test_physical_flow.py`（真实 Storage）

- **V3-T6 两个方向**：构造"一页多行、命中一行"的高选择性查询断言走索引；
  构造"命中大半张表"的低选择性查询断言走顺序扫描；
- **V3-T7 三模式一致**：同一批查询在 `auto` / `seq` / `index` 下结果的多重集合、表头、
  `affected_rows`、错误码逐条一致（含 UPDATE / DELETE 走索引的情况）；
- 强制模式错误归属：`index` + 无索引列 → `E_INDEX_NOT_FOUND`；
  `index` + 无 WHERE → `E_BAD_ARG`；`index` + DDL/INSERT → 正常执行；
- 索引路径的取证：用 `Runner.last_access_paths` 的理由码断言"确实走了索引"，
  不用行序或耗时；
- 边界值：端点等于 `min` / `max`、开闭区间相邻值、重复键、REAL / TEXT / BOOLEAN 键、
  负数值，逐条与 `seq` 模式对照。

### 13.5 回归

默认 `auto` 且没有索引时，选路结果恒为顺序扫描、规划期零额外 Storage 调用，
因此既有 golden、V2 用例与 V3 已有测试应当**零改动通过**。这条本身就是一个断言：
如果某个既有测试需要为了本次改动而改期望值，说明默认路径被改变了行为。

## 14. 实施顺序

1. `requests.py`：索引请求形状 + 翻译器，配纯函数单测（此时不接任何执行路径）；
2. `cost_model.py`：选择性与代价公式，配 §13.1 的单测；
3. `IndexScanExecutor` + `_fetch` 的候选回退，用手工构造的请求验证行形状与错误行为；
4. `planner.py`：`PhysicalPlanner` 先只实现 `auto`（无候选 → 顺序扫描；有候选 → 比较），配假统计单测；
5. 构建签名迁移到 `BuildContext`，接通 `auto` 路径，跑全量回归（默认路径行为不变）；
6. `physical` 进入 `Runner` 与 `main.py`，实现 `seq` / `index` 的两条例外与错误矩阵，补 SQL 级测试；
7. 接追踪事件与 `Runner.last_access_paths`；
8. 补 V3-T6 / V3-T7 的 SQL 级测试与边界值对照；
9. 与 bench 一起标定常量并回填注释（可在功能验收之后）。

第 3 步先于第 4 步：执行器可以脱离选路单独验证，选路的正确性判定（"选出的请求能否取到正确的行"）
因此有一个已经稳定的落点。

## 15. 验收标准

- V3-T6：高选择性走索引、低选择性放弃索引；无统计、无索引时安全退化为顺序扫描；
- V3-T7：`seq` / `index` / `auto` 三模式结果（行多重集合、表头、影响行数）一致；
- 强制模式三条例外与 §8.4 错误矩阵逐行可测：C 只抛 `E_BAD_ARG`，索引存在性一律由 B 判定；
- `auto` 与 `seq` 在无索引时规划期零额外 Storage 调用，既有测试零改动通过；
- 每次决策都有理由码与两个代价估计，可进追踪与 bench 报告；
- `cost_model.py` 的常量都带标定依据注释，标定结果按 §7.4 回填。

## 16. 风险与已知边界

1. **`optimize=False` + JOIN + `physical="index"` 报 `E_BAD_ARG`**（§3.3）：把单侧条件下推到扫描位置是
   优化器的职责，选路不重复实现。bench 的两条轴分开跑即可，不阻塞 V3-T6/T7。
2. **行序跨模式不同**：索引按键序返回行，无 `ORDER BY` 时 SQL 不承诺行序，
   等价性比对必须用行多重集合（§8.5）。写测试时若按逐行相等比对，会得到假失败。
3. **统计采集自身读页**：bench 若不做预热，会把采样成本记到第一个跑的模式头上（§7.4）。
4. **均匀分布假设与采样基数**：两者都偏向顺序扫描，可能让本该走索引的偏斜数据走顺序扫描；
   这是本版可接受的保守偏差，直方图留给后续版本。
5. **候选回退是"多问一次 B"**：它让"首选候选无索引、次选候选有索引"的查询成功，
   代价是选路记录里必须保留完整候选列表，否则事后无法解释为什么不是首选列生效。
6. **Inspector 路径的签名同步**：UI 侧未跟进前，`physical` 在 inspector 路径下无效（§11），
   bench 不走该路径。
