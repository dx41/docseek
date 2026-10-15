"""DocSeek 主窗口。

三栏式：搜索框 → 结果列表（内联命中片段）→ 预览（全文高亮 / 版面页图 / 命中清单）。

线程模型：GTK 只能在主线程碰控件，因此搜索、抽取、渲染都放到后台线程，
结果通过 GLib.idle_add 回到主线程更新界面。SQLite 连接不跨线程复用。
"""

import os
import shutil
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

from .. import (config, history, preview, procs, textstore,   # noqa: E402
                tokenizer)
from ..extract import ExtractError, extract, ocr   # noqa: E402
from ..index import Builder, Index                # noqa: E402
from ..search import (SORT_MODES, DEFAULT_SORT, Searcher,   # noqa: E402
                      find_spans)
from .results import ResultsList                  # noqa: E402

try:                                              # 代码高亮（装了就用）
    gi.require_version("GtkSource", "3.0")
    from gi.repository import GtkSource           # noqa: E402
    HAVE_SOURCE = True
except (ValueError, ImportError):                 # pragma: no cover
    GtkSource = None
    HAVE_SOURCE = False

TEXT_PREVIEW_LIMIT = 1500000      # 全文视图最多装载的字符数
RESULT_ROW_LIMIT = 800            # 列表最多渲染多少行（避免一次塞几万行）


def _launch_uri(uri):
    """交给系统默认程序打开（单独抽出来，测试时可替换，避免真的弹出窗口）。"""
    return Gio.AppInfo.launch_default_for_uri(uri, None)


class MainWindow(Gtk.Window):
    def __init__(self, db_path=None, cfg=None):
        Gtk.Window.__init__(self, title="DocSeek 文档内容搜索")
        self.set_default_size(1360, 840)
        self.db_path = db_path or str(config.INDEX_DB)
        self.cfg = cfg or config.load()
        self.index = Index(db_path)
        self.results = []
        self.query = None
        self._search_token = 0
        self._preview_token = 0
        self._stop_index = threading.Event()
        self._indexing = False
        self._builder = None
        self._history_index = -1        # 搜索框里 ↑/↓ 翻历史时的游标

        self._build_header()
        self._build_body()
        self._build_status()
        self.connect("key-press-event", self.on_key_press)
        self._refresh_summary()
        GLib.idle_add(self._first_run_hint)

    def _first_run_hint(self):
        """首次启动（没有索引目录、库里也没有文档）时直接打开设置，省得找不到入口。"""
        if not (self.cfg.get("roots") or []) and self.index.count() == 0:
            self.status_label.set_text("先选择要索引的目录，然后点左上角 ⟳ 建立索引")
            self.on_settings()
        return False

    # ================================================================ 界面

    def _build_header(self):
        bar = Gtk.HeaderBar()
        bar.set_show_close_button(True)
        bar.set_title("DocSeek")
        bar.set_subtitle("本地文档内容搜索")
        self.set_titlebar(bar)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            '搜索正文…… 支持 "短语" ext:pdf  path:目录  -排除  re:正则')
        self.search_entry.set_width_chars(44)
        self.search_entry.set_tooltip_text("回车搜索；点右侧箭头可下拉选择历史搜索")
        self.search_entry.connect("activate", self.on_search)
        self.search_entry.connect("key-press-event", self.on_entry_key)

        # 搜索历史：做成搜索框右侧的**下拉箭头**，点开就是历史列表（像下拉框那样挑）
        self.history_popover = Gtk.Popover()
        self.history_popover.set_position(Gtk.PositionType.BOTTOM)
        self.history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.history_box.set_margin_top(8)
        self.history_box.set_margin_bottom(8)
        self.history_box.set_margin_start(8)
        self.history_box.set_margin_end(8)
        self.history_popover.add(self.history_box)

        self.history_button = Gtk.MenuButton()
        self.history_button.add(Gtk.Image.new_from_icon_name(
            "pan-down-symbolic", Gtk.IconSize.BUTTON))
        self.history_button.set_tooltip_text("搜索历史（下拉选择；每条可删除，也可清空）")
        self.history_button.set_popover(self.history_popover)

        # 用 linked 样式把"输入框 + 箭头"连成一个整体，观感就是一个下拉框
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_box.get_style_context().add_class("linked")
        search_box.pack_start(self.search_entry, True, True, 0)
        search_box.pack_start(self.history_button, False, False, 0)
        self.search_box = search_box
        bar.set_custom_title(search_box)
        # 每次展开都重建，保证看到的是最新记录
        self.history_popover.connect("show", lambda *_: self._rebuild_history_panel())

        btn_index = Gtk.Button()
        btn_index.set_image(Gtk.Image.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON))
        btn_index.set_tooltip_text("更新索引（F5）")
        btn_index.connect("clicked", self.on_index_clicked)
        self.index_button = btn_index
        bar.pack_start(btn_index)

        btn_settings = Gtk.Button()
        btn_settings.set_image(Gtk.Image.new_from_icon_name(
            "preferences-system-symbolic", Gtk.IconSize.BUTTON))
        btn_settings.set_tooltip_text("索引设置")
        btn_settings.connect("clicked", self.on_settings)
        bar.pack_end(btn_settings)

        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name(
            "open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_popup(self._build_menu())    # GTK3 用 set_popup 挂 Gtk.Menu
        bar.pack_end(menu_button)

    def _build_menu(self):
        menu = Gtk.Menu()

        def item(label, callback):
            entry = Gtk.MenuItem(label=label)
            entry.connect("activate", callback)
            menu.append(entry)
            return entry

        item("索引设置…", self.on_settings)
        item("立即更新索引（F5）", self.on_index_clicked)
        item("重新识别选中的文件（只这一份）", self.on_reocr_selected)
        item("重试之前失败的文件（会跑一轮索引）", self.on_retry_failed)
        item("停止索引", self.on_stop_index)
        menu.append(Gtk.SeparatorMenuItem())
        item("显示/隐藏预览（Ctrl+P）", self.on_toggle_preview)
        item("索引统计", self.on_stats)
        item("清理版面渲染缓存", self.on_clear_cache)
        menu.append(Gtk.SeparatorMenuItem())
        item("关于 DocSeek", self.on_about)
        item("退出", lambda *_: self.destroy())
        menu.show_all()
        return menu

    def _build_body(self):
        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.paned = paned
        self.add(paned)

        # ---------------------------------------------------- 左：结果列表
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.summary_label = Gtk.Label(xalign=0)
        self.summary_label.set_margin_top(6)
        self.summary_label.set_margin_bottom(6)
        self.summary_label.set_margin_start(10)
        self.summary_label.set_margin_end(10)
        self.summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.pack_start(self.summary_label, True, True, 0)
        self.sort_combo = Gtk.ComboBoxText()
        for mode, label in SORT_MODES:
            self.sort_combo.append(mode, label)
        self.sort_combo.set_tooltip_text("结果排序方式")
        self.sort_combo.set_active_id(self.cfg.get("sort_mode", DEFAULT_SORT))
        self.sort_combo.connect("changed", self.on_sort_changed)
        sort_label = Gtk.Label(label="排序：", xalign=1)
        head.pack_end(self.sort_combo, False, False, 0)
        head.pack_end(sort_label, False, False, 0)
        left.pack_start(head, False, False, 0)

        # 结果列表：仿 TextSeek 的多行行样式（名称 / 路径 / 命中片段 + 右侧大小与时间）
        self.results_view = ResultsList(
            on_select=self.on_result_selected,
            on_activate=self.on_result_activated,
            on_context_menu=self.on_result_context_menu)
        left.pack_start(self.results_view, True, True, 0)
        paned.pack1(left, True, False)

        # ---------------------------------------------------- 右：预览
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.preview_title = Gtk.Label(xalign=0)
        self.preview_title.set_margin_top(6)
        self.preview_title.set_margin_bottom(6)
        self.preview_title.set_margin_start(10)
        self.preview_title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.pack_start(switcher, False, False, 6)
        head.pack_start(self.preview_title, True, True, 0)
        right.pack_start(head, False, False, 0)

        # 全文
        scroller_text = Gtk.ScrolledWindow()
        scroller_text.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        if HAVE_SOURCE:
            self.textview = GtkSource.View()
            self.textbuffer = self.textview.get_buffer()
        else:
            self.textview = Gtk.TextView()
            self.textbuffer = self.textview.get_buffer()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_monospace(True)
        self.textview.set_left_margin(10)
        self.textview.set_right_margin(10)
        self.textview.set_top_margin(8)
        self.tag_hit = self.textbuffer.create_tag(
            "hit", background="#ffe066", foreground="#111111")
        self.tag_current = self.textbuffer.create_tag(
            "current", background="#ffa94d", foreground="#111111",
            weight=Pango.Weight.BOLD)
        scroller_text.add(self.textview)
        self.stack.add_titled(scroller_text, "text", "全文")

        # 版面
        scroller_page = Gtk.ScrolledWindow()
        scroller_page.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.page_image = Gtk.Image()
        self.page_label = Gtk.Label(xalign=0.5)
        page_box.pack_start(self.page_image, True, True, 6)
        page_box.pack_start(self.page_label, False, False, 6)
        scroller_page.add(page_box)
        self.stack.add_titled(scroller_page, "page", "版面")

        # 命中清单
        scroller_hits = Gtk.ScrolledWindow()
        scroller_hits.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.hits_label = Gtk.Label(xalign=0, yalign=0)
        self.hits_label.set_selectable(True)
        self.hits_label.set_line_wrap(True)
        self.hits_label.set_margin_top(10)
        self.hits_label.set_margin_bottom(10)
        self.hits_label.set_margin_start(12)
        self.hits_label.set_margin_end(12)
        scroller_hits.add(self.hits_label)
        self.stack.add_titled(scroller_hits, "hits", "命中")

        right.pack_start(self.stack, True, True, 0)
        self.preview_pane = right
        paned.pack2(right, True, False)
        # 结果列表按 TextSeek 那样以"宽栏"为主；预览可随时用 Ctrl+P 收起变成通栏列表
        paned.set_position(820)
        self._split_position = 820

    def _build_status(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(220, -1)
        box.pack_start(self.status_label, True, True, 0)
        box.pack_end(self.progress, False, False, 0)

        # 主区域是 Paned，状态栏需要包一层纵向盒子
        paned = self.get_child()
        self.remove(paned)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.pack_start(paned, True, True, 0)
        wrapper.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                           False, False, 0)
        wrapper.pack_start(box, False, False, 0)
        self.add(wrapper)

    # ================================================================ 搜索

    def on_search(self, widget=None, *args):
        raw = self.search_entry.get_text().strip()
        if not raw:
            return
        self._search_token += 1
        token = self._search_token
        limit = int(self.cfg.get("max_results", 500))
        self.status_label.set_text("搜索中……")
        self.progress.set_fraction(0.5)
        self.summary_label.set_text("正在搜索：%s" % raw)
        self._history_index = -1                 # 重新开始翻历史
        history.add(raw)                         # 记入搜索历史

        def work():
            idx = Index(self.db_path, readonly=True)   # 索引进行中也能立刻搜
            try:
                searcher = Searcher(idx, self.cfg)
                results, query, elapsed = searcher.search(
                    raw, limit=limit, sort=self.cfg.get("sort_mode", DEFAULT_SORT))
                error = None
            except Exception as exc:                 # noqa: BLE001
                results, query, elapsed = [], None, 0.0
                error = "%s: %s" % (type(exc).__name__, exc)
            finally:
                idx.close()
            GLib.idle_add(self._finish_search, token, results, query,
                          elapsed, error)

        threading.Thread(target=work, daemon=True).start()

    def _finish_search(self, token, results, query, elapsed, error):
        if token != self._search_token:
            return False
        self.results = results
        self.query = query
        self.results_view.set_results(results[:RESULT_ROW_LIMIT])
        self.hits_label.set_markup(self._hits_markup(results))
        self.progress.set_fraction(0.0)
        if error:
            self.summary_label.set_text("搜索出错：%s" % error)
            self.status_label.set_text("搜索出错")
            return False
        extra = ""
        if query is not None and query.message:
            extra = "    提示：%s" % query.message
        if len(results) > RESULT_ROW_LIMIT:
            extra += "    仅显示前 %d 条" % RESULT_ROW_LIMIT
        elif not results:
            extra += "    换个关键词，或先点左上角按钮建立索引"
        self.summary_label.set_text("命中 %d 份文档，用时 %.2f s%s"
                                    % (len(results), elapsed, extra))
        self.status_label.set_text("搜索完成")
        if results:
            self.results_view.select_first()
        else:
            self._show_text("没有匹配结果。\n\n提示：\n"
                            "• 还没索引？点左上角 ⟳ 按钮建立索引\n"
                            "• 内容搜索只覆盖已索引的文档\n"
                            "• 可用 ext:pdf / path:目录 / -排除词 收窄范围\n")
        return False

    @staticmethod
    def _hits_markup(results):
        rows = ["<b>命中清单</b>（共 %d 份文档）\n" % len(results)]
        for i, res in enumerate(results[:200], 1):
            rows.append("\n<b>%d. %s</b>  <small>[%s] %s · %d 处</small>"
                        % (i, GLib.markup_escape_text(res.path), res.ext,
                           res.mtime_text, res.matches))
            for snippet in res.snippets:
                parts = []
                for kind, text, _start in snippet["segments"]:
                    chunk = " ".join(text.split())
                    if not chunk:
                        continue
                    chunk = GLib.markup_escape_text(chunk)
                    parts.append("<b>%s</b>" % chunk if kind == 1 else chunk)
                page = ("<small>第 %s 页 </small>" % snippet["page"]
                        if snippet.get("page") else "")
                rows.append("    " + page + "…".join(parts))
        return "\n".join(rows)

    # ================================================================ 预览

    def on_sort_changed(self, combo):
        mode = combo.get_active_id() or DEFAULT_SORT
        if mode == self.cfg.get("sort_mode"):
            return
        self.cfg["sort_mode"] = mode
        config.save(self.cfg)
        self.status_label.set_text("排序方式：%s" % dict(SORT_MODES).get(mode, mode))
        if self.search_entry.get_text().strip():
            self.on_search()

    def on_result_selected(self, res):
        if res is None:
            return
        self._preview_token += 1
        token = self._preview_token
        self.preview_title.set_text(res.path)
        self.status_label.set_text("正在加载预览……")
        self.page_image.set_from_pixbuf(None)
        self.page_label.set_text("正在渲染版面……")
        term = None
        if self.query is not None:
            if self.query.terms:
                term = self.query.terms[0]
            elif self.query.regex is not None:
                term = None
        self._show_text("正在读取正文……", reset=True)

        def work():
            """分三拍：先显示正文，再显示版面图。

            以前是"正文 + 转换 + 渲染"全干完才回主线程更新，于是一份 .doc 在索引
            运行时要等 60 多秒，界面一直停在"正在读取正文……"，看着就像坏了。
            """
            text, error = "", None
            # ① 优先用索引库里存的正文：毫秒级，绝大多数情况正文立刻就能显示
            try:
                idx = Index(self.db_path, readonly=True)
                try:
                    stored = idx.get_doc(res.path)
                    if (stored is not None and stored["size"] == res.size
                            and stored["content"] is not None):
                        text = textstore.unpack_content(stored["content"]) or ""
                        error = stored["warning"] or None
                finally:
                    idx.close()
            except Exception:                        # noqa: BLE001
                text = ""

            if text:
                GLib.idle_add(self._finish_text, token, res, text, error)
            else:
                # ② 库里没有正文：现抽（.doc/.wps 走 LibreOffice、扫描件走 OCR，都慢）
                GLib.idle_add(self._preview_note, token,
                              "正在抽取正文……（%s 需要转换，索引运行时可能较慢）"
                              % (res.ext or "该格式"))
                try:
                    got = extract(res.path, options=self.cfg)
                    text = got.text
                    if got.warning:
                        error = got.warning
                except ExtractError as exc:
                    error = str(exc)
                except Exception as exc:             # noqa: BLE001
                    error = "%s: %s" % (type(exc).__name__, exc)
                GLib.idle_add(self._finish_text, token, res, text, error)

            # ③ 版面图最后做，不影响正文显示
            GLib.idle_add(self._preview_note, token, "正在渲染版面……")
            png, page = None, None
            try:
                png, page = preview.render_match(res.path, term)
            except Exception:                        # noqa: BLE001
                png, page = None, None
            GLib.idle_add(self._finish_preview, token, res, text, error,
                          png, page)

        threading.Thread(target=work, daemon=True).start()

    def _preview_note(self, token, text):
        """更新状态栏提示（过期请求直接丢弃）。"""
        if token != self._preview_token:
            return False
        self.status_label.set_text(text)
        return False

    def _finish_text(self, token, res, text, error):
        """正文到齐就先显示，不等版面渲染。"""
        if token != self._preview_token:
            return False
        self._set_preview_text(res, text, error)
        self.status_label.set_text("正文已显示：%s" % os.path.basename(res.path))
        return False

    def _finish_preview(self, token, res, text, error, png, page):
        """版面图就绪（正文已经先显示过了，这里不再重复写入，免得又滚回顶部）。"""
        if token != self._preview_token:
            return False
        if png:
            try:
                self.page_image.set_from_file(str(png))
                self.page_label.set_text("第 %s 页（%d dpi）"
                                         % (page or 1,
                                            int(self.cfg.get("render_dpi", 150))))
            except GLib.Error:
                self.page_label.set_text("版面渲染失败")
        else:
            self.page_label.set_text(
                "无法生成版面预览（%s）" % (error or "该格式不支持按页渲染"))
        self.status_label.set_text("预览完成：%s" % os.path.basename(res.path))
        return False

    def _show_text(self, text, reset=True):
        if reset:
            self.textbuffer.set_text(text)
        else:
            end = self.textbuffer.get_end_iter()
            self.textbuffer.insert(end, text)

    def _set_preview_text(self, res, text, error):
        self.textbuffer.set_text("")
        note = "【%s】\n\n" % error if error else ""
        if not text:
            self._show_text(note + "没有抽取到正文。"
                                   "（图片型/扫描件可在设置里启用 OCR）")
            return
        truncated = ""
        if len(text) > TEXT_PREVIEW_LIMIT:
            text = text[:TEXT_PREVIEW_LIMIT]
            truncated = "\n\n……（正文过长，仅显示前 %d 字符）" % TEXT_PREVIEW_LIMIT
        self._show_text(note + text + truncated)

        # 关键词高亮。
        # 注意：正文前面还有 note（【warning】提示），缓冲区偏移要整体加 len(note)，
        # 否则高亮的会是"往后挪了 15 个字"的错位内容。
        delta = len(note)
        terms = []
        if self.query is not None:
            terms = list(self.query.terms)
        spans = []
        for term in terms:
            spans.extend(find_spans(text, term, cap=2000))
        if self.query is not None and self.query.regex is not None:
            spans.extend((m.start(), m.end())
                         for m in self.query.regex.finditer(text))

        start_iter = self.textbuffer.get_start_iter()
        tagged_first = False
        for span in sorted(spans)[:2000]:
            start, end = span[0], span[1]
            # 宽松命中可能含字间空白，掐掉首尾空白让高亮正好落在关键词上
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start >= end or start >= len(text) or end > len(text):
                continue
            begin = self.textbuffer.get_iter_at_offset(start + delta)
            finish = self.textbuffer.get_iter_at_offset(end + delta)
            tag = self.tag_current if not tagged_first else self.tag_hit
            self.textbuffer.apply_tag(tag, begin, finish)
            if not tagged_first:
                start_iter = begin
                tagged_first = True
        self.textview.scroll_to_iter(start_iter, 0.1, False, 0.0, 0.2)

    # ================================================================ 索引

    def on_index_clicked(self, *_args, retry_errors=False):
        """点 ⟳：常规增量索引。

        pending（关着 OCR 的扫描件）会自动重跑；error（真失败）**不会**——
        否则每轮都要把慢的旧格式重跑一遍。要重试失败文件请用菜单里的
        "重试之前失败的文件"（等价于命令行的 --retry-errors）。
        """
        if self._indexing:
            self.on_stop_index()
            return
        roots = self.cfg.get("roots") or []
        if not roots:
            self.on_settings()
            return
        self._stop_index.clear()
        self._indexing = True
        self._set_index_button(running=True)
        self.status_label.set_text(
            "开始建立索引……（含重试之前失败的文件）" if retry_errors
            else "开始建立索引……")
        self.progress.set_fraction(0.0)

        def progress(state):
            builder = self._builder
            if builder is not None and self._stop_index.is_set():
                builder.stop()
            GLib.idle_add(self._update_index_progress, state)

        def work():
            idx = Index(self.db_path)
            builder = Builder(idx, self.cfg, progress)
            self._builder = builder
            try:
                state = builder.run(retry_errors=retry_errors)
            except Exception as exc:                 # noqa: BLE001
                GLib.idle_add(self._index_failed,
                              "%s: %s" % (type(exc).__name__, exc))
                return
            finally:
                idx.close()
                self._builder = None
            GLib.idle_add(self._index_done, state)

        threading.Thread(target=work, daemon=True).start()

    def on_reocr_selected(self, *_args):
        """**立刻**只重认选中的这一份：清它的 OCR 缓存 → 按当前精度识别 → 写回索引。

        早先的版本只是把这一行标记成待重认，要等下一次点 ⟳ 才真正重认——而那次
        ⟳ 会把当时所有 pending 的文档一起重认，看起来就像"点了它却重认了所有文件"。
        现在改成就地处理这一份，并在状态栏明说"只处理了这一份"。
        """
        res = self._selected_result()
        if res is None:
            self.status_label.set_text("先在结果列表里选中一份文档")
            return
        if getattr(self, "_reocr_running", False):
            self.status_label.set_text("正在重新识别上一份，请稍候……")
            return
        self._reocr_running = True
        self.status_label.set_text("正在重新识别「%s」……（只处理这一份）" % res.name)
        started = time.time()

        def work():
            cleared = 0
            text, warning, pages, error = "", None, [], None
            try:
                if res.path.lower().endswith(".pdf"):
                    cleared = ocr.clear_page_cache(res.path, self.cfg)
                got = extract(res.path, options=self.cfg)
                text, warning, pages = got.text, got.warning or None, got.page_offsets
            except ExtractError as exc:
                error = str(exc)
            except Exception as exc:                 # noqa: BLE001
                error = "%s: %s" % (type(exc).__name__, exc)
            if error is None:
                try:
                    self._store_extraction(res, text, warning, pages)
                except Exception as exc:             # noqa: BLE001
                    error = "写回索引失败（索引正在跑？稍后再试）：%s" % exc
            GLib.idle_add(self._finish_reocr, res, len(text), warning, cleared,
                          error, time.time() - started)

        threading.Thread(target=work, daemon=True).start()

    def _store_extraction(self, res, text, warning, pages):
        """把重认结果写回索引库；索引正在写时等一会儿再试。"""
        import sqlite3
        index = Index(self.db_path)
        try:
            stat = os.stat(res.path)
            head = "%s\n%s\n" % (res.name, res.path)
            meta = {"path": res.path, "name": res.name,
                    "ext": os.path.splitext(res.path)[1].lstrip(".").lower(),
                    "kind": "pdf" if res.path.lower().endswith(".pdf") else None,
                    "size": stat.st_size, "mtime": stat.st_mtime,
                    "ino": stat.st_ino, "dev": stat.st_dev, "chars": len(text)}
            meta["kind"] = meta["kind"] or (res.kind or None)
            for attempt in range(6):
                try:
                    index.upsert(meta, tokens=tokenizer.index_tokens(head + text),
                                 state="ok" if text.strip() else "empty",
                                 warning=warning,
                                 content=textstore.pack_content(text),
                                 pages=textstore.pack_pages(pages))
                    index.commit()
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 5:
                        raise
                    time.sleep(3)
        finally:
            index.close()

    def _finish_reocr(self, res, chars, warning, cleared, error, elapsed):
        self._reocr_running = False
        if error:
            self.status_label.set_text("重新识别失败：%s" % error)
            return False
        note = "，%s" % warning if warning else ""
        self.status_label.set_text(
            "已重新识别「%s」：%d 字，清掉 %d 页缓存，用时 %.0f 秒%s（只处理了这一份）"
            % (res.name, chars, cleared, elapsed, note))
        if self.search_entry.get_text().strip():
            self.on_search()                       # 用新正文刷新结果与预览
        else:
            self.on_result_selected(res)
        return False

    def on_retry_failed(self, *_args):
        """重试 state=error 的文件（装了新解析器、或怀疑上次是偶发失败时用）。"""
        if self._indexing:
            self.status_label.set_text("索引正在进行，先停止再重试")
            return
        self.on_index_clicked(retry_errors=True)

    def on_stop_index(self, *_args):
        if self._indexing:
            self._stop_index.set()
            self.status_label.set_text("正在停止索引……（已完成的记录会保留，下次接着跑）")

    def _set_index_button(self, running):
        name = "process-stop-symbolic" if running else "view-refresh-symbolic"
        self.index_button.set_image(Gtk.Image.new_from_icon_name(
            name, Gtk.IconSize.BUTTON))
        self.index_button.set_tooltip_text(
            "停止索引" if running else "更新索引（F5）")

    def shutdown(self):
        """退出前收尾：停掉索引线程 + 回收所有子进程。

        不做这件事的后果很直观：关掉窗口后，已启动的 tesseract / pdftoppm 变成孤儿
        继续满核跑（实测踩过）。
        另外 concurrent.futures 在解释器退出时会 join 工作线程——如果索引线程还卡在
        OCR 子进程上，进程就会"窗口关了还在后台跑"。所以这里再加一道硬性兜底：
        4 秒内还没退干净就强制退出。
        """
        self._stop_index.set()
        builder = getattr(self, "_builder", None)
        if builder is not None:
            try:
                builder.stop()
            except Exception:                        # noqa: BLE001
                pass
        procs.terminate_all()
        watchdog = threading.Timer(4.0, lambda: os._exit(0))
        watchdog.daemon = True
        watchdog.start()

    def _update_index_progress(self, state):
        self.progress.set_fraction(state.percent / 100.0)
        extra = state.skip_summary()
        self.status_label.set_text(
            "[%s] %.1f%%  %d/%d  已索引 %d  未变 %d  失败 %d  %.0f s  %s%s"
            % (state.phase, state.percent, state.done_count, state.total,
               state.indexed, state.unchanged, state.failed, state.elapsed,
               os.path.basename(state.current or ""),
               ("  跳过：" + extra) if extra else ""))
        return False

    def _index_done(self, state):
        self._indexing = False
        self._set_index_button(running=False)
        self.progress.set_fraction(0.0)
        parts = ["已索引 %d" % state.indexed, "未变 %d" % state.unchanged]
        if state.failed:
            parts.append("失败 %d" % state.failed)
        parts.append("用时 %.0f s" % state.elapsed)
        if state.interrupted:
            left = max(0, state.total - state.done_count)
            head = "索引已中断（还剩 %d 份没处理，再按一次 ⟳ 会继续）" % left
        else:
            head = "索引完成"
        self.status_label.set_text("%s：%s" % (head, "，".join(parts)))
        skip = state.skip_summary()
        if skip:
            self.summary_label.set_text(
                "本次扫描 %d 个文件，其中 %d 个待索引；跳过：%s（可在设置里调整）"
                % (state.seen, state.total, skip))
        self._refresh_summary()
        if self.search_entry.get_text().strip():
            self.on_search()
        return False

    def _index_failed(self, message):
        self._indexing = False
        self._set_index_button(running=False)
        self.progress.set_fraction(0.0)
        self.status_label.set_text("索引失败：%s" % message)
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE, text="索引失败")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        return False

    def _refresh_summary(self):
        stats = self.index.stats()
        roots = self.cfg.get("roots") or []
        root_text = "、".join(roots) if roots else "（尚未设置索引目录）"
        text = ("已索引 %d 份文档 · 正文 %.1f MB · 索引库 %.1f MB · 目录：%s"
                % (stats["docs"], stats["chars"] / 1048576.0,
                   stats["db_bytes"] / 1048576.0, root_text))
        bad = stats["by_state"].get("error", 0)
        if bad:
            text += "　⚠ 失败 %d 份（菜单→重试之前失败的文件）" % bad
        self.summary_label.set_text(text)

    # ================================================================ 菜单动作

    def on_settings(self, *_args):
        dialog = SettingsDialog(self, self.cfg)
        response = dialog.run()
        new_cfg = dialog.result() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if new_cfg is None:
            return
        self.cfg.update(new_cfg)
        config.save(self.cfg)
        self._refresh_summary()
        roots = self.cfg.get("roots") or []
        if roots:
            self.on_index_clicked()

    def on_stats(self, *_args):
        stats = self.index.stats()
        lines = [
            "索引库：%s" % self.index.path,
            "体积：%.2f MB" % (stats["db_bytes"] / 1048576.0),
            "文档数：%d" % stats["docs"],
            "正文总量：%.2f MB" % (stats["chars"] / 1048576.0),
            "状态分布：%s" % stats["by_state"],
            "类型分布：%s" % "，".join("%s:%d" % (e or "-", c)
                                       for e, c in stats["by_ext"]),
        ]
        if stats["last_indexed"]:
            lines.append("最近索引：%s" % time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(stats["last_indexed"])))
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE, text="索引统计")
        dialog.format_secondary_text("\n".join(lines))
        dialog.run()
        dialog.destroy()

    def on_clear_cache(self, *_args):
        removed = preview.clear_cache()
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE, text="缓存已清理")
        dialog.format_secondary_text("清理了 %d 个版面渲染缓存文件。" % removed)
        dialog.run()
        dialog.destroy()

    def on_about(self, *_args):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("DocSeek")
        dialog.set_version("0.1.0")
        dialog.set_comments("本地文档内容搜索：中文 bigram + SQLite FTS5 索引，"
                            "命中片段内联显示，PDF/Office 版面按需渲染预览。")
        dialog.set_license_type(Gtk.License.GPL_3_0)
        dialog.run()
        dialog.destroy()

    # ================================================================ 结果操作

    def _selected_result(self):
        return self.results_view.selected_result()

    def on_result_activated(self, res):
        """双击/回车：用系统默认程序打开这个文件。"""
        if res:
            self._run_action("打开文件", lambda: self._open(res.path))

    def on_result_context_menu(self, res, event):
        if not res:
            return False
        entries = (
            ("打开文件", lambda: self._open(res.path)),
            ("在文件管理器中显示", lambda: self._reveal(res.path)),
            ("打开所在目录", lambda: self._open(os.path.dirname(res.path))),
            ("复制路径", lambda: self._copy(res.path)),
            ("复制文件名", lambda: self._copy(res.name)),
            ("重新加载预览", lambda: self.on_result_selected(res)),
        )
        menu = Gtk.Menu()
        for label, action in entries:
            entry = Gtk.MenuItem(label=label)
            # 注意：信号会先塞一个 menu item 参数，所以用 lambda 固定住 label/action，
            # 否则 _run_action 收到的"动作"其实是标签字符串（这个坑刚踩过）
            entry.connect("activate",
                          lambda _item, l=label, a=action: self._run_action(l, a))
            menu.append(entry)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ---------------------------------------------------------- 打开 / 复制

    def _run_action(self, label, action, *_args):
        """界面动作的统一护栏：出错就写进状态栏。

        以前这些回调直接调用（早已被删掉的）方法，一抛异常就被 GTK 吞进 stderr，
        界面上表现为"右键菜单点了毫无反应"，极难排查。
        """
        try:
            action()
        except Exception as exc:                     # noqa: BLE001
            self.status_label.set_text("「%s」失败：%s: %s"
                                       % (label, type(exc).__name__, exc))
            return False
        return True

    def _open(self, path):
        """用系统默认程序打开文件；传目录就是在文件管理器里打开该目录。"""
        path = os.path.expanduser(str(path or ""))
        if not path:
            self.status_label.set_text("没有可打开的路径")
            return False
        if not os.path.exists(path):
            self.status_label.set_text("文件不存在（可能已移动或删除）：%s" % path)
            return False
        uri = GLib.filename_to_uri(path, None)
        try:
            if _launch_uri(uri):
                self.status_label.set_text("已打开：%s" % os.path.basename(path) or path)
                return True
        except GLib.Error:
            pass
        # 兜底：xdg-open / gio open（走 procs 登记，退出时会被清理）
        for opener in ("xdg-open", "gio"):
            binary = shutil.which(opener)
            if not binary:
                continue
            try:
                cmd = [binary, path] if opener == "xdg-open" else [binary, "open", path]
                procs.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                self.status_label.set_text("已交给 %s 打开：%s"
                                           % (opener, os.path.basename(path)))
                return True
            except OSError:
                continue
        self.status_label.set_text("打不开（系统没有可用的默认程序）：%s" % path)
        return False

    def _reveal(self, path):
        """在文件管理器里定位文件（D-Bus ShowItems），不行就退化成打开所在目录。"""
        path = os.path.expanduser(str(path or ""))
        if not path or not os.path.exists(path):
            return self._open(os.path.dirname(path)) if path else False
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            proxy = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1", None)
            proxy.call_sync("ShowItems",
                            GLib.Variant("(ass)", ([GLib.filename_to_uri(path, None)], "")),
                            Gio.DBusCallFlags.NONE, 3000, None)
            self.status_label.set_text("已在文件管理器中定位：%s" % os.path.basename(path))
            return True
        except GLib.Error:
            return self._open(os.path.dirname(path))

    def _copy(self, text):
        """复制到剪贴板。"""
        from gi.repository import Gdk
        text = "" if text is None else str(text)
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        self.status_label.set_text("已复制：%s" % text)
        return True

    def on_toggle_preview(self, *_args):
        """收起/展开右侧预览。收起后结果列表通栏——与 TextSeek 的观感一致。"""
        showing = self.preview_pane.get_visible()
        if showing:
            self._split_position = self.paned.get_position() or self._split_position
            self.preview_pane.set_visible(False)
            self.status_label.set_text("已隐藏预览（Ctrl+P 可恢复）")
        else:
            self.preview_pane.set_visible(True)
            self.paned.set_position(self._split_position)
            self.status_label.set_text("已显示预览")
        return True

    # ================================================================ 搜索历史

    def on_entry_key(self, _widget, event):
        """搜索框里 ↑/↓ 翻历史（像终端那样）。"""
        from gi.repository import Gdk
        if event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        items = history.entries()
        if not items:
            return False
        if event.keyval == Gdk.KEY_Up:
            self._history_index = min(self._history_index + 1, len(items) - 1)
        else:
            self._history_index -= 1
            if self._history_index < -1:
                self._history_index = -1
        if self._history_index < 0:
            self.search_entry.set_text("")
        else:
            self.search_entry.set_text(items[self._history_index])
        self.search_entry.set_position(-1)
        return True

    def _rebuild_history_panel(self):
        """重建下拉面板内容（每次展开时调用，保证是最新记录）。"""
        for child in self.history_box.get_children():
            self.history_box.remove(child)

        # 宽度跟随搜索框：观感就是一个贴在输入框下面的下拉列表
        width = self.search_box.get_allocated_width()
        if width < 240:
            width = 400
        self.history_box.set_size_request(width - 16, -1)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(xalign=0)
        title.set_markup("<b>最近搜索</b>")
        header.pack_start(title, True, True, 0)
        clear_button = Gtk.Button()
        clear_button.set_image(Gtk.Image.new_from_icon_name(
            "user-trash-symbolic", Gtk.IconSize.MENU))
        clear_button.set_tooltip_text("清空全部历史记录")
        clear_button.set_relief(Gtk.ReliefStyle.NONE)
        clear_button.connect("clicked", self.on_history_clear)
        header.pack_end(clear_button, False, False, 0)
        self.history_box.pack_start(header, False, False, 0)
        self.history_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0)

        items = history.detailed()
        if not items:
            empty = Gtk.Label(xalign=0)
            empty.set_markup('<span foreground="#8a8a8a">还没有搜索记录</span>')
            empty.set_margin_top(6)
            empty.set_margin_bottom(6)
            self.history_box.pack_start(empty, False, False, 0)
            self.history_box.show_all()
            return

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, min(380, 32 * len(items) + 8))
        inner = Gtk.ListBox()
        # 单选：鼠标悬停/↑↓ 有选中反馈，回车或双击即选用（下拉框的常规手感）
        inner.set_selection_mode(Gtk.SelectionMode.SINGLE)
        inner.set_activate_on_single_click(True)
        inner.connect("row-activated", self._on_history_row_activated)
        for item in items:
            inner.add(self._history_row(item))
        scroller.add(inner)
        self.history_box.pack_start(scroller, True, True, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_markup('<span size="small" foreground="#8a8a8a">'
                        '点一条即可重搜；✕ 删除单条</span>')
        self.history_box.pack_start(hint, False, False, 0)
        self.history_box.show_all()

    def _history_row(self, item):
        row = Gtk.ListBoxRow()
        row.query = item["query"]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        label = Gtk.Label(xalign=0)
        label.set_markup("%s   <span size=\"small\" foreground=\"#8a8a8a\">%s</span>"
                         % (GLib.markup_escape_text(item["query"]),
                            GLib.markup_escape_text(
                                history.relative_time(item["last"]))))
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_tooltip_text(item["query"])
        box.pack_start(label, True, True, 0)

        remove = Gtk.Button()
        remove.set_image(Gtk.Image.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU))
        remove.set_relief(Gtk.ReliefStyle.NONE)
        remove.set_tooltip_text("删除这条记录")
        remove.connect("clicked", self.on_history_remove, item["query"])
        box.pack_end(remove, False, False, 0)
        row.add(box)
        row.show_all()
        return row

    def _on_history_row_activated(self, _listbox, row):
        query = getattr(row, "query", None)
        if query:
            self.on_history_pick(None, query)

    def on_history_pick(self, _button, query):
        self.history_popover.popdown()
        self.search_entry.set_text(query)
        self.on_search()

    def on_history_remove(self, _button, query):
        history.remove(query)
        self._rebuild_history_panel()
        self.status_label.set_text("已删除历史记录：%s" % query)

    def on_history_clear(self, *_args):
        history.clear()
        self._rebuild_history_panel()
        self.status_label.set_text("已清空搜索历史")

    # ================================================================ 快捷键

    def on_key_press(self, _widget, event):
        from gi.repository import Gdk
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if event.keyval == Gdk.KEY_F5:
            self.on_index_clicked()
            return True
        if ctrl and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_entry.grab_focus()
            return True
        if ctrl and event.keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self.history_button.set_active(not self.history_button.get_active())
            return True
        if ctrl and event.keyval in (Gdk.KEY_p, Gdk.KEY_P):
            self.on_toggle_preview()
            return True
        if ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.destroy()
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.search_entry.set_text("")
            return True
        return False


class SettingsDialog(Gtk.Dialog):
    """索引设置：根目录列表 + 若干开关。"""

    def __init__(self, parent, cfg):
        Gtk.Dialog.__init__(self, title="索引设置", transient_for=parent,
                            modal=True)
        self.set_default_size(640, 460)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        self.add_button("保存", Gtk.ResponseType.OK)
        self.cfg = dict(cfg)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        box.add(Gtk.Label(label="要索引的目录（文档内容搜索只覆盖这些目录）：",
                          xalign=0))
        self.root_store = Gtk.ListStore(str)
        for root in cfg.get("roots") or []:
            self.root_store.append([root])
        tree = Gtk.TreeView(model=self.root_store)
        tree.append_column(Gtk.TreeViewColumn(
            "目录", Gtk.CellRendererText(), text=0))
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(160)
        scroller.add(tree)
        box.add(scroller)
        self.tree = tree

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, callback in (("添加目录…", self._add_root),
                                ("移除所选", self._remove_root)):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            buttons.pack_start(button, False, False, 0)
        box.add(buttons)

        self.check_hidden = Gtk.CheckButton(
            label="包含隐藏文件与隐藏目录")
        self.check_hidden.set_active(bool(cfg.get("include_hidden")))
        box.add(self.check_hidden)

        self.check_lo = Gtk.CheckButton(
            label="允许用 LibreOffice 解析旧格式（.doc/.wps 等，首次较慢）")
        self.check_lo.set_active(bool(cfg.get("use_libreoffice", True)))
        box.add(self.check_lo)

        self.check_symlink = Gtk.CheckButton(
            label="跟随符号链接目录（桌面上的“快捷方式”文件夹；注意可能成环）")
        self.check_symlink.set_active(bool(cfg.get("follow_symlinks", False)))
        box.add(self.check_symlink)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.attach(Gtk.Label(label="单文件体积上限（MB）", xalign=0), 0, 0, 1, 1)
        self.spin_size = Gtk.SpinButton.new_with_range(1, 2048, 1)
        self.spin_size.set_value(float(cfg.get("max_file_mb", 64)))
        grid.attach(self.spin_size, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="抽取线程数", xalign=0), 0, 1, 1, 1)
        self.spin_workers = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.spin_workers.set_value(float(cfg.get("workers", 4)))
        grid.attach(self.spin_workers, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="版面渲染分辨率（dpi）", xalign=0), 0, 2, 1, 1)
        self.spin_dpi = Gtk.SpinButton.new_with_range(72, 300, 6)
        self.spin_dpi.set_value(float(cfg.get("render_dpi", 150)))
        grid.attach(self.spin_dpi, 1, 2, 1, 1)
        box.add(grid)

        # ------------------------------------------------ OCR
        box.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.check_ocr = Gtk.CheckButton(
            label="启用 OCR：图片型 PDF（扫描件）里的文字也能搜到")
        self.check_ocr.set_active(bool(cfg.get("ocr_enabled", True)))
        box.add(self.check_ocr)

        ocr_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        installed = ocr.languages()
        ocr_grid.attach(Gtk.Label(label="识别语言", xalign=0), 0, 0, 1, 1)
        self.entry_langs = Gtk.Entry()
        self.entry_langs.set_text(str(cfg.get("ocr_langs", "chi_sim+eng")))
        self.entry_langs.set_tooltip_text(
            "tesseract 语言串，用 + 连接。本机可用：%s"
            % ("、".join(installed) if installed else "未检测到语言包"))
        ocr_grid.attach(self.entry_langs, 1, 0, 1, 1)

        ocr_grid.attach(Gtk.Label(label="识别分辨率（dpi）", xalign=0), 0, 1, 1, 1)
        self.spin_ocr_dpi = Gtk.SpinButton.new_with_range(150, 600, 50)
        self.spin_ocr_dpi.set_value(float(cfg.get("ocr_dpi", 300)))
        ocr_grid.attach(self.spin_ocr_dpi, 1, 1, 1, 1)

        ocr_grid.attach(Gtk.Label(label="识别精度", xalign=0), 0, 3, 1, 1)
        self.combo_ocr_quality = Gtk.ComboBoxText()
        for pixels, label in ((12000000, "高（原始分辨率，推荐）"),
                              (4000000, "中（快约 1.5 倍，小字可能认不出）"),
                              (2000000, "低（更快，仅适合大字）")):
            self.combo_ocr_quality.append(str(pixels), label)
        self.combo_ocr_quality.set_active_id(
            str(cfg.get("ocr_max_pixels", 12000000)))
        if self.combo_ocr_quality.get_active_id() is None:
            self.combo_ocr_quality.set_active_id("12000000")
        ocr_grid.attach(self.combo_ocr_quality, 1, 3, 1, 1)

        ocr_grid.attach(Gtk.Label(label="单份 PDF 最多识别页数", xalign=0), 0, 2, 1, 1)
        self.spin_ocr_pages = Gtk.SpinButton.new_with_range(1, 500, 5)
        self.spin_ocr_pages.set_value(float(cfg.get("ocr_max_pages", 40)))
        ocr_grid.attach(self.spin_ocr_pages, 1, 2, 1, 1)
        box.add(ocr_grid)

        self.check_ocr_images = Gtk.CheckButton(
            label="把图片文件（jpg/png…）也纳入索引（照片目录会明显变慢）")
        self.check_ocr_images.set_active(bool(cfg.get("ocr_images", False)))
        box.add(self.check_ocr_images)

        ocr_hint = Gtk.Label(xalign=0)
        ocr_hint.set_markup(
            '<small>只有"没有文本层"的页才走 OCR，每页约 1–3 秒；识别结果缓存到 '
            '~/.cache/docseek/ocrcache，之后检索不再重认。本机语言包：%s</small>'
            % ("、".join(installed) if installed else "未检测到"))
        ocr_hint.set_line_wrap(True)
        box.add(ocr_hint)

        hint = Gtk.Label(xalign=0)
        hint.set_markup("<small>提示：索引可以随时中断，下次运行会接着未完成的部分继续；"
                        "文件被修改后会自动重新抽取。</small>")
        hint.set_line_wrap(True)
        box.add(hint)
        self.show_all()

    def _add_root(self, *_args):
        dialog = Gtk.FileChooserDialog(
            title="选择要索引的目录", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("选择", Gtk.ResponseType.OK)
        dialog.set_current_folder(os.path.expanduser("~"))
        if dialog.run() == Gtk.ResponseType.OK:
            folder = dialog.get_filename()
            existing = {row[0] for row in self.root_store}
            if folder and folder not in existing:
                self.root_store.append([folder])
        dialog.destroy()

    def _remove_root(self, *_args):
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is not None:
            model.remove(tree_iter)

    def result(self):
        roots = [row[0] for row in self.root_store]
        return {
            "roots": roots,
            "include_hidden": self.check_hidden.get_active(),
            "use_libreoffice": self.check_lo.get_active(),
            "follow_symlinks": self.check_symlink.get_active(),
            "max_file_mb": int(self.spin_size.get_value()),
            "workers": int(self.spin_workers.get_value()),
            "render_dpi": int(self.spin_dpi.get_value()),
            "ocr_enabled": self.check_ocr.get_active(),
            "ocr_langs": self.entry_langs.get_text().strip() or "chi_sim+eng",
            "ocr_dpi": int(self.spin_ocr_dpi.get_value()),
            "ocr_max_pages": int(self.spin_ocr_pages.get_value()),
            "ocr_max_pixels": int(self.combo_ocr_quality.get_active_id()
                                  or 12000000),
            "ocr_images": self.check_ocr_images.get_active(),
        }
