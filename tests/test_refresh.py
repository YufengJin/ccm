import json
import unittest
import tempfile
import urllib.error
from pathlib import Path

from tests.helpers import make_fake_home
from ccm.config import Registry
from ccm.profiles import Profile
from ccm.oauth import refresh_access_token, TOKEN_URL
from ccm.refresh import refresh_profile


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def token_opener(seen):
    def op(req, timeout=None):
        seen.append(req)
        return _Resp({"access_token": "sk-ant-oat01-NEW",
                      "refresh_token": "sk-ant-ort01-NEW",
                      "expires_in": 28800})
    return op


class TestRefreshApi(unittest.TestCase):
    def test_refresh_request_shape(self):
        seen = []
        out = refresh_access_token("sk-ant-ort01-OLD", opener=token_opener(seen))
        self.assertEqual(out["access_token"], "sk-ant-oat01-NEW")
        req = seen[0]
        self.assertEqual(req.full_url, TOKEN_URL)
        body = json.loads(req.data.decode())
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["refresh_token"], "sk-ant-ort01-OLD")
        self.assertIn("client_id", body)


class TestRefreshProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-rf-"))
        self.env = make_fake_home(self.tmp, b_expired=True)
        self.prof = Profile(name="work", path=self.tmp / ".claude-b")

    def creds_path(self):
        return self.tmp / ".claude-b" / ".credentials.json"

    def test_valid_token_skips(self):
        p = Profile(name="personal", path=self.tmp / ".claude-a")  # 未过期
        r = refresh_profile(self.env, p, scan={}, opener=None)
        self.assertEqual(r["status"], "skipped-valid")

    def test_active_process_refuses(self):
        import os
        scan = {os.path.realpath(self.prof.path): {123}}
        r = refresh_profile(self.env, self.prof, scan=scan)
        self.assertEqual(r["status"], "skipped-active")
        # 凭证未被动过
        self.assertIn("FAKE", self.creds_path().read_text())

    def test_refresh_writes_new_creds_0600(self):
        import os
        seen = []
        r = refresh_profile(self.env, self.prof, scan={}, opener=token_opener(seen))
        self.assertEqual(r["status"], "refreshed")
        data = json.loads(self.creds_path().read_text())["claudeAiOauth"]
        self.assertEqual(data["accessToken"], "sk-ant-oat01-NEW")
        self.assertEqual(data["refreshToken"], "sk-ant-ort01-NEW")
        self.assertEqual(data["subscriptionType"], "max")   # 旧字段保留
        self.assertGreater(data["expiresAt"], 0)
        self.assertEqual(os.stat(self.creds_path()).st_mode & 0o777, 0o600)

    def test_cas_abandons_if_rotated_midflight(self):
        # 请求期间 Claude Code 抢先刷新 → 放弃写入,保留更新的凭证
        def racing_opener(req, timeout=None):
            blob = json.loads(self.creds_path().read_text())
            blob["claudeAiOauth"]["refreshToken"] = "sk-ant-ort01-RACED"
            self.creds_path().write_text(json.dumps(blob))
            return _Resp({"access_token": "sk-ant-oat01-NEW",
                          "refresh_token": "sk-ant-ort01-NEW", "expires_in": 100})
        r = refresh_profile(self.env, self.prof, scan={}, opener=racing_opener)
        self.assertEqual(r["status"], "abandoned-cas")
        data = json.loads(self.creds_path().read_text())["claudeAiOauth"]
        self.assertEqual(data["refreshToken"], "sk-ant-ort01-RACED")  # 保留对方的

    def test_api_failure_never_writes(self):
        before = self.creds_path().read_text()
        def op(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "invalid_grant", {}, None)
        r = refresh_profile(self.env, self.prof, scan={}, opener=op)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.creds_path().read_text(), before)  # 绝不回写


if __name__ == "__main__":
    unittest.main()
