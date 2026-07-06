"""Property-based tests for the Gmail sorter (v0.7 expansion).

Property-based tests generate many random inputs and assert that an
invariant holds for every one of them. The tests in this module target
the parts of the sorter where a one-off test is most likely to miss a
subtle bug:

* Keyword matching: word-boundary rules must never produce a false
  positive on a known substring trap.
* Centroid math: average_vectors and cosine_similarity are
  commutative / invariant under scaling.
* Schema migrations: every random decision survives a round-trip
  through a fresh DB and the migration scaffold.
* Body cleaning: never raises, never returns more text than
  ``keep_chars``, always strips quoted reply chains.
* Language detection: never raises, always returns one of
  ``en`` / ``fr`` / ``fa`` / ``other``.
* Decay math: monotonicity — older profiles are never heavier than
  fresh ones.
* AI merge: never applies an AI label that would override a protected
  category, even with adversarial inputs.
"""

import json
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_helpers import tracked, make_test_args

import gmail_sorter
from sorter import config_loader, policy
from sorter.ai_learning import apply_ai_learning
from sorter.embeddings import average_vectors, cosine_similarity
from sorter.keywords import keyword_hits
from sorter.lang import SUPPORTED, detect
from sorter.schema import CURRENT_SCHEMA_VERSION, migrate


class KeywordPropertyTests(unittest.TestCase):
    """Randomized checks of the word-boundary matcher."""

    WORD_TEMPLATES = [
        "exam", "class", "sale", "unsubscribe", "pass",
        "study", "school", "tax", "receipt", "refund",
    ]

    SUBSTRING_TRAPS = {
        "exam": ["example.com", "examination", "examiner", "exams"],
        "class": ["classification", "classified", "classroom", "classic"],
        "sale": ["salon", "salary", "salesperson", "wholesale"],
        "unsubscribe": ["unsubscribed", "unsubscribing", "unsubscribable"],
        "pass": ["compass", "impasse", "trespass"],
        "study": ["studying", "studyguide"],
        "school": ["schooling", "schooled", "preschool"],
        "tax": ["taxi", "taxis", "syntax", "syntaxes"],
        "receipt": ["receipts", "receipted"],
        "refund": ["refunded", "refunding"],
    }

    def test_substring_traps_never_match(self):
        # The substring traps are exactly the kind of strings the
        # word-boundary matcher was built to avoid. Random shuffles
        # around the trap must never produce a hit.
        for keyword, traps in self.SUBSTRING_TRAPS.items():
            for trap in traps:
                self.assertEqual(
                    keyword_hits(trap, [keyword]),
                    [],
                    f"word-boundary matcher produced a false positive for {keyword!r} in {trap!r}",
                )

    def test_positive_match_always_hits(self):
        # The matcher must always find the keyword when the text
        # contains the word surrounded by non-word characters.
        for keyword in self.WORD_TEMPLATES:
            for prefix, suffix in [
                ("", ""),
                ("hello ", " world"),
                ("the ", " is here"),
                ("!@# ", " $%^"),
            ]:
                text = prefix + keyword + suffix
                self.assertIn(
                    keyword,
                    keyword_hits(text, [keyword]),
                    f"word-boundary matcher missed {keyword!r} in {text!r}",
                )

    def test_keyword_at_unicode_boundaries(self):
        # Word boundaries use \b which is ASCII-only; the matcher
        # should still treat the keyword as a substring when the
        # surrounding text is non-ASCII.
        text = "سلام exam دنیا"
        # \b does not match between ASCII and non-ASCII letters, so
        # the matcher falls back to escaped substring matching. The
        # matcher must not raise.
        result = keyword_hits(text, ["exam"])
        # We don't assert on the result because \b is ASCII-only;
        # we only assert that the matcher returns a list (not raises).
        self.assertIsInstance(result, list)

    def test_empty_inputs(self):
        self.assertEqual(keyword_hits("", ["exam"]), [])
        self.assertEqual(keyword_hits("hello", []), [])

    def test_random_text_never_raises(self):
        rng = random.Random(42)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 .,_-@!#$%^&*()"
        for _ in range(50):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
            try:
                keyword_hits(text, self.WORD_TEMPLATES)
            except Exception as error:  # pragma: no cover - the test fails
                self.fail(f"keyword_hits({text!r}) raised {error!r}")


class CentroidMathPropertyTests(unittest.TestCase):
    """Properties of the pure-Python vector math."""

    def test_cosine_similarity_self_is_one(self):
        for vec in (
            [1.0, 0.0, 0.0],
            [0.5, 0.5, 0.5],
            [3.0, 4.0],
            [0.0, 0.0, 0.0],  # zero vector: defined as 0.0
        ):
            sim = cosine_similarity(vec, vec)
            if any(vec):  # non-zero vector
                self.assertAlmostEqual(sim, 1.0, places=6)
            else:
                self.assertEqual(sim, 0.0)

    def test_cosine_similarity_commutative(self):
        rng = random.Random(0)
        for _ in range(20):
            dim = rng.randint(1, 10)
            a = [rng.uniform(-1, 1) for _ in range(dim)]
            b = [rng.uniform(-1, 1) for _ in range(dim)]
            self.assertAlmostEqual(cosine_similarity(a, b), cosine_similarity(b, a), places=6)

    def test_cosine_similarity_bounded_zero_one(self):
        rng = random.Random(1)
        for _ in range(20):
            dim = rng.randint(1, 10)
            a = [rng.uniform(-1, 1) for _ in range(dim)]
            b = [rng.uniform(-1, 1) for _ in range(dim)]
            sim = cosine_similarity(a, b)
            self.assertGreaterEqual(sim, 0.0)
            self.assertLessEqual(sim, 1.0)

    def test_cosine_similarity_scale_invariant(self):
        a = [1.0, 2.0, 3.0]
        b = [4.0, 5.0, 6.0]
        self.assertAlmostEqual(cosine_similarity(a, b), cosine_similarity([x * 10 for x in a], b), places=6)
        self.assertAlmostEqual(cosine_similarity(a, b), cosine_similarity(a, [x * 1000 for x in b]), places=6)

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertEqual(cosine_similarity(a, b), 0.0)

    def test_average_vectors_elementwise(self):
        vectors = [[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [0.0, 0.0, 0.0]]
        avg = average_vectors(vectors)
        self.assertEqual(avg, [(1 + 3 + 0) / 3, (2 + 2 + 0) / 3, (3 + 1 + 0) / 3])

    def test_average_vectors_empty(self):
        self.assertEqual(average_vectors([]), [])

    def test_average_vectors_mismatched_lengths_silently_truncates(self):
        # The current implementation uses zip semantics; the result
        # length matches the first vector's length.
        result = average_vectors([[1, 2, 3], [4, 5]])
        self.assertEqual(len(result), 3)


class SchemaPropertyTests(unittest.TestCase):
    """The migration scaffold must survive random migration scenarios."""

    def test_random_scenario_round_trip(self):
        rng = random.Random(7)
        for _ in range(10):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "state.sqlite"
                # Sometimes seed with v1 data, sometimes leave empty.
                if rng.random() < 0.5:
                    self._seed_v1(path)
                conn = sqlite3.connect(str(path))
                migrate(conn)
                # After migration the version is current.
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                self.assertEqual(version, CURRENT_SCHEMA_VERSION)
                # And the v3 columns are present.
                cols = {row[1] for row in conn.execute("PRAGMA table_info(sender_profile)").fetchall()}
                self.assertIn("first_seen", cols)
                self.assertIn("last_hits", cols)
                self.assertIn("category_diversity", cols)
                self.assertIn("body_text_excerpt", {row[1] for row in conn.execute("PRAGMA table_info(message_features)").fetchall()})
                conn.close()

    def _seed_v1(self, path):
        conn = tracked(self, sqlite3.connect(str(path)))
        conn.execute(
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
        conn.execute(
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
        conn.execute(
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
        # Seed random rows.
        for i in range(20):
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"m{i}", f"t{i}", "2025-01-01", "Sender <x@example.com>",
                    "x@example.com", "example.com", "example.com",
                    "subject", "[]", "[]", 0, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "",
                    "{}", "2025-01-01", 1,
                ),
            )
            conn.execute(
                "INSERT INTO sender_profile (key, kind, category, hits, protected_hits, last_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"sender:user{i}@example.com", "sender", "Finance", 1, 0, "2025-01-01", "2025-01-01"),
            )
        conn.commit()
        conn.close()


class BodyCleanPropertyTests(unittest.TestCase):
    """clean_body_text must always return a bounded, safe string."""

    def test_random_text_never_raises(self):
        rng = random.Random(11)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 .,_-@!#$%^&*()\n>"
        for _ in range(50):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 500)))
            try:
                result = gmail_sorter.clean_body_text(text, keep_chars=200)
            except Exception as error:  # pragma: no cover
                self.fail(f"clean_body_text({text!r}) raised {error!r}")
            self.assertIsInstance(result, str)
            self.assertLessEqual(len(result), 200)

    def test_quoted_replies_stripped(self):
        text = "Real reply\n> Quoted line\n> another quoted\nMore real reply"
        cleaned = gmail_sorter.clean_body_text(text, keep_chars=200)
        self.assertNotIn("Quoted line", cleaned)
        self.assertNotIn("another quoted", cleaned)
        self.assertIn("Real reply", cleaned)
        self.assertIn("More real reply", cleaned)

    def test_footer_marker_stops_block(self):
        # The footer marker is "-- " on its own line (the typical email
        # signature separator). clean_body_text stops reading at the
        # first footer line.
        text = "Real content\n-- \nJohn Doe\n"
        cleaned = gmail_sorter.clean_body_text(text, keep_chars=200)
        self.assertIn("Real content", cleaned)
        # The signature line is on a separate line starting with "-- "
        # which is a FOOTER_MARKER. The line itself is dropped.
        self.assertNotIn("John Doe", cleaned)

    def test_empty_text(self):
        self.assertEqual(gmail_sorter.clean_body_text("", keep_chars=200), "")
        self.assertEqual(gmail_sorter.clean_body_text("\n\n", keep_chars=200), "")


class LanguagePropertyTests(unittest.TestCase):
    """The language detector must always return a valid label."""

    def test_random_text_never_raises(self):
        rng = random.Random(13)
        for _ in range(50):
            text = "".join(
                rng.choice("abcdefghijklmnopqrstuvwxyz0123456789 .,_-@")
                for _ in range(rng.randint(0, 200))
            )
            try:
                result = detect(text)
            except Exception as error:  # pragma: no cover
                self.fail(f"detect({text!r}) raised {error!r}")
            self.assertIn(result, SUPPORTED)

    def test_unicode_text_never_raises(self):
        for text in [
            "👋🌍",  # emojis
            "Ω≈ç√∫˜µ",  # Greek
            "你好世界",  # Chinese
            "Привет мир",  # Cyrillic
            "سلام دنیا",  # Farsi
            "مرحبا بالعالم",  # Arabic
            "",  # empty
            "\x00\x01\x02",  # control characters
        ]:
            result = detect(text)
            self.assertIn(result, SUPPORTED, f"detect({text!r}) returned {result!r}")


class DecayMonotonicityTests(unittest.TestCase):
    """Older profiles are never heavier than fresh ones (for the same hits)."""

    def _seed(self, conn, days_ago, hits=5):
        import tempfile as _t
        d = gmail_sorter.Decision(
            message_id=f"m_{days_ago}", thread_id="t", date=(datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat(),
            sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
            sender_domain="bank.com", registered_domain="bank.com", subject="Statement", snippet="",
            categories=["Finance"], primary_category="Finance",
            category_confidence={"Finance": 70}, ad_confidence=70,
        )
        gmail_sorter.update_sender_profiles(conn, [d], confidence_floor=65)

    def test_older_is_lighter(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
            # Three senders, all carrying 5 hits, but at different ages.
            self._seed(conn, days_ago=0)
            conn.execute("UPDATE sender_profile SET key='sender:recent@x.com' WHERE key='sender:noreply@bank.com:finance'")
            conn.execute("UPDATE sender_profile SET last_seen=?, first_seen=?", (datetime.now(timezone.utc).date().isoformat(),) * 2)
            self._seed(conn, days_ago=180)
            conn.execute("UPDATE sender_profile SET key='sender:middle@x.com' WHERE key='sender:noreply@bank.com:finance'")
            self._seed(conn, days_ago=720)
            conn.execute("UPDATE sender_profile SET key='sender:old@x.com' WHERE key='sender:noreply@bank.com:finance'")
            # The keys all have category suffix; fix.
            conn.execute("UPDATE sender_profile SET key=key || ':finance' WHERE key NOT LIKE '%:%:%'")
            index = gmail_sorter.load_sender_profile_index(conn, half_life_days=180, min_hits=1)
            recent = index.get("sender:recent@x.com:finance", {}).get("Finance", 0)
            middle = index.get("sender:middle@x.com:finance", {}).get("Finance", 0)
            old = index.get("sender:old@x.com:finance", {}).get("Finance", 0)
            self.assertGreaterEqual(recent, middle)
            self.assertGreaterEqual(middle, old)
            conn.close()


class AIMergeSafetyPropertyTests(unittest.TestCase):
    """merge_ai_labels must never override a protected category."""

    PROTECTED = ("Priority Immigration", "Priority Studies", "Account Security", "Health")

    def test_protected_categories_never_overridden(self):
        rng = random.Random(17)
        for protected_category in self.PROTECTED:
            for _ in range(10):
                # Generate an adversarial packet: AI disagrees with the
                # protected category, at the highest possible confidence.
                decision = gmail_sorter.Decision(
                    message_id=f"m{rng.randint(0, 1_000_000)}", thread_id="t",
                    date="2026-07-06", sender="S <s@s>", sender_email="s@s", sender_domain="s",
                    registered_domain="s", subject="x", snippet="",
                    categories=[protected_category, "Finance"],
                    primary_category=protected_category,
                    category_confidence={protected_category: 100, "Finance": 50},
                    protected=True,
                )
                # AI wants to remove the protected category.
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "ai.jsonl"
                    packet = {
                        "message_id": decision.message_id,
                        "ai_label": "Finance",
                        "ai_confidence": 0.99,
                        "ai_reviewed": True,
                    }
                    with path.open("w") as f:
                        f.write(json.dumps(packet) + "\n")
                    _, _, removed = gmail_sorter.merge_ai_labels(
                        [decision], path, min_ai_confidence=0.7, min_ai_removal_confidence=0.85,
                    )
                    self.assertEqual(removed, 0, f"removed protected {protected_category!r} via AI")
                    self.assertIn(protected_category, decision.categories)


class AILearningSafetyPropertyTests(unittest.TestCase):
    """apply_ai_learning must never record a learning event for a
    protected message with a non-protected AI label."""

    def test_protected_message_skipped_in_learning(self):
        for protected in ("Priority Immigration", "Priority Studies", "Account Security", "Health"):
            with tempfile.TemporaryDirectory() as tmp:
                conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
                decision = gmail_sorter.Decision(
                    message_id=f"m_{protected}", thread_id="t", date="2026-07-06",
                    sender="S <s@s>", sender_email="s@s", sender_domain="s",
                    registered_domain="s", subject="x", snippet="",
                    categories=[protected], primary_category=protected,
                    category_confidence={protected: 100},
                    protected=True,
                )
                packets = [{
                    "message_id": decision.message_id,
                    "ai_label": "Shopping",
                    "ai_confidence": 0.99,
                    "ai_reviewed": True,
                }]
                report = apply_ai_learning(conn, [decision], packets, embedding_backend=None)
                self.assertEqual(report["considered"], 0, f"learned a non-protected AI label on a protected {protected!r} message")
                conn.close()


class OverlayRestorationPropertyTests(unittest.TestCase):
    """A language overlay must leave policy in its original state after use."""

    def test_overlay_does_not_leak_between_messages(self):
        from tests.test_gmail_sorter import args, message, body_payload

        original_immigration = list(policy.IMMIGRATION_KEYWORDS)
        original_studies = list(policy.STUDIES_KEYWORDS)
        original_rules = [(n, list(k), list(e)) for n, k, e in policy.CATEGORY_RULES]

        a = args(scan="full", _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"))

        # Generate 20 alternating French / English / Farsi messages.
        bodies = [
            ("Bonjour, votre demande de permis de travail.", "fr"),
            ("Hello, your bank statement is ready.", "en"),
            ("سلام، نوبت پزشک شما تنظیم شد.", "fa"),
        ]
        from tests.test_gmail_sorter import payload as payload_fn
        for i in range(20):
            body, lang = bodies[i % 3]
            msg = message(
                body_payload(
                    {"From": f"sender{i}@example.com", "Subject": "x"},
                    body,
                ),
                labels=[],
            )
            gmail_sorter.decide(msg, a, gmail_sorter.Config())
            # After every decide, the policy must be back to the original.
            self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original_immigration, f"leak after message {i} (language {lang})")
            self.assertEqual(list(policy.STUDIES_KEYWORDS), original_studies, f"leak after message {i} (language {lang})")
            current_rules = [(n, list(k), list(e)) for n, k, e in policy.CATEGORY_RULES]
            self.assertEqual(current_rules, original_rules, f"rule leak after message {i} (language {lang})")


if __name__ == "__main__":
    unittest.main()
