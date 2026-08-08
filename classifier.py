"""帖子分类器（纯函数，零依赖，可单测）。

给帖子打标签，供 x_relay 决定是否推送。

**2026-08-08 口径反转（用户决定）**：原设计是白名单——只推 index_*，
个股/商品/宏观/加密全被丢弃。实测 38 小时后发现代价：Dayu 14 条里 8 条是
个股（$spcx/$arqq/$aaoi/$gdx），正是用户认为他准的那部分，全被扔了。

现在改为**黑名单**：默认全推，只拦「与交易无关」的三类——
代币项目推广（用户数里程碑/TGE/KYC/空投）、鸡汤名言、账号公告与杂事。
标签仍细分（index/stock/commodity/macro/crypto），只为消息头可读，不再决定去留。

第一版是启发式：零成本、确定性、可单测。strategy_explore.md §A.9 记录了
LLM 是点位**抽取**的刚需（条件句/缩写/反讽），但「是否个股」这个二分类
启发式已够用——先跑起来拿真实分布，精度不够再升级。

原则：
- 混合帖（既提指数又提个股）按指数处理——宁可多推，不漏指数内容
- **标签优先级**：index > commodity > crypto > stock > macro > promo > chatter > other
  （先判最具体的标的类，再判宏观；promo/chatter 是拦截类，见 PUSH_BLOCKED）
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

# ── 商品 ────────────────────────────────────────────────────
_COMMODITY = re.compile(
    r"(?i)\b(gold|silver|copper|platinum|palladium|crude|oil|wti|brent|"
    r"natgas|natural\s?gas|miners?|bullion|"
    r"GC|SI|CL|NG|HG|GLD|SLV|GDX|GDXJ|USO|UNG)\b")
_COMMODITY_CN = ("黄金", "白银", "原油", "石油", "铜价", "天然气", "贵金属", "金价", "银价")

# ── 加密：行情观点 vs 项目推广，必须分开 ──────────────────────
# 行情观点（推）：谈价格、走势、仓位的主流币
_CRYPTO_MAJOR = re.compile(
    r"(?i)(\$)?\b(BTC|ETH|SOL|XRP|DOGE|ADA|BNB|LTC|bitcoin|ethereum|solana)\b")
_CRYPTO_CN = ("比特币", "以太坊", "以太币", "加密货币")
# 光提到主流币不够——「Pact 现已支持 BTC、ETH 之间的原生代币兑换」是产品公告，
# 不是行情观点，却因提到 BTC 命中 _CRYPTO_MAJOR（2026-08-08 真实回放发现）。
# 要求同时具备**行情语义**：价格/方向/仓位/技术位。
_MARKET_VIEW = re.compile(
    r"(?i)\b(support|resistance|target|breakout|breakdown|pullback|rally|"
    r"long|short|buy|sell|bull|bear|oversold|overbought|dip|top|bottom|"
    r"wave|trend|entry|exit|stop|level)\b")
_MARKET_VIEW_CN = ("支撑", "阻力", "目标", "突破", "跌破", "回调", "反弹",
                   "做多", "做空", "买入", "卖出", "多头", "空头", "超卖",
                   "超买", "见顶", "见底", "浪", "趋势", "止损", "点位", "仓位")
# 产品/生态公告的通用形态（不含 TGE/KYC 这类硬词时的兜底）
_PRODUCT_NEWS = re.compile(
    r"(?i)\b(now\s+(supports?|runs?|live)|update\s+notice|"
    r"native\s+swaps?|ecosystem|integration|partnership|milestone|users?\s+"
    r"milestone|upcoming\s+update)\b")
_PRODUCT_NEWS_CN = ("现已支持", "更新公告", "上线交易", "合作", "里程碑", "涵盖各类")
# 项目推广（拦）：用户数里程碑 / TGE / KYC / 空投 / 生态叙事 —— 这是广告不是观点。
# 2026-08-06~08 实测：@time_and_trade 14 条里 12 条是这个形态
# （Interlink/StarX/Pact/Parallax），已因此把该账号整个剔出名单。
_PROMO = re.compile(
    r"(?i)\b(TGE|KYC|airdrop|whitelist|presale|IDO|ICO|tokenomics|"
    r"roadmap|mainnet|testnet|layer-?1|layer-?2|staking\s?reward|"
    r"social\s?mining|referral)\b")
_PROMO_CN = ("空投", "白名单", "预售", "代币生成", "生态系统", "路线图",
             "用户里程碑", "主网", "测试网", "质押奖励", "邀请码")

# ── 宏观 / 政策 ─────────────────────────────────────────────
_MACRO = re.compile(
    r"(?i)\b(fed|fomc|powell|rate\s?(cut|hike)|cpi|ppi|pce|inflation|"
    r"nfp|payrolls?|jobless|tariffs?|treasury|yields?|10-?year|"
    r"recession|stimulus|QE|QT|dollar|DXY|debt\s?ceiling)\b")
_MACRO_CN = ("美联储", "降息", "加息", "通胀", "关税", "非农", "国债",
             "美元指数", "衰退", "政策", "议息")

# ── 拦截类：鸡汤 / 账号公告 / 杂事 ───────────────────────────
_CHATTER = re.compile(
    r"(?i)(not\s+(financial|investment)\s+advice|"
    r"i\s+(only\s+)?have\s+(no|only)\s+.{0,20}(account|group)|"
    r"forgot\s+to\s+send|"
    r"—\s*J\.?K\.?\s*Rowling)")
_CHATTER_CN = ("不作为任何投资建议", "盈亏自负", "没有任何群组", "忘记发",
               "对着镜子", "概不负责")

# ── 点位数字抽取 ────────────────────────────────────────────
# 带千分位或纯数字，可带小数；排除紧邻 % / : / $ 的（百分比/时间/价格个股常用$）
_NUMBER = re.compile(r"(?<![\d.,:%$])(\d{1,2},\d{3}(?:\.\d+)?|\d{3,5}(?:\.\d+)?)(?!\d)(?!,\d)(?!\s?%)(?!:)")

# 下限 600：SPY 现价约 770，是最低的相关标的；再低的数字在指数语境下几乎
# 都是"点数"而非"点位"（实测 Mancini "a 400+ point vertical rally" 的 400
# 曾被抽成点位）。上限留到 60,000 覆盖 NQ ~26,000 及未来上涨。
LEVEL_MIN, LEVEL_MAX = 600.0, 60_000.0


# 与交易无关、**不推送**的标签。这是全部拦截清单——其余一律推。
# 反转自 2026-08-08 前的白名单 PUSH_CLASSES={index_levels,index_view}，
# 理由见模块 docstring。
PUSH_BLOCKED = frozenset({"promo", "chatter", "other"})


@dataclass
class Classification:
    label: str   # index_levels|index_view|commodity|crypto|stock|macro|promo|chatter|other
    levels: list = field(default_factory=list)   # 抽出的候选点位（float）
    cashtags: list = field(default_factory=list)

    @property
    def is_index(self) -> bool:
        return self.label.startswith("index")

    @property
    def is_pushable(self) -> bool:
        """是否值得推送。**判据是标签不在黑名单里**，不是"是否指数"。"""
        return self.label not in PUSH_BLOCKED


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


def _hit(pattern, cn_words, text: str) -> bool:
    return bool(pattern.search(text)) or any(w in text for w in cn_words)


def classify_post(text: str, assume_index: bool = False) -> Classification:
    """text → Classification。assume_index 见模块 docstring。"""
    # X 微数据里 & 是双重编码（实测 "S&amp;P 500"），不解码则 s&p 关键词失效
    text = _html.unescape(text or "")
    stock = _stock_signals(text)
    is_index = _has_index_keyword(text)
    levels = _extract_levels(text)

    # 拦截优先：代币推广即使带 $TICKER 也不是交易观点。
    # 先于所有标的判定，否则 "$ITLG 生态系统" 会被判成 stock 推出去。
    if _hit(_PROMO, _PROMO_CN, text):
        return Classification("promo", [], stock)

    # 指数最优先（混合帖按指数处理，见模块 docstring）
    if is_index:
        return Classification("index_levels" if levels else "index_view",
                              levels, stock)
    if assume_index and levels and not stock:
        return Classification("index_levels", levels, stock)

    # 标的类：商品 > 加密 > 个股。都推，标签只为消息头可读。
    if _hit(_COMMODITY, _COMMODITY_CN, text):
        return Classification("commodity", levels, stock)
    if _hit(_CRYPTO_MAJOR, _CRYPTO_CN, text):
        # 主流币 + 行情语义 = 观点（推）；主流币 + 产品公告 = 广告（拦）
        if _hit(_MARKET_VIEW, _MARKET_VIEW_CN, text):
            return Classification("crypto", levels, stock)
        if _hit(_PRODUCT_NEWS, _PRODUCT_NEWS_CN, text):
            return Classification("promo", [], stock)
        return Classification("crypto", levels, stock)
    if stock:
        return Classification("stock", levels, stock)

    # 宏观放在标的之后：「关税打压纳指」应归指数，不是宏观
    if _hit(_MACRO, _MACRO_CN, text):
        return Classification("macro", levels, stock)

    # 鸡汤/公告/杂事——只在什么标的都没提到时才判，避免误杀
    # （"$spx 见顶，盈亏自负" 是观点不是免责声明）
    if _hit(_CHATTER, _CHATTER_CN, text):
        return Classification("chatter", [], [])

    # assume_index 账号的**无标的、无数字**行情观点。
    # 2026-08-08 真实回放发现：novicetrader888 的「多头不用太紧张…连续短时间
    # 超买…继续持多设好止损」是实打实的持仓观点，却因没写"纳指"二字、也没有
    # 点位数字而掉进 other 被拦。这类账号本就被确认专发指数评论（assume_index
    # 的含义），有明确行情语义即可判为 index_view。
    # 不对普通账号开这条：否则任何提到 "long/short" 的闲聊都会被推。
    if assume_index and _hit(_MARKET_VIEW, _MARKET_VIEW_CN, text):
        return Classification("index_view", levels, stock)
    return Classification("other", [], [])
