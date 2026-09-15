# V3 基准报告（索引 · 代价选路 · 优化器）

> 生成时间（UTC）：2026-09-15T02:11:12+00:00　|　Python 3.11.15　|　Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.39
> 数据集：`main.events`，4000 行，seed=1，索引 idx_events_id(id), idx_events_amount(amount)
> 采样：每种模式冷 3 次 + 热 3 次；数据集指纹 `0f34160918fc2da9`（复用缓存）
> 复现：`python -m bench run --rows 4000 --seed 1 --repeat 3`

## 口径（先读这几条，再读数字）

- **冷** = 每次采样新建 `DatabaseServer`：新 Buffer Pool、新 rid→页 映射。
- **热** = 同一个 server 第二次及以后执行，反映稳态。
- **SQL 模式先预热 `statistics()` 再归零计数**（sql-auto, sql-seq, sql-index）：统计基线会读满数据页，不预热就会把这笔成本记到第一个跑的模式头上。
- **存储模式（api-seq / api-index）不预热也不经过规划器**，报的是纯 B 成本。
- 逻辑页读 = `pager.read_page` 调用数，按 `用户表 / 索引 / 系统目录` 分桶；物理读盘 = 同一批调用里 `cache.misses` 的增量。
- **加速比按逻辑页读总数算**：SQL 行以 `sql-seq` 为基线、存储行以 `api-seq` 为基线（同一层的口径才可比）。SQL 行因预热已把页读进池子，物理读盘接近 0，所以那一列对 SQL 行只作参考。
- 冷启动的**第一次顺序扫描**还要付一次 D37 惰性布局基线（逐页读一遍），因此页读约为数据页数的 2 倍；这是 B 的既有机制，两种 seq 模式同等承担。
- 耗时只作辅证：本报告的所有判断都能用页读计数复现（CI 不断言墙钟）。
- 跨模式等价性用**行多重集**（排序后哈希）比对，不用行序——索引按键序返回行。

## 主表（冷启动）

| 场景 | 模式 | 层次 | 返回行 | 用户表页读 | 索引页读 | 系统目录页读 | 逻辑页读合计 | 物理读盘 | 耗时中位数(ms) | 页读加速比 | 选路 reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `point_hit` | `sql-auto` | A+B+C | 1 | 10 | 6 | 0 | 16 | 3 | 3.36 | 6.50× | INDEX_EQUALITY |
| `point_hit` | `sql-seq` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 40.06 | 1.00× | FORCED_SEQ |
| `point_hit` | `sql-index` | A+B+C | 1 | 10 | 6 | 0 | 16 | 3 | 2.90 | 6.50× | FORCED_INDEX |
| `point_hit` | `api-seq` | B | 1 | 113 | 0 | 0 | 113 | 54 | 18.62 | 1.00× | — |
| `point_hit` | `api-index` | B | 1 | 90 | 6 | 0 | 96 | 57 | 7.04 | 1.18× | — |
| `point_miss` | `sql-auto` | A+B+C | 0 | 9 | 6 | 0 | 15 | 3 | 3.03 | 6.93× | INDEX_EQUALITY |
| `point_miss` | `sql-seq` | A+B+C | 0 | 104 | 0 | 0 | 104 | 0 | 38.48 | 1.00× | FORCED_SEQ |
| `point_miss` | `sql-index` | A+B+C | 0 | 9 | 6 | 0 | 15 | 3 | 2.53 | 6.93× | FORCED_INDEX |
| `point_miss` | `api-seq` | B | 0 | 113 | 0 | 0 | 113 | 54 | 19.02 | 1.00× | — |
| `point_miss` | `api-index` | B | 0 | 18 | 6 | 0 | 24 | 9 | 2.00 | 4.71× | — |
| `range_selective` | `sql-auto` | A+B+C | 99 | 104 | 0 | 0 | 104 | 0 | 46.52 | 1.00× | SEQ_CHEAPER |
| `range_selective` | `sql-seq` | A+B+C | 99 | 104 | 0 | 0 | 104 | 0 | 37.37 | 1.00× | FORCED_SEQ |
| `range_selective` | `sql-index` | A+B+C | 99 | 108 | 7 | 0 | 115 | 4 | 18.70 | 0.90× | FORCED_INDEX |
| `range_selective` | `api-seq` | B | 99 | 113 | 0 | 0 | 113 | 54 | 18.58 | 1.00× | — |
| `range_selective` | `api-index` | B | 99 | 9368 | 7 | 0 | 9375 | 58 | 736.02 | 0.01× | — |
| `range_wide` | `sql-auto` | A+B+C | 2999 | 104 | 0 | 0 | 104 | 0 | 49.34 | 1.00× | SEQ_CHEAPER |
| `range_wide` | `sql-seq` | A+B+C | 2999 | 104 | 0 | 0 | 104 | 0 | 45.59 | 1.00× | FORCED_SEQ |
| `range_wide` | `sql-index` | A+B+C | 2999 | 3008 | 41 | 0 | 3049 | 70 | 527.51 | 0.03× | FORCED_INDEX |
| `range_wide` | `api-seq` | B | 2999 | 113 | 0 | 0 | 113 | 54 | 21.13 | 1.00× | — |
| `range_wide` | `api-index` | B | 2999 | 232668 | 41 | 0 | 232709 | 92 | 18920.91 | 0.00× | — |
| `dup_lookup` | `sql-auto` | A+B+C | 43 | 52 | 6 | 0 | 58 | 3 | 10.77 | 1.79× | INDEX_EQUALITY |
| `dup_lookup` | `sql-seq` | A+B+C | 43 | 104 | 0 | 0 | 104 | 0 | 38.02 | 1.00× | FORCED_SEQ |
| `dup_lookup` | `sql-index` | A+B+C | 43 | 52 | 6 | 0 | 58 | 3 | 10.64 | 1.79× | FORCED_INDEX |
| `dup_lookup` | `api-seq` | B | 43 | 113 | 0 | 0 | 113 | 54 | 21.46 | 1.00× | — |
| `dup_lookup` | `api-index` | B | 43 | 3208 | 6 | 0 | 3214 | 57 | 261.82 | 0.04× | — |
| `no_index_column` | `sql-auto` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 43.12 | 1.00× | NO_MATCHING_INDEX |
| `no_index_column` | `sql-seq` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 47.29 | 1.00× | FORCED_SEQ |
| `no_index_column` | `sql-index` | A+B+C | 0 | 9 | 0 | 0 | 9 | 0 | 1.95 | 11.56× | 错误 |
| `no_index_column` | `api-seq` | B | 1 | 113 | 0 | 0 | 113 | 54 | 33.95 | 1.00× | — |
| `no_index_column` | `api-index` | B | 0 | 18 | 0 | 0 | 18 | 6 | 1.17 | 6.28× | 错误 |

## 主表（同进程热）

| 场景 | 模式 | 层次 | 返回行 | 用户表页读 | 索引页读 | 系统目录页读 | 逻辑页读合计 | 物理读盘 | 耗时中位数(ms) | 页读加速比 | 选路 reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `point_hit` | `sql-auto` | A+B+C | 1 | 10 | 4 | 0 | 14 | 0 | 2.31 | 7.43× | INDEX_EQUALITY |
| `point_hit` | `sql-seq` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 42.33 | 1.00× | FORCED_SEQ |
| `point_hit` | `sql-index` | A+B+C | 1 | 10 | 4 | 0 | 14 | 0 | 2.79 | 7.43× | FORCED_INDEX |
| `point_hit` | `api-seq` | B | 1 | 104 | 0 | 0 | 104 | 0 | 17.92 | 1.00× | — |
| `point_hit` | `api-index` | B | 1 | 10 | 4 | 0 | 14 | 0 | 1.82 | 7.43× | — |
| `point_miss` | `sql-auto` | A+B+C | 0 | 9 | 4 | 0 | 13 | 0 | 2.10 | 8.00× | INDEX_EQUALITY |
| `point_miss` | `sql-seq` | A+B+C | 0 | 104 | 0 | 0 | 104 | 0 | 36.16 | 1.00× | FORCED_SEQ |
| `point_miss` | `sql-index` | A+B+C | 0 | 9 | 4 | 0 | 13 | 0 | 2.26 | 8.00× | FORCED_INDEX |
| `point_miss` | `api-seq` | B | 0 | 104 | 0 | 0 | 104 | 0 | 18.00 | 1.00× | — |
| `point_miss` | `api-index` | B | 0 | 9 | 4 | 0 | 13 | 0 | 1.55 | 8.00× | — |
| `range_selective` | `sql-auto` | A+B+C | 99 | 104 | 0 | 0 | 104 | 0 | 41.75 | 1.00× | SEQ_CHEAPER |
| `range_selective` | `sql-seq` | A+B+C | 99 | 104 | 0 | 0 | 104 | 0 | 44.47 | 1.00× | FORCED_SEQ |
| `range_selective` | `sql-index` | A+B+C | 99 | 108 | 5 | 0 | 113 | 0 | 16.99 | 0.92× | FORCED_INDEX |
| `range_selective` | `api-seq` | B | 99 | 104 | 0 | 0 | 104 | 0 | 17.68 | 1.00× | — |
| `range_selective` | `api-index` | B | 99 | 108 | 5 | 0 | 113 | 0 | 14.59 | 0.92× | — |
| `range_wide` | `sql-auto` | A+B+C | 2999 | 104 | 0 | 0 | 104 | 0 | 55.64 | 1.00× | SEQ_CHEAPER |
| `range_wide` | `sql-seq` | A+B+C | 2999 | 104 | 0 | 0 | 104 | 0 | 45.70 | 1.00× | FORCED_SEQ |
| `range_wide` | `sql-index` | A+B+C | 2999 | 3008 | 39 | 0 | 3047 | 80 | 535.20 | 0.03× | FORCED_INDEX |
| `range_wide` | `api-seq` | B | 2999 | 104 | 0 | 0 | 104 | 0 | 21.13 | 1.00× | — |
| `range_wide` | `api-index` | B | 2999 | 3008 | 39 | 0 | 3047 | 80 | 503.19 | 0.03× | — |
| `dup_lookup` | `sql-auto` | A+B+C | 43 | 52 | 4 | 0 | 56 | 0 | 9.21 | 1.86× | INDEX_EQUALITY |
| `dup_lookup` | `sql-seq` | A+B+C | 43 | 104 | 0 | 0 | 104 | 0 | 34.29 | 1.00× | FORCED_SEQ |
| `dup_lookup` | `sql-index` | A+B+C | 43 | 52 | 4 | 0 | 56 | 0 | 11.29 | 1.86× | FORCED_INDEX |
| `dup_lookup` | `api-seq` | B | 43 | 104 | 0 | 0 | 104 | 0 | 17.14 | 1.00× | — |
| `dup_lookup` | `api-index` | B | 43 | 52 | 4 | 0 | 56 | 0 | 10.20 | 1.86× | — |
| `no_index_column` | `sql-auto` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 39.56 | 1.00× | NO_MATCHING_INDEX |
| `no_index_column` | `sql-seq` | A+B+C | 1 | 104 | 0 | 0 | 104 | 0 | 39.52 | 1.00× | FORCED_SEQ |
| `no_index_column` | `sql-index` | A+B+C | 0 | 9 | 0 | 0 | 9 | 0 | 7.36 | 11.56× | 错误 |
| `no_index_column` | `api-seq` | B | 1 | 104 | 0 | 0 | 104 | 0 | 22.82 | 1.00× | — |
| `no_index_column` | `api-index` | B | 0 | 9 | 0 | 0 | 9 | 0 | 1.00 | 11.56× | 错误 |

## 选路决策（sql-auto，冷启动）

| 场景 | reason | 选择列 | 选择性 | 估计行数 | 扫描代价 | 索引代价 | 实际行数 |
|---|---|---|---|---|---|---|---|
| `point_hit` | INDEX_EQUALITY | `id` | 0.0007 | 3.00 | 47.00 | 6.00 | 1 |
| `point_miss` | INDEX_EQUALITY | `id` | 0.0000 | 0.0000 | 47.00 | 3.00 | 0 |
| `range_selective` | SEQ_CHEAPER | `id` | 0.0248 | 99.02 | 47.00 | 102.02 | 99 |
| `range_wide` | SEQ_CHEAPER | `id` | 0.7499 | 2999.75 | 47.00 | 3002.75 | 2999 |
| `dup_lookup` | INDEX_EQUALITY | `amount` | 0.0103 | 41.24 | 47.00 | 44.24 | 43 |
| `no_index_column` | NO_MATCHING_INDEX | `name` | — | — | — | — | 1 |

## 错误矩阵（强制物理模式的失败码归属）

| 场景 | 模式 | 错误码 | 是否符合预期 |
|---|---|---|---|
| `no_index_column` | `sql-index` | `E_INDEX_NOT_FOUND` | 是 |
| `no_index_column` | `api-index` | `E_INDEX_NOT_FOUND` | 是 |

## 跨模式一致性

- `point_hit`：一致（sql-auto=09feae41c02a878a、sql-seq=09feae41c02a878a、sql-index=09feae41c02a878a、api-seq=09feae41c02a878a、api-index=09feae41c02a878a）
- `point_miss`：一致（sql-auto=4f53cda18c2baa0c、sql-seq=4f53cda18c2baa0c、sql-index=4f53cda18c2baa0c、api-seq=4f53cda18c2baa0c、api-index=4f53cda18c2baa0c）
- `range_selective`：一致（sql-auto=a8b90081d8b44e46、sql-seq=a8b90081d8b44e46、sql-index=a8b90081d8b44e46、api-seq=a8b90081d8b44e46、api-index=a8b90081d8b44e46）
- `range_wide`：一致（sql-auto=05653facc5e99a4e、sql-seq=05653facc5e99a4e、sql-index=05653facc5e99a4e、api-seq=05653facc5e99a4e、api-index=05653facc5e99a4e）
- `dup_lookup`：一致（sql-auto=3c986ca4671368a3、sql-seq=3c986ca4671368a3、sql-index=3c986ca4671368a3、api-seq=3c986ca4671368a3、api-index=3c986ca4671368a3）
- `no_index_column`：一致（sql-auto=9358f8b88b3cd82b、sql-seq=9358f8b88b3cd82b、api-seq=9358f8b88b3cd82b）

## 结论

- 跨模式行集一致性：全部一致；计数可复现：是。
- SQL 层（含 C 的选路，冷启动、已统一预热）：
  - `point_hit`：seq 104 → index 16 逻辑页读（6.50×）；`auto` 选 `INDEX_EQUALITY`，实际 16 页
  - `point_miss`：seq 104 → index 15 逻辑页读（6.93×）；`auto` 选 `INDEX_EQUALITY`，实际 15 页
  - `range_selective`：seq 104 → index 115 逻辑页读（0.90×）；`auto` 选 `SEQ_CHEAPER`，实际 104 页
  - `range_wide`：seq 104 → index 3049 逻辑页读（0.03×）；`auto` 选 `SEQ_CHEAPER`，实际 104 页
  - `dup_lookup`：seq 104 → index 58 逻辑页读（1.79×）；`auto` 选 `INDEX_EQUALITY`，实际 58 页
- 存储层（纯 B，不经规划器）：冷启动下索引被回表定位拖累（D49），热态才体现索引收益：
  - `point_hit`：冷 seq 113 / index 96；热 seq 104 / index 14
  - `point_miss`：冷 seq 113 / index 24；热 seq 104 / index 13
  - `range_selective`：冷 seq 113 / index 9375；热 seq 104 / index 113
  - `range_wide`：冷 seq 113 / index 232709；热 seq 104 / index 3047
  - `dup_lookup`：冷 seq 113 / index 3214；热 seq 104 / index 56
  - `no_index_column`：冷 seq 113 / index 18；热 seq 104 / index 9

## 已知限制

- **冷启动回表**：B 在 rid→页 映射未建立时逐页探测（`engine._locate` 的退化分支），所以冷进程里索引点查/区间扫描的成本被放大；同进程第二次起才 O(1)。B 侧登记为 D49（回表 O(1)）待排期。
- **统计基线**：极值精确化后，`statistics()` 的首次调用会读满数据页（进程内一次性）；这也会顺带预热 rid 映射，使 `auto` 与强制 `index` 在同一进程里的成本不对称——这正是 `api-*` 两列存在的原因。
- **基数近似**：`distinct_count` 仍是有界采样（最多 16 页），大表会偏低；`min/max` 已精确。
- C 的优化器与选路只覆盖单表谓词下推；JOIN 重排不在本版范围。

## 复现

```bash
python -m bench run --rows 4000 --seed 1 --repeat 3
```
