"""索引构建器：跳过原因统计与"未完成"判定。

用户遇到过的困惑是："这个文件夹明明有 445 个文件，为什么只索引了 85 个？"
所以这里把"看到多少、跳过多少、为什么跳过、是否跑完"全部断言下来。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import config                            # noqa: E402
from docseek.index import Builder, Index              # noqa: E402
from docseek.index import builder as builder_mod      # noqa: E402


class BuilderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-builder-")
        # 索引库必须在被扫描目录之外：否则 index.db / -wal / -shm 自己会被算成"跳过"
        self.dbdir = tempfile.mkdtemp(prefix="docseek-builder-db-")
        self.cfg = dict(config.DEFAULTS, workers=2, ocr_enabled=False,
                        ocr_images=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    def write(self, relpath, text="内容 人民代表大会\n"):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def open_index(self):
        return Index(os.path.join(self.dbdir, "index.db"))

    def run_builder(self, **kwargs):
        index = self.open_index()
        builder = Builder(index, self.cfg)
        state = builder.run(roots=[self.tmp], sweep=False, **kwargs)
        return index, builder, state


class SkipReportTest(BuilderCase):
    def test_counts_every_skip_reason(self):
        self.write("正常.txt")
        self.write("文档.docx")  # 内容不是真 docx，会被记成 error，但属于"待抽取"
        self.write("照片.jpg")           # 格式不支持（ocr_images=False）
        self.write(".隐藏文件.txt")       # 隐藏文件
        self.write(".hidden/里面.txt")    # 隐藏目录
        self.write("node_modules/a.txt")  # 命中默认排除目录
        big = self.write("超大.txt")
        os.truncate(big, 5 * 1024 * 1024)  # 5MB，配合 1MB 上限 → 跳过
        self.cfg["max_file_mb"] = 1

        index, _builder, state = self.run_builder()
        self.assertEqual(state.total, 2, "待抽取的应只有 正常.txt 与 文档.docx")
        # 注意 seen 的口径：被剪枝的隐藏目录/排除目录里的文件不会逐个计入
        # （只按目录记一次），所以这里是 5 而不是 7
        self.assertGreaterEqual(state.seen, 5, "seen 应是磁盘上看到的文件数")
        self.assertEqual(state.skipped_format, 1, "jpg 应记为格式不支持")
        self.assertEqual(state.skipped_hidden, 2, "隐藏文件 + 隐藏目录里的文件")
        self.assertEqual(state.skipped_excluded, 1, "node_modules 里的文件")
        self.assertEqual(state.skipped_large, 1, "超大文件")
        self.assertEqual(state.skipped, 5)
        summary = state.skip_summary()
        for keyword in ("格式不支持", "隐藏", "排除目录", "体积上限"):
            self.assertIn(keyword, summary)
        index.close()

    def test_hidden_can_be_included(self):
        self.write("可见.txt")
        self.write(".隐藏.txt")
        self.cfg["include_hidden"] = True
        _index, _builder, state = self.run_builder()
        self.assertEqual(state.total, 2)
        self.assertEqual(state.skipped_hidden, 0)


class CompletionTest(BuilderCase):
    def test_completed_run_is_not_interrupted(self):
        self.write("a.txt")
        self.write("b.txt")
        index, _builder, state = self.run_builder()
        self.assertFalse(state.interrupted)
        self.assertEqual(state.phase, "done")
        self.assertEqual(state.done_count, 2)
        self.assertEqual(index.count(), 2)
        index.close()

    def test_interrupted_run_is_reported(self):
        """跑到一半被要求停止：状态必须说清"没跑完"，已完成的记录要留下。"""
        for i in range(40):
            self.write("文件%02d.txt" % i)
        index = self.open_index()
        holder = {}

        def on_progress(state):
            if state.done_count >= 3 and "builder" in holder:
                holder["builder"].stop()

        with mock.patch.object(builder_mod, "EMIT_INTERVAL", 0.0):
            builder = Builder(index, self.cfg, on_progress)
            holder["builder"] = builder
            state = builder.run(roots=[self.tmp], sweep=False)
        self.assertTrue(state.interrupted)
        self.assertEqual(state.phase, "stopped")
        self.assertGreater(state.done_count, 0, "已完成的记录应保留")
        self.assertLess(state.done_count, state.total, "应当确实没跑完")
        index.close()

    def test_progress_is_committed_incrementally(self):
        """提交要按"条数或时间"走：OCR 很慢时，中断也不该丢掉全部成果。"""
        for i in range(12):
            self.write("增量%02d.txt" % i)
        from docseek.index.builder import COMMIT_EVERY_DOCS
        self.assertLessEqual(COMMIT_EVERY_DOCS, 20, "提交粒度不能太大")
        index, _builder, state = self.run_builder()
        self.assertEqual(index.count(), 12)
        self.assertFalse(state.interrupted)
        index.close()

    def test_second_run_keeps_everything_unchanged(self):
        self.write("a.txt")
        self.write("b.txt")
        index, _builder, first = self.run_builder()
        self.assertEqual(first.indexed, 2)
        builder = Builder(index, self.cfg)
        second = builder.run(roots=[self.tmp], sweep=False)
        self.assertEqual(second.unchanged, 2)
        self.assertEqual(second.indexed, 0)
        index.close()


if __name__ == "__main__":
    unittest.main()


class PendingStateTest(BuilderCase):
    """关着 OCR 时抽不到正文的文件要记成 pending，下次开启 OCR 必须重跑。

    这是"先快后慢"批量策略的基础：先不跑 OCR 把有文本层的文档索引完，
    扫描件留待第二轮补上——如果记成 ok，指纹会把它们永久挡住。
    """

    def test_pending_is_retried_error_is_not(self):
        index = self.open_index()
        meta = {"path": "/tmp/docseek-fake/a.pdf", "name": "a.pdf", "ext": "pdf",
                "kind": "pdf", "size": 10, "mtime": 1000.0, "ino": 1, "dev": 1,
                "chars": 0}
        index.upsert(meta, tokens=None, state="pending")
        self.assertTrue(index.needs_update(meta["path"], 10, 1000.0),
                        "pending 必须重试")
        index.upsert(meta, tokens=None, state="error", err="坏了")
        self.assertFalse(index.needs_update(meta["path"], 10, 1000.0),
                         "error 默认不重试")
        self.assertTrue(index.needs_update(meta["path"], 10, 1000.0,
                                           retry_errors=True))
        index.upsert(meta, tokens="abc", state="ok")
        self.assertFalse(index.needs_update(meta["path"], 10, 1000.0),
                         "ok 且指纹未变则跳过")
        index.close()

    def test_progress_counts_pending(self):
        _index, _builder, state = self.run_builder()
        self.assertEqual(state.pending, 0, "没有扫描件时不该有 pending")


class JunkAndEmptyTest(BuilderCase):
    """区分"没有文字"与"真失败"，以及跳过 Office 临时锁文件。"""

    def test_office_lock_files_are_skipped(self):
        from docseek.extract import is_indexable
        self.assertFalse(is_indexable(os.path.join(self.tmp, "~$报告.docx")),
                         "Office 临时锁文件不该进索引")
        self.assertFalse(is_indexable(os.path.join(self.tmp, ".~lock.报告.docx#")))
        self.assertTrue(is_indexable(os.path.join(self.tmp, "报告.docx")))

    def test_empty_text_is_not_a_failure(self):
        """开了 OCR 却仍没文字的（照片/空白）记 empty，不算失败也不重试。

        注意要开着 OCR 才有这个语义：关着 OCR 时没文字会被记成 pending（待补抽）。
        """
        path = self.write("空.txt", "")
        cfg = dict(self.cfg, ocr_enabled=True)
        index = self.open_index()
        state = Builder(index, cfg).run(roots=[self.tmp], sweep=False)
        row = index.get_doc(path)
        self.assertEqual(row["state"], "empty")
        self.assertEqual(state.empty, 1)
        self.assertEqual(state.failed, 0)
        self.assertFalse(index.needs_update(row["path"], row["size"],
                                            row["mtime"]),
                         "empty 不该每次都重试")
        index.close()


class BlankImageTest(BuilderCase):
    """图片里没有文字 → empty（前提是本机有 OCR；没有就跳过）。"""

    def setUp(self):
        super().setUp()
        from docseek.extract import ocr
        if not (ocr.available() and ocr.HAVE_PIL):
            self.skipTest("需要 tesseract 与 PIL")

    def test_photo_without_text_becomes_empty(self):
        from PIL import Image
        path = os.path.join(self.tmp, "照片.png")
        Image.new("RGB", (240, 160), (120, 90, 60)).save(path)
        cfg = dict(self.cfg, ocr_enabled=True, ocr_images=True)
        index = self.open_index()
        state = Builder(index, cfg).run(roots=[self.tmp], sweep=False)
        row = index.get_doc(path)
        self.assertEqual(row["state"], "empty",
                         "照片没有文字应当是 empty，不是 error")
        self.assertIn("没有文字", row["warning"] or "")
        self.assertEqual(state.failed, 0)
        index.close()
