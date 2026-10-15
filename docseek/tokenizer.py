"""分词：中文按 2-gram，西文/数字按词。

本机 SQLite 3.31.1 自带 FTS5，但没有 trigram 分词器、也没有编进 ICU，
因此中文不能直接交给 unicode61。做法是：

* 写入索引时，把 CJK 连续串切成 2-gram（"中华人民共和国" → 中华 华人 人民 民共 共和 和国），
  西文数字词原样小写；
* 查询时用同一套规则切分，把多个 token 以 AND 组合交给 FTS5 粗筛；
* 粗筛结果再由 search.py 用原文做精确子串校验并生成命中片段。

因为索引使用 detail=none（不存位置与词频），token 顺序无意义，
所以写入前对 token 去重可以显著压缩索引体积。
"""

import re

# CJK 及东亚文字范围：扩展A、基本区、兼容区、日文假名、韩文音节
_CJK = (
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\u3005\u3007"
    "\u3040-\u309f"
    "\u30a0-\u30ff"
    "\uac00-\ud7af"
)

CJK_CHAR_RE = re.compile("[%s]" % _CJK)
_TOKEN_SCAN_RE = re.compile("[%s]+|[0-9A-Za-z_]+" % _CJK)
_HAS_CJK_RE = re.compile("[%s]" % _CJK)

# 两个汉字之间的空白（空格、制表符、换行都算）。
# pdftotext -layout 抽取中文时经常在字与字之间插空格（"人 民 代 表 大 会"），
# 更常见的是**短语被行宽截断**（"……人民代" 换行 "表大会……"）。
# 若不处理，bigram 会被空白切断成一个个单字 token，查询的 bigram 表达式
# 就永远进不了候选集——宽松匹配的兜底逻辑根本没机会执行。
_CJK_GAP_RE = re.compile("(?<=[%s])\\s+(?=[%s])" % (_CJK, _CJK))


def has_cjk_gaps(text):
    """文本里是否存在"汉字 空白 汉字"这种抽取产物。"""
    return _CJK_GAP_RE.search(text) is not None


def strip_cjk_gaps(text):
    """删除两个汉字之间的空白（只为建索引用，不改动原文）。

    只在"左右都是汉字"时删，因此不会影响正常的西文分词。
    副作用是把相邻两行拼起来，可能多出少量跨行的 bigram——索引只用于粗筛，
    精确性由检索阶段的原文校验保证，这点噪声换来的是"被换行截断的短语也能搜到"。
    """
    if not text or not _HAS_CJK_RE.search(text):
        return text
    return _CJK_GAP_RE.sub("", text)


def has_cjk(text):
    return bool(_HAS_CJK_RE.search(text))


def _cjk_tokens(run):
    """CJK 连续串 → bigram 列表（单字串返回其本身）。"""
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def index_tokens(text):
    """把正文转换成索引用的 token 串（空格分隔、已去重）。

    入库前先抹掉汉字之间的空白，使 -layout 抽出的文本也能切出完整 bigram；
    精确性由检索阶段的原文校验保证，这里只影响"能否进候选集"。
    """
    seen = {}
    for match in _TOKEN_SCAN_RE.finditer(strip_cjk_gaps(text)):
        chunk = match.group(0)
        if CJK_CHAR_RE.match(chunk[0]):
            for tok in _cjk_tokens(chunk):
                seen[tok] = None
        else:
            seen[chunk.lower()] = None
    return " ".join(seen)


def query_tokens(term):
    """把一个查询词切成 token 列表。"""
    out = []
    for match in _TOKEN_SCAN_RE.finditer(term):
        chunk = match.group(0)
        if CJK_CHAR_RE.match(chunk[0]):
            out.extend(_cjk_tokens(chunk))
        else:
            out.append(chunk.lower())
    return out


def single_char_terms(terms):
    """返回只含单个 CJK 字的词（这类词无法命中 bigram 索引，需要回退扫描）。"""
    out = []
    for term in terms:
        if len(term) == 1 and CJK_CHAR_RE.match(term):
            out.append(term)
    return out


def fts_query(terms):
    """把查询词列表拼成 FTS5 MATCH 表达式（各 token 之间 AND）。

    用双引号包裹 token，避免 token 里的特殊字符被当作语法。
    """
    toks = []
    for term in terms:
        toks.extend(query_tokens(term))
    # 去重但保序
    uniq = list(dict.fromkeys(t for t in toks if t))
    if not uniq:
        return ""
    return " AND ".join('"%s"' % t.replace('"', '""') for t in uniq)
