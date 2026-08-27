import os
import tempfile
import unittest
from pathlib import Path

import ccm.migrate as M
from ccm.migrate import build_plan, execute_migration, rollback_ops, Journal
from ccm.config import Registry, load_state
from ccm.errors import MigrationAborted
from tests.helpers import make_fake_home


class TestMigrateE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-e2e-"))
        self.env = make_fake_home(self.tmp)

    def snapshot(self):
        """经旧路径递归读取全部内容(跟随 symlink 进目录),字典形式。

        rglob 不进 symlink 目录,迁移后共享项变 symlink 会让内容凭空"消失",
        故用 os.walk(followlinks=True):比较的是"经旧路径能读到什么"。
        """
        out = {}
        for base in (".claude", ".claude-a", ".claude-b"):
            top = self.tmp / base
            for root, dirs, files in os.walk(top, followlinks=True):
                rel_root = os.path.relpath(root, self.tmp)
                for d in dirs:
                    out[os.path.join(rel_root, d)] = "dir"
                for f in files:
                    fp = Path(root) / f
                    out[os.path.join(rel_root, f)] = fp.read_bytes()
        return out

    def test_full_migration_then_rollback(self):
        before = self.snapshot()
        ino = os.stat(self.tmp / ".claude-b" / ".credentials.json").st_ino
        plan = build_plan(self.env)
        self.assertEqual(sorted(m["profile"] for m in plan["moves"]),
                         ["a1", "a2", "a3"])
        execute_migration(self.env, plan)
        # 新布局
        self.assertTrue((self.tmp / ".claude-accounts/a3/.credentials.json").is_file())
        self.assertTrue((self.tmp / ".claude-shared/settings.json").is_file())
        self.assertTrue((self.tmp / ".claude-b").is_symlink())
        # 经旧路径访问逐字节不变
        self.assertEqual(self.snapshot(), before)
        # inode 不变
        self.assertEqual(
            os.stat(self.tmp / ".claude-b" / ".credentials.json").st_ino, ino)
        # 注册表 + 身份回填(default 走 legacy 回退) + state
        r = Registry.load(self.env)
        self.assertEqual(set(r.profiles), {"a1", "a2", "a3"})
        self.assertEqual(r.get("a1").account_uuid, "acct-A")
        self.assertEqual(r.get("a3").account_uuid, "acct-B")
        self.assertEqual(load_state(self.env)["active"], "a1")
        # 各 profile 共享项直连 shared
        self.assertEqual(
            os.readlink(self.tmp / ".claude-accounts/a2/settings.json"),
            str(self.tmp / ".claude-shared/settings.json"))
        # 备份存在且 0600
        bks = list((self.env.ccm_home / "backups").glob("pre-migrate-*.tar.gz"))
        self.assertEqual(len(bks), 1)
        self.assertEqual(os.stat(bks[0]).st_mode & 0o777, 0o600)
        # journal 已清(成功后不留)
        self.assertFalse((self.env.ccm_home / "logs/migrate-journal.json").exists())

    def test_migration_rollback_restores(self):
        before = self.snapshot()
        plan = build_plan(self.env)
        # 保留 journal 以便回滚:打桩 doctor 让阶段 6 失败
        orig = M._doctor_gate
        M._doctor_gate = lambda env, plan=None: ["注入的失败"]
        try:
            with self.assertRaises(MigrationAborted):
                execute_migration(self.env, plan)
        finally:
            M._doctor_gate = orig
        self.assertFalse((self.tmp / ".claude-b").is_symlink())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.env.ccm_home / "logs/migrate-journal.json").exists())

    def test_preflight_rejects_nonempty_target(self):
        (self.tmp / ".claude-accounts").mkdir()
        (self.tmp / ".claude-accounts" / "junk").write_text("x")
        with self.assertRaises(MigrationAborted):
            build_plan(self.env)

    def test_dry_run_touches_nothing(self):
        before = self.snapshot()
        plan = build_plan(self.env)
        self.assertIn("moves", plan)
        self.assertIn("splits", plan)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.env.ccm_home / "profiles.json").exists())


if __name__ == "__main__":
    unittest.main()


class TestMigrateCli(unittest.TestCase):
    """CLI 层冒烟:dry-run 与 migrate --yes 的返回码与输出。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-mcli-"))
        self.env = make_fake_home(self.tmp)

    def run_cli(self, *argv):
        import io as _io
        from contextlib import redirect_stdout, redirect_stderr
        from ccm.cli import main
        out, err = _io.StringIO(), _io.StringIO()
        old = os.environ.get("CCM_USER_HOME")
        os.environ["CCM_USER_HOME"] = str(self.tmp)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(list(argv))
        finally:
            if old is None:
                os.environ.pop("CCM_USER_HOME", None)
            else:
                os.environ["CCM_USER_HOME"] = old
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_then_real_then_doctor(self):
        rc, out, _ = self.run_cli("migrate", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        self.assertFalse((self.tmp / ".claude-b").is_symlink())
        rc, out, _ = self.run_cli("migrate", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.tmp / ".claude-b").is_symlink())
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 0, out)   # 无 fail
        rc, out, _ = self.run_cli("migrate", "--rollback")
        self.assertEqual(rc, 0)
        self.assertIn("无未完结", out)  # 成功迁移后 journal 已清


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-cln-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))

    def test_cleanup_keeps_default_removes_ab(self):
        from ccm.migrate import cleanup
        (self.tmp / ".bashrc").write_text("cca() { :; }\nalias claude-a='x'\nother\n")
        actions = cleanup(self.env)
        # ~/.claude 必须保留(未设 CLAUDE_CONFIG_DIR 时 claude 的默认落点)
        self.assertTrue((self.tmp / ".claude").is_symlink())
        self.assertFalse(os.path.lexists(self.tmp / ".claude-a"))
        self.assertFalse(os.path.lexists(self.tmp / ".claude-b"))
        # 注册表同步:被清的 compat_link 置空,default 保留
        r = Registry.load(self.env)
        self.assertIsNone(r.get("a3").compat_link)
        self.assertIsNone(r.get("a2").compat_link)
        self.assertIsNotNone(r.get("a1").compat_link)
        # .bashrc 只报告不改
        self.assertIn("cca()", (self.tmp / ".bashrc").read_text())
        self.assertTrue(any("cca" in a for a in actions))
        # cleanup 后 doctor 无 fail
        from ccm.doctor import run_checks
        from ccm.config import load_state
        fails = [c for c in run_checks(self.env, Registry.load(self.env),
                                       load_state(self.env)) if c.level == "fail"]
        self.assertEqual(fails, [], [f"{c.check}:{c.msg}" for c in fails])
