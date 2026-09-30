"""Speech / statement watcher — "listen" to releases that have no number.

FOMC statements and press conferences, Fed Chair / FOMC member speeches,
President Trump, ECB / BOE / BOJ governors: the calendar gives a time but no
actual, and the market trades the WORDS. This module follows the live quote
wires during the event and has Claude read them:

  1. calendar_check (every 10 min) finds speech-type events on the FF calendar
     and publishes their listening windows + speaker keywords to the
     `_speech_windows` source_state row (plan_windows / publish_windows).
  2. news-cron (every 5 min) already fetches X squawk accounts (FirstSquawk,
     FinancialJuice, DeItaone) + wires; while a window is open, collect()
     keeps every headline that quotes the speaker ("WARSH: …",
     "TRUMP SAYS …") in a per-event buffer row `_speech:<id>`.
     For FOMC statements the official text is also pulled from the Fed's
     monetary-policy RSS (fetch_fomc_statement).
  3. calendar_check sends a card mid-way (≥ MIN_QUOTES quotes, +MID_AFTER min)
     and a final one when the window closes: Thai summary, key points, tone,
     gold direction + confidence, and XAU's move since the start.
  4. The final call is written to calibration_log (routed_as "speech",
     first_seen_ts = SEND time, so grading only credits what happened AFTER
     the call) and the daily scorecard grades it like any other verdict.

State lives in source_state rows (JSON in the items_last_hour column), the
same pattern line_client uses for its counters — no new sheet tab.
Everything here is best-effort: failures log and return, never raise into
the news / calendar runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

WINDOWS_ROW = "_speech_windows"
SKIP_ROUTE = "speech_skip"          # sent_log marker: window closed with no quotes
BUF_PREFIX = "_speech:"
MAX_QUOTES = 60
MAX_BUF_CHARS = 30000

_SPEECH_TITLE = re.compile(
    r"\b(speaks|testifies|press conference|statement|meeting minutes|remarks)\b", re.I)
_SPEAKER = re.compile(r"(\w+)\s+(?:Speaks|Testifies)\b")
_NOT_A_NAME = {"member", "gov", "governor", "president", "chairman", "chair", "sec"}

DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "currencies": ["USD", "EUR", "GBP", "JPY"],
    "impacts": ["High", "Medium"],
    "mid_after_min": 20,
    "min_quotes": 3,
    "fed_chair": "Warsh",
    # Words that identify the institution in wire headlines, per currency.
    "bodies": {
        "USD": ["FOMC", "FED", "FEDERAL RESERVE"],
        "EUR": ["ECB", "LAGARDE"],
        "GBP": ["BOE", "BANK OF ENGLAND", "BAILEY"],
        "JPY": ["BOJ", "BANK OF JAPAN", "UEDA"],
    },
}
QUOTE_SOURCES = ("x_", "forexlive", "fxstreet", "investing", "cnbc")
# Feeds where EVERY line is a live squawk quote (like the X accounts).
SQUAWK_SOURCES = ("x_", "financialjuice")


def cfg_from(sched_cfg: dict | None) -> dict:
    c = dict(DEFAULT_CFG)
    c.update((sched_cfg or {}).get("speech") or {})
    return c


# ---------------------------------------------------------------- planning

def is_speech_event(ev, cfg: dict) -> bool:
    from .calendar import is_statement_driven
    if (ev.country or "").upper() not in cfg["currencies"]:
        return False
    if (ev.impact or "") not in cfg["impacts"]:
        return False
    return bool(_SPEECH_TITLE.search(ev.title or "")) or is_statement_driven(ev)


def window_minutes(title: str) -> int:
    t = (title or "").lower()
    if "press conference" in t or "testifies" in t:
        return 90
    if "minutes" in t:
        return 30
    return 60


def speaker_keywords(ev, cfg: dict) -> list[str]:
    """Upper-case words a quote headline must contain.

    A named speaker ("FOMC Member Waller Speaks", "ECB President Lagarde
    Speaks", "President Trump Speaks") → that name ONLY: institution words
    like FOMC / FED would also catch other officials talking the same hour
    and blend their quotes into this speaker's read. Institution events with
    no person (FOMC Statement / Press Conference, rate decisions, minutes) →
    the institution's words, plus the Fed Chair's name on FOMC events since
    the chair runs the press conference ("WARSH: …")."""
    title = ev.title or ""
    ccy = (ev.country or "").upper()
    m = _SPEAKER.search(title)
    person = m.group(1) if m and m.group(1).lower() not in _NOT_A_NAME else None
    if person:
        return [person.upper()]
    keys = list(cfg["bodies"].get(ccy, []))
    if ccy == "USD" and cfg.get("fed_chair") and re.search(r"fomc|federal funds", title, re.I):
        keys.append(str(cfg["fed_chair"]).upper())
    return list(dict.fromkeys(k for k in keys if k))


def window_id(ev) -> str:
    return hashlib.sha1(f"{ev.country}|{ev.title}|{ev.dt_utc.isoformat()}".encode()).hexdigest()[:12]


_POLITICAL = re.compile(r"\b(President Trump|Treasury Sec)\b")
MERGE_GAP_MIN = 45


def plan_windows(events, now: datetime, cfg: dict) -> list[dict]:
    """Speech windows that are open or open within the next 15 min.

    Central-bank events of one currency that start within MERGE_GAP_MIN of
    each other are ONE window (FOMC day: Federal Funds Rate + FOMC Statement
    at 18:00 and the Press Conference at 18:30 → a single window to the end
    of the presser), so a policy day yields one mid + one final card, not four."""
    evs = sorted((e for e in events if is_speech_event(e, cfg)), key=lambda e: e.dt_utc)
    groups: list[list] = []
    open_cb: dict[str, list] = {}          # currency → latest central-bank group
    for ev in evs:
        if _POLITICAL.search(ev.title):
            groups.append([ev])            # political speeches never merge
            continue
        g = open_cb.get(ev.country)
        if g and (ev.dt_utc - g[-1].dt_utc) <= timedelta(minutes=MERGE_GAP_MIN):
            g.append(ev)                   # even if another speech sits in between
        else:
            g = [ev]
            groups.append(g)
            open_cb[ev.country] = g
    out = []
    for g in groups:
        start = g[0].dt_utc
        end = max(e.dt_utc + timedelta(minutes=window_minutes(e.title)) for e in g)
        if not (start - timedelta(minutes=15) <= now <= end + timedelta(minutes=30)):
            continue
        keys: list[str] = []
        for e in g:
            keys += speaker_keywords(e, cfg)
        out.append({"id": window_id(g[0]), "title": " + ".join(dict.fromkeys(e.title for e in g)),
                    "country": g[0].country, "start": start.isoformat(), "end": end.isoformat(),
                    "keywords": list(dict.fromkeys(keys))})
    return out


def publish_windows(store, windows: list[dict]) -> None:
    from .utils_time import iso_utc, now_utc
    store.upsert("source_state", {"source_id": WINDOWS_ROW,
                                  "items_last_hour": json.dumps(windows, ensure_ascii=False),
                                  "last_attempt_ts": iso_utc(now_utc())})


def stored_windows(store) -> list[dict]:
    row = store.get("source_state", (WINDOWS_ROW,)) or {}
    try:
        w = json.loads(row.get("items_last_hour") or "[]")
        return w if isinstance(w, list) else []
    except ValueError:
        return []


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- collection (news-cron)

def _has_keyword(up: str, keywords: list[str]) -> bool:
    """Whole-word match: "BOE" must not fire on "BOEING", "FED" on "FEDEX"."""
    return any(re.search(r"(?<![A-Z])" + re.escape(k.strip()) + r"(?![A-Z])", up)
               for k in keywords if k.strip())


def is_quote(title: str, source_id: str, keywords: list[str]) -> bool:
    up = (title or "").upper()
    if not _has_keyword(up, keywords):
        return False
    if (source_id or "").startswith(SQUAWK_SOURCES):
        return True               # squawk feeds: every line is a live quote
    # wires: only lines that actually report speech, not commentary about it
    return (" SAYS" in up or ": " in up or " SAID" in up) and \
        any((source_id or "").startswith(s) for s in QUOTE_SOURCES)


def drop_buffer(store, wid: str) -> None:
    """Remove a window's quote buffer once it can no longer be used."""
    store.delete("source_state", (BUF_PREFIX + wid,))


def load_buffer(store, wid: str) -> list[dict]:
    row = store.get("source_state", (BUF_PREFIX + wid,)) or {}
    try:
        b = json.loads(row.get("items_last_hour") or "[]")
        return b if isinstance(b, list) else []
    except ValueError:
        return []


def collect(store, items, now: datetime) -> int:
    """Append live quotes for every open window. Returns quotes added."""
    added = 0
    for w in stored_windows(store):
        start, end = _dt(w["start"]), _dt(w["end"])
        if not (start - timedelta(minutes=5) <= now <= end + timedelta(minutes=10)):
            continue
        buf = load_buffer(store, w["id"])
        seen = {q["t"] for q in buf}
        for it in items:
            ts = getattr(it, "published_ts", None) or getattr(it, "first_seen_ts", None) or now
            if ts < start - timedelta(minutes=5) or ts > end + timedelta(minutes=5):
                continue
            title = (getattr(it, "title", "") or "").strip()
            if not title or title in seen or not is_quote(title, getattr(it, "source_id", ""), w["keywords"]):
                continue
            buf.append({"t": title[:400], "src": getattr(it, "source_id", ""),
                        "ts": ts.astimezone(timezone.utc).isoformat()})
            seen.add(title)
            added += 1
        if added:
            buf.sort(key=lambda q: q["ts"])
            buf = buf[-MAX_QUOTES:]
            while len(json.dumps(buf, ensure_ascii=False)) > MAX_BUF_CHARS and len(buf) > 5:
                buf = buf[1:]
            store.upsert("source_state", {"source_id": BUF_PREFIX + w["id"],
                                          "items_last_hour": json.dumps(buf, ensure_ascii=False),
                                          "last_item_ts": now.isoformat()})
    return added


# ---------------------------------------------------------------- official FOMC statement

FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"


def fetch_fomc_statement(day: datetime, timeout: float = 15.0) -> str | None:
    """Plain text of today's FOMC statement from the Fed's own RSS, or None."""
    try:
        import httpx
        from bs4 import BeautifulSoup
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "gold-news-pipeline/1.0"}) as c:
            rss = c.get(FED_RSS).text
            soup = BeautifulSoup(rss, "xml")
            for item in soup.find_all("item"):
                title = (item.title.text if item.title else "").lower()
                pub = item.pubDate.text if item.pubDate else ""
                if "fomc statement" not in title and "federal reserve issues fomc" not in title:
                    continue
                from email.utils import parsedate_to_datetime
                if pub and parsedate_to_datetime(pub).date() != day.date():
                    continue
                link = item.link.text.strip() if item.link else ""
                page = BeautifulSoup(c.get(link).text, "html.parser")
                body = page.find(id="article") or page
                text = " ".join(p.get_text(" ", strip=True) for p in body.find_all("p"))
                return text[:5000] or None
    except Exception as e:  # noqa: BLE001 — optional context for the prompt
        log.warning("fomc statement fetch failed: %s", e)
    return None


# ---------------------------------------------------------------- analysis

_PROMPT = """You are a senior gold (XAU/USD) macro analyst on a Thai trading desk.
A scheduled {kind} is {stage}: "{title}" ({country}).
Below are the live headline quotes from wire / squawk feeds{stmt_note}, oldest first.

{quotes}
{statement}
Judge what was SAID — policy stance, forward guidance, surprises vs what markets
expected — and what it means for gold over the next hour. Quotes may repeat or be
partial; weigh the substance, ignore noise. If the quotes are thin or contradictory,
say so and lower confidence. XAU has moved {xau_move} since the start; if you
mention that move, quote it in US dollars exactly as given (never as a percentage).

Return ONLY JSON:
{{"tone": "hawkish" | "dovish" | "neutral" | "mixed",
  "summary_th": "one Thai sentence: the main message",
  "key_points_th": ["up to 3 short Thai bullets, each a concrete point that was said"],
  "gold_bias": "bullish" | "bearish" | "mixed",
  "gold_confidence": "high" | "medium" | "low",
  "why_th": "one Thai sentence: why that gold direction"}}
Thai rules: natural trading-desk Thai, keep names/acronyms like Fed, FOMC, ECB in English,
no em-dash, no Chinese/Japanese characters."""


def _kind(title: str) -> str:
    t = title.lower()
    if "press conference" in t:
        return "press conference"
    if "statement" in t or "rate" in t:
        return "policy statement"
    if "minutes" in t:
        return "meeting-minutes release"
    return "speech"


def _llm(prompt: str) -> str | None:
    """Sonnet → Gemini. Sonnet because tone reading is the whole job here."""
    from .translator import _get_anthropic_client
    client = _get_anthropic_client()
    if client is not None:
        model = os.environ.get("SPEECH_MODEL", "claude-sonnet-4-6").strip()
        try:
            resp = client.messages.create(model=model, max_tokens=700,
                                          messages=[{"role": "user", "content": prompt}])
            return resp.content[0].text
        except Exception as e:  # noqa: BLE001
            log.warning("speech analysis (claude) failed: %s", e)
    # Fallback chain of the calendar explainer: Claude Haiku (≤3 tries) → Gemini.
    from .news_alert import _cal_explain_llm
    return _cal_explain_llm(prompt)


def analyze(win: dict, quotes: list[dict], stage: str, xau_move_pct: float | None,
            statement_text: str | None = None, store=None) -> dict | None:
    if not quotes and not statement_text:
        return None
    key = "sp" + hashlib.sha1(f"{win['id']}|{stage}|{len(quotes)}".encode()).hexdigest()[:14]
    if store is not None:
        row = store.get("translation_cache", (key,))
        if row and row.get("thai_text"):
            try:
                cached = json.loads(row["thai_text"])
            except ValueError:
                cached = None
            if isinstance(cached, dict) and cached.get("failed"):
                return None          # already failed on these exact quotes
            if cached:
                return cached
    qtext = "\n".join(f"- [{q['ts'][11:16]} UTC] {q['t']}" for q in quotes[-40:]) or "(no quotes captured)"
    move = ("n/a" if xau_move_pct is None else
            xau_move_pct if isinstance(xau_move_pct, str) else f"{xau_move_pct:+.2f}%")
    prompt = _PROMPT.format(
        kind=_kind(win["title"]), stage="in progress" if stage == "mid" else "just finished",
        title=win["title"], country=win["country"], quotes=qtext, xau_move=move,
        stmt_note=" plus the official statement text" if statement_text else "",
        statement=f"\nOFFICIAL STATEMENT:\n{statement_text}\n" if statement_text else "")
    def _fail():
        # Remember the failure for these exact quotes so calendar_check (every
        # 10 min, for up to 3 h) doesn't re-run the whole model chain on them;
        # a new quote changes the key and earns a fresh attempt.
        if store is not None:
            from .utils_time import iso_utc, now_utc
            store.upsert("translation_cache", {"cache_key": key, "source_preview": win["title"][:80],
                                               "thai_text": json.dumps({"failed": True}),
                                               "hits": "0", "created_at": iso_utc(now_utc())})
        return None

    text = _llm(prompt)
    if not text:
        return _fail()
    from .news_alert import _norm_bias, _norm_conf, _parse_json_lenient
    d = _parse_json_lenient(text)
    if not d:
        return _fail()
    from .translator import _has_cjk, _patch_names, _patch_places, strip_em_dash

    def clean(s):
        return strip_em_dash(_patch_places(_patch_names(str(s or "")))).strip()
    out = {
        "tone": str(d.get("tone") or "neutral").lower(),
        "summary_th": clean(d.get("summary_th")),
        "key_points_th": [clean(x) for x in (d.get("key_points_th") or [])][:3],
        "gold_bias": _norm_bias(d.get("gold_bias")),
        "gold_confidence": _norm_conf(d.get("gold_confidence")),
        "why_th": clean(d.get("why_th")),
    }
    texts = [out["summary_th"], out["why_th"], *out["key_points_th"]]
    if not out["summary_th"] or any(_has_cjk(t) for t in texts):
        log.warning("speech analysis unusable (empty/CJK) win=%s", win["id"])
        return _fail()
    if store is not None:
        from .utils_time import iso_utc, now_utc
        store.upsert("translation_cache", {"cache_key": key, "source_preview": win["title"][:80],
                                           "thai_text": json.dumps(out, ensure_ascii=False),
                                           "hits": "1", "created_at": iso_utc(now_utc())})
    return out


# ---------------------------------------------------------------- stage runner (calendar_check)

def due_stages(win: dict, now: datetime, n_quotes: int, cfg: dict, sent) -> list[str]:
    """Which cards are due for this window: 'mid' (once, after mid_after_min,
    with enough quotes, before the end) and 'end' (once, after the window)."""
    start, end = _dt(win["start"]), _dt(win["end"])
    out = []
    if (now >= start + timedelta(minutes=int(cfg["mid_after_min"])) and now < end
            and n_quotes >= int(cfg["min_quotes"]) and not sent("mid")):
        out.append("mid")
    if now >= end and now <= end + timedelta(hours=3) and not sent("end"):
        out.append("end")
    return out


def run(store, line, target, sched_cfg, now: datetime, push, delivered) -> int:
    """Send due speech cards. `push(alt, bubble)` and `delivered(resp)` are the
    caller's quota/quiet-hours-aware push + success check. Returns cards sent."""
    cfg = cfg_from(sched_cfg)
    if not cfg.get("enabled", True):
        return 0
    from .line_flex import speech_bubble
    from .utils_time import iso_utc
    sent_n = 0
    for w in stored_windows(store):
        quotes = load_buffer(store, w["id"])

        def sent(stage, _w=w):
            key = f"speech_{stage}:{_w['id']}"
            return bool(store.get("sent_log", (key, "speech"))
                        or store.get("sent_log", (key, SKIP_ROUTE)))
        for stage in due_stages(w, now, len(quotes), cfg, sent):
            if stage == "end" and not quotes and "statement" not in w["title"].lower() \
                    and "rate" not in w["title"].lower():
                # Nothing was captured for a plain speech: mark done, no empty card.
                # (own route_type: a skip is not a failed delivery in delivery_stats)
                store.upsert("sent_log", {"event_id": f"speech_end:{w['id']}", "route_type": SKIP_ROUTE,
                                          "sent_ts": iso_utc(now), "line_status": "skipped"})
                continue
            start = _dt(w["start"])
            try:
                from . import spot_feed
                mv = spot_feed.move_since(store, start, now, spot_feed.current_spot())
            except Exception:  # noqa: BLE001
                mv = None
            move = (f"{'+' if mv['usd'] >= 0 else '-'}${abs(mv['usd']):,.1f} over "
                    f"{mv['minutes']} min (spot)") if mv else None
            stmt = None
            if w["country"] == "USD" and re.search(r"FOMC Statement|Federal Funds Rate", w["title"], re.I):
                stmt = fetch_fomc_statement(now)
            a = analyze(w, quotes, stage, move, stmt, store)
            if not a:
                continue
            bubble = speech_bubble(w, a, len(quotes), mv, stage)
            head = "🎙️ ระหว่างแถลง" if stage == "mid" else "🎙️ สรุปถ้อยแถลง"
            resp = push(f"{head} · {w['title']} · {a['summary_th']}", bubble)
            if not delivered(resp):
                continue
            store.upsert("sent_log", {"event_id": f"speech_{stage}:{w['id']}", "route_type": "speech",
                                      "sent_ts": iso_utc(now), "line_status": resp["status"]})
            sent_n += 1
            if stage == "end" and a["gold_bias"] in ("bullish", "bearish"):
                store.upsert("calibration_log", {
                    "event_id": f"speech:{w['id']}",
                    "first_seen_ts": iso_utc(now),     # grade what happens AFTER the call
                    "topic_bucket": "speech", "routed_as": "speech",
                    "title": w["title"][:300], "country": w["country"],
                    "predicted_dir": "bull" if a["gold_bias"] == "bullish" else "bear",
                    "predicted_verdict_th": a["summary_th"][:300],
                    "xau_return_5m": "", "xau_return_15m": "", "xau_return_30m": "",
                    "xau_base_price": "", "actual": "", "forecast": "",
                    "surprise": "", "xau_return_60m": "",
                })
    return sent_n
