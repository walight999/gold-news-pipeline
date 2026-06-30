"""Directional-accuracy scorecard logic (Phase 1 — calendar verdicts).

Pure-function tests: verdict→direction mapping, single-call grading with the
flat band, daily aggregation (counts, $ sums, miss ordering, pending), and the
7-day rolling accuracy. No I/O — main.run_scorecard wires these to the store."""
from __future__ import annotations

from src.scorecard import (
    build_scorecard,
    grade,
    rolling_accuracy,
    rolling_accuracy_detail,
    verdict_to_dir,
)


def test_verdict_to_dir_maps_emoji_and_words():
    assert verdict_to_dir("🟢 Bullish gold") == "bull"
    assert verdict_to_dir("🔴 Bearish gold") == "bear"
    assert verdict_to_dir("⚪ Neutral — print matched forecast") == "neutral"
    assert verdict_to_dir("Bullish") == "bull"
    assert verdict_to_dir("") == ""
    assert verdict_to_dir(None) == ""
    assert verdict_to_dir("something unrelated") == ""


def test_grade_directional_hits_and_misses():
    # bull call, gold up beyond band → correct
    assert grade("bull", 0.42) == "correct"
    # bull call, gold down → wrong
    assert grade("bull", -0.30) == "wrong"
    # bear call, gold down → correct
    assert grade("bear", -0.25) == "correct"
    # bear call, gold up → wrong
    assert grade("bear", 0.50) == "wrong"


def test_grade_flat_band_and_neutral():
    # within the flat band → "flat" (excluded from accuracy), either direction
    assert grade("bull", 0.05) == "flat"
    assert grade("bear", -0.04) == "flat"
    # neutral call never scores for or against
    assert grade("neutral", 1.0) == "flat"
    assert grade("neutral", 0.0) == "flat"


def test_grade_band_boundary_inclusive():
    """A move exactly at the band counts as directional (>=)."""
    assert grade("bull", 0.10) == "correct"
    assert grade("bull", -0.10) == "wrong"


def _row(eid, pdir, r15, base=2600.0, title="Event", country="US", verdict=""):
    return {
        "event_id": eid, "predicted_dir": pdir, "xau_return_15m": r15,
        "xau_base_price": base, "title": title, "country": country,
        "predicted_verdict_th": verdict,
    }


def test_build_scorecard_counts_and_accuracy():
    rows = [
        _row("a", "bull", 0.40),    # correct
        _row("b", "bear", -0.30),   # correct
        _row("c", "bull", -0.50),   # wrong
        _row("d", "bull", 0.02),    # flat (barely moved)
        _row("e", "neutral", 0.80), # flat (neutral never scores)
    ]
    sc = build_scorecard(rows)
    assert sc["n_correct"] == 2
    assert sc["n_wrong"] == 1
    assert sc["n_flat"] == 2
    assert sc["n_graded"] == 3
    assert round(sc["accuracy_pct"]) == 67   # 2/3


def test_build_scorecard_usd_sums_and_misses():
    rows = [
        _row("a", "bull", 0.40, base=2600.0),    # +$10.4 up
        _row("c", "bull", -0.50, base=2600.0, title="Core PCE"),  # -$13.0 down, WRONG
        _row("f", "bear", -0.20, base=2600.0),   # -$5.2 down, correct
    ]
    sc = build_scorecard(rows)
    # up sum = only the +0.40% row
    assert round(sc["sum_up_usd"], 1) == 10.4
    # down sum = -0.50% and -0.20% rows
    assert round(sc["sum_down_usd"], 1) == -18.2
    # one miss, carries the $ move and title
    assert len(sc["misses"]) == 1
    assert sc["misses"][0]["title"] == "Core PCE"
    assert round(sc["misses"][0]["usd_move"], 1) == -13.0


def test_build_scorecard_misses_sorted_by_dollar_magnitude():
    rows = [
        _row("x", "bull", -0.10, base=2600.0, title="small"),   # -$2.6 wrong
        _row("y", "bear", 0.60, base=2600.0, title="big"),      # +$15.6 wrong
    ]
    sc = build_scorecard(rows)
    assert [m["title"] for m in sc["misses"]] == ["big", "small"]


def test_build_scorecard_pending_when_no_price():
    rows = [
        _row("a", "bull", "", base=""),    # no 15m price yet → pending
        _row("b", "bull", 0.30),           # graded correct
    ]
    sc = build_scorecard(rows)
    assert sc["n_pending"] == 1
    assert sc["n_correct"] == 1
    assert sc["n_graded"] == 1


def test_build_scorecard_handles_missing_base_price():
    """%-move present but no base price → still graded, $ stays out of sums."""
    rows = [_row("a", "bull", 0.40, base="")]
    sc = build_scorecard(rows)
    assert sc["n_correct"] == 1
    assert sc["sum_up_usd"] == 0.0
    assert sc["misses"] == []


def test_build_scorecard_ignores_non_calendar_rows():
    """RSS rows (no predicted_dir) are invisible to the scorecard."""
    rows = [
        {"event_id": "rss1", "xau_return_15m": 0.9},   # no predicted_dir
        _row("cal1", "bull", 0.40),
    ]
    sc = build_scorecard(rows)
    assert sc["n_graded"] == 1


def test_build_scorecard_empty():
    sc = build_scorecard([])
    assert sc["n_graded"] == 0
    assert sc["accuracy_pct"] == 0.0
    assert sc["misses"] == []


def test_rolling_accuracy_weighted_by_calls():
    hist = [
        {"date_ict": "2026-06-24", "n_correct": 3, "n_graded": 4},
        {"date_ict": "2026-06-25", "n_correct": 1, "n_graded": 2},
        {"date_ict": "2026-06-26", "n_correct": 2, "n_graded": 2},
    ]
    # (3+1+2) / (4+2+2) = 6/8 = 75%
    assert round(rolling_accuracy(hist, days=7)) == 75


def test_rolling_accuracy_none_when_no_history():
    assert rolling_accuracy([]) is None
    assert rolling_accuracy([{"date_ict": "2026-06-26", "n_correct": 0, "n_graded": 0}]) is None


def test_rolling_accuracy_detail_shape():
    hist = [
        {"date_ict": "2026-06-24", "n_correct": 3, "n_graded": 4},
        {"date_ict": "2026-06-25", "n_correct": 1, "n_graded": 2},
        {"date_ict": "2026-06-26", "n_correct": 2, "n_graded": 2},
    ]
    d = rolling_accuracy_detail(hist, days=30)
    assert d == {"accuracy": 0.75, "correct": 6, "total": 8, "days": 30}


def test_rolling_accuracy_detail_none_when_no_history():
    assert rolling_accuracy_detail([]) is None
    assert rolling_accuracy_detail([{"date_ict": "2026-06-26", "n_correct": 0, "n_graded": 0}]) is None
