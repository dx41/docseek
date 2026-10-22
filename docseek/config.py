"""配置与路径管理。

配置文件：~/.config/docseek/config.json
索引库：  ~/.local/share/docseek/index.db
渲染缓存：~/.cache/docseek/
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

APP_NAME = "docseek"


def _xdg(env_var, fallback):
    base = os.environ.get(env_var) or str(Path.home() / fallback)
    return Path(base) / APP_NAME


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")

CONFIG_FILE = CONFIG_DIR / "config.json"
INDEX_DB = DATA_DIR / "index.db"
PDF_CACHE = CACHE_DIR / "pdfcache"
IMG_CACHE = CACHE_DIR / "imgcache"
OCR_CACHE = CACHE_DIR / "ocrcache"

# 默认跳过的目录名（任意层级匹配）
DEFAULT_EXCLUDE_DIRS = [
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".cache", ".thumbnails", ".npm", ".gradle", ".m2", ".local", ".config",
    "Trash", ".Trash", ".Trash-1000", ".trash", "proc", "sys", "dev", "run",
    "snap", "flatpak", "lost+found", ".dsh-conv", ".dsh-diag",
]

# 永远不索引的扩展名（二进制、媒体、压缩包、可执行文件等）
SKIP_EXTENSIONS = {
    # 图片/音频/视频
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg", "ico",
    "mp3", "wav", "flac", "ape", "ogg", "m4a", "aac", "mp4", "mkv", "avi",
    "mov", "wmv", "flv", "rmvb", "webm",
    # 压缩包 / 镜像 / 安装包
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso", "deb", "rpm",
    "apk", "jar", "whl", "cab", "msi", "dmg", "img",
    # 可执行 / 库 / 目标文件
    "so", "a", "o", "dll", "dylib", "exe", "bin", "class", "pyc", "pyo", "ko",
    # 数据库 / 索引 / 缓存
    "db", "sqlite", "sqlite3", "ati", "atc", "idx", "dat", "lock", "pid",
    # 字体
    "ttf", "ttc", "otf", "woff", "woff2", "pfb",
}

DEFAULTS = {
    # 索引根目录，由用户在界面里自选，默认空
    "roots": [],
    # 追加跳过的目录名
    "exclude_dirs": [],
    # 追加跳过的绝对路径前缀
    "exclude_paths": [],
    "include_hidden": False,
    "follow_symlinks": False,
    # 单文件最大体积（MB），超过则跳过
    "max_file_mb": 64,
    # 单个文件最多保留的正文字符数
    "max_text_chars": 4000000,
    # 抽取线程数
    "workers": 4,
    # 命中片段上下文长度（字符）
    "snippet_context": 40,
    # 每个文件最多展示几个命中片段
    "snippets_per_file": 3,
    # 结果数量上限
    "max_results": 500,
    # 结果排序：relevance / mtime_desc / mtime_asc / size_desc / size_asc / dir / type
    "sort_mode": "relevance",
    # 版面预览渲染分辨率
    "render_dpi": 150,
    # 是否允许调用 LibreOffice 解析旧格式（较慢）
    "use_libreoffice": True,
    # ---------------- OCR（图片型 PDF、图片里的文字）----------------
    # 总开关：开启后，pdftotext 抽不出文字的 PDF 页会自动走 OCR
    "ocr_enabled": True,
    "ocr_langs": "chi_sim+eng",
    # 渲染分辨率：150dpi 中文识别率明显下降，扫描件建议 300
    "ocr_dpi": 300,
    # 单个 PDF 最多 OCR 多少页（每页约 1~3 秒，防止一本文档卡住整轮索引）
    "ocr_max_pages": 40,
    # 一页文本少于这么多字符就当作"没有文本层"，交给 OCR
    "ocr_min_page_chars": 12,
    # 是否把图片文件（jpg/png/tif…）也纳入索引（照片目录会明显变慢）
    "ocr_images": False,
    # 扫描页取图方式：
    #   auto   优先直接抽取页里内嵌的图片（pdfimages，实测 0.01s/页），抽不到才渲染
    #   render 一律用 pdftoppm 渲染（实测扫描页 20~30s/页，很慢，仅作兼容开关）
    "ocr_page_source": "auto",
    # 送进 OCR 前的像素上限（超了才等比缩小）。
    # 默认 12M ≈ 300dpi A4 扫描件（2480x3508=8.7M）原样识别——实测压到 4M 会让
    # 姓名、编号这类小字认不出来（误差就出在这一步）。想更快可以调到 4000000。
    "ocr_max_pixels": 12000000,
    # 识别结果过短时，用 OSD 判方向、旋转后再试一次（横躺的扫描件不转会全是乱码）
    "ocr_retry_rotate": True,
    # 同时最多跑几个 tesseract（4 核机器建议 3；页与页之间本来就是独立的）
    "ocr_workers": 3,
    "theme": "system",
}


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, PDF_CACHE, IMG_CACHE, OCR_CACHE):
        d.mkdir(parents=True, exist_ok=True)


def temp_dir(prefix="docseek-"):
    """在 docseek 自己的缓存目录下建临时目录（由 cleanup_temp 负责清理）。"""
    ensure_dirs()
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(CACHE_DIR)))


def cleanup_temp(path):
    """只清理 CACHE_DIR 之下、名字带已知前缀的临时目录；其它路径一律拒绝。

    这是应用自身的缓存卫生（README 已写明），不会碰任何用户文件：中间产物
    直接删（不进回收站），免得用户回收站里堆满 docseek-pg-* 之类的目录。
    """
    try:
        target = Path(path).resolve()
        root = CACHE_DIR.resolve()
    except OSError:
        return False
    if target == root or root not in target.parents:
        return False
    if not target.name.startswith(("docseek-", "ocr-", "lo-", "render-")):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def load():
    """读取配置，缺失键用默认值补齐。"""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # 配置损坏时不阻塞启动，用默认值继续
        pass
    return cfg


def save(cfg):
    """原子写配置。"""
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_FILE)


def excluded_dirs(cfg):
    return set(DEFAULT_EXCLUDE_DIRS) | set(cfg.get("exclude_dirs") or [])


def human_size(num):
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return "%.0f %s" % (num, unit) if unit == "B" else "%.1f %s" % (num, unit)
        num /= step
    return "%.1f TB" % num
