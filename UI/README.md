# HELLO-SQL 全链路追踪契约

`UI` 是 A、B、C 三个模块共同向查询流程查看器提供数据的观察层。它不改变
SQL 的解析、语义、执行和存储结果，也不能为了生成视图重新执行 SQL。

## 第一阶段公开类型

- `TraceEvent`：阶段内部的一步真实操作；
- `StageTrace`：一个可以在界面中独立查看的阶段；
- `QueryTrace`：一条 SQL 的完整最终快照；
- `TraceOwner`：明确标记 A、B、C 或真正的公共阶段；
- `TraceStatus`：区分成功、失败、跳过和未启用。

## 第二阶段：TraceHub

C 在每条 SQL 开始执行时预约编号，结束后发布只读快照：

```python
from UI import QueryTrace, TraceHub, TraceStatus

hub = TraceHub(capacity=20)
reservation = hub.reserve()

trace = QueryTrace(
    trace_id=reservation.trace_id,
    query_number=reservation.query_number,
    sql="SELECT * FROM students;",
    database="main",
    status=TraceStatus.SUCCESS,
    stages=(),
)
hub.publish(trace)
assert hub.latest() is trace
```

`reserve()` 在查询开始时确定顺序；`publish()` 支持同一记录从运行中状态更新为
最终状态；`recent()` 从新到旧返回固定容量历史；`clear()` 清空内存但不回退
编号。所有操作均为线程安全，适合 REPL 和本地查看窗口并发访问。

## 第三阶段：A 编译追踪

`trace_parse` 与 `trace_parse_script` 在同一次真实编译中生成 Lexer、Parser、
AST 和 SourceSpan 四个阶段：

```python
from UI import trace_parse

traced = trace_parse("SELECT u.id FROM users u WHERE u.enabled = TRUE;")
statement = traced.require_statement()
```

- Lexer：Token 类型、原文、行列位置与字符偏移；
- Parser：真实规则调用顺序、递归深度和 Token 消费；
- AST：不可变 dataclass 树的节点类型、字段和路径；
- SourceSpan：语句原文在完整脚本中的一基闭区间。

追踪入口不修改 `compiler.parse` 的行为，也不会为生成界面数据重新解析 SQL。
词法或语法失败时，失败上游会保留，未运行的下游阶段标记为 `SKIPPED`。

## 第四阶段：B 存储追踪

`StorageTraceRouter` 长期注入 `DatabaseServer`，每条查询使用一次
`capture()`，即可从同一次真实存储调用构建 Catalog、Cache、Pager 和
Engine 四个阶段：

```python
from storage import DatabaseServer
from UI import StorageTraceRouter

router = StorageTraceRouter()
server = DatabaseServer("./data", trace_sink=router)

with router.capture() as storage_trace:
    storage = server.connect("main")
    rows = list(storage.scan("users"))

b_stages = storage_trace.build_stages()
```

- Catalog：展示 Schema 加载、查询、注册、删除及系统表回滚；
- Cache：展示 hit/miss、pin/unpin、dirty、LRU 淘汰和磁盘写回；
- Pager：展示页计数、读写、分配和空闲页链；
- Engine：展示行的插入、扫描、定位、更新、删除和溢出页链。

存储核心不反向导入 `UI`，只通过可选字典回调提交事件。没有注入回调时，
装饰器直接调用原函数。`ContextVar` 保证并发或嵌套查询不会串记录；
页字节只保存长度和前 16 字节预览，避免历史缓存复制整页。

## 第五阶段：C 绑定、计划与运行追踪

`ExecutionTraceRouter` 长期注入 `Runner`，每条 SQL 执行时开启一次
`capture()`：

```python
from compiler import parse, parse_script
from runner import Runner
from UI import ExecutionTraceRouter

router = ExecutionTraceRouter()
runner = Runner(
    server,
    parse,
    parse_script=parse_script,
    trace_sink=router,
)

with router.capture() as execution_trace:
    result = runner.execute("SELECT id FROM users WHERE enabled = TRUE;")

c_stages = execution_trace.build_stages()
```

- Binder：展示表 Schema、列与限定符解析、类型协调、WHERE 和投影；
- Logical Plan：保留真实的 `LogicalScan/Join/Filter/Projection` 树；
- Optimizer：当前没有规则实现，明确标记 `DISABLED`；
- Executor：保留 `SeqScan/Filter/NestedLoopJoin/Projection` 执行树；
- Runtime：记录每个拉取式算子的产出行数、耗时和最多 5 行样例，
  并区分 SELECT 的返回行数与 DML 的影响行数。

名称或类型绑定失败时，Binder 和 Logical Plan 保留失败事件，
Executor 和 Runtime 标记 `SKIPPED`。运行时失败则保留已完成的计划
和 Executor 树。追踪回调自身异常会被隔离，不改变 SQL 结果。

## 第六阶段：`/inspect` 与 A/B/C 模块筛选

`QueryInspector` 是会话级的统一编排器。它让 A 的追踪解析产物成为
本次执行的真实 AST，并在调用 Runner 执行该 AST 时同时打开 B/C
`capture()`。执行结束后，编排器依次合并：

```text
C REPL(1) → A(2–5) → B(6–9) → C(10–14)
```

每条实际执行的语句都在 `TraceHub` 中获得独立编号；多语句脚本还保留
`statement_index`/`statement_count` 和相对整段脚本的 `SourceSpan`。
语法错误会保留 Lexer/Parser 失败阶段，绑定或运行错误会保留已经
完成的上游阶段，但所有原异常契约都保持不变。

在交互终端先执行 SQL，然后使用：

```text
/inspect       # 默认显示 ALL
/inspect A     # Lexer / Parser / AST / SourceSpan
/inspect B     # Catalog / Cache / Pager / Engine
/inspect C     # REPL / Binder / Plan / Optimizer / Executor / Runtime
/inspect ALL   # 恢复全链路
```

参数不区分大小写。筛选仅对最近一个不可变 `QueryTrace` 做内存读取，
不会重新解析 SQL、重放计划或访问存储。交互模式会懒启动一个只监听
`127.0.0.1` 随机端口的本地服务，使用系统默认浏览器打开蓝紫粉暗色查看器。
窗口可以点击阶段查看契约、输入/输出快照和逐事件细节；纯文本/管道模式
不弹窗，只输出无 ANSI 的稳定摘要。

## 第七阶段：节点、Token、数据页和 SQL 原文联动

`UI.linkage.py` 从同一份 `InspectionSnapshot` 构建只读交叉索引：

```text
SQL 原文 ←→ Lexer Token ←→ AST / Logical Plan / Executor 节点
     ↑                                           ↕
     └── 表名 Token ←→ *.table 物理页 ←───────┘
```

- Token 来自 `a.lexer` 的真实偏移和 SourceSpan，不对 SQL 二次分词；
- AST 节点来自 A 的节点事件，Logical Plan/Executor 节点来自 C 的树快照；
- 物理页来自 B Cache/Pager/Engine 真实调用中的表页文件和页号；
- 同一页的 Cache/Pager 事件合并展示，但仍保留原 stage_id/event_id；
- 多语句脚本保留完整原文和全局字符偏移，Token 列表只显示当前语句；
- 子节点没有精确 SourceSpan 契约时，只根据已保存的表/列/限定符/字面量
  建立 `token_inference` 展示关联；无法确定时保持空集，不伪造编译位置。

查看器右侧增加 `STAGE / NODES / TOKENS / PAGES` 四个面板。可以：

1. 点击 SQL 原文中的 Token，查看类型、偏移、SourceSpan、所属节点和页；
2. 点击 AST/计划/Executor 节点，反向高亮 Token 和相关数据页；
3. 点击物理页，查看 Cache/Pager/Engine 操作链并高亮 SQL 表名；
4. 点击带 SourceSpan 的 TraceEvent，直接高亮它覆盖的 Token；
5. 使用“清除联动”恢复未选择状态。

A/B/C 筛选仍生效：Token 作为 SQL 锚点始终保留，节点和页只显示当前
责任模块的对象。所有联动都是浏览器内高亮，不会新增 QueryTrace 或重新执行 SQL。

## 第八阶段：成功与错误链路综合验收

`UI/tests/test_inspection_scenarios.py` 使用真实 Compiler、Runner 和临时
DatabaseServer 覆盖五类答辩场景，不通过 mock 伪造中间结果：

| 场景 | 触发方式 | 可视化验收点 |
|---|---|---|
| 成功 | BOOLEAN + 别名 + INNER JOIN + WHERE | 1–14 阶段完整，结果、Token、节点和页可联动 |
| 语法错误 | `SELECT FROM users` | Lexer 成功、Parser 失败，AST/SourceSpan 跳过，保留精确行列 |
| 语义错误 | 查询 Schema 中不存在的列 | A 全部成功，Binder/Logical Plan 失败，Executor/Runtime 跳过 |
| 存储错误 | 将临时表文件破坏为非整页长度 | Pager/Engine/Runtime 都保留 `E_STORAGE` 失败传播 |
| 多语句 | 中间语句产生 `E_VALUE_COUNT` | 逐条编号、全局 SourceSpan、继续/停止策略均正确 |

专项验收命令：

```bash
python -m pytest UI/tests/test_inspection_scenarios.py -q
```

存储损坏用例只修改 pytest 为当前测试创建的临时目录，不访问、
不修改用户的实际 `data` 目录。所有用例都只断言稳定的契约数据，
不依赖机器耗时、缓存命中率或系统浏览器。

## 建议阶段 ID 与责任归属

| 顺序 | stage_id | 负责人 | 内容 |
|---:|---|:---:|---|
| 1 | `c.repl` | C | 接收 SQL 并创建追踪 |
| 2 | `a.lexer` | A | Token 与源码位置 |
| 3 | `a.parser` | A | 递归下降调用与 Token 消费 |
| 4 | `a.ast` | A | AST 节点树 |
| 5 | `a.source_span` | A | 语句原文与全局 SourceSpan |
| 6 | `b.catalog` | B | 表结构和系统目录查询 |
| 7 | `b.cache` | B | 缓存命中、淘汰、pin 和脏页写回 |
| 8 | `b.pager` | B | 固定页读写、分配与空闲页链 |
| 9 | `b.engine` | B | 行、槽和溢出页链操作 |
| 10 | `c.binding` | C | 列绑定、歧义和类型检查 |
| 11 | `c.logical_plan` | C | 初始逻辑计划 |
| 12 | `c.optimizer` | C | 规则优化；未实现时标记 `DISABLED` |
| 13 | `c.executor` | C | Executor 树构建 |
| 14 | `c.runtime` | C | JOIN、Filter 和 Projection 行流 |
| 15 | `c.render` | C | QueryResult 与终端输出 |

## 交接契约

```text
A → C：ParsedStatement / AST / SourceSpan
C → B：describe / scan / insert / update / delete 等公开调用
B → C：TableInfo / Row / RowId
C → UI：QueryTrace（只读且可 JSON 序列化）
```

任何模块都应先把内部对象转换为简单快照再提交，不能把 Storage、Executor、
文件句柄或回调函数直接放入追踪数据。
