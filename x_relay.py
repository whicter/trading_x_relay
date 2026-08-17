"""X 博主点位中继（trading_x_relay 独立小服务）。

定位（quantrift_index_future/strategy_explore.md §A，2026-08-02 定稿；08-06 用户拍板施工）：
**信息中继 + 决策支持，不是 alpha**。抓取指定博主的 X 帖子 → 启发式分类
（指数分析 / 个股 / 其他）→ 全量落库（append-only）→ 只把指数类推送 Telegram。

**免登录**（2026-08-06 实测确定的路线）：X 未登录时 profile 页是降级渲染，
但它带 **schema.org 微数据**（`itemprop=identifier/datePublished/articleBody/
author/ImageObject`）——比登录后的 React DOM（`data-testid`）更干净也更稳定，
因为那是给搜索引擎看的结构化数据。五个账号实测均可拿到最近 1-2 天的帖子。
好处：无凭据、无账号封禁风险、无 session 过期、无 Google/X 登录反自动化拦截。
代价：**每账号每轮只能看到约 5 条最新帖，且不能下滑加载更多**——靠 15 分钟
轮询覆盖；`saturation` 检测负责在可能漏帖时显式告警（见 run_once）。

铁律（继承自主仓库讨论记录，不可放宽）：
- X 内容是**不可信输入**：可推送、可入库打分，**绝不进入任何下单路径**。
  本服务不 import ib_insync、不占 clientId、不碰任何订单/仓位文件
  （tests/test_x_relay.py 的 AST 扫描强制此边界）。
- **静默失败必须显性化**：页面结构失效（拿不到任何微数据）、连续整轮失败、
  可能漏帖（saturation）都必须告警，不得静默返回空。
- 定位是 4-8 周验证探针（回答「这些博主准不准」），不是长期管道；
  选择器脆弱是已接受的成本。

用法：
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
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from classifier import PUSH_BLOCKED, Classification, classify_post
from store import PostStore

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "runtime" / "x_profile"     # 只用于保留 guest cookie，无凭据
DB_PATH = BASE / "runtime" / "x_posts.sqlite3"
ALERT_STATE = BASE / "runtime" / "alert_state.json"

# 心跳写进**主仓库**的心跳目录，文件名 = pm2 进程名，这样
# quantrift_index_future/health_watchdog.py 才监控得到（CLAUDE.md 规则 2：
# 不登记 = watchdog 完全不监控它，进程挂掉没有任何人知道）。
# 格式与 heartbeat.py::write_heartbeat 一致：{name, ts, connected, ...extra}。
# 不 import 主仓库代码——两个仓库保持独立，只共享这个 JSON 约定。
PM2_NAME = "x-levels-relay"
HEARTBEAT_DIR = Path(os.environ.get(
    "QR_HEARTBEAT_DIR",
    Path.home() / "Documents/quantrift_index_future/data/runtime/heartbeat"))

ET = ZoneInfo("America/New_York")

POLL_SECONDS = 900                 # 15 分钟一轮
JITTER_SECONDS = 120               # ±2 分钟抖动，避免完全规律的访问节奏
PER_ACCOUNT_DELAY = (4.0, 9.0)     # 账号间随机停顿
PUSH_MAX_AGE_HOURS = 12            # 只推这么久以内发布的帖（防首轮回填刷屏）
STRUCTURE_ALERT_COOLDOWN = 6 * 3600   # 页面结构失效告警冷却
ALL_FAIL_ALERT_AFTER = 3           # 连续 N 轮全账号失败才告警（吸收网络抖动）
# 结构失效同样要去抖。StructureError 是 25 秒选择器超时抛的，"X 改版了"和
# "这一次页面加载慢/被临时限流"产生**完全相同**的信号，单轮判不出区别。
# 2026-08-09 实测：10:15 那一轮 4 个账号整齐地每 31 秒失败一个（25s 超时 +
# 翻页开销），下一轮全部恢复，此后 12 小时正常——却报了"抓取停摆"。
# 真改版会持续数小时，多等一轮（约 15 分钟）零成本；单轮误报的代价是
# 人对告警脱敏。取 2 而非 all_fail 的 3，因为结构失效是更具体的信号。
STRUCTURE_ALERT_AFTER = 2
# 2026-08-08：推送口径由白名单反转为黑名单，判据搬进 classifier.PUSH_BLOCKED
# （用户决定：个股/商品/宏观/加密行情都要，只拦代币推广与杂谈）。
# 此处不再维护类名清单，避免两处漂移——新增标签只需改 classifier。
PUSH_RETRY_MAX = 5                 # 单条帖推送失败的最大重试轮数


@dataclass
class Account:
    handle: str
    note: str = ""
    assume_index: bool = False     # 纯数字帖也按指数点位处理（见 classifier）


# 名单来源：strategy_explore.md §A.4/A.12。内容形态为 2026-08-06 实抓验证，
# 不再是先验判断（A.12 三个"未知账号"的问题由此解锁）。
ACCOUNTS = [
    # 实测：每 1-2 小时一帖，#ES_F 完整点位（"7741 (hit), 7708 were next down"），
    # 不是「see newsletter」钩子 —— A.12 该项验证通过
    Account("AdamMancini4", "ES 日内点位（Mancini）", assume_index=True),
    # 实测：$spx/$ndx 波浪 + 明确点位与自报进出场（"short entered at 7789"）
    Account("Investor_Dayu", "波浪理论，喊指数（用户确认 + 实测）", assume_index=True),
    # 实测：中文指数评论，带点位（"ES 8000"、"7660 回撤到 7760"）。中文行文常
    # 不写"纳指/标普"二字（"从我预测7660回撤到现在7760"），故也标 assume_index
    Account("novicetrader888", "中文指数评论，含点位", assume_index=True),
    # 实测：S&P500 自制指标，**点位在图里**，文本只有 "Updated 1-2-3-beyond"
    # → 大概率只出 index_view。留着是为了给 A.9「有图无数字」占比提供样本
    Account("willem82457275", "S&P500 指标图，点位多在图中"),
    # ── 2026-08-11 用户指定新增四个，下列形态均为当日实抓验证 ──────────
    # 实测 4 条/22.3h（约 7.4h 一帖）：SPY GEX 伽马敞口更新，带完整点位
    #（"SPY: 774.38 ... 773 / 772 / 775 / 780"）→ index_levels。
    # 不标 assume_index：它自己写 $SPY/SPX，分类器已能正确归类。
    Account("gexedgeio", "SPY/SPX GEX 伽马点位（实测 index_levels）"),
    # 实测 4 条/2.3h（约 0.8h 一帖）：盘中内部结构、板块轮动、暗池大单
    #（"INTERNALS + ROTATION"、"Dark pool alert $273.6M SPY buy"）。
    # 多为定性描述无点位 → index_view / commodity，四条全部可推。
    Account("alphaticaio", "盘中内部结构/轮动/暗池扫描（实测 index_view 为主）"),
    # 实测 6 条/0.6h（**约 6 分钟一帖**）：宏观新闻流。两个已知代价——
    # ① 可见窗口只有约 36 分钟，是目前唯一有撑爆风险的账号（见下方 gap 检测）；
    # ② 信噪比低：6 条里 5 条判 other 被拦，仅 1 条 macro 可推。
    # **绝不能标 assume_index**：实测 "subprime (FICO <660)" 会被当成指数点位
    # 660 而误判为 index_levels。用户 2026-08-11 明确指定加入。
    Account("zerohedge", "宏观新闻流，约 6 分钟一帖（信噪比低，勿加 assume_index）"),
    # 实测 6 条/13.5h（约 2.7h 一帖）：个股（$TSLA/$MU/$AMD）带明确点位
    #（"retest key 891 level"、"a move to 337+"）→ stock，六条全部可推。
    # 不标 assume_index：个股点位若按指数处理会被 600 下限误伤或误归类。
    Account("mentoviax", "个股 TSLA/MU/AMD 带点位（实测 stock）"),
    # 实测：加密代币推广（Interlink/StarX/PACT），与指数无关。
    # 2026-08-08 剔除 @time_and_trade：14 条实抓里 12 条是代币项目推广
    #（Interlink/StarX/Pact/Parallax——用户数里程碑、TGE、KYC、生态叙事），
    # 与交易观点无关。分类器的 promo 规则能拦住，但让一个 86% 是广告的账号
    # 留在名单里只会消耗每轮抓取预算并稀释信噪比。用户 2026-08-08 决定移除。
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


def _load_alert_state() -> dict:
    if ALERT_STATE.exists():
        try:
            return json.loads(ALERT_STATE.read_text())
        except Exception:                                     # noqa: BLE001
            pass
    return {}


def alert_once(key: str, text: str, cooldown: int):
    """带冷却的告警（登录失效这类持续状态，不刷屏但也不静默）。"""
    state = _load_alert_state()
    now = time.time()
    if now - state.get(key, 0) < cooldown:
        return
    if tg_send(text):
        state[key] = now
        ALERT_STATE.write_text(json.dumps(state))


def resolve_alert(key: str, text: str) -> bool:
    """故障解除：只有此前真发出过 `key` 这条告警时，才通知一次并清掉状态。

    没告警过就静默返回，否则每一轮正常抓取都会刷一条"已恢复"。发送失败
    **不清状态**，下一轮再试——与 alert_once 同口径：宁可迟到，不可把
    "已恢复"这条事实静默丢掉。

    2026-08-09 之前根本没有恢复通知：当天 10:15 一次单轮抖动报了"抓取停摆"，
    10:32 就自愈了，但那条告警在手机上一直挂着，人看到的始终是"还停着"——
    实际此后 12 小时全部正常。**故障告警必须成对**，只报不销等于长期误导。
    """
    state = _load_alert_state()
    if key not in state:
        return False
    if not tg_send(text):
        return False
    state.pop(key, None)
    ALERT_STATE.write_text(json.dumps(state))
    return True


EXPORT_PATH = Path.home() / "Documents/quantrift_index_future/data/runtime/x_levels_export.json"
EXPORT_WINDOW_HOURS = 48


def export_for_brief(store: PostStore):
    """导出近 EXPORT_WINDOW_HOURS 的可推送帖到**主仓库**，供盘前/盘后简报只读。

    跨仓库耦合的方向是刻意的：**生产者导出契约文件，消费者只读**，与本进程
    往主仓库写 heartbeat 是同一模式。若反过来让主仓库直接读本仓库的
    x_posts.sqlite3，耦合的就变成本仓库的内部 schema——本仓库以后想重构
    就会静默打破简报。

    导出带 generated_at，简报据此判断新鲜度；数据陈旧时简报应明说
    「X 中继数据滞后 N 分钟」而不是静默给空（fail closed）。
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc)
             - timedelta(hours=EXPORT_WINDOW_HOURS)).isoformat().replace("+00:00", "Z")
    rows = store.conn.execute(
        "SELECT post_id, handle, published_at, text, classification, levels, "
        "ticker_levels, has_image FROM posts "
        "WHERE published_at > ? AND is_retweet=0 "
        "ORDER BY published_at", (since,)).fetchall()
    posts = []
    for pid, handle, pub, text, cls, lv, tl, img in rows:
        if cls in PUSH_BLOCKED:
            continue
        posts.append({
            "post_id": pid, "handle": handle, "published_at": pub,
            "classification": cls, "text": text,
            "levels": json.loads(lv or "[]"),
            "ticker_levels": json.loads(tl or "{}"),
            "has_image": bool(img),
            "url": f"https://x.com/{handle}/status/{pid}",
        })
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "window_hours": EXPORT_WINDOW_HOURS,
               "accounts": [a.handle for a in ACCOUNTS],
               "posts": posts}
    try:
        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(EXPORT_PATH.parent),
                                   prefix=".x_levels_export.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, str(EXPORT_PATH))
        except Exception:                                     # noqa: BLE001
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        log(f"导出 {len(posts)} 条到主仓库 x_levels_export.json")
    except Exception as e:                                    # noqa: BLE001
        log(f"导出失败（不影响抓取）: {e}")


def write_heartbeat(ok: bool, detail: dict):
    """原子写主仓库心跳（temp + os.replace）。写失败绝不影响抓取主流程。

    `connected` 的语义（CLAUDE.md 规则 3）：本进程**不连 IB**，
    connected = "本轮至少有一个账号抓取成功"；`orders_enabled=False` 让
    watchdog 用只读引擎的措辞，不会报成"未连接 IB"把人带偏。"""
    rec = {"name": PM2_NAME, "ts": time.time(), "connected": bool(ok),
           "orders_enabled": False, **detail}
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(HEARTBEAT_DIR),
                                   prefix=f".{PM2_NAME}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, str(HEARTBEAT_DIR / f"{PM2_NAME}.json"))
        except Exception:                                     # noqa: BLE001
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:                                    # noqa: BLE001
        log(f"心跳写入失败（不影响抓取）: {e}")


# ── 抓取 ────────────────────────────────────────────────────
# 微数据抽取：schema.org 结构化字段，比 React 的 data-testid 稳定得多。
# 注意 author 块里也有 identifier/image/url 等同名 itemprop，必须排除
# （closest('[itemprop="author"]')），否则会把作者 id 当帖子 id。
# 两种页面形态都要支持：
#   · 未登录 = 降级渲染 + schema.org 微数据（干净，但每页只有约 5 条、不能下滑）
#   · 已登录 = React DOM（data-testid），**支持无限下滑** → 才能做到"补齐上次
#     之后的全部帖子"。登录态由用户人工建立（--login），凭据不经过本程序。
# 优先读微数据；读不到再走 data-testid。两条路产出同一份归一化字段。
_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('article')).map(a => {
  const own = (prop) => Array.from(a.querySelectorAll(`[itemprop="${prop}"]`))
      .filter(e => !e.closest('[itemprop="author"]'));
  const one = (prop) => { const e = own(prop)[0];
      return e ? (e.getAttribute('content') || e.textContent) : null; };
  const authorEl = a.querySelector('[itemprop="author"] [itemprop="alternateName"]');
  let post_id = one('identifier');
  let published_at = one('datePublished');
  // 2026-08-14 X 把正文的 itemprop 从 `articleBody` 改名成了 `text`。
  // 两个都读：改名当天页面上两种形态并存（当日入库 187 条里 52% 空正文），
  // 而且哪天改回去也不至于再瞎一次。**空串要当没有**，否则 `||` 会被
  // 空的 articleBody 短路掉。
  let text = one('articleBody') || one('text');
  let author = authorEl ? authorEl.getAttribute('content') : null;
  let n_images = own('image').length +
      a.querySelectorAll('[itemtype*="ImageObject"]').length;
  let mode = 'microdata';
  if (!post_id) {                      // 登录态：React DOM
    mode = 'testid';
    const link = a.querySelector('a[href*="/status/"]');
    if (link) {
      const parts = (link.getAttribute('href') || '').split('/').filter(Boolean);
      const i = parts.indexOf('status');
      if (i > 0) { author = parts[0]; post_id = (parts[i+1] || '').split('?')[0]; }
    }
    const t = a.querySelector('time');
    published_at = t ? t.getAttribute('datetime') : null;
    const tt = a.querySelector('[data-testid="tweetText"]');
    text = tt ? tt.innerText : '';
    n_images = a.querySelectorAll('[data-testid="tweetPhoto"]').length;
  }
  return {post_id, published_at, text, author, n_images, mode,
          head: (a.innerText || '').slice(0, 40)};
})
"""

# 下滑抓取的上限：登录态下每次最多翻这么多屏去补历史。设上限是为了
# "抓不完必须显式告警"而不是无限翻——真出现超过这个量的缺口，那是人要知道的事。
MAX_SCROLLS = 25
# 首次抓某账号（无基准线）时刻意**少翻**：中继要的是"从现在起不漏"，
# 不是把人家几年的历史全搬回来。翻几屏拿到近期上下文即可。
BOOTSTRAP_MAX_SCROLLS = 3
SCROLL_WAIT_MS = 1500


class StructureError(RuntimeError):
    """页面拿不到任何微数据——X 改版或被拦，必须显式告警而不是当成"没有新帖"。"""


def _snowflake(post_id) -> int:
    """post_id 转整数用于新旧比较。X 的 id 是雪花号，**全局单调递增**——
    同一账号内 id 大 = 发得晚，这是"抓到上次那条就可以停"的判据。"""
    try:
        return int(post_id)
    except (TypeError, ValueError):
        return -1


def extract_posts(page, handle: str, rows=None) -> list:
    """把页面上的 article 归一化成帖子记录，按 post_id 去重。

    同一条帖会出现在多个 <article>（thread 分组会把父帖重复渲染），
    去重键是 post_id。`rows` 用于传入已抓取的原始行（下滑累积时复用）。"""
    rows = page.evaluate(_EXTRACT_JS) if rows is None else rows
    out, seen = [], set()
    for r in rows:
        pid = r.get("post_id")
        if not pid or pid in seen:
            continue
        # 只有 id、没有时间戳 = article 还没 hydrate 完（下滑时常见）。
        # 收下它有两重害处：① 台账里多出无正文无时间的空壳行
        # ② 若它恰好在页面顶部，会成为增量基准线，而它的内容我们从没拿到过
        #    —— 之后永远不会再抓它，真漏帖也发现不了。
        # 2026-08-07 实测：一轮下滑抓进 10 条这样的空壳。
        if not r.get("published_at"):
            continue
        seen.add(pid)
        author = r.get("author") or handle
        head = (r.get("head") or "")
        out.append({
            "post_id": pid,
            "handle": handle,
            "author": author,
            "published_at": r.get("published_at"),
            "text": r.get("text") or "",
            "has_image": bool(r.get("n_images")),
            # 未登录页无 socialContext；转发的 author 与 handle 不同即可判定
            "is_retweet": author.casefold() != handle.casefold(),
            "is_pinned": ("Pinned" in head or "置顶" in head),
            "mode": r.get("mode"),          # microdata=未登录 / testid=已登录
        })
    return out


def fetch_account(ctx, acct: Account, since_id: str | None = None) -> tuple:
    """抓一个账号，**尽量把 since_id 之后的帖子全部取回**。

    返回 `(posts, complete)`：
      · posts    —— 本次看到的全部帖子（含已见过的，去重交给 store）
      · complete —— 是否**证明**没有遗漏。判据只有一个：本次抓到的最旧一条
                    id ≤ since_id，说明新旧两次的窗口重叠上了，中间不可能有洞。

    下滑只在登录态有效（未登录页固定约 5 条、滚动不加载）。因此未登录时
    complete 常为 False——那不是 bug，是如实报告"我不能证明没漏"。
    拿不到任何 article → StructureError。"""
    page = ctx.new_page()
    try:
        page.goto(f"https://x.com/{acct.handle}", timeout=45_000,
                  wait_until="domcontentloaded")
        try:
            # state="attached" 是必须的：微数据是隐藏的 <meta>，默认的
            # "visible" 永远等不到（2026-08-06 实测把整轮抓取判成结构失效）
            page.wait_for_selector(
                'article [itemprop="identifier"], article[data-testid="tweet"]',
                state="attached", timeout=25_000)
        except Exception:                                     # noqa: BLE001
            raise StructureError(acct.handle)
        page.wait_for_timeout(1200)                           # 让剩余 article 渲染完

        since = _snowflake(since_id) if since_id else -1
        rows, seen_ids, complete = [], set(), since < 0   # 没有基准线时无需证明
        budget = MAX_SCROLLS if since >= 0 else BOOTSTRAP_MAX_SCROLLS
        last_count = -1
        for attempt in range(budget + 1):
            for r in page.evaluate(_EXTRACT_JS):
                pid = r.get("post_id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    rows.append(r)
            # 置顶帖是很旧的一条、且永远在最上面，会让"最旧 id"永远看着已覆盖 →
            # 判断重叠必须排除它，否则第一屏就假装"抓全了"
            fresh = [r for r in rows
                     if not ("Pinned" in (r.get("head") or "")
                             or "置顶" in (r.get("head") or ""))]
            if since >= 0 and fresh and min(_snowflake(r["post_id"]) for r in fresh) <= since:
                complete = True
                break
            if len(rows) == last_count:                       # 下滑不再产出新内容
                break
            if attempt == budget:                             # 预算用尽，别再翻
                break
            last_count = len(rows)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(SCROLL_WAIT_MS)

        posts = extract_posts(page, acct.handle, rows=rows)
        if not posts:
            raise StructureError(acct.handle)
        # 拿到帖子、但**全都没有正文** = 微数据字段又被改名了，不是"这些人
        # 恰好都发了纯图"。必须和"拿不到 article"同等对待，理由是**入库即
        # 不可逆**：post_id 一旦落库就永久标记为"已见过"，之后再也不会重抓，
        # 那条帖子的内容就此永久丢失。
        #
        # 2026-08-14 实测代价：X 把 `articleBody` 改名为 `text`，抓取日志一路
        # 显示成功、`已证明无遗漏` 照常打印，而连续三天 123 条帖子全部以空正文
        # 入库 —— 空正文 → 分类成噪音 → 被 PUSH_BLOCKED 拦掉 → 用户什么都收不到。
        # 现有的每一层检查都通过了，因为它们只问"有没有 article"，不问"有没有内容"。
        #
        # 阈值取 3：单条纯图帖是正常的，一整页 ≥3 条全空不可能是巧合。
        fresh = [q for q in posts if not q.get("is_pinned")]
        if len(fresh) >= 3 and not any((q.get("text") or "").strip() for q in fresh):
            raise StructureError(f"{acct.handle}（{len(fresh)} 条全部没有正文，"
                                 f"微数据字段可能又改名了）")
        return posts, complete
    finally:
        page.close()


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
    tl = getattr(cls, "ticker_levels", None) or {}
    named = {k: v for k, v in tl.items() if k != "_lead"}
    if named:
        # 标的绑定优先：「$aaoi 149/160/170　$axti 89」比一串分不清归属的数字有用
        lines.append("📍 " + "　".join(
            f"${k} {'/'.join(f'{v:,.10g}' for v in vs[:6])}"
            for k, vs in list(named.items())[:6]))
    elif cls.levels:
        lines.append("📍 " + ", ".join(f"{v:,.10g}" for v in cls.levels[:12]))
    if post.get("has_image") and not cls.levels:
        lines.append("🖼 帖内含图（点位可能在图中，文本未抽出数字）")
    lines.append(f"🔗 x.com/{post['handle']}/status/{post['post_id']}")
    return "\n".join(lines)


def should_push(post: dict, cls, now_utc: datetime) -> bool:
    if not cls.is_pushable:
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
def run_once(store: PostStore, dry_run: bool = False,
             write_hb: bool = True) -> dict:
    """跑一轮抓取。`write_hb=False` 用于人工一次性诊断（见 main 的 --once）。"""
    from playwright.sync_api import sync_playwright

    now_utc = datetime.now(timezone.utc)
    result = {"ok": [], "fail": [], "structure_fail": [], "gap": [],
              "new": 0, "pushed": 0, "logged_in": None}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=True,
            viewport={"width": 1280, "height": 2200},
            args=["--disable-blink-features=AutomationControlled"])
        try:
            for acct in ACCOUNTS:
                ts = datetime.now(timezone.utc).isoformat()
                # 增量基准线：上次抓到的最新一条。下滑抓到它就证明没漏。
                since_id = store.newest_post_id(acct.handle)
                try:
                    posts, complete = fetch_account(ctx, acct, since_id=since_id)
                except StructureError:
                    result["structure_fail"].append(acct.handle)
                    store.record_fetch(ts, acct.handle, 0, 0, False, "structure")
                    log(f"@{acct.handle}: 拿不到微数据（页面结构失效或被拦）")
                    continue
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
                    post["ticker_levels"] = cls.ticker_levels
                    # dry-run **绝不写库**：否则它会把帖子标成"已见过"，
                    # 真正的循环之后就再也不会推它们——预览把真实推送吞掉。
                    # （2026-08-07 实测踩到：一次 --dry-run 吃掉了 10 条新帖。）
                    if dry_run:
                        if store.has_post(post["post_id"]):
                            continue
                        n_new += 1
                        if should_push(post, cls, now_utc):
                            log(f"—— dry-run 应推送 ——\n{format_push(post, cls)}")
                        continue
                    if not store.insert_post(post):
                        continue                              # 已见过（含置顶重复）
                    n_new += 1
                    if should_push(post, cls, now_utc):
                        msg = format_push(post, cls)
                        if tg_send(msg):
                            store.mark_pushed(
                                post["post_id"],
                                datetime.now(timezone.utc).isoformat())
                            result["pushed"] += 1
                        else:
                            # 失败不丢：计一次尝试，留给本轮末尾的重试队列。
                            # 2026-08-08 前这里什么都不做，帖子已入库、
                            # 下轮 insert_post 返回 False 就永久跳过了。
                            store.bump_attempt(post["post_id"])
                # 缺口判定（取代早先那个"整页全新"的粗略启发式）：只有当本轮
                # 抓到的最旧一条 ≤ 上次的最新一条，两次窗口才算重叠、才**证明**
                # 没漏。证明不了就记为 gap——未登录态因为翻不动页，几乎必然如此。
                if since_id and not complete:
                    result["gap"].append(acct.handle)
                result["ok"].append(acct.handle)
                result["new"] += n_new
                if result["logged_in"] is None:
                    result["logged_in"] = any(p.get("mode") == "testid"
                                              for p in posts)
                if not dry_run:
                    store.record_fetch(ts, acct.handle, len(posts), n_new, True)
                log(f"@{acct.handle}: 看到 {len(posts)} 条，新 {n_new} 条"
                    f"{'' if not since_id else (' [已证明无遗漏]' if complete else ' [无法证明无遗漏]')}")
                time.sleep(random.uniform(*PER_ACCOUNT_DELAY))
        finally:
            ctx.close()

    # ── 重试队列：补发此前发送失败的帖 ─────────────────────────
    # 放在抓取循环之后，避免与本轮新帖的推送挤在一起再次撞限流。
    if not dry_run:
        pending = store.pending_push(
            sorted(PUSH_BLOCKED), PUSH_MAX_AGE_HOURS,
            now_utc.isoformat(), PUSH_RETRY_MAX)
        for pid, handle, pub, text, img, cls_label, lv in pending:
            post = {"post_id": pid, "handle": handle, "published_at": pub,
                    "text": text, "has_image": bool(img)}
            cls = Classification(cls_label, json.loads(lv or "[]"), [])
            if tg_send(format_push(post, cls)):
                store.mark_pushed(pid, datetime.now(timezone.utc).isoformat())
                result["pushed"] += 1
                log(f"补发成功 @{handle} {pub[:16]}")
            else:
                store.bump_attempt(pid)
            time.sleep(1.0)          # 限流是丢帖主因，补发放慢
        # 重试用尽仍未送达 = 真的丢了，必须让人知道（原来是完全静默的）
        export_for_brief(store)          # 供主仓库简报只读
        gave_up = store.give_up_count(PUSH_RETRY_MAX)
        if gave_up:
            alert_once("giveup",
                       f"⚠️ [X中继] {gave_up} 条帖重试 {PUSH_RETRY_MAX} 轮仍未推送成功，"
                       f"已放弃。用 `--stats` 查看，或检查 TG 配置/限流。",
                       STRUCTURE_ALERT_COOLDOWN)

    # 「所有账号都拿不到帖子」的告警**不在这里发**：单轮判不出"改版"还是
    # "这次慢了"，去抖需要跨轮状态，因此归 run_loop 管（见 STRUCTURE_ALERT_AFTER）。
    # 这也顺带修掉：`--once --dry-run` 本是告警文案让人跑的诊断命令，以前它自己
    # 会再发一条"抓取停摆"。一次性诊断的结论走退出码 2 和日志，不该进 Telegram。
    # 漏帖告警。**2026-08-11 去掉了 `and result["logged_in"]` 这个条件。**
    #
    # 原注释的前提是"未登录态几乎每轮都证明不了无遗漏，每轮告警等于噪音"，
    # 所以只在登录态才报。但两点都不成立：
    #   ① `logged_in` 由 `mode == "testid"` 判定，而本服务是**免登录路线**
    #      （README「免登录」一节），永远走 microdata 模式 → 该值恒为 False
    #      → 这条告警从上线起**一次都不可能触发**。
    #   ② 前提本身与实测相反：1682 次抓取**全部**证明了无遗漏（日志里
    #      `已证明无遗漏` 1682 次、`无法证明无遗漏` 0 次）。免登录窗口虽只有
    #      约 5 条，但它覆盖数小时，15 分钟轮询绰绰有余。
    #
    # 也就是说检测本身一直好用，只是被一个恒假的条件掐死了。这在只有低频
    # 账号时没暴露；2026-08-11 加入 @zerohedge（约 6 分钟一帖、可见窗口仅
    # 约 36 分钟）后，撑爆窗口第一次成为现实风险，必须让它能报出来——
    # 否则漏帖是**静默**的。证明不了就是可能漏了，不分登录与否。
    if result["gap"]:
        alert_once("gap_" + ",".join(sorted(result["gap"])),
                   "⚠️ [X中继] 这些账号未能接上上次抓到的最新一条，**可能漏帖**："
                   f"{', '.join(result['gap'])}\n"
                   "多半是发帖量暴增、超出了免登录页约 5 条的可见窗口；"
                   "若持续出现，需要缩短轮询间隔或把该账号移出名单。",
                   6 * 3600)
    # 心跳的语义是"**守护进程**还活着并在循环"，一次性诊断跑不算。写了会骗人：
    # watchdog 对"pm2 online 但进程卡死"的唯一探测手段就是心跳新鲜度
    # （health_watchdog.py::bot_health_failure「心跳过期 → 进程卡死/停更」）。
    # 而卡死时人做的第一件事，正是按告警提示跑 `--once --dry-run`——那一下就把
    # 心跳刷新了，卡死信号被抹掉，watchdog 转头就安静，人还以为"跑一下就好了"。
    # 2026-08-09 实测：pm2 stop 之后跑一次 --once，心跳文件立刻变成
    # connected=true 的新鲜记录。（进程 stopped 这一种仍能被 watchdog 的
    # pm2 status 独立抓到，被这个副作用掩盖的是"online 但卡死"那一种。）
    if write_hb:
        write_heartbeat(
            ok=bool(result["ok"]),
            detail={"accounts_ok": result["ok"], "accounts_fail": result["fail"],
                    "structure_fail": result["structure_fail"],
                    "gap": result["gap"], "logged_in": result["logged_in"],
                    "new_posts": result["new"], "pushed": result["pushed"]})
    return result


def run_loop(store: PostStore):
    log(f"进入轮询循环：每 {POLL_SECONDS}s ±{JITTER_SECONDS}s，"
        f"{len(ACCOUNTS)} 个账号，拦截类别 {sorted(PUSH_BLOCKED)}（其余全推）")
    consecutive_all_fail = 0
    consecutive_structure = 0
    while True:
        try:
            r = run_once(store)
            if r["structure_fail"] and not r["ok"]:
                consecutive_structure += 1
                consecutive_all_fail = 0                      # 归 structure 路径管
                if consecutive_structure >= STRUCTURE_ALERT_AFTER:
                    mins = consecutive_structure * POLL_SECONDS // 60
                    alert_once(
                        "structure",
                        f"⚠️ [X中继] 连续 {consecutive_structure} 轮（约 {mins} 分钟）"
                        "所有账号都拿不到帖子，抓取停摆。\n"
                        "多半是 X 改版、被反爬拦截，或登录态失效，需人工看 "
                        "`x_relay.py --once --dry-run` 的输出。",
                        STRUCTURE_ALERT_COOLDOWN)
            elif not r["ok"]:
                consecutive_structure = 0
                consecutive_all_fail += 1
                if consecutive_all_fail == ALL_FAIL_ALERT_AFTER:
                    alert_once("all_fail",
                               f"⚠️ [X中继] 连续 {consecutive_all_fail} 轮所有账号抓取失败"
                               "（非登录墙），可能是选择器失效或网络问题，需人工看日志",
                               3600)
            else:
                # 有账号抓到了 = 抓取通路是好的，哪怕个别账号仍失败。
                consecutive_structure = 0
                consecutive_all_fail = 0
                resolve_alert(
                    "structure",
                    f"✅ [X中继] 抓取已恢复：本轮 {len(r['ok'])} 个账号正常拿到帖子。")
        except Exception as e:                                # noqa: BLE001
            log(f"轮询异常（下一轮重试）: {e}")
            write_heartbeat(ok=False, detail={"error": str(e)[:300]})
        time.sleep(POLL_SECONDS + random.uniform(-JITTER_SECONDS, JITTER_SECONDS))


def do_login() -> int:
    """打开有头浏览器，**由用户本人**登录 x.com；会话存进持久化 profile。

    铁律：Claude 不代输任何凭据——密码只由用户自己敲进浏览器窗口。
    本程序全程不接收、不存储、不日志任何用户名或密码；它只是把浏览器
    开出来，登录完成后 profile 里留下的 cookie 由 Chromium 自己管理。

    登录的唯一目的：未登录页每次只给约 5 条且**下滑不加载更多**，
    做不到"补齐上次之后的全部帖子"。登录态的时间线支持无限下滑。
    代价（用户已知悉）：抓取行为绑定到真实账号，X 的 ToS 风险由此产生。"""
    from playwright.sync_api import sync_playwright
    print("即将打开浏览器窗口。请**你自己**在窗口里登录 x.com。")
    print("（本程序不接收密码；我也不会代你输入。）")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://x.com/login")
        input("登录完成后回到这里按 Enter 验证并保存会话 … ")
        # 验证：登录态的时间线是 React DOM（data-testid），未登录是降级渲染
        ok = False
        try:
            page.goto("https://x.com/AdamMancini4", timeout=45_000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            ok = page.locator('article[data-testid="tweet"]').count() > 0
        except Exception as e:                                # noqa: BLE001
            print(f"验证时出错: {e}")
        ctx.close()
    if ok:
        print(f"✅ 登录态已确认并保存到 {PROFILE_DIR}")
        print("   之后 --once / --loop 会自动下滑补齐上次之后的全部帖子。")
        return 0
    print("❌ 未检测到登录态（页面仍是未登录的降级渲染）。")
    print("   抓取仍可工作，但只能拿到每个账号最新约 5 条、无法证明不漏帖。")
    return 1


def push_latest(store: PostStore, per_account: int = 1, dry_run: bool = False,
                include_filtered: bool = False) -> int:
    """把每个账号**最新的 N 条**推一遍，绕过 12h 时效门与"已推送"标记。

    存在的理由：常规路径只推「新抓到且发布未超 12h」的帖，所以刚部署完
    群里可能长时间是空的——老帖在 bootstrap 时全被标成"已见过"，新帖又还没发。
    那不是故障，但人看不到东西，也无从确认通道真的通。本命令用于
    「现在就让我看到各家最新在说什么」。
    默认按 classifier.PUSH_BLOCKED 排除噪音类；`include_filtered=True` 连
    promo/chatter 一起推（调试分类用）。"""
    # 注意是 NOT IN（黑名单）。2026-08-08 从白名单反转时，若沿用原来的 IN，
    # 语义会变成"只推被拦的那些"——正好反了。
    blocked = None if include_filtered else sorted(PUSH_BLOCKED)
    n = 0
    for acct in ACCOUNTS:
        sql = ("SELECT post_id, handle, published_at, text, has_image, "
               "classification, levels FROM posts "
               "WHERE handle=? AND is_retweet=0 AND is_pinned=0 ")
        params = [acct.handle]
        if blocked:
            sql += "AND (classification IS NULL OR classification NOT IN (%s)) " % ",".join("?" * len(blocked))
            params += blocked
        sql += "ORDER BY published_at DESC LIMIT ?"
        params.append(per_account)
        for pid, handle, pub, text, img, cls_label, lv in store.conn.execute(sql, params):
            post = {"post_id": pid, "handle": handle, "published_at": pub,
                    "text": text, "has_image": bool(img)}
            cls = Classification(cls_label, json.loads(lv or "[]"), [])
            msg = format_push(post, cls)
            if dry_run:
                log(f"—— dry-run 应推送 ——\n{msg}")
                n += 1
            elif tg_send(msg):
                store.mark_pushed(pid, datetime.now(timezone.utc).isoformat())
                n += 1
                time.sleep(1.0)                               # 避开 TG 频率限制
    return n


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
    ap.add_argument("--login", action="store_true",
                    help="打开浏览器由**你自己**登录 x.com（解锁下滑补齐历史）")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push-latest", type=int, metavar="N", default=0,
                    help="把每个账号最新 N 条推一遍（绕过 12h 时效门与已推送标记）")
    ap.add_argument("--include-filtered", action="store_true",
                    help="--push-latest 时连个股/其他一并推（调试分类用）")
    args = ap.parse_args()

    if args.login:
        return do_login()
    store = PostStore(DB_PATH)
    try:
        if args.push_latest:
            n = push_latest(store, args.push_latest, args.dry_run,
                            args.include_filtered)
            log(f"最新帖推送完成：{n} 条")
        elif args.stats:
            print_stats(store)
        elif args.once:
            # write_hb=False：一次性诊断绝不冒充守护进程的心跳，见 run_once 末尾。
            r = run_once(store, dry_run=args.dry_run, write_hb=False)
            log(f"完成：ok={r['ok']} fail={r['fail']} "
                f"结构失效={r['structure_fail']} 新帖 {r['new']} 推送 {r['pushed']}")
            return 0 if r["ok"] else 2
        elif args.loop:
            run_loop(store)
        else:
            ap.print_help()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
