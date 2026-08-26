import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.config import Registry, load_state, save_state
from ccm.errors import CcmError, LockBusy
from ccm.lifecycle import (add_profile, remove_profile, rename_profile,
                           logout_profile, login_profile, show_profile)
from ccm.migrate import build_plan, execute_migration

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-lc-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))
        self.registry = Registry.load(self.env)


class TestAdd(Base):
    def test_add_new_profile(self):
        p = add_profile(self.env, self.registry, "team", note="第4个号")
        self.assertTrue(p.path.is_dir())
        # 共享 symlink 已铺
        self.assertTrue((p.path / "settings.json").is_symlink())
        r2 = Registry.load(self.env)
        self.assertIn("team", r2.profiles)
        self.assertEqual(r2.get("team").note, "第4个号")

    def test_add_duplicate_rejected(self):
        with self.assertRaises(CcmError):
            add_profile(self.env, self.registry, "a3")

    def test_add_import_in_place(self):
        ext = self.tmp / "external-claude"
        ext.mkdir()
        (ext / ".credentials.json").write_text("{}")
        p = add_profile(self.env, self.registry, "ext", import_dir=ext)
        self.assertEqual(p.path, ext)          # 原地纳管,不移动
        self.assertTrue((ext / ".credentials.json").exists())

    def test_add_import_move(self):
        ext = self.tmp / "external-claude"
        ext.mkdir()
        (ext / "marker.txt").write_text("m")
        p = add_profile(self.env, self.registry, "ext", import_dir=ext, move=True)
        self.assertEqual(p.path, self.env.accounts_root / "ext")
        self.assertTrue(ext.is_symlink())      # 原位留兼容 symlink
        self.assertEqual((ext / "marker.txt").read_text(), "m")


class TestRemove(Base):
    def test_rm_default_refused(self):
        with self.assertRaises(CcmError):
            remove_profile(self.env, self.registry, "a1", scan={})

    def test_rm_active_refused(self):
        scan = {os.path.realpath(self.env.accounts_root / "a3"): {1}}
        with self.assertRaises(CcmError):
            remove_profile(self.env, self.registry, "a3", scan=scan)

    def test_rm_backs_up_with_credentials_then_deletes(self):
        bak = remove_profile(self.env, self.registry, "a3", scan={})
        self.assertTrue(bak and bak.exists())
        self.assertEqual(os.stat(bak).st_mode & 0o777, 0o600)
        import tarfile
        with tarfile.open(bak) as t:
            names = t.getnames()
        self.assertTrue(any(".credentials.json" in n for n in names))
        self.assertFalse((self.env.accounts_root / "a3").exists())
        self.assertFalse(os.path.lexists(self.tmp / ".claude-b"))  # 兼容链接一并清
        self.assertNotIn("a3", Registry.load(self.env).profiles)

    def test_rm_keep_data(self):
        remove_profile(self.env, self.registry, "a3", scan={}, keep_data=True)
        self.assertTrue((self.env.accounts_root / "a3").is_dir())
        self.assertNotIn("a3", Registry.load(self.env).profiles)


class TestRenameLogoutShow(Base):
    def test_rename_updates_dir_link_state(self):
        save_state(self.env, "a3", "test")
        rename_profile(self.env, self.registry, "a3", "corp")
        r2 = Registry.load(self.env)
        self.assertIn("corp", r2.profiles)
        self.assertEqual(r2.get("corp").path, self.env.accounts_root / "corp")
        self.assertEqual(os.readlink(self.tmp / ".claude-b"),
                         str(self.env.accounts_root / "corp"))
        self.assertEqual(load_state(self.env)["active"], "corp")

    def test_rename_active_refused(self):
        scan = {os.path.realpath(self.env.accounts_root / "a3"): {9}}
        with self.assertRaises(CcmError):
            rename_profile(self.env, self.registry, "a3", "corp", scan=scan)

    def test_logout_no_copy_by_default(self):
        logout_profile(self.env, self.registry, "a3", scan={})
        self.assertFalse((self.env.accounts_root / "a3" / ".credentials.json").exists())
        self.assertEqual(list((self.env.ccm_home / "backups").glob("logout-*")), [])

    def test_logout_keep_backup(self):
        logout_profile(self.env, self.registry, "a3", scan={}, keep_backup=True)
        baks = list((self.env.ccm_home / "backups").glob("logout-a3-*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(os.stat(baks[0]).st_mode & 0o777, 0o600)

    def test_show(self):
        info = show_profile(self.env, self.registry, "a3")
        self.assertEqual(info["email"], "work@example.com")
        self.assertIn("token", info)
        self.assertIn("links_ok", info)


class TestLogin(Base):
    def test_login_backfills_identity(self):
        add_profile(self.env, self.registry, "team")
        rc = login_profile(self.env, Registry.load(self.env), "team",
                           claude_bin=str(FIXTURES / "fake-claude-login"))
        self.assertEqual(rc, 0)
        r2 = Registry.load(self.env)
        self.assertEqual(r2.get("team").account_uuid, "acct-LOGIN")
        self.assertEqual(r2.get("team").email, "login@x")

    def test_login_claude_missing(self):
        with self.assertRaises(CcmError):
            login_profile(self.env, self.registry, "a3",
                          claude_bin=str(self.tmp / "no-such-claude"))


if __name__ == "__main__":
    unittest.main()
