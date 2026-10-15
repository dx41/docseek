"""结果排序的测试。

排序要在**截断之前**生效，所以核心逻辑放在 search.sort_results，由 Searcher 调用；
这里既测纯逻辑，也测"取前 N 条"确实是按排序后的顺序取。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import config                          # noqa: E402
from docseek.index import Builder, Index            # noqa: E402
from docseek.search import (SORT_LABELS, SORT_MODES, Result,   # noqa: E402
                            Searcher, sort_results)


def make_result(path, size=100, mtime=1000.0, ext=None, matches=1, name=None):
    return Result(doc_id=0, path=path, name=name or os.path.basename(path),
                  ext=ext if ext is not None else path.rsplit(".", 1)[-1],
                  size=size, mtime=mtime, matches=matches)


class SortLogicTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            make_result("/b/第二个.docx", size=300, mtime=2000.0, ext="docx"),
            make_result("/a/第一个.pdf", size=100, mtime=3000.0, ext="pdf"),
            make_result("/a/第三个.txt", size=200, mtime=1000.0, ext="txt"),
        ]

    def names(self, mode):
        return [r.name for r in sort_results(self.items, mode)]

    def test_all_modes_are_declared_with_labels(self):
        ids = [mode for mode, _ in SORT_MODES]
        self.assertEqual(ids[0], "relevance", "默认应当是相关度")
        for mode in ("relevance", "mtime_desc", "mtime_asc", "size_desc",
                     "size_asc", "dir", "type"):
            self.assertIn(mode, ids)
            self.assertTrue(SORT_LABELS[mode])

    def test_relevance_keeps_input_order(self):
        self.assertEqual(self.names("relevance"),
                         ["第二个.docx", "第一个.pdf", "第三个.txt"])

    def test_size(self):
        self.assertEqual(self.names("size_desc"),
                         ["第二个.docx", "第三个.txt", "第一个.pdf"])
        self.assertEqual(self.names("size_asc"),
                         ["第一个.pdf", "第三个.txt", "第二个.docx"])

    def test_mtime(self):
        self.assertEqual(self.names("mtime_desc"),
                         ["第一个.pdf", "第二个.docx", "第三个.txt"])
        self.assertEqual(self.names("mtime_asc"),
                         ["第三个.txt", "第二个.docx", "第一个.pdf"])

    def test_dir_groups_by_folder(self):
        self.assertEqual(self.names("dir"),
                         ["第一个.pdf", "第三个.txt", "第二个.docx"])

    def test_type_groups_by_extension(self):
        self.assertEqual(self.names("type"),
                         ["第二个.docx", "第一个.pdf", "第三个.txt"])

    def test_unknown_mode_falls_back_to_input_order(self):
        self.assertEqual(self.names("不存在的排序"), self.names("relevance"))

    def test_does_not_mutate_input(self):
        original = list(self.items)
        sort_results(self.items, "size_desc")
        self.assertEqual(self.items, original)

    def test_empty_list(self):
        self.assertEqual(sort_results([], "size_desc"), [])


class SearcherSortTest(unittest.TestCase):
    """端到端：排序应当在 limit 截断之前生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-sort-")
        self.dbdir = tempfile.mkdtemp(prefix="docseek-sort-db-")
        self.write("小.txt", "人民代表大会 小文件\n")
        self.write("大.txt", "人民代表大会 大文件" + "补充内容" * 400 + "\n")
        self.write("子目录/中.txt", "人民代表大会 中等文件" + "内容" * 40 + "\n")
        self.cfg = dict(config.DEFAULTS, workers=1, ocr_enabled=False,
                        ocr_images=False, roots=[self.tmp])
        self.index = Index(os.path.join(self.dbdir, "index.db"))
        Builder(self.index, self.cfg).run(roots=[self.tmp], sweep=False)
        self.searcher = Searcher(self.index, dict(self.cfg))

    def tearDown(self):
        self.index.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    def write(self, relpath, text):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_size_desc_before_truncation(self):
        results, _query, _elapsed = self.searcher.search(
            "人民代表大会", limit=1, sort="size_desc")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "大.txt",
                         "取前一条时应当是最大的那份，而不是相关度最高的")

    def test_size_asc_order(self):
        results, _q, _e = self.searcher.search("人民代表大会", sort="size_asc")
        sizes = [r.size for r in results]
        self.assertEqual(sizes, sorted(sizes))

    def test_dir_order_groups_folder(self):
        results, _q, _e = self.searcher.search("人民代表大会", sort="dir")
        dirs = [os.path.dirname(r.path) for r in results]
        self.assertEqual(dirs, sorted(dirs, key=str.lower))

    def test_default_is_relevance(self):
        with_default, _q, _e = self.searcher.search("人民代表大会")
        explicit, _q2, _e2 = self.searcher.search("人民代表大会",
                                                  sort="relevance")
        self.assertEqual([r.path for r in with_default],
                         [r.path for r in explicit])


if __name__ == "__main__":
    unittest.main()
