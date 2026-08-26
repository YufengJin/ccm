import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.backup import create_backup, restore_backup
from ccm.config import Registry
from ccm.errors import CcmError
from ccm.migrate import build_plan, execute_migration


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-bak-"))
        self.env = make_fake_home(self.tmp)
        execute_migration(self.env, build_plan(self.env))
        self.registry = Registry.load(self.env)

    def test_backup_excludes_credentials_by_default(self):
        p = create_backup(self.env, self.registry, "a3")
        with tarfile.open(p) as t:
            names = t.getnames()
        self.assertFalse(any(".credentials.json" in n for n in names))
        self.assertTrue(any("history.jsonl" in n for n in names))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_backup_with_credentials(self):
        p = create_backup(self.env, self.registry, "a3", with_credentials=True)
        with tarfile.open(p) as t:
            names = t.getnames()
        self.assertTrue(any(".credentials.json" in n for n in names))

    def test_restore_roundtrip_into_new_name(self):
        p = create_backup(self.env, self.registry, "a3", with_credentials=True)
        prof = restore_backup(self.env, Registry.load(self.env), p, into="work2")
        self.assertEqual(prof.path, self.env.accounts_root / "work2")
        self.assertTrue((prof.path / "history.jsonl").exists())
        self.assertIn("work2", Registry.load(self.env).profiles)
        # 内部指向 shared 的 symlink 原样保留
        self.assertTrue((prof.path / "settings.json").is_symlink())

    def test_restore_refuses_existing(self):
        p = create_backup(self.env, self.registry, "a3")
        with self.assertRaises(CcmError):
            restore_backup(self.env, Registry.load(self.env), p, into="a3")

    def test_restore_rejects_traversal(self):
        evil = self.tmp / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as t:
            data = b"pwned"
            info = tarfile.TarInfo("x/../../../evil.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        with self.assertRaises(CcmError):
            restore_backup(self.env, Registry.load(self.env), evil, into="x2")
        self.assertFalse((self.tmp / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
