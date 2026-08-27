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

_MAX_RELOCK = 3   # 锁键(refresh token 指纹)在持锁重读后变了 → 换锁重来的上限


def _fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _log(env, msg):
    logs = env.ccm_home / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(logs / "refresh.log", "a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def _backup_credentials(env, name, blob):
    """§9:刷新写回前先把整份凭证备份出去(0600),失败可人工回滚。"""
    dest_dir = env.ccm_home / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(100):
        dest = dest_dir / (f"creds-{name}-{stamp}.json" if n == 0
                           else f"creds-{name}-{stamp}-{n}.json")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(blob, f, ensure_ascii=False)
        return dest
    return None


def _write_creds(path, creds):
    blob = load_json(Path(path) / ".credentials.json", default={}) or {}
    blob["claudeAiOauth"] = creds
    atomic_write_json(Path(path) / ".credentials.json", blob, mode=0o600)


def _sync_siblings(env, siblings, used_refresh, new_creds):
    """把轮换后的凭证同步给同源(持有同一 refresh token)的兄弟 profile。

    不同步的话:refresh token 一经轮换,兄弟手里那份就被服务端作废了,下次刷新
    必失败,用户看到的是「另一个账号莫名掉线」(codex 审核发现)。
    """
    synced = []
    for sib in siblings or ():
        try:
            cur = read_credentials(sib.path)
        except CredentialsMissing:
            continue
        if not cur or cur.get("refreshToken") != used_refresh:
            continue
        merged = dict(cur)
        for k in ("accessToken", "refreshToken", "expiresAt", "refreshTokenExpiresAt"):
            if k in new_creds:
                merged[k] = new_creds[k]
        _write_creds(sib.path, merged)
        synced.append(sib.name)
    return synced


def refresh_profile(env, prof, scan=None, opener=None, force=False, now_ms=None,
                    siblings=()):
    """返回 {status, detail}。status:
    skipped-valid | skipped-active | refreshed | abandoned-cas | failed

    siblings:同一注册表里的其他 profile;刷新成功后,其中凭证同源的会被一并更新。
    """
    creds = read_credentials(prof.path)
    if not creds:
        raise CredentialsMissing(f"{prof.name} 未登录")
    if not token_state(creds, now_ms)["expired"] and not force:
        return {"status": "skipped-valid", "detail": "token 未过期"}
    if not creds.get("refreshToken"):
        raise CredentialsMissing(
            f"{prof.name} 的凭证没有 refreshToken,无法刷新;请 ccm login {prof.name}")
    # 活跃进程门禁(TOCTOU 存在,CAS 才是最后防线;未知状态按活跃处理)
    if not force:
        pids = profile_active_pids(prof.path, prof.compat_link, scan or {},
                                   include_unknown=True)
        dpid = daemon_lock_pid(prof.path)
        if dpid:
            pids = pids | {dpid}
        if pids:
            return {"status": "skipped-active",
                    "detail": f"有活跃 claude 进程 {sorted(pids)},拒绝并发刷新"
                              "(显示缓存数据;--force 可越过)"}
    locks = env.ccm_home / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    rt = creds["refreshToken"]
    # account 级锁:按 refresh token 指纹,同链凭证串行(codex 审核采纳)。
    # 指纹会随轮换而变,所以持锁重读发现换人了必须换锁重来,否则等于没锁。
    for _ in range(_MAX_RELOCK):
        fp = _fingerprint(rt)
        fd = os.open(locks / f"refresh-{fp}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy(f"另一个 ccm 正在刷新同一凭证链({fp})")
            # CAS#1:持锁后重读(别人可能刚刷完)
            cur = read_credentials(prof.path)
            if not cur:
                raise CredentialsMissing(
                    f"{prof.name} 的凭证在刷新期间消失;请 ccm login {prof.name}")
            if not token_state(cur, now_ms)["expired"] and not force:
                return {"status": "skipped-valid", "detail": "持锁重读后发现已被刷新"}
            used_refresh = cur.get("refreshToken")
            if not used_refresh:
                raise CredentialsMissing(
                    f"{prof.name} 的凭证没有 refreshToken,无法刷新;"
                    f"请 ccm login {prof.name}")
            if used_refresh != rt:
                rt = used_refresh   # 锁键过期:拿新指纹重新加锁
                continue
            try:
                resp = refresh_access_token(used_refresh, opener=opener)
            except ApiError as e:
                # 绝不回写旧凭证:请求已发出,旧 refresh token 视为可能已作废
                _log(env, f"failed {prof.name} fp={fp}: {e}")
                return {"status": "failed",
                        "detail": f"{e};未做任何写入,若持续失败请 ccm login {prof.name}"}
            if not isinstance(resp, dict) or not resp.get("access_token"):
                # 200 但没给 token(例如 {"error":"invalid_grant"}):同样绝不回写
                _log(env, f"failed {prof.name} fp={fp}: 响应缺 access_token")
                return {"status": "failed",
                        "detail": "刷新响应里没有 access_token(旧 refresh token 可能已被"
                                  f"服务端轮换);未做任何写入,请 ccm login {prof.name}"}
            # CAS#2:写回前再读,refreshToken 变了说明 Claude Code 抢先 → 放弃
            latest = read_credentials(prof.path)
            if latest and latest.get("refreshToken") != used_refresh:
                _log(env, f"abandoned-cas {prof.name} fp={fp}")
                return {"status": "abandoned-cas",
                        "detail": "凭证在请求期间被他方轮换,放弃写入并采用对方结果"}
            now = int(time.time() * 1000)
            new_creds = dict(cur)
            new_creds["accessToken"] = resp["access_token"]
            new_creds["refreshToken"] = resp.get("refresh_token") or used_refresh
            new_creds["expiresAt"] = now + int(resp.get("expires_in", 8 * 3600)) * 1000
            if resp.get("refresh_token_expires_in"):
                new_creds["refreshTokenExpiresAt"] = \
                    now + int(resp["refresh_token_expires_in"]) * 1000
            bak = _backup_credentials(
                env, prof.name,
                load_json(Path(prof.path) / ".credentials.json", default={}) or {})
            _write_creds(prof.path, new_creds)
            synced = _sync_siblings(env, siblings, used_refresh, new_creds)
            _log(env, f"refreshed {prof.name} fp={fp} -> "
                      f"{_fingerprint(new_creds['refreshToken'])}"
                      + (f" synced={','.join(synced)}" if synced else ""))
            detail = f"新 token 有效 {int(resp.get('expires_in', 0)) // 3600}h"
            if synced:
                detail += f";同源凭证已同步: {', '.join(synced)}"
            if bak:
                detail += f";旧凭证备份 {bak}"
            return {"status": "refreshed", "detail": detail}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return {"status": "abandoned-cas",
            "detail": "凭证在加锁期间被反复轮换,放弃本次刷新(他方已刷新成功)"}
