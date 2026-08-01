"""FF HTML scraper — day-shift regression.

The Week-Ahead card shifted every event by a day whenever a day's breaker
row was missing/unparseable (the "Monday has no event" report). The parser
must source each day's date from the authoritative `calendar__date` cell on
the day's first row, not from the separate day-breaker spacer text.
"""
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src import ff_scraper


def _table(html: str):
    return BeautifulSoup(f'<table class="calendar__table">{html}</table>',
                         "lxml").find("table", class_="calendar__table")


REF = datetime(2026, 7, 18, tzinfo=timezone.utc)  # a Saturday


def _event_row(date_cell, time_cell, ccy, title, *, new_day, impact="red"):
    date_td = (f'<td class="calendar__date">{date_cell}</td>'
               if date_cell else '<td class="calendar__date"></td>')
    cls = "calendar__row calendar__row--new-day" if new_day else "calendar__row"
    return (
        f'<tr class="{cls}">'
        f'{date_td}'
        f'<td class="calendar__time">{time_cell}</td>'
        f'<td class="calendar__currency">{ccy}</td>'
        f'<td class="calendar__impact">'
        f'<span class="icon icon--ff-impact-{impact}"></span></td>'
        f'<td class="calendar__event">{title}</td>'
        f'<td class="calendar__forecast"></td>'
        f'<td class="calendar__previous"></td>'
        f'</tr>'
    )


def test_date_sourced_from_cell_survives_missing_breaker():
    """Sunday has one event; Monday's day-breaker text is EMPTY (the failure
    mode) but its date cell is populated. Monday's event must stay on Monday."""
    html = (
        '<tr class="calendar__row calendar__row--day-breaker">'
        '<td>SunJul 19</td></tr>'
        + _event_row("SunJul 19", "8:30am", "USD", "Sunday Print", new_day=True)
        # Broken/empty breaker for Monday — reproduces the shift trigger.
        + '<tr class="calendar__row calendar__row--day-breaker"><td></td></tr>'
        + _event_row("MonJul 20", "1:30pm", "USD", "Monday Print", new_day=True)
        + _event_row("", "3:00pm", "EUR", "Monday Print 2", new_day=False)
    )
    out = ff_scraper._parse_calendar_table(_table(html), REF)
    by_title = {e["title"]: e["date"][:10] for e in out}
    assert by_title["Sunday Print"] == "2026-07-19"
    assert by_title["Monday Print"] == "2026-07-20"     # NOT shifted to the 19th
    assert by_title["Monday Print 2"] == "2026-07-20"   # inherits Monday's date


def test_time_inherited_within_same_time_block():
    """Rows with an empty time cell share the previous row's clock time."""
    html = (
        _event_row("MonJul 20", "1:30pm", "GBP", "CPI y/y", new_day=True)
        + _event_row("", "", "GBP", "Core CPI y/y", new_day=False)
    )
    out = ff_scraper._parse_calendar_table(_table(html), REF)
    times = {e["title"]: e["date"][11:16] for e in out}
    assert times["CPI y/y"] == "13:30"
    assert times["Core CPI y/y"] == "13:30"


def test_all_day_row_does_not_inherit_a_clock_time():
    """An 'All Day' / 'Tentative' row must be dropped, not stamped with the
    previous timed row's clock (which produced phantom same-time events)."""
    html = (
        _event_row("MonJul 20", "8:30am", "USD", "Timed Print", new_day=True)
        + _event_row("", "All Day", "JPY", "Bank Holiday", new_day=False)
        + _event_row("", "Tentative", "CNY", "FDI", new_day=False)
    )
    out = ff_scraper._parse_calendar_table(_table(html), REF)
    titles = {e["title"] for e in out}
    assert "Timed Print" in titles
    assert "Bank Holiday" not in titles
    assert "FDI" not in titles


# ---------- geo-IP timezone drift (user report 2026-08-01) ----------
#
# FF renders anonymous visitors in their geo-IP timezone, not a fixed Eastern.
# When the runner geo-located to UTC-6 (Mountain) the Week-Ahead card showed
# every event 2h early (CHF 13:30→11:30, USD 21:00→19:00). The scraper now
# self-calibrates the display offset from FF's own header clock.

from datetime import timedelta   # noqa: E402
from src.calendar import parse_ff_payload   # noqa: E402


def test_detect_display_offset_from_header_clock():
    # FF header shows 03:06 while true UTC is 09:07 → page rendered in UTC-6.
    ref_utc = datetime(2026, 8, 1, 9, 7, tzinfo=timezone.utc)
    html = ('<th class="calendar__time">'
            '<a href="/timezone" title="Time Options" '
            'class="calendar__header-time">3:06am</a></th>')
    tz = ff_scraper._detect_display_offset(html, ref_utc)
    assert tz is not None
    assert tz.utcoffset(None) == timedelta(hours=-6)


def test_detect_display_offset_snaps_and_handles_bangkok():
    # Bangkok geo-IP: FF shows 4:07pm (16:07) while UTC is 09:06 → +7, and a
    # 1-min skew must snap cleanly to +07:00.
    ref_utc = datetime(2026, 8, 1, 9, 6, tzinfo=timezone.utc)
    html = '<span class="calendar__header-time">4:07pm</span>'
    tz = ff_scraper._detect_display_offset(html, ref_utc)
    assert tz.utcoffset(None) == timedelta(hours=7)


def test_detect_display_offset_none_when_no_clock():
    assert ff_scraper._detect_display_offset("<div>no clock here</div>",
                                             datetime.now(timezone.utc)) is None


def test_mountain_runner_does_not_shift_times():
    """The core regression: a UTC-6 (Mountain) runner scraping a USD 10:00am ET
    event must still yield 21:00 ICT, not 19:00. We simulate by parsing rows
    with the Mountain-rendered clock under the detected Mountain offset, then
    converting through the real pipeline (parse_ff_payload → dt_ict)."""
    mountain = timezone(timedelta(hours=-6))
    # A USD event truly at 10:00 ET (=08:00 MDT) renders as "8:00am" on a
    # Mountain-geo-IP page; a CHF event truly at 08:30 CEST (=00:30 MDT next
    # anchor) renders here as an explicit clock too. We feed the Mountain clock.
    html = (
        _event_row("FriJul 31", "8:00am", "USD", "USD Ten AM ET", new_day=True)
    )
    rows = ff_scraper._parse_calendar_table(_table(html), REF, disp_tz=mountain)
    events = parse_ff_payload(rows)
    usd = next(e for e in events if e.title == "USD Ten AM ET")
    # 08:00 MDT = 14:00 UTC = 21:00 ICT  ✅ (the value the user expected)
    assert usd.hhmm_ict == "21:00"

    # And the OLD buggy behavior (hardcoded ET on a Mountain clock) would have
    # produced 19:00 — assert the default-ET path still reproduces that, proving
    # the offset argument is what corrects it.
    rows_buggy = ff_scraper._parse_calendar_table(_table(html), REF)  # ET default
    usd_buggy = next(e for e in parse_ff_payload(rows_buggy)
                     if e.title == "USD Ten AM ET")
    assert usd_buggy.hhmm_ict == "19:00"
