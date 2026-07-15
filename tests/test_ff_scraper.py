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
