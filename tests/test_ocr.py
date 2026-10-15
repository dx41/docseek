"""OCR 相关测试。

需要 tesseract（含 eng/chi_sim 语言包）与 poppler 的 pdftoppm；缺哪个就跳过哪部分。
所有样本都是现场用 PIL 合成的图片/图片型 PDF，放在临时目录里。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import config                          # noqa: E402
from docseek.extract import ExtractError, extract, is_indexable   # noqa: E402
from docseek.extract import ocr, pdf                # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:                                 # pragma: no cover
    HAVE_PIL = False

CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


def find_cjk_font():
    for path in CJK_FONTS:
        if os.path.exists(path):
            return path
    return None


def make_image(path, text, size=(900, 220), font_size=56):
    """合成一张写着 text 的图片。"""
    font = ImageFont.truetype(find_cjk_font(), font_size)
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((40, size[1] // 3), text, font=font, fill="black")
    image.save(path)
    return path


def make_image_pdf(path, text, **kwargs):
    """合成"图片型 PDF"：整页就是一张图，没有文本层（pdftotext 抽不出字）。"""
    font = ImageFont.truetype(find_cjk_font(), kwargs.pop("font_size", 56))
    image = Image.new("RGB", (900, 220), "white")
    ImageDraw.Draw(image).text((40, 70), text, font=font, fill="black")
    image.save(path, "PDF", resolution=200)
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-ocr-test-")

    def tearDown(self):
        # 测试自身的临时目录：进回收站，不物理删除
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)


class LangTest(unittest.TestCase):
    """语言解析逻辑（不依赖本机是否装了 tesseract）。"""

    def test_resolve_keeps_available_only(self):
        with mock.patch.object(ocr, "languages", return_value=["eng", "chi_sim"]):
            self.assertEqual(ocr.resolve_langs("chi_sim+eng"), "chi_sim+eng")
            self.assertEqual(ocr.resolve_langs("chi_sim+deu"), "chi_sim")

    def test_resolve_falls_back_to_eng(self):
        with mock.patch.object(ocr, "languages", return_value=["eng"]):
            self.assertEqual(ocr.resolve_langs("chi_sim"), "eng")

    def test_resolve_without_any_language_pack(self):
        with mock.patch.object(ocr, "languages", return_value=[]):
            self.assertEqual(ocr.resolve_langs("chi_sim+eng"), "chi_sim+eng")

    def test_languages_parses_first_line_header(self):
        output = b"List of available languages (3):\neng\nchi_sim\nosd\n"
        with mock.patch.object(ocr, "TESSERACT", "/usr/bin/tesseract"), \
                mock.patch.object(ocr.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=output)
            ocr.languages(force=True)
            self.assertEqual(ocr.languages(), ["eng", "chi_sim", "osd"])
        ocr.languages(force=True)   # 还原真实值


class PackingTest(unittest.TestCase):
    """fill_empty_pages 的页选择逻辑（把真正的识别替换掉，保证又快又确定）。"""

    def setUp(self):
        self.options = {"ocr_enabled": True, "ocr_min_page_chars": 12,
                        "ocr_max_pages": 40, "ocr_langs": "eng"}

    def test_pages_with_text_are_left_alone(self):
        pages = ["这是一页有足够文字的内容，不应该被送去 OCR。" * 2,
                 "另一页也有文字，字数超过阈值所以不该被识别。"]
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=True), \
                mock.patch.object(ocr, "ocr_pdf_page") as fake:
            result, done, skipped = ocr.fill_empty_pages("x.pdf", pages, self.options)
        fake.assert_not_called()
        self.assertEqual((result, done, skipped), (pages, 0, 0))

    def test_empty_pages_are_ocr_filled(self):
        pages = ["", "有文字的一页，内容足够长不需要 OCR。", ""]
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=True), \
                mock.patch.object(ocr, "ocr_pdf_page",
                                  return_value="识别出来的文字") as fake:
            result, done, skipped = ocr.fill_empty_pages("x.pdf", pages, self.options)
        self.assertEqual(done, 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(result, ["识别出来的文字",
                                  "有文字的一页，内容足够长不需要 OCR。",
                                  "识别出来的文字"])
        self.assertEqual([call.args[1] for call in fake.call_args_list], [1, 3],
                         "应当只识别第 1、3 页（页码从 1 开始）")

    def test_max_pages_limits_work(self):
        pages = [""] * 5
        options = dict(self.options, ocr_max_pages=2)
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=True), \
                mock.patch.object(ocr, "ocr_pdf_page", return_value="字"):
            _result, done, skipped = ocr.fill_empty_pages("x.pdf", pages, options)
        self.assertEqual(done, 2)
        self.assertEqual(skipped, 3)

    def test_disabled_ocr_does_nothing(self):
        pages = [""]
        options = dict(self.options, ocr_enabled=False)
        with mock.patch.object(ocr, "ocr_pdf_page") as fake:
            result, done, skipped = ocr.fill_empty_pages("x.pdf", pages, options)
        fake.assert_not_called()
        self.assertEqual((result, done, skipped), (pages, 0, 0))

    def test_missing_pdftoppm_does_nothing(self):
        pages = [""]
        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=False), \
                mock.patch.object(ocr, "ocr_pdf_page") as fake:
            result, done, _skipped = ocr.fill_empty_pages("x.pdf", pages, self.options)
        fake.assert_not_called()
        self.assertEqual((result, done), (pages, 0))


class ImageIndexableTest(unittest.TestCase):
    """图片是否纳入索引，取决于 OCR 与 ocr_images 两个开关。"""

    def test_image_skipped_by_default(self):
        cfg = dict(config.DEFAULTS)
        self.assertFalse(is_indexable("/tmp/whatever/照片.jpg", cfg))

    def test_image_indexable_when_enabled(self):
        cfg = dict(config.DEFAULTS, ocr_images=True)
        self.assertTrue(is_indexable("/tmp/whatever/扫描件.png", cfg))

    def test_image_not_indexable_when_ocr_off(self):
        cfg = dict(config.DEFAULTS, ocr_images=True, ocr_enabled=False)
        self.assertFalse(is_indexable("/tmp/whatever/扫描件.png", cfg))

    def test_normal_documents_unaffected(self):
        cfg = dict(config.DEFAULTS)
        self.assertTrue(is_indexable("/tmp/whatever/报告.docx", cfg))
        self.assertTrue(is_indexable("/tmp/whatever/说明.pdf", cfg))
        self.assertFalse(is_indexable("/tmp/whatever/压缩包.zip", cfg))


class CacheWhitelistTest(unittest.TestCase):
    """缓存清理只允许作用于 ~/.cache/docseek 之下。"""

    def test_refuses_paths_outside_cache(self):
        outside = tempfile.mkdtemp(prefix="not-docseek-cache-")
        try:
            self.assertFalse(config.cleanup_temp(outside))
            self.assertTrue(os.path.isdir(outside), "不该被删掉")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_refuses_unprefixed_dir_inside_cache(self):
        config.ensure_dirs()
        victim = os.path.join(str(config.CACHE_DIR), "keep-me")
        os.makedirs(victim, exist_ok=True)
        try:
            self.assertFalse(config.cleanup_temp(victim))
            self.assertTrue(os.path.isdir(victim))
        finally:
            shutil.rmtree(victim, ignore_errors=True)

    def test_accepts_own_temp_dir(self):
        path = config.temp_dir("ocr-")
        self.assertTrue(os.path.isdir(str(path)))
        self.assertTrue(config.cleanup_temp(path))
        self.assertFalse(os.path.isdir(str(path)))


@unittest.skipUnless(HAVE_PIL, "需要 PIL")
@unittest.skipUnless(find_cjk_font(), "没有可用的中文字体")
@unittest.skipUnless(ocr.available(), "需要 tesseract")
class RealOcrTest(TempCase):
    """真跑一次 tesseract，确认中文/西文都能认出来。"""

    def test_ocr_english_image(self):
        path = make_image(self.path("en.png"), "DOCSEEK OCR TEST 2026")
        text = ocr.ocr_image(path, langs="eng")
        self.assertIn("DOCSEEK", text.upper().replace(" ", ""))

    def test_ocr_chinese_image(self):
        path = make_image(self.path("cn.png"), "人民代表大会")
        text = ocr.ocr_image(path, langs="chi_sim+eng")
        self.assertIn("人民代表大会", text.replace(" ", ""))

    @unittest.skipUnless(ocr.pdf_available(), "需要 pdftoppm")
    def test_image_only_pdf_is_extracted_via_ocr(self):
        path = make_image_pdf(self.path("扫描件.pdf"), "DOCSEEK OCR TEST 2026")
        # 前提：这种 PDF 确实没有文本层
        self.assertEqual(pdf.extract(path, options={"ocr_enabled": False}).text.strip(), "")

        got = pdf.extract(path, options=dict(config.DEFAULTS, ocr_langs="eng"))
        self.assertIn("DOCSEEK", got.text.upper().replace(" ", ""))
        self.assertIn("OCR", got.warning)
        self.assertEqual(got.page_offsets[:1], [0])

    @unittest.skipUnless(ocr.pdf_available(), "需要 pdftoppm")
    def test_scanned_pdf_without_ocr_reports_hint(self):
        path = make_image_pdf(self.path("无文本层.pdf"), "DOCSEEK OCR TEST 2026")
        got = extract(path, options={"ocr_enabled": False})
        self.assertEqual(got.text.strip(), "")
        self.assertIn("扫描件", got.warning)

    @unittest.skipUnless(ocr.pdf_available(), "需要 pdftoppm")
    def test_ocr_result_is_cached(self):
        path = make_image_pdf(self.path("缓存.pdf"), "DOCSEEK OCR TEST 2026")
        options = dict(config.DEFAULTS, ocr_langs="eng")
        first = ocr.ocr_pdf_page(path, 1, langs="eng", dpi=300)
        self.assertTrue(first.strip())
        cache_file = ocr._cache_file(path, 1, ocr.resolve_langs("eng"), 300)
        self.assertTrue(cache_file.exists(), "识别结果应写入缓存")
        # 命中缓存时不该再调用 tesseract
        with mock.patch.object(ocr, "ocr_image") as fake:
            second = ocr.ocr_pdf_page(path, 1, langs="eng", dpi=300)
        fake.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(options["ocr_langs"], "eng")


@unittest.skipUnless(HAVE_PIL, "需要 PIL")
@unittest.skipUnless(find_cjk_font(), "没有可用的中文字体")
@unittest.skipUnless(ocr.available() and ocr.pdf_available(), "需要 tesseract 与 pdftoppm")
class ScanTwoPassTest(TempCase):
    """先关 OCR 快跑一遍 → 扫描件记成 pending；再开 OCR 跑一遍 → 补上文字。"""

    def test_scan_is_pending_then_filled_by_second_pass(self):
        from docseek.index import Builder, Index
        path = make_image_pdf(self.path("扫描件.pdf"), "DOCSEEK OCR TEST 2026")
        db = self.path("index.db")

        fast = dict(config.DEFAULTS, ocr_enabled=False, workers=1)
        index = Index(db)
        first = Builder(index, fast).run(roots=[self.tmp], sweep=False)
        row = index.get_doc(path)
        self.assertEqual(row["state"], "pending", "关 OCR 的扫描件应记成 pending")
        self.assertEqual(first.pending, 1)

        slow = dict(config.DEFAULTS, ocr_enabled=True, workers=1, ocr_langs="eng")
        index2 = Index(db)
        second = Builder(index2, slow).run(roots=[self.tmp], sweep=False)
        row = index2.get_doc(path)
        self.assertEqual(row["state"], "ok", "第二轮应把它补抽出来")
        self.assertEqual(second.indexed, 1)
        text = index2.conn.execute(
            "SELECT chars FROM docs WHERE path = ?", (path,)).fetchone()[0]
        self.assertGreater(text, 0, "补抽后应有正文字数")
        index.close()
        index2.close()


class NoTesseractTest(unittest.TestCase):
    """没有 tesseract 时要给出可读的错误，而不是崩溃。"""

    def test_image_backend_reports_missing_tesseract(self):
        from docseek.extract import image as image_backend
        with mock.patch.object(ocr, "available", return_value=False):
            with self.assertRaises(ExtractError) as ctx:
                image_backend.extract("/tmp/whatever.png",
                                      options={"ocr_enabled": True})
        self.assertIn("tesseract", str(ctx.exception))

    def test_image_backend_respects_switch(self):
        from docseek.extract import image as image_backend
        with self.assertRaises(ExtractError) as ctx:
            image_backend.extract("/tmp/whatever.png",
                                  options={"ocr_enabled": False})
        self.assertIn("未启用 OCR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 性能相关：这些数字都是实测出来的，改动前请先读 docseek/extract/ocr.py 的注释
# ---------------------------------------------------------------------------

class ThreadLimitTest(unittest.TestCase):
    def test_tesseract_is_forced_single_threaded(self):
        """回归：tesseract 必须单线程跑。

        实测（aarch64/4 核，4M 像素页图）：默认 4 线程 44.8 s，单线程 5.3 s，
        输出逐字相同。谁把这里改回去，性能会立刻掉 8 倍。
        """
        env = ocr._tess_env()
        self.assertEqual(env.get("OMP_THREAD_LIMIT"), "1")
        self.assertEqual(env.get("OMP_NUM_THREADS"), "1")

    def test_recognize_passes_the_env(self):
        with mock.patch.object(ocr, "TESSERACT", "/usr/bin/tesseract"), \
                mock.patch.object(ocr.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"hello")
            ocr.ocr_image("/tmp/x.png", langs="eng")
        env = run.call_args[1].get("env") or {}
        self.assertEqual(env.get("OMP_THREAD_LIMIT"), "1",
                         "tesseract 调用必须带上单线程环境")

    def test_rotation_detection_passes_the_env(self):
        with mock.patch.object(ocr, "TESSERACT", "/usr/bin/tesseract"), \
                mock.patch.object(ocr.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0,
                                         stdout=b"Rotate: 90\n")
            self.assertEqual(ocr.detect_rotation("/tmp/x.png"), 90)
        env = run.call_args[1].get("env") or {}
        self.assertEqual(env.get("OMP_THREAD_LIMIT"), "1")


class ThreadCountTest(TempCase):
    def test_rotation_parser(self):
        with mock.patch.object(ocr, "TESSERACT", "/usr/bin/tesseract"), \
                mock.patch.object(ocr.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"Rotate: 0\n")
            self.assertIsNone(ocr.detect_rotation("/tmp/x.png"))
            run.return_value = mock.Mock(returncode=0, stdout="乱码".encode("utf-8"))
            self.assertIsNone(ocr.detect_rotation("/tmp/x.png"))


@unittest.skipUnless(HAVE_PIL, "需要 PIL")
class ImagePipelineTest(TempCase):
    def test_render_side_follows_pixel_budget(self):
        """渲染前就按像素预算折算 -scale-to，别先画巨图再缩。"""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0)

        with mock.patch.object(ocr, "PDFTOPPM", "/usr/bin/pdftoppm"), \
                mock.patch.object(ocr.subprocess, "run", side_effect=fake_run):
            ocr.render_page("/tmp/x.pdf", 1, 300, self.tmp, 4000000)
        cmd = captured["cmd"]
        side = int(cmd[cmd.index("-scale-to") + 1])
        self.assertLess(side, 2000,
                        "4M 像素预算下长边不该超过 ~1680，实际 %d" % side)

    def test_shrink_reduces_pixels(self):
        path = self.path("big.png")
        Image.new("RGB", (3000, 2000), "white").save(path)   # 6M 像素
        out = ocr.shrink_image(path, 1000000, self.tmp)
        self.assertNotEqual(out, path, "超过预算就该另存缩小版")
        with Image.open(out) as image:
            self.assertLessEqual(image.size[0] * image.size[1], 1000000)
            self.assertLess(image.size[0], 3000)

    def test_shrink_skips_small_images(self):
        path = self.path("small.png")
        Image.new("RGB", (200, 100), "white").save(path)
        self.assertEqual(ocr.shrink_image(path, 4000000, self.tmp), path)

    def test_extract_page_image_requires_single_image(self):
        """一页里恰好一张内嵌图才走这条快路径（简单包装的 pdfimages 产物）。"""
        out_dir = Path(self.tmp)

        def fake_run(cmd, **kwargs):
            (out_dir / "x00001-000.jpg").write_bytes(b"fake")
            return mock.Mock(returncode=0)

        with mock.patch.object(ocr, "PDFIMAGES", "/usr/bin/pdfimages"), \
                mock.patch.object(ocr.subprocess, "run", side_effect=fake_run):
            got = ocr.extract_page_image("/tmp/x.pdf", 1, self.tmp)
        self.assertTrue(got and got.endswith("x00001-000.jpg"))

        # 一页产出两张图 → 不认为是"整页扫描"，返回 None 交给渲染
        def fake_run_two(cmd, **kwargs):
            (out_dir / "x00002-000.jpg").write_bytes(b"a")
            (out_dir / "x00002-001.jpg").write_bytes(b"b")
            return mock.Mock(returncode=0)

        with mock.patch.object(ocr, "PDFIMAGES", "/usr/bin/pdfimages"), \
                mock.patch.object(ocr.subprocess, "run", side_effect=fake_run_two):
            self.assertIsNone(ocr.extract_page_image("/tmp/x.pdf", 2, self.tmp))

    def test_extract_returns_none_without_pdfimages(self):
        with mock.patch.object(ocr, "PDFIMAGES", None):
            self.assertIsNone(ocr.extract_page_image("/tmp/x.pdf", 1, self.tmp))


class ParallelPagesTest(TempCase):
    def test_pages_are_recognized_in_parallel(self):
        """页与页互不依赖：3 页并发应当明显快于串行。"""
        pages = ["", "", ""]
        options = {"ocr_enabled": True, "ocr_min_page_chars": 12,
                   "ocr_max_pages": 40, "ocr_workers": 3}

        def slow_page(pdf, page, **kwargs):
            time.sleep(0.4)
            return "第 %d 页识别结果" % page

        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=True), \
                mock.patch.object(ocr, "ocr_pdf_page", side_effect=slow_page):
            started = time.time()
            result, done, skipped = ocr.fill_empty_pages(
                "/tmp/x.pdf", pages, options)
            elapsed = time.time() - started
        self.assertEqual(done, 3)
        self.assertEqual(skipped, 0)
        self.assertTrue(all(r for r in result))
        self.assertLess(elapsed, 0.4 * 3 * 0.75,
                        "并发没生效，耗时 %.2fs（串行约 1.2s）" % elapsed)

    def test_single_worker_stays_serial(self):
        pages = ["", ""]
        options = {"ocr_enabled": True, "ocr_min_page_chars": 12,
                   "ocr_max_pages": 40, "ocr_workers": 1}

        with mock.patch.object(ocr, "available", return_value=True), \
                mock.patch.object(ocr, "pdf_available", return_value=True), \
                mock.patch.object(ocr, "ocr_pdf_page",
                                  return_value="识别结果文字"):
            result, done, _skipped = ocr.fill_empty_pages(
                "/tmp/x.pdf", pages, options)
        self.assertEqual(done, 2)
        self.assertEqual(result, ["识别结果文字", "识别结果文字"])


class RobustImageTest(TempCase):
    """tesseract/leptonica 打不开的图（某些 BMP）要先转 PNG 再试。"""

    @unittest.skipUnless(HAVE_PIL, "需要 PIL")
    def test_converts_and_retries(self):
        path = self.path("weird.bmp")
        Image.new("RGB", (60, 40), "white").save(path)
        calls = {"n": 0}

        def fake_ocr(img, langs=None, **kwargs):
            calls["n"] += 1
            if str(img).endswith(".bmp"):
                raise ExtractError("Error in pixReadMemBmp: invalid imagebytes")
            return "转换后识别成功"

        with mock.patch.object(ocr, "ocr_image", side_effect=fake_ocr):
            text = ocr.ocr_image_robust(path, langs="eng")
        self.assertEqual(text, "转换后识别成功")
        self.assertEqual(calls["n"], 2, "应当先直接试、失败后转 PNG 再试")

    @unittest.skipUnless(HAVE_PIL, "需要 PIL")
    def test_returns_first_error_when_conversion_fails(self):
        path = Path(self.path("broken.png"))
        path.write_bytes(b"not an image")
        with mock.patch.object(ocr, "ocr_image",
                               side_effect=ExtractError("原始错误")):
            with self.assertRaises(ExtractError) as ctx:
                ocr.ocr_image_robust(path, langs="eng")
        self.assertIn("原始错误", str(ctx.exception))
