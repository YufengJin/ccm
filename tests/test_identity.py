import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.identity import read_credentials, resolve_identity
from ccm.errors import CredentialsMissing


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-idn-"))
        self.env = make_fake_home(self.tmp)

    def test_read_credentials(self):
        c = read_credentials(self.tmp / ".claude-b")
        self.assertTrue(c["accessToken"].startswith("sk-ant-oat"))
        self.assertIn("expiresAt", c)

    def test_missing_returns_none_corrupt_raises(self):
        self.assertIsNone(read_credentials(self.tmp / "nonexistent"))
        bad = self.tmp / ".claude-x"
        bad.mkdir()
        (bad / ".credentials.json").write_text("{}")
        with self.assertRaises(CredentialsMissing):
            read_credentials(bad)

    def test_level1_profile_json(self):
        r = resolve_identity(self.tmp / ".claude-b", self.tmp)
        self.assertEqual((r["account_uuid"], r["source"]), ("acct-B", "profile-json"))
        self.assertEqual(r["email"], "work@example.com")

    def test_level2_legacy_only_when_allowed(self):
        # .claude 的 .claude.json 无 oauthAccount → 不允许 legacy 且无 fetch 时 None
        self.assertIsNone(resolve_identity(self.tmp / ".claude", self.tmp))
        r = resolve_identity(self.tmp / ".claude", self.tmp, allow_legacy=True)
        self.assertEqual((r["account_uuid"], r["source"]), ("acct-A", "legacy-json"))

    def test_level3_fetch(self):
        def fake(tok):
            assert tok == "sk-ant-oat01-FAKE"
            return {"account": {"uuid": "acct-N", "email": "n@x"},
                    "organization": {"organization_type": "claude_max",
                                     "rate_limit_tier": "t"}}
        bare = self.tmp / ".claude-bare"
        bare.mkdir()
        (bare / ".credentials.json").write_text(
            (self.tmp / ".claude-b" / ".credentials.json").read_text())
        r = resolve_identity(bare, self.tmp, fetch=fake)
        self.assertEqual((r["account_uuid"], r["source"]), ("acct-N", "api"))
        self.assertEqual(r["subscription"], "claude_max")


if __name__ == "__main__":
    unittest.main()
