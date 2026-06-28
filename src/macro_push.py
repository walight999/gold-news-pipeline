"""Compute + POST the latest multi-factor MACRO STATE to the CHUM alert-bot worker.

This is the *live* half of the model in `backtest/multifactor.py` (which lives in
the CHUM alert-bot repo). The backtest learns the weights as priors; this module
computes the SAME factor fusion on the most recent data and ships only the latest
state to the worker's `/webhook/macro/<secret>` endpoint. The worker stores it in
KV (48h TTL) and tags every Pine signal with the macro context at alert time, so
Pillar B/C can learn whether macro-aligned signals grade better.

Why it lives here (not in the worker): the factor inputs are macro/market series
(FRED real yields, DXY, VIX, gold/silver) plus the pipeline's OWN classified-news
tone — exactly the data this repo already fetches. The worker has no market-data
feed; the pipeline is the natural producer.

Design (mirrors telegram_news.py / line_client.py):
  - Best-effort + env-gated. If MACRO_WEBHOOK_URL / MACRO_WEBHOOK_SECRET are unset,
    from_env() returns None and the caller no-ops — safe to ship before the founder
    wires the secret, and it never blocks the news run.
  - The fusion WEIGHTS MUST stay in sync with backtest/multifactor.py. The worker's
    Pillar C learns deviations FROM these priors; a drifted prior poisons learning.
  - The `news` factor (≈20% by the trader's call) is the pipeline's unique edge —
    fed LIVE from recent classified-news direction (0 in the offline backtest).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

# Trader priors — MUST match backtest/multifactor.py::WEIGHTS. Macro dominates for
# gold; news ≈ 20% (the user's explicit call). Keep these two in sync.
WEIGHTS: dict[str, float] = {"macro": 0.45, "tech": 0.20, "risk": 0.15, "news": 0.20}

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

# Per-asset data map (extensible). Start with XAUUSD; add tickers as the platform
# grows. `news_map` maps the pipeline's direction_label vocab to an asset-direction
# sign (for gold: dovish / risk_off bid the metal; hawkish / risk_on weigh on it).
ASSETS: dict[str, dict[str, Any]] = {
    "XAUUSD": {
        "price": "GC=F",
        "factors": {"usd": "DX-Y.NYB", "silver": "SI=F", "vix": "^VIX", "spx": "^GSPC"},
        "fred": {"real_yield": "DFII10", "breakeven": "T10YIE"},
        "news_map": {"dovish": 1.0, "risk_off": 1.0, "hawkish": -1.0, "risk_on": -1.0, "neutral": 0.0},
    },
}


def _clip1(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


# ---------------------------------------------------------------------------
# Live news factor — the pipeline's edge. Pure + testable (no network).
# ---------------------------------------------------------------------------
def recent_news_directions(rows: list[dict[str, Any]], cutoff_iso: str, limit: int = 50) -> list[str]:
    """From event_state rows, the most-recent `direction_label`s within the window.

    Pure + testable — this is the live news edge that was silently dead (the caller
    must `store.load_all()` first, or `rows` is empty). Sorted most-recent-first by
    `last_seen_ts`, filtered to >= cutoff (ISO UTC string compare), capped at `limit`.
    """
    recent = sorted(rows, key=lambda r: str(r.get("last_seen_ts", "")), reverse=True)
    dirs = [
        str(r.get("direction_label", "")).strip()
        for r in recent
        if str(r.get("last_seen_ts", "")) >= cutoff_iso
    ][:limit]
    return [d for d in dirs if d]


def news_factor_from_directions(directions: list[str], asset: str) -> float:
    """Net asset-direction of recent classified news, in [-1, 1].

    `directions` are the pipeline's direction_label values (hawkish / dovish /
    risk_off / risk_on / neutral) from recent gold-relevant events. Neutrals are
    INCLUDED as 0.0 so a stream of neutral news damps the factor toward zero;
    unrecognized labels are dropped. Returns 0.0 when nothing recognized.
    """
    nmap: dict[str, float] = ASSETS.get(asset, {}).get("news_map", {})
    vals = [nmap[d] for d in (directions or []) if d in nmap]
    if not vals:
        return 0.0
    return _clip1(sum(vals) / len(vals))


# ---------------------------------------------------------------------------
# Factor fusion — mirrors multifactor.py::build_signals but only the LATEST row.
# ---------------------------------------------------------------------------
def _z(s: "Any", n: int = 20) -> "Any":
    return ((s - s.rolling(n).mean()) / s.rolling(n).std()).clip(-3, 3).fillna(0)


def _ema(s: "Any", n: int) -> "Any":
    return s.ewm(span=n, adjust=False).mean()


def compute_state(px: "Any", news_factor: float = 0.0) -> dict[str, Any]:
    """px: DataFrame with column `price` (required) + any of usd, silver, vix, spx,
    real_yield, breakeven (daily Close, date index). Returns the latest
    {factors, conviction, regime}.

    Mirrors backtest/multifactor.py when all columns are present, but each factor
    input is GUARDED: a missing column or a NaN/short-series result contributes 0
    instead of crashing or poisoning the push with a non-finite value (a missing
    yfinance/FRED symbol degrades the factor, never the whole state).
      macro = falling real yields (THE driver) + low-yield regime + weak USD +
              rising breakevens + falling gold/silver ratio
      tech  = own EMA(20) vs EMA(50) cross
      risk  = rising VIX bid - equity-trend (small, sign-flipped for gold)
      news  = live from the pipeline (passed in)
      regime= sign of the 20d real-yield slope (yields_up = gold-bearish)
    """
    import numpy as np

    def col(name: str):
        return px[name] if name in getattr(px, "columns", []) else None

    def fin(v: Any, default: float = 0.0) -> float:
        try:
            f = float(v)
            return f if np.isfinite(f) else default
        except (TypeError, ValueError):
            return default

    p = px["price"]

    # MACRO — each sub-signal guarded; absent input → 0 contribution.
    macro = 0.0
    ry = col("real_yield")
    if ry is not None:
        macro += 0.4 * fin((-np.tanh(_z(ry.diff(5)))).iloc[-1])
        macro += 0.2 * fin((-np.tanh(_z(ry, 120)) * 0.6).iloc[-1])
    usd = col("usd")
    if usd is not None:
        macro += 0.2 * fin((-np.tanh(_z(usd.pct_change(5)))).iloc[-1])
    bei = col("breakeven")
    if bei is not None:
        macro += 0.1 * fin((np.tanh(_z(bei.diff(5)))).iloc[-1])
    sil = col("silver")
    if sil is not None:
        macro += 0.1 * fin((-np.tanh(_z((p / sil).pct_change(5)))).iloc[-1])
    macro = _clip1(macro)

    tech = fin(np.sign(_ema(p, 20).iloc[-1] - _ema(p, 50).iloc[-1]))

    risk = 0.0
    vix = col("vix")
    if vix is not None:
        risk += 0.6 * fin((np.tanh(_z(vix.pct_change(5)))).iloc[-1])
    spx = col("spx")
    if spx is not None:
        risk += -0.4 * fin(np.sign(_ema(spx, 20).iloc[-1] - _ema(spx, 50).iloc[-1]))
    risk = _clip1(risk) * 0.5

    news = _clip1(news_factor)

    factors = {"macro": macro, "tech": tech, "risk": risk, "news": news}
    conviction = _clip1(sum(WEIGHTS[k] * factors[k] for k in WEIGHTS))

    ry_slope = fin(np.sign(ry.diff(20).iloc[-1])) if ry is not None else 0.0
    regime = "yields_up" if ry_slope > 0 else "yields_down" if ry_slope < 0 else "flat"

    return {
        "factors": {k: round(fin(v), 4) for k, v in factors.items()},
        "conviction": round(fin(conviction), 4),
        "regime": regime,
    }


def fetch_fred_csv(series_id: str) -> "Any":
    """FRED public CSV (no API key). e.g. DFII10 = 10Y real yield, T10YIE = breakeven."""
    import pandas as pd

    df = pd.read_csv(FRED_CSV.format(series_id), parse_dates=[0], index_col=0)
    return pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()


def fetch_frame(asset: str) -> "Any":
    """Download ~2y of daily Close for the asset + its factor inputs, aligned."""
    import pandas as pd
    import yfinance as yf

    cfg = ASSETS[asset]
    syms = {"price": cfg["price"], **cfg["factors"]}
    raw = yf.download(list(syms.values()), period="2y", interval="1d",
                      progress=False, auto_adjust=True)["Close"]
    # Defensive: yfinance can omit a symbol (delisting / transient outage). Keep only
    # the columns it actually returned — compute_state treats an absent factor as 0,
    # so one flaky input degrades that factor instead of KeyError-ing the whole state.
    raw_cols = list(getattr(raw, "columns", []))
    px = pd.DataFrame()
    for name, sym in syms.items():
        if sym in raw_cols:
            px[name] = raw[sym]
        else:
            log.warning("macro: yfinance missing %s (%s) for %s — factor dropped", sym, name, asset)
    if "price" not in px.columns:
        raise ValueError(f"macro: no price data for {asset} ({cfg['price']})")
    for name, sid in cfg.get("fred", {}).items():
        try:
            px[name] = fetch_fred_csv(sid)
        except Exception as e:  # FRED CSV down → drop that factor, keep the rest
            log.warning("macro: FRED %s (%s) fetch failed for %s — factor dropped: %s", sid, name, asset, e)
    return px.ffill().dropna()


def build_payload(ticker: str, state: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
    """Map a computed state → the worker's /webhook/macro contract.
    Direction is intentionally omitted: the worker derives it from conviction
    (single source of truth in normalizeMacro)."""
    return {
        "ticker": ticker,
        "conviction": state["conviction"],
        "regime": state["regime"],
        "factors": state["factors"],
        "ts": ts or datetime.now(timezone.utc).isoformat(),
    }


@dataclass
class MacroPushClient:
    base_url: str
    secret: str

    @classmethod
    def from_env(cls) -> "MacroPushClient | None":
        url = os.environ.get("MACRO_WEBHOOK_URL", "").strip()
        secret = os.environ.get("MACRO_WEBHOOK_SECRET", "").strip()
        if not url or not secret:
            return None
        return cls(base_url=url.rstrip("/"), secret=secret)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           reraise=True)
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.base_url}/webhook/macro/{self.secret}"
        with httpx.Client(timeout=15.0) as c:
            r = c.post(endpoint, json=payload)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(f"macro webhook {r.status_code}", request=r.request, response=r)
        return {"status": r.status_code, "body": r.text}

    def push(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._post(payload)
        except RetryError as e:
            log.warning("macro push failed after retries: %s", e)
            return {"status": 0, "body": "retry_exhausted"}
        except httpx.HTTPError as e:
            log.warning("macro push http error: %s", e)
            return {"status": 0, "body": str(e)}


def compute_and_push(
    news_directions_by_asset: dict[str, list[str]] | None = None,
    assets: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute the latest macro state per asset and POST it. Best-effort: a
    failure on one asset is logged and skipped, never raised."""
    client = MacroPushClient.from_env()
    if client is None:
        log.info("macro push skipped — MACRO_WEBHOOK_URL/MACRO_WEBHOOK_SECRET unset")
        return []

    results: list[dict[str, Any]] = []
    for ticker in (assets or list(ASSETS)):
        try:
            px = fetch_frame(ticker)
            dirs = (news_directions_by_asset or {}).get(ticker, [])
            nf = news_factor_from_directions(dirs, ticker)
            state = compute_state(px, nf)
            res = client.push(build_payload(ticker, state))
            log.info("macro push %s conv=%.3f regime=%s news=%.2f → %s",
                     ticker, state["conviction"], state["regime"], nf, res.get("status"))
            results.append({"ticker": ticker, **state, "news_factor": round(nf, 4), "push": res})
        except Exception as e:  # best-effort — never crash the run
            log.warning("macro push failed for %s: %s", ticker, e)
            results.append({"ticker": ticker, "error": str(e)})
    return results
