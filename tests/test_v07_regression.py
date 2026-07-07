"""v0.7 regression tests for the four headline fixes.

These tests pin down the v0.7 behavior at the regression level so a
future change cannot silently re-break what v0.7 set out to fix:

1. Real body text in category centroids.
2. Multi-language keyword overlays (FR + FA).
3. AI active learning + AI removal.
4. Sender profile time-decay + diversity + per-category key.

Each test corresponds to a specific bug or design weakness that the
handover document called out. The tests are larger than the property
tests because the regression net for a fixed bug is the exact
behavior the user asked for, not a random input.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_helpers import tracked, make_test_args

import gmail_sorter


class CapturingBackend:
    """Embedding backend that records every embed() call."""

    def __init__(self, dim=8):
        self.dim = dim
        self.calls: list[str] = []

    def embed(self, text):
        self.calls.append(text)
        if not text:
            return None
        return [float((ord(c) % 13) / 13.0) for c in text[: self.dim]] + [0.0] * max(0, self.dim - len(text))


def make_decision(
    message_id="m1",
    sender_email="noreply@bank.com",
    registered_domain="bank.com",
    categories=None,
    primary="Finance",
    body_text_excerpt="",
    body_len=0,
    body_category_hits=None,
    date="2026-07-06",
    ad_confidence=70,
    protected=False,
    subject="Your statement",
    snippet="",
):
    return gmail_sorter.Decision(
        message_id=message_id,
        thread_id="t",
        date=date,
        sender=f"Bank <{sender_email}>",
        sender_email=sender_email,
        sender_domain=sender_email.split("@", 1)[1] if "@" in sender_email else "",
        registered_domain=registered_domain,
        subject=subject,
        snippet=snippet,
        body_len=body_len,
        body_category_hits=body_category_hits or [],
        body_text_excerpt=body_text_excerpt,
        categories=list(categories or [primary]),
        primary_category=primary,
        category_confidence={primary: ad_confidence},
        ad_confidence=ad_confidence,
        protected=protected,
    )


class V07CentroidFixTests(unittest.TestCase):
    """v0.7.0 fix: centroids embed the real body, not the hit names."""

    def test_centroid_uses_body_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            backend = CapturingBackend()
            decisions = [
                make_decision(
                    f"m{i}",
                    primary="Finance",
                    body_text_excerpt="Your January statement is now available in the portal.",
                    body_len=51,
                )
                for i in range(3)
            ]
            updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
            self.assertEqual(updated, 1)
            # The first embed call MUST contain the body text.
            self.assertTrue(
                any("statement is now available" in call for call in backend.calls),
                f"centroid text did not contain the body excerpt: {backend.calls}",
            )
            conn.close()

    def test_legacy_decision_still_works_via_fallback(self):
        # A pre-v0.7 decision (no body_text_excerpt) must still produce
        # a centroid that uses the body_category_hits fallback so the
        # in-flight centroid refresh doesn't lose precision.
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            backend = CapturingBackend()
            decisions = [
                make_decision(
                    f"m{i}",
                    primary="Health",
                    body_category_hits=["Health", "appointment"],
                    body_text_excerpt="",
                )
                for i in range(3)
            ]
            updated = gmail_sorter.update_category_centroids(conn, decisions, backend, confidence_floor=70)
            self.assertEqual(updated, 1)
            # The fallback path uses the category hits names.
            self.assertTrue(any("appointment" in call for call in backend.calls))
            conn.close()


class V07MultiLanguageTests(unittest.TestCase):
    """v0.7.0: French and Farsi messages land in the right protected bucket."""

    def test_french_immigration_message_lands_in_priority_immigration(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(
            scan="full",
            _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"),
        )
        body = (
            "Bonjour, votre demande de permis de travail est en cours de traitement. "
            "Veuillez envoyer vos documents. Cordialement, IRCC."
        )
        msg = message(
            body_payload(
                {"From": "IRCC <noreply@cic.gc.ca>", "Subject": "Votre demande de permis"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "fr")
        self.assertIn("Priority Immigration", decision.categories)

    def test_farsi_health_message_lands_in_health(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(
            scan="full",
            _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"),
        )
        body = "سلام، نوبت پزشک شما فردا تنظیم شد. لطفا مراجعه کنید. ممنون."
        msg = message(
            body_payload(
                {"From": "Clinic <noreply@clinic.com>", "Subject": "نوبت پزشک"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "fa")
        self.assertIn("Health", decision.categories)

    def test_overlay_does_not_persist_across_messages(self):
        # After a French message, the policy must be back to the English state.
        from tests.test_gmail_sorter import args, message, body_payload
        from sorter import policy

        a = args(
            scan="full",
            _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"),
        )
        original_immigration = list(policy.IMMIGRATION_KEYWORDS)
        fr_msg = message(
            body_payload(
                {"From": "IRCC <noreply@cic.gc.ca>", "Subject": "permis"},
                "Votre demande de permis de travail. Cordialement.",
            ),
            labels=[],
        )
        gmail_sorter.decide(fr_msg, a, gmail_sorter.Config())
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original_immigration)

    def test_english_message_does_not_apply_overlay(self):
        # A pure English message must not pull in any French/Farsi
        # keywords; the policy must be unchanged.
        from tests.test_gmail_sorter import args, message, body_payload
        from sorter import policy

        a = args(
            scan="full",
            _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"),
        )
        original_immigration = list(policy.IMMIGRATION_KEYWORDS)
        en_msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "Your bank statement is ready. Thank you for banking with us.",
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(en_msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "en")
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original_immigration)


class V07AIRemovalTests(unittest.TestCase):
    """v0.7.0: AI can REMOVE a non-protected category; protected stays."""

    def test_ai_can_remove_shopping_when_actually_finance(self):
        decision = make_decision(
            categories=["Finance", "Shopping"],
            primary="Finance",
            body_category_hits=[],
            ad_confidence=80,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            with path.open("w") as f:
                f.write(json.dumps({
                    "message_id": decision.message_id,
                    "ai_label": "Shopping",
                    "ai_confidence": 0.95,
                    "ai_reviewed": True,
                }) + "\n")
            _, _, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(removed, 1)
            self.assertNotIn("Shopping", decision.categories)
            self.assertIn("Finance", decision.categories)
            self.assertTrue(any(r.startswith("ai_remove:Shopping:0.95") for r in decision.reasons))

    def test_ai_cannot_remove_priority_immigration(self):
        decision = make_decision(
            categories=["Priority Immigration", "Finance"],
            primary="Priority Immigration",
            protected=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            with path.open("w") as f:
                f.write(json.dumps({
                    "message_id": decision.message_id,
                    "ai_label": "Priority Immigration",
                    "ai_confidence": 0.99,
                    "ai_reviewed": True,
                }) + "\n")
            _, _, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(removed, 0)
            self.assertIn("Priority Immigration", decision.categories)

    def test_ai_can_add_a_label_it_missed(self):
        # v0.6 additive behavior is preserved: the AI can add a label
        # the code missed, even at the default 0.7 threshold.
        decision = make_decision(
            categories=["Review"],
            primary="Review",
            body_category_hits=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.jsonl"
            with path.open("w") as f:
                f.write(json.dumps({
                    "message_id": decision.message_id,
                    "ai_label": "Finance",
                    "ai_confidence": 0.85,
                    "ai_reviewed": True,
                }) + "\n")
            _, overridden, removed = gmail_sorter.merge_ai_labels(
                [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
            )
            self.assertEqual(overridden, 1)
            self.assertEqual(removed, 0)
            self.assertIn("Finance", decision.categories)


class V07AILearningTests(unittest.TestCase):
    """v0.7.0: AI active learning pushes verified decisions back into state."""

    def test_ai_pushed_into_sender_profile(self):
        from sorter.ai_learning import apply_ai_learning

        decision = make_decision()
        packets = [{
            "message_id": decision.message_id,
            "ai_label": "Receipts Orders",
            "ai_confidence": 0.92,
            "ai_reviewed": True,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
            self.assertEqual(report["considered"], 1)
            self.assertGreaterEqual(report["profile_bumps"], 1)
            row = conn.execute(
                "SELECT category FROM sender_profile WHERE key='sender:noreply@bank.com:receipts orders'"
            ).fetchone()
            self.assertEqual(row[0], "Receipts Orders")
            conn.close()

    def test_ai_pushed_into_centroid(self):
        from sorter.ai_learning import apply_ai_learning

        decision = make_decision(
            body_text_excerpt="Your January statement is now available.",
            body_len=42,
        )
        packets = [{
            "message_id": decision.message_id,
            "ai_label": "Finance",
            "ai_confidence": 0.95,
            "ai_reviewed": True,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            backend = CapturingBackend()
            report = apply_ai_learning(conn, [decision], packets, embedding_backend=backend)
            self.assertEqual(report["centroid_contributions"], 1)
            row = conn.execute(
                "SELECT category, message_count FROM category_centroid WHERE category='Finance'"
            ).fetchone()
            self.assertEqual(row[0], "Finance")
            self.assertEqual(row[1], 1)
            conn.close()

    def test_ai_learning_skips_unreviewed_packets(self):
        from sorter.ai_learning import apply_ai_learning

        decision = make_decision()
        packets = [{
            "message_id": decision.message_id,
            "ai_label": "Finance",
            "ai_confidence": 0.9,
            "ai_reviewed": False,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
            self.assertEqual(report["considered"], 0)
            conn.close()


class V07SenderProfileTests(unittest.TestCase):
    """v0.7.0: time-decay, per-category keys, diversity."""

    def test_old_profile_decays(self):
        # A 720-day-old profile carries far less weight than a fresh one.
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            old = make_decision(message_id="old", date=(datetime.now(timezone.utc) - timedelta(days=720)).date().isoformat())
            fresh = make_decision(
                message_id="fresh", sender_email="noreply@insurer.com",
                registered_domain="insurer.com", primary="Insurance",
                date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
            )
            gmail_sorter.update_sender_profiles(conn, [old, fresh], confidence_floor=65)
            index = gmail_sorter.load_sender_profile_index(conn, half_life_days=30, min_hits=1)
            fresh_weight = index.get("sender:noreply@insurer.com:insurance", {}).get("Insurance", 0)
            old_weight = index.get("sender:noreply@bank.com:finance", {}).get("Finance", 0)
            self.assertGreaterEqual(fresh_weight, 3)
            self.assertLessEqual(old_weight, 1)
            conn.close()

    def test_per_category_key_collision_fixed(self):
        # A sender with two distinct categories gets two rows, not one
        # collision (the pre-v0.7 bug).
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            d1 = make_decision(message_id="m1", primary="Finance", categories=["Finance"])
            d2 = make_decision(message_id="m2", primary="Receipts Orders", categories=["Receipts Orders"])
            gmail_sorter.update_sender_profiles(conn, [d1, d2], confidence_floor=65)
            # Two distinct (key, category) rows.
            rows = conn.execute(
                "SELECT DISTINCT key FROM sender_profile WHERE kind='sender'"
            ).fetchall()
            keys = {row[0] for row in rows}
            self.assertIn("sender:noreply@bank.com:finance", keys)
            self.assertIn("sender:noreply@bank.com:receipts orders", keys)
            conn.close()

    def test_diversity_counts_distinct_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            for i, primary in enumerate(["Finance", "Receipts Orders", "Account Security", "Health", "Travel"]):
                d = make_decision(
                    message_id=f"m{i}", categories=[primary], primary=primary,
                )
                gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
            diversity = gmail_sorter.load_sender_diversity(conn)
            # The parent (sender) key carries 5 distinct categories.
            self.assertEqual(diversity.get("sender:noreply@bank.com", 0), 5)
            conn.close()


class V07BodyExcerptTests(unittest.TestCase):
    """v0.7.0: body excerpt is persisted and bounded."""

    def test_excerpt_bounded_to_4000_chars(self):
        from tests.test_gmail_sorter import args, message, body_payload
        a = args(scan="full")
        big_body = "x" * 10_000
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                big_body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertLessEqual(len(decision.body_text_excerpt), gmail_sorter.BODY_EXCERPT_FOR_FEATURES)

    def test_excerpt_persists_to_message_features(self):
        from tests.test_gmail_sorter import args, message, body_payload
        a = args(scan="full")
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "Your January statement is now available.",
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            gmail_sorter.upsert_message_features(conn, [decision], scan_mode="full")
            row = conn.execute(
                "SELECT body_text_excerpt FROM message_features WHERE message_id=?",
                (decision.message_id,),
            ).fetchone()
            self.assertEqual(row[0], decision.body_text_excerpt)
            conn.close()


class V07SchemaMigrationTests(unittest.TestCase):
    """v0.7.0: schema migrations work for fresh and v1-state databases."""

    def test_fresh_db_lands_at_v3(self):
        # v0.7 regression: a fresh DB lands at v3.
        # v0.8.1 adds v4 which also lands; this test pins the v3
        # floor, not the v3 ceiling.
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertGreaterEqual(row[0], 3)

    def test_v1_state_db_migrates_to_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sqlite"
            seed = tracked(self, sqlite3.connect(str(path)))
            seed.execute(
                """
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    date TEXT,
                    sender TEXT,
                    sender_email TEXT,
                    sender_domain TEXT,
                    registered_domain TEXT,
                    subject TEXT,
                    categories_json TEXT NOT NULL,
                    planned_actions_json TEXT NOT NULL,
                    ad_confidence INTEGER NOT NULL,
                    protected INTEGER NOT NULL,
                    perfect_ad_match INTEGER NOT NULL,
                    has_attachment INTEGER NOT NULL,
                    has_real_attachment INTEGER NOT NULL,
                    attachment_count INTEGER NOT NULL,
                    inline_attachment_count INTEGER NOT NULL,
                    message_size_estimate INTEGER NOT NULL,
                    review_priority TEXT,
                    action_done TEXT,
                    scanned_at TEXT,
                    decision_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            seed.execute(
                """
                CREATE TABLE message_features (
                    message_id TEXT PRIMARY KEY,
                    body_len INTEGER NOT NULL DEFAULT 0,
                    body_category_hits_json TEXT NOT NULL DEFAULT '[]',
                    body_unsubscribe_count INTEGER NOT NULL DEFAULT 0,
                    scan_mode TEXT NOT NULL DEFAULT 'metadata',
                    updated_at TEXT NOT NULL
                )
                """
            )
            seed.execute(
                """
                CREATE TABLE sender_profile (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    protected_hits INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            seed.commit()
            seed.close()
            # Open with the sorter; the migration must bring the DB to at
            # least v3 (v0.8.1 adds v4, but the v3 floor is what the
            # v0.7 regression test pins).
            conn = tracked(self, gmail_sorter.open_state_db(path))
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertGreaterEqual(row[0], 3)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sender_profile)").fetchall()}
            self.assertIn("first_seen", cols)
            self.assertIn("category_diversity", cols)
            self.assertIn("body_text_excerpt", {r[1] for r in conn.execute("PRAGMA table_info(message_features)").fetchall()})


if __name__ == "__main__":
    unittest.main()
