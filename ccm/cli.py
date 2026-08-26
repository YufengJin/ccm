import argparse
import os
import sys

from ccm import __version__
from ccm.errors import CcmError, ProfileNotFound


def _env_ctx():
    from ccm.config import Env, Registry, load_state
    env = Env.from_environ()
    return env, Registry.load(env), load_state(env)


def _resolve(env, registry, selector):
    """所有吃 profile 参数的命令统一走 id/email/序号/uuid 定位。"""
    from ccm.selector import resolve_profile
    return resolve_profile(env, registry, selector)


def cmd_env(args):
    env, registry, state = _env_ctx()
    if not state:
        return 0
    try:
        prof = registry.get(state["active"])
    except ProfileNotFound:
        return 0  # state 指向已删 profile → 视同未设置
    from ccm.shellinit import env_exports
    print(env_exports(prof.path))
    return 0


def cmd_use(args):
    from ccm.config import save_state
    from ccm.shellinit import env_exports
    env, registry, _ = _env_ctx()
    if args.auto:
        from ccm.usage import gather_usage, pick_best
        name, reason = pick_best(gather_usage(env, registry), registry, env)
        print(f"自动选择 {name} — {reason}", file=sys.stderr)
        prof = registry.get(name)
    else:
        if not args.name:
            raise CcmError("用法: ccm use <id|email|序号> 或 ccm use --auto")
        prof = _resolve(env, registry, args.name)
    save_state(env, prof.name, "ccm use")
    human = f"已切换到 {prof.name} ({prof.path})"
    if args.emit_env:
        print(env_exports(prof.path))
        print(human, file=sys.stderr)
    else:
        print(human)
    return 0


def cmd_current(args):
    env, registry, state = _env_ctx()
    if not state:
        return 1
    name = state["active"]
    try:
        prof = registry.get(name)
    except ProfileNotFound:
        return 1
    print(name if args.quiet else f"{name}\t{prof.path}")
    return 0


def cmd_init(args):
    from ccm.shellinit import install_block, rc_block
    env, _, _ = _env_ctx()
    if args.print:
        print(rc_block())
        return 0
    rc_path = env.user_home / ".bashrc"
    changed = install_block(rc_path)
    print(f"{'已写入' if changed else '无需改动'}: {rc_path}")
    return 0


def cmd_link(args):
    from ccm.layout import apply_links
    env, registry, _ = _env_ctx()
    names = [_resolve(env, registry, args.name).name] if args.name \
        else sorted(registry.profiles)
    for name in names:
        prof = registry.get(name)
        out = apply_links(prof.path, registry.shared_root, registry.shared)
        bad = [a for a in out if a.status not in ("ok", "skip_no_source")]
        mark = "⚠" if bad else "✓"
        print(f"{mark} {name}: " + ", ".join(f"{a.item}={a.status}" for a in out))
    return 0


def cmd_doctor(args):
    from ccm.doctor import run_checks
    env, registry, state = _env_ctx()
    if not registry.profiles:
        raise CcmError("尚无注册 profile(先跑 ccm migrate)")
    results = run_checks(env, registry, state, fix=args.fix, online=args.online)
    icon = {"ok": "✓", "warn": "△", "fail": "✗"}
    worst = 0
    for r in results:
        suffix = "(已修)" if r.fixed else ""
        print(f"{icon[r.level]} {r.check:18} {r.subject:10} {r.msg}{suffix}")
        worst = max(worst, {"ok": 0, "warn": 0, "fail": 1}[r.level])
    return worst


def cmd_migrate(args):
    import json as _json
    from ccm.migrate import build_plan, execute_migration, rollback_ops, Journal, cleanup
    env, _, _ = _env_ctx()
    if args.rollback:
        j = Journal.load(env)
        if not j.ops:
            print("无未完结的迁移 journal,无需回滚")
            return 0
        rollback_ops(env, j)
        print("已回滚")
        return 0
    if args.cleanup:
        for line in cleanup(env):
            print(line)
        return 0
    plan = build_plan(env)
    print("== 迁移计划 ==")
    for m in plan["moves"]:
        print(f"  搬迁 {m['old']} -> {m['new']}(旧路径留兼容 symlink)")
    print(f"  拆共享库 -> {env.shared_root}: " + ", ".join(plan["splits"]))
    if plan["active_procs"]:
        print("  活跃 claude 进程:")
        for d, pids in plan["active_procs"].items():
            print(f"    {d}: {pids}")
    else:
        print("  活跃 claude 进程: 无")
    if args.dry_run:
        print("(dry-run,未做任何修改)")
        return 0
    if not args.yes:
        import sys as _sys
        if not _sys.stdin.isatty():
            raise CcmError("非交互环境需 --yes 确认")
        if input("执行迁移? [y/N] ").strip().lower() != "y":
            print("已取消")
            return 1
    execute_migration(env, plan)
    print("迁移完成。验证: ccm doctor && ccm ls")
    return 0


def cmd_usage(args):
    import json as _json
    from ccm.usage import gather_usage, row_dicts
    from ccm.render import table
    env, registry, _ = _env_ctx()
    if not registry.profiles:
        raise CcmError("尚无注册 profile(先跑 ccm migrate 或 ccm add)")
    if args.history:
        from ccm.cost import CostDB
        from ccm.daemon import history as _history
        from ccm.render import table as _t
        days = int(args.history.rstrip("d"))
        h = _history(CostDB(env), days=days)
        print(_t(["日期", "账号", "5h峰值", "7d峰值"],
                 [[d, e or "-", f"{a}%", f"{b}%"] for d, e, a, b in h]))
        return 0
    if args.watch:
        import time as _time
        n = 0
        while True:
            rows = gather_usage(env, registry)
            print("\033[2J\033[H", end="")
            _print_usage_table(rows)
            n += 1
            if args.iterations and n >= args.iterations:
                return 0
            _time.sleep(args.interval)
    rows = gather_usage(env, registry)
    if args.json:
        print(_json.dumps(row_dicts(rows), ensure_ascii=False, indent=2))
        return 0
    _print_usage_table(rows)
    return 0


def _print_usage_table(rows):
    from ccm.render import table
    ALERT = 80  # 阈值告警(spec P2)
    body = []
    for r in rows:
        src = {"live": "实时", "cache": f"缓存({(r.cache_age_s or 0) // 3600}h前)",
               "unavailable": "不可用"}[r.source]
        def pct(v):
            if v is None:
                return "-"
            return f"⚠{v}%" if v >= ALERT else f"{v}%"
        body.append([r.email or r.account_uuid, "+".join(r.profiles),
                     pct(r.five_hour_pct), r.five_hour_resets or "-",
                     pct(r.seven_day_pct), r.seven_day_resets or "-", src])
    print(table(["账号", "profiles", "5h", "重置", "7d", "重置", "来源"], body))


def cmd_ls(args):
    import json as _json
    from ccm.identity import read_credentials, resolve_identity
    from ccm.oauth import token_state
    from ccm.render import table
    from ccm.errors import CredentialsMissing
    env, registry, state = _env_ctx()
    active = (state or {}).get("active")
    rows, uuid_group, next_group = [], {}, 1
    for name in sorted(registry.profiles):
        prof = registry.profiles[name]
        try:
            ident = resolve_identity(prof.path, env.user_home,
                                     allow_legacy=(name == registry.default_profile)) or {}
        except CredentialsMissing:
            ident = {}
        email = ident.get("email") or prof.email or "-"
        uuid = ident.get("account_uuid") or prof.account_uuid
        if uuid and uuid not in uuid_group:
            uuid_group[uuid] = next_group
            next_group += 1
        try:
            creds = read_credentials(prof.path)
        except CredentialsMissing:
            creds = None
        if creds:
            st = token_state(creds)
            tok = f"{st['expires_in_s'] // 3600}h" if not st["expired"] else "已过期"
        else:
            tok = "未登录"
        rows.append({"name": name, "active": name == active, "email": email,
                     "subscription": ident.get("subscription") or prof.subscription or "-",
                     "token": tok,
                     "account": f"#{uuid_group.get(uuid, '-')}",
                     "path": str(prof.path)})
    if args.json:
        print(_json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    body = [[("●" if r["active"] else " ") + r["name"], r["email"],
             r["subscription"], r["token"], r["account"], r["path"]] for r in rows]
    print(table(["profile", "email", "订阅", "token", "账号", "路径"], body))
    return 0


def cmd_add(args):
    from ccm.lifecycle import add_profile
    env, registry, _ = _env_ctx()
    prof = add_profile(env, registry, args.name, note=args.note or "",
                       import_dir=getattr(args, "import_dir", None), move=args.move)
    # args.name 为空时自动分配了编码 id
    print(f"已注册 {prof.name}: {prof.path}"
          + (f" (email={prof.email})" if prof.email else ",未登录 — 跑 ccm login "
             + prof.name))
    return 0


def cmd_rm(args):
    from ccm.lifecycle import remove_profile
    from ccm.procs import scan_claude_procs
    env, registry, _ = _env_ctx()
    prof = _resolve(env, registry, args.name)
    args.name = prof.name
    if not args.yes:
        import sys as _sys
        if not _sys.stdin.isatty():
            raise CcmError("非交互环境需 --yes 确认")
        if input(f"删除 {args.name} ({prof.path})? [y/N] ").strip().lower() != "y":
            print("已取消")
            return 1
    scan = scan_claude_procs(env.proc_root, env.user_home)
    bak = remove_profile(env, registry, args.name, scan=scan,
                         keep_data=args.keep_data)
    print(f"已移除 {args.name}" + (f",备份: {bak}" if bak else "(数据保留)"))
    return 0


def cmd_rename(args):
    from ccm.lifecycle import rename_profile
    from ccm.procs import scan_claude_procs
    env, registry, _ = _env_ctx()
    scan = scan_claude_procs(env.proc_root, env.user_home)
    old = _resolve(env, registry, args.old).name
    rename_profile(env, registry, old, args.new, scan=scan)
    args.old = old
    print(f"{args.old} -> {args.new}")
    return 0


def cmd_logout(args):
    from ccm.lifecycle import logout_profile
    from ccm.procs import scan_claude_procs
    env, registry, _ = _env_ctx()
    scan = scan_claude_procs(env.proc_root, env.user_home)
    args.name = _resolve(env, registry, args.name).name
    logout_profile(env, registry, args.name, scan=scan, keep_backup=args.keep_backup)
    print(f"{args.name} 已登出" + ("(凭证已备份)" if args.keep_backup else "(未留副本)"))
    return 0


def cmd_login(args):
    from ccm.lifecycle import login_profile
    env, registry, _ = _env_ctx()
    print(f"启动 claude,请在其中执行 /login 完成登录后退出…", file=sys.stderr)
    return login_profile(env, registry, _resolve(env, registry, args.name).name)


def cmd_show(args):
    import json as _json
    from ccm.lifecycle import show_profile
    env, registry, _ = _env_ctx()
    info = show_profile(env, registry, _resolve(env, registry, args.name).name)
    if args.json:
        print(_json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    for k in ("name", "path", "compat_link", "email", "account_uuid",
              "subscription", "note"):
        print(f"{k:14} {info[k] or '-'}")
    t = info["token"]
    print(f"{'token':14} " + (f"剩余 {t['expires_in_s'] // 3600}h"
                              f"(refresh {t['refresh_expires_in_s'] // 86400}d)"
                              if t and not t["expired"] else
                              "已过期" if t else "未登录"))
    print(f"{'links':14} " + ("全部一致" if info["links_ok"] else
                              str({k: v for k, v in info["links"].items()
                                   if v not in ("ok", "skip_no_source")})))
    return 0


def cmd_shell(args):
    import subprocess
    env, registry, _ = _env_ctx()
    prof = _resolve(env, registry, args.name)
    shell = os.environ.get("SHELL") or "/bin/bash"
    print(f"进入 {args.name} 的子 shell(exit 返回)…", file=sys.stderr)
    return subprocess.call([shell], env=dict(
        os.environ, CLAUDE_CONFIG_DIR=str(prof.path), CCM_PROFILE_PINNED="1"))


def cmd_shared(args):
    from ccm.sharing import shared_add, shared_rm
    env, registry, _ = _env_ctx()
    if args.action == "ls":
        for item in registry.shared:
            src = registry.shared_root / item
            print(f"{'✓' if os.path.lexists(src) else '✗'} {item}")
    elif args.action == "add":
        shared_add(env, registry, args.item, from_profile=args.from_profile)
        print(f"已加入共享并为全部 profile 铺链: {args.item}")
    elif args.action == "rm":
        shared_rm(env, registry, args.item)
        print(f"已从清单移除(文件保留): {args.item}")
    return 0


def cmd_unlink(args):
    from ccm.sharing import unlink_item
    env, registry, _ = _env_ctx()
    args.name = _resolve(env, registry, args.name).name
    unlink_item(env, registry, args.name, args.item)
    print(f"{args.name}/{args.item} 已独立(不再随共享变化)")
    return 0


def cmd_diff(args):
    from ccm.sharing import diff_profiles
    from ccm.render import table
    env, registry, _ = _env_ctx()
    a = _resolve(env, registry, args.a).name
    b = _resolve(env, registry, args.b).name
    d = diff_profiles(registry, a, b)
    args.a, args.b = a, b
    if not d:
        print("非共享配置无差异")
        return 0
    print(table(["条目", args.a, args.b], [[x["item"], x["a"], x["b"]] for x in d]))
    return 0


def cmd_token(args):
    from ccm.identity import read_credentials
    env, registry, _ = _env_ctx()
    if not args.yes:
        print("ccm: 打印 access token 需要 --yes 显式确认", file=sys.stderr)
        return 1
    creds = read_credentials(_resolve(env, registry, args.name).path)
    if not creds:
        raise CcmError(f"{args.name} 未登录")
    print(creds["accessToken"])
    return 0


def cmd_completion(args):
    cmds = ("add rm rename login logout show ls use current run env init shell "
            "usage best cost refresh doctor link migrate backup restore export "
            "import statusline daemon shared unlink diff token completion")
    print(f'''_ccm() {{
  local cur=${{COMP_WORDS[COMP_CWORD]}}
  if [ $COMP_CWORD -eq 1 ]; then
    COMPREPLY=($(compgen -W "{cmds}" -- "$cur"))
  else
    COMPREPLY=($(compgen -W "$(command ccm _complete-names 2>/dev/null)" -- "$cur"))
  fi
}}
complete -F _ccm ccm''')
    return 0


def cmd_complete_names(args):
    env, registry, _ = _env_ctx()
    words = sorted(registry.profiles)
    words += sorted({p.email for p in registry.profiles.values() if p.email})
    print(" ".join(words))
    return 0


def cmd_best(args):
    import json as _json
    from ccm.usage import gather_usage, pick_best
    env, registry, _ = _env_ctx()
    rows = gather_usage(env, registry)
    name, reason = pick_best(rows, registry, env)
    if args.json:
        print(_json.dumps({"profile": name, "reason": reason}, ensure_ascii=False))
    else:
        print(name)
        print(f"依据: {reason}", file=sys.stderr)
    return 0


def cmd_statusline(args):
    # 必须快且离线:优先 usage.db 最新样本(<15min),否则 .claude.json 缓存,不打网络
    from ccm.cost import CostDB
    from ccm.daemon import latest_sample
    from ccm.usage import AccountRow, statusline_text, _fresh_identity
    from ccm.config import load_json
    env, registry, state = _env_ctx()
    name = (state or {}).get("active") or registry.default_profile or "?"
    prof = registry.profiles.get(name)
    if not prof:
        print(name)
        return 0
    uuid, _email = _fresh_identity(prof, env)
    row = None
    if uuid:
        s = latest_sample(CostDB(env), uuid, max_age_s=900)
        if s:
            row = AccountRow(account_uuid=uuid, email=None,
                             five_hour_pct=s["five_h"], seven_day_pct=s["seven_d"],
                             source=s["source"])
        else:
            try:
                data = load_json(prof.path / ".claude.json") or {}
            except CcmError:
                data = {}
            cu = data.get("cachedUsageUtilization")
            if cu:
                from ccm.usage import extract_pcts
                pct = extract_pcts(cu.get("utilization") or {})
                row = AccountRow(account_uuid=uuid, email=None,
                                 five_hour_pct=pct["five_hour_pct"],
                                 seven_day_pct=pct["seven_day_pct"], source="cache")
    print(statusline_text(name, row))
    return 0


def cmd_daemon(args):
    from ccm.cost import CostDB
    from ccm.daemon import (daemon_start, daemon_status, daemon_stop, history,
                            run_sampler)
    from ccm.render import table
    env, registry, _ = _env_ctx()
    if args.action == "start":
        pid = daemon_start(env, interval=args.interval)
        print(f"daemon 已启动 pid={pid} interval={args.interval}s")
    elif args.action == "stop":
        print("已停止" if daemon_stop(env) else "没有在运行的 daemon")
    elif args.action == "status":
        info = daemon_status(env)
        print(f"运行中 pid={info['pid']} interval={info['interval']}s"
              if info else "未运行")
    elif args.action == "_run":   # 内部:daemon 子进程主体
        run_sampler(env, CostDB(env), interval=args.interval)
    return 0


def cmd_backup(args):
    from ccm.backup import create_backup
    env, registry, _ = _env_ctx()
    names = [_resolve(env, registry, args.name).name] if args.name \
        else sorted(registry.profiles)
    for name in names:
        p = create_backup(env, registry, name,
                          with_credentials=args.with_credentials)
        print(f"{name}: {p}")
    if args.with_credentials:
        print("注意: 备份含凭证(0600),请妥善保管")
    return 0


def cmd_restore(args):
    from ccm.backup import restore_backup
    env, registry, _ = _env_ctx()
    prof = restore_backup(env, registry, args.archive, into=args.into)
    print(f"已恢复为 {prof.name}: {prof.path}")
    return 0


def cmd_export(args):
    from ccm.backup import create_backup
    env, registry, _ = _env_ctx()
    p = create_backup(env, registry, _resolve(env, registry, args.name).name,
                      with_credentials=True, dest=args.file)
    print(f"已导出(含凭证,0600): {p}")
    return 0


def cmd_import(args):
    from ccm.backup import restore_backup
    env, registry, _ = _env_ctx()
    prof = restore_backup(env, registry, args.file, into=args.into)
    print(f"已导入为 {prof.name}: {prof.path}")
    return 0


def cmd_refresh(args):
    from ccm.procs import scan_claude_procs
    from ccm.refresh import refresh_profile
    env, registry, _ = _env_ctx()
    names = sorted(registry.profiles) if getattr(args, "all", False) else \
        ([_resolve(env, registry, args.name).name] if args.name else None)
    if not names:
        raise CcmError("用法: ccm refresh <id|email> 或 ccm refresh --all")
    scan = scan_claude_procs(env.proc_root, env.user_home)
    worst = 0
    for name in names:
        prof = registry.get(name)
        r = refresh_profile(env, prof, scan=scan, force=args.force)
        icon = {"refreshed": "✓", "skipped-valid": "✓", "skipped-active": "△",
                "abandoned-cas": "△", "failed": "✗"}[r["status"]]
        print(f"{icon} {name}: {r['status']} — {r['detail']}")
        worst = max(worst, 1 if r["status"] == "failed" else 0)
    return worst


def cmd_cost(args):
    import json as _json
    from datetime import datetime, timedelta, timezone
    from ccm.cost import CostDB, scan_projects, aggregate
    from ccm.render import table
    env, registry, _ = _env_ctx()
    db = CostDB(env)
    scan_projects(env, registry, db)
    since = None
    if args.since:
        n = int(args.since.rstrip("d"))
        since = (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")
    rows = aggregate(db, by=args.by, since_ts=since)
    if args.json:
        print(_json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    body = [[r["key"], str(r["events"]), f'{r["in_tok"]:,}', f'{r["out_tok"]:,}',
             f'{r["cache_read"]:,}', f'${r["cost_usd"]:.2f}'
             + ("+?" if r["unknown_models"] else "")] for r in rows]
    print(table([args.by, "事件", "input", "output", "cache读", "等价金额"], body))
    print("\n注: Max 订阅不按 API 计费;金额是按 API 价目表的等价折算,仅供比较,不是账单。")
    unk = sorted({m for r in rows for m in r["unknown_models"]})
    if unk:
        print(f"未知模型(计 0): {', '.join(unk)}")
    return 0


def cmd_run(args):
    env, registry, _ = _env_ctx()
    prof = _resolve(env, registry, args.name)
    child_env = dict(os.environ,
                     CLAUDE_CONFIG_DIR=str(prof.path),
                     CCM_PROFILE_PINNED="1")
    argv = ["claude"] + list(args.rest or [])
    if argv[1:2] == ["--"]:
        argv = ["claude"] + argv[2:]
    try:
        os.execvpe("claude", argv, child_env)
    except FileNotFoundError:
        raise CcmError("claude 未安装或不在 PATH(安装后重试,或用 ccm env 手动注入)")


def _dispatch_guard(fn):
    """执行子命令函数;CcmError → (1, 消息),其余异常向上抛。"""
    try:
        rc = fn()
        return (rc if isinstance(rc, int) else 0), ""
    except CcmError as e:
        print(f"ccm: {e}", file=sys.stderr)
        return 1, str(e)


def build_parser():
    p = argparse.ArgumentParser(prog="ccm", description="Claude 多账号统一管理")
    p.add_argument("--version", action="version", version=f"ccm {__version__}")
    p.set_defaults(func=None)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("env", help="输出当前 profile 的 export 语句")
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("use", help="切换活跃 profile")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--auto", action="store_true", help="切到额度最宽裕的账号")
    sp.add_argument("--emit-env", action="store_true",
                    help=argparse.SUPPRESS)  # shell 函数内部用
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser("current", help="显示当前 profile")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser("init", help="安装 shell 集成")
    sp.add_argument("shell", nargs="?", default="bash", choices=["bash"])
    sp.add_argument("--print", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("link", help="按共享清单幂等重建 symlink")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser("doctor", help="体检")
    sp.add_argument("--fix", action="store_true")
    sp.add_argument("--online", action="store_true", help="附带 API 连通性探测")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("migrate", help="迁移到 ccm 布局")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--rollback", action="store_true")
    sp.add_argument("--cleanup", action="store_true")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("usage", help="各 account 实时用量")
    sp.add_argument("--all", action="store_true", default=True,
                    help="全部 account(默认即全部)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--watch", action="store_true")
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--iterations", type=int, help=argparse.SUPPRESS)  # 测试用
    sp.add_argument("--history", metavar="7d", help="显示采样历史(需 daemon)")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("shell", help="开一个注入好环境的子 shell")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_shell)

    sp = sub.add_parser("shared", help="维护共享清单")
    sp.add_argument("action", choices=["ls", "add", "rm"])
    sp.add_argument("item", nargs="?")
    sp.add_argument("--from", dest="from_profile", help="add: 从该 profile 收编")
    sp.set_defaults(func=cmd_shared)

    sp = sub.add_parser("unlink", help="把共享项复制为独立副本")
    sp.add_argument("name")
    sp.add_argument("item")
    sp.set_defaults(func=cmd_unlink)

    sp = sub.add_parser("diff", help="比较两 profile 的非共享配置")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("token", help="打印 access token(需 --yes)")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_token)

    sp = sub.add_parser("completion", help="输出补全脚本")
    sp.add_argument("shell", nargs="?", default="bash", choices=["bash"])
    sp.set_defaults(func=cmd_completion)

    sp = sub.add_parser("_complete-names")
    sp.set_defaults(func=cmd_complete_names)

    sp = sub.add_parser("best", help="输出当前最宽裕的 profile")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_best)

    sp = sub.add_parser("statusline", help="给 Claude Code statusline 的单行(离线快速)")
    sp.set_defaults(func=cmd_statusline)

    sp = sub.add_parser("daemon", help="后台定时采样用量")
    sp.add_argument("action", choices=["start", "stop", "status", "_run"])
    sp.add_argument("--interval", type=int, default=300)
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("ls", aliases=["list"], help="列出全部 profile")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("add", help="新建或纳管一个 profile(不填名字则自动编号 aN)")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--note")
    sp.add_argument("--import", dest="import_dir", metavar="DIR",
                    help="纳管已有目录(默认原地,不移动)")
    sp.add_argument("--move", action="store_true",
                    help="配合 --import: 搬入 accounts_root,原位留 symlink")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("rm", help="删除 profile(先备份,含凭证)")
    sp.add_argument("name")
    sp.add_argument("--keep-data", action="store_true", help="只摘注册,不删目录")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("rename", help="改名")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("login", help="引导登录(启动 claude 走 /login)")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("logout", help="删除凭证(默认不留副本)")
    sp.add_argument("name")
    sp.add_argument("--keep-backup", action="store_true")
    sp.set_defaults(func=cmd_logout)

    sp = sub.add_parser("show", help="profile 详情")
    sp.add_argument("name")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("backup", help="备份 profile(默认不含凭证)")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--with-credentials", action="store_true")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="从备份恢复(安全解包)")
    sp.add_argument("archive")
    sp.add_argument("--into", help="恢复为新名字")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("export", help="导出单个 profile(含凭证)供跨机器搬运")
    sp.add_argument("name")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="导入 export 产物")
    sp.add_argument("file")
    sp.add_argument("--into")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("refresh", help="刷新过期 access token(有活跃进程时拒绝)")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--force", action="store_true",
                    help="越过活跃进程保护(CAS 仍生效)")
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("cost", help="本地 token 统计与等价金额(非账单)")
    sp.add_argument("--by", choices=["profile", "model", "project", "day"],
                    default="profile")
    sp.add_argument("--since", metavar="7d", help="只统计最近 N 天")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_cost)

    sp = sub.add_parser("run", help="用指定 profile 启动 claude")
    sp.add_argument("name")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_run)
    return p, sub


def main(argv=None):
    p, _sub = build_parser()
    args = p.parse_args(argv)
    if args.func is None:
        p.print_help()
        return 0
    rc, _ = _dispatch_guard(lambda: args.func(args))
    return rc
