import json
import os
from pathlib import Path

from ccm.config import load_json
from ccm.errors import CcmError


UNKNOWN = "?unknown"   # environ 不可读、但看着像 claude 的进程


def _looks_like_claude(pid_dir):
    """environ 读不到时的兜底:cmdline 是世界可读的。"""
    try:
        cmd = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return False
    return b"claude" in cmd.lower()


def scan_claude_procs(proc_root, user_home):
    """扫描 /proc,返回 {realpath(配置目录): {pid,…}},外加 UNKNOWN 桶。

    判定:environ 有 CLAUDE_CONFIG_DIR → 归入该目录;
    否则有 CLAUDECODE=1 → 归入默认 <user_home>/.claude;
    两者皆无 → 不算 claude 进程。
    environ 不可读时**不能**当作「没有进程」(§9:未知状态一律按有活跃进程处理)——
    回退看 cmdline,像 claude 就丢进 UNKNOWN 桶,由调用方决定是否保守对待。
    """
    out = {}
    proc_root = Path(proc_root)
    default_dir = os.path.realpath(Path(user_home) / ".claude")
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        try:
            raw = (proc_root / name / "environ").read_bytes()
        except OSError:
            if _looks_like_claude(proc_root / name):
                out.setdefault(UNKNOWN, set()).add(int(name))
            continue
        envd = {}
        for chunk in raw.split(b"\0"):
            if b"=" in chunk:
                k, _, v = chunk.partition(b"=")
                envd[k.decode(errors="replace")] = v.decode(errors="replace")
        cfg = envd.get("CLAUDE_CONFIG_DIR")
        if cfg:
            key = os.path.realpath(cfg)
        elif envd.get("CLAUDECODE") == "1":
            key = default_dir
        else:
            continue
        out.setdefault(key, set()).add(int(name))
    return out


def profile_active_pids(profile_path, compat_link, scan, include_unknown=False):
    """归属该 profile 的活跃 pid。

    include_unknown=True 时把无法归属的可疑 claude 进程也算进来 —— 刷新凭证这类
    「猜错就掉线」的操作必须保守(§9)。
    """
    pids = set()
    for p in (profile_path, compat_link):
        if p is None:
            continue
        pids |= scan.get(os.path.realpath(p), set())
    if include_unknown:
        pids |= scan.get(UNKNOWN, set())
    return pids


def _default_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def daemon_lock_pid(profile_path, pid_alive=None):
    """daemon.lock 里记录的 pid,若仍存活;否则 None。"""
    try:
        data = load_json(Path(profile_path) / "daemon.lock")
    except CcmError:
        return None
    pid = (data or {}).get("pid")
    if not isinstance(pid, int):
        return None
    alive = pid_alive or _default_pid_alive
    return pid if alive(pid) else None
