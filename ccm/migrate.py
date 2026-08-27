"""布局迁移:预置 dangling symlink + 两次 rename 的最小空窗算法。

journal 两段式(intent → done),SIGKILL/断电后按文件系统实际状态续跑或回滚。
"""
import ctypes
import ctypes.util
import errno
import os
from pathlib import Path

from ccm.config import atomic_write_json, fsync_dir, load_json
from ccm.errors import MigrationAborted

_STAGING_SUFFIX = ".ccm-staging"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_renameat2 = None


def _get_renameat2():
    """glibc renameat2;拿不到就返回 None(退回尽力而为的 lstat+rename)。"""
    global _renameat2
    if _renameat2 is None:
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                               use_errno=True)
            fn = libc.renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            _renameat2 = fn
        except (OSError, AttributeError):
            _renameat2 = False
    return _renameat2 or None


def rename_noreplace(src, dst):
    """rename,但目标已存在就失败而不是静默覆盖。

    普通 os.rename 会覆盖同类型目标,「先检查后 rename」的事后类型复核发现不了
    竞态创建的抢占者(codex 审核发现)。内核/文件系统不支持 RENAME_NOREPLACE 时
    退回尽力而为版本 —— 窗口仍在,但迁移的前置条件本来就是无并发写者。
    """
    src, dst = str(src), str(dst)
    fn = _get_renameat2()
    if fn is not None:
        ctypes.set_errno(0)
        if fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst),
              _RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise MigrationAborted(f"目标在操作期间被抢占创建,已中止: {dst}")
        if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTTY):
            raise OSError(err, os.strerror(err), src, None, dst)
    if os.path.lexists(dst):
        raise MigrationAborted(f"目标在操作期间被抢占创建,已中止: {dst}")
    os.rename(src, dst)


MIGRATE_JOURNAL = "migrate-journal.json"
OP_JOURNAL = "op-journal.json"       # add --move / shared add 等单步操作专用


class Journal:
    def __init__(self, env, ops=None, name=MIGRATE_JOURNAL):
        self._env = env
        self.ops = ops or []
        self.name = name

    @property
    def path(self):
        return self._env.ccm_home / "logs" / self.name

    @classmethod
    def load(cls, env, name=MIGRATE_JOURNAL):
        return cls(env, load_json(env.ccm_home / "logs" / name, default=[]), name=name)

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


def assert_no_pending_migration(env):
    """单步操作前的门禁:migrate journal 未完结时任何搬迁都可能踩坏回滚基线。"""
    if Journal.load(env).ops:
        raise MigrationAborted(
            "存在未完结的迁移 journal,拒绝执行搬迁类操作;先 ccm migrate --rollback")


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
    fsync_dir(staging.parent)
    try:
        rename_noreplace(old, new)
    except BaseException:
        os.unlink(staging)
        raise
    fsync_dir(new.parent)
    if _lstat_kind(new) not in ("dir", "file"):
        raise MigrationAborted(f"rename 后 {new} 类型异常: {_lstat_kind(new)}")
    try:
        rename_noreplace(staging, old)
    except BaseException:
        try:
            rename_noreplace(new, old)   # 回退;失败也不掩盖原异常,journal 能续跑
        except BaseException:
            pass
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise
    fsync_dir(old.parent)
    if _lstat_kind(old) != "symlink":
        raise MigrationAborted(f"rename 后 {old} 不是 symlink: {_lstat_kind(old)}")
    journal.done(idx)


def move_dir_with_compat(old, new, journal):
    _staged_swap(old, new, journal, "move_dir")


def split_item(src_dir, item, shared_root, journal):
    src = Path(src_dir) / item
    want = Path(shared_root) / item
    if src.is_symlink():
        # 只有**精确指向**预期共享项才算已拆分。以前任何 symlink 都当已完成,
        # 于是「迁移前 settings.json 已链到 dotfiles」会让 shared_root 里根本
        # 不生成源,所有 profile 都 skip 铺链,迁移却报成功(codex 审核发现)。
        actual = os.readlink(src)
        if actual == str(want):
            return
        raise MigrationAborted(
            f"{src} 已是 symlink 且指向 {actual}(预期 {want});"
            f"请先把它换成实体文件、或手工把目标移入 {shared_root} 后重试")
    if not os.path.lexists(src):
        return  # 该共享项在源中不存在,跳过
    _staged_swap(src, want, journal, "split_item")


def _undo_one(op):
    """逆转单条 op;按文件系统实际状态判定该步进行到哪(intent 未 done 亦可)。"""
    old, new = Path(op["old"]), Path(op["new"])
    staging = old.with_name(old.name + _STAGING_SUFFIX)
    k_old, k_new = _lstat_kind(old), _lstat_kind(new)
    if k_old in ("dir", "file") and k_new == "absent":
        pass  # 该步未开始(或已被逆转)
    elif k_old == "symlink" and os.readlink(old) == str(new) and k_new in ("dir", "file"):
        os.unlink(old)          # 完整完成态:摘链 + 搬回
        rename_noreplace(new, old)
    elif k_old == "absent" and k_new in ("dir", "file"):
        rename_noreplace(new, old)   # 两次 rename 之间崩溃
    else:
        raise MigrationAborted(
            f"回滚校验失败: {old}({k_old}) / {new}({k_new}) 状态非预期,疑被外部修改;"
            f"请人工检查后用备份恢复")
    if staging.is_symlink():
        os.unlink(staging)


def _undo_relink(op):
    """按 new/prev/absent 严格判定,绝不删除不是本操作写入的链接。

    以前无条件 unlink:intent 落盘后崩溃、别的进程在该路径建了链接,
    --rollback 会把它删掉(codex 审核发现)。
    """
    dst = Path(op["path"])
    prev, new = op.get("prev"), op.get("new")
    kind = _lstat_kind(dst)
    if kind == "symlink":
        actual = os.readlink(dst)
        if actual == prev:
            return                      # 已是回滚后的样子,幂等
        if new is not None and actual != new:
            raise MigrationAborted(
                f"回滚校验失败: {dst} 指向 {actual},既不是本次写入的 {new} "
                f"也不是原值 {prev};疑被外部修改,请人工检查")
        os.unlink(dst)
    elif kind != "absent":
        raise MigrationAborted(f"回滚校验失败: {dst} 是 {kind},预期 symlink 或不存在")
    if prev:
        os.symlink(prev, dst)


def rollback_ops(env, journal):
    """逆序回滚 journal 里的全部 op;完成后删 journal。"""
    for op in reversed(journal.ops):
        if op["op"] in ("move_dir", "split_item"):
            _undo_one(op)
        elif op["op"] == "relink":
            _undo_relink(op)
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
    # 预检:迁移要占用的 id 不能已经被注册表里的别的 profile 用掉
    from ccm.config import Registry
    existing = Registry.load(env).profiles
    moves = []
    for name, base in PROFILE_MAP:
        old = home / base
        if old.is_dir() and not old.is_symlink():
            new = env.accounts_root / name
            if os.path.lexists(new):
                raise MigrationAborted(f"目标已存在: {new}")
            if name in existing:
                raise MigrationAborted(
                    f"注册表里已有 profile {name}({existing[name].path}),"
                    f"与迁移要创建的同名;先 ccm rename {name} <新名> 再迁移")
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


def _doctor_gate(env, plan=None):
    """阶段 6:只把迁移不变量类 fail 当回滚信号(codex 审核采纳)。"""
    from ccm.config import Registry, load_state
    from ccm.doctor import INVARIANT_CHECKS, run_checks
    registry = Registry.load(env)
    results = run_checks(env, registry, load_state(env), fix=False)
    problems = [f"{r.check}[{r.subject}]: {r.msg}" for r in results
                if r.level == "fail" and r.check in INVARIANT_CHECKS]
    # doctor 的 shared-source 只会是 warn(清单里的可选项本来就允许不存在),
    # 所以那条不变量在门禁里其实是死的。这里改为按**本次计划**校验:计划里说要
    # 拆出去的源必须真的落在 shared_root(codex 审核发现)。
    for item in (plan or {}).get("splits", []):
        if not os.path.lexists(env.shared_root / item):
            problems.append(f"shared-source[{item}]: 计划要求拆出的共享源未生成")
    return problems


def execute_migration(env, plan, backup=True):
    from ccm.config import Registry, save_state
    from ccm.identity import resolve_identity
    from ccm.profiles import Profile

    journal = Journal.load(env)
    if journal.ops:
        raise MigrationAborted("存在未完结的迁移 journal;先 ccm migrate --rollback")
    # 失败时要还原到迁移前的注册表快照,而不是把用户既有的删掉
    prev_reg = load_json(env.ccm_home / "profiles.json")
    prev_state = load_json(env.ccm_home / "state.json")
    if backup:
        _backup(env, plan)
    # 记录本次创建的 root(rollback 时移除)
    for root in (env.accounts_root, env.shared_root):
        if not os.path.lexists(root):
            # write-ahead:先记 intent 再建,否则两句之间被 SIGKILL 会留下
            # 没人认领的 root,--rollback 说无事可回滚(codex 审核发现)
            idx = journal.intent({"op": "mkdir", "path": str(root)})
            root.mkdir(parents=True)
            journal.done(idx)
    try:
        # 阶段 2:搬 profile(旧路径留兼容 symlink)
        for m in plan["moves"]:
            move_dir_with_compat(Path(m["old"]), Path(m["new"]), journal)
        # 阶段 3:从 default 拆出共享库
        default_dir = env.accounts_root / DEFAULT_ID
        for item in plan["splits"]:
            split_item(default_dir, item, env.shared_root, journal)
        # 阶段 4:全部 profile 铺共享链接
        # 用既有注册表而非 Registry.empty():已经 ccm add 过的 profile 不能被抹掉
        registry = Registry.load(env)
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
        # §10 阶段 4 要求「为**全部** profile 生成共享链接」:迁移前就注册过的
        # profile 当时 shared_root 还是空的(skip_no_source),现在才补得上。
        moved = {m["profile"] for m in plan["moves"]}
        for name, prof in registry.profiles.items():
            if name not in moved and prof.path.is_dir():
                journaled_apply_links(prof.path, env.shared_root,
                                      registry.shared, journal)
        # 阶段 5:落注册表与 state
        registry.default_profile = DEFAULT_ID
        registry.save(env)
        save_state(env, DEFAULT_ID, "ccm migrate")
        # 阶段 6:doctor 门禁
        problems = _doctor_gate(env, plan)
        if problems:
            raise MigrationAborted("doctor 不变量检查失败: " + "; ".join(problems))
    except BaseException:
        rollback_ops(env, Journal.load(env))
        # 还原迁移前的注册表快照:之前是什么就写回什么,没有才删除
        for fname, snap in (("profiles.json", prev_reg), ("state.json", prev_state)):
            if snap is None:
                try:
                    os.unlink(env.ccm_home / fname)
                except FileNotFoundError:
                    pass
            else:
                atomic_write_json(env.ccm_home / fname, snap)
        raise
    journal.clear()


def cleanup(env, scan=None):
    """长跑进程结束后:删兼容 symlink(default 的 ~/.claude 永久保留——
    它是未设 CLAUDE_CONFIG_DIR 时 claude 的默认落点);.bashrc 只报告不自动改。

    活跃进程门禁:§10 的「长跑进程结束后」不能只靠用户自觉 —— 有进程的
    CLAUDE_CONFIG_DIR 还指着某条兼容链接时删它,该进程后续按路径 open 会直接
    ENOENT。逐条检查,被占用的跳过并提示(实际清理时踩出来的)。
    """
    from ccm.config import Registry
    from ccm.procs import UNKNOWN, profile_active_pids, scan_claude_procs
    actions = []
    registry = Registry.load(env)
    if scan is None:
        scan = scan_claude_procs(env.proc_root, env.user_home)
    changed = False
    for prof in registry.profiles.values():
        if prof.name == registry.default_profile:
            continue   # 默认落点的 ~/.claude 永久保留
        cl = prof.compat_link
        if not (cl and cl.is_symlink()):
            continue
        pids = profile_active_pids(prof.path, cl, scan)
        if pids:
            actions.append(f"跳过 {cl}: 有活跃 claude 进程 {sorted(pids)}"
                           f"(等它们退出后重跑 ccm migrate --cleanup)")
            continue
        os.unlink(cl)
        prof.compat_link = None   # 注册表同步,否则 doctor 报兼容链接缺失
        changed = True
        actions.append(f"已删除兼容链接 {cl}")
    if scan.get(UNKNOWN):
        actions.append(f"注意: 有 {len(scan[UNKNOWN])} 个无法归属的疑似 claude 进程"
                       f"{sorted(scan[UNKNOWN])},已按不占用处理;若清理后它们报错,"
                       f"用 ccm doctor --fix 重建链接")
    if changed:
        registry.save(env)
    rc = env.user_home / ".bashrc"
    if rc.exists():
        for i, line in enumerate(rc.read_text().splitlines(), 1):
            if "cca()" in line or "ccb()" in line or "claude-a=" in line or "claude-b=" in line:
                actions.append(f"建议手动删除 {rc}:{i}: {line.strip()}")
    return actions
