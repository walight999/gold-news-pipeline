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

from datetime import date, timedelta
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


def collapse_minutes(rows: list[dict[str, Any]], minute_dir=None,
                     gated_call=None) -> list[dict[str, Any]]:
    """Grade releases that print in the SAME minute as one call.

    The tape answers the minute, not each line: on 2026-09-30 ADP/Core PCE/GDP
    carried opposite calls and the per-line grading had to score some of them
    wrong whatever gold did. A group of ≥2 calendar rows sharing a release
    minute becomes ONE row graded on what the card SHOWED: `minute_dir(group)`
    = the gated 🎯 gold call ("bull"/"bear"/None); without one, the common
    direction of the individual pills if they all agree, else neutral (the
    card made no call → not graded). Speech rows pass through unchanged.

    `gated_call(group)` → ("bull"|"bear", window) | None is the 🎯 call with
    the horizon it was made for (release_stats.minute_gated_xau_call). It is
    checked for EVERY minute, singletons included, and wins: that row is graded
    on `window` (e.g. "5m") instead of the scorecard default and tagged
    call_kind="gated". Everything else is call_kind="rule" (the hand-rule pill)
    on the default window — so the two are measured on their own horizons."""
    groups: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("routed_as") or "") != "calendar_post":
            out.append(r)
            continue
        groups.setdefault(str(r.get("first_seen_ts") or "")[:16], []).append(r)
    for _, g in sorted(groups.items()):
        gc = gated_call(g) if gated_call else None
        if gc and gc[0] in ("bull", "bear"):
            rep = dict(g[0])
            rep["predicted_dir"], rep["grade_window"] = gc
            rep["call_kind"] = "gated"
            if len(g) > 1:
                rep["title"] = " + ".join(str(x.get("title") or "") for x in g)[:300]
            out.append(rep)
            continue
        if len(g) == 1:
            out.append(g[0])
            continue
        d = minute_dir(g) if minute_dir else None
        if d not in ("bull", "bear"):
            # No 🎯 call on the card: grade only if every directional pill in
            # the minute agreed; conflicting pills = the card made no call.
            votes = {x.get("predicted_dir") for x in g if x.get("predicted_dir") in ("bull", "bear")}
            d = votes.pop() if len(votes) == 1 else "neutral"
        rep = dict(g[0])
        rep["predicted_dir"] = d
        rep["title"] = " + ".join(str(x.get("title") or "") for x in g)[:300]
        out.append(rep)
    return out


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


def gradeability(rows: list[dict[str, Any]], flat_pct: float = DEFAULT_FLAT_PCT,
                 windows: tuple[str, ...] = ("5m", "15m", "30m", "60m")) -> dict[str, dict[str, int]]:
    """Diagnostic: for each return window, how many directional calls are
    gradeable (|move| >= flat band → correct/wrong) vs flat (excluded) vs
    pending (no price yet). Logged every scorecard run so the flat-band
    EXCLUSION RATE is a measured fact — the input for any future band/window
    change — instead of a guess. Pure, no I/O."""
    out: dict[str, dict[str, int]] = {}
    for w in windows:
        col = f"xau_return_{w}"
        graded = flat = pending = 0
        for r in rows:
            if (r.get("predicted_dir") or "").strip() not in ("bull", "bear"):
                continue
            v = _to_float(r.get(col))
            if v is None:
                pending += 1
            elif abs(v) >= flat_pct:
                graded += 1
            else:
                flat += 1
        out[w] = {"graded": graded, "flat": flat, "pending": pending}
    return out


def build_scorecard(rows: list[dict[str, Any]], flat_pct: float = DEFAULT_FLAT_PCT,
                    window: str = "15m") -> dict[str, Any]:
    """Aggregate today's gradeable calibration rows into a scoreboard.

    `window` selects which `xau_return_<window>` column drives grading (5m/15m/
    30m/60m). Default 15m. A longer window develops larger moves so fewer calls
    fall in the flat band — see `gradeability` for the measured trade-off.
    A row carrying `grade_window` (🎯 gated calls) is graded on that instead.

    `rows` should already be filtered to the target day. Each row is expected to
    carry `predicted_dir`, `xau_return_<window>`, and (ideally) `xau_base_price`
    + `title`/`country`/`predicted_verdict_th` for the miss list.

    Returns a dict with both the headline aggregate and a `misses` list (the
    wrong calls, largest $-move first). `n_pending` counts directional calls
    whose 15-min price isn't available yet (off-hours / still settling) — shown
    so a small scoreboard isn't mistaken for "nothing happened"."""
    n_correct = n_wrong = n_flat = n_pending = 0
    sum_up_usd = 0.0
    sum_down_usd = 0.0
    misses: list[dict[str, Any]] = []

    n_gated_correct = n_gated_wrong = 0
    for r in rows:
        pdir = (r.get("predicted_dir") or "").strip()
        if pdir not in ("bull", "bear", "neutral"):
            continue
        # A 🎯 gated call is graded on the horizon it was made for (collapse_minutes).
        gated = r.get("call_kind") == "gated"
        r15 = _to_float(r.get(f"xau_return_{r.get('grade_window') or window}"))
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
            n_gated_correct += gated
        elif g == "wrong":
            n_wrong += 1
            n_gated_wrong += gated
            misses.append({
                "title": r.get("title") or r.get("topic_bucket") or "(event)",
                "country": r.get("country") or "",
                "predicted_dir": pdir,
                "predicted_verdict_th": r.get("predicted_verdict_th") or "",
                "r15_pct": r15,
                "usd_move": usd_move,
                "window": r.get("grade_window") or window,
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
        # 🎯 gated subset (graded at its own horizon); rule = total − gated.
        "n_gated_correct": n_gated_correct,
        "n_gated_graded": n_gated_correct + n_gated_wrong,
        "sum_up_usd": round(sum_up_usd, 2),
        "sum_down_usd": round(sum_down_usd, 2),
        "misses": misses,
    }


def _window_rows(scorecard_rows: list[dict[str, Any]], days: int,
                 as_of: date | None) -> list[dict[str, Any]]:
    """scorecard_daily rows whose date_ict falls in the `days` CALENDAR days
    ending at `as_of` (inclusive); `as_of` None → anchor on the newest row.

    Calendar days, not "the last N rows that graded something": a quiet stretch
    must age old calls out, not freeze them (2026-10: the 1:1 card read "7 วัน"
    while summing calls from 3+ weeks back)."""
    dated = []
    for r in scorecard_rows:
        try:
            d = date.fromisoformat(str(r.get("date_ict") or "")[:10])
        except ValueError:
            continue
        dated.append((d, r))
    if not dated:
        return []
    anchor = as_of or max(d for d, _ in dated)
    lo = anchor - timedelta(days=days - 1)
    return [r for d, r in dated if lo <= d <= anchor]


def rolling_accuracy(scorecard_rows: list[dict[str, Any]], days: int = 7,
                     as_of: date | None = None) -> float | None:
    """Percent form of rolling_accuracy_detail (0..100). None on no graded calls."""
    d = rolling_accuracy_detail(scorecard_rows, days=days, as_of=as_of)
    return d["accuracy"] * 100.0 if d else None


# Public gate defaults (overridable via schedule.yaml::scorecard). The free bot
# advertises this number as a credibility line — a hit-rate on a handful of
# calls, or one under a coin flip, does the opposite. Below the gate nothing is
# published; the worker's cached line ages out on its own (KV TTL ~10d).
DEFAULT_PUBLIC_MIN_CALLS = 30
DEFAULT_PUBLIC_MIN_ACCURACY = 0.55


def public_accuracy_ok(detail: dict[str, Any] | None,
                       min_calls: int = DEFAULT_PUBLIC_MIN_CALLS,
                       min_accuracy: float = DEFAULT_PUBLIC_MIN_ACCURACY) -> tuple[bool, str]:
    """(publishable, reason): a big-enough sample AND a rate worth advertising."""
    if not detail:
        return False, "no graded calls in window"
    if detail["total"] < min_calls:
        return False, f"sample {detail['total']} < {min_calls} calls"
    if detail["accuracy"] < min_accuracy:
        return False, f"accuracy {detail['accuracy']:.0%} < {min_accuracy:.0%}"
    return True, "ok"


def rolling_accuracy_detail(scorecard_rows: list[dict[str, Any]], days: int = 30,
                            as_of: date | None = None,
                            gated_only: bool = False) -> dict[str, Any] | None:
    """Call-weighted accuracy over the last `days` calendar days:
    {accuracy: 0..1, correct, total, days}. None when nothing was graded.
    `gated_only` counts just the 🎯 gated calls (n_gated_* columns; days
    before those columns existed contribute nothing)."""
    window = _window_rows(scorecard_rows, days, as_of)
    c_col, g_col = ("n_gated_correct", "n_gated_graded") if gated_only else ("n_correct", "n_graded")
    tot_correct = sum(int(_to_float(r.get(c_col)) or 0) for r in window)
    tot_graded = sum(int(_to_float(r.get(g_col)) or 0) for r in window)
    if not tot_graded:
        return None
    return {
        "accuracy": round(tot_correct / tot_graded, 4),
        "correct": tot_correct,
        "total": tot_graded,
        "days": days,
    }
