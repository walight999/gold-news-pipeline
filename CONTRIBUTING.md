# Contributing

Thanks for your interest! This repo is small and personal but PRs that
improve robustness, add sources, or refactor are welcome.

## Quick start

```
git clone https://github.com/walight999/gold-news-pipeline
cd gold-news-pipeline
pip install -r requirements.txt
cp .env.example .env       # fill in your own LINE / Sheets / FRED / Claude keys
pytest -q                  # 81 tests
python -m src.main --mode cron
```

## Testing

The full suite must pass before a PR is merged:

```bash
pytest                     # full run
pytest tests/test_translator.py -v    # single file
pytest -k cache            # by keyword
```

When you add behaviour, add a test for it. The existing tests use the
`store` fixture (`tests/conftest.py:FakeStore`) — an in-memory
drop-in for Google Sheets — so most tests don't need any external
service. Hit real services only in `tests/smoke_*.py` files (they're not
run by default, only via direct `python tests/smoke_*.py`).

## Code style

- **No emojis in source code** unless they're part of the LINE Flex
  output (e.g. `🚨`, `📊`) — these are user-facing strings.
- Comments explain **why**, not **what**. The code already says what
  it does.
- Each function should fit on one screen unless there's a specific
  reason. If a function is approaching 100 lines, look for an extraction.
- Prefer Python's standard library to new dependencies. The current
  dependency list is deliberately small (see `requirements.txt`).

## Architecture invariants (locked — don't break in a PR)

1. **`event_id` is content-keyed, not headline-keyed.**
   `hash(topic_bucket + entity + direction_label + 15m_bucket)`. Two
   sources reporting the same event get the same `event_id`.
2. **One Sheet read per run, one batched write per tab.** No per-event
   API calls. See `src/store.py`.
3. **No LLM rewrites of news content.** Translation only. Claude is
   used for *translation*, never to summarize, paraphrase, or invent.
4. **Rate-limit applies to BREAKING+ALERT only.** Overflow downgrades to
   digest — never dropped silently.
5. **Quiet hours suppress all pushes 04:00–05:00 ICT.**
   The calendar_daily at 04:40 is the single deliberate exception
   (`bypass_quiet=True`).

If your PR needs to bend one of these, raise it in the issue first.

## Adding a new RSS source

1. Add an entry under `sources:` in `config/sources.yaml`:
   ```yaml
   - id: my_source
     name: My Source
     url: https://example.com/rss
     tier: 1                  # 0=official, 1=macro, 2=trader, 3=context
     role: macro
     poll_min: 5
     source_class: independent # for independent_source_count
     enabled: true
   ```
2. Add the source's human label to `SOURCE_NAMES` in `src/line_flex.py`.
3. Run `python -m src.main --mode verify_sources` to confirm the feed
   parses and the cluster keys look right.
4. Watch the first few cron runs in production — if it's noisy, tune
   `tier` / `poll_min` rather than disabling.

## Reporting bugs

Open a GitHub issue with:

- What you expected
- What happened
- The relevant section of `health_log` or run log
- Your Python version + OS

## Code of conduct

Be kind. Assume the other person is doing their best.

## License

MIT — by contributing you agree your code can be re-licensed under MIT.
