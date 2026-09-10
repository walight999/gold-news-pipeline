# Apify upgrades — getting more from the subscription (2026-09-10)

The pipeline pays a flat Apify subscription but historically used it for ONE
thing: scraping 7 X accounts every 12 min (~$2–10/mo). These three upgrades put
the already-paid capacity to work without changing the plan.

All three feed the SAME pool as the existing tweets (normalize → dedup → score →
route → LINE + social feed) and are gated by `APIFY_TOKEN` — no token, no calls.

---

## 1. Event-mode burst — LIVE (no action needed)

**What:** during `--mode event` (the 30-min hot window around a CPI/NFP/FOMC
print) X is scraped every **3 min** instead of 12, looking back only 5 min, and
widened with `event_extra_handles` (LiveSquawk / Fxhedgers / zerohedge). Normal
cron cadence is unchanged.

**Why:** concentrates Apify spend on the minutes gold actually moves instead of
smearing a flat 12-min cadence across a dead afternoon. ~10 scrapes/window at a
few cents total.

**Config:** `config/sources.yaml` → `x_accounts.event_min_interval_min`,
`event_since_minutes`, `event_extra_handles`. Already on.

---

## 2. Truth Social (Trump) — OPT-IN (needs one probe)

**What:** scrapes Trump / Trump Jr posts (tariffs, Fed pressure, geopolitics —
gold movers with NO RSS feed) into the pool as tier-2 wire entries.

**Why it's opt-in:** third-party actor output shapes differ. We can't verify the
field names from code, so validate once (a few cents) before enabling.

**Go-live steps:**
```bash
# 1. Pick an actor in your Apify console (defaults to parsebird/truth-social-scraper).
#    Set APIFY_TOKEN locally, then probe:
python -m src.main --mode apify_probe --target truth

# 2. Read the output:
#    - "raw_records=N" + "record[0] keys: [...]" shows the actor's real fields.
#    - "mapped M entries" shows what we extracted.
#    - If raw>0 but mapped=0 → set truth_social.field_map to the keys shown, e.g.
#         field_map: {text: [content], handle: [username], created: [created_at]}
#      (also check input_key — some actors want "usernames" or "startUrls").

# 3. When the probe maps entries, flip in config/sources.yaml:
#         truth_social.enabled: true
```
Cost: min-interval 15 min (5 min during event burst), ~6 posts/handle.

---

## 3. Cloudflare-recovery (Benzinga) — OPT-IN (needs one probe)

**What:** fetches an RSS feed a plain httpx GET can't reach anymore through an
Apify browser/unblocker actor, then hands the RAW body to the existing
`parse_feed`. Benzinga has 403'd behind Cloudflare since 2026-06-19; this
recovers it as a real wire source.

> Reuters is deliberately NOT included — it has no public RSS to recover, and its
> gold/Fed coverage already arrives via the `yahoo_finance` syndication feed.

**Go-live steps:**
```bash
# 1. Choose a Cloudflare-bypass actor in your console, set apify_recover.actor_id.
python -m src.main --mode apify_probe --target recover:benzinga_apify

# 2. The probe prints the first 300 bytes of the returned body + how many entries
#    parse_feed produced. If 0 bytes → fix actor_id / input_key / url_as_object.
#    If bytes but 0 entries → the actor returned rendered HTML, not the RSS XML;
#    point body_field at the field holding the raw response, or use a
#    "fetch raw URL" actor rather than a JS-rendering scraper.

# 3. When parse_feed yields entries, flip:
#         apify_recover.enabled: true
```
Cost: min-interval 10 min, one actor call per feed per cycle.

---

## Probe cheat-sheet

| Command | Validates |
|---|---|
| `--mode apify_probe --target x` | the existing tweet scraper (should already work) |
| `--mode apify_probe --target truth` | Truth Social actor + field mapping |
| `--mode apify_probe --target recover:benzinga_apify` | Cloudflare actor + parse_feed |

Each probe = exactly ONE actor call. Needs `APIFY_TOKEN` in the local env; prints
"APIFY_TOKEN not set" and exits 1 otherwise (never spends).

## Safety properties

- Every sub-source is best-effort: any error → `[]`, never blocks the news run.
- Independently cost-gated via synthetic `source_state` rows (`_apify`,
  `_apify_truth`, `_apify_recover:<id>`).
- Token only ever travels in the `Authorization` header (never the query string)
  so an Apify 4xx/5xx can't leak it into this PUBLIC repo's Actions logs.
- Truth Social maps on STRUCTURE (no author / empty text ⇒ drop), same as the
  tweet billing-notice guard, so a malformed record can't become a news event.
