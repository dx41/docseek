"""搜索结果列表：仿 TextSeek 的多行行样式。

每条结果占三行（对齐后视觉上就是"名称 / 大小 / 修改时间"三列）：

    文件名（加粗，过长中间省略）                    23 KB   2015-05-13
        完整路径（小字、灰色，过长开头省略）
        …命中片段…（关键词红色加粗，最多 3 处，超出折叠为"另有 N 处"）

用 Gtk.ListBox + 自绘行，而不是 TreeView：TreeView 的行高与多行 markup 都不好控，
而 TextSeek 那种"一行里三种字号 + 右对齐两列"的排版用容器拼最省事。
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango      # noqa: E402

SNIPPET_LIMIT = 3           # 行内最多展示几处命中
SNIPPET_MAX_LEN = 300       # 片段总长上限（按字符）
HIT_COLOR = "#c0392b"


def esc(text):
    return GLib.markup_escape_text(text or "")


def snippet_markup(result, limit=SNIPPET_LIMIT, max_len=SNIPPET_MAX_LEN):
    """把命中片段拼成带高亮的 markup（命中的词用红色加粗，与 TextSeek 一致）。"""
    pieces = []
    total = 0
    for snippet in result.snippets[:limit]:
        for kind, text, _start in snippet["segments"]:
            chunk = " ".join(text.split())
            if not chunk:
                continue
            escaped = esc(chunk)
            pieces.append('<span foreground="%s" weight="bold">%s</span>'
                          % (HIT_COLOR, escaped) if kind == 1 else escaped)
            total += len(chunk)
        pieces.append("  ")
        if total > max_len:
            break
    text = "".join(pieces).strip()
    if len(result.snippets) > limit:
        text += '  <span foreground="#888888">（另有 %d 处）</span>' % (
            len(result.snippets) - limit)
    return text


def plain_snippet(result, limit=SNIPPET_LIMIT):
    """不带标记的片段纯文本（给 tooltip 用）。"""
    parts = []
    for snippet in result.snippets[:limit]:
        parts.append("".join(text for _kind, text, _s in snippet["segments"]))
    return " … ".join(p.strip() for p in parts if p.strip())


def _dim_label(markup, ellipsize=Pango.EllipsizeMode.END):
    label = Gtk.Label(xalign=0)
    label.set_markup(markup)
    label.set_ellipsize(ellipsize)
    return label


class ResultRow(Gtk.ListBoxRow):
    """一条结果。"""

    def __init__(self, result):
        Gtk.ListBoxRow.__init__(self)
        self.result = result
        self.set_tooltip_text(result.path or "")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_margin_top(5)
        outer.set_margin_bottom(5)

        # 左：文件名 / 路径 / 命中片段
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name = Gtk.Label(xalign=0)
        name.set_markup("<b>%s</b>" % esc(result.name or result.path))
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        left.pack_start(name, False, False, 0)

        path = Gtk.Label(xalign=0)
        path.set_markup('<span size="small" foreground="#7a7a7a">%s</span>'
                        % esc(result.path))
        path.set_ellipsize(Pango.EllipsizeMode.START)
        left.pack_start(path, False, False, 0)

        markup = snippet_markup(result)
        if markup:
            snippet = Gtk.Label(xalign=0)
            snippet.set_markup(markup)
            snippet.set_line_wrap(True)
            snippet.set_lines(2)
            snippet.set_ellipsize(Pango.EllipsizeMode.END)
            snippet.set_margin_top(2)
            left.pack_start(snippet, False, False, 0)
        elif result.warning:
            note = Gtk.Label(xalign=0)
            note.set_markup('<span size="small" foreground="#7a7a7a">%s</span>'
                            % esc(result.warning))
            left.pack_start(note, False, False, 0)
        outer.pack_start(left, True, True, 0)

        # 右：大小 / 修改时间（与左栏顶部对齐）
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        size = Gtk.Label(xalign=1)
        size.set_markup('<span size="small">%s</span>'
                        % esc(_size_text(result)))
        right.pack_start(size, False, False, 0)
        when = Gtk.Label(xalign=1)
        when.set_markup('<span size="small" foreground="#7a7a7a">%s</span>'
                        % esc(result.mtime_text))
        right.pack_start(when, False, False, 0)
        outer.pack_start(right, False, False, 0)

        self.add(outer)
        self.show_all()


def _size_text(result):
    try:
        from .. import config
        return config.human_size(result.size)
    except Exception:                               # noqa: BLE001
        return ""


class ResultsList(Gtk.ScrolledWindow):
    """结果列表容器。

    回调：
        on_select(result)              选中某条
        on_activate(result)            双击/回车打开
        on_context_menu(result, event) 右键菜单
    """

    def __init__(self, on_select=None, on_activate=None, on_context_menu=None):
        Gtk.ScrolledWindow.__init__(self)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.set_shadow_type(Gtk.ShadowType.IN)
        self.on_select = on_select
        self.on_activate = on_activate
        self.on_context_menu = on_context_menu
        self._rows = []

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-selected", self._row_selected)
        self.listbox.connect("row-activated", self._row_activated)
        self.listbox.connect("button-press-event", self._button_press)
        self.add(self.listbox)

        self.placeholder = Gtk.Label(xalign=0.5, yalign=0.5)
        self.placeholder.set_markup(
            '<span foreground="#8a8a8a">输入关键词开始搜索</span>')
        self.placeholder.set_margin_top(24)

    # ------------------------------------------------------------ 数据
    def set_results(self, results):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self._rows = []
        if not results:
            self.show_placeholder("没有匹配结果。\n\n"
                                  "· 还没索引？点左上角 ⟳ 建立索引\n"
                                  "· 可用 ext:pdf / path:目录 / -排除词 收窄范围")
            return
        for result in results:
            row = ResultRow(result)
            self.listbox.add(row)
            self._rows.append(row)
        self.listbox.show_all()

    def show_placeholder(self, text):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self._rows = []
        self.placeholder.set_text(text)
        self.listbox.add(self.placeholder)
        self.listbox.show_all()

    def select_first(self):
        if self._rows:
            self.listbox.select_row(self._rows[0])

    def selected_result(self):
        row = self.listbox.get_selected_row()
        return row.result if isinstance(row, ResultRow) else None

    def rows(self):
        return list(self._rows)

    # ------------------------------------------------------------ 信号
    def _row_selected(self, _listbox, row):
        if self.on_select and isinstance(row, ResultRow):
            self.on_select(row.result)

    def _row_activated(self, _listbox, row):
        if self.on_activate and isinstance(row, ResultRow):
            self.on_activate(row.result)

    def _button_press(self, _widget, event):
        if event.button != 3 or not self.on_context_menu:
            return False
        row = self.listbox.get_row_at_y(int(event.y))
        if isinstance(row, ResultRow):
            self.listbox.select_row(row)
            self.on_context_menu(row.result, event)
            return True
        return False
