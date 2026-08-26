"""本地 token 统计:jsonl 增量扫描 → sqlite → 聚合。

- offset 只推进到最后一个完整换行(写入方可能留半行)
- st_ino 变化/文件缩小 → 同事务删旧事件后整文件重扫,(sid,rid,uid) 主键防重复
- 归因主数据源 session-env/(目录名=sessionId);映射持久化;多重映射 → ambiguous
"""
import json
import os
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, ino INTEGER, size INTEGER, mtime REAL, offset INTEGER);
CREATE TABLE IF NOT EXISTS events(
  sid TEXT, rid TEXT, uid TEXT, src TEXT, ts TEXT, model TEXT, project TEXT,
  in_tok INTEGER, out_tok INTEGER, cr_tok INTEGER, c5m INTEGER, c1h INTEGER,
  PRIMARY KEY(sid, rid, uid));
CREATE TABLE IF NOT EXISTS sess_profile(sid TEXT PRIMARY KEY, profile TEXT);
CREATE TABLE IF NOT EXISTS samples(
  ts INTEGER, account TEXT, email TEXT, five_h INTEGER, seven_d INTEGER, source TEXT);
"""


class CostDB:
    def __init__(self, env):
        env.ccm_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(env.ccm_home / "usage.db")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()


def _update_session_map(env, registry, db):
    """扫各 profile 的 session-env/,新映射持久化;冲突 → ambiguous,绝不任选其一。"""
    cur = db.conn
    for prof in registry.profiles.values():
        se = Path(prof.path) / "session-env"
        if not se.is_dir():
            continue
        for sid in os.listdir(se):
            row = cur.execute("SELECT profile FROM sess_profile WHERE sid=?",
                              (sid,)).fetchone()
            if row is None:
                cur.execute("INSERT INTO sess_profile VALUES(?,?)", (sid, prof.name))
            elif row[0] not in (prof.name, "ambiguous"):
                cur.execute("UPDATE sess_profile SET profile='ambiguous' WHERE sid=?",
                            (sid,))
    cur.commit()


def _parse_lines(chunk, src, project):
    events = []
    for line in chunk.split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        msg = d.get("message") or {}
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not usage or not msg.get("model"):
            continue
        cc = usage.get("cache_creation") or {}
        events.append((
            d.get("sessionId") or "?", d.get("requestId") or "?",
            d.get("uuid") or "?", src, d.get("timestamp") or "",
            msg["model"], project,
            usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
            usage.get("cache_read_input_tokens") or 0,
            cc.get("ephemeral_5m_input_tokens") or 0,
            cc.get("ephemeral_1h_input_tokens") or 0))
    return events


def scan_projects(env, registry, db):
    """增量扫描共享 projects/ 下全部 jsonl;返回新增事件数。"""
    _update_session_map(env, registry, db)
    projects = env.shared_root / "projects"
    if not projects.is_dir():   # 未迁移布局:退回 default 的 projects
        projects = env.user_home / ".claude" / "projects"
    if not projects.is_dir():
        return 0
    cur = db.conn
    added = 0
    for f in sorted(projects.glob("*/*.jsonl")):
        try:
            st = os.stat(f)
        except OSError:
            continue
        row = cur.execute("SELECT ino,size,mtime,offset FROM files WHERE path=?",
                          (str(f),)).fetchone()
        offset = 0
        if row:
            ino, size, mtime, offset = row
            if ino != st.st_ino or st.st_size < size:
                # 原子替换/缩小 → 同事务删旧事件,整文件重扫
                cur.execute("DELETE FROM events WHERE src=?", (str(f),))
                offset = 0
            elif st.st_size == size and st.st_mtime == mtime:
                continue  # 无变化
        with open(f, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        cut = chunk.rfind(b"\n")
        if cut < 0:
            continue  # 只有半行,等补全
        events = _parse_lines(chunk[:cut], str(f), f.parent.name)
        for ev in events:
            r = cur.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ev)
            added += r.rowcount
        cur.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                    (str(f), st.st_ino, st.st_size, st.st_mtime, offset + cut + 1))
        cur.commit()
    return added


def aggregate(db, by="profile", since_ts=None):
    """聚合;cost 在查询时按当前价格表折算(unknown 模型计 0 并单列)。"""
    from ccm.pricing import price_event
    key_expr = {
        "profile": "COALESCE(p.profile, 'unknown')",
        "model": "e.model",
        "project": "e.project",
        "day": "substr(e.ts, 1, 10)",
    }[by]
    where = f"WHERE e.ts >= '{since_ts}'" if since_ts else ""
    rows = db.conn.execute(f"""
        SELECT {key_expr} AS k, COUNT(*), e.model,
               SUM(e.in_tok), SUM(e.out_tok), SUM(e.cr_tok), SUM(e.c5m), SUM(e.c1h)
        FROM events e LEFT JOIN sess_profile p ON e.sid = p.sid
        {where}
        GROUP BY k, e.model""").fetchall()
    agg = {}
    for k, n, model, i, o, cr, c5, c1 in rows:
        a = agg.setdefault(k, {"key": k, "events": 0, "in_tok": 0, "out_tok": 0,
                               "cache_read": 0, "cost_usd": 0.0, "unknown_models": []})
        a["events"] += n
        a["in_tok"] += i or 0
        a["out_tok"] += o or 0
        a["cache_read"] += cr or 0
        c = price_event(model, i or 0, o or 0, cr or 0, c5 or 0, c1 or 0)
        if c is None:
            if model not in a["unknown_models"]:
                a["unknown_models"].append(model)
        else:
            a["cost_usd"] += c
    return sorted(agg.values(), key=lambda r: -r["cost_usd"])
