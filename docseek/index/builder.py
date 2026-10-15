"""扫描 + 增量索引构建。

流程：扫描（步进遍历）→ 指纹比对（size + mtime）→ 线程池抽取 → 主线程写库。
抽取以文件 IO 与子进程等待为主，用 4 线程即可；SQLite 只在主线程写，避免锁竞争。
中断安全：任何时刻停下，已写入的记录都是完整可用的，下次运行会接着补。

统计口径（Progress）：`seen` 是磁盘上看到的文件总数，`total` 是"值得抽取"的数量，
`skipped_*` 说明其余文件为什么被跳过——这样界面上能回答"为什么 445 个文件只索引了 85 个"。
"""

import os
import stat as stat_mod
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .. import config, textstore, tokenizer
from ..extract import ExtractError, extract, file_ext, is_indexable

EMIT_INTERVAL = 0.1         # 进度回调最短间隔（秒），避免每个文件都刷界面
COMMIT_EVERY_DOCS = 10      # 每多少份提交一次
COMMIT_EVERY_SECONDS = 2.0  # 或者最多隔多少秒提交一次


@dataclass
class Progress:
    phase: str = "idle"          # idle / scan / index / sweep / done / stopped
    total: int = 0               # 待抽取的文件数
    seen: int = 0                # 磁盘上扫到的文件总数
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    skipped: int = 0             # 被跳过合计（下面各项之和）
    skipped_format: int = 0      # 格式不支持（压缩包/视频/未接入的类型…）
    skipped_hidden: int = 0      # 隐藏文件/隐藏目录
    skipped_large: int = 0       # 超过体积上限
    skipped_excluded: int = 0    # 命中了排除目录名
    skipped_symlink: int = 0     # 符号链接目录（未开启跟随时跳过）
    failed: int = 0
    empty: int = 0               # 抽取成功但确实没有文字（照片、空白页）
    pending: int = 0             # 本轮没抽出正文、等开启 OCR 再补的
    current: str = ""
    message: str = ""
    started: float = 0.0
    finished: float = 0.0

    @property
    def elapsed(self):
        end = self.finished or time.time()
        return max(0.0, end - (self.started or end))

    @property
    def done_count(self):
        return self.indexed + self.unchanged + self.failed

    @property
    def percent(self):
        if not self.total:
            return 0.0
        return min(100.0, self.done_count * 100.0 / self.total)

    @property
    def interrupted(self):
        """是否没跑完：还剩没处理的文件，或者被要求停止。"""
        return self.phase == "stopped" or self.done_count < self.total

    def skip_summary(self):
        bits = []
        if self.empty:
            bits.append("没有文字 %d" % self.empty)
        if self.skipped_format:
            bits.append("格式不支持 %d" % self.skipped_format)
        if self.skipped_hidden:
            bits.append("隐藏 %d" % self.skipped_hidden)
        if self.skipped_excluded:
            bits.append("排除目录内 %d" % self.skipped_excluded)
        if self.skipped_symlink:
            bits.append("软链目录 %d（可在设置里开启跟随）" % self.skipped_symlink)
        if self.skipped_large:
            bits.append("超过体积上限 %d" % self.skipped_large)
        return "，".join(bits)


class Builder:
    def __init__(self, index, cfg=None, on_progress=None):
        self.index = index
        self.cfg = cfg or config.load()
        self.on_progress = on_progress
        self.progress = Progress()
        self._stop = threading.Event()
        self._last_emit = 0.0

    # ------------------------------------------------------------ 控制
    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def _bump(self, field, count=1):
        setattr(self.progress, field, getattr(self.progress, field) + count)

    def _emit(self, force=False, **changes):
        for key, value in changes.items():
            setattr(self.progress, key, value)
        now = time.time()
        if not force and now - self._last_emit < EMIT_INTERVAL:
            return
        self._last_emit = now
        if self.on_progress:
            try:
                self.on_progress(self.progress)
            except Exception:       # noqa: BLE001 回调异常不应打断索引
                pass

    # ------------------------------------------------------------ 扫描
    def collect(self, roots):
        """遍历 roots，产出可索引的 (path, stat)，并把跳过原因记进 progress。"""
        excludes = config.excluded_dirs(self.cfg)
        include_hidden = bool(self.cfg.get("include_hidden"))
        follow = bool(self.cfg.get("follow_symlinks"))
        max_bytes = int(self.cfg.get("max_file_mb", 64)) * 1024 * 1024
        for root in roots:
            root = str(root)
            if not os.path.isdir(root):
                self._emit(message="目录不存在：%s" % root, force=True)
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
                if self._stop.is_set():
                    return
                kept = []
                for name in dirnames:
                    full = os.path.join(dirpath, name)
                    if name in excludes:
                        self._bump("skipped_excluded")
                    elif not include_hidden and name.startswith("."):
                        self._bump("skipped_hidden")
                    elif not follow and os.path.islink(full):
                        self._bump("skipped_symlink")
                    else:
                        kept.append(name)
                dirnames[:] = kept

                for name in filenames:
                    if self._stop.is_set():
                        return
                    self._bump("seen")
                    if not include_hidden and name.startswith("."):
                        self._bump("skipped_hidden")
                        continue
                    path = os.path.join(dirpath, name)
                    if not is_indexable(path, self.cfg):
                        self._bump("skipped_format")
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        self._bump("skipped_format")
                        continue
                    if not stat_mod.S_ISREG(st.st_mode):
                        self._bump("skipped_format")
                        continue
                    if st.st_size > max_bytes:
                        self._bump("skipped_large")
                        continue
                    yield path, st
                self._sync_skipped()
                self._emit(current=dirpath)

    def _sync_skipped(self):
        self.progress.skipped = (self.progress.skipped_format
                                 + self.progress.skipped_hidden
                                 + self.progress.skipped_excluded
                                 + self.progress.skipped_large
                                 + self.progress.skipped_symlink)

    def rebuild_tokens(self, on_progress=None):
        """从库里已有的 name/path/content 重算 FTS token（不读文件、不重跑 OCR）。

        "文件名/路径也要能搜"这类改动只需要重算 token，几万份也就几秒，
        没必要把 27k 份文档重新抽取一遍（那还要重跑几千张图片的 OCR）。
        没有存正文的记录（empty/pending/error）只补 name/path，本来就搜不到内容。
        """
        rows = list(self.index.conn.execute(
            "SELECT id, name, path, content, chars, state FROM docs"))
        done = 0
        for row in rows:
            if self._stop.is_set():
                break
            head = tokenizer.index_tokens(
                "%s\n%s" % (row["name"] or "", row["path"]))
            text = textstore.unpack_content(row["content"])
            if text is None and row["chars"]:
                # 有正文但没存下来（超长或早期记录）：**只能追加**文件名/路径，
                # 覆盖会把原有正文 token 抹掉——踩过这个坑（185 万字那份就搜不到了）
                current = self.index.conn.execute(
                    "SELECT tokens FROM fts WHERE rowid = ?", (row["id"],)).fetchone()
                tokens = ((current[0] + " ") if current and current[0] else "") + head
            else:
                tokens = tokenizer.index_tokens(
                    "%s\n%s\n%s" % (row["name"] or "", row["path"], text or ""))
            self.index.conn.execute("DELETE FROM fts WHERE rowid = ?", (row["id"],))
            self.index.conn.execute(
                "INSERT INTO fts (rowid, tokens) VALUES (?, ?)",
                (row["id"], tokens))
            done += 1
            if done % 500 == 0:
                self.index.commit()
                self._emit(phase="tokens", current="重建索引词 %d/%d" % (done, len(rows)),
                           force=True)
                if on_progress:
                    on_progress(self.progress)
        self.index.commit()
        self.index.mark_tokens_current()
        self._emit(phase="tokens", current="重建索引词完成：%d 份" % done, force=True)
        if on_progress:
            on_progress(self.progress)
        return done

    # ------------------------------------------------------------ 构建
    def run(self, roots=None, rebuild=False, sweep=True, retry_errors=False):
        roots = [str(r) for r in (roots or self.cfg.get("roots") or [])]
        if not roots:
            raise ValueError("尚未配置索引根目录（roots 为空）")
        self._stop.clear()
        self._retry_errors = bool(retry_errors)
        self.progress = Progress(phase="scan", started=time.time())
        self._last_emit = 0.0

        files = []
        for path, st in self.collect(roots):
            files.append((path, st))
        self._sync_skipped()
        self._emit(total=len(files), scanned=len(files), phase="index", force=True)

        if rebuild:
            self._purge_under(roots)
        # 老索引库没存正文（检索时得现抽，很慢）→ 整体回填一次
        refresh_content = self.index.content_stale()
        if not refresh_content and self.index.tokens_stale():
            self.rebuild_tokens()          # 切词方式升级：几秒，不动文件

        workers = max(1, int(self.cfg.get("workers", 4)))
        max_bytes = int(self.cfg.get("max_file_mb", 64)) * 1024 * 1024
        pool = ThreadPoolExecutor(max_workers=workers)
        pending = {}
        since_commit = 0
        last_commit = time.time()

        def maybe_commit(force=False):
            """分批提交：按条数或时间，避免一次 OCR 很慢时中断丢掉几小时成果。"""
            nonlocal since_commit, last_commit
            if not force and since_commit < COMMIT_EVERY_DOCS \
                    and time.time() - last_commit < COMMIT_EVERY_SECONDS:
                return
            if since_commit:
                self.index.commit()
                since_commit = 0
            last_commit = time.time()

        def handle(future):
            nonlocal since_commit
            path, st, result, err = future.result()
            ext = file_ext(path)
            if err:
                # 用户按了停止导致的失败不算错：不写记录，下次索引会重新处理
                if self._stop.is_set():
                    self._emit(current=path)
                    return
                if "已跳过" in err or "疑似二进制" in err:
                    # "这不是文本文件"不是失败，别混进失败统计里吓人
                    meta = {"path": path, "name": os.path.basename(path),
                            "ext": ext, "kind": None, "size": st.st_size,
                            "mtime": st.st_mtime, "ino": st.st_ino,
                            "dev": st.st_dev, "chars": 0}
                    self.index.upsert(meta, tokens=None, state="empty",
                                      warning=err)
                    self._bump("empty")
                    self._emit(current=path)
                    return
                meta = {"path": path, "name": os.path.basename(path), "ext": ext,
                        "kind": None, "size": st.st_size, "mtime": st.st_mtime,
                        "ino": st.st_ino, "dev": st.st_dev, "chars": 0}
                self.index.upsert(meta, tokens=None, state="error", err=err)
                self._bump("failed")
                self._emit(current=path, message=err)
            else:
                # 把"文件名 + 路径"一并切进索引：用户经常直接按文件名的词去搜
                # （例如搜"选民登记联系函汇总"，而正文里只有"选民登记联系函"）。
                # 正文本身仍单独存在 docs.content 里，预览不受影响。
                head = "%s\n%s\n" % (os.path.basename(path), path)
                tokens = tokenizer.index_tokens(head + result.text)
                meta = {"path": path, "name": os.path.basename(path), "ext": ext,
                        "kind": result.kind, "size": st.st_size,
                        "mtime": st.st_mtime, "ino": st.st_ino,
                        "dev": st.st_dev, "chars": len(result.text)}
                # 关着 OCR 时抽不到正文的（扫描件就是这种），记成 pending：
                # 下次开启 OCR 会自动重跑，不会像 ok 那样被指纹挡掉。
                state = "ok"
                warning = result.warning or None
                if not result.text.strip():
                    if not self.cfg.get("ocr_enabled", True):
                        # 关着 OCR 的扫描件：记待补，下次开启 OCR 自动重跑
                        state = "pending"
                        warning = "无文本层，待开启 OCR 后补抽"
                        self._bump("pending")
                    else:
                        # 已经尽力（含 OCR）却没有文字：照片、空白页之类，
                        # 不算失败，也不该反复重试
                        state = "empty"
                        warning = warning or "没有可识别的文字"
                        self._bump("empty")
                self.index.upsert(meta, tokens=tokens, state=state,
                                  warning=warning,
                                  content=textstore.pack_content(result.text),
                                  pages=textstore.pack_pages(result.page_offsets))
                self._bump("indexed")
                self._emit(current=path)
            since_commit += 1
            maybe_commit()

        try:
            for path, st in files:
                if self._stop.is_set():
                    break
                if not rebuild and not refresh_content \
                        and not self.index.needs_update(
                            path, st.st_size, st.st_mtime,
                            retry_errors=getattr(self, "_retry_errors", False)):
                    self._bump("unchanged")
                    self._emit(current=path)
                    continue
                future = pool.submit(self._extract_one, path, st, max_bytes,
                                     self.cfg)
                pending[future] = path
                if len(pending) >= workers * 4:
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for fut in done:
                        pending.pop(fut, None)
                        handle(fut)
                maybe_commit()

            while pending:
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    pending.pop(fut, None)
                    handle(fut)
            maybe_commit(force=True)
        finally:
            pool.shutdown(wait=True)

        removed = 0
        if sweep and not self._stop.is_set():
            self._emit(phase="sweep", force=True)
            removed = self.sweep(roots)
        self.index.optimize()
        self.index.set_meta("last_index", time.time())
        if not self._stop.is_set():
            self.index.mark_content_current()
        self._emit(phase="stopped" if self._stop.is_set() else "done",
                   finished=time.time(), force=True,
                   message="清理失效记录 %d 条" % removed)
        return self.progress

    def _extract_one(self, path, st, max_bytes, options=None):
        # 已被要求停止就别再开工：线程池里排队的任务要尽快排空，否则解释器退出时
        # 会卡在 concurrent.futures 的"join 工作线程"上，表现为"窗口关了进程还在跑"。
        if self._stop.is_set():
            return path, st, None, "已停止"
        try:
            result = extract(path, max_bytes=max_bytes, options=options)
        except ExtractError as exc:
            return path, st, None, str(exc)
        except Exception as exc:                    # noqa: BLE001 逐文件隔离
            return path, st, None, "%s: %s" % (type(exc).__name__, exc)
        return path, st, result, None

    def _purge_under(self, roots):
        for doc_id, _ in list(self.index.iter_paths_under(roots)):
            self.index.delete_doc(doc_id)
        self.index.commit()

    def sweep(self, roots):
        """删除磁盘上已不存在的记录。"""
        removed = 0
        for doc_id, path in list(self.index.iter_paths_under(roots)):
            if not os.path.exists(path):
                self.index.delete_doc(doc_id)
                removed += 1
        self.index.commit()
        return removed
