"""界面层测试（需要 DISPLAY；无显示环境或没有 GTK 时整体跳过）。

跑法：
    xvfb-run -a python3 -m unittest tests.test_ui -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    HAVE_GTK = True
except (ImportError, ValueError):        # pragma: no cover
    HAVE_GTK = False

from docseek.search import Result, parse_query     # noqa: E402


def tag_ranges(buffer, name):
    """取出某个 tag 在缓冲区里覆盖的所有区间 [(start, end)]。"""
    tag = buffer.get_tag_table().lookup(name)
    ranges = []
    if tag is None:
        return ranges
    it = buffer.get_start_iter()
    while it.forward_to_tag_toggle(tag):
        if it.starts_tag(tag):
            start = it.get_offset()
            end_it = it.copy()
            if not end_it.forward_to_tag_toggle(tag):
                break
            ranges.append((start, end_it.get_offset()))
            it = end_it
    return ranges


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class HighlightOffsetTest(unittest.TestCase):
    """回归：正文前面有【warning】提示时，高亮不能整体错位。

    真实表现是"搜索后高亮的文字不是关键词"——因为 .doc/.wps 这类走 LibreOffice
    的文档会在预览顶部插入一段【libreoffice】提示，而高亮偏移是按纯正文算的。
    """

    def setUp(self):
        from docseek.ui.window import MainWindow
        self.tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "docseek-ui-test-%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        self.window = MainWindow(db_path=os.path.join(self.tmp, "ui-test.db"))
        self.text = "前面有一段普通说明文字，人民代表大会在这里出现，后面还有别的。"
        self.result = Result(doc_id=1, path=os.path.join(self.tmp, "样例.doc"),
                             name="样例.doc", ext="doc", size=123, mtime=0.0)

    def tearDown(self):
        self.window.destroy()
        # 测试自建的临时目录交由 tempfile 语义处理：直接进回收站
        import subprocess
        subprocess.run(["gio", "trash", self.tmp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _tagged_texts(self, name):
        buffer = self.window.textbuffer
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        full = buffer.get_text(start, end, True)
        return [full[s:e] for s, e in tag_ranges(buffer, name)]

    def test_highlight_with_warning_note(self):
        """带【libreoffice】提示时，高亮必须正好落在关键词上。"""
        self.window.query = parse_query("人民代表大会")
        self.window._set_preview_text(self.result, self.text, "libreoffice")

        hits = self._tagged_texts("current") + self._tagged_texts("hit")
        self.assertTrue(hits, "应当至少有一处高亮")
        for chunk in hits:
            self.assertEqual(chunk, "人民代表大会",
                             "高亮的应是关键词本身，实际高亮了 %r" % chunk)

    def test_highlight_without_note(self):
        """没有提示时同样要对齐。"""
        self.window.query = parse_query("人民代表大会")
        self.window._set_preview_text(self.result, self.text, "")
        hits = self._tagged_texts("current") + self._tagged_texts("hit")
        self.assertTrue(hits)
        for chunk in hits:
            self.assertEqual(chunk, "人民代表大会")

    def test_all_occurrences_are_highlighted(self):
        """多处出现要全部高亮。"""
        self.window.query = parse_query("人民代表大会")
        text = "人民代表大会 甲 人民代表大会 乙 人民代表大会"
        self.window._set_preview_text(self.result, text, "libreoffice")
        hits = self._tagged_texts("current") + self._tagged_texts("hit")
        self.assertEqual(len(hits), 3)
        self.assertTrue(all(chunk == "人民代表大会" for chunk in hits))

    def test_loose_match_highlight_is_trimmed(self):
        """字间被插空白的命中，高亮不应把空白也圈进去。"""
        self.window.query = parse_query("人民代表大会")
        self.window._set_preview_text(self.result, "人 民 代 表 大 会", "libreoffice")
        hits = self._tagged_texts("current") + self._tagged_texts("hit")
        self.assertTrue(hits)
        for chunk in hits:
            self.assertFalse(chunk.startswith(" "), "高亮不应以空白开头")
            self.assertFalse(chunk.endswith(" "), "高亮不应以空白结尾")
            self.assertEqual(chunk.replace(" ", ""), "人民代表大会")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 结果列表布局（仿 TextSeek 的多行行样式）与搜索历史面板
# ---------------------------------------------------------------------------

import shutil                                    # noqa: E402
import tempfile                                  # noqa: E402
import time                                      # noqa: E402
from pathlib import Path                         # noqa: E402

from docseek import config, history              # noqa: E402
from docseek.index import Builder, Index         # noqa: E402


def pump(predicate, timeout=20.0):
    """跑 GTK 主循环直到条件成立或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def find_widget(widget, kind):
    """在控件树里递归找第一个指定类型的控件。"""
    if isinstance(widget, kind):
        return widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found = find_widget(child, kind)
            if found is not None:
                return found
    return None


def collect_labels(widget, out=None):
    """把一个容器里的所有 Gtk.Label 文本收集起来。"""
    out = [] if out is None else out
    if isinstance(widget, Gtk.Label):
        out.append(widget.get_text())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            collect_labels(child, out)
    return out


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class ResultsListUiTest(unittest.TestCase):
    """结果行应当同时给出：文件名、路径、命中片段、大小、修改时间。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docseek-ui-list-"))
        (self.tmp / "笔记.txt").write_text(
            "人民代表大会 在这里出现\n人民代表大会 又出现一次\n其他内容\n",
            encoding="utf-8")
        (self.tmp / "另一个.md").write_text(
            "# 说明\n\n本文提到人民代表大会制度。\n", encoding="utf-8")
        self.cfg = dict(config.DEFAULTS, workers=2, ocr_enabled=False,
                        roots=[str(self.tmp)])
        self.db = str(self.tmp / "index.db")
        index = Index(self.db)
        Builder(index, self.cfg).run(roots=[str(self.tmp)])
        index.close()

        self._history_file = history.HISTORY_FILE
        history.HISTORY_FILE = self.tmp / "history.json"

        from docseek.ui.window import MainWindow
        self.window = MainWindow(db_path=self.db, cfg=self.cfg)
        self.window.show_all()

    def tearDown(self):
        history.HISTORY_FILE = self._history_file
        self.window.destroy()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _search(self, text, expect_results=True):
        self.window.search_entry.set_text(text)
        self.window.on_search()
        if expect_results:
            self.assertTrue(pump(lambda: bool(self.window.results_view.rows())),
                            "搜索没有返回结果行")
        else:
            self.assertTrue(
                pump(lambda: "没有匹配结果" in "\n".join(
                    collect_labels(self.window.results_view.listbox))),
                "空结果应给出提示")

    def test_row_shows_name_path_snippet_size_time(self):
        self._search("人民代表大会")
        rows = self.window.results_view.rows()
        self.assertGreaterEqual(len(rows), 1)
        texts = collect_labels(rows[0])
        joined = "\n".join(texts)
        self.assertIn("笔记.txt", joined, "行内应有文件名")
        self.assertIn(str(self.tmp), joined, "行内应有完整路径")
        self.assertIn("人民代表大会", joined, "行内应有命中片段")
        self.assertTrue(any(t.endswith("B") or t.endswith("KB") for t in texts),
                        "行内应有文件大小：%r" % texts)
        self.assertTrue(any("-" in t and ":" in t for t in texts),
                        "行内应有修改时间：%r" % texts)

    def test_no_results_shows_placeholder(self):
        self._search("绝对不存在的词组ZZZ", expect_results=False)
        self.assertEqual(self.window.results_view.rows(), [])
        texts = collect_labels(self.window.results_view.listbox)
        self.assertTrue(any("没有匹配结果" in t for t in texts),
                        "空结果应给出提示，实际：%r" % texts)

    def test_selection_updates_preview(self):
        self._search("人民代表大会")
        rows = self.window.results_view.rows()
        self.window.results_view.listbox.select_row(rows[0])
        self.assertIsNotNone(self.window.results_view.selected_result())
        self.assertTrue(
            pump(lambda: self.window.preview_title.get_text() != ""),
            "选中后预览标题应更新")

    def test_sort_combo_lists_all_modes(self):
        from docseek.search import SORT_MODES
        model = self.window.sort_combo.get_model()
        # ComboBoxText 的模型列是 (显示文案, id)
        labels = [row[0] for row in model]
        ids = [row[1] for row in model]
        self.assertEqual(ids, [mode for mode, _ in SORT_MODES])
        self.assertEqual(labels, [text for _, text in SORT_MODES])
        self.assertEqual(self.window.sort_combo.get_active_id(), "relevance")

    def test_changing_sort_reorders_results(self):
        """切换排序应当立刻重排结果（排序在截断前生效，所以要重跑查询）。"""
        from unittest import mock
        self._search("人民代表大会")
        # 别把测试配置写进用户真实配置文件
        with mock.patch.object(config, "save", lambda *a, **k: None):
            self.window.sort_combo.set_active_id("size_desc")
            self.assertTrue(
                pump(lambda: [r.size for r in self.window.results]
                     == sorted((r.size for r in self.window.results),
                               reverse=True)),
                "结果应按大小降序：%r" % [r.size for r in self.window.results])
            self.assertEqual(self.window.cfg["sort_mode"], "size_desc")
            self.window.sort_combo.set_active_id("mtime_asc")
            self.assertTrue(
                pump(lambda: [r.mtime for r in self.window.results]
                     == sorted(r.mtime for r in self.window.results)),
                "结果应按修改时间升序")

    def test_search_records_history(self):
        self._search("人民代表大会")
        self.assertIn("人民代表大会", history.entries())

    def test_toggle_preview_gives_full_width_list(self):
        """Ctrl+P 收起预览后，结果列表应当通栏（对齐 TextSeek 的观感）。"""
        self._search("人民代表大会")
        before = self.window.results_view.get_allocated_width()
        self.window.on_toggle_preview()
        pump(lambda: False, timeout=0.3)
        self.assertFalse(self.window.preview_pane.get_visible())
        self.assertGreater(self.window.results_view.get_allocated_width(), before)
        self.window.on_toggle_preview()
        pump(lambda: False, timeout=0.3)
        self.assertTrue(self.window.preview_pane.get_visible())
        self.assertEqual(self.window.results_view.get_allocated_width(), before,
                         "恢复后分栏位置应还原")


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class HistoryPanelUiTest(unittest.TestCase):
    """历史面板：列出记录、逐条删除、一键清空、↑↓ 翻历史。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docseek-ui-hist-"))
        self.cfg = dict(config.DEFAULTS, worker=1, roots=[str(self.tmp)])
        self._history_file = history.HISTORY_FILE
        history.HISTORY_FILE = self.tmp / "history.json"
        from docseek.ui.window import MainWindow
        self.window = MainWindow(db_path=str(self.tmp / "index.db"),
                                 cfg=self.cfg)
        self.window.show_all()

    def tearDown(self):
        history.HISTORY_FILE = self._history_file
        self.window.destroy()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_panel_lists_entries_and_clear_button(self):
        history.add("甲")
        history.add("乙")
        self.window._rebuild_history_panel()
        box = self.window.history_box
        texts = collect_labels(box)
        self.assertIn("最近搜索", texts)
        self.assertTrue(any("甲" in t for t in texts), "面板应列出历史：%r" % texts)
        self.assertTrue(any("乙" in t for t in texts))
        buttons = []

        def walk(widget):
            if isinstance(widget, Gtk.Button):
                buttons.append(widget)
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    walk(child)

        walk(box)
        tooltips = [b.get_tooltip_text() or "" for b in buttons]
        self.assertTrue(any("清空" in t for t in tooltips),
                        "应有清空按钮：%r" % tooltips)
        self.assertTrue(any("删除这条" in t for t in tooltips),
                        "每条应有删除按钮：%r" % tooltips)

    def test_remove_single_entry(self):
        history.add("要删的")
        history.add("保留的")
        self.window._rebuild_history_panel()
        self.window.on_history_remove(None, "要删的")
        self.assertNotIn("要删的", history.entries())
        self.assertIn("保留的", history.entries())
        self.assertNotIn("要删的",
                         "\n".join(collect_labels(self.window.history_box)))

    def test_clear_all_entries(self):
        history.add("甲")
        history.add("乙")
        self.window.on_history_clear()
        self.assertEqual(history.entries(), [])
        self.assertIn("还没有搜索记录",
                      "\n".join(collect_labels(self.window.history_box)))

    def test_arrow_keys_walk_history(self):
        from gi.repository import Gdk
        history.add("旧")
        history.add("新")

        class Key:
            def __init__(self, keyval):
                self.keyval = keyval

        self.window._history_index = -1
        self.window.on_entry_key(self.window.search_entry, Key(Gdk.KEY_Up))
        self.assertEqual(self.window.search_entry.get_text(), "新")
        self.window.on_entry_key(self.window.search_entry, Key(Gdk.KEY_Up))
        self.assertEqual(self.window.search_entry.get_text(), "旧")
        self.window.on_entry_key(self.window.search_entry, Key(Gdk.KEY_Down))
        self.assertEqual(self.window.search_entry.get_text(), "新")
        self.window.on_entry_key(self.window.search_entry, Key(Gdk.KEY_Down))
        self.assertEqual(self.window.search_entry.get_text(), "",
                         "再往下应回到空")

@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class HistoryDropdownTest(unittest.TestCase):
    """搜索历史做成搜索框右侧的下拉箭头（不是靠上下方向键）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docseek-ui-drop-"))
        self.cfg = dict(config.DEFAULTS, roots=[str(self.tmp)])
        self._history_file = history.HISTORY_FILE
        history.HISTORY_FILE = self.tmp / "history.json"
        from docseek.ui.window import MainWindow
        self.window = MainWindow(db_path=str(self.tmp / "index.db"), cfg=self.cfg)
        self.window.show_all()
        pump(lambda: self.window.get_allocated_width() > 0, timeout=5)

    def tearDown(self):
        history.HISTORY_FILE = self._history_file
        self.window.destroy()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_arrow_sits_next_to_entry_as_one_control(self):
        children = self.window.search_box.get_children()
        self.assertIn(self.window.search_entry, children)
        self.assertIn(self.window.history_button, children)
        self.assertIsInstance(self.window.history_button, Gtk.MenuButton)
        self.assertTrue(
            self.window.search_box.get_style_context().has_class("linked"),
            "输入框与箭头应当用 linked 样式连成一体（下拉框观感）")

    def test_arrow_opens_dropdown_with_history(self):
        history.add("甲")
        history.add("乙")
        self.window.history_button.set_active(True)      # 等价于点箭头
        self.assertTrue(pump(lambda: self.window.history_popover.get_visible(),
                             timeout=5), "下拉应当展开")
        labels = "\n".join(collect_labels(self.window.history_box))
        self.assertIn("最近搜索", labels)
        self.assertIn("甲", labels)
        self.assertIn("乙", labels)
        self.window.history_button.set_active(False)

    def test_dropdown_width_follows_search_box(self):
        history.add("某个比较长的搜索关键词")
        self.window.history_button.set_active(True)
        pump(lambda: self.window.history_popover.get_visible(), timeout=5)
        width = self.window.history_box.get_size_request()[0]
        box_width = self.window.search_box.get_allocated_width()
        self.assertGreater(width, 200, "下拉不该窄成一条")
        self.assertLessEqual(width, box_width + 1,
                             "下拉宽度不应超过搜索框（否则不像下拉）")
        self.window.history_button.set_active(False)

    def test_picking_entry_fills_search_and_runs(self):
        history.add("人民代表大会")
        seen = {}
        self.window.on_search = lambda *a, **k: seen.setdefault(
            "query", self.window.search_entry.get_text())
        self.window.on_history_pick(None, "人民代表大会")
        self.assertEqual(self.window.search_entry.get_text(), "人民代表大会")
        self.assertEqual(seen.get("query"), "人民代表大会",
                         "选中历史条目后应当直接触发搜索")

    def test_activating_row_runs_that_query(self):
        history.add("甲")
        self.window._rebuild_history_panel()
        # ScrolledWindow 里会被包一层 Viewport，所以要递归找 ListBox
        inner = find_widget(self.window.history_box, Gtk.ListBox)
        self.assertIsNotNone(inner)
        rows = inner.get_children()
        self.assertTrue(rows)
        seen = {}
        self.window.on_history_pick = lambda _b, q: seen.setdefault("q", q)
        inner.emit("row-activated", rows[0])
        self.assertEqual(seen.get("q"), "甲")

    def test_empty_state_in_dropdown(self):
        history.clear()
        self.window.history_button.set_active(True)
        pump(lambda: self.window.history_popover.get_visible(), timeout=5)
        labels = "\n".join(collect_labels(self.window.history_box))
        self.assertIn("还没有搜索记录", labels)
        self.window.history_button.set_active(False)
