"""Weekly performance / tuning dashboard (measure-first companion).

`content_review` already handles CONTENT feedback (typos/tone/missed rejects).
This module is the PERFORMANCE + TUNING side: it rolls the last 7 days of the
durable tabs into one structured report and — the actionable part — emits
EVIDENCE-CITED tuning candidates so quality changes are driven by data on a
weekly cadence instead of a hunch.

Pure functions, no I/O. `main.run_weekly_report` wires this to the Store, the
`weekly_report` tab, the run log, and a 1:1 LINE card.

Data sources (all durable enough for a 7-day window):
  - delivery_daily   → volume sent/failed + per-route split
  - scorecard_daily  → directional-accuracy trend
  - calibration_log  → routing precision per topic·route (the base_impact input)
                       + flat-band gradeability at each return window
  - source_state     → classifier token cost (month) + Apify entry counts +
                       per-source staleness/error snapshot
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

# A 15-min move at/above this (%) counts as a real directional move — same
# threshold precision_report uses, so the two agree.
HIT_PCT = 0.15


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------- delivery
def summarize_delivery(delivery_rows: list[dict[str, Any]], since_date_ict: str) -> dict[str, Any]:
    """7-day sent/failed totals + per-route split from the delivery_daily tab
    (`by_route` is a per-day JSON blob of route→attempts)."""
    n_sent = n_failed = 0
    by_route: dict[str, int] = {}
    days = 0
    for r in delivery_rows:
        d = str(r.get("date_ict") or "")
        if d < since_date_ict:
            continue
        days += 1
        n_sent += int(_f(r.get("n_sent")) or 0)
        n_failed += int(_f(r.get("n_failed")) or 0)
        try:
            for route, n in (json.loads(r.get("by_route") or "{}") or {}).items():
                by_route[route] = by_route.get(route, 0) + int(n)
        except (ValueError, TypeError):
            pass
    return {"days": days, "n_sent": n_sent, "n_failed": n_failed,
            "fail_rate": round(n_failed / (n_sent + n_failed), 3) if (n_sent + n_failed) else 0.0,
            "by_route": dict(sorted(by_route.items(), key=lambda kv: -kv[1]))}


# --------------------------------------------------------------- routing precision
def routing_precision(calibration_rows: list[dict[str, Any]], cutoff_utc: datetime) -> list[dict[str, Any]]:
    """Per (topic, route) over the window: n, hit% (|15m move|>=0.15%), avg|move|.
    This is the exact signal that drove the 2026-09-10 base_impact tune."""
    groups: dict[tuple[str, str], list[float]] = {}
    for r in calibration_rows:
        ts = _parse(r.get("first_seen_ts"))
        if ts is None or ts < cutoff_utc:
            continue
        mv = _f(r.get("xau_return_15m"))
        if mv is None:
            continue
        key = (r.get("topic_bucket") or "?", r.get("routed_as") or "?")
        groups.setdefault(key, []).append(mv)
    out = []
    for (topic, route), moves in groups.items():
        n = len(moves)
        hits = sum(1 for m in moves if abs(m) >= HIT_PCT)
        out.append({
            "topic": topic, "route": route, "n": n,
            "hit_pct": round(hits / n * 100, 1) if n else 0.0,
            "avg_abs_move": round(sum(abs(m) for m in moves) / n, 3) if n else 0.0,
        })
    return sorted(out, key=lambda d: -d["n"])


# --------------------------------------------------------------- scorecard trend
def scorecard_trend(scorecard_rows: list[dict[str, Any]], since_date_ict: str) -> dict[str, Any]:
    correct = graded = flat = 0
    for r in scorecard_rows:
        if str(r.get("date_ict") or "") < since_date_ict:
            continue
        correct += int(_f(r.get("n_correct")) or 0)
        graded += int(_f(r.get("n_graded")) or 0)
        flat += int(_f(r.get("n_flat")) or 0)
    return {"correct": correct, "graded": graded, "flat": flat,
            "accuracy_pct": round(correct / graded * 100, 1) if graded else None}


def gradeability(calibration_rows: list[dict[str, Any]], cutoff_utc: datetime,
                 flat_pct: float = 0.10,
                 windows: tuple[str, ...] = ("15m", "30m", "60m")) -> dict[str, dict[str, int]]:
    """Flat-band exclusion at each window over the window — the input for a
    scorecard window/band decision (mirrors scorecard.gradeability but time-windowed)."""
    out: dict[str, dict[str, int]] = {w: {"graded": 0, "flat": 0} for w in windows}
    for r in calibration_rows:
        if (r.get("predicted_dir") or "").strip() not in ("bull", "bear"):
            continue
        ts = _parse(r.get("first_seen_ts"))
        if ts is None or ts < cutoff_utc:
            continue
        for w in windows:
            v = _f(r.get(f"xau_return_{w}"))
            if v is None:
                continue
            if abs(v) >= flat_pct:
                out[w]["graded"] += 1
            else:
                out[w]["flat"] += 1
    return out


# --------------------------------------------------------------- cost + sources
def classifier_cost(classifier_health_row: dict[str, Any] | None) -> dict[str, Any]:
    """Month-to-date classifier token spend + high-quality (Sonnet) split, read
    from the _classifier_health source_state blob."""
    if not classifier_health_row:
        return {}
    try:
        blob = json.loads(classifier_health_row.get("items_last_hour") or "{}")
    except (ValueError, TypeError):
        return {}
    return {
        "month": blob.get("month"),
        "tokens_in": int(blob.get("month_tokens_in", 0) or 0),
        "tokens_out": int(blob.get("month_tokens_out", 0) or 0),
        "hq_tokens_in": int(blob.get("month_hq_tokens_in", 0) or 0),
        "hq_tokens_out": int(blob.get("month_hq_tokens_out", 0) or 0),
    }


def source_snapshot(source_rows: list[dict[str, Any]], now_utc: datetime) -> dict[str, Any]:
    """Apify entry counts (last run) + count of RSS sources currently stale
    (>6h since last success) or erroring, as a point-in-time health snapshot."""
    apify: dict[str, int] = {}
    stale = 0
    for r in source_rows:
        sid = str(r.get("source_id") or "")
        if sid.startswith("_apify"):
            apify[sid] = int(_f(r.get("items_last_hour")) or 0)
            continue
        if sid.startswith("_"):
            continue  # other synthetic rows (_classifier_health, _ff_scraper, etc.)
        last_ok = _parse(r.get("last_success_ts"))
        if last_ok is None or (now_utc - last_ok) > timedelta(hours=6):
            stale += 1
    return {"apify_entries_last_run": apify, "stale_rss_sources": stale}


# --------------------------------------------------------------- tuning candidates
def tuning_candidates(routing: list[dict[str, Any]], scorecard: dict[str, Any],
                      grade: dict[str, dict[str, int]], min_n: int = 20) -> list[str]:
    """Evidence-cited, rule-generated suggestions. Each cites its numbers so a
    change is justified, never a hunch. Empty when nothing crosses a threshold."""
    out: list[str] = []
    # Baseline = avg |move| of the SENT routes (breaking/alert/digest), so we
    # compare archived topics against what actually ships.
    sent = [g for g in routing if g["route"] in ("breaking", "alert", "digest") and g["n"] >= min_n]
    sent_avg = (sum(g["avg_abs_move"] for g in sent) / len(sent)) if sent else 0.0

    # Under-routed: an ARCHIVED topic (big n) moving MORE than the sent average.
    for g in routing:
        if g["route"] == "archive" and g["n"] >= min_n and sent_avg and g["avg_abs_move"] > sent_avg * 1.05:
            out.append(f"↑ '{g['topic']}' looks UNDER-routed: archived n={g['n']}, "
                       f"avg|move|={g['avg_abs_move']:.3f}% > sent-route avg {sent_avg:.3f}%. "
                       f"Consider raising its base_impact.")
    # Over-routed: a SENT topic (big n) with weak move AND low hit vs sent average.
    for g in sent:
        if g["avg_abs_move"] < sent_avg * 0.85 and g["hit_pct"] < 25.0:
            out.append(f"↓ '{g['topic']}·{g['route']}' looks OVER-routed/noisy: n={g['n']}, "
                       f"avg|move|={g['avg_abs_move']:.3f}% (< sent avg {sent_avg:.3f}%), "
                       f"hit {g['hit_pct']}%. Consider lowering its base_impact.")
    # Scorecard band/window: if 15m excludes most calls but 30m recovers a lot.
    g15, g30 = grade.get("15m", {}), grade.get("30m", {})
    tot15 = g15.get("graded", 0) + g15.get("flat", 0)
    if tot15 >= min_n:
        excl15 = g15.get("flat", 0) / tot15
        tot30 = g30.get("graded", 0) + g30.get("flat", 0)
        excl30 = (g30.get("flat", 0) / tot30) if tot30 else 1.0
        if excl15 > 0.5 and excl30 < excl15 - 0.15:
            out.append(f"⏱ scorecard flat-band excludes {excl15*100:.0f}% at 15m but only "
                       f"{excl30*100:.0f}% at 30m — consider window: 30m (more gradeable).")
    # Accuracy sanity: enough graded but accuracy near coin-flip.
    if scorecard.get("graded", 0) >= min_n and (scorecard.get("accuracy_pct") or 0) < 55:
        out.append(f"🎯 directional accuracy {scorecard['accuracy_pct']}% over "
                   f"{scorecard['graded']} graded calls — verdict logic (fred.reconcile) may need review.")
    return out


def build_weekly_report(now_utc: datetime, *, delivery_rows: list[dict[str, Any]],
                        scorecard_rows: list[dict[str, Any]], calibration_rows: list[dict[str, Any]],
                        source_rows: list[dict[str, Any]], classifier_health_row: dict[str, Any] | None,
                        days: int = 7, flat_pct: float = 0.10) -> dict[str, Any]:
    """Assemble the full weekly report dict. `days` = look-back window."""
    cutoff_utc = now_utc - timedelta(days=days)
    since_date_ict = (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")
    routing = routing_precision(calibration_rows, cutoff_utc)
    scard = scorecard_trend(scorecard_rows, since_date_ict)
    grade = gradeability(calibration_rows, cutoff_utc, flat_pct=flat_pct)
    return {
        "window_days": days,
        "delivery": summarize_delivery(delivery_rows, since_date_ict),
        "routing": routing,
        "scorecard": scard,
        "gradeability": grade,
        "classifier_cost": classifier_cost(classifier_health_row),
        "sources": source_snapshot(source_rows, now_utc),
        "tuning_candidates": tuning_candidates(routing, scard, grade),
    }
