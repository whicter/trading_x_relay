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
    def test_mancini_style_es_levels(self):
        c = classify_post("ES recovered 6900. Bulls need to hold 6885, "
                          "below that 6862 then 6841.")
        self.assertEqual(c.label, "index_levels")
        self.assertEqual(c.levels, [6900.0, 6885.0, 6862.0, 6841.0])

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
