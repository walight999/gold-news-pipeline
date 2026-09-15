"""Curated prediction-market odds for the XAU drivers — PRINT-ONLY PROTOTYPE.

Why this exists (and why it is narrow on purpose):
  Gold is driven by a SMALL, stable set of macro variables — the Fed path,
  inflation, recession risk, fiscal/geopolitical shocks. Prediction markets only
  cover ~a dozen macro events, and that is exactly the point: those events map
  almost 1:1 onto what an XAU trader must watch. So this module does NOT try to
  attach a probability to every news item (most have no market). It tracks a hand
  -curated WHITELIST of ~7 markets and reports each one's current probability plus
  the CHANGE (delta) since the last run — because for a gold trader the delta, not
  the level, is the signal (a repricing of "0 cuts" from 80% to 94% is the trade).

Source: Polymarket Gamma public API (free, open, reachable from TH, no auth).
  - public-search returns events with embedded markets + outcomePrices + 24h vol,
    so one call per watch item gets both the number and a liquidity read.
  - Kalshi is a possible cross-check for a few indicators, but its Fed ladders
    came back empty (no trades); Polymarket carries the liquidity here.

This prototype is deliberately standalone: it prints a table and persists a tiny
local JSON snapshot for delta tracking. It does NOT touch the Google-Sheet store,
does NOT push to LINE, and is not wired into main.py. Run it, judge whether the
coverage + liquidity + deltas are worth shipping, THEN decide the display mode.

Run:  python -m src.prediction_odds
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_SEARCH = "https://gamma-api.polymarket.com/public-search"
_UA = {"Accept": "application/json", "User-Agent": "gold-news-pipeline/odds-proto"}
_ROOT = os.path.dirname(os.path.dirname(__file__))
# State lives under snapshots/ (same convention as snapshots/headlines.json) so the
# daily observe workflow can commit it back and deltas accrue across runs. The log
# is append-only JSONL — one line per run — so after 1-2 weeks there is a real
# history to judge whether the odds actually reprice around the events gold cares about.
_SNAPSHOT = os.path.join(_ROOT, "snapshots", "prediction_odds.json")
_LOG = os.path.join(_ROOT, "snapshots", "prediction_odds_log.jsonl")

# Below this 24h USD volume a market is too thin to trust its probability as a
# signal — we still print it, but flag it THIN so a noisy tick is not mistaken
# for a real repricing.
_THIN_VOL24 = 5_000.0


@dataclass(frozen=True)
class Watch:
    """One curated market to track.

    key      stable id used in the snapshot (survives monthly event rollover)
    query    text handed to Polymarket public-search
    match    substring the event title MUST contain (picks the right event when
             search returns several; the highest-volume match wins)
    signal   substring identifying the ONE outcome we track for the delta; None
             means the event is a single Yes/No market and we track "Yes"
    xau      +1 / -1 / 0 — if the tracked probability RISES, XAU tends to go
             up (+1), down (-1), or it is ambiguous / it IS gold (0). Drives the
             direction arrow attached to the delta.
    ladder   if True, also print the full outcome distribution (multi-bucket
             markets like "how many cuts" / "what will gold hit")
    """
    key: str
    query: str
    match: str
    signal: str | None
    xau: int
    ladder: bool = False


# The curated whitelist. Add/remove here — this is the whole "coverage" surface.
WATCHLIST: list[Watch] = [
    Watch("fed_decision",  "Fed Decision",              "Fed Decision in",              "No change", -1),
    Watch("fed_cuts_2026", "How many Fed rate cuts",    "How many Fed rate cuts",       "0 (0 bps)", -1, ladder=True),
    Watch("fed_hike_2026", "Fed rate hike 2026",        "Fed rate hike in",             None,        -1),
    Watch("emergency_cut", "Fed emergency rate cut",    "emergency rate cut",           None,        +1),
    Watch("recession",     "recession 2026",            "US recession by end of",       None,        +1),
    Watch("shutdown",      "government shutdown",        "Government shutdown by",        None,        +1),
    Watch("gold_target",   "gold price XAUUSD",          "Gold (XAUUSD) hit in",          None,         0, ladder=True),
]


@dataclass
class Outcome:
    label: str
    prob: float          # 0..1
    vol24: float
    bid: float | None
    ask: float | None


@dataclass
class Reading:
    watch: Watch
    event_title: str
    event_vol: float
    signal: Outcome | None       # the tracked outcome (None if not found)
    ladder: list[Outcome] = field(default_factory=list)


def _prices(m: dict) -> list[float]:
    raw = m.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [float(x) for x in (raw or [])]


def _outcomes(event: dict) -> list[Outcome]:
    """Flatten an event's markets into (label, yes-prob, 24h vol) outcomes.

    Polymarket models both shapes uniformly once you look at each market as a
    binary: a single Yes/No market -> one outcome labelled "Yes"; a grouped
    (neg-risk) market -> one binary market per bucket, labelled by groupItemTitle.
    In both cases the bucket's probability is the "Yes" price (outcomePrices[0]).
    """
    out: list[Outcome] = []
    for m in event.get("markets", []) or []:
        if m.get("closed"):
            continue
        pr = _prices(m)
        if not pr:
            continue
        label = m.get("groupItemTitle") or "Yes"
        out.append(Outcome(
            label=label,
            prob=pr[0],
            vol24=float(m.get("volume24hr") or 0),
            bid=float(m["bestBid"]) if m.get("bestBid") is not None else None,
            ask=float(m["bestAsk"]) if m.get("bestAsk") is not None else None,
        ))
    return out


def _search_event(client: httpx.Client, w: Watch) -> dict | None:
    url = f"{_SEARCH}?q={urllib.parse.quote(w.query)}&limit_per_type=8&events_status=active"
    r = client.get(url, headers=_UA, timeout=20.0)
    r.raise_for_status()
    events = (r.json() or {}).get("events", []) or []
    cands = [
        e for e in events
        if w.match.lower() in (e.get("title") or "").lower() and not e.get("closed")
    ]
    if not cands:
        return None
    # Highest-volume matching event wins (avoids stale/duplicate shells).
    return max(cands, key=lambda e: float(e.get("volume") or 0))


def fetch() -> list[Reading]:
    readings: list[Reading] = []
    with httpx.Client(http2=False) as client:
        for w in WATCHLIST:
            try:
                ev = _search_event(client, w)
            except Exception as e:  # network / API shape — report as a miss, keep going
                log.warning("odds fetch failed for %s: %s", w.key, e)
                ev = None
            if not ev:
                readings.append(Reading(w, "(no active market found)", 0.0, None))
                continue
            outs = _outcomes(ev)
            if w.signal is None:
                # binary Yes/No event -> the sole market; a price ladder (xau==0,
                # e.g. gold_target) has no single "Yes" -> track the highest-prob
                # bucket, since outs[0] (event order) is meaningless there.
                pool = sorted(outs, key=lambda o: o.prob, reverse=True) if w.ladder else outs
                sig = pool[0] if pool else None
            else:
                sig = next((o for o in outs if w.signal.lower() in o.label.lower()), None)
            readings.append(Reading(
                watch=w,
                event_title=ev.get("title") or "",
                event_vol=float(ev.get("volume") or 0),
                signal=sig,
                ladder=sorted(outs, key=lambda o: o.prob, reverse=True) if w.ladder else [],
            ))
    return readings


def _load_snapshot() -> dict:
    if not os.path.exists(_SNAPSHOT):
        return {}
    try:
        with open(_SNAPSHOT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_snapshot(readings: list[Reading]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    snap = {"ts": ts, "odds": {r.watch.key: r.signal.prob for r in readings if r.signal}}
    os.makedirs(os.path.dirname(_SNAPSHOT), exist_ok=True)
    with open(_SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    # Append a full richer row to the history log for later offline analysis.
    row = {
        "ts": ts,
        "readings": {
            r.watch.key: {"label": r.signal.label, "prob": r.signal.prob, "vol24": r.signal.vol24}
            for r in readings if r.signal
        },
    }
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _arrow(delta_pct: float, xau: int) -> str:
    if xau == 0 or abs(delta_pct) < 1.0:
        return ""
    eff = delta_pct * xau
    return "XAU+" if eff > 0 else "XAU-"


def render(readings: list[Reading], prev: dict) -> str:
    prev_odds = prev.get("odds", {})
    prev_ts = prev.get("ts", "never")
    lines: list[str] = []
    lines.append("=" * 74)
    lines.append("CURATED PREDICTION-MARKET ODDS  (Polymarket)  — prototype, print-only")
    lines.append(f"now={datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z   prev snapshot={prev_ts}")
    lines.append("=" * 74)
    lines.append(f"{'market':<26}{'signal outcome':<16}{'prob':>6}{'Δ24h':>8}  {'dir':<5}{'liq':<6}")
    lines.append("-" * 74)
    for r in readings:
        w = r.watch
        if not r.signal:
            lines.append(f"{w.key:<26}{'— no market —':<16}")
            continue
        s = r.signal
        prob_pct = s.prob * 100
        prev_p = prev_odds.get(w.key)
        if prev_p is None:
            dstr, arrow = "  seed", ""
        else:
            d = prob_pct - prev_p * 100
            dstr = f"{d:+.1f}"
            arrow = _arrow(d, w.xau)
        thin = "THIN" if s.vol24 < _THIN_VOL24 else "ok"
        lines.append(f"{w.key:<26}{s.label[:15]:<16}{prob_pct:>5.1f}%{dstr:>8}  {arrow:<5}{thin:<6}")
        if r.ladder:
            for o in r.ladder[:5]:
                lines.append(f"    {o.label[:22]:<22}{o.prob*100:>5.1f}%   (24h vol ${o.vol24:,.0f})")
    lines.append("-" * 74)
    # Suitability read: how much of the whitelist is actually live + liquid.
    found = [r for r in readings if r.signal]
    liquid = [r for r in found if r.signal.vol24 >= _THIN_VOL24]
    lines.append(f"coverage: {len(found)}/{len(readings)} markets live  |  "
                 f"liquid (24h≥${_THIN_VOL24:,.0f}): {len(liquid)}/{len(found)}")
    lines.append("Δ populates from the 2nd run onward (needs a prior snapshot).")
    return "\n".join(lines)


def main() -> None:
    import sys
    try:  # the Δ glyph breaks a cp874 (Thai) Windows console; CI/Linux is utf-8 already
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    prev = _load_snapshot()
    readings = fetch()
    print(render(readings, prev))
    _save_snapshot(readings)


if __name__ == "__main__":
    main()
