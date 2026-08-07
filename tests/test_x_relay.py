"""x_relay 单测：分类器、台账幂等、推送门控、以及「绝不下单」的 AST 边界。"""
from __future__ import annotations

import ast
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

    def test_push_index_not_stock(self):
        now = datetime.now(timezone.utc)
        idx = classify_post("ES 6900 key")
        stock = classify_post("$MRNA to 160")
        self.assertTrue(x_relay.should_push(self._post(), idx, now))
        self.assertFalse(x_relay.should_push(self._post(), stock, now))

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

    def test_rows_without_identifier_skipped(self):
        """author 块里也有 identifier；解析若没排除 author 会串号——
        这里保证没有 post_id 的行安静跳过而不是崩。"""
        rows = [{"post_id": None, "published_at": None, "text": "",
                 "author": None, "n_images": 0, "head": ""}]
        self.assertEqual(x_relay.extract_posts(self.FakePage(rows), "h"), [])


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


if __name__ == "__main__":
    unittest.main()
