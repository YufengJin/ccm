"""后台用量采样:samples 入库、历史查询、daemon 进程管理。"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ccm.config import atomic_write_json, load_json
from ccm.errors import CcmError


def record_samples(db, rows, now=None):
    now = int(time.time()) if now is None else now
    for r in rows:
        db.conn.execute("INSERT INTO samples VALUES(?,?,?,?,?,?)",
                        (now, r.account_uuid, r.email,
                         r.five_hour_pct, r.seven_day_pct, r.source))
    db.conn.commit()


def latest_sample(db, account_uuid, max_age_s=None, now=None):
    row = db.conn.execute(
        "SELECT ts, five_h, seven_d, source FROM samples WHERE account=? "
        "ORDER BY ts DESC LIMIT 1", (account_uuid,)).fetchone()
    if not row:
        return None
    if max_age_s is not None:
        now = int(time.time()) if now is None else now
        if now - row[0] > max_age_s:
            return None
    return {"ts": row[0], "five_h": row[1], "seven_d": row[2], "source": row[3]}


def history(db, days=7, now=None):
    """按天 × account 的峰值。"""
    now = int(time.time()) if now is None else now
    since = now - days * 86400
    return db.conn.execute(
        "SELECT date(ts, 'unixepoch') d, email, MAX(five_h), MAX(seven_d) "
        "FROM samples WHERE ts >= ? GROUP BY d, account ORDER BY d", (since,)).fetchall()


def run_sampler(env, db, interval, iterations=None, gather=None, sleep=time.sleep):
    """采样循环;iterations=None 表示常驻。返回完成的采样轮数。"""
    from ccm.config import Registry
    from ccm.usage import gather_usage
    gather = gather or gather_usage
    n = 0
    while iterations is None or n < iterations:
        registry = Registry.load(env)
        try:
            record_samples(db, gather(env, registry))
        except Exception as e:  # 采样失败不退出 daemon
            print(f"采样失败: {e}", file=sys.stderr)
        n += 1
        if iterations is None or n < iterations:
            sleep(interval)
    return n


def _pidfile(env):
    return env.ccm_home / "daemon.pid"


def daemon_status(env):
    info = load_json(_pidfile(env))
    if not info:
        return None
    try:
        os.kill(info["pid"], 0)
    except (ProcessLookupError, PermissionError):
        return None
    return info


def daemon_start(env, interval=300):
    if daemon_status(env):
        raise CcmError("daemon 已在运行(ccm daemon status)")
    logs = env.ccm_home / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(logs / "daemon.log", "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ccm", "daemon", "_run",
             "--interval", str(interval)],
            stdout=log, stderr=log, start_new_session=True,
            env=dict(os.environ, CCM_USER_HOME=str(env.user_home),
                     CCM_HOME=str(env.ccm_home)))
    atomic_write_json(_pidfile(env), {"pid": proc.pid, "interval": interval,
                                      "started": int(time.time())})
    return proc.pid


def daemon_stop(env):
    info = daemon_status(env)
    if not info:
        return False
    os.kill(info["pid"], signal.SIGTERM)
    try:
        os.unlink(_pidfile(env))
    except FileNotFoundError:
        pass
    return True
