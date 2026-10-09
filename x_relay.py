"""X 博主点位中继（trading_x_relay 独立小服务）。

定位（quantrift_index_future/strategy_explore.md §A，2026-08-02 定稿；08-06 用户拍板施工）：
**信息中继 + 决策支持，不是 alpha**。抓取指定博主的 X 帖子 → 启发式分类
（指数分析 / 个股 / 其他）→ 全量落库（append-only）→ 只把指数类推送 Telegram。

**免登录**（2026-08-06 实测确定的路线）：X 未登录时 profile 页是降级渲染，
但它带 **schema.org 微数据**（`itemprop=identifier/datePublished/articleBody/
author/ImageObject`）——比登录后的 React DOM（`data-testid`）更干净也更稳定，
因为那是给搜索引擎看的结构化数据。五个账号实测均可拿到最近 1-2 天的帖子。
好处：无凭据、无账号封禁风险、无 session 过期、无 Google/X 登录反自动化拦截。
代价：**每账号每轮只能看到约 5 条最新帖，且不能下滑加载更多**——靠按账号
发帖率自适应的轮询覆盖（2026-10-08 前是一律 15 分钟）；`saturation` 检测负责在可能漏帖时显式告警（见 run_once）。

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
  venv/bin/python3.11 x_relay.py --loop           # 常驻轮询（pm2 用这个，按账号自适应间隔）
  venv/bin/python3.11 x_relay.py --stats          # 台账分布（含有图无数字占比）
"""
from __future__ import annotations

import argparse
import html
import json
import re
import os
import random
import sys
import tempfile
import time
import urllib.error
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

# 改前的固定轮询周期。2026-10-08 起轮询改为按账号自适应（见下方
# PollScheduler），它仍是：失败重试上限、推送重试队列节奏、Mancini 的间隔上限。
POLL_SECONDS = 900
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

# ── 自适应轮询（2026-10-08）───────────────────────────────────
# 免登录页每账号只有约 4 个非置顶帖位，而 8 个账号的发帖率差了几十倍
# （21 天实测：willem 0.01 帖/小时 vs zerohedge 盘中 7.6 帖/小时）。一律 15 分钟
# 一轮 = 低频账号白抓、高频账号漏帖（mentoviax 21 次、zerohedge 95 次 gap）。
# 改为按各账号的**预期发帖率**定间隔：预计新帖填到 FILL_TARGET 个帖位就抓。
# 上限仍是 15 分钟——**任何账号都不比改前慢**：低频账号（Dayu、novicetrader888
# 等）恰恰是质量最高的，用户明确不接受为省请求把它们放长（2026-10-08）。
# 所以这是"给高频账号加抓"，总请求量由 HOURLY_FETCH_BUDGET 封顶。
# 参数由历史回放定出，见 README。
POLL_MIN_SECONDS = 300             # 再短就是在给 X 的限流送样本
POLL_MAX_SECONDS = POLL_SECONDS    # = 改前的固定周期，只许缩短不许放长
# 帖位共约 4 个，目标却只填 0.75 个：发帖是成簇的（mentoviax 均值 3.4 帖/小时，
# 但 10 分钟能连发 6 帖），按均值填一半帖位照样撑爆。回放 09-24..10-08 实测
#（上限 15min、预算 40）：1.5 → 16 gap / 估漏 2；0.75 → 3 / 0、均 34.3 次/小时；
# 0.4 → 2 / 0 但 36.5 次/小时——差别在噪声内，取请求更少的 0.75。
FILL_TARGET = 0.75
RATE_RECENT_HOURS = 2              # 近期发帖率的回看窗口
RATE_HIST_DAYS = 14                # 同时段历史发帖率的回看天数
# 滚动 1 小时抓取上限。改前 8 × 4 = 32，而低频账号的 15 分钟必抓就占满 32，
# 卡在 32 等于高频账号没有加抓余地（回放 24 gap / 估漏 4，等于没修）。
# 40 留出加抓空间；实际均值 34.3 次/小时（+7%），见 FILL_TARGET。
HOURLY_FETCH_BUDGET = 40


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


def tg_send(text: str, plain_fallback: str | None = None) -> bool:
    """发一条 TG 消息。给了 `plain_fallback` 就按 HTML 解析发 `text`。

    HTML 被 Telegram 拒收（400：标签不合法/转义漏了）时**立刻改发纯文本**，
    不进重试队列——格式错误重试多少次都一样错，那条帖子会被耗到"已放弃"。
    网络/限流类失败仍返回 False，交给重试队列。"""
    token, chat = resolve_tg()
    if not token:
        log("TG 未配置（缺 TG_TOKEN/TG_CHAT_ID），本条只落库不推送")
        return False
    fields = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if plain_fallback is not None:
        fields["parse_mode"] = "HTML"
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 400 and plain_fallback is not None:
            log(f"TG 拒收 HTML（{e}），改发纯文本")
            return tg_send(plain_fallback)
        log(f"TG 发送失败: {e}")
        return False
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


# ══════════════════════════════════════════════════════════════════════
# RSC 路线（2026-08-19 起的唯一抓取路径）
# ══════════════════════════════════════════════════════════════════════
# X 从 2026-08-19 开始拦 Playwright/headless Chromium 的浏览器指纹：
# **全新 profile 也一律 HTTP 403、正文全空**，所以不是 IP 被封、也不是
# profile 被标记，是浏览器这条路整条不通了。而同一台机器上普通 HTTP GET
# 仍然 200（Python 默认 UA 就够，不需要伪装成 curl）。
#
# curl/urllib 拿到的 HTML **没有 schema.org 微数据**——那是浏览器执行 JS
# 之后才有的，所以旧的 `_EXTRACT_JS` 那条路在这里用不上。但服务端直接把一份
# React-Server-Components 的扁平化缓存渲染进了 HTML，帖子字段都在里面：
#
#   "client:VHdlZXQ6<b64>:details" : {... created_at_ms:<ms> ... full_text:"<正文>" ...}
#   "client:VHdlZXQ6<b64>:core"    : {... user_results:{__ref:"VXNlclJlc3VsdHM6<b64uid>"} ...}
#   "client:VXNlcjo<b64uid>:core"  : {... screen_name:"<作者>" ...}
#   "client:VHdlZXQ6<b64>:media_entities2:0" : {... media_url_https:"..." ...}
#   pinned_entry_ids:[...]=["tweet-<id>"]
#
# `VHdlZXQ6<b64>` 解码是 `Tweet:<post_id>`，`VXNlcjo<b64>` 解码是 `User:<uid>`。
# 这条路**比微数据更稳**：它是 X 自己前端要用的数据，不是给爬虫看的附属品。

X_PROFILE_TIMEOUT = 20


def fetch_profile_html(handle: str, timeout: int = X_PROFILE_TIMEOUT) -> str:
    """取 profile 页 HTML。不开浏览器、不带 cookie、不伪装 UA。"""
    import urllib.request
    req = urllib.request.Request(f"https://x.com/{handle}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if not (200 <= resp.status < 300):
            raise StructureError(f"{handle}（HTTP {resp.status}）")
        return resp.read().decode("utf-8", "replace")


def _b64_tail(token: str) -> str:
    """解 RSC 的 base64 键，返回冒号后那半（`Tweet:123` → `123`）。"""
    import base64
    try:
        d = base64.b64decode(token + "==").decode("utf-8", "replace")
    except Exception:                                         # noqa: BLE001
        return ""
    return d.split(":", 1)[1] if ":" in d else ""

# ── RSC 缓存块的边界 ────────────────────────────────────────────────────
# 2026-08-27 12:05 ET 起八个账号同时"拿不到帖子"。抓取本身没坏（HTTP 200、
# 226-270 KB、`:details` 键 18 个都在），坏的是**取块的边界**：原来三条正则
# 都用 `\{(.{0,N}?)\}` 拿块正文，非贪婪 `?` 会停在**第一个** `}`——这等于
# 假设"我要的字段之前不会出现嵌套花括号"。当天 X 在 `full_text` 和
# `created_at_ms` 之间插进了 `hashtag_entities`/`cashtag_entities`/
# `smarttags`（每个都是 `={__refs:...}`），第一个 `}` 就落在 full_text 之后、
# created_at_ms 之前 ⇒ 六个块全部只截到一半，`if not ts or not tx: continue`
# 全数跳过 ⇒ 0 帖 ⇒ 结构失效告警。**字段值一个都没变，只是换了顺序。**
#
# 换成配平花括号扫描（并跳过 JSON 字符串，正文里本来就可能有 `{` `}`）。
# 这样字段怎么重排、中间插多少嵌套对象都无所谓——**这是"再改也不会重演"
# 与"下次还得再修一遍"的分界**。
#
# ⚠️ 试过但否决的写法：以 `,"client:` 作为下一条缓存记录的分隔符来切块。
# 看着干净，实际会误切——非空的 `__refs` 列表内部就长这样：
#     __refs:$R[76]=["client:...instructions:0","client:...instructions:1"]
# 带两个以上 hashtag 的帖子恰好会命中，然后**静默丢帖**。
_RSC_BLOCK_LIMIT = 200_000


def _rsc_block_end(html: str, start: int, limit: int = _RSC_BLOCK_LIMIT) -> int:
    """`start` 位于开花括号之后；返回与之配对的 `}` 的下标，找不到返回 -1。"""
    depth, i, n = 1, start, min(len(html), start + limit)
    while i < n:
        c = html[i]
        if c == '"':                      # 整个 JSON 字符串原样跳过
            i += 1
            while i < n:
                if html[i] == "\\":
                    i += 2
                    continue
                if html[i] == '"':
                    break
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _rsc_entries(html: str, prefix: str, suffix: str):
    """遍历 `"client:<prefix><b64>:<suffix>":$R[n]={...}`，产出 (b64, 块正文)。

    只认**赋值**形式（`={`）；`__ref:"client:...:details"` 那种引用不会命中。
    """
    import re as _re
    pat = _re.compile(r'"client:(' + prefix + r'[A-Za-z0-9+/=]+):'
                      + suffix + r'"\s*:\s*\$R\[\d+\]\s*=\s*\{')
    for m in pat.finditer(html):
        end = _rsc_block_end(html, m.end())
        if end < 0:                       # 截断的 HTML：宁可少一条，不给半条
            continue
        yield m.group(1), html[m.end():end]


def parse_rsc_posts(html: str, handle: str) -> list:
    """从 RSC 缓存里抽帖子。产出与 `extract_posts` **完全同形**的记录。"""
    import re as _re
    # uid → screen_name
    authors = {}
    for token, body in _rsc_entries(html, "VXNlcjo", "core"):
        uid = _b64_tail(token)
        sn = _re.search(r'screen_name:"([^"]{1,30})"', body)
        if uid and sn:
            authors[uid] = sn.group(1)
    # tweet → uid
    tweet_author = {}
    for token, body in _rsc_entries(html, "VHdlZXQ6", "core"):
        tid = _b64_tail(token)
        # ⚠️ `VXNlclJlc3VsdHM6...` 是**一整串 base64**（解出来是
        # `UserResults:<uid>`），不是"前缀 + 后缀"。按文本切开再解码得到的是
        # 垃圾——第一版就这么写的，结果作者永远解析不出、`is_retweet` 恒为
        # False，他人帖会被当成账号自己的帖推出去。
        ref = _re.search(r'__ref:"(VXNlclJlc3VsdHM6[A-Za-z0-9+/=]+)"', body)
        if tid and ref:
            tweet_author[tid] = authors.get(_b64_tail(ref.group(1)), "")
    # 置顶
    pinned = set()
    pm = _re.search(r'pinned_entry_ids:(?:\$R\[\d+\])?=?\[([^\]]*)\]', html)
    if pm:
        pinned = set(_re.findall(r'tweet-(\d+)', pm.group(1)))
    # 带图的 tweet
    with_media = {_b64_tail(t) for t in
                  _re.findall(r'"client:(VHdlZXQ6[A-Za-z0-9+/=]+):media_entities2', html)}

    out, seen = [], set()
    for token, body in _rsc_entries(html, "VHdlZXQ6", "details"):
        pid = _b64_tail(token)
        if not pid.isdigit() or pid in seen:
            continue
        ts = _re.search(r'created_at_ms:(\d{10,16})', body)
        tx = _re.search(r'full_text:"((?:[^"\\]|\\.)*)"', body)
        if not ts or not tx:
            continue
        seen.add(pid)
        try:
            text = json.loads('"' + tx.group(1) + '"')
        except Exception:                                     # noqa: BLE001
            text = tx.group(1)
        published = datetime.fromtimestamp(
            int(ts.group(1)) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        author = tweet_author.get(pid) or handle
        out.append({
            "post_id": pid,
            "handle": handle,
            "author": author,
            "published_at": published,
            "text": text,
            "has_image": pid in with_media,
            # 转发/他人帖：作者与账号不符（大小写不敏感，X 的 screen_name
            # 大小写与我们配置里写的常常不同）
            "is_retweet": author.casefold() != handle.casefold(),
            "is_pinned": pid in pinned,
            "mode": "rsc",
        })
    out.sort(key=lambda r: _snowflake(r["post_id"]), reverse=True)
    return out


# ── 2026-09-24 第三次改版：帖子不再放 `client:<b64>:details` 缓存键，改进 GraphQL 流 ──
# 22:01 ET 还正常，22:16 起八个账号同时 0 帖。HTTP 200、体积没变，`:details` 键从 18 个变成
# 0 个；同样的字段（`created_at_ms` / `full_text` / `screen_name` / `media_entities2`）搬进了
# `GraphQLRequestStream` 的 timeline entries：
#     $R[n]={content:{__typename:"TimelineTimelineItem",
#                     client_event_info:{component:"organic_tweets"|"pinned_tweets"},
#                     content:{tweet_results:{rest_id:"<id>",result:{core:{user_results:{result:{core:{screen_name}}}},
#                                                                  details:{created_at_ms,full_text},media_entities2:[...]}}}},
#            entry_id:"tweet-<id>",sort_index:"..."}
# 三点观察决定了写法：
#   · result 内键是**字母序**的：core < details < media_entities2 < quoted_status_result，
#     所以块内第一个 screen_name / created_at_ms / full_text 一定是外层帖子的，不是被引用帖的；
#     媒体判定要先把 `quoted_status_result` 之后截掉，否则纯文字帖引用了带图帖会误判有图
#   · 置顶不再有 `pinned_entry_ids`，改看 entry 的 `client_event_info.component == "pinned_tweets"`
#   · 这条 timeline 叫 `profile_user_originals_timeline`——**只有本人原创**，转发不在里面；
#     `is_retweet` 仍按作者 ≠ 账号判，保持与旧路线同形
# 两条路线都保留：先试旧的 `parse_rsc_posts`，0 帖再试这条。X 来回切时不用再改代码。
def parse_graphql_posts(html: str, handle: str) -> list:
    """从 GraphQL timeline entries 抽帖子。产出与 `parse_rsc_posts` **完全同形**的记录。"""
    import re as _re
    out, seen = [], set()
    for m in _re.finditer(r'client_event_info:\$R\[\d+\]=\{component:"(pinned_tweets|organic_tweets)"', html):
        tr = _re.compile(r'tweet_results:\$R\[\d+\]=\{').search(html, m.end())
        if not tr:
            continue
        end = _rsc_block_end(html, tr.end())
        if end < 0:
            continue
        body = html[tr.end():end]
        pid_m = _re.search(r'rest_id:"(\d+)"', body)
        if not pid_m or pid_m.group(1) in seen:
            continue
        pid = pid_m.group(1)
        ts = _re.search(r'created_at_ms:(\d{10,16})', body)
        tx = _re.search(r'full_text:"((?:[^"\\]|\\.)*)"', body)
        if not ts or not tx:
            continue
        seen.add(pid)
        sn = _re.search(r'screen_name:"([^"]{1,30})"', body)
        author = sn.group(1) if sn else handle
        own = body.split("quoted_status_result", 1)[0]
        has_image = bool(_re.search(r'media_entities2:\$R\[\d+\]=\[\$R', own))
        try:
            text = json.loads('"' + tx.group(1) + '"')
        except Exception:                                     # noqa: BLE001
            text = tx.group(1)
        out.append({
            "post_id": pid,
            "handle": handle,
            "author": author,
            "published_at": datetime.fromtimestamp(
                int(ts.group(1)) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "text": text,
            "has_image": has_image,
            "is_retweet": author.casefold() != handle.casefold(),
            "is_pinned": m.group(1) == "pinned_tweets",
            "mode": "graphql",
        })
    out.sort(key=lambda r: _snowflake(r["post_id"]), reverse=True)
    return out


def fetch_account(acct: Account, since_id: str | None = None) -> tuple:
    """抓一个账号。返回 `(posts, complete)`。

    2026-08-19 起走 RSC 路线（普通 HTTP GET + 解析服务端渲染的前端缓存），
    不再开浏览器——X 已经拦死 Playwright 指纹，全新 profile 也是 403。
    见 `parse_rsc_posts` 上方那段说明。

      · posts    —— 本次看到的全部帖子（含已见过的，去重交给 store）
      · complete —— 是否**证明**没有遗漏。判据只有一个：本次抓到的最旧一条
                    （排除置顶）id ≤ since_id，说明新旧两次窗口重叠上了。

    每页仍然只有约 5 条、没有翻页，所以 complete 靠轮询间隔够短、窗口重叠
    来保证；证明不了就如实返回 False，由调用方告警。
    """
    html = fetch_profile_html(acct.handle)
    posts = parse_rsc_posts(html, acct.handle) or parse_graphql_posts(html, acct.handle)
    if not posts:
        raise StructureError(acct.handle)

    # 拿到帖子、但**全都没有正文** = 字段又被改名了，不是"这些人恰好都发了纯图"。
    # 必须和"拿不到帖子"同等对待，理由是**入库即不可逆**：post_id 一旦落库就
    # 永久标记为"已见过"，之后再也不会重抓，那条帖子的内容就此永久丢失。
    #
    # 2026-08-14 实测代价：X 把 `articleBody` 改名为 `text`，抓取日志一路显示
    # 成功、`已证明无遗漏` 照常打印，而连续三天 123 条帖子全部以空正文入库 ——
    # 空正文 → 分类成噪音 → 被 PUSH_BLOCKED 拦掉 → 用户什么都收不到。
    # 当时每一层检查都通过了，因为它们只问"有没有记录"，不问"有没有内容"。
    #
    # 阈值取 3：单条纯图帖是正常的，一整页 ≥3 条全空不可能是巧合。
    fresh = [q for q in posts if not q.get("is_pinned")]
    if len(fresh) >= 3 and not any((q.get("text") or "").strip() for q in fresh):
        raise StructureError(f"{acct.handle}（{len(fresh)} 条全部没有正文，"
                             f"字段可能又改名了）")

    # 置顶帖是很旧的一条、且永远在最上面，会让"最旧 id"永远看着已覆盖 →
    # 判断重叠必须排除它，否则第一屏就假装"抓全了"。
    complete = True
    if since_id and fresh:
        complete = min(_snowflake(q["post_id"]) for q in fresh) <= _snowflake(since_id)
    return posts, complete


# ── 推送格式 ────────────────────────────────────────────────
CLASS_CN = {"index_levels": "指数点位", "index_view": "指数观点",
            "stock": "个股", "commodity": "商品", "crypto": "加密",
            "macro": "宏观", "other": "其他"}


# 2026-10-08 起推送用 Telegram HTML 排版（账号加粗、正文进引用块、长帖折叠、
# 原帖链接收成一个"原帖 ↗"）。旧的纯文本格式保留为 format_push_plain，
# 仅在 Telegram 拒收 HTML 时兜底，以及 dry-run 日志里用。
_TCO = re.compile(r"\s*https?://t\.co/\S+")
FOLD_CHARS = 300                   # 正文超过这么长就折叠，免得一条占满一屏
BODY_MAX_CHARS = 2000              # Telegram 单条上限 4096，留足余量


def _when(post: dict) -> str:
    if post.get("published_at"):
        try:
            dt = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
            return f"{dt.astimezone(ET):%m-%d %H:%M} ET"
        except ValueError:
            pass
    return ""


def _body(post: dict) -> str:
    """正文：还原 X 的 HTML 实体、去掉 t.co 短链。

    `&` 在微数据/缓存里是编码过的（`S&amp;P 500`）——分类器读的时候会还原，
    推送这边一直没还原，2026-10-08 前用户看到的就是字面的 `S&amp;P`。
    t.co 只是图片/外链的跳转地址，原帖链接另有一行，留着纯属噪音。"""
    text = _TCO.sub("", html.unescape(post.get("text") or ""))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fmt_level(v: float) -> str:
    return f"{v:,.10g}"


def _level_parts(cls) -> tuple:
    """返回 [(标的或 None, [点位串])]：标的绑定优先，否则一组扁平点位。"""
    tl = getattr(cls, "ticker_levels", None) or {}
    named = {k: v for k, v in tl.items() if k != "_lead"}
    if named:
        # 标的绑定优先：「$aaoi 149/160/170　$axti 89」比一串分不清归属的数字有用
        return [(k, [_fmt_level(v) for v in vs[:6]]) for k, vs in list(named.items())[:6]]
    if cls.levels:
        return [(None, [_fmt_level(v) for v in cls.levels[:12]])]
    return []


def format_push(post: dict, cls) -> str:
    """推送正文（Telegram HTML）。所有来自帖子的文字一律 html.escape。"""
    esc = lambda t: html.escape(t, quote=False)               # noqa: E731
    handle = post["handle"]
    url = f"https://x.com/{handle}/status/{post['post_id']}"
    # 账号名做成指向 X 主页的链接：裸写 @handle 会被 Telegram 当成**TG 用户名**
    # 自动加链接，点过去是一个毫不相干的 Telegram 账号。
    lines = [f'<b><a href="https://x.com/{handle}">@{esc(handle)}</a></b>'
             f"  #{esc(CLASS_CN.get(cls.label, cls.label))}"]
    when = _when(post)
    lines.append((f"{when} · " if when else "") + f'<a href="{url}">原帖 ↗</a>')
    body = _body(post)
    if body:
        if len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS] + "…"
        tag = "blockquote expandable" if len(body) > FOLD_CHARS else "blockquote"
        lines.append(f"<{tag}>{esc(body)}</blockquote>")
    parts = _level_parts(cls)
    if parts:
        lines.append("📍 " + "　".join(
            (f"<b>${esc(k)}</b> " if k else "") + f"<code>{'/'.join(vs) if k else ', '.join(vs)}</code>"
            for k, vs in parts))
    if post.get("has_image") and not cls.levels:
        lines.append("🖼 帖内含图（点位可能在图中，文本未抽出数字）")
    return "\n".join(lines)


def format_push_plain(post: dict, cls) -> str:
    """纯文本版（HTML 被拒收时的兜底、dry-run 日志）。与 format_push 同内容。"""
    when = _when(post)
    lines = [f"📡 [X·{CLASS_CN.get(cls.label, cls.label)}] @{post['handle']}"
             + (f" · {when}" if when else "")]
    body = _body(post)
    if body:
        lines.append(body[:900] + ("…" if len(body) > 900 else ""))
    parts = _level_parts(cls)
    if parts:
        lines.append("📍 " + "　".join(
            (f"${k} {'/'.join(vs)}" if k else ", ".join(vs)) for k, vs in parts))
    if post.get("has_image") and not cls.levels:
        lines.append("🖼 帖内含图（点位可能在图中，文本未抽出数字）")
    lines.append(f"🔗 x.com/{post['handle']}/status/{post['post_id']}")
    return "\n".join(lines)


def push_post(post: dict, cls) -> bool:
    return tg_send(format_push(post, cls), plain_fallback=format_push_plain(post, cls))


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
_last_retry_at = 0.0               # 推送重试队列上次运行的时刻（epoch 秒）


def run_once(store: PostStore, dry_run: bool = False,
             write_hb: bool = True, accounts: list | None = None) -> dict:
    """跑一轮抓取。`write_hb=False` 用于人工一次性诊断（见 main 的 --once）。

    `accounts` 缺省为整个名单（`--once`）；守护进程只传本拍到期的账号
    （见 PollScheduler，2026-10-08 起按账号自适应间隔）。

    2026-08-19 起**不再开浏览器**：X 拦死了 Playwright 指纹（全新 profile 也
    403），改走 RSC 路线的普通 HTTP GET。顺带消掉了一整类问题——不需要
    persistent context、不需要 profile 目录、不会有 Chromium 进程残留。
    """
    now_utc = datetime.now(timezone.utc)
    result = {"ok": [], "fail": [], "structure_fail": [], "gap": [],
              "new": 0, "pushed": 0, "logged_in": None}
    for acct in (ACCOUNTS if accounts is None else accounts):
        ts = datetime.now(timezone.utc).isoformat()
        # 增量基准线：上次抓到的最新一条。下滑抓到它就证明没漏。
        since_id = store.newest_post_id(acct.handle)
        try:
            posts, complete = fetch_account(acct, since_id=since_id)
        except StructureError:
            result["structure_fail"].append(acct.handle)
            store.record_fetch(ts, acct.handle, 0, 0, False, "structure")
            log(f"@{acct.handle}: 拿不到帖子（页面结构失效或被拦）")
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
                    log(f"—— dry-run 应推送 ——\n{format_push_plain(post, cls)}")
                continue
            if not store.insert_post(post):
                continue                              # 已见过（含置顶重复）
            n_new += 1
            if should_push(post, cls, now_utc):
                if push_post(post, cls):
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

    # ── 重试队列：补发此前发送失败的帖 ─────────────────────────
    # 放在抓取循环之后，避免与本轮新帖的推送挤在一起再次撞限流。
    # 按时间节流到每 POLL_SECONDS 一次：自适应轮询后一拍可能只隔一两分钟，
    # 若每拍都重试，PUSH_RETRY_MAX=5 次会在几分钟内耗尽——TG 短暂故障就会
    # 变成"已放弃"。保持改前约 75 分钟的重试跨度。
    global _last_retry_at
    if not dry_run and time.time() - _last_retry_at >= POLL_SECONDS:
        _last_retry_at = time.time()
        pending = store.pending_push(
            sorted(PUSH_BLOCKED), PUSH_MAX_AGE_HOURS,
            now_utc.isoformat(), PUSH_RETRY_MAX)
        for pid, handle, pub, text, img, cls_label, lv in pending:
            post = {"post_id": pid, "handle": handle, "published_at": pub,
                    "text": text, "has_image": bool(img)}
            cls = Classification(cls_label, json.loads(lv or "[]"), [])
            if push_post(post, cls):
                store.mark_pushed(pid, datetime.now(timezone.utc).isoformat())
                result["pushed"] += 1
                log(f"补发成功 @{handle} {pub[:16]}")
            else:
                store.bump_attempt(pid)
            time.sleep(1.0)          # 限流是丢帖主因，补发放慢
    if not dry_run:
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
                   "多半是发帖量暴增、超出了免登录页约 5 条的可见窗口。"
                   "自适应轮询会把它下次的间隔压到最短；若仍持续出现，"
                   "考虑调低 FILL_TARGET 或把该账号移出名单。",
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


def expected_rate(times: list, now: datetime) -> float:
    """该账号此刻的预期发帖率（帖/小时）。`times` 为升序的发布时间（aware）。

    取两者较大值：
      · 近 RATE_RECENT_HOURS 小时的实际速率 —— 跟上当天的突发；
      · 过去 RATE_HIST_DAYS 天里**同类日子、同一时刻往后 1 小时**的平均速率
        —— 只看近期会在开盘那一刻落后：隔夜 1 帖/小时的账号 9:30 突然 4 帖/小时，
        回看窗口要一个多小时才追上，而那正是撑爆窗口的时段。历史同时段能提前
        预见"马上要开盘了"。工作日与周末分开算（周末发帖量约为工作日一半）。
    """
    import bisect
    from datetime import timedelta

    def count(a, b):
        return bisect.bisect_right(times, b) - bisect.bisect_left(times, a)

    recent = count(now - timedelta(hours=RATE_RECENT_HOURS), now) / RATE_RECENT_HOURS
    weekend = now.astimezone(ET).weekday() >= 5
    hist, days = 0, 0
    for d in range(1, RATE_HIST_DAYS + 1):
        t = now - timedelta(days=d)
        if (t.astimezone(ET).weekday() >= 5) != weekend:
            continue
        hist += count(t, t + timedelta(hours=1))
        days += 1
    return max(recent, hist / days if days else 0.0)


def poll_interval(times: list, now: datetime) -> int:
    """下次抓这个账号前该等多少秒：预计新帖填到 FILL_TARGET 个帖位的时间。"""
    rate = expected_rate(times, now)
    if rate <= 0:
        return POLL_MAX_SECONDS
    return int(min(max(FILL_TARGET / rate * 3600, POLL_MIN_SECONDS), POLL_MAX_SECONDS))


class PollScheduler:
    """决定每一拍抓哪些账号。纯逻辑、时间由调用方传入（epoch 秒），便于回放与单测。

    预算（滚动 1 小时 HOURLY_FETCH_BUDGET 次）只压**加抓**：距上次抓取已满
    POLL_MAX_SECONDS 的账号**必抓**，不受预算限制——这保证任何账号都不比改前
    （15 分钟一轮）慢，也保证高频账号不能把低频账号饿死。预算只决定高频账号
    在两次必抓之间能额外抓几次（间隔短的优先）。
    """

    def __init__(self, accounts: list, now: float, seed=None):
        self.accounts = list(accounts)
        self.next_due = {a.handle: now for a in self.accounts}   # 启动时全部到期
        self.last_fetch = {a.handle: None for a in self.accounts}
        self.interval = {a.handle: POLL_MAX_SECONDS for a in self.accounts}
        self.history = []                                       # 本小时内的抓取时刻
        self._rnd = random.Random(seed)

    def due(self, now: float) -> list:
        self.history = [t for t in self.history if now - t < 3600]
        ready = [a for a in self.accounts if self.next_due[a.handle] <= now]

        def overdue(a):
            last = self.last_fetch[a.handle]
            return last is None or now - last >= POLL_MAX_SECONDS

        must = [a for a in ready if overdue(a)]
        rest = sorted((a for a in ready if not overdue(a)),
                      key=lambda a: self.interval[a.handle])
        room = max(HOURLY_FETCH_BUDGET - len(self.history) - len(must), 0)
        chosen = {a.handle for a in must + rest[:room]}
        return [a for a in self.accounts if a.handle in chosen]   # 保持名单顺序

    def record(self, handle: str, now: float, interval: int):
        """抓完一次后登记，并排下次。±10% 抖动，避免完全规律的访问节奏。"""
        self.history.append(now)
        self.last_fetch[handle] = now
        self.interval[handle] = interval
        self.next_due[handle] = now + interval * self._rnd.uniform(0.9, 1.1)

    def seconds_until_next(self, now: float) -> float:
        """到下一拍要睡多久。有账号因预算被顺延时（next_due 已过）每分钟复查一次。"""
        return max(min(self.next_due.values()) - now, 60.0)


def _next_interval(store: PostStore, acct: Account, now_utc: datetime,
                   outcome: str) -> int:
    """outcome: ok / gap / fail。gap 说明窗口刚被撑爆，立刻按最短间隔追。
    失败的账号照常按发帖率排期：上限本就是 15 分钟，停摆告警不会被拖慢。"""
    if outcome == "gap":
        return POLL_MIN_SECONDS
    from datetime import timedelta
    since = (now_utc - timedelta(days=RATE_HIST_DAYS + 1)).isoformat()
    return poll_interval(store.post_times(acct.handle, since), now_utc)


def run_loop(store: PostStore):
    """常驻轮询。2026-10-08 起每个账号按自己的发帖率定间隔（PollScheduler），
    不再"每 15 分钟扫一遍全部账号"。

    于是"轮"不再存在，跨轮去抖改为按**账号**计：每个账号记连续失败次数，
    只有**每个**账号都至少连续失败 N 次才算抓取停摆（N 同改前：结构失效 2、
    其他失败 3）。每个账号最迟 15 分钟抓一次，所以停摆报出的时间与改前相同。
    """
    log(f"进入轮询循环：自适应间隔 {POLL_MIN_SECONDS}-{POLL_MAX_SECONDS}s，"
        f"预算 {HOURLY_FETCH_BUDGET} 次/小时，{len(ACCOUNTS)} 个账号，"
        f"拦截类别 {sorted(PUSH_BLOCKED)}（其余全推）")
    sched = PollScheduler(ACCOUNTS, time.time())
    streak = {a.handle: 0 for a in ACCOUNTS}          # 连续失败次数
    first_fail = {}                                   # 本段连续失败的起点
    last_kind = {}                                    # structure / fail
    all_fail_alerted = False
    while True:
        due = sched.due(time.time())
        if due:
            try:
                r = run_once(store, accounts=due)
            except Exception as e:                        # noqa: BLE001
                # 整拍异常（库、导出等）：这批账号按失败处理、照常排期，
                # 否则它们一直"到期"，每分钟重抓一次，等于在给 X 刷请求。
                log(f"轮询异常（稍后重试）: {e}")
                write_heartbeat(ok=False, detail={"error": str(e)[:300]})
                r = {"ok": [], "fail": [a.handle for a in due],
                     "structure_fail": [], "gap": []}
            now = time.time()
            now_utc = datetime.now(timezone.utc)
            plan = []
            for acct in due:
                h = acct.handle
                if h in r["ok"]:
                    streak[h] = 0
                    first_fail.pop(h, None)
                    outcome = "gap" if h in r["gap"] else "ok"
                else:
                    streak[h] += 1
                    first_fail.setdefault(h, now)
                    last_kind[h] = ("structure" if h in r["structure_fail"]
                                    else "fail")
                    outcome = "fail"
                try:
                    iv = _next_interval(store, acct, now_utc, outcome)
                except Exception as e:                    # noqa: BLE001
                    log(f"@{h}: 估算间隔失败，按 {POLL_SECONDS}s 排期: {e}")
                    iv = POLL_SECONDS
                sched.record(h, now, iv)
                plan.append(f"@{h} {iv // 60}m")
            log("下次间隔：" + "，".join(plan))

            if r["ok"]:
                # 有账号抓到了 = 抓取通路是好的，哪怕个别账号仍失败。
                all_fail_alerted = False
                resolve_alert(
                    "structure",
                    f"✅ [X中继] 抓取已恢复：本拍 {len(r['ok'])} 个账号正常拿到帖子。")
            elif all(streak.values()):
                least = min(streak.values())
                mins = int(now - min(first_fail.values())) // 60
                if "structure" in last_kind.values():
                    if least >= STRUCTURE_ALERT_AFTER:
                        alert_once(
                            "structure",
                            f"⚠️ [X中继] 所有账号连续至少 {least} 次（约 {mins} 分钟）"
                            "都拿不到帖子，抓取停摆。\n"
                            "多半是 X 改版、被反爬拦截，或登录态失效，需人工看 "
                            "`x_relay.py --once --dry-run` 的输出。",
                            STRUCTURE_ALERT_COOLDOWN)
                elif least >= ALL_FAIL_ALERT_AFTER and not all_fail_alerted:
                    all_fail_alerted = True
                    alert_once("all_fail",
                               f"⚠️ [X中继] 所有账号连续至少 {least} 次抓取失败"
                               "（非登录墙），可能是选择器失效或网络问题，需人工看日志",
                               3600)
        time.sleep(sched.seconds_until_next(time.time()))


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
            if dry_run:
                log(f"—— dry-run 应推送 ——\n{format_push_plain(post, cls)}")
                n += 1
            elif push_post(post, cls):
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
