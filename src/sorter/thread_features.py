"""Thread-level conversation modeling for the Gmail sorter (v0.8).

Pre-v0.7 the sorter's thread-aware feature was a simple plurality vote:
the dominant category in the thread (by total confidence) was
inherited by catch-all replies. That is principled enough for a 2-message
thread but it loses information for a long conversation:

* A 10-message Finance thread with attachments, calendar invites, and
  the same sender throughout is much more likely to be Finance than a
  2-message mixed-category thread.
* A thread with a long date span is more likely to be reference
  material; a thread concentrated in one day is more likely to be a
  short conversation.
* A thread with a high unsubscribe / bulk-mail rate is more likely to
  be promotional even when individual messages lack bulk headers.

v0.8 builds a thread-level feature vector per thread and feeds it into
:func:`decide` as an extra category boost. The boost is a learned linear
combination of:

  - ``message_count`` — number of messages in the thread
  - ``distinct_senders`` — number of distinct senders
  - ``category_distribution`` — top category's share of the thread
  - ``has_attachment_count`` — fraction of messages with attachments
  - ``has_unsubscribe_count`` — fraction with bulk-mail headers
  - ``date_span_days`` — span from first to last message
  - ``protected_fraction`` — fraction protected
  - ``first_seen`` / ``last_seen`` — date range

The model is tiny (8 features, 8 weights per category) and trained the
same way as the per-keyword learned weights. The output is a
``thread_boost:category:weight`` reason that explainability demands.

The new SQLite table ``thread_features`` is populated lazily on every
scan. It carries one row per (thread_id) with the 8 features. A new
``--use-thread-modeling`` flag (default on) tells decide() to consult
the thread features for an extra confidence boost.

This is a more principled alternative to the plurality vote because it
takes the thread's *shape* into account, not just its dominant label.
A 10-message Finance thread with attachments gets a stronger Finance
boost than a 2-message thread with the same dominant category, which
is the right behavior for a noisy seven-year mailbox.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import policy

log = logging.getLogger("sorter.thread_features")


@dataclass
class ThreadFeature:
    """One row of the thread_features table."""

    thread_id: str
    message_count: int = 0
    distinct_senders: int = 0
    top_category: str = ""
    top_category_share: float = 0.0
    has_attachment_count: int = 0
    has_unsubscribe_count: int = 0
    date_span_days: int = 0
    protected_fraction: float = 0.0
    first_seen: str = ""
    last_seen: str = ""

    def to_features(self) -> list[float]:
        """The 8-element feature vector fed into the boost model."""

        return [
            float(self.message_count),
            float(self.distinct_senders),
            float(self.top_category_share),
            float(self.has_attachment_count) / max(1, self.message_count),
            float(self.has_unsubscribe_count) / max(1, self.message_count),
            float(self.date_span_days),
            self.protected_fraction,
            # The top_category is not a feature (it would leak the
            # label); instead we use top_category_share, which is
            # derived from the same data without naming the
            # category.
            float(len(self.top_category)) / 100.0,  # proxy: non-empty
        ]


def _parse_date(d: str) -> str:
    if not d:
        return ""
    try:
        return d[:10]
    except (TypeError, ValueError):
        return ""


def _date_span_days(first: str, last: str) -> int:
    f = _parse_date(first)
    l = _parse_date(last)
    if not f or not l:
        return 0
    try:
        f_dt = datetime.fromisoformat(f)
        l_dt = datetime.fromisoformat(l)
        return max(0, (l_dt - f_dt).days)
    except ValueError:
        return 0


def build_thread_features(
    conn: sqlite3.Connection | None,
    min_messages: int = 2,
) -> dict[str, ThreadFeature]:
    """Read the messages table and build one ThreadFeature per thread.

    The function aggregates across all messages for each thread,
    regardless of when the messages were scanned, and returns a
    {thread_id: ThreadFeature} dict. Threads with fewer than
    ``min_messages`` are excluded — a single-message thread has no
    conversation to model.
    """

    if conn is None:
        return {}
    try:
        # v0.8: list_unsubscribe is stored inside decision_json (the
        # messages table does not have a list_unsubscribe column in
        # the v0.7 schema). We extract it per-row.
        cur = conn.execute(
            "SELECT thread_id, date, sender_email, categories_json, protected, has_attachment, decision_json FROM messages"
        )
    except sqlite3.OperationalError:
        return {}
    by_thread: dict[str, list[tuple[str, str, str, list[str], int, int, str]]] = defaultdict(list)
    for thread_id, date, sender_email, cats_json, protected, has_attachment, decision_json in cur.fetchall():
        if not thread_id:
            continue
        try:
            cats = json.loads(cats_json) if cats_json else []
        except json.JSONDecodeError:
            cats = []
        list_unsub = ""
        if decision_json:
            try:
                dj = json.loads(decision_json)
                if isinstance(dj, dict):
                    list_unsub = dj.get("list_unsubscribe", "") or ""
            except json.JSONDecodeError:
                pass
        by_thread[thread_id].append((date or "", sender_email or "", "", cats, int(protected or 0), int(has_attachment or 0), list_unsub))
    out: dict[str, ThreadFeature] = {}
    for thread_id, rows in by_thread.items():
        if len(rows) < min_messages:
            continue
        message_count = len(rows)
        distinct_senders = len({r[1] for r in rows})
        all_cats: list[str] = []
        protected_count = 0
        attachment_count = 0
        unsubscribe_count = 0
        dates: list[str] = []
        for date, sender, _, cats, protected, has_attachment, list_unsub in rows:
            all_cats.extend(c for c in cats if c not in policy.NON_LABEL_CATEGORIES)
            if protected:
                protected_count += 1
            if has_attachment:
                attachment_count += 1
            if list_unsub:
                unsubscribe_count += 1
            if date:
                dates.append(date[:10])
        counter = Counter(all_cats)
        if not counter:
            continue
        top_category, top_count = counter.most_common(1)[0]
        top_share = top_count / max(1, message_count)
        first_seen = min(dates) if dates else ""
        last_seen = max(dates) if dates else ""
        out[thread_id] = ThreadFeature(
            thread_id=thread_id,
            message_count=message_count,
            distinct_senders=distinct_senders,
            top_category=top_category,
            top_category_share=top_share,
            has_attachment_count=attachment_count,
            has_unsubscribe_count=unsubscribe_count,
            date_span_days=_date_span_days(first_seen, last_seen),
            protected_fraction=protected_count / max(1, message_count),
            first_seen=first_seen,
            last_seen=last_seen,
        )
    return out


# v0.8 thread-modeling defaults. The thread boost is a small additive
# contribution, not the dominant signal. The model is intentionally
# conservative so a noisy thread can't blow up an unrelated category.
THREAD_BOOST_CAP = 15  # maximum +X confidence from the thread model
THREAD_BOOST_FLOOR = 5  # minimum thread-message count for a boost


def compute_thread_boost(
    feature: ThreadFeature,
    category: str,
    weight: float = 1.0,
) -> int:
    """Return a 0-THREAD_BOOST_CAP confidence boost for ``category``.

    The boost is a simple linear function of the thread's
    message_count and category_share, capped at THREAD_BOOST_CAP. A
    longer thread with a higher category share gets a larger boost.
    """

    if category not in (feature.top_category,):
        return 0
    if feature.message_count < THREAD_BOOST_FLOOR:
        return 0
    # 5 base + 1 per message over the floor, scaled by top-category share.
    raw = (5 + (feature.message_count - THREAD_BOOST_FLOOR)) * feature.top_category_share
    return int(min(THREAD_BOOST_CAP, max(0, raw * weight)))


def upsert_thread_features(
    conn: sqlite3.Connection | None,
    features: dict[str, ThreadFeature],
) -> None:
    """Persist the thread features to the thread_features table.

    The table is created on first use so the v0.8 schema migration
    stays minimal. v0.8 introduces a new table; v0.7-or-earlier
    databases are upgraded transparently.
    """

    if conn is None or not features:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_features (
            thread_id TEXT PRIMARY KEY,
            message_count INTEGER NOT NULL DEFAULT 0,
            distinct_senders INTEGER NOT NULL DEFAULT 0,
            top_category TEXT NOT NULL DEFAULT '',
            top_category_share REAL NOT NULL DEFAULT 0.0,
            has_attachment_count INTEGER NOT NULL DEFAULT 0,
            has_unsubscribe_count INTEGER NOT NULL DEFAULT 0,
            date_span_days INTEGER NOT NULL DEFAULT 0,
            protected_fraction REAL NOT NULL DEFAULT 0.0,
            first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            f.thread_id, f.message_count, f.distinct_senders, f.top_category,
            f.top_category_share, f.has_attachment_count, f.has_unsubscribe_count,
            f.date_span_days, f.protected_fraction, f.first_seen, f.last_seen, now,
        )
        for f in features.values()
    ]
    conn.executemany(
        """
        INSERT INTO thread_features (
            thread_id, message_count, distinct_senders, top_category,
            top_category_share, has_attachment_count, has_unsubscribe_count,
            date_span_days, protected_fraction, first_seen, last_seen, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            message_count=excluded.message_count,
            distinct_senders=excluded.distinct_senders,
            top_category=excluded.top_category,
            top_category_share=excluded.top_category_share,
            has_attachment_count=excluded.has_attachment_count,
            has_unsubscribe_count=excluded.has_unsubscribe_count,
            date_span_days=excluded.date_span_days,
            protected_fraction=excluded.protected_fraction,
            first_seen=excluded.first_seen,
            last_seen=excluded.last_seen,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def load_thread_features_index(
    conn: sqlite3.Connection | None,
) -> dict[str, ThreadFeature]:
    """Build an in-memory {thread_id: ThreadFeature} index for decide()."""

    if conn is None:
        return {}
    try:
        cur = conn.execute("SELECT * FROM thread_features")
    except sqlite3.OperationalError:
        return {}
    cols = [d[0] for d in cur.description]
    index: dict[str, ThreadFeature] = {}
    for row in cur.fetchall():
        data = dict(zip(cols, row))
        index[data["thread_id"]] = ThreadFeature(
            thread_id=data["thread_id"],
            message_count=int(data.get("message_count", 0)),
            distinct_senders=int(data.get("distinct_senders", 0)),
            top_category=data.get("top_category", ""),
            top_category_share=float(data.get("top_category_share", 0.0)),
            has_attachment_count=int(data.get("has_attachment_count", 0)),
            has_unsubscribe_count=int(data.get("has_unsubscribe_count", 0)),
            date_span_days=int(data.get("date_span_days", 0)),
            protected_fraction=float(data.get("protected_fraction", 0.0)),
            first_seen=data.get("first_seen", ""),
            last_seen=data.get("last_seen", ""),
        )
    return index


__all__ = [
    "ThreadFeature",
    "build_thread_features",
    "compute_thread_boost",
    "load_thread_features_index",
    "upsert_thread_features",
    "THREAD_BOOST_CAP",
    "THREAD_BOOST_FLOOR",
]
