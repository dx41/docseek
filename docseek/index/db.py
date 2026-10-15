"""SQLite + FTS5 索引层。

设计要点
--------
* FTS5 用 `detail=none`：不存位置与词频，索引最小；代价是不能做短语/片段高亮，
  因此查询只拿它做"粗筛"，精确校验与片段生成交给 search.py 用原文完成。
* 因为不需要位置，写入前对 token 去重（见 tokenizer.index_tokens）可显著省空间。
* fts.rowid == docs.id，一一对应。
"""

import sqlite3
import time
from pathlib import Path

from .. import config

# 切词来源版本：1=只有正文；2=正文+文件名+路径。改动切词方式时 +1。
TOKENS_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS docs (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    name       TEXT,
    ext        TEXT,
    kind       TEXT,
    size       INTEGER,
    mtime      REAL,
    ino        INTEGER,
    dev        INTEGER,
    chars      INTEGER DEFAULT 0,
    content    BLOB,                  -- zlib 压缩后的正文（见 textstore.py）
    pages      TEXT,                  -- 分页偏移，逗号分隔
    state      TEXT DEFAULT 'ok',      -- ok / error / skipped
    err        TEXT,
    backend    TEXT,
    warning    TEXT,
    indexed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_docs_ext   ON docs(ext);
CREATE INDEX IF NOT EXISTS idx_docs_state ON docs(state);
CREATE INDEX IF NOT EXISTS idx_docs_mtime ON docs(mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    tokens,
    tokenize = 'unicode61 remove_diacritics 2',
    detail = none
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Index:
    def __init__(self, path=None, readonly=False):
        """打开索引库。

        `readonly=True` 供**检索**使用：跳过建表/迁移脚本，并置 query_only，
        这样搜索永远不会去拿写锁——索引正在进行时也能秒开、不会卡在锁上。
        """
        config.ensure_dirs()
        self.path = Path(path or config.INDEX_DB)
        self.readonly = bool(readonly)
        self.conn = sqlite3.connect(str(self.path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        if self.readonly:
            self.conn.execute("PRAGMA query_only=ON")
            return
        if not self._has_schema():
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()
        else:
            self._migrate()
            self.conn.commit()

    def _has_schema(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='docs'"
        ).fetchone()
        return row is not None

    def _migrate(self):
        """老索引库补列：content / pages 是后加的，缺了就 ALTER 上去。"""
        columns = {row[1] for row in
                   self.conn.execute("PRAGMA table_info(docs)")}
        if "content" not in columns:
            self.conn.execute("ALTER TABLE docs ADD COLUMN content BLOB")
        if "pages" not in columns:
            self.conn.execute("ALTER TABLE docs ADD COLUMN pages TEXT")

    # ------------------------------------------------------------ 基础
    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def commit(self):
        self.conn.commit()

    # ------------------------------------------------------------ 元数据
    def get_doc(self, path):
        cur = self.conn.execute("SELECT * FROM docs WHERE path = ?", (str(path),))
        return cur.fetchone()

    def get_doc_by_id(self, doc_id):
        cur = self.conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,))
        return cur.fetchone()

    def needs_update(self, path, size, mtime, retry_errors=False):
        """指纹未变就不重新抽取。

        state 的语义：
          ok       抽取成功；指纹没变就跳过
          pending  这次没抽出正文（例如关着 OCR 时的扫描件）——**必须重试**，
                   否则"先快后慢"的批量策略会把扫描件永久漏掉
          error    抽取失败（坏文件等）——默认不重试，因为坏文件每轮都重抽太浪费
                   （一个 .doc 走 LibreOffice 要一秒多）；用 --retry-errors 或 --rebuild
        """
        row = self.get_doc(path)
        if row is None:
            return True
        if row["size"] != size or abs((row["mtime"] or 0) - mtime) > 1e-6:
            return True
        state = row["state"]
        if state in ("ok", "empty"):
            return False            # ok 指纹未变就跳过；empty（照片没文字）也别重试
        if state == "pending":
            return True             # 等 OCR 补抽的必须重跑
        return bool(retry_errors)

    def delete_by_path(self, path):
        row = self.get_doc(path)
        if row:
            self.delete_doc(row["id"])

    def delete_doc(self, doc_id):
        self.conn.execute("DELETE FROM fts WHERE rowid = ?", (doc_id,))
        self.conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))

    def upsert(self, meta, tokens=None, state="ok", err=None, warning=None,
               content=None, pages=None):
        """写入/更新一条文档记录；tokens 为 None 时不动 FTS（用于错误记录）。

        content/pages 为正文与分页偏移（已编码），检索与预览直接读它，避免每次
        都重新抽取（.doc/.wps 走 LibreOffice 要一两秒，搜索会因此卡几十秒）。
        """
        now = time.time()
        path = str(meta["path"])
        row = self.get_doc(path)
        cols = (meta.get("name"), meta.get("ext"), meta.get("kind"),
                meta.get("size"), meta.get("mtime"), meta.get("ino"),
                meta.get("dev"), meta.get("chars", 0), state, err,
                meta.get("backend"), warning, now, content, pages)
        if row:
            doc_id = row["id"]
            self.conn.execute(
                "UPDATE docs SET name=?, ext=?, kind=?, size=?, mtime=?, ino=?,"
                " dev=?, chars=?, state=?, err=?, backend=?, warning=?,"
                " indexed_at=?, content=?, pages=? WHERE id=?",
                cols + (doc_id,))
        else:
            cur = self.conn.execute(
                "INSERT INTO docs (name, ext, kind, size, mtime, ino, dev,"
                " chars, state, err, backend, warning, indexed_at, content,"
                " pages, path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                cols + (path,))
            doc_id = cur.lastrowid
        if tokens is not None:
            self.conn.execute("DELETE FROM fts WHERE rowid = ?", (doc_id,))
            self.conn.execute("INSERT INTO fts (rowid, tokens) VALUES (?, ?)",
                              (doc_id, tokens))
        return doc_id

    # ------------------------------------------------------------ 查询
    def search_ids(self, match_expr, exts=None, path_substr=None, limit=2000):
        """FTS 粗筛，返回文档 id 列表（按 bm25 相关度）。"""
        sql = ["SELECT d.id FROM fts JOIN docs d ON d.id = fts.rowid",
               "WHERE fts MATCH ?", "AND d.state = 'ok'"]
        params = [match_expr]
        if exts:
            sql.append("AND d.ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend([e.lower() for e in exts])
        if path_substr:
            for sub in path_substr:
                sql.append("AND d.path LIKE ?")
                params.append("%" + sub + "%")
        sql.append("ORDER BY bm25(fts) LIMIT ?")
        params.append(int(limit))
        try:
            cur = self.conn.execute(" ".join(sql), params)
        except sqlite3.OperationalError:
            # bm25 在极端情况下不可用（例如没有词频统计）时退回普通排序
            sql = [s for s in sql if not s.startswith("ORDER BY")]
            sql.append("LIMIT ?")
            cur = self.conn.execute(" ".join(sql), params)
        return [row[0] for row in cur.fetchall()]

    def iter_docs(self, exts=None, path_substr=None, state="ok", limit=None):
        sql = ["SELECT * FROM docs WHERE 1=1"]
        params = []
        if state:
            sql.append("AND state = ?")
            params.append(state)
        if exts:
            sql.append("AND ext IN (%s)" % ",".join("?" * len(exts)))
            params.extend([e.lower() for e in exts])
        if path_substr:
            for sub in path_substr:
                sql.append("AND path LIKE ?")
                params.append("%" + sub + "%")
        sql.append("ORDER BY mtime DESC")
        if limit:
            sql.append("LIMIT ?")
            params.append(int(limit))
        return self.conn.execute(" ".join(sql), params)

    def iter_paths_under(self, roots):
        roots = [str(r).rstrip("/") + "/" for r in roots]
        for row in self.conn.execute("SELECT id, path FROM docs"):
            path = row["path"]
            if not roots or any(path.startswith(r) for r in roots):
                yield row["id"], path

    # ------------------------------------------------------------ 维护
    def count(self, state=None):
        if state:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM docs WHERE state = ?", (state,))
        else:
            cur = self.conn.execute("SELECT COUNT(*) FROM docs")
        return cur.fetchone()[0]

    def stats(self):
        out = {"docs": self.count(), "db_bytes": 0, "chars": 0,
               "last_indexed": None, "by_state": {}, "by_ext": []}
        try:
            out["db_bytes"] = self.path.stat().st_size
        except OSError:
            pass
        for row in self.conn.execute(
                "SELECT state, COUNT(*) c FROM docs GROUP BY state"):
            out["by_state"][row["state"]] = row["c"]
        row = self.conn.execute(
            "SELECT SUM(chars) s, MAX(indexed_at) t FROM docs").fetchone()
        out["chars"] = row["s"] or 0
        out["last_indexed"] = row["t"]
        out["by_ext"] = [(r["ext"], r["c"]) for r in self.conn.execute(
            "SELECT ext, COUNT(*) c FROM docs GROUP BY ext ORDER BY c DESC"
            " LIMIT 12")]
        return out

    def tokens_stale(self):
        """切词来源版本落后时为真（例如"文件名/路径入索引"这类改动）。"""
        from . import db as _self            # noqa: F401 兼容旧引用
        return self.get_meta("tokens_version") != str(TOKENS_VERSION)

    def mark_tokens_current(self):
        self.set_meta("tokens_version", str(TOKENS_VERSION))

    # 早期版本拿 MAX_STORED(1200000) 当版本号，升级后要认这个历史标记，
    # 否则会把"已经回填过"的库判成需要回填，把几万份文档（含几千张图片的 OCR）
    # 全部重抽一遍。
    _LEGACY_CONTENT_MARKERS = ("1200000",)

    def content_stale(self):
        """索引里还缺正文（老库或首次升级）时为真，需要整体回填一次。"""
        from .. import textstore
        current = self.get_meta("content_version")
        if current is None:
            return True
        if current in self._LEGACY_CONTENT_MARKERS:
            return False
        return current != str(textstore.CONTENT_VERSION)

    def mark_content_current(self):
        from .. import textstore
        self.set_meta("content_version", str(textstore.CONTENT_VERSION))

    def set_meta(self, key, value):
        # 必须提交：否则这个写事务一直挂着，会挡住其它连接（同一进程里的第二个
        # Index、或下次运行的 CLI）——实测会卡满 60 秒的 busy timeout。
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                          (key, str(value)))
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def optimize(self):
        try:
            self.conn.execute("INSERT INTO fts (fts) VALUES ('optimize')")
            self.conn.commit()
        except sqlite3.Error:
            pass

    def reset(self, vacuum=False):
        self.conn.execute("DELETE FROM fts")
        self.conn.execute("DELETE FROM docs")
        self.conn.commit()
        if vacuum:
            try:
                self.conn.execute("VACUUM")
            except sqlite3.Error:
                pass
