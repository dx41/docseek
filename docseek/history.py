"""搜索历史：最近查询的记录、删除与清空。

存放位置 `~/.config/docseek/history.json`，结构：

    {"version": 1, "items": [{"query": "...", "count": 3, "last": 1750000000.0}]}

最近使用的排在最前面，最多保留 `MAX_ITEMS` 条。所有写操作都是"临时文件 + 原子替换"，
避免中途崩溃留下半个 JSON。
"""

import json
import os
import time

from . import config

HISTORY_FILE = config.CONFIG_DIR / "history.json"
MAX_ITEMS = 50
MAX_QUERY_LEN = 200


def _read():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    cleaned = []
    for item in items:
        if isinstance(item, str):
            cleaned.append({"query": item, "count": 1, "last": 0.0})
        elif isinstance(item, dict) and item.get("query"):
            cleaned.append({"query": str(item["query"]),
                            "count": int(item.get("count") or 1),
                            "last": float(item.get("last") or 0.0)})
    return cleaned


def _write(items):
    config.ensure_dirs()
    payload = {"version": 1, "items": items[:MAX_ITEMS]}
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)
    except OSError:
        pass


def entries(limit=None):
    """按最近使用排序的查询词列表。"""
    items = _read()
    queries = [item["query"] for item in items]
    return queries[:limit] if limit else queries


def detailed(limit=None):
    """带次数与时间的完整记录（给界面展示用）。"""
    items = _read()
    return items[:limit] if limit else items


def add(query):
    """记录一次搜索；已存在则前移并累加次数。"""
    query = (query or "").strip()[:MAX_QUERY_LEN]
    if not query:
        return _read()
    items = _read()
    now = time.time()
    kept = [item for item in items if item["query"] != query]
    previous = next((item for item in items if item["query"] == query), None)
    kept.insert(0, {"query": query,
                    "count": (previous["count"] + 1) if previous else 1,
                    "last": now})
    _write(kept)
    return kept


def remove(query):
    """删除一条记录。"""
    items = [item for item in _read() if item["query"] != query]
    _write(items)
    return items


def clear():
    """清空全部历史。"""
    _write([])


def count():
    return len(_read())


def relative_time(stamp):
    """把时间戳变成"刚刚 / 5 分钟前 / 3 天前"这类短文案。"""
    if not stamp:
        return ""
    delta = max(0, time.time() - stamp)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    if delta < 86400 * 30:
        return "%d 天前" % (delta // 86400)
    return time.strftime("%Y-%m-%d", time.localtime(stamp))
