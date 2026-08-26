import json
import time
import urllib.error
import urllib.request

from ccm.errors import ApiError

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://claude.ai/v1/oauth/token"  # 2026-08-26 ws02 实测 200;console/platform 域名会 429
# Claude Code CLI 的公开 OAuth client_id(PKCE 公共客户端,非机密)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
BETA_HEADER = "oauth-2025-04-20"
TIMEOUT_S = 8
_MARGIN_MS = 60_000  # 60s 提前量:临期 token 视为过期,避免请求半路失效


def token_state(creds, now_ms=None):
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expires = int(creds.get("expiresAt") or 0)
    refresh_expires = int(creds.get("refreshTokenExpiresAt") or 0)
    return {
        "expired": now >= expires - _MARGIN_MS,
        "expires_in_s": max(0, (expires - now) // 1000),
        "refresh_expires_in_s": max(0, (refresh_expires - now) // 1000),
    }


def _get(url, access_token, opener=None):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": BETA_HEADER,
    })
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 注意:异常信息不得包含 token
        raise ApiError(f"HTTP {e.code} @ {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"网络错误 @ {url}: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
        raise ApiError(f"响应不是 JSON @ {url}")


def fetch_usage(access_token, opener=None):
    return _get(USAGE_URL, access_token, opener)


def fetch_profile(access_token, opener=None):
    return _get(PROFILE_URL, access_token, opener)


def refresh_access_token(refresh_token, opener=None):
    """POST token endpoint 换新 token。调用方负责 CAS 与落盘;此处只打网络。"""
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": refresh_token,
                       "client_id": CLIENT_ID}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} @ {TOKEN_URL}(refresh token 可能已作废)")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"网络错误 @ {TOKEN_URL}: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
        raise ApiError(f"响应不是 JSON @ {TOKEN_URL}")
