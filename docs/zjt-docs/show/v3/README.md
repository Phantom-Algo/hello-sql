# V3 实机验证手册（索引 · 代价选路 · 优化器 · 全链路追踪）

V3 的目标是让 hello-sql「自己挑最省的路走，并且拿得出证据」。本文按可复制的
顺序列出验证步骤，每条命令都已在当前代码上实跑通过。

- 所有命令都在**仓库根目录**执行。
- 每个小节用**独立的数据目录**，互不干扰，可以任意顺序单独跑。
- 预期输出直接写在命令下方，用来对照。

## 0. 先说三条会影响你写测试脚本的事实

1. **SQL 不支持注释**。词法层没有 `--` 或 `#`，脚本里写注释会直接报
   `[E_SYNTAX] unexpected character '-'`。测试 SQL 请保持纯语句。
   （本文 bash 代码块里的 `#` 是 shell 注释，不进入 SQL。）
2. **没有 `EXPLAIN` 语句**。选路理由和代价不在 SQL 层暴露，只能通过
   全链路追踪（§8）或 §4 的证据脚本、§7 的 Python 公开属性观察。
3. **`--physical` 在交互会话里同样生效**，会话中还可以用
   `/physical auto|seq|index` 随时切换。强制模式会显示在提示符里
   （如 `main [index] ❯`），不会忘记当前状态。

环境准备（已装好可跳过）：

```bash
.venv/bin/python --version          # 需要 3.11+
.venv/bin/python main.py --version
```

下文统一用 `.venv/bin/python main.py` 调用。已经 `./install.sh` 装过命令的话，
把 `.venv/bin/python main.py` 换成 `hello-sql` 即可，参数完全一致。

## 1. 冒烟：一条命令确认 V3 已经接通

```bash
.venv/bin/python main.py --data-dir /tmp/v3-smoke -e "
CREATE TABLE users (id INT, name TEXT);
CREATE INDEX idx_users_id ON users (id);
INSERT INTO users VALUES (1, 'alice');
INSERT INTO users VALUES (2, 'bob');
SELECT * FROM users WHERE id = 2;
"
```

预期输出（最后一段是查询结果）：

```text
0 row(s) affected
0 row(s) affected
1 row(s) affected
1 row(s) affected
id	name
2	bob
```

索引能被解析（A）、建立（B）、并在查询中被选中（C），说明三层都已接通。

## 2. 索引 DDL 与错误语义（对应验收项 V3-T1）

准备一张表：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-ddl -e "CREATE TABLE t (id INT, name TEXT);"
```

然后逐条执行，**每条命令单独跑**，方便看清错误码：

```bash
P=".venv/bin/python main.py --data-dir /tmp/v3-ddl"

$P -e "CREATE INDEX idx_t_id ON t (id);"        # 成功
$P -e "CREATE INDEX idx_t_id ON t (id);"        # 索引重名
$P -e "CREATE INDEX idx_bad ON t (nope);"       # 列不存在
$P -e "CREATE UNIQUE INDEX ux ON t (id);"       # UNIQUE 不支持
$P -e "CREATE INDEX idx_x ON nosuch (id);"      # 表不存在
$P -e "CREATE INDEX __sys_x ON t (id);"         # 保留前缀
$P -e "DROP INDEX idx_t_id;"                    # 成功
$P -e "DROP INDEX idx_t_id;"                    # 已经删掉了
```

预期错误码（错误走 stderr，退出码为 1）：

| 命令 | 错误码 |
|---|---|
| 重名建索引 | `E_INDEX_EXISTS` |
| 索引列不存在 | `E_COLUMN_NOT_FOUND` |
| `CREATE UNIQUE INDEX` | `E_SYNTAX` |
| 目标表不存在 | `E_TABLE_NOT_FOUND` |
| 删不存在的索引 | `E_INDEX_NOT_FOUND` |
| `__sys_` 开头 | `E_BAD_ARG` |

再验证「先有数据、后建索引」的**回填**能力：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-backfill -e "
CREATE TABLE t (id INT, v TEXT);
INSERT INTO t VALUES (1, 'a');
INSERT INTO t VALUES (2, 'b');
INSERT INTO t VALUES (3, 'c');
CREATE INDEX ix ON t (id);
"
.venv/bin/python main.py --data-dir /tmp/v3-backfill --physical index -e "SELECT * FROM t WHERE id = 2;"
```

预期能查到 `2	b`，说明建索引时对存量数据做了回填。

## 3. 索引与 DML 的一致性（对应验收项 V3-T2）

```bash
P=".venv/bin/python main.py --data-dir /tmp/v3-cons"
rm -rf /tmp/v3-cons
$P -e "CREATE TABLE t (id INT, v TEXT); CREATE INDEX ix ON t (id);"
$P -e "INSERT INTO t VALUES (1,'a'); INSERT INTO t VALUES (2,'b'); INSERT INTO t VALUES (3,'c');"
```

用 `--physical index` 强制走索引，绕开选路，单独检验索引内容是否正确：

```bash
$P --physical index -e "SELECT * FROM t WHERE id = 2;"     # 应命中 2	b
$P -e "UPDATE t SET id = 20 WHERE id = 2;"
$P --physical index -e "SELECT * FROM t WHERE id = 20;"    # 应命中 20	b
$P --physical index -e "SELECT * FROM t WHERE id = 2;"     # 应为空（旧键已摘除）
$P -e "DELETE FROM t WHERE id = 3;"
$P --physical index -e "SELECT * FROM t WHERE id = 3;"     # 应为空
$P --physical index -e "SELECT * FROM t WHERE id >= 20;"   # 区间查找，应命中 20	b
```

`UPDATE` 改键后旧键查不到、新键查得到，`DELETE` 后查不到，即索引与表数据一致。

最后验证删表会连带清理索引（索引名可以复用）：

```bash
$P -e "DROP TABLE t; CREATE TABLE t (id INT); CREATE INDEX ix ON t (id);"
```

预期输出三行 `0 row(s) affected`（删表、建表、建索引各一行）。
如果删表没有连带清理索引，最后一条建索引会报 `E_INDEX_EXISTS`。

## 4. 代价选路：`auto` 的两副面孔（对应验收项 V3-T6）★

这是 V3 的核心看点。先装入 100 行、20 列的演示夹具（约 5 个数据页）：

```bash
rm -rf /tmp/v3-select
.venv/bin/python main.py --data-dir /tmp/v3-select -f docs/zjt-docs/show/v3/v3_demo_load.sql > /dev/null
```

夹具故意用「宽表 + 固定宽度 INT 列」，目的是用尽量短的 SQL 文本堆出足够多的
数据页，让顺序扫描与索引访问的代价分水岭落在 100 行这个量级上。
`v3_demo_load.sql` 已经建好 `idx_wide_id`（id 列）和 `idx_wide_grp`（grp 列）。

用证据面板一次看完所有决策：

```bash
.venv/bin/python docs/zjt-docs/show/v3/v3_evidence.py --data-dir /tmp/v3-select
```

输出中的决策表（数值是确定性的，每次运行都一样）：

| SQL | reason | 选择性 | 估计行数 | 扫描代价 | 索引代价 | 候选索引请求（按优先级） |
|---|---|---|---|---|---|---|
| `WHERE id = 42` | `INDEX_EQUALITY` | 0.0100 | 1.00 | 5.00 | **3.00** | `IndexLookup(id)` |
| `WHERE grp = 1` | `SEQ_CHEAPER` | 0.5000 | 50.00 | **5.00** | 52.00 | 无 |
| `WHERE id > 10` | `SEQ_CHEAPER` | 0.9091 | 90.91 | **5.00** | 92.91 | 无 |
| `WHERE id > 1000` | `INDEX_RANGE` | 0.0000 | 0.00 | 5.00 | **2.00** | `IndexRange(id)` |
| `WHERE m1 = 500` | `NO_MATCHING_INDEX` | – | – | – | – | 无 |
| 没有 `WHERE` | `NO_PREDICATE` | – | – | – | – | 无 |
| `WHERE id = 42 AND grp = 0` | `INDEX_EQUALITY` | 0.0100 | 1.00 | 5.00 | **3.00** | `IndexLookup(id)`、`IndexLookup(grp)` |

代价单位是**预计的数据页读取次数**：顺序扫描按读满 `page_count` 页算，
索引访问按「B+ 树高度 + 每命中一行一次回表」算。这张表 5 页、100 行，树高 2，
所以索引代价是 `2 + 1 = 3`。

要读懂的四件事：

1. **有索引不一定用**。`grp` 上有索引，但命中一半（50 行）、索引代价 52 远大于
   扫描代价 5，优化器主动放弃，退回顺序扫描。
2. **区间太宽也会放弃**。`id > 10` 命中约 91% 的行，同样退回顺序扫描。
3. **区间键越界时反而走索引**。`id > 1000` 超出 `max=100`，估算 0 行，
   索引代价降到 2，直接走索引返回空集。
4. **多个条件会生成多个候选请求**。`id = 42 AND grp = 0` 排出一个按优先级排序的
   候选列表 `[IndexLookup(id), IndexLookup(grp)]`：执行器**只取第一个可用索引**
   取行，只有该列没有索引（B 抛 `E_INDEX_NOT_FOUND`）才顺延到下一个候选，
   其余条件留在 C 侧做残余过滤——不是把两个索引的结果求交。

那 `INDEX_EQUALITY` / `SEQ_CHEAPER` 到底有没有真落到执行器上？看这两条：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-select -e "SELECT id FROM wide_facts WHERE id = 42;" 
.venv/bin/python main.py --data-dir /tmp/v3-select -e "SELECT id FROM wide_facts WHERE grp = 1;"
```

两条结果都对，但执行的算子不同——在 §8 的追踪里能直接看到
`IndexScanExecutor` 与 `SeqScanExecutor` 的区别。

## 5. 强制物理模式（对应验收项 V3-T7）

`--physical` 三选一，只影响选路，不影响结果：

```bash
P=".venv/bin/python main.py --data-dir /tmp/v3-select"

$P --physical auto  -e "SELECT id FROM wide_facts WHERE id = 42;"   # 按代价选 → 索引
$P --physical seq   -e "SELECT id FROM wide_facts WHERE id = 42;"   # 强制顺序扫描
$P --physical index -e "SELECT id FROM wide_facts WHERE id = 42;"   # 强制索引
```

三条命令输出必须**完全相同**：

```text
id
42
```

`--physical` 对交互会话同样有效，并且在会话里可以用 `/physical` 随时切换。
**这是把「强制模式」和「全链路追踪」接起来的唯一方式** —— `-e` / `-f` 能强制
但只打印结果，只有 TUI 能把两者放到一起：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-select
```

```text
/physical index
SELECT id FROM wide_facts WHERE id = 42;
/inspect C
/physical auto
```

`/physical` 不带参数时只显示当前模式。切到 `index` 后提示符会变成
`main [index] ❯`；此时在查看器的 `13 C Executor Tree` 阶段里，
`executor.plan_access` 的 `reason` 就是 `FORCED_INDEX`，Runtime 首个事件是
`runtime.index_scan.rows`。换成 `seq` 再跑一次，两者分别变成 `FORCED_SEQ`
与 `runtime.seq_scan.rows`，结果行数不变。

强制模式的边界行为（这两条是设计约定，不是 bug）：

```bash
# 谓词所在列没有索引 → 由 B 抛 E_INDEX_NOT_FOUND，不静默退化为顺序扫描
$P --physical index -e "SELECT id FROM wide_facts WHERE m1 = 500;"

# 整条查询没有任何可翻译成索引访问的条件 → 由 C 抛 E_BAD_ARG
$P --physical index -e "SELECT * FROM wide_facts;"
```

预期：

```text
[E_INDEX_NOT_FOUND] no index on wide_facts.m1
[E_BAD_ARG] physical="index" requires at least one index-eligible predicate
```

「不得静默退化」是刻意设计：否则基准会把「没走索引」误判成「走了索引」。

再验证一次第 4 节说的**候选请求顺延**（在一个数据副本上做，别破坏 §4 的目录）：

```bash
rm -rf /tmp/v3-fallback
.venv/bin/python main.py --data-dir /tmp/v3-fallback -f docs/zjt-docs/show/v3/v3_demo_load.sql > /dev/null

# 两个索引都在：用 id（优先级更高）这一个候选
.venv/bin/python main.py --data-dir /tmp/v3-fallback --physical index -e "SELECT id FROM wide_facts WHERE id = 42 AND grp = 0;"

# 拆掉 id 索引：候选顺延到 grp，结果依然是 42
.venv/bin/python main.py --data-dir /tmp/v3-fallback -e "DROP INDEX idx_wide_id;"
.venv/bin/python main.py --data-dir /tmp/v3-fallback --physical index -e "SELECT id FROM wide_facts WHERE id = 42 AND grp = 0;"
```

强制 `index` 模式下 C 不查索引是否存在，所以两个索引都拆掉之后才会失败，
并且报的是最后一个候选的列：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-fallback -e "DROP INDEX idx_wide_grp;"
.venv/bin/python main.py --data-dir /tmp/v3-fallback --physical index -e "SELECT id FROM wide_facts WHERE id = 42 AND grp = 0;"
```

预期：`[E_INDEX_NOT_FOUND] no index on wide_facts.grp`。

## 6. 性能对比：索引到底快多少

装入 800 行夹具（约 35 个数据页），再跑三模式对比：

```bash
rm -rf /tmp/v3-perf
.venv/bin/python main.py --data-dir /tmp/v3-perf -f docs/zjt-docs/show/v3/v3_perf_load.sql > /dev/null
.venv/bin/python docs/zjt-docs/show/v3/v3_evidence.py --data-dir /tmp/v3-perf --bench
```

`--bench` 部分的典型输出（绝对耗时随机器变化，比值稳定）：

```text
SQL: SELECT id FROM wide_facts WHERE id = 42;   （高选择性）
  auto   reason=INDEX_EQUALITY   中位耗时=   0.33 ms   返回行数=1
  seq    reason=FORCED_SEQ       中位耗时=   3.74 ms   返回行数=1
  index  reason=FORCED_INDEX     中位耗时=   0.32 ms   返回行数=1
  三模式结果一致: True

SQL: SELECT id FROM wide_facts WHERE id > 80;   （低选择性）
  auto   reason=SEQ_CHEAPER      中位耗时=   4.11 ms   返回行数=720
  seq    reason=FORCED_SEQ       中位耗时=   4.16 ms   返回行数=720
  index  reason=FORCED_INDEX     中位耗时=  17.47 ms   返回行数=720
  三模式结果一致: True
```

两句话读结论：

- **高选择性**：`auto` 选中索引，比强制顺序扫描快约一个数量级（本机 ~11×）。
- **低选择性**：`auto` 放弃索引，比强制索引快约 4×——证明第 4 节里
  「主动放弃」不是保守，而是划算。

也可以用命令行看单次耗时（TTY 下会打印 `· 0.003s`）：

```bash
.venv/bin/python main.py --data-dir /tmp/v3-perf --physical seq   -e "SELECT id FROM wide_facts WHERE id = 42;"
.venv/bin/python main.py --data-dir /tmp/v3-perf --physical index -e "SELECT id FROM wide_facts WHERE id = 42;"
```

每次调用都是新进程、冷缓存，两边条件对等；建议各跑两遍取稳定值。

> 说明：列统计的**基数**来自有界采样（最多 16 个数据页），800 行夹具的
> `id` 会显示 `distinct_count` 约 368 而不是 800——代价模型正是在这种
> 「基数可能偏低」的输入上工作。
>
> 但 **`min_value` / `max_value` 是精确的**（B 的 D47，契约 3.1）：它们不是
> 采样窗口的极值，而是整张表的真实极值。C 的选择性估算可以据此做"键越界即
> 0 行"的推论。早期版本这里是采样极值，曾让 `WHERE id > 大值` 被估成 0 行
> 而误选索引，已修复。

## 7. 逻辑优化器（对应验收项 V3-T5）

优化器是 `LogicalPlan → LogicalPlan` 的纯变换，共五条规则，按固定顺序迭代到不动点：

| 规则 | 作用 |
|---|---|
| `fold_constants` | 常量折叠 |
| `flatten_and_merge_filters` | AND 展平、多层 Filter 合并 |
| `normalize_booleans` | NOT 下推、真值化简、同层去重 |
| `push_join_predicates` | WHERE conjunct 与单侧 ON 条件下推到 JOIN 两侧 |
| `prune_and_eliminate` | 投影裁剪、冗余节点消除 |

开关是 `Runner.execute(sql, optimize=True/False)`，逐语句生效；**DDL 与 DML
的计划不做优化**（UPDATE / DELETE 用输出行整列替换，列序改写会错位写入）。

用证据面板看规则命中日志与等价性校验：

```bash
.venv/bin/python docs/zjt-docs/show/v3/v3_evidence.py --data-dir /tmp/v3-select | sed -n '/优化器/,$p'
```

典型输出：

```text
迭代轮数=2  触顶=False  规则命中次数=4
原始计划: Projection -> Filter -> Join(Scan(wide_facts×20), Scan(wide_facts×20))
优化计划: Projection -> Join(Filter -> Scan(wide_facts×1), Filter -> Scan(wide_facts×2))
  [fold_constants] 折叠 1 处常量表达式
  [normalize_booleans] 化简 1 处常量项
  [push_join_predicates] 下推 1 项到左子树；下推 1 项到右子树
  [prune_and_eliminate] 裁剪 2 处来源（列 -37）
优化开的结果行数=1  优化关的结果行数=1
两种模式结果逐行一致: True
```

看点是最后两行：谓词下推到 JOIN 两侧、投影从 20 列裁到 1+2 列，结果逐行不变。

想自己写等价性对比，直接用公开 API：

```python
from compiler import parse, parse_script
from runner import Runner
from storage import DatabaseServer

runner = Runner(server=DatabaseServer("/tmp/v3-select"), parse=parse, parse_script=parse_script)
sql = "SELECT w.id, g.grp FROM wide_facts w INNER JOIN wide_facts g ON w.id = g.id WHERE w.id = 42;"
assert runner.execute(sql, optimize=True) == runner.execute(sql, optimize=False)
print("优化前后结果一致")
```

## 8. 全链路追踪：trace 怎么用

追踪是 A/B/C 共 14 个阶段的真实调用记录，**不会为了出图重新执行 SQL**。

### 8.1 交互式看（推荐）

```bash
.venv/bin/python main.py --data-dir /tmp/v3-select
```

进去后先跑一条想观察的 SQL，再输入 `/inspect` 系列命令（参数不区分大小写）：

| 命令 | 显示阶段 |
|---|---|
| `/inspect` | ALL：A+B+C 完整 14 阶段 |
| `/inspect A` | Lexer / Parser / AST / SourceSpan |
| `/inspect B` | Catalog / Buffer Cache / Pager / Storage Engine |
| `/inspect C` | REPL / Binder / Logical Plan / Optimizer / Executor / Runtime |

`/inspect` 会在终端打印一张阶段总览表，并**打开系统默认浏览器**的查看器
（只监听 `127.0.0.1` 的随机端口）。你也可以顺手执行这几条来对比：

```sql
SELECT id FROM wide_facts WHERE id = 42;
SELECT id FROM wide_facts WHERE grp = 1;
```

浏览器查看器里专门为 V3 准备了三处证据：

1. **Pipeline 左栏**：`13 C Executor Tree` 阶段有 2 个事件，其中
   `executor.plan_access` 的返回值就是这次选路的 `AccessPath`——里面写着
   `reason`、`selectivity`、`estimated_rows`、`seq_cost`、`index_cost`。
   第 4 节的决策表就是这份数据。
2. **NODES 树**：Executor 子树里出现 `IndexScanExecutor` 还是 `SeqScanExecutor`，
   直接说明选路结果有没有真的落到执行器。
3. **STAGE 面板的 Runtime 事件**：索引路径的首个事件是
   `runtime.index_scan.rows`，顺序扫描是 `runtime.seq_scan.rows`。

页面右侧还有 `STAGE / NODES / TOKENS / PAGES` 四个面板，可以点 SQL 原文里的
Token、AST/计划节点、物理页互相联动高亮；顶部 `RAW DATA` 开关控制原始 JSON 的
显示与隐藏。`RESET LINK` 清除选择。

### 8.2 非交互式看（管道 / 重定向）

自动降级为纯文本，不弹窗：

```bash
printf "SELECT id FROM wide_facts WHERE id = 42;\n/inspect C\n/quit\n" \
  | .venv/bin/python main.py --data-dir /tmp/v3-select
```

输出形如：

```text
Query #1 trace-000001 [SUCCESS] module=C
01 C REPL [SUCCESS] events=1 elapsed=0.000ms
10 C Binder [SUCCESS] events=5 elapsed=0.387ms
11 C Logical Plan [SUCCESS] events=1 elapsed=0.437ms
12 C Optimizer [SUCCESS] events=1 elapsed=0.163ms
13 C Executor Tree [SUCCESS] events=2 elapsed=1.510ms
14 C Runtime [SUCCESS] events=4 elapsed=0.789ms
```

文本模式只给阶段级摘要：第 12 阶段的规则命中明细、以及选路理由，都要看
浏览器查看器或 §4 的证据脚本。

### 8.3 第 12 阶段看什么

`C Optimizer` 不再是占位阶段：它在每条语句上提交一条真实记录，阶段输出
`optimization` 直接给出轮数、是否触顶、逐规则命中数与改写前后的单行对照：

```json
{
  "rounds": 2,
  "hit_limit": false,
  "application_count": 2,
  "rule_hits": {"fold_constants": 1, "prune_and_eliminate": 1},
  "applications": [
    {"rule": "fold_constants", "summary": "折叠 1 处常量表达式",
     "plan_before": "Projection -> Filter -> Scan(t×2)",
     "plan_after":  "Projection -> Filter -> Scan(t×2)"},
    {"rule": "prune_and_eliminate", "summary": "裁剪 1 处来源（列 -1）；删除 1 处恒真 Filter",
     "plan_before": "Projection -> Filter -> Scan(t×2)",
     "plan_after":  "Projection -> Scan(t×1)"}
  ]
}
```

阶段状态按真实原因区分：跑过是 `SUCCESS`，`optimize=False` 是 `DISABLED`
（带 `reason`），绑定失败没跑到是 `SKIPPED`。三条口径分别对应开关打开、
开关关闭与上游失败，验收优化器自身仍以 §7 的日志为准。

## 9. 其他你可能没注意到的功能

### 9.1 脚本与批量执行

```bash
P=".venv/bin/python main.py --data-dir /tmp/v3-misc"

$P -e "CREATE TABLE a (id INT); INSERT INTO a VALUES (1); SELECT * FROM a;"   # -e 支持多语句
$P -f docs/zjt-docs/show/v3/v3_demo_load.sql                                  # -f 执行 SQL 文件
```

默认遇错停止；加 `--continue-on-error` 记录错误后继续，退出码仍为 1。
交互式下用 `/stop-on-error on|off` 切换同一行为。

### 9.2 TUI 命令与快捷键

| 命令 / 快捷键 | 行为 |
|---|---|
| `/help` | 帮助 |
| `/databases`、`/tables` | 列出数据库 / 当前库的表 |
| `/describe wide_facts` | 查看列名与类型 |
| `/file 路径` | 执行 SQL 文件（路径含空格用引号） |
| `/stop-on-error on\|off` | 脚本遇错停止 / 继续 |
| `/physical [auto\|seq\|index]` | 查看或切换物理访问模式（不带参数只显示当前值） |
| `/inspect [ALL\|A\|B\|C]` | 查看最近 SQL 的追踪 |
| `/clear`、`/quit` | 清屏、退出（`quit`、`exit`、`Ctrl+D` 亦可） |
| Enter / Alt+Enter | 执行当前缓冲区 / 插入换行 |
| Tab、↑↓ | 补全（关键字、库名、表名、`/inspect` 参数）、历史 |
| Ctrl+C | 清空尚未提交的输入（**不是**事务回滚） |

一个输入缓冲区可以放多行、多条 SQL；末尾分号可省略，多条之间必须用分号。

### 9.3 命令行参数

```bash
--data-dir PATH        # 数据目录；优先级 --data-dir > HELLO_SQL_DATA_DIR > ~/.hello-sql/data
-D / --database NAME   # 初始库，默认 main
--plain                # 纯文本，关闭艺术字与颜色
--no-history           # 不读写磁盘历史
--physical {auto,seq,index}   # 物理模式，对 -e / -f 与交互会话都生效
--continue-on-error
```

非交互输入输出自动使用纯文本，不打印欢迎页与颜色。

### 9.4 存储侧新接口（B 的 V3 增量，C 只在选路时消费）

TUI 目前**没有**「列出索引」和「查看统计」的命令，需要时直接用公开接口：

```python
from storage import DatabaseServer

storage = DatabaseServer("/tmp/v3-select").connect("main")
print(storage.list_indexes("wide_facts"))   # [IndexInfo(name='idx_wide_grp', ...), ...]
stats = storage.statistics("wide_facts")
print(stats.row_count, stats.page_count)    # 100 5
for column in stats.columns:                # columns 按建表列序完整返回
    print(column.name, column.distinct_count, column.min_value, column.max_value)
```

另有 `index_lookup(table, column, key)` 与
`index_range(table, column, lower, upper, lower_inclusive=..., upper_inclusive=...)`
两个迭代器接口，返回的 `Row` 形状与 `scan()` 完全一致。这两个接口**不接收操作符**：
把 `>` `>=` 翻译成键区间的责任在 C 侧。

### 9.5 终端外观与预览

交互式启动会打开一个 pyfiglet 艺术字欢迎页；数据目录、当前库都会显示。
不启动数据库也能导出静态预览：

```bash
.venv/bin/python -m scripts.preview_terminal /tmp/hello-sql-preview.svg
```

### 9.6 数据目录与旧数据

默认数据目录是 `~/.hello-sql/data`，**不随当前工作目录变化**。想继续用仓库里
旧的 `data/`，需要显式指定：

```bash
.venv/bin/python main.py --data-dir ./data --database shop
```

底层仍是单进程单线程，不要让多个进程同时写同一个数据目录。

## 10. 回归测试

```bash
.venv/bin/python -m pytest -q
```

当前基线：**1235 passed**。C 模块单独的用例：

```bash
.venv/bin/python -m pytest runner/tests -q
.venv/bin/python -m pytest UI/tests -q
```

## 11. 已知缺口（验收时会遇到，先说明）

| 项 | 现状 | 影响 |
|---|---|---|
| 基准工具 F8 | 仓库里没有 `bench/`，未落地 | 没有三模式对比报告文件，本文 §6 用证据脚本替代 |
| `EXPLAIN` | 不存在 | 选路理由只能从追踪或 Python API 读 |
| SQL 注释 | 词法不支持 `--` / `#` | 测试脚本不能带注释 |
| TUI 索引 / 统计查看 | 无对应命令 | 需用 §9.4 的 Python 接口 |
| TUI 优化开关 | 无 `/optimize` 命令 | 开关只能走 `execute`/`execute_script` 的关键字参数 |
| 索引能力边界 | 不支持 UNIQUE、多列组合索引、索引覆盖扫描 | 按设计文档 §2.2 属本版非目标 |
| 统计口径 | 极值（`min` / `max`）精确；基数（`distinct_count`）为最多 16 页的有界采样 | 大表 `distinct_count` 会偏低（§6 已说明）；极值不受采样影响 |
| 回表定位 | 冷进程下 `_locate` 需逐页探测（`rid→页` 映射未建立） | 同一进程内第二次起才 O(1)；B 侧已登记为 D49（回表 O(1)）待排期 |

## 附：本目录文件

| 文件 | 说明 |
|---|---|
| `README.md` | 本文 |
| `v3_demo_load.sql` | 100 行 / 20 列夹具，含两个索引；用于 §4 选路演示 |
| `v3_perf_load.sql` | 800 行 / 20 列夹具，含两个索引；用于 §6 性能对比 |
| `v3_evidence.py` | 证据面板：打印选路决策表、优化器规则日志、三模式耗时对比 |
