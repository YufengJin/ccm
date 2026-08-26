"""模型价格表(美元/百万 token)与等价折算。

重要口径:Max 订阅不按 API 计费,算出的是「若按 API 价目表折算的等价金额」,
用于横向比较,不是账单(spec §9)。可用 ~/.ccm/pricing.json 覆盖。
"""
from ccm.config import load_json
from ccm.errors import CcmError

BUILTIN = {
    "updated": "2026-08-26",
    "prices": {
        "claude-fable-5": {"in": 10.0, "out": 50.0},
        "claude-mythos-5": {"in": 10.0, "out": 50.0},
        "claude-opus-5": {"in": 5.0, "out": 25.0},
        "claude-opus-4-8": {"in": 5.0, "out": 25.0},
        "claude-opus-4-7": {"in": 5.0, "out": 25.0},
        "claude-opus-4-6": {"in": 5.0, "out": 25.0},
        "claude-sonnet-5": {"in": 2.0, "out": 10.0},
        "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
        "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
    },
}
CACHE_READ_X = 0.1
CACHE_5M_X = 1.25
CACHE_1H_X = 2.0

_table = None


def load_table(env=None):
    global _table
    if env is not None:
        try:
            override = load_json(env.ccm_home / "pricing.json")
        except CcmError:
            override = None
        if override and override.get("prices"):
            return override
    if _table is None:
        _table = BUILTIN
    return _table


def resolve_price(model, table=None):
    """最长前缀匹配;未命中 → None(计入 unknown 桶,金额记 0)。"""
    table = table or load_table()
    best = None
    for prefix, price in table["prices"].items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best else None


def price_event(model, in_tok, out_tok, cache_read, cache_5m, cache_1h, table=None):
    """单条事件的等价金额;模型未知 → None。单价是 $/1M token,必须 /1e6。"""
    p = resolve_price(model, table)
    if p is None:
        return None
    return (in_tok * p["in"]
            + out_tok * p["out"]
            + cache_read * p["in"] * CACHE_READ_X
            + cache_5m * p["in"] * CACHE_5M_X
            + cache_1h * p["in"] * CACHE_1H_X) / 1_000_000
