# LogicalColumn.index 机制说明（V3）

## 1. 本文范围

本文说明 `runner/logical_plan/base.py` 中 `LogicalColumn.index` 的语义、建立过程与消费方式，
并给出 V3 中两条会改动 index 的规则（JOIN 谓词下推、投影裁剪）的换算方法。

文中的计划形态与行元组均取自本仓库的实跑结果。第 6、7 节的「下推后」「裁剪后」计划最初由
按下文规则手工改写得到，改写结果由真实的 `__post_init__`（`LogicalFilter` / `LogicalJoin` /
`LogicalProjection`）完成校验，全部通过；优化器落地后这些形态与实跑输出一致。

示例统一使用：

```sql
CREATE TABLE users  (id INT, name TEXT, active BOOLEAN, email TEXT);
CREATE TABLE orders (id INT, user_id INT, total REAL, cancelled BOOLEAN, notes TEXT);
```

users 3 行、orders 5 行，数据见附录 A。

---

## 2. index 的语义

### 2.1 定义

```python
# runner/logical_plan/base.py :: LogicalColumn
@dataclass(frozen=True, slots=True)
class LogicalColumn:
    table: str        # 物理表名
    qualifier: str    # 解析与表头使用的限定符，别名优先
    name: str         # 列名；投影输出列这里是结果表头文本
    index: int        # 该列在「求值该表达式时所用的那一行」中的零基位置
    type: SqlType
```

定义中的限定语是「哪一行」，它由持有该表达式的节点决定：

| 表达式所在位置 | 求值所用的行 |
|---|---|
| `LogicalFilter.predicate` | child 的输出行 |
| `LogicalJoin.on` | 左右拼接行 |
| `LogicalProjection.columns` | child 的输出行 |
| `LogicalUpdate.assignments[i].column` | 语义为表列序，见第 10 节 |

执行期读取 index 的地方只有一处：

```python
# runner/logical_plan/expressions.py :: eval_expr
case BoundColumnRef():
    return row[expr.column.index]
```

由此得到 index 的基本性质：它是相对坐标。同一个物理列在不同节点的表达式里可以取不同的
index，比较两个 index 之前必须先确认它们描述的是同一套行布局。

### 2.2 两条不变式

1. 每个节点的 `output_schema.columns[i].index == i`，输出列位置从 0 连续编号。
2. `plans.py :: _check_column` 要求 `schema.columns[col.index]` 与 `col` 的 `table` /
   `qualifier` / `name` / `type` 全部相等。

第 2 条使 index 成为可自检的坐标：引用与实际列不符时，节点构造即报错。第 7 节的裁剪
正确性主要依赖这条校验，实现时不需要额外断言。

### 2.3 取值位置与展示位置

`LogicalProjection` 是唯一同时持有两套语义的节点：

```python
# SELECT name FROM users
LogicalProjection
  columns       = (BoundColumnRef(users.name@1),)         # index=1：在 child 行中取值
  output_schema = (LogicalColumn(name="name", index=0),)  # 展示位置 0，表头 "name"
```

`output_schema` 的构造见 `plans.py :: LogicalProjection.output_schema`：输出列的 `name`
取 `output_names[i]`（结果表头文本），`table` / `qualifier` / `type` 沿用来源列，`index`
取 i。

JOIN 查询的表头带限定符，因此投影输出列形如 `qualifier="u", name="u.name"`，调试打印为
`u.u.name@0`。`SelectExecutor` 直接取 `output_schema` 各列的 `name` 作为结果表头。

在 `Filter` 谓词与 `Join ON` 中，`BoundColumnRef` 在表达式树里的位置不携带位置语义，只有
`index` 参与求值。

---

## 3. 坐标系的建立（绑定期）

| 建立点 | 代码位置 | 规则 |
|---|---|---|
| 表 Schema | `builder.py :: _build_source` | `enumerate(table_info.columns)` |
| JOIN 拼接 | `base.py :: join_schema` | 左列在前、右列在后，整体重新编号 |
| 投影输出 | `plans.py :: LogicalProjection.output_schema` | 按 SELECT 书写顺序从 0 编号 |
| 表达式绑定 | `expressions.py :: bind_expr` | 经 `LogicalSchema.resolve` 取得输入 Schema 中的列 |

### 3.1 单表

`_build_source` 按表定义列序建立 Schema，`index` 等于建表列号：

```
users: id@0, name@1, active@2, email@3
```

此时「表内列号」与「Scan 输出行位置」相等。第 7 节的裁剪会打破这个相等关系。

### 3.2 JOIN 拼接

```python
# runner/logical_plan/base.py :: join_schema
for index, col in enumerate(left.columns + right.columns)
```

左列保持原位置，右列整体平移 `len(left.columns)`。这条平移规则是第 6 节下推换算的依据。

### 3.3 表达式绑定

`bind_expr` 解析列引用时返回的是输入 Schema 中的列对象，其 `index` 就是该列在输入行中的
位置。绑定只读 Schema，不重写 index，因此表达式中的 index 与所在节点的输入行布局天然一致。

---

## 4. 单表执行过程

```sql
SELECT name FROM users WHERE id = 2;
```

计划形态（实跑输出）：

```
LogicalProjection  out=[users.name@0]
  columns   = [users.name@1]
  LogicalFilter    out=[users.id@0, users.name@1, users.active@2, users.email@3]
    predicate = (users.id@0 = 2)
    LogicalScan    out=[users.id@0, users.name@1, users.active@2, users.email@3]
```

运行期行元组：

```
Scan        row=(1, 'alice', True, 'a@x')   predicate 取 values[0]=1，1 = 2 为假
Filter      行未命中，丢弃
Scan        row=(2, 'bob',   True, 'b@x')   predicate 取 values[0]=2，命中，原样下传
Projection  按 columns 取 values[1]         输出 ('bob',)
```

各执行器对行形状的处理，构成对应节点的坐标系：

| 执行器 | 对行的操作 | 对 index 的影响 |
|---|---|---|
| `SeqScanExecutor` | 按 `storage.scan()` 的整行直接下传 | 输出行与 `schema` 同序 |
| `FilterExecutor` | 命中行原样下传 | 谓词与 child 共用一套坐标 |
| `NestedLoopJoinExecutor` | `left.values + right.values` | 左列不变，右列偏移 `len(left)` |
| `ProjectionExecutor` | 按 `columns` 逐个取值重排 | 输出行由输出 Schema 定义 |

---

## 5. 多表（JOIN）执行过程

```sql
SELECT u.name, o.total
FROM users u JOIN orders o ON u.id = o.user_id
WHERE o.total > 20 AND u.active;
```

计划形态（实跑输出）：

```
LogicalProjection  out=[u.u.name@0, o.o.total@1]
  columns   = [u.name@1, o.total@6]
  LogicalFilter    out=[u.id@0, u.name@1, u.active@2, u.email@3,
                        o.id@4, o.user_id@5, o.total@6, o.cancelled@7, o.notes@8]
    predicate = (o.total@6 > 20.0) AND u.active@2
    LogicalJoin
      on      = (u.id@0 = o.user_id@5)
      LogicalScan  out=[u.id@0, u.name@1, u.active@2, u.email@3]
      LogicalScan  out=[o.id@0, o.user_id@1, o.total@2, o.cancelled@3, o.notes@4]
```

拼接行布局：

```
位置        0      1       2        3     |  4      5          6         7          8
           u.id  u.name  u.active  u.email | o.id  o.user_id  o.total  o.cancelled  o.notes
                                            └──────────── + n_left = 4 ─────────────┘
```

运行期行元组：

```
左 Scan   (1, 'alice', True, 'a@x')                       u.name 位于 values[1]
右 Scan   (101, 1, 30.5, False, 'n1')                     o.total 位于 values[2]
Join 拼接 (1, 'alice', True, 'a@x', 101, 1, 30.5, False, 'n1')
          ON 取 values[0]=1 与 values[5]=1，相等，保留
Filter    取 values[6]=30.5 (>20 为真) 与 values[2]=True，命中，原样下传
Projection 按 columns 取 values[1] 与 values[6]             输出 ('alice', 30.5)
```

同一个 `30.5` 在右 Scan 的输出行里是 `values[2]`，在拼接行里是 `values[6]`，两者相差
`n_left = 4`。JOIN 执行过程中同时存在三套坐标：

| 坐标 | 适用表达式 | `o.total` 的 index |
|---|---|---|
| 左子树行 | 下推到左子树的谓词 | 不适用 |
| 右子树行 | 下推到右子树的谓词 | 2 |
| 拼接行 | `Join.on`、JOIN 之上的 Filter / Projection | 6 |

链式 JOIN 按书写顺序构造左深树，第 i 个 JOIN 的 ON 绑定在「左侧累积 Schema + 当前右表」
上（`builder.py :: _build_source_range`），因此每一层 JOIN 的 ON 使用该层自己的拼接坐标。

---

## 6. 谓词下推对 index 的影响

### 6.1 IN / OUT 模型

为描述 index 的换算，为每个节点定义两套行布局：

- `OUT(N)`：N 输出的行布局，即 `N.output_schema`；
- `IN(N)`：N 自身表达式求值时所吃的行布局。

三类查询节点的 `IN` 与 `OUT` 关系：

| 节点 | IN | OUT | 原因 |
|---|---|---|---|
| `LogicalFilter` | `OUT(child)` | 与 IN 相同 | 命中行原样下传，形状不变 |
| `LogicalJoin` | 拼接行 | 与 IN 相同 | 拼接行即该节点输出的行 |
| `LogicalProjection` | `OUT(child)` | 另起一套，从 0 重编号 | 投影按 `columns` 重排行值 |

据此可以把 index 改写问题统一表述为：**把坐标从表达式当前所在的行布局，换算到它改写后
将要面对的行布局**。

### 6.2 换算公式

下推前，conjunct 挂在 JOIN 之上的 Filter 中，坐标为 `OUT(Join)`。下推后它挂在子树 Filter
中，坐标为 `OUT(子树)`。两种布局的关系由 `join_schema` 给出：

```
OUT(Join) = OUT(left) ++ OUT(right)
```

于是换算规则为：

| 列所属 | 在 `OUT(Join)` 中的位置 | 在子树中的位置 | 换算 |
|---|---|---|---|
| 左子树 | `p` | `p` | 保持不变 |
| 右子树 | `n_left + k` | `k` | 减去 `n_left` |

`n_left` 指被跨过的那一层 JOIN 的左子树列数。

### 6.3 实例

承接第 5 节的查询。上层 Filter 的两个 conjunct 分别是
`o.total@6 > 20.0`（限定符 `o`）与 `u.active@2`（限定符 `u`），`n_left = 4`。

下推前后对照（实跑校验通过）：

| 位置 | 下推前 | 下推后 |
|---|---|---|
| `Projection.columns` | `u.name@1, o.total@6` | `u.name@1, o.total@6` |
| `Join.on` | `u.id@0 = o.user_id@5` | `u.id@0 = o.user_id@5` |
| JOIN 之上的 Filter | `o.total@6 > 20.0 AND u.active@2` | 节点删除 |
| 左子树 Filter | 无 | `u.active@2`，位于 `[u.id@0, u.name@1, u.active@2, u.email@3]` 之上 |
| 右子树 Filter | 无 | `o.total@2 > 20.0`，位于 `[o.id@0, o.user_id@1, o.total@2, o.cancelled@3, o.notes@4]` 之上 |

左侧下推是恒等换算：左子树的行就是拼接行的前缀，列位置不变。右侧下推需要显式做减法，
本例中 `o.total` 由 `@6` 变为 `@2`。

### 6.4 归属判定

判定 conjunct 归属于哪一侧，方法是递归收集其中全部 `BoundColumnRef` 的
`column.qualifier`，检查该集合是否为某一侧 `output_schema.qualifiers` 的子集。

使用 `qualifier` 而非 `table`：自连接场景下两侧 table 相同而 qualifier 不同，`join_schema`
保证两侧 qualifier 不相交，判定结果只会落在一侧。

引用了两侧的 conjunct（如 `u.id = o.user_id`，或 `u.age = 1 OR o.total = 2`）在两套子树
坐标中都无法表达，只能留在原处。

**ON 里的单侧项走同一套判定与换算。** 规则的 conjunct 有两个来源：JOIN 之上的 `LogicalFilter`
（WHERE）与 `LogicalJoin.on`。ON 里的单侧项下推到子树时，坐标同样从 `OUT(Join)` 换到
`OUT(子树)`，公式与本节完全相同（左恒等、右减 `n_left`）；差别只在去处——WHERE 来源的
conjunct 留在原 Filter 里，ON 来源的留在 ON 里。ON 必须保持非空 AND 信封，因此全部项都被
推走时要留一个 `TRUE` 占位，而不是构造空 AND。

### 6.5 多层 JOIN

左深树中向下连续下推时，每跨过一层 JOIN 做一次换算，减去的 `n_left` 是那一层 JOIN 的左
子树列数：

```
JOIN 之上的 Filter
  → 跨过最外层 JOIN，减去该层 n_left
  → 跨过内层 JOIN，减去该层 n_left
  → 落在目标子树
```

### 6.6 错误示例

换一组更宽的表，使漏做减法后的 index 仍落在右子树范围内：

```sql
users  (id INT, name TEXT, active BOOLEAN)                     -- 3 列，n_left = 3
orders (id INT, user_id INT, total REAL, c1 INT, c2 INT, c3 INT, c4 INT)   -- 7 列
```

`o.total` 在 JOIN 层的 index 为 5（3 + 2），在右子树的 index 为 2。漏做减法时保留 `@5`，
而右子树的 5 号位置是整型列 `c3`，构造期即报错：

```
[E_TYPE_MISMATCH] bound column mismatch: o.total@5(REAL) vs schema o.c3(INT)
```

---

## 7. 投影裁剪对 index 的影响

### 7.1 三步流程

裁剪包含三段遍历，方向与产物各不相同：

| 顺序 | 方向 | 产物 | 必要性 |
|---|---|---|---|
| 1 | 自顶向下 | 每个节点的 `need` 集合 | 需求来自父节点的引用 |
| 2 | 自底向上 | 每个节点的新 Schema，以及该节点「旧 Schema → 新 Schema」的映射 | 父节点的新 Schema 由子节点的新 Schema 拼出 |
| 3 | 自顶向下 | 按映射改写后的表达式与投影列 | 改写需要子节点的新旧映射已经就绪 |

第 2 步的产物是纯 Schema 数据，可以独立断言，便于区分「裁剪范围算错」与「映射改写写错」。

### 7.2 需求传播规则

设 `need(N)` 为父节点要求 N 输出的列集合，以 `(table, qualifier, name, type)` 为键：

| 节点 | 向子节点传播的需求 |
|---|---|
| `LogicalProjection` | 自身 `columns` 引用的全部列 |
| `LogicalFilter` | 自身谓词引用的全部列 ∪ 父节点需求 |
| `LogicalJoin` | 左：ON 中左列 ∪ 父需求中的左列；右：ON 中右列 ∪ 父需求中的右列 |
| `LogicalScan` | 需求集合与自身 Schema 的交集；交集为空时保留裁剪前 Schema 的首列 |

Join 一行需要留意：ON 引用的列必须计入需求，否则 ON 求值时该列已被裁掉。

### 7.3 实例

`need` 传播（承接第 6 节下推后的计划）：

| 节点 | 自身引用的列 | 父节点要求输出 | 向子节点要求 |
|---|---|---|---|
| `LogicalProjection` | `u.name, o.total` | 无（根） | `{u.name, o.total}` |
| `LogicalJoin` | ON：`u.id, o.user_id` | `{u.name, o.total}` | 左 `{u.id, u.name}`；右 `{o.user_id, o.total}` |
| 左 Filter | `u.active` | `{u.id, u.name}` | 左 Scan `{u.id, u.name, u.active}` |
| 右 Filter | `o.total` | `{o.user_id, o.total}` | 右 Scan `{o.user_id, o.total}` |

自底向上重建 Schema：Scan 保留需求与自身 Schema 的交集，按**裁剪前的列序**排列，再从 0
连续重新编号。

```
users  Scan: (id@0, name@1, active@2, email@3) ∩ {id, name, active}
          →  (id@0, name@1, active@2)                 映射 {0:0, 1:1, 2:2}

orders Scan: (id@0, user_id@1, total@2, cancelled@3, notes@4) ∩ {user_id, total}
          →  (user_id@0, total@1)                     映射 {1:0, 2:1}

Join 新 Schema = OUT_new(left) ++ OUT_new(right)
               = (u.id@0, u.name@1, u.active@2, o.user_id@3, o.total@4)
Join 层映射   = 旧拼接 9 列 → 新拼接 5 列 = {0:0, 1:1, 2:2, 5:3, 6:4}
```

Join 层映射的构造方式：取旧拼接行的每一列，在新拼接行中按 `(qualifier, name)` 查找同名列，
两个位置构成一条映射。`(qualifier, name)` 在同一 Schema 内唯一（存储层拒绝重名列，重复建列
报 `E_DUP_COLUMN`），可以安全用作匹配键；自连接场景下两侧 qualifier 不同，也不会串列。
查找不到的列即被裁掉的列，不应再有任何引用指向它们。

改写后的最终计划（构造期全部校验通过）：

```
LogicalProjection columns=[u.name@1, o.total@4]  out=[u.name@0, o.total@1]
└─ LogicalJoin    on=(u.id@0 = o.user_id@3)      out=[u.id@0, u.name@1, u.active@2,
                                                     o.user_id@3, o.total@4]
   ├─ LogicalFilter u.active@2          over Scan [u.id@0, u.name@1, u.active@2]
   └─ LogicalFilter (o.total@1 > 20.0)  over Scan [o.user_id@0, o.total@1]
```

### 7.4 改写时使用哪张映射表

改写节点 N 的表达式时，使用的映射表由该表达式求值时所面对的行布局决定：

| 表达式 | 求值所用的行 | 使用的映射表 |
|---|---|---|
| `Filter.predicate` | `OUT(child)`，而 Filter 形状不变，等于 `OUT(Filter)` | N 自身的映射 |
| `Join.on` | 拼接行，即 `OUT(Join)` | N 自身的映射 |
| `Projection.columns` | `OUT(child)` | **child 的映射** |

`Projection` 自身的映射描述的是「旧结果 2 列 → 新结果 2 列」，与它的 `columns` 无关。用错
映射表会在构造期被 `_check_column` 拦下。

本例的逐引用对照：

| 引用位置 | 求值所用的行 | 旧 index | 使用的映射表 | 新 index |
|---|---|---|---|---|
| 左 Filter 谓词 | 新左 Scan 的行 | `u.active@2` | 左 Scan `{0:0,1:1,2:2}` | `u.active@2` |
| 右 Filter 谓词 | 新右 Scan 的行 | `o.total@2` | 右 Scan `{1:0,2:1}` | `o.total@1` |
| `Join.on` | 新拼接行 | `u.id@0`、`o.user_id@5` | Join 层 `{0:0,1:1,2:2,5:3,6:4}` | `u.id@0`、`o.user_id@3` |
| `Projection.columns` | 新拼接行 | `u.name@1`、`o.total@6` | Join 层同一张 | `u.name@1`、`o.total@4` |
| `Projection.output_schema` | 自己产出的行 | 无 | 从 0 重新编号 | `u.name@0`、`o.total@1` |

改写动作只改 `.index` 一个字段，`table` / `qualifier` / `name` / `type` 保持不变。

### 7.5 冗余节点消除中的 Schema 约定

| 情形 | 处理 |
|---|---|
| Filter 谓词恒为 `TRUE` | 删除 Filter |
| Filter 谓词恒为 `FALSE` | 替换为 `LogicalEmpty(child.output_schema)` |
| Filter 的 child 是 `LogicalEmpty` | 替换为 `LogicalEmpty`，Schema 不变 |
| Join 任一侧是 `LogicalEmpty` | 替换为 `LogicalEmpty(join.output_schema)` |
| `LogicalProjection` 的 child 是 `LogicalEmpty` | 保留 Projection，列数与表头仍需正确，行数为 0 |

`LogicalEmpty` 携带的 Schema 与被替换节点一致，使上层已改写好的 index 继续有效。恒假谓词
之上若还有 Projection，它仍按原 index 引用列，`LogicalEmpty` 不带同构 Schema 会导致
`Projection.__post_init__` 立即报错。

零列来源保留首列的原因与行数有关。`SELECT u.id FROM users u JOIN orders o ON u.active`
中 orders 的所有列都未被引用，实跑结果为 10 行（2 个 active 用户 × 5 张订单）。若把该来源
替换为零行的 `LogicalEmpty`，上层 Join 按「任一侧零行则结果零行」得到 0 行，结果行数被改变。
保留一列即可：该列没有任何引用者，保留哪一列都不影响结果，固定取裁剪前 Schema 的首列是为
了让不动点判定稳定。

谓词整体化简为 `TRUE` 时，不能构造 `BoundLogical(AND, ())`（`LogicalFilter.__post_init__`
会拒绝空 AND）。实现上要么当场删除 Filter，要么先保留单元素 `AND(TRUE)` 交给冗余消除处理。

### 7.6 错误示例

**映射表构造错误**：按列名顺序 zip 拼接新旧 Schema，未按 `(qualifier, name)` 匹配，改写后
`o.user_id` 落在 4 号位置，而新拼接行的 4 号位置是 `o.total`：

```
[E_TYPE_MISMATCH] bound column mismatch: o.user_id@4(INT) vs schema o.total(REAL)
```

**新 Schema 的 index 不连续**：重建时对裁剪前的列序做 `enumerate` 再过滤，得到的 Schema 是
`(user_id@1, total@2)`，位置不连续，构造期报错。

```
[E_COLUMN_NOT_FOUND] column not found: total
```

正确做法是对过滤后的列列表重新 `enumerate`。

---

## 8. 同一物理列的多重 index

以第 5 节的查询为例，`o.total` 在整条链路上出现过六个 index，全部合法：

| 出现位置 | index | 行布局 |
|---|---|---|
| 裁剪前，右子树 Filter 谓词 | 2 | 5 列 orders 行 |
| 裁剪前，Join 层（谓词、投影） | 6 | 9 列拼接行 |
| 裁剪后，右子树 Filter 谓词 | 1 | 2 列裁剪行 |
| 裁剪后，Join 层（投影列） | 4 | 5 列裁剪后的拼接行 |
| 存储层整行 | 2 | 5 列物理行（执行侧，见第 9 节） |
| 最终结果行 | 1 | 2 列输出（展示位置） |

每个 index 只对它所在节点的行布局负责。

---

## 9. 执行侧配套：source_indexes

### 9.1 两套下标

裁剪后 `LogicalScan.schema` 是表 Schema 的子序列，而 `storage.scan()` 仍返回整行，因此同一个
输出列存在两套下标：

| | 值（orders 裁剪后） | 含义 | 使用方 |
|---|---|---|---|
| `LogicalColumn.index` | `(0, 1)` | 裁剪后元组中的位置 | `eval_expr` |
| `source_indexes` | `(1, 2)` | 存储整行中的位置 | Scan 执行器读行投影 |

### 9.2 现有 SeqScanExecutor 的缺口

`runner/executor/dql.py :: SeqScanExecutor` 目前把 `storage.scan()` 的整行直接下传。裁剪后
该行为会导致错位：存储整行 `(101, 1, 30.5, False, 'n1')` 的 1 号位置是 `user_id`，而裁剪后的
Schema 声明 1 号位置是 `total`。

补上按 `source_indexes` 的投影后，裁剪计划与未优化版本的执行结果逐行一致，表头一致。

### 9.3 source_indexes 的存放位置

`source_indexes` 不写入 `LogicalScan` 字段，而是由执行器构建期调用 `storage.describe(table)`
反查每个输出列在完整列序中的位置得到。这样做保持 `LogicalPlan → LogicalPlan` 的纯变换，物理
列位置留在执行期；代价是构建期多一次 `describe` 调用，该调用在会话内可缓存。

---

## 10. DML 子树的硬约束

`UpdateExecutor` 用 child 的输出行做整行替换：

```python
values[assignment.column.index] = assignment.value.value
```

`assignment.column.index` 来自表 Schema（绑定期由 `schema.column(name)` 得到），`row.values`
是 child 的输出行。两者对齐的前提是 child 输出恒等于表的完整列序。实跑示例：

```
child 输出: ['id@0', 'name@1', 'active@2', 'email@3']
赋值目标  : [('email', 3)]
把 child 裁成 (id, email) 两列 → [E_COLUMN_NOT_FOUND] column not found: email
```

计划校验会拦下这类改写；即使通过，写入也会落到错误的列上。因此优化器只作用于 SELECT 计划树，
`LogicalUpdate` / `LogicalDelete` 的 child 原样保留。`DeleteExecutor` 只消费 `row_id`，与
`UpdateExecutor` 同构，不作单独放宽。

---

## 11. 校验覆盖范围

### 11.1 计划层

`_check_column` 要求 `schema.columns[col.index]` 与引用的四个字段全等，而 `(qualifier, name)`
在同一 Schema 内唯一，因此漏改、错改、映射方向写反都会落到名字不同的列上，在构造期报错。
实现裁剪与下推时，这一层不需要额外断言。

### 11.2 校验覆盖不到的两处

1. **执行侧 `source_indexes`**：它不属于任何 Schema，没有校验。取值写错会静默产生错误结果。
   实测中把裁剪后的 Plan 侧 index 当作 `source_indexes` 使用，查询不报错，返回 0 行。
2. **零列来源的处理方式**：把需求为空的来源替换为 `LogicalEmpty` 会静默改变结果行数（第 7.5
   节的例子为 10 行变 0 行），`_check_column` 无法发现。

---

## 12. 实现检查清单

改写 index 的规则（下推、裁剪）在实现时逐条核对：

1. 确认被改写的表达式挂在哪一类节点上，它求值时吃的是 child 输出行还是拼接行。
2. 子节点的 Schema 是否变化。变化时先自底向上算出新 Schema 与每个节点的旧 → 新映射。
3. 映射键使用 `(qualifier, name)`，不使用单独的 `name`。
4. 新 Schema 的 index 从 0 连续编号，对过滤后的列列表重新编号。
5. 谓词下推：左子树换算为恒等，右子树减去该层 `n_left`；归属判定使用 qualifier。
6. 改写只改 `.index`；改写 `Projection.columns` 时使用 child 的映射，不使用 Projection 自身
   的映射。
7. 不触碰 `LogicalUpdate` / `LogicalDelete` 的 child；不裁到零列。
8. 改写完成后由节点 `__post_init__` 校验，计划层错误会在构造期暴露。
9. 执行侧同步提供 `source_indexes`，它与 Plan 侧 index 是两套下标。

---

## 附录 A 实测环境与数据

验证方式：使用 `compiler.parse` + `runner.Runner` + `storage.DatabaseServer`（临时目录）建表
插数，直接读取 `LogicalPlanBuilder` 产出的计划树并遍历执行器打印行元组。V3 优化器未落地，
第 6、7 节的改写计划由脚本按本文规则构造，构造过程调用真实的节点 `__post_init__`。

建表与数据：

```sql
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
```

第 5 节查询的结果：`columns=('u.name', 'o.total')`，`rows=(('alice', 30.5), ('bob', 50.0))`。

第 7 节裁剪计划执行结果与未优化版本一致，`columns` 与 `rows` 均相同。
