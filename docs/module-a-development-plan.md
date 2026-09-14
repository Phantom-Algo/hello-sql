# 模块 A（编译层）V3 个人开发计划与完成记录

> 负责人：A
>
> 当前版本：V3 / Contract 3.0
>
> 依据：`docs/v3-dev/v3-dev-plan.md`、`contracts/ast.py`、`compiler/` 真实实现
>
> 当前状态：V3 索引 DDL 编译能力已经完成，A 模块、V3 公共契约和全项目回归测试全部通过
>
> 核心目标：在保持 V1/V2 SQL 兼容的基础上，把 `CREATE INDEX` 和
> `DROP INDEX` 转换为 C 可以直接消费的统一 AST，并继续提供准确的源码位置和追踪信息

## 一、版本升级说明

V2 已经完成 BOOLEAN、限定列、表别名、INNER JOIN、表达式优先级、
`parse_script()` 和 `SourceSpan`。V3 在这些稳定能力上增加单列、非唯一索引
DDL；不修改 V2 AST 的既有字段，也不改变 `parse()` / `parse_script()` 的公开用法。

本轮 A 的新增范围只有索引 DDL 的词法、语法、AST 输出、错误定位、追踪展示和
回归测试。索引是否存在、目标表或列是否存在、索引文件如何组织以及是否选择索引
执行，仍分别属于 B、C 的职责。

## 二、当前完成情况

| 项目 | V3 实现结果 | 验收状态 |
|---|---|---|
| V1/V2 回归 | 原有数据库、表、DML、BOOLEAN、JOIN、表达式与脚本解析保持可用 | 已完成 |
| INDEX Token | 新增 `TokenType.KW_INDEX` 和 `KEYWORDS["INDEX"]` | 已完成 |
| 关键字大小写 | `INDEX`、`index`、`Index` 均识别为 `KW_INDEX`，原始 lexeme 保持不变 | 已完成 |
| CREATE 分派 | `CREATE DATABASE/TABLE/INDEX` 由统一入口准确分派 | 已完成 |
| DROP 分派 | `DROP DATABASE/TABLE/INDEX` 由统一入口准确分派 | 已完成 |
| CREATE INDEX AST | 支持 `CREATE INDEX name ON table (column)`，三个名称统一小写 | 已完成 |
| DROP INDEX AST | 支持 `DROP INDEX name`，索引名统一小写 | 已完成 |
| 错误位置 | 不完整语法和不支持的 `CREATE UNIQUE INDEX` 返回 `E_SYNTAX` 与全局行列位置 | 已完成 |
| 多语句兼容 | 索引 DDL 可由 `parse_script()` 在完整 Token 流中解析并生成原文与 `SourceSpan` | 已完成 |
| AST 追踪 | 两种索引 AST 均进入 A 的 AST 追踪阶段 | 已完成 |
| AST 可视化 | `/inspect` 和通用树适配器可展示索引节点及其字段 | 已完成 |
| 完整验收 | A 模块 163 项、V3 契约 15 项、全项目 804 项测试通过 | 已完成 |

## 三、A 的职责边界

### 3.1 A 已完成的工作

- 维护 `TokenType`、关键字表、Lexer、Parser 和编译层公开入口。
- 保留 Token 的原始文本、全局一基行列位置以及源码起止偏移。
- 解析 V1/V2 的 BOOLEAN、限定列、别名、JOIN 和逻辑表达式。
- 解析 V3 的单列、非唯一 `CREATE INDEX` 与 `DROP INDEX`。
- 所有进入 AST 的数据库名、表名、列名、别名、限定符和索引名统一转为小写。
- `parse()` 继续只接受一条 SQL；`parse_script()` 直接消费完整 Token 流，
  不使用 `split(";")`。
- 为每条脚本语句保留原始 SQL 和相对于完整脚本的 `SourceSpan`。
- 将 Token、Parser、AST 和 SourceSpan 结果接入既有追踪与可视化链路。
- 通过专项测试、公共契约测试和全项目回归测试固定上述行为。

### 3.2 不由 A 完成的工作

- 判断索引、表或列是否真实存在。
- 判断索引是否重名，以及抛出 `E_INDEX_EXISTS` / `E_INDEX_NOT_FOUND`。
- 创建、删除、维护或持久化 B+ 树索引。
- 在 INSERT、UPDATE、DELETE 后维护索引一致性。
- 根据统计信息选择顺序扫描或索引扫描。
- 构建索引逻辑计划、物理计划、优化规则和执行器。

A 的职责在“语法正确时生成 Contract 3.0 AST”处结束。C 消费 AST 并完成语义、
计划和执行编排；B 是索引存在性、索引数据和存储状态的权威来源。

## 四、V3 索引 DDL 实现记录

### 4.1 Token 与 Lexer

主要文件：`compiler/tokens.py`、`compiler/lexer.py`

新增 Token：

```python
TokenType.KW_INDEX
```

新增关键字映射：

```python
"INDEX": TokenType.KW_INDEX
```

Lexer 扫描标识符后使用大写形式查询 `KEYWORDS`，因此三种大小写输入都会得到
`KW_INDEX`；Token 的 `lexeme` 仍保留用户输入原文。V3 没有新增符号，
`CREATE INDEX` 使用的 `ON`、`(`、`)` 和 `;` 均复用已有 Token。

每个 Token 继续携带：

- `position.line`：相对于完整输入的一基行号；
- `position.column`：相对于完整输入的一基列号；
- `start_offset`：Token 在完整源码中的起始偏移；
- `end_offset`：Token 在完整源码中的半开结束偏移。

### 4.2 Parser 分派函数

主要文件：`compiler/parser.py`

#### `_parse_create_statement()`

该函数先消费 `CREATE`，再检查后续 Token，并在 `DATABASE`、`TABLE`、`INDEX`
三种语法之间分派。遇到 `KW_INDEX` 时进入 `_parse_create_index_statement()`。
它只决定语法分支，不访问 Catalog 或 Storage。

#### `_parse_create_index_statement()`

该函数按固定顺序消费：

```text
INDEX identifier ON identifier ( identifier )
```

三个标识符都通过 `parse_identifier()` 读取，因此索引名、表名和列名进入 AST
前统一转为小写。缺少名称、`ON`、括号或列名时，函数在第一个不符合预期的
Token 处抛出 `ParseError(E_SYNTAX)`。

#### `_parse_drop_statement()`

该函数先消费 `DROP`，再在 `DATABASE`、`TABLE`、`INDEX` 三种分支之间选择。
遇到 `KW_INDEX` 时进入 `_parse_drop_index_statement()`，不改变原有数据库和表
删除语法。

#### `_parse_drop_index_statement()`

该函数消费 `INDEX` 后读取一个索引名并返回 `DropIndexStmt`。V3 规定索引名在
同一数据库中唯一，因此语法中不携带表名。索引是否存在由 B 判断，不属于 Parser。

#### `parse()` 与 `parse_script()`

`parse(sql)` 保持单语句入口兼容，索引 DDL 与已有 SQL 使用相同的结尾检查。
`parse_script(source)` 继续直接消费一份完整 Token 流，可以在同一脚本中连续解析
创建和删除索引，并为每条语句生成正确的原文与全局 `SourceSpan`。

### 4.3 V3 AST 输出

公共契约位于 `contracts/ast.py`，两个节点都使用冻结数据类，并已加入
`Statement` 联合类型。

输入：

```sql
CREATE INDEX Idx_Users_ID ON Users (ID);
```

输出：

```python
CreateIndexStmt(
    index_name="idx_users_id",
    table="users",
    column="id",
)
```

输入：

```sql
DROP INDEX Idx_Users_ID;
```

输出：

```python
DropIndexStmt(index_name="idx_users_id")
```

V3 明确不支持 `UNIQUE INDEX`、多列组合索引、`IF EXISTS`、`IF NOT EXISTS`
以及在 `DROP INDEX` 后指定表名。这些输入应返回语法错误，而不是生成近似 AST。

## 五、索引 DDL 文法与错误行为

### 5.1 文法

```ebnf
createIndexStmt := CREATE INDEX identifier ON identifier
                   '(' identifier ')'
dropIndexStmt   := DROP INDEX identifier
```

该文法与 V2 语法共同存在；V2 的 SELECT、BOOLEAN、JOIN 和表达式规则不变。

### 5.2 必须成功的输入

```sql
CREATE INDEX idx_users_id ON users (id);
DROP INDEX idx_users_id;
```

### 5.3 必须返回 `E_SYNTAX` 的输入

```sql
CREATE INDEX;
CREATE INDEX idx;
CREATE INDEX idx ON users;
CREATE INDEX idx ON users ();
DROP INDEX;
CREATE UNIQUE INDEX idx ON users (id);
```

错误行列必须来自 Lexer 的全局位置。多语句脚本中第二条索引 DDL 出错时，位置不能
从第二条语句重新按第一行、第一列计算。

## 六、测试覆盖与验收记录

### 6.1 测试类型

| 测试类型 | 主要测试文件 | 验证内容 |
|---|---|---|
| Token 识别 | `tests/A_tests/test_lexer_v3_tokens.py` | 大小写、完整 Token 顺序、lexeme、偏移 |
| CREATE 分派 | `tests/A_tests/test_parser_v3_create_dispatch.py` | DATABASE/TABLE/INDEX 分支互不干扰 |
| CREATE INDEX AST | `tests/A_tests/test_parser_v3_create_index.py` | AST 类型、字段和值 |
| DROP 分派 | `tests/A_tests/test_parser_v3_drop_dispatch.py` | DATABASE/TABLE/INDEX 分支互不干扰 |
| DROP INDEX AST | `tests/A_tests/test_parser_v3_drop_index.py` | AST 类型、字段和值 |
| 错误语法和位置 | `tests/A_tests/test_parser_v3_index_errors.py` | `E_SYNTAX` 与全局行列位置 |
| 多语句与 SourceSpan | `tests/A_tests/test_parser_v3_index_compatibility.py` | 原文、范围、parse 兼容性 |
| V1/V2 回归 | `tests/A_tests/test_parser_v3_regression.py` | CREATE TABLE 与 V2 SELECT 精确 AST |
| AST 追踪 | `UI/tests/test_compiler_trace.py` | 索引 AST 进入 A 的追踪阶段 |
| AST 可视化 | `tests/A_tests/test_ast_tree_adapter.py`、`UI/tests/test_viewer.py` | 树节点、字段和值可视化 |
| 公共契约 | `tests/test_v3_contract.py` | Contract 3.0、Statement 联合与冻结字段 |

### 6.2 最终验收命令和结果

第一项，验收 A 的全部模块测试：

```bash
.venv/bin/python -m pytest -q tests/A_tests
```

结果：`163 passed`。

第二项，验收 V3 公共契约：

```bash
.venv/bin/python -m pytest -q tests/test_v3_contract.py
```

结果：`15 passed`。

第三项，验收整个项目的 V1/V2/V3 和 UI 回归：

```bash
.venv/bin/python -m pytest -q
```

结果：`804 passed`。

最终结论：三项命令退出码均为 0，`CREATE INDEX` 和 `DROP INDEX` 能从 SQL
正确生成名称已小写化的 AST，A 的 V3 阶段任务已完成。

## 七、A 向 C 的索引 AST 交接

### 7.1 C 应导入的公共节点

```python
from compiler import parse, parse_script
from contracts.ast import CreateIndexStmt, DropIndexStmt, Statement
```

C 应通过 `compiler` 包公开的 `parse` / `parse_script` 入口接收 A 的结果，并只依赖
`contracts.ast` 中的公共类型；不应从 `compiler.parser` 导入 Parser 内部类或调用
以下划线开头的解析函数。

### 7.2 CREATE INDEX 交接示例

A 的输入与输出：

```python
statement = parse("CREATE INDEX Idx_Users_ID ON Users (ID);")

assert statement == CreateIndexStmt(
    index_name="idx_users_id",
    table="users",
    column="id",
)
```

C 接收后可按公共节点类型分派：

```python
match statement:
    case CreateIndexStmt(index_name=name, table=table, column=column):
        # C 负责语义检查与执行编排，再通过 BaseStorage 公共接口交给 B。
        storage.create_index(name, table, column)
```

字段含义：

- `index_name`：已经小写化的索引名；
- `table`：已经小写化的目标表名；
- `column`：已经小写化的单个目标列名。

C 需要按团队契约处理表和列语义；索引是否已经存在是 B 的权威事实，最终由
`BaseStorage.create_index()` 返回 `E_INDEX_EXISTS` 等存储边界错误。

### 7.3 DROP INDEX 交接示例

A 的输入与输出：

```python
statement = parse("DROP INDEX Idx_Users_ID;")

assert statement == DropIndexStmt(index_name="idx_users_id")
```

C 接收后可按公共节点类型分派：

```python
match statement:
    case DropIndexStmt(index_name=name):
        # 索引存在性由 B 判定，C 不维护第二份索引存在状态。
        storage.drop_index(name)
```

`DropIndexStmt` 不携带表名和列名。C 不应根据历史 Catalog 快照自行拼接这些字段；
应把索引名交给 B 的 `drop_index()`，由 B 成功删除或返回 `E_INDEX_NOT_FOUND`。

### 7.4 多语句交接示例

```python
parsed = parse_script(
    "CREATE INDEX Idx ON Users (ID);\n"
    "DROP INDEX Idx;"
)
```

`parsed` 中每个 `ParsedStatement` 都包含：

- `statement`：`CreateIndexStmt` 或 `DropIndexStmt`；
- `sql`：对应语句的原始 SQL；
- `span`：相对于完整脚本的 `SourceSpan`。

C 在执行脚本时应使用 `statement` 做类型分派，使用 `sql` 和 `span` 做错误展示或
追踪关联，不应再次对原始字符串执行 `split(";")` 或重新解析名称。

## 八、V3 十二步完成记录

- [x] 步骤一：增加 `KW_INDEX` 和关键字映射。
- [x] 步骤二：接入 `CreateIndexStmt`、`DropIndexStmt` 与 `Statement` 联合类型。
- [x] 步骤三：扩展 CREATE 语句分派。
- [x] 步骤四：实现 `CREATE INDEX` 解析与名称小写化。
- [x] 步骤五：扩展 DROP 语句分派。
- [x] 步骤六：实现 `DROP INDEX` 解析与名称小写化。
- [x] 步骤七：补齐错误语法与全局错误位置。
- [x] 步骤八：保持 `parse()`、`parse_script()`、原文和 `SourceSpan` 兼容。
- [x] 步骤九：接入追踪与 AST 可视化。
- [x] 步骤十：补充 Token、AST、错误、脚本、回归和可视化测试。
- [x] 步骤十一：完成 A、V3 契约与全项目验收。
- [x] 步骤十二：更新 A 的 V3 个人文档并提供 C 交接示例。

## 九、最终完成标准

- [x] 文档版本已从 V2 更新为 V3，并以 Contract 3.0 为准。
- [x] `INDEX/index/Index` 均稳定识别为 `KW_INDEX`。
- [x] `CREATE INDEX` 生成 `CreateIndexStmt(index_name, table, column)`。
- [x] `DROP INDEX` 生成 `DropIndexStmt(index_name)`。
- [x] 索引名、表名和列名进入 AST 前统一转为小写。
- [x] 非法索引语法返回 `E_SYNTAX`，错误位置准确。
- [x] `parse()` 与 `parse_script()` 保持兼容，SourceSpan 和原文准确。
- [x] 索引 AST 已进入追踪和可视化链路，未修改公共追踪契约。
- [x] A 模块 163 项测试全部通过。
- [x] V3 公共契约 15 项测试全部通过。
- [x] 全项目 804 项测试全部通过。
- [x] 已向 C 提供 `CreateIndexStmt` 与 `DropIndexStmt` 的消费示例和职责边界。

A 的 V3 编译层任务至此完成。后续如扩展 UNIQUE、多列索引、`IF EXISTS` 或新的
索引语法，必须先更新三方公共契约和 V3/V4 计划，再修改 Token、Parser 与测试。
