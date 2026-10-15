"""纯文本 / 源码抽取：编码探测 + 二进制嗅探。"""

from pathlib import Path

from .. import config
from .base import Extracted, ExtractError, clean_text

# 编码尝试顺序：中文环境优先 UTF-8 与 GB18030
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16")

# 这些扩展名先剥掉标记再入库，避免标签名干扰检索
_MARKUP_EXTS = {"html", "htm", "xhtml"}


def _looks_binary(raw):
    """NUL 字节比例过高即认为是二进制。"""
    if not raw:
        return False
    sample = raw[:8192]
    return sample.count(b"\x00") > max(1, len(sample) // 100)


def _decode(raw):
    # 带 BOM 的 UTF-16 直接按 BOM 处理
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        import chardet  # 本机若装了 chardet 则优先用它的判断
        guess = chardet.detect(raw[:200000]) or {}
        enc = guess.get("encoding")
        if enc and guess.get("confidence", 0) >= 0.6:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _strip_markup(text):
    """极简 HTML 处理：去 script/style、去标签、还原常见实体。"""
    import html as _html
    import re
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _html.unescape(text)


def extract(path, max_bytes=None, options=None):
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractError("无法读取文件属性：%s" % exc)
    if max_bytes and size > max_bytes:
        raise ExtractError("文件过大（%.1f MB），已跳过" % (size / 1048576.0))

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractError("读取失败：%s" % exc)

    if _looks_binary(raw):
        raise ExtractError("疑似二进制文件，已跳过")

    text = _decode(raw)
    if ext in _MARKUP_EXTS:
        text = _strip_markup(text)
    max_chars = int(config.DEFAULTS["max_text_chars"])
    return Extracted(text=clean_text(text, max_chars), kind="text")
