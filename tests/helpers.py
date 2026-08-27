"""fake home 构造器:复刻 ws02 迁移前布局,供全部测试使用。"""
import json
import os
import time
from pathlib import Path

from ccm.config import Env

# 测试跑在 tmpfs/临时目录上,不需要断电级持久化;开着 fsync 会让每个迁移类测试
# 多花近 1 秒。生产默认仍是开启的(见 test_regressions 里的断言)。
os.environ.setdefault("CCM_FSYNC", "0")

ACCT_A = "acct-A"  # jyf* 账号(default 与 personal 共享)
ACCT_B = "acct-B"  # 公司账号(work)

SHARED_ITEMS = ["settings.json", "settings.local.json", "CLAUDE.md", "plugins",
                "skills", "projects", "statusline-command.sh"]


def _creds(expired):
    now = int(time.time() * 1000)
    dt = -3_600_000 if expired else 4 * 3_600_000
    return {"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FAKE", "refreshToken": "sk-ant-ort01-FAKE",
        "expiresAt": now + dt, "refreshTokenExpiresAt": now + 30 * 86_400_000,
        "scopes": ["user:inference"], "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x"}}


def _oauth_account(uuid, email):
    return {"accountUuid": uuid, "emailAddress": email,
            "organizationType": "claude_max",
            "organizationRateLimitTier": "default_claude_max_20x"}


def _cached_usage(uuid, age_s=3 * 3600):
    return {"fetchedAtMs": int((time.time() - age_s) * 1000),
            "accountUuid": uuid,
            "utilization": {
                "five_hour": {"utilization": 12, "resets_at": "2026-08-26T15:50:00+00:00"},
                "seven_day": {"utilization": 34, "resets_at": "2026-08-28T07:00:00+00:00"},
                "limits": [
                    {"kind": "session", "percent": 12, "severity": "normal",
                     "resets_at": "2026-08-26T15:50:00+00:00", "is_active": False},
                    {"kind": "weekly_all", "percent": 34, "severity": "normal",
                     "resets_at": "2026-08-28T07:00:00+00:00", "is_active": False}]}}


def make_fake_home(tmp, a_expired=False, b_expired=False, default_expired=False):
    tmp = Path(tmp)
    # --- 共享库现状: ~/.claude 是实体 ---
    d = tmp / ".claude"
    d.mkdir(parents=True)
    (d / "settings.json").write_text('{"model": "opus"}')
    (d / "settings.local.json").write_text("{}")
    (d / "CLAUDE.md").write_text("# 全局指令\n")
    (d / "plugins").mkdir()
    (d / "plugins" / "marker.txt").write_text("plugin-data")
    (d / "skills").mkdir()
    (d / "projects").mkdir()
    proj = d / "projects" / "-home-user-Desktop"
    proj.mkdir()
    (proj / "sess-1111.jsonl").write_text(json.dumps({
        "sessionId": "sess-1111", "requestId": "r1", "uuid": "u1",
        "timestamp": "2026-08-26T10:00:00Z",
        "message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 5,
                               "ephemeral_1h_input_tokens": 7}}}}) + "\n")
    (d / "statusline-command.sh").write_text("#!/bin/sh\necho hi\n")
    # default 的私有实体
    (d / ".credentials.json").write_text(json.dumps(_creds(default_expired)))
    (d / ".claude.json").write_text(json.dumps({"firstStartTime": "2026-08-15"}))  # 133B 无 oauthAccount
    (d / "history.jsonl").write_text("{}\n")
    for sub in ("sessions", "session-env", "shell-snapshots", "cache", "file-history"):
        (d / sub).mkdir()
    (d / "session-env" / "sess-1111").mkdir()

    # --- 遗留 ~/.claude.json(default 的真实身份所在) ---
    (tmp / ".claude.json").write_text(json.dumps({
        "oauthAccount": _oauth_account(ACCT_A, "jyf@example.com"),
        "numStartups": 500}))

    # --- a / b:私有实体 + 指向 ~/.claude 的 symlink ---
    for base, uuid, email, expired, cached in (
            (".claude-a", ACCT_A, "jyf@example.com", a_expired, False),
            (".claude-b", ACCT_B, "work@example.com", b_expired, True)):
        p = tmp / base
        p.mkdir()
        (p / ".credentials.json").write_text(json.dumps(_creds(expired)))
        blob = {"oauthAccount": _oauth_account(uuid, email)}
        if cached:
            blob["cachedUsageUtilization"] = _cached_usage(uuid)
        (p / ".claude.json").write_text(json.dumps(blob))
        (p / "history.jsonl").write_text("{}\n")
        for sub in ("sessions", "session-env", "shell-snapshots", "cache"):
            (p / sub).mkdir()
        for item in SHARED_ITEMS:
            os.symlink(tmp / ".claude" / item, p / item)
    (tmp / ".claude-b" / "session-env" / "sess-2222").mkdir()

    return Env.from_environ({"CCM_USER_HOME": str(tmp)})
