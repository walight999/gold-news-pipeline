# `push_ff_to_gas.py` — setup + ops guide

Sender half of the FF prefetch flow. Companion receiver (the
WeeklyCache.gs WebApp) lives in the **private** gold-analyst repo. Both
halves are needed for the LINE bot's Saturday Weekly Brief + daily
Calendar V2 to read fresh FF data.

```
THIS REPO (public)                         gold-analyst (private)
────────────────────────────────           ──────────────────────────────
scripts/push_ff_to_gas.py                  scripts/WeeklyCache.gs
  ├─ Fri 22:00 / Sat 03:00 / 06:00 ICT
  │    --mode weekly
  │    └─ scrape FF HTML next-week  ────►  doPost (kind: weekly_html)
  │       (src/ff_scraper.py reused)         └─ ff_weekly_cache_v1
  │                                            ScriptProperty
  │
  └─ 04:30 / 12:30 / 20:30 ICT Mon-Fri
       --mode thisweek
       └─ fetch FF thisweek.json    ────►  doPost (kind: thisweek_json)
          (US IP avoids 429)                 └─ FF_LAST_JSON
                                              ScriptProperty (same key
                                              ffFetchWeekJson already reads)
```

## Why this lives here, not in gold-analyst

1. **Free GHA minutes** — public repo = unlimited; gold-analyst exhausted
   its private free tier 2026-05-21 and its scheduled jobs have been
   failing silently since.
2. **No duplicate scrape code** — `src/ff_scraper.py` already has
   `scrape_ff_html()` with curl_cffi `chrome124+` Cloudflare bypass.
   `push_ff_to_gas.py` is a thin POST adapter over the existing function.
3. **Honest architecture** — fetching calendar data is news/calendar
   infrastructure; the trading-specific Make/LINE wiring stays private.

## One-time setup

### 1. Deploy the GAS WebApp first

The GAS side must be deployed before secrets can be set here. See
`scripts/WEEKLY_CACHE_GAS_SETUP.md` in the gold-analyst repo:

1. Paste `gold-analyst/scripts/WeeklyCache.gs` into the GAS project that
   owns `goldbot_gas.js` / `Code.gs`.
2. Add `GAS_WEBAPP_TOKEN` ScriptProperty (32+ char random).
3. Deploy as Web app → Execute as: Me · Access: Anyone.
4. Copy the `/exec` URL.

### 2. Set GHA secrets on this repo

Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `GAS_WEBAPP_URL` | the `/exec` URL from GAS deploy step |
| `GAS_WEBAPP_TOKEN` | the **same string** that's in the GAS Script Property |

### 3. Dry-run weekly

Actions tab → `ff-gas-weekly` → Run workflow → ✅ `Dry run` → Run.

Expect:
```
INFO scrape_ff_html: ~100-115 total events
INFO week 2026-XX-XX · Mon-Fri events: ~95-105
INFO by impact: {'Low': ~75, 'High': ~8, ...}
INFO high-impact: 5-15
INFO   2026-XX-XXT08:30 USD · ...
INFO DRY_RUN — skipping POST
```

If you see:
- `Total parsed events: 0` → FF HTML schema changed; check
  `src/ff_scraper.py:scrape_ff_html` selectors.
- `zero Mon-Fri events parsed` while total > 0 → next-Monday calculation
  off (timezone, weekend run); inspect `_next_workweek_monday_target_dates`.
- HTTP errors / Cloudflare challenges → curl_cffi profile list in
  `src/ff_scraper.py:_IMPERSONATIONS` may need a newer Chrome version.

### 4. Real run (after dry-run passes)

Actions → `ff-gas-weekly` → Run workflow → ❌ uncheck Dry run → Run.

Expect: `GAS response: {'ok': True, 'kind': 'weekly_html', 'stored': N, ...}`.

Then in GAS editor: run `__debugWeeklyCache` (defined in WeeklyCache.gs).
Expect age < 1h and matching event count.

### 5. Dry-run thisweek

Actions → `ff-gas-thisweek` → Run workflow → ✅ Dry run → Run.

Expect:
```
INFO FF JSON: ~100-300 items, ~30-50000 bytes
INFO date span: ['2026-XX-XX', '2026-XX-XX']
INFO DRY_RUN — skipping POST
```

### 6. Real-run thisweek

Same as step 4 but for the `ff-gas-thisweek` workflow. Then in GAS editor:

```javascript
function __debugThisWeekCache() {
  var P = PropertiesService.getScriptProperties();
  Logger.log('FF_LAST_FETCH_TS: ' + P.getProperty('FF_LAST_FETCH_TS'));
  Logger.log('FF_LAST_JSON bytes: ' + (P.getProperty('FF_LAST_JSON') || '').length);
  Logger.log('FF_LAST_SOURCE: ' + P.getProperty('FF_LAST_SOURCE'));
  Logger.log('FF_LAST_PREFETCH_AT_ICT: ' + P.getProperty('FF_LAST_PREFETCH_AT_ICT'));
}
```

(Paste it into WeeklyCache.gs as a helper; not bundled to keep the file
focused on the receiver path.)

Expect `FF_LAST_SOURCE: gha-prefetch` and bytes 15000-50000.

## Schedule reference

| Workflow | Cron (UTC) | ICT | Purpose |
|---|---|---|---|
| ff-gas-weekly | `0 15 * * 5` | Fri 22:00 | Primary — gives Sat 06:30 brief headroom |
| ff-gas-weekly | `0 20 * * 5` | Sat 03:00 | Retry if Fri fetch failed |
| ff-gas-weekly | `0 23 * * 5` | Sat 06:00 | Last chance before Sat 06:30 brief |
| ff-gas-thisweek | `30 21 * * 0-4` | 04:30 Mon-Fri | Before 05:00 ICT broadcast |
| ff-gas-thisweek | `30 5 * * 1-5` | 12:30 Mon-Fri | Before 13:00 ICT broadcast |
| ff-gas-thisweek | `30 13 * * 1-5` | 20:30 Mon-Fri | Before 21:00 ICT broadcast |

Each successful run overwrites the GAS cache; later same-window runs are
no-ops at the GAS side (idempotent).

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `FF scrape: every impersonation failed` | Cloudflare blocking all profiles | Add a newer profile (e.g. `chrome133a`) to `_IMPERSONATIONS` in `src/ff_scraper.py` |
| `Total parsed events: 0` | FF HTML schema change | Update CSS selectors in `scrape_ff_html` |
| GAS HTTP 401 / `"unauthorized"` | Token mismatch | Re-set `GAS_WEBAPP_TOKEN` on both sides (same value) |
| GAS HTTP 200 but `"ok": false` | See `error` field in response body | Common: payload shape mismatch — check WeeklyCache.gs `handleWeeklyHtml` / `handleThisWeekJson` |
| Cache present but Sat brief still warns "FF not rolled" | Code.gs `ffLoadNextWeek_()` not wire-in'd | Apply `gold-analyst/scripts/A1_WIRE_IN_ffLoadNextWeek.md` |
| All 3 weekly runs fail Fri-Sat | FF down / CF hardened | Manual brief that week + investigate; `gold-analyst/scripts/populateFFCacheNow.gs` is the emergency manual loader |

## Health monitoring

This script does NOT integrate with `src/health.py` watchdog (intentional
— watchdog reads `source_state` rows, but the push is one-shot and the
GAS side has its own cache-age check via `__debugWeeklyCache`).

If you want alerts on GHA failures, GitHub already emails repo
notifications for failed workflow runs. For sub-day alerts, consider
adding a 5th `source_state` row `_ff_gas_push` with a heartbeat
timestamp — defer until needed.
