"""索引文件层（V3 D27/D28/D29）。

职责：把"一个索引 = 一个 .idx 文件"落地——页 0 文件头（magic/version/
root/height/free_head）与建文件、按页读写（本层不解释键与树结构）。

B+ 树的节点布局与树算法（插入/分裂/查找/范围/删除）属于 M2，本文件在
M1 只提供：页 0 的建与校验、空叶根页的构造、以及页 0 字段的读取。

不变量：
- 索引文件长度恒为 PAGE_SIZE 的整数倍，页 0 永不释放（D28）；
- 空树也有合法根：页 0 的 root 指向一个 `entry_count == 0` 的空叶页，
  因此不存在"没有根"的特殊状态（D35 的前置条件）；
- free_head 复用表文件页 0 的偏移与宽度，空闲页链表由 pager 直接服务（D29）；
- magic/version/文件长度/root 越界校验失败 → E_STORAGE。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import os

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    FREE_LIST_END,
    INDEX_FILE_VERSION,
    INDEX_MAGIC,
    INDEX_NODE_COUNT_OFFSET,
    INDEX_NODE_FREE_PTR_OFFSET,
    INDEX_NODE_HEADER_SIZE,
    INDEX_NODE_NEXT_LEAF_OFFSET,
    INDEX_NODE_TYPE_OFFSET,
    INDEX_PAGE0_FREE_HEAD_OFFSET,
    INDEX_PAGE0_HEADER_SIZE,
    INDEX_PAGE0_HEIGHT_OFFSET,
    INDEX_PAGE0_ROOT_OFFSET,
    INTERIOR_NODE,
    LEAF_NODE,
    PAGE_SIZE,
)
from storage.pager import INDEX_FILE_KIND, page_count, read_page
from storage.trace_hooks import trace_storage_operation


# 页 0 头部 20 B：magic(4s) + version(H) + reserved(H) + root(I) + height(I) + free_head(I)
_PAGE0_STRUCT = struct.Struct("<4sHHIII")

# 初始空树的根页号与树高（D28：空树 = 一个空叶页，height = 1）。
_EMPTY_ROOT_PAGE = 1
_EMPTY_TREE_HEIGHT = 1


@dataclass(frozen=True)
class IndexFileHeader:
    """索引文件页 0 的对外可读字段。"""

    root_page: int
    height: int
    free_head: int


def new_leaf_page() -> bytearray:
    """构造一个空叶页（V3 D32 节点页布局）。

    页头：node_type(u8) + entry_count(u16) + free_ptr(u32) + next_leaf(u32)。
    条目区从 INDEX_NODE_HEADER_SIZE 起向后写，槽目录自页尾向前长。
    """
    page = bytearray(PAGE_SIZE)
    page[INDEX_NODE_TYPE_OFFSET] = LEAF_NODE
    struct.pack_into("<H", page, INDEX_NODE_COUNT_OFFSET, 0)
    struct.pack_into("<I", page, INDEX_NODE_FREE_PTR_OFFSET, INDEX_NODE_HEADER_SIZE)
    struct.pack_into("<I", page, INDEX_NODE_NEXT_LEAF_OFFSET, 0)
    return page


def create_index_file(file_path: Path) -> None:
    """建索引文件：页 0 头 + 一个空叶根页，落盘后返回。

    与 create_table_file 一致：文件已存在时直接覆盖重建（调用方已确认
    索引不重复；孤儿索引文件属垃圾）。父目录（indexes/）不存在时自动创建。
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SqlError(
            E_STORAGE, f"cannot create index directory: {file_path.parent}"
        ) from exc

    page0 = bytearray(PAGE_SIZE)
    _PAGE0_STRUCT.pack_into(
        page0,
        0,
        INDEX_MAGIC,
        INDEX_FILE_VERSION,
        0,  # reserved
        _EMPTY_ROOT_PAGE,
        _EMPTY_TREE_HEIGHT,
        FREE_LIST_END,
    )
    leaf = new_leaf_page()
    try:
        with open(file_path, "wb") as fh:
            written = fh.write(bytes(page0)) + fh.write(bytes(leaf))
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot create index file: {file_path}") from exc
    if written != 2 * PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write creating index file: {file_path}")


@trace_storage_operation("index", "open_index_file")
def open_index_file(pool: BufferPool, file_path: Path) -> IndexFileHeader:
    """打开索引文件并校验页 0，返回 root / height / free_head。

    magic / version / 文件长度由 pager 按 INDEX_FILE_KIND 校验；本函数再校验
    根页号范围、根页类型与 free_head 范围，任何不符都是 E_STORAGE。
    """
    page0 = read_page(pool, file_path, 0, kind=INDEX_FILE_KIND)
    total_pages = page_count(pool, file_path, kind=INDEX_FILE_KIND)

    root_page = int.from_bytes(
        page0[INDEX_PAGE0_ROOT_OFFSET : INDEX_PAGE0_ROOT_OFFSET + 4], "little"
    )
    height = int.from_bytes(
        page0[INDEX_PAGE0_HEIGHT_OFFSET : INDEX_PAGE0_HEIGHT_OFFSET + 4], "little"
    )
    free_head = int.from_bytes(
        page0[
            INDEX_PAGE0_FREE_HEAD_OFFSET : INDEX_PAGE0_FREE_HEAD_OFFSET + 4
        ],
        "little",
    )

    if height < 1:
        raise SqlError(
            E_STORAGE, f"corrupt index file {file_path}: height {height} is invalid"
        )
    if not 0 < root_page < total_pages:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: root {root_page} out of range "
            f"({total_pages} pages)",
        )
    if free_head != FREE_LIST_END and not 0 < free_head < total_pages:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: free_head {free_head} out of range",
        )

    root = read_page(pool, file_path, root_page, kind=INDEX_FILE_KIND)
    node_type = root[INDEX_NODE_TYPE_OFFSET]
    if node_type not in (LEAF_NODE, INTERIOR_NODE):
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: unknown node type {node_type} "
            f"at root page {root_page}",
        )
    if height == 1 and node_type != LEAF_NODE:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: height 1 requires a leaf root",
        )

    return IndexFileHeader(
        root_page=root_page, height=height, free_head=free_head
    )
