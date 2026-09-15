# V3 演示手册（索引 · 代价选路 · 优化器 · 全链路追踪 · 基准）

这份手册把 V3 的全部能力按**可复制的顺序**走一遍。每条命令都在当前代码上
实跑过，下面的"实录"就是真实输出（不是示范文本）。

所有命令都在**仓库根目录**执行，并且用**独立数据目录**，互不干扰。

## 0. 三条会踩的坑，先说清楚

1. **SQL 不支持注释**（`--` / `#` 会直接 `E_SYNTAX`）。注释只能写在本文里。
2. **没有 `EXPLAIN`**。选路理由与代价不在 SQL 层暴露，要看 `/inspect`（交互）
   或本文的取证面板 `v3_showcase_evidence.py`。
3. 批量装载请加 `--no-trace`：交互模式需要追踪（`/inspect`），但对几千条
   `INSERT` 的脚本来说，追踪快照比真正写数据还贵——实测 2000 行 **40s → 3.2s**。

## 1. 准备数据（2000 行 / 25 数据页）

```bash
rm -rf /tmp/v3show
python main.py --no-trace --data-dir /tmp/v3show -f docs/v3-dev/demo/v3_showcase_load.sql
```

装载脚本做三件事：建 `events(id, amount, grp, note)`、插 2000 行
（`amount=id%97`、`grp=id%2`）、在 `id` 与 `grp` 上建索引；再建一张三行的
`labels` 表供 JOIN 演示。

> 选 2000 行是刻意的：25 个数据页刚好让代价模型的"分水岭"落在演示能看见的位置
> （见第 4 幕的 1990 / 1900 两条）。

## 2. 场景一览与覆盖矩阵

| 幕 | 内容 | 覆盖的设计 |
|---|---|---|
| 1 | 索引 DDL 与错误码 | F1、V3-T1、契约错误归属 |
| 2 | DML 与索引同步 | F2/F3、V3-T2、D40 |
| 3 | 优化器五条规则 + 开/关等价 | F5、V3-T5 |
| 4 | 代价选路七种理由 | F6、V3-T6、D47（精确极值） |
| 5 | 强制物理模式与错误归属 | F7、V3-T7、DV3-12 |
| 6 | 谓词下推 + 残余过滤 | F7、§7.5 |
| 7 | JOIN 单侧下推与强制索引边界 | F5、DV3-12 |
| 8 | 统计语义（精确 vs 近似） | F4、V3-T4、契约 3.1 |
| 9 | 全链路追踪 `/inspect` | 追踪契约、优化器阶段 |
| 10 | 重启持久化 | D38/D39、D49a |
| 11 | 基准报告 | F8、V3-T8 |

## 3. 逐幕脚本

### 第 1 幕：索引 DDL 与错误码（A 的文法 + 三方的错误归属）

```bash
P="python main.py --no-trace --data-dir /tmp/v3show"

$P -e "CREATE INDEX idx_events_note ON events (note);"   # TEXT 列也能建索引
$P -e "CREATE UNIQUE INDEX ux ON events (id);"           # E_SYNTAX（A：文法不支持 UNIQUE）
$P --continue-on-error -f docs/v3-dev/demo/v3_showcase_errors.sql   # 其余 5 个码一次看完
```

实录（错误矩阵）：

```text
[E_BAD_ARG] reserved index name: __sys_bad
[E_COLUMN_NOT_FOUND] column not found in table events: nocol
[E_INDEX_EXISTS] index already exists: idx_events_id
[E_INDEX_NOT_FOUND] index not found: nope
[E_TABLE_NOT_FOUND] table not found: nosuch
```

看点：`E_SYNTAX` 是**解析期**错误——整段脚本会中止（这本身就是设计：
先整段解析再执行）；其余 5 个是**语义/存储期**错误，`--continue-on-error`
能让它们一次跑完，退出码仍为 1。

### 第 2 幕：DML 与索引同步

```bash
$P -e "INSERT INTO events VALUES (99999, 7, 1, 'tail');"
$P -e "SELECT id FROM events WHERE id = 99999;"                 # 1 行（走索引）
$P -e "UPDATE events SET id = 88888 WHERE id = 99999;"
$P -e "SELECT id FROM events WHERE id = 99999;"                 # 0 行（旧键已摘除）
$P -e "SELECT id FROM events WHERE id = 88888;"                 # 1 行（新键已入索引）
$P -e "DELETE FROM events WHERE id = 88888;"
$P -e "SELECT id FROM events WHERE id = 88888;"                 # 0 行
```

### 第 3–8 幕：取证面板（选路 / 优化器 / 统计 / 三模式 / 错误归属 / DML）

```bash
python docs/v3-dev/demo/v3_showcase_evidence.py --data-dir /tmp/v3show
```

**实录（节选自一次真实运行）**：

```text
① 选路决策（physical=auto）：代价单位 = 预计数据页读取次数
场景              reason            列       选择性    估计行数     扫描     索引  下推请求
高选择性点查      INDEX_EQUALITY    id      0.00079     1.57   25.00    4.57  IndexLookup
窄区间(9 行)      INDEX_RANGE       id      0.00450     9.00   25.00   12.00  IndexRange
宽区间(99 行)     SEQ_CHEAPER       id      0.04952    99.05   25.00  102.05  -
低基数列(一半)    SEQ_CHEAPER       grp     0.50000  1000.00   25.00 1003.00  -
键真实越界        INDEX_EQUALITY    id      0.00000     0.00   25.00    3.00  IndexLookup
无索引列          NO_MATCHING_INDEX -       -           -       -        -     -
无谓词            NO_PREDICATE      -       -           -       -        -     -
下推 + 残余过滤   INDEX_RANGE       id      0.00450     9.00   25.00   12.00  IndexRange

② 逻辑优化器
优化前：Projection(Filter(Join(Scan, Scan)))
优化后：Projection(Join(Filter(Scan), Filter(Scan)))
  - fold_constants         折叠 1 处常量表达式
  - normalize_booleans     下推 1 处 NOT；化简 1 处常量项
  - push_join_predicates   下推 2 项到左子树；下推 1 项到右子树
  - prune_and_eliminate    裁剪 1 处来源（列 -2）
优化开 5 行 / 优化关 5 行；结果逐行一致：True

③ 统计（契约 3.1）
表 events：row_count=2000  page_count=25
  id       distinct≈1271  min=0        max=1999     ← min/max 精确
  grp      distinct≈2     min=0        max=1
  note     distinct≈1271  min=n00000   max=n01999

④ 强制物理模式（同一条查询）
  auto   reason=INDEX_EQUALITY   页读=5    返回行=1
  seq    reason=FORCED_SEQ       页读=25   返回行=1
  index  reason=FORCED_INDEX     页读=5    返回行=1
  auto / seq / index 返回行完全一致：True

⑤ 错误归属
  无索引列 + 强制 index → E_INDEX_NOT_FOUND（B 抛）
  无谓词   + 强制 index → E_BAD_ARG（C 抛）

⑥ DML 与索引同步
  插入 99999 后按索引查：1 行
  改键后旧键 99999：0 行　新键 88888：1 行
  删除后新键 88888：0 行
```

**这三件事最值得讲给别人听**：

1. `id > 1990`（9 行）走索引、`id > 1900`（99 行）放弃索引——**分水岭是算出来的**，
   不是拍脑袋的阈值；
2. `id = 999999` 估 0 行且真的 0 行——因为 `min/max` 是**精确**极值（D47 修的正是
   "采样极值偏低，把其实有 99 行的区间估成 0 行"）；
3. `distinct≈1271` 而 `row_count=2000`——基数是有界采样（近似），极值才是精确的，
   契约把两项精度分开写死。

### 第 7 幕补充：JOIN + 强制索引的边界

```bash
# 两侧都有索引才允许强制索引：给 labels.id 也建一个
$P -e "CREATE INDEX idx_labels_id ON labels (id);"
$P --physical index -e "SELECT e.id, m.label FROM events e INNER JOIN labels m ON e.grp = m.id WHERE e.id > 1990 AND m.id = 1;"
```

不建这个索引时，同一条查询在 `--physical index` 下会报 `E_INDEX_NOT_FOUND`
（labels 侧没有索引）。这不是缺陷：强制索引要求**每个扫描位置都能走索引**，
否则就会静默退化成顺序扫描，基准数据也就失去意义（契约 DV3-12）。

### 第 9 幕：全链路追踪（交互）

```bash
python main.py --data-dir /tmp/v3show      # 注意：不要加 --no-trace
```

在提示符下依次输入：

```text
SELECT e.id, m.label FROM events e INNER JOIN labels m ON e.grp = m.id WHERE 1 = 1 AND NOT (e.id < 0) AND e.id > 1990 AND m.id = 1;
/inspect ALL
/inspect A
/inspect B
/inspect C
/physical index
/physical
```

- `/inspect ALL`：14 个阶段（含 V3 新增的**优化器**阶段，不再是 OFF）；
  右侧面板可在"阶段 / 节点 / 词法单元 / 数据页"之间联动；
- `/inspect A`：看索引 DDL 的 Token、AST 节点与 SourceSpan；
- `/inspect B`：看本次查询的 Catalog / 缓存 / Pager / Engine 调用与数据页；
- `/physical index`：提示符变成 `main [index] ❯`，`/physical` 可回显当前模式。

### 第 10 幕：重启持久化

```bash
$P --physical index -e "SELECT id FROM events WHERE id = 1999;"
```

新进程里索引仍可用——行页号提示随索引条目一起落盘（D49a），所以冷进程回表
也是"每行一页"，不需要先预热统计或重建映射。

### 第 11 幕：基准报告

```bash
# 演示用 2000 行；写到临时路径，避免覆盖仓库里 4000 行的正式报告
python -m bench run --rows 2000 --repeat 3 \
  --json /tmp/v3-bench.json --markdown /tmp/v3-bench.md
```

产物：`docs/v3-dev/benchmark-report.md` 与 `bench/results/v3-benchmark.json`。
读法见 [bench/README.md](../../../bench/README.md)：先读口径（冷/热、预热、
逻辑页读 vs 物理读盘、加速比只与同层 seq 比），再看主表，最后看选路决策表与
错误矩阵。

## 4. 一份完整的演示脚本（可直接照念）

```bash
# 0) 准备
rm -rf /tmp/v3show
python main.py --no-trace --data-dir /tmp/v3show -f docs/v3-dev/demo/v3_showcase_load.sql

# 1) 选路、优化器、统计、三模式、错误归属、DML 同步：一次看完
python docs/v3-dev/demo/v3_showcase_evidence.py --data-dir /tmp/v3show

# 2) 错误码矩阵（语义类 5 个）
python main.py --no-trace --data-dir /tmp/v3show --continue-on-error \
  -f docs/v3-dev/demo/v3_showcase_errors.sql

# 3) 语法错误的特例（解析期即失败）
python main.py --no-trace --data-dir /tmp/v3show -e "CREATE UNIQUE INDEX ux ON events (id);"

# 4) 基准（写临时路径，不覆盖仓库里的正式报告）
python -m bench run --rows 2000 --repeat 3 \
  --json /tmp/v3-bench.json --markdown /tmp/v3-bench.md

# 5) 全链路追踪（交互，不要加 --no-trace）
python main.py --data-dir /tmp/v3show
```

预计耗时：准备 ~3s、取证面板 <1s、错误矩阵 <1s、bench 约 15s、交互按需。

## 5. 文件清单

| 文件 | 说明 |
|---|---|
| `v3_showcase_load.sql` | 建表 + 2000 行 + 两个索引 + `labels` 表 |
| `v3_showcase_errors.sql` | 语义类错误矩阵（配 `--continue-on-error` 一次跑完） |
| `v3_showcase_evidence.py` | 取证面板：选路决策 / 优化日志 / 统计 / 三模式 / 错误归属 / DML 同步 |
| `README.md` | 本文 |
