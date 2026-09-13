"""页级原语（PRD §6；D04/D05/D06）。

职责：把“表文件 = 一维页数组”落地——页 0 文件头、页分配/释放、
按页读写；具体行 / 槽布局属于 engine，本层不解释记录。

不变量：
- 表文件长度恒为 PAGE_SIZE 的整数倍，否则视为损坏（E_STORAGE）；
- 页 0 永不释放、不存用户行；字段布局见 constants（D04）；
- 文件只增不减，不自动收缩（D06）；
- magic / version / 文件长度校验失败 → E_STORAGE（契约 §4）。

目标不变量（随阶段生效，见“实现阶段”）：
- M3 起：一切页读写经 BufferPool，本层不直接裸 I/O（§6.5）；
- M4 起：空闲页链表——页 0 free_head 指向头块，空闲页头部前 4 B 存 next，
  FREE_LIST_END(0) 表示链尾；alloc 优先弹空闲链表（D05）。

错误归属：本层是 B 内部原语，所有失败统一 E_STORAGE（E_BAD_ARG 只属于
公开方法对库名/表名/值的边界，D13）。

实现阶段：M1 已完成（固定页、追加页、页 0）；M3 已完成（read/write 走
BufferPool）；M4 已完成（空闲页链表 free_page/alloc 弹链/free_pages）。

追踪：带 BufferPool 参数的页级原语会记录真实返回值或 E_STORAGE；
create_table_file 和纯校验辅助函数不独立生成事件，避免重复噪声。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    FIRST_ROW_ID,
    FREE_LIST_END,
    PAGE0_FREE_HEAD_OFFSET,
    PAGE0_FREE_HEAD_SIZE,
    PAGE0_HEADER_SIZE,
    PAGE_SIZE,
    TABLE_FILE_MAGIC,
    TABLE_FILE_VERSION,
)
from storage.trace_hooks import trace_storage_operation


# 页 0 头部 20 B：magic(4s) + version(H) + reserved(H) + next_row_id(Q) + free_head(I)
_PAGE0_STRUCT = struct.Struct("<4sHHQI")


def create_table_file(file_path: Path) -> None:
    """建表文件并写页 0（magic/version/next_row_id=1/free_head=0），flush。

    列定义不写进本文件（页式系统表 catalog 是权威，D20）。
    文件已存在（孤儿文件）时直接覆盖重建：catalog 是权威，孤儿数据属垃圾。
    """
    page = bytearray(PAGE_SIZE)
    _PAGE0_STRUCT.pack_into(
        page,
        0,
        TABLE_FILE_MAGIC,
        TABLE_FILE_VERSION,
        0,  # reserved
        FIRST_ROW_ID,
        FREE_LIST_END,
    )
    try:
        with open(file_path, "wb") as fh:
            written = fh.write(page)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot create table file: {file_path}") from exc
    if written != PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write creating table file: {file_path}")


def _table_page_count(file_path: Path) -> int:
    """文件长度 → 页数；缺失/半页/空文件都视为损坏（E_STORAGE）。"""
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot access table file: {file_path}") from exc
    if size < PAGE_SIZE or size % PAGE_SIZE != 0:
        raise SqlError(
            E_STORAGE,
            f"corrupt table file {file_path}: size {size} is not page-aligned",
        )
    return size // PAGE_SIZE


@trace_storage_operation("pager", "page_count")
def page_count(pool: BufferPool, file_path: Path) -> int:
    """返回文件当前页数（engine scan/遍历用；缺失/半页 → E_STORAGE）。

    PRD §6.5 未列此原语，但 scan 需要知道文件有几页，M2 补充。
    pool 形参同其他原语：M3 起内部可改走缓存，调用方不变。
    """
    return _table_page_count(file_path)


@trace_storage_operation("pager", "free_pages")
def free_pages(pool: BufferPool, file_path: Path) -> list[int]:
    """遍历空闲页链表，返回链序（最新释放在前）的页号列表（D05）。

    链成环 / next 越界 / 自环 → E_STORAGE；engine 扫描前用本函数避开空闲页。
    """
    total_pages = _table_page_count(file_path)
    page0 = read_page(pool, file_path, 0)
    head = int.from_bytes(
        page0[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + PAGE0_FREE_HEAD_SIZE],
        "little",
    )
    result: list[int] = []
    seen: set[int] = set()
    while head != FREE_LIST_END:
        if head in seen or not 0 < head < total_pages:
            raise SqlError(
                E_STORAGE,
                f"corrupt free list in {file_path}: invalid page {head}",
            )
        seen.add(head)
        result.append(head)
        free_page_bytes = read_page(pool, file_path, head)
        head = int.from_bytes(free_page_bytes[:4], "little")
    return result


def _check_page0(file_path: Path) -> None:
    """校验页 0 头部：magic / version 不符 → E_STORAGE（文件身份/格式错误）。"""
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read(PAGE0_HEADER_SIZE)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot read table file: {file_path}") from exc
    if len(raw) < PAGE0_HEADER_SIZE:
        raise SqlError(E_STORAGE, f"corrupt table file {file_path}: short page 0")
    magic, version, _reserved, _next_row_id, _free_head = _PAGE0_STRUCT.unpack(raw)
    if magic != TABLE_FILE_MAGIC:
        raise SqlError(E_STORAGE, f"not a hello-sql table file: {file_path}")
    if version != TABLE_FILE_VERSION:
        raise SqlError(
            E_STORAGE,
            f"unsupported table file version {version}: {file_path}",
        )


def _check_page_no(file_path: Path, page_no: int, page_count: int) -> None:
    """页号必须是整型且在 [0, page_count) 内，否则 E_STORAGE。"""
    if type(page_no) is not int or page_no < 0 or page_no >= page_count:
        raise SqlError(
            E_STORAGE,
            f"page {page_no!r} out of range in {file_path} ({page_count} pages)",
        )


@trace_storage_operation("pager", "alloc_page")
def alloc_page(pool: BufferPool, file_path: Path) -> int:
    """分配一个数据页号：优先弹空闲页链表，空链表才在文件末尾追加（D05/D06）。

    追加前校验页 0 身份，防止在冒牌/损坏文件上继续扩展（E_STORAGE）。
    """
    total_pages = _table_page_count(file_path)
    _check_page0(file_path)
    page0 = read_page(pool, file_path, 0)
    free_head = int.from_bytes(
        page0[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + PAGE0_FREE_HEAD_SIZE],
        "little",
    )
    if free_head != FREE_LIST_END:
        if not 0 < free_head < total_pages:
            raise SqlError(
                E_STORAGE,
                f"corrupt free list in {file_path}: head {free_head} out of range",
            )
        head_page = read_page(pool, file_path, free_head)
        next_head = int.from_bytes(head_page[:4], "little")
        if next_head == free_head or (
            next_head != FREE_LIST_END and not 0 < next_head < total_pages
        ):
            raise SqlError(
                E_STORAGE,
                f"corrupt free list in {file_path}: next {next_head} invalid",
            )
        updated_page0 = bytearray(page0)
        updated_page0[
            PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + PAGE0_FREE_HEAD_SIZE
        ] = next_head.to_bytes(PAGE0_FREE_HEAD_SIZE, "little")
        write_page(pool, file_path, 0, updated_page0)
        return free_head
    # free list 空 → 文件末尾追加一页（D06：只增不减）
    new_page_no = total_pages
    try:
        with open(file_path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            written = fh.write(bytes(PAGE_SIZE))
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot extend table file: {file_path}") from exc
    if written != PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write extending table file: {file_path}")
    return new_page_no


@trace_storage_operation("pager", "free_page")
def free_page(pool: BufferPool, file_path: Path, page_no: int) -> None:
    """把整页空的数据页还进空闲链表（D05）。

    该页前 4 B 写入旧 free_head 作 next，页 0 free_head 指向该页；
    文件长度不变（D06）。页 0 / 越界页号 → E_STORAGE。
    """
    total_pages = _table_page_count(file_path)
    if type(page_no) is not int or not 0 < page_no < total_pages:
        raise SqlError(
            E_STORAGE,
            f"cannot free page {page_no!r} in {file_path} ({total_pages} pages)",
        )
    page0 = read_page(pool, file_path, 0)
    old_head = int.from_bytes(
        page0[PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + PAGE0_FREE_HEAD_SIZE],
        "little",
    )
    freed = bytearray(read_page(pool, file_path, page_no))
    freed[: PAGE0_FREE_HEAD_SIZE] = old_head.to_bytes(PAGE0_FREE_HEAD_SIZE, "little")
    write_page(pool, file_path, page_no, freed)
    updated_page0 = bytearray(page0)
    updated_page0[
        PAGE0_FREE_HEAD_OFFSET : PAGE0_FREE_HEAD_OFFSET + PAGE0_FREE_HEAD_SIZE
    ] = page_no.to_bytes(PAGE0_FREE_HEAD_SIZE, "little")
    write_page(pool, file_path, 0, updated_page0)


@trace_storage_operation("pager", "read_page")
def read_page(pool: BufferPool, file_path: Path, page_no: int) -> bytes:
    """读一整页返回 bytes 副本。

    M3 起经 BufferPool：缺页读盘、命中直接用（D09）；读页 0 时额外校验
    magic/version；页越界/半页/缺失一律 E_STORAGE。
    """
    page_count = _table_page_count(file_path)
    _check_page_no(file_path, page_no, page_count)
    if page_no == 0:
        _check_page0(file_path)
    frame = pool.get_page(file_path, page_no)
    try:
        return bytes(frame)
    finally:
        pool.unpin_page(file_path, page_no)


@trace_storage_operation("pager", "write_page")
def write_page(
    pool: BufferPool, file_path: Path, page_no: int, data: bytes
) -> None:
    """把一整页内容写回文件偏移 page_no * PAGE_SIZE。

    M3 起写进缓存帧并标脏，真正落盘由 flush 决定（D11）；data 必须恰好
    一整页，页号必须在文件现有范围内，否则 E_STORAGE。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != PAGE_SIZE:
        raise SqlError(
            E_STORAGE, f"write_page requires exactly {PAGE_SIZE} bytes: {file_path}"
        )
    page_count = _table_page_count(file_path)
    _check_page_no(file_path, page_no, page_count)
    frame = pool.get_page(file_path, page_no)
    try:
        frame[:] = data
        pool.mark_dirty(file_path, page_no)
    finally:
        pool.unpin_page(file_path, page_no)
