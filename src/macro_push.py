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
    """px: DataFrame with columns price, usd, silver, vix, spx, real_yield, breakeven
    (daily Close, date index). Returns the latest {factors, conviction, regime}.

    Mirrors backtest/multifactor.py exactly so the live signal == the backtested one:
      macro = falling real yields (THE driver) + low-yield regime + weak USD +
              rising breakevens + falling gold/silver ratio
      tech  = own EMA(20) vs EMA(50) cross
      risk  = rising VIX bid - equity-trend (small, sign-flipped for gold)
      news  = live from the pipeline (passed in)
      regime= sign of the 20d real-yield slope (yields_up = gold-bearish)
    """
    import numpy as np

    p = px["price"]
    ry_mom = -np.tanh(_z(px["real_yield"].diff(5)))
    ry_lvl = -np.tanh(_z(px["real_yield"], 120)) * 0.6
    usd = -np.tanh(_z(px["usd"].pct_change(5)))
    bei = np.tanh(_z(px["breakeven"].diff(5)))
    gsr = -np.tanh(_z((p / px["silver"]).pct_change(5)))
    macro = float((0.4 * ry_mom + 0.2 * ry_lvl + 0.2 * usd + 0.1 * bei + 0.1 * gsr).clip(-1, 1).iloc[-1])

    tech = float(np.sign(_ema(p, 20).iloc[-1] - _ema(p, 50).iloc[-1]))

    risk_series = (0.6 * np.tanh(_z(px["vix"].pct_change(5)))
                   - 0.4 * np.sign(_ema(px["spx"], 20) - _ema(px["spx"], 50))).clip(-1, 1) * 0.5
    risk = float(risk_series.iloc[-1])

    news = _clip1(news_factor)

    factors = {"macro": macro, "tech": tech, "risk": risk, "news": news}
    conviction = _clip1(sum(WEIGHTS[k] * factors[k] for k in WEIGHTS))

    ry_slope = float(np.sign(px["real_yield"].diff(20).iloc[-1]))
    regime = "yields_up" if ry_slope > 0 else "yields_down" if ry_slope < 0 else "flat"

    return {
        "factors": {k: round(v, 4) for k, v in factors.items()},
        "conviction": round(conviction, 4),
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
    px = pd.DataFrame({name: raw[sym] for name, sym in syms.items()})
    for name, sid in cfg.get("fred", {}).items():
        px[name] = fetch_fred_csv(sid)
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
