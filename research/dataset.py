"""Join FF release history with 1-minute prices → research/data/dataset.csv.

One row per release. For every asset in prices.ASSETS:

    pre60_<A>, pre15_<A>   % move from T-60 / T-15 to the last price before
                           the print (does the market position ahead of it?)
    r5_<A>, r15_<A>, r60_<A>
                           % move from the last pre-print price to T+5/15/60

Reference price p0 = CLOSE of the bar that ends at the release minute (the
12:29 bar for a 12:30 print) — the last price that cannot contain the news.
T+N = OPEN of the bar starting N minutes after the release.

Release selection (config below): USD high+medium, plus high-impact releases
of the other majors — the events that actually reach the LINE group.
Rows need numeric actual AND forecast; "previous" is optional.

    python -m research.dataset            # builds, downloading prices as needed
"""
from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from research import ff_history
from research.prices import ASSETS, PriceBook

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.fred import parse_forecast_value  # noqa: E402

log = logging.getLogger(__name__)

# Committed (≈1.6 MB): the weekly refresh in CI appends to it instead of
# rebuilding history from the ~300 MB of cached raw downloads.
OUT_CSV = os.path.join(os.path.dirname(__file__), "dataset.csv")
USD_IMPACTS = ("high", "medium")
OTHER_CCY = ("EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "CNY")
OTHER_IMPACTS = ("high",)
PRE = (60, 15)
POST = (5, 15, 60)


def selected(e: dict) -> bool:
    if e["currency"] == "USD":
        return e["impact"] in USD_IMPACTS
    return e["currency"] in OTHER_CCY and e["impact"] in OTHER_IMPACTS


def _num(s: str) -> float | None:
    s = (s or "").strip().lstrip("<>").strip()
    return parse_forecast_value(s) if s else None


def _pct(a: float | None, b: float | None) -> str:
    if a is None or b is None or a == 0:
        return ""
    return f"{(b / a - 1) * 100:.5f}"


FIELDS = (["event_uid", "ebase_id", "dt_utc", "currency", "impact", "title",
           "actual", "forecast", "previous", "better_worse",
           "actual_v", "forecast_v", "previous_v", "surprise", "expectation"]
          + [f"pre{m}_{k}" for k in ASSETS for m in PRE]
          + [f"r{m}_{k}" for k in ASSETS for m in POST])


def row_for(e: dict, pb) -> dict | None:
    """Dataset row for one FF event, priced from `pb` (anything with
    price_at(asset, ts, use)). None when actual/forecast aren't numeric."""
    a, f, p = _num(e["actual"]), _num(e["forecast"]), _num(e["previous"])
    if a is None or f is None:
        return None
    ts = datetime.strptime(e["dt_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    row = {k: e.get(k, "") for k in ("event_uid", "ebase_id", "dt_utc", "currency",
                                      "impact", "title", "actual", "forecast",
                                      "previous", "better_worse")}
    row.update(actual_v=a, forecast_v=f, previous_v="" if p is None else p,
               surprise=round(a - f, 6),
               expectation="" if p is None else round(f - p, 6))
    for k in ASSETS:
        p0 = pb.price_at(k, ts - timedelta(minutes=1), use="close")
        for m in PRE:
            row[f"pre{m}_{k}"] = _pct(pb.price_at(k, ts - timedelta(minutes=m)), p0)
        for m in POST:
            row[f"r{m}_{k}"] = _pct(p0, pb.price_at(k, ts + timedelta(minutes=m)))
    return row


def build(limit: int | None = None) -> int:
    events = [e for e in ff_history.load_events()
              if selected(e) and _num(e["actual"]) is not None and _num(e["forecast"]) is not None]
    if limit:
        events = events[:limit]
    years = sorted({int(e["dt_utc"][:4]) for e in events})
    log.info("loading 1-minute prices for %s", years)
    pb = PriceBook(years)
    n = 0
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for i, e in enumerate(events):
            row = row_for(e, pb)
            if row is None:
                continue
            w.writerow(row)
            n += 1
            if i % 500 == 0:
                log.info("%d/%d %s %s", i, len(events), e["dt_utc"], e["title"])
    pb.close()
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print("rows:", build(lim))
