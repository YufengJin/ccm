import filecmp
import json
import os
import tempfile
import unittest
from pathlib import Path

import ccm.migrate as M
from ccm.migrate import Journal, move_dir_with_compat, split_item, rollback_ops
from ccm.config import Env
from ccm.errors import MigrationAborted


def deep_equal(a, b):
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(deep_equal(Path(a) / d, Path(b) / d) for d in cmp.common_dirs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-mig-"))
        self.env = Env.from_environ({"CCM_USER_HOME": str(self.tmp)})
        self.journal = Journal(self.env)
        d = self.tmp / ".claude-b"
        d.mkdir()
        (d / ".credentials.json").write_text('{"k": 1}')
        (d / "sub").mkdir()
        (d / "sub" / "f.txt").write_text("data")


class TestMoveDir(Base):
    def test_move_preserves_inode_and_old_path(self):
        old = self.tmp / ".claude-b"
        new = self.tmp / "accounts" / "work"
        ino = os.stat(old / ".credentials.json").st_ino
        move_dir_with_compat(old, new, self.journal)
        self.assertTrue(old.is_symlink())
        self.assertEqual(os.readlink(old), str(new))
        self.assertEqual(os.stat(new / ".credentials.json").st_ino, ino)
        # 经旧路径照常读
        self.assertEqual((old / "sub" / "f.txt").read_text(), "data")
        # journal: intent 已标 done
        self.assertEqual(self.journal.ops[-1]["status"], "done")

    def test_open_fd_survives_move(self):
        old = self.tmp / ".claude-b"
        new = self.tmp / "accounts" / "work"
        f = open(old / "sub" / "f.txt", "a")
        move_dir_with_compat(old, new, self.journal)
        f.write("+more")
        f.close()
        self.assertEqual((new / "sub" / "f.txt").read_text(), "data+more")

    def test_idempotent_reentry(self):
        old = self.tmp / ".claude-b"
        new = self.tmp / "accounts" / "work"
        move_dir_with_compat(old, new, self.journal)
        n = len(self.journal.ops)
        move_dir_with_compat(old, new, self.journal)  # 已迁 → no-op
        self.assertEqual(len(self.journal.ops), n)

    def test_second_rename_failure_restores(self):
        old = self.tmp / ".claude-b"
        new = self.tmp / "accounts" / "work"
        # 注入点是 rename_noreplace(不再是 os.rename):_staged_swap 改用它
        # 以拒绝竞态创建的目标
        real_rename = M.rename_noreplace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("注入失败")
            real_rename(src, dst)

        M.rename_noreplace = flaky
        try:
            with self.assertRaises(OSError):
                move_dir_with_compat(old, new, self.journal)
        finally:
            M.rename_noreplace = real_rename
        self.assertTrue(old.is_dir())
        self.assertFalse(old.is_symlink())
        self.assertFalse(list(self.tmp.glob("*.ccm-staging")))
        self.assertEqual((old / "sub" / "f.txt").read_text(), "data")


class TestSplitAndRollback(Base):
    def test_split_item_inode(self):
        src_dir = self.tmp / ".claude-b"
        shared = self.tmp / "shared"
        ino = os.stat(src_dir / "sub" / "f.txt").st_ino
        split_item(src_dir, "sub", shared, self.journal)
        self.assertTrue((src_dir / "sub").is_symlink())
        self.assertEqual(os.stat(shared / "sub" / "f.txt").st_ino, ino)
        # 已是 symlink → 幂等跳过
        n = len(self.journal.ops)
        split_item(src_dir, "sub", shared, self.journal)
        self.assertEqual(len(self.journal.ops), n)

    def test_rollback_full_restore(self):
        import shutil
        src = self.tmp / ".claude-b"
        ref = self.tmp / "ref"
        shutil.copytree(src, ref, symlinks=True)
        move_dir_with_compat(src, self.tmp / "accounts" / "work", self.journal)
        split_item(self.tmp / "accounts" / "work", "sub",
                   self.tmp / "shared", self.journal)
        rollback_ops(self.env, self.journal)
        self.assertFalse(src.is_symlink())
        self.assertTrue(deep_equal(src, ref))
        self.assertFalse((self.env.ccm_home / "logs" / "migrate-journal.json").exists())

    def test_rollback_tampered_aborts(self):
        src = self.tmp / ".claude-b"
        move_dir_with_compat(src, self.tmp / "accounts" / "work", self.journal)
        os.unlink(src)
        src.mkdir()  # 被换成实体目录
        with self.assertRaises(MigrationAborted):
            rollback_ops(self.env, self.journal)

    def test_crash_between_renames_resumable(self):
        # 现场:intent 无 done + old 缺失 + staging 残留 + new 已就位
        old = self.tmp / ".claude-b"
        new = self.tmp / "accounts" / "work"
        new.parent.mkdir(parents=True)
        staging = old.with_name(old.name + ".ccm-staging")
        os.symlink(new, staging)
        idx = self.journal.intent({"op": "move_dir", "old": str(old), "new": str(new)})
        os.rename(old, new)  # 崩溃点:第二次 rename 没执行,idx 未标 done
        j2 = Journal.load(self.env)
        self.assertEqual(j2.ops[idx]["status"], "intent")
        rollback_ops(self.env, j2)
        self.assertTrue(old.is_dir())
        self.assertEqual((old / "sub" / "f.txt").read_text(), "data")
        self.assertFalse(staging.is_symlink())


if __name__ == "__main__":
    unittest.main()
