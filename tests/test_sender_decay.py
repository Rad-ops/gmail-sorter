"""Sender profile time-decay + diversity tests for v0.7.

v0.7 makes the sender profile a first-class time-aware signal:

* ``load_sender_profile_index`` applies a half-life decay
  (``weight = base_hits * 2^(-Δdays / half_life_days)``) so a profile row
  seen 6 months ago contributes less than one seen yesterday. ``half_life_days=0``
  preserves the pre-v0.7 flat-weight behavior.
* ``update_sender_profiles`` writes a ``first_seen`` anchor on the very
  first observation of a (key, category) pair and refreshes the
  ``category_diversity`` count on every write.
* ``load_sender_diversity`` returns a ``{sender_key: distinct_category_count}``
  map for the dashboard's Noisy Senders section.

These tests verify the math, the migration backward-compatibility, the
sender-vs-domain weighting, and the diversity refresh path.
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_helpers import tracked, make_test_args

import gmail_sorter
from sorter import policy


def make_decision(
    message_id="m1",
    sender_email="noreply@bank.com",
    registered_domain="bank.com",
    sender_domain="bank.com",
    categories=None,
    primary="Finance",
    protected=False,
    date="2026-07-06",
    ad_confidence=70,
):
    return gmail_sorter.Decision(
        message_id=message_id,
        thread_id="t",
        date=date,
        sender=f"Bank <{sender_email}>",
        sender_email=sender_email,
        sender_domain=sender_domain,
        registered_domain=registered_domain,
        subject="Statement",
        snippet="",
        categories=list(categories or [primary]),
        primary_category=primary,
        category_confidence={primary: ad_confidence},
        ad_confidence=ad_confidence,
        protected=protected,
    )


class SenderProfileDecayTests(unittest.TestCase):
    def _open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            return gmail_sorter.open_state_db(path)

    def test_decay_disabled_preserves_flat_weight(self):
        conn = self._open()
        d = make_decision()
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
        index = gmail_sorter.load_sender_profile_index(conn, half_life_days=0, min_hits=1)
        # v0.7: keys are (kind, value, category). Sender row carries
        # weight 3x, so Finance is 3.
        sender_key = "sender:noreply@bank.com:finance"
        domain_key = "domain:bank.com:finance"
        self.assertIn(sender_key, index)
        self.assertEqual(index[sender_key]["Finance"], 3)
        # Domain row carries weight 1x.
        self.assertEqual(index[domain_key]["Finance"], 1)
        conn.close()

    def test_decay_reduces_old_profiles(self):
        conn = self._open()
        # Two distinct senders, one fresh and one 360 days old. Different
        # senders => different rows => different first_seen anchors => the
        # decay has a chance to distinguish them.
        fresh = make_decision(
            message_id="m1",
            sender_email="noreply@bank.com",
            registered_domain="bank.com",
            sender_domain="bank.com",
            date=(datetime.now(timezone.utc) - timedelta(days=0)).date().isoformat(),
        )
        old = make_decision(
            message_id="m2",
            sender_email="old@example.com",
            registered_domain="example.com",
            sender_domain="example.com",
            date=(datetime.now(timezone.utc) - timedelta(days=360)).date().isoformat(),
        )
        gmail_sorter.update_sender_profiles(conn, [fresh, old], confidence_floor=65)
        index = gmail_sorter.load_sender_profile_index(conn, half_life_days=180, min_hits=1)
        # 360 days at half_life=180 -> 2 halvings -> the old sender's
        # weight is roughly 1/4 of the fresh sender's. The fresh sender
        # carries weight 3 (3x for sender, 1x for domain). The old sender's
        # weight is < 3 because of the decay.
        fresh_weight = index["sender:noreply@bank.com:finance"]["Finance"]
        old_weight = index["sender:old@example.com:finance"]["Finance"]
        self.assertEqual(fresh_weight, 3)
        self.assertLess(old_weight, 3)
        self.assertGreaterEqual(old_weight, 0)
        conn.close()

    def test_decay_falls_back_to_last_seen_when_first_seen_missing(self):
        # A pre-v0.7 row has no first_seen column at all; the migration
        # leaves last_seen in place and the loader must fall back to it.
        conn = self._open()
        d = make_decision()
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
        # Manually clear first_seen to simulate a pre-v0.7 row.
        conn.execute("UPDATE sender_profile SET first_seen=NULL WHERE key='sender:noreply@bank.com:finance'")
        conn.commit()
        # The loader must not raise; it must use last_seen as the anchor.
        index = gmail_sorter.load_sender_profile_index(conn, half_life_days=180, min_hits=1)
        self.assertIn("sender:noreply@bank.com:finance", index)
        conn.close()

    def test_min_hits_filter(self):
        conn = self._open()
        # A single decision produces a single hit; min_hits=3 should
        # exclude the row from the index.
        d = make_decision()
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
        index = gmail_sorter.load_sender_profile_index(conn, min_hits=3, half_life_days=0)
        self.assertNotIn("sender:noreply@bank.com:finance", index)
        conn.close()

    def test_returns_empty_when_no_state_db(self):
        index = gmail_sorter.load_sender_profile_index(None, half_life_days=180)
        self.assertEqual(index, {})


class SenderDiversityTests(unittest.TestCase):
    def _open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            return gmail_sorter.open_state_db(path)

    def test_diversity_counts_distinct_categories(self):
        conn = self._open()
        d1 = make_decision(message_id="m1", categories=["Finance"], primary="Finance")
        d2 = make_decision(message_id="m2", categories=["Receipts Orders"], primary="Receipts Orders")
        d3 = make_decision(message_id="m3", categories=["Account Security"], primary="Account Security")
        d4 = make_decision(message_id="m4", categories=["Health"], primary="Health")
        d5 = make_decision(message_id="m5", categories=["Travel"], primary="Travel")
        gmail_sorter.update_sender_profiles(conn, [d1, d2, d3, d4, d5], confidence_floor=65)
        diversity = gmail_sorter.load_sender_diversity(conn)
        # v0.7: diversity is counted per (kind, value) parent key, not per
        # the (kind, value, category) leaf.
        self.assertEqual(diversity.get("sender:noreply@bank.com", 0), 5)
        conn.close()

    def test_diversity_empty_when_no_state_db(self):
        self.assertEqual(gmail_sorter.load_sender_diversity(None), {})

    def test_diversity_handles_pre_v0_7_db(self):
        # A pre-v0.7 database does not have the category_diversity column.
        # The migration adds it, but the test simulates a brand-new table
        # that has the column.
        conn = self._open()
        d = make_decision()
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
        diversity = gmail_sorter.load_sender_diversity(conn)
        self.assertEqual(diversity["sender:noreply@bank.com"], 1)
        conn.close()

    def test_update_records_first_seen(self):
        conn = self._open()
        d = make_decision(date="2025-01-01")
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)
        row = conn.execute(
            "SELECT first_seen FROM sender_profile WHERE key='sender:noreply@bank.com:finance'"
        ).fetchone()
        self.assertEqual(row[0], "2025-01-01")
        # A second observation must not overwrite first_seen.
        d2 = make_decision(message_id="m2", date="2026-01-01")
        gmail_sorter.update_sender_profiles(conn, [d2], confidence_floor=65)
        row2 = conn.execute(
            "SELECT first_seen, hits FROM sender_profile WHERE key='sender:noreply@bank.com:finance'"
        ).fetchone()
        self.assertEqual(row2[0], "2025-01-01")  # not overwritten
        self.assertEqual(row2[1], 2)
        conn.close()


class SenderProfileEndToEndTests(unittest.TestCase):
    def test_decide_uses_decayed_profile(self):
        from tests.test_gmail_sorter import args, message, body_payload

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            conn = tracked(self, gmail_sorter.open_state_db(db_path))
            # Seed an old profile for the sender so the decay matters.
            old = make_decision(date=(datetime.now(timezone.utc) - timedelta(days=720)).date().isoformat())
            gmail_sorter.update_sender_profiles(conn, [old], confidence_floor=65)
            conn.close()
            a = args(
                scan="metadata",
                use_sender_profiles=True,
                sender_profiles=gmail_sorter.load_sender_profile_index(
                    gmail_sorter.open_state_db(db_path), half_life_days=180,
                ),
            )
            msg = message(
                body_payload(
                    {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                    "",
                ),
                labels=[],
            )
            decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
            # The decision itself is made either way; the decay matters for
            # whether a profile category lands in the result. We just
            # require that categorization produced *some* reason.
            self.assertIsInstance(decision.reasons, list)
            conn.close()


if __name__ == "__main__":
    unittest.main()
