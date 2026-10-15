"""图片抽取：靠 OCR 把图片里的文字取出来（jpg/png/tif/webp…）。

默认**不**索引图片（`ocr_images` 为 false），因为照片目录里绝大多数图片没有文字，
逐张 OCR 会让索引时间爆炸。需要时在设置里打开。
"""

from pathlib import Path

from .. import config
from . import ocr
from .base import Extracted, ExtractError, clean_text


def extract(path, max_bytes=None, options=None):
    path = Path(path)
    options = options or {}
    if not options.get("ocr_enabled", config.DEFAULTS["ocr_enabled"]):
        raise ExtractError("未启用 OCR（设置里可打开）")
    if not ocr.available():
        raise ExtractError("未找到 tesseract，无法识别图片文字")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractError("无法读取文件属性：%s" % exc)
    if max_bytes and size > max_bytes:
        raise ExtractError("文件过大（%.1f MB），已跳过" % (size / 1048576.0))

    langs = ocr.resolve_langs(options.get("ocr_langs"))
    text = ocr.ocr_image_robust(path, langs=langs)
    max_chars = int(config.DEFAULTS["max_text_chars"])
    if not text.strip():
        # 照片里本来就没文字，这不是"抽取失败"，交给上层记成 empty
        return Extracted(text="", kind="image", warning="图片里没有文字")
    return Extracted(text=clean_text(text, max_chars), kind="image",
                     warning="OCR %s" % langs)
