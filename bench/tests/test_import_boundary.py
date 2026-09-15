"""导入红线：bench 只准走三家公开入口（DV3-05）。

只用 AST 检查 `bench/*.py`（测试目录可以为了 monkeypatch 深入内部），
因此将来有人顺手 `from storage.engine import ...` 时这条会先红。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


BENCH_DIR = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = {"contracts", "compiler", "runner", "storage", "bench"}
BANNED_PREFIXES = (
    "storage.engine",
    "storage.pager",
    "storage.index",
    "storage.cache",
    "storage.stats",
    "storage.catalog",
    "storage.syscatalog",
    "storage.valuecodec",
    "storage.trace_hooks",
    "storage.constants",
    "runner.physical",
    "runner.executor",
    "runner.logical_plan",
    "runner.trace_hooks",
    "compiler.parser",
    "compiler.lexer",
    "compiler.tokens",
    "UI",
)


def _module_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_bench_modules_only_import_public_entrypoints() -> None:
    sources = sorted(BENCH_DIR.glob("*.py"))
    assert sources, "bench 包为空"
    for path in sources:
        for module in _module_names(path):
            root = module.split(".")[0]
            if root not in ALLOWED_ROOTS:
                # 标准库（含 __future__）与第三方包不受这条红线约束。
                assert root in sys.stdlib_module_names, (
                    f"{path.name} 导入了非公开模块：{module}"
                )
                continue
            assert not module.startswith(BANNED_PREFIXES), (
                f"{path.name} 越过公开入口：{module}"
            )
