"""界面层测试（需要 DISPLAY；无显示环境或没有 GTK 时整体跳过）。

跑法：
    xvfb-run -a python3 -m unittest tests.test_ui -v
"""

import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    HAVE_GTK = True
except (ImportError, ValueError):        # pragma: no cover
    HAVE_GTK = False

from docseek.search import Result, parse_query     # noqa: E402


_launch_patch = None
_reveal_patch = None


def setUpModule():
    """所有 UI 测试一律不许真的启动外部程序。

    否则测试里"双击打开"会真的调起文本编辑器（本机是 pluma），既慢又会在
    xvfb 退出时报 X 连接错误。

    同时必须挡住"在文件管理器中显示"这条 **D-Bus 路径**：它走会话总线，会把真实
    桌面上的文件管理器（本机 peony）激活并弹窗——xvfb 拦不住，用户看到的现象就是
    "跑测试时桌面上冒出一堆文件夹窗口，测试完也没人关"。
    """
    global _launch_patch, _reveal_patch
    import docseek.ui.window as window_module
    _launch_patch = mock.patch.object(window_module, "_launch_uri",
                                      return_value=True)
    _launch_patch.start()
    _reveal_patch = mock.patch.object(window_module, "_reveal_in_manager",
                                      return_value=None)
    _reveal_patch.start()


def tearDownModule():
    for patch in (_reveal_patch, _launch_patch):
        if patch is not None:
            patch.stop()


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
def _remove_files(*paths):
    """删掉文件及其 SQLite 兄弟（-wal/-shm）；清理用，不存在不算错。"""
    for path in paths:
        for candidate in (path, path + "-wal", path + "-shm"):
            try:
                os.unlink(candidate)
            except OSError:
                pass


def _remove_tree(path):
    """删掉测试自建的临时目录：本机约定不做物理删除，一律进回收站。

    和 test_core.py 的 `_discard` 同一套路：`gio trash` 不在就留在原地，
    绝不 `rm -rf`/`shutil.rmtree`。
    """
    path = str(path)
    if not os.path.exists(path):
        return
    gio = shutil.which("gio")
    if not gio:
        return
    try:
        subprocess.run([gio, "trash", "--", path], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


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
        self.addCleanup(_remove_tree, self.tmp)
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


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class ResultActionsTest(unittest.TestCase):
    """结果行的动作必须真的有效：双击打开、右键菜单六项。

    回归背景：`_open` / `_copy` 只有调用没有定义，双击和右键菜单点了**毫无反应**
    （异常被 GTK 吞进 stderr，界面上看不到任何提示）。这里既验证"动作能跑"，
    也验证"出错会显示在状态栏"。
    """

    def setUp(self):
        from unittest import mock                        # noqa: F401
        from docseek.ui.window import MainWindow
        self.tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "docseek-actions-%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        self.addCleanup(_remove_tree, self.tmp)
        self.path = os.path.join(self.tmp, "被打开的文件.txt")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("内容\n")
        self.window = MainWindow(db_path=os.path.join(self.tmp, "actions.db"))
        self.result = Result(doc_id=1, path=self.path, name="被打开的文件.txt",
                             ext="txt", size=6, mtime=0.0)

    def tearDown(self):
        self.window.destroy()

    def _patch_launch(self):
        from unittest import mock
        import docseek.ui.window as window_module
        # 别真的去启动系统程序（否则会在桌面上弹出窗口）
        return mock.patch.object(window_module, "_launch_uri", return_value=True)

    def test_all_result_actions_exist(self):
        """菜单和双击用到的每个方法都必须存在（少一个就是"点了没反应"）。"""
        for name in ("_open", "_reveal", "_copy", "_run_action",
                     "on_result_activated", "on_result_context_menu"):
            self.assertTrue(callable(getattr(self.window, name, None)),
                            "界面缺少方法 %s" % name)

    def test_activate_opens_file(self):
        with self._patch_launch() as launch:
            self.window.on_result_activated(self.result)
        self.assertTrue(launch.called, "双击应当去启动默认程序")
        self.assertIn("已打开", self.window.status_label.get_text())

    def test_open_missing_file_reports_instead_of_crashing(self):
        self.assertFalse(self.window._open(os.path.join(self.tmp, "没有这个文件")))
        self.assertIn("文件不存在", self.window.status_label.get_text())

    def test_copy_puts_text_on_clipboard(self):
        from gi.repository import Gdk, Gtk
        self.assertTrue(self.window._copy("/tmp/某个路径.txt"))
        self.assertIn("已复制", self.window.status_label.get_text())
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.assertEqual(clipboard.wait_for_text(), "/tmp/某个路径.txt")

    def test_context_menu_items_all_work(self):
        """六个菜单项逐个触发：不能抛异常，且状态栏要给出反馈。"""
        from unittest import mock
        menus = []
        with mock.patch.object(Gtk.Menu, "popup_at_pointer",
                               lambda self, event: menus.append(self)), \
                self._patch_launch():
            self.assertTrue(self.window.on_result_context_menu(self.result, None))
        menu = menus[0]
        items = [child for child in menu.get_children()
                 if isinstance(child, Gtk.MenuItem)]
        labels = [item.get_label() for item in items]
        self.assertEqual(labels, ["打开文件", "在文件管理器中显示", "打开所在目录",
                                  "复制路径", "复制文件名", "重新加载预览"])
        for item in items:
            self.window.status_label.set_text("")
            item.activate()                     # 出错会被护栏写进状态栏
            text = self.window.status_label.get_text()
            self.assertTrue(text, "菜单项「%s」没有任何反馈" % item.get_label())
            self.assertNotIn("失败：", text,
                             "菜单项「%s」出错：%s" % (item.get_label(), text))

    def test_reveal_does_not_touch_real_file_manager(self):
        """「在文件管理器中显示」必须走可替换的模块级函数。

        回归背景：它原来是内联的 D-Bus 调用（org.freedesktop.FileManager1），
        测试里替换不掉，于是每次跑测试都会把真实桌面上的 peony 激活、弹出一个
        文件夹窗口，而且没人去关。
        """
        import docseek.ui.window as window_module
        self.assertTrue(os.path.exists(self.path))
        with mock.patch.object(window_module, "_reveal_in_manager") as reveal, \
                mock.patch.dict(os.environ):
            # tests/run.sh 会设 DOCSEEK_NO_LAUNCH=1，这里要验证的是"没被禁止时"
            # 走哪条出路，所以先把它摘掉（patch.dict 会在退出时还原）。
            os.environ.pop("DOCSEEK_NO_LAUNCH", None)
            self.assertTrue(self.window._reveal(self.path))
        reveal.assert_called_once_with(self.path)
        self.assertIn("已在文件管理器中定位", self.window.status_label.get_text())

    def test_reveal_is_blocked_when_launching_is_disabled(self):
        """DOCSEEK_NO_LAUNCH=1 时连 D-Bus 都不许碰（D-Bus 会弹真实窗口）。"""
        import docseek.ui.window as window_module
        with mock.patch.object(window_module, "_reveal_in_manager") as reveal, \
                mock.patch.dict(os.environ, {"DOCSEEK_NO_LAUNCH": "1"}):
            self.assertFalse(self.window._reveal(self.path))
        self.assertFalse(reveal.called, "被禁止时不许调用文件管理器")
        self.assertIn("已跳过外部打开", self.window.status_label.get_text())

    def test_failing_action_is_reported_not_swallowed(self):
        """护栏：动作内部抛异常时必须显示在状态栏（而不是静默无反应）。"""
        def boom():
            raise RuntimeError("故意炸一个")
        self.assertFalse(self.window._run_action("测试动作", boom))
        text = self.window.status_label.get_text()
        self.assertIn("测试动作", text)
        self.assertIn("故意炸一个", text)


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class NoMissingHandlersTest(unittest.TestCase):
    """界面里每一个 `self.xxx()` 调用都必须在实例上真实存在。

    回归背景：`self._open(...)` / `self._copy(...)` 被调用但方法压根没定义，
    双击与右键菜单因此"点了毫无反应"（异常被 GTK 吞掉）。
    这里用静态扫描 + 真实实例双重验证，把这一类问题一次挡死。
    """

    def _called_attrs(self, tree, cls):
        """取出某个类里所有 self.xxx() 的 xxx。"""
        import ast
        names = set()
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "self":
                names.add(node.func.attr)
        return names

    def test_every_self_call_exists(self):
        import ast
        from docseek.ui import window as window_module
        from docseek.ui.window import MainWindow
        source = window_module.__file__
        with open(source, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), source)

        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertTrue(classes, "没扫到任何类，检查一下实现")

        handlers_db = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "handlers-%d.db" % os.getpid())
        self.addCleanup(_remove_files, handlers_db)
        parent = MainWindow(db_path=handlers_db)
        instances = {}
        dialog = None
        try:
            instances["MainWindow"] = parent
            dialog = window_module.SettingsDialog(parent, {"roots": ["/tmp"]})
            instances["SettingsDialog"] = dialog
            for cls in classes:
                called = self._called_attrs(tree, cls)
                instance = instances.get(cls.name)
                if instance is None:
                    continue          # 其它辅助类不在这里实例化
                missing = sorted(name for name in called
                                 if not hasattr(instance, name))
                self.assertEqual(
                    missing, [],
                    "%s 调用了不存在的方法，点了会毫无反应：%s"
                    % (cls.name, missing))
        finally:
            if dialog is not None:
                dialog.destroy()
            parent.destroy()


@unittest.skipUnless(HAVE_GTK, "需要 GTK3 与 PyGObject")
@unittest.skipUnless(os.environ.get("DISPLAY"), "需要显示环境（可用 xvfb-run）")
class PreviewStagingTest(unittest.TestCase):
    """预览必须"正文先出来，版面图后出来"。

    回归背景：以前要"读正文 + 转换 + 渲染"全做完才更新界面，索引运行时一份 .doc
    的转换会排到 60 秒以上，界面就死死停在"正在读取正文……"，看着像坏了。
    """

    def setUp(self):
        import docseek.ui.window as window_module
        from docseek.ui.window import MainWindow
        from docseek.index import Index
        from docseek import textstore
        self.tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "docseek-preview-%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        self.addCleanup(_remove_tree, self.tmp)
        self.path = os.path.join(self.tmp, "预演.txt")
        self.body = "人民代表大会 在这里\n" * 5
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(self.body)
        self.db = os.path.join(self.tmp, "preview.db")
        index = Index(self.db)
        index.upsert({"path": self.path, "name": "预演.txt", "ext": "txt",
                      "kind": "text", "size": os.path.getsize(self.path),
                      "mtime": os.stat(self.path).st_mtime, "chars": len(self.body)},
                     tokens="预演 人民代表大会", state="ok",
                     content=textstore.pack_content(self.body))
        index.commit()
        index.close()
        self.window = MainWindow(db_path=self.db)
        self.result = Result(doc_id=1, path=self.path, name="预演.txt", ext="txt",
                             size=os.path.getsize(self.path), mtime=0.0)
        self.window_module = window_module

    def tearDown(self):
        self.window.destroy()

    def _text(self):
        buffer = self.window.textbuffer
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _pump_until(self, predicate, timeout):
        import time as _time
        end = _time.time() + timeout
        while _time.time() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            if predicate():
                return True
            _time.sleep(0.02)
        return False

    def test_text_appears_without_waiting_for_page_render(self):
        """版面渲染慢 2 秒时，正文也要立刻出现（来自库里存的正文）。"""
        calls = {"render": 0}

        def slow_render(path, needle, dpi=None):
            calls["render"] += 1
            import time as _time
            _time.sleep(2.0)
            return None, None

        status_when_text_showed = {"text": None}
        with mock.patch.object(self.window_module.preview, "render_match",
                               side_effect=slow_render):
            started = __import__("time").time()
            self.window.on_result_selected(self.result)
            appeared = self._pump_until(
                lambda: "人民代表大会" in self._text()
                and not status_when_text_showed.update(
                    text=self.window.status_label.get_text()), timeout=1.0)
            elapsed = __import__("time").time() - started
        self.assertTrue(appeared, "正文没有在渲染完成前显示出来（用时 %.2fs）" % elapsed)
        self.assertLess(elapsed, 1.0, "正文显示耗时 %.2fs，说明还在等渲染" % elapsed)
        # 正文出现时状态栏必须已经离开"正在读取正文……"（那是卡住时的表现）
        self.assertNotIn("正在读取正文", status_when_text_showed["text"] or "",
                         "正文显示了但状态还停在读取中")

    def test_page_label_reports_render_progress(self):
        with mock.patch.object(self.window_module.preview, "render_match",
                               return_value=(None, None)):
            self.window.on_result_selected(self.result)
            self._pump_until(
                lambda: "预览完成" in self.window.status_label.get_text()
                or "无法生成" in self.window.page_label.get_text(), timeout=5)
        self.assertTrue(self.window.page_label.get_text(),
                        "版面区域应当有状态说明，不能是空白")

    def test_stale_preview_result_is_ignored(self):
        """切到另一条结果后，旧请求回来的正文不能覆盖新预览。"""
        with mock.patch.object(self.window_module.preview, "render_match",
                               return_value=(None, None)):
            self.window.on_result_selected(self.result)
            self.window._preview_token += 1          # 模拟用户已经切走
            self.assertFalse(self.window._finish_text(
                self.window._preview_token - 1, self.result, "过期的正文", None))
            self._pump_until(lambda: "人民代表大会" in self._text(), timeout=3)
            self.assertNotIn("过期的正文", self._text())


class CheckDepsButtonTest(unittest.TestCase):
    """设置对话框里的"检查依赖"按钮必须真能跑通（别再出现引用不存在的方法）。"""

    def _dialog(self):
        import docseek.ui.window as window_module
        parent = window_module.Gtk.Window()
        return window_module, window_module.SettingsDialog(parent, {"roots": []})

    def test_button_exists_in_action_area(self):
        module, dialog = self._dialog()
        labels = []
        for child in dialog.get_action_area().get_children():
            if isinstance(child, module.Gtk.Button):
                labels.append(child.get_label() or "")
        self.assertIn("检查依赖", labels)

    def test_handler_opens_report_without_crashing(self):
        module, dialog = self._dialog()
        captured = {}

        def fake_run(self):
            captured["text"] = self.get_property("text")
            captured["secondary"] = self.get_property("secondary-text")
            return module.Gtk.ResponseType.OK

        with mock.patch.object(module.Gtk.MessageDialog, "run", fake_run), \
                mock.patch.object(module.Gtk.MessageDialog, "destroy"):
            dialog._on_check_deps()

        self.assertIn("运行依赖自检", captured["text"] + captured["secondary"])
