# hello-sql

三人并行开发的最小 SQL demo：A=编译（compiler/）、B=存储（storage/）、
C=运行（runner/）。所有公共约定已经冻结：

- 契约规范：[docs/contract-v1.md](docs/contract-v1.md)
- 类型定义与 AST：[contracts/ast.py](contracts/ast.py)
- 存储（B 的实现入口）：`storage/__init__.py` 暴露 `DatabaseServer`（库层）与 `Storage`（表层）；方法清单见 [docs/contract-v1.md](docs/contract-v1.md) 第 3 节
- 存储共享形状（Row/TableInfo）：[contracts/storage.py](contracts/storage.py)
- 错误码：[contracts/errors.py](contracts/errors.py)
- 执行结果：[contracts/result.py](contracts/result.py)
- 三方共同基准：[tests/golden_sql.py](tests/golden_sql.py)
- 装配入口：`main.py`（唯一同时 import 三家的文件）

已接通三层真实实现，完整 golden 序列包含 37 条 SQL。

## 启动与终端界面

安装后直接运行 `hello-sql`。这是本地数据库终端，启动时打开数据目录，
无需另启后台服务，也不监听网络端口。

艺术字由 **pyfiglet 的 ansi_shadow 字体**实时生成，Rich 添加蓝紫粉渐变并绘制
结果表格和欢迎信息；没有手工绘制 HELLO-SQL 的字符矩阵。
prompt_toolkit 提供语法高亮、Tab 补全、历史记录和快捷键。
终端较窄时改为上下布局，必要时使用紧凑字体。

### 一步安装，任意目录启动

准备条件：安装 Python 3.11 或更高版本，并能访问 Python 包仓库。
安装器只写当前用户目录，不需要管理员权限；会创建隔离运行环境、安装全部依赖、
创建 `hello-sql` 命令并在必要时把命令目录加入用户 PATH。

macOS / Linux，在项目根目录执行：

```bash
./install.sh
```

如果下载工具没有保留脚本的可执行权限，运行 `sh install.sh`，仍然只需一条命令。

Windows PowerShell，在项目根目录执行：

```powershell
.\install.ps1
```

Windows CMD 也可以双击 `install.bat`，或在命令行执行 `install.bat`。
如果 PowerShell 的脚本策略禁止运行 `.ps1`，使用无需调整策略的命令：

```powershell
py -3 install.py
```

安装结束后按提示重新打开终端，随后无需激活项目虚拟环境：

```bash
hello-sql
hello-sql --help
hello-sql --version
```

再次执行安装脚本即可升级或修复安装。开发时可运行 `./install.sh --editable`；
不希望安装器修改 PATH 时使用 `--no-path`。`--force` 只用于确认覆盖命令目录中
已有且并非本安装器创建的同名启动文件。

### 数据目录与旧数据

默认使用固定目录 `~/.hello-sql/data`，不随当前工作目录改变。
优先级为 `--data-dir` > `HELLO_SQL_DATA_DIR` 环境变量 > 默认目录。
旧版项目根目录的 `data/` **不会自动迁移或删除**；要继续使用它，请显式指定：

```bash
hello-sql --data-dir /absolute/path/to/hello-sql/data
hello-sql --data-dir /absolute/path/to/hello-sql/data --database shop
```

`--database` 选择已经存在的库，默认库 `main` 自动创建。欢迎页显示实际数据路径。
如希望每次自动打开旧数据，可在自己的 shell 配置中设置
`HELLO_SQL_DATA_DIR` 为旧数据的绝对路径。
底层仍按原契约使用单进程单线程，请勿让多个进程同时写同一数据目录。

### 常用命令

| 命令 / 快捷键 | 行为 |
|---|---|
| `/help` | 查看帮助 |
| `/databases`、`/tables` | 查看数据库 / 当前库的表 |
| `/describe users` | 查看列名和类型 |
| `/inspect [ALL\|A\|B\|C]` | 查看最近 SQL 的全链路或指定负责模块 |
| `USE shop;` | 切换库，输入提示符同步更新 |
| `/clear` | 清屏 |
| `/quit`、`quit`、`exit`、Ctrl+D | 退出（Ctrl+D 在空输入时） |
| Tab、↑↓ | 补全支持的关键字/库名/表名、浏览历史 |
| Ctrl+C | 清空尚未提交的输入 |

执行 SQL 后输入 `/inspect`，交互模式会打开本地浏览器窗口，
默认查看 A+B+C 完整流程；
`/inspect A`、`/inspect B` 和 `/inspect C` 可以切换负责模块。
查看操作只读取最近追踪快照，不会重新执行 SQL。

一个输入缓冲区可包含多行和多条 SQL；Enter 执行，Alt+Enter 插入换行。
末尾分号可省略。
Ctrl+C 不是事务回滚命令。历史存于所选数据目录中的 `.hello_sql_history`，
`--no-history` 可禁用磁盘历史。历史文件无法写入时降级为内存历史。

```bash
hello-sql --plain                         # 无艺术字的纯文本模式
hello-sql -e "SELECT * FROM users;"       # 执行一条 SQL 后退出
hello-sql --data-dir ./data < demo.sql    # 脚本每行一条 SQL
```

非交互输入/输出自动使用纯文本，不打印欢迎页或 ANSI 颜色。
脚本中 SQL 错误会打印错误并继续下一行，最终退出码为 1；`-e` 失败退出码也是 1。

## 调用关系（包名即 import 名）

```python
from compiler import parse          # A：SQL -> AST
from storage import DatabaseServer  # B：库层 + 表层，Storage 由 connect 得到
from runner import Runner           # C：执行与 REPL

# main.py 装配（唯一允许同时接触三家的地方）
server = DatabaseServer("data")     # 自动创建默认库 main
runner = Runner(server=server, parse=parse)
```

运行期由 runner 维护“当前库”：USE 时换一个 `server.connect(...)` 的
Storage，然后调用它的 describe / scan / insert / update_row / delete_row。
compiler、storage、runner 之间互不 import，只允许 import contracts
里的共享数据格式。

## 环境配置（统一 Python 3.11）

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
hello-sql                       # 此安装方式需先激活 .venv
python -m pytest -q
```

`python3 main.py` 仍可使用，现与 `hello-sql` 共用参数和默认数据路径。
核心编译、存储与执行只使用标准库；终端依赖清单统一维护在 `pyproject.toml`。

可用真实渲染器导出静态预览（示例数据，不访问实际数据库）：

```bash
python -m scripts.preview_terminal /tmp/hello-sql-preview.svg
```
