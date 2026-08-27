"""profile 定位:编码 id + email 双轨,账号再多也不用记别名。

解析顺序(短路):
1. 精确 profile id(a3)
2. 纯数字 N → aN
3. 精确 email(不区分大小写)
4. email 子串(≥3 字符,不区分大小写)/ account uuid 前缀(≥6 字符)
命中多个且同属一个 account → 自动挑最优(token 有效 > 默认 profile > id 序);
跨 account 的多命中 → 报歧义并列出候选,绝不猜。
"""
import os
import re

from ccm.errors import CcmError, CredentialsMissing, ProfileNotFound
from ccm.identity import read_credentials, resolve_identity
from ccm.oauth import token_state

_AUTO_RE = re.compile(r"^a(\d+)$")


def profile_for_path(registry, path):
    """把一个配置目录路径(通常是 CLAUDE_CONFIG_DIR)映射回注册表里的 profile。

    兼容链接也算数:~/.claude-b 与 accounts_root/a3 指的是同一个 profile。
    认不出来返回 None(比如指向一个没注册过的目录)。
    """
    if not path:
        return None
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    for prof in registry.profiles.values():
        for cand in (prof.path, prof.compat_link):
            if cand is None:
                continue
            try:
                if os.path.realpath(cand) == target:
                    return prof
            except OSError:
                continue
    return None


def next_auto_id(registry):
    n = 1
    while f"a{n}" in registry.profiles:
        n += 1
    return f"a{n}"


def _identities(env, registry):
    out = []
    for prof in registry.profiles.values():
        try:
            ident = resolve_identity(
                prof.path, env.user_home,
                allow_legacy=(prof.name == registry.default_profile)) or {}
        except CredentialsMissing:
            ident = {}
        out.append((prof,
                    (ident.get("email") or prof.email or ""),
                    (ident.get("account_uuid") or prof.account_uuid or "")))
    return out


def pick_preferred(profs, registry):
    """同一 account 的多个 profile 里挑最优:token 有效 > 默认 > id 序。"""
    def key(p):
        try:
            creds = read_credentials(p.path)
        except CredentialsMissing:
            creds = None
        valid = bool(creds) and not token_state(creds)["expired"]
        return (not valid, p.name != registry.default_profile, p.name)
    return sorted(profs, key=key)[0]


def resolve_profile(env, registry, selector):
    if not registry.profiles:
        raise ProfileNotFound("尚无注册 profile(先跑 ccm migrate 或 ccm add)")
    if selector in registry.profiles:
        return registry.profiles[selector]
    if selector.isdigit() and f"a{selector}" in registry.profiles:
        return registry.profiles[f"a{selector}"]
    sel = selector.lower()
    idents = _identities(env, registry)
    # 逐级短路(design §4):前一级有命中就不再看后一级。以前 email 子串与 uuid
    # 前缀塞在同一级,「同时是 A 的 email 子串和 B 的 uuid 前缀」会被误判为歧义。
    for matches in (
            [t for t in idents if t[1].lower() == sel],                   # 精确 email
            [t for t in idents if len(sel) >= 3 and sel in t[1].lower()],  # email 子串
            [t for t in idents                                             # uuid 前缀
             if len(sel) >= 6 and t[2] and t[2].lower().startswith(sel)]):
        if matches:
            break
    else:
        matches = []
    if not matches:
        listing = ", ".join(f"{p.name}={e or '?'}" for p, e, _ in idents)
        raise ProfileNotFound(f"没有匹配 {selector!r} 的 profile(现有: {listing})")
    if len(matches) > 1:
        uuids = {u for _, _, u in matches}
        # 空 uuid = 「无法证明同属一个 account」,不能当成同 account 静默挑一个。
        # 自动挑选的前提是全部候选都有**同一个非空** uuid(codex 审核发现)。
        if len(uuids) > 1 or "" in uuids:
            listing = ", ".join(f"{p.name}={e or '?'}" for p, e, _ in matches)
            raise CcmError(
                f"{selector!r} 命中多个 profile 且无法确认同属一个账号,"
                f"请说更具体: {listing}")
    return pick_preferred([p for p, _, _ in matches], registry)
