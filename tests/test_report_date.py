"""EOD reports guard on the day being reported, not the wall clock (Friday
recap/scorecard were skipped once the throttled run slipped into Saturday ICT)."""
from __future__ import annotations

from datetime import date, datetime

from src.main import _report_date_ict
from src.utils_time import ICT


def test_evening_run_reports_today():
    assert _report_date_ict(datetime(2026, 9, 25, 23, 45, tzinfo=ICT)) == date(2026, 9, 25)


def test_friday_run_slipped_into_saturday_still_reports_friday():
    d = _report_date_ict(datetime(2026, 9, 26, 3, 10, tzinfo=ICT))
    assert d == date(2026, 9, 25) and d.weekday() == 4


def test_monday_early_run_reports_sunday_which_is_skipped():
    d = _report_date_ict(datetime(2026, 9, 28, 1, 0, tzinfo=ICT))
    assert d.weekday() == 6


def test_saturday_afternoon_reports_saturday():
    assert _report_date_ict(datetime(2026, 9, 26, 14, 0, tzinfo=ICT)).weekday() == 5
