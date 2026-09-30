"""Fast Released cards: pinger dispatch, spot base from the pinger, and a card
held back while its actual is not yet published."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from src import main as m
from src import spot_feed
from src import store as store_mod
from src.calendar import CalEvent

NOW = datetime(2026, 10, 2, 12, 5, tzinfo=timezone.utc)
NFP = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


def _ev(title, dt, forecast="55K"):
    return CalEvent("x" + title, title, "USD", "High", forecast, "", dt)


def test_pinger_dispatched_once_per_release_minute(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["inputs"]["release_ts"])
        return httpx.Response(204)
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(m, "now_utc", lambda: NOW)
    s = _store()
    evs = [_ev("Non-Farm Employment Change", NFP), _ev("Unemployment Rate", NFP, "4.3%"),
           _ev("Lagarde Speaks", NFP, forecast=""),                      # no number → no ping
           _ev("ISM Manufacturing PMI", NOW + timedelta(hours=2))]        # too far ahead
    assert m._dispatch_release_pingers(s, evs) == 1
    assert calls == ["2026-10-02T12:30:00Z"]
    assert m._dispatch_release_pingers(s, evs) == 0                       # marker → once
    assert s.get("sent_log", ("ping:2026-10-02T12:30Z", "ping"))


def test_pinger_off_without_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert m._dispatch_release_pingers(_store(), [_ev("NFP", NFP)]) == 0


def test_pinger_spot_base_lands_on_the_tape(monkeypatch):
    s = _store()
    monkeypatch.setenv("SPOT_BASE", "4285.25")
    monkeypatch.setenv("SPOT_BASE_TS", "2026-10-02T12:29:35Z")
    m._record_pinger_spot_base(s)
    assert spot_feed.tape_price_before(s, NFP) == (4285.25, 1)
    mv = spot_feed.move_since(s, NFP, NFP + timedelta(minutes=2), 4270.0)
    assert round(mv["usd"], 2) == -15.25 and mv["minutes"] == 2


def test_pinger_spot_base_ignores_garbage(monkeypatch):
    s = _store()
    monkeypatch.setenv("SPOT_BASE", "")
    monkeypatch.setenv("SPOT_BASE_TS", "")
    m._record_pinger_spot_base(s)
    monkeypatch.setenv("SPOT_BASE", "abc")
    monkeypatch.setenv("SPOT_BASE_TS", "2026-10-02T12:29:35Z")
    m._record_pinger_spot_base(s)                  # logged, not raised
    assert spot_feed.load_tape(s) == []


def test_actual_wait_window_is_eight_minutes():
    assert m._ACTUAL_WAIT == timedelta(minutes=8)
    assert m._PING_AHEAD == timedelta(minutes=30)


def test_claims_last_week_obs_is_stale(monkeypatch):
    """Thursday 2026-10-01 claims: FRED still holding the week ending 09-19
    (last week's print) must be rejected; the week ending 09-26 is fresh."""
    from src import fred

    def obs(date):
        return lambda sid, key, n: [{"date": date, "value": "197000"}, {"date": "2026-09-12", "value": "201000"}]
    rel = datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(fred, "_get_observations", obs("2026-09-19"))
    assert fred.fetch_actual("Unemployment Claims", "k", release_dt=rel, country="USD") is None
    monkeypatch.setattr(fred, "_get_observations", obs("2026-09-26"))
    assert fred.fetch_actual("Unemployment Claims", "k", release_dt=rel, country="USD") is not None


def test_nfp_august_obs_is_stale_on_october_2(monkeypatch):
    from src import fred
    rel = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)

    def obs(d1, d0):
        return lambda sid, key, n: [{"date": d1, "value": "159000"}, {"date": d0, "value": "158900"}]
    monkeypatch.setattr(fred, "_get_observations", obs("2026-08-01", "2026-07-01"))
    assert fred.fetch_actual("Non-Farm Employment Change", "k", release_dt=rel, country="USD") is None
    monkeypatch.setattr(fred, "_get_observations", obs("2026-09-01", "2026-08-01"))
    assert fred.fetch_actual("Non-Farm Employment Change", "k", release_dt=rel, country="USD") is not None


def test_pce_fresh_obs_still_accepted_at_month_end(monkeypatch):
    from src import fred
    rel = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)      # Aug PCE, obs 08-01 = 60 d
    monkeypatch.setattr(fred, "_get_observations",
                        lambda sid, key, n: [{"date": "2026-08-01", "value": "125.3"}, {"date": "2026-07-01", "value": "125.0"}])
    assert fred.fetch_actual("Core PCE Price Index m/m", "k", release_dt=rel, country="USD") is not None


def test_fed_funds_old_target_rejected_on_decision_day(monkeypatch):
    from src import fred
    rel = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)

    def obs(d1):
        return lambda sid, key, n: [{"date": d1, "value": "4.00"}, {"date": "2026-09-14", "value": "4.00"}]
    monkeypatch.setattr(fred, "_get_observations", obs("2026-09-16"))   # still the old target
    assert fred.fetch_actual("Federal Funds Rate", "k", release_dt=rel, country="USD") is None
    monkeypatch.setattr(fred, "_get_observations", obs("2026-09-17"))   # new target effective
    assert fred.fetch_actual("Federal Funds Rate", "k", release_dt=rel, country="USD") is not None
