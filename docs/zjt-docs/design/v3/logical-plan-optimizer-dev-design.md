# 逻辑优化器开发设计（V3 · LogicalPlan → LogicalPlan 纯变换）

## 1. 文档目标

本文定义 C 模块 V3 的 F5 规则型逻辑优化器：一组 **`LogicalPlan → LogicalPlan` 的纯变换**，
在不改变查询结果的前提下把 Builder 产出的计划规范成便于后续代价选路的形态。

本文只定稿优化器的**规则集、每条的改写语义、驱动方式、开关接口与日志形态**。
物理计划、代价估算、索引下推不在本轮范围；本文只在必要处标注交接点，不预设代价模型。

对本轮范围，落地位置与受影响文件：

```text
1. runner/logical_plan/optimizer/rules.py       # 五条规则，纯函数

2. runner/logical_plan/optimizer/optimizer.py   # LogicalOptimizer + OptimizationLog + RuleApplication

3. runner/logical_plan/optimizer/__init__.py    # 包导出

4. runner/logical_plan/plans.py                 # 新增 LogicalEmpty

5. runner/executor/dql.py                       # 新增 EmptyExecutor

6. runner/executor/builder.py                   # Empty 分支接入

7. runner/runner.py                             # execute(..., optimize=...)

8. runner/logical_plan/__init__.py              # 导出新节点
```

## 2. 范围与非目标

### 2.1 本轮做

| 项 | 内容 |
|---|---|
| 规则 | 五条：常量折叠、AND 展平与 Filter 合并、布尔规范化（NOT 下推、真值化简与同层去重）、JOIN 谓词下推（WHERE conjunct 与单侧 ON conjunct）、投影裁剪与冗余节点消除 |
| 新增逻辑节点 | `LogicalEmpty` |
| 新增执行器 | `EmptyExecutor` |
| 开关 | `Runner.execute(sql, *, optimize: bool = True)` 逐语句生效 |
| 日志 | `OptimizationLog`：逐条规则的应用记录与计划形态变化 |
| 裁剪深度 | 完整裁剪，含 `LogicalScan` 只输出被引用的列（来源不得裁到零列，见 §5.1） |

### 2.2 本轮不做

- **不优化 DML 子树**：`LogicalUpdate` / `LogicalDelete` 的 `child` 原样保留（理由见 §3.3）；
- 不做代价估算与物理路径选择，只产出**语义等价的规范化逻辑计划**；
- 不做 JOIN 顺序重排、不做 JOIN 算法选择（V3 明文非目标）。ON 里的单侧条件下推不算重排：
  它既不改变连接顺序也不改变算法，属 §7.4 的范围；
- 不做 NULL、聚合、子查询、ORDER BY 相关的规范化——这些语法在 V3 不存在；
- 不感知追踪：只产出 `OptimizationLog`，由 Runner 与追踪层消费该日志（见 §10.3）；
- 不新增任何错误码，优化器不抛语义错误（见 §10.5）。

## 3. 现状与约束

### 3.1 现有计划节点

`runner/logical_plan/plans.py` 当前只有四类查询节点，全部是 `frozen=True, slots=True` 的不可变 dataclass：

| 节点 | 输出 Schema | 关键不变式（构造时校验） |
|---|---|---|
| `LogicalScan(table, schema, alias)` | `schema` | 叶子；`schema` 即表完整 Schema，index 按建表列序 |
| `LogicalFilter(predicate, child)` | `child.output_schema` | `predicate` 顶层必须是**非空** `BoundLogical(AND)`；列引用可按 index 定位 |
| `LogicalProjection(columns, output_names, child)` | 依 `columns` 重新编号 | `len(output_names) == len(columns)`；每一项可在 child Schema 定位 |
| `LogicalJoin(left, right, on, schema, kind)` | `schema` | `schema == join_schema(left, right)`；`on` 顶层非空 AND；每项为 BOOLEAN |

DDL / DML 节点（`LogicalInsert` / `LogicalUpdate` / `LogicalDelete` 等）与本文的规则无关。

### 3.2 表达式已全部绑定

优化器**只做树改写，不做名称解析、不做类型推导、不做类型转换**。这是本设计的根本前提：

- `BoundColumnRef.column` 是完整的 `LogicalColumn`（table / qualifier / name / index / type）；
- 比较两侧的类型协调（INT → REAL 提升）在绑定期已完成，因此 `BoundComparison` 的两侧恒同型；
- `coerce()` 只对字面量做值重塑，只有非字面量才插 `BoundCast`。

由此得到一条对常量折叠至关重要的结论：**绑定后的两侧同型比较，用运行期的 `cmp_eval()`
求值，结果与运行期逐行求值完全一致**。折叠不引入任何新的语义规则。

另需注意：AST 契约的 `Cmp` 两侧是 `Column | Literal`，不含算术与函数调用。因此实践中
**可折叠的比较只有「字面量 vs 字面量」一种**，`BoundArith` 在 V3 无可达输入（其 docstring
已标注为预留节点）。折叠器仍然写成递归函数以容纳未来节点，但测试集只需覆盖字面量比较。

### 3.3 DML 子树的硬约束（决定优化范围的唯一原因）

`UpdateExecutor` 用 child 输出的行**做整行替换**：

```python
values = list(row.values)
for assignment in self.assignments:
    values[assignment.column.index] = assignment.value.value
context.storage.update_row(self.table, row.row_id, tuple(values))
```

`assignment.column.index` 是**物理表列序号**（绑定期由 `logical_schema.column(name)` 得到），
而 `row.values` 是 child 的输出行。两者只有在「child 输出恒等于表完整列序」时才对齐。
`DeleteExecutor` 虽然只消费 `row_id`，但与之同构，没有单独放宽的必要。

因此：**优化器只作用于 SELECT 计划树**。这条范围不是保守，而是消除一整类静默的数据损坏风险——
投影裁剪一旦落到 UPDATE 的 child 上，被裁掉的列会在整行写回时变成错位写入。

### 3.4 `LogicalColumn.index` 的语义

`index` 是「该列在**所属算子输出行**中的位置」，不是它在数据库表中的位置。
`_build_source()` 里两者恰好相同（表 Schema 未裁剪），投影裁剪之后就不再相同。
`_check_column()` 校验的正是这个语义：`schema.columns[col.index]` 必须与 `col` 的
table / qualifier / name / type 全部一致。**这条校验是裁剪后整棵树的免费自检器。**

## 4. 设计原则

### 4.1 规则是纯函数

```python
def rule_name(plan: LogicalPlan) -> RuleResult
```

规则不得 import `storage`、不得执行计划、不得读数据、不得原地修改输入。
所有节点都是 frozen dataclass，改写天然是「构造新节点、复用未变子树」。
回传的 `RuleResult` 除计划外还带一句**规则自述的摘要**（§6.1）：摘要由规则在改写过程中
统计命中得出，驱动层不反推规则语义。

### 4.2 结果等价是第一约束，形态规范是第二约束

每条规则都必须满足：对同一份数据，改写前后的 `QueryResult` 逐条相同（含列序、表头、
行内容，以及错误行为）。做不到等价的改写一律不做，宁可少一条规则。

### 4.3 优化不是语义改变的补偿

与 V3 §7.5 对下推的定性一致：**关闭优化器必须结果正确**。优化器的任何一条规则被跳过、
被开关关掉、在不动点迭代中因达到上限而中断，都只影响性能，不影响正确性。

### 4.4 只做能自证终止的改写

每条规则都是单调的：要么降低节点数，要么把节点推向叶子。驱动层仍然设迭代上限兜底（§6.3），
上限触顶不抛错，只记录日志并返回当前计划。

## 5. 输出契约与新增不变式

优化器的输出必须与 Builder 的输出满足**同一套**节点不变式——执行器只认这套不变式，
不做二次校验。为此需要强化两条、新增两条：

### 5.1 强化：`LogicalScan.schema` 是输出列的子序列

- 裁剪后 `LogicalScan.schema` 是物理表 Schema 的**子序列**（保持建表列序，不重排）；
- `schema.columns[i].index == i`：输出列 index 从 0 连续编号，与输出行元组一一对应；
- **零列来源不允许**：`LogicalScan` 至少输出一列，裁剪不得把一个来源裁到零列。
  零列 Scan 的行数仍然有意义——它决定上层 JOIN 的产生行数：`SELECT u.id FROM users u
  JOIN orders o ON u.active` 中 `orders` 一列都不被引用，但结果按「active 的 users 行数
  × orders 行数」产生。需求集合为空时，裁剪**保留裁剪前 Schema 的首列**（取法固定，
  保证不动点判定稳定），而不是把该来源换成 `LogicalEmpty`——后者是零行，被裁到零列的
  来源仍是非零行，替换会改变结果行数（§7.5 给出该查询的行数对照）；
- 物理层仍 `scan()` 整行，由执行器按裁剪后 Schema 投影——即「裁剪下推到 Scan 的输出，
  不下推到存储的解码」，V3 的 SeqScan 可以仍解码完整物理行。

### 5.2 强化：裁剪后所有列引用的 index 同步重映射

任何改变子节点 Schema 的规则，都必须把**该子节点之上的全部列引用**（Filter 谓词、
Join ON、Projection 列、以及更上层）的 `index` 重映射到新位置。
重映射只改 `.index`，table / qualifier / name / type 一律不动，否则 `_check_column` 会报
`E_TYPE_MISMATCH`。实现上统一走一个辅助函数，避免手写多处：

```python
def remap_column(column: LogicalColumn, mapping: dict[int, int]) -> LogicalColumn:
    """按旧 index → 新 index 的映射重塑列引用；映射缺失即实现错误。"""
```

**`mapping` 的方向容易写反**：它来自「子节点**裁剪前**的 Schema」到「子节点**裁剪后**的
Schema」的位置映射，因此**必须先确定子节点的新 Schema，再改写父节点的引用**。
自上而下一次遍历做不完这件事——父节点的引用要等子节点的裁剪结果落定后才可能重写。
因此 §7.5 的裁剪规则明确拆成两个阶段：先自顶向下传播需求并**自底向上**重建各节点的新
Schema，再**自顶向下**用子节点的新旧映射改写各节点的表达式与投影列。

### 5.3 新增：`LogicalEmpty`

```python
@dataclass(frozen=True, slots=True)
class LogicalEmpty(LogicalPlan):
    schema: LogicalSchema
```

- **语义**：恒零行的叶子，`output_schema` 即 `schema`。规则证明某个子树必然零行
  （恒假 Filter、或 JOIN 的某一侧已是 `LogicalEmpty`）后用它替换该子树——删掉 Filter
  会让下面的行全部漏上来，零行必须有个显式表示；
- **`schema` 必须携带**：父节点的列引用是按被替换子树的旧 Schema 编写的，
  `_check_column` 会按 `schema.columns[col.index]` 校验。`LogicalEmpty` 不带同构 Schema 时，
  `SELECT * FROM t WHERE FALSE` 会在构造 Projection 时报错，而不是返回「有表头、零行」；
- **它同时是可执行节点**（§9.1）：把 Empty 沿树向上塌陷（JOIN 任一侧为 Empty → 整个 JOIN
  为 Empty）是 §7.5 的规则职责，不是节点的语义。规则没跑到那一步时，计划照常执行且结果正确。

## 6. 优化器结构与驱动

### 6.1 规则契约

```python
@dataclass(frozen=True, slots=True)
class RuleResult:
    plan: LogicalPlan
    summary: str           # 本规则自述的单行摘要


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    apply: Callable[[LogicalPlan], RuleResult]
```

规则自带名字，供日志与后续追踪器使用；名字是**稳定的诊断标识**，改名视为接口变更。

`summary` 由规则自己给出，因为只有规则知道「这次改的是什么」：常量折叠报折叠了几处、
下推报各项落在哪一侧、裁剪报裁掉了多少列。驱动层只负责搬运，不做语义反推，
规则因此可以各自选择摘要口径，新增规则不必改驱动。

### 6.2 规则的应用粒度

统一采用**整树递归重写**：规则内部自顶向下遍历整棵树，对每个节点尝试改写，
子节点递归处理。好处是规则可以自证作用范围，驱动层不需要理解规则结构。

唯一的例外是投影裁剪——它需要「父节点要求子节点提供哪些列」的自顶向下信息，
因此它的签名多一个输入需求集合，属规则内部实现细节（§7.5），驱动层接口不变。

### 6.3 不动点迭代

```python
def optimize(self, plan: LogicalPlan) -> OptimizationLog:
    """反复按固定顺序应用规则，直到计划不再变化或达到迭代上限。"""
```

- 每轮按**固定顺序**跑全部五条规则，单条规则内部跑到自身不动点或本轮无变化；
- 计划的「是否变化」用**结构相等**判断（frozen dataclass 的 `==`），不用对象身份；
- 迭代上限为常量（建议 8，作为防御性兜底），触顶不抛错：记录日志、返回当前计划；
- 每次规则命中产出一条 `RuleApplication`，仅在 `plan` 实际变化时记录（用 `==` 判定），
  避免日志被「跑了但没改」的规则淹没。

### 6.4 规则顺序

顺序由**依赖方向**决定，不是随意排列：

| 序 | 规则 | 为什么在这个位置 |
|---|---|---|
| 1 | 常量折叠 | 先消掉字面量噪音，后续规则才看得到干净的谓词 |
| 2 | AND 展平与 Filter 合并 | 把谓词拆成独立 conjunct，是第 3、4 条的前置条件 |
| 3 | 布尔规范化（NOT 下推、恒真恒假化简与同层去重） | 把 NOT 压到比较层，使 conjunct 可被下推与索引翻译 |
| 4 | JOIN 谓词下推 | 把 Filter 与 ON 里的单侧 conjunct 都送到叶子，缩短中间结果 |
| 5 | 投影裁剪与冗余节点消除 | 放最后：前面几条会删节点、改引用，裁剪只做一次 |

第 4 条必须在第 5 条之前：下推改变列的可用位置，裁剪需要看到最终引用集合。
裁剪放最后还有一个实际好处——它是唯一大面积重映射 index 的规则，一次成型比多次增量更易验证。

## 7. 规则详述

以下每条都给出「触发条件 → 改写 → 等价性依据」。示例统一使用：

```sql
CREATE TABLE events (id INT, kind TEXT, amount INT);
```

### 7.1 常量折叠

**触发**：表达式中出现所有操作数均为字面量的可求值节点。

**改写**：

| 原节点 | 改写为 |
|---|---|
| `BoundComparison(BoundLiteral, op, BoundLiteral)` | `BoundLiteral(cmp_eval(op, l, r), SqlType.BOOLEAN)` |
| `BoundUnaryNot(BoundLiteral)` | 取反后的 `BoundLiteral` |
| `BoundCast(BoundLiteral, target)` | `BoundLiteral(cast_value(v, target), target)` |

**边界：只折叠值级表达式，不碰 `AND` / `OR` 层。** 逻辑层的恒真恒假化简（`TRUE AND xs` → `xs`、
`FALSE OR xs` → `xs` 等）统一由 §7.3(b) 负责，本条规则不做。理由是那份化简表在 §7.3 无论如何都要
实现——NOT 下推会现场造出新的逻辑常量（`NOT (a OR b)` → `NOT a AND NOT b`），那是本条规则看不到的。
同一条化简只保留一份实现，规则命中才不会随改写来源落在不同规则名下。

**关键实现要求**：折叠直接用运行期的 `cmp_eval()` / `cast_value()`，**不得**另写一套比较或转换。
理由见 §3.2——绑定已完成类型协调，两处求值必然一致；自写一套才是等价性的风险源。

**等价性依据**：折叠结果与逐行求值结果由同一个函数产生。

### 7.2 AND 展平与 Filter 合并

**触发**：

1. `BoundLogical(AND, terms)` 的某个 term 本身是 `BoundLogical(AND, ...)`；
2. `LogicalFilter` 的 child 也是 `LogicalFilter`。

**改写**：

1. AND 链递归展平为单一层的 conjunct 列表，顺序不变；
2. 相邻 Filter 合并为一个，conjunct 顺序为「上层谓词在前、下层谓词在后」——
   顺序只影响短路求值的快慢，不影响结果；固定顺序是为了让不动点判定稳定。

**等价性依据**：AND 满足结合律与交换律；Filter 是纯谓词筛选，`Filter(p, Filter(q, X))`
与 `Filter(p AND q, X)` 对 X 的筛选结果相同。布尔比较是二值逻辑（V3 无 NULL），
不存在三值逻辑下的顺序依赖。

**注**：`LogicalJoin.on` 的顶层同样是 `BoundLogical(AND)`，展平对它同样适用，
但 JOIN 之间的合并不在范围内（会改变 JOIN 语义，属 JOIN 重排）。

### 7.3 布尔规范化（NOT 下推 + 恒真恒假化简）

这是 V2 §5.2 之外新增的一条规则，把布尔表达式整理成「**比较运算 + AND/OR 连接**」的规范形，
使每个 conjunct 都能被第 4 条规则与后续的索引翻译直接消费。

**(b) 的恒真恒假化简只在本条规则实现**：NOT 下推会现场产生新的逻辑常量，规则 1 看不到这些常量，
因此整张化简表放在这里才只有一份实现（规则 1 只做值级折叠，见 §7.1）。

**(a) NOT 递归下推（德摩根）**

| 原式 | 改写为 |
|---|---|
| `NOT (a AND b)` | `NOT a OR NOT b` |
| `NOT (a OR b)` | `NOT a AND NOT b` |
| `NOT NOT a` | `a` |
| `NOT (l op r)` | `l 取反(op) r`，取反表见下 |
| `NOT (l op r)` 且两侧均为字面量 | 交由常量折叠处理 |

比较运算符翻转表：

```text
=  →  <>        <>  →  =
<  →  >=        >=  →  <
<=  →  >         >   →  <=
```

**等价性依据**：德摩根律；比较运算符取反在二值逻辑下与 `NOT` 逐值等价（无 NULL 即无 UNKNOWN）。
翻转不改变两侧的类型协调结果，因此不改变值域——例如 `NOT (amount > 1.5)` 翻转为
`amount <= 1.5`，两侧仍是 REAL 与 REAL。

**(b) 恒真恒假化简**

对每个 AND / OR 节点，按 `BoundLiteral(value=True/False)` 的出现情况化简：

| 原式 | 改写为 |
|---|---|
| `TRUE AND xs` | `xs`（`xs` 仅一项时即该项本身） |
| `FALSE AND xs` | `FALSE` |
| `TRUE OR xs` | `TRUE` |
| `FALSE OR xs` | `xs` |
| `AND` 的 terms 全部为 `TRUE` | `TRUE` |
| `OR` 的 terms 全部为 `FALSE` | `FALSE` |

**(c) 去重**：同一 AND / OR 层内按结构相等去重（`a = 1 AND a = 1` → `a = 1`）。
这是布尔幂等律的直接应用，也让后续的谓词下推候选集合更干净。

**边界**：化简结果为空 AND（`terms` 为空）是**非法节点**，绝不允许构造出来——
`LogicalFilter` 的 `__post_init__` 会拒绝。谓词整体化为 `TRUE` 时，处理方式是
**删除整个 Filter 节点**（由 §7.5 的冗余节点消除执行），不是留一个空 AND。

### 7.4 JOIN 谓词下推

**触发**：INNER JOIN 的某个 conjunct 只引用该 JOIN 的**单侧**列。conjunct 有两个来源，都要处理：

| 来源 | 形态 |
|---|---|
| WHERE | `LogicalFilter` 位于 `LogicalJoin` 之上 |
| ON | `LogicalJoin.on` 的 conjunct 列表 |

**改写**：

- 只引用左子树列的 conjunct → 下推到左子树（与其既有 Filter 合并）；
- 只引用右子树列的 conjunct → 下推到右子树；
- 同时引用两侧、或不引用任何列（如字面量 `TRUE`）的 conjunct → 留在原处；
- WHERE 来源：下推后原 Filter 若已无 conjunct，删除该 Filter 节点；
- ON 来源：留下的 conjunct 组成新的 ON。**ON 必须保持非空 AND 信封**，因此全部项都被推走时
  留一个 `TRUE` 占位，而不是构造空 AND——这是 §7.3「边界」那条约束在 JOIN 上的同构情形。

ON 下推不改变 JOIN 的 `schema`：`LogicalFilter` 的输出 Schema 恒等于其 child，
`join_schema(left, right)` 因此不受影响，节点不变式（`schema == join_schema(left, right)`）照常成立。

**为什么 ON 里的单侧项也属于本条规则**：`NestedLoopJoinExecutor` 对每个 (左,右) 组合求值一次 ON，
单侧项在另一侧的内层循环里是常量，留在 ON 里会被求值 `|左| × |右|` 次，下推后只求值 `|左|` 次，
且**不合格的行不再参与配对**，外层循环本身被缩小。更要紧的是这是 F7 的前置：索引访问需要
「绑定到单表某一列、且位于该表 Scan 之上」的 conjunct，而唯一能占据该位置的节点正是本条规则
造出的 `LogicalFilter`。`ON o.user_id = u.id AND o.total > 100` 若不拆，`o.total > 100` 永远停在 ON 里，
即使 `orders.total` 上有索引，选路器也没有可翻译的节点。

**「只引用单侧」的判定**：递归收集 conjunct 中全部 `BoundColumnRef` 的 `column.qualifier`，
若集合是左子树 `output_schema.qualifiers` 的子集即只引用左侧，右同理。
用 qualifier 而非 table，因为别名场景下 SQL 可见的限定符是别名
（`join_schema` 保证两侧 qualifier 不相交，因此判定不会两侧同时成立）。

**OR 的处理**：OR 节点内部的列引用按「整体并集」判定，不拆开判断。
`u.id = o.user_id OR u.age = 1` 同时引用左右时整体保留在 JOIN 之上——这与 V2 §5.2
「OR 跨多个来源时不得拆开下推」一致。

**下推时列的 index**：下推到左子树的 conjunct 引用的是左子树输出行的位置，
而 JOIN 左侧行的位置就是左子树输出的位置（`join_schema` 只对右侧列做整体偏移），
所以左侧下推无需重映射；右侧下推必须把每个 index 减去左子树列数。这一步必须显式做，
漏做会静默读到错误的列。

**等价性依据**：INNER JOIN 的过滤满足「先过滤再连接 = 先连接再过滤」（选择对 JOIN 的可分配性）。
V3 只有 INNER JOIN，没有 OUTER JOIN 的外表补 NULL 语义，因此下推不会漏行。ON 与 WHERE
在此依据上完全同构：`A ⋈_{q ∧ p} B = σ_p(A) ⋈_q B`（p 只引用 A 的列）。

### 7.5 投影裁剪与冗余节点消除

这条规则做三件事：自顶向下算出「每个节点必须输出哪些列」、按需重建 Schema 与列引用、
并把化简后失去意义的节点删掉。

**(a) 需求传播（自顶向下）**

设 `need(node)` 为父节点要求 `node` 输出的列集合（用 `(table, qualifier, name, type)` 作键去重）：

| 节点 | 向子节点传播的需求 |
|---|---|
| `LogicalProjection` | 自身 `columns` 引用的全部列 |
| `LogicalFilter` | 自身谓词引用的全部列 ∪ 父节点需求 |
| `LogicalJoin` | 左：ON 中左列 ∪ 父需求中的左列；右：ON 中右列 ∪ 父需求中的右列 |
| `LogicalScan` | 需求集合与自身 Schema 的交集，即裁剪后的输出列；交集为空时保留裁剪前 Schema 的首列（§5.1） |
| `LogicalEmpty` | 无子节点，Schema 原样保留 |

根节点是 `LogicalProjection`（Builder 保证所有 SELECT 都有 Projection 根），
其 `columns` 即初始需求，因此不需要额外的「全部保留」特例。

**两阶段执行（顺序不可颠倒）**：

1. **需求传播 + 自底向上重建**：递归求出每个节点的新 Schema。子节点的新 Schema 必须
   先于父节点确定——父节点的表达式要按子节点的新旧映射改写，映射依赖子节点的结果；
2. **自顶向下改写引用**：用步骤 1 得到的「子节点旧 Schema → 新 Schema」映射，
   改写父节点自身的谓词、ON、投影列（§5.2 的 `remap_column`）。

一次自顶向下的遍历无法完成这件事，因为父节点的列引用必须等子节点的裁剪结果落定。
把两个阶段分开还有一个好处：步骤 1 的产物是纯 Schema，可以独立断言，便于定位问题。

**(b) 重建与重映射**

- `LogicalScan` 按需求子序列重建 `schema`，index 从 0 重新编号；需求为空时按 §5.1 保留首列；
- 上层节点的表达式与投影列，按 §5.2 的映射重塑；
- `LogicalProjection` 本身**永不裁剪**：它承载最终列顺序与表头语义（V2 §5.2 明文要求
  「不删除承担最终列顺序和表头语义的 Projection」），只重映射其引用；
**(c) 冗余节点消除**

| 情形 | 处理 |
|---|---|
| Filter 谓词恒为 `TRUE` | 删除 Filter |
| Filter 谓词恒为 `FALSE` | 替换为 `LogicalEmpty(child.output_schema)` |
| Filter 的 child 是 `LogicalEmpty` | 替换为 `LogicalEmpty`，Schema 不变 |
| Join 任一侧是 `LogicalEmpty` | 替换为 `LogicalEmpty(join.output_schema)`（INNER JOIN 零行传染） |
| Projection 的 child 是 `LogicalEmpty` | **保留 Projection**：列数与表头仍需正确，行数为 0 |

**零列来源为什么保留一列，而不是换成 `LogicalEmpty`**：零列 Scan 的行数仍然有意义，
它决定 JOIN 的产生行数。以 `SELECT u.id FROM users u JOIN orders o ON u.active` 为例，
`orders` 的列一个都没被引用，但结果按「active 的 users 行数 × orders 行数」产生：
`ON` 为真时每个 (users, orders) 组合产出一行。若把需求为空的 `orders` 换成
`LogicalEmpty`，上层 JOIN 按「任一侧为零行则结果为零行」得到零行——这是改变行数的改写，
违反 §4.2。保留一列即可：该列没有任何引用者，留哪一列都不影响结果，固定取裁剪前
Schema 的首列只是为了让不动点判定稳定。

**`LogicalEmpty` 的 Schema 为什么必须传下去**：恒假 Filter 之上的 Projection 仍按旧
index 引用列，若 `LogicalEmpty` 不携带同构 Schema，Projection 的 `__post_init__` 会立刻报错，
而它的输出表头其实是正确的（0 行的结果集仍有列名）。这保证 `SELECT * FROM t WHERE FALSE`
返回「有表头、零行」而非报错。

**等价性依据**：裁剪只移除无人引用的列，被移除的列不参与任何谓词求值、不参与投影输出，
因此行数与每行的可见值都不变；冗余节点消除是恒真/恒假谓词与空集传染的直接推论。

## 8. `LogicalScan` 裁剪的执行侧配套

裁剪后的 `LogicalScan.schema` 是表 Schema 的子序列，因此 SeqScan 必须做投影：

```python
@dataclass(frozen=True, slots=True)
class SeqScanExecutor(RowExecutor):
    table: str
    schema: LogicalSchema
    source_indexes: tuple[int, ...]   # 输出列在表完整行中的位置
```

- `source_indexes[i]` 给出第 i 个输出列在 `storage.scan()` 行元组中的位置；
- `rows()` 从整行中按 `source_indexes` 取值，重排为新行元组；
- **不裁剪时 `source_indexes == (0, 1, ..., n-1)`**，行为与现状完全一致。

`source_indexes` 的来源有两条路：写进 `LogicalScan` 字段（优化器负责填），
或在执行器构建时反查表结构。**本文选择后者**：执行器构建期用 `context.storage.describe(table)`
取表完整列序，按列名定位每个输出列的位置。理由是前者会把「物理列位置」这一执行期信息
写进逻辑节点，让 `LogicalPlan → LogicalPlan` 的纯变换沾上物理细节；后者的代价是构建期
多一次 describe 调用，而这个调用本来就在会话内、结果可缓存。

## 9. 执行器接入

### 9.1 EmptyExecutor

```python
@dataclass(frozen=True, slots=True)
class EmptyExecutor(RowExecutor):
    schema: LogicalSchema

    def rows(self, context: ExecutionContext) -> Iterator[ExecRow]:
        return iter(())
```

零行、零副作用，不访问 Storage。

### 9.2 构建入口

`build_row_executor()` 增加一个分支：

```python
case LogicalEmpty():
    return EmptyExecutor(schema=plan.output_schema)
```

`ExecutorTreeBuilder.build()` 的既有分支不受影响：`LogicalEmpty`
只可能出现在 `LogicalProjection` 之下，根节点仍是 `LogicalProjection`。

## 10. 开关、API 与日志

### 10.1 开关位置

```python
def execute(
    self,
    sql: str,
    *,
    optimize: bool = True,
) -> QueryResult: ...
```

- **逐语句生效**，默认开启，既有调用零影响；
- `execute_script` / `execute_file` 提供同名关键字并透传给每条语句，
  使基准工具能对整段脚本统一关闭优化器；
- 开关只影响是否经过 `LogicalOptimizer.optimize()`，不影响绑定阶段，因此
  优化器开 / 关时**名称绑定与类型错误的行为完全一致**（这是 V2 §4.3 的验收项
  「优化器开关前后查询结果和错误行为一致」）。

**范围限定**：开关只作用于 SELECT。`LogicalUpdate` / `LogicalDelete` 的 child
（见 §3.3）与 DDL 完全不受 `optimize` 取值影响。

### 10.2 与 `physical` 的关系

F6 的 `physical: Literal["auto", "seq", "index"]` 与本开关是**正交**的两个维度：

| 维度 | 归属 | 作用点 |
|---|---|---|
| `optimize` | F5（本文） | 逻辑计划形态：有没有 Filter、是否下推、是否裁剪 |
| `physical` | F6（另文） | 物理路径：同一个逻辑 Scan 走顺序扫描还是索引访问 |

`optimize=False, physical="index"` 是合法组合：不做逻辑改写，但强制走索引。
两个参数互不推导、互不覆盖。

### 10.3 日志形态

```python
@dataclass(frozen=True, slots=True)
class RuleApplication:
    rule: str              # 规则名，稳定标识
    summary: str           # 规则自述的改写摘要（§6.1）
    plan_before: str       # 改写前的紧凑单行表示
    plan_after: str        # 改写后的紧凑单行表示


@dataclass(frozen=True, slots=True)
class OptimizationLog:
    original: LogicalPlan
    optimized: LogicalPlan
    applications: tuple[RuleApplication, ...]
    rounds: int            # 实际迭代轮数
    hit_limit: bool        # 是否因迭代上限中断（中断不影响正确性）
```

- `plan_before` / `plan_after` 用**紧凑单行表示**（如 `Projection -> Filter -> Join -> Scan`），
  不是完整多行树。理由：日志要进基准对比表与 TUI 单行展示，多行树会淹没表格；
  需要完整树时用计划自身的渲染接口，不塞进日志；
- **日志是优化器的返回值而非全局状态**：`optimize()` 返回 `OptimizationLog`，
  携带 `optimized` 计划本身。这样「计划」与「产生计划的记录」同源返回，
  不可能出现日志与实际执行计划不一致的情况；
- `Runner` 内部用该返回值拿计划，`OptimizationLog` 暂存备用；
- **追踪接入**：`Runner._optimize_plan()` 加追踪装饰器，一次 `optimize()` 调用上报
  一条 `optimizer` 记录，内容就是该调用真实返回的日志与耗时；规则命中明细
  （规则名、摘要、`plan_before` → `plan_after`）由返回值摊平后随记录提交，查看器
  无需解析计划快照。`optimize=False` 时没有可装饰的调用，由 Runner 显式提交一条
  禁用记录，使追踪把「开关关闭」与「上游失败没跑」区分开。优化器本身不感知
  `TraceSink`，也不因追踪改变返回值；
- 一条 `OptimizationLog` 对应一条语句的一次优化，不跨语句累积。

### 10.4 优化器不做的事

- 不读 `storage.statistics()`、不调 `list_indexes()`——那是 F6 的输入；
- 不 import `storage`，不接触 `BaseStorage`；
- 不抛任何 SQL 语义错误（`E_*`）：输入是已绑定且已通过节点不变式的计划，
  优化器只做保义改写，没有可报的语义问题；
- 不修改传入的计划对象（frozen dataclass 从类型层面保证）。

### 10.5 错误边界

优化器不新增错误码、不改变任何既有错误码的触发时机。因为优化只作用于 SELECT 且只在
绑定成功之后运行，绑定期错误（`E_TABLE_NOT_FOUND` / `E_AMBIGUOUS_COLUMN` /
`E_BOOLEAN_REQUIRED` 等）在优化器运行前就已抛出，开关与否行为一致。

## 11. 测试方案

### 11.1 规则单元测试

每条规则一组，直接构造计划、调用规则、断言改写后的计划形状：

| 规则 | 覆盖点 |
|---|---|
| 常量折叠 | 字面量比较的六种运算符；`NOT TRUE`；`TRUE AND` / `FALSE OR` 组合 |
| AND 展平与 Filter 合并 | 嵌套 AND 展平顺序；两层 Filter 合并；Join.on 展平 |
| 布尔规范化 | 德摩根两条；`NOT NOT`；六种运算符翻转；同层去重 |
| JOIN 下推 | WHERE 与 ON 两个来源：仅左列 / 仅右列 / 跨两侧 / OR 跨来源保留 / 不引用列的项留原位；右侧下推的 index 偏移；下推后空 Filter 删除；ON 全部推走时留 `TRUE` 占位信封 |
| 裁剪与冗余消除 | 需列传播；Scan 子序列重建；index 重映射；恒真 Filter 删除；恒假换 Empty；零列来源保留首列 |

计划形状断言用结构相等或节点类型序列，**不写完整树的字符串快照**——后者每次
节点字段调整都要批量改测试，收益不抵维护成本。

### 11.2 等价性测试（V3-T5）

这是本设计的**一线验收**，分三层：

1. **golden 全量**：`tests/golden_sql.py` 的 37 条 V1 golden，在 `optimize=True` 下
   逐条与 `optimize=False` 的结果比对（列、表头、行多重集合、`affected_rows`、错误码）。
   参数化跑双模式，成本最低、覆盖最广；
2. **V2 用例**：JOIN、BOOLEAN、别名、脚本执行的既有测试同样参数化跑双模式；
3. **随机小表**：随机生成小表数据与查询，比对双模式结果。这一层专门抓
   「规则组合起来才出错」的情况——单条规则各自正确、叠加后漏行是本设计最大的残余风险。

### 11.3 专项风险测试

- **不下推也正确**：构造只在 `optimize=False` 下执行的同一批查询，断言结果与
  已下推版本一致（V3 §11 明文要求「必须保留不下推也正确的测试」）；
- **ON 下推的等价性**：同一条件分别写在 `WHERE` 与 `ON`，断言两者结果一致、且都与
  `optimize=False` 一致；`ON u.active` 这类「全部项都是单侧」的查询单独覆盖，
  它是 `AND(TRUE)` 占位信封的唯一触发路径，同时锁住零列来源的行数语义（§5.1）；
- **恒假与空表**：`WHERE FALSE`、空表、`WHERE` 命中零行，断言返回「有表头、零行」；
- **零列来源（JOIN 的重复语义）**：构造「JOIN 的一侧列完全不被引用」的查询
  （如 `SELECT u.id FROM users u JOIN orders o ON u.active`），断言结果与 `optimize=False`
  下的结果行数一致。这条专测 §5.1 的零列来源保留策略——它是裁剪最容易被漏掉的语义边界；
- **不动点**：断言每轮迭代后计划结构相等即停止，且 `rounds <=` 迭代上限；
- **列 index 正确性**：裁剪后所有节点的 `__post_init__` 全部通过，
  即 `_check_column` 对整棵树无违反——这是裁剪正确性的强校验，不需要额外断言。

### 11.4 不做时序断言

与 V3 §11 一致：不断言墙钟耗时。优化器的收益用**节点数、计划规模、页读取计数**
这类确定性指标表达，不用时间阈值。

## 12. 实施顺序

1. 新增 `LogicalEmpty` 计划节点与不变式校验，单独补节点测试；
2. 新增 `EmptyExecutor` 并接入 `build_row_executor()`，
   先用「手工构造 Empty 计划」的测试验证列序与表头，**此时还没有任何规则**；
3. 实现规则 1–3（纯表达式与谓词层改写，不碰 Schema），补规则单测；
4. 实现规则 4（JOIN 下推），补单测与「不下推也正确」对照测试；
5. 实现规则 5（裁剪与冗余消除）——**单独一步，风险最高**：
   先做「Scan 恒全列」的裁剪，跑通后再打开 Scan 子集输出，分两次提交；
6. 接入 `OptimizationLog` 与 `optimize()` 驱动；
7. 在 `Runner.execute` / `execute_script` / `execute_file` 接入 `optimize` 开关；
8. golden 与 V2 用例参数化跑双模式，补随机等价性测试；
9. 全量回归。

第 5 步分两次提交的理由：裁剪是唯一大面积改动列 index 的规则，Scan 子集输出又是唯一
触及执行侧读行的改动，两者叠加出问题时分不清是哪一层引起的。

## 13. 验收标准

- 五条规则均有单测覆盖，且每条都有明确的等价性依据（§7 各条已给出）；
- `optimize=True` / `optimize=False` 下，37 条 V1 golden 与全部 V2 用例的结果逐条一致（V3-T5）；
- 优化器开关前后**错误行为**一致：绑定与类型错误在两种模式下同码同文；
- 优化器可整体关闭，关闭时执行路径与 V3 之前完全一致；
- `OptimizationLog` 能说明「哪些规则、在哪些节点、各命中几次」，`hit_limit` 可观测；
- 优化器不 import `storage`、不读统计信息、不抛 SQL 语义错误；
- `UPDATE` / `DELETE` 的计划树不因 `optimize` 取值而改变（§3.3 的硬约束，
  用「开关前后 DML 计划结构相等」的断言锁住）；
- 全量回归通过，V1 37 条 golden 与 V2 全部用例不倒退（V3-T9）。
