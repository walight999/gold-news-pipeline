"""Speech / statement watcher: planning, quote capture, staging, card, grading."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import speech_watch as sw
from src import store as store_mod
from src.calendar import CalEvent

CFG = sw.cfg_from({})
T0 = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)


def _ev(title, country="USD", impact="High", dt=T0):
    return CalEvent(event_id="x", title=title, country=country, impact=impact,
                    forecast="", previous="", dt_utc=dt)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


# ---- which events, which words ----

@pytest.mark.parametrize("title,ccy,expected", [
    ("President Trump Speaks", "USD", True),
    ("FOMC Press Conference", "USD", True),
    ("Federal Funds Rate", "USD", True),          # statement-driven
    ("ECB President Lagarde Speaks", "EUR", True),
    ("Core CPI m/m", "USD", False),
    ("RBA Gov Bullock Speaks", "AUD", False),     # currency not watched
])
def test_is_speech_event(title, ccy, expected):
    assert sw.is_speech_event(_ev(title, ccy), CFG) is expected


def test_speaker_keywords():
    assert sw.speaker_keywords(_ev("President Trump Speaks"), CFG) == ["TRUMP"]
    k = sw.speaker_keywords(_ev("Fed Chairman Warsh Testifies"), CFG)
    assert k[0] == "WARSH" and "FOMC" in k
    k = sw.speaker_keywords(_ev("FOMC Press Conference"), CFG)
    assert "FOMC" in k and "WARSH" in k            # fed_chair added on FOMC events
    assert "LAGARDE" in sw.speaker_keywords(_ev("ECB Press Conference", "EUR"), CFG)
    assert "MEMBER" not in sw.speaker_keywords(_ev("FOMC Member Waller Speaks"), CFG)


def test_fomc_day_merges_into_one_window():
    evs = [_ev("Federal Funds Rate"), _ev("FOMC Statement"),
           _ev("FOMC Press Conference", dt=T0 + timedelta(minutes=30)),
           _ev("President Trump Speaks", dt=T0 + timedelta(minutes=10))]
    ws = sw.plan_windows(evs, T0, CFG)
    fomc = [w for w in ws if "FOMC" in w["title"]]
    assert len(fomc) == 1
    assert fomc[0]["title"] == "Federal Funds Rate + FOMC Statement + FOMC Press Conference"
    assert fomc[0]["end"] == (T0 + timedelta(minutes=120)).isoformat()   # presser 90 min
    assert any(w["title"] == "President Trump Speaks" for w in ws)       # political stays separate


def test_windows_only_near_now():
    assert sw.plan_windows([_ev("President Trump Speaks")], T0 - timedelta(hours=2), CFG) == []
    assert sw.plan_windows([_ev("President Trump Speaks")], T0 - timedelta(minutes=10), CFG)


# ---- quote capture ----

def test_is_quote():
    k = ["WARSH", "FOMC", "FED "]
    assert sw.is_quote("WARSH: INFLATION STILL TOO HIGH", "x_firstsquawk", k)
    assert sw.is_quote("Fed's Warsh says more hikes possible", "forexlive", k)
    assert not sw.is_quote("Three words from Kevin Warsh have Wall Street wondering", "cnbc", k)
    assert not sw.is_quote("GOLD RISES", "x_firstsquawk", k)


def _item(title, src="x_firstsquawk", ts=T0 + timedelta(minutes=5)):
    return SimpleNamespace(title=title, source_id=src, published_ts=ts, first_seen_ts=ts)


def test_collect_buffers_quotes_once():
    s = _store()
    sw.publish_windows(s, sw.plan_windows([_ev("FOMC Press Conference")], T0, CFG))
    items = [_item("WARSH: WE ARE PREPARED TO HIKE AGAIN"), _item("OIL FALLS 2%"),
             _item("WARSH: WE ARE PREPARED TO HIKE AGAIN")]
    assert sw.collect(s, items, T0 + timedelta(minutes=6)) == 1
    assert sw.collect(s, items, T0 + timedelta(minutes=11)) == 0          # dedup across runs
    wid = sw.stored_windows(s)[0]["id"]
    assert [q["t"] for q in sw.load_buffer(s, wid)] == ["WARSH: WE ARE PREPARED TO HIKE AGAIN"]


def test_collect_ignores_closed_window():
    s = _store()
    sw.publish_windows(s, sw.plan_windows([_ev("President Trump Speaks")], T0, CFG))
    assert sw.collect(s, [_item("TRUMP SAYS X", ts=T0 + timedelta(hours=5))], T0 + timedelta(hours=5)) == 0


# ---- staging ----

def test_due_stages():
    w = {"start": T0.isoformat(), "end": (T0 + timedelta(minutes=60)).isoformat()}
    never = lambda st: False   # noqa: E731
    assert sw.due_stages(w, T0 + timedelta(minutes=10), 5, CFG, never) == []
    assert sw.due_stages(w, T0 + timedelta(minutes=25), 2, CFG, never) == []        # too few quotes
    assert sw.due_stages(w, T0 + timedelta(minutes=25), 3, CFG, never) == ["mid"]
    assert sw.due_stages(w, T0 + timedelta(minutes=65), 3, CFG, never) == ["end"]
    assert sw.due_stages(w, T0 + timedelta(minutes=65), 3, CFG, lambda st: True) == []


# ---- end to end with a stubbed model ----

ANALYSIS = {"tone": "hawkish", "summary_th": "Fed ส่งสัญญาณพร้อมขึ้นดอกเบี้ยต่อ",
            "key_points_th": ["เงินเฟ้อยังสูงเกินเป้า", "ไม่รีบลดดอกเบี้ย"],
            "gold_bias": "bearish", "gold_confidence": "medium",
            "why_th": "ดอกเบี้ยจริงสูงขึ้นกดดันทอง"}


def test_run_sends_final_card_and_logs_graded_call(monkeypatch):
    s = _store()
    sw.publish_windows(s, sw.plan_windows([_ev("FOMC Member Waller Speaks")], T0, CFG))
    wid = sw.stored_windows(s)[0]["id"]
    sw.collect(s, [_item(f"FED'S WALLER: POINT {i}") for i in range(4)], T0 + timedelta(minutes=5))
    monkeypatch.setattr(sw, "_llm", lambda prompt: json.dumps(ANALYSIS, ensure_ascii=False))
    monkeypatch.setattr("src.price_feed.xau_return_pct", lambda *a, **k: -0.42)
    pushed = []

    def push(alt, bubble):
        pushed.append((alt, bubble))
        return {"status": 200}
    now = T0 + timedelta(minutes=65)
    n = sw.run(s, None, "C1", {}, now, push=push, delivered=lambda r: r["status"] == 200)
    assert n == 1
    alt, bubble = pushed[0]
    assert alt.startswith("🎙️ สรุปถ้อยแถลง")
    assert bubble["header"]["contents"]  # renders
    assert s.get("sent_log", (f"speech_end:{wid}", "speech"))
    row = s.get("calibration_log", (f"speech:{wid}",))
    assert row["predicted_dir"] == "bear" and row["routed_as"] == "speech"
    assert row["first_seen_ts"].startswith("2026-09-16T19:05")   # graded from the CALL time
    # idempotent
    assert sw.run(s, None, "C1", {}, now + timedelta(minutes=10), push=push,
                  delivered=lambda r: True) == 0


def test_run_silent_speech_marks_done_without_card(monkeypatch):
    s = _store()
    sw.publish_windows(s, sw.plan_windows([_ev("President Trump Speaks")], T0, CFG))
    monkeypatch.setattr(sw, "_llm", lambda p: pytest.fail("no model call for an empty buffer"))
    pushed = []
    n = sw.run(s, None, "C1", {}, T0 + timedelta(minutes=65),
               push=lambda a, b: pushed.append(a) or {"status": 200}, delivered=lambda r: True)
    assert n == 0 and not pushed


def test_analyze_rejects_cjk(monkeypatch):
    bad = dict(ANALYSIS, summary_th="Fed 利上げ")
    monkeypatch.setattr(sw, "_llm", lambda p: json.dumps(bad, ensure_ascii=False))
    w = {"id": "w", "title": "FOMC Press Conference", "country": "USD"}
    assert sw.analyze(w, [{"t": "WARSH: X", "ts": T0.isoformat()}], "end", None) is None
