"""备份 / 恢复 / 导出 / 导入。

默认备份不含凭证;`--with-credentials`/export 才含(0600)。
恢复走安全解包:tarfile filter='tar'(拒绝绝对路径与 `..` 穿越;symlink 目标是数据,
允许指向 shared 的绝对链接),先落 staging 再原子提升。
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
            tar.add(prof.path, arcname=name, filter=_filter)
    return dest


def restore_backup(env, registry, archive, into=None):
    archive = Path(archive)
    if not archive.exists():
        raise CcmError(f"归档不存在: {archive}")
    with tarfile.open(archive) as tar:
        tops = {m.name.split("/", 1)[0] for m in tar.getmembers()}
        if len(tops) != 1:
            raise CcmError(f"归档应只含一个顶层目录,实际: {sorted(tops)}")
        orig = tops.pop()
        name = into or orig
        validate_profile_name(name)
        if name in registry.profiles:
            raise CcmError(f"profile 已存在: {name}")
        target = env.accounts_root / name
        if os.path.lexists(target):
            raise CcmError(f"目录已被占用: {target}")
        staging = Path(tempfile.mkdtemp(prefix="ccm-restore-",
                                        dir=env.ccm_home))
        try:
            try:
                tar.extractall(staging, filter="tar")   # 拒绝绝对路径与 .. 穿越
            except tarfile.FilterError as e:
                raise CcmError(f"归档含不安全成员,拒绝解包: {e}")
            env.accounts_root.mkdir(parents=True, exist_ok=True)
            os.rename(staging / orig, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    apply_links(target, registry.shared_root, registry.shared)
    ident = resolve_identity(target, env.user_home) or {}
    prof = Profile(name=name, path=target,
                   account_uuid=ident.get("account_uuid"), email=ident.get("email"),
                   subscription=ident.get("subscription"),
                   rate_limit_tier=ident.get("rate_limit_tier"))
    registry.profiles[name] = prof
    registry.save(env)
    return prof
