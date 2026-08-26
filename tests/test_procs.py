import json
import os
import tempfile
import unittest
from pathlib import Path

from ccm.procs import scan_claude_procs, profile_active_pids, daemon_lock_pid


def make_proc(tmp, pid, environ):
    d = tmp / "proc" / str(pid)
    d.mkdir(parents=True)
    (d / "environ").write_bytes(
        b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")


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
