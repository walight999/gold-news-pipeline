from datetime import datetime

from src.utils_time import ICT, within_digest_slot

SLOTS = ["05:30", "13:30", "21:30"]


def _ict(h, m):
    return datetime(2026, 6, 12, h, m, tzinfo=ICT)


def test_symmetric_default_window():
    # Exactly on slot
    assert within_digest_slot(SLOTS, 5, dt=_ict(13, 30)) == "13:30"
    # Within +/-5 min
    assert within_digest_slot(SLOTS, 5, dt=_ict(13, 33)) == "13:30"
    assert within_digest_slot(SLOTS, 5, dt=_ict(13, 26)) == "13:30"
    # Outside the tight window (the bug: throttled cron lands here -> missed)
    assert within_digest_slot(SLOTS, 5, dt=_ict(14, 30)) is None


def test_catch_up_window_fires_late_runs():
    # 1h after the slot, with a 4h catch-up -> still fires
    assert within_digest_slot(SLOTS, 5, dt=_ict(14, 30), catch_up_min=240) == "13:30"
    # 3h59m after -> still within catch-up
    assert within_digest_slot(SLOTS, 5, dt=_ict(17, 29), catch_up_min=240) == "13:30"
    # past the catch-up window -> None (next slot 21:30 not reached)
    assert within_digest_slot(SLOTS, 5, dt=_ict(18, 0), catch_up_min=240) is None


def test_catch_up_does_not_fire_before_slot_beyond_window():
    # 30 min BEFORE the slot is still outside the small pre-window
    assert within_digest_slot(SLOTS, 5, dt=_ict(13, 0), catch_up_min=240) is None
    # 4 min before -> within pre-window
    assert within_digest_slot(SLOTS, 5, dt=_ict(13, 26), catch_up_min=240) == "13:30"
