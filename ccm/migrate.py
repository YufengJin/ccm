"""布局迁移:预置 dangling symlink + 两次 rename 的最小空窗算法。

journal 两段式(intent → done),SIGKILL/断电后按文件系统实际状态续跑或回滚。
"""
import os
from pathlib import Path

from ccm.config import atomic_write_json, load_json
from ccm.errors import MigrationAborted

_STAGING_SUFFIX = ".ccm-staging"


class Journal:
    def __init__(self, env, ops=None):
        self._env = env
        self.ops = ops or []

    @property
    def path(self):
        return self._env.ccm_home / "logs" / "migrate-journal.json"

    @classmethod
    def load(cls, env):
        return cls(env, load_json(env.ccm_home / "logs" / "migrate-journal.json",
                                  default=[]))

    def _flush(self):
        atomic_write_json(self.path, self.ops, mode=0o600)

    def intent(self, op):
        self.ops.append({**op, "status": "intent"})
        self._flush()
        return len(self.ops) - 1

    def done(self, idx):
        self.ops[idx]["status"] = "done"
        self._flush()

    def clear(self):
        self.ops = []
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def _lstat_kind(p):
    p = Path(p)
    if p.is_symlink():
        return "symlink"
    if not os.path.lexists(p):
        return "absent"
    return "dir" if p.is_dir() else "file"


def _staged_swap(old, new, journal, op_name):
    """核心算法:staging symlink → rename(old,new) → rename(staging,old)。

    每次 rename 后 lstat 复核结果类型,发现被抢占创建即中止(codex 审核采纳)。
    """
    old, new = Path(old), Path(new)
    if old.is_symlink():
        if os.readlink(old) == str(new):
            return  # 已迁,幂等
        raise MigrationAborted(f"{old} 已是 symlink 但指向 {os.readlink(old)},预期 {new}")
    if not os.path.lexists(old):
        raise MigrationAborted(f"源不存在: {old}")
    if os.path.lexists(new):
        raise MigrationAborted(f"目标已存在: {new}")
    new.parent.mkdir(parents=True, exist_ok=True)
    staging = old.with_name(old.name + _STAGING_SUFFIX)
    if staging.is_symlink():
        os.unlink(staging)  # 上次崩溃残留(仅 symlink)
    elif os.path.lexists(staging):
        raise MigrationAborted(f"staging 位置被占用且不是 symlink: {staging}")
    idx = journal.intent({"op": op_name, "old": str(old), "new": str(new)})
    os.symlink(new, staging)  # 此刻 dangling,合法
    try:
        os.rename(old, new)
    except OSError:
        os.unlink(staging)
        raise
    if _lstat_kind(new) not in ("dir", "file"):
        raise MigrationAborted(f"rename 后 {new} 类型异常: {_lstat_kind(new)}")
    try:
        os.rename(staging, old)
    except OSError:
        os.rename(new, old)  # 回退
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise
    if _lstat_kind(old) != "symlink":
        raise MigrationAborted(f"rename 后 {old} 不是 symlink: {_lstat_kind(old)}")
    journal.done(idx)


def move_dir_with_compat(old, new, journal):
    _staged_swap(old, new, journal, "move_dir")


def split_item(src_dir, item, shared_root, journal):
    src = Path(src_dir) / item
    if src.is_symlink():
        return  # 已拆分或本来就是链接,幂等
    if not os.path.lexists(src):
        return  # 该共享项在源中不存在,跳过
    _staged_swap(src, Path(shared_root) / item, journal, "split_item")


def _undo_one(op):
    """逆转单条 op;按文件系统实际状态判定该步进行到哪(intent 未 done 亦可)。"""
    old, new = Path(op["old"]), Path(op["new"])
    staging = old.with_name(old.name + _STAGING_SUFFIX)
    k_old, k_new = _lstat_kind(old), _lstat_kind(new)
    if k_old in ("dir", "file") and k_new == "absent":
        pass  # 该步未开始(或已被逆转)
    elif k_old == "symlink" and os.readlink(old) == str(new) and k_new in ("dir", "file"):
        os.unlink(old)          # 完整完成态:摘链 + 搬回
        os.rename(new, old)
    elif k_old == "absent" and k_new in ("dir", "file"):
        os.rename(new, old)     # 两次 rename 之间崩溃
    else:
        raise MigrationAborted(
            f"回滚校验失败: {old}({k_old}) / {new}({k_new}) 状态非预期,疑被外部修改;"
            f"请人工检查后用备份恢复")
    if staging.is_symlink():
        os.unlink(staging)


def rollback_ops(env, journal):
    """逆序回滚 journal 里的全部 op;完成后删 journal。"""
    for op in reversed(journal.ops):
        if op["op"] in ("move_dir", "split_item"):
            _undo_one(op)
        elif op["op"] == "relink":
            dst = Path(op["path"])
            if dst.is_symlink():
                os.unlink(dst)
            if op.get("prev"):
                os.symlink(op["prev"], dst)
        elif op["op"] == "mkdir":
            try:
                os.rmdir(op["path"])
            except OSError:
                pass  # 非空/已删:保留,不算失败
    journal.clear()


# ---------------- 全流程 ----------------

# ws02/本机现状的固定映射(spec §4)
# 编码 id:a1=原 ~/.claude(默认落点), a2/a3=原 -a/-b;身份看 email,不用别名
PROFILE_MAP = [("a1", ".claude"), ("a2", ".claude-a"), ("a3", ".claude-b")]
DEFAULT_ID = "a1"


def build_plan(env):
    """探测现状,产出执行计划;顺带做预检(不动任何文件)。"""
    from ccm.config import DEFAULT_SHARED
    from ccm.procs import scan_claude_procs

    home = env.user_home
    if not (home / ".claude").is_dir() or (home / ".claude").is_symlink():
        raise MigrationAborted(f"{home / '.claude'} 不存在或已迁移")
    # 预检:目标 root 必须不存在或为空(codex 审核采纳)
    for root in (env.accounts_root, env.shared_root):
        if os.path.lexists(root) and (not root.is_dir() or any(root.iterdir())):
            raise MigrationAborted(f"目标已存在且非空: {root}")
    moves = []
    for name, base in PROFILE_MAP:
        old = home / base
        if old.is_dir() and not old.is_symlink():
            new = env.accounts_root / name
            if os.path.lexists(new):
                raise MigrationAborted(f"目标已存在: {new}")
            moves.append({"profile": name, "old": str(old), "new": str(new)})
    splits = [item for item in DEFAULT_SHARED
              if os.path.lexists(home / ".claude" / item)]
    return {"moves": moves, "splits": splits,
            "active_procs": {k: sorted(v)
                             for k, v in scan_claude_procs(env.proc_root, home).items()}}


def journaled_apply_links(profile_path, shared_root, shared, journal):
    """apply_links 的 journal 版:记录每条被改写链接的原指向,rollback 可还原。

    (E2E 抓到的 bug:不记账的链接改写会让 rollback 留下 dangling symlink。)
    """
    from ccm.layout import link_plan
    for a in link_plan(profile_path, shared_root, shared):
        if a.status not in ("missing", "wrong"):
            continue  # conflict/skip/ok 一律不动
        dst = Path(profile_path) / a.item
        idx = journal.intent({"op": "relink", "path": str(dst),
                              "prev": a.actual if a.status == "wrong" else None,
                              "new": str(a.desired)})
        if a.status == "wrong":
            os.unlink(dst)
        os.symlink(a.desired, dst)
        journal.done(idx)


def _backup(env, plan):
    import tarfile
    import time as _t
    dest_dir = env.ccm_home / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = dest_dir / f"pre-migrate-{_t.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        with tarfile.open(fileobj=fh, mode="w:gz", dereference=False) as tar:
            for m in plan["moves"]:
                tar.add(m["old"], arcname=Path(m["old"]).name)
    return dest


def _doctor_gate(env):
    """阶段 6:只把迁移不变量类 fail 当回滚信号(codex 审核采纳)。"""
    from ccm.config import Registry, load_state
    from ccm.doctor import INVARIANT_CHECKS, run_checks
    registry = Registry.load(env)
    results = run_checks(env, registry, load_state(env), fix=False)
    return [f"{r.check}[{r.subject}]: {r.msg}" for r in results
            if r.level == "fail" and r.check in INVARIANT_CHECKS]


def execute_migration(env, plan, backup=True):
    from ccm.config import Registry, save_state
    from ccm.identity import resolve_identity
    from ccm.profiles import Profile

    journal = Journal.load(env)
    if journal.ops:
        raise MigrationAborted("存在未完结的迁移 journal;先 ccm migrate --rollback")
    if backup:
        _backup(env, plan)
    # 记录本次创建的 root(rollback 时移除)
    for root in (env.accounts_root, env.shared_root):
        if not os.path.lexists(root):
            root.mkdir(parents=True)
            journal.intent({"op": "mkdir", "path": str(root)})
            journal.done(len(journal.ops) - 1)
    try:
        # 阶段 2:搬 profile(旧路径留兼容 symlink)
        for m in plan["moves"]:
            move_dir_with_compat(Path(m["old"]), Path(m["new"]), journal)
        # 阶段 3:从 default 拆出共享库
        default_dir = env.accounts_root / DEFAULT_ID
        for item in plan["splits"]:
            split_item(default_dir, item, env.shared_root, journal)
        # 阶段 4:全部 profile 铺共享链接
        registry = Registry.empty(env)
        for m in plan["moves"]:
            name = m["profile"]
            path = env.accounts_root / name
            journaled_apply_links(path, env.shared_root, registry.shared, journal)
            ident = resolve_identity(path, env.user_home,
                                     allow_legacy=(name == DEFAULT_ID)) or {}
            registry.profiles[name] = Profile(
                name=name, path=path, compat_link=Path(m["old"]),
                account_uuid=ident.get("account_uuid"), email=ident.get("email"),
                subscription=ident.get("subscription"),
                rate_limit_tier=ident.get("rate_limit_tier"))
        # 阶段 5:落注册表与 state
        registry.default_profile = DEFAULT_ID
        registry.save(env)
        save_state(env, DEFAULT_ID, "ccm migrate")
        # 阶段 6:doctor 门禁
        problems = _doctor_gate(env)
        if problems:
            raise MigrationAborted("doctor 不变量检查失败: " + "; ".join(problems))
    except BaseException:
        rollback_ops(env, Journal.load(env))
        # 迁移中途写的注册表/State 一并清掉,回到未迁移状态
        for f in ("profiles.json", "state.json"):
            try:
                os.unlink(env.ccm_home / f)
            except FileNotFoundError:
                pass
        raise
    journal.clear()


def cleanup(env):
    """长跑进程结束后:删兼容 symlink(default 的 ~/.claude 永久保留——
    它是未设 CLAUDE_CONFIG_DIR 时 claude 的默认落点);.bashrc 只报告不自动改。"""
    from ccm.config import Registry
    actions = []
    registry = Registry.load(env)
    changed = False
    for prof in registry.profiles.values():
        if prof.name == registry.default_profile:
            continue   # 默认落点的 ~/.claude 永久保留
        cl = prof.compat_link
        if cl and cl.is_symlink():
            os.unlink(cl)
            prof.compat_link = None   # 注册表同步,否则 doctor 报兼容链接缺失
            changed = True
            actions.append(f"已删除兼容链接 {cl}")
    if changed:
        registry.save(env)
    rc = env.user_home / ".bashrc"
    if rc.exists():
        for i, line in enumerate(rc.read_text().splitlines(), 1):
            if "cca()" in line or "ccb()" in line or "claude-a=" in line or "claude-b=" in line:
                actions.append(f"建议手动删除 {rc}:{i}: {line.strip()}")
    return actions
