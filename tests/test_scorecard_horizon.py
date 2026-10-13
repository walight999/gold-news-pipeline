"""2026-10-11: each call is graded on the horizon it was made for.

The 🎯 gated composite on the Released card is a 5-minute call (the gate's
walk-forward edge lives at 5m); the hand-rule pills stay on the scorecard's
default window (15m). Before this, both were graded at 15m."""
from __future__ import annotations

from src import release_stats, scorecard
from src.line_flex import scorecard_bubble


def _row(ts, title, d, r5=None, r15=None, routed="calendar_post"):
    return {"first_seen_ts": ts, "title": title, "predicted_dir": d, "routed_as": routed,
            "country": "USD", "xau_return_5m": r5, "xau_return_15m": r15,
            "xau_base_price": "4000"}


def test_gated_call_wins_even_on_a_singleton_and_carries_its_window():
    rows = [_row("2026-10-02T12:30:00Z", "NFP", "bull", r5="-0.3", r15="0.4")]
    out = scorecard.collapse_minutes(rows, gated_call=lambda g: ("bear", "5m"))
    assert len(out) == 1
    assert out[0]["predicted_dir"] == "bear"
    assert out[0]["grade_window"] == "5m" and out[0]["call_kind"] == "gated"
    assert out[0]["title"] == "NFP"
    # the input row is not mutated (store rows are live references)
    assert "grade_window" not in rows[0] and rows[0]["predicted_dir"] == "bull"


def test_no_gated_call_falls_back_to_rule_logic_unchanged():
    rows = [_row("2026-10-02T12:30:00Z", "NFP", "bear", r15="-0.4"),
            _row("2026-10-02T12:30:00Z", "Unemployment Rate", "bear", r15="-0.4"),
            _row("2026-10-02T14:00:00Z", "ISM", "bull", r15="0.2")]
    out = scorecard.collapse_minutes(rows, gated_call=lambda g: None)
    assert [(r["predicted_dir"], r.get("call_kind")) for r in out] == [("bear", None), ("bull", None)]


def test_build_scorecard_grades_each_row_on_its_own_window():
    gated = dict(_row("2026-10-02T12:30:00Z", "NFP", "bear", r5="-0.30", r15="0.50"),
                 grade_window="5m", call_kind="gated")
    rule = _row("2026-10-02T14:00:00Z", "ISM", "bull", r5="0.50", r15="-0.20")
    sc = scorecard.build_scorecard([gated, rule], window="15m")
    # gated: bear vs 5m -0.30 → correct (at 15m it would have been wrong)
    # rule:  bull vs 15m -0.20 → wrong (at 5m it would have been right)
    assert (sc["n_correct"], sc["n_wrong"], sc["n_graded"]) == (1, 1, 2)
    assert (sc["n_gated_correct"], sc["n_gated_graded"]) == (1, 1)
    assert [m["window"] for m in sc["misses"]] == ["15m"]


def test_gated_row_pending_until_its_own_window_is_filled():
    gated = dict(_row("2026-10-02T12:30:00Z", "NFP", "bear", r5=None, r15="-0.5"),
                 grade_window="5m", call_kind="gated")
    sc = scorecard.build_scorecard([gated])
    assert sc["n_pending"] == 1 and sc["n_graded"] == 0


def test_minute_gated_xau_call_matches_the_live_model():
    # Build a print big enough to clear the live XAU gate from the shipped
    # config/release_stats.json — whatever series it currently weights.
    from src.calendar import _GOLD_IMPACT_RULES
    model = release_stats._load()["model"]
    cells = [c for c, g in (model.get("gates") or {}).items() if g and c.startswith("XAU|")]
    assert cells, "shipped model has no XAU gate"
    for key, sc in model["scales"].items():
        title = key.split("|", 1)[1]
        if not key.startswith("USD|") or not float(model["weights"].get(f"{key}@XAU|5", 0) or 0):
            continue
        if not any(p.search(title.lower()) for p, _, _ in _GOLD_IMPACT_RULES):
            continue
        call = release_stats.minute_gated_xau_call([("USD", title, str(100 + 50 * sc), "100")])
        if call:
            d, w = call
            assert d in ("bull", "bear") and w.endswith("m")
            # same direction the existing card helper reports
            assert release_stats.minute_gated_xau([("USD", title, str(100 + 50 * sc), "100")]) == d
            assert w[:-1] in release_stats.minute_call_line_th(
                [("USD", title, str(100 + 50 * sc), "100")])
            return
    raise AssertionError("no USD series produced a gated XAU call")


def test_bubble_shows_gated_vs_rule_split_and_miss_window():
    data = {"n_correct": 1, "n_wrong": 1, "n_flat": 0, "n_pending": 0, "n_graded": 2,
            "accuracy_pct": 50.0, "sum_up_usd": 10, "sum_down_usd": -5,
            "n_gated_correct": 1, "n_gated_graded": 1,
            "misses": [{"title": "ISM", "country": "USD", "predicted_dir": "bull",
                        "r15_pct": -0.2, "usd_move": -8.0, "window": "15m"}]}
    texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(scorecard_bubble(data, "2/10/26"))
    assert any("1/1" in t and "0/1" in t for t in texts)        # 🎯 1/1 · rule 0/1
    assert any(t.rstrip().endswith("15 นาที") for t in texts)  # "15 นาที"
