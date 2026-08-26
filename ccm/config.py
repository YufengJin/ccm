import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ccm.errors import CcmError, ProfileNotFound
from ccm.profiles import Profile

# §6 默认共享清单
DEFAULT_SHARED = [
    "settings.json", "settings.local.json", "CLAUDE.md",
    "plugins", "skills", "commands", "agents",
    "projects", "statusline-command.sh",
]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def validate_profile_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise CcmError(f"非法 profile 名: {name!r}(须匹配 [A-Za-z0-9][A-Za-z0-9_-]{{0,31}})")


def validate_shared_item(item):
    if not isinstance(item, str) or not item or "/" in item or item in (".", ".."):
        raise CcmError(f"非法共享条目: {item!r}(须为单段相对名)")


@dataclass(frozen=True)
class Env:
    user_home: Path
    ccm_home: Path
    accounts_root: Path
    shared_root: Path
    proc_root: Path

    @classmethod
    def from_environ(cls, environ=None):
        if environ is None:
            environ = os.environ
        home = Path(environ.get("CCM_USER_HOME") or Path.home())
        return cls(
            user_home=home,
            ccm_home=Path(environ.get("CCM_HOME") or home / ".ccm"),
            accounts_root=Path(environ.get("CCM_ACCOUNTS_ROOT") or home / ".claude-accounts"),
            shared_root=Path(environ.get("CCM_SHARED_ROOT") or home / ".claude-shared"),
            proc_root=Path(environ.get("CCM_PROC_ROOT") or "/proc"),
        )


def expand(s, home):
    """'~/x' → home/x;不用 os.path.expanduser(它读真实 HOME,破坏测试隔离)。"""
    if s == "~":
        return Path(home)
    if s.startswith("~/"):
        return Path(home) / s[2:]
    return Path(s)


def contract(p, home):
    """Path → '~/...' 字符串(仅当以 home 为前缀)。"""
    p, home = Path(p), Path(home)
    try:
        return "~/" + str(p.relative_to(home))
    except ValueError:
        return str(p)


def _ensure_ccm_home(env):
    env.ccm_home.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write_json(path, obj, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True,
                      mode=0o700 if ".ccm" in path.parts or path.parent.name == ".ccm" else 0o755)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path, default=None):
    path = Path(path)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise CcmError(f"JSON 损坏: {path} ({e})")


@contextmanager
def registry_lock(env):
    _ensure_ccm_home(env)
    lock_path = env.ccm_home / "lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Registry:
    def __init__(self, env, shared_root, accounts_root, shared, profiles,
                 default_profile=None):
        self._env = env
        self.shared_root = shared_root
        self.accounts_root = accounts_root
        self.shared = shared
        self.profiles = profiles
        # 结构性指针:哪个 profile 是 ~/.claude 的默认落点(取代魔法名 "default")
        self.default_profile = default_profile

    @classmethod
    def empty(cls, env):
        return cls(env, env.shared_root, env.accounts_root, list(DEFAULT_SHARED), {})

    @classmethod
    def load(cls, env):
        raw = load_json(env.ccm_home / "profiles.json")
        if raw is None:
            return cls.empty(env)
        home = env.user_home
        shared = raw.get("shared", list(DEFAULT_SHARED))
        for item in shared:
            validate_shared_item(item)
        profiles = {}
        for name, pd in raw.get("profiles", {}).items():
            validate_profile_name(name)
            profiles[name] = Profile(
                name=name,
                path=expand(pd["path"], home),
                compat_link=expand(pd["compat_link"], home) if pd.get("compat_link") else None,
                account_uuid=pd.get("account_uuid"),
                email=pd.get("email"),
                subscription=pd.get("subscription"),
                rate_limit_tier=pd.get("rate_limit_tier"),
                identity_fetched_at=pd.get("identity_fetched_at"),
                note=pd.get("note", ""),
            )
        default = raw.get("default_profile")
        if default is None:   # 旧注册表升级:魔法名 "default" 时代
            default = "default" if "default" in profiles else \
                ("a1" if "a1" in profiles else None)
        return cls(env,
                   expand(raw.get("shared_root", contract(env.shared_root, home)), home),
                   expand(raw.get("accounts_root", contract(env.accounts_root, home)), home),
                   shared, profiles, default_profile=default)

    def save(self, env=None):
        env = env or self._env
        home = env.user_home
        raw = {
            "version": 2,
            "default_profile": self.default_profile,
            "shared_root": contract(self.shared_root, home),
            "accounts_root": contract(self.accounts_root, home),
            "shared": self.shared,
            "profiles": {},
        }
        for name, p in self.profiles.items():
            validate_profile_name(name)
            raw["profiles"][name] = {
                "path": contract(p.path, home),
                "compat_link": contract(p.compat_link, home) if p.compat_link else None,
                "account_uuid": p.account_uuid,
                "email": p.email,
                "subscription": p.subscription,
                "rate_limit_tier": p.rate_limit_tier,
                "identity_fetched_at": p.identity_fetched_at,
                "note": p.note,
            }
        with registry_lock(env):
            atomic_write_json(env.ccm_home / "profiles.json", raw)

    def get(self, name):
        try:
            return self.profiles[name]
        except KeyError:
            raise ProfileNotFound(f"profile 不存在: {name}(现有: {', '.join(sorted(self.profiles)) or '无'})")


def load_state(env):
    return load_json(env.ccm_home / "state.json")


def save_state(env, active, changed_by):
    with registry_lock(env):
        atomic_write_json(env.ccm_home / "state.json",
                          {"active": active,
                           "changed_at": int(time.time() * 1000),
                           "changed_by": changed_by})
