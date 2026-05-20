/**
 * Standalone Google Apps Script dispatcher for gold-news-pipeline.
 *
 * Purpose: fires GitHub repository_dispatch ~5 minutes before high-impact
 * USD/EUR economic releases, kicking off the news-pipeline's event-mode
 * workflow for 30 min of tight (60 s) polling around the release.
 *
 * Independent from any other bot — copy this whole file into a NEW Apps
 * Script project. No interaction with the existing GoldBot Calendar V2.
 *
 * SETUP (~5 min):
 *   1. Generate fine-grained PAT — see docs/INTEGRATION_CALENDAR_BOT.md
 *   2. Create a new GAS project: https://script.google.com/home/projects/create
 *   3. Replace Code.gs contents with THIS file.
 *   4. Project Settings (gear icon) → Script properties → Add property:
 *        Name:  GH_PAT_GOLD_NEWS
 *        Value: <the fine-grained PAT from step 1>
 *   5. Run setupTrigger() once. Authorize when prompted.
 *   6. Run testFire() to verify. Check
 *        https://github.com/walight999/gold-news-pipeline/actions
 *      for a fresh event-mode run.
 *
 * That's it. The trigger runs scanAndFire() every 5 min thereafter.
 */

var REPO          = "walight999/gold-news-pipeline";
var DISPATCH_TYPE = "news-event-mode";
var DURATION_MIN  = 30;

// Fire when event is within this many minutes (inclusive..exclusive).
// 5-min trigger cadence × 4-min window width = each event fires exactly once.
var FIRE_LOW  = 4;   // T-4 min
var FIRE_HIGH = 8;   // T-8 min

// Tight filter — match the post-release alert filter so dispatch only fires
// on the events that genuinely matter for XAU.
var COUNTRIES = ["USD", "EUR"];
var IMPACTS   = ["High"];

var FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";


/**
 * Install/replace the 5-min trigger. Run once after setting GH_PAT_GOLD_NEWS.
 */
function setupTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === "scanAndFire") {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }
  ScriptApp.newTrigger("scanAndFire")
    .timeBased()
    .everyMinutes(5)
    .create();
  Logger.log("Trigger installed: scanAndFire every 5 min");
}


/**
 * The cron body. Scans the FF calendar, fires repository_dispatch for any
 * matching event in the T-FIRE_LOW..T-FIRE_HIGH window. Idempotent.
 */
function scanAndFire() {
  var events = fetchCalendar();
  if (!events.length) return;

  var now = Date.now();
  var fired = readFiredCache();

  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    if (COUNTRIES.indexOf(ev.country) === -1) continue;
    if (IMPACTS.indexOf(ev.impact)   === -1) continue;

    var releaseMs = Date.parse(ev.date || "");
    if (!releaseMs || isNaN(releaseMs)) continue;

    var minsTo = (releaseMs - now) / 60000.0;
    if (minsTo < FIRE_LOW || minsTo >= FIRE_HIGH) continue;

    var key = ev.country + "|" + ev.title + "|" + ev.date;
    if (fired.indexOf(key) !== -1) continue;

    var code = fireEventModeDispatch(DURATION_MIN);
    Logger.log("Fired " + key + "  minsTo=" + minsTo.toFixed(1) + "  HTTP " + code);
    if (code === 204) {
      fired.push(key);
    }
  }
  writeFiredCache(fired);
}


/**
 * Sends repository_dispatch to GitHub. Returns the HTTP status code
 * (204 == success).
 */
function fireEventModeDispatch(durationMin) {
  var token = PropertiesService.getScriptProperties().getProperty("GH_PAT_GOLD_NEWS");
  if (!token) {
    Logger.log("Missing GH_PAT_GOLD_NEWS script property — see setup step 4");
    return 0;
  }
  var res = UrlFetchApp.fetch("https://api.github.com/repos/" + REPO + "/dispatches", {
    method:      "post",
    contentType: "application/json",
    headers: {
      Authorization:           "token " + token,
      Accept:                  "application/vnd.github+json",
      "X-GitHub-Api-Version":  "2022-11-28"
    },
    payload: JSON.stringify({
      event_type:     DISPATCH_TYPE,
      client_payload: { duration_min: durationMin || 30 }
    }),
    muteHttpExceptions: true
  });
  return res.getResponseCode();
}


/**
 * One-shot test from the GAS editor. Fires a 5-min event-mode run.
 *
 *   1. Save & run testFire from the function picker.
 *   2. Authorize if prompted (Advanced → Go to <project> → Allow).
 *   3. Logger shows "OK" if HTTP 204.
 *   4. Open https://github.com/walight999/gold-news-pipeline/actions —
 *      a brand-new event-mode run should appear within ~10 seconds.
 */
function testFire() {
  var code = fireEventModeDispatch(5);
  if (code !== 204) {
    throw new Error("testFire failed: HTTP " + code +
                    " — verify GH_PAT_GOLD_NEWS is set and has Actions:write");
  }
  Logger.log("OK — check https://github.com/" + REPO + "/actions for a fresh event-mode run");
}


/* ---------- internal helpers ---------- */

function fetchCalendar() {
  var res = UrlFetchApp.fetch(FF_URL, { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) {
    Logger.log("FF fetch failed: HTTP " + res.getResponseCode());
    return [];
  }
  try {
    return JSON.parse(res.getContentText()) || [];
  } catch (e) {
    Logger.log("FF parse error: " + e);
    return [];
  }
}


function todayKey() {
  return "FIRED_" + Utilities.formatDate(new Date(), "GMT+7", "yyyyMMdd");
}


function readFiredCache() {
  var raw = PropertiesService.getScriptProperties().getProperty(todayKey()) || "";
  return raw ? raw.split("\n").filter(function(s) { return s; }) : [];
}


/**
 * Persist today's fired keys + prune day-keyed caches older than yesterday.
 */
function writeFiredCache(list) {
  PropertiesService.getScriptProperties().setProperty(todayKey(), list.join("\n"));
  var allProps = PropertiesService.getScriptProperties().getProperties();
  var keep = {};
  keep[todayKey()] = 1;
  keep["FIRED_" + Utilities.formatDate(
    new Date(Date.now() - 86400000), "GMT+7", "yyyyMMdd"
  )] = 1;
  for (var pk in allProps) {
    if (pk.indexOf("FIRED_") === 0 && !keep[pk]) {
      PropertiesService.getScriptProperties().deleteProperty(pk);
    }
  }
}
