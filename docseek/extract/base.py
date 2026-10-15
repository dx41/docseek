"""抽取层的公共类型与工具（放在独立模块以避免循环导入）。"""

import re
from dataclasses import dataclass, field


class ExtractError(Exception):
    """抽取失败（不支持、损坏、加密、工具缺失等）。"""


@dataclass
class Extracted:
    """抽取结果。

    text          正文文本（已做换行归一化）
    kind          来源类型：text/docx/xlsx/pptx/pdf/doc/legacy/odf ...
    page_offsets  各"页/幻灯片/工作表"起始处在 text 中的字符偏移（可为空）
    warning       非致命提示（例如"疑似扫描件，无文本层"）
    """

    text: str
    kind: str = "text"
    page_offsets: list = field(default_factory=list)
    warning: str = ""

    def page_of(self, offset):
        """返回字符偏移所在的页号（1 起；无分页信息时返回 None）。"""
        if not self.page_offsets:
            return None
        page = 1
        for i, start in enumerate(self.page_offsets, start=1):
            if offset >= start:
                page = i
            else:
                break
        return page


_CTRL_RE = re.compile(r"[\x00-\x08\x0e-\x1f]")
_BLANK_RE = re.compile(r"\n{3,}")


def clean_text(text, max_chars=4000000):
    """归一化换行、剔除控制字符、压缩连续空行、截断超长正文。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub("", text)
    text = _BLANK_RE.sub("\n\n", text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text.strip("\n")
