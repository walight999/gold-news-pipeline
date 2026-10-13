"""2026-10-13: story-level dedup for LINE (12% of 1,499 sent cards repeated a
story already in the group under different wording)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import digest
from src import store as store_mod

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_near_identical_headline_is_same_story():
    a = "Iran says Strait of Hormuz is under its control and management"
    b = "IRAN SAYS STRAIT OF HORMUZ IS UNDER ITS CONTROL AND MANAGEMENT - STATE TV"
    assert digest.same_story(b, [a]) == "near-dup"
    assert digest.same_story(b, [a], near_only=True) == "near-dup"


def test_reworded_same_incident_caught_by_digest_rule_only():
    a = "UKMTO WARNS INBOUND LPG TANKER STRUCK BY PROJECTILE IN STRAIT OF HORMUZ"
    b = "UKMTO: tanker struck by unknown projectile in Strait of Hormuz, engine-room fire"
    assert (digest.same_story(b, [a]) or "").startswith("story:")
    assert digest.same_story(b, [a], near_only=True) is None


def test_same_speaker_same_subject_is_same_story():
    a = "Bank of Japan may need to raise rates quickly if inflation rises, board member Masu says"
    b = "Bank of Japan board member Masu: underlying inflation remains under 2% but close to that level"
    assert (digest.same_story(b, [a]) or "").startswith("story:")
    # breaking/alert only drop near-identical headlines
    assert digest.same_story(b, [a], near_only=True) is None


def test_shared_name_alone_is_not_enough():
    # Trump/Iran span unrelated stories all day — needs real word overlap too
    a = "Trump says tariffs on Chinese goods will rise next month"
    b = "Iran says it has rebuilt its missile capability, Trump warns of strikes"
    assert digest.same_story(b, [a]) is None


def test_unrelated_and_empty():
    assert digest.same_story("US CPI rises 0.4% in September", ["ECB's Lane on energy prices"]) is None
    assert digest.same_story("", ["anything"]) is None
    assert digest.same_story("Fed's Waller speaks on rates", []) is None


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recent_sent_titles_window_and_routes():
    s = _store()
    for eid, title in (("e1", "Fed's Kashkari: more hikes may be needed"),
                       ("e2", "Old story"), ("e3", "Calendar thing")):
        s.upsert("event_state", {"event_id": eid, "title": title})
    s.upsert("sent_log", {"event_id": "e1", "route_type": "digest", "sent_ts": _iso(NOW - timedelta(hours=2))})
    s.upsert("sent_log", {"event_id": "e2", "route_type": "breaking", "sent_ts": _iso(NOW - timedelta(hours=9))})
    s.upsert("sent_log", {"event_id": "e3", "route_type": "calendar_post", "sent_ts": _iso(NOW - timedelta(hours=1))})
    s.upsert("sent_log", {"event_id": "gone", "route_type": "alert", "sent_ts": _iso(NOW)})   # no event_state row
    assert digest.recent_sent_titles(s, NOW, 6) == ["Fed's Kashkari: more hikes may be needed"]


def test_replay_precision_on_kashkari_burst():
    # Same-day burst from one speaker: the 2nd+ cards should be caught
    sent = ["Fed's Kashkari says more rate hikes may be needed to tame inflation"]
    again = "Kashkari: more rate hikes may be needed if inflation stays high"
    assert digest.same_story(again, sent) is not None
