import json
import os
from pathlib import Path

from ccm.config import load_json
from ccm.errors import CcmError


def scan_claude_procs(proc_root, user_home):
    """扫描 /proc,返回 {realpath(配置目录): {pid,…}}。

    判定:environ 有 CLAUDE_CONFIG_DIR → 归入该目录;
    否则有 CLAUDECODE=1 → 归入默认 <user_home>/.claude;
    两者皆无 → 不算 claude 进程。不可读一律跳过。
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


def profile_active_pids(profile_path, compat_link, scan):
    pids = set()
    for p in (profile_path, compat_link):
        if p is None:
            continue
        pids |= scan.get(os.path.realpath(p), set())
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
