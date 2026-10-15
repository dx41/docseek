"""LibreOffice 无头转换（旧格式兜底）。

注意：
* 使用 docseek 自己的 UserInstallation 目录，避免干扰用户已打开的 LibreOffice/WPS；
* soffice 同一 profile 不允许并发，模块级锁串行化；
* 实测本机（LO 6.4 / aarch64）批量转换约 0.32 s/份，单份冷启约 1.5 s。
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .. import config
from .base import ExtractError, clean_text

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")

# LibreOffice 的 UserInstallation 目录。
# **索引与预览必须用不同的 profile**：同一个 profile 下 LibreOffice 一次只处理一份
# 文档，索引正在批量转换时，预览的转换会排队——实测一份 .doc 从 1 秒变成 69 秒，
# 界面表现就是预览一直卡在"正在读取正文……"。
PROFILE_DIR = config.CACHE_DIR / "loprofile"
PROFILE_DIR_PREVIEW = config.CACHE_DIR / "loprofile-preview"

# **按 profile 分别加锁**：LibreOffice 同一 profile 不能并发，但不同 profile 可以。
# 索引与预览用的是不同 profile，所以预览不会再被索引的批量转换堵住
# （以前一把全局锁，索引在转大 .ppt 时预览要干等，界面卡在"正在读取正文……"）。
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(profile):
    key = str(profile or PROFILE_DIR)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock

# target -> LibreOffice 转换过滤器
# 注意 "txt" 是 Writer 专用过滤器，表格类文档必然失败（0xc10），
# 表格请走 "csv"（快得多，实测 .et 5.5s → 约 1.5s）。
FILTERS = {
    "txt": "txt:Text (encoded):UTF8",
    "csv": "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,true",
    "pdf": "pdf",
    "html": "html:HTML (StarWriter)",
}


def available():
    return bool(SOFFICE)


def _env():
    env = dict(os.environ)
    env["SAL_USE_VCLPLUGIN"] = "svp"       # 无显示环境下的 headless 渲染
    env["SAL_DISABLE_OPENCL"] = "1"
    env.setdefault("HOME", str(Path.home()))
    return env


def convert(src, target="txt", out_dir=None, timeout=180, profile=None):
    """把 src 转换成 target（txt/pdf/html），返回产物路径。

    找不到 soffice 时抛 ExtractError。
    """
    if not SOFFICE:
        raise ExtractError("未找到 soffice（LibreOffice），无法解析该格式")
    src = Path(src)
    if not src.is_file():
        raise ExtractError("文件不存在：%s" % src)
    target = target.lower()
    if target not in FILTERS:
        raise ExtractError("不支持的转换目标：%s" % target)

    own_out = out_dir is None
    out_dir = Path(out_dir) if out_dir else Path(
        tempfile.mkdtemp(prefix="docseek-lo-", dir=str(config.CACHE_DIR)))
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        "-env:UserInstallation=file://%s" % (profile or PROFILE_DIR),
        "--convert-to", FILTERS[target],
        "--outdir", str(out_dir),
        str(src),
    ]

    with _lock_for(profile):
        last = ""
        for attempt in (1, 2):
            try:
                proc = subprocess.run(
                    cmd, env=_env(), timeout=timeout,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                last = proc.stdout.decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                last = "转换超时（%ss）" % timeout
            produced = _find_output(out_dir, src, target)
            if produced:
                return produced
            time.sleep(1.0 if attempt == 1 else 0)

    raise ExtractError("LibreOffice 转换失败：%s" % (last.strip()[:300] or "无输出"))


def _find_output(out_dir, src, target):
    want = {"txt": (".txt",), "csv": (".csv",), "pdf": (".pdf",),
            "html": (".html", ".htm")}[target]
    stem = src.stem
    cands = []
    for p in Path(out_dir).iterdir():
        if p.suffix.lower() in want:
            cands.append(p)
    if not cands:
        return None
    for p in cands:
        if p.stem == stem:
            return p
    return sorted(cands)[0]


def convert_to_text(src, timeout=180, target="txt"):
    """旧格式 → 纯文本；表格类可传 target="csv"（Writer 的 txt 过滤器对表格无效）。"""
    out = convert(src, target, timeout=timeout)
    try:
        raw = out.read_bytes()
    except OSError as exc:
        raise ExtractError("读取转换结果失败：%s" % exc)
    text = _decode(raw)
    _cleanup(out)
    return clean_text(text, config.DEFAULTS["max_text_chars"])


def convert_to_pdf(src, timeout=240, profile=None):
    """任意 LO 可打开的格式 → PDF，返回 PDF 路径（调用方负责缓存/清理）。"""
    return convert(src, "pdf", timeout=timeout, profile=profile)


def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _cleanup(path):
    """删除转换产生的临时文件（进回收站不可行，故仅删除本次自建临时目录）。

    这里只在调用方未指定 out_dir 时清理，且忽略失败。
    """
    try:
        parent = Path(path).parent
        if parent.name.startswith("docseek-lo-") and str(parent).startswith(
                str(config.CACHE_DIR)):
            shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass
