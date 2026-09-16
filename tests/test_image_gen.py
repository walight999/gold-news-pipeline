from src import image_gen as ig


class _Ev:
    def __init__(self, hhmm, country, title, impact="High"):
        self.hhmm_ict = hhmm
        self.country = country
        self.title = title
        self.impact = impact


def test_calendar_prompt_includes_events_and_brand_guardrail():
    events = [_Ev("19:30", "USD", "CPI y/y"), _Ev("01:00", "USD", "FOMC Statement")]
    p = ig.calendar_artwork_prompt(events, "16 Sep 2026")
    assert "16 Sep 2026" in p
    assert "CPI y/y" in p and "FOMC Statement" in p
    # brand guardrail present — no AI faces (the no-ai-slop rule)
    assert "NO realistic human faces" in p


def test_calendar_prompt_handles_empty_and_dicts():
    assert "no major economic releases" in ig.calendar_artwork_prompt([], "16 Sep 2026")
    dict_ev = [{"hhmm_ict": "20:30", "country": "USD", "title": "NFP"}]
    assert "NFP" in ig.calendar_artwork_prompt(dict_ev, "16 Sep 2026")


def test_calendar_prompt_caps_events():
    events = [_Ev(f"{h:02d}:00", "USD", f"Event{h}") for h in range(10)]
    p = ig.calendar_artwork_prompt(events, "d", cap=3)
    assert "Event0" in p and "Event1" in p and "Event2" in p
    assert "Event5" not in p


def test_brief_prompt_includes_theme_and_guardrail():
    p = ig.brief_artwork_prompt("Fed วันชี้ชะตา yields แตะ 5%")
    assert "Fed วันชี้ชะตา" in p
    assert "NO realistic human faces" in p


def test_generate_none_on_empty_args():
    assert ig.generate("", api_key="k") is None
    assert ig.generate("a prompt", api_key="") is None
