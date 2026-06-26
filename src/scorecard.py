"""Daily directional-accuracy scorecard for calendar releases.

For every released economic event we publish a verdict — 🟢 Bullish / 🔴 Bearish
/ ⚪ Neutral *for gold*. This module grades that verdict against the ACTUAL
15-minute XAU move recorded in `calibration_log`, then aggregates a daily
scoreboard: how many DIRECTIONAL calls were right vs wrong, the $ gold moved up
vs down in those 15-min windows, and which calls missed.

Phase 1 scope = calendar releases only. A `calibration_log` row is gradeable
here only when it carries a `predicted_dir` (written at calendar-release send
time) AND a numeric `xau_return_15m` (filled by the daily backfill). RSS/news
rows have no `predicted_dir`, so they're naturally excluded.

Pure functions, no I/O. `main.run_scorecard` wires this to the Store + LINE 1:1.

Grading model (honest by construction):
  - Only bull/bear predictions enter the accuracy denominator. Neutral calls and
    direction-calls that barely moved (|move| < flat band) are bucketed as
    ⚪ "ไม่ชัด" and EXCLUDED from accuracy — we don't reward or punish a flat tape.
  - accuracy = correct / (correct + wrong), where `wrong` is only an
    opposite-direction move on a bull/bear call.
"""
from __future__ import annotations

from typing import Any

# A move smaller than this (in %) within 15 min is treated as "flat" — the
# tape didn't really pick a side, so a directional call there is neither a
# clean hit nor a clean miss. 0.10% of ~$2,600 gold ≈ $2.6.
DEFAULT_FLAT_PCT = 0.10


def verdict_to_dir(verdict: str | None) -> str:
    """Map the published verdict string to a direction token.

    Verdict text comes from `fred.reconcile_with_impact`, e.g.
    "🟢 Bullish gold", "🔴 Bearish gold", "⚪ Neutral — print matched forecast".
    Returns "bull" | "bear" | "neutral" | "" (empty = no directional call)."""
    if not verdict:
        return ""
    v = verdict.lower()
    if "🟢" in verdict or "bullish" in v:
        return "bull"
    if "🔴" in verdict or "bearish" in v:
        return "bear"
    if "⚪" in verdict or "neutral" in v:
        return "neutral"
    return ""


def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Guard against NaN/inf leaking from a bad cell.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def grade(predicted_dir: str, r15_pct: float, flat_pct: float = DEFAULT_FLAT_PCT) -> str:
    """Grade one directional call against the actual 15-min %-move.

    Returns one of:
      "correct" — bull/bear call that matched the actual move (≥ flat band)
      "wrong"   — bull/bear call, actual moved the OPPOSITE way (≥ flat band)
      "flat"    — actual move within the flat band (a bull/bear call that
                  barely moved), OR a neutral call (with any outcome). Excluded
                  from accuracy.
    """
    if predicted_dir not in ("bull", "bear"):
        return "flat"   # neutral / no-call → never counts for or against us
    if r15_pct >= flat_pct:
        actual = "bull"
    elif r15_pct <= -flat_pct:
        actual = "bear"
    else:
        return "flat"
    return "correct" if actual == predicted_dir else "wrong"


def build_scorecard(rows: list[dict[str, Any]], flat_pct: float = DEFAULT_FLAT_PCT) -> dict[str, Any]:
    """Aggregate today's gradeable calibration rows into a scoreboard.

    `rows` should already be filtered to the target day. Each row is expected to
    carry `predicted_dir`, `xau_return_15m`, and (ideally) `xau_base_price` +
    `title`/`country`/`predicted_verdict_th` for the miss list.

    Returns a dict with both the headline aggregate and a `misses` list (the
    wrong calls, largest $-move first). `n_pending` counts directional calls
    whose 15-min price isn't available yet (off-hours / still settling) — shown
    so a small scoreboard isn't mistaken for "nothing happened"."""
    n_correct = n_wrong = n_flat = n_pending = 0
    sum_up_usd = 0.0
    sum_down_usd = 0.0
    misses: list[dict[str, Any]] = []

    for r in rows:
        pdir = (r.get("predicted_dir") or "").strip()
        if pdir not in ("bull", "bear", "neutral"):
            continue
        r15 = _to_float(r.get("xau_return_15m"))
        if r15 is None:
            # Directional call we made but can't grade yet (no price bar).
            if pdir in ("bull", "bear"):
                n_pending += 1
            continue
        base = _to_float(r.get("xau_base_price"))
        usd_move = (r15 / 100.0 * base) if base else None
        if usd_move is not None:
            if usd_move > 0:
                sum_up_usd += usd_move
            elif usd_move < 0:
                sum_down_usd += usd_move

        g = grade(pdir, r15, flat_pct)
        if g == "correct":
            n_correct += 1
        elif g == "wrong":
            n_wrong += 1
            misses.append({
                "title": r.get("title") or r.get("topic_bucket") or "(event)",
                "country": r.get("country") or "",
                "predicted_dir": pdir,
                "predicted_verdict_th": r.get("predicted_verdict_th") or "",
                "r15_pct": r15,
                "usd_move": usd_move,
            })
        else:
            n_flat += 1

    n_graded = n_correct + n_wrong
    accuracy_pct = (n_correct / n_graded * 100.0) if n_graded else 0.0
    # Largest $-move miss first; rows without a $ value sort last.
    misses.sort(key=lambda m: abs(m["usd_move"]) if m["usd_move"] is not None else -1.0,
                reverse=True)
    return {
        "n_correct": n_correct,
        "n_wrong": n_wrong,
        "n_flat": n_flat,
        "n_pending": n_pending,
        "n_graded": n_graded,
        "accuracy_pct": accuracy_pct,
        "sum_up_usd": round(sum_up_usd, 2),
        "sum_down_usd": round(sum_down_usd, 2),
        "misses": misses,
    }


def rolling_accuracy(scorecard_rows: list[dict[str, Any]], days: int = 7) -> float | None:
    """Average directional accuracy over the most recent `days` scorecard_daily
    rows that actually graded at least one call. None when there's no history."""
    usable = [r for r in scorecard_rows if _to_float(r.get("n_graded"))]
    usable.sort(key=lambda r: r.get("date_ict") or "", reverse=True)
    window = usable[:days]
    tot_correct = sum(int(_to_float(r.get("n_correct")) or 0) for r in window)
    tot_graded = sum(int(_to_float(r.get("n_graded")) or 0) for r in window)
    if not tot_graded:
        return None
    return tot_correct / tot_graded * 100.0
