import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional

from ccm.errors import ApiError, CredentialsMissing
from ccm.identity import read_credentials, resolve_identity
from ccm.oauth import token_state, fetch_usage

_DEFAULT_OPENER = urllib.request.urlopen  # 测试可整体替换以禁网


@dataclass
class AccountRow:
    account_uuid: str
    email: Optional[str]
    profiles: List[str] = field(default_factory=list)
    source: str = "unavailable"      # live | cache | unavailable
    probe_profile: Optional[str] = None   # live 行:实际查通的那个 profile
    five_hour_pct: Optional[int] = None
    seven_day_pct: Optional[int] = None
    five_hour_resets: Optional[str] = None
    seven_day_resets: Optional[str] = None
    cache_age_s: Optional[int] = None
    detail: str = ""


def _parse_reset(value):
    """resets_at → aware datetime;认不出就 None。

    服务端可能给 ISO 串(带 Z 或 +00:00)、也可能给 epoch 秒/毫秒。之前只 try
    fromisoformat 且只捕 ValueError,数字会抛 TypeError 冲垮整个 usage/ls/best;
    另外 Python 3.10 的 fromisoformat 不认 "Z" 后缀(CI 矩阵含 3.10)。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:          # 毫秒
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    try:
        return _parse_reset(float(s))
    except ValueError:
        return None


def _countdown(resets_at, now=None):
    """ISO 时间 / epoch → '3h12m' 剩余;解析失败 → None。"""
    if not resets_at:
        return None
    target = _parse_reset(resets_at)
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    s = int((target - now).total_seconds())
    if s <= 0:
        return "0m"
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def extract_pcts(payload, now=None):
    """优先 limits[](服务端规范化列表),回退 five_hour/seven_day 对象。"""
    out = {"five_hour_pct": None, "seven_day_pct": None,
           "five_hour_resets": None, "seven_day_resets": None}
    kind_map = {"session": "five_hour", "weekly_all": "seven_day"}

    def _pct(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    if not isinstance(payload, dict):
        return out
    for lim in payload.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        slot = kind_map.get(lim.get("kind"))
        if not slot:
            continue
        pct = _pct(lim.get("percent"))
        if pct is not None:
            out[f"{slot}_pct"] = pct
        # pct 与 resets 各自回退:limits 给了 percent 但 resets_at 缺失/损坏时,
        # 仍然允许 legacy 对象补上倒计时(codex 审核发现)。
        if out[f"{slot}_resets"] is None:
            out[f"{slot}_resets"] = _countdown(lim.get("resets_at"), now)
    for slot in ("five_hour", "seven_day"):
        obj = payload.get(slot) or {}
        if not isinstance(obj, dict):
            continue
        if out[f"{slot}_pct"] is None:
            pct = _pct(obj.get("utilization"))
            if pct is not None:
                out[f"{slot}_pct"] = pct
        if out[f"{slot}_resets"] is None:
            out[f"{slot}_resets"] = _countdown(obj.get("resets_at"), now)
    return out


def _fresh_identity(prof, env, default_name=None):
    """分组键现场重读(注册表缓存可能陈旧,codex 审核采纳);读不出再用缓存。"""
    try:
        ident = resolve_identity(prof.path, env.user_home,
                                 allow_legacy=(prof.name == default_name))
    except CredentialsMissing:
        ident = None
    if ident and ident.get("account_uuid"):
        return ident["account_uuid"], ident.get("email")
    return prof.account_uuid, prof.email


def gather_usage(env, registry, opener=None, now_ms=None):
    """按 account 去重聚合实时用量。P0 全程只读,绝不刷新 token。"""
    opener = opener or _DEFAULT_OPENER
    now_ms = now_ms or int(time.time() * 1000)
    groups = {}
    for prof in registry.profiles.values():
        uuid, email = _fresh_identity(prof, env, registry.default_profile)
        key = uuid or f"?{prof.name}"
        groups.setdefault(key, {"email": email, "profiles": []})
        groups[key]["profiles"].append(prof)
        groups[key]["email"] = groups[key]["email"] or email

    rows = []
    for uuid, g in sorted(groups.items()):
        row = AccountRow(account_uuid=uuid, email=g["email"],
                         profiles=sorted(p.name for p in g["profiles"]))
        # 1) 未过期的 token 按到期时间从晚到早**逐个**试。只试最晚那枚的话,
        #    它一旦被服务端撤销,整组就被错误降级成 cache/unavailable。
        cands = []
        for prof in g["profiles"]:
            try:
                creds = read_credentials(prof.path)
            except CredentialsMissing:
                continue
            if creds and not token_state(creds, now_ms)["expired"]:
                cands.append((creds.get("expiresAt", 0), prof, creds))
        cands.sort(key=lambda c: -c[0])
        for _exp, prof, creds in cands:
            try:
                payload = fetch_usage(creds["accessToken"], opener=opener)
            except ApiError as e:
                row.detail = str(e)
                continue
            row.source = "live"
            row.probe_profile = prof.name
            for k, v in extract_pcts(payload).items():
                setattr(row, k, v)
            break
        if row.source == "live":
            rows.append(row)
            continue
        # 2) 降级:组内最新的 cachedUsageUtilization
        freshest = None
        for prof in g["profiles"]:
            from ccm.config import load_json
            from ccm.errors import CcmError
            try:
                data = load_json(prof.path / ".claude.json") or {}
            except CcmError:
                continue
            cu = data.get("cachedUsageUtilization")
            if cu and (freshest is None or
                       cu.get("fetchedAtMs", 0) > freshest.get("fetchedAtMs", 0)):
                freshest = cu
        if freshest:
            row.source = "cache"
            row.cache_age_s = max(0, (now_ms - freshest.get("fetchedAtMs", 0)) // 1000)
            for k, v in extract_pcts(freshest.get("utilization") or {}).items():
                setattr(row, k, v)
        rows.append(row)
    return rows


def row_dicts(rows):
    return [asdict(r) for r in rows]


def pick_best(rows, registry, env):
    """选最宽裕 account 并映射到 profile。评分规则(公开且稳定):
    候选 = 有百分比数据的 account;score = (max(5h,7d), 7d, uuid) 取最小;
    profile 映射:组内优先 token 未过期者,否则字典序第一个。
    """
    from ccm.errors import CcmError
    eligible = [r for r in rows if r.source in ("live", "cache")
                and (r.five_hour_pct is not None or r.seven_day_pct is not None)]
    if not eligible:
        raise CcmError("所有 account 都无可用用量数据(token 过期且无缓存)")
    def score(r):
        h5 = r.five_hour_pct if r.five_hour_pct is not None else 999
        d7 = r.seven_day_pct if r.seven_day_pct is not None else 999
        return (max(h5, d7), d7, r.account_uuid)
    best = min(eligible, key=score)
    from ccm.selector import pick_preferred
    cand = [registry.profiles[n] for n in best.profiles if n in registry.profiles]
    if best.probe_profile and best.probe_profile in registry.profiles:
        # 只有它的 token 是**被服务端确认过**可用的;别的 profile 可能 expiresAt
        # 还没到但已被撤销,按「未过期 > 默认」重选会切到那种死号(codex 审核发现)
        chosen = best.probe_profile
    else:
        chosen = pick_preferred(cand, registry).name if cand else sorted(best.profiles)[0]
    reason = (f"{best.email or best.account_uuid}: "
              f"5h={best.five_hour_pct}% 7d={best.seven_day_pct}% "
              f"(max={max(best.five_hour_pct or 0, best.seven_day_pct or 0)}, "
              f"来源={best.source})")
    return chosen, reason


def statusline_text(profile_name, row):
    """给 Claude Code statusline 的单行输出;无数据用 ? 占位。"""
    if row is None:
        return f"{profile_name} 5h:? 7d:?"
    h5 = f"{row.five_hour_pct}%" if row.five_hour_pct is not None else "?"
    d7 = f"{row.seven_day_pct}%" if row.seven_day_pct is not None else "?"
    return f"{profile_name} 5h:{h5} 7d:{d7}"
