"""Synthesize + push 4 representative bubble types to LINE for visual
verification of the latest Batch K + M designs.

Loads creds via .env + creds.json (no Sheet writes — we bypass Store).
Each push is labelled "[DEMO]" so the user knows it's a test.

Run:  python tests/demo_bubbles.py
"""
from __future__ import annotations

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

# Force UTF-8 console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Load .env then patch GSHEET_CREDS from creds.json
env = pathlib.Path(__file__).resolve().parents[1] / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
creds_p = pathlib.Path(__file__).resolve().parents[1] / "creds.json"
if creds_p.exists():
    os.environ["GSHEET_CREDS"] = creds_p.read_text(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.calendar import CalEvent, gold_impact_directional, forecast_vs_previous_effect, event_impact_pills  # noqa: E402
from src.line_client import LineClient  # noqa: E402
from src.line_flex import (  # noqa: E402
    calendar_day_bubble,
    post_release_bubble,
    pre_release_bubble,
    weekly_preview_bubble,
)


def _ev(title: str, country: str, impact: str = "High",
         forecast: str = "", previous: str = "",
         offset_days: int = 0, hour: int = 13, minute: int = 0) -> CalEvent:
    dt = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=offset_days)
    dt = dt.replace(hour=hour, minute=minute, second=0)
    return CalEvent(
        event_id=f"demo:{title}:{country}",
        title=title, country=country, impact=impact,
        forecast=forecast, previous=previous, dt_utc=dt,
    )


def main() -> int:
    if not os.environ.get("LINE_CHANNEL_TOKEN"):
        print("FAIL: LINE_CHANNEL_TOKEN not set")
        return 1
    target = os.environ.get("LINE_NEWS_TARGET") or os.environ.get("LINE_HEALTH_TARGET")
    if not target:
        print("FAIL: LINE_NEWS_TARGET / LINE_HEALTH_TARGET not set")
        return 1

    line = LineClient.from_env()

    # === DEMO 1: Pre-release — GBP CPI y/y, F: 3.0% / P: 3.3% (cooling) ===
    # Expect: GBP↓, USD↑, XAU↑ (matches user's mockup image)
    ev1 = _ev("CPI y/y", "GBP", impact="High",
              forecast="3.0%", previous="3.3%",
              offset_days=1, hour=13, minute=0)
    bubble1 = pre_release_bubble(ev1, minutes_to_release=15,
                                  effect=forecast_vs_previous_effect(ev1))
    print("Pushing DEMO 1: Pre-release GBP CPI cooling...")
    resp = line.push_flex(target, "[DEMO 1] Pre-release · GBP CPI y/y", bubble1)
    print(f"  -> {resp.get('status')}")

    # === DEMO 2: Post-release WITH FRED actual — US Core CPI hot ===
    # Actual 0.5%, Forecast 0.3%, Previous 0.2% — BEAT
    # Expect: USD↑, EUR↓, XAU↓ (hot CPI = hawkish Fed = bearish gold)
    ev2 = _ev("Core CPI m/m", "USD", impact="High",
              forecast="0.3%", previous="0.2%",
              offset_days=0, hour=20, minute=30)
    info2 = gold_impact_directional(ev2)
    bubble2 = post_release_bubble(
        ev2, info2, actual_text="0.5%", surprise="beat",
        verdict="🔴 Bearish gold — Hot CPI",
        effect=forecast_vs_previous_effect(ev2),
    )
    print("Pushing DEMO 2: Post-release FRED-path · US Core CPI hot...")
    resp = line.push_flex(target, "[DEMO 2] Post-release · US Core CPI hot", bubble2)
    print(f"  -> {resp.get('status')}")

    # === DEMO 3: Post-release WITHOUT actual (no-FRED) — JPY Tokyo CPI ===
    # F: 2.5%, P: 2.2% (forecast > previous)
    # Expect: JPY↑, USD↓, XAU↑ (BoJ hawkish, dovish-for-gold globally)
    ev3 = _ev("Tokyo Core CPI y/y", "JPY", impact="Medium",
              forecast="2.5%", previous="2.2%",
              offset_days=0, hour=6, minute=30)
    info3 = gold_impact_directional(ev3)
    bubble3 = post_release_bubble(
        ev3, info3, actual_text=None,
        effect=forecast_vs_previous_effect(ev3),
    )
    print("Pushing DEMO 3: Post-release no-FRED · JPY Tokyo CPI...")
    resp = line.push_flex(target, "[DEMO 3] Post-release · JPY Tokyo CPI (no actual)", bubble3)
    print(f"  -> {resp.get('status')}")

    # === DEMO 4: Economic Calendar daily — 5-ticker strip + 5 events ===
    # Compact 1-XAU pill per event row (no 3-pill row in this layout).
    events4 = [
        _ev("Retail Sales m/m", "GBP", "Medium", "0.3%", "0.5%",
             offset_days=1, hour=13, minute=0),
        _ev("Core Retail Sales m/m", "CAD", "Medium", "0.2%", "0.1%",
             offset_days=1, hour=19, minute=30),
        _ev("Retail Sales m/m", "CAD", "Medium", "0.4%", "0.2%",
             offset_days=1, hour=19, minute=30),
        _ev("Revised UoM Consumer Sentiment", "USD", "Medium", "59.5", "59.8",
             offset_days=1, hour=21, minute=0),
        _ev("Unemployment Claims", "USD", "Medium", "240K", "220K",
             offset_days=2, hour=12, minute=30),
    ]
    # Live price snapshots from yfinance (with retry — may be slow off-hours)
    from src import price_feed
    def _to_tuple(snap):
        return (snap.last, snap.pct_change_day) if snap else None
    xau_tuple = _to_tuple(price_feed.get_xau_snapshot())
    dxy_tuple = _to_tuple(price_feed.get_dxy_snapshot())
    hui_tuple = _to_tuple(price_feed.get_hui_snapshot())
    gld_tuple = _to_tuple(price_feed.get_gld_snapshot())
    thb_tuple = _to_tuple(price_feed.get_thb_snapshot())
    print(f"  prices XAU={xau_tuple} DXY={dxy_tuple} HUI={hui_tuple} GLD={gld_tuple} THB={thb_tuple}")
    bubble4 = calendar_day_bubble(
        events4, "Sat 23 May 2026 [DEMO]",
        xau_snapshot=xau_tuple, dxy_snapshot=dxy_tuple,
        hui_snapshot=hui_tuple, gld_snapshot=gld_tuple,
        thb_snapshot=thb_tuple,
    )
    print("Pushing DEMO 4: Economic Calendar daily · 5-ticker strip...")
    resp = line.push_flex(target, "[DEMO 4] Calendar daily · 5 tickers + 5 events", bubble4)
    print(f"  -> {resp.get('status')}")

    # === DEMO 5: Weekly preview — same compact 1-XAU pill per row ===
    events5 = events4 + [
        _ev("CPI y/y", "GBP", "High", "3.0%", "3.3%",
             offset_days=2, hour=13, minute=0),
        _ev("Core CPI m/m", "USD", "High", "0.3%", "0.2%",
             offset_days=3, hour=20, minute=30),
        _ev("Non-Farm Employment Change", "USD", "High", "200K", "180K",
             offset_days=4, hour=20, minute=30),
        _ev("BoE Gov Bailey Speaks", "GBP", "High", "", "",
             offset_days=4, hour=22, minute=0),
    ]
    effects5 = {ev.event_id: forecast_vs_previous_effect(ev) for ev in events5}
    bubble5 = weekly_preview_bubble(events5, effects5, "Mon 25 May – Fri 29 May [DEMO]")
    print("Pushing DEMO 5: Weekly Preview · compact pills...")
    resp = line.push_flex(target, "[DEMO 5] Weekly Preview · per-day compact", bubble5)
    print(f"  -> {resp.get('status')}")

    print("\nAll 5 demos pushed. Check LINE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
