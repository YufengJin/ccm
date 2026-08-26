import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.config import Registry
from ccm.errors import CcmError, ProfileNotFound
from ccm.profiles import Profile
from ccm.selector import resolve_profile, next_auto_id


class TestSelector(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-sel-"))
        self.env = make_fake_home(self.tmp)   # a: jyf@ / b: work@
        self.registry = Registry.empty(self.env)
        self.registry.default_profile = "a1"
        for pid, base in (("a1", ".claude"), ("a2", ".claude-a"), ("a3", ".claude-b")):
            self.registry.profiles[pid] = Profile(name=pid, path=self.tmp / base)

    def test_exact_id(self):
        self.assertEqual(resolve_profile(self.env, self.registry, "a3").name, "a3")

    def test_bare_number(self):
        self.assertEqual(resolve_profile(self.env, self.registry, "3").name, "a3")

    def test_exact_email(self):
        p = resolve_profile(self.env, self.registry, "work@example.com")
        self.assertEqual(p.name, "a3")

    def test_email_prefix_case_insensitive(self):
        self.assertEqual(resolve_profile(self.env, self.registry, "WORK").name, "a3")
        self.assertEqual(resolve_profile(self.env, self.registry, "work@ex").name, "a3")

    def test_same_account_multi_profile_picks_best(self):
        # jyf 匹配 a1+a2(同账号):a1 token 有效 → a1;a1 过期则 a2
        self.assertEqual(resolve_profile(self.env, self.registry, "jyf").name, "a1")
        env2 = make_fake_home(Path(tempfile.mkdtemp()), default_expired=True)
        reg2 = Registry.empty(env2)
        reg2.default_profile = "a1"
        for pid, base in (("a1", ".claude"), ("a2", ".claude-a")):
            reg2.profiles[pid] = Profile(name=pid, path=env2.user_home / base)
        self.assertEqual(resolve_profile(env2, reg2, "jyf").name, "a2")

    def test_uuid_prefix(self):
        self.assertEqual(resolve_profile(self.env, self.registry, "acct-B").name, "a3")

    def test_ambiguous_cross_account_raises(self):
        # "example" 同时命中 acct-A 与 acct-B → 拒绝并列出候选
        with self.assertRaises(CcmError) as cm:
            resolve_profile(self.env, self.registry, "example")
        self.assertIn("a1", str(cm.exception))
        self.assertIn("a3", str(cm.exception))

    def test_not_found_lists_available(self):
        with self.assertRaises(ProfileNotFound) as cm:
            resolve_profile(self.env, self.registry, "nobody")
        self.assertIn("a1", str(cm.exception))

    def test_short_selector_no_substring(self):
        # <3 字符不做子串匹配,避免 "a" 之类误命中
        with self.assertRaises(ProfileNotFound):
            resolve_profile(self.env, self.registry, "jy")

    def test_next_auto_id(self):
        self.assertEqual(next_auto_id(self.registry), "a4")
        r = Registry.empty(self.env)
        self.assertEqual(next_auto_id(r), "a1")
        r.profiles["a1"] = Profile(name="a1", path=self.tmp / "x")
        r.profiles["a3"] = Profile(name="a3", path=self.tmp / "y")
        self.assertEqual(next_auto_id(r), "a2")   # 补洞


if __name__ == "__main__":
    unittest.main()


class TestSelectorCli(unittest.TestCase):
    """CLI 端到端:迁移后用 email / 序号 / uuid 前缀切换。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-selcli-"))
        self.env = make_fake_home(self.tmp)
        from ccm.migrate import build_plan, execute_migration
        execute_migration(self.env, build_plan(self.env))

    def run_cli(self, *argv):
        import io as _io
        import os
        from contextlib import redirect_stdout, redirect_stderr
        from ccm.cli import main
        out, err = _io.StringIO(), _io.StringIO()
        (self.tmp / "emptyproc").mkdir(exist_ok=True)
        old = {k: os.environ.get(k) for k in ("CCM_USER_HOME", "CCM_PROC_ROOT")}
        os.environ["CCM_USER_HOME"] = str(self.tmp)
        os.environ["CCM_PROC_ROOT"] = str(self.tmp / "emptyproc")   # 隔离真机进程
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(list(argv))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue(), err.getvalue()

    def test_use_by_email_then_number_then_uuid(self):
        rc, _, err = self.run_cli("use", "work@example.com")
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("current", "--quiet")
        self.assertEqual(out.strip(), "a3")
        rc, _, _ = self.run_cli("use", "1")
        rc, out, _ = self.run_cli("current", "--quiet")
        self.assertEqual(out.strip(), "a1")
        rc, _, _ = self.run_cli("use", "acct-B")
        rc, out, _ = self.run_cli("current", "--quiet")
        self.assertEqual(out.strip(), "a3")

    def test_use_same_account_prefix_picks_valid_token(self):
        rc, _, err = self.run_cli("use", "jyf")   # a1+a2 同账号 → a1(token 有效+默认)
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("current", "--quiet")
        self.assertEqual(out.strip(), "a1")

    def test_cross_account_ambiguity_rc1(self):
        rc, _, err = self.run_cli("use", "example")   # 两个账号都命中
        self.assertEqual(rc, 1)
        self.assertIn("a1", err)
        self.assertIn("a3", err)

    def test_add_auto_id_then_show_by_selector(self):
        rc, out, _ = self.run_cli("add")
        self.assertEqual(rc, 0)
        self.assertIn("a4", out)
        rc, out, _ = self.run_cli("show", "a4", "--json")
        self.assertEqual(rc, 0)

    def test_rename_default_moves_pointer(self):
        # 默认 profile 允许改名,指针与 ~/.claude 链接同步
        rc, out, err = self.run_cli("rename", "a1", "main")
        self.assertEqual(rc, 0, err)
        import json, os
        from ccm.config import Registry, Env
        r = Registry.load(Env.from_environ({"CCM_USER_HOME": str(self.tmp)}))
        self.assertEqual(r.default_profile, "main")
        self.assertEqual(os.readlink(self.tmp / ".claude"),
                         str(self.env.accounts_root / "main"))
