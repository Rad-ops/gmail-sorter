"""Body-feature excerpt tests for v0.7.

v0.7 persists a privacy-bounded cleaned body excerpt to
``message_features.body_text_excerpt`` so the embedding centroid learner can
embed real message text on subsequent scans without re-fetching from Gmail.

These tests verify:
- :data:`gmail_sorter.BODY_EXCERPT_FOR_FEATURES` is the documented bound.
- The Decision dataclass carries a ``body_text_excerpt`` field.
- ``upsert_message_features`` writes the excerpt column; a second upsert is
  idempotent.
- ``load_body_features_index`` reads the excerpt back.
- The excerpt is bounded to 4000 chars and goes through the same
  quote/footer stripping as categorization.
- A message with no body does not get a row written (privacy floor).
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter


def make_decision(message_id, body_len, body_text_excerpt, body_category_hits=None, body_unsubscribe_links=None):
    """Build a minimal Decision for body-features testing."""

    body_unsubscribe_links = body_unsubscribe_links or []
    body_category_hits = body_category_hits or []
    return gmail_sorter.Decision(
        message_id=message_id,
        thread_id="t",
        date="2026-07-06",
        sender="Sender <x@example.com>",
        sender_email="x@example.com",
        sender_domain="example.com",
        registered_domain="example.com",
        subject="hi",
        snippet="",
        body_len=body_len,
        body_category_hits=body_category_hits,
        body_text_excerpt=body_text_excerpt,
        body_unsubscribe_links=body_unsubscribe_links,
    )


class BodyExcerptConstantTests(unittest.TestCase):
    def test_body_excerpt_constant(self):
        # The excerpt is bounded so a multi-year mailbox does not blow up the
        # local cache. 4000 chars is enough for a bank statement subject and a
        # few paragraphs; longer bodies are truncated.
        self.assertEqual(gmail_sorter.BODY_EXCERPT_FOR_FEATURES, 4000)


class BodyExcerptFieldTests(unittest.TestCase):
    def test_decision_has_body_text_excerpt_field(self):
        d = gmail_sorter.Decision(message_id="m", thread_id="t", date="", sender="", sender_email="", sender_domain="", registered_domain="", subject="", snippet="")
        self.assertEqual(d.body_text_excerpt, "")
        d.body_text_excerpt = "hello"
        self.assertEqual(d.body_text_excerpt, "hello")


class UpsertBodyFeaturesTests(unittest.TestCase):
    def _make_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            return gmail_sorter.open_state_db(path)

    def test_upsert_writes_excerpt_column(self):
        conn = self._make_conn()
        decision = make_decision("m1", body_len=120, body_text_excerpt="cleaned body text here")
        gmail_sorter.upsert_message_features(conn, [decision], scan_mode="full")
        row = conn.execute(
            "SELECT body_text_excerpt FROM message_features WHERE message_id=?", ("m1",)
        ).fetchone()
        self.assertEqual(row[0], "cleaned body text here")
        conn.close()

    def test_upsert_skips_zero_body(self):
        # Privacy floor: a message with no body never gets a feature row.
        conn = self._make_conn()
        decision = make_decision("m1", body_len=0, body_text_excerpt="")
        gmail_sorter.upsert_message_features(conn, [decision], scan_mode="full")
        row = conn.execute("SELECT message_id FROM message_features WHERE message_id=?", ("m1",)).fetchone()
        self.assertIsNone(row)
        conn.close()

    def test_upsert_is_idempotent(self):
        conn = self._make_conn()
        d1 = make_decision("m1", body_len=50, body_text_excerpt="first")
        d2 = make_decision("m1", body_len=80, body_text_excerpt="second")
        gmail_sorter.upsert_message_features(conn, [d1], scan_mode="full")
        gmail_sorter.upsert_message_features(conn, [d2], scan_mode="full")
        row = conn.execute(
            "SELECT body_len, body_text_excerpt FROM message_features WHERE message_id=?", ("m1",)
        ).fetchone()
        self.assertEqual(row[0], 80)
        self.assertEqual(row[1], "second")
        conn.close()

    def test_upsert_handles_empty_excerpt(self):
        # A body of length > 0 with no cleaned excerpt is allowed; the column
        # stores the empty string, the categorization still works because the
        # body_category_hits and body_len are populated.
        conn = self._make_conn()
        decision = make_decision("m1", body_len=10, body_text_excerpt="")
        gmail_sorter.upsert_message_features(conn, [decision], scan_mode="full")
        row = conn.execute(
            "SELECT body_text_excerpt, body_len FROM message_features WHERE message_id=?", ("m1",)
        ).fetchone()
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], 10)
        conn.close()


class LoadBodyFeaturesIndexTests(unittest.TestCase):
    def _make_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            return gmail_sorter.open_state_db(path)

    def test_index_includes_excerpt(self):
        conn = self._make_conn()
        d = make_decision(
            "m1",
            body_len=200,
            body_text_excerpt="the cleaned body",
            body_category_hits=["Finance"],
            body_unsubscribe_links=["https://example.com/u"],
        )
        gmail_sorter.upsert_message_features(conn, [d], scan_mode="full")
        index = gmail_sorter.load_body_features_index(conn)
        self.assertIn("m1", index)
        self.assertEqual(index["m1"]["body_text_excerpt"], "the cleaned body")
        self.assertEqual(index["m1"]["body_len"], 200)
        self.assertEqual(index["m1"]["body_category_hits"], ["Finance"])
        self.assertEqual(index["m1"]["body_unsubscribe_count"], 1)
        conn.close()

    def test_index_omits_non_full_rows(self):
        # The index only serves the --scan full fast path; metadata-only
        # cache rows should not be exposed (they have no body anyway).
        conn = self._make_conn()
        d = make_decision("m1", body_len=20, body_text_excerpt="text")
        gmail_sorter.upsert_message_features(conn, [d], scan_mode="metadata")
        index = gmail_sorter.load_body_features_index(conn)
        self.assertNotIn("m1", index)
        conn.close()

    def test_index_empty_excerpt_round_trip(self):
        conn = self._make_conn()
        d = make_decision("m1", body_len=20, body_text_excerpt="")
        gmail_sorter.upsert_message_features(conn, [d], scan_mode="full")
        index = gmail_sorter.load_body_features_index(conn)
        self.assertEqual(index["m1"]["body_text_excerpt"], "")
        conn.close()


class BodyExcerptInDecideTests(unittest.TestCase):
    """decide() must populate body_text_excerpt for body-aware scans."""

    def test_decide_sets_excerpt_for_full_scan(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full")
        body_text = "Hello, this is a finance newsletter from your bank."
        msg = message(
            body_payload({"From": "Bank <noreply@bank.com>", "Subject": "Statement ready"}, body_text),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertTrue(decision.body_text_excerpt)
        self.assertLessEqual(len(decision.body_text_excerpt), gmail_sorter.BODY_EXCERPT_FOR_FEATURES)

    def test_decide_omits_excerpt_for_metadata_scan(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="metadata")
        body_text = "Hello, this is a finance newsletter from your bank."
        msg = message(
            body_payload({"From": "Bank <noreply@bank.com>", "Subject": "Statement ready"}, body_text),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # Metadata scans do not see the body, so the excerpt stays empty.
        self.assertEqual(decision.body_text_excerpt, "")

    def test_decide_strips_quotes_and_footer_in_excerpt(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full")
        body_text = (
            "Original reply above the line.\n"
            "> Quoted promo: 50% off, unsubscribe here\n"
            "Real reply content follows.\n"
            "-- \n"
            "John Doe\n"
        )
        msg = message(
            body_payload({"From": "Real Person <john@example.com>", "Subject": "Re: hi"}, body_text),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # Quoted line and signature must not appear in the persisted excerpt.
        self.assertNotIn("Quoted promo", decision.body_text_excerpt)
        self.assertNotIn("unsubscribe", decision.body_text_excerpt.lower().split("real reply content follows.")[0])
        # But the real reply content should.
        self.assertIn("Real reply content follows", decision.body_text_excerpt)


if __name__ == "__main__":
    unittest.main()
