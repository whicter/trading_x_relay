"""帖子分类器（纯函数，零依赖，可单测）。

判断一条 X 帖子是「指数分析」还是「个股分析/其他」，供 x_relay 决定是否推送。

第一版是启发式：零成本、确定性、可单测。strategy_explore.md §A.9 记录了
LLM 是点位**抽取**的刚需（条件句/缩写/反讽），但「是否个股」这个二分类
启发式已够用——先跑起来拿真实分布，精度不够再升级。

原则：
- 混合帖（既提指数又提个股）按指数处理——宁可多推，不漏指数内容
- 分类只影响推送，不影响入库——所有帖子原文全部落库，分错可回溯重放
- assume_index 账号（Mancini/Dayu 这类被确认专发指数点位的）：
  纯数字帖（如「6900 support, 6885 below」不写 ES 二字）也判为指数点位
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field

# ── 指数关键词 ──────────────────────────────────────────────
# 短代码歧义大（"es" 是常见英文词尾），要求大写 + 词边界
_INDEX_UPPER = re.compile(r"\b(ES|NQ|YM|RTY|NDX|SPX|MNQ|MES|M2K|MYM)\b")
# 期货 hashtag 写法（Mancini 常年用 #ES_F）
_INDEX_FUT = re.compile(r"(?i)#?\b(ES_F|NQ_F|YM_F|RTY_F)\b")
# 长词不分大小写。**不含裸 "future(s)"**——实测 2026-08-06 它把加密推广帖
# "Interlink Network's future is very bright" 误判成指数。真正的指数期货帖
# 一定会写出品种（ES/NQ/#ES_F/S&P…），裸 futures 只带来假阳性。
_INDEX_ANY = re.compile(r"(?i)\b(nasdaq|s&p\s?500|s&p|sp500|spy|qqq|dow|djia|russell|vix|e-?mini)\b")
# 指数名里自带的数字不是点位（"S&P 500" 的 500、"Russell 2000" 的 2000）——
# 抽点位前先抹掉，否则 willem 那类帖会凭空多出一个 500 点位
_INDEX_NAME_NUM = re.compile(
    r"(?i)\b(s\s?&\s?p\s?500|sp\s?500|nasdaq\s?100|ndx\s?100|russell\s?2000|"
    r"dow\s?30|nifty\s?50)\b")
_INDEX_CN = ("纳指", "纳斯达克", "标普", "大盘", "道指", "指数", "期指", "纳次达克")

# 指数类 cashtag/代码，不算个股信号
_INDEX_TICKERS = {
    "ES", "NQ", "YM", "RTY", "NDX", "SPX", "SPY", "QQQ", "VIX", "DIA",
    "IWM", "DJI", "RUT", "COMP", "MNQ", "MES", "M2K", "MYM", "DJIA",
}

# ── 个股信号 ────────────────────────────────────────────────
_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
# 无 $ 前缀的知名个股代码（大写词边界，避免误伤普通单词）
_STOCK_TICKERS = re.compile(
    r"\b(TSLA|NVDA|AAPL|MSFT|GOOGL?|AMZN|META|AMD|NFLX|PLTR|COIN|MSTR|"
    r"AVGO|SMCI|MU|INTC|BABA|SNDK|HOOD|SOFI|CRWD|ARM|TSM)\b")
_STOCK_CN = ("特斯拉", "英伟达", "苹果", "微软", "谷歌", "亚马逊", "台积电",
             "阿里", "个股", "财报")

# ── 点位数字抽取 ────────────────────────────────────────────
# 带千分位或纯数字，可带小数；排除紧邻 % / : / $ 的（百分比/时间/价格个股常用$）
_NUMBER = re.compile(r"(?<![\d.,:%$])(\d{1,2},\d{3}(?:\.\d+)?|\d{3,5}(?:\.\d+)?)(?!\d)(?!,\d)(?!\s?%)(?!:)")

# 下限 600：SPY 现价约 770，是最低的相关标的；再低的数字在指数语境下几乎
# 都是"点数"而非"点位"（实测 Mancini "a 400+ point vertical rally" 的 400
# 曾被抽成点位）。上限留到 60,000 覆盖 NQ ~26,000 及未来上涨。
LEVEL_MIN, LEVEL_MAX = 600.0, 60_000.0


@dataclass
class Classification:
    label: str                       # index_levels | index_view | stock | other
    levels: list = field(default_factory=list)   # 抽出的候选点位（float）
    cashtags: list = field(default_factory=list)

    @property
    def is_index(self) -> bool:
        return self.label.startswith("index")


def _extract_levels(text: str) -> list:
    text = _INDEX_NAME_NUM.sub(" INDEXNAME ", text)
    out = []
    for m in _NUMBER.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        # 过滤年份（1900-2100 的整数几乎必是年份；现今指数点位不落在此区间）
        if v == int(v) and 1900 <= v <= 2100:
            continue
        if LEVEL_MIN <= v <= LEVEL_MAX:
            out.append(v)
    # 去重保序
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def _has_index_keyword(text: str) -> bool:
    if _INDEX_UPPER.search(text) or _INDEX_FUT.search(text) or _INDEX_ANY.search(text):
        return True
    # 指数 cashtag 本身就是指数关键词（Dayu 全用小写 $spx / $ndx，
    # 大写正则抓不到——2026-08-06 实测发现）
    if any(t.upper() in _INDEX_TICKERS for t in _CASHTAG.findall(text)):
        return True
    return any(k in text for k in _INDEX_CN)


def _stock_signals(text: str) -> list:
    tags = [t.upper() for t in _CASHTAG.findall(text)]
    stock_tags = [t for t in tags if t not in _INDEX_TICKERS]
    if stock_tags:
        return stock_tags
    m = _STOCK_TICKERS.findall(text)
    if m:
        return [x.upper() for x in m]
    return [w for w in _STOCK_CN if w in text]


def classify_post(text: str, assume_index: bool = False) -> Classification:
    """text → Classification。assume_index 见模块 docstring。"""
    # X 微数据里 & 是双重编码（实测 "S&amp;P 500"），不解码则 s&p 关键词失效
    text = _html.unescape(text or "")
    stock = _stock_signals(text)
    is_index = _has_index_keyword(text)
    levels = _extract_levels(text)

    if is_index:
        return Classification("index_levels" if levels else "index_view",
                              levels, stock)
    if assume_index and levels and not stock:
        return Classification("index_levels", levels, stock)
    if stock:
        return Classification("stock", [], stock)
    return Classification("other", [], [])
