"""X 博主点位中继（quantrift_x_relay 独立小服务）。

定位（quantrift_index_future/strategy_explore.md §A，2026-08-02 定稿；08-06 用户拍板施工）：
**信息中继 + 决策支持，不是 alpha**。抓取指定博主的 X 帖子 → 启发式分类
（指数分析 / 个股 / 其他）→ 全量落库（append-only）→ 只把指数类推送 Telegram。

铁律（继承自主仓库讨论记录，不可放宽）：
- X 内容是**不可信输入**：可推送、可入库打分，**绝不进入任何下单路径**。
  本服务不 import ib_insync、不占 clientId、不碰任何订单/仓位文件
  （tests/test_x_relay.py 的 AST 扫描强制此边界）。
- **登录不能由 Claude 代做**：`--login` 打开有头浏览器由用户人工登录一次，
  凭据保存在本地持久化 profile（runtime/x_profile/，不入库）。
- **静默失败必须显性化**：登录失效/整轮零帖子必须告警，不得静默返回空。
- 定位是 4-8 周验证探针（回答「这些博主准不准」），不是长期管道；
  选择器脆弱是已接受的成本。

用法：
  venv/bin/python3.11 x_relay.py --login          # 一次性：人工登录
  venv/bin/python3.11 x_relay.py --once --dry-run # 抓一轮，只打印不推送
  venv/bin/python3.11 x_relay.py --once           # 抓一轮并推送
  venv/bin/python3.11 x_relay.py --loop           # 常驻轮询（pm2 用这个）
  venv/bin/python3.11 x_relay.py --stats          # 台账分布（含有图无数字占比）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from classifier import classify_post
from store import PostStore

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "runtime" / "x_profile"
DB_PATH = BASE / "runtime" / "x_posts.sqlite3"
HEARTBEAT = BASE / "runtime" / "heartbeat.json"
ALERT_STATE = BASE / "runtime" / "alert_state.json"

ET = ZoneInfo("America/New_York")

POLL_SECONDS = 900                 # 15 分钟一轮
JITTER_SECONDS = 120               # ±2 分钟抖动，避免完全规律的访问节奏
PER_ACCOUNT_DELAY = (4.0, 9.0)     # 账号间随机停顿
PUSH_MAX_AGE_HOURS = 12            # 只推这么久以内发布的帖（防首轮回填刷屏）
LOGIN_ALERT_COOLDOWN = 6 * 3600    # 登录失效告警冷却
ALL_FAIL_ALERT_AFTER = 3           # 连续 N 轮全账号失败才告警（吸收网络抖动）
PUSH_CLASSES = {"index_levels", "index_view"}


@dataclass
class Account:
    handle: str
    note: str = ""
    assume_index: bool = False     # 纯数字帖也按指数点位处理（见 classifier）


# 名单来源：strategy_explore.md §A.4/A.12 + 用户 2026-08-06 确认
# （Dayu 喊指数；Mancini 在 X 免费发点位；三个未知账号靠分类器自动判别形态）
ACCOUNTS = [
    Account("AdamMancini4", "ES 日内点位（Mancini）", assume_index=True),
    Account("Investor_Dayu", "波浪理论，喊指数（用户确认）", assume_index=True),
    Account("time_and_trade", "内容形态未知，由分类器判别"),
    Account("willem82457275", "内容形态未知，由分类器判别"),
    Account("novicetrader888", "内容形态未知，由分类器判别"),
]


def log(msg: str):
    print(f"[{datetime.now(ET):%m-%d %H:%M:%S} ET] {msg}", flush=True)


# ── Telegram（与主仓库同模式：token/chat 走环境变量，不落盘） ──────────
def resolve_tg():
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = (os.environ.get("X_RELAY_TG_CHAT_ID", "").strip()
            or os.environ.get("TG_CHAT_ID", "").strip())
    return (token, chat) if token and chat else (None, None)


def tg_send(text: str) -> bool:
    token, chat = resolve_tg()
    if not token:
        log("TG 未配置（缺 TG_TOKEN/TG_CHAT_ID），本条只落库不推送")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text,
            "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:                                    # noqa: BLE001
        log(f"TG 发送失败: {e}")
        return False


def alert_once(key: str, text: str, cooldown: int):
    """带冷却的告警（登录失效这类持续状态，不刷屏但也不静默）。"""
    state = {}
    if ALERT_STATE.exists():
        try:
            state = json.loads(ALERT_STATE.read_text())
        except Exception:                                     # noqa: BLE001
            state = {}
    now = time.time()
    if now - state.get(key, 0) < cooldown:
        return
    if tg_send(text):
        state[key] = now
        ALERT_STATE.write_text(json.dumps(state))


def write_heartbeat(ok: bool, detail: dict):
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": ok, **detail}))


# ── 抓取 ────────────────────────────────────────────────────
def looks_logged_out(page) -> bool:
    """仅在页面拿不到正规 tweet article 时调用（实测 2026-08-06：未登录的
    profile 页是降级渲染——裸 <article> 无 data-testid、无 <time>，并带
    5 个指向 login 的导航链接；登录后才有 article[data-testid="tweet"]）。
    因此「login 链接存在」在此调用前提下即可判登录墙，不会误伤正文里
    偶然含 login 字样链接的已登录页——那种页有正规 article，根本走不到这里。"""
    url = page.url or ""
    if "/i/flow/login" in url or url.rstrip("/").endswith("/login"):
        return True
    for sel in ('[data-testid="loginButton"]', '[data-testid="login"]',
                'a[href="/login"]', 'a[href*="login"]'):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:                                     # noqa: BLE001
            continue
    return False


def extract_posts(page, handle: str) -> list:
    """从当前已加载的账号页提取帖子（不滚动之外的逻辑不放这里）。"""
    posts = []
    for art in page.locator('article[data-testid="tweet"]').all():
        try:
            link = art.locator('a[href*="/status/"]:has(time)').first
            href = link.get_attribute("href") or ""
            parts = href.strip("/").split("/")
            if "status" not in parts:
                continue
            author = parts[0]
            post_id = parts[parts.index("status") + 1].split("?")[0]
            t = art.locator("time").first.get_attribute("datetime")
            txt_loc = art.locator('[data-testid="tweetText"]')
            text = txt_loc.first.inner_text() if txt_loc.count() else ""
            social = art.locator('[data-testid="socialContext"]')
            social_txt = social.first.inner_text() if social.count() else ""
            posts.append({
                "post_id": post_id,
                "handle": handle,
                "author": author,
                "published_at": t,
                "text": text,
                "has_image": art.locator('[data-testid="tweetPhoto"]').count() > 0,
                "is_retweet": (author.lower() != handle.lower()
                               or "repost" in social_txt.lower()
                               or "转推" in social_txt),
                "is_pinned": ("pin" in social_txt.lower() or "置顶" in social_txt),
            })
        except Exception as e:                                # noqa: BLE001
            log(f"  单条解析失败（跳过）: {e}")
    return posts


def fetch_account(ctx, acct: Account) -> list:
    """抓一个账号页；登录失效抛 LoginWallError，其余异常向上抛。"""
    page = ctx.new_page()
    try:
        page.goto(f"https://x.com/{acct.handle}", timeout=45_000,
                  wait_until="domcontentloaded")
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
        except Exception:                                     # noqa: BLE001
            # 正规 tweet article 拿不到才需要区分：登录墙 vs 其他故障
            if looks_logged_out(page):
                raise LoginWallError(acct.handle)
            raise
        page.mouse.wheel(0, 2500)                             # 多拿一屏
        page.wait_for_timeout(1500)
        return extract_posts(page, acct.handle)
    finally:
        page.close()


class LoginWallError(RuntimeError):
    pass


# ── 推送格式 ────────────────────────────────────────────────
CLASS_CN = {"index_levels": "指数点位", "index_view": "指数观点",
            "stock": "个股", "other": "其他"}


def format_push(post: dict, cls) -> str:
    when = ""
    if post.get("published_at"):
        try:
            dt = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
            when = f" · {dt.astimezone(ET):%m-%d %H:%M} ET"
        except ValueError:
            pass
    lines = [f"📡 [X·{CLASS_CN.get(cls.label, cls.label)}] @{post['handle']}{when}"]
    text = (post.get("text") or "").strip()
    lines.append(text[:900] + ("…" if len(text) > 900 else ""))
    if cls.levels:
        lines.append("📍 " + ", ".join(f"{v:,.10g}" for v in cls.levels[:12]))
    if post.get("has_image") and not cls.levels:
        lines.append("🖼 帖内含图（点位可能在图中，文本未抽出数字）")
    lines.append(f"🔗 x.com/{post['handle']}/status/{post['post_id']}")
    return "\n".join(lines)


def should_push(post: dict, cls, now_utc: datetime) -> bool:
    if cls.label not in PUSH_CLASSES:
        return False
    if post.get("is_retweet"):
        return False
    if post.get("published_at"):
        try:
            dt = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
            if (now_utc - dt).total_seconds() > PUSH_MAX_AGE_HOURS * 3600:
                return False                                  # 防首轮回填刷屏
        except ValueError:
            pass
    return True


# ── 主流程 ──────────────────────────────────────────────────
def run_once(store: PostStore, dry_run: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    now_utc = datetime.now(timezone.utc)
    result = {"ok": [], "fail": [], "login_wall": False, "new": 0, "pushed": 0}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True,
            viewport={"width": 1280, "height": 1600},
            args=["--disable-blink-features=AutomationControlled"])
        try:
            for acct in ACCOUNTS:
                ts = datetime.now(timezone.utc).isoformat()
                try:
                    posts = fetch_account(ctx, acct)
                except LoginWallError:
                    result["login_wall"] = True
                    store.record_fetch(ts, acct.handle, 0, 0, False, "login_wall")
                    log(f"@{acct.handle}: 登录墙！")
                    break                                     # 登录失效对所有账号成立
                except Exception as e:                        # noqa: BLE001
                    result["fail"].append(acct.handle)
                    store.record_fetch(ts, acct.handle, 0, 0, False, str(e)[:200])
                    log(f"@{acct.handle}: 抓取失败 {e}")
                    continue

                n_new = 0
                for post in posts:
                    cls = classify_post(post["text"],
                                        assume_index=acct.assume_index)
                    post["fetched_at"] = ts
                    post["classification"] = cls.label
                    post["levels"] = cls.levels
                    if not store.insert_post(post):
                        continue                              # 已见过（含置顶重复）
                    n_new += 1
                    if should_push(post, cls, now_utc):
                        msg = format_push(post, cls)
                        if dry_run:
                            log(f"—— dry-run 应推送 ——\n{msg}")
                        elif tg_send(msg):
                            store.mark_pushed(
                                post["post_id"],
                                datetime.now(timezone.utc).isoformat())
                            result["pushed"] += 1
                result["ok"].append(acct.handle)
                result["new"] += n_new
                store.record_fetch(ts, acct.handle, len(posts), n_new, True)
                log(f"@{acct.handle}: 看到 {len(posts)} 条，新 {n_new} 条")
                time.sleep(random.uniform(*PER_ACCOUNT_DELAY))
        finally:
            ctx.close()

    if result["login_wall"]:
        alert_once("login_wall",
                   "⚠️ [X中继] 登录已失效，抓取停摆。请在 Mac Studio 上执行:\n"
                   "cd ~/Documents/quantrift_x_relay && venv/bin/python3.11 x_relay.py --login",
                   LOGIN_ALERT_COOLDOWN)
    write_heartbeat(
        ok=not result["login_wall"] and bool(result["ok"]),
        detail={"accounts_ok": result["ok"], "accounts_fail": result["fail"],
                "login_wall": result["login_wall"],
                "new_posts": result["new"], "pushed": result["pushed"]})
    return result


def run_loop(store: PostStore):
    log(f"进入轮询循环：每 {POLL_SECONDS}s ±{JITTER_SECONDS}s，"
        f"{len(ACCOUNTS)} 个账号，推送类别 {sorted(PUSH_CLASSES)}")
    consecutive_all_fail = 0
    while True:
        try:
            r = run_once(store)
            if r["login_wall"]:
                consecutive_all_fail = 0                      # 已单独告警
            elif not r["ok"]:
                consecutive_all_fail += 1
                if consecutive_all_fail == ALL_FAIL_ALERT_AFTER:
                    alert_once("all_fail",
                               f"⚠️ [X中继] 连续 {consecutive_all_fail} 轮所有账号抓取失败"
                               "（非登录墙），可能是选择器失效或网络问题，需人工看日志",
                               3600)
            else:
                consecutive_all_fail = 0
        except Exception as e:                                # noqa: BLE001
            log(f"轮询异常（下一轮重试）: {e}")
            write_heartbeat(ok=False, detail={"error": str(e)[:300]})
        time.sleep(POLL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS))


def do_login():
    """有头浏览器 + 持久化 profile，登录由用户人工完成（铁律：Claude 不代输密码）。"""
    from playwright.sync_api import sync_playwright
    print("将打开浏览器窗口，请人工登录 x.com。登录完成后回到终端按 Enter。")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False,
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://x.com/login")
        input("登录完成后按 Enter 关闭浏览器并保存会话 … ")
        ctx.close()
    print(f"会话已保存到 {PROFILE_DIR}")


def print_stats(store: PostStore):
    s = store.stats()
    print(f"台账共 {s['total']} 条")
    print(f"有图但文本无数字: {s['image_no_levels']} 条"
          f"（占 {s['image_no_levels'] / s['total'] * 100:.0f}%）" if s["total"]
          else "（空台账）")
    print(f"{'账号':<20}{'分类':<14}{'条数':>6}{'已推送':>8}")
    for handle, cls, n, pushed in s["by_handle_class"]:
        print(f"{handle:<20}{cls or '-':<14}{n:>6}{pushed or 0:>8}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.login:
        do_login()
        return 0
    store = PostStore(DB_PATH)
    try:
        if args.stats:
            print_stats(store)
        elif args.once:
            r = run_once(store, dry_run=args.dry_run)
            log(f"完成：ok={r['ok']} fail={r['fail']} 新帖 {r['new']} 推送 {r['pushed']}"
                f"{' [登录墙]' if r['login_wall'] else ''}")
            return 2 if r["login_wall"] else 0
        elif args.loop:
            run_loop(store)
        else:
            ap.print_help()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
