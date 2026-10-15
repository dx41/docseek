"""OOXML / ODF / EPUB 抽取：只用 zipfile + ElementTree，不依赖外部程序。

实测 docx 抽取约 7 ms/份（本机 aarch64）。元素匹配一律按"本地名"（去掉命名空间）
进行，可同时兼容 OOXML 与 ODF。

分页/分表/分幻灯片的字符偏移在"每单元先 clean_text、再拼接"的前提下计算，
保证偏移与最终返回的 text 严格对应（若最后再整体 clean，偏移就会错位）。
"""

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import config
from .base import Extracted, ExtractError, clean_text

# ---------------------------------------------------------------- 通用工具


def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _walk(data, rules):
    """按 rules 把 XML 流拍平成文本。

    rules: {本地名: 'text' | 'newline' | 'tab'}
    遇到换行/制表符类元素时，若它自身直接持有文本（例如 ODF 的 text:p），
    一并取出，避免丢失。
    """
    out = []
    try:
        for event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
            action = rules.get(_local(elem.tag))
            if action == "text":
                if elem.text:
                    out.append(elem.text)
            elif action == "newline":
                if elem.text and elem.text.strip():
                    out.append(elem.text)
                out.append("\n")
            elif action == "tab":
                if elem.text and elem.text.strip():
                    out.append(elem.text)
                out.append("\t")
            elem.clear()
    except ET.ParseError as exc:
        raise ExtractError("XML 解析失败：%s" % exc)
    return "".join(out)


def _read(zf, name):
    try:
        return zf.read(name)
    except KeyError:
        return None


def _parts(zf, pattern):
    """按名称里的数字排序，返回匹配的 zip 成员名。"""
    rx = re.compile(pattern)
    found = []
    for name in zf.namelist():
        match = rx.match(name)
        if match:
            try:
                key = int(match.group(1))
            except (IndexError, ValueError):
                key = 0
            found.append((key, name))
    return [name for _, name in sorted(found)]


def _join_pages(pages):
    """拼接各单元文本，返回 (text, 每单元起始偏移)。"""
    chunks = []
    offsets = []
    pos = 0
    for page in pages:
        offsets.append(pos)
        chunks.append(page)
        pos += len(page) + 1        # +1 = join 用的换行
    return "\n".join(chunks), offsets


# ---------------------------------------------------------------- Word

_WORD_RULES = {"t": "text", "tab": "tab", "br": "newline", "cr": "newline",
               "p": "newline", "tc": "tab", "tr": "newline"}


def _extract_docx(zf):
    main = _read(zf, "word/document.xml")
    if main is None:
        raise ExtractError("不是有效的 docx（缺少 word/document.xml）")
    chunks = [_walk(main, _WORD_RULES)]
    extra = ["word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"]
    extra += _parts(zf, r"word/(?:header|footer)\d+\.xml")
    for name in extra:
        data = _read(zf, name)
        if not data:
            continue
        text = _walk(data, _WORD_RULES)
        if text.strip():
            chunks.append("\n" + text)
    return Extracted(text=clean_text("".join(chunks)), kind="docx")


# ---------------------------------------------------------------- Excel

def _shared_strings(zf):
    data = _read(zf, "xl/sharedStrings.xml")
    if not data:
        return []
    strings = []
    try:
        for event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
            if _local(elem.tag) == "si":
                strings.append("".join(e.text or "" for e in elem.iter()
                                       if _local(e.tag) == "t"))
                elem.clear()
    except ET.ParseError:
        pass
    return strings


def _cell_value(cell, shared):
    ctype = cell.get("t")
    if ctype == "inlineStr":
        return "".join(e.text or "" for e in cell.iter()
                       if _local(e.tag) == "t")
    value = None
    for child in cell:
        if _local(child.tag) == "v":
            value = child.text or ""
            break
    if value is None:
        return ""
    if ctype == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "FALSE" if value.strip() in ("0", "") else "TRUE"
    return value


def _extract_xlsx(zf):
    shared = _shared_strings(zf)
    sheets = _parts(zf, r"xl/worksheets/sheet(\d+)\.xml")
    if not sheets:
        raise ExtractError("不是有效的 xlsx（缺少工作表）")
    pages = []
    for name in sheets:
        data = _read(zf, name)
        if not data:
            continue
        rows = []
        try:
            for event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
                tag = _local(elem.tag)
                if tag == "row":
                    cells = [_cell_value(c, shared) for c in elem
                             if _local(c.tag) == "c"]
                    rows.append("\t".join(cells))
                    elem.clear()
                elif tag == "sheetData":
                    elem.clear()
        except ET.ParseError:
            continue
        pages.append(clean_text("\n".join(rows)))
    text, offsets = _join_pages(pages)
    return Extracted(text=text, kind="xlsx", page_offsets=offsets)


# ---------------------------------------------------------------- PowerPoint

_PPTX_RULES = {"t": "text", "br": "newline", "p": "newline", "tab": "tab"}


def _extract_pptx(zf):
    slides = _parts(zf, r"ppt/slides/slide(\d+)\.xml")
    if not slides:
        raise ExtractError("不是有效的 pptx（缺少幻灯片）")
    pages = []
    for name in slides:
        data = _read(zf, name)
        if not data:
            continue
        pages.append(clean_text(_walk(data, _PPTX_RULES)))
    text, offsets = _join_pages(pages)
    return Extracted(text=text, kind="pptx", page_offsets=offsets)


# ---------------------------------------------------------------- ODF

_ODF_RULES = {"p": "newline", "h": "newline", "tab": "tab",
              "table-cell": "tab", "table-row": "newline",
              "line-break": "newline", "s": "text", "span": "text",
              "a": "text", "title": "newline"}


def _extract_odf(zf, kind):
    data = _read(zf, "content.xml")
    if data is None:
        raise ExtractError("不是有效的 ODF 文件（缺少 content.xml）")
    out = []
    try:
        for event, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
            action = _ODF_RULES.get(_local(elem.tag))
            if action == "text":
                if elem.text:
                    out.append(elem.text)
            elif action == "newline":
                if elem.text and elem.text.strip():
                    out.append(elem.text)
                out.append("\n")
            elif action == "tab":
                if elem.text and elem.text.strip():
                    out.append(elem.text)
                out.append("\t")
            if elem.tail:
                out.append(elem.tail)
            elem.clear()
    except ET.ParseError as exc:
        raise ExtractError("ODF XML 解析失败：%s" % exc)
    return Extracted(text=clean_text("".join(out)), kind=kind)


# ---------------------------------------------------------------- EPUB

def _extract_epub(zf):
    import html as _html
    names = sorted(n for n in zf.namelist()
                   if n.lower().endswith((".xhtml", ".html", ".htm")))
    if not names:
        raise ExtractError("不是有效的 epub（没有 HTML 内容）")
    pages = []
    for name in names:
        data = _read(zf, name)
        if not data:
            continue
        text = data.decode("utf-8", "replace")
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        pages.append(clean_text(_html.unescape(text)))
    text, offsets = _join_pages(pages)
    return Extracted(text=text, kind="epub", page_offsets=offsets)


# ---------------------------------------------------------------- 入口

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
        with zipfile.ZipFile(path) as zf:
            if ext in ("docx", "docm", "dotx", "dotm"):
                return _extract_docx(zf)
            if ext in ("xlsx", "xlsm", "xltx", "xltm"):
                return _extract_xlsx(zf)
            if ext in ("pptx", "pptm", "ppsx", "potx"):
                return _extract_pptx(zf)
            if ext == "epub":
                return _extract_epub(zf)
            return _extract_odf(zf, ext)
    except zipfile.BadZipFile as exc:
        raise ExtractError("压缩包损坏或不是 OOXML 文件：%s" % exc)
    except OSError as exc:
        raise ExtractError("读取失败：%s" % exc)
