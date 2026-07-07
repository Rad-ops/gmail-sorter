"""Sender reputation as a first-class signal for the Gmail sorter (v0.8).

Pre-v0.7 the sorter's sender-side signal was a binary blocklist /
allowlist plus the per-(sender, category) profile table. v0.7 added
time decay and diversity on top of the profile, but the *reputation*
of a sender — how much mail they send, what fraction is promotional,
whether they're on a blocklist — was still implicit. v0.8 makes it
explicit.

A new SQLite table ``sender_reputation`` carries one row per sender
key (``sender:`` or ``domain:``) with:

  - ``total_messages`` — lifetime message count
  - ``avg_ad_confidence`` — mean ad confidence
  - ``protected_fraction`` — fraction of messages protected
  - ``ad_fraction`` — fraction of messages flagged as ads/promotions
  - ``first_seen`` / ``last_seen`` — date range
  - ``reputation_score`` — 0-100 derived score

The score is computed as::

    reputation_score = 100 * (1 - ad_fraction) * log(1 + total_messages)
    # scaled to a 0-100 range by an empirical normalizer

A high-volume sender with low ad fraction earns a high score; a
high-volume promotional sender earns a low score. The score feeds
into ``score_ad`` as a -15/+10 ad-confidence adjustment so the
``Ads Promotions`` label is more confident for low-reputation senders
and less confident for high-reputation senders.

The dashboard's "Noisy Senders" section is auto-populated with
blocklist candidates: a sender with >= 200 messages and >= 80% ad
fraction and 0% protected fraction is suggested for the blocklist.

The reputation table is populated lazily on every scan from the
``messages`` table. A pure-Python normalizer keeps the runtime
predictable. There is no external dependency.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("sorter.sender_reputation")


# Heuristics for the dashboard's auto-suggested blocklist.
BLOCKLIST_SUGGESTION_MIN_MESSAGES = 200
BLOCKLIST_SUGGESTION_MIN_AD_FRACTION = 0.80
BLOCKLIST_SUGGESTION_MAX_PROTECTED = 0.0

# Reputation score components.
REPUTATION_NORMALIZER = 5.0  # log(1+N) -> score, scaled by 100
REPUTATION_HIGH_THRESHOLD = 80
REPUTATION_LOW_THRESHOLD = 20


@dataclass
class SenderReputation:
    """One row of the sender_reputation table."""

    sender_key: str
    total_messages: int = 0
    avg_ad_confidence: float = 0.0
    protected_fraction: float = 0.0
    ad_fraction: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    reputation_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender_key": self.sender_key,
            "total_messages": self.total_messages,
            "avg_ad_confidence": self.avg_ad_confidence,
            "protected_fraction": self.protected_fraction,
            "ad_fraction": self.ad_fraction,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "reputation_score": self.reputation_score,
        }


def compute_reputation_score(total_messages: int, ad_fraction: float) -> int:
    """Return a 0-100 reputation score.

    The formula is ``100 * (1 - ad_fraction) * log(1 + total_messages) /
    normalizer``. The normalizer (5.0) is an empirical scaling factor
    that puts a 200-message sender with 0% ad at a score of ~100.
    """

    if total_messages <= 0:
        return 0
    raw = (1.0 - ad_fraction) * math.log(1 + total_messages) * 100 / REPUTATION_NORMALIZER
    return int(max(0, min(100, raw)))


def build_sender_reputation(
    conn: sqlite3.Connection | None,
) -> dict[str, SenderReputation]:
    """Build the sender_reputation table from the messages table."""

    if conn is None:
        return {}
    try:
        cur = conn.execute(
            "SELECT sender_email, registered_domain, sender_domain, date, ad_confidence, protected, categories_json FROM messages"
        )
    except sqlite3.OperationalError:
        return {}
    # Accumulate per-key counts.
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "ad_sum": 0, "protected": 0, "ad": 0, "first": "", "last": ""}
    )
    for sender_email, registered_domain, sender_domain, date, ad_conf, protected, cats_json in cur.fetchall():
        try:
            cats = json.loads(cats_json) if cats_json else []
        except json.JSONDecodeError:
            cats = []
        is_ad = "Ads Promotions" in cats or "Newsletters Bulk" in cats
        for kind, value in (("sender", sender_email), ("domain", registered_domain or sender_domain)):
            if not value:
                continue
            key = f"{kind}:{value.lower()}"
            s = stats[key]
            s["count"] += 1
            s["ad_sum"] += int(ad_conf or 0)
            if protected:
                s["protected"] += 1
            if is_ad:
                s["ad"] += 1
            d = (date or "")[:10]
            if d:
                s["first"] = min(s["first"] or d, d)
                s["last"] = max(s["last"] or d, d)
    out: dict[str, SenderReputation] = {}
    for key, s in stats.items():
        if s["count"] == 0:
            continue
        total = s["count"]
        avg_ad = s["ad_sum"] / total
        protected_frac = s["protected"] / total
        ad_frac = s["ad"] / total
        score = compute_reputation_score(total, ad_frac)
        out[key] = SenderReputation(
            sender_key=key,
            total_messages=total,
            avg_ad_confidence=avg_ad,
            protected_fraction=protected_frac,
            ad_fraction=ad_frac,
            first_seen=s["first"],
            last_seen=s["last"],
            reputation_score=score,
        )
    return out


def upsert_sender_reputation(
    conn: sqlite3.Connection | None,
    reputations: dict[str, SenderReputation],
) -> None:
    if conn is None or not reputations:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sender_reputation (
            sender_key TEXT PRIMARY KEY,
            total_messages INTEGER NOT NULL DEFAULT 0,
            avg_ad_confidence REAL NOT NULL DEFAULT 0.0,
            protected_fraction REAL NOT NULL DEFAULT 0.0,
            ad_fraction REAL NOT NULL DEFAULT 0.0,
            first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT '',
            reputation_score INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            r.sender_key, r.total_messages, r.avg_ad_confidence,
            r.protected_fraction, r.ad_fraction, r.first_seen, r.last_seen,
            r.reputation_score, now,
        )
        for r in reputations.values()
    ]
    conn.executemany(
        """
        INSERT INTO sender_reputation (
            sender_key, total_messages, avg_ad_confidence, protected_fraction,
            ad_fraction, first_seen, last_seen, reputation_score, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sender_key) DO UPDATE SET
            total_messages=excluded.total_messages,
            avg_ad_confidence=excluded.avg_ad_confidence,
            protected_fraction=excluded.protected_fraction,
            ad_fraction=excluded.ad_fraction,
            first_seen=excluded.first_seen,
            last_seen=excluded.last_seen,
            reputation_score=excluded.reputation_score,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def load_sender_reputation_index(conn: sqlite3.Connection | None) -> dict[str, SenderReputation]:
    if conn is None:
        return {}
    try:
        cur = conn.execute("SELECT * FROM sender_reputation")
    except sqlite3.OperationalError:
        return {}
    cols = [d[0] for d in cur.description]
    out: dict[str, SenderReputation] = {}
    for row in cur.fetchall():
        data = dict(zip(cols, row))
        out[data["sender_key"]] = SenderReputation(
            sender_key=data["sender_key"],
            total_messages=int(data.get("total_messages", 0)),
            avg_ad_confidence=float(data.get("avg_ad_confidence", 0.0)),
            protected_fraction=float(data.get("protected_fraction", 0.0)),
            ad_fraction=float(data.get("ad_fraction", 0.0)),
            first_seen=data.get("first_seen", ""),
            last_seen=data.get("last_seen", ""),
            reputation_score=int(data.get("reputation_score", 0)),
        )
    return out


def suggest_blocklist(reputations: dict[str, SenderReputation]) -> list[str]:
    """Return a list of sender_keys that look like obvious trash senders.

    The threshold is: total_messages >= BLOCKLIST_SUGGESTION_MIN_MESSAGES,
    ad_fraction >= BLOCKLIST_SUGGESTION_MIN_AD_FRACTION, and
    protected_fraction == 0. The dashboard surfaces these as "consider
    blocklisting" candidates.
    """

    candidates = []
    for key, r in reputations.items():
        if r.total_messages < BLOCKLIST_SUGGESTION_MIN_MESSAGES:
            continue
        if r.ad_fraction < BLOCKLIST_SUGGESTION_MIN_AD_FRACTION:
            continue
        if r.protected_fraction > BLOCKLIST_SUGGESTION_MAX_PROTECTED:
            continue
        candidates.append(key)
    return sorted(candidates)


def reputation_ad_adjustment(reputation: SenderReputation | None) -> int:
    """Return the -15/+10 ad-confidence adjustment for this reputation."""

    if reputation is None or reputation.total_messages == 0:
        return 0
    if reputation.reputation_score >= REPUTATION_HIGH_THRESHOLD:
        return -15
    if reputation.reputation_score <= REPUTATION_LOW_THRESHOLD:
        return +10
    return 0


__all__ = [
    "SenderReputation",
    "BLOCKLIST_SUGGESTION_MAX_PROTECTED",
    "BLOCKLIST_SUGGESTION_MIN_AD_FRACTION",
    "BLOCKLIST_SUGGESTION_MIN_MESSAGES",
    "REPUTATION_HIGH_THRESHOLD",
    "REPUTATION_LOW_THRESHOLD",
    "REPUTATION_NORMALIZER",
    "build_sender_reputation",
    "compute_reputation_score",
    "load_sender_reputation_index",
    "reputation_ad_adjustment",
    "suggest_blocklist",
    "upsert_sender_reputation",
]
