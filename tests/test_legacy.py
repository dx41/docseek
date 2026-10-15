# -*- coding: utf-8 -*-
"""docseek.extract.legacy 的单元测试。

覆盖内容：

* 后端优先级与降级链：在临时目录里造假的 catdoc / antiword / unrtf / wps2text
  shell 脚本，不需要真的装这些工具就能验证“选谁/换谁”；
* 外部命令异常（命令不存在、超时、非零退出码）都转成中文 ExtractError 并降级；
* max_bytes 限制、文件不存在、kind 归并、warning 记录后端名；
* 内置 RTF 剥离器（控制字、\\'hh 代码页转义、\\uNNNN、可忽略目标、\\bin）；
* 乱码守卫（避免把 .dps 这类二进制被 LO 当纯文本导入的产物写进索引）；
* 真实样本（在 /data、/home 里现找，找不到就 skip）：.doc/.xls/.rtf/.wps/.et。

注意：按本机约定不做物理删除，因此测试临时文件统一放在“一次运行一个”的临时根目录
（/tmp/docseek-test-legacy-XXXX，tmpfs）里，全部跑完后整目录 `gio trash` 进回收站；
用户文件只读不改、不移动、不删除。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from docseek.extract import legacy                      # noqa: E402
from docseek.extract.base import ExtractError           # noqa: E402

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# WPS Office 自带的关联模板目录（里面有真实的 .dps/.ppt/.pps 样本）
WPS_TEMPLATES = "/opt/kingsoft/wps-office/office6/asso_template"

# 本次运行共用的临时根目录：跑完由 tearDownModule 送进回收站（不做物理删除）
_TMP_ROOT = tempfile.mkdtemp(prefix="docseek-test-legacy-")


def tearDownModule():
    """整轮测试结束后清理临时根目录：进回收站，失败就留在 /tmp（重启即清）。"""
    gio = shutil.which("gio")
    if not gio:
        return
    try:
        subprocess.run([gio, "trash", _TMP_ROOT], timeout=60,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------
# 辅助工具
# --------------------------------------------------------------------------

def make_tmpdir(prefix="case-"):
    """在本次运行的临时根目录下建一个用例专用目录（不物理删除）。"""
    return tempfile.mkdtemp(prefix=prefix, dir=_TMP_ROOT)


def write_tool(directory, name, body):
    """造一个可执行的假外部工具（shell 脚本），用于验证后端选择逻辑。"""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def write_file(directory, name, data):
    path = os.path.join(directory, name)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    kwargs = {} if mode == "wb" else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as fh:
        fh.write(data)
    return path


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _head(path, size=8):
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


_SKIP_DIRS = {".Trash", ".Trash-1000", ".cache", ".local", ".git", "node_modules",
              ".dsh-conv", ".dsh-diag", "proc", "sys", "dev"}


def find_samples(want, roots=("/data", "/home"), max_depth=8, max_size=300 * 1024):
    """在真实目录里找样本文件：{扩展名: 路径}，找不到的扩展名不出现。

    只读扫描，不修改任何用户文件；每个扩展名取第一个命中即停，因此很快。
    """
    found = {}
    for root in roots:
        if len(found) == len(want):
            break
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in filenames:
                if name.startswith("~$"):           # Word/WPS 的临时锁文件
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in want or ext in found:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > max_size:
                        continue
                except OSError:
                    continue
                if _is_real_sample(ext, path):
                    found[ext] = path
            if len(found) == len(want):
                break
    return found


def _is_real_sample(ext, path):
    """粗验文件确实是该旧格式（避免 .doc 其实是 RTF/文本等干扰）。"""
    head = _head(path, 8)
    if ext in ("doc", "xls", "wps", "et", "dps", "ppt"):
        return head == OLE_MAGIC
    if ext == "rtf":
        return _head(path, 16).startswith(b"{\\rtf")
    return True


def _try_xlrd():
    try:
        import xlrd  # noqa: F401
        return True
    except ImportError:
        return False


HAVE_XLRD = _try_xlrd()
HAVE_LO = bool(legacy.lo.available())
try:
    SAMPLES = find_samples(("doc", "xls", "rtf", "wps", "et", "dps"))
except OSError:                                     # 扫描失败不影响其它用例
    SAMPLES = {}


class FakeLoBackend(object):
    """替换 legacy._lo：记录是否被调用，并返回固定文本。"""

    def __init__(self, text="来自 LibreOffice 的中文正文", label="libreoffice"):
        self.text = text
        self.label = label
        self.calls = []

    def __call__(self, path):
        self.calls.append(str(path))
        if self.text is None:
            raise ExtractError("模拟：LibreOffice 不可用")
        return self.text, [], self.label


class FakeBackend(object):
    """替换任意后端：抛错或返回固定文本。"""

    def __init__(self, text=None, error="模拟后端失败"):
        self.text = text
        self.error = error
        self.calls = []

    def __call__(self, path):
        self.calls.append(str(path))
        if self.text is None:
            raise ExtractError(self.error)
        return self.text, [], "fake"


# --------------------------------------------------------------------------
# 后端优先级 / 降级
# --------------------------------------------------------------------------

class TestDocChain(unittest.TestCase):
    """doc/dot：catdoc → antiword → LibreOffice。"""

    def setUp(self):
        self.tmp = make_tmpdir()
        self.doc = write_file(self.tmp, "样本.doc", OLE_MAGIC + b"\x00" * 64)
        self.catdoc = write_tool(
            self.tmp, "catdoc", "printf '%s\\n' '假 catdoc 抽出的中文'")
        self.antiword = write_tool(
            self.tmp, "antiword", "printf '%s\\n' '假 antiword 抽出的中文'")

    def test_prefers_catdoc(self):
        with mock.patch.object(legacy, "CATDOC", self.catdoc), \
                mock.patch.object(legacy, "ANTIWORD", self.antiword):
            result = legacy.extract(self.doc)
        self.assertEqual(result.kind, "doc")
        self.assertEqual(result.warning, "catdoc")
        self.assertIn("假 catdoc", result.text)

    def test_falls_back_to_antiword(self):
        with mock.patch.object(legacy, "CATDOC", None), \
                mock.patch.object(legacy, "ANTIWORD", self.antiword):
            result = legacy.extract(self.doc)
        self.assertEqual(result.warning, "antiword")
        self.assertIn("假 antiword", result.text)

    def test_falls_back_to_libreoffice(self):
        lo_backend = FakeLoBackend()
        with mock.patch.object(legacy, "CATDOC", None), \
                mock.patch.object(legacy, "ANTIWORD", None), \
                mock.patch.object(legacy, "_lo", lo_backend):
            result = legacy.extract(self.doc)
        self.assertEqual(result.warning, "libreoffice")
        self.assertTrue(lo_backend.calls)

    def test_nonzero_exit_degrades(self):
        """catdoc 退出码非 0 时要降级，而不是把异常抛给调用方。"""
        broken = write_tool(self.tmp, "broken-catdoc",
                            "echo 'bad file' >&2\nexit 3")
        with mock.patch.object(legacy, "CATDOC", broken), \
                mock.patch.object(legacy, "ANTIWORD", self.antiword):
            result = legacy.extract(self.doc)
        self.assertEqual(result.warning, "antiword")

    def test_missing_binary_degrades(self):
        """路径不存在（FileNotFoundError）也要降级。"""
        with mock.patch.object(legacy, "CATDOC", "/no/such/catdoc"), \
                mock.patch.object(legacy, "ANTIWORD", self.antiword):
            result = legacy.extract(self.doc)
        self.assertEqual(result.warning, "antiword")

    def test_empty_output_degrades(self):
        silent = write_tool(self.tmp, "silent-catdoc", "exit 0")
        with mock.patch.object(legacy, "CATDOC", silent), \
                mock.patch.object(legacy, "ANTIWORD", self.antiword):
            result = legacy.extract(self.doc)
        self.assertEqual(result.warning, "antiword")

    def test_all_backends_fail_reports_each(self):
        with mock.patch.object(legacy, "CATDOC", None), \
                mock.patch.object(legacy, "ANTIWORD", None), \
                mock.patch.object(legacy, "_lo", FakeLoBackend(text=None)):
            with self.assertRaises(ExtractError) as ctx:
                legacy.extract(self.doc)
        message = str(ctx.exception)
        self.assertIn("catdoc", message)
        self.assertIn("antiword", message)
        self.assertIn("libreoffice", message)

    def test_dot_ext_uses_doc_chain(self):
        path = write_file(self.tmp, "模板.dot", OLE_MAGIC + b"\x00" * 64)
        with mock.patch.object(legacy, "CATDOC", self.catdoc):
            result = legacy.extract(path)
        self.assertEqual(result.kind, "doc")

    def test_gbk_output_is_decoded(self):
        """工具直接吐 GB18030 字节时，_decode 要能猜对编码。"""
        tool = write_tool(self.tmp, "gbk-catdoc", "printf '\\326\\320\\316\\304\\n'")
        with mock.patch.object(legacy, "CATDOC", tool):
            result = legacy.extract(self.doc)
        self.assertEqual(result.text.strip(), "中文")


class TestXlsChain(unittest.TestCase):
    """xls/xlt：xlrd → LibreOffice。"""

    def setUp(self):
        self.tmp = make_tmpdir()

    def test_corrupt_xls_degrades_to_libreoffice(self):
        path = write_file(self.tmp, "坏文件.xls", b"this is not a real xls")
        lo_backend = FakeLoBackend()
        with mock.patch.object(legacy, "_lo", lo_backend):
            result = legacy.extract(path)
        self.assertEqual(result.warning, "libreoffice")
        self.assertEqual(result.kind, "xls")

    def test_xlrd_failure_degrades(self):
        """xlrd 抛错（例如加密/损坏）也要降级。"""
        path = write_file(self.tmp, "样本.xlt", OLE_MAGIC + b"\x00" * 128)
        lo_backend = FakeLoBackend()
        with mock.patch.object(legacy, "_lo", lo_backend):
            result = legacy.extract(path)
        self.assertEqual(result.warning, "libreoffice")
        self.assertEqual(result.kind, "xls")

    def test_xlrd_missing_degrades(self):
        path = write_file(self.tmp, "样本.xls", OLE_MAGIC + b"\x00" * 128)
        lo_backend = FakeLoBackend()
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "xlrd":
                raise ImportError("模拟：未安装 xlrd")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(legacy, "_lo", lo_backend), \
                mock.patch("builtins.__import__", side_effect=fake_import):
            result = legacy.extract(path)
        self.assertEqual(result.warning, "libreoffice")


class TestRtfChain(unittest.TestCase):
    """rtf：unrtf → 内置剥离器 → LibreOffice。"""

    # \'xx 是 GBK（ansicpg936）字节：d6d0 ce c4 d5 fd ce c4 b2 e2 ca d4 = 中文正文测试
    # b5da b6fe b6ce = 第二段
    TEXT = "中文正文测试"
    TEXT2 = "第二段"
    RTF = ("{\\rtf1\\ansi\\ansicpg936\\uc1\\deff0"
           "{\\fonttbl{\\f0\\fnil\\fcharset134 SimSun;}}"
           "{\\*\\generator Riched20 10.0}\\pard\\f0 "
           "\\'d6\\'d0\\'ce\\'c4\\'d5\\'fd\\'ce\\'c4\\'b2\\'e2\\'ca\\'d4"
           "\\par \\'b5\\'da\\'b6\\'fe\\'b6\\'ce\\par}")

    def setUp(self):
        self.tmp = make_tmpdir()
        self.rtf = write_file(self.tmp, "样本.rtf", self.RTF.encode("latin-1"))

    def test_prefers_unrtf(self):
        tool = write_tool(self.tmp, "unrtf", "printf '%s\\n' '假 unrtf 抽出的中文'")
        with mock.patch.object(legacy, "UNRTF", tool):
            result = legacy.extract(self.rtf)
        self.assertEqual(result.warning, "unrtf")
        self.assertEqual(result.kind, "rtf")
        self.assertIn("假 unrtf", result.text)

    def test_builtin_when_unrtf_missing(self):
        with mock.patch.object(legacy, "UNRTF", None), \
                mock.patch.object(legacy, "_lo", FakeLoBackend(text=None)):
            result = legacy.extract(self.rtf)
        self.assertEqual(result.warning, "builtin-rtf")
        self.assertIn(self.TEXT, result.text)
        self.assertIn(self.TEXT2, result.text)

    def test_unrtf_failure_falls_to_builtin(self):
        broken = write_tool(self.tmp, "broken-unrtf", "exit 2")
        with mock.patch.object(legacy, "UNRTF", broken):
            result = legacy.extract(self.rtf)
        self.assertEqual(result.warning, "builtin-rtf")

    def test_falls_back_to_libreoffice(self):
        """内置剥离器也失败（这里用空正文模拟）时降级到 LO。"""
        with mock.patch.object(legacy, "UNRTF", None), \
                mock.patch.object(legacy, "_rtf_builtin",
                                  FakeBackend(error="模拟：内置剥离器失败")), \
                mock.patch.object(legacy, "_lo", FakeLoBackend()):
            result = legacy.extract(self.rtf)
        self.assertEqual(result.warning, "libreoffice")


class TestWpsChain(unittest.TestCase):
    """wps/wpt：wps2text → LibreOffice。"""

    def setUp(self):
        self.tmp = make_tmpdir()
        self.wps = write_file(self.tmp, "样本.wps", OLE_MAGIC + b"\x00" * 64)

    def test_prefers_wps2text(self):
        tool = write_tool(
            self.tmp, "wps2text", "printf '%s\\n' '假 wps2text 抽出的中文'")
        with mock.patch.object(legacy, "WPS2TEXT", tool):
            result = legacy.extract(self.wps)
        self.assertEqual(result.warning, "wps2text")
        self.assertEqual(result.kind, "wps")
        self.assertIn("假 wps2text", result.text)

    def test_missing_tool_degrades_to_libreoffice(self):
        lo_backend = FakeLoBackend()
        with mock.patch.object(legacy, "WPS2TEXT", None), \
                mock.patch.object(legacy, "_lo", lo_backend):
            result = legacy.extract(self.wps)
        self.assertEqual(result.warning, "libreoffice")
        self.assertTrue(lo_backend.calls)

    def test_wpt_maps_to_wps_kind(self):
        path = write_file(self.tmp, "模板.wpt", OLE_MAGIC + b"\x00" * 64)
        with mock.patch.object(legacy, "WPS2TEXT", None), \
                mock.patch.object(legacy, "_lo", FakeLoBackend()):
            result = legacy.extract(path)
        self.assertEqual(result.kind, "wps")


class TestOtherFormats(unittest.TestCase):
    """et/ett、dps/dpt、ppt/pot/pps 及其它 LEGACY_EXTS 都走 LibreOffice。"""

    def setUp(self):
        self.tmp = make_tmpdir()

    def test_kind_mapping_and_lo_route(self):
        cases = [("表.et", "et"), ("表.ett", "et"), ("演示.dps", "dps"),
                 ("演示.dpt", "dps"), ("讲稿.ppt", "ppt"), ("讲稿.pot", "ppt"),
                 ("放映.pps", "ppt"), ("旧表.wk1", "legacy"),
                 ("文档.sxw", "legacy"), ("邮件.mht", "legacy")]
        for name, kind in cases:
            path = write_file(self.tmp, name, OLE_MAGIC + b"\x00" * 32)
            with mock.patch.object(legacy, "_lo", FakeLoBackend()):
                result = legacy.extract(path)
            self.assertEqual(result.kind, kind, name)
            self.assertEqual(result.warning, "libreoffice", name)


# --------------------------------------------------------------------------
# 参数与异常处理
# --------------------------------------------------------------------------

class TestExtractGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir()

    def test_missing_file(self):
        with self.assertRaises(ExtractError) as ctx:
            legacy.extract(os.path.join(self.tmp, "不存在.doc"))
        self.assertIn("文件不存在", str(ctx.exception))

    def test_max_bytes_rejects_before_converter(self):
        """超过 max_bytes 时必须直接拒绝，且不能调用转换器。"""
        marker = os.path.join(self.tmp, "called.marker")
        tool = write_tool(self.tmp, "marker-catdoc",
                          "touch '%s'\nprintf 'x\\n'" % marker)
        path = write_file(self.tmp, "大文件.doc", OLE_MAGIC + b"\x00" * 4096)
        with mock.patch.object(legacy, "CATDOC", tool):
            with self.assertRaises(ExtractError) as ctx:
                legacy.extract(path, max_bytes=1024)
        self.assertIn("过大", str(ctx.exception))
        self.assertFalse(os.path.exists(marker), "超限文件不应交给外部转换器")

    def test_under_limit_passes(self):
        path = write_file(self.tmp, "小文件.doc", OLE_MAGIC + b"\x00" * 32)
        tool = write_tool(self.tmp, "ok-catdoc", "printf '%s\\n' '限内文件中文'")
        with mock.patch.object(legacy, "CATDOC", tool):
            result = legacy.extract(path, max_bytes=1024 * 1024)
        self.assertIn("限内文件中文", result.text)

    def test_default_max_bytes_from_config(self):
        """max_bytes=None 时用 config.DEFAULTS["max_file_mb"]，正常文件不受影响。"""
        from docseek import config
        path = write_file(self.tmp, "默认.doc", OLE_MAGIC + b"\x00" * 32)
        tool = write_tool(self.tmp, "ok2-catdoc", "printf '%s\\n' '默认上限中文'")
        with mock.patch.object(legacy, "CATDOC", tool):
            result = legacy.extract(path)
        self.assertGreater(int(config.DEFAULTS["max_file_mb"]) * 1024 * 1024, 32)
        self.assertIn("默认上限中文", result.text)

    def test_run_soft_missing_command(self):
        with self.assertRaises(ExtractError) as ctx:
            legacy._run(["/no/such/binary", "x"])
        self.assertIn("未找到命令", str(ctx.exception))

    def test_run_soft_timeout(self):
        with self.assertRaises(ExtractError) as ctx:
            legacy._run(["sleep", "5"], timeout=1)
        self.assertIn("超时", str(ctx.exception))

    def test_run_soft_nonzero_exit(self):
        with self.assertRaises(ExtractError) as ctx:
            legacy._run(["sh", "-c", "echo 出错了 >&2; exit 4"])
        self.assertIn("退出码 4", str(ctx.exception))

    def test_package_dispatch_reaches_legacy(self):
        """docseek.extract.extract 应按分发逻辑走到本模块。"""
        from docseek import extract as dispatch
        path = write_file(self.tmp, "分发.doc", OLE_MAGIC + b"\x00" * 32)
        tool = write_tool(self.tmp, "dispatch-catdoc", "printf '%s\\n' '分发链路中文'")
        with mock.patch.object(legacy, "CATDOC", tool):
            result = dispatch.extract(path)
        self.assertEqual(result.kind, "doc")
        self.assertEqual(result.warning, "catdoc")
        self.assertIn("分发链路中文", result.text)


# --------------------------------------------------------------------------
# 内置 RTF 剥离器
# --------------------------------------------------------------------------

class TestRtfToText(unittest.TestCase):
    def test_plain_paragraphs(self):
        self.assertEqual(
            legacy.rtf_to_text("{\\rtf1\\ansi 第一段\\par 第二段\\par}"),
            "第一段\n第二段")

    def test_hex_escapes_with_codepage(self):
        self.assertEqual(
            legacy.rtf_to_text("{\\rtf1\\ansi\\ansicpg936 \\'d6\\'d0\\'ce\\'c4}"),
            "中文")

    def test_unicode_escapes(self):
        self.assertEqual(
            legacy.rtf_to_text("{\\rtf1\\ansi\\ansicpg936\\uc2 "
                               "\\u20013?\\u25991?\\u27979?\\u35797?}"),
            "中文测试")

    def test_unicode_with_zero_fallback(self):
        self.assertEqual(legacy.rtf_to_text("{\\rtf1\\ansi\\uc0\\u20320\\u22909}"),
                         "你好")

    def test_ignorable_destinations_dropped(self):
        src = ("{\\rtf1\\ansi{\\fonttbl{\\f0\\fnil Arial;}}"
               "{\\*\\generator Riched20}{\\colortbl;\\red0\\green0\\blue0;}"
               "\\f0 正文}")
        self.assertEqual(legacy.rtf_to_text(src), "正文")

    def test_skips_fldinst_but_keeps_result(self):
        src = ("{\\rtf1\\ansi{\\field{\\*\\fldinst HYPERLINK \"http://x\"}"
               "{\\fldrslt 链接文字}}}")
        self.assertEqual(legacy.rtf_to_text(src), "链接文字")

    def test_bin_data_skipped(self):
        src = ("{\\rtf1\\ansi\\ansicpg936 甲\\tab 乙\\par"
               "{\\pict\\bin8 0011223344556677}丙\\par}")
        self.assertEqual(legacy.rtf_to_text(src), "甲\t乙\n丙")

    def test_file_newlines_are_dropped(self):
        """RTF 文件里的折行不是正文换行，应该丢掉（否则会把词切断）。"""
        src = "{\\rtf1\\ansi\\ansicpg936 三级人大换\n届选举\\par}"
        self.assertEqual(legacy.rtf_to_text(src), "三级人大换届选举")

    def test_latin1_text(self):
        self.assertEqual(
            legacy.rtf_to_text("{\\rtf1\\ansi\\ansicpg1252 caf\\'e9 na\\'efve}"),
            "café naïve")

    def test_curly_quotes(self):
        self.assertEqual(
            legacy.rtf_to_text(
                "{\\rtf1\\ansi\\ansicpg936 引号\\'a1\\'b0测试\\'a1\\'b1}"),
            "引号“测试”")

    def test_non_rtf_input_is_text(self):
        plain = "这其实不是 RTF，是纯文本"
        self.assertEqual(legacy.rtf_to_text(plain), plain)

    def test_bytes_input(self):
        raw = "{\\rtf1\\ansi\\ansicpg936 \\'d6\\'d0}".encode("latin-1")
        self.assertEqual(legacy.rtf_to_text(raw), "中")

    def test_returns_str_and_strips(self):
        text = legacy.rtf_to_text("{\\rtf1\\ansi\\par\\par 内容 \\par\\par}")
        self.assertIsInstance(text, str)
        self.assertNotIn("\n\n\n", text)


# --------------------------------------------------------------------------
# 乱码守卫
# --------------------------------------------------------------------------

class TestJunkGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir()
        self.path = write_file(self.tmp, "演示.dps", OLE_MAGIC + b"\x00" * 64)

    def test_binary_junk_rejected(self):
        junk = "#" * 600 + ("\x01\x02祆" * 200)
        with mock.patch.object(legacy, "_lo",
                               FakeLoBackend(text=legacy.clean_text(junk))):
            with self.assertRaises(ExtractError) as ctx:
                legacy.extract(self.path)
        self.assertIn("乱码", str(ctx.exception))

    def test_normal_text_accepted(self):
        text = "这是一段正常的中文正文，用来确认乱码守卫不会误伤。" * 20
        lo_backend = FakeLoBackend(text=legacy.clean_text(text))
        with mock.patch.object(legacy, "_lo", lo_backend):
            result = legacy.extract(self.path)
        self.assertIn("正常的中文正文", result.text)

    def test_short_text_never_flagged(self):
        self.assertFalse(legacy._looks_like_junk("#" * 100))
        self.assertFalse(legacy._looks_like_junk(""))
        self.assertTrue(legacy._looks_like_junk("#" * 400))


class TestHtmlToText(unittest.TestCase):
    """LibreOffice HTML 导出 → 纯文本。"""

    def test_tables_and_headings(self):
        html = (
            "<html><head><style>td { color: red }</style></head><body>"
            "<A NAME=\"table0\"><h1>Sheet 1: <em>第一张表</em></h1></A>"
            "<table><tr><td>甲组数据</td><td>123</td></tr>"
            "<tr><td>乙组数据</td><td><br></td></tr></table>"
            "<!-- comment --></body></html>")
        text = legacy._html_to_text(html)
        self.assertIn("Sheet 1: 第一张表", text)
        self.assertIn("甲组数据\t123", text)
        self.assertIn("乙组数据", text)
        self.assertNotIn("<", text)
        self.assertNotIn("color: red", text)

    def test_entities_unescaped(self):
        self.assertIn("&", legacy._html_to_text("<p>a&amp;b</p>"))


# --------------------------------------------------------------------------
# 真实样本（找不到样本 / 缺工具就 skip）
# --------------------------------------------------------------------------

@unittest.skipUnless(HAVE_LO, "需要 LibreOffice（soffice）")
@unittest.skipUnless("doc" in SAMPLES, "在 /data、/home 里没找到 .doc 样本")
class TestRealDoc(unittest.TestCase):
    def test_real_doc_extracts_chinese(self):
        result = legacy.extract(SAMPLES["doc"])
        self.assertEqual(result.kind, "doc")
        self.assertTrue(result.text.strip(), "真实 .doc 应抽出非空正文")
        self.assertTrue(CJK_RE.search(result.text), "应抽出中文正文")
        self.assertIn(result.warning, ("catdoc", "antiword", "libreoffice",
                                       "libreoffice-html"))


@unittest.skipUnless(HAVE_XLRD, "需要 xlrd")
@unittest.skipUnless("xls" in SAMPLES, "在 /data、/home 里没找到 .xls 样本")
class TestRealXls(unittest.TestCase):
    def test_real_xls_extracts_chinese(self):
        result = legacy.extract(SAMPLES["xls"])
        self.assertEqual(result.kind, "xls")
        self.assertEqual(result.warning, "xlrd")
        self.assertTrue(result.text.strip())
        self.assertTrue(CJK_RE.search(result.text), "应抽出中文表格内容")
        self.assertTrue(result.page_offsets, "xlrd 后端应给出工作表偏移")
        self.assertEqual(result.page_of(result.page_offsets[0]), 1)

    def test_real_xls_via_libreoffice_fallback(self):
        """xlrd 不可用时，表格要走 LibreOffice 的 csv 通路（HTML 为后备）。

        txt 过滤器是 Writer 专用的、对表格必然失败；csv 带上 UTF-8 token 后
        既能抽出中文又比 HTML 快数倍（实测 .et 5.5s → 0.9s）。
        """
        if not HAVE_LO:
            self.skipTest("需要 LibreOffice（soffice）")
        with mock.patch.object(legacy, "_xlrd",
                               FakeBackend(error="模拟：xlrd 不可用")):
            result = legacy.extract(SAMPLES["xls"])
        self.assertIn(result.warning, ("libreoffice-csv", "libreoffice-html"))
        self.assertTrue(result.text.strip())
        self.assertTrue(CJK_RE.search(result.text))


@unittest.skipUnless("rtf" in SAMPLES, "在 /data、/home 里没找到 .rtf 样本")
class TestRealRtf(unittest.TestCase):
    def test_real_rtf_extracts_chinese(self):
        result = legacy.extract(SAMPLES["rtf"])
        self.assertEqual(result.kind, "rtf")
        self.assertTrue(result.text.strip())
        self.assertTrue(CJK_RE.search(result.text), "应抽出中文正文")
        # 无论走 unrtf 还是内置剥离器，都不该出现大片 '?' 回退字符
        self.assertLess(result.text.count("?"), max(5, len(result.text) // 100))

    def test_real_rtf_builtin_directly(self):
        with open(SAMPLES["rtf"], "rb") as fh:
            text = legacy.rtf_to_text(fh.read())
        self.assertTrue(CJK_RE.search(text))
        self.assertNotIn("\\par", text)
        self.assertNotIn("\\'", text)


@unittest.skipUnless(HAVE_LO, "需要 LibreOffice（soffice）")
@unittest.skipUnless("wps" in SAMPLES, "在 /data、/home 里没找到 .wps 样本")
class TestRealWps(unittest.TestCase):
    def test_real_wps_extracts_chinese(self):
        result = legacy.extract(SAMPLES["wps"])
        self.assertEqual(result.kind, "wps")
        self.assertTrue(result.text.strip())
        self.assertTrue(CJK_RE.search(result.text), "应抽出中文正文")


@unittest.skipUnless(HAVE_LO, "需要 LibreOffice（soffice）")
@unittest.skipUnless("et" in SAMPLES, "在 /data、/home 里没找到 .et 样本")
class TestRealEt(unittest.TestCase):
    def test_real_et_extracts_chinese(self):
        """金山表格：lo.convert_to_text 会失败，实际由 HTML 通路救回来。"""
        result = legacy.extract(SAMPLES["et"])
        self.assertEqual(result.kind, "et")
        self.assertTrue(result.text.strip())
        self.assertTrue(CJK_RE.search(result.text), "应抽出中文表格内容")


# WPS Office 自带的关联模板里有真实的 .dps/.ppt/.pps，用来验证演示文稿的表现
_PRESENTATIONS = [os.path.join(WPS_TEMPLATES, name)
                  for name in ("wps.dps", "wps.ppt", "wps.pps")]
_PRESENTATIONS = [p for p in _PRESENTATIONS if os.path.isfile(p)]


@unittest.skipUnless(HAVE_LO, "需要 LibreOffice（soffice）")
@unittest.skipUnless(_PRESENTATIONS, "没找到演示文稿样本（WPS 模板目录）")
class TestRealPresentations(unittest.TestCase):
    """演示文稿：无论能否解析，都不许把二进制乱码当成正文返回。"""

    def test_never_returns_junk(self):
        for path in _PRESENTATIONS:
            try:
                result = legacy.extract(path)
            except ExtractError as exc:
                # 本机 LO 读不了 WPS 演示，必须给出中文原因而不是乱码正文
                self.assertTrue(str(exc).strip(), path)
                self.assertIn("libreoffice", str(exc), path)
                continue
            self.assertTrue(result.text.strip(), path)
            self.assertFalse(legacy._looks_like_junk(result.text),
                             "%s 抽出了疑似乱码" % path)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LibreOfficeProfileTest(unittest.TestCase):
    """索引与预览必须用不同的 LibreOffice profile（否则预览要排队几十秒）。"""

    def test_preview_profile_is_separate_from_index_profile(self):
        from docseek.extract import lo
        self.assertNotEqual(lo.PROFILE_DIR, lo.PROFILE_DIR_PREVIEW,
                            "预览与索引不能共用同一个 UserInstallation")

    def test_lock_is_per_profile(self):
        """不同 profile 不该共用一把锁（实测：共用时预览被索引堵到 69 秒）。"""
        from docseek.extract import lo
        index_lock = lo._lock_for(None)                 # 默认 = 索引用
        preview_lock = lo._lock_for(lo.PROFILE_DIR_PREVIEW)
        self.assertIsNot(index_lock, preview_lock)
        self.assertIs(lo._lock_for(lo.PROFILE_DIR_PREVIEW), preview_lock,
                      "同一 profile 必须复用同一把锁")

    def test_convert_passes_profile_to_command(self):
        import os as _os
        import tempfile
        from unittest import mock
        from docseek.extract import lo
        handle, sample = tempfile.mkstemp(suffix=".doc")
        _os.close(handle)
        self.addCleanup(_os.unlink, sample)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0)

        with mock.patch.object(lo, "SOFFICE", "/usr/bin/soffice"), \
                mock.patch.object(lo.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(lo, "_find_output", return_value=None):
            try:
                lo.convert_to_pdf(sample, timeout=5,
                                  profile=lo.PROFILE_DIR_PREVIEW)
            except Exception:                       # noqa: BLE001 找不到产物属正常
                pass
        cmd = captured.get("cmd") or []
        self.assertTrue(any("loprofile-preview" in str(part) for part in cmd),
                        "转换命令里应带上预览专用 profile：%s" % cmd)
