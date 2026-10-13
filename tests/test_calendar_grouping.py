"""Same-minute calendar cards go out as ONE carousel push (1 quota unit per
recipient instead of N), and every card in a delivered group is ledgered."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import main as m
from src import store as store_mod


class _Line:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def push_flex(self, target, alt, contents):
        self.calls.append((alt, contents))
        return {"status": self.status, "results": [{"status": self.status}]}


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    return s


T0 = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)


def _item(key, dt, cal_row=None):
    return {"ev": SimpleNamespace(dt_utc=dt), "sent_key": key,
            "bubble": {"type": "bubble", "k": key}, "alt": f"alt {key}",
            "on_delivered": cal_row}


def _run(pending, line, store, monkeypatch):
    monkeypatch.setattr(m, "is_quiet_hours_ict", lambda cfg: False)
    monkeypatch.setattr(m, "record_line_outcome", lambda *a, **k: None)
    return m._push_calendar_groups(line, "C1", pending, {}, store,
                                   route_type="calendar_post", priority=0)


def test_same_minute_events_share_one_carousel(monkeypatch):
    line, s = _Line(), _store()
    pending = [_item("a", T0, {"event_id": "cal:a", "predicted_dir": "bull"}),
               _item("b", T0), _item("c", T0),
               _item("d", T0 + timedelta(minutes=90))]
    n = _run(pending, line, s, monkeypatch)
    assert n == 4
    assert len(line.calls) == 2
    alt, contents = line.calls[0]
    assert contents["type"] == "carousel" and len(contents["contents"]) == 3
    assert alt == "alt a +2"
    assert line.calls[1][1]["type"] == "bubble"
    for k in "abcd":
        assert s.get("sent_log", (k, "calendar_post"))
    assert s.get("calibration_log", ("cal:a",))


def test_failed_push_ledgers_nothing(monkeypatch):
    line, s = _Line(status=429), _store()
    n = _run([_item("a", T0, {"event_id": "cal:a"}), _item("b", T0)], line, s, monkeypatch)
    assert n == 0
    assert not s.all_rows("sent_log") and not s.all_rows("calibration_log")


def test_more_than_12_same_minute_splits(monkeypatch):
    line, s = _Line(), _store()
    n = _run([_item(str(i), T0) for i in range(14)], line, s, monkeypatch)
    assert n == 14
    assert [len(c["contents"]) if c["type"] == "carousel" else 1 for _, c in line.calls] == [12, 2]


def test_delivered_ungraded_card_logs_its_reason(monkeypatch, caplog):
    # 2026-10-09: two Released cards went out, the scorecard graded 0 rows and
    # no log said why. A delivered card with no calibration row now names it.
    line, s = _Line(), _store()
    ev = SimpleNamespace(dt_utc=T0, country="USD", title="UoM Sentiment")
    item = {"ev": ev, "sent_key": "u", "bubble": {"type": "bubble"}, "alt": "alt u",
            "on_delivered": None, "ungraded": "no numeric forecast"}
    with caplog.at_level("INFO", logger="gold-news"):
        assert _run([item], line, s, monkeypatch) == 1
    assert "not graded (no numeric forecast): USD UoM Sentiment" in caplog.text
    assert not s.all_rows("calibration_log")


def test_failed_push_does_not_log_ungraded(monkeypatch, caplog):
    line, s = _Line(status=429), _store()
    ev = SimpleNamespace(dt_utc=T0, country="USD", title="X")
    item = {"ev": ev, "sent_key": "u", "bubble": {"type": "bubble"}, "alt": "a",
            "on_delivered": None, "ungraded": "no actual"}
    with caplog.at_level("INFO", logger="gold-news"):
        _run([item], line, s, monkeypatch)
    assert "not graded" not in caplog.text
