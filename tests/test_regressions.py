"""2026-08-27 深度审查(含 codex 交叉审查)查出的问题的回归测试。

每个 test 对应一条已复现的缺陷,命名里带原始症状,便于回看。
"""
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.config import (Registry, registry_lock, registry_transaction,
                        validate_roots)
from ccm.errors import CcmError, CredentialsMissing, MigrationAborted
from ccm.lifecycle import add_profile, remove_profile
from ccm.migrate import (MIGRATE_JOURNAL, OP_JOURNAL, Journal, build_plan,
                         execute_migration)
from ccm.profiles import Profile


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-reg-"))
        self.env = make_fake_home(self.tmp)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- 注册表

class TestRegistryConcurrency(Base):
    def test_transaction_prevents_lost_update(self):
        """两个进程各自 load 再 add,以前后写的会抹掉先写的。"""
        with registry_transaction(self.env) as r1:
            add_profile(self.env, r1, "p1")
        with registry_transaction(self.env) as r2:
            add_profile(self.env, r2, "p2")
        self.assertEqual(sorted(Registry.load(self.env).profiles), ["p1", "p2"])

    def test_lock_is_reentrant(self):
        """registry_transaction 内部还会调 Registry.save(),后者自己也取锁。

        flock 按 open file description 计,不可重入的实现会在这里自锁死。
        """
        with registry_lock(self.env):
            with registry_lock(self.env):
                Registry.empty(self.env).save(self.env)
        self.assertTrue((self.env.ccm_home / "profiles.json").exists())

    def test_missing_path_field_is_ccm_error(self):
        self.env.ccm_home.mkdir(parents=True, exist_ok=True)
        (self.env.ccm_home / "profiles.json").write_text(
            json.dumps({"version": 2, "profiles": {"a1": {}}}))
        with self.assertRaises(CcmError):
            Registry.load(self.env)

    def test_nested_roots_rejected(self):
        with self.assertRaises(CcmError):
            validate_roots(self.tmp / "root" / "shared", self.tmp / "root", {})
        prof = Profile(name="x", path=self.tmp / "shared" / "x")
        with self.assertRaises(CcmError):
            validate_roots(self.tmp / "shared", self.tmp / "accounts", {"x": prof})


# ---------------------------------------------------------------- 迁移

class TestMigratePreservesRegistry(Base):
    def test_existing_profiles_survive_migration(self):
        """以前 execute_migration 用 Registry.empty() 覆盖,已注册的 profile 全丢。"""
        ext = self.tmp / "external"
        ext.mkdir()
        with registry_transaction(self.env) as reg:
            add_profile(self.env, reg, "mine", import_dir=ext)
        execute_migration(self.env, build_plan(self.env), backup=False)
        after = Registry.load(self.env).profiles
        self.assertIn("mine", after)
        self.assertEqual(sorted(after), ["a1", "a2", "a3", "mine"])

    def test_name_collision_aborts_before_touching_files(self):
        (self.tmp / "ext-a1").mkdir()
        with registry_transaction(self.env) as reg:
            add_profile(self.env, reg, "a1", import_dir=self.tmp / "ext-a1")
        with self.assertRaises(MigrationAborted):
            build_plan(self.env)
        self.assertTrue((self.tmp / ".claude").is_dir())
        self.assertFalse((self.tmp / ".claude").is_symlink())

    def test_failed_migration_restores_registry_snapshot(self):
        """失败路径以前无条件 unlink profiles.json,把用户既有注册表删掉。"""
        ext = self.tmp / "external"
        ext.mkdir()
        with registry_transaction(self.env) as reg:
            add_profile(self.env, reg, "mine", import_dir=ext)
        before = (self.env.ccm_home / "profiles.json").read_text()
        plan = build_plan(self.env)
        plan["moves"].append({"profile": "nope", "old": str(self.tmp / "no-such"),
                              "new": str(self.env.accounts_root / "nope")})
        with self.assertRaises(MigrationAborted):
            execute_migration(self.env, plan, backup=False)
        self.assertEqual((self.env.ccm_home / "profiles.json").read_text(), before)
        self.assertIn("mine", Registry.load(self.env).profiles)


class TestJournalIsolation(Base):
    def test_add_move_does_not_touch_migrate_journal(self):
        """以前 add --import --move 复用并 clear() migrate journal,回滚能力全丢。"""
        j = Journal.load(self.env)
        j.intent({"op": "move_dir", "old": "/x", "new": "/y"})
        ext = self.tmp / "ext"
        ext.mkdir()
        reg = Registry.empty(self.env)
        with self.assertRaises(MigrationAborted):     # 未完结迁移时直接拒绝
            add_profile(self.env, reg, "imp", import_dir=ext, move=True)
        self.assertEqual(len(Journal.load(self.env).ops), 1)

    def test_op_journal_is_a_separate_file(self):
        execute_migration(self.env, build_plan(self.env), backup=False)
        ext = self.tmp / "ext"
        ext.mkdir()
        with registry_transaction(self.env) as reg:
            add_profile(self.env, reg, "imp", import_dir=ext, move=True)
        self.assertNotEqual(MIGRATE_JOURNAL, OP_JOURNAL)
        self.assertFalse((self.env.ccm_home / "logs" / OP_JOURNAL).exists())
        self.assertTrue((self.env.accounts_root / "imp").is_dir())


# ---------------------------------------------------------------- 生命周期

class TestRemoveLastProfile(Base):
    def test_removing_only_profile_does_not_crash(self):
        """default_profile 为 None 时,以前 sorted(profiles)[0] 抛 IndexError。"""
        from ccm.config import save_state
        with registry_transaction(self.env) as reg:
            add_profile(self.env, reg, "solo")
        save_state(self.env, "solo", "test")
        with registry_transaction(self.env) as reg:
            remove_profile(self.env, reg, "solo", scan={})
        self.assertEqual(Registry.load(self.env).profiles, {})
        self.assertFalse((self.env.ccm_home / "state.json").exists())


# ---------------------------------------------------------------- 用量解析

class TestUsageParsing(unittest.TestCase):
    def test_numeric_resets_at_does_not_raise(self):
        """resets_at 给数字时,以前 fromisoformat 抛 TypeError 冲垮整个 usage。"""
        from ccm.usage import extract_pcts
        out = extract_pcts({"limits": [{"kind": "session", "percent": 10,
                                        "resets_at": 1787744335}]})
        self.assertEqual(out["five_hour_pct"], 10)

    def test_z_suffix_parsed(self):
        """Python 3.10 的 fromisoformat 不认 Z;CI 矩阵里有 3.10。"""
        from ccm.usage import _parse_reset
        self.assertIsNotNone(_parse_reset("2026-08-28T07:00:00Z"))

    def test_garbage_payload_yields_none(self):
        from ccm.usage import extract_pcts
        out = extract_pcts({"five_hour": "garbage", "limits": ["nope"]})
        self.assertIsNone(out["five_hour_pct"])


# ---------------------------------------------------------------- 凭证刷新

class TestRefreshHardening(Base):
    def setUp(self):
        super().setUp()
        self.prof = Profile(name="work", path=self.tmp / ".claude-b")
        self._expire(self.prof.path)

    @staticmethod
    def _expire(path):
        cp = Path(path) / ".credentials.json"
        blob = json.loads(cp.read_text())
        blob["claudeAiOauth"]["expiresAt"] = 1
        cp.write_text(json.dumps(blob))

    def test_missing_refresh_token_is_credentials_missing(self):
        cp = self.prof.path / ".credentials.json"
        blob = json.loads(cp.read_text())
        blob["claudeAiOauth"].pop("refreshToken")
        cp.write_text(json.dumps(blob))
        from ccm.refresh import refresh_profile
        with self.assertRaises(CredentialsMissing):
            refresh_profile(self.env, self.prof, scan={})

    def test_200_without_access_token_never_writes(self):
        """服务端返回 {"error": ...} 但状态码 200 时,以前是 KeyError traceback。"""
        from ccm.refresh import refresh_profile
        before = (self.prof.path / ".credentials.json").read_text()
        r = refresh_profile(self.env, self.prof, scan={},
                            opener=lambda req, timeout=None:
                                _Resp({"error": "invalid_grant"}))
        self.assertEqual(r["status"], "failed")
        self.assertEqual((self.prof.path / ".credentials.json").read_text(), before)

    def test_siblings_with_same_refresh_token_get_synced(self):
        """同源凭证不同步的话,兄弟 profile 手里那份轮换后就作废了。"""
        from ccm.refresh import refresh_profile
        sib = Profile(name="clone", path=self.tmp / "clone")
        sib.path.mkdir()
        (sib.path / ".credentials.json").write_bytes(
            (self.prof.path / ".credentials.json").read_bytes())
        r = refresh_profile(
            self.env, self.prof, scan={}, siblings=[sib],
            opener=lambda req, timeout=None: _Resp(
                {"access_token": "NEW-AT", "refresh_token": "NEW-RT",
                 "expires_in": 28800}))
        self.assertEqual(r["status"], "refreshed")
        got = json.loads((sib.path / ".credentials.json").read_text())["claudeAiOauth"]
        self.assertEqual(got["refreshToken"], "NEW-RT")
        self.assertEqual(got["accessToken"], "NEW-AT")

    def test_backs_up_old_credentials_before_write(self):
        from ccm.refresh import refresh_profile
        refresh_profile(self.env, self.prof, scan={},
                        opener=lambda req, timeout=None: _Resp(
                            {"access_token": "A", "refresh_token": "R",
                             "expires_in": 100}))
        baks = list((self.env.ccm_home / "backups").glob("creds-work-*.json"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(os.stat(baks[0]).st_mode & 0o777, 0o600)

    def test_unreadable_proc_counts_as_active(self):
        """environ 读不到但 cmdline 像 claude → 必须按「有活跃进程」处理(§9)。"""
        from ccm.procs import UNKNOWN, scan_claude_procs
        from ccm.refresh import refresh_profile
        proc = self.tmp / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cmdline").write_bytes(b"node\0/usr/bin/claude\0")
        os.chmod(proc / "4242" / "cmdline", 0o444)
        scan = scan_claude_procs(proc, self.tmp)
        self.assertIn(UNKNOWN, scan)
        r = refresh_profile(self.env, self.prof, scan=scan)
        self.assertEqual(r["status"], "skipped-active")


# ---------------------------------------------------------------- 解包安全

class TestRestoreSafety(Base):
    def _victim(self):
        v = self.tmp / "VICTIM"
        v.mkdir()
        return v

    def test_symlink_toplevel_rejected(self):
        """顶层成员是绝对 symlink 时,以前 apply_links 会往链接目标里写文件。"""
        from ccm.backup import restore_backup
        victim = self._victim()
        arc = self.tmp / "evil.tar.gz"
        with tarfile.open(arc, "w:gz") as tar:
            ln = tarfile.TarInfo("evil")
            ln.type = tarfile.SYMTYPE
            ln.linkname = str(victim)
            tar.addfile(ln)
        self.env.ccm_home.mkdir(parents=True, exist_ok=True)
        self.env.shared_root.mkdir(parents=True, exist_ok=True)
        (self.env.shared_root / "CLAUDE.md").write_text("shared")
        with self.assertRaises(CcmError):
            restore_backup(self.env, Registry.empty(self.env), arc)
        self.assertEqual(os.listdir(victim), [])

    def test_write_through_symlink_rejected(self):
        from ccm.backup import restore_backup
        victim = self._victim()
        arc = self.tmp / "evil2.tar.gz"
        with tarfile.open(arc, "w:gz") as tar:
            d = tarfile.TarInfo("evil")
            d.type, d.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(d)
            ln = tarfile.TarInfo("evil/esc")
            ln.type = tarfile.SYMTYPE
            ln.linkname = str(victim)
            tar.addfile(ln)
            data = b"PWNED\n"
            fi = tarfile.TarInfo("evil/esc/p.txt")
            fi.size = len(data)
            tar.addfile(fi, io.BytesIO(data))
        self.env.ccm_home.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(CcmError):
            restore_backup(self.env, Registry.empty(self.env), arc)
        self.assertEqual(os.listdir(victim), [])

    def test_restore_works_without_preexisting_ccm_home(self):
        """~/.ccm 还不存在时,mkdtemp 以前直接 FileNotFoundError。"""
        from ccm.backup import restore_backup
        arc = self.tmp / "ok.tar.gz"
        with tarfile.open(arc, "w:gz") as tar:
            d = tarfile.TarInfo("fresh")
            d.type, d.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(d)
        self.assertFalse(self.env.ccm_home.exists())
        prof = restore_backup(self.env, Registry.empty(self.env), arc)
        self.assertTrue(prof.path.is_dir())


# ---------------------------------------------------------------- CLI

class TestCliGuards(Base):
    def test_parse_days_rejects_garbage(self):
        from ccm.cli import _parse_days
        self.assertEqual(_parse_days("7d", "--since"), 7)
        self.assertEqual(_parse_days("30", "--since"), 30)
        for bad in ("1w", "", "-3d", "abc"):
            with self.assertRaises(CcmError):
                _parse_days(bad, "--since")

    def test_statusline_identity_uses_legacy_fallback(self):
        """默认 profile 的身份在 ~/.claude.json 里;漏传 default_name 就查不到。"""
        from ccm.usage import _fresh_identity
        execute_migration(self.env, build_plan(self.env), backup=False)
        reg = Registry.load(self.env)
        a1 = reg.profiles["a1"]
        a1.account_uuid = None
        uuid, _ = _fresh_identity(a1, self.env, reg.default_profile)
        self.assertTrue(uuid)


# ------------------------------------------- codex 第 3 轮:cost 增量扫描

class TestCostIncremental(Base):
    def _setup(self):
        execute_migration(self.env, build_plan(self.env), backup=False)
        from ccm.cost import CostDB
        reg = Registry.load(self.env)
        return reg, CostDB(self.env), next(
            (self.env.shared_root / "projects").glob("*/*.jsonl"))

    def test_truncate_and_rewrite_same_inode(self):
        """同 inode ftruncate(0) 后重写到 >= 旧 size:只看 size/mtime 挡不住。

        症状是旧事件变幽灵、新文件前缀漏计 —— 条数可能还对,内容是错的。
        """
        from ccm.cost import scan_projects
        reg, db, f = self._setup()
        scan_projects(self.env, reg, db)
        ev = json.loads(f.read_text().strip())
        with open(f, "r+b") as fh:
            fh.truncate(0)
            for i in (2, 3, 4):
                e = dict(ev, requestId=f"r{i}", uuid=f"u{i}")
                fh.write((json.dumps(e) + "\n").encode())
        scan_projects(self.env, reg, db)
        got = sorted(r[0] for r in db.conn.execute("SELECT rid FROM events"))
        self.assertEqual(got, ["r2", "r3", "r4"])

    def test_event_in_two_files_survives_one_being_replaced(self):
        """events 主键含 src:否则 `DELETE src=A` 会连带删掉同样存在于 B 的事件。"""
        from ccm.cost import scan_projects
        reg, db, f = self._setup()
        twin = f.with_name("twin.jsonl")
        twin.write_bytes(f.read_bytes())          # 同一事件出现在两个 jsonl
        scan_projects(self.env, reg, db)
        self.assertEqual(
            db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        f.write_text(json.dumps(dict(json.loads(f.read_text().strip()),
                                     requestId="rX", uuid="uX")) + "\n")
        scan_projects(self.env, reg, db)
        rids = sorted(r[0] for r in db.conn.execute(
            "SELECT DISTINCT rid FROM events"))
        self.assertIn("r1", rids, "twin 里那份不该被 DELETE src=f 连坐")
        self.assertIn("rX", rids)

    def test_aggregate_deduplicates_multi_source_events(self):
        """主键加了 src 之后,聚合必须按 (sid,rid,uid) 去重,不能重复计数。"""
        from ccm.cost import aggregate, scan_projects
        reg, db, f = self._setup()
        f.with_name("twin.jsonl").write_bytes(f.read_bytes())
        scan_projects(self.env, reg, db)
        rows = aggregate(db, by="model")
        self.assertEqual(sum(r["events"] for r in rows), 1)


# ------------------------------------------- codex 第 3 轮:迁移原语

class TestMigratePrimitives(Base):
    def test_split_item_rejects_foreign_symlink(self):
        """迁移前共享项已链到别处(如 dotfiles)时,以前静默当作「已拆分」。

        结果:shared_root 里没有源,所有 profile skip 铺链,迁移却报成功。
        """
        dot = self.tmp / "dotfiles"
        dot.mkdir()
        (dot / "settings.json").write_text('{"from": "dotfiles"}')
        os.unlink(self.tmp / ".claude" / "settings.json")
        os.symlink(dot / "settings.json", self.tmp / ".claude" / "settings.json")
        with self.assertRaises(MigrationAborted):
            execute_migration(self.env, build_plan(self.env), backup=False)
        self.assertTrue((self.tmp / ".claude").is_dir())      # 已回滚

    def test_relink_rollback_keeps_foreign_symlink(self):
        """回滚只删本操作写入的链接,不碰别人后来创建的。"""
        from ccm.migrate import rollback_ops
        self.env.ccm_home.mkdir(parents=True, exist_ok=True)
        d = self.tmp / "prof"
        d.mkdir()
        j = Journal.load(self.env)
        j.intent({"op": "relink", "path": str(d / "x"), "prev": None,
                  "new": str(self.tmp / "shared" / "x")})
        os.symlink(self.tmp / "OTHER", d / "x")   # 别的进程建的
        with self.assertRaises(MigrationAborted):
            rollback_ops(self.env, Journal.load(self.env))
        self.assertTrue((d / "x").is_symlink())

    def test_rename_noreplace_refuses_existing_target(self):
        from ccm.migrate import rename_noreplace
        (self.tmp / "src").write_text("a")
        (self.tmp / "dst").write_text("b")
        with self.assertRaises(MigrationAborted):
            rename_noreplace(self.tmp / "src", self.tmp / "dst")
        self.assertEqual((self.tmp / "dst").read_text(), "b")

    def test_mkdir_intent_written_before_creation(self):
        """write-ahead:两句之间被 SIGKILL 时 journal 里必须已有这条 op。"""
        import ccm.migrate as M
        seen = []
        real = M.Journal.intent

        def spy(self_j, op):
            seen.append((op.get("op"), os.path.lexists(op.get("path", "/nope"))))
            return real(self_j, op)

        M.Journal.intent = spy
        try:
            execute_migration(self.env, build_plan(self.env), backup=False)
        finally:
            M.Journal.intent = real
        mkdirs = [(o, existed) for o, existed in seen if o == "mkdir"]
        self.assertTrue(mkdirs)
        self.assertTrue(all(not existed for _o, existed in mkdirs),
                        "写 intent 时目录不该已经存在")

    def test_gate_catches_missing_shared_source(self):
        """doctor 的 shared-source 只会是 warn,门禁必须自己按计划校验。"""
        from ccm.migrate import _doctor_gate
        execute_migration(self.env, build_plan(self.env), backup=False)
        os.unlink(self.env.shared_root / "CLAUDE.md")
        self.assertTrue(_doctor_gate(self.env, {"splits": ["CLAUDE.md"]}))
        self.assertFalse(_doctor_gate(self.env, {"splits": []}))


# ------------------------------------------- codex 第 3 轮:selector / usage / doctor

class TestSelectorStages(Base):
    def test_empty_uuid_multi_match_is_ambiguous(self):
        """空 uuid = 无法证明同 account,不能当成同 account 静默挑一个。"""
        from ccm.selector import resolve_profile
        execute_migration(self.env, build_plan(self.env), backup=False)
        reg = Registry.load(self.env)
        g = self.tmp / "ghost"
        g.mkdir()
        reg.profiles["ghost"] = Profile(name="ghost", path=g,
                                        email="jyf@example.com")
        reg.save(self.env)
        reg = Registry.load(self.env)
        with self.assertRaises(CcmError):
            resolve_profile(self.env, reg, "jyf")

    def test_uuid_prefix_is_a_later_stage_than_email_substring(self):
        from ccm.selector import resolve_profile
        execute_migration(self.env, build_plan(self.env), backup=False)
        reg = Registry.load(self.env)
        self.assertEqual(resolve_profile(self.env, reg, "work@ex").name, "a3")
        self.assertEqual(resolve_profile(self.env, reg, "acct-B").name, "a3")


class TestUsageProbing(Base):
    def _rows(self, opener):
        from ccm.usage import gather_usage
        execute_migration(self.env, build_plan(self.env), backup=False)
        return gather_usage(self.env, Registry.load(self.env), opener=opener)

    def test_falls_through_to_second_token(self):
        """最晚到期的 token 被撤销时,不该把整组降级成 cache。"""
        from ccm.errors import ApiError
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                raise __import__("urllib.error", fromlist=["x"]).HTTPError(
                    req.full_url, 401, "revoked", {}, None)
            return _Resp({"limits": [{"kind": "session", "percent": 7,
                                      "resets_at": "2026-08-28T07:00:00Z"}]})
        rows = self._rows(opener)
        live = [r for r in rows if r.source == "live"]
        self.assertTrue(live)
        self.assertEqual(live[0].five_hour_pct, 7)
        self.assertIsNotNone(live[0].probe_profile)

    def test_pick_best_returns_the_token_that_worked(self):
        from ccm.usage import pick_best
        rows = self._rows(lambda req, timeout=None: _Resp(
            {"limits": [{"kind": "session", "percent": 5}]}))
        reg = Registry.load(self.env)
        name, _ = pick_best(rows, reg, self.env)
        probed = {r.probe_profile for r in rows if r.source == "live"}
        self.assertIn(name, probed)

    def test_limits_percent_does_not_block_resets_fallback(self):
        from ccm.usage import extract_pcts
        out = extract_pcts({
            "limits": [{"kind": "session", "percent": 42, "resets_at": None}],
            "five_hour": {"utilization": 99, "resets_at": "2099-01-01T00:00:00Z"}})
        self.assertEqual(out["five_hour_pct"], 42)        # limits 优先
        self.assertIsNotNone(out["five_hour_resets"])     # 倒计时从 legacy 补


class TestDoctorState(Base):
    def test_missing_state_is_a_failure(self):
        """state.json 整个不见时,以前 doctor 一条 state 结果都不产生。"""
        from ccm.config import load_state
        from ccm.doctor import run_checks
        execute_migration(self.env, build_plan(self.env), backup=False)
        os.unlink(self.env.ccm_home / "state.json")
        reg = Registry.load(self.env)
        res = run_checks(self.env, reg, load_state(self.env))
        state_fails = [r for r in res if r.check == "state" and r.level == "fail"]
        self.assertEqual(len(state_fails), 1)
        run_checks(self.env, reg, load_state(self.env), fix=True)
        self.assertEqual(load_state(self.env)["active"], reg.default_profile)


class TestDurability(Base):
    def test_fsync_is_on_by_default(self):
        """CCM_FSYNC 是给测试提速的开关,默认必须是开的。"""
        import ccm.config as C
        old = os.environ.pop("CCM_FSYNC", None)
        calls = []
        real = C.os.fsync
        C.os.fsync = lambda fd: calls.append(fd)
        try:
            C.atomic_write_json(self.tmp / "d" / "x.json", {"a": 1})
        finally:
            C.os.fsync = real
            if old is not None:
                os.environ["CCM_FSYNC"] = old
        self.assertGreaterEqual(len(calls), 2, "文件与父目录都要 fsync")


class TestCleanupActiveGate(Base):
    """cleanup 的活跃进程门禁:实际清理时踩出来的 —— 有进程还挂在兼容链接上时
    删链接,它后续按路径 open 直接 ENOENT。§10 的「长跑进程结束后」必须由代码
    把关,不能只靠用户自觉。"""

    def setUp(self):
        super().setUp()
        execute_migration(self.env, build_plan(self.env), backup=False)
        self.registry = Registry.load(self.env)

    def test_skips_compat_link_with_active_process(self):
        from ccm.migrate import cleanup
        scan = {os.path.realpath(self.tmp / ".claude-b"): {4242}}
        actions = cleanup(self.env, scan=scan)
        self.assertTrue((self.tmp / ".claude-b").is_symlink(), "被占用的不能删")
        self.assertFalse(os.path.lexists(self.tmp / ".claude-a"), "没被占用的照删")
        self.assertTrue(any("跳过" in a and "4242" in a for a in actions))
        # 注册表:被跳过的 compat_link 必须保留,下次还能清
        reg = Registry.load(self.env)
        self.assertIsNotNone(reg.get("a3").compat_link)
        self.assertIsNone(reg.get("a2").compat_link)

    def test_rerun_after_process_exits_finishes_the_job(self):
        from ccm.migrate import cleanup
        cleanup(self.env, scan={os.path.realpath(self.tmp / ".claude-b"): {4242}})
        actions = cleanup(self.env, scan={})    # 进程退出后重跑
        self.assertFalse(os.path.lexists(self.tmp / ".claude-b"))
        self.assertTrue(any("已删除" in a and ".claude-b" in a for a in actions))
        self.assertIsNone(Registry.load(self.env).get("a3").compat_link)

    def test_unknown_procs_warn_but_do_not_block(self):
        """UNKNOWN 桶只提示不拦截:删链接可用 doctor --fix 恢复,而永久拦截会让
        cleanup 在有任何不可读进程的机器上永远跑不完(与 refresh 的保守策略相反,
        那边猜错是不可逆的掉线)。"""
        from ccm.migrate import cleanup
        from ccm.procs import UNKNOWN
        actions = cleanup(self.env, scan={UNKNOWN: {7777}})
        self.assertFalse(os.path.lexists(self.tmp / ".claude-b"))
        self.assertTrue(any("7777" in a for a in actions))


class TestStatuslineIsSessionScoped(Base):
    """statusline 报的必须是**本会话**的账号,不是全局 state。"""

    def _run(self, *argv, config_dir=None):
        import io as _io
        from contextlib import redirect_stdout
        from ccm.cli import main
        saved = {k: os.environ.get(k)
                 for k in ("CCM_USER_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["CCM_USER_HOME"] = str(self.tmp)
        if config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        out = _io.StringIO()
        try:
            with redirect_stdout(out):
                main(list(argv))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return out.getvalue().strip()

    def setUp(self):
        super().setUp()
        execute_migration(self.env, build_plan(self.env), backup=False)
        from ccm.config import save_state
        save_state(self.env, "a3", "test")     # 全局默认切到 a3

    def test_env_wins_over_state(self):
        """在别的终端切过号之后,本会话的 statusline 不该跟着变。"""
        line = self._run("statusline",
                         config_dir=self.env.accounts_root / "a1")
        self.assertTrue(line.startswith("a1 "), line)

    def test_compat_link_resolves_to_same_profile(self):
        """老进程用的是 ~/.claude-a 这种兼容链接,也要认得出来。"""
        line = self._run("statusline", config_dir=self.tmp / ".claude-a")
        self.assertTrue(line.startswith("a2 "), line)

    def test_falls_back_to_state_without_env(self):
        self.assertTrue(self._run("statusline").startswith("a3 "))

    def test_unregistered_dir_falls_back_to_state(self):
        stray = self.tmp / "somewhere-else"
        stray.mkdir()
        self.assertTrue(self._run("statusline", config_dir=stray)
                        .startswith("a3 "))

    def test_profile_for_path_helper(self):
        from ccm.selector import profile_for_path
        reg = Registry.load(self.env)
        self.assertEqual(
            profile_for_path(reg, self.env.accounts_root / "a1").name, "a1")
        self.assertEqual(profile_for_path(reg, self.tmp / ".claude-b").name, "a3")
        self.assertIsNone(profile_for_path(reg, None))
        self.assertIsNone(profile_for_path(reg, self.tmp / "nope"))


class TestRcBlockHelp(unittest.TestCase):
    def test_help_is_not_evaluated_as_shell_code(self):
        """`ccm switch --help` 会带上 --emit-env,argparse 以 0 返回帮助文本。

        以前那段文本被 eval,逐行当命令执行(usage:: command not found)。
        """
        import shutil
        import subprocess
        if not shutil.which("bash"):
            self.skipTest("无 bash")
        from ccm.shellinit import rc_block
        tmp = Path(tempfile.mkdtemp(prefix="ccm-help-"))
        (tmp / "rc.sh").write_text(rc_block())
        repo = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            ["bash", "-c", f'export PATH="{repo / "bin"}:$PATH"; '
                           f'source {tmp / "rc.sh"}; ccm switch --help'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: ccm switch", r.stdout)
        self.assertNotIn("command not found", r.stderr)


class TestRcBlockRecursion(unittest.TestCase):
    def test_second_source_does_not_recurse(self):
        """二次 source ~/.bashrc 时 `command -v ccm` 会命中同名函数。

        以前 _CCM_BIN 因此变成 "ccm",函数调用自己 → 无限递归 → bash 段错误。
        """
        import shutil
        import subprocess
        if not shutil.which("bash"):
            self.skipTest("无 bash")
        from ccm.shellinit import rc_block
        tmp = Path(tempfile.mkdtemp(prefix="ccm-rc-"))
        (tmp / "rc.sh").write_text(rc_block())
        script = (f"export HOME={tmp / 'nonexistent'}\n"
                  "export PATH=/usr/bin:/bin\n"
                  f"source {tmp / 'rc.sh'}\n"
                  f"source {tmp / 'rc.sh'}\n"
                  "ccm ls\n"
                  "echo rc=$?\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=30)
        self.assertNotEqual(r.returncode, -11, "bash 段错误 = 无限递归回来了")
        self.assertIn("rc=127", r.stdout)

    def test_bin_resolution_ignores_functions(self):
        from ccm.shellinit import rc_block
        blk = rc_block()
        self.assertIn("type -P ccm", blk)          # 只查 PATH,不会命中函数
        self.assertNotIn('${_CCM_BIN:-ccm}', blk)  # 裸 "ccm" 兜底 = 递归入口


if __name__ == "__main__":
    unittest.main()
