"""正文存储（textstore）与"检索不再现抽"的测试。

背景：检索要用原文做精确校验和片段。早期版本每次搜索都重新抽取候选文档，
遇到 .doc/.wps（走 LibreOffice）或几百页 PDF 会把一次搜索拖到 30 秒。
现在建索引时把正文压缩进 `docs.content`，检索直接读——这里把这些行为钉住。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import config, textstore                 # noqa: E402
from docseek.index import Builder, Index              # noqa: E402
from docseek.index import db as index_db              # noqa: E402
from docseek.search import Searcher                   # noqa: E402


class CodecTest(unittest.TestCase):
    def test_content_round_trip(self):
        text = "人民代表大会制度\n" * 500
        blob = textstore.pack_content(text)
        self.assertIsInstance(blob, bytes)
        self.assertLess(len(blob), len(text.encode("utf-8")),
                        "压缩后应当变小")
        self.assertEqual(textstore.unpack_content(blob), text)

    def test_empty_and_oversized_are_not_stored(self):
        self.assertIsNone(textstore.pack_content(""))
        self.assertIsNone(textstore.pack_content(None))
        huge = "字" * (textstore.MAX_STORED + 1)
        self.assertIsNone(textstore.pack_content(huge),
                          "超长正文不存，检索时回退现抽")

    def test_corrupt_blob_returns_none(self):
        self.assertIsNone(textstore.unpack_content(b"not zlib at all"))

    def test_pages_round_trip(self):
        self.assertEqual(textstore.unpack_pages(textstore.pack_pages([0, 10, 20])),
                         [0, 10, 20])
        self.assertIsNone(textstore.pack_pages([]))
        self.assertEqual(textstore.unpack_pages(None), [])
        self.assertEqual(textstore.unpack_pages("坏数据"), [])


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-store-")
        self.dbdir = tempfile.mkdtemp(prefix="docseek-store-db-")
        self.cfg = dict(config.DEFAULTS, workers=1, ocr_enabled=False,
                        ocr_images=False, roots=[self.tmp])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def db_path(self):
        return os.path.join(self.dbdir, "index.db")


class StoredContentTest(StorageCase):
    def test_index_stores_content_and_pages(self):
        path = self.write("资料.txt", "人民代表大会 在这里\n")
        index = Index(self.db_path())
        Builder(index, self.cfg).run(roots=[self.tmp], sweep=False)
        row = index.get_doc(path)
        self.assertIsNotNone(row["content"], "建索引时应当存下正文")
        self.assertIn("人民代表大会", textstore.unpack_content(row["content"]))
        self.assertTrue(index.get_meta("content_version"))
        index.close()

    def test_search_reads_stored_content_without_extracting(self):
        """关键回归：搜索阶段不该再调用抽取后端。"""
        self.write("a.txt", "人民代表大会 出现在这里\n")
        self.write("b.txt", "人民代表大会 也在这一份\n")
        index = Index(self.db_path())
        Builder(index, self.cfg).run(roots=[self.tmp], sweep=False)

        searcher = Searcher(index, dict(self.cfg))
        with mock.patch.object(search_module(), "extract",
                               side_effect=AssertionError("不该再抽取！")):
            results, _query, _elapsed = searcher.search("人民代表大会")
        self.assertTrue(results, "应当命中")
        self.assertTrue(all(r.snippets for r in results))
        index.close()

    def test_old_index_without_content_column_is_migrated(self):
        """老索引库（没有 content/pages 列）打开时要能自动补列。"""
        path = self.db_path()
        raw = sqlite3.connect(path)
        raw.executescript("""
            CREATE TABLE docs (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT,
                ext TEXT, kind TEXT, size INTEGER, mtime REAL, ino INTEGER,
                dev INTEGER, chars INTEGER DEFAULT 0, state TEXT DEFAULT 'ok',
                err TEXT, backend TEXT, warning TEXT, indexed_at REAL);
            CREATE VIRTUAL TABLE fts USING fts5(tokens, detail=none);
        """)
        raw.execute("INSERT INTO docs (path, name, ext, state) VALUES (?,?,?,?)",
                    ("/tmp/old.txt", "old.txt", "txt", "ok"))
        raw.commit()
        raw.close()

        index = Index(path)                  # 打开即迁移
        columns = {row[1] for row in index.conn.execute("PRAGMA table_info(docs)")}
        self.assertIn("content", columns)
        self.assertIn("pages", columns)
        self.assertTrue(index.content_stale(),
                        "老库缺正文 → 需要回填一次")
        index.mark_content_current()
        self.assertFalse(index.content_stale())
        index.close()

    def test_old_rows_are_refilled_by_next_run(self):
        """回填：老记录（无正文）在下次建索引时应被重新抽取并存入正文。"""
        path = self.write("资料.txt", "人民代表大会 制度\n")
        index = Index(self.db_path())
        Builder(index, self.cfg).run(roots=[self.tmp], sweep=False)
        # 模拟"老库"：清掉正文并把版本标记抹掉
        index.conn.execute("UPDATE docs SET content = NULL, pages = NULL")
        index.conn.execute("DELETE FROM meta WHERE k = 'content_version'")
        index.conn.commit()
        self.assertTrue(index.content_stale())

        builder = Builder(index, self.cfg)
        state = builder.run(roots=[self.tmp], sweep=False)
        self.assertEqual(state.indexed, 1, "应重新抽取一次以回填正文")
        self.assertIsNotNone(index.get_doc(path)["content"])
        index.close()

    def test_second_run_after_backfill_is_unchanged(self):
        self.write("资料.txt", "人民代表大会 制度\n")
        index = Index(self.db_path())
        Builder(index, self.cfg).run(roots=[self.tmp], sweep=False)
        state = Builder(index, self.cfg).run(roots=[self.tmp], sweep=False)
        self.assertEqual(state.unchanged, 1)
        self.assertEqual(state.indexed, 0)
        index.close()


def search_module():
    from docseek import search
    return search


if __name__ == "__main__":
    unittest.main()
