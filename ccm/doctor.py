import os
from dataclasses import dataclass
from pathlib import Path

from ccm.config import load_json, save_state
from ccm.errors import CcmError, CredentialsMissing
from ccm.identity import read_credentials, resolve_identity
from ccm.layout import apply_links, link_plan
from ccm.oauth import token_state


@dataclass
class CheckResult:
    check: str
    subject: str
    level: str  # ok | warn | fail
    msg: str
    fixed: bool = False


# 迁移不变量类检查:仅这些 fail 才触发 migrate 阶段 6 自动回滚(codex 审核采纳)
INVARIANT_CHECKS = {"profile-dir", "shared-links", "shared-source", "compat-link", "state"}


def run_checks(env, registry, state, fix=False, opener=None, online=False):
    out = []
    for name in sorted(registry.profiles):
        prof = registry.profiles[name]
        # 目录存在可写
        if not prof.path.is_dir():
            out.append(CheckResult("profile-dir", name, "fail", f"目录缺失: {prof.path}"))
            continue
        if not os.access(prof.path, os.W_OK):
            out.append(CheckResult("profile-dir", name, "fail", f"不可写: {prof.path}"))
        else:
            out.append(CheckResult("profile-dir", name, "ok", str(prof.path)))
        # 凭证与 token 寿命(过期是 warn 不是 fail:不属于迁移不变量)
        try:
            creds = read_credentials(prof.path)
        except CredentialsMissing as e:
            creds = None
            out.append(CheckResult("credentials", name, "warn", str(e)))
        if creds:
            st = token_state(creds)
            if st["expired"]:
                out.append(CheckResult("token", name, "warn",
                                       "access token 已过期(P1 前用 ccm login 重登)"))
            else:
                out.append(CheckResult("token", name, "ok",
                                       f"剩余 {st['expires_in_s'] // 3600}h;"
                                       f"refresh 剩余 {st['refresh_expires_in_s'] // 86400}d"))
        elif creds is None and not (prof.path / ".credentials.json").exists():
            out.append(CheckResult("credentials", name, "warn", "未登录"))
        # 身份可解析
        try:
            ident = resolve_identity(prof.path, env.user_home,
                                     allow_legacy=(name == registry.default_profile))
        except CredentialsMissing:
            ident = None
        if ident:
            out.append(CheckResult("identity", name, "ok",
                                   f"{ident.get('email')} ({ident.get('source')})"))
        else:
            out.append(CheckResult("identity", name, "warn",
                                   "身份不可解析(登录后自动恢复)"))
        # 共享 symlink
        plan = link_plan(prof.path, registry.shared_root, registry.shared)
        broken = [a for a in plan if a.status in ("missing", "wrong")]
        conflicts = [a for a in plan if a.status == "conflict"]
        fixed = False
        if broken and fix:
            apply_links(prof.path, registry.shared_root, registry.shared)
            fixed = True
        if broken:
            out.append(CheckResult("shared-links", name, "ok" if fixed else "fail",
                                   f"{len(broken)} 条链接待修: "
                                   + ", ".join(a.item for a in broken), fixed))
        if conflicts:
            # 实体冲突绝不自动修(可能含用户新内容,spec §15.7)
            out.append(CheckResult("shared-links", name, "warn",
                                   "conflict(实体占位,需人工处理): "
                                   + ", ".join(a.item for a in conflicts)))
        if not broken and not conflicts:
            out.append(CheckResult("shared-links", name, "ok", "全部一致"))
        # 兼容链接
        if prof.compat_link:
            k = "compat-link"
            if prof.compat_link.is_symlink() and \
                    os.readlink(prof.compat_link) == str(prof.path):
                out.append(CheckResult(k, name, "ok", str(prof.compat_link)))
            elif os.path.lexists(prof.compat_link) and not prof.compat_link.is_symlink():
                out.append(CheckResult(k, name, "fail",
                                       f"{prof.compat_link} 被实体占用,不敢动"))
            else:
                if fix:
                    if prof.compat_link.is_symlink():
                        os.unlink(prof.compat_link)
                    os.symlink(prof.path, prof.compat_link)
                    out.append(CheckResult(k, name, "ok", "已重建", True))
                else:
                    out.append(CheckResult(k, name, "fail",
                                           f"兼容链接缺失或指错: {prof.compat_link}"))
    # 共享源存在性
    for item in registry.shared:
        src = registry.shared_root / item
        if not os.path.lexists(src):
            out.append(CheckResult("shared-source", item, "warn",
                                   f"清单中的共享项不存在: {src}(跳过铺链)"))
    # state 指向有效性
    if state:
        active = state.get("active")
        if active not in registry.profiles:
            fallback = registry.default_profile \
                if registry.default_profile in registry.profiles else \
                (sorted(registry.profiles)[0] if registry.profiles else None)
            if fix and fallback:
                save_state(env, fallback, "doctor --fix")
                out.append(CheckResult("state", active or "?", "ok",
                                       f"已回落到 {fallback}", True))
            else:
                out.append(CheckResult("state", active or "?", "fail",
                                       f"state 指向不存在的 profile: {active}"))
        else:
            out.append(CheckResult("state", active, "ok", "有效"))
    # 遗留文件提示
    legacy = env.user_home / ".claude.json"
    if legacy.exists():
        out.append(CheckResult("legacy-claude-json", "-", "warn",
                               f"遗留 {legacy} 仍被 Claude Code 读取,保持原位(spec §15.6)"))
    # API 连通性:仅 --online,失败永远只是 warn
    if online:
        from ccm.oauth import fetch_usage
        from ccm.errors import ApiError
        probe = None
        for name in sorted(registry.profiles):
            try:
                creds = read_credentials(registry.profiles[name].path)
            except CredentialsMissing:
                continue
            if creds and not token_state(creds)["expired"]:
                probe = (name, creds)
                break
        if probe:
            try:
                fetch_usage(probe[1]["accessToken"], opener=opener)
                out.append(CheckResult("api", probe[0], "ok", "连通"))
            except ApiError as e:
                out.append(CheckResult("api", probe[0], "warn", str(e)))
        else:
            out.append(CheckResult("api", "-", "warn", "无有效 token 可探测"))
    return out
