"""Weekly incremental refresh: new releases → dataset → re-learn.

Run by .github/workflows/release_learning.yml every weekend. It does NOT
rebuild history (that needs ~300 MB of cached pages/zips, done once locally
with research.ff_history + research.dataset). Instead:

  1. fetch the ForexFactory month pages for last month + this month,
  2. keep releases not yet in research/dataset.csv (or there but unpriced)
     whose prints are ≥ 2 h old,
  3. price them from Dukascopy day files (a handful of requests per week;
     authoritative, and HistData's current-month file lags and is incomplete),
  4. append, then re-run research.learn → config/release_stats.json.

    python -m research.refresh
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime, timedelta, timezone

from research import dataset as ds
from research import ff_history, learn
from research import prices_duka

log = logging.getLogger(__name__)


def _month_pages(today: date) -> list[tuple[int, int]]:
    first = today.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return [(prev.year, prev.month), (today.year, today.month)]


class _DukaBook:
    """Adapter: the dataset builder's price_at() over the Dukascopy feed."""

    def __init__(self):
        self._b = prices_duka.PriceBook()

    def price_at(self, asset, ts, use="open", search_back_min=5):
        return self._b.price_at(asset, ts, use=use, search_back_min=search_back_min)

    def close(self):
        self._b.close()


def refresh(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with open(ds.OUT_CSV, encoding="utf-8") as f:
        existing = list(csv.DictReader(f))
    # A row counts as "have" only once it is fully priced: rows written while
    # the price source was still incomplete (the current month) get re-priced.
    have = {r["event_uid"] for r in existing if r.get("r60_XAU")}
    cutoff = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    fresh = []
    for y, m in _month_pages(now.date()):
        html = ff_history._fetch_month(y, m, force=True)
        for e in ff_history.events_from_days(ff_history.extract_days(html)):
            e = {k: str(v) for k, v in e.items()}
            if e["event_uid"] in have or e["dt_utc"] > cutoff or not ds.selected(e):
                continue
            fresh.append(e)
    if not fresh:
        log.info("no new releases")
        return 0
    book = _DukaBook()
    rows = [r for r in (ds.row_for(e, book) for e in fresh) if r]
    book.close()
    fields = list(existing[0].keys()) if existing else ds.FIELDS
    replaced = {r["event_uid"] for r in rows}
    kept = [r for r in existing if r["event_uid"] not in replaced]
    merged = sorted(kept + rows, key=lambda r: (r["dt_utc"], str(r["event_uid"])))
    with open(ds.OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")   # LF: no whole-file diff in CI
        w.writeheader()
        w.writerows(merged)
    log.info("appended %d releases", len(rows))
    return len(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    added = refresh()
    learn.main()
    print("added:", added)
