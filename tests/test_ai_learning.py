"""AI active learning + AI removal tests for v0.7.

v0.7 closes the AI review loop:

* ``merge_ai_labels`` learns a new third return value, ``removed``: the
  count of messages the AI actively corrected by removing a non-protected
  category the code assigned. Removal requires ``--ai-merge-min-removal-confidence``
  (default 0.85), stricter than the addition threshold (0.7), because
  removal is harder to undo than addition.
* After every merge, ``sorter.ai_learning.apply_ai_learning`` pushes the
  AI's verified decisions into the local SQLite state:
  - a sender_profile bump for the AI's chosen category, and
  - when an embedding backend is available, a centroid contribution
    weighted by AI confidence.
* The protection gate is preserved end-to-end: a protected category is
  never removed by the AI, and a protected message with a non-protected
  AI label is excluded from the learning pass.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter import policy
from sorter.ai_learning import apply_ai_learning


def make_decision(
    message_id="m1",
    sender_email="noreply@bank.com",
    registered_domain="bank.com",
    categories=None,
    primary="Finance",
    protected=False,
    subject="Your statement",
    snippet="",
    body_text_excerpt="",
    body_len=0,
    category_confidence=None,
):
    categories = categories if categories is not None else [primary]
    category_confidence = category_confidence or {primary: 90}
    return gmail_sorter.Decision(
        message_id=message_id,
        thread_id="t",
        date="2026-07-06",
        sender=f"Bank <{sender_email}>",
        sender_email=sender_email,
        sender_domain=sender_email.split("@", 1)[1] if "@" in sender_email else "",
        registered_domain=registered_domain,
        subject=subject,
        snippet=snippet,
        body_len=body_len,
        body_category_hits=[],
        body_text_excerpt=body_text_excerpt,
        categories=list(categories),
        primary_category=primary,
        category_confidence=dict(category_confidence),
        protected=protected,
    )


def write_packet(path, message_id, ai_label, ai_confidence, ai_reason="AI reason", ai_reviewed=True):
    packet = {
        "message_id": message_id,
        "ai_label": ai_label,
        "ai_confidence": ai_confidence,
        "ai_reason": ai_reason,
        "ai_reviewed": ai_reviewed,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(packet) + "\n")


class CapturingBackend:
    def __init__(self, dim=8):
        self.dim = dim
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        if not text:
            return None
        return [float((ord(c) % 13) / 13.0) for c in text[: self.dim]] + [0.0] * max(0, self.dim - len(text))


def open_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite"
        return gmail_sorter.open_state_db(path)


class AIRemovalTests(unittest.TestCase):
    """merge_ai_labels gain a third return value: the count of removals."""

    def test_removal_removes_non_protected_category(self):
        decision = make_decision(
            message_id="m1",
            categories=["Shopping", "Finance"],
            primary="Finance",
            category_confidence={"Shopping": 60, "Finance": 90},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            write_packet(path, "m1", "Shopping", 0.95, ai_reason="Actually finance only")
            agreed, overridden, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(removed, 1)
            self.assertEqual(overridden, 0)
            self.assertNotIn("Shopping", decision.categories)
            self.assertIn("Finance", decision.categories)
            self.assertTrue(any(r.startswith("ai_remove:Shopping:0.95") for r in decision.reasons))

    def test_removal_requires_strict_confidence(self):
        decision = make_decision(
            message_id="m1",
            categories=["Shopping", "Finance"],
            primary="Finance",
            category_confidence={"Shopping": 60, "Finance": 90},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            # 0.8 < 0.85 (default removal threshold) -> no removal.
            write_packet(path, "m1", "Shopping", 0.8)
            _, _, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(removed, 0)
            self.assertIn("Shopping", decision.categories)

    def test_removal_never_touches_protected_category(self):
        decision = make_decision(
            message_id="m1",
            categories=["Priority Immigration", "Finance"],
            primary="Priority Immigration",
            protected=True,
            category_confidence={"Priority Immigration": 100, "Finance": 80},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            write_packet(path, "m1", "Priority Immigration", 0.99)
            _, _, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            # Protected category cannot be removed, even at 0.99 confidence.
            self.assertEqual(removed, 0)
            self.assertIn("Priority Immigration", decision.categories)

    def test_agrees_when_ai_label_matches_code_label(self):
        decision = make_decision(
            message_id="m1",
            categories=["Finance"],
            primary="Finance",
            category_confidence={"Finance": 90},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            write_packet(path, "m1", "Finance", 0.9)
            agreed, overridden, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(agreed, 1)
            self.assertEqual(overridden, 0)
            self.assertEqual(removed, 0)


class AILearningTests(unittest.TestCase):
    """apply_ai_learning pushes AI decisions into sender_profile and centroids."""

    def test_learning_writes_sender_profile(self):
        conn = open_db()
        decision = make_decision(
            message_id="m1",
            sender_email="noreply@bank.com",
            registered_domain="bank.com",
            primary="Finance",
            categories=["Finance"],
        )
        packets = [{
            "message_id": "m1",
            "ai_label": "Receipts Orders",
            "ai_confidence": 0.92,
            "ai_reviewed": True,
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
        self.assertEqual(report["considered"], 1)
        self.assertGreaterEqual(report["profile_bumps"], 1)
        rows = conn.execute(
            "SELECT category, hits FROM sender_profile WHERE key=?",
            ("sender:noreply@bank.com",),
        ).fetchall()
        cats = {row[0] for row in rows}
        self.assertIn("Receipts Orders", cats)
        conn.close()

    def test_learning_writes_domain_profile(self):
        conn = open_db()
        decision = make_decision(
            message_id="m1",
            sender_email="noreply@bank.com",
            registered_domain="bank.com",
            primary="Finance",
        )
        packets = [{
            "message_id": "m1",
            "ai_label": "Receipts Orders",
            "ai_confidence": 0.92,
            "ai_reviewed": True,
        }]
        apply_ai_learning(conn, [decision], packets, embedding_backend=None)
        rows = conn.execute(
            "SELECT category FROM sender_profile WHERE key=?",
            ("domain:bank.com",),
        ).fetchall()
        self.assertIn("Receipts Orders", {row[0] for row in rows})
        conn.close()

    def test_learning_skips_protected_message_with_non_protected_label(self):
        conn = open_db()
        decision = make_decision(
            message_id="m1",
            sender_email="noreply@bank.com",
            registered_domain="bank.com",
            primary="Priority Immigration",
            protected=True,
            categories=["Priority Immigration"],
            category_confidence={"Priority Immigration": 100},
        )
        packets = [{
            "message_id": "m1",
            "ai_label": "Shopping",
            "ai_confidence": 0.99,
            "ai_reviewed": True,
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
        # Considered 0 because the message is protected and the AI label is
        # not protected. The merge step is what enforces the broader
        # invariant; the learning path mirrors it defensively.
        self.assertEqual(report["considered"], 0)
        conn.close()

    def test_learning_writes_centroid_when_backend_present(self):
        conn = open_db()
        backend = CapturingBackend()
        decision = make_decision(
            message_id="m1",
            subject="Your statement",
            body_text_excerpt="Your January statement is now available.",
            body_len=40,
        )
        packets = [{
            "message_id": "m1",
            "ai_label": "Finance",
            "ai_confidence": 0.95,
            "ai_reviewed": True,
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=backend)
        self.assertEqual(report["considered"], 1)
        self.assertEqual(report["centroid_contributions"], 1)
        row = conn.execute(
            "SELECT category, dimension, message_count FROM category_centroid WHERE category=?",
            ("Finance",),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], 8)
        self.assertEqual(row[2], 1)
        conn.close()

    def test_learning_skips_centroid_when_confidence_below_floor(self):
        conn = open_db()
        backend = CapturingBackend()
        decision = make_decision(message_id="m1", body_text_excerpt="some text", body_len=9)
        packets = [{
            "message_id": "m1",
            "ai_label": "Finance",
            "ai_confidence": 0.5,  # below 0.7 floor
            "ai_reviewed": True,
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=backend)
        self.assertEqual(report["considered"], 1)
        self.assertEqual(report["centroid_contributions"], 0)
        conn.close()

    def test_learning_skips_unreviewed_packets(self):
        conn = open_db()
        decision = make_decision()
        packets = [{
            "message_id": "m1",
            "ai_label": "Finance",
            "ai_confidence": 0.9,
            "ai_reviewed": False,  # not reviewed yet
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
        self.assertEqual(report["considered"], 0)
        self.assertEqual(report["profile_bumps"], 0)
        conn.close()

    def test_learning_handles_no_state_db(self):
        decision = make_decision()
        packets = [{
            "message_id": "m1",
            "ai_label": "Finance",
            "ai_confidence": 0.9,
            "ai_reviewed": True,
        }]
        # No state connection: function returns a no-op report.
        report = apply_ai_learning(None, [decision], packets, embedding_backend=None)
        self.assertEqual(report["considered"], 0)
        self.assertEqual(report["profile_bumps"], 0)

    def test_learning_skips_catchall_labels(self):
        conn = open_db()
        decision = make_decision()
        packets = [{
            "message_id": "m1",
            "ai_label": "Review",
            "ai_confidence": 0.99,
            "ai_reviewed": True,
        }]
        report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
        self.assertEqual(report["considered"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
