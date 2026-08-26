"""共享清单维护(shared add/rm)、unlink 独立副本、profile diff。"""
import hashlib
import os
import shutil
from pathlib import Path

from ccm.config import validate_shared_item
from ccm.errors import CcmError
from ccm.layout import apply_links
from ccm.migrate import Journal, split_item


def shared_add(env, registry, item, from_profile=None):
    """新增共享项。源不存在时必须指明 --from(从该 profile 收编,原位留链)。"""
    validate_shared_item(item)
    if item in registry.shared:
        raise CcmError(f"已在共享清单中: {item}")
    src = registry.shared_root / item
    if not os.path.lexists(src):
        if not from_profile:
            raise CcmError(f"{src} 不存在;用 --from <profile> 指明从哪个 profile 收编")
        prof = registry.get(from_profile)
        if not os.path.lexists(prof.path / item):
            raise CcmError(f"{prof.path / item} 不存在,无从收编")
        j = Journal.load(env)
        split_item(prof.path, item, registry.shared_root, j)
        j.clear()
    registry.shared.append(item)
    registry.save(env)
    for p in registry.profiles.values():
        apply_links(p.path, registry.shared_root, registry.shared)


def shared_rm(env, registry, item):
    """仅从清单移除;共享文件与各 profile 的既有链接原样保留(删文件是用户的事)。"""
    if item not in registry.shared:
        raise CcmError(f"不在共享清单中: {item}")
    registry.shared.remove(item)
    registry.save(env)


def unlink_item(env, registry, name, item):
    """把某共享项复制成该 profile 的独立副本(脱离共享)。"""
    prof = registry.get(name)
    dst = prof.path / item
    if not dst.is_symlink():
        raise CcmError(f"{dst} 不是共享 symlink,无需 unlink")
    src = Path(os.path.realpath(dst))
    os.unlink(dst)
    try:
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    except BaseException:
        os.symlink(src, dst)   # 复制失败恢复链接,不留半成品
        raise


def _digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def diff_profiles(registry, a, b):
    """比较两 profile 的非共享顶层条目;返回差异列表。"""
    pa, pb = registry.get(a), registry.get(b)
    skip = set(registry.shared)
    out = []
    items = set()
    for base in (pa.path, pb.path):
        items |= {e for e in os.listdir(base) if e not in skip}
    for item in sorted(items):
        fa, fb = pa.path / item, pb.path / item
        ka = "缺失" if not os.path.lexists(fa) else \
            ("链接" if fa.is_symlink() else "目录" if fa.is_dir() else "文件")
        kb = "缺失" if not os.path.lexists(fb) else \
            ("链接" if fb.is_symlink() else "目录" if fb.is_dir() else "文件")
        if ka == kb == "文件":
            if _digest(fa) == _digest(fb):
                continue
            out.append({"item": item, "a": f"文件({os.path.getsize(fa)}B)",
                        "b": f"文件({os.path.getsize(fb)}B)"})
        elif ka != kb:
            out.append({"item": item, "a": ka, "b": kb})
        # 同为目录/链接:不深比(YAGNI,列出来也没法逐字节看)
    return out
