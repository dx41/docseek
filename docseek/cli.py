"""DocSeek 命令行入口。

    docseek index [根目录...] [--rebuild] [--workers N]
    docseek search "关键词" [--limit N] [--json]
    docseek stats
    docseek check 文件或目录...      # 抽取自检（验证某格式能否抽到文本）
    docseek gui

所有子命令都支持 `--db 路径` 指定索引库，便于用独立库做试验。
"""

import argparse
import glob
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__, config, doctor, procs, textstore, tokenizer
from .index import Builder, Index
from .search import SORT_LABELS, SORT_MODES, Searcher
from .tokenizer import index_tokens


# ---------------------------------------------------------------- 输出小工具

def _color(text, code):
    if sys.stdout.isatty():
        return "\033[%sm%s\033[0m" % (code, text)
    return text


def _hit(text):
    return _color(text, "1;31")


def _dim(text):
    return _color(text, "90")


def _spinner_progress(state):
    if state.phase in ("done", "stopped"):
        return
    line = "[%s] %5.1f%%  已索引 %d  未变 %d  失败 %d  %.0fs  %s" % (
        state.phase, state.percent, state.indexed, state.unchanged,
        state.failed, state.elapsed, os.path.basename(state.current or ""))
    sys.stdout.write("\r" + line[:150].ljust(150))
    sys.stdout.flush()


# ---------------------------------------------------------------- 子命令

def cmd_index(args):
    cfg = config.load()
    if args.roots:
        cfg["roots"] = [os.path.abspath(os.path.expanduser(r)) for r in args.roots]
    if args.include_hidden:
        cfg["include_hidden"] = True
    if args.workers:
        cfg["workers"] = args.workers
    if args.max_file_mb:
        cfg["max_file_mb"] = args.max_file_mb
    if args.ocr is not None:
        cfg["ocr_enabled"] = args.ocr
    if not cfg.get("roots"):
        print("错误：没有指定要索引的目录。用法：docseek index /home/xxx", file=sys.stderr)
        return 2

    index = Index(args.db)
    builder = Builder(index, cfg, None if args.quiet else _spinner_progress)
    started = time.time()
    try:
        state = builder.run(rebuild=args.rebuild, retry_errors=args.retry_errors)
    except KeyboardInterrupt:
        builder.stop()
        print("\n已请求中断，正在收尾……")
        state = builder.progress
    finally:
        if not args.quiet:
            sys.stdout.write("\r" + " " * 150 + "\r")

    done = state.done_count
    left = max(0, state.total - done)
    if state.interrupted:
        print("索引未完成：已处理 %d/%d，还剩 %d 份（再跑一次会接着做）"
              % (done, state.total, left))
    else:
        print("索引完成")
    st = index.stats()
    print("  用时        %.1f s" % (time.time() - started))
    print("  扫描到文件  %d 个（其中 %d 个待抽取）" % (state.seen, state.total))
    print("  已索引      %d" % state.indexed)
    print("  已是最新    %d" % state.unchanged)
    print("  失败        %d" % state.failed)
    skip = state.skip_summary()
    if state.skipped:
        print("  跳过        %d 个（%s）" % (state.skipped, skip or "见设置"))
    if state.pending:
        print("  待 OCR 补抽 %d 个（本次没跑 OCR/抽不到文字，下次开启会自动补）"
              % state.pending)
    print("  索引库      %s（%.1f MB）" % (index.path, st["db_bytes"] / 1048576.0))
    print("  库中文档    %d 份" % st["docs"])
    index.close()
    return 0


def _render_snippet(snippet):
    parts = []
    for kind, text, _start in snippet["segments"]:
        chunk = text.replace("\n", " ").strip()
        if not chunk:
            continue
        parts.append(_hit(chunk) if kind == 1 else chunk)
    page = snippet.get("page")
    prefix = ("第 %s 页  " % page) if page else ""
    return prefix + "…".join(parts)


def cmd_search(args):
    cfg = config.load()
    index = Index(args.db, readonly=True)     # 检索只读，不参与写锁竞争
    searcher = Searcher(index, cfg)
    sort_mode = args.sort or cfg.get("sort_mode", "relevance")
    results, query, elapsed = searcher.search(args.query, limit=args.limit,
                                              sort=sort_mode)

    if args.json:
        payload = {"query": query.raw, "elapsed": elapsed,
                   "message": query.message, "count": len(results),
                   "results": [{"path": r.path, "ext": r.ext, "kind": r.kind,
                                "matches": r.matches, "page": r.page,
                                "size": r.size, "mtime": r.mtime,
                                "snippets": [[[k, t] for k, t, _ in s["segments"]]
                                             for s in r.snippets]}
                               for r in results]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        index.close()
        return 0

    if query.message:
        print(_dim(query.message))
    limit = args.limit or int(cfg.get("max_results", 500))
    shown = len(results)
    tail = "" if shown < limit else "（已达上限 %d，可用 --limit 调大）" % limit
    print("命中 %d 份文档%s，用时 %.2f s    （%s；排序：%s）"
          % (shown, tail, elapsed, query.describe(),
             SORT_LABELS.get(sort_mode, sort_mode)))
    for i, res in enumerate(results, 1):
        print("-" * 78)
        print("%d. %s" % (i, _color(res.path, "1")))
        print("   %s  %.1f KB  %s  命中 %d 处" % (
            res.ext, res.size / 1024.0, res.mtime_text, res.matches))
        for snippet in res.snippets:
            print("   " + _render_snippet(snippet))
        if res.warning:
            print("   " + _dim("提示：" + res.warning))
    index.close()
    return 0


def cmd_stats(args):
    index = Index(args.db)
    st = index.stats()
    print("索引库      %s" % index.path)
    print("体积        %.1f MB" % (st["db_bytes"] / 1048576.0))
    print("文档数      %d" % st["docs"])
    print("正文总量    %.1f MB" % (st["chars"] / 1048576.0))
    if st["last_indexed"]:
        print("最近索引    %s" % time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(st["last_indexed"])))
    print("状态分布    %s" % st["by_state"])
    print("类型分布    %s" % ", ".join("%s:%d" % (e or "-", c)
                                       for e, c in st["by_ext"]))
    try:
        from .extract import ocr as ocr_mod
        files, size = ocr_mod.cache_stats()
        if files:
            print("OCR 缓存    %d 个文件，%.1f MB（docseek cache --clear-ocr 可清理）"
                  % (files, size / 1048576.0))
    except ImportError:
        pass
    index.close()
    return 0


def _iter_paths(items):
    for item in items:
        path = os.path.expanduser(item)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in sorted(files):
                    if not name.startswith("."):
                        yield os.path.join(root, name)
        elif os.path.isfile(path):
            yield path
        else:
            for hit in sorted(glob.glob(path)):
                yield hit


def cmd_check(args):
    """抽取自检：不写库，只报告每个文件能抽出什么。"""
    from .extract import ExtractError, extract, is_indexable, file_ext
    cfg = config.load()
    if args.ocr is not None:
        cfg["ocr_enabled"] = args.ocr
    count = ok = failed = 0
    for path in _iter_paths(args.paths):
        count += 1
        if args.only_indexable and not is_indexable(path, cfg):
            continue
        started = time.time()
        try:
            got = extract(path, options=cfg)
        except ExtractError as exc:
            failed += 1
            print("%-70s  ✗  %s" % (_short(path), exc))
            continue
        except Exception as exc:                    # noqa: BLE001
            failed += 1
            print("%-70s  ✗  %s: %s" % (_short(path), type(exc).__name__, exc))
            continue
        ok += 1
        ms = (time.time() - started) * 1000
        pages = len(got.page_offsets)
        print("%-70s  ✓  %-6s %7d 字  %2d 页  %6.0f ms  %s" % (
            _short(path), got.kind, len(got.text), pages, ms,
            got.warning or ""))
        if args.preview:
            text = got.text[:args.preview].replace("\n", " ")
            print("      %s" % text)
        if args.tokens:
            toks = index_tokens(got.text).split()
            print("      token %d 个，前 20 个：%s" % (len(toks), " ".join(toks[:20])))
    print("\n共检查 %d 个文件：成功 %d，失败 %d" % (count, ok, failed))
    return 0


def _short(path):
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= 68 else "…" + path[-67:]


def cmd_ocr(args):
    """对单个文件做 OCR（PDF 按页，图片直接识别）——用于验证扫描件能不能搜。"""
    from .extract import ocr as ocr_mod
    path = Path(os.path.expanduser(args.file))
    if not path.is_file():
        print("文件不存在：%s" % path, file=sys.stderr)
        return 2
    if not ocr_mod.available():
        print("未找到 tesseract，无法 OCR（可 apt install tesseract-ocr tesseract-ocr-chi-sim）",
              file=sys.stderr)
        return 3
    cfg = config.load()
    langs = args.langs or cfg.get("ocr_langs")
    dpi = args.dpi or cfg.get("ocr_dpi")
    print("语言：%s（本机可用：%s）  分辨率：%sdpi"
          % (ocr_mod.resolve_langs(langs), "、".join(ocr_mod.languages()), dpi))

    started = time.time()
    if path.suffix.lower() == ".pdf":
        text, offsets = ocr_mod.ocr_pdf(path, {
            "ocr_langs": langs, "ocr_dpi": dpi,
            "ocr_max_pages": args.pages or cfg.get("ocr_max_pages")})
        for index, offset in enumerate(offsets):
            end = offsets[index + 1] if index + 1 < len(offsets) else len(text)
            body = text[offset:end].strip()
            print("\n--- 第 %d 页（%d 字）---" % (index + 1, len(body)))
            print(body[:args.preview])
    else:
        text = ocr_mod.ocr_image(path, langs=langs)
        print("\n识别到 %d 字：" % len(text))
        print(text[:args.preview])
    print("\n用时 %.1f s" % (time.time() - started))
    return 0


def cmd_cache(args):
    """查看/清理各类缓存。"""
    from .extract import ocr as ocr_mod
    files, size = ocr_mod.cache_stats()
    pdf_files, pdf_size = _dir_stats(config.PDF_CACHE)
    img_files, img_size = _dir_stats(config.IMG_CACHE)
    print("OCR 识别缓存      %5d 个文件  %8.1f MB   %s" % (files, size / 1048576.0, config.OCR_CACHE))
    print("PDF 转换缓存      %5d 个文件  %8.1f MB   %s" % (pdf_files, pdf_size / 1048576.0, config.PDF_CACHE))
    print("页面图片缓存      %5d 个文件  %8.1f MB   %s" % (img_files, img_size / 1048576.0, config.IMG_CACHE))
    if args.clear_ocr:
        removed = _clear_dir(config.OCR_CACHE)
        print("\n已清理 OCR 缓存：%d 个文件（下次遇到扫描件会重新识别）" % removed)
    if args.clear_render:
        from . import preview as preview_mod
        print("\n已清理渲染缓存：%d 个文件" % preview_mod.clear_cache())
    return 0


def _dir_stats(path):
    files = 0
    size = 0
    try:
        for name in os.listdir(str(path)):
            item = path / name
            if item.is_file():
                files += 1
                size += item.stat().st_size
    except OSError:
        pass
    return files, size


def _clear_dir(path):
    """清理 docseek 自己的缓存目录（白名单校验，绝不碰用户文件）。"""
    removed = 0
    try:
        root = path.resolve()
        for name in os.listdir(str(root)):
            item = root / name
            if item.is_file():
                item.unlink()
                removed += 1
    except OSError:
        pass
    return removed


def cmd_reindex_tokens(args):
    """只重算索引词（含文件名/路径），不重新抽取文件。"""
    index = Index(args.db)
    builder = Builder(index, config.load())
    n = builder.rebuild_tokens(on_progress=None if args.quiet else None)
    print("已为 %d 份文档重算索引词（文件名与路径现在也能搜到）" % n)
    index.close()
    return 0


def cmd_doctor(args):
    """检查运行依赖；缺失时给出可直接复制的安装命令。"""
    import json as _json
    results = doctor.run_checks()
    if args.json:
        print(_json.dumps({
            "ok": not doctor.missing_required(results),
            "checks": results,
            "missing": doctor.missing_packages(results),
            "command": doctor.apt_command(results),
        }, ensure_ascii=False, indent=2))
        return 0 if not doctor.missing_required(results) else 1
    print("DocSeek 运行环境自检：")
    print(doctor.format_report(results))
    if not doctor.missing_required(results):
        return 0
    return 1


def cmd_explain(args):
    """解释某份文档的索引状态，以及某个词为什么（不）能搜到它。"""
    import builtins
    lines = []

    def emit(*parts, **kwargs):
        text = " ".join(str(part) for part in parts)
        lines.append(text)
        builtins.print(text, **kwargs)

    try:
        return _explain(args, emit)
    finally:
        if args.save:
            try:
                with open(os.path.expanduser(args.save), "w",
                          encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                builtins.print("（以上内容已存到 %s，可直接把这个文件发给别人）"
                               % os.path.expanduser(args.save))
            except OSError as exc:
                builtins.print("写入 %s 失败：%s" % (args.save, exc))


def _explain(args, print):                           # noqa: A002
    index = Index(args.db, readonly=True)
    rows = list(index.conn.execute(
        "SELECT * FROM docs WHERE path LIKE ? LIMIT ?",
        ("%" + args.pattern + "%", args.max)))
    if not rows:
        print("索引库里没有匹配 %r 的文档。" % args.pattern)
        print("提示：确认它所在的目录已加入索引根目录；用 `docseek problems` 看未入库的文件。")
        index.close()
        return 1
    terms = [t.strip() for t in (args.term or "").split(",") if t.strip()]
    for row in rows:
        print("文件   %s" % row["path"])
        print("状态   %s   类型 %s   大小 %.1f KB   修改 %s"
              % (row["state"], row["ext"] or "-", (row["size"] or 0) / 1024.0,
                 time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(row["mtime"] or 0))))
        print("正文   %s 字%s" % (row["chars"] or 0,
                                  "" if row["content"] is not None else "（未存库）"))
        if row["warning"]:
            print("提示   %s" % row["warning"])
        if row["err"]:
            print("错误   %s" % row["err"])
        text = textstore.unpack_content(row["content"]) or ""
        for term in terms:
            in_name = term in (row["name"] or "") or term in row["path"]
            in_text = term in text
            print("  词 %-10r 文件名/路径: %s   正文: %s"
                  % (term, "有" if in_name else "无", "有" if in_text else "无"))
            if in_text:
                at = text.index(term)
                print("      正文上下文：…%s…"
                      % text[max(0, at - 30):at + 30].replace("\n", " "))
            expr = tokenizer.fts_query([term])
            hit = index.conn.execute(
                "SELECT COUNT(*) FROM fts WHERE rowid=? AND fts MATCH ?",
                (row["id"], expr)).fetchone()[0] if expr else 0
            print("      FTS 索引里: %s" % ("有" if hit else "没有"))
            if in_text and not hit:
                print("      ⚠ 正文有但索引没有 → 跑一次 `docseek reindex-tokens`")
            if not in_text and not in_name:
                print("      两者都没有 → 这份文档确实不含该词"
                      "（扫描件可能是 OCR 没认出来）")
    index.close()
    return 0


def cmd_refresh(args):
    """对单个文件重跑抽取/OCR（忽略缓存）并更新索引。

    改了 OCR 参数（分辨率、语言、上限）之后，用它单独重跑某份文件，
    不用把几万份文档全部重建。
    """
    from .extract import ExtractError, extract, ocr as ocr_mod
    pattern = args.pattern
    index = Index(args.db)
    rows = list(index.conn.execute(
        "SELECT id, path, chars FROM docs WHERE path LIKE ? LIMIT ?",
        ("%" + pattern + "%", args.max)))
    if not rows:
        print("索引库里没有匹配 %r 的文档（先用 `docseek index` 让它进库）" % pattern)
        index.close()
        return 1
    cfg = config.load()
    if args.dpi:
        cfg["ocr_dpi"] = args.dpi
    if args.max_pixels:
        cfg["ocr_max_pixels"] = args.max_pixels
    if args.langs:
        cfg["ocr_langs"] = args.langs
    touched = 0
    for row in rows:
        path = row["path"]
        if not os.path.exists(path):
            print("跳过（文件已不在）：%s" % path)
            continue
        if args.no_cache and path.lower().endswith(".pdf"):
            cleared = ocr_mod.clear_page_cache(path, cfg)
            if cleared:
                print("  已清掉 %d 页的 OCR 缓存" % cleared)
        t0 = time.time()
        try:
            got = extract(path, options=cfg)
        except ExtractError as exc:
            print("  ✗ %s：%s" % (os.path.basename(path), exc))
            continue
        except Exception as exc:                      # noqa: BLE001
            print("  ✗ %s：%s: %s" % (os.path.basename(path), type(exc).__name__, exc))
            continue
        st = os.stat(path)
        head = "%s\n%s\n" % (os.path.basename(path), path)
        _upsert_with_retry(index, {
                "path": path, "name": os.path.basename(path),
                "ext": os.path.splitext(path)[1].lstrip(".").lower(),
                "kind": got.kind, "size": st.st_size, "mtime": st.st_mtime,
                "ino": st.st_ino, "dev": st.st_dev, "chars": len(got.text)},
            tokens=tokenizer.index_tokens(head + got.text),
            state="ok" if got.text.strip() else "empty",
            warning=got.warning or None,
            content=textstore.pack_content(got.text),
            pages=textstore.pack_pages(got.page_offsets))
        touched += 1
        print("  ✓ %s：%d 字 → %d 字（%.1fs）%s"
              % (os.path.basename(path), row["chars"] or 0, len(got.text),
                 time.time() - t0, ("  " + got.warning) if got.warning else ""))
    index.commit()
    index.optimize()
    print("\n已刷新 %d 份文档" % touched)
    index.close()
    return 0


def _upsert_with_retry(index, meta, attempts=6, delay=5.0, **kwargs):
    """写库重试：索引正在跑时数据库会被占用，等一会儿再写（别直接失败）。"""
    import sqlite3 as _sqlite
    for attempt in range(1, attempts + 1):
        try:
            return index.upsert(meta, **kwargs)
        except _sqlite.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts:
                raise
            print("  数据库被占用（是不是正在索引？），%d 秒后重试（%d/%d）"
                  % (delay, attempt, attempts))
            time.sleep(delay)
    return None


def cmd_reocr(args):
    """把"靠 OCR 出正文"的文档标记为待补抽，下次索引会用新参数重认。

    改过识别精度/语言之后用：已入库的不会因为指纹没变就跳过。
    """
    index = Index(args.db)
    where = ["state IN ('ok','empty')",
             "(warning LIKE '%OCR%' OR kind='image')"]
    params = []
    if args.pattern:
        where.append("path LIKE ?")
        params.append("%" + args.pattern + "%")
    sql = "SELECT COUNT(*) FROM docs WHERE " + " AND ".join(where)
    total = index.conn.execute(sql, params).fetchone()[0]
    pages = index.conn.execute(
        "SELECT COALESCE(SUM(LENGTH(pages) - LENGTH(REPLACE(pages, ',', '')) + 1), 0)"
        " FROM docs WHERE " + " AND ".join(where), params).fetchone()[0]
    print("靠 OCR 出正文的文档：%d 份，约 %d 页" % (total, pages or 0))
    if args.dry_run:
        print("（--dry-run：没有改动任何东西）")
        index.close()
        return 0
    if not total:
        index.close()
        return 0
    index.conn.execute("UPDATE docs SET state='pending', warning='待用新参数重新识别'"
                       " WHERE " + " AND ".join(where), params)
    index.commit()
    print("已标记为待补抽：下次 `docseek index`（或界面点 ⟳）会重新识别这 %d 份" % total)
    index.close()
    return 0


def cmd_problems(args):
    """列出没能正常索引的文件及原因（失败/没文字/待 OCR 补抽）。"""
    index = Index(args.db)
    states = [args.state] if args.state else ["error", "empty", "pending"]
    where = "state IN (%s)" % ",".join("?" * len(states))
    params = list(states)
    if args.ext:
        where += " AND ext = ?"
        params.append(args.ext.lstrip(".").lower())

    total = index.conn.execute(
        "SELECT COUNT(*) FROM docs WHERE " + where, params).fetchone()[0]
    print("不符合预期（非 ok）的文件：%d 个" % total)
    if not total:
        index.close()
        return 0

    print("\n按状态：")
    for state, count in index.conn.execute(
            "SELECT state, COUNT(*) FROM docs WHERE " + where +
            " GROUP BY state ORDER BY 2 DESC", params):
        label = {"error": "失败", "empty": "没有文字", "pending": "待 OCR 补抽"}.get(
            state, state)
        print("  %-10s %6d" % (label, count))

    print("\n按原因：")
    for reason, count in index.conn.execute(
            "SELECT substr(COALESCE(err, warning, '(无说明)'), 1, 60) r,"
            " COUNT(*) FROM docs WHERE " + where +
            " GROUP BY r ORDER BY 2 DESC LIMIT 12", params):
        print("  %6d  %s" % (count, reason))

    print("\n按扩展名：")
    for ext, count in index.conn.execute(
            "SELECT COALESCE(ext,'(无)'), COUNT(*) FROM docs WHERE " + where +
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 10", params):
        print("  %-8s %6d" % (ext, count))

    print("\n示例（最多 %d 条）：" % args.limit)
    for path, state, reason in index.conn.execute(
            "SELECT path, state, substr(COALESCE(err, warning, ''), 1, 46)"
            " FROM docs WHERE " + where + " ORDER BY state, path LIMIT ?",
            params + [args.limit]):
        print("  [%s] %s\n        %s" % (state, path, reason))

    print("\n说明：")
    print("  · 失败(error)   ：真的没抽出来，可用 `docseek index --retry-errors` 重试")
    print("  · 没有文字(empty)：照片、空白页之类，本来就没有文字，不是错误")
    print("  · 待补抽(pending)：关着 OCR 时的扫描件，`docseek index` 会自动补")
    index.close()
    return 0


def cmd_gui(args):
    try:
        from .ui.app import main as gui_main
    except ImportError as exc:
        print("界面模块尚未就绪：%s" % exc, file=sys.stderr)
        return 3
    return gui_main(args.db)


# ---------------------------------------------------------------- 入口

def build_parser():
    parser = argparse.ArgumentParser(
        prog="docseek", description="DocSeek —— 本地文档内容搜索（麒麟 / GTK3）")
    parser.add_argument("--version", action="version",
                        version="DocSeek %s" % __version__)
    parser.add_argument("--db", default=None, help="索引库路径（默认 ~/.local/share/docseek/index.db）")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="建立/更新索引")
    p_index.add_argument("roots", nargs="*", help="要索引的目录（缺省用配置里的 roots）")
    p_index.add_argument("--rebuild", action="store_true", help="忽略指纹，全部重建")
    p_index.add_argument("--retry-errors", action="store_true",
                         help="重试上次抽取失败的文件（默认不重试，避免每次白跑）")
    p_index.add_argument("--workers", type=int, default=0, help="抽取线程数，默认 4")
    p_index.add_argument("--max-file-mb", type=int, default=0, help="单文件体积上限")
    p_index.add_argument("--include-hidden", action="store_true", help="包含隐藏文件")
    p_index.add_argument("--ocr", dest="ocr", action="store_true", default=None,
                         help="本次强制开启 OCR")
    p_index.add_argument("--no-ocr", dest="ocr", action="store_false",
                         help="本次不跑 OCR（扫描件记成待补，下次开启 OCR 会自动重跑）")
    p_index.add_argument("--quiet", action="store_true", help="不显示进度")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="搜索内容")
    p_search.add_argument("query", help="关键词，支持 ext: / path: / -排除 / re: 语法")
    p_search.add_argument("--limit", type=int, default=0, help="结果数上限")
    p_search.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_search.add_argument("--sort", choices=[mode for mode, _ in SORT_MODES],
                          default=None,
                          help="排序：relevance/mtime_desc/mtime_asc/size_desc/size_asc/dir/type")
    p_search.set_defaults(func=cmd_search)

    p_stats = sub.add_parser("stats", help="查看索引概况")
    p_stats.set_defaults(func=cmd_stats)

    p_check = sub.add_parser("check", help="抽取自检（不写索引）")
    p_check.add_argument("paths", nargs="+", help="文件、目录或通配符")
    p_check.add_argument("--preview", type=int, default=0, help="额外打印前 N 个字符")
    p_check.add_argument("--tokens", action="store_true", help="打印分词结果前若干项")
    p_check.add_argument("--only-indexable", action="store_true",
                         help="跳过不支持索引的文件")
    p_check.add_argument("--ocr", dest="ocr", action="store_true", default=None,
                         help="强制开启 OCR")
    p_check.add_argument("--no-ocr", dest="ocr", action="store_false",
                         help="关闭 OCR（只看有文本层的部分）")
    p_check.set_defaults(func=cmd_check)

    p_ocr = sub.add_parser("ocr", help="对单个文件跑 OCR（验证扫描件能否搜到）")
    p_ocr.add_argument("file", help="PDF 或图片")
    p_ocr.add_argument("--langs", default=None, help="tesseract 语言串，默认取配置")
    p_ocr.add_argument("--dpi", type=int, default=None, help="渲染分辨率，默认取配置")
    p_ocr.add_argument("--pages", type=int, default=None, help="最多识别多少页")
    p_ocr.add_argument("--preview", type=int, default=1200, help="每页最多打印多少字")
    p_ocr.set_defaults(func=cmd_ocr)

    p_cache = sub.add_parser("cache", help="查看/清理缓存")
    p_cache.add_argument("--clear-ocr", action="store_true", help="清空 OCR 识别缓存")
    p_cache.add_argument("--clear-render", action="store_true",
                         help="清空 PDF 转换与页面图片缓存")
    p_cache.set_defaults(func=cmd_cache)

    p_tok = sub.add_parser("reindex-tokens",
                           help="只重算索引词（把文件名/路径纳入检索），不重新抽取文件")
    p_tok.add_argument("--quiet", action="store_true")
    p_tok.set_defaults(func=cmd_reindex_tokens)

    p_doc = sub.add_parser("doctor", help="检查运行依赖，缺失时给出安装命令")
    p_doc.add_argument("--json", action="store_true", help="输出 JSON")
    p_doc.set_defaults(func=cmd_doctor)

    p_expl = sub.add_parser("explain", help="解释某份文档的索引状态与某词为何搜不到")
    p_expl.add_argument("pattern", help="文件路径的一部分")
    p_expl.add_argument("--term", default=None, help="要核对的词，多个用逗号分隔")
    p_expl.add_argument("--max", type=int, default=5, help="最多解释几份匹配的文档")
    p_expl.add_argument("--save", default=None,
                        help="把结果同时写入文件，例如 --save ~/下载/explain.txt")
    p_expl.set_defaults(func=cmd_explain)

    p_ref = sub.add_parser("refresh", help="对单个文件重跑抽取/OCR 并更新索引")
    p_ref.add_argument("pattern", help="文件路径的一部分")
    p_ref.add_argument("--max", type=int, default=5, help="最多处理几份")
    p_ref.add_argument("--no-cache", action="store_true",
                       help="忽略并清掉该文件的 OCR 缓存，强制重新识别")
    p_ref.add_argument("--dpi", type=int, default=None, help="本次使用的 OCR 分辨率")
    p_ref.add_argument("--max-pixels", type=int, default=None, help="本次的像素上限")
    p_ref.add_argument("--langs", default=None, help="本次的识别语言")
    p_ref.set_defaults(func=cmd_refresh)

    p_reocr = sub.add_parser("reocr",
                             help="把已 OCR 的文档标记为待重认（改了识别参数后用）")
    p_reocr.add_argument("--pattern", default=None, help="只处理路径含该片段的文档")
    p_reocr.add_argument("--dry-run", action="store_true", help="只统计，不改动")
    p_reocr.set_defaults(func=cmd_reocr)

    p_prob = sub.add_parser("problems", help="没正常索引的文件及原因")
    p_prob.add_argument("--state", choices=["error", "empty", "pending"],
                        default=None, help="只看某一种状态")
    p_prob.add_argument("--ext", default=None, help="只看某个扩展名，如 pdf")
    p_prob.add_argument("--limit", type=int, default=15, help="最多列几条示例")
    p_prob.set_defaults(func=cmd_problems)

    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=cmd_gui)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    procs.install()          # 登记子进程，退出时统一回收（避免孤儿 tesseract 满核跑）
    procs.install_signals((signal.SIGTERM, signal.SIGINT))
    config.ensure_dirs()
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    finally:
        # sticky=False：命令收尾只收自己起的子进程。真正的进程退出由
        # atexit / 信号处理器兜底（那两处用的是 sticky 默认值）。
        procs.terminate_all(sticky=False)


if __name__ == "__main__":
    sys.exit(main())
