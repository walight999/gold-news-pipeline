"""Emit a small headlines snapshot for agent-hq's adapter to read.

Reads the most recent scored events from the pipeline's Google-Sheets state
(`event_state` tab) and writes the top N to snapshots/headlines.json — a tiny,
agent-hq-readable file consumed by the `gold-news-headlines` tool adapter.

Run:  python scripts/snapshot_headlines.py
Cron: add as a final step in a GitHub Actions workflow (env: GSHEET_ID,
      GSHEET_CREDS) so the snapshot stays fresh.

Note: `event_state` stores the *clustered* event (topic / entity / direction /
score), NOT the raw article title — the pipeline only keeps raw titles in
memory. So the headline here is synthesized from those structured fields, which
is real routed data, not a placeholder.

Output schema (what the agent-hq adapter slices `headlines[:5]` from):
{
  "status": "ok" | "empty",
  "headlines": [
    {"ts": "...", "title": "US CPI · hawkish", "source": "Reuters, Bloomberg",
     "score": 4.2, "xau_relevant": true},
    ...
  ],
  "count": N,
  "written_at": "2026-...Z"
}
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "snapshots" / "headlines.json"
SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

# Routed-as-digest threshold (router.py: ≥2.5 → digest). Events at/above this
# were actionable enough to surface, so we mark them xau_relevant.
DIGEST_THRESHOLD = 2.5


def _humanize(s: str) -> str:
    return (s or "").replace("_", " ").strip()


def _source_label(source_list: str, max_n: int = 3) -> str:
    """`source_list` is persisted comma-joined (e.g. 'reuters,bloomberg')."""
    ids = [s for s in (source_list or "").split(",") if s.strip()]
    names = [_humanize(s).title() for s in ids[:max_n]]
    out = ", ".join(names)
    extra = len(ids) - max_n
    if extra > 0:
        out += f" +{extra}"
    return out


def _build_title(row: dict) -> str:
    entity = _humanize(row.get("entity"))
    topic = _humanize(row.get("topic_bucket")).title()
    direction = _humanize(row.get("direction_label"))
    head = " · ".join(p for p in (entity, topic) if p) or (topic or "ข่าวทอง")
    if direction and direction.lower() not in ("neutral", "none", "flat"):
        head = f"{head} · {direction}"
    return head


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _load_event_rows(limit: int) -> list[dict]:
    """Read event_state via the pipeline Store, newest first. Requires
    GSHEET_ID + GSHEET_CREDS in the environment (set in GitHub Actions)."""
    from src.store import Store  # type: ignore

    store = Store.from_env()
    store.connect()
    store.load_all()
    rows = store.all_rows("event_state")
    rows.sort(
        key=lambda r: (r.get("last_seen_ts") or r.get("first_seen_ts") or ""),
        reverse=True,
    )
    return rows[:limit]


def build_headlines(limit: int) -> list[dict]:
    headlines = []
    for r in _load_event_rows(limit):
        score = _to_float(r.get("score"))
        headlines.append({
            "ts":           r.get("last_seen_ts") or r.get("first_seen_ts"),
            "title":        _build_title(r),
            "source":       _source_label(r.get("source_list")),
            "score":        round(score, 3),
            "xau_relevant": score >= DIGEST_THRESHOLD,
        })
    return headlines


def main(limit: int = 10) -> int:
    try:
        headlines = build_headlines(limit)
    except Exception as e:
        print(f"WARN: could not load events ({e}) — emitting empty snapshot",
              file=sys.stderr)
        headlines = []

    payload = {
        "status":     "ok" if headlines else "empty",
        "headlines":  headlines,
        "count":      len(headlines),
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    SNAPSHOT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(headlines)} headlines to {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
