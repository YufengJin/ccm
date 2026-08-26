import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.config import Registry
from ccm.profiles import Profile
from ccm.pricing import resolve_price, price_event
from ccm.cost import CostDB, scan_projects, aggregate


def _line(sid, rid, model="claude-opus-5", in_t=10, out_t=20, cr=100, c5=5, c1=7,
          ts="2026-08-26T10:00:00Z"):
    return json.dumps({
        "sessionId": sid, "requestId": rid, "uuid": f"u-{rid}",
        "timestamp": ts,
        "message": {"model": model, "usage": {
            "input_tokens": in_t, "output_tokens": out_t,
            "cache_read_input_tokens": cr,
            "cache_creation": {"ephemeral_5m_input_tokens": c5,
                               "ephemeral_1h_input_tokens": c1}}}}) + "\n"


class TestPricing(unittest.TestCase):
    def test_longest_prefix(self):
        self.assertEqual(resolve_price("claude-opus-5")["in"], 5.0)
        self.assertEqual(resolve_price("claude-opus-4-8")["in"], 5.0)
        self.assertEqual(resolve_price("claude-sonnet-4-6")["in"], 3.0)
        self.assertIsNone(resolve_price("gpt-9"))

    def test_price_event_million_baseline(self):
        # 1M input token 恰好等于表内单价(codex: 公式必须 /1e6)
        cost = price_event("claude-opus-5", 1_000_000, 0, 0, 0, 0)
        self.assertAlmostEqual(cost, 5.0)
        cost = price_event("claude-opus-5", 0, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 25.0)
        # 缓存倍率 0.1 / 1.25 / 2.0
        self.assertAlmostEqual(price_event("claude-opus-5", 0, 0, 1_000_000, 0, 0), 0.5)
        self.assertAlmostEqual(price_event("claude-opus-5", 0, 0, 0, 1_000_000, 0), 6.25)
        self.assertAlmostEqual(price_event("claude-opus-5", 0, 0, 0, 0, 1_000_000), 10.0)
        self.assertIsNone(price_event("unknown-model", 1, 1, 1, 1, 1))


class TestCostScan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-cost-"))
        self.env = make_fake_home(self.tmp)
        self.registry = Registry.empty(self.env)
        for name, base in (("default", ".claude"), ("work", ".claude-b")):
            self.registry.profiles[name] = Profile(name=name, path=self.tmp / base)
        self.proj = self.tmp / ".claude" / "projects" / "-home-user-Desktop"
        self.db = CostDB(self.env)

    def test_scan_attribution_and_incremental(self):
        f = self.proj / "sess-1111.jsonl"   # helpers 已放了 1 行
        f.write_text(_line("sess-1111", "r1") + _line("sess-1111", "r2"))
        n = scan_projects(self.env, self.registry, self.db)
        self.assertEqual(n, 2)
        # sess-1111 在 default 的 session-env → 归 default
        rows = aggregate(self.db, by="profile")
        self.assertEqual({r["key"]: r["events"] for r in rows}, {"default": 2})
        # 增量:追加 1 行只解析 1 行
        with open(f, "a") as fh:
            fh.write(_line("sess-2222", "r3"))
        self.assertEqual(scan_projects(self.env, self.registry, self.db), 1)
        rows = {r["key"]: r["events"] for r in aggregate(self.db, by="profile")}
        self.assertEqual(rows, {"default": 2, "work": 1})   # sess-2222 → work

    def test_half_line_not_consumed(self):
        f = self.proj / "sess-1111.jsonl"
        f.write_text(_line("sess-1111", "r1") + '{"half')   # 写入方残留半行
        self.assertEqual(scan_projects(self.env, self.registry, self.db), 1)
        with open(f, "a") as fh:
            fh.write('-line", "requestId": "rX"}\n')        # 补全(仍不是合法事件,忽略)
        self.assertEqual(scan_projects(self.env, self.registry, self.db), 0)
        f2 = self.proj / "sess-1111.jsonl"
        with open(f2, "a") as fh:
            fh.write(_line("sess-1111", "r9"))
        self.assertEqual(scan_projects(self.env, self.registry, self.db), 1)

    def test_replace_rescans_without_double_count(self):
        f = self.proj / "sess-1111.jsonl"
        f.write_text(_line("sess-1111", "r1"))
        scan_projects(self.env, self.registry, self.db)
        # 原子替换(ino 变)且内容重写
        f2 = f.with_name("tmp")
        f2.write_text(_line("sess-1111", "r1") + _line("sess-1111", "r2"))
        os.replace(f2, f)
        scan_projects(self.env, self.registry, self.db)
        rows = aggregate(self.db, by="profile")
        self.assertEqual(sum(r["events"] for r in rows), 2)  # 不重复计数

    def test_unknown_and_ambiguous(self):
        f = self.proj / "sess-9999.jsonl"
        f.write_text(_line("sess-9999", "r1"))
        scan_projects(self.env, self.registry, self.db)
        rows = {r["key"]: r for r in aggregate(self.db, by="profile")}
        self.assertIn("unknown", rows)                       # 无 session-env 映射
        # 两个 profile 都出现同一 sid → ambiguous(跨账号 resume)
        (self.tmp / ".claude" / "session-env" / "sess-9999").mkdir()
        (self.tmp / ".claude-b" / "session-env" / "sess-9999").mkdir()
        scan_projects(self.env, self.registry, self.db)
        rows = {r["key"]: r for r in aggregate(self.db, by="profile")}
        self.assertIn("ambiguous", rows)
        self.assertNotIn("unknown", rows)

    def test_aggregate_by_model_cost(self):
        f = self.proj / "sess-1111.jsonl"
        f.write_text(_line("sess-1111", "r1", in_t=1_000_000, out_t=0, cr=0, c5=0, c1=0))
        scan_projects(self.env, self.registry, self.db)
        rows = {r["key"]: r for r in aggregate(self.db, by="model")}
        self.assertAlmostEqual(rows["claude-opus-5"]["cost_usd"], 5.0)


if __name__ == "__main__":
    unittest.main()
