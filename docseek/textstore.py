"""正文的存取编码：把抽取出来的文本压缩后存进索引库。

为什么要把正文也存一份
----------------------
检索阶段需要用**原文**做精确校验并生成命中片段（FTS 只负责粗筛）。如果每次搜索
都重新抽取候选文档，遇到 `.doc/.wps`（要调 LibreOffice，1~2 秒一份）或几百页的
PDF 就会卡到几秒甚至几十秒——实测一次两词查询花了 30 秒，完全不能接受。

代价很小：本机 382 份文档共 3.3 MB 正文，zlib 压缩后只占索引库零头。
超过 `MAX_STORED` 的超长正文不存，检索时仍旧现抽（有降级路径）。
"""

import zlib

# 正文存储格式版本：改动存储方式时 +1（用它判断要不要整体回填，
# 而不是用 MAX_STORED——否则调一下上限就会触发几万份文档重抽）
CONTENT_VERSION = 1

# 单份最多存多少字符的正文。上限调大不会触发回填（见上）。
MAX_STORED = 4000000
LEVEL = 6


def pack_content(text):
    """正文 → BLOB（太长或为空时返回 None）。"""
    if not text:
        return None
    if len(text) > MAX_STORED:
        return None
    try:
        return zlib.compress(text.encode("utf-8", "replace"), LEVEL)
    except (zlib.error, MemoryError):
        return None


def unpack_content(blob):
    """BLOB → 正文（解不开时返回 None，让调用方回退到现抽）。"""
    if blob is None:
        return None
    if isinstance(blob, str):           # 有些驱动会给出 str
        return blob
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except (zlib.error, MemoryError):
        return None


def pack_pages(offsets):
    """分页偏移 → TEXT（例如 "0,1200,2500"）。"""
    if not offsets:
        return None
    try:
        return ",".join(str(int(value)) for value in offsets)
    except (TypeError, ValueError):
        return None


def unpack_pages(text):
    if not text:
        return []
    try:
        return [int(part) for part in str(text).split(",") if part != ""]
    except ValueError:
        return []
