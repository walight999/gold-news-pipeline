"""The `other` bucket never earned a digest slot — stop paying for it.

Measured on the live sheet 2026-08-13: `other` was 1611 of 3366 stored events
and 560 of the 1326 digest-eligible rows, yet across 342 digest sends ZERO
`other` event was ever published. Every one was rejected by the classifier's
relevance gate — after the call was paid for, and after it had already pushed a
real candidate out of the top-12 ranking.

The exclusion is deliberately scoped to the DIGEST pool only; these tests pin
that scope so a later refactor can't quietly widen it into breaking/alert.
"""
from __future__ import annotations

from datetime import timedelta

import yaml

from src import digest
from src.utils_time import iso_utc, now_utc


def _put_event(store, *, event_id, bucket, score=1.0, status="digest",
               minutes_ago=30):
    ts = now_utc() - timedelta(minutes=minutes_ago)
    store.upsert("event_state", {
        "event_id": event_id, "cluster_key": "k", "topic_bucket": bucket,
        "entity": "us", "direction_label": "neutral",
        "first_seen_ts": iso_utc(ts), "last_seen_ts": iso_utc(ts),
        "source_list": "x_firstsquawk", "source_count": 1,
        "score": score, "status": status,
        "title": "EXPEDIA GROUP Q2 EPS $4.14", "summary": "s", "url": "https://x/a",
    })


def test_excluded_bucket_is_dropped_from_the_pool(store):
    _put_event(store, event_id="junk", bucket="other", score=2.0)
    _put_event(store, event_id="real", bucket="inflation", score=1.0)

    rows = digest.collect_window_events(
        store, now_utc(), 4, 0.5, exclude_buckets={"other"})

    # "junk" outranks "real" on score — exclusion has to beat the ranking.
    assert [r["event_id"] for r in rows] == ["real"]


def test_omitting_the_argument_changes_nothing(store):
    """Default is no exclusion, so every existing caller keeps its behaviour."""
    _put_event(store, event_id="junk", bucket="other", score=2.0)

    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)

    assert [r["event_id"] for r in rows] == ["junk"]


def test_an_empty_exclusion_set_restores_the_old_behaviour(store):
    """`exclude_buckets: []` in config is the documented escape hatch."""
    _put_event(store, event_id="junk", bucket="other", score=2.0)

    rows = digest.collect_window_events(
        store, now_utc(), 4, 0.5, exclude_buckets=set())

    assert [r["event_id"] for r in rows] == ["junk"]


def test_several_buckets_can_be_excluded_at_once(store):
    _put_event(store, event_id="a", bucket="other", score=3.0)
    _put_event(store, event_id="b", bucket="growth", score=2.0)
    _put_event(store, event_id="c", bucket="gold_flow", score=1.0)

    rows = digest.collect_window_events(
        store, now_utc(), 4, 0.5, exclude_buckets={"other", "growth"})

    assert [r["event_id"] for r in rows] == ["c"]


def test_a_blank_bucket_is_not_swept_up_by_the_other_exclusion(store):
    """An event whose bucket never got written is unclassified, not `other` —
    excluding one must not silently exclude the other."""
    _put_event(store, event_id="blank", bucket="", score=1.0)

    rows = digest.collect_window_events(
        store, now_utc(), 4, 0.5, exclude_buckets={"other"})

    assert [r["event_id"] for r in rows] == ["blank"]


def test_bucket_matching_tolerates_stray_whitespace(store):
    """The column is hand-editable in the sheet; " other " is still `other`."""
    _put_event(store, event_id="junk", bucket=" other ", score=1.0)

    rows = digest.collect_window_events(
        store, now_utc(), 4, 0.5, exclude_buckets={"other"})

    assert rows == []


def test_shipped_config_excludes_other():
    """The measurement is only worth anything if the config actually carries it."""
    cfg = yaml.safe_load(open("config/schedule.yaml", encoding="utf-8"))

    assert cfg["digest"]["exclude_buckets"] == ["other"]
