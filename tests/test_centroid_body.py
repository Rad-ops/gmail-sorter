"""Centroid body text tests for v0.7.

The v0.6 embedding pre-classifier learned category centroids from
``subject + snippet + body_category_hits`` (the names of categories that hit,
not the body text itself). v0.7 fixes that by embedding the cleaned body
excerpt persisted in v0.7 step 2.

These tests verify:
- ``update_category_centroids`` now includes the body excerpt in the embed
  text when present.
- A legacy decision (no body_text_excerpt) still produces a centroid that
  uses the body_category_hits fallback.
- The cap is configurable and bounds the embed text.
- Decisions below the confidence floor are skipped.
- A category with fewer than 3 messages does not get a centroid.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter


class CapturingBackend:
    """Test double for the embedding backend that records every call."""

    def __init__(self, dim=8):
        self.dim = dim
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        # Deterministic embedding: every char of text contributes to the
        # vector so the cosine math is well-defined in any subsequent
        # verification.
        if not text:
            return None
        vec = [0.0] * self.dim
        for i, ch in enumerate(text):
            vec[i % self.dim] += (ord(ch) % 13) / 13.0
        return vec


def make_decision(message_id, category, confidence, body_text_excerpt="", body_category_hits=None, subject="", snippet=""):
    body_category_hits = body_category_hits or []
    return gmail_sorter.Decision(
        message_id=message_id,
        thread_id="t",
        date="2026-07-06",
        sender="Sender <x@example.com>",
        sender_email="x@example.com",
        sender_domain="example.com",
        registered_domain="example.com",
        subject=subject,
        snippet=snippet,
        body_len=len(body_text_excerpt),
        body_category_hits=body_category_hits,
        body_text_excerpt=body_text_excerpt,
        category_confidence={category: confidence},
    )


class CentroidBodyTextTests(unittest.TestCase):
    def _open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            return gmail_sorter.open_state_db(path)

    def test_centroid_text_contains_body_excerpt(self):
        conn = self._open()
        backend = CapturingBackend()
        decisions = [
            make_decision(
                f"m{i}", "Finance", 90,
                body_text_excerpt="Your January statement is now available in the portal.",
                subject="Statement",
                snippet="",
            )
            for i in range(3)
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        self.assertEqual(updated, 1)
        # The first embed call must contain the body excerpt.
        self.assertTrue(any("statement is now available" in call for call in backend.calls))
        conn.close()

    def test_legacy_decision_falls_back_to_category_hits(self):
        conn = self._open()
        backend = CapturingBackend()
        # A pre-v0.7 decision has no body_text_excerpt but carries body_category_hits.
        decisions = [
            make_decision(
                f"m{i}", "Health", 80,
                body_text_excerpt="",
                body_category_hits=["Health", "appointment"],
                subject="Appointment",
                snippet="",
            )
            for i in range(3)
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        self.assertEqual(updated, 1)
        # The fallback text is the legacy shape: subject + hits names.
        self.assertTrue(any("appointment" in call for call in backend.calls))
        conn.close()

    def test_low_confidence_messages_are_skipped(self):
        conn = self._open()
        backend = CapturingBackend()
        # Only one message at confidence 90; the other two are below the floor.
        decisions = [
            make_decision("m1", "Travel", 90, body_text_excerpt="flight to Paris"),
            make_decision("m2", "Travel", 50, body_text_excerpt="hotel reservation"),
            make_decision("m3", "Travel", 30, body_text_excerpt=""),
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        # Only one message above the floor: no centroid (need >= 3).
        self.assertEqual(updated, 0)
        # The below-floor messages should not be embedded at all.
        self.assertFalse(any("hotel reservation" in call for call in backend.calls))
        self.assertFalse(any("flight to Paris" in call for call in backend.calls))
        conn.close()

    def test_category_with_too_few_messages_yields_no_centroid(self):
        conn = self._open()
        backend = CapturingBackend()
        decisions = [
            make_decision("m1", "Travel", 90, body_text_excerpt="flight to Paris"),
            make_decision("m2", "Travel", 85, body_text_excerpt="hotel reservation"),
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        self.assertEqual(updated, 0)
        conn.close()

    def test_body_cap_bounds_embed_text(self):
        conn = self._open()
        backend = CapturingBackend()
        long_body = "x" * 10000
        decisions = [
            make_decision(f"m{i}", "Finance", 80, body_text_excerpt=long_body)
            for i in range(3)
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70, body_cap=200)
        self.assertEqual(updated, 1)
        for call in backend.calls:
            # The text passed to the embedder must respect the cap.
            self.assertLessEqual(len(call), 200)
        conn.close()

    def test_centroid_uses_subject_and_snippet(self):
        conn = self._open()
        backend = CapturingBackend()
        decisions = [
            make_decision(
                f"m{i}", "Shopping", 80,
                body_text_excerpt="Big sale today on all items",
                subject="Sale today",
                snippet="limited time",
            )
            for i in range(3)
        ]
        gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        # Every embed call must include the subject text.
        for call in backend.calls:
            self.assertIn("Sale today", call)
            self.assertIn("limited time", call)
        conn.close()

    def test_catchall_categories_are_excluded(self):
        conn = self._open()
        backend = CapturingBackend()
        decisions = [
            make_decision(f"m{i}", "Review", 90, body_text_excerpt="noise") for i in range(5)
        ]
        decisions += [
            make_decision(f"n{i}", "Updates", 90, body_text_excerpt="more noise") for i in range(5)
        ]
        updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
        # Catch-all categories never produce a centroid.
        self.assertEqual(updated, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
