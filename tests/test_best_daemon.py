import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import make_fake_home, ACCT_A, ACCT_B
from ccm.config import Registry, Env
from ccm.cost import CostDB
from ccm.daemon import run_sampler, record_samples, history, latest_sample
from ccm.errors import CcmError
from ccm.profiles import Profile
from ccm.usage import AccountRow, pick_best, statusline_text


def rows2():
    return [
        AccountRow(account_uuid=ACCT_A, email="a@x", profiles=["default", "personal"],
                   source="live", five_hour_pct=75, seven_day_pct=20),
        AccountRow(account_uuid=ACCT_B, email="b@x", profiles=["work"],
                   source="live", five_hour_pct=4, seven_day_pct=35),
    ]


class TestBest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-best-"))
        self.env = make_fake_home(self.tmp)
        self.registry = Registry.empty(self.env)
        for n, b in (("default", ".claude"), ("personal", ".claude-a"),
                     ("work", ".claude-b")):
            self.registry.profiles[n] = Profile(name=n, path=self.tmp / b)

    def test_pick_lowest_max_pct(self):
        # A: max(75,20)=75; B: max(4,35)=35 → B 更宽裕
        name, reason = pick_best(rows2(), self.registry, self.env)
        self.assertEqual(name, "work")
        self.assertIn("35", reason)

    def test_tie_break_by_seven_day(self):
        rows = rows2()
        rows[0].five_hour_pct = 35
        rows[0].seven_day_pct = 10   # A: max=35 tie, 7d 10 < 35 → A
        name, _ = pick_best(rows, self.registry, self.env)
        self.assertIn(name, ("default", "personal"))

    def test_unavailable_excluded(self):
        rows = rows2()
        rows[1].source = "unavailable"
        rows[1].five_hour_pct = None
        name, _ = pick_best(rows, self.registry, self.env)
        self.assertIn(name, ("default", "personal"))
        rows[0].source = "unavailable"
        rows[0].five_hour_pct = None
        with self.assertRaises(CcmError):
            pick_best(rows, self.registry, self.env)

    def test_statusline_text(self):
        txt = statusline_text("work", rows2()[1])
        self.assertEqual(txt, "work 5h:4% 7d:35%")
        self.assertEqual(statusline_text("work", None), "work 5h:? 7d:?")


class TestDaemonSampling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-dmn-"))
        self.env = make_fake_home(self.tmp)
        r = Registry.empty(self.env)
        for n, b in (("default", ".claude"), ("work", ".claude-b")):
            r.profiles[n] = Profile(name=n, path=self.tmp / b)
        r.save(self.env)
        self.db = CostDB(self.env)

    def test_record_and_history_and_latest(self):
        record_samples(self.db, rows2(), now=1000)
        record_samples(self.db, rows2(), now=90000)
        h = history(self.db, days=7, now=100000)
        self.assertTrue(h)
        latest = latest_sample(self.db, ACCT_B)
        self.assertEqual(latest["five_h"], 4)
        self.assertEqual(latest["ts"], 90000)

    def test_run_sampler_iterations(self):
        calls = []
        def fake_gather(env, registry, opener=None):
            calls.append(1)
            return rows2()
        n = run_sampler(self.env, self.db, interval=0, iterations=3,
                        gather=fake_gather, sleep=lambda s: None)
        self.assertEqual(n, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.db.conn.execute(
            "SELECT * FROM samples").fetchall()), 6)


if __name__ == "__main__":
    unittest.main()


class TestDaemonProcessSmoke(unittest.TestCase):
    """真实 spawn 冒烟:start → status → stop(隔离 CCM_HOME)。"""

    def test_start_status_stop(self):
        import subprocess
        import sys
        import time as _t
        tmp = Path(tempfile.mkdtemp(prefix="ccm-dspawn-"))
        env = make_fake_home(tmp)
        r = Registry.empty(env)
        r.profiles["work"] = Profile(name="work", path=tmp / ".claude-b")
        r.save(env)
        from ccm.daemon import daemon_start, daemon_status, daemon_stop
        pid = daemon_start(env, interval=3600)
        try:
            _t.sleep(0.8)
            info = daemon_status(env)
            self.assertIsNotNone(info)
            self.assertEqual(info["pid"], pid)
        finally:
            self.assertTrue(daemon_stop(env))
        _t.sleep(0.3)
        self.assertIsNone(daemon_status(env))
