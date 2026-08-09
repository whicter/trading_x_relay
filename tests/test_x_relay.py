"""x_relay 单测：分类器、台账幂等、推送门控、以及「绝不下单」的 AST 边界。"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from classifier import classify_post                         # noqa: E402
from store import PostStore                                  # noqa: E402
import x_relay                                               # noqa: E402


class ClassifierIndexTest(unittest.TestCase):
    """样本取自 2026-08-06 五个账号的**真实抓取内容**，不是编的。"""

    def test_mancini_style_es_levels(self):
        c = classify_post("ES recovered 6900. Bulls need to hold 6885, "
                          "below that 6862 then 6841.")
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [6900.0, 6885.0, 6862.0, 6841.0])

    def test_real_mancini_post(self):
        c = classify_post(
            "No volatility in #ES_F at all and everything in slow motion today. "
            "Price is resting after a 400+ point vertical rally from the 7325 "
            "Failed Breakdown last Wednesday. Bulls must recover 7751 to see "
            "7763, 7774, 7782+.\n\nNo big supports now until 7708")
        self.assertEqual(c.label, "index_levels")
        for lvl in (7325.0, 7751.0, 7763.0, 7774.0, 7782.0, 7708.0):
            self.assertIn(lvl, c.levels)

    def test_real_dayu_lowercase_cashtag(self):
        """Dayu 全用小写 $spx/$ndx——大写正则抓不到，靠指数 cashtag 识别。"""
        c = classify_post(
            "Cashed out my $spx short entered at 7789. As in a typical wave-4 "
            "pullback, $spx has moved down correctively. Although my ideal "
            "target for wave-4 is 7660-7580, I am not going to risk gains.")
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [7789.0, 7660.0, 7580.0])

    def test_real_chinese_index_commentary(self):
        """中文行文常不写"纳指/标普"，只有数字——靠账号级 assume_index 兜底。"""
        c = classify_post(
            "从我预测7660回撤到现在7760正正100点一柱擎天式样拉升，逼空是很可能的。",
            assume_index=True)
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [7660.0, 7760.0], "100 点不是点位（低于下限）")

    def test_html_entity_ampersand(self):
        """微数据里 & 是双重编码（实测 "S&amp;P 500"），不解码则关键词失效。
        且指数名自带的 500 不是点位。"""
        c = classify_post("Updated 1-top, 2-fail, 3-beyond S&amp;P 500. "
                          "Wil it work properly?")
        self.assertEqual(c.label, "index_view")
        self.assertEqual(c.levels, [], '"S&P 500" 的 500 不是点位')

    def test_index_name_numbers_are_not_levels(self):
        c = classify_post("Russell 2000 leading while S&P 500 stalls at 7,751")
        self.assertEqual(c.levels, [7751.0])

    def test_dayu_style_chinese_index(self):
        c = classify_post("纳指四浪回调目标 24,800，若跌破则看 24,350，"
                          "上方压力 25,600")
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [24800.0, 24350.0, 25600.0])

    def test_index_view_without_numbers(self):
        c = classify_post("Nasdaq looking heavy into the close, breadth terrible")
        self.assertEqual(c.label, "index_view")
        self.assertEqual(c.levels, [])

    def test_thousands_separator_and_decimal(self):
        c = classify_post("NQ pivot 25,712.25 支撑 25,488")
        self.assertEqual(c.levels, [25712.25, 25488.0])


class ClassifierStockTest(unittest.TestCase):
    def test_cashtag_stock(self):
        c = classify_post("$MRNA breaking out over 145, next stop 160")
        self.assertEqual(c.label, "stock")
        self.assertIn("MRNA", c.cashtags)

    def test_bare_famous_ticker(self):
        c = classify_post("TSLA earnings tonight, expecting big move")
        self.assertEqual(c.label, "stock")

    def test_chinese_stock_name(self):
        c = classify_post("特斯拉今天的走势说明资金在出逃")
        self.assertEqual(c.label, "stock")

    def test_real_crypto_shill_is_filtered(self):
        """@time_and_trade 实测内容：加密代币推广，必须进不了推送。"""
        c = classify_post(
            "Interlink Network ( $ITLG, $ITL ) is different from its peers. "
            "Interlink Network's future is very bright because the core team "
            "is dynamic, forward looking, well planning and fast moving.")
        self.assertEqual(c.label, "stock")
        self.assertFalse(c.is_index)

    def test_mixed_post_counts_as_index(self):
        """混合帖按指数处理——宁可多推，不漏指数内容。"""
        c = classify_post("$NVDA strong but SPX rejected at 6,900 again")
        self.assertTrue(c.is_index)

    def test_index_cashtag_is_not_stock(self):
        c = classify_post("$SPX 6900 是关键位")
        self.assertEqual(c.label, "index_levels")


class ClassifierEdgeTest(unittest.TestCase):
    def test_lowercase_es_word_fragment_not_keyword(self):
        """小写 es 是常见英文词尾，绝不能当成 ES 期货。"""
        c = classify_post("my best guesses on estimates here, nothing else")
        self.assertEqual(c.label, "other")

    def test_year_not_a_level(self):
        c = classify_post("SPX has been ripping since 2024")
        self.assertEqual(c.label, "index_view", "年份不是点位")

    def test_percent_not_a_level(self):
        c = classify_post("NQ down 350% no wait that cant be right")
        self.assertNotIn(350.0, c.levels)

    def test_move_size_is_not_a_level(self):
        """实测：Mancini "a 400+ point vertical rally" 的 400 是涨跌幅不是点位。"""
        c = classify_post("ES rallied 400+ points off the 7325 low")
        self.assertEqual(c.levels, [7325.0])

    def test_assume_index_numbers_only(self):
        """Mancini 常年不写 ES 二字只发数字——assume_index 账号纯数字帖判指数。"""
        c = classify_post("6900 support held. 6925 next, then 6941.",
                          assume_index=True)
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [6900.0, 6925.0, 6941.0])

    def test_assume_index_does_not_swallow_stock(self):
        c = classify_post("$MRNA to 160", assume_index=True)
        self.assertEqual(c.label, "stock")

    def test_no_assume_numbers_only_is_other(self):
        c = classify_post("6900 support held.")
        self.assertEqual(c.label, "other",
                         "未知账号的纯数字帖不猜——落库靠人工复核，不推送")

    def test_empty_and_none(self):
        self.assertEqual(classify_post("").label, "other")
        self.assertEqual(classify_post(None).label, "other")


class StoreTest(unittest.TestCase):
    def _store(self):
        return PostStore(Path(tempfile.mkdtemp()) / "t.sqlite3")

    def _post(self, pid="1", **kw):
        d = {"post_id": pid, "handle": "a", "author": "a",
             "published_at": "2026-08-06T14:00:00.000Z",
             "fetched_at": "2026-08-06T14:05:00+00:00",
             "text": "ES 6900", "classification": "index_levels",
             "levels": [6900.0]}
        d.update(kw)
        return d

    def test_insert_dedup(self):
        """置顶帖每轮重复出现——post_id 主键幂等是台账不写重的保证。"""
        s = self._store()
        self.assertTrue(s.insert_post(self._post()))
        self.assertFalse(s.insert_post(self._post()))
        self.assertEqual(s.stats()["total"], 1)

    def test_mark_pushed_only_once(self):
        s = self._store()
        s.insert_post(self._post())
        s.mark_pushed("1", "2026-08-06T14:06:00+00:00")
        s.mark_pushed("1", "2026-08-07T00:00:00+00:00")     # 不得覆盖首推时间
        row = s.conn.execute("SELECT pushed_at FROM posts").fetchone()
        self.assertEqual(row[0], "2026-08-06T14:06:00+00:00")

    def test_image_no_levels_stat(self):
        s = self._store()
        s.insert_post(self._post("1", has_image=True, levels=[]))
        s.insert_post(self._post("2", has_image=True, levels=[6900.0]))
        self.assertEqual(s.stats()["image_no_levels"], 1)

    def test_has_post_does_not_insert(self):
        """dry-run 靠它判断"是不是新帖"——必须只读，不能有写副作用。"""
        s = self._store()
        self.assertFalse(s.has_post("1"))
        self.assertEqual(s.stats()["total"], 0, "查询不得写库")
        s.insert_post(self._post())
        self.assertTrue(s.has_post("1"))

    def test_has_handle_gates_bootstrap(self):
        """首轮空库时整页全是新帖是必然的，不该被当成"漏帖"告警。"""
        s = self._store()
        self.assertFalse(s.has_handle("a"))
        s.insert_post(self._post())
        self.assertTrue(s.has_handle("a"))
        self.assertFalse(s.has_handle("b"))


class PushGatingTest(unittest.TestCase):
    def _post(self, **kw):
        d = {"post_id": "1", "handle": "a", "is_retweet": False,
             "published_at": datetime.now(timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%S.000Z")}
        d.update(kw)
        return d

    def test_push_all_trading_content_block_only_noise(self):
        """2026-08-08 口径反转：原断言是「个股不推」，那正是被改掉的行为。

        用户决定：个股/商品/宏观/加密行情全要，只拦代币推广与杂谈。
        本测试因此从「白名单」改为守住「黑名单」——推的判据是
        classification 不在 PUSH_BLOCKED 里，不是 is_index。"""
        now = datetime.now(timezone.utc)
        for text, should in [
            ("ES 6900 key", True),                                    # 指数
            ("$MRNA to 160", True),                                   # 个股（原来被拦）
            ("gold and silver miners look strong here", True),        # 商品
            ("BTC broke support, next target 88,000", True),          # 加密行情
            ("Fed likely cuts rates, tariffs still the wildcard", True),  # 宏观
            ("StarX ($STRX): KYC open, TGE in Q4", False),            # 代币推广
            ("— J.K. Rowling", False),                                # 鸡汤
        ]:
            cls = classify_post(text)
            self.assertEqual(
                x_relay.should_push(self._post(), cls, now), should,
                f"{text!r} → {cls.label}，期望 push={should}")

    def test_no_push_retweet(self):
        now = datetime.now(timezone.utc)
        idx = classify_post("ES 6900 key")
        self.assertFalse(x_relay.should_push(self._post(is_retweet=True), idx, now))

    def test_no_push_stale_backfill(self):
        """首轮抓到几天前的历史帖：落库但不推，防刷屏。"""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=x_relay.PUSH_MAX_AGE_HOURS + 1)
               ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        idx = classify_post("ES 6900 key")
        self.assertFalse(x_relay.should_push(self._post(published_at=old), idx, now))

    def test_format_flags_image_without_levels(self):
        """A.9 已知难点：点位在图里、文本没数字——推送里必须提示看图。"""
        msg = x_relay.format_push(
            self._post(has_image=True, text="levels updated 👇"),
            classify_post("levels updated 👇 nasdaq"))
        self.assertIn("帖内含图", msg)


class MicrodataExtractionTest(unittest.TestCase):
    """免登录微数据解析（2026-08-06 实测路线）的行为固定。"""

    class FakePage:
        def __init__(self, rows):
            self.rows = rows

        def evaluate(self, _js):
            return self.rows

    def test_dedup_by_identifier(self):
        """thread 分组会把同一条帖重复渲染成多个 <article>。"""
        rows = [{"post_id": "1", "published_at": "2026-08-06T17:08:49.000Z",
                 "text": "ES 7751", "author": "AdamMancini4", "n_images": 0,
                 "head": ""},
                {"post_id": "1", "published_at": "2026-08-06T17:08:49.000Z",
                 "text": "ES 7751", "author": "AdamMancini4", "n_images": 0,
                 "head": ""},
                {"post_id": "2", "published_at": "2026-08-06T15:55:39.000Z",
                 "text": "ES 7741", "author": "AdamMancini4", "n_images": 0,
                 "head": ""}]
        posts = x_relay.extract_posts(self.FakePage(rows), "AdamMancini4")
        self.assertEqual([p["post_id"] for p in posts], ["1", "2"])

    def test_author_case_difference_is_not_retweet(self):
        """实测：微数据 author 是 "Time_and_Trade"，handle 是 time_and_trade。
        大小写不同不是转发——大小写敏感比较会把本人帖全判成转发、全部不推。"""
        rows = [{"post_id": "9", "published_at": "2026-08-06T18:43:42.000Z",
                 "text": "x", "author": "Time_and_Trade", "n_images": 0,
                 "head": ""}]
        posts = x_relay.extract_posts(self.FakePage(rows), "time_and_trade")
        self.assertFalse(posts[0]["is_retweet"])

    def test_real_retweet_detected(self):
        rows = [{"post_id": "9", "published_at": "2026-08-06T18:43:42.000Z",
                 "text": "x", "author": "SomeoneElse", "n_images": 0, "head": ""}]
        posts = x_relay.extract_posts(self.FakePage(rows), "time_and_trade")
        self.assertTrue(posts[0]["is_retweet"])

    def test_image_and_pinned_flags(self):
        rows = [{"post_id": "9", "published_at": "2026-08-06T10:26:39.000Z",
                 "text": "Updated 1-2-3", "author": "willem82457275",
                 "n_images": 2, "head": "Pinned\nWillem"}]
        posts = x_relay.extract_posts(self.FakePage(rows), "willem82457275")
        self.assertTrue(posts[0]["has_image"])
        self.assertTrue(posts[0]["is_pinned"])

    def test_unhydrated_rows_are_dropped(self):
        """有 id、无时间戳 = 下滑时抓到还没 hydrate 完的 article。

        2026-08-07 实测一轮进了 10 条这种空壳。危害不止脏数据：它若在页面
        顶部就会成为增量基准线，而其内容我们从没拿到过 → 之后永远不再抓它。"""
        rows = [{"post_id": "123", "published_at": None, "text": "",
                 "author": None, "n_images": 0, "head": ""},
                {"post_id": "124", "published_at": "2026-08-06T17:08:49.000Z",
                 "text": "ES 7751", "author": "a", "n_images": 0, "head": ""}]
        posts = x_relay.extract_posts(self.FakePage(rows), "a")
        self.assertEqual([p["post_id"] for p in posts], ["124"])

    def test_rows_without_identifier_skipped(self):
        """author 块里也有 identifier；解析若没排除 author 会串号——
        这里保证没有 post_id 的行安静跳过而不是崩。"""
        rows = [{"post_id": None, "published_at": None, "text": "",
                 "author": None, "n_images": 0, "head": ""}]
        self.assertEqual(x_relay.extract_posts(self.FakePage(rows), "h"), [])


class IncrementalFetchTest(unittest.TestCase):
    """「补齐上次之后的全部帖子」：靠雪花 id 单调递增 + 窗口重叠证明。"""

    class FakePage:
        """模拟下滑：每 scrollTo 一次多返回一屏，直到耗尽。"""

        def __init__(self, pages):
            self.pages, self.i, self.scrolls = pages, 0, 0

        def evaluate(self, js):
            if "scrollTo" in js:
                self.scrolls += 1
                self.i = min(self.i + 1, len(self.pages) - 1)
                return None
            return self.pages[self.i]

        def wait_for_timeout(self, _ms):
            pass

        def goto(self, *a, **k):
            pass

        def wait_for_selector(self, *a, **k):
            pass

        def close(self):
            pass

    @staticmethod
    def _row(pid, head=""):
        return {"post_id": str(pid), "published_at": "2026-08-06T12:00:00.000Z",
                "text": "ES 7751", "author": "AdamMancini4", "n_images": 0,
                "mode": "testid", "head": head}

    def _fetch(self, pages, since_id):
        page = self.FakePage(pages)

        class Ctx:
            def new_page(_self):
                return page
        acct = x_relay.Account("AdamMancini4")
        return x_relay.fetch_account(Ctx(), acct, since_id=since_id), page

    def test_snowflake_ordering(self):
        self.assertGreater(x_relay._snowflake("2085412793302360247"),
                           x_relay._snowflake("2085394380060328419"))
        self.assertEqual(x_relay._snowflake(None), -1)
        self.assertEqual(x_relay._snowflake("not-a-number"), -1)

    def test_newest_post_id_ignores_pinned(self):
        """置顶帖永远在页面顶部却很旧——当基准线会让"已接上"永远成立、真漏帖。"""
        s = PostStore(Path(tempfile.mkdtemp()) / "t.sqlite3")
        base = {"handle": "a", "author": "a", "published_at": "2026-08-06T12:00:00Z",
                "fetched_at": "2026-08-06T12:05:00Z", "text": "x",
                "classification": "index_levels", "levels": []}
        s.insert_post({**base, "post_id": "9999999999999999999", "is_pinned": True})
        s.insert_post({**base, "post_id": "2085394380060328419"})
        s.insert_post({**base, "post_id": "2085412793302360247"})
        self.assertEqual(s.newest_post_id("a"), "2085412793302360247")

    def test_newest_post_id_ignores_unhydrated_rows(self):
        """第二道防线：空壳行即便入了库，也不得当基准线。"""
        s = PostStore(Path(tempfile.mkdtemp()) / "t.sqlite3")
        base = {"handle": "a", "author": "a", "fetched_at": "2026-08-06T12:05:00Z",
                "text": "x", "classification": "other", "levels": []}
        s.insert_post({**base, "post_id": "2085412793302360247",
                       "published_at": "2026-08-06T12:00:00Z"})
        s.insert_post({**base, "post_id": "2085999999999999999",
                       "published_at": None})            # id 更大的空壳
        self.assertEqual(s.newest_post_id("a"), "2085412793302360247")

    def test_newest_post_id_sorts_numerically_not_lexically(self):
        """字符串排序会把 "999" 排到 "2085…" 之后——必须按数值。"""
        s = PostStore(Path(tempfile.mkdtemp()) / "t.sqlite3")
        base = {"handle": "a", "author": "a", "published_at": "2026-08-06T12:00:00Z",
                "fetched_at": "2026-08-06T12:05:00Z", "text": "x",
                "classification": "other", "levels": []}
        s.insert_post({**base, "post_id": "999"})
        s.insert_post({**base, "post_id": "2085412793302360247"})
        self.assertEqual(s.newest_post_id("a"), "2085412793302360247")

    def test_stops_as_soon_as_overlap_proven(self):
        """抓到上次那条即停——不做无谓翻页。"""
        pages = [[self._row(30), self._row(29)],
                 [self._row(30), self._row(29), self._row(28), self._row(27)]]
        (posts, complete), page = self._fetch(pages, since_id="28")
        self.assertTrue(complete)
        self.assertEqual(page.scrolls, 1, "接上后应立即停止下滑")
        self.assertIn("27", {p["post_id"] for p in posts})

    def test_reports_incomplete_when_never_reaches_baseline(self):
        """翻到底仍没接上 → 必须如实报 complete=False（未登录态的常态）。"""
        (_, complete), _ = self._fetch([[self._row(50), self._row(49)]],
                                       since_id="10")
        self.assertFalse(complete, "证明不了没漏，就不能说没漏")

    def test_pinned_post_cannot_fake_overlap(self):
        """第一屏就带着很旧的置顶帖——不能据此判定"已接上"。"""
        pages = [[self._row(50), self._row(1, head="Pinned\nAdam")],
                 [self._row(50), self._row(1, head="Pinned\nAdam"), self._row(49)]]
        (_, complete), _ = self._fetch(pages, since_id="10")
        self.assertFalse(complete, "置顶帖 id 很旧，不能当窗口重叠的证据")

    def test_no_baseline_means_bootstrap_not_gap(self):
        """首次抓某账号没有基准线：不报缺口，且**刻意少翻**（不搬全部历史）。"""
        pages = [[self._row(50 - i) for i in range(n)] for n in range(1, 40)]
        (_, complete), page = self._fetch(pages, since_id=None)
        self.assertTrue(complete, "没有基准线时无从证伪，不该记成漏帖")
        self.assertLessEqual(page.scrolls, x_relay.BOOTSTRAP_MAX_SCROLLS,
                             "首抓不该把人家几年的历史全搬回来")

    def test_incremental_scrolls_deeper_than_bootstrap(self):
        """有基准线时预算更大——那才是"补齐上次之后全部"要用的深度。"""
        pages = [[self._row(500 - i) for i in range(n)] for n in range(1, 40)]
        (_, complete), page = self._fetch(pages, since_id="1")
        self.assertFalse(complete)
        self.assertGreater(page.scrolls, x_relay.BOOTSTRAP_MAX_SCROLLS)
        self.assertLessEqual(page.scrolls, x_relay.MAX_SCROLLS)


class NeverTradesTest(unittest.TestCase):
    """本服务绝不进任何下单路径：AST 层禁止 IB 相关 import 与下单标识符。"""

    FORBIDDEN_IMPORTS = {"ib_insync", "ibapi"}
    FORBIDDEN_NAMES = {"placeOrder", "place_order", "MarketOrder", "LimitOrder",
                       "StopOrder", "reqIds", "bracket_order"}

    def test_no_ib_no_orders(self):
        for f in ("x_relay.py", "classifier.py", "store.py"):
            tree = ast.parse((BASE / f).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = {a.name.split(".")[0] for a in node.names}
                    self.assertFalse(mods & self.FORBIDDEN_IMPORTS,
                                     f"{f} import 了 IB 库")
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0],
                                     self.FORBIDDEN_IMPORTS, f"{f} import 了 IB 库")
                elif isinstance(node, (ast.Name, ast.Attribute)):
                    name = node.id if isinstance(node, ast.Name) else node.attr
                    self.assertNotIn(name, self.FORBIDDEN_NAMES,
                                     f"{f} 出现下单标识符 {name}")

    def test_scan_catches_real_violation(self):
        """反向验证：真去 import ib_insync 的代码必须被上面的扫描逮住。"""
        tree = ast.parse("import ib_insync\nib.placeOrder(c, o)\n")
        hits = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hits += bool({a.name.split(".")[0] for a in node.names}
                             & self.FORBIDDEN_IMPORTS)
            elif isinstance(node, ast.Attribute):
                hits += node.attr in self.FORBIDDEN_NAMES
        self.assertGreaterEqual(hits, 2)


# ══════════════════════════════════════════════════════════════
# 2026-08-08 口径反转：白名单 index_* → 黑名单（只拦与交易无关的）
# 用户决定：个股/商品/宏观/加密行情都要推。
# 下列用例全部来自**真实抓取的帖子**，不是构造的。
# ══════════════════════════════════════════════════════════════
class BlacklistPushPolicyTest(unittest.TestCase):
    def test_commodity_is_pushed(self):
        """Dayu 08-07 真帖。旧口径判 stock 被丢弃——正是用户认为他准的那类。"""
        c = classify_post("Among all the asset classes, I think gold, silver, "
                          "and miners are the most appealing investment asset now. "
                          "I bought some when $gdx dipped below 70.",
                          assume_index=True)
        self.assertEqual(c.label, "commodity")
        self.assertTrue(c.is_pushable)

    def test_stock_levels_are_pushed(self):
        """Dayu 08-08 真帖：$arqq 回调 19-20 是可打分的点位。"""
        c = classify_post("quantum stocks ($qtum, $ionq, $rgti, $qbts, $arqq) "
                          "popped up recently. For $arqq, I want to buy again "
                          "at its pullback to 19-20.", assume_index=True)
        self.assertEqual(c.label, "stock")
        self.assertTrue(c.is_pushable)

    def test_macro_is_pushed(self):
        c = classify_post("Fed likely cuts rates next meeting, tariffs still "
                          "the wildcard for risk assets")
        self.assertEqual(c.label, "macro")
        self.assertTrue(c.is_pushable)

    def test_crypto_market_view_is_pushed(self):
        """用户 2026-08-08 明确要 BTC。"""
        c = classify_post("BTC broke down below its support, next target 88,000")
        self.assertEqual(c.label, "crypto")
        self.assertTrue(c.is_pushable)

    def test_crypto_product_announcement_is_blocked(self):
        """真帖：提到 BTC/ETH 但那是产品公告不是行情观点。

        没有 TGE/KYC 这类硬词，仅靠 _PROMO 拦不住——2026-08-08 真实回放
        发现它会被判成 crypto 推出去。"""
        c = classify_post("From BTC to Tron, and everything between. Pact now "
                          "runs native swaps across BTC, ETH, BNB, LTC.")
        self.assertEqual(c.label, "promo")
        self.assertFalse(c.is_pushable)

    def test_token_promo_with_cashtag_is_blocked(self):
        """带 $TICKER 也不能当 stock 推——拦截必须先于标的判定。"""
        c = classify_post("StarX Network ($STRX) is progressing. KYC is open, "
                          "preparing TGE in Q4 2026.")
        self.assertEqual(c.label, "promo")
        self.assertFalse(c.is_pushable)

    def test_disclaimer_is_blocked(self):
        c = classify_post("这个账户是我自己对着镜子自己说，不作为任何投资建议。盈亏自负，概不负责。")
        self.assertFalse(c.is_pushable)

    def test_quote_is_blocked(self):
        c = classify_post("“It's impossible to live without failing at something.”\n\n— J.K. Rowling")
        self.assertFalse(c.is_pushable)

    def test_assume_index_view_without_ticker_or_number(self):
        """novicetrader888 真帖：无标的、无数字，但是明确持仓观点。

        旧口径掉进 other 被拦。assume_index 账号本就被确认专发指数评论。"""
        c = classify_post("多头不用太紧张，似乎上面还有油水虽然不多。"
                          "但连续短时间超买也有个喘气的机会不是？继续持多设好止损",
                          assume_index=True)
        self.assertEqual(c.label, "index_view")
        self.assertTrue(c.is_pushable)

    def test_plain_account_chatter_not_promoted(self):
        """非 assume_index 账号不享受上面那条路径，否则任何闲聊都会被推。"""
        c = classify_post("going long on life today, no stops")
        self.assertFalse(c.is_pushable)

    def test_disclaimer_with_ticker_is_still_a_view(self):
        """「$spx 见顶，盈亏自负」是观点不是免责声明——拦截不得误杀。"""
        c = classify_post("$spx 见顶了，我开始做空。盈亏自负。")
        self.assertTrue(c.is_pushable)


class PushRetryTest(unittest.TestCase):
    """2026-08-08 事故：发送失败 = 永久丢失，无重试无告警。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.store = PostStore(Path(self.dir) / "t.sqlite3")

    def _post(self, pid, pub, cls="index_levels"):
        return {"post_id": pid, "handle": "h", "author": "h",
                "published_at": pub, "fetched_at": pub, "text": "SPX 7700",
                "classification": cls, "levels": [7700.0]}

    def test_failed_push_stays_in_queue(self):
        now = "2026-08-08T12:00:00+00:00"
        self.store.insert_post(self._post("1", "2026-08-08T11:00:00Z"))
        pending = self.store.pending_push(["promo", "chatter", "other"], 12, now, 5)
        self.assertEqual(len(pending), 1, "未推送的帖必须留在重试队列里")

    def test_pushed_post_leaves_queue(self):
        now = "2026-08-08T12:00:00+00:00"
        self.store.insert_post(self._post("1", "2026-08-08T11:00:00Z"))
        self.store.mark_pushed("1", now)
        self.assertEqual(
            len(self.store.pending_push(["other"], 12, now, 5)), 0)

    def test_retry_gives_up_after_max(self):
        now = "2026-08-08T12:00:00+00:00"
        self.store.insert_post(self._post("1", "2026-08-08T11:00:00Z"))
        for _ in range(5):
            self.store.bump_attempt("1")
        self.assertEqual(len(self.store.pending_push(["other"], 12, now, 5)), 0,
                         "超过重试上限应退出队列")
        self.assertEqual(self.store.give_up_count(5), 1,
                         "放弃的条数必须可见——原来是完全静默的")

    def test_blocked_class_never_queued(self):
        now = "2026-08-08T12:00:00+00:00"
        self.store.insert_post(self._post("1", "2026-08-08T11:00:00Z", cls="promo"))
        self.assertEqual(len(self.store.pending_push(["promo"], 12, now, 5)), 0)

    def test_old_post_not_queued(self):
        """12h 时效门对重试同样生效，避免补发几天前的旧帖刷屏。"""
        now = "2026-08-08T12:00:00+00:00"
        self.store.insert_post(self._post("1", "2026-08-05T11:00:00Z"))
        self.assertEqual(len(self.store.pending_push(["other"], 12, now, 5)), 0)


class AccountListTest(unittest.TestCase):
    def test_time_and_trade_removed(self):
        """14 条实抓里 12 条是代币推广，2026-08-08 用户决定移除。"""
        import x_relay
        self.assertNotIn("time_and_trade",
                         {a.handle for a in x_relay.ACCOUNTS})


class StartupPathTest(unittest.TestCase):
    """2026-08-08：口径反转时漏改 run_loop 的启动日志，进程起不来。

    单测全在纯函数上，没有一条覆盖 run_loop 的启动路径——重启后才炸。
    这条用 import 期的符号检查兜住同类遗漏（不需要真跑循环）。"""

    def test_no_stale_push_classes_symbol(self):
        import ast
        src = (Path(__file__).resolve().parent.parent / "x_relay.py").read_text(encoding="utf-8")
        names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        self.assertNotIn("PUSH_CLASSES", names,
                         "白名单常量已废弃，残留引用会让进程启动即崩")

    def test_module_level_names_all_resolvable(self):
        """所有模块级引用的自定义常量都必须真的存在。"""
        import x_relay
        for sym in ("PUSH_BLOCKED", "PUSH_RETRY_MAX", "PUSH_MAX_AGE_HOURS", "ACCOUNTS"):
            self.assertTrue(hasattr(x_relay, sym), f"{sym} 未定义")


class TickerLevelBindingTest(unittest.TestCase):
    """2026-08-08：点位必须绑定到标的，且个股点位不能被指数下限滤掉。

    原 LEVEL_MIN=600 是给指数定的（SPY≈770），个股点位大量是两位数
    （$arqq 19-20、$axti 89、$gdx 70）——全被丢弃，推送里只有原文没点位。
    简报要求「看好的标的和点位都给出来」，扁平 levels 列表做不到：
    「$aaoi 到 149，$axti 到 89」必须知道哪个数字属于谁。
    下列全部是真实抓到的原文。"""

    def test_two_tickers_two_levels_bound_separately(self):
        c = classify_post(
            "Both $aaoi and $axti have surged. $aaoi has reached 149 before "
            "pulling back. $axti has reached 89.", assume_index=True)
        self.assertEqual(c.ticker_levels.get("AAOI"), [149.0])
        self.assertEqual(c.ticker_levels.get("AXTI"), [89.0])

    def test_two_digit_stock_level_survives(self):
        c = classify_post("For $arqq, I want to buy again at its pullback to 19-20.",
                          assume_index=True)
        self.assertEqual(c.ticker_levels.get("ARQQ"), [19.0, 20.0])

    def test_commodity_etf_levels(self):
        c = classify_post("I bought some when $gdx dipped below 70. "
                          "$gdx 65-60 will be great buying opportunity.",
                          assume_index=True)
        # 顺序 = 文本出现顺序（70 在第一句，65-60 在第二句）。
        # 保持出现顺序有意义：读的人能对上原文。
        self.assertEqual(c.ticker_levels.get("GDX"), [70.0, 65.0, 60.0])

    def test_wave_ordinals_are_not_levels(self):
        """「wave-3」「5 waves」「第4浪」里的数字不是点位。

        放宽到两位数后这类噪音会灌进来（实测 $spcx 那条抽出 3/7/23/8/1）。"""
        c = classify_post("$abcd is in the wave-5 of C, forming 3 waves so far. "
                          "Target 133.", assume_index=True)
        self.assertEqual(c.ticker_levels.get("ABCD"), [133.0])

    def test_date_slash_not_a_level(self):
        c = classify_post("$xyz entered on 7/23 at the time of my post. Now 88.",
                          assume_index=True)
        self.assertNotIn(7.0, c.ticker_levels.get("XYZ", []))
        self.assertNotIn(23.0, c.ticker_levels.get("XYZ", []))
        self.assertIn(88.0, c.ticker_levels.get("XYZ", []))

    def test_index_tickers_excluded_from_ticker_map(self):
        """$SPX 走 levels，不进个股表——否则简报会把指数当个股列。"""
        c = classify_post("$spx needs to break above 7750")
        self.assertNotIn("SPX", c.ticker_levels)
        self.assertIn(7750.0, c.levels)

    def test_index_lower_bound_unchanged(self):
        """指数侧下限仍是 600，行为不能被这次放宽改掉。

        Mancini 的「a 400+ point vertical rally」里的 400 是点数不是点位。"""
        c = classify_post("ES rallied 400+ points off the 7325 low")
        self.assertNotIn(400.0, c.levels)
        self.assertIn(7325.0, c.levels)


class _Stop(BaseException):
    """跳出 run_loop 的哨兵。

    必须继承 BaseException：run_loop 里 `except Exception` 会把普通异常当成
    "本轮失败、下轮重试"吞掉，用 Exception 就停不下来。"""


class _FakeTime:
    """只替换 x_relay 模块里的 time 引用，不动全局 time 模块。"""

    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def time(self):
        return 1_800_000_000.0


class StructureAlertTest(unittest.TestCase):
    """2026-08-09 误报：单轮抖动报「抓取停摆」，自愈后无恢复通知。

    StructureError 是 25 秒选择器超时抛的——"X 改版了"和"这次页面加载慢"
    产生完全相同的信号，单轮区分不了。当天 10:15 那轮 4 个账号整齐地每
    31 秒失败一个，10:32 全部恢复，此后 12 小时正常；但告警一直挂在手机上，
    因为压根没有恢复通知。两个缺陷各自都能单独造成误导，分开测。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sent = []
        self.send_ok = True
        self._saved = {k: getattr(x_relay, k) for k in
                       ("ALERT_STATE", "tg_send", "run_once", "write_heartbeat",
                        "time", "JITTER_SECONDS", "log")}
        x_relay.ALERT_STATE = Path(self.dir) / "alert_state.json"
        x_relay.tg_send = self._tg_send
        x_relay.write_heartbeat = lambda **kw: None
        x_relay.log = lambda *a, **k: None
        x_relay.time = _FakeTime()
        x_relay.JITTER_SECONDS = 0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(x_relay, k, v)

    def _tg_send(self, text):
        if self.send_ok:
            self.sent.append(text)
        return self.send_ok

    @staticmethod
    def _ok(n=4):
        return {"ok": [f"a{i}" for i in range(n)], "fail": [],
                "structure_fail": [], "gap": [], "new": 0, "pushed": 0}

    @staticmethod
    def _struct():
        return {"ok": [], "fail": [], "structure_fail": ["a0", "a1"],
                "gap": [], "new": 0, "pushed": 0}

    def _drive(self, rounds):
        """用真正的 run_loop 跑给定轮次——测循环本身，不是它的复制品。"""
        seq = list(rounds)

        def fake_run_once(store, dry_run=False):
            if not seq:
                raise _Stop()
            return seq.pop(0)

        x_relay.run_once = fake_run_once
        try:
            x_relay.run_loop(None)
        except _Stop:
            pass

    def _alerts(self):
        return [m for m in self.sent if "抓取停摆" in m]

    def _recoveries(self):
        return [m for m in self.sent if "抓取已恢复" in m]

    def test_single_round_does_not_alert(self):
        """就是 08-09 那次误报的直接回归：一轮全灭 + 下轮恢复 = 不该告警。"""
        self._drive([self._struct(), self._ok()])
        self.assertEqual(self._alerts(), [], "单轮抖动不能报「抓取停摆」")
        self.assertEqual(self._recoveries(), [], "没告警过就不该发恢复通知")

    def test_two_consecutive_rounds_alert(self):
        self._drive([self._struct(), self._struct()])
        self.assertEqual(len(self._alerts()), 1, "连续两轮全灭必须告警一次")
        self.assertIn("30 分钟", self._alerts()[0], "告警要说明已持续多久")

    def test_recovery_notifies_once_and_clears_state(self):
        self._drive([self._struct(), self._struct(), self._ok(), self._ok()])
        self.assertEqual(len(self._alerts()), 1)
        self.assertEqual(len(self._recoveries()), 1, "恢复只通知一次，不能每轮刷")
        state = json.loads(x_relay.ALERT_STATE.read_text())
        self.assertNotIn("structure", state,
                         "恢复后必须清状态，否则下次真故障被 6h 冷却压掉")

    def test_partial_success_is_healthy(self):
        """个别账号失败但有账号抓到 = 抓取通路是好的，不是停摆。"""
        partial = {"ok": ["a0"], "fail": [], "structure_fail": ["a1"],
                   "gap": [], "new": 0, "pushed": 0}
        self._drive([partial, partial, partial])
        self.assertEqual(self._alerts(), [])

    def test_failed_send_keeps_alert_state(self):
        """恢复通知发不出去时不能清状态——否则这条事实被静默丢掉。"""
        x_relay.ALERT_STATE.write_text(json.dumps({"structure": 1.0}))
        self.send_ok = False
        self.assertFalse(x_relay.resolve_alert("structure", "✅ 恢复"))
        self.assertIn("structure", json.loads(x_relay.ALERT_STATE.read_text()))

    def test_resolve_is_noop_when_never_alerted(self):
        x_relay.ALERT_STATE.write_text(json.dumps({}))
        self.assertFalse(x_relay.resolve_alert("structure", "✅ 恢复"))
        self.assertEqual(self.sent, [])

    def test_once_path_does_not_alert(self):
        """`--once --dry-run` 正是告警文案让人跑的诊断命令，它自己不能再报警。"""
        src = (Path(__file__).resolve().parent.parent
               / "x_relay.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "run_once")
        keys = [c.args[0].value for c in ast.walk(fn)
                if isinstance(c, ast.Call)
                and getattr(c.func, "id", None) == "alert_once"
                and c.args and isinstance(c.args[0], ast.Constant)]
        self.assertNotIn("structure", keys,
                         "结构失效告警需要跨轮去抖，必须留在 run_loop")


class DiagnosticSideEffectTest(unittest.TestCase):
    """人工一次性诊断不得改动生产监控状态。

    2026-08-09 实测：`pm2 stop x-levels-relay` 之后跑一次 `--once`，主仓库的
    data/runtime/heartbeat/x-levels-relay.json 立刻变成 connected=true 的新鲜
    记录——进程明明停着。watchdog 对"pm2 online 但进程卡死"的唯一探测手段就是
    心跳新鲜度（health_watchdog.py::bot_health_failure），而卡死时人做的第一件
    事，正是按告警文案提示跑 `--once --dry-run`：一跑就把卡死信号抹掉。"""

    @staticmethod
    def _fn(name):
        src = (Path(__file__).resolve().parent.parent
               / "x_relay.py").read_text(encoding="utf-8")
        return next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    @staticmethod
    def _run_once_calls(fn):
        return [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                and getattr(c.func, "id", None) == "run_once"]

    def test_once_disables_heartbeat(self):
        calls = self._run_once_calls(self._fn("main"))
        self.assertEqual(len(calls), 1, "main() 里应只有 --once 那一处 run_once")
        kw = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("write_hb", kw, "--once 必须显式关掉心跳写入")
        self.assertIs(kw["write_hb"].value, False)

    def test_loop_keeps_heartbeat(self):
        """守护进程路径必须照常写心跳，否则 watchdog 反过来彻底瞎了。"""
        for call in self._run_once_calls(self._fn("run_loop")):
            for k in call.keywords:
                self.assertNotEqual(
                    (k.arg, getattr(k.value, "value", None)), ("write_hb", False),
                    "run_loop 关掉心跳 = watchdog 再也发现不了这个进程卡死")

    def test_default_is_to_write(self):
        import inspect
        p = inspect.signature(x_relay.run_once).parameters["write_hb"]
        self.assertIs(p.default, True, "默认必须写——只有诊断路径才显式关")


if __name__ == "__main__":
    unittest.main()
