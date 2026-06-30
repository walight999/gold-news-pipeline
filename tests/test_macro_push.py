"""Tests for the macro-state push to the CHUM alert-bot worker (src/macro_push.py).

No network: compute_state runs on a synthetic DataFrame; the client is faked.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import macro_push


# ---------------------------------------------------------------------------
# news_factor_from_directions — pure mapping
# ---------------------------------------------------------------------------
def test_news_factor_empty_is_zero():
    assert macro_push.news_factor_from_directions([], "XAUUSD") == 0.0


def test_news_factor_unknown_labels_dropped():
    # nothing recognized → 0.0 (not a crash)
    assert macro_push.news_factor_from_directions(["banana", "xyz"], "XAUUSD") == 0.0


def test_news_factor_dovish_is_bullish_gold():
    # dovish + risk_off both bid gold → strongly positive
    assert macro_push.news_factor_from_directions(["dovish", "risk_off"], "XAUUSD") == 1.0


def test_news_factor_hawkish_is_bearish_gold():
    assert macro_push.news_factor_from_directions(["hawkish", "risk_on"], "XAUUSD") == -1.0


def test_news_factor_excludes_neutrals_from_denominator():
    # neutral = "no view" (≈ absent) → excluded; one dovish among neutrals → full +1,
    # not diluted toward 0 (otherwise the 20% news weight contributes ~nothing live).
    assert macro_push.news_factor_from_directions(["dovish", "neutral", "neutral"], "XAUUSD") == 1.0
    # all-neutral → 0.0 (no signed view)
    assert macro_push.news_factor_from_directions(["neutral", "neutral"], "XAUUSD") == 0.0
    # mixed signed: dovish(+1) + hawkish(-1) → 0.0 (genuine disagreement, not dilution)
    assert macro_push.news_factor_from_directions(["dovish", "hawkish"], "XAUUSD") == 0.0


def test_news_factor_unknown_asset_is_zero():
    assert macro_push.news_factor_from_directions(["dovish"], "NOPE") == 0.0


# ---------------------------------------------------------------------------
# compute_state — factor fusion on a synthetic frame
# ---------------------------------------------------------------------------
def _frame(n: int = 200, *, rising_yields: bool, uptrend: bool) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    trend = np.linspace(0, 1, n) * (1 if uptrend else -1)
    price = 2000 + trend * 200 + np.sin(np.arange(n) / 7) * 5
    ry = np.linspace(0, 1, n) * (1 if rising_yields else -1) + 1.5
    return pd.DataFrame({
        "price": price,
        "usd": 100 + np.sin(np.arange(n) / 11),
        "silver": 25 + np.cos(np.arange(n) / 9),
        "vix": 15 + np.sin(np.arange(n) / 5),
        "spx": 5000 + trend * 100,
        "real_yield": ry,
        "breakeven": 2.3 + np.sin(np.arange(n) / 13) * 0.05,
    }, index=idx)


def test_compute_state_shape_and_bounds():
    st = macro_push.compute_state(_frame(rising_yields=False, uptrend=True), news_factor=0.0)
    assert set(st["factors"]) == {"macro", "tech", "risk", "news"}
    assert -1.0 <= st["conviction"] <= 1.0
    for v in st["factors"].values():
        assert -1.0 <= v <= 1.0
    assert st["regime"] in {"yields_up", "yields_down", "flat"}


def test_compute_state_regime_tracks_yield_slope():
    up = macro_push.compute_state(_frame(rising_yields=True, uptrend=True))
    down = macro_push.compute_state(_frame(rising_yields=False, uptrend=True))
    assert up["regime"] == "yields_up"
    assert down["regime"] == "yields_down"


def test_compute_state_tech_follows_trend():
    up = macro_push.compute_state(_frame(rising_yields=False, uptrend=True))
    down = macro_push.compute_state(_frame(rising_yields=False, uptrend=False))
    assert up["factors"]["tech"] == 1.0
    assert down["factors"]["tech"] == -1.0


def test_compute_state_news_factor_flows_through_and_clips():
    st = macro_push.compute_state(_frame(rising_yields=False, uptrend=True), news_factor=5.0)
    assert st["factors"]["news"] == 1.0  # clipped to +1


def test_compute_state_news_weight_moves_conviction():
    f = _frame(rising_yields=False, uptrend=True)
    base = macro_push.compute_state(f, news_factor=0.0)["conviction"]
    bull = macro_push.compute_state(f, news_factor=1.0)["conviction"]
    bear = macro_push.compute_state(f, news_factor=-1.0)["conviction"]
    # news has weight 0.20 → ±1 news shifts conviction by ~±0.20 around base
    assert bull > base > bear
    assert bull - base == pytest.approx(macro_push.WEIGHTS["news"], abs=0.02)


# ---------------------------------------------------------------------------
# build_payload — worker contract
# ---------------------------------------------------------------------------
def test_build_payload_shape():
    st = {"factors": {"macro": 0.1, "tech": 1.0, "risk": 0.0, "news": 0.2}, "conviction": 0.25, "regime": "yields_down"}
    p = macro_push.build_payload("XAUUSD", st, ts="2026-06-28T00:00:00+00:00")
    assert p == {
        "ticker": "XAUUSD",
        "conviction": 0.25,
        "regime": "yields_down",
        "factors": {"macro": 0.1, "tech": 1.0, "risk": 0.0, "news": 0.2},
        "ts": "2026-06-28T00:00:00+00:00",
    }


def test_build_payload_defaults_ts_to_now_iso():
    p = macro_push.build_payload("XAUUSD", {"factors": {}, "conviction": 0.0, "regime": "flat"})
    assert p["ts"].endswith("+00:00") and "T" in p["ts"]


# ---------------------------------------------------------------------------
# MacroPushClient — env gating + endpoint shape
# ---------------------------------------------------------------------------
def test_from_env_none_when_unset(monkeypatch):
    monkeypatch.delenv("MACRO_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("MACRO_WEBHOOK_SECRET", raising=False)
    assert macro_push.MacroPushClient.from_env() is None


def test_from_env_none_when_partial(monkeypatch):
    monkeypatch.setenv("MACRO_WEBHOOK_URL", "https://api.justchum.com")
    monkeypatch.delenv("MACRO_WEBHOOK_SECRET", raising=False)
    assert macro_push.MacroPushClient.from_env() is None


def test_from_env_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("MACRO_WEBHOOK_URL", "https://api.justchum.com/")
    monkeypatch.setenv("MACRO_WEBHOOK_SECRET", "s3cr3t")
    c = macro_push.MacroPushClient.from_env()
    assert c is not None and c.base_url == "https://api.justchum.com" and c.secret == "s3cr3t"


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "ok"
        self.request = None


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):  # noqa: A002
        _FakeClient.last_url = url
        _FakeClient.last_json = json
        _FakeClient.last_headers = headers
        return _FakeResp(200)


def test_push_hits_macro_endpoint(monkeypatch):
    monkeypatch.setattr(macro_push.httpx, "Client", _FakeClient)
    c = macro_push.MacroPushClient(base_url="https://api.justchum.com", secret="abc")
    res = c.push({"ticker": "XAUUSD"})
    assert res["status"] == 200
    # secret rides the X-Webhook-Secret header now, not the URL path (audit H3)
    assert _FakeClient.last_url == "https://api.justchum.com/webhook/macro"
    assert _FakeClient.last_headers == {"X-Webhook-Secret": "abc"}
    assert _FakeClient.last_json == {"ticker": "XAUUSD"}


# ---------------------------------------------------------------------------
# next_high_impact — feeds the alert-bot news-event entry gate
# ---------------------------------------------------------------------------
class _Ev:
    def __init__(self, mins_from_now, impact, title="x"):
        from datetime import datetime, timedelta, timezone
        self.dt_utc = datetime.now(timezone.utc) + timedelta(minutes=mins_from_now)
        self.impact = impact
        self.title = title
        self.country = "US"


def test_next_high_impact_picks_soonest_high_within_window():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    evs = [_Ev(120, "high", "far"), _Ev(20, "high", "soon"), _Ev(10, "medium", "med"), _Ev(30, "high", "mid")]
    pick = macro_push.next_high_impact(evs, now, within_min=45)
    assert pick is not None and pick.title == "soon"  # 20m, soonest high within 45


def test_next_high_impact_ignores_medium_and_far_and_past():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert macro_push.next_high_impact([_Ev(10, "medium")], now) is None  # not high
    assert macro_push.next_high_impact([_Ev(200, "high")], now, within_min=45) is None  # too far
    assert macro_push.next_high_impact([_Ev(-5, "high")], now) is None  # already released
    assert macro_push.next_high_impact([], now) is None


def test_push_next_event_window_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MACRO_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("MACRO_WEBHOOK_SECRET", raising=False)
    from datetime import datetime, timezone
    assert macro_push.push_next_event_window([_Ev(20, "high")], datetime.now(timezone.utc)) is None


def test_compute_and_push_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MACRO_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("MACRO_WEBHOOK_SECRET", raising=False)
    assert macro_push.compute_and_push() == []


# ---------------------------------------------------------------------------
# recent_news_directions — the live news edge (regression: was silently empty)
# ---------------------------------------------------------------------------
def test_recent_news_directions_filters_and_orders():
    rows = [
        {"direction_label": "dovish", "last_seen_ts": "2026-06-28T10:00:00+00:00"},
        {"direction_label": "hawkish", "last_seen_ts": "2026-06-28T11:00:00+00:00"},
        {"direction_label": "risk_off", "last_seen_ts": "2026-06-20T00:00:00+00:00"},  # stale → dropped
        {"direction_label": "", "last_seen_ts": "2026-06-28T12:00:00+00:00"},  # blank → dropped
    ]
    cutoff = "2026-06-27T00:00:00+00:00"
    # most-recent first, within window, non-blank
    assert macro_push.recent_news_directions(rows, cutoff) == ["hawkish", "dovish"]


def test_recent_news_directions_empty_rows():
    assert macro_push.recent_news_directions([], "2026-06-27T00:00:00+00:00") == []


def test_recent_news_directions_caps_limit():
    rows = [{"direction_label": "dovish", "last_seen_ts": f"2026-06-28T{h:02d}:00:00+00:00"} for h in range(20)]
    assert len(macro_push.recent_news_directions(rows, "2026-06-27T00:00:00+00:00", limit=5)) == 5


# ---------------------------------------------------------------------------
# Vocab + WEIGHTS sync guards (drift catchers)
# ---------------------------------------------------------------------------
def test_news_map_covers_direction_keyword_vocab():
    """The pipeline's direction_label vocab (config/keywords.yaml) must be a subset
    of the XAUUSD news_map, else live news directions get silently dropped."""
    import pathlib

    import yaml

    cfg = yaml.safe_load(
        (pathlib.Path(__file__).parent.parent / "config" / "keywords.yaml").read_text(encoding="utf-8")
    )
    vocab = set(cfg.get("direction_keywords", {}).keys()) | {"neutral"}
    news_map = set(macro_push.ASSETS["XAUUSD"]["news_map"].keys())
    assert vocab <= news_map, f"direction labels missing from news_map: {vocab - news_map}"


def test_weights_match_backtest_priors():
    """Golden: must equal backtest/multifactor.py WEIGHTS (Pillar C learns deviations
    FROM these). If you change one, change both."""
    assert macro_push.WEIGHTS == {"macro": 0.45, "tech": 0.20, "risk": 0.15, "news": 0.20}


def test_compute_state_tolerates_missing_columns():
    """A missing factor column degrades that factor to 0, never crashes."""
    f = _frame(rising_yields=False, uptrend=True).drop(columns=["vix", "silver"])
    st = macro_push.compute_state(f, news_factor=0.0)
    assert -1.0 <= st["conviction"] <= 1.0
    assert all(-1.0 <= v <= 1.0 for v in st["factors"].values())
