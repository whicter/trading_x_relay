"""帖子台账（SQLite WAL，append-only）。

原则与主仓库一致：原始事实（帖子原文、发布时间、抓取时间）永不修改；
`pushed_at` / `classification` 是投递状态与派生标签，允许更新（分类器升级后
可对历史行重打标签，原文不动即可重放验证）。post_id 主键天然幂等——
置顶帖每轮重复出现、翻页重叠都不会写重。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id      TEXT PRIMARY KEY,
    handle       TEXT NOT NULL,      -- 我们盯的账号
    author       TEXT NOT NULL,      -- 帖子实际作者（≠handle 即转发）
    published_at TEXT,               -- UTC ISO（来自 <time datetime>）
    fetched_at   TEXT NOT NULL,      -- UTC ISO
    text         TEXT,
    has_image    INTEGER NOT NULL DEFAULT 0,
    is_retweet   INTEGER NOT NULL DEFAULT 0,
    is_pinned    INTEGER NOT NULL DEFAULT 0,
    classification TEXT,
    levels       TEXT,               -- JSON array
    pushed_at    TEXT                -- NULL = 未推送
);
CREATE TABLE IF NOT EXISTS fetch_log (
    ts      TEXT NOT NULL,
    handle  TEXT NOT NULL,
    n_seen  INTEGER,
    n_new   INTEGER,
    ok      INTEGER NOT NULL,
    error   TEXT
);
"""


class PostStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert_post(self, post: dict) -> bool:
        """写入一条帖子；已存在（同 post_id）返回 False。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO posts
               (post_id, handle, author, published_at, fetched_at, text,
                has_image, is_retweet, is_pinned, classification, levels)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (post["post_id"], post["handle"], post["author"],
             post.get("published_at"), post["fetched_at"], post.get("text", ""),
             int(post.get("has_image", False)), int(post.get("is_retweet", False)),
             int(post.get("is_pinned", False)), post.get("classification"),
             json.dumps(post.get("levels", []))))
        self.conn.commit()
        return cur.rowcount > 0

    def has_handle(self, handle: str) -> bool:
        """该账号此前是否已有帖子入库——用于区分 bootstrap 与真正的窗口打满。"""
        return self.conn.execute(
            "SELECT 1 FROM posts WHERE handle=? LIMIT 1", (handle,)).fetchone() is not None

    def mark_pushed(self, post_id: str, ts: str):
        self.conn.execute("UPDATE posts SET pushed_at=? WHERE post_id=? AND pushed_at IS NULL",
                          (ts, post_id))
        self.conn.commit()

    def record_fetch(self, ts: str, handle: str, n_seen: int, n_new: int,
                     ok: bool, error: str = None):
        self.conn.execute(
            "INSERT INTO fetch_log (ts, handle, n_seen, n_new, ok, error) VALUES (?,?,?,?,?,?)",
            (ts, handle, n_seen, n_new, int(ok), error))
        self.conn.commit()

    def stats(self) -> dict:
        """分类分布 + 「有图无数字」占比（strategy_explore.md §A.9 要求显式统计）。"""
        rows = self.conn.execute(
            "SELECT handle, classification, COUNT(*), SUM(pushed_at IS NOT NULL) "
            "FROM posts GROUP BY handle, classification").fetchall()
        img = self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE has_image=1 AND "
            "(levels IS NULL OR levels='[]')").fetchone()[0]
        total = self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        return {"by_handle_class": rows, "image_no_levels": img, "total": total}

    def close(self):
        self.conn.close()
