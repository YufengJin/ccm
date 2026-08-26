def table(headers, rows):
    """纯文本等宽表格:左对齐,列间两空格。"""
    cols = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    lines = []
    for r in cols:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)
