"""2026-10-13: the classify prompt states the gold-direction signs explicitly.

The read of 1,499 sent cards found 15% with a backwards or self-contradicting
FX→gold chain ("EUR แข็ง … กดดันทอง"), and two of the prompt's own few-shot
examples modelled exactly that hedge. The prompt now carries the rules, the
fixed examples, and TODAY so models stop writing stale years."""
from __future__ import annotations

from src import news_alert
from src.news_alert import _SYSTEM_PROMPT, _build_prompt


def test_prompt_has_gold_direction_rules():
    assert "GOLD DIRECTION RULES" in _SYSTEM_PROMPT
    assert "USD weaker → gold UP" in _SYSTEM_PROMPT
    assert "ผลต่อทองจำกัด" in _SYSTEM_PROMPT          # the non-USD escape hatch


def test_backwards_examples_removed():
    # the old examples ended a JPY/USD chain without a gold direction, or
    # hedged both ways — the exact pattern the cards copied
    assert "อาจหนุน JPY และกด USD ทางอ้อม\"" not in _SYSTEM_PROMPT
    assert "แต่ USD แข็งอาจจำกัด upside ทอง" not in _SYSTEM_PROMPT
    assert "hawkish BoJ ≠ bad for gold" in _SYSTEM_PROMPT


def test_today_is_injected(monkeypatch):
    from datetime import datetime, timezone, timedelta
    fixed = datetime(2026, 10, 13, 9, 0, tzinfo=timezone(timedelta(hours=7)))
    import src.utils_time as ut
    monkeypatch.setattr(ut, "now_ict", lambda: fixed)
    p = _build_prompt("Fed's Waller speaks", "", "x", "1.0")
    assert "TODAY: 2026-10-13" in p
    # the date lives only in the per-item INPUT block (the rules block above it
    # is the cached static prefix and must not change day to day)
    from src.news_alert import _prompt_parts
    static, user = _prompt_parts("Fed's Waller speaks", "", "x", "1.0")
    assert "TODAY: 2026-10-13" in user and "2026-10-13" not in static
    for ph in ("{today}", "{title}", "{summary}", "{source_id}", "{age_hours}"):
        assert ph not in p


def test_gemini_uses_the_same_prompt():
    # both providers render through _build_prompt — the rules reach Gemini too
    import inspect
    assert "_build_prompt(" in inspect.getsource(news_alert._classify_gemini_with_usage)
