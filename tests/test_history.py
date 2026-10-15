"""搜索历史的单元测试（纯逻辑，不需要界面）。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import history                       # noqa: E402


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docseek-history-"))
        self._original = history.HISTORY_FILE
        history.HISTORY_FILE = self.tmp / "history.json"

    def tearDown(self):
        history.HISTORY_FILE = self._original
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)


class BasicTest(HistoryCase):
    def test_add_and_entries_most_recent_first(self):
        history.add("甲")
        history.add("乙")
        self.assertEqual(history.entries(), ["乙", "甲"])

    def test_repeat_moves_to_front_and_counts(self):
        history.add("甲")
        history.add("乙")
        history.add("甲")
        self.assertEqual(history.entries(), ["甲", "乙"])
        detail = {item["query"]: item for item in history.detailed()}
        self.assertEqual(detail["甲"]["count"], 2)
        self.assertEqual(detail["乙"]["count"], 1)

    def test_empty_and_whitespace_ignored(self):
        history.add("")
        history.add("   ")
        self.assertEqual(history.entries(), [])

    def test_query_is_trimmed_and_capped(self):
        history.add("  " + "x" * 500 + "  ")
        self.assertEqual(len(history.entries()[0]), history.MAX_QUERY_LEN)

    def test_remove_one(self):
        history.add("甲")
        history.add("乙")
        history.remove("甲")
        self.assertEqual(history.entries(), ["乙"])

    def test_clear(self):
        history.add("甲")
        history.add("乙")
        history.clear()
        self.assertEqual(history.entries(), [])
        self.assertEqual(history.count(), 0)

    def test_max_items_enforced(self):
        for index in range(history.MAX_ITEMS + 15):
            history.add("查询%d" % index)
        entries = history.entries()
        self.assertEqual(len(entries), history.MAX_ITEMS)
        self.assertEqual(entries[0], "查询%d" % (history.MAX_ITEMS + 14),
                         "最新的应排在最前")
        self.assertNotIn("查询0", entries, "最老的应被挤掉")


class RobustnessTest(HistoryCase):
    def test_corrupt_file_is_tolerated(self):
        history.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(history.HISTORY_FILE, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        self.assertEqual(history.entries(), [])
        history.add("甲")
        self.assertEqual(history.entries(), ["甲"])

    def test_legacy_plain_list_format_is_accepted(self):
        with open(history.HISTORY_FILE, "w", encoding="utf-8") as handle:
            json.dump(["旧格式一", "旧格式二"], handle, ensure_ascii=False)
        self.assertEqual(history.entries(), ["旧格式一", "旧格式二"])

    def test_write_is_atomic(self):
        history.add("甲")
        with open(history.HISTORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["items"][0]["query"], "甲")
        leftovers = [p.name for p in history.HISTORY_FILE.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "不应留下临时文件")


class RelativeTimeTest(HistoryCase):
    def test_buckets(self):
        now = time.time()
        self.assertEqual(history.relative_time(now - 5), "刚刚")
        self.assertEqual(history.relative_time(now - 300), "5 分钟前")
        self.assertEqual(history.relative_time(now - 7200), "2 小时前")
        self.assertEqual(history.relative_time(now - 86400 * 3), "3 天前")
        self.assertEqual(history.relative_time(0), "")
        self.assertRegex(history.relative_time(now - 86400 * 90), r"^\d{4}-")


if __name__ == "__main__":
    unittest.main()
