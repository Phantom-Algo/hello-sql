"""索引常量前提测试（V3 D28/D29）。

这两条不是"随手断言常量"，而是**空闲页链表能被复用的前提**：
索引页 0 的 free_head 偏移与大小必须与表文件一致，否则 pager 的
alloc / free / free_pages 无法原样服务索引文件。
"""

from __future__ import annotations

from storage.constants import (
    INDEX_MAGIC,
    INDEX_PAGE0_FREE_HEAD_OFFSET,
    INDEX_PAGE0_FREE_HEAD_SIZE,
    PAGE0_FREE_HEAD_OFFSET,
    PAGE0_FREE_HEAD_SIZE,
    TABLE_FILE_MAGIC,
)


def test_index_magic_differs_from_table_magic() -> None:
    """索引文件必须能被识别成"不是表文件"，否则身份校验形同虚设。"""
    assert INDEX_MAGIC != TABLE_FILE_MAGIC


def test_index_free_head_offset_matches_table() -> None:
    """空闲页链表复用的前提：两种页 0 的 free_head 位于同一位置与宽度。"""
    assert INDEX_PAGE0_FREE_HEAD_OFFSET == PAGE0_FREE_HEAD_OFFSET
    assert INDEX_PAGE0_FREE_HEAD_SIZE == PAGE0_FREE_HEAD_SIZE
