"""Growth phase 2: Gold Week Ahead email (newsletter) + X CTA (2026-10-12)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from src import newsletter as nl
from src import x_cta

UTC = timezone.utc
ICT_OFF = 7


def ev(title, dt, country="USD", impact="High", forecast="180K", previous="150K"):
    from datetime import timedelta
    return SimpleNamespace(title=title, country=country, impact=impact, forecast=forecast,
                           previous=previous, dt_utc=dt,
                           dt_ict=(dt + timedelta(hours=ICT_OFF)).replace(tzinfo=None))


class Store:
    def __init__(self):
        self.rows = {}

    def get(self, tab, key):
        return self.rows.get((tab,) + key)

    def upsert(self, tab, row):
        self.rows[(tab, row["event_id"], row["route_type"])] = row


EVENTS = [
    ev("Non-Farm Employment Change", datetime(2026, 10, 16, 12, 30, tzinfo=UTC)),
    ev("Unemployment Rate", datetime(2026, 10, 16, 12, 30, tzinfo=UTC), forecast="4.2%", previous="4.1%"),
    ev("CPI m/m", datetime(2026, 10, 14, 12, 30, tzinfo=UTC), forecast="0.3%", previous="0.2%"),
    ev("German ZEW", datetime(2026, 10, 13, 9, 0, tzinfo=UTC), country="EUR"),
    ev("Minor thing", datetime(2026, 10, 13, 14, 0, tzinfo=UTC), impact="Medium", forecast=""),
]


def test_build_usd_only_sorted_with_thai_days_and_footer(monkeypatch):
    monkeypatch.setattr(nl, "_range", lambda e: "$16–$34")
    m = nl.build(EVENTS, "12/10/26-16/10/26")
    assert "German ZEW" not in m["html"]
    t = m["text"]
    assert t.index("วันอังคารที่ 13/10") < t.index("วันพุธที่ 14/10") < t.index("วันศุกร์ที่ 16/10")
    assert "19:30 NFP (การจ้างงานนอกภาคเกษตร) คาด 180K / ก่อนหน้า 150K (ทองมักขยับ $16–$34 ใน 15 นาที)" in t
    assert "Minor thing คาด - / ก่อนหน้า 150K" in t and "Minor thing คาด - / ก่อนหน้า 150K (" not in t
    for part in (m["html"], m["text"]):
        assert "{{{RESEND_UNSUBSCRIBE_URL}}}" in part and "ไม่ใช่คำแนะนำการลงทุน" in part
        assert "—" not in part
    assert "<b>CPI m/m</b>" in m["html"]


def test_build_none_without_usd():
    assert nl.build([EVENTS[3]], "x") is None


def test_send_env_gated_idempotent_and_failure_retries(monkeypatch):
    monkeypatch.setattr(nl, "_range", lambda e: "")
    for k in ("RESEND_API_KEY", "RESEND_SEGMENT_ID", "NEWSLETTER_FROM"):
        monkeypatch.delenv(k, raising=False)
    s = Store()
    assert nl.send_week_ahead(s, EVENTS, "2026-10-12", "w", http_post=lambda *a, **k: 1 / 0) is False
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("RESEND_SEGMENT_ID", "seg")
    monkeypatch.setenv("NEWSLETTER_FROM", "Tradetongkam <news@finisit.app>")
    calls = []

    def bad(url, **kw):
        calls.append(kw["json"])
        return SimpleNamespace(status_code=500, text="down")
    assert nl.send_week_ahead(s, EVENTS, "2026-10-12", "w", http_post=bad) is False
    assert not nl.already_sent(s, "2026-10-12")                    # retried next slot

    def ok(url, **kw):
        calls.append(kw["json"])
        return SimpleNamespace(status_code=200, text='{"id":"b1"}')
    assert nl.send_week_ahead(s, EVENTS, "2026-10-12", "w", http_post=ok) is True
    assert calls[-1]["segment_id"] == "seg" and calls[-1]["send"] is True
    assert nl.send_week_ahead(s, EVENTS, "2026-10-12", "w", http_post=ok) is False
    assert len(calls) == 2


def test_send_never_raises(monkeypatch):
    monkeypatch.setattr(nl, "build", lambda *a: 1 / 0)
    for k in ("RESEND_API_KEY", "RESEND_SEGMENT_ID", "NEWSLETTER_FROM"):
        monkeypatch.setenv(k, "x")
    assert nl.send_week_ahead(Store(), EVENTS, "m", "w") is False


MON_10 = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)     # Mon 10:00 ICT


def test_cta_posts_once_per_day_in_window():
    s, posted = Store(), []
    poster = lambda t: posted.append(t) or "https://x.com/i/1"
    assert x_cta.maybe_post(s, None, poster=poster, now=MON_10, require_creds=False)
    assert "t.me/FinisitNews_bot" in posted[0] and posted[0].endswith("#ทองวันนี้ #ทองคำ")
    assert "—" not in posted[0]
    assert not x_cta.maybe_post(s, None, poster=poster, now=MON_10, require_creds=False)
    assert len(posted) == 1


def test_cta_skips_wrong_day_hour_disabled():
    p = lambda t: 1 / 0
    tue = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
    early = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)   # Mon 07:30 ICT
    for now, cfg in ((tue, None), (early, None), (MON_10, {"enabled": False})):
        assert not x_cta.maybe_post(Store(), cfg, poster=p, now=now, require_creds=False)


def test_cta_alternates_to_email_only_when_signup_set():
    cfg = {**x_cta.DEFAULTS, "signup_url": "https://finisit.app/gold"}
    texts = {x_cta.pick_text(cfg, datetime(2026, 10, d, 3, 0, tzinfo=UTC)) for d in (12, 15, 19, 22)}
    assert any("finisit.app/gold" in t for t in texts) and any("t.me/" in t for t in texts)
    no_signup = {x_cta.pick_text(x_cta.DEFAULTS, datetime(2026, 10, d, 3, 0, tzinfo=UTC))
                 for d in (12, 15, 19, 22)}
    assert all("t.me/" in t for t in no_signup)


def test_cta_failed_post_not_marked():
    s = Store()

    def boom(t):
        raise RuntimeError("402")
    assert not x_cta.maybe_post(s, None, poster=boom, now=MON_10, require_creds=False)
    assert s.rows == {}
