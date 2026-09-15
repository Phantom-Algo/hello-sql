# bench：V3 基准（F8）

五种模式跑同一批查询，产出可复现的对比报告，用来回答两个问题：

1. **升级之后是否更有效**——有索引 vs 无索引的页读与耗时对比；
2. **选路是否正确**——`auto` 有没有在该放弃索引时放弃（或反之）。

## 用法

```bash
# 全量：4000 行、每模式冷热各 3 次、写报告与证据
python -m bench run

# 小规模自检：几秒钟跑完
python -m bench run --rows 400 --repeat 1

# 强制重建数据集、只跑两个模式
python -m bench run --rows 8000 --rebuild --modes sql-seq,sql-index
```

产物：

- `docs/v3-dev/benchmark-report.md`：进仓库的对比报告；
- `bench/results/*.json`：机读证据（含每次采样的计数、选路估算与错误矩阵）；
- `bench/.cache/ds-<指纹>/`：数据集缓存（已 gitignore，同参数复用）。

## 五个模式

| 模式 | 层次 | 说明 |
|---|---|---|
| `sql-auto` | A+B+C | `Runner.execute(sql, physical="auto")`，走 C 的代价选路 |
| `sql-seq` | A+B+C | 强制顺序扫描 |
| `sql-index` | A+B+C | 强制索引访问（无索引列由 B 抛 `E_INDEX_NOT_FOUND`） |
| `api-seq` | B | `scan()` + bench 侧等价过滤 |
| `api-index` | B | `index_lookup` / `index_range` |

## 口径（重要）

- **冷**：每次采样新建 `DatabaseServer`——新 Buffer Pool、新 rid→页 映射。
  页读计数在多次采样间完全一致，因此 CI 断言只吃计数（不断言耗时）。
- **热**：同一个 server 第二次及以后执行，反映稳态。
- **SQL 模式先预热 `statistics()` 再归零计数**：极值精确化后统计基线会读满
  数据页，不预热就会把这笔成本算到第一个跑的模式头上。
- **存储模式不预热也不经过规划器**，报的是纯 B 成本；冷启动下 B 的回表定位
  会逐页探测（D49 待排期），这也是两组数字差异的来源之一。
- **等价性用行多重集**（排序后哈希）比对：索引按键序返回行，行序本就不承诺。

## 与上一版报告的口径差异（B 的 D49b/D49c 落地后）

- 布局遍历只付一次（D49b）：稳态扫描从 2× 数据页降到 1×，冷进程的第一次访问
  仍要付一次布局基线；因此 SQL 层的"无索引基线"变小，索引的加速比数字随之下降
  （4000 行点查从 6.50× 降到 3.11×）——这是基线回归真实值，不是索引变慢。
- 插入页提示（D49c）：装载 4000 行的页读从约 34/行降到约 2/行，数据集构建时间
  从 ~7s 降到 ~1.4s。

## 红线

`bench/` 是装配层，允许 import A/B/C 三家，但只准走公开入口：
`compiler`、`runner`、`storage`、`contracts` 的顶层模块。
`bench/tests/test_import_boundary.py` 会用 AST 扫描强制这条约束。
