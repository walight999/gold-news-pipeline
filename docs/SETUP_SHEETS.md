# Google Sheets state store setup

The pipeline stores all state (source health, event clusters, sent log,
calibration history, translation cache) in a Google Sheet. Sheets is
free, has a generous API quota, lets you eyeball the state for
debugging, and survives ephemeral GitHub Actions runs.

You need:
- A Google account
- A service-account JSON key (for the bot to read/write)
- A Sheet, shared with the service account

This takes ~10 minutes.

## 1. Create a Google Cloud project

1. https://console.cloud.google.com/projectcreate
2. Project name: `gold-news` (or anything)
3. Click **Create**.

## 2. Enable the APIs

1. https://console.cloud.google.com/apis/library
2. Search **Google Sheets API** → click → **Enable**.
3. Search **Google Drive API** → click → **Enable**.

Drive API is needed because the service account reads the Sheet
metadata via Drive before opening it.

## 3. Create a service account

1. https://console.cloud.google.com/iam-admin/serviceaccounts
2. **+ Create service account**.
3. Name: `gold-news-bot` (or anything).
4. Click **Done** (skip roles + user access — not needed).
5. Click the new service account → **Keys** tab → **Add key** →
   **Create new key** → **JSON** → **Create**.
6. A `.json` file downloads. **Save it as `creds.json` in the repo root.**
   It's in `.gitignore` so it won't accidentally commit.

The JSON file looks like:

```json
{
  "type": "service_account",
  "project_id": "gold-news-...",
  "client_email": "gold-news-bot@gold-news-....iam.gserviceaccount.com",
  ...
}
```

The `client_email` is what you'll share the Sheet with.

## 4. Create the Sheet

1. https://sheets.google.com → blank sheet.
2. Name it `gold-news-state` (or anything).
3. **Share** (top-right) → paste the `client_email` from `creds.json`
   → give **Editor** access → uncheck "Notify people" → **Share**.
4. The Sheet ID is in the URL:
   `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`
   Copy it — that's your `GSHEET_ID`.

You don't need to create any tabs — the pipeline auto-creates them on
first run:

- `source_state` — per-source fetch state + scraper health
- `event_state` — clustered news events
- `sent_log` — idempotency for LINE pushes
- `calibration_log` — score>=2 events for Phase 3 calibration
- `health_log` — warning records (per-source + watchdog)
- `translation_cache` — SHA-keyed Thai translation cache

## 5. Populate .env

```bash
# .env (repo root)
GSHEET_ID=1Abc...XyzPasteSheetIdHere
GSHEET_CREDS=          # paste the full JSON content of creds.json HERE (single line)
```

The `GSHEET_CREDS` value is the **whole JSON file as a single line of
text**. Easy way:

```bash
# macOS / Linux
GSHEET_CREDS_VALUE=$(cat creds.json | tr -d '\n')
echo "GSHEET_CREDS=$GSHEET_CREDS_VALUE" >> .env

# Windows PowerShell
$creds = (Get-Content creds.json -Raw) -replace "`r`n",""
Add-Content .env "GSHEET_CREDS=$creds"
```

Or just open `creds.json`, copy everything, paste after `GSHEET_CREDS=`.

## 6. For GitHub Actions

Add both as repository **Secrets** (Settings → Secrets and variables →
Actions → New repository secret):

- `GSHEET_ID` — the Sheet ID
- `GSHEET_CREDS` — the JSON content (paste it raw; GitHub handles
  multi-line secrets correctly)

## 7. Verify

```bash
python -m src.main --mode verify_sources
```

You should see in the log:

```
store.load_all done: {'source_state': N, 'event_state': N, 'sent_log': N, ...}
```

If you get `gspread.exceptions.APIError: [403]: ...permission denied`,
double-check step 4 — the service account email must have Editor
access on the Sheet.

## 8. Quota notes

- Sheets API: **60 reads + 60 writes per minute per user**. The
  pipeline does ~25 API calls per `cron` run; running every 5 min you
  use ~5 reads/min on average. Plenty of headroom.
- Sheet row limit: 10M cells total. Tab caps in `maintain` mode:
  - `event_state` purged after 7 days
  - `sent_log` purged after 30 days
  - `translation_cache` purged 24h TTL + cap 2000 rows
  - `calibration_log` kept forever (Phase 3 needs the history)

If you fork this and run at higher cadence, watch `api_calls` in the
run log and consider raising your project's API quota in GCP.
