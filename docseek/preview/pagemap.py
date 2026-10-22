"""版面还原预览：把命中页渲染成 PNG 图片。

串联流程（UI 一般只需调用 render_match）：

    to_pdf()      Office/旧格式 → PDF（LibreOffice），PDF 直接透传
    find_page()   pdftotext -layout 逐页定位命中页
    render_page() pdftoppm 把该页渲染成 PNG

缓存（都在 docseek 自己的缓存目录内，不碰用户文件）：

    ~/.cache/docseek/pdfcache/<名字>-<sha1>.pdf
    ~/.cache/docseek/imgcache/<名字>-<sha1>.png

sha1 由「绝对路径 + mtime + size」算出（图片再加页码与 dpi），源文件一改即失效。

约定：
* 对外函数一律不抛异常，失败返回 None / 0，是否提示由 UI 决定；
* 外部命令（pdftotext / pdftoppm / pdfinfo / soffice）都带超时，异常一律吞掉；
* 清理只允许发生在 config.PDF_CACHE / config.IMG_CACHE 之下，并且逐条做白名单
  路径校验（clear_cache 就是干这个的，缓存回收必须真正释放磁盘）；
* 自建的缓存临时目录（docseek-pg-* / docseek-lo-* / render-*，都在
  config.CACHE_DIR 之下）用 config.cleanup_temp 直接删掉——那是程序自己的中间
  产物，不是用户文件；以前一律 `gio trash`，结果每预览一次就往用户回收站塞一个
  目录（用户报障）。不在白名单里的路径才回退到 `gio trash`。
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .. import config
from ..extract import lo
from ..extract.base import ExtractError

PDFTOTEXT = shutil.which("pdftotext")
PDFTOPPM = shutil.which("pdftoppm")
PDFINFO = shutil.which("pdfinfo")
GIO = shutil.which("gio")

# 已经是 PDF，直接可用
PDF_EXTS = {"pdf"}

# 交给 LibreOffice 转换的格式（Office / WPS / ODF / RTF）
OFFICE_EXTS = {
    "doc", "docx", "docm", "dot", "dotx", "dotm",
    "xls", "xlsx", "xlsm", "xlt", "xltx", "xltm",
    "ppt", "pptx", "pptm", "pps", "ppsx", "pot", "potx",
    "odt", "ods", "odp", "odg",
    "wps", "wpt", "et", "ett", "dps", "dpt", "rtf",
}

# 外部命令超时（秒）
TIMEOUT_CONVERT = 240
TIMEOUT_LAYOUT = 60
TIMEOUT_RENDER = 120
TIMEOUT_INFO = 60
TIMEOUT_TRASH = 30

# 非缓存渲染的临时目录名（位于 CACHE_DIR 下，每次渲染前清空重来）
SCRATCH_NAME = "preview-tmp"

_SCRATCH_LOCK = threading.Lock()

_SAFE_RE = re.compile(r"[^\w.-]+")
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.M)
_WS_RE = re.compile(r"\s+")


def available():
    """预览所需的外部命令是否齐备（缺 pdftoppm 就只能看文字）。"""
    return {"pdftotext": bool(PDFTOTEXT), "pdftoppm": bool(PDFTOPPM),
            "pdfinfo": bool(PDFINFO), "libreoffice": lo.available()}


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------

def _ensure_cache():
    """确保缓存目录存在，返回是否可用。"""
    try:
        config.ensure_dirs()
        return True
    except OSError:
        return False


def _sha1(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _safe_stem(name):
    """把文件名压成适合放进缓存目录的短前缀（保留中文，便于人眼排查）。"""
    stem = _SAFE_RE.sub("_", str(name)).strip("._")
    return (stem[:48] or "doc")


def _stat_key(path, extra=""):
    """缓存键 = 绝对路径 + mtime + size（+ extra）。取不到 stat 时回退到路径。"""
    try:
        st = path.stat()
        sig = "%s|%d|%d|%s" % (path.resolve(), st.st_mtime_ns, st.st_size, extra)
    except OSError:
        sig = "%s|||%s" % (path, extra)
    return _sha1(sig)


def _cache_path(root, src, extra="", suffix=None):
    """算出缓存文件路径：<缓存目录>/<名字>-<sha1 前 16 位><后缀>。"""
    key = _stat_key(src, extra)
    if suffix is None:
        suffix = src.suffix.lower()
    return Path(root) / ("%s-%s%s" % (_safe_stem(src.stem), key[:16], suffix))


def _run(cmd, timeout):
    """跑外部命令，返回 (returncode, stdout bytes)；任何异常返回 (None, b"")。"""
    try:
        proc = subprocess.run(
            cmd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        return None, b""
    return proc.returncode, proc.stdout or b""


def _trash(path):
    """把 docseek 自建的临时条目送进回收站（失败就原样留着，绝不物理删除）。"""
    if not GIO:
        return False
    try:
        proc = subprocess.run(
            [GIO, "trash", "--", str(path)], timeout=TIMEOUT_TRASH,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _recycle(path):
    """回收自建临时目录：自己的缓存直接删，其它路径才送进回收站。

    用户报障：跑一阵子后回收站里多出一堆 docseek-pg-* / docseek-lo-* 目录。
    这些是预览过程的中间产物，程序自己能重建，本来就不该占用用户的回收站。
    """
    if config.cleanup_temp(path):
        return True
    return _trash(path)


def _is_temp_pdf(path):
    """是否是 LibreOffice 转换临时目录（docseek-lo-*）里的 PDF。"""
    try:
        parent = Path(path).parent
    except (TypeError, ValueError):
        return False
    return parent.name.startswith("docseek-lo-")


def _release_temp_pdf(path):
    """回收 to_pdf(cache=False) 留下的临时 PDF 及其目录（仅限 CACHE_DIR 之下）。"""
    try:
        parent = Path(path).parent
        real_parent = parent.resolve()
        real_cache = Path(config.CACHE_DIR).resolve()
    except (OSError, TypeError, ValueError):
        return False
    if not parent.name.startswith("docseek-lo-"):
        return False
    if not _within(real_parent, real_cache):
        return False
    return _recycle(parent)


def _within(path, real_root):
    """path（解析软链后）是否严格位于 real_root 之下——删除前的白名单校验。"""
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    if p == real_root:
        return False
    try:
        p.relative_to(real_root)
    except ValueError:
        return False
    return True


def _clear_root(root, cutoff):
    """清掉 root 直接子级（不含 root 自身），返回清理条目数。

    仅用于 docseek 自己的缓存目录：逐条校验解析后的真实路径确实在 root 之下，
    软链指向外部的一律跳过。
    """
    removed = 0
    try:
        root = Path(root)
        if not root.is_dir():
            return 0
        real_root = root.resolve()
        entries = sorted(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not _within(entry, real_root):
                continue
            if cutoff is not None and entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _scratch_dir():
    """非缓存渲染的临时目录：清空旧内容后新建一个子目录，避免无限堆积。"""
    root = Path(config.CACHE_DIR) / SCRATCH_NAME
    with _SCRATCH_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        _clear_root(root, None)
        try:
            return Path(tempfile.mkdtemp(prefix="render-", dir=str(root)))
        except OSError:
            return root


def _page_texts(pdf_path, timeout=TIMEOUT_LAYOUT):
    """pdftotext -layout 抽取逐页文本，返回页文本列表；失败返回 None。

    pdftotext 以 \\f 分页，末尾通常多出一个空段，这里去掉。
    """
    if not PDFTOTEXT:
        return None
    _, out = _run(
        [PDFTOTEXT, "-layout", "-enc", "UTF-8", "-q", str(pdf_path), "-"], timeout)
    if not out:
        return None
    text = out.decode("utf-8", "replace")
    pages = text.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages or None


def _flatten(text):
    """去掉全部空白，用于跨行/被 -layout 插空格的命中判断。"""
    return _WS_RE.sub("", text)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def to_pdf(src, cache=True, profile=None):
    """把 src 转成 PDF 并返回路径；失败返回 None（异常不外抛）。

    * .pdf 直接返回自身；
    * Office / 旧格式调 LibreOffice 转换（lo.convert_to_pdf）；
    * cache=True 时把产物放进 config.PDF_CACHE，键含 mtime 与 size，
      源文件改动后自动失效；cache=False 时产物在 CACHE_DIR 下的临时目录里，
      render_page 用完会回收，调用方不必操心。
    """
    try:
        src = Path(src)
    except (TypeError, ValueError):
        return None
    if not src.is_file():
        return None
    ext = src.suffix.lower().lstrip(".")
    if ext in PDF_EXTS:
        return src
    if ext not in OFFICE_EXTS:
        return None
    if not lo.available():
        return None

    _ensure_cache()
    dest = _cache_path(config.PDF_CACHE, src, "", ".pdf") if cache else None
    if dest is not None:
        try:
            if dest.is_file() and dest.stat().st_size > 0:
                return dest
        except OSError:
            pass

    try:
        # 预览用独立 profile：否则索引批量转换时这里要排队（实测 1s → 69s）
        produced = lo.convert_to_pdf(src, timeout=TIMEOUT_CONVERT,
                                     profile=lo.PROFILE_DIR_PREVIEW)
    except ExtractError:
        return None
    except Exception:      # 兜底：LibreOffice 可能抛出任何东西，都不能影响 UI
        return None
    if not produced:
        return None
    produced = Path(produced)
    try:
        if not produced.is_file() or produced.stat().st_size <= 0:
            return None
    except OSError:
        return None

    if dest is None:
        return produced

    try:
        os.replace(str(produced), str(dest))
    except OSError:
        try:
            shutil.move(str(produced), str(dest))
        except (OSError, shutil.Error):
            return produced      # 挪不动就先用临时产物，功能不受影响
    _recycle(produced.parent)    # 回收 LibreOffice 的 docseek-lo-* 临时目录
    return dest


def find_page(pdf_path, needle, ignore_case=True):
    """定位包含 needle 的页号（1 起）；找不到或读取失败返回 None。

    按页顺序逐页判断：先按原样匹配，再退化为「去掉空白后」匹配，这样跨行断开
    的短语、以及 -layout 在中文之间插入的空格都能命中；ignore_case 时统一
    转小写比较（中文不受影响）。
    """
    if not needle:
        return None
    try:
        pdf_path = Path(pdf_path)
    except (TypeError, ValueError):
        return None
    if not pdf_path.is_file():
        return None
    needle = str(needle).strip()
    if not needle:
        return None
    pages = _page_texts(pdf_path)
    if not pages:
        return None

    key = needle.lower() if ignore_case else needle
    flat_key = _flatten(key)
    for idx, text in enumerate(pages, start=1):
        hay = text.lower() if ignore_case else text
        if key in hay:
            return idx
        # 退化匹配：跨行断开的短语、-layout 在中文之间插入的空格
        if flat_key and flat_key in _flatten(hay):
            return idx
    return None


def page_count(pdf_path):
    """PDF 页数；读不到返回 0。"""
    try:
        pdf_path = Path(pdf_path)
    except (TypeError, ValueError):
        return 0
    if not pdf_path.is_file():
        return 0
    if PDFINFO:
        _, out = _run([PDFINFO, str(pdf_path)], TIMEOUT_INFO)
        if out:
            m = _PAGES_RE.search(out.decode("utf-8", "replace"))
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
    # 兜底：数一数 pdftotext 的分页
    pages = _page_texts(pdf_path)
    return len(pages) if pages else 0


def render_page(src, page=1, dpi=None, cache=True):
    """把 PDF（或可转换的 Office 文件）的第 page 页渲染成 PNG，返回图片路径。

    * 缓存键含 pdf 路径、mtime、页码与 dpi；
    * page 越界、文件损坏、缺 pdftoppm 都返回 None；
    * cache=False 时产物放在 CACHE_DIR 的临时目录里，下次渲染会被清掉。
    """
    try:
        pdf_path = Path(src)
    except (TypeError, ValueError):
        return None
    if not pdf_path.is_file():
        return None
    temp_pdf = None
    if pdf_path.suffix.lower().lstrip(".") not in PDF_EXTS:
        # 允许直接丢 Office 文件进来，先转 PDF；不缓存时产物用完就回收
        pdf_path = to_pdf(pdf_path, cache=cache)
        if pdf_path is None:
            return None
        if _is_temp_pdf(pdf_path):
            temp_pdf = pdf_path
    try:
        return _render_pdf_page(pdf_path, page, dpi, cache)
    finally:
        if temp_pdf is not None:
            _release_temp_pdf(temp_pdf)


def _render_pdf_page(pdf_path, page, dpi, cache):
    """真正干活的部分：校验参数 → 查缓存 → pdftoppm → 落缓存。"""
    try:
        page = int(page)
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None

    try:
        dpi = int(dpi or config.DEFAULTS["render_dpi"])
    except (TypeError, ValueError):
        dpi = int(config.DEFAULTS["render_dpi"])
    if dpi < 20:
        dpi = 20
    if not PDFTOPPM:
        return None

    _ensure_cache()
    extra = "|p%d|d%d" % (page, dpi)
    dest = _cache_path(config.IMG_CACHE, pdf_path, extra, ".png") if cache else None
    if dest is not None:
        try:
            if dest.is_file() and dest.stat().st_size > 0:
                return dest
        except OSError:
            pass

    total = page_count(pdf_path)
    if total and page > total:
        return None

    work = _scratch_dir() if cache is False else Path(
        tempfile.mkdtemp(prefix="docseek-pg-", dir=str(config.CACHE_DIR)))
    prefix = work / "page"
    _run(
        [PDFTOPPM, "-f", str(page), "-l", str(page), "-r", str(dpi),
         "-png", str(pdf_path), str(prefix)], TIMEOUT_RENDER)
    try:
        produced = sorted(work.glob("page-*.png"))
    except OSError:
        produced = []
    if not produced:
        _recycle(work)
        return None
    png = produced[0]
    try:
        if png.stat().st_size <= 0:
            _recycle(work)
            return None
    except OSError:
        _recycle(work)
        return None

    if dest is None:
        return png               # 非缓存：留在临时目录给本次展示用
    try:
        os.replace(str(png), str(dest))
    except OSError:
        try:
            shutil.move(str(png), str(dest))
        except (OSError, shutil.Error):
            return png
    _recycle(work)
    return dest


def render_match(src, needle, dpi=None):
    """一步到位：转 PDF → 定位命中页 → 渲染，返回 (png 路径, 页号)。

    任一环节失败返回 (None, 页号或 None)；find_page 找不到时渲染第 1 页。
    """
    try:
        pdf_path = to_pdf(src)
    except Exception:
        pdf_path = None
    if pdf_path is None:
        return (None, None)

    page = None
    try:
        page = find_page(pdf_path, needle)
    except Exception:
        page = None
    if page is None:
        page = 1

    try:
        png = render_page(pdf_path, page=page, dpi=dpi)
    except Exception:
        png = None
    return (png, page)


def clear_cache(older_than_days=None):
    """清理 PDF/图片缓存，返回清理掉的条目数。

    只动 config.PDF_CACHE 与 config.IMG_CACHE 之下的直接子级（逐条白名单校验），
    older_than_days 给定时只清 mtime 更早的条目。
    """
    cutoff = None
    if older_than_days is not None:
        try:
            cutoff = time.time() - max(0.0, float(older_than_days)) * 86400.0
        except (TypeError, ValueError):
            cutoff = None
    total = 0
    for root in (config.PDF_CACHE, config.IMG_CACHE):
        total += _clear_root(root, cutoff)
    return total
