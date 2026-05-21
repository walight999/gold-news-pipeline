# Gold News Intelligence Pipeline

RSS news + ForexFactory economic calendar + FRED actuals →
LINE Flex Messages, scored, deduped, rate-limited, and **translated to Thai**.

Runs on **GitHub Actions** (free for public repos), stores state in
**Google Sheets**, pushes to **LINE Messaging API**. ~$0 / month at current
scale; Claude translation costs ~$1–3 / month if enabled (optional).

> **Output language:** Thai 🇹🇭. Flex bubble labels and inline translations
> are hard-coded Thai. If you need English / another language, fork and
> i18n `src/line_flex.py`. Source headlines themselves stay original.

## Run modes

```
python -m src.main --mode cron              # */5 min — RSS fetch + cluster + score
python -m src.main --mode event             # Tier-0 hot loop (30 min) — fired by Calendar Bot
python -m src.main --mode digest            # build digest if now_ict ∈ ±5m of a slot
python -m src.main --mode calendar_daily    # push today's economic calendar (06:30 ICT)
python -m src.main --mode calendar_check    # T-15min pre + post-release sweep (every 10 min Mon-Fri)
python -m src.main --mode weekly_preview    # push next week's calendar (Sat 06:30 ICT)
python -m src.main --mode eod_recap         # daily recap (22:00 ICT)
python -m src.main --mode watchdog          # self-monitor — alert on silent failure
python -m src.main --mode verify_sources    # manual health audit of all sources
python -m src.main --mode maintain          # daily — purge stale event_state / sent_log
```

## Workflows (UTC schedules)

| Workflow | Cron | Purpose |
|---|---|---|
| `news-cron` | `*/5 * * * *` | RSS fetch + cluster + Flex push |
| `event-mode` | `repository_dispatch` type=`news-event-mode` | Tier-0 hot poll (30 min, fired ~5 min before CPI/NFP/FOMC) |
| `calendar-daily` | `30 23 * * *` (= 06:30 ICT) | Today's calendar Flex with XAU/DXY snapshot |
| `calendar-check` | `*/10 * * * 1-5` | Pre-release (T-15) + post-release alerts with FRED actuals |
| `weekly-preview` | `30 23 * * 5` (= Sat 06:30 ICT) | Next week's high-impact calendar |
| `eod-recap` | `0 15 * * 1-5` (= 22:00 ICT) | Daily news + price summary |
| `watchdog` | `*/30 * * * *` | Pipeline self-monitor — alerts on silent failure |
| `maintain` | `0 16 * * *` (= 23:00 ICT) | Purge old `event_state` + `sent_log` |

GitHub free-tier cron drops ~96% of `*/5 * * * *` slots — if you need
reliable cron, drive `news-cron` from a Google Apps Script trigger via
`workflow_dispatch`. See [docs/INTEGRATION_CALENDAR_BOT.md](docs/INTEGRATION_CALENDAR_BOT.md).

## Repository secrets

| Secret | Required | Purpose |
|---|---|---|
| `LINE_CHANNEL_TOKEN` | yes | LINE Messaging API channel token |
| `LINE_NEWS_TARGET` | yes | userId / groupId for news + calendar pushes |
| `LINE_HEALTH_TARGET` | yes | userId / groupId for health warnings + recoveries |
| `GSHEET_ID` | yes | Google Sheet ID for state tabs |
| `GSHEET_CREDS` | yes | Service-account JSON (full file pasted in) |
| `FRED_API_KEY` | optional | Enables actual values + surprise calc in post-release alerts. Free signup at https://fred.stlouisfed.org/docs/api/api_key.html |
| `ANTHROPIC_API_KEY` | optional | Claude Haiku for higher-quality Thai translation (finance jargon + correct proper-noun forms like ทรัมป์ / ปูติน / สี จิ้นผิง). Falls back to free Google Translate when unset. |

## Google Sheet tabs (auto-created on first run)

- `source_state` — per-source fetch timestamps + ETags + pipeline heartbeat
- `event_state` — clustered news events with scores
- `sent_log` — idempotency for LINE pushes
- `calibration_log` — every score ≥ 2 event (kept forever for Phase 3 backfill)
- `health_log` — per-(source, warning_type) warning records, incl. watchdog

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
| 1 | `yahoo_finance` | Macro | ✅ enabled |
| 1 | `investing_general` | Macro | ✅ enabled |
| 1 | `investing_cb` | Central banks | ✅ enabled |
| 1 | `investing_commodities` | Commodities | ✅ enabled |
| 1 | `benzinga` | Fast newsflow | ✅ enabled |
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
- Quiet hours 04:00–05:00 ICT — pushes (except daily calendar at 04:40)
  are suppressed during the market-close window.

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

## Thai translation

Breaking / Alert / Digest events carry inline Thai title + summary so the
trader gets full context without clicking through.

Two-backend waterfall:

1. **Claude Haiku** (when `ANTHROPIC_API_KEY` is set) — best quality.
   Explicit in-prompt glossary covers 25+ proper nouns (Trump → ทรัมป์,
   Xi Jinping → สี จิ้นผิง, Putin → ปูติน, Powell → พาวเวลล์, Lagarde →
   ลาการ์ด, etc.), countries (จีน / รัสเซีย / สหรัฐ / ยุโรป), and 40+
   finance terms (เงินเฟ้อ, ภาวะถดถอย, อัตราผลตอบแทน, สินทรัพย์ปลอดภัย,
   ท่าทีเข้มงวด/ผ่อนคลาย, ลด/ขึ้นดอกเบี้ย, ฯลฯ).
2. **Google Translate** (free, fallback) — kicks in when Claude fails or
   the API key isn't set. Post-process regex catches the most common
   English-name leaks (Trump / Putin / Powell / etc.) since Google
   doesn't follow our prompt.

**CJK leak validator**: rejects any translation that contains Chinese /
Japanese / Korean script characters. Reuters-style sources sometimes
carry Chinese names verbatim (习近平) — Claude transliterates them
properly; Google passes them through unchanged, so the validator forces
fallback to ensure pure Thai output.

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
Fed Funds. For non-FRED series (ISM PMIs, flash PMIs, etc.), the
pipeline falls back to **ForexFactory HTML scrape** via `curl_cffi`
(safari17_0 impersonation bypasses Cloudflare) to pull the actual value.

## Self-monitoring

The `watchdog` workflow runs every 30 min and reads the pipeline
heartbeat (a synthetic row in `source_state`). It pushes a LINE health
alert when:

- **`watchdog_silence`** — heartbeat hasn't ticked in >25 min. Cron stopped
  firing, OR Sheet writes failed, OR the runner crashed.
- **`watchdog_no_items`** — heartbeat is fresh but 0 items have been
  fetched across all sources for >180 min. All scrapers dead simultaneously
  (network down, Cloudflare blocking, RSS endpoints all dead at once).

Cooldown-gated (120 min) so it won't spam during an extended outage. A
recovered bubble is pushed automatically when the warning clears.

## Calendar Bot v2.6 integration

Event-mode is fired by `repository_dispatch`. To wire your existing
GoldBot Calendar V2 (GAS) to fire it ~5 min before high-impact releases,
see [docs/INTEGRATION_CALENDAR_BOT.md](docs/INTEGRATION_CALENDAR_BOT.md).

## Roadmap

- **Phase 3 (deferred):** Backfill `xau_return_5m / 15m / 30m` in
  `calibration_log` from a price feed → calibrate `base_impact` and
  per-source weights from real reaction data. Needs ~2 months of data.
- **Phase 4 (deferred):** Benzinga websocket / webhook for sub-minute
  newsflow; LINE webhook commands (requires Cloudflare Workers hosting);
  Apify Twitter relay.

## Limitations

- **Output is Thai-locked** — Flex labels in `src/line_flex.py` are
  hard-coded Thai. Fork + i18n to localize.
- **LINE setup is non-trivial** — you need a LINE Messaging API
  channel + a userId / groupId to push to. See
  https://developers.line.biz/.
- **Google Sheets quota** — Sheets API allows 60 reads + 60 writes per
  minute per user. At current scale (1 cron / 5 min, batched writes) this
  is comfortably within limits, but if you fork and add high-frequency
  modes, watch for 429s.
- **GitHub Actions cron drops** — free-tier `*/5` cron drops ~96% of
  slots during peak hours. Use a GAS dispatcher (see above) for reliable
  triggering.
- **No price feed cache** — yfinance calls go straight through; transient
  429s show as "no data" in calendar / EOD bubbles. Roadmap.
- **No translation cache** — every cron call re-translates same/similar
  titles. Wasteful of Claude API tokens; consider adding a SHA-keyed
  cache tab if running at scale.

## Local development

```
git clone https://github.com/walight999/gold-news-pipeline
cd gold-news-pipeline
pip install -r requirements.txt
cp .env.example .env       # fill in values
pytest -q                  # 68 tests
python -m src.main --mode cron
```

## License

MIT — see [LICENSE](LICENSE). Use, modify, redistribute freely; just
keep the copyright notice. PRs welcome.
