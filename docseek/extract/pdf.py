"""PDF 抽取：poppler 的 pdftotext；抽不出文字的页（图片型 PDF）自动走 OCR。

按 \\f 分页并在每页内先做 clean_text，再拼接，这样 page_offsets 与最终 text
严格对应，预览层可用它定位到具体页。
"""

import shutil
import subprocess
from pathlib import Path

from .. import config
from . import ocr
from .base import Extracted, ExtractError, clean_text

PDFTOTEXT = shutil.which("pdftotext")


def available():
    return bool(PDFTOTEXT)


def extract(path, max_bytes=None, options=None):
    path = Path(path)
    options = options or {}
    if not PDFTOTEXT:
        raise ExtractError("未找到 pdftotext（poppler-utils），无法解析 PDF")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractError("无法读取文件属性：%s" % exc)
    if max_bytes and size > max_bytes:
        raise ExtractError("文件过大（%.1f MB），已跳过" % (size / 1048576.0))

    # 不加 -q：否则语法错误被吞掉，失败原因只剩"未知错误"（踩过）
    cmd = [PDFTOTEXT, "-layout", "-enc", "UTF-8", str(path), "-"]
    try:
        proc = subprocess.run(cmd, timeout=60, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise ExtractError("PDF 解析超时")
    except OSError as exc:
        raise ExtractError("无法调用 pdftotext：%s" % exc)

    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        low = msg.lower()
        if "password" in low or "encrypted" in low or "incorrect" in low:
            raise ExtractError("PDF 已加密，需要密码")
        if "may not be a pdf" in low or "trailer" in low or "xref" in low:
            raise ExtractError("文件不是有效的 PDF（可能损坏或被截断）")
        raise ExtractError("pdftotext 失败：%s" % (msg[:200] or "未知错误"))

    raw = proc.stdout.decode("utf-8", "replace")
    pages = raw.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()

    # 没有文本层的页（图片型/扫描页）交给 OCR 补抽
    pages, ocr_pages, ocr_skipped = ocr.fill_empty_pages(path, pages, options)
    notes = []
    if ocr_pages:
        notes.append("OCR 识别 %d 页（%s）"
                     % (ocr_pages, ocr.resolve_langs(options.get("ocr_langs"))))
    if ocr_skipped:
        notes.append("另有 %d 页超出上限未识别" % ocr_skipped)

    max_chars = int(config.DEFAULTS["max_text_chars"])
    cleaned = [clean_text(page) for page in pages]

    chunks = []
    offsets = []
    pos = 0
    for page in cleaned:
        offsets.append(pos)
        chunks.append(page)
        pos += len(page) + 1

    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars]
        offsets = [o for o in offsets if o < max_chars]

    if not text.strip():
        notes.append("未提取到文本（可能是扫描件或纯图片 PDF）")
        if not ocr.available():
            notes.append("未安装 tesseract，无法 OCR")
        elif not ocr.pdf_available():
            notes.append("未安装 pdftoppm，无法渲染扫描页")
    return Extracted(text=text, kind="pdf", page_offsets=offsets,
                     warning="；".join(notes))
