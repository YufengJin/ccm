import io as _io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.cli import main
from ccm.migrate import build_plan, execute_migration
from ccm.complete import complete_words
from ccm.config import Env, Registry


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-ux-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))
        self.registry = Registry.load(self.env)

    def run_cli(self, *argv):
        out, err = _io.StringIO(), _io.StringIO()
        (self.tmp / "emptyproc").mkdir(exist_ok=True)
        old = {k: os.environ.get(k) for k in ("CCM_USER_HOME", "CCM_PROC_ROOT")}
        os.environ["CCM_USER_HOME"] = str(self.tmp)
        os.environ["CCM_PROC_ROOT"] = str(self.tmp / "emptyproc")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = main(list(argv))
                except SystemExit as e:
                    rc = e.code or 0
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue(), err.getvalue()


class TestSwitch(Base):
    def test_switch_is_primary_use_is_alias(self):
        rc, _, _ = self.run_cli("switch", "a3")
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("current", "-q")
        self.assertEqual(out.strip(), "a3")
        rc, _, _ = self.run_cli("use", "1")       # 别名仍可用
        self.assertEqual(rc, 0)

    def test_custom_name_switch(self):
        rc, _, _ = self.run_cli("rename", "a3", "corp")
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_cli("switch", "corp")  # 自定义名直接切
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("current", "-q")
        self.assertEqual(out.strip(), "corp")


class TestHelp(Base):
    def test_bare_ccm_grouped_help_with_current(self):
        self.run_cli("switch", "a3")
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        for word in ("常用", "switch", "usage", "账号管理"):
            self.assertIn(word, out)
        self.assertIn("a3", out)                   # 当前账号一行

    def test_help_named_command(self):
        rc, out, _ = self.run_cli("help", "migrate")
        self.assertEqual(rc, 0)
        self.assertIn("--dry-run", out)

    def test_unknown_command_suggests(self):
        rc, _, err = self.run_cli("swich")
        self.assertEqual(rc, 1)
        self.assertIn("switch", err)               # did-you-mean


class TestShortFlags(Base):
    def test_migrate_dash_n(self):
        # -n 等价 --dry-run;需要未迁移的 home
        tmp2 = Path(tempfile.mkdtemp(prefix="ccm-ux-n-"))
        make_fake_home(tmp2)
        old = os.environ.get("CCM_USER_HOME")
        os.environ["CCM_USER_HOME"] = str(tmp2)
        try:
            out, err = _io.StringIO(), _io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(["migrate", "-n"])
        finally:
            if old is None:
                os.environ.pop("CCM_USER_HOME", None)
            else:
                os.environ["CCM_USER_HOME"] = old
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out.getvalue())

    def test_logout_requires_confirm_noninteractive(self):
        rc, _, err = self.run_cli("logout", "a3")   # 非 tty 且无 -y
        self.assertEqual(rc, 1)
        self.assertTrue((self.env.accounts_root / "a3" / ".credentials.json").exists())
        rc, _, _ = self.run_cli("logout", "a3", "-y")
        self.assertEqual(rc, 0)


class TestComplete(Base):
    def comp(self, line_words):
        # 模拟 bash: words[0]=ccm, cword=len-1(正在补最后一个词)
        words = ["ccm"] + line_words
        return complete_words(self.env, self.registry, len(words) - 1, words)

    def test_subcommand_prefix(self):
        self.assertEqual(self.comp(["sw"]), ["switch"])
        got = self.comp([""])
        self.assertIn("switch", got)
        self.assertIn("usage", got)
        self.assertNotIn("_complete", got)         # 隐藏命令不出现
        self.assertNotIn("use", got)               # 别名不重复展示

    def test_profile_positions_get_ids_and_emails(self):
        got = self.comp(["switch", ""])
        self.assertIn("a1", got)
        self.assertIn("a3", got)
        self.assertIn("work@example.com", got)
        self.assertEqual(self.comp(["switch", "work@"]), ["work@example.com"])
        self.assertIn("a3", self.comp(["rm", "a"]))
        self.assertIn("a1", self.comp(["diff", "a3", ""]))  # 第二个也是 profile

    def test_flag_completion_per_subcommand(self):
        got = self.comp(["usage", "--"])
        self.assertIn("--watch", got)
        self.assertIn("--json", got)
        self.assertNotIn("--emit-env", got)        # SUPPRESS 的不出现
        self.assertIn("--dry-run", self.comp(["migrate", "--d"]))

    def test_choice_positions(self):
        self.assertEqual(self.comp(["daemon", "st"]), ["start", "status", "stop"])
        self.assertIn("add", self.comp(["shared", ""]))
        self.assertIn("settings.json", self.comp(["shared", "rm", ""]))
        self.assertIn("plugins", self.comp(["unlink", "a3", ""]))
        self.assertIn("migrate", self.comp(["help", "mi"]))


if __name__ == "__main__":
    unittest.main()
