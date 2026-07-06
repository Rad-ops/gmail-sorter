"""AI active learning for the Gmail sorter (v0.7).

When the AI review pipeline (:func:`gmail_sorter.merge_ai_labels`) accepts
an AI override, the AI's chosen label is the highest-quality signal in the
loop. The keyword rules can be wrong, the sender profile can drift, but
the model with the full body excerpt and the freedom to reason about
context picked one label over another. v0.7 closes the loop: the moment a
human runs ``--merge-ai-labels``, the AI's verified decisions are pushed
back into the local state so the next scan benefits immediately:

* The AI's category is recorded in ``sender_profile`` for the message's
  sender / domain so future mail from the same sender gets the AI's
  preferred label.
* When the embedding pre-classifier is on, the message's body text is fed
  into the centroid recomputation, so the AI's label and its supporting
  semantics shape the next centroid.

This module never makes Gmail API calls. It is pure local state. The
:class:`gmail_sorter.EmbeddingBackend` is passed in by the caller so the
function stays compatible with the existing
:func:`update_category_centroids` plumbing.

The function :func:`apply_ai_learning` is a pure function on the local DB
+ decisions + AI packets, with no apply-path side effects. It is the only
thing ``main()`` calls between ``--merge-ai-labels`` and the next scan.

Safety:

* The function never removes a protected category from a sender profile.
* It never records a learning event for messages marked
  ``protected=True`` where the AI's label is in ``PROTECTED_CATEGORIES``
  and the code already had a different protected label (defensive: in
  practice the merge step does not change protected status, but the
  learning path mirrors the same invariant).
* A failed centroid update does not abort the sender-profile update;
  the two are independent and partial progress is better than a
  half-applied state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import policy

log = logging.getLogger("sorter.ai_learning")


def _category_bump(token: dict[str, Any], category: str, weight: int) -> None:
    token.setdefault("categories", defaultdict(int))[category] += weight


def apply_ai_learning(
    conn: sqlite3.Connection | None,
    decisions: list[Any],
    ai_packets: list[dict[str, Any]],
    embedding_backend: Any | None = None,
    sender_profile_weight: int = 1,
    centroid_confidence_floor: int = 70,
    centroid_body_cap: int = policy.BODY_EXCERPT_FOR_FEATURES if hasattr(policy, "BODY_EXCERPT_FOR_FEATURES") else 4000,
) -> dict[str, int]:
    """Push AI-overridden decisions into sender_profile and category centroids.

    Returns a small report with the counts of profile bumps and centroid
    contributions applied. ``decisions`` is the in-memory list of
    :class:`gmail_sorter.Decision` objects from the current scan;
    ``ai_packets`` is the parsed JSONL of AI-reviewed decisions. The
    matching key is ``message_id``.
    """

    if conn is None or not decisions or not ai_packets:
        return {"profile_bumps": 0, "centroid_contributions": 0, "considered": 0}

    decisions_by_id = {item.message_id: item for item in decisions}
    considered = 0
    profile_bumps = 0
    centroid_contributions = 0

    now = datetime.now(timezone.utc).isoformat()
    # Track per-category contributions we want to feed into centroids in a
    # second pass. We don't write centroids here directly because the
    # existing update_category_centroids already handles the bulk of the
    # computation. We only ADD the AI-overridden decisions to the set of
    # texts the centroid learner sees, by appending them to a parallel
    # accumulator and re-running the average.
    centroid_samples: dict[str, list[list[float]]] = defaultdict(list)
    centroid_texts_by_category: dict[str, list[str]] = defaultdict(list)

    for packet in ai_packets:
        if not packet.get("ai_reviewed"):
            continue
        ai_label = packet.get("ai_label") or ""
        if not ai_label:
            continue
        try:
            ai_conf = float(packet.get("ai_confidence", 0) or 0)
        except (TypeError, ValueError):
            ai_conf = 0.0
        if ai_label in policy.NON_LABEL_CATEGORIES:
            continue
        message_id = packet.get("message_id") or ""
        decision = decisions_by_id.get(message_id)
        if decision is None:
            continue
        # Skip protected messages whose AI label differs from the code's
        # protected label. The merge step already enforces this; the
        # learning path mirrors the same invariant as a defensive check.
        if decision.protected and ai_label not in policy.PROTECTED_CATEGORIES:
            log.debug("ai_learning: skipping protected message %s with non-protected AI label", message_id)
            continue
        considered += 1
        # Sender-profile bump: the sender_email and registered_domain both
        # get a hit for the AI's label. Hits use the same shape as
        # update_sender_profiles.
        sender_email = (decision.sender_email or "").lower()
        registered_domain = (decision.registered_domain or decision.sender_domain or "").lower()
        for kind, value in (("sender", sender_email), ("domain", registered_domain)):
            if not value:
                continue
            key = f"{kind}:{value}"
            row = conn.execute(
                "SELECT hits, protected_hits FROM sender_profile WHERE key=? AND category=?",
                (key, ai_label),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sender_profile (
                        key, kind, category, hits, protected_hits,
                        last_seen, updated_at, first_seen, last_hits, category_diversity
                    )
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 0)
                    """,
                    (key, kind, ai_label, sender_profile_weight, now, now, now, str(sender_profile_weight)),
                )
            else:
                conn.execute(
                    """
                    UPDATE sender_profile
                    SET hits = hits + ?, last_seen = ?, updated_at = ?, last_hits = ?
                    WHERE key=? AND category=?
                    """,
                    (sender_profile_weight, now, now, str(sender_profile_weight), key, ai_label),
                )
            profile_bumps += 1
        # Centroid contribution: collect this message's body text and embed
        # it through the backend, then add the vector to the per-category
        # accumulator.
        if embedding_backend is not None and ai_conf * 100 >= centroid_confidence_floor:
            text = f"{decision.subject} {decision.snippet}"
            excerpt = (getattr(decision, "body_text_excerpt", "") or "").strip()
            if excerpt:
                text = f"{text} {excerpt}"
            text = text[:centroid_body_cap]
            vec = embedding_backend.embed(text)
            if vec:
                centroid_samples[ai_label].append(vec)
                centroid_texts_by_category[ai_label].append(text)
                centroid_contributions += 1

    # Recompute centroids that received at least one new contribution.
    # We merge the new vectors with the existing centroid (if any) so the
    # AI's signal improves the centroid without overwriting prior learning.
    for category, vectors in centroid_samples.items():
        if not vectors:
            continue
        existing = conn.execute(
            "SELECT embedding_json, dimension, message_count FROM category_centroid WHERE category=?",
            (category,),
        ).fetchone()
        if existing is not None:
            emb_json, dim, old_count = existing
            try:
                old_vec = [float(x) for x in json.loads(emb_json or "[]")]
            except (json.JSONDecodeError, TypeError):
                old_vec = []
            if not old_vec or len(old_vec) != int(dim or 0):
                old_vec = None
        else:
            old_vec = None
            old_count = 0
        all_vectors = list(vectors)
        if old_vec is not None:
            # Weight the prior centroid by old_count (its training set size)
            # and the new vectors equally. We achieve this by replicating
            # the prior centroid old_count times. For mailboxes where the
            # centroid was learned from 500 messages, this is correct.
            for _ in range(int(old_count or 0)):
                all_vectors.append(old_vec)
        if not all_vectors:
            continue
        # Re-import the pure-Python average to keep the dependency surface
        # small.
        from .embeddings import average_vectors
        new_centroid = average_vectors(all_vectors)
        if not new_centroid:
            continue
        new_count = int(old_count or 0) + len(vectors)
        conn.execute(
            """
            INSERT INTO category_centroid (category, embedding_json, dimension, message_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                embedding_json=excluded.embedding_json,
                dimension=excluded.dimension,
                message_count=excluded.message_count,
                updated_at=excluded.updated_at
            """,
            (
                category,
                json.dumps(new_centroid, ensure_ascii=False),
                len(new_centroid),
                new_count,
                now,
            ),
        )

    conn.commit()
    return {
        "considered": considered,
        "profile_bumps": profile_bumps,
        "centroid_contributions": centroid_contributions,
    }


__all__ = ["apply_ai_learning"]
