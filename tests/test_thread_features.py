"""Tests for v0.8 thread-level conversation modeling."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter import thread_features
from sorter.thread_features import (
    ThreadFeature,
    build_thread_features,
    compute_thread_boost,
    load_thread_features_index,
    upsert_thread_features,
    THREAD_BOOST_CAP,
)


class ThreadFeatureTests(unittest.TestCase):
    def test_feature_to_features(self):
        f = ThreadFeature(
            thread_id="t1",
            message_count=10,
            distinct_senders=3,
            top_category="Finance",
            top_category_share=0.8,
            has_attachment_count=5,
            has_unsubscribe_count=2,
            date_span_days=30,
            protected_fraction=0.5,
        )
        features = f.to_features()
        self.assertEqual(len(features), 8)
        self.assertEqual(features[0], 10.0)  # message_count
        self.assertEqual(features[1], 3.0)  # distinct_senders
        self.assertEqual(features[2], 0.8)  # top_category_share
        self.assertEqual(features[3], 0.5)  # has_attachment_count / message_count
        self.assertEqual(features[4], 0.2)  # has_unsubscribe_count / message_count
        self.assertEqual(features[5], 30.0)  # date_span_days
        self.assertEqual(features[6], 0.5)  # protected_fraction
        self.assertEqual(features[7], 0.07)  # len("Finance") / 100

    def test_feature_default_to_features(self):
        f = ThreadFeature(thread_id="t1")
        features = f.to_features()
        # All zeros.
        self.assertEqual(features, [0.0] * 8)


class BuildThreadFeaturesTests(unittest.TestCase):
    def _seed(self, conn, n_finance=3, n_receipts=2, dates=None):
        dates = dates or ["2024-01-01", "2024-01-15", "2024-02-01", "2024-02-15", "2024-03-01"]
        rows = []
        idx = 0
        for i in range(n_finance):
            rows.append((
                f"m{idx}", "t1", dates[idx % len(dates)], f"bank{i}@bank.com",
                f"bank{i}@bank.com", "bank.com", "bank.com",
                f"Statement {i}", '["Finance"]', '["label:Finance"]',
                70, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", json.dumps({
                    "category_confidence": {"Finance": 80},
                    "primary_category": "Finance",
                }), "2024-03-01",
            ))
            idx += 1
        for i in range(n_receipts):
            rows.append((
                f"m{idx}", "t1", dates[idx % len(dates)], f"shop{i}@shop.com",
                f"shop{i}@shop.com", "shop.com", "shop.com",
                f"Receipt {i}", '["Receipts Orders"]', '["label:Receipts Orders"]',
                70, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", json.dumps({
                    "category_confidence": {"Receipts Orders": 80},
                    "primary_category": "Receipts Orders",
                }), "2024-03-01",
            ))
            idx += 1
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    def test_build_aggregates_per_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn, n_finance=3, n_receipts=2)
            features = build_thread_features(conn)
            self.assertEqual(len(features), 1)
            self.assertIn("t1", features)
            f = features["t1"]
            self.assertEqual(f.message_count, 5)
            self.assertEqual(f.distinct_senders, 5)  # all different
            self.assertIn(f.top_category, ("Finance", "Receipts Orders"))
            self.assertEqual(f.has_attachment_count, 0)
            conn.close()

    def test_build_excludes_single_message_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn, n_finance=1, n_receipts=0)
            # min_messages=2 by default
            features = build_thread_features(conn)
            self.assertEqual(features, {})
            conn.close()

    def test_build_ignores_catchall_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            # Seed a thread that's mostly catch-all categories.
            rows = []
            for i in range(3):
                rows.append((
                    f"m{i}", "t1", "2024-01-01", "x@x.com", "x@x.com", "x", "x",
                    "S", '["Review"]', '[]', 70, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", json.dumps({
                        "category_confidence": {"Review": 50},
                    }), "2024-01-01",
                ))
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            features = build_thread_features(conn)
            # A thread whose only category is Review (catch-all) is
            # skipped — the top_category would be Review, which is a
            # NON_LABEL_CATEGORY.
            self.assertEqual(features, {})
            conn.close()

    def test_build_no_state(self):
        self.assertEqual(build_thread_features(None), {})


class UpsertLoadThreadFeaturesTests(unittest.TestCase):
    def test_upsert_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            features = {
                "t1": ThreadFeature(
                    thread_id="t1", message_count=10, distinct_senders=3,
                    top_category="Finance", top_category_share=0.7,
                    has_attachment_count=5, has_unsubscribe_count=2,
                    date_span_days=30, protected_fraction=0.5,
                ),
                "t2": ThreadFeature(
                    thread_id="t2", message_count=5, distinct_senders=2,
                    top_category="Health", top_category_share=0.6,
                ),
            }
            upsert_thread_features(conn, features)
            loaded = load_thread_features_index(conn)
            self.assertEqual(set(loaded), {"t1", "t2"})
            self.assertEqual(loaded["t1"].message_count, 10)
            self.assertEqual(loaded["t1"].top_category, "Finance")
            self.assertEqual(loaded["t2"].message_count, 5)
            conn.close()

    def test_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            features = {
                "t1": ThreadFeature(
                    thread_id="t1", message_count=10, top_category="Finance", top_category_share=0.7,
                ),
            }
            upsert_thread_features(conn, features)
            upsert_thread_features(conn, features)
            count = conn.execute("SELECT COUNT(*) FROM thread_features").fetchone()[0]
            self.assertEqual(count, 1)
            conn.close()

    def test_upsert_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            upsert_thread_features(conn, {})  # no-op
            self.assertEqual(load_thread_features_index(conn), {})
            conn.close()

    def test_load_no_state(self):
        self.assertEqual(load_thread_features_index(None), {})


class ComputeThreadBoostTests(unittest.TestCase):
    def test_zero_for_short_thread(self):
        f = ThreadFeature(thread_id="t1", message_count=1, top_category="Finance", top_category_share=1.0)
        self.assertEqual(compute_thread_boost(f, "Finance"), 0)

    def test_zero_for_wrong_category(self):
        f = ThreadFeature(thread_id="t1", message_count=10, top_category="Finance", top_category_share=0.8)
        self.assertEqual(compute_thread_boost(f, "Health"), 0)

    def test_positive_boost_for_top_category(self):
        f = ThreadFeature(thread_id="t1", message_count=10, top_category="Finance", top_category_share=0.8)
        boost = compute_thread_boost(f, "Finance")
        self.assertGreater(boost, 0)
        self.assertLessEqual(boost, THREAD_BOOST_CAP)

    def test_boost_capped(self):
        # A huge thread with a perfect category share must still be capped.
        f = ThreadFeature(thread_id="t1", message_count=1000, top_category="Finance", top_category_share=1.0)
        boost = compute_thread_boost(f, "Finance")
        self.assertEqual(boost, THREAD_BOOST_CAP)

    def test_boost_scales_with_share(self):
        # A thread with a higher category share gets a larger boost.
        f1 = ThreadFeature(thread_id="t1", message_count=10, top_category="Finance", top_category_share=0.5)
        f2 = ThreadFeature(thread_id="t2", message_count=10, top_category="Finance", top_category_share=0.9)
        self.assertGreater(compute_thread_boost(f2, "Finance"), compute_thread_boost(f1, "Finance"))


class EndToEndThreadModelingTests(unittest.TestCase):
    def test_decide_applies_thread_boost(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(
            scan="metadata",
            use_thread_modeling=True,
            thread_features={
                "t1": ThreadFeature(
                    thread_id="t1", message_count=10, top_category="Finance",
                    top_category_share=0.8,
                ),
            },
        )
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "",
            ),
            labels=[],
        )
        msg["threadId"] = "t1"
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # The thread_model_boost reason must be present.
        self.assertTrue(
            any(r.startswith("thread_model_boost:Finance:+") for r in decision.reasons),
            f"no thread_model_boost reason: {decision.reasons}",
        )

    def test_decide_skips_boost_for_short_thread(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(
            scan="metadata",
            use_thread_modeling=True,
            thread_features={
                "t1": ThreadFeature(
                    thread_id="t1", message_count=1, top_category="Finance",
                    top_category_share=1.0,
                ),
            },
        )
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "",
            ),
            labels=[],
        )
        msg["threadId"] = "t1"
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # No thread_model_boost reason because the thread is too short.
        self.assertFalse(
            any(r.startswith("thread_model_boost:") for r in decision.reasons),
        )

    def test_decide_thread_modeling_disabled(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(
            scan="metadata",
            use_thread_modeling=False,
            thread_features={
                "t1": ThreadFeature(
                    thread_id="t1", message_count=10, top_category="Finance",
                    top_category_share=0.8,
                ),
            },
        )
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "",
            ),
            labels=[],
        )
        msg["threadId"] = "t1"
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertFalse(
            any(r.startswith("thread_model_boost:") for r in decision.reasons),
        )


if __name__ == "__main__":
    unittest.main()
