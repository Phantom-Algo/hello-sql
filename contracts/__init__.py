"""共享契约包（V3.1）。

三方共同约定：
- contracts/ast.py     —— A 输出、C 输入
- contracts/storage.py —— B 实现、C 调用
- contracts/errors.py  —— 错误码，三方共用
- contracts/result.py  —— C 输出

V1 的历史冻结稿见 docs/contract-v1.md，V2 的范围与验收见
docs/v2-dev/v2-dev-plan.md，V3 的功能范围、公共契约与数据流见
docs/v3-dev/v3-dev-plan.md。

contracts 仍只保存跨模块数据格式和协议，不承载具体实现：V3 新增的
索引与统计只定义形状和语义边界，索引文件布局、维护时机与统计采集
方式由 storage/ 自行设计。
"""

__version__ = "3.1"
