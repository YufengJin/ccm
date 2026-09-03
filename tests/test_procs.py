import json
import os
import tempfile
import unittest
from pathlib import Path

from ccm.procs import (UNKNOWN, daemon_lock_pid, profile_active_pids,
                       scan_claude_procs)


CLAUDE_EXE = "/home/u/.local/share/claude/versions/2.1.259"


def make_proc(tmp, pid, environ, argv=("claude",), exe=CLAUDE_EXE):
    """伪造 /proc/<pid>:environ=None 表示不可读;exe=None 表示 readlink 失败。"""
    d = tmp / "proc" / str(pid)
    d.mkdir(parents=True)
    if environ is not None:
        (d / "environ").write_bytes(
            b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
    if argv is not None:
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if exe is not None:
        os.symlink(exe, d / "exe")


class TestProcs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-procs-"))
        self.home = self.tmp / "home"
        self.home.mkdir()

    def test_scan_rules(self):
        (self.home / ".claude-real").mkdir()
        (self.home / ".claude").mkdir()
        os.symlink(self.home / ".claude-real", self.home / ".claude-b")
        make_proc(self.tmp, 100, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude-b"),
                                  "CLAUDECODE": "1"})
        make_proc(self.tmp, 101, {"CLAUDECODE": "1"})          # 默认目录
        make_proc(self.tmp, 102, {"PATH": "/usr/bin"})          # 无关进程
        (self.tmp / "proc" / "self").mkdir()                    # 非数字条目
        unread = self.tmp / "proc" / "103"
        unread.mkdir()                                          # 无 environ 文件
        scan = scan_claude_procs(self.tmp / "proc", self.home)
        real = os.path.realpath(self.home / ".claude-real")
        self.assertEqual(scan[real], {100})
        self.assertEqual(scan[os.path.realpath(self.home / ".claude")], {101})
        self.assertNotIn(102, {p for s in scan.values() for p in s})
        # symlink 与真实路径都命中同一 profile
        self.assertEqual(profile_active_pids(self.home / ".claude-real",
                                             self.home / ".claude-b", scan), {100})
        self.assertEqual(profile_active_pids(self.home / ".claude-b", None, scan), {100})

    def test_only_claude_executables_count(self):
        """Claude Code 的子进程(bash 工具、后台服务、ccm daemon)会原样继承
        CLAUDE_CONFIG_DIR / CLAUDECODE,但它们不持有也不刷新凭证,不能算活跃
        claude 进程 —— 否则 refresh 永远 skipped-active(实机踩坑)。"""
        cfg = str(self.home / ".claude-accounts" / "a3")
        (self.home / ".claude-accounts" / "a3").mkdir(parents=True)
        env = {"CLAUDE_CONFIG_DIR": cfg, "CLAUDECODE": "1"}
        # 真 claude:原生二进制(comm/argv0 可能被改写成版本号)
        make_proc(self.tmp, 200, env, argv=("2.1.259", "--resume"), exe=CLAUDE_EXE)
        # 真 claude:npm 安装,exe 是 node,argv[1] 是 claude-code/cli.js
        make_proc(self.tmp, 201, env,
                  argv=("node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
                  exe="/usr/bin/node")
        # 真 claude:exe 不可读(readlink EACCES / 已删除),只能靠 argv0
        make_proc(self.tmp, 202, env, argv=("/home/u/.local/bin/claude",), exe=None)
        # 真 claude:二进制被升级后 exe 带 " (deleted)" 后缀
        make_proc(self.tmp, 203, env, argv=("claude",), exe=CLAUDE_EXE + " (deleted)")
        # 子进程:Bash 工具跑的 sleep 循环,cmdline 里恰好带 .claude 路径
        make_proc(self.tmp, 300, env,
                  argv=("/bin/bash", "-c",
                        "source /home/u/.claude/shell-snapshots/x.sh; sleep 30"),
                  exe="/usr/bin/bash")
        # 子进程:ccm 自己的 daemon,从 claude 会话里启动
        make_proc(self.tmp, 301, {"CLAUDECODE": "1"},
                  argv=("/usr/bin/python3", "-m", "ccm", "daemon", "_run"),
                  exe="/usr/bin/python3.12")
        # 子进程:名字里带 claude 的第三方 node 服务(claudecodeui)
        make_proc(self.tmp, 302, env,
                  argv=("node", "/home/u/claudecodeui/dist-server/server/cli.js"),
                  exe="/usr/bin/node")
        # 子进程:litellm 等无关服务
        make_proc(self.tmp, 303, env, argv=("/venv/bin/python", "/venv/bin/litellm"),
                  exe="/usr/bin/python3.12")
        scan = scan_claude_procs(self.tmp / "proc", self.home)
        self.assertEqual(scan.get(os.path.realpath(cfg)), {200, 201, 202, 203})
        self.assertNotIn(os.path.realpath(self.home / ".claude"), scan)
        self.assertNotIn(UNKNOWN, scan)

    def test_unknown_bucket_requires_claude_executable(self):
        """environ 不可读时的兜底同样只认 claude 可执行文件,不认 cmdline 子串。"""
        make_proc(self.tmp, 400, None, argv=("claude",), exe=None)
        make_proc(self.tmp, 401, None,
                  argv=("/bin/bash", "-c", "source /home/u/.claude/snap.sh"), exe=None)
        make_proc(self.tmp, 402, None, argv=None, exe=None)   # 什么都读不到
        scan = scan_claude_procs(self.tmp / "proc", self.home)
        self.assertEqual(scan.get(UNKNOWN), {400})

    def test_daemon_lock(self):
        p = Path(tempfile.mkdtemp())
        self.assertIsNone(daemon_lock_pid(p))
        (p / "daemon.lock").write_text(json.dumps({"pid": 4242}))
        self.assertEqual(daemon_lock_pid(p, pid_alive=lambda pid: True), 4242)
        self.assertIsNone(daemon_lock_pid(p, pid_alive=lambda pid: False))
        (p / "daemon.lock").write_text("not json")
        self.assertIsNone(daemon_lock_pid(p, pid_alive=lambda pid: True))


if __name__ == "__main__":
    unittest.main()
