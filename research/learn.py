"""Learn release → asset reactions, with an honest walk-forward test.

Question 1 (after the print): given how much the US releases at one minute
surprised, which way do XAU / EURUSD / USDJPY / GBPUSD / US500 move over
5 / 15 / 60 min, how often, and when is that reliable enough (≥ 85%) to say?

Question 2 (before the print): does forecast-vs-previous say anything about
the move INTO the release? (Answer from the data: no — see pre_release.)

Model ("composite", USD releases):
  * each release: robust z = (actual − forecast) / scale, where scale =
    1.4826·MAD of that series' past surprises. (Plain SD is useless here: the
    2020 COVID prints — NFP/claims surprises in the millions — inflated it
    so much that ~no later surprise ever looked large.)
  * hawkish sign from the hand rules in src/calendar.py (higher CPI = +1,
    higher claims = −1, …): the "logic must be right" prior. Statement-driven
    releases and series without a rule are skipped.
  * everything printing in the same minute is ONE event: composite
        C = Σ_series  w[series, asset, h] · rule_sign · z
    where w is LEARNED from training data: how often that series' surprise
    alone lined up with the asset's move (shrunk toward 50% with PRIOR_N
    pseudo-counts; series no better than a coin flip get weight 0 — e.g.
    PPI, which the data shows does not move gold).
  * asset direction per unit of C is learned too (sign of the training
    relation), so nothing about "USD up = gold down" is hard-coded.
  * CONFIDENCE GATE: per (asset, horizon) the smallest |C| threshold whose
    TRAINING precision is ≥ GATE_PREC with ≥ GATE_MIN_N graded calls.
    Below it → no call ("ในอดีตไม่ชัด").

Grading convention (same as the live scorecard): a move smaller than
FLAT[asset] % is "flat" — neither right nor wrong. Precision = right /
(right + wrong). Coverage = calls / release-minutes. Both are reported: a
high precision on a tiny coverage is honest only when shown together.

Walk-forward: at the start of each month (2021→now), fit weights + gates on
every row before that month and predict the month. Out-of-sample (OOS)
numbers are what a live bot retrained monthly would have scored.

    python -m research.learn
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research.prices import ASSETS  # noqa: E402
from src import calendar as cal  # noqa: E402

HERE = os.path.dirname(__file__)
DATASET = os.path.join(HERE, "dataset.csv")
OUT_DIR = os.path.join(HERE, "data")
LIVE_JSON = os.path.join(os.path.dirname(HERE), "config", "release_stats.json")

HORIZONS = (5, 15, 60)
FLAT = {"XAU": 0.10, "US500": 0.10, "EURUSD": 0.05, "GBPUSD": 0.05, "USDJPY": 0.05}
PRIOR_N = 10               # shrinkage pseudo-counts toward 50%
MIN_SERIES_N = 8           # series needs this many graded samples to get a weight
GATE_PREC = 0.85
GATE_MIN_N = 20
THRESHOLDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0)
TEST_YEARS = range(2021, 2027)
# Recency weighting: a training sample counts 0.5^(age / HALF_LIFE_Y). Gold's
# reaction function drifts (2026: hot US data often did NOT sink gold), so the
# learner should lean on recent years. None = all years equal.
HALF_LIFE_Y = float(os.environ.get("LEARN_HALF_LIFE_Y", "2.0")) or None
# Drift guard: a gate is switched off when its precision over the last
# DRIFT_WINDOW_D days of training falls below GATE_PREC - DRIFT_TOLERANCE.
DRIFT_WINDOW_D = int(os.environ.get("LEARN_DRIFT_DAYS", "180"))
DRIFT_TOLERANCE = 0.10
DRIFT_MIN_N = 10
# Card history uses |z| >= 0.5: many headline series print in 0.1 steps, so a
# one-tick CPI miss is z ≈ 0.7 — a |z| >= 1 cut would drop most real surprises.
SERIES_Z = 0.5
MIN_SCALE_N = 8


# ---------------------------------------------------------------- helpers

def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def wilson_lb(k: int, n: int, z: float = 1.645) -> float:
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def robust_scale(vals: list[float]) -> float | None:
    if len(vals) < MIN_SCALE_N:
        return None
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals) * 1.4826
    if mad > 0:
        return mad
    sd = statistics.pstdev(vals)
    return sd or None


def rule_sign(title: str, currency: str) -> int | None:
    """+1 if a higher print is hawkish for the currency, −1 if inverse, None
    if statement-driven or no rule. Reuses the live rules (the logic prior)."""
    ev = cal.CalEvent("x", title, currency, "High", "", "", datetime(2024, 1, 1))
    if cal.is_statement_driven(ev):
        return None
    tl = title.lower()
    for pat, inv, _ in cal._GOLD_IMPACT_RULES:
        if pat.search(tl):
            return -1 if inv else 1
    return None


def outcome(ret: float | None, band: float) -> int | None:
    if ret is None:
        return None
    if abs(ret) < band:
        return 0
    return 1 if ret > 0 else -1


def load_rows() -> list[dict]:
    with open(DATASET, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_ts"] = datetime.strptime(r["dt_utc"], "%Y-%m-%dT%H:%M:%SZ")
        r["_s"] = _f(r["surprise"])
        r["_e"] = _f(r["expectation"])
        r["_key"] = f"{r['currency']}|{r['title']}"
    rows.sort(key=lambda r: r["_ts"])
    return rows


def minutes(rows: list[dict]) -> list[dict]:
    """Group USD releases by release minute (one market event)."""
    by = defaultdict(list)
    for r in rows:
        if r["currency"] == "USD" and r["_s"] is not None:
            by[r["dt_utc"]].append(r)
    out = []
    for ts, rs in sorted(by.items()):
        out.append({"dt_utc": ts, "_ts": rs[0]["_ts"], "rows": rs,
                    "ret": {(a, h): _f(rs[0][f"r{h}_{a}"]) for a in ASSETS for h in HORIZONS}})
    return out


# ---------------------------------------------------------------- fitting

def fit(train_rows: list[dict]) -> dict:
    """Scales, per-series weights, asset directions and confidence gates."""
    # 1. robust surprise scale per series
    vals = defaultdict(list)
    for r in train_rows:
        if r["_s"] not in (None, 0):
            vals[r["_key"]].append(r["_s"])
    scales = {k: s for k, v in vals.items() if (s := robust_scale(v))}

    def comps(minute):
        out = []
        for r in minute["rows"]:
            sc = scales.get(r["_key"])
            rs = rule_sign(r["title"], r["currency"])
            if sc is None or rs is None or not r["_s"]:
                continue
            out.append((r["_key"], rs * r["_s"] / sc))
        return out

    mins = minutes(train_rows)
    t_end = max(m["_ts"] for m in mins) if mins else None

    def rw(m) -> float:
        if not HALF_LIFE_Y or t_end is None:
            return 1.0
        return 0.5 ** (((t_end - m["_ts"]).days / 365.25) / HALF_LIFE_Y)

    # 2. asset direction per unit of hawkish surprise (unweighted composite)
    asset_dir = {}
    for a in ASSETS:
        for h in HORIZONS:
            agree = disagree = 0
            for m in mins:
                c = sum(z for _, z in comps(m))
                o = outcome(m["ret"][(a, h)], FLAT[a])
                if not c or not o:
                    continue
                if (c > 0) == (o > 0):
                    agree += rw(m)
                else:
                    disagree += rw(m)
            asset_dir[f"{a}|{h}"] = 1 if agree >= disagree else -1

    # 3. per-series weights: how often that series' own surprise sign lined up
    #    with the asset's move in the learned direction (shrunk toward 50%).
    weights = {}
    tally = defaultdict(lambda: [0.0, 0.0, 0])      # weighted hit, weighted n, raw n
    for m in mins:
        cs = comps(m)
        for a in ASSETS:
            for h in HORIZONS:
                o = outcome(m["ret"][(a, h)], FLAT[a])
                if not o:
                    continue
                d = asset_dir[f"{a}|{h}"]
                for k, z in cs:
                    if abs(z) < 0.5:
                        continue
                    t = tally[(k, a, h)]
                    w_ = rw(m)
                    t[1] += w_
                    t[0] += w_ * int((1 if z > 0 else -1) * d == o)
                    t[2] += 1
    for (k, a, h), (hit, n, raw_n) in tally.items():
        if raw_n < MIN_SERIES_N:
            continue
        shrunk = (hit + PRIOR_N / 2) / (n + PRIOR_N)
        w = max(0.0, (shrunk - 0.5) * 2)
        if w > 0:
            weights[f"{k}@{a}|{h}"] = round(w, 4)

    model = {"scales": scales, "asset_dir": asset_dir, "weights": weights, "gates": {}}

    # 4. confidence gates from training precision
    for a in ASSETS:
        for h in HORIZONS:
            gate = None
            for t in THRESHOLDS:
                right = wrong = 0.0
                raw_n = 0
                recent = [0, 0]
                for m in mins:
                    c = composite(model, m, a, h)
                    if c is None or abs(c) < t:
                        continue
                    o = outcome(m["ret"][(a, h)], FLAT[a])
                    if not o:
                        continue
                    pred = (1 if c > 0 else -1) * model["asset_dir"][f"{a}|{h}"]
                    right += rw(m) * int(pred == o)
                    wrong += rw(m) * int(pred != o)
                    raw_n += 1
                    if (t_end - m["_ts"]).days <= DRIFT_WINDOW_D:
                        recent[0] += int(pred == o)
                        recent[1] += 1
                if raw_n < GATE_MIN_N or right / (right + wrong) < GATE_PREC:
                    continue
                if recent[1] >= DRIFT_MIN_N and recent[0] / recent[1] < GATE_PREC - DRIFT_TOLERANCE:
                    continue        # historically fine, but it has stopped working lately
                gate = {"threshold": t, "train_prec": round(right / (right + wrong), 4),
                        "train_n": raw_n,
                        "recent_prec": round(recent[0] / recent[1], 4) if recent[1] else None,
                        "recent_n": recent[1]}
                break
            model["gates"][f"{a}|{h}"] = gate
    return model


def composite(model: dict, minute: dict, asset: str, h: int) -> float | None:
    c, used = 0.0, 0
    for r in minute["rows"]:
        sc = model["scales"].get(r["_key"])
        rs = rule_sign(r["title"], r["currency"])
        w = model["weights"].get(f"{r['_key']}@{asset}|{h}", 0.0)
        if sc is None or rs is None or not r["_s"] or w == 0:
            continue
        c += w * rs * r["_s"] / sc
        used += 1
    return c if used else None


# ---------------------------------------------------------------- baseline

def logic_pred_xau(minute: dict) -> int | None:
    """What the CURRENT live card said for gold: majority of per-release XAU
    pills at that minute (+1 bullish / −1 bearish / None)."""
    votes = 0
    for r in minute["rows"]:
        ev = cal.CalEvent("x", r["title"], r["currency"], "High", r["forecast"],
                          r["previous"], r["_ts"])
        d = dict(cal.event_impact_pills(ev, actual_text=r["actual"])).get("XAU")
        votes += {"bullish": 1, "bearish": -1}.get(d, 0)
    return (1 if votes > 0 else -1) if votes else None


# ---------------------------------------------------------------- evaluation

def walk_forward(rows: list[dict]) -> list[dict]:
    """Refit at the start of every month on everything before it, predict
    that month (the live job refits weekly, so monthly is if anything a bit
    pessimistic). `year` on each prediction = the test year for reporting."""
    preds = []
    all_min = minutes(rows)
    months = sorted({(m["_ts"].year, m["_ts"].month) for m in all_min
                     if m["_ts"].year in TEST_YEARS})
    for (y, mo) in months:
        start = datetime(y, mo, 1)
        train = [r for r in rows if r["_ts"] < start]
        test_min = [m for m in all_min if (m["_ts"].year, m["_ts"].month) == (y, mo)]
        if not train or not test_min:
            continue
        model = fit(train)
        for m in test_min:
            for a in ASSETS:
                for h in HORIZONS:
                    ret = m["ret"][(a, h)]
                    if ret is None:
                        continue
                    c = composite(model, m, a, h)
                    gate = model["gates"][f"{a}|{h}"]
                    pred_all = None if c is None or c == 0 else \
                        (1 if c > 0 else -1) * model["asset_dir"][f"{a}|{h}"]
                    pred_gated = pred_all if (pred_all is not None and gate
                                              and abs(c) >= gate["threshold"]) else None
                    preds.append({
                        "year": y, "dt_utc": m["dt_utc"], "asset": a, "h": h,
                        "titles": " + ".join(r["title"] for r in m["rows"])[:200],
                        "composite": "" if c is None else round(c, 4), "ret_pct": ret,
                        "pred_gated": "" if pred_gated is None else pred_gated,
                        "pred_all": "" if pred_all is None else pred_all,
                        "pred_logic": (logic_pred_xau(m) or "") if a == "XAU" else "",
                    })
    return preds


def score(preds, col, asset=None, h=None, band=None):
    right = wrong = flat = calls = eligible = 0
    for p in preds:
        if (asset and p["asset"] != asset) or (h and p["h"] != h):
            continue
        eligible += 1
        if p[col] == "":
            continue
        calls += 1
        o = outcome(p["ret_pct"], band if band is not None else FLAT[p["asset"]])
        if o == 0:
            flat += 1
        elif o == p[col]:
            right += 1
        else:
            wrong += 1
    n = right + wrong
    return {"precision": round(right / n, 4) if n else None,
            "wilson_lb": round(wilson_lb(right, n), 4) if n else None,
            "right": right, "wrong": wrong, "flat": flat,
            "calls": calls, "minutes": eligible,
            "coverage": round(calls / eligible, 4) if eligible else None}


def pre_release_study(rows: list[dict]) -> dict:
    """Share of pre-release drifts (T-60/T-15 → T0) moving in the direction
    the consensus change (forecast − previous) implies via the hawkish rule
    and the XAU-style USD mapping. ~50% = no usable signal."""
    out = {}
    exp_dir = {"XAU": -1, "EURUSD": -1, "GBPUSD": -1, "USDJPY": 1, "US500": -1}
    for a in ASSETS:
        for m in (60, 15):
            w = n = 0
            for r in rows:
                if r["currency"] != "USD" or r["_e"] in (None, 0):
                    continue
                rs = rule_sign(r["title"], "USD")
                v = _f(r[f"pre{m}_{a}"])
                if rs is None or v is None or abs(v) < FLAT[a]:
                    continue
                pred = (1 if r["_e"] * rs > 0 else -1) * exp_dir[a]
                n += 1
                w += int((v > 0) == (pred > 0))
            out[f"{a}|pre{m}"] = {"n": n, "share_as_consensus_implies": round(w / n, 4) if n else None}
    return out


def series_table(rows: list[dict], model: dict) -> dict:
    """Descriptive per-series history for the cards (in-sample, labelled so):
    for a meaningful surprise (|z| ≥ SERIES_Z), how often XAU/… moved the way the
    learned direction says, the typical move, and n."""
    out = {}
    by = defaultdict(list)
    for r in rows:
        if r["currency"] == "USD":
            by[r["_key"]].append(r)
    for k, rs in by.items():
        sc = model["scales"].get(k)
        sign = rule_sign(rs[0]["title"], "USD")
        if not sc or sign is None:
            continue
        cells = {}
        for a in ASSETS:
            for h in HORIZONS:
                d = model["asset_dir"][f"{a}|{h}"]
                right = wrong = 0
                moves = []
                for r in rs:
                    if not r["_s"] or abs(r["_s"] / sc) < SERIES_Z:
                        continue
                    ret = _f(r[f"r{h}_{a}"])
                    o = outcome(ret, FLAT[a])
                    if ret is None:
                        continue
                    hawk = 1 if r["_s"] * sign > 0 else -1
                    moves.append(ret * hawk)
                    if o:
                        right += int(o == hawk * d)
                        wrong += int(o != hawk * d)
                n = right + wrong
                if n >= 5:
                    cells[f"{a}|{h}"] = {"n": n, "hit": round(right / n, 3),
                                         "median_move_hawkish_pct": round(statistics.median(moves), 4)}
        if cells:
            out[k] = {"scale": sc, "rule_sign": sign, "cells": cells}
    return out


def main():
    rows = load_rows()
    preds = walk_forward(rows)
    with open(os.path.join(OUT_DIR, "oos_predictions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(preds[0].keys()))
        w.writeheader()
        w.writerows(preds)

    report = {"rows": len(rows), "span": [rows[0]["dt_utc"], rows[-1]["dt_utc"]],
              "flat_pct": FLAT, "gate": {"precision": GATE_PREC, "min_n": GATE_MIN_N},
              "oos": {}, "oos_xau_by_year": {}, "oos_strict_band": {},
              "pre_release": pre_release_study(rows)}
    for a in ASSETS:
        for h in HORIZONS:
            cell = {"gated": score(preds, "pred_gated", a, h),
                    "all": score(preds, "pred_all", a, h)}
            if a == "XAU":
                cell["current_logic"] = score(preds, "pred_logic", a, h)
            report["oos"][f"{a}|{h}"] = cell
            report["oos_strict_band"][f"{a}|{h}"] = score(preds, "pred_gated", a, h,
                                                         band=FLAT[a] * 2)
    for y in TEST_YEARS:
        py = [p for p in preds if p["year"] == y]
        report["oos_xau_by_year"][y] = {h: score(py, "pred_gated", "XAU", h) for h in HORIZONS}

    live = fit(rows)
    report["live_gates"] = live["gates"]
    report["live_weights_xau15"] = {k.split("@")[0]: v for k, v in live["weights"].items()
                                    if k.endswith("@XAU|15")}
    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)

    payload = {"generated_from": report["span"], "flat_pct": FLAT,
               "model": live, "series": series_table(rows, live),
               "oos": report["oos"]}
    with open(LIVE_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return report


if __name__ == "__main__":
    rep = main()
    for k, v in rep["oos"].items():
        print(k.ljust(10), " | ".join(
            f"{c}: prec={s['precision']} n={s['right']+s['wrong']} flat={s['flat']} cov={s['coverage']}"
            for c, s in v.items()))
