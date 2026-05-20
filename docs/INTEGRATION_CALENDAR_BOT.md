# Calendar Bot dispatcher — standalone GAS

Goal: kick off `event-mode` (Tier-0 hot poll for 30 min) ~5 minutes before
high-impact USD/EUR releases so we catch the actual print within one polling
cycle instead of waiting for the next `*/5` `news-cron`.

Implementation: a **standalone Google Apps Script** project that polls
ForexFactory every 5 min and fires `repository_dispatch` for matching events.
It does NOT touch the user's existing GoldBot Calendar V2 — it's its own bot
with its own trigger.

The full code lives in `docs/gas_dispatcher.gs`.

---

## Setup — 5 steps (~5 minutes)

### 1. Generate a fine-grained PAT

1. Open https://github.com/settings/personal-access-tokens/new
2. **Token name**: `gold-news-event-dispatch`
3. **Expiration**: 1 year (set a calendar reminder to renew)
4. **Resource owner**: `walight999`
5. **Repository access** → **Only select repositories** → pick
   `walight999/gold-news-pipeline`
6. **Repository permissions**:
   - **Actions**: **Read and write** ← required
   - Everything else: leave at default (no extra access)
7. Click **Generate token**
8. Copy the `github_pat_...` value somewhere safe — it's shown ONCE only.

### 2. Create a new Apps Script project

1. Open https://script.google.com/home/projects/create
2. Name the project: `gold-news-event-dispatcher`

### 3. Paste the code

1. Replace the default `Code.gs` content entirely with the code from
   `docs/gas_dispatcher.gs` in this repo.
2. Save (Ctrl-S / ⌘-S).

### 4. Store the PAT as a script property

1. Click the gear icon **Project Settings** in the left rail.
2. Scroll to **Script properties** → **Edit script properties** → **Add property**:
   - **Property**: `GH_PAT_GOLD_NEWS`
   - **Value**: paste the `github_pat_...` token from step 1.
3. Click **Save script properties**.

### 5. Test + install the trigger

1. Back in the Code editor, pick `testFire` from the function dropdown next
   to **Debug** at the top, then click **Run**.
2. On first run, Google prompts you to authorize:
   - Click **Review permissions**
   - Pick your Google account
   - Click **Advanced** → **Go to gold-news-event-dispatcher (unsafe)**
   - Click **Allow**
3. The Logs tab should say
   `OK — check https://github.com/walight999/gold-news-pipeline/actions for a fresh event-mode run`
4. Open that URL — within ~10 seconds you should see a fresh `event-mode`
   run kicked off.
5. Once verified, pick `setupTrigger` from the function dropdown and click
   **Run** once. This installs the 5-min cron that calls `scanAndFire`.

You're done. From now on the dispatcher polls the FF calendar every 5 min
and fires `event-mode` ~5 min before each High-impact USD/EUR release.

---

## What it does, concretely

Every 5 minutes:

1. Fetches the public FF JSON (no auth needed — same source the Python
   pipeline uses for `calendar-daily` / `calendar-check`).
2. For each event matching `country ∈ {USD, EUR}` and `impact = High`:
   - If the event's release time is `4..8` minutes from now…
   - …and we haven't already fired for this exact event today…
   - …POST `{event_type: "news-event-mode", client_payload: {duration_min: 30}}`
     to `https://api.github.com/repos/walight999/gold-news-pipeline/dispatches`.
3. Persists a per-day fired-keys set in the GAS script properties so
   restarts / crashes can't double-fire.
4. Auto-prunes the fired-keys storage — only today and yesterday are kept.

---

## Safety

- The PAT is scoped to **one repo** with **Actions: write** only. Compromise
  surface = ability to spam dispatch events on this repo. No code-write,
  no secrets-read, no access to any other repo.
- Inside GAS the token lives in Script Properties (encrypted at rest, not
  shown in editor source).
- Rotate yearly per the calendar reminder set in step 1.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `testFire` throws HTTP 401 | PAT not set or wrong | Re-check step 4 |
| `testFire` throws HTTP 403 | PAT missing Actions:write | Re-create per step 1 |
| `testFire` throws HTTP 422 | Event-mode workflow not on default branch | Confirm `.github/workflows/event_mode.yml` is on `main` |
| No fires after a known release | Trigger not installed | Re-run `setupTrigger` (step 5) |
| Logs say "Missing GH_PAT_GOLD_NEWS" | Property not saved | Re-do step 4, then run `testFire` |
