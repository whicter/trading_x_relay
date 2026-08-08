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
    pushed_at    TEXT,               -- NULL = 未推送
    push_attempts INTEGER NOT NULL DEFAULT 0   -- 推送尝试次数（重试队列用）
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


def _iso_minus_hours(now_iso: str, hours: int) -> str:
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return (dt - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class PostStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # 老库迁移：push_attempts 是 2026-08-08 新增列
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "push_attempts" not in cols:
            self.conn.execute(
                "ALTER TABLE posts ADD COLUMN push_attempts INTEGER NOT NULL DEFAULT 0")
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

    def has_post(self, post_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM posts WHERE post_id=?",
                                 (post_id,)).fetchone() is not None

    def has_handle(self, handle: str) -> bool:
        """该账号此前是否已有帖子入库——用于区分 bootstrap 与真正的漏帖。"""
        return self.conn.execute(
            "SELECT 1 FROM posts WHERE handle=? LIMIT 1", (handle,)).fetchone() is not None

    def newest_post_id(self, handle: str) -> str | None:
        """该账号已入库的最新一条 post_id —— 增量抓取的基准线。

        按 **id 数值**排序而不是 published_at：X 的雪花号全局单调递增，是权威
        的先后判据；published_at 是页面给的字符串，缺失或格式异常时排序会悄悄
        错位，基准线一错就会误判"已接上"从而真漏帖。
        置顶帖永远在页面顶部却很旧，不能当基准线，排除。
        没有 published_at 的行同样排除——那是没 hydrate 完的空壳，
        内容我们其实没拿到；拿它当基准线等于宣称"这条已收录"（第二道防线，
        第一道在 extract_posts）。"""
        row = self.conn.execute(
            "SELECT post_id FROM posts WHERE handle=? AND is_pinned=0 "
            "AND published_at IS NOT NULL "
            "ORDER BY CAST(post_id AS INTEGER) DESC LIMIT 1", (handle,)).fetchone()
        return row[0] if row else None

    def pending_push(self, blocked: list, max_age_hours: int,
                     now_iso: str, retry_max: int) -> list:
        """待重试推送的帖：入库了、该推、但至今 pushed_at 仍为 NULL。

        **存在的理由（2026-08-08 事故）**：原流程是「先 insert_post 再 tg_send，
        只有发送成功才 mark_pushed」。发送失败时帖子已经在库里，下一轮
        insert_post 返回 False 直接 continue —— 那条帖**永久丢失，无重试、
        无告警**。实测 Telegram 限流（单 chat 约 20 条/分钟）在 bootstrap
        突发推送时吃掉了 2 条 index_levels。

        与主仓库 alert_outbox 同一模式：先落库、只有 2xx 才标 delivered、
        失败留在队列里重试。push_attempts 计数防止一条坏帖无限重试刷屏。
        """
        sql = ("SELECT post_id, handle, published_at, text, has_image, "
               "classification, levels FROM posts "
               "WHERE pushed_at IS NULL AND is_retweet=0 AND is_pinned=0 "
               "AND published_at IS NOT NULL AND published_at > ? "
               "AND push_attempts < ? ")
        params = [_iso_minus_hours(now_iso, max_age_hours), retry_max]
        if blocked:
            sql += "AND (classification IS NULL OR classification NOT IN (%s)) " % ",".join("?" * len(blocked))
            params += blocked
        sql += "ORDER BY published_at"
        return self.conn.execute(sql, params).fetchall()

    def bump_attempt(self, post_id: str):
        self.conn.execute(
            "UPDATE posts SET push_attempts = push_attempts + 1 WHERE post_id=?",
            (post_id,))
        self.conn.commit()

    def give_up_count(self, retry_max: int) -> int:
        """已放弃重试的条数——非零说明有帖子真的没送出去，必须让人知道。"""
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE pushed_at IS NULL "
            "AND push_attempts >= ?", (retry_max,)).fetchone()[0]

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
