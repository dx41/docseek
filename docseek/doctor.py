"""运行环境自检：哪些依赖在、缺了影响什么、要装就敲哪条命令。

装 deb 只是"声明"依赖，装不装得上取决于用哪条命令：
`sudo dpkg -i` 只检查不安装，`sudo apt install ./包.deb` 才会自动补齐。
所以这里给出**现成的安装命令**，用户复制粘贴即可。

每一项：键、展示名、检查方式、缺失影响、对应安装包、是否必需。
"""

import importlib
import shutil
import sqlite3

# 检查方式：
#   which      外部命令（可给多个，全部要有）
#   module     Python 模块可导入
#   gi         GI 命名空间可取（Gtk 3.0 之类）
#   gi_soft    同上，但缺了只是功能降级
#   fts5       SQLite 是否编译了 FTS5
#   lang       tesseract 语言包
#   always     天然满足（仅用于展示）

CHECKS = (
    ("gi", "PyGObject", "module", "gi",
     "图形界面完全无法启动", "python3-gi", True),
    ("gtk", "GTK3 绑定", "gi", ("Gtk", "3.0"),
     "图形界面完全无法启动", "gir1.2-gtk-3.0", True),
    ("fts5", "SQLite FTS5 全文索引", "fts5", None,
     "索引与检索都不可用", None, True),
    ("poppler", "poppler-utils（pdftotext 等）", "which",
     ("pdftotext", "pdftoppm", "pdfinfo", "pdfimages"),
     "PDF 抽不出正文，也无法渲染版面预览", "poppler-utils", True),

    ("gtksource", "GtkSourceView", "gi_soft", ("GtkSource", "3.0"),
     "预览退化成纯文本视图（高亮仍在）", "gir1.2-gtksource-3.0", False),
    ("tesseract", "tesseract OCR", "which", "tesseract",
     "扫描件与图片无法识别文字", "tesseract-ocr", False),
    ("chi_sim", "中文简体语言包", "lang", "chi_sim",
     "简体中文扫描件识别不出字", "tesseract-ocr-chi-sim", False),
    ("chi_tra", "中文繁体语言包", "lang", "chi_tra",
     "繁体中文扫描件识别不出字", "tesseract-ocr-chi-tra", False),
    ("osd", "方向检测语言包", "lang", "osd",
     "横躺的扫描件无法自动转正（会识别成乱码）", "tesseract-ocr-osd", False),
    ("pil", "Pillow", "module", "PIL",
     "图片无法缩放/转码后再识别，个别格式会识别失败", "python3-pil", False),
    ("xlrd", "xlrd", "module", "xlrd",
     "旧版 .xls 表格只能退回 LibreOffice（慢很多）", "python3-xlrd", False),
    ("soffice", "LibreOffice", "which", "soffice",
     "doc/xls/ppt/wps/et 等旧格式无法兜底解析", "libreoffice-core", False),
    ("catdoc", "catdoc", "which", "catdoc",
     ".doc 只能靠 LibreOffice，一份要一两秒", "catdoc", False),
    ("wps2text", "libwps-tools", "which", "wps2text",
     ".wps 只能靠 LibreOffice", "libwps-tools", False),
)


def _check(kind, target):
    """返回 (是否可用, 补充说明)。"""
    if kind == "which":
        names = (target,) if isinstance(target, str) else tuple(target)
        missing = [n for n in names if not shutil.which(n)]
        return (not missing), ("缺：" + "、".join(missing)) if missing else ""
    if kind == "module":
        try:
            importlib.import_module(target)
            return True, ""
        except Exception as exc:                     # noqa: BLE001
            return False, str(exc)[:60]
    if kind in ("gi", "gi_soft"):
        namespace, version = target
        try:
            import gi
            gi.require_version(namespace, version)
            __import__("gi.repository", fromlist=[namespace])
            return True, ""
        except Exception as exc:                     # noqa: BLE001
            return False, str(exc)[:60]
    if kind == "fts5":
        try:
            conn = sqlite3.connect(":memory:")
            try:
                conn.execute("CREATE VIRTUAL TABLE __probe USING fts5(x)")
            finally:
                conn.close()
            return True, ""
        except Exception as exc:                     # noqa: BLE001
            return False, "当前 SQLite 未编译 FTS5：%s" % exc
    if kind == "lang":
        try:
            from .extract import ocr
            if not ocr.available():
                return False, "未安装 tesseract"
            return (target in ocr.languages()), ""
        except Exception as exc:                     # noqa: BLE001
            return False, str(exc)[:60]
    return False, "未知检查方式：%s" % kind


def run_checks():
    """逐项检查，返回结果列表。"""
    results = []
    for key, label, kind, target, impact, package, required in CHECKS:
        ok, detail = _check(kind, target)
        results.append({"key": key, "label": label, "ok": ok, "detail": detail,
                        "impact": impact, "package": package,
                        "required": required})
    return results


def missing_packages(results=None):
    """缺少的安装包名（必需项排在前面）。"""
    results = results if results is not None else run_checks()
    must = [r["package"] for r in results
            if not r["ok"] and r["required"] and r["package"]]
    soft = [r["package"] for r in results
            if not r["ok"] and not r["required"] and r["package"]]
    ordered = []
    for name in must + soft:
        if name not in ordered:
            ordered.append(name)
    return ordered


def apt_command(results=None):
    """现成的安装命令；没有缺失时返回 None。"""
    packages = missing_packages(results)
    if not packages:
        return None
    return "sudo apt install -y " + " ".join(packages)


def missing_required(results=None):
    results = results if results is not None else run_checks()
    return [r for r in results if r["required"] and not r["ok"]]


def format_report(results=None):
    """人类可读的自检报告。"""
    results = results if results is not None else run_checks()
    lines = []
    for item in results:
        mark = "✓" if item["ok"] else ("✗" if item["required"] else "·")
        line = "  %s %-30s %s" % (mark, item["label"],
                                  "正常" if item["ok"] else item["impact"])
        if not item["ok"] and item["detail"]:
            line += "（%s）" % item["detail"]
        lines.append(line)
    broken = [r for r in results if not r["ok"]]
    lines.append("")
    if not broken:
        lines.append("依赖齐备，没有任何缺失。")
        return "\n".join(lines)
    must = missing_required(results)
    if must:
        lines.append("必需依赖缺失 %d 项，程序无法正常运行：" % len(must))
        for item in must:
            lines.append("  · %s → 需要 %s" % (item["label"], item["package"]))
    else:
        lines.append("必需依赖齐备；下面这些是可选项，缺了只影响对应格式。")
    command = apt_command(results)
    if command:
        lines.append("")
        lines.append("一条命令补齐缺失项：")
        lines.append("    " + command)
    return "\n".join(lines)
