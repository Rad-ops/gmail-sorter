"""Tests for v0.8 sender reputation as a first-class signal."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter import sender_reputation
from sorter.sender_reputation import (
    BLOCKLIST_SUGGESTION_MAX_PROTECTED,
    BLOCKLIST_SUGGESTION_MIN_AD_FRACTION,
    BLOCKLIST_SUGGESTION_MIN_MESSAGES,
    REPUTATION_HIGH_THRESHOLD,
    REPUTATION_LOW_THRESHOLD,
    SenderReputation,
    build_sender_reputation,
    compute_reputation_score,
    load_sender_reputation_index,
    reputation_ad_adjustment,
    suggest_blocklist,
    upsert_sender_reputation,
)


class ComputeReputationScoreTests(unittest.TestCase):
    def test_zero_messages_returns_zero(self):
        self.assertEqual(compute_reputation_score(0, 0.0), 0)

    def test_high_volume_low_ad_high_score(self):
        # 1000 messages, 5% ad -> very high score.
        score = compute_reputation_score(1000, 0.05)
        self.assertGreater(score, 80)

    def test_high_volume_high_ad_low_score(self):
        # 1000 messages, 95% ad -> very low score.
        score = compute_reputation_score(1000, 0.95)
        self.assertLess(score, 20)

    def test_low_volume_low_ad_medium_score(self):
        # 5 messages, 0% ad -> moderate score.
        score = compute_reputation_score(5, 0.0)
        # log(1+5) * 100 / 5 ~= 32
        self.assertGreater(score, 20)
        self.assertLess(score, 60)

    def test_score_in_range(self):
        for n in [1, 5, 10, 50, 100, 1000]:
            for ad_frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
                score = compute_reputation_score(n, ad_frac)
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 100)


class BuildSenderReputationTests(unittest.TestCase):
    def _seed(self, conn, sender_email, n_messages, n_ads=0, n_protected=0, dates=None):
        dates = dates or ["2024-01-01"] * n_messages
        rows = []
        for i in range(n_messages):
            is_ad = i < n_ads
            is_protected = i < n_protected
            cats = ["Ads Promotions"] if is_ad else ["Finance"]
            rows.append((
                f"m{i}", "t1", dates[i % len(dates)], f"Bank <{sender_email}>",
                sender_email, "example.com", "example.com",
                f"Subject {i}", json.dumps(cats), json.dumps(["label:Finance"]),
                80 if is_ad else 30, 1 if is_protected else 0, 0, 0, 0, 0, 0, 0,
                "normal", "no", "", json.dumps({"category_confidence": {c: 70 for c in cats}}),
                "2024-01-01",
            ))
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    def test_build_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(build_sender_reputation(conn), {})
            conn.close()

    def test_build_no_state(self):
        self.assertEqual(build_sender_reputation(None), {})

    def test_build_aggregates_per_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn, "noreply@bank.com", n_messages=10, n_ads=0, n_protected=2)
            reps = build_sender_reputation(conn)
            self.assertIn("sender:noreply@bank.com", reps)
            r = reps["sender:noreply@bank.com"]
            self.assertEqual(r.total_messages, 10)
            self.assertEqual(r.protected_fraction, 0.2)
            self.assertEqual(r.ad_fraction, 0.0)
            conn.close()

    def test_build_aggregates_per_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn, "noreply@bank.com", n_messages=5)
            reps = build_sender_reputation(conn)
            # Domain row also exists.
            domain_keys = [k for k in reps if k.startswith("domain:")]
            self.assertGreater(len(domain_keys), 0)
            conn.close()

    def test_build_sender_with_ads(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn, "promo@spam.com", n_messages=5, n_ads=5)
            reps = build_sender_reputation(conn)
            r = reps["sender:promo@spam.com"]
            self.assertEqual(r.ad_fraction, 1.0)
            # High ad fraction = low score.
            self.assertLess(r.reputation_score, 30)
            conn.close()


class UpsertLoadSenderReputationTests(unittest.TestCase):
    def test_upsert_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            reputations = {
                "sender:a@b.com": SenderReputation(
                    sender_key="sender:a@b.com",
                    total_messages=100,
                    avg_ad_confidence=50.0,
                    protected_fraction=0.1,
                    ad_fraction=0.05,
                    reputation_score=80,
                ),
                "domain:b.com": SenderReputation(
                    sender_key="domain:b.com",
                    total_messages=10,
                    reputation_score=40,
                ),
            }
            upsert_sender_reputation(conn, reputations)
            loaded = load_sender_reputation_index(conn)
            self.assertEqual(set(loaded), {"sender:a@b.com", "domain:b.com"})
            self.assertEqual(loaded["sender:a@b.com"].total_messages, 100)
            self.assertEqual(loaded["domain:b.com"].reputation_score, 40)
            conn.close()

    def test_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            reputations = {"sender:x@x.com": SenderReputation(sender_key="sender:x@x.com", total_messages=5)}
            upsert_sender_reputation(conn, reputations)
            upsert_sender_reputation(conn, reputations)
            count = conn.execute("SELECT COUNT(*) FROM sender_reputation").fetchone()[0]
            self.assertEqual(count, 1)
            conn.close()

    def test_load_no_state(self):
        self.assertEqual(load_sender_reputation_index(None), {})

    def test_upsert_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            upsert_sender_reputation(conn, {})  # no-op
            self.assertEqual(load_sender_reputation_index(conn), {})
            conn.close()


class SuggestBlocklistTests(unittest.TestCase):
    def test_suggests_only_high_volume_low_reputation(self):
        reputations = {
            "sender:promo@spam.com": SenderReputation(
                sender_key="sender:promo@spam.com",
                total_messages=BLOCKLIST_SUGGESTION_MIN_MESSAGES,
                ad_fraction=BLOCKLIST_SUGGESTION_MIN_AD_FRACTION,
                protected_fraction=0.0,
            ),
            "sender:good@vip.com": SenderReputation(
                sender_key="sender:good@vip.com",
                total_messages=BLOCKLIST_SUGGESTION_MIN_MESSAGES,
                ad_fraction=0.0,
                protected_fraction=0.5,
            ),
            "sender:small@x.com": SenderReputation(
                sender_key="sender:small@x.com",
                total_messages=10,
                ad_fraction=1.0,
                protected_fraction=0.0,
            ),
        }
        candidates = suggest_blocklist(reputations)
        self.assertEqual(candidates, ["sender:promo@spam.com"])

    def test_skips_protected_senders(self):
        reputations = {
            "sender:promo@spam.com": SenderReputation(
                sender_key="sender:promo@spam.com",
                total_messages=500,
                ad_fraction=1.0,
                protected_fraction=0.01,  # any protected = skip
            ),
        }
        self.assertEqual(suggest_blocklist(reputations), [])

    def test_empty_input(self):
        self.assertEqual(suggest_blocklist({}), [])


class ReputationAdAdjustmentTests(unittest.TestCase):
    def test_high_reputation_gets_negative_adjustment(self):
        r = SenderReputation(sender_key="x", total_messages=100, ad_fraction=0.05)
        # A high-volume low-ad sender's score will be high.
        r.reputation_score = compute_reputation_score(r.total_messages, r.ad_fraction)
        self.assertGreaterEqual(r.reputation_score, REPUTATION_HIGH_THRESHOLD)
        self.assertEqual(reputation_ad_adjustment(r), -15)

    def test_low_reputation_gets_positive_adjustment(self):
        r = SenderReputation(sender_key="x", total_messages=1000, ad_fraction=0.95)
        # A high-volume high-ad sender's score will be low.
        r.reputation_score = compute_reputation_score(r.total_messages, r.ad_fraction)
        self.assertLessEqual(r.reputation_score, REPUTATION_LOW_THRESHOLD)
        self.assertEqual(reputation_ad_adjustment(r), 10)

    def test_medium_reputation_gets_zero_adjustment(self):
        r = SenderReputation(sender_key="x", total_messages=10, ad_fraction=0.5)
        r.reputation_score = compute_reputation_score(r.total_messages, r.ad_fraction)
        # Medium score is between thresholds.
        self.assertGreater(r.reputation_score, REPUTATION_LOW_THRESHOLD)
        self.assertLess(r.reputation_score, REPUTATION_HIGH_THRESHOLD)
        self.assertEqual(reputation_ad_adjustment(r), 0)

    def test_no_reputation_no_adjustment(self):
        self.assertEqual(reputation_ad_adjustment(None), 0)

    def test_zero_messages_no_adjustment(self):
        r = SenderReputation(sender_key="x", total_messages=0)
        self.assertEqual(reputation_ad_adjustment(r), 0)


if __name__ == "__main__":
    unittest.main()
