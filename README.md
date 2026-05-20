# Gold News Intelligence Pipeline

RSS news + ForexFactory economic calendar + FRED actuals →
LINE Flex Messages, scored, deduped, and rate-limited.

Runs on GitHub Actions, stores state in Google Sheets, pushes to LINE.
Free for public repos, ~$0 / month at current scale.

## Run modes

```
python -m src.main --mode cron              # */5 min — RSS fetch + cluster + score
python -m src.main --mode event             # Tier-0 hot loop (30 min) — fired by Calendar Bot
python -m src.main --mode digest            # build digest if now_ict ∈ ±5m of a slot
python -m src.main --mode calendar_daily    # push today's economic calendar (06:30 ICT)
python -m src.main --mode calendar_check    # T-15min pre + post-release sweep (every 10 min Mon-Fri)
python -m src.main --mode maintain          # daily — purge stale event_state / sent_log
```

## Workflows (UTC schedules)

| Workflow | Cron | Purpose |
|---|---|---|
| `news-cron` | `*/5 * * * *` | RSS fetch + cluster + Flex push |
| `event-mode` | `repository_dispatch` type=`news-event-mode` | Tier-0 hot poll (30 min, fired ~5 min before CPI/NFP/FOMC) |
| `calendar-daily` | `30 23 * * *` (= 06:30 ICT) | Today's calendar Flex |
| `calendar-check` | `*/10 * * * 1-5` | Pre-release (T-15) + post-release alerts |
| `maintain` | `0 16 * * *` (= 23:00 ICT) | Purge old `event_state` + `sent_log` |

## Repository secrets

| Secret | Required | Purpose |
|---|---|---|
| `LINE_CHANNEL_TOKEN` | yes | LINE Messaging API channel token |
| `LINE_NEWS_TARGET` | yes | userId / groupId for news + calendar pushes |
| `LINE_HEALTH_TARGET` | yes | userId / groupId for health warnings + recoveries |
| `GSHEET_ID` | yes | Google Sheet ID for state tabs |
| `GSHEET_CREDS` | yes | Service-account JSON (full file pasted in) |
| `FRED_API_KEY` | optional | Enables actual values + surprise calc in post-release alerts. Free signup at https://fred.stlouisfed.org/docs/api/api_key.html |

## Google Sheet tabs (auto-created on first run)

- `source_state` — per-source fetch timestamps + ETags
- `event_state` — clustered news events with scores
- `sent_log` — idempotency for LINE pushes
- `calibration_log` — every score ≥ 2 event (kept forever for Phase 3 backfill)
- `health_log` — per-(source, warning_type) warning records

## Sources

| Tier | Source | Role | Status |
|---|---|---|---|
| 0 | `fed` | Federal Reserve policy | ✅ enabled |
| 0 | `bls` | BLS data releases (CPI/NFP/PPI) | ✅ enabled |
| 0 | `ecb` | ECB policy | ✅ enabled |
| 0 | `treasury` | US Treasury yields | ❌ no public RSS endpoint |
| 1 | `bbc_world` | Geopolitics | ✅ enabled |
| 1 | `aljazeera` | Geopolitics | ✅ enabled |
| 1 | `cnbc` | Macro | ✅ enabled |
| 1 | `marketwatch` | Macro | ✅ enabled |
| 2 | `forexlive` | Trader-macro | ✅ enabled |
| 2 | `fxstreet` | Gold-bias commentary | ✅ enabled |
| 3 | `kitco` | Gold context | ❌ Kitco discontinued free RSS |

Edit `config/sources.yaml` to flip `enabled: true/false`.

## Phase 1 invariants (locked)

- `event_id = hash(topic_bucket + entity + direction_label + 15m_bucket)` —
  headline NOT in the key.
- Rate-limit 5 BREAKING+ALERT / 15 min; overflow downgrades to digest
  (never dropped).
- Always-pass: Tier 0 official scheduled (CPI/NFP/FOMC) + score-5 confirmed.
- One state load per run; batch flush only changed rows; `updated_at` on
  every row.
- Freshness anchor uses earliest `published_ts` (not `first_seen_ts`) to
  prevent false breaking on cold start.
- No LLM, no Apify, no sub-minute polling, no price-feed.

## Opt-in tightening

If single-source BREAKING (typically ForexLive) is too noisy:

```yaml
# config/schedule.yaml
routing:
  breaking_require_confirmation: true   # default false
```

When true, score ≥ 4.5 events that aren't from an official source AND
don't have source_count ≥ 2 are downgraded to ALERT (still pushed but
visually less alarming).

## FRED-backed post-release

When `FRED_API_KEY` is set, post-release alerts upgrade from directional
guidance to a specific verdict using the released number:

```
📊 Released — Watch              Just released
─────────────────────────────────
🕐 21:30 ICT · [High] · USD
Core CPI m/m
Actual    Forecast    Previous
+0.5%      0.3%        0.4%
─────────────────────────────────
🟢 BEAT vs forecast
Gold Impact: 🔴 Bearish gold
Higher inflation lifts USD/yields
```

16 series supported — CPI / Core CPI / PCE / Core PCE / PPI / Core PPI /
Retail Sales / Durable Goods / NFP / Unemployment Rate / Initial Claims /
Continuing Claims / Building Permits / Housing Starts / GDP q/q /
Fed Funds. ISM PMIs remain directional-only (not in FRED free tier).

## Calendar Bot v2.6 integration

Event-mode is fired by `repository_dispatch`. To wire your existing
GoldBot Calendar V2 (GAS) to fire it ~5 min before high-impact releases,
see [docs/INTEGRATION_CALENDAR_BOT.md](docs/INTEGRATION_CALENDAR_BOT.md).

## Roadmap

- **Phase 2 (deferred):** X relay via Apify + worker always-on,
  `independent_source_count` replacing raw `source_count`.
- **Phase 3 (deferred):** Backfill `xau_return_5m / 15m / 30m` in
  `calibration_log` from a price feed → calibrate `base_impact` and
  per-source weights from real reaction data.
- **Phase 4 (deferred):** Benzinga websocket / webhook for sub-minute
  newsflow.

## Local development

```
git clone https://github.com/walight999/gold-news-pipeline
cd gold-news-pipeline
pip install -r requirements.txt
cp .env.example .env       # fill in values
pytest -q                  # 44 tests
python -m src.main --mode cron
```
