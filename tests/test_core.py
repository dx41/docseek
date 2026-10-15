# -*- coding: utf-8 -*-
"""docseek 核心模块单元测试：tokenizer / search / index / extract。

覆盖范围
--------
* ``docseek.tokenizer``：CJK bigram 切分、西文小写、混合串、去重、FTS 表达式、
  单汉字识别；
* ``docseek.search``：``parse_query`` 的各种语法、``find_spans`` 的精确/宽松匹配、
  ``Searcher`` 端到端（AND 语义、排除词、类型过滤、粗筛假阳性、排序）；
* ``docseek.index``：``Index`` 的 upsert/指纹/search_ids/delete/stats/reset，
  ``Builder`` 的增量与 sweep；
* ``docseek.extract``：分发与错误、二进制嗅探、HTML 去标签、``Extracted.page_of``
  分页偏移语义、合成 docx、真实 PDF 的分页偏移。

原则
----
* 只用标准库 unittest，不引入任何新依赖；
* 所有产物（索引库、样本文件）都在 ``tempfile.mkdtemp()`` 里，绝不写入 /home、/data
  下的用户文件；真实样本只读打开（并断言 size/mtime 未变）；
* 按本机约定不做物理删除：临时目录优先 ``gio trash`` 进回收站，
  gio 不可用时留在 tempfile 目录里（不做 rm/unlink）；
* 需要真实 PDF 时在 cwd/家目录//data 下有界扫描（限时、限深、限量），
  找不到就 ``skipTest``。
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from docseek import config, tokenizer                          # noqa: E402
from docseek.extract import extract, handler_for, is_indexable  # noqa: E402
from docseek.extract.base import Extracted, ExtractError, clean_text  # noqa: E402
from docseek.index.builder import Builder                      # noqa: E402
from docseek.index.db import Index                             # noqa: E402
from docseek.search import MAX_SPANS, find_spans, parse_query, Searcher  # noqa: E402

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{4,6}")

# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

SCAN_ROOTS = [os.environ.get("DOCSEEK_TEST_ROOT"), os.getcwd(),
              str(Path.home()), "/data"]
SCAN_BUDGET = 10.0          # 扫描真实样本的时间上限（秒）
SCAN_MAX_DEPTH = 5
SCAN_LIMIT = 12
SCAN_PRUNE = {"node_modules", "__pycache__", ".git", ".hg", ".svn", ".cargo",
              ".rustup", ".npm", ".cache", "snap", "flatpak", "proc", "sys",
              "dev", "run", "lost+found", "site-packages", ".Trash",
              ".Trash-1000", ".trash"}


def _discard(path):
    """把测试临时目录送进回收站（本机约定：不做物理删除；失败就留在原地）。"""
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


def core_cfg(**overrides):
    """测试用配置：从 DEFAULTS 出发，不读用户配置文件，线程数压到 2。

    默认关掉 OCR：核心测试不该依赖 tesseract，也不该因为磁盘上某个真实 PDF
    有几页没文本层就跑去识别（每页十几秒）。OCR 另有 tests/test_ocr.py 专测。
    """
    cfg = dict(config.DEFAULTS)
    cfg.update({"workers": 2, "ocr_enabled": False})
    cfg.update(overrides)
    return cfg


# 合成 docx 用的最小 [Content_Types].xml
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')


def make_docx(path, paragraphs):
    """写一个最小可用的 .docx（zip + [Content_Types].xml + word/document.xml）。"""
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>'
        % xml_escape(text) for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>%s</w:body></w:document>' % body)
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("word/document.xml", document)
    return str(path)


class TempCase(unittest.TestCase):
    """公共基类：每个用例一个 tempfile 临时目录，结束时送进回收站。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docseek-core-")
        self.addCleanup(_discard, self.tmp)

    # ------------------------------------------------------------ 小工具
    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write_text(self, name, text, encoding="utf-8"):
        path = self.path(name)
        with open(path, "w", encoding=encoding) as fh:
            fh.write(text)
        return path

    def write_bytes(self, name, data):
        path = self.path(name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def open_index(self, name="index.db"):
        """建一个临时索引库；cleanup 时先关连接（后注册先执行），再回收目录。"""
        index = Index(self.path(name))
        self.addCleanup(index.close)
        return index

    def make_docx(self, name, paragraphs):
        return make_docx(self.path(name), paragraphs)


# ==========================================================================
# 1. tokenizer
# ==========================================================================

class TokenizerTest(unittest.TestCase):
    """docseek.tokenizer：中文 bigram + 西文词。"""

    def test_cjk_bigram(self):
        """连续汉字切成 2-gram。"""
        self.assertEqual(tokenizer.index_tokens("中华人民共和国"),
                         "中华 华人 人民 民共 共和 和国")
        self.assertEqual(tokenizer.query_tokens("中华人民共和国"),
                         ["中华", "华人", "人民", "民共", "共和", "和国"])

    def test_single_cjk_char_kept_as_is(self):
        """单个汉字（无法成 bigram）保留其本身。"""
        self.assertEqual(tokenizer.index_tokens("中"), "中")
        self.assertEqual(tokenizer.query_tokens("中"), ["中"])

    def test_western_lowercased(self):
        """西文/数字词小写化，且按词切分。"""
        self.assertEqual(tokenizer.index_tokens("Hello WPS 2024"), "hello wps 2024")
        self.assertEqual(tokenizer.query_tokens("Hello/WPS"), ["hello", "wps"])

    def test_mixed_string(self):
        """混合串同时产出西文词与中文 bigram。"""
        tokens = tokenizer.index_tokens("WPS文档")
        self.assertEqual(tokens, "wps 文档")
        self.assertIn("wps", tokens.split())
        self.assertIn("文档", tokens.split())
        self.assertEqual(tokenizer.query_tokens("WPS文档"), ["wps", "文档"])
        self.assertEqual(tokenizer.index_tokens("Python3.8与WPS"), "python3 8 与 wps")

    def test_query_tokens_consistent_with_index_tokens(self):
        """同一串两种切分必须一致，否则粗筛与写入对不上。"""
        for text in ("中华人民共和国", "WPS文档", "Hello 中文测试", "中"):
            self.assertEqual(tokenizer.query_tokens(text),
                             tokenizer.index_tokens(text).split(),
                             "index/query 切分不一致：%r" % text)

    def test_index_tokens_dedup_preserves_order(self):
        """写入前去重（detail=none 不存位置，重复 token 无意义）。"""
        self.assertEqual(tokenizer.index_tokens("中华中华"), "中华 华中")
        self.assertEqual(tokenizer.index_tokens("aaa aaa aaa"), "aaa")
        self.assertEqual(
            tokenizer.index_tokens("人民代表大会人民代表大会"),
            "人民 民代 代表 表大 大会 会人")
        self.assertEqual(tokenizer.index_tokens(""), "")

    def test_strip_cjk_gaps_joins_only_han(self):
        """入库前抹平汉字之间的空白；西文侧与"汉字↔西文"边界不动。"""
        self.assertEqual(tokenizer.strip_cjk_gaps("人 民 代 表"), "人民代表")
        self.assertEqual(tokenizer.strip_cjk_gaps("人民代\n表大会"), "人民代表大会")
        self.assertEqual(tokenizer.strip_cjk_gaps("WPS 文档"), "WPS 文档")
        self.assertEqual(tokenizer.strip_cjk_gaps("文档 WPS"), "文档 WPS")
        self.assertEqual(tokenizer.strip_cjk_gaps(""), "")
        self.assertTrue(tokenizer.has_cjk_gaps("人 民"))
        self.assertTrue(tokenizer.has_cjk_gaps("人民代\n表"))
        self.assertFalse(tokenizer.has_cjk_gaps("人民代表大会"))
        self.assertFalse(tokenizer.has_cjk_gaps("a b"))

    def test_fts_query_is_and_of_quoted_tokens(self):
        """FTS5 表达式：token 之间 AND，且每个 token 用双引号包裹。"""
        expr = tokenizer.fts_query(["中华人民共和国"])
        self.assertEqual(expr, '"中华" AND "华人" AND "人民" AND "民共"'
                               ' AND "共和" AND "和国"')
        for piece in expr.split(" AND "):
            self.assertTrue(piece.startswith('"') and piece.endswith('"'),
                            "token 未被双引号包裹：%r" % piece)

    def test_fts_query_dedup_and_empty(self):
        self.assertEqual(tokenizer.fts_query(["文档", "文档"]), '"文档"')
        self.assertEqual(tokenizer.fts_query(["中华人民共和国", "WPS文档"]),
                         '"中华" AND "华人" AND "人民" AND "民共" AND "共和"'
                         ' AND "和国" AND "wps" AND "文档"')
        self.assertEqual(tokenizer.fts_query([]), "")
        self.assertEqual(tokenizer.fts_query(["", "   "]), "")

    def test_single_char_terms(self):
        """只有单个汉字需要回退扫描（bigram 索引命中不了单字）。"""
        self.assertEqual(tokenizer.single_char_terms(["人", "人民", "ab", "中x"]),
                         ["人"])
        self.assertEqual(tokenizer.single_char_terms(["中", "文"]), ["中", "文"])
        self.assertEqual(tokenizer.single_char_terms(["ab", "123", "文档"]), [])

    def test_has_cjk(self):
        self.assertFalse(tokenizer.has_cjk("abc 123"))
        self.assertTrue(tokenizer.has_cjk("abc 中"))
        self.assertTrue(tokenizer.has_cjk("あ"))          # 日文假名也在 CJK 范围内


# ==========================================================================
# 2. search.parse_query
# ==========================================================================

class ParseQueryTest(unittest.TestCase):
    """docseek.search.parse_query 的语法解析。"""

    def test_plain_term(self):
        query = parse_query("人民代表大会")
        self.assertEqual(query.terms, ["人民代表大会"])
        self.assertEqual(query.neg_terms, [])
        self.assertEqual(query.raw, "人民代表大会")
        self.assertEqual(query.message, "")

    def test_multiple_terms(self):
        query = parse_query("中文 关键词")
        self.assertEqual(query.terms, ["中文", "关键词"])

    def test_quoted_phrase_keeps_spaces(self):
        query = parse_query('关键词 "带 空格 短语"')
        self.assertEqual(query.terms, ["关键词", "带 空格 短语"])
        self.assertEqual(query.neg_terms, [])

    def test_negative_term(self):
        query = parse_query("报告 -排除词")
        self.assertEqual(query.terms, ["报告"])
        self.assertEqual(query.neg_terms, ["排除词"])

    def test_negative_only_query_asks_for_keyword(self):
        query = parse_query("-只有排除")
        self.assertEqual(query.terms, [])
        self.assertEqual(query.neg_terms, ["只有排除"])
        self.assertTrue(query.message)

    def test_ext_list_accepts_ascii_and_chinese_separators(self):
        for raw in ("ext:pdf,docx", "ext:pdf、docx", "ext:PDF，DOCX",
                    "ext:.pdf;docx"):
            query = parse_query(raw)
            self.assertEqual(query.exts, ["pdf", "docx"], "解析失败：%r" % raw)
            self.assertEqual(query.terms, [])

    def test_ext_with_terms(self):
        query = parse_query("报告 ext:txt,md")
        self.assertEqual(query.terms, ["报告"])
        self.assertEqual(query.exts, ["txt", "md"])

    def test_path_filter(self):
        query = parse_query("报告 path:资料/2024")
        self.assertEqual(query.terms, ["报告"])
        self.assertEqual(query.paths, ["资料/2024"])

    def test_regex_compiled_with_ignorecase(self):
        query = parse_query("re:第[0-9]+章")
        self.assertIsNotNone(query.regex)
        self.assertEqual(query.regex.pattern, "第[0-9]+章")
        self.assertTrue(query.regex.flags & re.IGNORECASE)
        self.assertEqual(query.terms, [])
        self.assertEqual(query.message, "")

    def test_regex_matches(self):
        query = parse_query("re:第[0-9]+章")
        self.assertTrue(query.regex.search("第12章 标题"))
        query2 = parse_query("re:abc")
        self.assertTrue(query2.regex.search("xxABCyy"), "应忽略大小写")

    def test_invalid_regex_goes_to_message(self):
        query = parse_query("re:[unclosed")
        self.assertIsNone(query.regex)
        self.assertIn("正则", query.message)
        self.assertEqual(query.terms, [])

    def test_empty_query_hint(self):
        for raw in ("", "   ", None):
            query = parse_query(raw)
            self.assertEqual(query.terms, [])
            self.assertTrue(query.message, "空查询应给出提示")

    def test_single_cjk_char_sets_slow(self):
        query = parse_query("中")
        self.assertTrue(query.slow)
        self.assertIn("扫描", query.message)
        self.assertEqual(query.terms, ["中"])

    def test_multi_char_query_not_slow(self):
        self.assertFalse(parse_query("中文").slow)
        self.assertFalse(parse_query("ab").slow)

    def test_describe_summarizes_filters(self):
        text = parse_query("报告 -草稿 ext:txt path:资料").describe()
        self.assertIn("关键词：报告", text)
        self.assertIn("排除：草稿", text)
        self.assertIn("类型：txt", text)
        self.assertIn("路径含：资料", text)


# ==========================================================================
# 3. search.find_spans
# ==========================================================================

class FindSpansTest(unittest.TestCase):
    """docseek.search.find_spans：忽略大小写 + 中文宽松匹配。"""

    def test_case_insensitive(self):
        self.assertEqual(find_spans("Hello World", "hello"), [(0, 5)])
        self.assertEqual(find_spans("HELLO world", "hello"), [(0, 5)])
        self.assertEqual(find_spans("hello hello", "Hello"), [(0, 5), (6, 11)])

    def test_chinese_hit_with_absolute_offsets(self):
        text = "这是人民代表大会的报告"
        spans = find_spans(text, "人民代表大会")
        self.assertEqual(spans, [(2, 8)])
        for start, end in spans:
            self.assertEqual(text[start:end], "人民代表大会")

    def test_chinese_not_found(self):
        self.assertEqual(find_spans("人民代表制度", "人民代表大会"), [])
        self.assertEqual(find_spans("abc", "zzz"), [])

    def test_loose_match_allows_whitespace_between_chars(self):
        """宽松匹配：字间被插入空白的正文（pdftotext -layout 场景）也要命中。"""
        text = "人 民 代 表 大 会 万岁"
        spans = find_spans(text, "人民代表大会")
        self.assertEqual(spans, [(0, 11)])
        start, end = spans[0]
        self.assertEqual(text[start:end], "人 民 代 表 大 会")
        # 换行分隔同样算空白
        self.assertEqual(find_spans("人\n民\n代\n表", "人民代表"), [(0, 7)])

    def test_loose_match_still_requires_every_char(self):
        """宽松只放宽“字间空白”，不放宽字本身：缺字仍然不命中。"""
        self.assertEqual(find_spans("人 民 代 表", "人民代表大会"), [])
        self.assertEqual(find_spans("人 民 代 表", "人民代表"), [(0, 7)])

    def test_loose_match_keeps_character_order(self):
        """顺序颠倒不算命中。"""
        self.assertEqual(find_spans("会大表代民人", "人民代表大会"), [])

    def test_cap_truncates(self):
        self.assertEqual(find_spans("a" * 10, "a", cap=2), [(0, 1), (1, 2)])
        text = "人民代表大会 " * (MAX_SPANS + 5)
        self.assertEqual(len(find_spans(text, "人民代表大会")), MAX_SPANS)

    def test_whitespace_only_term_returns_empty(self):
        self.assertEqual(find_spans("abc", "   "), [])
        self.assertEqual(find_spans("abc", ""), [])


# ==========================================================================
# 4. extract.base：Extracted / clean_text
# ==========================================================================

class ExtractedTest(unittest.TestCase):
    """Extracted.page_of 的分页偏移语义与 clean_text 归一化。"""

    def test_page_of_maps_offset_to_page(self):
        """page_offsets 是各页起始偏移；落在两页之间的偏移归前一页。"""
        item = Extracted(text="AAA\nBBB\nCCC", page_offsets=[0, 4, 8])
        self.assertEqual([item.page_of(off) for off in (0, 3, 4, 7, 8, 99)],
                         [1, 1, 2, 2, 3, 3])
        # 第二页起点即 page_offsets[1]
        self.assertEqual(item.text[4:7], "BBB")

    def test_page_of_without_offsets_is_none(self):
        self.assertIsNone(Extracted(text="随便什么").page_of(0))
        self.assertIsNone(Extracted(text="").page_of(0))

    def test_clean_text_normalizes(self):
        self.assertEqual(clean_text("a\r\nb\rc"), "a\nb\nc")
        self.assertEqual(clean_text("a\x00\x07b"), "ab")        # 控制字符剔除
        self.assertEqual(clean_text("a\n\n\n\n\nb"), "a\n\nb")  # 空行压缩
        self.assertEqual(clean_text("\n  x  \n"), "  x  ")      # 只去首尾换行
        self.assertEqual(clean_text("abcdef", max_chars=3), "abc")
        self.assertEqual(clean_text(""), "")
        self.assertEqual(clean_text(None), "")


# ==========================================================================
# 5. extract 分发与 plain 后端
# ==========================================================================

class ExtractDispatchTest(TempCase):
    """docseek.extract 的分发、二进制嗅探、HTML 去标签、编码探测。"""

    def test_handler_for(self):
        self.assertEqual(handler_for("txt"), "plain")
        self.assertEqual(handler_for("html"), "plain")
        self.assertEqual(handler_for("docx"), "ooxml")
        self.assertEqual(handler_for("odt"), "ooxml")
        self.assertEqual(handler_for("pdf"), "pdf")
        self.assertEqual(handler_for("doc"), "legacy")
        self.assertIsNone(handler_for("xyz"))

    def test_is_indexable(self):
        self.assertTrue(is_indexable("a.txt"))
        self.assertTrue(is_indexable("a.docx"))
        self.assertTrue(is_indexable("Makefile"))          # 无扩展名的已知文本名
        self.assertTrue(is_indexable(".gitignore"))
        self.assertFalse(is_indexable("a.png"))            # SKIP_EXTENSIONS
        self.assertFalse(is_indexable("a.xyz"))
        self.assertFalse(is_indexable("noext"))
        self.assertFalse(is_indexable(".hidden"))          # 隐藏文件

    def test_unsupported_extension_raises(self):
        path = self.write_text("a.xyz", "内容")
        with self.assertRaises(ExtractError) as ctx:
            extract(path)
        self.assertIn("不支持的格式", str(ctx.exception))

    def test_no_extension_unknown_name_raises(self):
        path = self.write_text("noext", "内容")
        with self.assertRaises(ExtractError) as ctx:
            extract(path)
        self.assertIn("无扩展名", str(ctx.exception))

    def test_plain_names_without_extension_extract(self):
        path = self.write_text("Makefile", "all:\n\techo 构建\n")
        self.assertEqual(extract(path).kind, "text")
        self.assertIn("构建", extract(path).text)

    def test_missing_file_raises(self):
        with self.assertRaises(ExtractError):
            extract(self.path("不存在.txt"))

    def test_binary_file_rejected_by_plain(self):
        """含 NUL 的二进制文件被 plain 后端拒绝（NUL 占比超阈值）。"""
        path = self.write_bytes("bin.txt", b"\x00" * 512)
        with self.assertRaises(ExtractError) as ctx:
            extract(path)
        self.assertIn("二进制", str(ctx.exception))

    def test_low_nul_ratio_still_treated_as_text(self):
        """嗅探是“NUL 比例”启发式：偶发一个 NUL 仍按文本处理（记录现状）。"""
        path = self.write_bytes("almost.txt", b"abc\x00def")
        self.assertEqual(extract(path).text, "abcdef")

    def test_html_strips_tags_and_scripts(self):
        path = self.write_text("t.html", (
            "<html><head><style>p{color:red}</style>"
            "<script>var a = 1;</script></head>"
            "<body><h1>标题</h1><p>正文 &amp; 结束</p></body></html>"))
        text = extract(path).text
        self.assertIn("标题", text)
        self.assertIn("正文", text)
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("var a", text)
        self.assertIn("&", text)                # 实体已还原
        self.assertNotIn("&amp;", text)

    def test_txt_keeps_markup(self):
        """只有 html/htm/xhtml 才剥标签，.txt 原样保留。"""
        path = self.write_text("t.txt", "<b>加粗</b> 正文")
        self.assertIn("<b>", extract(path).text)

    def test_gb18030_decoded(self):
        path = self.write_bytes("gbk.txt", "中文GBK编码".encode("gb18030"))
        self.assertEqual(extract(path).text, "中文GBK编码")

    def test_max_bytes_rejected(self):
        path = self.write_text("big.txt", "内容" * 100)
        with self.assertRaises(ExtractError) as ctx:
            extract(path, max_bytes=1)
        self.assertIn("过大", str(ctx.exception))


class OoxmlExtractTest(TempCase):
    """合成的最小 docx：文本抽取、分页偏移为空、损坏文件的错误。"""

    def test_synthetic_docx_extracts_chinese(self):
        path = self.make_docx("报告.docx",
                              ["这是人民代表大会的报告", "包含 WPS 与 中文关键词"])
        result = extract(path)
        self.assertEqual(result.kind, "docx")
        self.assertEqual(result.text,
                         "这是人民代表大会的报告\n包含 WPS 与 中文关键词")
        self.assertEqual(result.warning, "")

    def test_synthetic_docx_has_no_page_offsets(self):
        """docx 没有固定分页信息，page_offsets 必须为空（page_of → None）。"""
        path = self.make_docx("报告.docx", ["第一段", "第二段"])
        result = extract(path)
        self.assertEqual(result.page_offsets, [])
        self.assertIsNone(result.page_of(0))

    def test_broken_zip_raises(self):
        path = self.write_bytes("bad.docx", b"this is not a zip file")
        with self.assertRaises(ExtractError) as ctx:
            extract(path)
        self.assertIn("压缩包", str(ctx.exception))

    def test_docx_without_document_xml_raises(self):
        path = self.path("empty.docx")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        with self.assertRaises(ExtractError) as ctx:
            extract(path)
        self.assertIn("word/document.xml", str(ctx.exception))


# ==========================================================================
# 6. Index（SQLite + FTS5）
# ==========================================================================

class IndexTest(TempCase):
    """docseek.index.db.Index：upsert / 指纹 / 粗筛 / 维护。

    这里的 path 都是假的字符串（不落盘），Index 层本身不访问文件系统。
    """

    def make_meta(self, path, size=10, mtime=1000.0, ext="txt", chars=5):
        return {"path": path, "name": os.path.basename(path), "ext": ext,
                "kind": "text", "size": size, "mtime": mtime, "ino": 1,
                "dev": 1, "chars": chars}

    def fts_rows(self, index):
        return index.conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0]

    def test_upsert_inserts_searchable_row(self):
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        doc_id = index.upsert(self.make_meta(path),
                              tokens=tokenizer.index_tokens("中华人民共和国万岁"))
        self.assertEqual(index.count(), 1)
        self.assertEqual(index.get_doc(path)["id"], doc_id)
        self.assertEqual(index.search_ids(tokenizer.fts_query(["中华人民共和国"])),
                         [doc_id])
        self.assertEqual(self.fts_rows(index), 1)

    def test_upsert_same_path_updates_without_duplicate(self):
        """同 path 二次 upsert：不新增行，且 FTS 里的旧 token 被替换。"""
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        first = index.upsert(self.make_meta(path, size=10),
                             tokens=tokenizer.index_tokens("中华人民共和国"))
        second = index.upsert(self.make_meta(path, size=20, chars=8),
                              tokens=tokenizer.index_tokens("北京大学"))
        self.assertEqual(first, second)
        self.assertEqual(index.count(), 1)
        self.assertEqual(self.fts_rows(index), 1)
        self.assertEqual(index.search_ids(tokenizer.fts_query(["中华人民共和国"])), [])
        self.assertEqual(index.search_ids(tokenizer.fts_query(["北京大学"])), [second])
        row = index.get_doc(path)
        self.assertEqual(row["size"], 20)
        self.assertEqual(row["chars"], 8)

    def test_upsert_without_tokens_keeps_fts_untouched(self):
        """错误记录（tokens=None）不应写 FTS，也不应清掉已有 token。"""
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        doc_id = index.upsert(self.make_meta(path),
                              tokens=tokenizer.index_tokens("北京大学"))
        index.upsert(self.make_meta(path), tokens=None, state="error", err="坏了")
        self.assertEqual(self.fts_rows(index), 1, "tokens=None 不该动 FTS")
        self.assertEqual([r[0] for r in index.conn.execute("SELECT rowid FROM fts")],
                         [doc_id])
        self.assertEqual(index.count("error"), 1)
        # 变成 error 后即使 FTS 里还有 token，粗筛也不该再返回它
        self.assertEqual(index.search_ids(tokenizer.fts_query(["北京大学"])), [])

    def test_needs_update_fingerprint_semantics(self):
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        index.upsert(self.make_meta(path, size=10, mtime=1000.0),
                     tokens=tokenizer.index_tokens("北京大学"))
        self.assertFalse(index.needs_update(path, 10, 1000.0))
        self.assertFalse(index.needs_update(path, 10, 1000.0 + 1e-9), "容差 1e-6 内算没变")
        self.assertTrue(index.needs_update(path, 11, 1000.0), "size 变了")
        self.assertTrue(index.needs_update(path, 10, 1000.5), "mtime 变了")

    def test_needs_update_unknown_path(self):
        index = self.open_index()
        self.assertTrue(index.needs_update("/tmp/docseek-fake/没有.txt", 1, 1.0))

    def test_needs_update_after_error_state(self):
        """上次失败且指纹未变：默认不重试，`retry_errors=True` 才重试。

        坏文件每次索引都重抽太浪费（一个 .doc 走 LibreOffice 要一秒多），
        所以默认跳过，由 `--retry-errors` 或 `--rebuild` 显式触发。
        """
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        index.upsert(self.make_meta(path, size=10, mtime=1000.0),
                     tokens=None, state="error", err="坏了")
        self.assertFalse(index.needs_update(path, 10, 1000.0),
                         "默认不应反复重试失败文件")
        self.assertTrue(index.needs_update(path, 10, 1000.0, retry_errors=True))
        self.assertTrue(index.needs_update(path, 20, 1000.0),
                        "文件变了就应该重试")

    def test_search_ids_only_returns_ok_state(self):
        """state != ok 的文档即使有 token 也不该被粗筛返回。"""
        index = self.open_index()
        good = index.upsert(self.make_meta("/tmp/docseek-fake/好.txt"),
                            tokens=tokenizer.index_tokens("北京大学"))
        bad = index.upsert(self.make_meta("/tmp/docseek-fake/坏.txt"),
                           tokens=tokenizer.index_tokens("北京大学"),
                           state="error", err="坏了")
        ids = index.search_ids(tokenizer.fts_query(["北京大学"]))
        self.assertEqual(ids, [good])
        self.assertNotIn(bad, ids)

    def test_search_ids_ext_filter(self):
        index = self.open_index()
        txt_id = index.upsert(self.make_meta("/tmp/docseek-fake/甲.txt", ext="txt"),
                              tokens=tokenizer.index_tokens("北京大学"))
        md_id = index.upsert(self.make_meta("/tmp/docseek-fake/乙.md", ext="md"),
                             tokens=tokenizer.index_tokens("北京大学"))
        expr = tokenizer.fts_query(["北京大学"])
        self.assertEqual(sorted(index.search_ids(expr)), sorted([txt_id, md_id]))
        self.assertEqual(index.search_ids(expr, exts=["txt"]), [txt_id])
        self.assertEqual(index.search_ids(expr, exts=["TXT"]), [txt_id], "大写应被归一")
        self.assertEqual(index.search_ids(expr, exts=["md"]), [md_id])
        self.assertEqual(index.search_ids(expr, exts=["docx"]), [])

    def test_search_ids_path_filter(self):
        index = self.open_index()
        inside = index.upsert(
            self.make_meta("/tmp/docseek-fake/资料/甲.txt"),
            tokens=tokenizer.index_tokens("北京大学"))
        index.upsert(self.make_meta("/tmp/docseek-fake/其它/乙.txt"),
                     tokens=tokenizer.index_tokens("北京大学"))
        expr = tokenizer.fts_query(["北京大学"])
        self.assertEqual(index.search_ids(expr, path_substr=["资料"]), [inside])
        self.assertEqual(index.search_ids(expr, path_substr=["不存在的目录"]), [])

    def test_search_ids_limit(self):
        index = self.open_index()
        for i in range(5):
            index.upsert(self.make_meta("/tmp/docseek-fake/%d.txt" % i),
                         tokens=tokenizer.index_tokens("北京大学"))
        self.assertEqual(len(index.search_ids(tokenizer.fts_query(["北京大学"]),
                                              limit=2)), 2)

    def test_iter_docs_filters(self):
        index = self.open_index()
        index.upsert(self.make_meta("/tmp/docseek-fake/甲.txt", ext="txt", mtime=1.0),
                     tokens=tokenizer.index_tokens("北京大学"))
        index.upsert(self.make_meta("/tmp/docseek-fake/乙.md", ext="md", mtime=2.0),
                     tokens=tokenizer.index_tokens("北京大学"))
        index.upsert(self.make_meta("/tmp/docseek-fake/丙.txt"),
                     tokens=None, state="error")
        self.assertEqual([r["name"] for r in index.iter_docs()], ["乙.md", "甲.txt"])
        self.assertEqual([r["name"] for r in index.iter_docs(exts=["txt"])], ["甲.txt"])
        self.assertEqual([r["name"] for r in index.iter_docs(state="error")], ["丙.txt"])

    def test_delete_doc_cleans_fts_row(self):
        index = self.open_index()
        doc_id = index.upsert(self.make_meta("/tmp/docseek-fake/甲.txt"),
                              tokens=tokenizer.index_tokens("北京大学"))
        self.assertEqual(self.fts_rows(index), 1)
        index.delete_doc(doc_id)
        self.assertEqual(index.count(), 0)
        self.assertEqual(self.fts_rows(index), 0)
        self.assertEqual(index.search_ids(tokenizer.fts_query(["北京大学"])), [])

    def test_delete_by_path(self):
        index = self.open_index()
        path = "/tmp/docseek-fake/甲.txt"
        index.upsert(self.make_meta(path), tokens=tokenizer.index_tokens("北京大学"))
        index.delete_by_path(path)
        self.assertEqual(index.count(), 0)
        self.assertEqual(self.fts_rows(index), 0)
        index.delete_by_path("/tmp/docseek-fake/不存在.txt")   # 不应抛异常

    def test_stats_counts(self):
        index = self.open_index()
        index.upsert(self.make_meta("/tmp/docseek-fake/甲.txt", chars=5),
                     tokens=tokenizer.index_tokens("北京大学"))
        index.upsert(self.make_meta("/tmp/docseek-fake/乙.txt", chars=3),
                     tokens=tokenizer.index_tokens("清华大学"))
        index.upsert(self.make_meta("/tmp/docseek-fake/丙.txt", chars=0),
                     tokens=None, state="error", err="坏了")
        stats = index.stats()
        self.assertEqual(stats["docs"], 3)
        self.assertEqual(stats["chars"], 8)
        self.assertEqual(stats["by_state"], {"ok": 2, "error": 1})
        self.assertEqual(stats["by_ext"], [("txt", 3)])
        self.assertIsNotNone(stats["last_indexed"])
        self.assertGreater(stats["db_bytes"], 0)

    def test_reset_clears_docs_and_fts(self):
        index = self.open_index()
        index.upsert(self.make_meta("/tmp/docseek-fake/甲.txt"),
                     tokens=tokenizer.index_tokens("北京大学"))
        index.set_meta("last_index", 123)
        index.reset(vacuum=True)
        self.assertEqual(index.count(), 0)
        self.assertEqual(self.fts_rows(index), 0)
        self.assertEqual(index.search_ids(tokenizer.fts_query(["北京大学"])), [])
        self.assertEqual(index.stats()["by_state"], {})
        self.assertEqual(index.get_meta("last_index"), "123", "meta 不随 reset 清空")

    def test_meta_roundtrip(self):
        index = self.open_index()
        self.assertIsNone(index.get_meta("缺失"))
        self.assertEqual(index.get_meta("缺失", "默认"), "默认")
        index.set_meta("last_index", 456)
        self.assertEqual(index.get_meta("last_index"), "456")
        index.set_meta("last_index", 789)
        self.assertEqual(index.get_meta("last_index"), "789")

    def test_context_manager_closes_connection(self):
        path = self.path("ctx.db")
        with Index(path) as index:
            self.assertEqual(index.count(), 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            index.count()


# ==========================================================================
# 7. Builder + Searcher 端到端
# ==========================================================================

class SearchEndToEndTest(TempCase):
    """合成最小样本 → Builder 建库 → Searcher 检索（含粗筛假阳性校验）。"""

    DOCX_PARAS = ["这是人民代表大会的报告", "包含 WPS 与 中文关键词"]
    NOTES_TEXT = ("人民代表大会 在这里\n"
                  "人民代表大会 又出现一次\n"
                  "TARGETWORD 出现\n")
    README_TEXT = "# 标题\n只有 TARGETWORD 一个词\n"
    # 典型 -layout 抽取结果：字间被插了空格；入库前会被 strip_cjk_gaps 抹平
    SPACED_ONLY_TEXT = "人 民 代 表 大 会\n"
    # 全部 bigram 都在，但整词并不连续（粗筛会命中，原文校验必须否决）
    SCATTER_TEXT = "人民 民代 代表 表大 大会\n"

    def setUp(self):
        super().setUp()
        self.files = {}
        self.files["报告.docx"] = self.make_docx("报告.docx", self.DOCX_PARAS)
        self.files["notes.txt"] = self.write_text("notes.txt", self.NOTES_TEXT)
        self.files["readme.md"] = self.write_text("readme.md", self.README_TEXT)
        self.files["scatter.txt"] = self.write_text("scatter.txt", self.SCATTER_TEXT)
        self.files["spaced.txt"] = self.write_text("spaced.txt", self.SPACED_ONLY_TEXT)
        self.files["old.txt"] = self.write_text("old.txt", "排序专用词 只出现一次\n")
        self.files["new.txt"] = self.write_text("new.txt", "排序专用词 只出现一次\n")
        # 固定 mtime，用来验证“命中数相同按时间倒序”
        os.utime(self.files["old.txt"], (1500000000, 1500000000))
        os.utime(self.files["new.txt"], (1600000000, 1600000000))

        self.cfg = core_cfg()
        self.index = self.open_index()
        self.builder = Builder(self.index, self.cfg)
        self.progress = self.builder.run(roots=[self.tmp])
        self.searcher = Searcher(self.index, self.cfg)

    # ------------------------------------------------------------ 小工具
    def names(self, raw):
        results, _query, _elapsed = self.searcher.search(raw)
        return [r.name for r in results]

    def test_build_indexes_every_sample(self):
        self.assertEqual(self.progress.phase, "done")
        self.assertEqual(self.progress.total, len(self.files))
        self.assertEqual(self.progress.indexed, len(self.files))
        self.assertEqual(self.progress.failed, 0)
        self.assertEqual(self.index.count("ok"), len(self.files))

    def test_phrase_hit_with_highlight_segment(self):
        """中文短语命中，且片段里必须有 kind==1 的命中段。"""
        results, query, _elapsed = self.searcher.search("人民代表大会")
        names = [r.name for r in results]
        self.assertIn("notes.txt", names)
        self.assertIn("报告.docx", names)
        self.assertEqual(query.terms, ["人民代表大会"])

        hit = [r for r in results if r.name == "notes.txt"][0]
        self.assertTrue(hit.snippets, "应有命中片段")
        kinds = [seg[0] for seg in hit.snippets[0]["segments"]]
        self.assertIn(1, kinds)
        for kind, text, _pos in hit.snippets[0]["segments"]:
            if kind == 1:
                self.assertEqual(text, "人民代表大会")

    def test_snippet_offsets_point_back_into_source_text(self):
        """片段里的绝对偏移必须能还原原文（UI 靠它高亮定位）。"""
        results, _query, _elapsed = self.searcher.search("人民代表大会")
        hit = [r for r in results if r.name == "notes.txt"][0]
        text = extract(self.files["notes.txt"]).text
        for snippet in hit.snippets:
            for kind, seg_text, pos in snippet["segments"]:
                self.assertEqual(text[pos:pos + len(seg_text)], seg_text,
                                 "偏移 %d 与原文不符（kind=%d）" % (pos, kind))
            self.assertEqual(snippet["segments"][0][2], snippet["start"])
        # 无分页信息的后端，页号必须是 None
        self.assertIsNone(hit.snippets[0]["page"])

    def test_multiple_term_query_is_and_semantics(self):
        """多词查询是 AND：只含其中一个词的文件不得出现。"""
        self.assertEqual(self.names("人民代表大会 TARGETWORD"), ["notes.txt"])

    def test_negative_term_excludes_file(self):
        """-排除词 生效：含排除词的 notes.txt 被剔除。"""
        self.assertEqual(self.names("TARGETWORD -人民代表大会"), ["readme.md"])

    def test_ext_filter(self):
        """ext:txt 只放行 txt（notes.txt），把 md 挡掉。"""
        self.assertEqual(self.names("TARGETWORD ext:txt"), ["notes.txt"])
        self.assertEqual(self.names("TARGETWORD ext:md"), ["readme.md"])
        self.assertEqual(self.names("TARGETWORD ext:pdf,docx"), [])

    def test_western_term_is_case_insensitive(self):
        self.assertEqual(self.names("wps"), ["报告.docx"])
        self.assertEqual(self.names("WPS"), ["报告.docx"])

    def test_missing_term_returns_nothing(self):
        self.assertEqual(self.names("完全不存在的词汇zzz"), [])
        self.assertEqual(self.names(""), [])

    def test_ghost_fts_tokens_are_rejected_by_verification(self):
        """假阳性校验（一）：FTS 里有 token 但正文没有该词 → 不得返回。"""
        ghost_path = self.write_text("ghost.txt", "这里没有那个词，只有无关内容\n")
        stat = os.stat(ghost_path)
        ghost_id = self.index.upsert(
            {"path": ghost_path, "name": "ghost.txt", "ext": "txt",
             "kind": "text", "size": stat.st_size, "mtime": stat.st_mtime,
             "chars": 3},
            tokens=tokenizer.index_tokens("中华"), state="ok")

        expr = tokenizer.fts_query(["中华"])
        self.assertIn(ghost_id, self.index.search_ids(expr),
                      "前置条件：该文档必须能通过 FTS 粗筛")
        self.assertEqual(find_spans(extract(ghost_path).text, "中华"), [],
                         "前置条件：正文里确实没有该词")
        self.assertEqual(self.names("中华"), [],
                         "粗筛命中但原文校验不通过，不得进入结果")

    def test_bigram_scatter_is_rejected_by_verification(self):
        """假阳性校验（二）：所有 bigram 都在但整词不连续 → 不得返回。

        detail=none 不存位置，粗筛无法判断相邻关系，所以必须有原文校验兜底。
        """
        expr = tokenizer.fts_query(["人民代表大会"])
        scatter_id = self.index.get_doc(self.files["scatter.txt"])["id"]
        self.assertIn(scatter_id, self.index.search_ids(expr),
                      "前置条件：bigram 齐全，粗筛应命中")
        self.assertEqual(find_spans(self.SCATTER_TEXT, "人民代表大会"), [])
        self.assertNotIn("scatter.txt", self.names("人民代表大会"))

    def test_matches_count_and_sorting_by_hits(self):
        """matches 是命中次数；结果按命中数倒序。"""
        results, _query, _elapsed = self.searcher.search("人民代表大会")
        by_name = {r.name: r.matches for r in results}
        self.assertEqual(by_name.get("notes.txt"), 2)
        self.assertEqual(by_name.get("报告.docx"), 1)
        self.assertEqual(results[0].name, "notes.txt")
        matches = [r.matches for r in results]
        self.assertEqual(matches, sorted(matches, reverse=True))
        self.assertEqual(results[0].page, None)
        self.assertEqual(results[0].ext, "txt")

    def test_sorting_by_mtime_when_hits_tie(self):
        results, _query, _elapsed = self.searcher.search("排序专用词")
        self.assertEqual([r.name for r in results], ["new.txt", "old.txt"])
        self.assertEqual([r.matches for r in results], [1, 1])
        self.assertGreater(results[0].mtime, results[1].mtime)

    def test_regex_search_scans_ignoring_index(self):
        results, query, _elapsed = self.searcher.search("re:人民代表大会")
        self.assertIsNotNone(query.regex)
        self.assertEqual(sorted(r.name for r in results),
                         ["notes.txt", "报告.docx"])

    def test_single_char_query_falls_back_to_scan(self):
        """单汉字无法命中 bigram 索引，回退全量扫描并带 slow 提示。"""
        results, query, _elapsed = self.searcher.search("中")
        self.assertTrue(query.slow)
        self.assertTrue(query.message)
        self.assertEqual([r.name for r in results], ["报告.docx"])
        self.assertEqual(results[0].matches, 1)

    def test_loose_match_reachable_through_pipeline(self):
        """宽松匹配在粗筛能命中时确实生效。

        “人民代 表大会”整词不连续，但 5 个 bigram 都能在文中其它位置取到
        （代表专家 → 代表），因此能过粗筛，再由 find_spans 宽松匹配命中。
        """
        path = self.write_text("layout2.txt", "人民代 表大会\n代表专家\n")
        Builder(self.index, self.cfg).run(roots=[self.tmp])
        text = extract(path).text
        self.assertEqual(find_spans(text, "人民代表大会"), [(0, 7)])
        results, _query, _elapsed = self.searcher.search("人民代表大会")
        by_name = {r.name: r for r in results}
        self.assertIn("layout2.txt", by_name)
        self.assertEqual(by_name["layout2.txt"].matches, 1)

    def test_loose_match_reachable_through_coarse_filter(self):
        """【原缺口已修复】只含单字 token 的 -layout 文档现在能进候选集。

        过去 index_tokens("人 民 代 表 大 会") 只产出 人/民/代/表/大/会 六个单字
        token，与查询的 bigram 表达式（人民/民代/代表/表大/大会）永不相交，文档
        进不了候选集，宽松的原文校验根本没机会执行。现在入库前会抹掉汉字之间的
        空白（tokenizer.strip_cjk_gaps），粗筛先命中，再由原文校验确认。
        """
        self.assertTrue(find_spans(self.SPACED_ONLY_TEXT, "人民代表大会"),
                        "前置条件：原文校验本身能宽松命中")
        self.assertEqual(tokenizer.index_tokens(self.SPACED_ONLY_TEXT),
                         "人民 民代 代表 表大 大会")
        expr = tokenizer.fts_query(["人民代表大会"])
        spaced_id = self.index.get_doc(self.files["spaced.txt"])["id"]
        self.assertIn(spaced_id, self.index.search_ids(expr),
                      "粗筛应把 spaced.txt 列为候选")
        by_name = {r.name: r for r in self.searcher.search("人民代表大会")[0]}
        self.assertIn("spaced.txt", by_name)
        self.assertEqual(by_name["spaced.txt"].matches, 1)

    def test_incremental_second_run_is_unchanged(self):
        """指纹未变时二次建库不重新抽取。"""
        progress = Builder(self.index, self.cfg).run(roots=[self.tmp])
        self.assertEqual(progress.indexed, 0)
        self.assertEqual(progress.unchanged, len(self.files))
        self.assertEqual(self.index.count(), len(self.files))

    def test_incremental_reindexes_changed_file_only(self):
        """mtime 变化 → 只重抽这一个文件。"""
        stat = os.stat(self.files["notes.txt"])
        os.utime(self.files["notes.txt"], (stat.st_mtime + 10,) * 2)
        progress = Builder(self.index, self.cfg).run(roots=[self.tmp])
        self.assertEqual(progress.indexed, 1)
        self.assertEqual(progress.unchanged, len(self.files) - 1)
        self.assertEqual(self.index.count(), len(self.files), "不应产生重复行")

    def test_error_file_recorded_as_error_state(self):
        """抽取失败的文件记为 state=error，不进检索结果。"""
        self.write_bytes("bad.docx", b"not a zip at all")
        Builder(self.index, self.cfg).run(roots=[self.tmp])
        row = self.index.get_doc(self.path("bad.docx"))
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "error")
        self.assertTrue(row["err"])
        self.assertNotIn("bad.docx", self.names("人民代表大会"))

    def test_sweep_removes_records_of_missing_files(self):
        """sweep 删除磁盘上已不存在的记录（这里用一条虚构记录验证）。"""
        gone = self.path("gone.txt")
        self.index.upsert({"path": gone, "name": "gone.txt", "ext": "txt",
                           "kind": "text", "size": 1, "mtime": 1.0, "chars": 1},
                          tokens=tokenizer.index_tokens("幽灵"), state="ok")
        before = self.index.count()
        removed = self.builder.sweep([self.tmp])
        self.assertEqual(removed, 1)
        self.assertEqual(self.index.count(), before - 1)
        self.assertIsNone(self.index.get_doc(gone))

    def test_progress_reports_counts(self):
        self.assertEqual(self.progress.scanned, len(self.files))
        self.assertEqual(self.progress.percent, 100.0)
        self.assertTrue(self.progress.elapsed >= 0)
        self.assertIn("清理失效记录", self.progress.message)


# ==========================================================================
# 8. 真实 PDF 样本（只读；找不到就 skip）
# ==========================================================================

_pdf_sample = {"done": False, "path": None}


def _scan_pdfs(limit=SCAN_LIMIT, budget=SCAN_BUDGET):
    """在有界范围内收集若干 PDF 路径（限时/限深/限量）。"""
    deadline = time.time() + budget
    found = []
    seen_roots = set()
    for root in [r for r in SCAN_ROOTS if r]:
        root = Path(root)
        try:
            real = str(root.resolve())
        except OSError:
            continue
        if real in seen_roots or not root.is_dir():
            continue
        seen_roots.add(real)
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(str(root),
                                                    onerror=lambda exc: None):
            if len(found) >= limit or time.time() > deadline:
                return found
            if len(Path(dirpath).parts) - base_depth >= SCAN_MAX_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in SCAN_PRUNE]
            for name in sorted(filenames):
                if name.lower().endswith(".pdf") and not name.startswith("~$"):
                    found.append(os.path.join(dirpath, name))
                    if len(found) >= limit:
                        return found
    return found


def find_pdf_sample():
    """找一个能抽出连续中文的真实 PDF（优先多页且正文长），找不到返回 None。"""
    if _pdf_sample["done"]:
        return _pdf_sample["path"]
    _pdf_sample["done"] = True
    best = None
    no_ocr = core_cfg()          # 抽样阶段一律关 OCR，避免对着任意 PDF 跑识别
    for path in _scan_pdfs():
        try:
            result = extract(path, options=no_ocr)
        except ExtractError:
            continue
        except Exception:                       # noqa: BLE001 坏样本直接跳过
            continue
        if not result.text.strip():
            continue
        if not CJK_RUN_RE.search(result.text):
            continue                            # 端到端用例需要连续中文串
        score = (1 if len(result.page_offsets) >= 2 else 0,
                 min(len(result.text), 100000))
        if best is None or score > best[0]:
            best = (score, path)
            if score[0] and score[1] >= 200:
                break
    _pdf_sample["path"] = best[1] if best else None
    return _pdf_sample["path"]


class PdfSampleTest(TempCase):
    """真实 PDF：分页偏移单调递增 + 端到端检索带页码（只读样本）。"""

    def setUp(self):
        super().setUp()
        self.pdf = find_pdf_sample()
        if not self.pdf:
            self.skipTest("在 cwd/家目录//data 下没找到可抽取文本的 PDF 样本")
        self.before = os.stat(self.pdf)
        self.result = extract(self.pdf, options=core_cfg())

    def test_pdf_text_extracted_and_source_untouched(self):
        self.assertEqual(self.result.kind, "pdf")
        self.assertTrue(self.result.text.strip())
        after = os.stat(self.pdf)
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (self.before.st_size, self.before.st_mtime_ns),
                         "抽取必须只读，不得改动样本")

    def test_page_offsets_start_at_zero_and_increase(self):
        offsets = self.result.page_offsets
        self.assertTrue(offsets, "有文本的 PDF 应有分页偏移")
        self.assertEqual(offsets[0], 0)
        for prev, cur in zip(offsets, offsets[1:]):
            self.assertGreater(cur, prev, "page_offsets 必须严格递增")
        self.assertLess(offsets[-1], len(self.result.text) + 1)

    def test_page_of_consistent_with_offsets(self):
        """page_of 的语义：返回满足 offsets[i-1] <= offset 的最大页号 i。"""
        offsets = self.result.page_offsets
        for i, start in enumerate(offsets, start=1):
            self.assertEqual(self.result.page_of(start), i)
        last = len(self.result.text) - 1
        expected = max(i for i, start in enumerate(offsets, start=1)
                       if start <= last)
        self.assertEqual(self.result.page_of(last), expected)
        self.assertLessEqual(self.result.page_of(last), len(offsets))

    def test_searcher_returns_page_for_real_pdf(self):
        """真实 PDF 端到端：命中的页码要和 page_of(偏移) 一致。"""
        match = CJK_RUN_RE.search(self.result.text)
        self.assertIsNotNone(match, "样本正文里应有连续中文串")
        term = match.group(0)

        link = self.path("sample.pdf")
        try:
            os.symlink(os.path.abspath(self.pdf), link)
        except OSError:
            shutil.copyfile(self.pdf, link)

        cfg = core_cfg()
        index = self.open_index()
        Builder(index, cfg).run(roots=[self.tmp])
        results, _query, _elapsed = Searcher(index, cfg).search(term)

        hits = [r for r in results if r.name == "sample.pdf"]
        self.assertTrue(hits, "刚抽出的连续中文串应能被检索到：%r" % term)
        hit = hits[0]
        self.assertGreaterEqual(hit.matches, 1)
        self.assertIsNotNone(hit.page, "PDF 结果应带页码")
        self.assertGreaterEqual(hit.page, 1)
        self.assertLessEqual(hit.page, len(self.result.page_offsets))

        first_span = find_spans(self.result.text, term)[0]
        self.assertEqual(hit.page, self.result.page_of(first_span[0]))
        self.assertTrue(hit.snippets)
        self.assertEqual(hit.snippets[0]["page"], self.result.page_of(first_span[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ==========================================================================
# 文件名 / 路径检索（用户习惯直接拿文件名的词去搜）
# ==========================================================================

class FilenameSearchTest(TempCase):
    """正文里没有的词，只要出现在**文件名或目录名**里也要能搜到。

    实际案例：用户搜的是**完整文件名**（比正文里出现的词多一截，例如多一个"汇总"），
    按正文搜就落空。现在文件名与路径也进索引。
    """

    def setUp(self):
        super().setUp()
        folder = self.path("归档资料")
        os.makedirs(folder, exist_ok=True)
        # 正文刻意不含任何检索词
        self.target = os.path.join(folder, "季度工作报告汇总.txt")
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("会议纪要正文，很普通的一句话。\n")
        self.other = self.write_text("无关文件.txt", "另一份普通文件。\n")
        self.cfg = core_cfg()
        self.index = self.open_index()
        Builder(self.index, self.cfg).run(roots=[self.tmp], sweep=False)
        self.searcher = Searcher(self.index, core_cfg())

    def _names(self, query):
        results, _query, _elapsed = self.searcher.search(query)
        return [r.name for r in results]

    def test_filename_words_are_searchable(self):
        self.assertIn("季度工作报告汇总.txt",
                      self._names("季度工作报告汇总"),
                      "整串文件名应当能搜到")
        self.assertIn("季度工作报告汇总.txt", self._names("报告汇总"))
        self.assertIn("季度工作报告汇总.txt", self._names("季度工作报告"))

    def test_directory_name_is_searchable(self):
        self.assertIn("季度工作报告汇总.txt", self._names("归档资料"),
                      "目录名也应当能搜到")

    def test_unrelated_file_not_matched(self):
        self.assertNotIn("无关文件.txt", self._names("季度工作报告汇总"))

    def test_no_matches_for_absent_term(self):
        self.assertEqual(self._names("完全不存在的词组zzz"), [])

    def test_name_hit_still_counts_and_has_no_bogus_snippet(self):
        results, _query, _elapsed = self.searcher.search("季度工作报告汇总")
        hit = [r for r in results if r.name == "季度工作报告汇总.txt"][0]
        self.assertGreaterEqual(hit.matches, 1, "名称命中也要计入命中数")
        # 正文里没有这个词，因此不该凭空造出正文片段
        source = extract(self.target).text
        for snippet in hit.snippets:
            for kind, seg_text, pos in snippet["segments"]:
                self.assertEqual(source[pos:pos + len(seg_text)], seg_text,
                                 "片段偏移必须仍然指向正文")

    def test_content_hits_are_unaffected(self):
        path = self.write_text("正文命中.txt", "人民代表大会 在这里\n")
        Builder(self.index, self.cfg).run(roots=[self.tmp], sweep=False)
        results, _query, _elapsed = self.searcher.search("人民代表大会")
        hit = [r for r in results if r.path == path][0]
        self.assertTrue(hit.snippets, "正文命中仍应有片段")
        self.assertTrue(any(seg[0] == 1 for seg in hit.snippets[0]["segments"]))


# ==========================================================================
# 只读连接与"部分匹配"降级
# ==========================================================================

class ReadonlyIndexTest(TempCase):
    """检索走只读连接：不执行建表脚本、不拿写锁（索引运行中也能立刻搜）。"""

    def test_readonly_connection_refuses_writes(self):
        import sqlite3 as _sqlite
        path = self.path("ro.db")
        writer = Index(path)
        writer.upsert({"path": "/tmp/a.txt", "name": "a.txt", "ext": "txt",
                       "size": 1, "mtime": 1.0, "chars": 0}, tokens="a")
        writer.commit()
        reader = Index(path, readonly=True)
        self.assertEqual(reader.count(), 1)
        with self.assertRaises(_sqlite.OperationalError):
            reader.conn.execute("DELETE FROM docs")
        reader.close()
        writer.close()

    def test_readonly_open_does_not_create_schema(self):
        """空库只读打开不建表（说明它确实没跑建表脚本）。"""
        path = self.path("empty.db")
        reader = Index(path, readonly=True)
        tables = {r[0] for r in reader.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("docs", tables)
        reader.close()
        writer = Index(path)                    # 正常打开才建表
        tables = {r[0] for r in writer.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("docs", tables)
        writer.close()


class RelaxedSearchTest(TempCase):
    """严格匹配没结果时，降级为"命中任一关键词片段"，并给出提示。"""

    def setUp(self):
        super().setUp()
        self.write_text("会议.txt", "人民代表大会 在这里召开\n")
        self.cfg = core_cfg()
        self.index = self.open_index()
        Builder(self.index, self.cfg).run(roots=[self.tmp], sweep=False)
        self.searcher = Searcher(self.index, core_cfg())

    def test_strict_hit_needs_no_fallback(self):
        results, query, _elapsed = self.searcher.search("人民代表大会")
        self.assertTrue(results)
        self.assertNotIn("部分匹配", query.message or "")

    def test_absent_term_returns_nothing(self):
        results, _query, _elapsed = self.searcher.search("量子力学导论")
        self.assertEqual(results, [], "完全无关的词不该硬凑结果")

    def test_longer_phrase_falls_back_to_partial_match(self):
        """正文只有"人民代表大会"，搜"人民代表大会制度"严格匹配不成立，
        但应当给出部分匹配结果（并说明是部分匹配）。"""
        results, query, _elapsed = self.searcher.search("人民代表大会制度")
        self.assertTrue(results, "应当降级给出部分匹配")
        self.assertIn("部分匹配", query.message)
        self.assertIn("会议.txt", [r.name for r in results])

    def test_partial_match_sorted_by_hit_count(self):
        self.write_text("另一份.txt", "人民代表大会 制度 人民代表大会\n")
        Builder(self.index, self.cfg).run(roots=[self.tmp], sweep=False)
        results, _query, _elapsed = self.searcher.search("人民代表大会制度")
        self.assertGreaterEqual(len(results), 1)
        counts = [r.matches for r in results]
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "部分匹配应按命中多少排序")
