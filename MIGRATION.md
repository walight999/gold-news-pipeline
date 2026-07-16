# Gold Ecosystem Consolidation — Migration Plan

**Goal:** Collapse 3 codebases (gold-analyst Python + goldbot-line GAS + Make scenario 5656446)
into ONE Python pipeline running in this repo.

**Why:** Today 3 different runtimes push to the SAME LINE channel using 3 different LINE
credentials. Maintaining 3 codebases = 3× bugs, 3× deploy paths, 3× failure modes,
no single source of truth for "what got pushed to LINE about gold today".

**Status:** PLANNED — not yet executed. Live trading brief (3x daily) is critical path;
migration must be staged + tested in parallel before kill.

**Last updated:** 2026-05-26 by Agent HQ Council

---

## Architecture: today vs target

### Today
```
TradingView Pine alert ──webhook──▶ goldbot-line/Code.gs (218KB GAS)
                                          │
                                          ├──▶ LINE push (entry/exit)
                                          └──▶ LINE Phase 4 weekly brief (Sat)

gold-analyst (Windows Task)
  ├─ analyst.py: XAUUSD model
  └─ push_to_make.py ──HTTP──▶ Make 5656446 ──▶ LINE (3x daily brief)

gold-news-pipeline (GHA)
  ├─ news_cron.yml: RSS+FF+FRED → cluster+score+translate
  ├─ src/line_client.py: ──▶ LINE (news digest)
  ├─ ff_gas_weekly.yml: feeds FF data INTO Code.gs cache (1-way)
  └─ calendar_daily.yml: ──▶ LINE (06:30 ICT economic calendar)
```

3 LINE codepaths · 3 LINE tokens · 0 unified view of gold output

### Target (post-migration)
```
TradingView Pine alert ──webhook──▶ goldbot-line/Code.gs (~80 lines)
                                          │
                                          └──HTTP──▶ GHA repository_dispatch event

gold-news-pipeline (GHA — single source of truth)
  ├─ src/xau_signal/        ← migrated from gold-analyst
  │  ├─ analyst.py            (USD/UST→XAU model)
  │  ├─ confidence.py
  │  └─ currency_rules.py
  ├─ src/line_client.py     (existing — handles ALL LINE pushes)
  ├─ workflows/
  │  ├─ xau_daily_brief.yml      (cron: 0 0,8,16 * * * UTC = 07/15/23 ICT)
  │  ├─ xau_weekly_phase4.yml    (cron: Sat 23,0,1,2 UTC Fri = Sat 06/07/08/09 ICT)
  │  ├─ xau_live_entry.yml       (on: repository_dispatch xau-pine-alert)
  │  └─ (existing news/calendar/health workflows)
  └─ All output via src/line_client.py → ONE LINE token
```

1 LINE codepath · 1 LINE token · all output observable in GHA Actions tab

---

## 4 phases (execute in order, 2-3 weekends total)

### Phase A — Migrate 3x daily brief (lowest risk)
**Scope:** Move `push_to_make.py` logic into a new GHA workflow.

**Why first:** Non-interactive (cron only), no webhook latency, easy rollback.

**Steps:**
1. Create `src/xau_brief/composer.py` — port message composition from `gold-analyst/push_to_make.py`
2. Create `.github/workflows/xau_daily_brief.yml`:
   ```yaml
   on:
     schedule:
       - cron: '0 0,8,16 * * *'   # 07/15/23 ICT (UTC+7)
     workflow_dispatch:            # manual fire button
   jobs:
     compose-and-push:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - run: pip install -r requirements.txt
         - run: python -m src.xau_brief.compose_and_push
           env:
             LINE_CHANNEL_TOKEN: ${{ secrets.LINE_CHANNEL_TOKEN }}
             LINE_NEWS_TARGET:   ${{ secrets.LINE_NEWS_TARGET }}
   ```
3. **Parallel-run for 7 days** — Make scenario stays alive, GHA also fires. Compare output.
4. Day 8: disable Make scenario 5656446 (don't delete — keep as rollback). Watch for 7 more days.
5. Day 15: delete Make scenario + `push_to_make.py` from gold-analyst.

**Risk:** ⚠️ MEDIUM — GHA cron can drop slots. Mitigation: add `workflow_dispatch` manual trigger
+ Watchdog: if no push fires within 5min of cron slot, send LINE alert (re-use `health.py`).

**Rollback:** Re-enable Make scenario, re-enable Windows Task. Zero data loss.

**Owner:** `goldbot-news` (delivery infra) + `goldbot-quant` (validates output matches old brief).

---

### Phase B — Migrate Phase 4 weekly brief
**Scope:** Move `goldbot-line/Code.gs` Phase 4 weekly brief composition to Python.

**Why second:** Saturday-only test window = 1 attempt per week. Lower frequency = more time to verify.

**Existing infra (already in this repo):**
- `ff_gas_thisweek.yml` — pre-fetches ForexFactory data into GAS cache (1-way)
- `ff_gas_weekly.yml` — Saturday FF prefetch

**Steps:**
1. Port `goldbot-line/Code.gs` Phase 4 functions (`buildWeeklyBrief()`, `pushPhase4Card()`) → `src/xau_brief/weekly.py`
2. Create `.github/workflows/xau_weekly_phase4.yml`:
   ```yaml
   on:
     schedule:
       # Sat 06/07/08/09 ICT = Sat 23 UTC Fri / Sun 00/01/02 UTC
       - cron: '0 23 * * 5'  # Sat 06:00 ICT
       - cron: '0 0,1,2 * * 6'
     workflow_dispatch:
   ```
3. **Parallel-run for 2 Saturdays** — GAS still fires, GHA also fires. Compare.
4. Sat 3: disable GAS Phase 4 triggers (rename function to `_DISABLED_pushPhase4Card`).
5. Sat 4 onward: GHA only.

**Risk:** ⚠️ MEDIUM — Saturday is the critical "trader prep" briefing. Mitigation: GAS stays
disabled (not deleted) for 4 weeks before GAS code purge.

**Rollback:** Rename GAS function back to active name, deploy GAS update.

**Owner:** `goldbot-news` (workflow + composition) + `goldbot-quant` (brief content validation).

---

### Phase C — Migrate XAUUSD signal model
**Scope:** Move `gold-analyst/analyst.py + confidence.py + currency_rules.py + data_sources.py`
into `gold-news-pipeline/src/xau_signal/`.

**Why third:** Largest code move. Want Phase A/B stable first so we know LINE delivery works
before adding model risk.

**Steps:**
1. Create `src/xau_signal/` directory.
2. Copy (don't move yet) model files from `gold-analyst/` → `src/xau_signal/`
3. Update imports + add to `requirements.txt` any missing deps.
4. Write `tests/test_xau_signal.py` — compare new module output vs `gold-analyst/analyst.py`
   output on last 30 days of historical data. Must match exactly.
5. Update `xau_daily_brief.yml` (from Phase A) to call `src.xau_signal.compute()` instead of
   reading from Make payload.
6. **Parallel-run 7 days** — both `gold-analyst` Windows Task AND GHA compute + push.
   Compare each push.
7. Day 8: disable Windows Task for gold-analyst push.
8. Day 14: archive `gold-analyst/push_to_make.py`. Keep `analyst.py` as a "reference
   implementation" until Day 30.
9. Day 30: delete `gold-analyst/push_to_make.py + email_notify.py`. Move remaining
   gold-analyst code (broker, backtest, journal, doctor) → `src/xau_research/`.

**Risk:** ⚠️ HIGH — model output drives real trading decisions. Mitigation: NEVER skip the
parallel-run window. Bit-exact comparison of new vs old.

**Rollback:** Re-enable Windows Task. Original code path resumes.

**Owner:** `goldbot-quant` (model integrity) + `goldbot-news` (delivery infra).

---

### Phase D — Slim goldbot-line GAS to webhook proxy only
**Scope:** Reduce `goldbot-line/Code.gs` from 218KB → ~80 lines.

**Why last:** Need Phases A-C done before stripping GAS, because today goldbot-line is the
ONLY thing pushing live entries to LINE.

**New goldbot-line/Code.gs (~80 lines, after migration):**
```javascript
// Webhook proxy ONLY — forward TradingView Pine alerts to gold-news-pipeline GHA
function doPost(e) {
  const payload = JSON.parse(e.postData.contents);
  // Validate signature/secret (reject random callers)
  if (payload.secret !== PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET")) {
    return ContentService.createTextOutput("unauthorized").setMimeType(ContentService.MimeType.TEXT);
  }
  // Forward to GHA repository_dispatch
  UrlFetchApp.fetch("https://api.github.com/repos/walight999/gold-news-pipeline/dispatches", {
    method: "POST",
    headers: { Authorization: "token " + PropertiesService.getScriptProperties().getProperty("GH_PAT"),
               Accept: "application/vnd.github.v3+json" },
    payload: JSON.stringify({ event_type: "xau-pine-alert", client_payload: payload }),
  });
  return ContentService.createTextOutput("ok").setMimeType(ContentService.MimeType.TEXT);
}
```

GHA workflow `xau_live_entry.yml`:
```yaml
on:
  repository_dispatch:
    types: [xau-pine-alert]
jobs:
  compose-entry-card:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m src.xau_brief.live_entry
        env:
          PINE_PAYLOAD: ${{ toJSON(github.event.client_payload) }}
          LINE_CHANNEL_TOKEN: ${{ secrets.LINE_CHANNEL_TOKEN }}
```

**Latency check:** TradingView → GAS (1-2s) → GitHub dispatch (1-3s) → GHA workflow start
(5-30s) → LINE push (1-2s) = **~10-40 seconds total** for live entry. Acceptable for swing
trades; not for scalping.

**Risk:** ⚠️ MEDIUM — adds 5-30s latency vs current GAS-direct push. If user does scalping
needing sub-5s alerts, KEEP business logic in GAS for the live-entry path.

**Rollback:** Restore full Code.gs.

**Owner:** `goldbot-ops` (GAS slim) + `goldbot-news` (GHA dispatch handler).

---

## Phase decisions: what STAYS vs GOES

| Component | Today | Post-migration | Why |
|---|---|---|---|
| gold-analyst/analyst.py (model) | Windows Task | GHA cron in gold-news-pipeline | Reliable enough for 3x daily, free hosting |
| gold-analyst/push_to_make.py | Windows Task → Make | DELETED | Replaced by GHA workflow |
| Make scenario 5656446 | LINE push relay | DELETED | Direct LINE push from Python |
| goldbot-line/Code.gs (218KB) | XAUUSD bot deployed GAS | SLIM to ~80 lines | Webhook proxy only |
| goldbot-line/Code.gs Phase 4 brief | GAS Saturday composer | DELETED → GHA workflow | One brief composer in Python |
| goldbot-line/populateFFCacheNow.gs | FF cache populator | DELETED | FF data lives in GHA `ff_gas_*.yml` already |
| LINE_CHANNEL_TOKEN | 3 different tokens used | 1 token (gold-news-pipeline's) | Single source of truth |

**Net code reduction:** 218KB GAS → 80 lines GAS · `gold-analyst/` shrinks ~30% (push_to_make
+ email_notify gone) · Make config GUI → 0 · 1 LINE token to rotate (not 3).

---

## Roster impact (agent-hq side)

After all 4 phases complete:
- `goldbot-news` charter expands: "Source of truth for news AND all gold LINE delivery"
- `goldbot-quant` charter unchanged in spirit but cwd shifts: model code lives in
  `gold-news-pipeline/src/xau_signal/` (same repo, different folder)
- `goldbot-ops` charter shrinks: still owns alertbots fleet, but no longer owns 218KB GAS
- Could fold `goldbot-quant` + `goldbot-news` into single `goldbot-engine` agent — but keep
  separate for "model vs delivery" perspective in discussions.

---

## Open questions (decide before Phase A starts)

1. **Where does the model run for backtesting?** `backtest.py` is still in gold-analyst.
   Stay local, or move to GHA too? (Recommendation: stay local — backtests are interactive,
   developer-driven, not scheduled.)

2. **Does FRED API rate limit handle increased calls?** GHA will call FRED more often than
   today. Test in Phase A.

3. **Saturday GHA cron reliability?** Phase B depends on Sat brief firing on schedule.
   Add `workflow_dispatch` as manual safety net for first 4 Saturdays.

4. **Notion sync** — `gold-analyst/push_to_make.py` also writes to Notion via Make. If we
   kill Make, who writes to Notion? Options:
   - (a) gold-news-pipeline calls Notion API directly (add `notion-client` dep)
   - (b) Use Make for Notion only (keep scenario alive for Notion, not LINE)
   - (c) Skip Notion — `personal-notion` agent (agent-hq) handles sync separately

**Decision needed:** which Notion option?

---

## How to use this doc

Each phase = 1 weekend sprint. Read the relevant section, do the work, mark the phase done
below.

```
[ ] Phase A — 3x daily brief
[ ] Phase B — Phase 4 weekly brief
[ ] Phase C — XAUUSD signal model
[ ] Phase D — Slim GAS to webhook proxy
```

When done, archive this doc as `MIGRATION.done.md` and update README architecture diagram.
