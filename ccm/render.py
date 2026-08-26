"""终端渲染:CJK 宽度对齐、ANSI 配色、进度条、账号用量卡片。

零依赖;非 TTY 或设置了 NO_COLOR 时自动降级为纯文本。
"""
import os
import re
import sys
import unicodedata

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

GREEN, YELLOW, RED, DIM, BOLD, CYAN = "32", "33", "31", "2", "1", "36"


def color_enabled(explicit=None):
    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def colorize(s, code, enable):
    return f"\x1b[{code}m{s}\x1b[0m" if enable else s


def visual_width(s):
    """显示宽度:CJK 全角算 2,ANSI 序列算 0。"""
    s = _ANSI_RE.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in s)


def pad(s, width):
    return s + " " * max(0, width - visual_width(s))


def table(headers, rows):
    """纯文本等宽表格:左对齐,列间两空格,CJK 宽度正确。"""
    cols = [list(headers)] + [[str(c) for c in r] for r in rows]
    widths = [max(visual_width(r[i]) for r in cols) for i in range(len(headers))]
    return "\n".join(
        "  ".join(pad(c, w) for c, w in zip(r, widths)).rstrip() for r in cols)


def _pct_color(pct):
    return RED if pct >= 80 else (YELLOW if pct >= 50 else GREEN)


def bar(pct, width=20, color=False):
    """▓░ 进度条;≥80% 红 / ≥50% 黄 / 其余绿。pct 越界自动钳制。"""
    p = max(0, min(100, int(pct)))
    filled = round(p / 100 * width)
    body = "▓" * filled + "░" * (width - filled)
    return colorize(body, _pct_color(p), color) if color else body


def _usage_line(label, pct, resets, color):
    if pct is None:
        return f"  {label} {'░' * 20}   -"
    c = _pct_color(pct)
    pct_s = colorize(f"{pct:>3}%", c + (";1" if pct >= 80 else ""), color)
    tail = f"  {resets} 后重置" if resets else ""
    warn = " ⚠" if pct >= 80 else ""
    return f"  {label} {bar(pct, color=color)}  {pct_s}{tail}{warn}"


def render_usage(rows, active_uuid=None, color=None):
    """账号用量卡片(供 ccm usage / --watch)。活跃账号排最前并标 ●。"""
    color = color_enabled(color)
    rows = sorted(rows, key=lambda r: (r.account_uuid != active_uuid,
                                       r.email or r.account_uuid))
    out = []
    for r in rows:
        mark = "●" if r.account_uuid == active_uuid else " "
        head = f"{mark} {r.email or r.account_uuid}"
        head = colorize(head, BOLD, color) if r.account_uuid == active_uuid else head
        profs = colorize("(" + "+".join(r.profiles) + ")", DIM, color)
        if r.source == "live":
            src = colorize("实时", CYAN, color)
        elif r.source == "cache":
            age = (r.cache_age_s or 0) // 3600
            src = colorize(f"缓存·{age}h前", YELLOW, color)
        else:
            src = colorize("不可用", DIM, color)
        out.append(f"{head} {profs}  [{src}]")
        out.append(_usage_line("5h", r.five_hour_pct, r.five_hour_resets, color))
        out.append(_usage_line("7d", r.seven_day_pct, r.seven_day_resets, color))
        if r.source == "unavailable":
            hint = f"  ↳ token 过期且无缓存;试试 ccm refresh {r.profiles[0]}" \
                if r.profiles else "  ↳ 无凭证"
            out.append(colorize(hint, DIM, color))
        out.append("")
    return "\n".join(out).rstrip("\n")
