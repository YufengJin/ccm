import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class LinkAction:
    item: str
    status: str  # ok | missing | wrong | conflict | skip_no_source
    desired: Path
    actual: Optional[str] = None  # 现 readlink 值,或 "file"/"dir"


def link_plan(profile_path, shared_root, shared):
    """比对 profile 的共享 symlink 现状与期望,不做任何修改。"""
    out = []
    profile_path, shared_root = Path(profile_path), Path(shared_root)
    for item in shared:
        src = shared_root / item
        dst = profile_path / item
        if not os.path.lexists(src):
            out.append(LinkAction(item, "skip_no_source", src))
            continue
        if not os.path.lexists(dst):
            out.append(LinkAction(item, "missing", src))
        elif dst.is_symlink():
            actual = os.readlink(dst)
            out.append(LinkAction(item, "ok" if actual == str(src) else "wrong",
                                  src, actual))
        else:
            out.append(LinkAction(item, "conflict", src,
                                  "dir" if dst.is_dir() else "file"))
    return out


def apply_links(profile_path, shared_root, shared):
    """修 missing/wrong;conflict(实体)绝不动。幂等。返回执行后的最终 plan。"""
    profile_path, shared_root = Path(profile_path), Path(shared_root)
    for a in link_plan(profile_path, shared_root, shared):
        dst = profile_path / a.item
        if a.status == "wrong":
            os.unlink(dst)          # 只删 symlink,永不删实体
            os.symlink(a.desired, dst)
        elif a.status == "missing":
            os.symlink(a.desired, dst)
    return link_plan(profile_path, shared_root, shared)
