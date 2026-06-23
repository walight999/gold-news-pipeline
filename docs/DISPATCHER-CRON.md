# Dispatcher — external cron trigger (cron-job.org)

## Why this exists

GitHub throttles `schedule:` cron on public repos hard — the `*/5` news cron
actually fires roughly **once every 2–4 hours**. To get real 5-minute cadence
the pipeline is driven by an **external scheduler** that calls GitHub's
`workflow_dispatch` API on a fixed interval, 24/7.

### History / the failure this fixes (2026-06-23)

The original driver was a **Google Apps Script (GAS) dispatcher**. It went
**silent every day ~22:00–06:00 UTC = 05:00–13:00 ICT** (Thai morning) — likely
a GAS daily trigger quota or an active-hours guard in the script. Symptoms White
saw:

- Digest rounds arrived late: the **08:30 ICT** round was delivered ~**11:30 ICT**
  (a throttled `schedule` run fired it 3 h late via the 210-min catch-up window).
- The 04:30 → 11:30 jump (the missing 08:30 round on time).
- Far fewer candidates per round (e.g. `08:30: 2/5 kept` instead of up to 12) —
  sparse fetches mean fewer events seen fresh in the 4 h window, and items fetched
  stale score below the `digest.min_score` 0.5 floor.
- **watchdog** could not catch it: `watchdog.yml` is *also* a `schedule` cron, so
  it was throttled too — worker and monitor shared the same throttled lifeline.

**Fix:** drive `workflow_dispatch` from **cron-job.org** (free, runs 24/7, no GAS
quota). Diagnosis: the run list shows `workflow_dispatch` events stopping while
only `schedule` events remain —
`gh run list --workflow news_cron.yml --json createdAt,event`.

## Setup

### 1. Create a fine-grained PAT (minimal scope)

GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new:

- **Repository access:** only `walight999/gold-news-pipeline`
- **Permissions:** `Actions` → **Read and write** (nothing else)
- **Expiry:** set one (e.g. 90 days) and put a calendar reminder to rotate
- Copy the token. **Never paste it into chat** — only into cron-job.org.

> A classic PAT also works but needs the broad `repo` + `workflow` scopes — prefer
> the fine-grained, repo-scoped token to limit blast radius.

### 2. Create one cron-job.org job per workflow

Sign up at cron-job.org (free) → **Create cronjob** for each row below. Every job
is identical except the workflow filename in the URL and the interval.

| Job | URL (`…/actions/workflows/<FILE>/dispatches`) | Interval |
|-----|-----------------------------------------------|----------|
| News | `news_cron.yml` | every 5 min |
| Watchdog | `watchdog.yml` | every 30 min |
| Calendar (Released News) | `calendar_check.yml` | every 10 min |

For each job:

- **URL:**
  `https://api.github.com/repos/walight999/gold-news-pipeline/actions/workflows/<FILE>/dispatches`
- **Method:** `POST`
- **Request headers:**
  ```
  Authorization: Bearer <PAT>
  Accept: application/vnd.github+json
  Content-Type: application/json
  X-GitHub-Api-Version: 2022-11-28
  ```
- **Request body:**
  ```json
  {"ref":"main"}
  ```
- **Schedule:** the interval from the table
- Save, then **Test run** → expect **HTTP 204 No Content** (success; GitHub
  returns 204 with an empty body on a successful dispatch).

> `event_mode.yml` is intentionally **not** on a fixed cron — it is fired
> on-demand (repository_dispatch) just before CPI/NFP/FOMC. Leave it out.

## Verify

```bash
# workflow_dispatch events should reappear at a steady ~5-min cadence,
# INCLUDING the old dead zone (22:00–06:00 UTC):
gh run list --workflow news_cron.yml --limit 40 --json createdAt,event \
  -q '.[] | "\(.createdAt) \(.event)"'
```

Healthy = a continuous stream of `workflow_dispatch` rows with no multi-hour gap,
across all 24 hours. The 6 digest rounds
(04:30/08:30/12:30/16:30/20:30/00:30 ICT) should then land on time without the
3-hour catch-up lag.

## When the PAT expires

A dead PAT looks exactly like the original GAS failure: `workflow_dispatch`
events stop, only `schedule` remains, morning coverage drops. cron-job.org will
show the job failing with **HTTP 401**. Regenerate the fine-grained PAT (step 1)
and update the `Authorization` header on each cron-job.org job.

## Related

- Throttle history + catch-up window: `config/schedule.yaml` `digest.catch_up_minutes`.
- Degraded-mode fallback (classifier outage safety net):
  `digest.allow_fallback_when_ai_down`.
