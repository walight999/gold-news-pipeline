"""Learned release-reaction history for the calendar cards.

Reads config/release_stats.json (produced offline by research/learn.py and
refreshed weekly) and turns it into one honest Thai line per card:

    ย้อนหลัง 2019–2026 (n=26): ทองไปตามทิศนี้ 85% ใน 5 นาที · 73% ใน 15 นาที

"Follows the arrow" = moved the way the card's XAU pill points, counted only
when the surprise was meaningful (|z| ≥ 0.5) and gold moved ≥ the flat band;
tiny moves are neither right nor wrong (same convention as the scorecard).
Descriptive history, not a promise — the walk-forward test in the research
report is the out-of-sample check. Nothing is shown for series with too
little history (MIN_N), so a 3-for-3 streak never renders as "100%".

Best-effort: a missing / unreadable JSON just means no line on the card.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger(__name__)

STATS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "release_stats.json")
MIN_N = 12
UNCLEAR_HIT = 0.55          # below this at both 5 and 15 min → "direction unclear"


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.warning("release_stats unavailable: %s", e)
        return {}


def _span_label(d: dict) -> str:
    span = d.get("generated_from") or []
    if len(span) == 2:
        return f"{span[0][:4]}–{span[1][:4]}"
    return ""


def series_cells(country: str, title: str, asset: str = "XAU") -> dict[int, dict]:
    """{horizon_min: {"n", "hit", ...}} for cells with at least MIN_N samples.
    Non-USD series live under "pair_series" (their own USD pair)."""
    d = _load()
    key = f"{(country or '').upper()}|{title}"
    s = (d.get("series") or {}).get(key) or (d.get("pair_series") or {}).get(key)
    if not s:
        return {}
    out = {}
    for h in (5, 15, 60):
        c = (s.get("cells") or {}).get(f"{asset}|{h}")
        if c and int(c.get("n", 0)) >= MIN_N:
            out[h] = c
    return out


PAIR_FOR = {"EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY"}


def history_line_th(country: str, title: str, asset: str | None = None,
                    spot: float | None = None) -> str | None:
    """One Thai line of reaction history, or None when there's not enough.
    USD releases → gold; EUR/GBP/JPY releases → their USD pair (gold has no
    reliable reaction to them — see calendar.NON_USD_GOLD_RATIONALE)."""
    ccy = (country or "").upper()
    if asset is None:
        asset = "XAU" if ccy == "USD" else PAIR_FOR.get(ccy, "")
    if not asset:
        return None
    cells = series_cells(country, title, asset)
    if not cells:
        return None
    shown = {h: c for h, c in cells.items() if h in (5, 15)}
    if not shown:
        return None
    parts = [f"{round(c['hit'] * 100)}% ใน {h} นาที" for h, c in sorted(shown.items())]
    n = max(int(c["n"]) for c in shown.values())
    span = _span_label(_load())
    head = f"ย้อนหลัง {span} " if span else "ย้อนหลัง "
    # Thai "ทอง" joins the next word directly; a ticker (EURUSD) needs a space.
    name = "ทอง" if asset == "XAU" else f"{asset} "
    if max(float(c["hit"]) for c in shown.values()) < UNCLEAR_HIT:
        # History doesn't back the pill's direction (e.g. AHE, which prints in
        # the same minute as NFP): say so instead of "ไปตามทิศนี้ 47%".
        line = f"{head}(n={n}): ทิศ{name}หลังข่าวนี้ไม่ชัด (" + " · ".join(parts) + ")"
    else:
        line = f"{head}(n={n}): {name}ไปตามทิศนี้ " + " · ".join(parts)
    rng = typical_range_usd(cells.get(15), spot)
    if rng and asset == "XAU":
        line += f" · ปกติขยับ ${rng[0]:,.0f}–${rng[1]:,.0f} ใน 15 นาที"
    return line


def typical_range_usd(cell: dict | None, spot: float | None = None) -> tuple[float, float] | None:
    """Typical 15-min move of this release as a $ range at TODAY's price:
    median and 80th percentile of |move| (research/learn.py, % of price)
    × current spot. Scales with the gold price level by construction."""
    if not cell or cell.get("abs_p50_pct") is None or cell.get("abs_p80_pct") is None:
        return None
    if spot is None:
        spot = _spot_cached()
    if not spot:
        return None
    lo = float(cell["abs_p50_pct"]) / 100 * spot
    hi = float(cell["abs_p80_pct"]) / 100 * spot
    if hi < 1:
        return None
    return lo, hi


@lru_cache(maxsize=1)
def _spot_cached() -> float | None:
    try:
        from .spot_feed import current_spot
        return current_spot()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- live composite call

_ASSET_TH = {"XAU": "ทอง", "USDJPY": "USDJPY", "EURUSD": "EURUSD",
             "GBPUSD": "GBPUSD", "US500": "S&P500"}


def minute_calls(prints: list[tuple[str, str, str, str]]) -> list[dict]:
    """Gated calls for one release minute.

    `prints` = [(country, title, actual, forecast), ...] for everything that
    printed together. Mirrors research.learn.composite(): robust-z surprise
    × hawkish rule sign × learned series weight, summed; a call is returned
    only where that (asset, horizon) has a confidence gate AND the composite
    clears its threshold. Each call carries the gate's OUT-OF-SAMPLE precision
    from the walk-forward (not the training fit), so the card quotes what the
    method actually scored on data it had not seen."""
    d = _load()
    model = d.get("model") or {}
    gates = model.get("gates") or {}
    if not gates:
        return []
    from .calendar import CalEvent, _GOLD_IMPACT_RULES, is_statement_driven
    from .fred import parse_forecast_value
    from datetime import datetime
    comps = []
    for country, title, actual, forecast in prints:
        if (country or "").upper() != "USD":
            continue
        key = f"USD|{title}"
        sc = (model.get("scales") or {}).get(key)
        a, f = parse_forecast_value(actual or ""), parse_forecast_value(forecast or "")
        if not sc or a is None or f is None or a == f:
            continue
        ev = CalEvent("x", title, "USD", "High", "", "", datetime(2024, 1, 1))
        if is_statement_driven(ev):
            continue
        sign = None
        for pat, inv, _ in _GOLD_IMPACT_RULES:
            if pat.search(title.lower()):
                sign = -1 if inv else 1
                break
        if sign is None:
            continue
        comps.append((key, sign * (a - f) / sc))
    calls = []
    for cell, gate in gates.items():
        if not gate:
            continue
        asset, h = cell.split("|")
        c = sum(z * float((model.get("weights") or {}).get(f"{k}@{cell}", 0.0)) for k, z in comps)
        if c == 0 or abs(c) < float(gate["threshold"]):
            continue
        direction = (1 if c > 0 else -1) * int((model.get("asset_dir") or {}).get(cell, 0))
        oos = ((d.get("oos") or {}).get(cell) or {}).get("gated") or {}
        n = int(oos.get("right", 0)) + int(oos.get("wrong", 0))
        if not direction or not oos.get("precision") or n < MIN_N:
            continue
        calls.append({"asset": asset, "h": int(h), "dir": direction,
                      "oos_precision": float(oos["precision"]), "oos_n": n})
    calls.sort(key=lambda x: (x["asset"] != "XAU", x["h"]))
    return calls


def minute_call_line_th(prints: list[tuple[str, str, str, str]]) -> str | None:
    """"🎯 สัญญาณรวมผ่านเกณฑ์: ทอง ↓ 5 นาที (แม่นย้อนหลัง 89%, n=85) · …"
    listing XAU first, then at most two other assets. None = no gated call."""
    calls = minute_calls(prints)
    if not calls:
        return None
    seen, parts = set(), []
    for c in calls:
        if c["asset"] in seen:
            continue                       # one horizon per asset: the shortest
        seen.add(c["asset"])
        arrow = "↑" if c["dir"] > 0 else "↓"
        parts.append(f"{_ASSET_TH.get(c['asset'], c['asset'])} {arrow} {c['h']} นาที "
                     f"(แม่นย้อนหลัง {round(c['oos_precision'] * 100)}%, n={c['oos_n']})")
        if len(parts) == 3:
            break
    return "🎯 สัญญาณรวมผ่านเกณฑ์: " + " · ".join(parts)
