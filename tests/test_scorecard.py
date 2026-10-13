"""Directional-accuracy scorecard logic (Phase 1 — calendar verdicts).

Pure-function tests: verdict→direction mapping, single-call grading with the
flat band, daily aggregation (counts, $ sums, miss ordering, pending), and the
7-day rolling accuracy. No I/O — main.run_scorecard wires these to the store."""
from __future__ import annotations

from datetime import date

from src.scorecard import (
    build_scorecard,
    grade,
    gradeability,
    public_accuracy_ok,
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


def test_build_scorecard_window_selects_return_column():
    """A move flat at 15m but clear at 30m grades only under window='30m'."""
    rows = [{"predicted_dir": "bull", "xau_return_15m": 0.05,   # < 0.10 band → flat
             "xau_return_30m": 0.22, "xau_base_price": 2600}]
    sc15 = build_scorecard(rows, window="15m")
    assert sc15["n_graded"] == 0 and sc15["n_flat"] == 1
    sc30 = build_scorecard(rows, window="30m")
    assert sc30["n_graded"] == 1 and sc30["n_correct"] == 1


def test_gradeability_reports_per_window_exclusion():
    rows = [
        {"predicted_dir": "bull", "xau_return_15m": 0.05, "xau_return_30m": 0.20},  # flat@15, graded@30
        {"predicted_dir": "bear", "xau_return_15m": 0.30, "xau_return_30m": 0.30},  # graded both
        {"predicted_dir": "bull", "xau_return_15m": None, "xau_return_30m": 0.40},  # pending@15, graded@30
        {"predicted_dir": "neutral", "xau_return_15m": 0.50},                        # not directional → ignored
    ]
    g = gradeability(rows, flat_pct=0.10, windows=("15m", "30m"))
    assert g["15m"] == {"graded": 1, "flat": 1, "pending": 1}
    assert g["30m"] == {"graded": 3, "flat": 0, "pending": 0}


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


def test_rolling_window_is_calendar_days_not_last_n_graded_rows():
    # Regression (2026-10): quiet days must age old calls out. The old code took
    # the last 7 rows that graded anything, so calls from weeks back stayed in a
    # "7-day" number that never moved.
    hist = [
        {"date_ict": "2026-09-15", "n_correct": 0, "n_graded": 10},
        {"date_ict": "2026-10-06", "n_correct": 0, "n_graded": 0},
        {"date_ict": "2026-10-08", "n_correct": 2, "n_graded": 3},
    ]
    d = rolling_accuracy_detail(hist, days=7, as_of=date(2026, 10, 9))
    assert d == {"accuracy": round(2 / 3, 4), "correct": 2, "total": 3, "days": 7}
    # nothing graded inside the window -> None, even with older history
    assert rolling_accuracy(hist, days=7, as_of=date(2026, 10, 30)) is None


def test_rolling_window_edges_inclusive_and_bad_dates_skipped():
    hist = [
        {"date_ict": "2026-10-03", "n_correct": 1, "n_graded": 1},   # 7th day back: in
        {"date_ict": "2026-10-02", "n_correct": 0, "n_graded": 5},   # 8th: out
        {"date_ict": "", "n_correct": 0, "n_graded": 9},             # undated: ignored
        {"date_ict": "garbage", "n_correct": 0, "n_graded": 9},
    ]
    assert rolling_accuracy(hist, days=7, as_of=date(2026, 10, 9)) == 100.0


def test_public_accuracy_gate():
    ok, _ = public_accuracy_ok({"accuracy": 0.6, "correct": 18, "total": 30, "days": 30})
    assert ok
    ok, why = public_accuracy_ok({"accuracy": 0.9, "correct": 9, "total": 10, "days": 30})
    assert not ok and "sample" in why
    ok, why = public_accuracy_ok({"accuracy": 0.4545, "correct": 15, "total": 33, "days": 30})
    assert not ok and "accuracy" in why
    ok, why = public_accuracy_ok(None)
    assert not ok
    # thresholds are configurable
    assert public_accuracy_ok({"accuracy": 0.5, "correct": 5, "total": 10, "days": 30},
                              min_calls=10, min_accuracy=0.5)[0]
