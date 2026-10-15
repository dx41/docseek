"""旧式二进制办公格式抽取（catdoc / antiword / xlrd / unrtf / wps2text，
外加 LibreOffice 兜底）。

按扩展名逐个后端尝试，前一个失败就降级到下一个：

    doc dot     catdoc → antiword → LibreOffice
    xls xlt     xlrd   → LibreOffice
    rtf         unrtf  → 内置剥离器 → LibreOffice
    wps wpt     wps2text → LibreOffice
    其它        LibreOffice（ppt pot pps / et ett / dps dpt / 123 wk1 ...）

统一约定：

* 所有外部命令都是 subprocess.run(..., timeout=60)；FileNotFoundError /
  TimeoutExpired / CalledProcessError 一律转成中文 ExtractError 再降级，
  不让底层异常外泄；
* 文件体积超过 max_bytes 时直接拒绝，不交给转换器；
* 后端返回的正文都经 clean_text 归一化，并按 config.DEFAULTS["max_text_chars"] 截断；
* Extracted.warning 记录真正生效的后端名（catdoc / xlrd / libreoffice ...）。

实测结论（本机 银河麒麟 V10 / aarch64 / LibreOffice 6.4.7）：

* catdoc、antiword、unrtf、wps2text 均未安装，因此 .doc/.wps/.rtf 实际走
  LibreOffice，.xls 走 xlrd；
* lo.py 的 txt 过滤器是 Writer 专用的，导出表格类文档必然失败（0xc10
  "impl_store failed"），所以 _lo() 在 txt 失败后会改用 HTML 导出再剥标签，
  这样 .et/.xls 等表格（含多工作表）也能抽出中文；
* 已知限制：WPS 演示（.dps）LO 无法识别，会被当成纯文本导入而抽出乱码。
"""

import re
import shutil
import subprocess
import tempfile
import unicodedata
from html import unescape
from pathlib import Path

from .. import config
from . import file_ext, lo
from .base import ExtractError, Extracted, clean_text

# 单个外部命令的超时（秒）
TIMEOUT = 60

# 外部工具位置（导入时探测一次，缺失即降级；测试可直接改这些模块属性）
CATDOC = shutil.which("catdoc")
ANTIWORD = shutil.which("antiword")
UNRTF = shutil.which("unrtf")
WPS2TEXT = shutil.which("wps2text")

# 正文最大字符数（与 lo.py 保持一致）
MAX_CHARS = config.DEFAULTS["max_text_chars"]

# 扩展名 → 归并后的 kind
_KIND = {
    "doc": "doc", "dot": "doc",
    "xls": "xls", "xlt": "xls",
    "rtf": "rtf",
    "wps": "wps", "wpt": "wps",
    "et": "et", "ett": "et",
    "dps": "dps", "dpt": "dps",
    "ppt": "ppt", "pot": "ppt", "pps": "ppt",
}

_DOC_EXTS = ("doc", "dot")
_XLS_EXTS = ("xls", "xlt")
_RTF_EXTS = ("rtf",)
_WPS_EXTS = ("wps", "wpt")

# 表格类：走 LibreOffice 时必须用 csv 过滤器（txt 是 Writer 专用，必然失败）
_SPREADSHEET_EXTS = frozenset((
    "xls", "xlt", "xlsx", "et", "ett", "ods", "sxc", "123",
    "wk1", "wk3", "wq1", "wq2", "csv", "tsv", "dbf",
))


def available():
    """返回本机可用的解析后端名（供界面/日志展示）。"""
    tools = []
    for name, path in (("catdoc", CATDOC), ("antiword", ANTIWORD),
                       ("unrtf", UNRTF), ("wps2text", WPS2TEXT),
                       ("libreoffice", lo.SOFFICE)):
        if path:
            tools.append(name)
    try:
        import xlrd  # noqa: F401
        tools.append("xlrd")
    except ImportError:
        pass
    return tools


# --------------------------------------------------------------------------
# 外部命令执行
# --------------------------------------------------------------------------

def _run_soft(cmd, timeout=TIMEOUT):
    """执行命令，返回 (返回码, stdout 字节, stderr 文本)。

    命令缺失 / 超时 / 无法执行会抛中文 ExtractError；非零返回码不抛异常，
    因为 LibreOffice 出错时也常常返回 0，真正可靠的判据是有没有产出文件。
    """
    try:
        proc = subprocess.run(
            cmd, timeout=timeout, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ExtractError("未找到命令：%s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise ExtractError("调用 %s 超时（%ss）" % (cmd[0], timeout))
    except subprocess.CalledProcessError as exc:   # check=True 时才会触发，防御性保留
        raise ExtractError("%s 执行失败：%s" % (cmd[0], exc))
    except OSError as exc:
        raise ExtractError("无法执行 %s：%s" % (cmd[0], exc))
    err = proc.stderr.decode("utf-8", "replace").strip().replace("\n", " ")
    return proc.returncode, proc.stdout, err


def _run(cmd, timeout=TIMEOUT):
    """执行命令并要求成功，返回 stdout 字节；失败抛中文 ExtractError。"""
    code, out, err = _run_soft(cmd, timeout=timeout)
    if code != 0:
        raise ExtractError("%s 退出码 %s：%s"
                           % (cmd[0], code, err[:200] or "无错误输出"))
    return out


def _run_variants(variants, timeout=TIMEOUT):
    """按顺序尝试命令的多种写法（兼容不同版本的参数差异），全失败抛首个错误。"""
    first = None
    for cmd in variants:
        try:
            return _run(cmd, timeout=timeout)
        except ExtractError as exc:
            if first is None:
                first = exc
    raise first


def _decode(raw):
    """外部命令输出 → 文本：先按 UTF-8，再按 GB18030（Windows 中文档常见）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _plain(text, label, offsets=()):
    """普通后端的统一返回：(归一化正文, 分页偏移, 实际后端名)。"""
    return clean_text(text, MAX_CHARS), list(offsets), label


# “符号/未分配/私用区/代理对/格式”类字符：正常文档里几乎不出现
_SUSPICIOUS_CATS = frozenset(("So", "Cn", "Co", "Cs", "Cf"))

# 分隔符不承载信息：CSV/TSV 导出里逗号、制表符天然占多数，
# 早期判据会因此把正常的表格导出误判成乱码（实测某 .xls 的 CSV 中逗号占 46%）。
_SEPARATOR_CHARS = frozenset(",;\t|")

# 乱码判据的取样长度（够统计，又不至于拖慢批量索引）
_JUNK_SAMPLE = 4000


def _looks_like_junk(text):
    """粗判后端吐出的“正文”其实是二进制乱码。

    典型场景：LibreOffice 认不出 WPS 演示（.dps），会把它当纯文本导入，
    于是抽出几万个 "#"，这种内容进了索引就是纯噪声。
    判据取得很保守（实测正常文档远达不到）：

    * 单个非空白字符占比 ≥ 25%（实测正常中文文档最高频字符只有 3%~5%）；
    * 或符号/未分配/私用区字符占比 ≥ 5%（实测正常文档≈0）。

    空白与分隔符（`, ; \\t |`）不参与统计。
    样本不足 200 字符时一律放过——短文本统计不可靠。
    """
    sample = text[:_JUNK_SAMPLE]
    if len(sample) < 200:
        return False
    counts = {}
    total = 0
    suspicious = 0
    for ch in sample:
        if ch.isspace() or ch in _SEPARATOR_CHARS:
            continue
        total += 1
        counts[ch] = counts.get(ch, 0) + 1
        if unicodedata.category(ch) in _SUSPICIOUS_CATS:
            suspicious += 1
    if total < 150:
        return False
    if suspicious * 20 >= total:
        return True
    return max(counts.values()) * 4 >= total


# --------------------------------------------------------------------------
# doc / dot：catdoc → antiword
# --------------------------------------------------------------------------

def _catdoc(path):
    if not CATDOC:
        raise ExtractError("未安装 catdoc")
    # -d utf-8 让 catdoc 直接吐 UTF-8；老版本不认 -d，退回默认再按多编码猜测
    raw = _run_variants(([CATDOC, "-d", "utf-8", str(path)],
                         [CATDOC, str(path)]))
    text = _decode(raw)
    if not text.strip():
        raise ExtractError("catdoc 未提取到文本")
    return _plain(text, "catdoc")


def _antiword(path):
    if not ANTIWORD:
        raise ExtractError("未安装 antiword")
    # -m UTF-8.txt 需要映射文件，缺失时退回默认输出
    raw = _run_variants(([ANTIWORD, "-m", "UTF-8.txt", str(path)],
                         [ANTIWORD, "-t", str(path)],
                         [ANTIWORD, str(path)]))
    text = _decode(raw)
    if not text.strip():
        raise ExtractError("antiword 未提取到文本")
    return _plain(text, "antiword")


# --------------------------------------------------------------------------
# xls / xlt：xlrd（1.x 支持 .xls，2.x 起只支持 .xlsx）
# --------------------------------------------------------------------------

def _xlrd(path):
    try:
        import xlrd
    except ImportError:
        raise ExtractError("未安装 xlrd")
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:                    # 损坏/加密文件会抛各种异常
        raise ExtractError("xlrd 无法打开：%s" % exc)
    try:
        try:
            text, sheets = _xls_text(book)
        except Exception as exc:                # 单个工作表损坏时也降级，不要炸穿
            raise ExtractError("xlrd 解析失败：%s" % exc)
    finally:
        try:
            book.release_resources()
        except Exception:
            pass
    if not text.strip():
        raise ExtractError("xlrd 未提取到文本")
    return text, _sheet_offsets(text, sheets), "xlrd"


def _xls_text(book):
    """把工作簿拼成正文，返回 (正文, 工作表列表)。"""
    sheets = book.sheets()
    blocks = []
    for sheet in sheets:
        blocks.append("=== %s ===" % sheet.name)    # 工作表标题行，便于定位
        for row in range(sheet.nrows):
            line = _xls_row(sheet, row, book.datemode)
            if line:
                blocks.append(line)
    return clean_text("\n".join(blocks), MAX_CHARS), sheets


def _xls_row(sheet, row, datemode):
    """把一行单元格拼成制表符分隔的文本（全空行返回空串）。"""
    cells = []
    for col in range(sheet.ncols):
        cells.append(_xls_cell(sheet, row, col, datemode))
    return "\t".join(cells).strip()


def _xls_cell(sheet, row, col, datemode):
    try:
        import xlrd
    except ImportError:
        return ""
    ctype = sheet.cell_type(row, col)
    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    value = sheet.cell_value(row, col)
    if ctype == xlrd.XL_CELL_NUMBER:
        try:
            if float(value).is_integer() and abs(value) < 1e15:
                return str(int(value))
        except (TypeError, ValueError):
            pass
        return ("%g" % value) if isinstance(value, float) else str(value)
    if ctype == xlrd.XL_CELL_DATE:
        return _format_date(value, datemode)
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if value else "FALSE"
    if ctype == xlrd.XL_CELL_ERROR:
        try:
            return xlrd.error_text_from_code.get(value, "")
        except AttributeError:
            return ""
    return str(value).strip()


def _format_date(value, datemode):
    """Excel 序列日期 → 可读文本。"""
    try:
        import xlrd
        parts = xlrd.xldate_as_tuple(value, datemode)
    except Exception:
        return str(value)
    if parts[3:] == (0, 0, 0):
        return "%04d-%02d-%02d" % parts[:3]
    return "%04d-%02d-%02d %02d:%02d:%02d" % parts


def _sheet_offsets(text, sheets):
    """在最终正文里定位各工作表标题行，得到工作表起始偏移。

    用 find 而不是累加长度：clean_text 会压缩空行、去首尾换行，偏移可能位移；
    定位不到的工作表直接略过（偏移列表保持递增）。
    """
    offsets = []
    cursor = 0
    for sheet in sheets:
        marker = "=== %s ===" % sheet.name
        pos = text.find(marker, cursor)
        if pos < 0:
            continue
        offsets.append(pos)
        cursor = pos + len(marker)
    return offsets


# --------------------------------------------------------------------------
# rtf：unrtf → 内置剥离器
# --------------------------------------------------------------------------

# 只作元数据、正文不应出现的“目标”（destination）
_RTF_SKIP_DESTS = frozenset((
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "shppict",
    "nonshppict", "object", "objdata", "header", "headerl", "headerr",
    "headerf", "footer", "footerl", "footerr", "footerf", "footnote",
    "annotation", "fldinst", "datafield", "datastore", "themedata",
    "colorschememapping", "latentstyles", "listtable", "listoverridetable",
    "rsidtbl", "generator", "filetbl", "xmlnstbl", "upr", "bkmkstart",
    "bkmkend", "doccomm", "oldcprops", "oldpprops", "oldsprops", "oldtprops",
    "mmathPr", "panose", "falt", "keycode", "password", "passwordhash",
))

# 常见控制字 → 文本
_RTF_WORD_TEXT = {
    "par": "\n", "line": "\n", "sect": "\n", "page": "\n", "softline": "\n",
    "tab": "\t", "emdash": "\u2014", "endash": "\u2013", "bullet": "\u2022",
    "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
    "rdblquote": "\u201d", "nbsp": "\u00a0", "enspace": " ", "emspace": " ",
    "qmspace": " ", "zwj": "", "zwnj": "",
}

# 控制符号 → 文本
_RTF_SYMBOL_TEXT = {"~": "\u00a0", "-": "", "_": "-", ":": "", "|": ""}

# \ansicpgN → Python 编码
_RTF_CODEPAGES = {
    936: "gb18030", 950: "big5", 932: "cp932", 949: "cp949", 1361: "johab",
    65001: "utf-8", 874: "cp874", 1250: "cp1250", 1251: "cp1251",
    1252: "cp1252", 1253: "cp1253", 1254: "cp1254", 1255: "cp1255",
    1256: "cp1256", 1257: "cp1257", 1258: "cp1258", 437: "cp437",
    850: "cp850", 852: "cp852", 866: "cp866", 10000: "mac_roman",
    10007: "mac_cyrillic",
}

_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_HEX = frozenset("0123456789abcdefABCDEF")


def _unrtf(path):
    if not UNRTF:
        raise ExtractError("未安装 unrtf")
    raw = _run_variants(([UNRTF, "--nopict", "--text", str(path)],
                         [UNRTF, "--text", str(path)],
                         [UNRTF, str(path)]))
    text = _decode(raw)
    if not text.strip():
        raise ExtractError("unrtf 未提取到文本")
    return _plain(text, "unrtf")


def _rtf_builtin(path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractError("读取 RTF 失败：%s" % exc)
    text = rtf_to_text(raw)
    if not text.strip():
        raise ExtractError("内置 RTF 剥离器未提取到文本")
    return _plain(text, "builtin-rtf")


def _decode_bytes(raw, codec):
    """按 \\ansicpg 声明的代码页解码一段字节，失败再依次尝试 GB18030/UTF-8/CP1252。

    未声明代码页时（codec 为 None）先试 GB18030：中文 RTF 占多数，而 CP1252
    几乎能“解码成功”任何字节，放在前面会把中文变成乱码。
    """
    order = [codec] if codec else []
    order.extend(("gb18030", "utf-8", "cp1252"))
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def rtf_to_text(raw):
    """剥离 RTF 控制字，返回纯文本（不依赖 unrtf）。

    只做“够用”的解析：分组、可忽略目标（{\\*...}、fonttbl 等）、\\'hh 字节转义
    （按 \\ansicpg 解码）、\\uNNNN Unicode 转义（按 \\ucN 跳过回退字符）、
    常用控制字的换行/制表。raw 可以是 bytes；str 按 latin-1 逐字节看待。
    """
    is_bytes = isinstance(raw, (bytes, bytearray))
    if is_bytes:
        data = bytes(raw)
        src = data.decode("latin-1", "replace")   # 1 字节 = 1 字符，便于逐字符扫描
    else:
        data = b""
        src = raw or ""
    if "\\rtf" not in src[:2048]:
        # 名不副实的“RTF”（其实是纯文本或其它格式），按普通文本猜编码
        return clean_text(_decode(data) if is_bytes else src, MAX_CHARS)

    out = []                 # 已产出的文本片段
    pending = bytearray()    # 待按代码页解码的高位字节
    codec = None             # \ansicpg 声明的代码页
    skip_stack = []          # 每层的“是否跳过”状态
    skip = False             # 当前分组是否跳过
    uc_skip = 1              # \ucN：\uNNNN 之后要吞掉的回退字符数
    fallback_left = 0        # 当前还需吞掉几个回退字符
    n = len(src)
    i = 0

    def flush():
        """把累积的高位字节按代码页解码后输出（保持字符顺序）。"""
        if pending:
            out.append(_decode_bytes(bytes(pending), codec))
            del pending[:]

    def emit(text):
        if text:
            flush()
            out.append(text)

    while i < n:
        ch = src[i]

        if ch in "\r\n":
            # RTF 文件里的换行只是排版折行（甚至可能断在词中间），不是正文内容，
            # 真正的段落分隔是 \par，所以这里直接丢弃
            i += 1
            continue

        if ch == "{":
            skip_stack.append(skip)
            i += 1
            continue
        if ch == "}":
            flush()
            skip = skip_stack.pop() if skip_stack else False
            i += 1
            continue

        if ch == "\\":                              # 控制字 / 控制符号
            i += 1
            if i >= n:
                break
            c = src[i]
            if c in "\\{}":                         # 转义的字面量
                if fallback_left > 0:
                    fallback_left -= 1
                elif not skip:
                    emit(c)
                i += 1
                continue
            if c == "'":                            # \'hh：代码页里的一个字节
                i += 1
                hexs = src[i:i + 2]
                i += 2
                if fallback_left > 0:
                    fallback_left -= 1
                elif (not skip and len(hexs) == 2
                      and hexs[0] in _HEX and hexs[1] in _HEX):
                    pending.append(int(hexs, 16))
                continue
            if c in _RTF_SYMBOL_TEXT:               # \~ \- \_ 等控制符号
                if fallback_left > 0:
                    fallback_left -= 1
                elif not skip:
                    emit(_RTF_SYMBOL_TEXT[c])
                i += 1
                continue
            if c == "*":                            # {\*\...}：可忽略目标，整组丢弃
                skip = True
                i += 1
                continue
            if c not in _LETTERS:                   # 其它控制符号（如 \<换行>）无意义
                i += 1
                continue

            j = i                                   # 读控制字与可选的数字参数
            while j < n and src[j] in _LETTERS:
                j += 1
            word = src[i:j]
            k = j
            if k < n and (src[k] == "-" or src[k].isdigit()):
                k += 1
                while k < n and src[k].isdigit():
                    k += 1
            param = src[j:k]
            i = k
            if i < n and src[i] == " ":             # 控制字后的一个空格是分隔符
                i += 1

            if word == "bin":                       # \binN 后面跟 N 个原始字节
                try:
                    i += max(int(param), 0)
                except (TypeError, ValueError):
                    pass
            elif word == "ansicpg":
                try:
                    codec = _RTF_CODEPAGES.get(int(param))
                except (TypeError, ValueError):
                    codec = None
            elif word == "uc":
                try:
                    uc_skip = max(int(param), 0)
                except (TypeError, ValueError):
                    uc_skip = 1
            elif word == "u":                       # \uNNNN：Unicode 转义
                try:
                    code = int(param)
                except (TypeError, ValueError):
                    continue
                if code < 0:
                    code += 0x10000
                if not (0xD800 <= code <= 0xDFFF):  # 代理对区不合法，忽略
                    if not skip:
                        emit(chr(code))
                    fallback_left = uc_skip
            elif word == "mac":
                codec = codec or "mac_roman"
            elif word == "pc":
                codec = codec or "cp437"
            elif word == "pca":
                codec = codec or "cp850"
            elif word in _RTF_SKIP_DESTS:
                skip = True
            else:
                piece = _RTF_WORD_TEXT.get(word)
                if piece and not skip:
                    emit(piece)
            continue

        if fallback_left > 0:                       # \uNNNN 的回退字符（通常是 '?'）
            fallback_left -= 1
            i += 1
            continue
        if skip:
            i += 1
            continue
        if is_bytes and ord(ch) > 127:
            pending.append(ord(ch) & 0xFF)          # 高位字节留着按代码页解码
        else:
            emit(ch)                                # str 输入里的字符本身就是文本
        i += 1

    flush()
    return clean_text("".join(out), MAX_CHARS)


# --------------------------------------------------------------------------
# wps / wpt：wps2text（libwps-tools）
# --------------------------------------------------------------------------

def _wps2text(path):
    if not WPS2TEXT:
        raise ExtractError("未安装 wps2text（libwps-tools）")
    raw = _run([WPS2TEXT, str(path)])
    text = _decode(raw)
    if not text.strip():
        raise ExtractError("wps2text 未提取到文本")
    return _plain(text, "wps2text")


# --------------------------------------------------------------------------
# LibreOffice 兜底：txt 过滤器 → HTML 导出
# --------------------------------------------------------------------------

def _ensure_cache_dir():
    """保证 config.CACHE_DIR 存在（lo.py 与本地临时目录都建在它下面）。"""
    try:
        config.ensure_dirs()
    except OSError as exc:
        raise ExtractError("无法创建缓存目录 %s：%s" % (config.CACHE_DIR, exc))


def _mkdtemp():
    """在缓存目录下建一个本次转换专用的临时目录。"""
    _ensure_cache_dir()
    try:
        return Path(tempfile.mkdtemp(prefix="docseek-legacy-",
                                     dir=str(config.CACHE_DIR)))
    except OSError as exc:
        raise ExtractError("无法创建临时目录：%s" % exc)


def _trash(path):
    """把本次自建的临时产物送进回收站（遵守“不物理删除”约定），失败就留在原地。"""
    gio = shutil.which("gio")
    if not gio or not path:
        return
    try:
        subprocess.run([gio, "trash", str(path)], timeout=TIMEOUT,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        pass


def _soffice_html(src, out_dir):
    """用 LibreOffice 把文档导出为 HTML。

    故意不指定过滤器：lo.py 里的 "HTML (StarWriter)" 是 Writer 专用的，
    表格/演示文档会失败；不指定时 LO 会按文档家族自选（StarCalc / Impress）。
    转换与 lo.py 共用同一个 UserInstallation 与串行锁，避免 profile 冲突。
    """
    if not lo.SOFFICE:
        raise ExtractError("未找到 soffice（LibreOffice），无法解析该格式")
    cmd = [
        lo.SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        "-env:UserInstallation=file://%s" % lo.PROFILE_DIR,
        "--convert-to", "html",
        "--outdir", str(out_dir),
        str(src),
    ]
    # 与 lo.convert 用同一把锁（同一个 profile 不能并发跑 soffice）；
    # 这把锁现在按 profile 区分，预览用的独立 profile 不会被索引堵住。
    with lo._lock_for(lo.PROFILE_DIR):              # noqa: SLF001
        _code, _out, err = _run_soft(cmd, timeout=TIMEOUT)
    return err


def _find_html(out_dir):
    cands = []
    for p in sorted(Path(out_dir).iterdir()):
        if p.suffix.lower() in (".html", ".htm"):
            cands.append(p)
    return cands[0] if cands else None


# LibreOffice HTML 导出的结构标记 → 文本
_RE_HTML_DROP = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_RE_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_RE_HTML_CELL = re.compile(r"(?i)<\s*/\s*(td|th)\b[^>]*>")
_RE_HTML_BLOCK = re.compile(
    r"(?i)<\s*/?\s*(br|p|div|tr|h[1-6]|hr|table|li|ul|ol|dl|dt|dd|pre|"
    r"blockquote|body|head|html)\b[^>]*>")
_RE_HTML_TAG = re.compile(r"(?s)<[^>]*>")


def _html_to_text(html):
    """把 LibreOffice 导出的 HTML 粗转成纯文本（单元格用制表符、行/段落用换行）。"""
    if not html:
        return ""
    text = _RE_HTML_DROP.sub(" ", html)
    text = _RE_HTML_COMMENT.sub(" ", text)
    text = _RE_HTML_CELL.sub("\t", text)
    text = _RE_HTML_BLOCK.sub("\n", text)
    text = _RE_HTML_TAG.sub("", text)
    return unescape(text)


def _lo_html_text(src):
    """LibreOffice → HTML → 纯文本（表格/演示文档真正可用的通路）。"""
    out_dir = _mkdtemp()
    try:
        err = _soffice_html(src, out_dir)
        produced = _find_html(out_dir)
        if produced is None:
            raise ExtractError("LibreOffice 未产生 HTML 输出：%s"
                               % (err[:200] or "无输出"))
        try:
            raw = produced.read_bytes()
        except OSError as exc:
            raise ExtractError("读取转换结果失败：%s" % exc)
        text = _html_to_text(_decode(raw))
    finally:
        _trash(out_dir)                             # 临时目录进回收站，不物理删除
    if not text.strip():
        raise ExtractError("LibreOffice HTML 导出未提取到文本")
    return clean_text(text, MAX_CHARS)


def _lo(path):
    """LibreOffice 兜底：文本类先 txt，表格类先 csv，最后退到 HTML 导出。

    lo.py 的 txt 过滤器是 Writer 专用的，表格类文档必然失败（实测 0xc10，
    内部还会重试并空等约 4 s），所以表格类直接走 csv 通路（实测 .et 5.5s → 约 1.5s）。
    两个通路都失败时把各次原因一并报出。
    """
    if not lo.available():
        raise ExtractError("未安装 LibreOffice（soffice）")
    _ensure_cache_dir()
    problems = []
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in _SPREADSHEET_EXTS:
        try:
            text = lo.convert_to_text(path, target="csv")
            if text.strip() and not _looks_like_junk(text):
                return clean_text(text, MAX_CHARS), [], "libreoffice-csv"
            problems.append("csv 转换为空或疑似乱码")
        except ExtractError as exc:
            problems.append("csv 转换失败：%s" % exc)
    else:
        try:
            text = lo.convert_to_text(path)
            if text.strip() and not _looks_like_junk(text):
                return clean_text(text, MAX_CHARS), [], "libreoffice"
            problems.append("txt 转换为空或疑似乱码")
        except ExtractError as exc:
            problems.append("txt 转换失败：%s" % exc)
    try:
        return _lo_html_text(path), [], "libreoffice-html"
    except ExtractError as exc:
        problems.append("html 转换失败：%s" % exc)
    raise ExtractError("；".join(problems))


# --------------------------------------------------------------------------
# 分派
# --------------------------------------------------------------------------

def _chain(ext):
    """该扩展名的后端尝试顺序（每次新建，便于测试替换模块属性）。"""
    if ext in _DOC_EXTS:
        return (("catdoc", _catdoc), ("antiword", _antiword),
                ("libreoffice", _lo))
    if ext in _XLS_EXTS:
        return (("xlrd", _xlrd), ("libreoffice", _lo))
    if ext in _RTF_EXTS:
        return (("unrtf", _unrtf), ("builtin-rtf", _rtf_builtin),
                ("libreoffice", _lo))
    if ext in _WPS_EXTS:
        return (("wps2text", _wps2text), ("libreoffice", _lo))
    return (("libreoffice", _lo),)


def extract(path, max_bytes=None, options=None):
    """抽取旧式二进制文档正文（本模块唯一对外入口）。

    path      待抽取文件
    max_bytes 体积上限；超过直接抛 ExtractError，默认取 config.DEFAULTS["max_file_mb"]
    返回 Extracted（kind 为格式名，warning 为实际生效的后端）；
    所有后端都失败时抛 ExtractError，异常消息里带每个后端的失败原因。
    """
    path = Path(path)
    if not path.is_file():
        raise ExtractError("文件不存在：%s" % path)
    if max_bytes is None:
        max_bytes = int(config.DEFAULTS["max_file_mb"]) * 1024 * 1024
    if max_bytes:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ExtractError("无法读取文件信息：%s" % exc)
        if size > max_bytes:
            raise ExtractError(
                "文件过大（%.1f MB，上限 %.1f MB），未交给转换器"
                % (size / 1048576.0, max_bytes / 1048576.0))

    ext = file_ext(path)
    kind = _KIND.get(ext, "legacy")
    problems = []
    for name, backend in _chain(ext):
        try:
            text, offsets, label = backend(path)
        except ExtractError as exc:
            problems.append("%s：%s" % (name, exc))
            continue
        if not (text and text.strip()):
            problems.append("%s：未提取到文本" % name)
            continue
        if _looks_like_junk(text):
            problems.append("%s：疑似二进制乱码，已丢弃" % name)
            continue
        return Extracted(
            text=clean_text(text, MAX_CHARS),
            kind=kind,
            page_offsets=list(offsets),
            warning=label)
    raise ExtractError("无法抽取 .%s（%s）"
                       % (ext or "无扩展名", "；".join(problems) or "无可用后端"))
