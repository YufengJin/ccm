"""备份 / 恢复 / 导出 / 导入。

默认备份不含凭证;`--with-credentials`/export 才含(0600)。
恢复走安全解包:优先 tarfile filter='tar'(拒绝绝对路径与 `..` 穿越;symlink 目标是
数据,允许指向 shared 的绝对链接),老 Python 无该参数时用等价的手写校验;
解包后**必须复核顶层是真目录而非 symlink** —— 否则后续 apply_links 会顺着这条
链接往任意可写目录里写文件(codex 审核发现)。先落 staging 再原子提升。
"""
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from ccm.config import validate_profile_name
from ccm.errors import CcmError
from ccm.layout import apply_links
from ccm.identity import resolve_identity
from ccm.profiles import Profile


def create_backup(env, registry, name, with_credentials=False, dest=None):
    prof = registry.get(name)
    dest_dir = env.ccm_home / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = Path(dest) if dest else \
        dest_dir / f"backup-{name}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"

    def _filter(info):
        if not with_credentials and info.name.endswith(".credentials.json"):
            return None
        return info

    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        with tarfile.open(fileobj=fh, mode="w:gz", dereference=False) as tar:
            try:
                tar.add(prof.path, arcname=name, filter=_filter)
            except FileNotFoundError as e:
                # profile 目录是活的:Claude Code 的 tmp 文件随时会消失
                raise CcmError(f"备份时文件被并发删除,请稍后重试: {e}")
    return dest


def _manual_safe_extract(tar, dest):
    """Python < 3.10.12 / 3.11.4 没有 extractall(filter=) 时的等价校验(§7)。"""
    dest = Path(dest).resolve()
    for m in tar.getmembers():
        if m.isdev():
            raise CcmError(f"归档含设备节点,拒绝解包: {m.name}")
        target = (dest / m.name).resolve()
        if target != dest and dest not in target.parents:
            raise CcmError(f"归档成员逃出解包目录,拒绝解包: {m.name}")
        if m.issym() or m.islnk():
            link = (target.parent / m.linkname).resolve()
            if m.islnk() and dest not in link.parents:
                raise CcmError(f"归档硬链接指向外部,拒绝解包: {m.name}")
        tar.extract(m, dest)


def restore_backup(env, registry, archive, into=None):
    archive = Path(archive)
    if not archive.exists():
        raise CcmError(f"归档不存在: {archive}")
    accounts_root = registry.accounts_root
    with tarfile.open(archive) as tar:
        tops = {m.name.split("/", 1)[0] for m in tar.getmembers()} - {"", "."}
        if len(tops) != 1:
            raise CcmError(f"归档应只含一个顶层目录,实际: {sorted(tops)}")
        orig = tops.pop()
        name = into or orig
        validate_profile_name(name)
        if name in registry.profiles:
            raise CcmError(f"profile 已存在: {name}")
        target = accounts_root / name
        if os.path.lexists(target):
            raise CcmError(f"目录已被占用: {target}")
        env.ccm_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix="ccm-restore-", dir=env.ccm_home))
        try:
            try:
                tar.extractall(staging, filter="tar")   # 拒绝绝对路径与 .. 穿越
            except TypeError:                           # 老 Python:没有 filter 参数
                _manual_safe_extract(tar, staging)
            except tarfile.FilterError as e:
                raise CcmError(f"归档含不安全成员,拒绝解包: {e}")
            src = staging / orig
            # 顶层必须是真目录:若是 symlink,提升后 apply_links 会往链接目标里写
            if src.is_symlink() or not src.is_dir():
                raise CcmError(f"归档顶层成员不是普通目录(疑似 symlink 逃逸),"
                               f"拒绝导入: {orig}")
            accounts_root.mkdir(parents=True, exist_ok=True)
            os.rename(src, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    if target.is_symlink() or not target.is_dir():      # 提升后再复核一次
        raise CcmError(f"恢复目标异常,不是普通目录: {target}")
    apply_links(target, registry.shared_root, registry.shared)
    ident = resolve_identity(target, env.user_home) or {}
    prof = Profile(name=name, path=target,
                   account_uuid=ident.get("account_uuid"), email=ident.get("email"),
                   subscription=ident.get("subscription"),
                   rate_limit_tier=ident.get("rate_limit_tier"))
    registry.profiles[name] = prof
    registry.save(env)
    return prof
