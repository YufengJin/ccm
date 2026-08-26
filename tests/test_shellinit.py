import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.cli import main
from ccm.config import Registry, load_state
from ccm.profiles import Profile
from ccm.shellinit import env_exports, rc_block, install_block, BLOCK_BEGIN, BLOCK_END
import io
from contextlib import redirect_stdout, redirect_stderr

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestShellInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-sh-"))
        self.env = make_fake_home(self.tmp)
        # 注册 work profile(指向现有 .claude-b,原地纳管场景)
        r = Registry.empty(self.env)
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-b")
        r.profiles["default"] = Profile(name="default", path=self.tmp / ".claude")
        r.save(self.env)
        self.cli_env = {"CCM_USER_HOME": str(self.tmp)}

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = {k: os.environ.get(k) for k in self.cli_env}
        os.environ.update(self.cli_env)
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

    def test_env_exports_quoting(self):
        line = env_exports(Path("/x/with space/dir"))
        self.assertEqual(line, "export CLAUDE_CONFIG_DIR='/x/with space/dir'")

    def test_env_empty_when_no_state(self):
        rc, out, _ = self.run_cli("env")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_use_then_env_and_current(self):
        rc, out, err = self.run_cli("use", "work")
        self.assertEqual(rc, 0)
        self.assertEqual(load_state(self.env)["active"], "work")
        rc, out, _ = self.run_cli("env")
        self.assertIn("CLAUDE_CONFIG_DIR", out)
        self.assertIn(".claude-b", out)
        rc, out, _ = self.run_cli("current", "--quiet")
        self.assertEqual(out.strip(), "work")

    def test_use_emit_env_streams(self):
        rc, out, err = self.run_cli("use", "work", "--emit-env")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("export CLAUDE_CONFIG_DIR="))
        self.assertNotIn("export", err)   # 人话只去 stderr
        self.assertIn("work", err)

    def test_use_unknown_rc1(self):
        rc, _, err = self.run_cli("use", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("nope", err)

    def test_current_none_rc1(self):
        rc, out, _ = self.run_cli("current")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_rc_block_has_pin_guard(self):
        blk = rc_block()
        self.assertIn("CCM_PROFILE_PINNED", blk)
        self.assertIn(BLOCK_BEGIN, blk)
        self.assertIn(BLOCK_END, blk)

    def test_rc_block_resolves_bin_before_path(self):
        # Ubuntu ~/.profile 先 source .bashrc 再加 ~/.local/bin 进 PATH,
        # 块内必须用绝对路径兜底,否则新登录 shell 的 eval 静默失败
        blk = rc_block()
        self.assertIn(".local/bin/ccm", blk)
        self.assertIn("_CCM_BIN", blk)

    def test_rc_block_survives_login_shell_path_order(self):
        # 端到端复现:PATH 不含 ccm 时 source 块,env 仍应生效
        import subprocess
        rc_path = self.tmp / ".bashrc"
        install_block(rc_path)
        bindir = self.tmp / ".local" / "bin"
        bindir.mkdir(parents=True)
        # 假 ccm:env 子命令输出 export 语句
        fake = bindir / "ccm"
        fake.write_text("#!/bin/sh\n[ \"$1\" = env ] && echo 'export CLAUDE_CONFIG_DIR=/probe/dir'\n")
        import os as _os
        _os.chmod(fake, 0o755)
        r = subprocess.run(
            ["/bin/bash", "-c",
             f"HOME={self.tmp} PATH=/usr/bin:/bin source {rc_path}; "
             "echo GOT=$CLAUDE_CONFIG_DIR"],
            capture_output=True, text=True)
        self.assertIn("GOT=/probe/dir", r.stdout)

    def test_install_block_idempotent(self):
        rc_path = self.tmp / ".bashrc"
        rc_path.write_text("# 用户原有内容\ncca() { :; }\n")
        self.assertTrue(install_block(rc_path))
        self.assertFalse(install_block(rc_path))   # 第二次无改动
        content = rc_path.read_text()
        self.assertEqual(content.count(BLOCK_BEGIN), 1)
        self.assertIn("cca()", content)             # 用户内容保留
        # 块内容变化时可升级
        rc_path.write_text(content.replace("CCM_PROFILE_PINNED", "OLD_VAR"))
        self.assertTrue(install_block(rc_path))
        self.assertEqual(rc_path.read_text().count(BLOCK_BEGIN), 1)

    def test_init_writes_bashrc(self):
        rc, out, _ = self.run_cli("init", "bash")
        self.assertEqual(rc, 0)
        self.assertIn(BLOCK_BEGIN, (self.tmp / ".bashrc").read_text())


class TestRunCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-run-"))
        self.env = make_fake_home(self.tmp)
        r = Registry.empty(self.env)
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-b")
        r.save(self.env)

    def _subprocess_env(self, path_prefix):
        return dict(os.environ,
                    HOME=str(self.tmp),
                    CCM_USER_HOME=str(self.tmp),
                    PATH=f"{path_prefix}:{os.environ['PATH']}",
                    PYTHONPATH=str(REPO_ROOT))

    def test_run_injects_env(self):
        r = subprocess.run([sys.executable, "-m", "ccm", "run", "work", "--", "--foo"],
                           capture_output=True, text=True,
                           env=self._subprocess_env(FIXTURES))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"CFG={self.tmp}/.claude-b", r.stdout)
        self.assertIn("PIN=1", r.stdout)
        self.assertIn("ARGS=--foo", r.stdout)

    def test_run_claude_missing_clear_error(self):
        empty = self.tmp / "emptybin"
        empty.mkdir()
        r = subprocess.run([sys.executable, "-m", "ccm", "run", "work"],
                           capture_output=True, text=True,
                           env=dict(self._subprocess_env(empty),
                                    PATH=str(empty)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("未安装", r.stderr)


if __name__ == "__main__":
    unittest.main()
