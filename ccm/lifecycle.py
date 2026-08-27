"""profile 生命周期:add / rm / rename / logout / login / show(spec §7)。

纪律:default 保留名;有活跃进程一律拒绝;破坏性操作先备份(rm 的备份含凭证,0600);
logout 默认不留副本(否则「退出」语义不完整)。
"""
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from ccm.config import load_state, save_state, validate_profile_name
from ccm.errors import CcmError
from ccm.identity import read_credentials, resolve_identity
from ccm.layout import apply_links, link_plan
from ccm.migrate import (OP_JOURNAL, Journal, assert_no_pending_migration,
                         move_dir_with_compat)
from ccm.oauth import token_state
from ccm.procs import profile_active_pids
from ccm.profiles import Profile


def _refuse_active(prof, scan, verb):
    pids = profile_active_pids(prof.path, prof.compat_link, scan or {})
    if pids:
        raise CcmError(f"{prof.name} 有活跃 claude 进程 {sorted(pids)},拒绝{verb}")


def add_profile(env, registry, name=None, note="", import_dir=None, move=False):
    if name is None:   # 不起别名:自动分配编码 id(a1/a2/…)
        from ccm.selector import next_auto_id
        name = next_auto_id(registry)
    validate_profile_name(name)
    if name in registry.profiles:
        raise CcmError(f"profile 已存在: {name}")
    if import_dir is not None:
        import_dir = Path(import_dir)
        if not import_dir.is_dir():
            raise CcmError(f"导入目录不存在: {import_dir}")
        # 先查重再动文件:同一目录被两个 profile 纳管会让 rm/link 互相踩
        dup = next((n for n, q in registry.profiles.items()
                    if os.path.realpath(q.path) == os.path.realpath(import_dir)), None)
        if dup:
            raise CcmError(f"该目录已被 profile {dup} 纳管: {import_dir}")
        if move:
            assert_no_pending_migration(env)   # 绝不与未完结的 migrate 抢 journal
            new = registry.accounts_root / name
            j = Journal.load(env, OP_JOURNAL)  # 独立 journal,不碰 migrate 的
            move_dir_with_compat(import_dir, new, j)
            j.clear()  # 单目录迁移即完即清(整体 migrate 才需要留 journal)
            path, compat = new, import_dir
        else:
            path, compat = import_dir, None   # 原地纳管,不动文件
    else:
        path = registry.accounts_root / name
        if os.path.lexists(path):
            raise CcmError(f"目录已被占用: {path}")
        path.mkdir(parents=True)
        compat = None
    apply_links(path, registry.shared_root, registry.shared)
    ident = resolve_identity(path, env.user_home) or {}
    prof = Profile(name=name, path=path, compat_link=compat,
                   account_uuid=ident.get("account_uuid"), email=ident.get("email"),
                   subscription=ident.get("subscription"),
                   rate_limit_tier=ident.get("rate_limit_tier"), note=note)
    registry.profiles[name] = prof
    registry.save(env)
    return prof


def _backup_profile(env, prof, tag):
    dest_dir = env.ccm_home / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = dest_dir / f"{tag}-{prof.name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        with tarfile.open(fileobj=fh, mode="w:gz", dereference=False) as tar:
            tar.add(prof.path, arcname=prof.name)   # 含 .credentials.json(可恢复删除)
    return dest


def remove_profile(env, registry, name, scan=None, keep_data=False):
    if name == registry.default_profile:
        raise CcmError(f"{name} 是默认落点(~/.claude 指向它),不可删除")
    prof = registry.get(name)
    _refuse_active(prof, scan, "删除")
    bak = None
    if not keep_data:
        if prof.path.is_symlink():
            raise CcmError(f"{name} 的路径是一个 symlink({prof.path}),"
                           f"不确定该删链接还是删目标;请用 --keep-data 摘注册后手工处理")
        bak = _backup_profile(env, prof, "rm")
        shutil.rmtree(prof.path)
        if prof.compat_link and prof.compat_link.is_symlink():
            os.unlink(prof.compat_link)
    del registry.profiles[name]
    registry.save(env)
    state = load_state(env)
    if state and state.get("active") == name:
        fallback = registry.default_profile \
            if registry.default_profile in registry.profiles \
            else (sorted(registry.profiles)[0] if registry.profiles else None)
        if fallback:
            save_state(env, fallback, "ccm rm 回落")
        else:   # 删光了:清掉 state,而不是崩在 sorted([])[0]
            try:
                os.unlink(env.ccm_home / "state.json")
            except FileNotFoundError:
                pass
    return bak


def rename_profile(env, registry, old, new, scan=None):
    validate_profile_name(new)
    if new in registry.profiles:
        raise CcmError(f"目标名已存在: {new}")
    prof = registry.get(old)
    _refuse_active(prof, scan, "改名")
    new_path = prof.path
    if prof.path.parent == registry.accounts_root:  # 只有住在 accounts_root 里才改目录名
        new_path = registry.accounts_root / new
        if os.path.lexists(new_path):
            raise CcmError(f"目标目录已存在: {new_path}")
        os.rename(prof.path, new_path)
        if prof.compat_link and prof.compat_link.is_symlink():
            os.unlink(prof.compat_link)
            os.symlink(new_path, prof.compat_link)
    prof.name, prof.path = new, new_path
    registry.profiles[new] = prof
    del registry.profiles[old]
    if registry.default_profile == old:   # 默认指针跟着走
        registry.default_profile = new
    registry.save(env)
    state = load_state(env)
    if state and state.get("active") == old:
        save_state(env, new, "ccm rename")


def logout_profile(env, registry, name, scan=None, keep_backup=False):
    prof = registry.get(name)
    _refuse_active(prof, scan, "登出")
    cred = prof.path / ".credentials.json"
    if not cred.exists():
        raise CcmError(f"{name} 本就未登录")
    if keep_backup:
        dest_dir = env.ccm_home / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = dest_dir / f"logout-{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(cred.read_bytes())
    os.unlink(cred)


def login_profile(env, registry, name, claude_bin="claude"):
    """跑一个 claude 让用户走 /login;退出后回填身份。"""
    prof = registry.get(name)
    child_env = dict(os.environ, CLAUDE_CONFIG_DIR=str(prof.path),
                     CCM_PROFILE_PINNED="1")
    try:
        rc = subprocess.call([claude_bin], env=child_env)
    except FileNotFoundError:
        raise CcmError("claude 未安装或不在 PATH,无法引导登录")
    ident = resolve_identity(prof.path, env.user_home,
                             allow_legacy=(name == registry.default_profile)) or {}
    if ident.get("account_uuid"):
        # 登录可能持续几分钟,不能全程占着注册表锁;回来后才在事务里回填,
        # 且以磁盘上的最新注册表为准(期间可能有别的 ccm 改过)。
        from ccm.config import registry_transaction
        with registry_transaction(env) as fresh:
            target = fresh.profiles.get(name)
            if target is None:
                return rc   # 期间被删了,不复活
            for obj in (target, prof):
                obj.account_uuid = ident["account_uuid"]
                obj.email = ident.get("email")
                obj.subscription = ident.get("subscription")
                obj.rate_limit_tier = ident.get("rate_limit_tier")
                obj.identity_fetched_at = int(time.time() * 1000)
        registry.profiles[name] = prof
    return rc


def show_profile(env, registry, name):
    prof = registry.get(name)
    ident = resolve_identity(prof.path, env.user_home,
                             allow_legacy=(name == registry.default_profile)) or {}
    creds = None
    try:
        creds = read_credentials(prof.path)
    except CcmError:
        pass
    plan = link_plan(prof.path, registry.shared_root, registry.shared)
    hist = prof.path / "history.jsonl"
    return {
        "name": name, "path": str(prof.path),
        "compat_link": str(prof.compat_link) if prof.compat_link else None,
        "email": ident.get("email"), "account_uuid": ident.get("account_uuid"),
        "subscription": ident.get("subscription"),
        "token": token_state(creds) if creds else None,
        "links_ok": all(a.status in ("ok", "skip_no_source") for a in plan),
        "links": {a.item: a.status for a in plan},
        "note": prof.note,
        "last_used": hist.stat().st_mtime if hist.exists() else None,
    }
