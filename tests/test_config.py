import json
import os
import tempfile
import unittest
from pathlib import Path

from ccm.config import (Env, expand, atomic_write_json, load_json, Registry,
                        load_state, save_state, validate_profile_name,
                        validate_shared_item, registry_lock)
from ccm.profiles import Profile
from ccm.errors import CcmError, ProfileNotFound


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-test-"))
        self.env = Env.from_environ({"CCM_USER_HOME": str(self.tmp)})

    def test_env_defaults_derive_from_user_home(self):
        self.assertEqual(self.env.ccm_home, self.tmp / ".ccm")
        self.assertEqual(self.env.accounts_root, self.tmp / ".claude-accounts")
        self.assertEqual(self.env.shared_root, self.tmp / ".claude-shared")
        self.assertEqual(self.env.proc_root, Path("/proc"))

    def test_env_explicit_overrides(self):
        e = Env.from_environ({"CCM_USER_HOME": str(self.tmp),
                              "CCM_HOME": str(self.tmp / "x"),
                              "CCM_PROC_ROOT": str(self.tmp / "proc")})
        self.assertEqual(e.ccm_home, self.tmp / "x")
        self.assertEqual(e.proc_root, self.tmp / "proc")

    def test_expand_never_touches_real_home(self):
        self.assertEqual(expand("~/.claude-b", self.tmp), self.tmp / ".claude-b")
        self.assertEqual(expand("/abs/x", self.tmp), Path("/abs/x"))

    def test_atomic_write_leaves_no_tmp(self):
        p = self.env.ccm_home / "state.json"
        atomic_write_json(p, {"a": 1})
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        self.assertEqual([f for f in p.parent.iterdir() if ".tmp" in f.name], [])

    def test_sensitive_mode_at_create(self):
        p = self.env.ccm_home / "cred-copy.json"
        atomic_write_json(p, {"k": 1}, mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.env.ccm_home).st_mode & 0o777, 0o700)

    def test_load_json_corrupt_raises_with_path(self):
        p = self.tmp / "bad.json"
        p.write_text("{oops")
        with self.assertRaises(CcmError) as cm:
            load_json(p)
        self.assertIn("bad.json", str(cm.exception))
        self.assertEqual(load_json(self.tmp / "nope.json", default=42), 42)

    def test_name_validation(self):
        validate_profile_name("work-2")
        for bad in ("../x", "a b", "", "-x", "x" * 33):
            with self.assertRaises(CcmError):
                validate_profile_name(bad)
        validate_shared_item("settings.json")
        for bad in ("a/b", "..", "."):
            with self.assertRaises(CcmError):
                validate_shared_item(bad)

    def test_registry_roundtrip_and_tilde(self):
        r = Registry.empty(self.env)
        self.assertIn("settings.json", r.shared)
        r.profiles["work"] = Profile(name="work", path=self.tmp / ".claude-accounts/work",
                                     compat_link=self.tmp / ".claude-b", account_uuid="u1",
                                     email="e@x", subscription="max", rate_limit_tier=None,
                                     identity_fetched_at=None, note="")
        r.save(self.env)
        raw = json.loads((self.env.ccm_home / "profiles.json").read_text())
        self.assertEqual(raw["profiles"]["work"]["path"], "~/.claude-accounts/work")
        r2 = Registry.load(self.env)
        self.assertEqual(r2.get("work").path, self.tmp / ".claude-accounts/work")
        self.assertEqual(r2.get("work").compat_link, self.tmp / ".claude-b")
        with self.assertRaises(ProfileNotFound):
            r2.get("nope")

    def test_registry_load_rejects_bad_names(self):
        r = Registry.empty(self.env)
        r.save(self.env)
        raw = json.loads((self.env.ccm_home / "profiles.json").read_text())
        raw["profiles"]["../evil"] = {"path": "~/.x"}
        (self.env.ccm_home / "profiles.json").write_text(json.dumps(raw))
        with self.assertRaises(CcmError):
            Registry.load(self.env)

    def test_registry_lock_reentrant_across_calls(self):
        with registry_lock(self.env):
            pass  # 简单冒烟:能取到锁并释放
        with registry_lock(self.env):
            pass

    def test_state_roundtrip(self):
        self.assertIsNone(load_state(self.env))
        save_state(self.env, "work", "test")
        st = load_state(self.env)
        self.assertEqual(st["active"], "work")
        self.assertEqual(st["changed_by"], "test")


if __name__ == "__main__":
    unittest.main()
