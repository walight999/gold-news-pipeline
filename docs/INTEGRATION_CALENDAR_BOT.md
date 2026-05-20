# Integration — Calendar Bot v2.6 → `event-mode` dispatcher

## Purpose

Trigger `news-cron`'s **Event Mode** loop (Tier-0-only, 30 min, polls every
60 s) automatically ~5 minutes before high-impact scheduled releases
(CPI / NFP / FOMC). Increases the odds we catch the actual release within
one polling cycle instead of the default `*/5` cron cadence.

The trigger is a `repository_dispatch` event of type `news-event-mode`
that this repo's `.github/workflows/event_mode.yml` listens for.

## High-level wiring

```
GoldBot Calendar V2 (GAS)
   │  every minute trigger scans today's calendar
   │  finds high-impact USD event in T-6 ± 30s
   │  calls fireEventModeDispatch()
   ▼
GitHub REST API
   POST /repos/walight999/gold-news-pipeline/dispatches
   { event_type: "news-event-mode",
     client_payload: { duration_min: 30 } }
   ▼
GitHub Actions
   event-mode.yml runs
   → Tier-0 sources polled every 60 s for 30 min
   → Breaking alerts hit LINE the moment a CPI/NFP headline lands
```

## Setup

### 1. Create a fine-grained Personal Access Token

1. https://github.com/settings/personal-access-tokens/new
2. **Token name**: `gold-news-event-dispatch`
3. **Resource owner**: `walight999`
4. **Repository access** → **Only select repositories** → pick `gold-news-pipeline`
5. **Repository permissions**:
   - **Actions**: Read and write   ← required to fire `dispatches`
   - **Contents**: Read-only (default)
   - **Metadata**: Read-only (default)
6. **Expiration**: 1 year (set a calendar reminder to renew)
7. Generate, copy the token — starts with `github_pat_...`

### 2. Store the token in GoldBot GAS Script Properties

In your existing GoldBot Calendar V2 Apps Script project:

1. Project Settings (gear icon) → Script properties → Add property
2. **Property**: `GH_PAT_GOLD_NEWS`
3. **Value**: paste the `github_pat_...` token
4. Save

### 3. Paste this function into the GoldBot GAS

```javascript
/**
 * Fires GitHub repository_dispatch to kick off the news-event-mode workflow.
 * Call from your minute-level calendar scanner ~5 min before CPI/NFP/FOMC.
 *
 * Returns the HTTP status code (204 on success).
 */
function fireEventModeDispatch(durationMin) {
  var token = PropertiesService.getScriptProperties().getProperty("GH_PAT_GOLD_NEWS");
  if (!token) {
    Logger.log("fireEventModeDispatch: missing GH_PAT_GOLD_NEWS property");
    return 0;
  }
  var url = "https://api.github.com/repos/walight999/gold-news-pipeline/dispatches";
  var res = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: "token " + token,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    },
    payload: JSON.stringify({
      event_type: "news-event-mode",
      client_payload: { duration_min: durationMin || 30 }
    }),
    muteHttpExceptions: true
  });
  var code = res.getResponseCode();
  Logger.log("fireEventModeDispatch -> HTTP " + code + " " + res.getContentText());
  return code;
}

/**
 * One-time test — paste this into the editor and Run.
 * Should return 204 and start a workflow visible at
 *   https://github.com/walight999/gold-news-pipeline/actions
 */
function testFireEventModeDispatch() {
  var code = fireEventModeDispatch(5);  // short 5-min burst for test
  if (code !== 204) {
    throw new Error("Expected 204, got " + code);
  }
  Logger.log("OK — check Actions tab for a fresh event-mode run.");
}
```

### 4. Wire it into your minute-level scanner

In whichever GAS function scans today's high-impact events every minute,
add the trigger:

```javascript
function scanCalendarAndDispatch() {
  var events = getTodaysHighImpactEvents();  // your existing function
  var now = new Date().getTime();
  var alreadyFiredKey = "EVT_MODE_FIRED_" + Utilities.formatDate(new Date(), "GMT+7", "yyyyMMdd");
  var fired = PropertiesService.getScriptProperties().getProperty(alreadyFiredKey) || "";

  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    var releaseMs = ev.releaseTimeMs;            // your existing field
    var fiveMinBefore = releaseMs - 5 * 60 * 1000;

    // Fire once per event, between T-5:30 and T-4:30 from release
    var window = Math.abs(now - fiveMinBefore);
    var key = ev.eventId || (ev.title + "@" + ev.releaseTimeMs);

    if (window < 30 * 1000 && fired.indexOf(key) === -1) {
      fireEventModeDispatch(30);
      fired += "|" + key;
      PropertiesService.getScriptProperties().setProperty(alreadyFiredKey, fired);
    }
  }
}
```

The `EVT_MODE_FIRED_<date>` property guards against double-firing inside
the 30-second window. It auto-rolls each ICT trading day.

## Verifying

After the first real trigger:

1. Open https://github.com/walight999/gold-news-pipeline/actions
2. You should see a fresh `event-mode` run within ~10 seconds of the GAS
   call.
3. The run logs Tier-0 polling every 60 s for 30 min.
4. Any qualifying news (e.g. "US CPI hotter than expected" picked up by
   Fed/BLS/ECB feeds) hits LINE within one polling cycle.

## Safety

- The PAT is scoped to **one repo** with **Actions:write** only. Compromise
  surface ≈ ability to spam this repo's workflows. No code-write, no
  secrets-read.
- Inside GAS the token lives in Script Properties (encrypted at rest, not
  shown in the editor).
- Rotate yearly via the calendar reminder you set in step 1.
