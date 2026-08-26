from pathlib import Path

from ccm.config import load_json
from ccm.errors import CcmError, CredentialsMissing


def read_credentials(profile_path):
    """返回 .credentials.json 的 claudeAiOauth 对象;缺失→None;损坏/缺 key→CredentialsMissing。"""
    p = Path(profile_path) / ".credentials.json"
    if not p.exists():
        return None
    try:
        data = load_json(p)
    except CcmError as e:
        raise CredentialsMissing(str(e))
    creds = (data or {}).get("claudeAiOauth")
    if not isinstance(creds, dict) or "accessToken" not in creds:
        raise CredentialsMissing(f"凭证格式异常(缺 claudeAiOauth.accessToken): {p}")
    return creds


def _from_oauth_account(oa, source):
    return {
        "account_uuid": oa.get("accountUuid"),
        "email": oa.get("emailAddress"),
        "subscription": oa.get("organizationType"),
        "rate_limit_tier": oa.get("organizationRateLimitTier"),
        "source": source,
    }


def resolve_identity(profile_path, user_home, allow_legacy=False, fetch=None):
    """三级回退解析 profile 的 account 身份(spec §4)。

    1. <profile>/.claude.json 的 oauthAccount
    2. (仅 allow_legacy) <user_home>/.claude.json 的 oauthAccount —— 旧版遗留路径
    3. fetch(access_token) 现查 /api/oauth/profile
    """
    profile_path = Path(profile_path)
    sources = [(profile_path / ".claude.json", "profile-json")]
    if allow_legacy:
        sources.append((Path(user_home) / ".claude.json", "legacy-json"))
    for path, tag in sources:
        try:
            data = load_json(path)
        except CcmError:
            continue  # 损坏的候选文件不阻断回退链
        oa = (data or {}).get("oauthAccount") or {}
        if oa.get("accountUuid"):
            return _from_oauth_account(oa, tag)
    if fetch is not None:
        creds = read_credentials(profile_path)
        if creds:
            prof = fetch(creds["accessToken"])
            acct = prof.get("account", {})
            org = prof.get("organization", {})
            if acct.get("uuid"):
                return {
                    "account_uuid": acct.get("uuid"),
                    "email": acct.get("email"),
                    "subscription": org.get("organization_type"),
                    "rate_limit_tier": org.get("rate_limit_tier"),
                    "source": "api",
                }
    return None
