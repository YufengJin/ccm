"""凭证刷新:spec §9 阶梯 + P1 附加约束(account 级锁、CAS、绝不回写旧凭证)。"""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from ccm.config import atomic_write_json, load_json
from ccm.errors import ApiError, CredentialsMissing, LockBusy
from ccm.identity import read_credentials
from ccm.oauth import refresh_access_token, token_state
from ccm.procs import daemon_lock_pid, profile_active_pids


def _fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _log(env, msg):
    logs = env.ccm_home / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(logs / "refresh.log", "a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def refresh_profile(env, prof, scan=None, opener=None, force=False, now_ms=None):
    """返回 {status, detail}。status:
    skipped-valid | skipped-active | refreshed | abandoned-cas | failed
    """
    creds = read_credentials(prof.path)
    if not creds:
        raise CredentialsMissing(f"{prof.name} 未登录")
    if not token_state(creds, now_ms)["expired"] and not force:
        return {"status": "skipped-valid", "detail": "token 未过期"}
    # 活跃进程门禁(TOCTOU 存在,CAS 才是最后防线;未知状态按活跃处理)
    if not force:
        pids = profile_active_pids(prof.path, prof.compat_link, scan or {})
        dpid = daemon_lock_pid(prof.path)
        if dpid:
            pids = pids | {dpid}
        if pids:
            return {"status": "skipped-active",
                    "detail": f"有活跃 claude 进程 {sorted(pids)},拒绝并发刷新"
                              "(显示缓存数据;--force 可越过)"}
    # account 级锁:按 refresh token 指纹,同链凭证串行(codex 审核采纳)
    fp = _fingerprint(creds["refreshToken"])
    locks = env.ccm_home / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / f"refresh-{fp}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(f"另一个 ccm 正在刷新同一凭证链({fp})")
        # CAS#1:持锁后重读(别人可能刚刷完)
        creds = read_credentials(prof.path)
        st = token_state(creds, now_ms)
        if not st["expired"] and not force:
            return {"status": "skipped-valid", "detail": "持锁重读后发现已被刷新"}
        used_refresh = creds["refreshToken"]
        try:
            resp = refresh_access_token(used_refresh, opener=opener)
        except ApiError as e:
            # 绝不回写旧凭证:请求已发出,旧 refresh token 视为可能已作废
            _log(env, f"failed {prof.name} fp={fp}: {e}")
            return {"status": "failed",
                    "detail": f"{e};未做任何写入,若持续失败请 ccm login {prof.name}"}
        # CAS#2:写回前再读,refreshToken 变了说明 Claude Code 抢先 → 放弃
        latest = read_credentials(prof.path)
        if latest and latest.get("refreshToken") != used_refresh:
            _log(env, f"abandoned-cas {prof.name} fp={fp}")
            return {"status": "abandoned-cas",
                    "detail": "凭证在请求期间被他方轮换,放弃写入并采用对方结果"}
        new_creds = dict(creds)
        new_creds["accessToken"] = resp["access_token"]
        new_creds["refreshToken"] = resp.get("refresh_token", used_refresh)
        new_creds["expiresAt"] = int(time.time() * 1000) + \
            int(resp.get("expires_in", 8 * 3600)) * 1000
        blob = load_json(Path(prof.path) / ".credentials.json", default={}) or {}
        blob["claudeAiOauth"] = new_creds
        atomic_write_json(Path(prof.path) / ".credentials.json", blob, mode=0o600)
        _log(env, f"refreshed {prof.name} fp={fp} -> "
                  f"{_fingerprint(new_creds['refreshToken'])}")
        return {"status": "refreshed",
                "detail": f"新 token 有效 {int(resp.get('expires_in', 0)) // 3600}h"}
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
