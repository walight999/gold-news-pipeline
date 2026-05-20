# Gold News Intelligence Pipeline — Phase 1

RSS → dedup → score → LINE push (+ calibration log).
Runtime: Python on GitHub Actions. Store: Google Sheets. Budget: free–$5.

## Run modes

```
python -m src.main --mode cron      # */5 min — respect each source's poll_min
python -m src.main --mode event     # 30-min in-job loop, Tier 0 only (triggered by Calendar Bot dispatch)
python -m src.main --mode digest    # build a digest if now_ict is within ±5m of a slot
```

## Repository secrets required

- `LINE_CHANNEL_TOKEN` — LINE Messaging API channel access token
- `LINE_NEWS_TARGET` — userId / groupId for breaking + digest
- `LINE_HEALTH_TARGET` — userId / groupId for health/heartbeat
- `GSHEET_ID` — Google Sheet ID containing the 5 state tabs
- `GSHEET_CREDS` — service-account JSON (paste the whole file content as one secret)

## Sheets tabs (auto-created on first run)

`source_state`, `event_state`, `sent_log`, `calibration_log`, `health_log` — all carry `updated_at`.

## Phase 1 invariants

- No LLM. No Apify. No sub-minute polling. No price-feed.
- `event_id = hash(topic_bucket + entity + direction_label + time_window)` — headline NOT in key.
- One state load per run; batch write only changed rows at end.
- Breaking/alert rate-limit 5 / 15m; overflow → digest (never dropped).
- Always-pass: Tier 0 scheduled (CPI/NFP/FOMC) and score-5 official-or-confirmed events.

See the build spec (`docs/SPEC.md`) for full details if you keep it; the canonical source is the conversation thread.
