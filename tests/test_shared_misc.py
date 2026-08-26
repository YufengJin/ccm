import io as _io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.cli import main
from ccm.config import Registry
from ccm.errors import CcmError
from ccm.migrate import build_plan, execute_migration
from ccm.sharing import shared_add, shared_rm, unlink_item, diff_profiles


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-shr-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))
        self.registry = Registry.load(self.env)

    def run_cli(self, *argv):
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


class TestShared(Base):
    def test_shared_add_adopt_from_profile(self):
        # work 里造一个私有 keybindings.json,收编为共享项
        src = self.env.accounts_root / "a3" / "keybindings.json"
        src.write_text('{"key": 1}')
        shared_add(self.env, self.registry, "keybindings.json", from_profile="a3")
        self.assertTrue((self.env.shared_root / "keybindings.json").is_file())
        self.assertTrue(src.is_symlink())                     # 原位变链接
        # 其他 profile 也铺上了
        self.assertTrue((self.env.accounts_root / "a1" / "keybindings.json")
                        .is_symlink())
        self.assertIn("keybindings.json", Registry.load(self.env).shared)

    def test_shared_add_missing_source_needs_from(self):
        with self.assertRaises(CcmError):
            shared_add(self.env, self.registry, "nothing.json")

    def test_shared_rm_manifest_only(self):
        shared_rm(self.env, self.registry, "CLAUDE.md")
        self.assertNotIn("CLAUDE.md", Registry.load(self.env).shared)
        self.assertTrue((self.env.shared_root / "CLAUDE.md").exists())  # 文件保留

    def test_unlink_makes_independent_copy(self):
        unlink_item(self.env, self.registry, "a3", "settings.json")
        p = self.env.accounts_root / "a3" / "settings.json"
        self.assertFalse(p.is_symlink())
        self.assertEqual(p.read_text(),
                         (self.env.shared_root / "settings.json").read_text())
        # 改共享不再影响 work
        (self.env.shared_root / "settings.json").write_text('{"changed": 1}')
        self.assertNotIn("changed", p.read_text())

    def test_unlink_dir(self):
        unlink_item(self.env, self.registry, "a3", "plugins")
        p = self.env.accounts_root / "a3" / "plugins"
        self.assertFalse(p.is_symlink())
        self.assertEqual((p / "marker.txt").read_text(), "plugin-data")

    def test_diff(self):
        (self.env.accounts_root / "a3" / "history.jsonl").write_text("AAA\n")
        d = diff_profiles(self.registry, "a1", "a3")
        keys = {x["item"] for x in d}
        self.assertIn("history.jsonl", keys)
        self.assertNotIn("settings.json", keys)   # 共享项不参与 diff


class TestTokenCompletion(Base):
    def test_token_requires_yes(self):
        rc, out, err = self.run_cli("token", "a3")
        self.assertEqual(rc, 1)
        self.assertNotIn("sk-ant", out)
        rc, out, _ = self.run_cli("token", "a3", "--yes")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().startswith("sk-ant-oat"))

    def test_completion_script_uses_engine(self):
        rc, out, _ = self.run_cli("completion", "bash")
        self.assertEqual(rc, 0)
        self.assertIn("_complete", out)                # 走上下文引擎
        self.assertIn("complete -o nosort -F", out)

    def test_complete_names_hidden(self):
        rc, out, _ = self.run_cli("_complete-names")
        self.assertEqual(rc, 0)
        self.assertIn("a3", out.split())


if __name__ == "__main__":
    unittest.main()


class TestShellCmd(Base):
    def test_shell_injects_env_and_pin(self):
        # 子进程直接写 fd1,redirect_stdout 捕不到 → 让假 shell 落盘再断言
        probe = self.tmp / "shell-probe"
        fake = self.tmp / "fake-shell"
        fake.write_text("#!/bin/sh\necho \"CFG=$CLAUDE_CONFIG_DIR PIN=$CCM_PROFILE_PINNED\" > "
                        + str(probe) + "\n")
        os.chmod(fake, 0o755)
        old = os.environ.get("SHELL")
        os.environ["SHELL"] = str(fake)
        try:
            rc, _, _ = self.run_cli("shell", "a3")
        finally:
            if old is None:
                os.environ.pop("SHELL", None)
            else:
                os.environ["SHELL"] = old
        self.assertEqual(rc, 0)
        got = probe.read_text()
        self.assertIn(f"CFG={self.env.accounts_root}/a3", got)
        self.assertIn("PIN=1", got)
