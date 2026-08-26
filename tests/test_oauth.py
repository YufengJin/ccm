import json
import unittest
import urllib.error

from ccm.oauth import token_state, fetch_usage, fetch_profile, USAGE_URL, PROFILE_URL
from ccm.errors import ApiError


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(payload, seen):
    def op(req, timeout=None):
        seen.append((req, timeout))
        return _FakeResp(payload)
    return op


class TestOauth(unittest.TestCase):
    def test_token_state_margin(self):
        now = 1_000_000_000_000
        creds = {"expiresAt": now + 61_000, "refreshTokenExpiresAt": now + 86_400_000}
        st = token_state(creds, now)
        self.assertFalse(st["expired"])
        self.assertEqual(st["expires_in_s"], 61)
        self.assertEqual(st["refresh_expires_in_s"], 86_400)
        creds["expiresAt"] = now + 59_000
        self.assertTrue(token_state(creds, now)["expired"])

    def test_fetch_usage_headers_and_timeout(self):
        seen = []
        out = fetch_usage("tok123", opener=fake_opener({"five_hour": {}}, seen))
        self.assertEqual(out, {"five_hour": {}})
        req, timeout = seen[0]
        self.assertEqual(req.full_url, USAGE_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer tok123")
        self.assertEqual(req.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertLessEqual(timeout, 10)

    def test_fetch_profile_url(self):
        seen = []
        fetch_profile("t", opener=fake_opener({"account": {}}, seen))
        self.assertEqual(seen[0][0].full_url, PROFILE_URL)

    def test_http_error_maps_to_apierror_without_token_leak(self):
        def op(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {}, None)
        with self.assertRaises(ApiError) as cm:
            fetch_usage("sk-ant-oat01-SECRETSECRET", opener=op)
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("SECRETSECRET", str(cm.exception))

    def test_network_error_maps_to_apierror(self):
        def op(req, timeout=None):
            raise urllib.error.URLError("conn refused")
        with self.assertRaises(ApiError):
            fetch_usage("t", opener=op)


if __name__ == "__main__":
    unittest.main()
