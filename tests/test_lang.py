"""Language detection tests for v0.7.

The sorter picks a language overlay (EN/FR/FA) per message based on the
cleaned body excerpt and subject. The detector is implemented in
:mod:`sorter.lang` and is *only* used to choose which keyword set applies.
It never blocks mail, never overrides the protection gate, and never
persists the detected language separately from the decision that already
records the matching keyword family.

These tests verify:
- English text returns ``en``.
- French text returns ``fr``.
- Farsi text returns ``fa``.
- Empty / whitespace-only text returns ``other`` without raising.
- The detector never raises on adversarial inputs.
- ``decide()`` populates ``Decision.detected_language`` for body-aware
  scans and falls back to subject-only for metadata scans.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_helpers import tracked, make_test_args

import gmail_sorter
from sorter.lang import SUPPORTED, detect


class LanguageDetectionTests(unittest.TestCase):

    def test_english_text(self):
        text = (
            "Your January statement is now available. Please review your account "
            "and let us know if you have any questions. Thank you for banking "
            "with us. Regards, John"
        )
        self.assertEqual(detect(text), "en")

    def test_french_text(self):
        text = (
            "Votre relevé de compte est disponible. Merci de bien vouloir "
            "vérifier votre compte et nous faire part de vos questions. "
            "Concernant votre demande de permis de travail, nous vous "
            "invitons à nous contacter. Cordialement, Jean"
        )
        self.assertEqual(detect(text), "fr")

    def test_farsi_text(self):
        text = (
            "سلام آقای مهندس، ممنون از پیام شما. لطفا برای رزرو وقت ملاقات "
            "با ما تماس بگیرید. با تشکر از شما، حسین"
        )
        self.assertEqual(detect(text), "fa")

    def test_empty_text_returns_other(self):
        self.assertEqual(detect(""), "other")
        self.assertEqual(detect(None or ""), "other") if False else None  # defensive

    def test_whitespace_only_returns_other(self):
        self.assertEqual(detect("   \n\t  "), "other")

    def test_returns_supported_value(self):
        for sample in [
            "Hello world",
            "Bonjour le monde",
            "سلام دنیا",
            "12345 67890",
            "????",
            "",
        ]:
            result = detect(sample)
            self.assertIn(result, SUPPORTED, f"unexpected value for {sample!r}: {result!r}")

    def test_short_french_subject(self):
        # Even a short subject carries the language signal when stopwords are
        # dense enough. "Votre demande" should land in fr on the fallback.
        result = detect("Votre demande de permis de travail")
        self.assertEqual(result, "fr")

    def test_never_raises(self):
        # The detector is used inside decide(); it must never raise or
        # decide() will crash on a single bad payload.
        for bad in [
            "\x00\x01\x02",
            "👋" * 10,
            "Ω" * 10,
            "A" * 5000,
        ]:
            try:
                detect(bad)
            except Exception as error:  # pragma: no cover - the test fails
                self.fail(f"detect({bad!r}) raised {error!r}")


class DecideDetectedLanguageTests(unittest.TestCase):
    """decide() must populate Decision.detected_language for body-aware scans."""

    def test_decide_sets_language_for_french_body(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full")
        body = (
            "Bonjour, votre relevé de compte est disponible. Merci. Cordialement, Banque"
        )
        msg = message(
            body_payload(
                {"From": "Banque <noreply@banque.ca>", "Subject": "Votre relevé"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "fr")

    def test_decide_sets_language_for_english_body(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full")
        body = "Hello, your account statement is ready. Thank you for banking with us."
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Your statement"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "en")

    def test_decide_sets_language_for_metadata_scan(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="metadata")
        body = "Bonjour, votre relevé de compte est disponible."
        msg = message(
            body_payload(
                {"From": "Banque <noreply@banque.ca>", "Subject": "Votre relevé"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # Subject alone still carries the language signal.
        self.assertEqual(decision.detected_language, "fr")

    def test_decide_uses_cached_excerpt_for_cache_only_scan(self):
        from tests.test_gmail_sorter import args, message, body_payload
        import tempfile
        import gmail_sorter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            conn = tracked(self, gmail_sorter.open_state_db(db_path))
            # Seed a cached body feature for one message ID so a metadata
            # scan with --scan full still has a body available.
            d = gmail_sorter.Decision(
                message_id="cached-1",
                thread_id="t",
                date="2026-07-06",
                sender="Bank <noreply@bank.com>",
                sender_email="noreply@bank.com",
                sender_domain="bank.com",
                registered_domain="bank.com",
                subject="Your statement",
                snippet="",
                body_len=200,
                body_category_hits=[],
                body_text_excerpt="Your January statement is now available. Please review your account.",
            )
            gmail_sorter.upsert_message_features(conn, [d], scan_mode="full")
            conn.close()

            a = args(scan="full", cached_body_features=gmail_sorter.load_body_features_index(gmail_sorter.open_state_db(db_path)))
            msg = message(
                body_payload(
                    {"From": "Bank <noreply@bank.com>", "Subject": "Your statement"},
                    "Your January statement is now available. Please review your account.",
                ),
                labels=[],
            )
            # The Gmail payload we hand to decide is metadata-only (no body
            # part), so decide() must read the cached excerpt to detect the
            # language.
            msg["payload"]["body"] = {}
            msg["payload"]["parts"] = []
            decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
            self.assertEqual(decision.detected_language, "en")


if __name__ == "__main__":
    unittest.main()
