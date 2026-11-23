"""X autopost circuit breaker + watchdog tripwires (2026-11-23).

The 2026-10-31 → 11-23 outage: X credits at -$0.05, every post rejected,
mirror_events logged and moved on — 3 silent weeks, Sonnet still composing.
"""
from datetime import datetime, timedelta, timezone

from src import health
from src import squawk_mirror as sq

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)   # Tue 10:00 ICT


class StateStore:
    """Feed tabs + a source_state table — enough for the breaker and watchdog."""
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.state: dict[str, dict] = {}

    def read_feed(self, tab):
        return sq.MIRROR_HEADERS, [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        for r in rows:
            self.rows.append(dict(zip(headers, r)))

    def get(self, table, key):
        return self.state.get(key[0])

    def upsert(self, table, row):
        self.state[row["source_id"]] = dict(row)


class Resp:
    def __init__(self, code):
        self.status_code = code


class HTTPErr(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.response = Resp(code)


def _cand(i):
    return {"event_id": f"e{i}", "en_title": f"Fed official speaks on topic number {i} today",
            "headline_th": "h", "body_th": "b", "impact_th": "i", "category": "Fed"}


def _counting_composer():
    calls = []

    def compose(**kw):
        calls.append(kw["en_title"])
        return "ทอง " + kw["en_title"][:30]
    return compose, calls


# -- classify_post_error --------------------------------------------------

def test_classify_credits_by_status_and_text():
    assert sq.classify_post_error(HTTPErr(402, "Payment Required")) == "credits"
    assert sq.classify_post_error(Exception("403 Forbidden: CreditsDepleted")) == "credits"


def test_classify_auth():
    assert sq.classify_post_error(HTTPErr(401, "Unauthorized")) == "auth"


def test_classify_transient_is_none():
    assert sq.classify_post_error(HTTPErr(503, "Service Unavailable")) is None
    assert sq.classify_post_error(HTTPErr(429, "Too Many Requests")) is None
    assert sq.classify_post_error(Exception("duplicate content")) is None


# -- breaker in mirror_events ----------------------------------------------

def test_credit_rejection_opens_breaker_and_stops_the_run():
    store = StateStore()
    compose, calls = _counting_composer()

    def poster(_):
        raise HTTPErr(402, "CreditsDepleted")
    n = sq.mirror_events(store, [_cand(1), _cand(2), _cand(3)], {},
                         composer=compose, poster=poster, now=NOW)
    assert n == 0
    assert len(calls) == 1                       # stopped after the first rejection
    st = sq.x_state(store)
    assert st["status"] == sq.X_BLOCKED and st["reason"] == "credits"
    assert store.rows == []                      # nothing logged as posted


def test_open_breaker_skips_composing_until_retry_after():
    store = StateStore()
    sq._write_x_state(store, NOW, sq.X_BLOCKED, reason="credits", since="x")
    compose, calls = _counting_composer()
    n = sq.mirror_events(store, [_cand(1)], {}, composer=compose,
                         poster=lambda t: "u", now=NOW + timedelta(hours=1))
    assert n == 0 and calls == []                # no Sonnet spend while blocked


def test_probe_after_retry_window_closes_breaker_on_success():
    store = StateStore()
    sq._write_x_state(store, NOW, sq.X_BLOCKED, reason="credits", since="x")
    compose, calls = _counting_composer()
    later = NOW + sq.X_BLOCK_RETRY + timedelta(minutes=1)
    n = sq.mirror_events(store, [_cand(1)], {}, composer=compose,
                         poster=lambda t: "https://x.com/i/web/status/1", now=later)
    assert n == 1
    assert sq.x_state(store)["status"] == sq.X_OK


def test_failed_probe_keeps_original_since():
    store = StateStore()
    sq._write_x_state(store, NOW, sq.X_BLOCKED, reason="credits", since="2026-10-31T00:00:00Z")

    def poster(_):
        raise HTTPErr(402, "CreditsDepleted")
    compose, _ = _counting_composer()
    sq.mirror_events(store, [_cand(1)], {}, composer=compose, poster=poster,
                     now=NOW + sq.X_BLOCK_RETRY + timedelta(minutes=1))
    assert sq.x_state(store)["since"] == "2026-10-31T00:00:00Z"


def test_transient_failure_does_not_open_breaker():
    store = StateStore()

    def poster(_):
        raise HTTPErr(503, "Service Unavailable")
    compose, calls = _counting_composer()
    sq.mirror_events(store, [_cand(1), _cand(2)], {}, composer=compose,
                     poster=poster, now=NOW)
    assert len(calls) == 2                       # kept trying the rest
    assert sq.x_state(store).get("status") != sq.X_BLOCKED


# -- watchdog --------------------------------------------------------------

def _posted_row(ts):
    return {"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "ts_ict": "", "fs_id": "ev:1",
            "fs_text": "t", "tweet_text": "x", "posted": "https://x.com/i/web/status/1"}


def test_watchdog_reports_blocked():
    store = StateStore()
    sq._write_x_state(store, NOW, sq.X_BLOCKED, reason="credits", since="2026-10-31T00:00:00Z")
    out = health.check_x_autopost(store, now=NOW)
    assert [w for w, _ in out] == ["x_post_blocked"]


def test_watchdog_stale_after_24_market_hours():
    store = StateStore([_posted_row(NOW - timedelta(hours=30))])   # Mon→Tue, all market
    assert [w for w, _ in health.check_x_autopost(store, now=NOW)] == ["x_post_stale"]


def test_watchdog_not_stale_across_a_weekend():
    # Last post Fri 22:00 ICT, now Mon 10:00 ICT: ~2h Fri + 10h Mon open = 12h.
    last = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    mon = datetime(2026, 11, 23, 3, 0, tzinfo=UTC)
    store = StateStore([_posted_row(last)])
    assert health.check_x_autopost(store, now=mon) == []


def test_watchdog_quiet_when_recent_post():
    store = StateStore([_posted_row(NOW - timedelta(hours=2))])
    assert health.check_x_autopost(store, now=NOW) == []
