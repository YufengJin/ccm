"""git 风格上下文补全引擎:子命令 → 该命令的 flags → 按位补位置参数。

bash 侧只做一件事:把 COMP_WORDS 原样交给 `ccm _complete`,回显候选。
单一真相来源是 argparse parser(flags 自动同步)+ 下方位置参数表。
"""
import argparse

# 每个命令的位置参数语义;"profile"→id/自定义名/email,列表→固定选项,None→不补
POS_SPEC = {
    "switch": ["profile"], "run": ["profile"], "shell": ["profile"],
    "show": ["profile"], "rm": ["profile"], "rename": ["profile", None],
    "login": ["profile"], "logout": ["profile"], "refresh": ["profile"],
    "token": ["profile"], "backup": ["profile"], "export": ["profile", None],
    "link": ["profile"], "unlink": ["profile", "shared_item"],
    "diff": ["profile", "profile"],
    "shared": [["ls", "add", "rm"], "shared_item"],
    "daemon": [["start", "stop", "status"]],
    "cost": [], "usage": [], "ls": [], "doctor": [], "best": [],
    "statusline": [], "env": [], "current": [], "migrate": [],
    "add": [None], "restore": [None], "import": [None],
    "init": [["bash"]], "completion": [["bash"]],
    "help": ["command"],
}
HIDDEN = {"_complete", "_complete-names", "use"}   # use 是 switch 的别名,不重复展示


def _visible_commands(sub):
    seen, out = set(), []
    for name, sp in sub.choices.items():
        if name in HIDDEN or id(sp) in seen:
            continue
        seen.add(id(sp))
        out.append(name)
    return sorted(out)


def _flags_of(sub, cmd):
    sp = sub.choices.get(cmd)
    if sp is None:
        return []
    out = []
    for a in sp._actions:
        if a.help == argparse.SUPPRESS:
            continue
        out.extend(o for o in a.option_strings)
    return sorted(set(out))


def _profiles_and_emails(registry):
    words = sorted(registry.profiles)
    words += sorted({p.email for p in registry.profiles.values() if p.email})
    return words


def complete_words(env, registry, cword, words):
    """words[0]='ccm';返回按 words[cword] 前缀过滤并排序的候选。"""
    from ccm.cli import build_parser
    _p, sub = build_parser()
    cur = words[cword] if cword < len(words) else ""

    def done(cands):
        return sorted({c for c in cands if c.startswith(cur)})

    if cword <= 1:
        return done(_visible_commands(sub))
    cmd = words[1]
    if cmd == "use":
        cmd = "switch"
    if cur.startswith("-"):
        return done(_flags_of(sub, words[1]))
    spec = POS_SPEC.get(cmd)
    if spec is None:
        return []
    # 位置序号 = 命令之后、光标之前的非 flag 词数(近似;flag 取值场景可容忍)
    pos = sum(1 for w in words[2:cword] if not w.startswith("-"))
    if pos >= len(spec):
        return []
    kind = spec[pos]
    if kind == "profile":
        return done(_profiles_and_emails(registry))
    if kind == "shared_item":
        return done(registry.shared)
    if kind == "command":
        return done(_visible_commands(sub))
    if isinstance(kind, list):
        return done(kind)
    return []
