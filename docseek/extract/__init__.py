"""文本抽取统一入口。

后端模块（plain / ooxml / pdf / legacy / image）都实现同一签名：

    extract(path, max_bytes=None, options=None) -> Extracted     # 失败抛 ExtractError

`options` 是配置字典（`config.load()` 的结果），目前主要给 OCR 用；
不认识它的后端直接忽略即可。新增格式时把扩展名加进对应集合即可。
"""

import importlib
import os
from pathlib import Path

from .. import config
from .base import Extracted, ExtractError, clean_text  # noqa: F401  (对外转出)

__all__ = ["Extracted", "ExtractError", "clean_text", "extract",
           "is_indexable", "supported_extensions", "handler_for"]

# 纯文本与源码
PLAIN_EXTS = {
    "txt", "text", "log", "md", "markdown", "rst", "csv", "tsv", "tab",
    "ini", "cfg", "conf", "cnf", "json", "jsonl", "ndjson", "xml", "yaml",
    "yml", "toml", "properties", "env", "list", "html", "htm", "xhtml",
    "css", "scss", "less", "js", "mjs", "cjs", "ts", "jsx", "tsx", "vue",
    "svelte", "py", "pyw", "java", "c", "h", "cpp", "cxx", "hpp", "cc",
    "cs", "go", "rs", "rb", "php", "pl", "pm", "lua", "sh", "bash", "zsh",
    "ksh", "bat", "cmd", "ps1", "sql", "swift", "kt", "kts", "scala", "r",
    "asm", "s", "tex", "bib", "desktop", "service", "patch", "diff",
    "gitignore", "gitattributes", "editorconfig", "srt", "ass", "vtt",
    "tcl", "vim", "el", "hs", "erl", "ex", "exs", "dart", "proto",
    "thrift", "gradle", "sln", "csproj", "vcxproj", "make", "mk", "cmake",
    "lrc", "po", "pot", "pem", "crt", "key", "asc", "sig",
}

# 无扩展名但通常是文本的文件名
PLAIN_NAMES = {
    "makefile", "dockerfile", "readme", "license", "licence", "changelog",
    "authors", "copying", "notice", "install", "todo", "news", "version",
    "kconfig", "pkgbuild", "rakefile", "gemfile", "procfile", "hosts",
    "fstab", "passwd", "group", "profile", "bashrc", "zshrc", "vimrc",
    "gitconfig", "npmrc", "yarnrc", "editorconfig", "gitignore",
}

# OOXML / ODF / EPUB：本质都是 zip + XML
OOXML_EXTS = {
    "docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm",
    "pptx", "pptm", "ppsx", "potx", "odt", "ods", "odp", "odg", "epub",
}

PDF_EXTS = {"pdf"}

# 图片：只能靠 OCR（默认不索引，ocr_images 打开后才纳入）
IMAGE_EXTS = {
    "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp", "pnm", "pgm", "pbm",
}

# 旧式二进制格式（走 catdoc/xlrd/wps2text，必要时 LibreOffice 兜底）
LEGACY_EXTS = {
    "doc", "dot", "xls", "xlt", "ppt", "pot", "pps", "rtf", "wps", "wpt",
    "et", "ett", "dps", "dpt", "wdb", "mht", "mhtml", "pub", "vsd", "sxw",
    "sxc", "sxi", "123", "wk1", "wk3", "wq1", "wq2", "abw", "hwp",
}

_HANDLERS = {
    "plain": "docseek.extract.plain",
    "ooxml": "docseek.extract.ooxml",
    "pdf": "docseek.extract.pdf",
    "legacy": "docseek.extract.legacy",
    "image": "docseek.extract.image",
}

_CACHE = {}


def file_ext(path):
    """取小写扩展名（无扩展名返回空串）。"""
    name = os.path.basename(str(path))
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def handler_for(ext):
    if ext in PLAIN_EXTS:
        return "plain"
    if ext in OOXML_EXTS:
        return "ooxml"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in LEGACY_EXTS:
        return "legacy"
    return None


def supported_extensions():
    return PLAIN_EXTS | OOXML_EXTS | PDF_EXTS | LEGACY_EXTS | IMAGE_EXTS


def is_indexable(path, options=None):
    """粗判是否值得抽取（不读文件内容）。

    图片只在"开了 OCR 且允许索引图片"时才算可索引——照片目录逐张识别代价太大。
    """
    path = Path(path)
    name = path.name
    opts = options or {}
    # Office 异常退出留下的临时锁文件（~$xxx.docx），从来不是有效文档
    if name.startswith("~$") or name.startswith(".~lock."):
        return False
    if name.startswith(".") and name not in (".gitignore", ".editorconfig"):
        # 勾了"包含隐藏文件"就要真的收进来（.gitignore/.editorconfig 本来就值得索引）
        if not opts.get("include_hidden"):
            return False
    ext = file_ext(name)
    if ext:
        handler = handler_for(ext)
        if handler == "image":
            return (bool(opts.get("ocr_enabled", config.DEFAULTS["ocr_enabled"]))
                    and bool(opts.get("ocr_images", config.DEFAULTS["ocr_images"])))
        if ext in config.SKIP_EXTENSIONS:
            return False
        return handler is not None
    return name.lower() in PLAIN_NAMES


def _module(handler):
    mod = _CACHE.get(handler)
    if mod is None:
        mod = importlib.import_module(_HANDLERS[handler])
        _CACHE[handler] = mod
    return mod


# 同一扩展名可能分属不同格式，按文件头魔数消解歧义。
# 目前只有 .pot：gettext 翻译模板（纯文本）与 PowerPoint 模板（OLE 二进制）同名。
AMBIGUOUS_EXTS = (PLAIN_EXTS & LEGACY_EXTS) | (PLAIN_EXTS & OOXML_EXTS)

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def _sniff_handler(path):
    """读文件头判断真实类型；判断不了返回 None。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if head.startswith(_OLE_MAGIC) or head.startswith(_ZIP_MAGIC):
        return "legacy"
    if head[:5] in (b"msgid", b"#: ", b"#  ", b"#\n", b"#\r"):
        return "plain"
    return None


def extract(path, max_bytes=None, options=None):
    """按扩展名分发到具体后端（歧义扩展名先做内容嗅探）。"""
    path = Path(path)
    ext = file_ext(path)
    handler = handler_for(ext)
    if ext in AMBIGUOUS_EXTS:
        sniffed = _sniff_handler(path)
        if sniffed is not None:
            handler = sniffed
    if handler is None and not ext and path.name.lower() in PLAIN_NAMES:
        handler = "plain"
    if handler is None:
        raise ExtractError("不支持的格式：.%s" % ext if ext else "无扩展名文件")
    if max_bytes is None:
        max_bytes = int(config.DEFAULTS["max_file_mb"]) * 1024 * 1024
    return _module(handler).extract(path, max_bytes=max_bytes, options=options)
