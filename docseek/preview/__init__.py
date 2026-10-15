"""版面还原预览层。

对外只暴露 pagemap 里的几个函数：

    to_pdf(src, cache=True)               # Office/旧格式 → PDF（PDF 原样返回）
    find_page(pdf, needle)                # 命中在第几页
    render_page(src, page=1, dpi=None)    # 渲染某页为 PNG
    render_match(src, needle, dpi=None)   # 上面三步串起来 -> (png, page)
    page_count(pdf)
    clear_cache(older_than_days=None)

所有函数都不抛异常，失败返回 None / 0，由 UI 决定怎么提示。
缓存位置见 config.PDF_CACHE / config.IMG_CACHE，清理时只动这两个目录之下。
"""

from .pagemap import (  # noqa: F401  (对外转出)
    available, clear_cache, find_page, page_count, render_match, render_page,
    to_pdf,
)

__all__ = ["to_pdf", "find_page", "render_page", "render_match",
           "page_count", "clear_cache", "available"]
