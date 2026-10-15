"""OCR：把图片型 PDF 与图片里的文字抽出来（tesseract）。

这里的每一步都是被实测数据逼出来的（本机 aarch64 / FT-2000/4）：

* **直接抽内嵌图，不渲染**：扫描页往往就是一张内嵌 JPEG。用 `pdftoppm` 渲染成
  150~300dpi 位图实测要 **20~30 秒/页**，而 `pdfimages -j` 把同一张图原样取出来只要
  **0.01 秒/页**（差三个数量级）。只有抽不到（多图页、矢量页、特殊编码）才回退渲染。
* **限制像素数**：识别耗时几乎与像素数成正比，送进去之前按 `ocr_max_pixels` 等比缩小。
* **横躺的页要转正**：扫描件方向不对时 tesseract 只吐乱码；识别结果过短就用 OSD 判方向、
  旋转后重试一次。
* **页与页并行**：同一份 PDF 的页互不依赖，用 `ocr_workers` 个并发（全局闸门，
  避免"4 个文件 × 3 页"把 4 核机器压死）。
* **逐页缓存**：识别结果按 (路径, mtime, 页码, 语言, dpi) 存 `~/.cache/docseek/ocrcache`，
  检索时的原文校验和预览都会重新取正文，没有缓存就要重认一遍。

页数上限 `ocr_max_pages` 兜底，超出的页跳过并在 warning 里说明。
"""

import hashlib
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from .base import ExtractError, clean_text

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:                                 # pragma: no cover
    Image = None
    HAVE_PIL = False

TESSERACT = shutil.which("tesseract")
PDFTOPPM = shutil.which("pdftoppm")
PDFIMAGES = shutil.which("pdfimages")

DEFAULT_LANGS = "chi_sim+eng"
DEFAULT_TIMEOUT = 300
MIN_OK_CHARS = 20           # 少于这么多字就怀疑方向不对，试着转正重认
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".ppm", ".pgm", ".pbm", ".tif",
                  ".tiff", ".bmp", ".webp")

_langs_cache = None
_gate = None
_gate_size = 0
_gate_lock = threading.Lock()


def _tess_env():
    """给 tesseract 的环境变量。

    **务必限制成单线程**：tesseract 4.1 的 OpenMP 在本机（aarch64 / 4 核）是负优化——
    同一张 4M 像素的页图，默认 4 线程要 **44.8 s**，`OMP_THREAD_LIMIT=1` 只要 **5.3 s**
    （8.5 倍，输出逐字相同）。吞吐靠我们自己的多进程并行（`ocr_workers`）来拿，
    即"每进程 1 线程 × 3 个进程"，而不是让单个进程开 4 线程。
    """
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    return env


def available():
    """OCR 是否可用（tesseract 存在）。"""
    return bool(TESSERACT)


def pdf_available():
    """扫描页取图是否可用（pdftoppm；pdfimages 只是加速路径）。"""
    return bool(PDFTOPPM)


def _concurrency(workers):
    """全局并发闸门：同一时刻最多跑这么多个 tesseract。"""
    global _gate, _gate_size
    workers = max(1, int(workers or 1))
    with _gate_lock:
        if _gate is None or _gate_size != workers:
            _gate = threading.BoundedSemaphore(workers)
            _gate_size = workers
        return _gate


# ---------------------------------------------------------------- 语言

def languages(force=False):
    """已安装的 tesseract 语言包列表。"""
    global _langs_cache
    if _langs_cache is not None and not force:
        return _langs_cache
    if not TESSERACT:
        _langs_cache = []
        return _langs_cache
    try:
        proc = subprocess.run([TESSERACT, "--list-langs"], timeout=30,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        _langs_cache = [line.strip() for line in lines[1:] if line.strip()]
    except (OSError, subprocess.SubprocessError):
        _langs_cache = []
    return _langs_cache


def resolve_langs(requested=None):
    """把配置里的语言串收敛成"本机真实可用"的组合，缺哪个去掉哪个。"""
    want = [item for item in
            (requested or DEFAULT_LANGS).replace(",", "+").split("+") if item]
    have = set(languages())
    if not have:
        return requested or DEFAULT_LANGS
    picked = [item for item in want if item in have]
    if not picked:
        picked = ["eng"] if "eng" in have else sorted(have)[:1]
    return "+".join(picked)


# ---------------------------------------------------------------- 识别

def ocr_image(path, langs=None, timeout=DEFAULT_TIMEOUT, psm=None):
    """识别一张图片，返回文本（失败抛 ExtractError）。"""
    if not TESSERACT:
        raise ExtractError("未找到 tesseract，无法 OCR")
    cmd = [TESSERACT, str(path), "stdout", "-l", resolve_langs(langs)]
    if psm:
        cmd += ["--psm", str(psm)]
    try:
        proc = subprocess.run(cmd, timeout=timeout, env=_tess_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise ExtractError("OCR 超时（%ss）" % timeout)
    except OSError as exc:
        raise ExtractError("无法调用 tesseract：%s" % exc)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise ExtractError("tesseract 失败：%s" % (message[:200] or "未知错误"))
    return clean_text(proc.stdout.decode("utf-8", "replace"))


def ocr_image_robust(path, langs=None, timeout=DEFAULT_TIMEOUT, work_dir=None,
                     psm=None):
    """识别一张图；tesseract/leptonica 打不开的格式先转 PNG 再试一次。

    实测有些 BMP（144x175x32）会被 leptonica 拒读——
    `Error in pixReadMemBmp: invalid imagebytes = 100802; not equal to 100800`，
    而 PIL 能正常打开。这类图不该算"识别失败"。
    """
    try:
        return ocr_image(path, langs, timeout=timeout, psm=psm)
    except ExtractError:
        if not HAVE_PIL:
            raise
        own_dir = work_dir is None
        work = work_dir or config.temp_dir("ocr-")
        try:
            stem = os.path.basename(str(path)).rsplit(".", 1)[0]
            png = convert_image(str(path), os.path.join(str(work),
                                                        "conv-%s.png" % stem))
            if not png:
                raise
            return ocr_image(png, langs, timeout=timeout, psm=psm)
        finally:
            if own_dir:
                config.cleanup_temp(work)


def detect_rotation(path, timeout=120):
    """用 OSD 判断图片需要旋转的角度（返回 90/180/270；判断不了返回 None）。"""
    if not TESSERACT:
        return None
    try:
        proc = subprocess.run([TESSERACT, str(path), "stdout", "--psm", "0",
                               "-l", "osd"], timeout=timeout, env=_tess_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace")
    match = re.search(r"Rotate:\s*(\d+)", text)
    if not match:
        return None
    angle = int(match.group(1)) % 360
    return angle or None


# ---------------------------------------------------------------- 取图

def _new_files(out_dir, prefix, before):
    produced = []
    for name in sorted(os.listdir(str(out_dir))):
        if name.startswith(prefix) and name not in before:
            produced.append(os.path.join(str(out_dir), name))
    return produced


def extract_page_image(pdf, page, out_dir):
    """直接抽取该页内嵌的图片（pdfimages）。不是"恰好一张图"就返回 None。

    这是扫描 PDF 的极快路径：实测 0.01 秒/页，而渲染同一页要 20+ 秒。
    """
    if not PDFIMAGES:
        return None
    prefix = "x%05d" % page
    before = set(os.listdir(str(out_dir)))
    cmd = [PDFIMAGES, "-j", "-f", str(page), "-l", str(page),
           str(pdf), os.path.join(str(out_dir), prefix)]
    try:
        subprocess.run(cmd, timeout=120, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    produced = [p for p in _new_files(out_dir, prefix, before)
                if os.path.isfile(p)]
    if len(produced) != 1:
        return None
    path = produced[0]
    if path.lower().endswith(IMAGE_SUFFIXES):
        return path
    # 抽出来的是 jp2/jb2 之类 tesseract 不认的格式：能转就转，转不了交给渲染
    return convert_image(path, os.path.join(str(out_dir), prefix + "-conv.png"))


def convert_image(src, dest):
    """用 PIL 转成 PNG。失败返回 None。"""
    if not HAVE_PIL:
        return None
    try:
        with Image.open(src) as image:
            image.convert("RGB").save(dest, "PNG")
        return dest
    except Exception:                                # noqa: BLE001
        return None


def render_page(pdf, page, dpi, out_dir, max_pixels=None):
    """用 pdftoppm 渲染一页（回退路径）。

    `-scale-to` 按像素预算折算长边（A4 长宽比约 1.414），避免"先画 20M 像素的巨图
    再缩小"——渲染本身就要 20~30 秒/页。
    """
    budget = int(max_pixels or config.DEFAULTS["ocr_max_pixels"])
    max_side = max(800, int((budget / 1.414) ** 0.5))
    if not PDFTOPPM:
        raise ExtractError("未找到 pdftoppm，无法渲染扫描页")
    prefix = "p%05d" % page
    cmd = [PDFTOPPM, "-f", str(page), "-l", str(page), "-r", str(int(dpi)),
           "-scale-to", str(int(max_side)), "-png", str(pdf),
           os.path.join(str(out_dir), prefix)]
    try:
        subprocess.run(cmd, timeout=600, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    for name in sorted(os.listdir(str(out_dir))):
        if name.startswith(prefix) and name.lower().endswith(".png"):
            return os.path.join(str(out_dir), name)
    return None


def shrink_image(path, max_pixels, out_dir):
    """像素数超上限就等比缩小（识别耗时几乎与像素数成正比）。"""
    if not HAVE_PIL or not max_pixels:
        return path
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width * height <= max_pixels:
                return path
            scale = (float(max_pixels) / (width * height)) ** 0.5
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            dest = os.path.join(str(out_dir), "small-%s.png"
                                % os.path.basename(path).rsplit(".", 1)[0])
            image.convert("RGB").resize(size, Image.LANCZOS).save(dest, "PNG")
            return dest
    except Exception:                                # noqa: BLE001
        return path


def rotate_image(path, angle, out_dir):
    """按 OSD 给出的角度转正（PIL 的 rotate 是逆时针，这里取反）。"""
    if not HAVE_PIL or not angle:
        return None
    try:
        with Image.open(path) as image:
            dest = os.path.join(str(out_dir), "rot-%s.png"
                                % os.path.basename(path).rsplit(".", 1)[0])
            image.convert("RGB").rotate(-int(angle), expand=True).save(dest, "PNG")
            return dest
    except Exception:                                # noqa: BLE001
        return None


# ---------------------------------------------------------------- 缓存

def _cache_file(src, page, langs, dpi, max_pixels=None):
    """缓存键 = 文件指纹 + 页码 + 语言 + dpi + **像素上限**。

    带上 max_pixels 很关键：改了识别精度就必须重认，否则会一直吃旧的（低质量）结果。
    """
    try:
        stat = os.stat(src)
        stamp = "%s|%s|%s" % (os.path.abspath(src), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = os.path.abspath(src)
    if max_pixels is None:
        max_pixels = config.DEFAULTS["ocr_max_pixels"]
    key = "%s|%s|%s|%s|%s" % (stamp, page, langs, dpi, max_pixels)
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return config.OCR_CACHE / (digest[:24] + ".txt")


def _cache_read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _cache_write(path, text):
    try:
        config.ensure_dirs()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- 单页 OCR

def _recognize(img_path, langs, timeout, work_dir, retry_rotate=True):
    """识别一张页图；结果太短就怀疑方向不对，转正重试一次。"""
    text = ocr_image_robust(img_path, langs, timeout=timeout, work_dir=work_dir)
    if not retry_rotate or len(text.strip()) >= MIN_OK_CHARS:
        return text
    angle = detect_rotation(img_path)
    if not angle:
        return text
    rotated = rotate_image(img_path, angle, work_dir)
    if not rotated:
        return text
    try:
        retried = ocr_image_robust(rotated, langs, timeout=timeout,
                                   work_dir=work_dir)
    except ExtractError:
        return text
    return retried if len(retried.strip()) > len(text.strip()) else text


def ocr_pdf_page(pdf, page, langs=None, dpi=None, use_cache=True,
                 timeout=DEFAULT_TIMEOUT, options=None):
    """OCR 一页 PDF，带磁盘缓存。失败返回空串（调用方按"没识别到"处理）。"""
    options = options or {}
    dpi = int(dpi or options.get("ocr_dpi", config.DEFAULTS["ocr_dpi"]))
    langs = resolve_langs(langs or options.get("ocr_langs"))
    source = options.get("ocr_page_source", config.DEFAULTS["ocr_page_source"])
    max_pixels = int(options.get("ocr_max_pixels",
                                 config.DEFAULTS["ocr_max_pixels"]))
    retry_rotate = bool(options.get("ocr_retry_rotate",
                                    config.DEFAULTS["ocr_retry_rotate"]))

    cache_path = _cache_file(pdf, page, langs, dpi, max_pixels)
    if use_cache:
        cached = _cache_read(cache_path)
        if cached is not None:
            return cached

    work_dir = config.temp_dir("ocr-")
    try:
        image = None
        if source != "render":
            image = extract_page_image(pdf, page, work_dir)
        if image is None:
            if not PDFTOPPM:
                raise ExtractError("未找到 pdftoppm，无法渲染扫描页")
            image = render_page(pdf, page, dpi, work_dir, max_pixels)
        if not image:
            return ""
        image = shrink_image(image, max_pixels, work_dir)
        text = _recognize(image, langs, timeout, work_dir, retry_rotate)
    except ExtractError:
        return ""
    finally:
        config.cleanup_temp(work_dir)

    if use_cache:
        _cache_write(cache_path, text)
    return text


def fill_empty_pages(pdf, pages, options=None, on_page=None):
    """对"没有文本层"的页做 OCR，返回 (新的页文本列表, 已识别页数, 跳过页数)。

    pages 是 pdftotext 分出来的每页文本（可能为空串）。页与页之间并行，
    并发数由 `ocr_workers` 控制（全局闸门，避免多个文件各自再乘一份）。
    """
    options = options or {}
    if not (available() and pdf_available()):
        return pages, 0, 0
    if not options.get("ocr_enabled", config.DEFAULTS["ocr_enabled"]):
        return pages, 0, 0

    min_chars = int(options.get("ocr_min_page_chars",
                                config.DEFAULTS["ocr_min_page_chars"]))
    max_pages = int(options.get("ocr_max_pages",
                                config.DEFAULTS["ocr_max_pages"]))
    workers = max(1, int(options.get("ocr_workers",
                                     config.DEFAULTS["ocr_workers"])))

    targets = [index for index, text in enumerate(pages)
               if len(text.strip()) < min_chars]
    if not targets:
        return pages, 0, 0
    chosen, skipped = targets[:max_pages], targets[max_pages:]

    result = list(pages)
    done = 0

    def work(index):
        gate = _concurrency(workers)
        with gate:                       # 全局限制同时运行的 tesseract 数量
            return index, ocr_pdf_page(pdf, index + 1, options=options)

    if len(chosen) == 1 or workers == 1:
        for index in chosen:
            _index, text = work(index)
            if text.strip():
                result[index] = text
                done += 1
            if on_page:
                _notify(on_page, index + 1, len(pages))
        return result, done, len(skipped)

    with ThreadPoolExecutor(max_workers=min(workers, len(chosen))) as pool:
        futures = [pool.submit(work, index) for index in chosen]
        for future in as_completed(futures):
            try:
                index, text = future.result()
            except Exception:                    # noqa: BLE001 单页失败不影响整份
                continue
            if text.strip():
                result[index] = text
                done += 1
            if on_page:
                _notify(on_page, index + 1, len(pages))
    return result, done, len(skipped)


def _notify(callback, page, total):
    try:
        callback(page, total)
    except Exception:                            # noqa: BLE001 回调不影响主流程
        pass


def ocr_pdf(pdf, options=None):
    """整本 PDF 直接 OCR（不先看有没有文本层），返回 (文本, 页偏移)。"""
    options = options or {}
    if not pdf_available():
        raise ExtractError("未找到 pdftoppm，无法渲染 PDF")
    if not available():
        raise ExtractError("未找到 tesseract，无法 OCR")
    max_pages = int(options.get("ocr_max_pages",
                                config.DEFAULTS["ocr_max_pages"]))
    pages = []
    offsets = []
    position = 0
    for page in range(1, max_pages + 1):
        text = ocr_pdf_page(pdf, page, options=options)
        if not text.strip() and page > 1:
            break
        offsets.append(position)
        pages.append(text)
        position += len(text) + 1
    return "\n".join(pages), offsets


def clear_page_cache(pdf, options=None, max_pages=500):
    """清掉某份 PDF 的逐页 OCR 缓存（改了精度/语言后要重认时用）。返回清除页数。"""
    options = options or {}
    langs = resolve_langs(options.get("ocr_langs"))
    dpi = int(options.get("ocr_dpi", config.DEFAULTS["ocr_dpi"]))
    max_pixels = int(options.get("ocr_max_pixels",
                                 config.DEFAULTS["ocr_max_pixels"]))
    cleared = 0
    for page in range(1, max_pages + 1):
        path = _cache_file(pdf, page, langs, dpi, max_pixels)
        if path.exists():
            try:
                path.unlink()
                cleared += 1
            except OSError:
                pass
    return cleared


def cache_stats():
    """OCR 缓存现状：(文件数, 总字节)。"""
    files = 0
    size = 0
    try:
        for name in os.listdir(str(config.OCR_CACHE)):
            path = config.OCR_CACHE / name
            if path.is_file():
                files += 1
                size += path.stat().st_size
    except OSError:
        pass
    return files, size
