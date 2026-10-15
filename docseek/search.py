"""查询解析、精确校验与命中片段生成。

FTS5 索引用 detail=none（不存位置），只做粗筛；最终判定在这里用原文完成：

1. 每个正项都必须以"忽略大小写、空白宽松"的子串形式命中（AND 语义）；
2. 中文词整体找不到时，退化为允许字间空白，用来兜住 pdftotext -layout
   在汉字之间插入空格的情况；
3. 命中片段带绝对字符偏移，供 UI 在全文视图里滚动定位并高亮；
4. 单字中文词无法命中 bigram 索引，自动回退为扫描模式并给出提示。
"""

import bisect
import os
import re
import time
from dataclasses import dataclass, field

from . import config, textstore, tokenizer
from .extract import ExtractError, Extracted, extract

MAX_SPANS = 1000            # 单个词最多记录多少处命中
SCAN_CAP = 60000            # 扫描模式下最多检查多少份文档


# ---------------------------------------------------------------- 查询解析

_QUOTE_RE = re.compile(r'"([^"]*)"|(\S+)')


# 结果排序方式：(id, 界面文案)
SORT_MODES = (
    ("relevance", "相关度"),
    ("mtime_desc", "修改时间（新→旧）"),
    ("mtime_asc", "修改时间（旧→新）"),
    ("size_desc", "大小（大→小）"),
    ("size_asc", "大小（小→大）"),
    ("dir", "目录"),
    ("type", "类型"),
)
SORT_LABELS = dict(SORT_MODES)
DEFAULT_SORT = "relevance"


def sort_label(mode):
    return SORT_LABELS.get(mode, mode)


def sort_results(results, mode=DEFAULT_SORT):
    """按指定方式排序（不改原列表）。

    排序发生在**截断之前**（见 Searcher.search），所以"取前 N 条"是按排序后的顺序取。
    """
    if mode in (None, "", "relevance"):
        return list(results)                      # 保持检索给出的相关度顺序
    if mode == "mtime_desc":
        key = lambda r: (-(r.mtime or 0), r.name)
    elif mode == "mtime_asc":
        key = lambda r: (r.mtime or 0, r.name)
    elif mode == "size_desc":
        key = lambda r: (-(r.size or 0), r.name)
    elif mode == "size_asc":
        key = lambda r: (r.size or 0, r.name)
    elif mode == "dir":
        key = lambda r: (os.path.dirname(r.path).lower(),
                         os.path.basename(r.path).lower())
    elif mode == "type":
        key = lambda r: ((r.ext or "").lower(),
                         os.path.basename(r.path).lower())
    else:
        return list(results)
    return sorted(results, key=key)


@dataclass
class Query:
    raw: str = ""
    terms: list = field(default_factory=list)
    neg_terms: list = field(default_factory=list)
    exts: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    regex: object = None
    slow: bool = False
    message: str = ""

    def describe(self):
        bits = []
        if self.terms:
            bits.append("关键词：" + "、".join(self.terms))
        if self.neg_terms:
            bits.append("排除：" + "、".join(self.neg_terms))
        if self.exts:
            bits.append("类型：" + ",".join(self.exts))
        if self.paths:
            bits.append("路径含：" + "、".join(self.paths))
        if self.regex:
            bits.append("正则：%s" % self.regex.pattern)
        return "；".join(bits)


def parse_query(raw):
    """解析查询串。

    支持：普通关键词、`"带 空格 的短语"`、`-排除词`、
    `ext:pdf,docx`、`path:目录片段`、`re:正则`
    """
    query = Query(raw=(raw or "").strip())
    if not query.raw:
        query.message = "请输入查询关键词"
        return query

    for match in _QUOTE_RE.finditer(query.raw):
        token = match.group(1) if match.group(1) is not None else match.group(2)
        if not token:
            continue
        low = token.lower()
        if token.startswith("-") and len(token) > 1:
            query.neg_terms.append(token[1:])
        elif low.startswith("ext:"):
            for item in re.split(r"[,;，、]", token[4:]):
                item = item.strip().lstrip(".").lower()
                if item:
                    query.exts.append(item)
        elif low.startswith("path:"):
            value = token[5:].strip()
            if value:
                query.paths.append(value)
        elif low.startswith("re:"):
            try:
                query.regex = re.compile(token[3:], re.IGNORECASE)
            except re.error as exc:
                query.message = "正则表达式有误：%s" % exc
        else:
            query.terms.append(token)

    if not query.terms and not query.regex and not query.message:
        query.message = "请输入查询关键词"
    if tokenizer.single_char_terms(query.terms):
        query.slow = True
        query.message = "含单个汉字的关键词，已回退为全量扫描（较慢）"
    return query


# ---------------------------------------------------------------- 匹配

_WS_RE = re.compile(r"\s+")


def _pattern(term, loose=False):
    parts = [p for p in _WS_RE.split(term) if p]
    if not parts:
        return None
    if loose:
        # 允许任意位置出现空白：应对 -layout 抽取在汉字间插空格
        joined = r"\s*".join(re.escape(ch) for ch in "".join(parts))
        return re.compile(joined, re.IGNORECASE)
    return re.compile(r"\s+".join(re.escape(p) for p in parts), re.IGNORECASE)


def find_spans(text, term, cap=MAX_SPANS, allow_loose=None):
    """返回 term 在 text 中的所有命中区间 [(start, end)]。

    精确命中之外，还会并入"允许字间空白"的宽松命中（应对 pdftotext -layout
    在汉字间插空格），重叠部分不重复计数。宽松匹配只要求字符按序出现且其间
    可以有空白，因此每一处结果在原文里都真实存在。

    allow_loose 为 None 时自动判断正文里是否存在"汉字 空格 汉字"，
    避免不需要时多扫一遍正文。
    """
    pattern = _pattern(term)
    if pattern is None:
        return []
    spans = [(m.start(), m.end()) for m in pattern.finditer(text)]
    if tokenizer.has_cjk(term):
        if allow_loose is None:
            allow_loose = tokenizer.has_cjk_gaps(text)
        if allow_loose:
            loose = _pattern(term, loose=True)
            if loose is not None:
                spans = _merge_loose(spans, loose, text, cap)
    spans.sort()
    if len(spans) > cap:
        spans = spans[:cap]
    return spans


def _expand_runs(text, runs, query_text, head_len, max_side=8):
    """把部分匹配的碎片区间向两侧扩到"仍是查询串子串"的最长范围。

    例：搜「中华人民共和国」时只命中片段「人民共和」，若原文相邻处有"中华"，
    就扩展成「中华人民共和国」——避免高亮出零碎片段。
    """
    out = []
    for start, end in runs:
        best = (start, end)
        for left in range(1, max_side + 1):
            for right in range(max_side + 1):
                a = max(head_len, start - left)
                b = min(len(text), end + right)
                if b <= a:
                    continue
                fragment = text[a:b].replace("\n", "")
                if fragment and fragment in query_text \
                        and (b - a) > (best[1] - best[0]):
                    best = (a, b)
        out.append(best)
    return out


def _merge_ranges(spans):
    """把重叠或相邻的区间合并（相邻的 bigram 合并后就还原成完整词组）。"""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _merge_loose(exact, loose_pattern, text, cap):
    """把宽松命中并入精确命中（宽松命中是精确命中的超集，重叠的不再计一次）。"""
    ordered = sorted(exact)
    merged = list(ordered)
    limit = cap * 4
    for match in loose_pattern.finditer(text):
        if len(merged) > limit:
            break
        start, end = match.start(), match.end()
        index = bisect.bisect_left(ordered, (start, -1))
        overlap = False
        for probe in (index - 1, index):
            if 0 <= probe < len(ordered):
                s, e = ordered[probe]
                if s < end and start < e:
                    overlap = True
                    break
        if not overlap:
            merged.append((start, end))
    return merged


def _snippet(text, span, all_spans, ctx):
    start = max(0, span[0] - ctx)
    end = min(len(text), span[1] + ctx)
    segments = []
    pos = start
    for item in all_spans:
        s, e = item[0], item[1]
        if e <= start or s >= end:
            continue
        s2, e2 = max(s, start), min(e, end)
        if s2 > pos:
            segments.append((0, text[pos:s2], pos))
        if e2 > s2:
            segments.append((1, text[s2:e2], s2))
        pos = max(pos, e2)
    if pos < end:
        segments.append((0, text[pos:end], pos))
    if not any(seg[0] == 1 for seg in segments):
        return None
    return {"segments": segments, "start": start, "end": end,
            "page": span[2] if len(span) > 2 else None}


# ---------------------------------------------------------------- 结果

@dataclass
class Result:
    doc_id: int
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    kind: str = ""
    matches: int = 0
    snippets: list = field(default_factory=list)
    page: object = None
    warning: str = ""
    backend: str = ""

    @property
    def mtime_text(self):
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))
        except (TypeError, ValueError):
            return ""


class Searcher:
    def __init__(self, index, cfg=None, cache_size=32):
        self.index = index
        self.cfg = cfg or config.load()
        self.cache_size = cache_size
        self._cache = {}

    # -------------------------------------------------- 抽取缓存
    def _text(self, row):
        """取原文：优先读索引库里存的正文，没有再回退到现抽。

        索引时已经把正文压缩存进 docs.content（见 textstore.py），检索因此不需要
        每次调用 LibreOffice/pdftotext——实测把一次两词查询从 30 秒降到毫秒级。
        """
        key = (row["path"], row["mtime"])
        got = self._cache.get(key)
        if got is not None:
            return got
        extracted = self._stored(row)
        if extracted is None:
            extracted = extract(row["path"], options=self.cfg)
        if len(self._cache) >= self.cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = extracted
        return extracted

    @staticmethod
    def _stored(row):
        """从库里还原正文；缺正文（老记录/超长）时返回 None。"""
        try:
            blob = row["content"]
        except (IndexError, KeyError):
            return None
        text = textstore.unpack_content(blob)
        if text is None:
            return None
        try:
            pages = textstore.unpack_pages(row["pages"])
            warning = row["warning"] or ""
            kind = row["kind"] or ""
        except (IndexError, KeyError):
            pages, warning, kind = [], "", ""
        return Extracted(text=text, kind=kind, page_offsets=pages,
                         warning=warning)

    # -------------------------------------------------- 检索
    def search(self, raw, limit=None, on_progress=None, sort=None):
        query = parse_query(raw)
        limit = int(limit or self.cfg.get("max_results", 500))
        started = time.time()
        if query.message and not (query.terms or query.regex):
            return [], query, 0.0

        rows = self._candidates(query, limit)
        results = []
        checked = 0
        for row in rows:
            checked += 1
            if on_progress and checked % 20 == 0:
                on_progress(checked, len(rows))
            try:
                extracted = self._text(row)
            except ExtractError:
                continue
            except Exception:                       # noqa: BLE001 逐文件隔离
                continue
            # 校验用"文件名 + 路径 + 正文"：与建索引时喂给 FTS 的文本保持一致，
            # 否则只在文件名里出现的词会被当成假阳性丢掉。
            # 匹配判定用"文件名 + 路径 + 正文"（与建索引时喂给 FTS 的文本一致），
            # 但片段只保留**正文坐标**——UI 的高亮是按正文偏移定位的。
            header = "%s\n%s\n" % (row["name"] or "", row["path"])
            head_len = len(header)
            content = extracted.text
            text = header + content
            if not text.strip():
                continue

            # 每份文档只判断一次"是否存在汉字间空白"，避免每个查询词各扫一遍正文
            allow_loose = tokenizer.has_cjk_gaps(text)

            if query.neg_terms and any(find_spans(text, t, cap=1,
                                                  allow_loose=allow_loose)
                                       for t in query.neg_terms):
                continue

            name_hits = 0
            if query.regex is not None:
                spans = [(m.start(), m.end()) for m in query.regex.finditer(text)]
                if not spans:
                    continue
                per_term = {"正则": [(s - head_len, e - head_len)
                                     for s, e in spans if s >= head_len][:MAX_SPANS]}
                name_hits = sum(1 for s, _e in spans if s < head_len)
            else:
                per_term = {}
                missing = False
                for term in query.terms:
                    spans = find_spans(text, term, allow_loose=allow_loose)
                    if not spans:
                        missing = True
                        break
                    in_content = [(s - head_len, e - head_len)
                                  for s, e in spans if s >= head_len]
                    name_hits += len(spans) - len(in_content)
                    per_term[term] = in_content
                if missing:
                    continue

            results.append(self._build(row, extracted, per_term,
                                       name_hits=name_hits))
            if len(results) >= limit * 3:
                break

        if not results and query.terms and query.regex is None:
            # 严格 AND 没结果时降级：命中任一关键词片段也算，按命中多少排序。
            # 用户抱怨"搜不到"多数是差一两个字，宁可给个近似结果也别空手。
            relaxed = self._relaxed(query, limit)
            if relaxed:
                query.message = ("没有完全匹配的结果，以下是部分匹配"
                                 "（命中其中任意关键词片段）")
                relaxed.sort(key=lambda r: (-r.matches, -r.mtime))
                relaxed = sort_results(relaxed, sort or DEFAULT_SORT)
                return relaxed[:limit], query, time.time() - started

        results.sort(key=lambda r: (-r.matches, -r.mtime))   # 先按相关度
        results = sort_results(results, sort or DEFAULT_SORT)  # 再按用户选择
        return results[:limit], query, time.time() - started

    def _relaxed(self, query, limit):
        """宽松模式：OR 掉所有关键词片段，命中任一即算候选。"""
        toks = []
        for term in query.terms:
            toks.extend(tokenizer.query_tokens(term))
        toks = list(dict.fromkeys(t for t in toks if t))
        if not toks:
            return []
        expr = " OR ".join('"%s"' % t.replace('"', '""') for t in toks)
        ids = self.index.search_ids(expr, exts=query.exts,
                                    path_substr=query.paths,
                                    limit=max(limit * 10, 400))
        results = []
        for row in (self.index.get_doc_by_id(i) for i in ids):
            if row is None:
                continue
            try:
                extracted = self._text(row)
            except (ExtractError, Exception):        # noqa: BLE001
                continue
            header = "%s\n%s\n" % (row["name"] or "", row["path"])
            head_len = len(header)
            text = header + extracted.text
            if not text.strip():
                continue
            allow_loose = tokenizer.has_cjk_gaps(text)
            per_term = {}
            name_hits = 0
            for term in query.terms:
                # 关键：部分匹配要按**切分后的片段**校验，而不是整串。
                # 搜"人民代表大会制度"时，正文只有"人民代表大会"——整串找不到，
                # 但 人民/民代/…/大会 这些片段都在，把它们合并成连续的命中区间。
                collected = []
                for token in tokenizer.query_tokens(term):
                    for start, end in find_spans(text, token,
                                                 allow_loose=allow_loose):
                        if start < head_len:
                            name_hits += 1
                        else:
                            collected.append((start - head_len,
                                              end - head_len))
                merged = _merge_ranges(collected)
                if merged:
                    per_term[term] = _expand_runs(text, merged, term,
                                                  head_len)[:MAX_SPANS]
            if not per_term:
                continue
            results.append(self._build(row, extracted, per_term,
                                       name_hits=name_hits))
            if len(results) >= limit * 3:
                break
        return results

    def _candidates(self, query, limit):
        if not query.slow and query.regex is None:
            expr = tokenizer.fts_query(query.terms)
            if expr:
                ids = self.index.search_ids(
                    expr, exts=query.exts, path_substr=query.paths,
                    limit=max(limit * 10, 400))
                return [row for row in (self.index.get_doc_by_id(i) for i in ids)
                        if row is not None]
        # 扫描模式：正则、单字词，或 FTS 表达式为空
        return list(self.index.iter_docs(exts=query.exts,
                                         path_substr=query.paths,
                                         limit=SCAN_CAP))

    def _build(self, row, extracted, per_term, name_hits=0):
        """per_term 里的区间都是**正文坐标**（文件名/路径命中不计入片段）。

        `name_hits` 只在文件名或路径里出现的命中数：这类命中算"搜到了"，
        但片段仍取自正文；一个片段都没有时由界面显示文件名。
        """
        all_spans = []
        for spans in per_term.values():
            for span in spans:
                page = extracted.page_of(span[0])
                all_spans.append((span[0], span[1], page))
        all_spans.sort()

        ctx = int(self.cfg.get("snippet_context", 40))
        max_snips = int(self.cfg.get("snippets_per_file", 3))
        snippets = []
        consumed_until = -1
        for span in all_spans:
            if len(snippets) >= max_snips:
                break
            if span[0] < consumed_until:
                continue
            snippet = _snippet(extracted.text, span, all_spans, ctx)
            if snippet:
                snippet["page"] = span[2]
                snippets.append(snippet)
                consumed_until = snippet["end"]

        return Result(
            doc_id=row["id"], path=row["path"], name=row["name"] or "",
            ext=row["ext"] or "", size=row["size"] or 0,
            mtime=row["mtime"] or 0, kind=row["kind"] or "",
            matches=sum(len(s) for s in per_term.values()) + name_hits,
            snippets=snippets, page=all_spans[0][2] if all_spans else None,
            warning=row["warning"] or "", backend=row["backend"] or "")
