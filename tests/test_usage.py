import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from tests.helpers import make_fake_home, ACCT_A, ACCT_B
from ccm.cli import main
from ccm.config import Registry
from ccm.profiles import Profile
from ccm.usage import gather_usage, extract_pcts
from ccm.render import table


def live_payload():
    return {"five_hour": {"utilization": 6.0, "resets_at": "2026-08-26T15:50:00+00:00"},
            "seven_day": {"utilization": 29.0, "resets_at": "2026-08-28T07:00:00+00:00"},
            "limits": [
                {"kind": "session", "percent": 6, "severity": "normal",
                 "resets_at": "2026-08-26T15:50:00+00:00", "is_active": False},
                {"kind": "weekly_all", "percent": 29, "severity": "normal",
                 "resets_at": "2026-08-28T07:00:00+00:00", "is_active": False}]}


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-usage-"))

    def _registry(self, env):
        r = Registry.empty(env)
        r.profiles["default"] = Profile(name="default", path=self.tmp / ".claude",
                                        account_uuid=ACCT_A)
        r.profiles["personal"] = Profile(name="personal", path=self.tmp / ".claude-a",
                                         account_uuid=ACCT_A)
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-b",
                                     account_uuid=ACCT_B)
        return r

    def test_dedup_one_request_per_account(self):
        env = make_fake_home(self.tmp)
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            return _Resp(live_payload())

        rows = gather_usage(env, self._registry(env), opener=opener)
        self.assertEqual(len(calls), 2)  # 3 profile / 2 account
        self.assertEqual(len(rows), 2)
        by_uuid = {r.account_uuid: r for r in rows}
        self.assertEqual(sorted(by_uuid[ACCT_A].profiles), ["default", "personal"])
        self.assertEqual(by_uuid[ACCT_A].source, "live")
        self.assertEqual(by_uuid[ACCT_A].five_hour_pct, 6)
        self.assertEqual(by_uuid[ACCT_A].seven_day_pct, 29)

    def test_cache_fallback_when_all_expired(self):
        env = make_fake_home(self.tmp, a_expired=True, b_expired=True,
                             default_expired=True)
        def opener(req, timeout=None):
            raise AssertionError("过期 token 不应发请求")
        rows = gather_usage(env, self._registry(env), opener=opener)
        by_uuid = {r.account_uuid: r for r in rows}
        w = by_uuid[ACCT_B]
        self.assertEqual(w.source, "cache")       # b 有 cachedUsageUtilization
        self.assertEqual(w.five_hour_pct, 12)
        self.assertGreater(w.cache_age_s, 2 * 3600)
        self.assertEqual(by_uuid[ACCT_A].source, "unavailable")  # a/default 无缓存

    def test_api_error_degrades_not_raises(self):
        env = make_fake_home(self.tmp)
        def opener(req, timeout=None):
            import urllib.error
            raise urllib.error.URLError("net down")
        rows = gather_usage(env, self._registry(env), opener=opener)
        self.assertEqual({r.source for r in rows} - {"cache", "unavailable"}, set())

    def test_grouping_uses_fresh_oauth_account(self):
        # 注册表 uuid 陈旧(写成同一个),但 .claude.json 现读应分成两组
        env = make_fake_home(self.tmp)
        r = Registry.empty(env)
        r.profiles["personal"] = Profile(name="personal", path=self.tmp / ".claude-a",
                                         account_uuid="stale-X")
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-b",
                                     account_uuid="stale-X")
        def opener(req, timeout=None):
            return _Resp(live_payload())
        rows = gather_usage(env, r, opener=opener)
        self.assertEqual(sorted(x.account_uuid for x in rows), [ACCT_A, ACCT_B])

    def test_extract_pcts_fallback_objects(self):
        p = {"five_hour": {"utilization": 3.0, "resets_at": None},
             "seven_day": {"utilization": 5.0, "resets_at": None}}
        got = extract_pcts(p)
        self.assertEqual((got["five_hour_pct"], got["seven_day_pct"]), (3, 5))

    def test_render_table_alignment(self):
        out = table(["A", "BB"], [["1", "2"], ["333", "4"]])
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("1"))


class TestUsageCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-ucli-"))
        self.env = make_fake_home(self.tmp)
        r = Registry.empty(self.env)
        r.profiles["default"] = Profile(name="default", path=self.tmp / ".claude")
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-b")
        r.save(self.env)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = os.environ.get("CCM_USER_HOME")
        os.environ["CCM_USER_HOME"] = str(self.tmp)
        # 禁网:任何真实请求直接炸
        import ccm.usage as U
        orig = U._DEFAULT_OPENER
        def no_net(req, timeout=None):
            import urllib.error
            raise urllib.error.URLError("测试禁网")  # 模拟离线,walk 降级路径
        U._DEFAULT_OPENER = no_net
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(list(argv))
        finally:
            U._DEFAULT_OPENER = orig
            if old is None:
                os.environ.pop("CCM_USER_HOME", None)
            else:
                os.environ["CCM_USER_HOME"] = old
        return rc, out.getvalue(), err.getvalue()

    def test_usage_all_json(self):
        rc, out, _ = self.run_cli("usage", "--all", "--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)

    def test_ls_local_only(self):
        rc, out, _ = self.run_cli("ls")
        self.assertEqual(rc, 0)
        self.assertIn("work", out)
        self.assertIn("work@example.com", out)


if __name__ == "__main__":
    unittest.main()
