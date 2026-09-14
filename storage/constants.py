"""storage 内部常量与字节布局契约（B 私有，不进公共契约）。

本文件是“常量层面的单一真相”：所有内部模块只从这里取值，
不各自写死 magic / 偏移 / 大小，避免改一处漏多处。
对应决策编号见 .codex/docs/storage/storage_prd.md。
"""

# ---- 页大小与文件命名（D02/D03）----
PAGE_SIZE = 4096
TABLE_FILE_SUFFIX = ".table"
CATALOG_FILE_NAME = "catalog.json"

# V2 页式系统表（D20/D21；M1 只实现自举，M2 起作为权威目录）
SYS_TABLES_FILE_NAME = "sys_tables.db"
SYS_COLUMNS_FILE_NAME = "sys_columns.db"
RESERVED_TABLE_PREFIX = "__sys_"
LEGACY_MIGRATED_FILE_NAME = "catalog.v1.migrated.json"

# ---- 表文件头（页 0）标识（D04）----
TABLE_FILE_MAGIC = b"HSQL"  # 4 B
TABLE_FILE_VERSION = 1      # 写入 2 B

# ---- 页 0 布局（D04）：offset / 大小 ----
PAGE0_MAGIC_OFFSET = 0
PAGE0_MAGIC_SIZE = 4
PAGE0_VERSION_OFFSET = 4
PAGE0_VERSION_SIZE = 2
PAGE0_RESERVED_OFFSET = 6
PAGE0_RESERVED_SIZE = 2
PAGE0_NEXT_ROW_ID_OFFSET = 8
PAGE0_NEXT_ROW_ID_SIZE = 8
PAGE0_FREE_HEAD_OFFSET = 16
PAGE0_FREE_HEAD_SIZE = 4
PAGE0_HEADER_SIZE = 20  # 之后的字节全部置 0，留白
FIRST_ROW_ID = 1        # 新建表第一个可分配的 row_id（D08；页 0 初值）

# ---- 空闲页链表（D05）----
FREE_LIST_END = 0  # 链表尾哨兵：0 表示“没有下一个空闲页”

# ---- 数据页 slotted 布局（D07）----
PAGE_HEADER_SIZE = 8   # u16 slot_count + u16 flags + u32 free_ptr
PAGE_SLOT_COUNT_OFFSET = 0
PAGE_SLOT_COUNT_SIZE = 2
PAGE_FLAGS_OFFSET = 2
PAGE_FLAGS_SIZE = 2
PAGE_FREE_PTR_OFFSET = 4
PAGE_FREE_PTR_SIZE = 4
SLOT_SIZE = 8          # u32 record_offset + u32 record_length
RECORD_HEADER_SIZE = 8  # 记录头：u64 row_id
TEXT_LEN_SIZE = 4
INT_SIZE = 8
REAL_SIZE = 8
BOOL_SIZE = 1
BOOL_FALSE_BYTE = 0x00
BOOL_TRUE_BYTE = 0x01

# 单条记录能放进一个数据页的最大编码长度（≈ 4080 B）
INLINE_RECORD_LIMIT = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_SIZE

# ---- 超长行溢出页链（D14，M5 定稿）----
# 槽长度最高位 = 溢出锚点标志；inline 记录最大 4080B，永不触及该位。
SLOT_OVERFLOW_FLAG = 0x80000000

# 锚点记录（存放在普通数据页槽里，物理 16B）：
# u64 row_id + u32 first_chain_page + u32 total_len
OVERFLOW_ANCHOR_SIZE = 16
OVERFLOW_ANCHOR_ROW_ID_OFFSET = 0
OVERFLOW_ANCHOR_ROW_ID_SIZE = 8
OVERFLOW_ANCHOR_FIRST_PAGE_OFFSET = 8
OVERFLOW_ANCHOR_FIRST_PAGE_SIZE = 4
OVERFLOW_ANCHOR_TOTAL_LEN_OFFSET = 12
OVERFLOW_ANCHOR_TOTAL_LEN_SIZE = 4

# 溢出页头（每页 16B）：magic b"OVFL" + u32 next_page + u64 total_len。
# magic 让 scan 能识别链页、不与数据页/空闲页混读（替换 12B 草案）。
OVERFLOW_MAGIC = b"OVFL"
OVERFLOW_HEADER_SIZE = 16
OVERFLOW_NEXT_PAGE_OFFSET = 4
OVERFLOW_NEXT_PAGE_SIZE = 4
OVERFLOW_TOTAL_LEN_OFFSET = 8
OVERFLOW_TOTAL_LEN_SIZE = 8
OVERFLOW_PAYLOAD_SIZE = PAGE_SIZE - OVERFLOW_HEADER_SIZE

# 单行编码总长上限（防演示把内存/缓存撑爆；超出仍 E_STORAGE）
MAX_ROW_BYTES = 16 * 1024 * 1024

# ---- 缓存（D16）----
DEFAULT_CACHE_CAPACITY = 64

# ---- V1 catalog.json（D12；V2 起只作为一次性迁移输入，不再创建）----
CATALOG_VERSION = 1
JSON_VERSION_KEY = "version"
JSON_TABLES_KEY = "tables"
JSON_COLUMNS_KEY = "columns"
JSON_NAME_KEY = "name"
JSON_TYPE_KEY = "type"

# ---- V3 索引：系统表与文件命名（D27/D28/D30）----
# 第三张内部系统表；它本身是普通表文件（magic HSQL），不是索引文件。
SYS_INDEXES_FILE_NAME = "sys_indexes.db"
# 每个索引一个独立文件，统一放在每库的 indexes/ 子目录下（D27）。
INDEX_DIR_NAME = "indexes"
INDEX_FILE_SUFFIX = ".idx"

# ---- V3 索引文件页 0（D28）----
# 与表文件页 0 同为 20 B，且 free_head 的偏移/宽度完全一致，
# 因此 pager 的空闲页链表可以原样复用（D29，见 test_index_constants_m1）。
INDEX_MAGIC = b"HSIX"          # 4 B，区分索引文件与表文件
INDEX_FILE_VERSION = 1         # 2 B
INDEX_PAGE0_MAGIC_OFFSET = 0
INDEX_PAGE0_MAGIC_SIZE = 4
INDEX_PAGE0_VERSION_OFFSET = 4
INDEX_PAGE0_VERSION_SIZE = 2
INDEX_PAGE0_RESERVED_OFFSET = 6
INDEX_PAGE0_RESERVED_SIZE = 2
# 键类型标记（M2 决策 3）：写在原本 reserved 的 2 字节里，让索引文件自描述，
# 避免传错列类型时 INT/REAL 同为 8 字节而静默解出错序。
INDEX_PAGE0_KEY_TYPE_OFFSET = 6
INDEX_PAGE0_KEY_TYPE_SIZE = 2
INDEX_PAGE0_ROOT_OFFSET = 8    # u32 根页号，空树指向一个空叶页
INDEX_PAGE0_ROOT_SIZE = 4
INDEX_PAGE0_HEIGHT_OFFSET = 12  # u32 树高，空树 = 1
INDEX_PAGE0_HEIGHT_SIZE = 4
INDEX_PAGE0_FREE_HEAD_OFFSET = 16
INDEX_PAGE0_FREE_HEAD_SIZE = 4
INDEX_PAGE0_HEADER_SIZE = 20

# ---- V3 索引节点页（D32）----
# 与数据页同样采用 slotted 写法：条目区自页头向后、槽目录自页尾向前。
INDEX_NODE_HEADER_SIZE = 16
INDEX_NODE_TYPE_OFFSET = 0     # u8：LEAF_NODE / INTERIOR_NODE
INDEX_NODE_TYPE_SIZE = 1
INDEX_NODE_COUNT_OFFSET = 1    # u16 条目数
INDEX_NODE_COUNT_SIZE = 2
INDEX_NODE_FREE_PTR_OFFSET = 4  # u32 条目区当前末尾
INDEX_NODE_FREE_PTR_SIZE = 4
INDEX_NODE_NEXT_LEAF_OFFSET = 8  # u32 仅叶页使用，0 = 链尾
INDEX_NODE_NEXT_LEAF_SIZE = 4
# offset 12 按节点类型复用：叶页存前驱叶页号（M2 决策 1，删除时 O(1) 摘链），
# 内节点存最左子页号。
INDEX_NODE_PREV_LEAF_OFFSET = 12
INDEX_NODE_PREV_LEAF_SIZE = 4
INDEX_NODE_FIRST_CHILD_OFFSET = 12
INDEX_NODE_FIRST_CHILD_SIZE = 4
LEAF_NODE = 1
INTERIOR_NODE = 2
INDEX_SLOT_SIZE = 8            # u32 偏移 + u32 长度
INDEX_RID_SIZE = 8             # u64 rid
INDEX_CHILD_PTR_SIZE = 4       # u32 子页号

# 键类型标记取值（与 SqlType 的对应关系定义在 storage/index.py）
INDEX_KEY_TYPE_INT = 1
INDEX_KEY_TYPE_REAL = 2
INDEX_KEY_TYPE_TEXT = 3
INDEX_KEY_TYPE_BOOLEAN = 4

# 单个条目的最大键长度：整页减去页头与一个槽，再减去条目里的定长指针。
# 超过它的键无法被索引（M2 决策 2：报 E_STORAGE），TEXT 值过大的列不可索引。
MAX_INDEX_KEY_BYTES = (
    PAGE_SIZE
    - INDEX_NODE_HEADER_SIZE
    - INDEX_SLOT_SIZE
    - max(INDEX_RID_SIZE, INDEX_CHILD_PTR_SIZE)
)

# ---- V3 统计（D37/D38/D41）----
# 列级统计只采样前 N 个活动数据页：结果是有界近似值，契约允许。
STATS_SAMPLE_PAGES = 16
