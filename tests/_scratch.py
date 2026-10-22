# -*- coding: utf-8 -*-
"""测试临时产物的清理规则。

本机纪律：**用户的文件**删除必须走回收站，绝不物理删除。但测试自己造的临时目录是
"一次运行就作废"的中间产物，把它们一股脑塞进回收站会把用户的回收站变成垃圾场
（用户实际报障过：跑一阵子回收站里多出一堆 docseek-pg-* / docseek-lo-* / docseek-test-*）。

所以分两种：

* 路径在系统临时目录或 docseek 自己的缓存目录之下，且目录名带本项目的已知前缀
  （docseek-、render-、ocr-、lo-）→ 直接删（shutil.rmtree）；
* 其它路径（真碰到用户数据的情况）→ 交给 `gio trash`，绝不物理删除。
"""

import os
import shutil
import subprocess
import tempfile

# 本项目自己造的所有临时目录/文件都用这些前缀（见 config.temp_dir 与各 extractor）
SAFE_PREFIXES = ("docseek-", "render-", "ocr-", "lo-")


def _safe_roots():
    """允许直接物理删除的根目录：系统临时目录 + docseek 缓存目录。"""
    roots = [tempfile.gettempdir()]
    try:
        from docseek import config
        roots.append(str(config.CACHE_DIR))
    except Exception:            # 导入失败就不放行缓存目录，宁严不松
        pass
    out = []
    for root in roots:
        try:
            out.append(os.path.realpath(root).rstrip(os.sep) + os.sep)
        except OSError:
            continue
    return out


def is_scratch(path):
    """是否为"本项目自造、可直接删"的临时路径。"""
    try:
        real = os.path.realpath(str(path))
    except OSError:
        return False
    name = os.path.basename(real.rstrip(os.sep))
    if not name.startswith(SAFE_PREFIXES):
        return False
    return any(real.startswith(root) for root in _safe_roots())


def discard(path, timeout=60):
    """清理测试临时产物：自建临时目录直接删，其它交回回收站。返回是否已清掉。"""
    path = str(path)
    if not os.path.exists(path):
        return True
    if is_scratch(path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            return True
        except OSError:
            return False
    gio = shutil.which("gio")
    if not gio:
        return False
    try:
        proc = subprocess.run([gio, "trash", "--", path], timeout=timeout,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
