"""docseek.preview.pagemap 测试。

原则：
* 不依赖固定样本：运行时在有界范围内扫真实 PDF / DOCX，扫不到就 skipTest；
* 校验用「另一条路」算期望值（测试自己调 pdftotext / pdfinfo），不复用被测内部函数；
* 测试产物写到 docseek 自己的缓存目录或 tempfile，临时目录由 tests/_scratch.py
  清理：本项目自造的直接删，其余才进回收站。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from docseek import config
from docseek.preview import pagemap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch import discard as _discard_scratch   # noqa: E402

PDFTOTEXT = shutil.which("pdftotext")
PDFINFO = shutil.which("pdfinfo")

# 扫描样本的根目录与时间预算（避免在测试里遍历整个磁盘）
SEARCH_ROOTS = [os.environ.get("DOCSEEK_TEST_ROOT"), os.getcwd(),
                str(Path.home()), "/data", "/tmp"]
SCAN_BUDGET = 12.0
SCAN_MAX_DEPTH = 6
PRUNE_DIRS = {"node_modules", "__pycache__", ".git", ".hg", ".svn", ".cargo",
              ".rustup", ".npm", ".cache", "snap", "flatpak", "proc", "sys",
              "dev", "run", "lost+found", "site-packages"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _discard(path):
    """清理测试临时目录：自造临时目录直接删，其它走回收站（见 tests/_scratch.py）。"""
    return _discard_scratch(path)


def _scan(exts, limit=40, budget=SCAN_BUDGET):
    """在 SEARCH_ROOTS 下有界扫描指定扩展名的文件。"""
    found = []
    deadline = time.time() + budget
    seen_roots = set()
    for root in [r for r in SEARCH_ROOTS if r]:
        root = Path(root)
        try:
            real = str(root.resolve())
        except OSError:
            continue
        if real in seen_roots or not root.is_dir():
            continue
        seen_roots.add(real)
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False,
                                                    onerror=lambda exc: None):
            if time.time() > deadline or len(found) >= limit:
                return found
            if len(Path(dirpath).parts) - base_depth >= SCAN_MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for name in filenames:
                if name.startswith("~$"):
                    continue
                if os.path.splitext(name)[1].lower().lstrip(".") in exts:
                    found.append(Path(dirpath) / name)
                    if len(found) >= limit:
                        return found
    return found


def _pdf_pages_text(pdf_path):
    """测试自带的 pdftotext 调用（独立于 pagemap 实现）。"""
    if not PDFTOTEXT:
        return None
    try:
        proc = subprocess.run(
            [PDFTOTEXT, "-layout", "-enc", "UTF-8", "-q", str(pdf_path), "-"],
            timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        return None
    if not proc.stdout:
        return None
    pages = proc.stdout.decode("utf-8", "replace").split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages or None


def _flatten(text):
    return re.sub(r"\s+", "", text)


def _pdfinfo_pages(pdf_path):
    if not PDFINFO:
        return None
    try:
        proc = subprocess.run([PDFINFO, str(pdf_path)], timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^Pages:\s+(\d+)", proc.stdout.decode("utf-8", "replace"), re.M)
    return int(m.group(1)) if m else None


def _pick_text_pdf():
    """挑一个「有文本层」的 PDF 样本，优先页数多的。"""
    override = os.environ.get("DOCSEEK_TEST_PDF")
    cands = [Path(override)] if override else _scan({"pdf"})
    best = None
    for cand in cands:
        try:
            if not cand.is_file() or cand.stat().st_size < 1024:
                continue
        except OSError:
            continue
        pages = _pdf_pages_text(cand)
        if not pages or not any(p.strip() for p in pages):
            continue
        if best is None or len(pages) > best[1]:
            best = (cand, len(pages))
            if len(pages) >= 5:
                break
    return best[0] if best else None


def _pick_docx():
    override = os.environ.get("DOCSEEK_TEST_DOCX")
    cands = [Path(override)] if override else _scan({"docx"})
    for cand in cands:
        try:
            if not cand.is_file() or cand.stat().st_size < 1024:
                continue
            with open(str(cand), "rb") as fh:
                if fh.read(2) != b"PK":        # docx 必须是 zip
                    continue
        except OSError:
            continue
        return cand
    return None


class TestBadInputs(unittest.TestCase):
    """缺文件 / 不支持格式 / 非法参数：一律安静返回 None / 0。"""

    def test_missing_file(self):
        self.assertIsNone(pagemap.to_pdf("/nonexistent/docseek-xyz.docx"))
        self.assertIsNone(pagemap.render_page("/nonexistent/docseek-xyz.pdf"))
        self.assertIsNone(pagemap.find_page("/nonexistent/docseek-xyz.pdf", "abc"))
        self.assertEqual(pagemap.page_count("/nonexistent/docseek-xyz.pdf"), 0)
        self.assertEqual(pagemap.render_match("/nonexistent/docseek-xyz.pdf", "a"),
                         (None, None))

    def test_unsupported_extension(self):
        tmp = Path(tempfile.mkdtemp(prefix="docseek-test-bad-"))
        try:
            txt = tmp / "note.txt"
            txt.write_text("hello docseek", encoding="utf-8")
            self.assertIsNone(pagemap.to_pdf(txt))
            self.assertIsNone(pagemap.render_page(txt))
            self.assertIsNone(pagemap.find_page(txt, "hello"))
        finally:
            _discard(tmp)

    def test_bad_arguments(self):
        self.assertIsNone(pagemap.render_page(None))
        self.assertIsNone(pagemap.to_pdf(None))
        self.assertIsNone(pagemap.find_page(None, None))
        self.assertIsNone(pagemap.to_pdf(object()))

    def test_broken_pdf(self):
        tmp = Path(tempfile.mkdtemp(prefix="docseek-test-broken-"))
        try:
            bad = tmp / "broken.pdf"
            bad.write_bytes(b"%PDF-1.4\nthis is not a real pdf\n")
            self.assertEqual(pagemap.page_count(bad), 0)
            self.assertIsNone(pagemap.find_page(bad, "x"))
            self.assertIsNone(pagemap.render_page(bad, page=1))
        finally:
            _discard(tmp)


class TestClearCache(unittest.TestCase):
    """clear_cache 的白名单与计数行为（把缓存目录指向临时目录后再测）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docseek-test-cache-"))
        self.pdf_dir = self.tmp / "pdfcache"
        self.img_dir = self.tmp / "imgcache"
        self.pdf_dir.mkdir()
        self.img_dir.mkdir()
        self.outside = self.tmp / "keep.txt"
        self.outside.write_text("不要动我", encoding="utf-8")
        self._saved = (config.PDF_CACHE, config.IMG_CACHE)
        config.PDF_CACHE = self.pdf_dir
        config.IMG_CACHE = self.img_dir

    def tearDown(self):
        config.PDF_CACHE, config.IMG_CACHE = self._saved
        _discard(self.tmp)

    def test_clears_only_cache_dirs(self):
        (self.pdf_dir / "a.pdf").write_bytes(b"pdf")
        sub = self.pdf_dir / "sub"
        sub.mkdir()
        (sub / "nested.pdf").write_bytes(b"pdf")
        for i in range(3):
            (self.img_dir / ("p%d.png" % i)).write_bytes(b"png")
        removed = pagemap.clear_cache()
        self.assertEqual(removed, 5)                       # a.pdf + sub + 3 张图
        self.assertEqual(sorted(p.name for p in self.pdf_dir.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.img_dir.iterdir()), [])
        self.assertTrue(self.outside.is_file(), "缓存之外的条目绝不能被删")

    def test_older_than_days(self):
        old = self.pdf_dir / "old.pdf"
        old.write_bytes(b"pdf")
        fresh = self.pdf_dir / "fresh.pdf"
        fresh.write_bytes(b"pdf")
        long_ago = time.time() - 10 * 86400
        os.utime(str(old), (long_ago, long_ago))
        self.assertEqual(pagemap.clear_cache(older_than_days=1), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_file())
        self.assertEqual(pagemap.clear_cache(older_than_days=1), 0)

    def test_symlink_escape_is_skipped(self):
        link = self.pdf_dir / "sneaky"
        try:
            os.symlink(str(self.tmp), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("无法创建软链接")
        self.assertEqual(pagemap.clear_cache(), 0)
        self.assertTrue(self.outside.is_file())
        self.assertTrue(link.is_symlink())

    def test_missing_dirs(self):
        config.PDF_CACHE = self.tmp / "no-such-dir"
        config.IMG_CACHE = self.tmp / "no-such-dir2"
        self.assertEqual(pagemap.clear_cache(), 0)
        self.assertEqual(pagemap.clear_cache(older_than_days=0), 0)


class TestRealPdf(unittest.TestCase):
    """真实 PDF：page_count / find_page / render_page。"""

    pdf = None
    pages = None

    @classmethod
    def setUpClass(cls):
        cls.pdf = _pick_text_pdf()
        if cls.pdf is None:
            raise unittest.SkipTest("没找到带文本层的 PDF 样本")
        cls.pages = _pdf_pages_text(cls.pdf)

    def _expected_page(self, needle, ignore_case=True):
        key = _flatten(needle.lower() if ignore_case else needle)
        for idx, text in enumerate(self.pages, start=1):
            hay = text.lower() if ignore_case else text
            if key in _flatten(hay):
                return idx
        return None

    def test_page_count(self):
        expected = _pdfinfo_pages(self.pdf)
        got = pagemap.page_count(self.pdf)
        self.assertGreaterEqual(got, 1)
        if expected is not None:
            self.assertEqual(got, expected)
        # pdftotext 会吞掉末尾的空白页，所以只要求不超过真实页数
        self.assertLessEqual(len(self.pages), got)

    def test_find_page_chinese(self):
        target = min(3, len(self.pages))
        flat = _flatten(self.pages[target - 1])
        if len(flat) < 12:
            self.skipTest("样本文字太少")
        needle = flat[len(flat) // 3: len(flat) // 3 + 8]
        expected = self._expected_page(needle)
        self.assertIsNotNone(expected)
        self.assertEqual(pagemap.find_page(self.pdf, needle), expected)

    def test_find_page_ignore_case(self):
        text = "\n".join(self.pages)
        m = re.search(r"[A-Za-z]{6,}", text)
        if not m:
            self.skipTest("样本里没有足够长的西文单词")
        needle = m.group(0)
        expected = self._expected_page(needle, ignore_case=True)
        self.assertIsNotNone(expected)
        self.assertEqual(pagemap.find_page(self.pdf, needle.lower()), expected)
        self.assertEqual(pagemap.find_page(self.pdf, needle.upper()), expected)

    def test_find_page_across_line_break(self):
        """跨行断开的短语：空白归一化后仍应命中。"""
        target = min(2, len(self.pages))
        flat = _flatten(self.pages[target - 1])
        if len(flat) < 16:
            self.skipTest("样本文字太少")
        needle = flat[20:32]
        broken = needle[:6] + "\n  " + needle[6:]
        expected = self._expected_page(broken)
        self.assertIsNotNone(expected)
        self.assertEqual(pagemap.find_page(self.pdf, broken), expected)

    def test_find_page_not_found(self):
        self.assertIsNone(pagemap.find_page(self.pdf, "绝对不存在的字符串XYZQQ9988"))
        self.assertIsNone(pagemap.find_page(self.pdf, ""))
        self.assertIsNone(pagemap.find_page(self.pdf, "   "))

    def test_to_pdf_passthrough(self):
        self.assertEqual(Path(pagemap.to_pdf(self.pdf)), self.pdf)

    def test_render_page_and_cache(self):
        page = len(self.pages)                 # 最后一页，避开其它用例已渲染过的页
        started = time.time()
        cold = pagemap.render_page(self.pdf, page=page, dpi=150, cache=False)
        cold_dt = time.time() - started
        self.assertIsNotNone(cold, "冷渲染失败")
        with open(str(cold), "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC)

        started = time.time()
        png = pagemap.render_page(self.pdf, page=page, dpi=150)
        first = time.time() - started
        self.assertIsNotNone(png, "渲染失败")
        png = Path(png)
        self.assertTrue(png.is_file())
        self.assertGreater(png.stat().st_size, 1000)
        with open(str(png), "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC)
        self.assertTrue(str(png).startswith(str(config.IMG_CACHE)),
                        "缓存图片必须落在 config.IMG_CACHE 下")

        started = time.time()
        again = pagemap.render_page(self.pdf, page=page, dpi=150)
        cached = time.time() - started
        self.assertEqual(Path(again), png)
        self.assertLess(cached, max(first, 0.05), "命缓存应该更快")
        print("\n[实测] pdftoppm 150dpi 第 %d 页：冷渲染 %.2fs，首次落缓存 %.2fs，"
              "命中缓存 %.3fs" % (page, cold_dt, first, cached))

    def test_render_page_different_dpi(self):
        png150 = pagemap.render_page(self.pdf, page=1, dpi=150)
        png72 = pagemap.render_page(self.pdf, page=1, dpi=72)
        self.assertIsNotNone(png150)
        self.assertIsNotNone(png72)
        self.assertNotEqual(Path(png150), Path(png72), "缓存键必须含 dpi")
        self.assertGreater(Path(png150).stat().st_size, Path(png72).stat().st_size)

    def test_render_page_out_of_range(self):
        total = pagemap.page_count(self.pdf)
        self.assertIsNone(pagemap.render_page(self.pdf, page=total + 5))
        self.assertIsNone(pagemap.render_page(self.pdf, page=0))
        self.assertIsNone(pagemap.render_page(self.pdf, page=-3))
        self.assertIsNone(pagemap.render_page(self.pdf, page="不是数字"))
        self.assertIsNone(pagemap.render_page(self.pdf, page=None))

    def test_render_match(self):
        target = min(2, len(self.pages))
        flat = _flatten(self.pages[target - 1])
        if len(flat) < 12:
            self.skipTest("样本文字太少")
        needle = flat[len(flat) // 2: len(flat) // 2 + 8]
        expected = self._expected_page(needle)
        png, page = pagemap.render_match(self.pdf, needle)
        self.assertEqual(page, expected)
        self.assertIsNotNone(png)
        self.assertTrue(Path(png).is_file())

    def test_render_match_falls_back_to_first_page(self):
        png, page = pagemap.render_match(self.pdf, "绝对不存在的字符串XYZQQ9988")
        self.assertEqual(page, 1)
        self.assertIsNotNone(png)
        self.assertTrue(Path(png).is_file())


class TestRealOffice(unittest.TestCase):
    """真实 .docx：to_pdf（LibreOffice）→ render_page 出图。"""

    docx = None

    @classmethod
    def setUpClass(cls):
        cls.docx = _pick_docx()
        if cls.docx is None:
            raise unittest.SkipTest("没找到 .docx 样本")
        if not pagemap.lo.available():
            raise unittest.SkipTest("本机没有 LibreOffice")

    def test_to_pdf_and_render(self):
        # 冷转换（不落缓存）拿到真实的 LibreOffice 耗时
        started = time.time()
        temp_pdf = pagemap.to_pdf(self.docx, cache=False)
        cold_convert = time.time() - started
        if temp_pdf is None:
            self.skipTest("LibreOffice 转换失败（%s）" % self.docx.name)
        try:
            self.assertTrue(Path(temp_pdf).is_file())
            self.assertTrue(pagemap._is_temp_pdf(temp_pdf),
                            "cache=False 的产物应位于 CACHE_DIR 的临时目录里")
        finally:
            pagemap._release_temp_pdf(temp_pdf)

        started = time.time()
        pdf = pagemap.to_pdf(self.docx)
        first_convert = time.time() - started
        pdf = Path(pdf)
        self.assertTrue(pdf.is_file())
        self.assertEqual(pdf.suffix.lower(), ".pdf")
        self.assertTrue(str(pdf).startswith(str(config.PDF_CACHE)),
                        "缓存 PDF 必须落在 config.PDF_CACHE 下")

        started = time.time()
        again = pagemap.to_pdf(self.docx)
        cached_convert = time.time() - started
        self.assertEqual(Path(again), pdf)
        self.assertLess(cached_convert, max(first_convert, 0.05))
        self.assertGreaterEqual(pagemap.page_count(pdf), 1)

        started = time.time()
        cold_png = pagemap.render_page(pdf, page=1, dpi=150, cache=False)
        cold_render = time.time() - started
        self.assertIsNotNone(cold_png, "转换后的 PDF 没能渲染出图")
        with open(str(cold_png), "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC)

        png = pagemap.render_page(pdf, page=1, dpi=150)
        self.assertIsNotNone(png, "转换后的 PDF 没能渲染出图")
        with open(str(png), "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC)

        started = time.time()
        again_png = pagemap.render_page(pdf, page=1, dpi=150)
        cached_render = time.time() - started
        self.assertEqual(Path(again_png), Path(png))
        # 注意：首次落缓存那一档无法稳定测到冷值（同类里别的用例可能已经预热过），
        # 所以只用 cache=False 的冷值 + 命中缓存的值来报耗时。
        print("\n[实测] LibreOffice 转 PDF：冷转换（cache=False）%.2fs，"
              "命中缓存 %.3fs；pdftoppm 150dpi 首页：冷渲染 %.2fs，命中缓存 %.3fs"
              % (cold_convert, cached_convert, cold_render, cached_render))

    def test_render_match_on_docx(self):
        png, page = pagemap.render_match(self.docx, "的")
        if png is None:
            self.skipTest("LibreOffice 转换失败（%s）" % self.docx.name)
        self.assertGreaterEqual(page, 1)
        self.assertTrue(Path(png).is_file())

    def test_to_pdf_text_file_is_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="docseek-test-txt-"))
        try:
            txt = tmp / "a.txt"
            txt.write_text("hi", encoding="utf-8")
            self.assertIsNone(pagemap.to_pdf(txt))
        finally:
            _discard(tmp)


class TestSyntheticPdf(unittest.TestCase):
    """用 LibreOffice 现场造一份已知内容的 PDF，确定性地验证大小写与中文匹配。"""

    pdf = None
    fixture_dir = None

    TEXT = "DocSeek ALPHAPROBE-42 探针行\n第二行 中文探针内容\n"

    @classmethod
    def setUpClass(cls):
        if not pagemap.lo.available():
            raise unittest.SkipTest("本机没有 LibreOffice，跳过自造样本")
        try:
            config.ensure_dirs()
            cls.fixture_dir = Path(tempfile.mkdtemp(
                prefix="docseek-test-fixture-", dir=str(config.CACHE_DIR)))
            src = cls.fixture_dir / "probe.txt"
            src.write_text(cls.TEXT, encoding="utf-8")
            produced = pagemap.lo.convert_to_pdf(src)
        except Exception as exc:                        # noqa: BLE001 - 测试兜底
            cls.pdf = None
            raise unittest.SkipTest("构造 PDF 样本失败：%s" % exc)
        if produced is None:
            raise unittest.SkipTest("LibreOffice 无法把 txt 转成 PDF")
        cls.pdf = Path(produced)

    @classmethod
    def tearDownClass(cls):
        if cls.pdf is not None:
            _discard(cls.pdf.parent)                    # docseek-lo-* 临时目录
        if cls.fixture_dir is not None:
            _discard(cls.fixture_dir)

    def test_find_page_case_sensitivity(self):
        self.assertGreaterEqual(pagemap.page_count(self.pdf), 1)
        self.assertEqual(pagemap.find_page(self.pdf, "alphaprobe"), 1)
        self.assertEqual(pagemap.find_page(self.pdf, "ALPHAPROBE"), 1)
        self.assertEqual(pagemap.find_page(self.pdf, "alphaprobe-42"), 1)
        self.assertEqual(pagemap.find_page(self.pdf, "中文探针"), 1)
        self.assertIsNone(pagemap.find_page(self.pdf, "alphaprobe", ignore_case=False))
        self.assertEqual(
            pagemap.find_page(self.pdf, "ALPHAPROBE", ignore_case=False), 1)
        self.assertIsNone(pagemap.find_page(self.pdf, "PROBE-NOT-THERE"))

    def test_render_created_pdf(self):
        png, page = pagemap.render_match(self.pdf, "中文探针")
        self.assertEqual(page, 1)
        self.assertIsNotNone(png)
        with open(str(png), "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC)



class TestTempHygiene(unittest.TestCase):
    """自建的中间目录不许塞进用户回收站。

    用户报障：跑一阵子后，回收站里多出一堆 docseek-pg-* / docseek-lo-* 目录。
    这些是预览过程的中间产物，程序自己能重建，本来就不该占用用户的回收站。
    """

    def test_own_cache_dir_is_deleted_not_trashed(self):
        config.ensure_dirs()
        work = config.temp_dir("docseek-pg-")
        (work / "page-1.png").write_bytes(b"x")
        with mock.patch.object(pagemap, "_trash") as trash:
            self.assertTrue(pagemap._recycle(work))
        self.assertFalse(work.exists(), "自建缓存目录应该被删掉")
        self.assertFalse(trash.called, "自家缓存不该走 gio trash")

    def test_render_scratch_prefix_is_whitelisted(self):
        config.ensure_dirs()
        work = config.temp_dir("render-")
        self.assertTrue(config.cleanup_temp(work))
        self.assertFalse(work.exists())

    def test_unknown_name_falls_back_to_trash(self):
        config.ensure_dirs()
        odd = config.temp_dir("keepme-")          # 在缓存目录里，但不在白名单
        try:
            with mock.patch.object(pagemap, "_trash", return_value=True) as trash:
                self.assertTrue(pagemap._recycle(odd))
            self.assertTrue(trash.called, "白名单之外的路径要交回回收站")
        finally:
            shutil.rmtree(odd, ignore_errors=True)

    def test_paths_outside_cache_dir_are_never_deleted(self):
        outside = tempfile.mkdtemp(prefix="docseek-pg-")   # 名字对，但位置不对
        try:
            self.assertFalse(config.cleanup_temp(outside))
            self.assertTrue(os.path.isdir(outside), "缓存目录之外一律不许删")
        finally:
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
