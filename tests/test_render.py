import unittest

from ccm.render import visual_width, pad, bar, table, colorize
from ccm.usage import AccountRow
from ccm.render import render_usage


def rows():
    return [
        AccountRow(account_uuid="acct-A", email="alice@example.com",
                   profiles=["a1"], source="live",
                   five_hour_pct=75, seven_day_pct=20,
                   five_hour_resets="1m", seven_day_resets="137h01m"),
        AccountRow(account_uuid="acct-B", email="bob@example.com",
                   profiles=["a3"], source="live",
                   five_hour_pct=4, seven_day_pct=85,
                   five_hour_resets="4h41m", seven_day_resets="38h51m"),
    ]


class TestWidth(unittest.TestCase):
    def test_visual_width_cjk(self):
        self.assertEqual(visual_width("已过期"), 6)
        self.assertEqual(visual_width("7h"), 2)
        self.assertEqual(visual_width("\x1b[31m已过期\x1b[0m"), 6)  # ANSI 不计宽

    def test_pad_aligns_cjk(self):
        self.assertEqual(visual_width(pad("已过期", 10)), 10)
        self.assertEqual(visual_width(pad("ok", 10)), 10)

    def test_table_cjk_alignment(self):
        out = table(["a", "b"], [["已过期", "x"], ["okk", "y"]])
        l1, l2 = out.splitlines()[1:3]
        self.assertEqual(visual_width(l1[:l1.index("x")]),
                         visual_width(l2[:l2.index("y")]))


class TestBar(unittest.TestCase):
    def test_bar_proportions(self):
        b = bar(35, width=20, color=False)
        self.assertEqual(len(b), 20)
        self.assertEqual(b.count("▓"), 7)
        self.assertEqual(b.count("░"), 13)
        self.assertEqual(bar(0, width=20, color=False), "░" * 20)
        self.assertEqual(bar(100, width=20, color=False), "▓" * 20)
        self.assertEqual(bar(120, width=20, color=False), "▓" * 20)  # clamp

    def test_bar_threshold_colors(self):
        self.assertIn("\x1b[32m", bar(30, color=True))   # 绿
        self.assertIn("\x1b[33m", bar(60, color=True))   # 黄
        self.assertIn("\x1b[31m", bar(85, color=True))   # 红

    def test_colorize_off_is_identity(self):
        self.assertEqual(colorize("x", "31", False), "x")


class TestRenderUsage(unittest.TestCase):
    def test_plain_contains_bars_and_marks(self):
        out = render_usage(rows(), active_uuid="acct-B", color=False)
        self.assertIn("▓", out)
        self.assertIn("75%", out)
        self.assertIn("85%", out)
        self.assertIn("alice@example.com", out)
        self.assertIn("● bob@example.com", out)      # 活跃账号标记
        self.assertNotIn("● alice", out)
        self.assertIn("1m 后重置", out)
        self.assertNotIn("\x1b[", out)               # 无色模式无 ANSI

    def test_active_account_sorts_first(self):
        out = render_usage(rows(), active_uuid="acct-B", color=False)
        self.assertLess(out.index("bob@"), out.index("alice@"))

    def test_cache_and_unavailable(self):
        rs = rows()
        rs[0].source = "cache"
        rs[0].cache_age_s = 3 * 3600 + 120
        rs[1].source = "unavailable"
        rs[1].five_hour_pct = None
        rs[1].seven_day_pct = None
        out = render_usage(rs, active_uuid=None, color=False)
        self.assertIn("缓存·3h前", out)
        self.assertIn("不可用", out)
        self.assertIn("ccm refresh", out)            # 修复提示
        self.assertIn("░" * 20, out)                 # 无数据空条

    def test_color_mode_has_ansi(self):
        out = render_usage(rows(), active_uuid=None, color=True)
        self.assertIn("\x1b[31m", out)               # 85% 红条
        self.assertIn("\x1b[0m", out)


if __name__ == "__main__":
    unittest.main()
