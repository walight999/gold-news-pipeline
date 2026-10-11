"""Released-number tweets (src/x_release.py, 2026-10-11)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from src import x_release as xr
from src import tweet_writer as tw

UTC = timezone.utc
REL = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)          # 19:30 ICT
NOW = datetime(2026, 10, 2, 12, 32, tzinfo=UTC)
TAGS = "#ทองวันนี้ #ข่าวทอง #เฟด #ทองคำ"


def ev(title="Non-Farm Employment Change", country="USD", impact="High",
       forecast="180K", previous="150K", dt=REL):
    return SimpleNamespace(title=title, country=country, impact=impact,
                           forecast=forecast, previous=previous, dt_utc=dt)


class FakeStore:
    def __init__(self, rows=None, fail_read=False):
        self.rows = list(rows or [])
        self.fail_read = fail_read
        self.appended = []

    def read_feed(self, tab):
        if self.fail_read:
            raise RuntimeError("sheets down")
        return [], [dict(r) for r in self.rows]

    def append_feed(self, tab, headers, rows):
        self.appended += rows


def test_single_print_tweet_has_facts_no_direction_call():
    t = xr.build_tweet([{"country": "USD", "title": "Non-Farm Employment Change",
                         "actual": "250K", "forecast": "180K", "previous": "150K",
                         "surprise": "beat"}],
                       {"usd": -12.44, "minutes": 2}, REL, tags=TAGS,
                       range_line="ปกติข่าวนี้ทองขยับ $9–$18 ใน 15 นาที")
    assert t.startswith("NFP (การจ้างงานนอกภาคเกษตร) สหรัฐฯ ออกแล้ว 250K")
    assert "คาด 180K · ก่อนหน้า 150K (สูงกว่าคาด)" in t
    assert "ทองหลังตัวเลขออก 2 นาที: -$12.4" in t
    assert t.endswith(TAGS) and len(t) <= tw.TWEET_LIMIT
    for word in ("Bullish", "Bearish", "🟢", "🔴", "ทองจะ"):
        assert word not in t


def test_same_minute_prints_one_tweet_with_ict_time():
    prints = [{"country": "USD", "title": t, "actual": a, "forecast": f, "previous": "",
               "surprise": s}
              for t, a, f, s in (("CPI m/m", "0.4%", "0.3%", "beat"),
                                 ("Core CPI m/m", "0.3%", "0.3%", "in-line"),
                                 ("CPI y/y", "3.1%", "3.0%", "beat"))]
    t = xr.build_tweet(prints, None, REL, tags=TAGS)
    assert t.splitlines()[0] == "ตัวเลขสหรัฐฯ 19:30 น. ออกแล้ว"
    assert "Core CPI m/m 0.3% คาด 0.3% ตามคาด" in t
    assert "ทองหลังตัวเลขออก" not in t           # no honest base → line hidden


def test_no_actual_no_tweet():
    assert xr.build_tweet([{"country": "USD", "title": "CPI m/m", "actual": "",
                            "forecast": "0.3%", "previous": "", "surprise": None}],
                          None, REL, tags=TAGS) is None


def test_overlong_drops_trailing_lines_not_mid_line():
    p = {"country": "USD", "title": "X" * 150, "actual": "1", "forecast": "2",
         "previous": "3", "surprise": "miss"}
    t = xr.build_tweet([p], {"usd": 5, "minutes": 3}, REL, tags=TAGS,
                       range_line="ปกติข่าวนี้ทองขยับ $9–$18 ใน 15 นาที " * 3)
    assert len(t) <= tw.TWEET_LIMIT and t.endswith(TAGS)
    assert "ปกติข่าวนี้" not in t


def _items(*evs, actual="250K"):
    return [{"ev": e, "actual": actual, "surprise": "beat",
             "xau_move": {"usd": 3.0, "minutes": 2}} for e in evs]


def test_post_filters_groups_dedups_and_logs(monkeypatch):
    monkeypatch.setattr(xr, "_typical_range_line", lambda c, t: None)
    posted = []
    store = FakeStore()
    items = _items(ev(), ev(title="Unemployment Rate", forecast="4.2%"),
                   ev(country="EUR", title="ECB thing"),            # wrong country
                   ev(impact="Medium", title="Minor"))              # wrong impact
    n = xr.post_release_minutes(store, items, poster=lambda t: posted.append(t) or "u1",
                                now=NOW, require_creds=False)
    assert n == 1 and len(posted) == 1                              # one per minute
    assert "อัตราว่างงาน" in posted[0] and "ECB" not in posted[0]
    assert store.appended[0][2] == "rel:2026-10-02T12:30Z"
    # Re-run with the logged row: already posted → no second tweet.
    store2 = FakeStore(rows=[{"fs_id": "rel:2026-10-02T12:30Z", "posted": "u1",
                              "ts_utc": "2026-10-02T12:32:00Z"}])
    assert xr.post_release_minutes(store2, items, poster=lambda t: 1 / 0,
                                   now=NOW, require_creds=False) == 0


def test_daily_cap(monkeypatch):
    monkeypatch.setattr(xr, "_typical_range_line", lambda c, t: None)
    rows = [{"fs_id": f"rel:2026-10-02T0{i}:30Z", "posted": "u",
             "ts_utc": f"2026-10-02T0{i}:31:00Z"} for i in range(6)]
    n = xr.post_release_minutes(FakeStore(rows), _items(ev()), poster=lambda t: "u",
                                now=NOW, require_creds=False)
    assert n == 0


def test_never_raises_on_store_or_poster_failure(monkeypatch):
    monkeypatch.setattr(xr, "_typical_range_line", lambda c, t: None)
    assert xr.post_release_minutes(FakeStore(fail_read=True), _items(ev()),
                                   poster=lambda t: "u", now=NOW, require_creds=False) == 0

    def boom(t):
        raise RuntimeError("402")
    store = FakeStore()
    assert xr.post_release_minutes(store, _items(ev()), poster=boom,
                                   now=NOW, require_creds=False) == 0
    assert store.appended == []                    # failed post not logged → retried


def test_disabled_or_no_creds_is_noop(monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert xr.post_release_minutes(FakeStore(), _items(ev()), poster=lambda t: 1 / 0,
                                   now=NOW) == 0
    assert xr.post_release_minutes(FakeStore(), _items(ev()), {"enabled": False},
                                   poster=lambda t: 1 / 0, now=NOW, require_creds=False) == 0
