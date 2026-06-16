"""Emit a snapshot of social_feed drafts awaiting review, for agent-hq's
finisit-tradetongkam EDITOR adapter (social-pending) to read via raw GitHub.

The tradetongkam agent in Agent HQ is the editor-in-chief over this pipeline's
draft queue: the pipeline composes + posts, the agent reviews each pending draft
against brand voice + the macro thesis and recommends approve/hold/edit. For it
to have a real queue to act on, we expose the rows the pipeline composed that
no one has approved or posted yet.

Pending = `approved` blank (operator hasn't typed yes) AND `posted` blank.

Run: python scripts/snapshot_social_pending.py
Cron: .github/workflows/snapshot_social.yml (every 30 min, commits the JSON).

Output schema (snapshots/social_pending.json):
{
  "pending": [
    {"row": 12, "ts": "...", "type": "alert", "category": "...", "tone": "...",
     "impact_level": "...", "headline_th": "...", "tweet_text": "..."},
    ...
  ],
  "count": N,
  "written_at": "2026-..."
}

Best-effort by design: never raises — on any failure it writes an empty
snapshot so the editor adapter sees `count: 0` rather than stale data.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "snapshots" / "social_pending.json"
SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))

FEED_TAB = "social_feed"
_APPROVED_YES = {"yes", "y", "true", "1", "approved"}


def _is_pending(row: dict) -> bool:
    approved = str(row.get("approved", "")).strip().lower()
    posted = str(row.get("posted", "")).strip()
    return approved not in _APPROVED_YES and not posted


def main(limit: int = 15) -> int:
    pending: list[dict] = []
    try:
        from src.store import Store  # type: ignore

        store = Store.from_env()
        store.connect()
        _, rows = store.read_feed(FEED_TAB)
        for r in rows:
            if not _is_pending(r):
                continue
            if not str(r.get("tweet_text", "")).strip():
                continue
            pending.append({
                "row":          r.get("_row"),
                "ts":           r.get("ts_ict") or r.get("ts_utc"),
                "type":         r.get("type"),
                "category":     r.get("category"),
                "tone":         r.get("tone"),
                "impact_level": r.get("impact_level"),
                "headline_th":  r.get("headline_th"),
                "tweet_text":   r.get("tweet_text"),
            })
        # social_feed is append-only (newest last) — keep the most recent `limit`.
        pending = pending[-limit:]
    except Exception as ex:  # noqa: BLE001 — best-effort, must never crash a run
        print(f"WARN: social_pending snapshot failed: {ex}", file=sys.stderr)

    payload = {
        "pending":    pending,
        "count":      len(pending),
        "written_at": datetime.utcnow().isoformat() + "Z",
    }
    SNAPSHOT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(pending)} pending drafts to {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
