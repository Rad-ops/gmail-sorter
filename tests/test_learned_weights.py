"""Tests for v0.8 per-keyword learned weights."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from tests.test_helpers import make_test_args
from sorter import learned_weights


class CategoryWeightsTests(unittest.TestCase):
    def test_default_weights(self):
        w = learned_weights.CategoryWeights()
        self.assertEqual(w.subject, 30.0)
        self.assertEqual(w.body, 20.0)
        self.assertEqual(w.sender, 15.0)
        self.assertEqual(w.confidence, 0)
        self.assertEqual(len(w.lr_weights), 6)

    def test_round_trip_serialization(self):
        w = learned_weights.CategoryWeights(
            subject=42.0, body=28.0, sender=18.0,
            lr_weights=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            lr_bias=0.7,
            confidence=50,
        )
        d = w.to_dict()
        w2 = learned_weights.CategoryWeights.from_dict(d)
        self.assertEqual(w.subject, w2.subject)
        self.assertEqual(w.body, w2.body)
        self.assertEqual(w.sender, w2.sender)
        self.assertEqual(w.lr_weights, w2.lr_weights)
        self.assertEqual(w.lr_bias, w2.lr_bias)
        self.assertEqual(w.confidence, w2.confidence)

    def test_save_load_round_trip(self):
        w = learned_weights.CategoryWeights(
            subject=42.0, lr_weights=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6], confidence=20,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            learned_weights.save_weights(path, {"Finance": w})
            loaded = learned_weights.load_weights(path)
            self.assertIn("Finance", loaded)
            self.assertEqual(loaded["Finance"].subject, 42.0)
            self.assertEqual(loaded["Finance"].confidence, 20)

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(learned_weights.load_weights(Path(tmp) / "missing.json"), {})

    def test_load_malformed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            path.write_text("not valid json", encoding="utf-8")
            self.assertEqual(learned_weights.load_weights(path), {})


class SigmoidTests(unittest.TestCase):
    def test_sigmoid_zero(self):
        self.assertAlmostEqual(learned_weights._sigmoid(0.0), 0.5)

    def test_sigmoid_large_positive(self):
        self.assertAlmostEqual(learned_weights._sigmoid(100.0), 1.0, places=4)

    def test_sigmoid_large_negative(self):
        self.assertAlmostEqual(learned_weights._sigmoid(-100.0), 0.0, places=4)

    def test_logit_round_trip(self):
        for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
            self.assertAlmostEqual(learned_weights._sigmoid(learned_weights._logit(p)), p, places=6)


class TrainWeightsTests(unittest.TestCase):
    def test_train_separates_classes(self):
        # A trivial training set: positive examples have subject_hits=3,
        # negative examples have subject_hits=0. The trained model
        # should give a higher score to the positive features.
        examples = []
        for _ in range(20):
            examples.append(([3.0, 0.0, 0.0, 0.0, 0.0, 0.0], 1))
            examples.append(([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 0))
        weights, bias = learned_weights.train_category_weights(examples, epochs=50)
        # The positive feature's weight should be positive.
        self.assertGreater(weights[0], 0.1)

    def test_train_empty(self):
        weights, bias = learned_weights.train_category_weights([])
        self.assertEqual(weights, [0.0] * 6)
        self.assertEqual(bias, 0.0)

    def test_train_single_class(self):
        # All-positive training set: the model should still train.
        examples = [([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], 1)] * 5
        weights, bias = learned_weights.train_category_weights(examples, epochs=3)
        # At least one weight should move.
        self.assertTrue(any(w != 0 for w in weights))


class ApplyLearnedScoreTests(unittest.TestCase):
    def test_undertrained_returns_zero(self):
        w = learned_weights.CategoryWeights(confidence=learned_weights.MIN_CONFIDENCE - 1)
        score = learned_weights.apply_learned_score(
            w, 1, 1, 1, False, False, 0.0,
        )
        self.assertEqual(score, 0)

    def test_trained_returns_in_range(self):
        w = learned_weights.CategoryWeights(
            lr_weights=[1.0, 1.0, 1.0, 0.5, 0.5, 0.1],
            lr_bias=-2.0,
            confidence=100,
        )
        score = learned_weights.apply_learned_score(
            w, 3, 2, 1, True, False, 5.0,
        )
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


class TrainFromDecisionsTests(unittest.TestCase):
    def _seed(self, conn):
        # Seed a few decisions with high confidence so training has data.
        rows = []
        for i, primary in enumerate(["Finance", "Receipts Orders", "Health", "Account Security"] * 5):
            decision = {"category_confidence": {primary: 90}, "ad_confidence": 70}
            rows.append((
                f"m{i}", f"t{i}", "2026-07-06", f"sender{i}@example.com",
                f"sender{i}@example.com", "example.com", "example.com",
                "Subject", json.dumps([primary]), json.dumps(["label:" + primary]),
                70, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", json.dumps(decision), "2026-07-06",
            ))
        # Schema has 23 columns.
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    def test_train_from_decisions_returns_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self._seed(conn)
            weights = learned_weights.train_from_decisions(conn)
            self.assertGreater(len(weights), 0)
            # Each weight is a CategoryWeights instance.
            for cat, w in weights.items():
                self.assertIsInstance(w, learned_weights.CategoryWeights)
            conn.close()

    def test_train_from_decisions_no_state(self):
        self.assertEqual(learned_weights.train_from_decisions(None), {})

    def test_load_or_train_learned_weights_no_state_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_test_args(
                use_learned_weights=True,
                learned_weights_file=str(Path(tmp) / "missing.json"),
            )
            self.assertEqual(gmail_sorter.load_or_train_learned_weights(args, None), {})

    def test_train_from_decisions_undertrained(self):
        # Less than MIN_CONFIDENCE examples per category -> still
        # return CategoryWeights but with confidence below MIN_CONFIDENCE
        # so the apply function returns 0.
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            # Only 2 examples per category.
            for i in range(2):
                decision = {"category_confidence": {"Finance": 90}, "ad_confidence": 70}
                conn.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"m{i}", "t", "2026-07-06", "x@x.com", "x@x.com", "x", "x",
                        "S", '["Finance"]', '["label:Finance"]',
                        70, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", json.dumps(decision), "2026-07-06",
                    ),
                )
            conn.commit()
            weights = learned_weights.train_from_decisions(conn)
            self.assertIn("Finance", weights)
            self.assertLess(weights["Finance"].confidence, learned_weights.MIN_CONFIDENCE)
            conn.close()


class EndToEndLearnedWeightsTests(unittest.TestCase):
    def test_decide_uses_learned_weights_when_enabled(self):
        from tests.test_gmail_sorter import args, message, body_payload

        # Pre-populate the learned weights so decide() will use them.
        # A high-bias model that pushes Finance up.
        w = learned_weights.CategoryWeights(
            lr_weights=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            lr_bias=2.5,
            confidence=100,
        )
        a = args(
            scan="metadata",
            use_learned_weights=True,
            _learned_weights={"Finance": w},
        )
        # Subject and body with a finance keyword so the keyword
        # rules actually fire and put Finance in category_confidence.
        msg = message(
            body_payload({"From": "Bank <noreply@bank.com>", "Subject": "Your statement is ready"}, ""),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # The decision must include a learned_boost reason.
        self.assertTrue(
            any(r.startswith("learned_boost:Finance") for r in decision.reasons),
            f"no learned_boost reason: {decision.reasons}",
        )


if __name__ == "__main__":
    unittest.main()
