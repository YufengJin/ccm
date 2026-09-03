import os
from pathlib import Path

from ccm.config import load_json
from ccm.errors import CcmError


UNKNOWN = "?unknown"   # environ 不可读、但看着像 claude 的进程


# Claude Code 可执行文件的路径特征(按路径分量精确匹配,不做子串匹配):
#   原生安装  ~/.local/share/claude/versions/2.1.259、~/.local/bin/claude
#   npm 安装  node …/@anthropic-ai/claude-code/cli.js
# 子串匹配会把 claudecodeui 之类第三方服务、以及 cmdline 里带 ~/.claude 路径的
# bash 包装进程都误判成 claude。
_CLAUDE_PATH_PARTS = frozenset({"claude", "claude-code"})


def _path_is_claude(path):
    if not path:
        return False
    if path.endswith(" (deleted)"):       # 二进制升级后旧进程的 exe 链接
        path = path[:-len(" (deleted)")]
    return any(part.lower() in _CLAUDE_PATH_PARTS for part in path.split("/"))


def _read_argv(pid_dir):
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _is_claude_exe(pid_dir):
    """该进程是否是 Claude Code **本体**。

    只看可执行文件(exe 链接、argv[0],以及 node/bun 解释器后面的脚本 argv[1]),
    **不看** environ:Claude Code 派生的所有子进程(Bash 工具跑的命令、后台服务、
    从会话里启动的 ccm daemon)都原样继承 CLAUDE_CONFIG_DIR / CLAUDECODE,但它们
    既不持有也不刷新凭证。把它们算作活跃进程,refresh 就会永远 skipped-active
    (实机:a1 名下 17 个「活跃」pid 里只有 1 个是 claude)。
    exe 与 cmdline 之一读得到就够;都读不到的进程(内核线程等)视为无关。
    """
    try:
        if _path_is_claude(os.readlink(pid_dir / "exe")):
            return True
    except OSError:
        pass                                 # EACCES / ENOENT:退回 cmdline
    argv = _read_argv(pid_dir)
    if not argv:
        return False
    if _path_is_claude(argv[0]):
        return True
    interp = os.path.basename(argv[0]).lower()
    return len(argv) > 1 and interp.startswith(("node", "bun")) \
        and _path_is_claude(argv[1])


def scan_claude_procs(proc_root, user_home):
    """扫描 /proc,返回 {realpath(配置目录): {pid,…}},外加 UNKNOWN 桶。

    先按可执行文件筛出 Claude Code 本体(见 _is_claude_exe),再按 environ 归属:
    有 CLAUDE_CONFIG_DIR → 归入该目录;否则有 CLAUDECODE=1 → 归入默认
    <user_home>/.claude;两者皆无 → 不算。
    environ 不可读时**不能**当作「没有进程」(§9:未知状态一律按有活跃进程处理)
    —— 丢进 UNKNOWN 桶,由调用方决定是否保守对待。
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
        pid_dir = proc_root / name
        if not _is_claude_exe(pid_dir):
            continue
        try:
            raw = (pid_dir / "environ").read_bytes()
        except OSError:
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
