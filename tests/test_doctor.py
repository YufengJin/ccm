import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.migrate import build_plan, execute_migration
from ccm.config import Registry, load_state, save_state
from ccm.doctor import run_checks


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-doc-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))
        self.registry = Registry.load(self.env)

    def by_check(self, results):
        out = {}
        for r in results:
            out.setdefault(r.check, []).append(r)
        return out

    def test_healthy_after_migration(self):
        results = run_checks(self.env, self.registry, load_state(self.env))
        fails = [r for r in results if r.level == "fail"]
        self.assertEqual(fails, [], [f"{r.check}:{r.msg}" for r in fails])
        # 遗留 ~/.claude.json 是 warn 级 note
        legacy = [r for r in results if r.check == "legacy-claude-json"]
        self.assertEqual(legacy[0].level, "warn")

    def test_broken_link_detected_and_fixed(self):
        victim = self.tmp / ".claude-accounts/a2/settings.json"
        os.unlink(victim)
        results = run_checks(self.env, self.registry, load_state(self.env))
        bad = [r for r in results if r.check == "shared-links" and r.level == "fail"]
        self.assertTrue(bad)
        results = run_checks(self.env, self.registry, load_state(self.env), fix=True)
        fixed = [r for r in results if r.check == "shared-links" and r.fixed]
        self.assertTrue(fixed)
        self.assertTrue(victim.is_symlink())

    def test_conflict_reported_never_fixed(self):
        victim = self.tmp / ".claude-accounts/a2/settings.json"
        os.unlink(victim)
        victim.write_text('{"private": 1}')
        results = run_checks(self.env, self.registry, load_state(self.env), fix=True)
        conf = [r for r in results if r.check == "shared-links" and "conflict" in r.msg]
        self.assertTrue(conf)
        self.assertEqual(victim.read_text(), '{"private": 1}')  # 绝不覆盖

    def test_compat_link_fix(self):
        os.unlink(self.tmp / ".claude-b")
        results = run_checks(self.env, self.registry, load_state(self.env), fix=True)
        c = [r for r in results if r.check == "compat-link" and r.fixed]
        self.assertTrue(c)
        self.assertEqual(os.readlink(self.tmp / ".claude-b"),
                         str(self.tmp / ".claude-accounts/a3"))

    def test_state_fallback(self):
        save_state(self.env, "ghost", "test")
        results = run_checks(self.env, self.registry, load_state(self.env), fix=True)
        st = [r for r in results if r.check == "state"]
        self.assertTrue(st[0].fixed)
        self.assertEqual(load_state(self.env)["active"], "a1")


if __name__ == "__main__":
    unittest.main()
