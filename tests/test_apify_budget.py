"""Apify spend pacing (2026-09-27): fit the fixed monthly limit, never raise it."""
import json
from datetime import datetime, timezone

from src import apify_budget as ab
from src import apify_source as ap
from src import health
from src import main
from src import squawk_mirror as sq

UTC = timezone.utc
START = datetime(2026, 9, 4, tzinfo=UTC)
END = datetime(2026, 10, 4, tzinfo=UTC)          # 30-day cycle
MID = datetime(2026, 9, 19, tzinfo=UTC)          # exactly half-way
WEEKDAY = datetime(2026, 9, 23, 3, 0, tzinfo=UTC)  # Wed 10:00 ICT
CFG = {"enabled": True, "refresh_min": 30, "reserve_pct": 5, "slack_pct": 5,
       "tight_multiplier": 2, "weekend_multiplier": 3}


class _Store:
    def __init__(self, rows=None):
        self._s = {("source_state", r["source_id"]): r for r in (rows or [])}

    def get(self, tab, key):
        return self._s.get((tab, *key))

    def upsert(self, tab, row):
        self._s[(tab, row["source_id"])] = row


def _usage(usage, limit=29.0):
    return {"usage": usage, "limit": limit,
            "cycle_start": "2026-09-04T00:00:00.000Z",
            "cycle_end": "2026-10-03T23:59:59.999Z"}


# ---------------- budget_level (pure) ----------------

def test_on_pace_is_ok():
    # half-way through, pace = 29*0.95*0.5 = 13.78; 13 spent → ok
    assert ab.budget_level(13.0, 29.0, START, END, MID) == ab.OK


def test_ahead_of_pace_is_tight():
    # 13.78 pace + 1.45 slack = 15.23 → 16 is over
    assert ab.budget_level(16.0, 29.0, START, END, MID) == ab.TIGHT


def test_at_limit_is_exhausted():
    assert ab.budget_level(29.01, 29.0, START, END, MID) == ab.EXHAUSTED
    assert ab.budget_level(28.8, 29.0, START, END, END) == ab.EXHAUSTED  # ≥99%


def test_cycle_start_small_spend_is_ok_and_bad_limits_are_ok():
    assert ab.budget_level(1.0, 29.0, START, END, START) == ab.OK   # within slack
    assert ab.budget_level(5.0, 0.0, START, END, MID) == ab.OK      # no limit set
    assert ab.budget_level(5.0, 29.0, END, START, MID) == ab.OK     # bad cycle


# ---------------- weekend window ----------------

def test_weekend_closed_window_ict():
    assert not ab.is_weekend_closed(datetime(2026, 9, 25, 21, 0, tzinfo=UTC))  # Sat 04:00 ICT
    assert ab.is_weekend_closed(datetime(2026, 9, 25, 22, 0, tzinfo=UTC))      # Sat 05:00 ICT
    assert ab.is_weekend_closed(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))      # Sun
    assert ab.is_weekend_closed(datetime(2026, 9, 27, 21, 59, tzinfo=UTC))     # Mon 04:59 ICT
    assert not ab.is_weekend_closed(datetime(2026, 9, 27, 22, 0, tzinfo=UTC))  # Mon 05:00 ICT


def test_interval_multiplier_compounds():
    sat = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)
    assert ab.interval_multiplier(ab.OK, CFG, WEEKDAY) == 1.0
    assert ab.interval_multiplier(ab.TIGHT, CFG, WEEKDAY) == 2.0
    assert ab.interval_multiplier(ab.OK, CFG, sat) == 3.0
    assert ab.interval_multiplier(ab.TIGHT, CFG, sat) == 6.0
    assert ab.interval_multiplier(ab.TIGHT, {}, sat) == 1.0   # disabled ⇒ legacy


# ---------------- current_level caching ----------------

def test_current_level_refreshes_then_caches():
    calls = []

    def fetch(token):
        calls.append(token)
        return _usage(29.01)

    st = _Store()
    assert ab.current_level(st, "tok", CFG, now=MID, fetch=fetch) == ab.EXHAUSTED
    # within refresh_min → cached, no second API read
    assert ab.current_level(st, "tok", CFG, now=MID, fetch=fetch) == ab.EXHAUSTED
    assert len(calls) == 1
    level, blob = ab.read_cached(st)
    assert level == ab.EXHAUSTED and blob["limit"] == 29.0


def test_current_level_keeps_cache_when_read_fails():
    st = _Store([{"source_id": ab.STATE_KEY, "last_status": ab.TIGHT,
                  "last_attempt_ts": "2026-09-01T00:00:00Z", "items_last_hour": "{}"}])
    assert ab.current_level(st, "tok", CFG, now=MID, fetch=lambda t: None) == ab.TIGHT


def test_current_level_disabled_never_calls_api():
    def boom(token):
        raise AssertionError("disabled pacing must not hit the API")
    assert ab.current_level(_Store(), "tok", {"enabled": False}, now=MID, fetch=boom) == ab.OK


# ---------------- _collect_apify_entries wiring ----------------

def _x_cfg(extra=None):
    cfg = {"x_accounts": {"enabled": True, "handles": ["DeItaone", "FirstSquawk"],
                          "min_interval_min": 12, "since_minutes": 14},
           "apify_budget": dict(CFG)}
    cfg.update(extra or {})
    return cfg


def test_exhausted_budget_skips_every_actor(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.setattr(ab, "current_level", lambda *a, **k: ab.EXHAUSTED)

    def boom(*a, **k):
        raise AssertionError("no Apify call when the budget is exhausted")
    monkeypatch.setattr(ap, "fetch_tweets", boom)
    monkeypatch.setattr(ap, "fetch_truth_social", boom)
    meta = {}
    assert main._collect_apify_entries(_Store(), _x_cfg(), "cron", meta) == []
    assert meta["budget_level"] == ab.EXHAUSTED
    assert not meta.get("x_scraped")


def test_tight_budget_stretches_interval_and_lookback(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok")
    monkeypatch.setattr(ab, "current_level", lambda *a, **k: ab.TIGHT)
    monkeypatch.setattr(ab, "is_weekend_closed", lambda now: False)
    seen = {}

    def fake(token, handles, since_minutes=0, max_per_handle=8, tier=2):
        seen["since"] = since_minutes
        return []
    monkeypatch.setattr(ap, "fetch_tweets", fake)
    # last X scrape 20 min ago: due at 12 min normally, NOT due at 24 (tight ×2)
    ts = (main.now_utc().replace(microsecond=0)).timestamp() - 20 * 60
    last = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = _Store([{"source_id": "_apify", "last_attempt_ts": last}])
    meta = {}
    main._collect_apify_entries(st, _x_cfg(), "cron", meta)
    assert "since" not in seen and not meta.get("x_scraped")
    # fresh store → due; lookback widened to the stretched interval + 2
    main._collect_apify_entries(_Store(), _x_cfg(), "cron", meta)
    assert seen["since"] == 26 and meta["x_scraped"]


# ---------------- squawk reuses the shared scrape ----------------

class _FeedStore:
    def __init__(self):
        self.rows = []

    def read_feed(self, tab):
        return sq.MIRROR_HEADERS, list(self.rows)

    def append_feed(self, tab, headers, rows):
        for r in rows:
            self.rows.append(dict(zip(headers, r)))


def _fs(fid, text):
    return {"title": text, "url": f"https://x.com/FirstSquawk/status/{fid}",
            "published_ts": WEEKDAY, "source_id": "x_firstsquawk"}


def test_mirror_uses_given_entries_without_scraping(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("shared entries ⇒ no own Apify scrape")
    monkeypatch.setattr(ap, "fetch_tweets", boom)
    posted = []
    n = sq.mirror(_FeedStore(), token="tok", cfg={"cap_per_day": 20, "max_items": 5},
                  composer=lambda **kw: "ทอง " + kw["en_title"][:20],
                  poster=lambda t: posted.append(t) or "https://x.com/i/1",
                  now=WEEKDAY, entries=[_fs(1, "FED RAISES RATES 25BPS")])
    assert n == 1 and len(posted) == 1


def test_mirror_per_run_limit(monkeypatch):
    entries = [_fs(i, f"GOLD HEADLINE NUMBER {i} ABOUT FED POLICY SHIFT {i*7}") for i in range(1, 9)]
    # make them distinct enough for the near-dup guard
    entries = [dict(e, title=t) for e, t in zip(entries, [
        "FED RAISES RATES 25BPS", "GOLD HITS RECORD HIGH", "IRAN MISSILE STRIKE REPORTED",
        "US 10-YEAR YIELD JUMPS", "ECB LAGARDE SIGNALS PAUSE", "OPEC CUTS OUTPUT",
        "DOLLAR INDEX SLIDES", "BOJ HIKES TO ONE PERCENT"])]
    n = sq.mirror(_FeedStore(), token="tok", cfg={"cap_per_day": 20, "max_items": 3},
                  composer=lambda **kw: "ทอง " + kw["en_title"][:20],
                  poster=lambda t: "https://x.com/i/1", now=WEEKDAY, entries=entries)
    assert n == 3


# ---------------- watchdog alert ----------------

def test_watchdog_alerts_on_exhausted_and_tight():
    blob = json.dumps(_usage(29.01))
    st = _Store([{"source_id": ab.STATE_KEY, "last_status": ab.EXHAUSTED, "items_last_hour": blob}])
    [(wt, msg)] = health.check_apify_budget(st)
    assert wt == "apify_budget_exhausted" and "$29.01 / $29.00" in msg and "2026-10-03" in msg
    assert health.is_critical_warning(wt)
    st = _Store([{"source_id": ab.STATE_KEY, "last_status": ab.TIGHT, "items_last_hour": blob}])
    assert health.check_apify_budget(st)[0][0] == "apify_budget_high"
    assert not health.is_critical_warning("apify_budget_high")
    assert health.check_apify_budget(_Store()) == []
