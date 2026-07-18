# Operator steps — 2026-07-18 reliability sprint

The Week Ahead (and every LINE push) went silent for ~a week because LINE's free
monthly quota was exhausted and the "LINE is down" alert was itself sent over
LINE. PRs #68–75 fixed the code; the items below are the **console/manual steps
only you can do**. Nothing here is required for the code to be *safe* — every new
path is an env-gated no-op until you set its secret — but each unlocks a layer of
protection.

Priority: 🔴 do now · 🟡 this week · 🟢 when convenient.

---

## 🔴 1. Activate the LINE-independent ops channel

The single most important gap: critical alerts, the EoD-recap fallback, and the
monthly precision report all DM you over Telegram so a LINE outage can't hide its
own alarm. Inert until BOTH secrets are set.

- `OPS_TG_BOT_TOKEN` — a Telegram bot token. **You may reuse @FinisitNews_bot's
  token** (the token identifies the bot; the chat_id below scopes it to a private
  DM, so this does NOT post to the public subscriber group).
- `OPS_TG_CHAT_ID` — your **private** chat id with that bot. Get it by DMing the
  bot once, then opening
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and reading `message.chat.id`.

```
gh secret set OPS_TG_BOT_TOKEN  --repo walight999/gold-news-pipeline
gh secret set OPS_TG_CHAT_ID    --repo walight999/gold-news-pipeline
```

Wired into: news_cron, event_mode, watchdog, eod_recap, calibration.

## 🔴 2. Set the cron dead-man ping (it was never configured)

`HEALTHCHECK_PING_URL` does not exist as a secret — the deadman monitor wired in
PR #65 has been inert the whole time. This is what catches a full GitHub-Actions
/ cron-job.org outage (the 2026-06-23 dead-zone class).

1. Create a free check at https://healthchecks.io (period 5m, grace 10m).
2. `gh secret set HEALTHCHECK_PING_URL --repo walight999/gold-news-pipeline`
   (paste the check's ping URL). Configure its email/LINE/Telegram alert.

## 🔴 3. Rotate two stale credentials

- `LINE_CHANNEL_TOKEN` — dates to 2026-05-20 and was pasted in chat 2026-06-11.
- `APIFY_TOKEN` — 2026-06-11; earlier code leaked it via query-string in public
  Actions logs (fixed since, but the old token is exposed in old logs).

Issue new ones (LINE Developers console / Apify console) and
`gh secret set <NAME> --repo walight999/gold-news-pipeline`.

---

## 🟡 4. Give the watchdog its own dead-man check

The watchdog can't detect *itself* sitting disabled (its check runs inside it).
A **separate** healthchecks.io check catches a watchdog-only death.

1. Second healthchecks.io check (period 60m, grace 30m).
2. `gh secret set WATCHDOG_PING_URL --repo walight999/gold-news-pipeline`.
   Must be a DIFFERENT URL than #2, or a watchdog-only outage stays masked.

## 🟡 5. Add the missing cron-job.org exact-time jobs

`eod_recap`, `weekly_preview`, `scorecard`, and `macro_push` still rely on
throttled native cron (eod has fired ~3.5h late). Add `workflow_dispatch` jobs on
cron-job.org (same PAT as the news/calendar jobs) — see `docs/DISPATCHER-CRON.md`:

| workflow | UTC time |
|---|---|
| eod_recap | `0 16 * * 1-5` |
| weekly_preview | `0 23 * * 5` + `0 21 * * 0` |
| scorecard | `45 16 * * 1-5` |
| macro_push | `0 5,11,17,23 * * *` |

Then verify with `gh run list --workflow eod_recap.yml` that runs show as
`workflow_dispatch`, not just `schedule`.

## 🟡 6. Monthly precision report trigger

Add one cron-job.org job hitting `precision_report.yml`… actually
`--mode precision_report` runs via the **calibration** workflow's dispatch —
create a monthly cron-job.org trigger (e.g. `0 1 1 * *`) calling
`calibration.yml` with `mode=precision_report`. It DMs the "what actually moves
gold" summary to your ops Telegram (needs #1).

---

## 🟢 7. cron-job.org PAT expiry reminder

The fine-grained PAT that drives the dispatcher has no auto-rotation. Set a
calendar reminder ~7 days before it expires; on expiry the jobs 401 and morning
coverage silently drops. (Nothing alerts on this yet except the deadman in #2.)

## 🟢 8. LINE plan decision

After group-only routing (#68) + the quota gate (#71), projected burn is
~473/500 per month — safe but tight. Options: leave it (the gate sheds
low-value cards near the cap and Telegram always delivers), or move to the LINE
Light plan for headroom. The July quota resets on **Aug 1**; nothing delivers on
LINE before then regardless.

## 🟢 9. Decide the fate of ff_gas_thisweek / ff_gas_weekly workflows

These two workflows are `disabled_manually`. Their own comments say they feed the
**gold-analyst** GAS WeeklyCache (Calendar V2) — but CLAUDE.md says they only fed
the *retired* newsupdate GAS. Your `project_ff_weekly_prefetch` note says the
gold-analyst WeeklyCache paste is still pending. **I did not delete them** because
of that contradiction. If gold-analyst no longer needs them, delete both `.yml`
files (and `scripts/push_ff_to_gas.py`); otherwise finish the gold-analyst paste.

---

## Backlog (future sessions, not blocking)

- **Fast-path for tier-0 latency** — investigated, NOT needed: quiet hours are
  only 04:00–05:00 ICT and US tier-0 releases never land there.
- **Speech-calendar source** (Fed/ECB/BoJ) — real coverage gap; needs per-bank
  HTML scraping.
- **CFTC COT source** — already covered by your `xau-positioning-system`; don't
  duplicate.
- **Auto-tuning of scoring** — wait for the precision breakdowns (#74/#75) to
  accumulate data, then wire the winning signals with data-derived coefficients
  (never guessed).
