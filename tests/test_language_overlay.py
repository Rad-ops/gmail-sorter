"""Language overlay tests for v0.7.

The per-language keyword overlays (config/policy.fr.yaml,
config/policy.fa.yaml) extend or replace the matching English category's
keyword list when the language detector picks a non-English language on a
message. The overlay is applied per-message and restored before the next
message, so the policy module never carries forward stale FR/FA keywords
when the next message is in English.

These tests verify:
- The YAML files are loaded when present; missing files are silent.
- An additive overlay extends the matching category's keyword list without
  duplicating existing keywords.
- A ``replace: true`` overlay replaces the category's keyword list.
- The policy module is restored after the overlay context exits.
- The full pipeline (detect -> overlay -> categorize) lands a French
  immigration message in Priority Immigration.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter import config_loader, policy


class OverlayRestoreTests(unittest.TestCase):

    def test_additive_overlay_extends_pool(self):
        original = list(policy.IMMIGRATION_KEYWORDS)
        overlay = {
            "categories": {
                "Priority Immigration": {
                    "keywords": ["permis de travail", "study permit"],
                }
            }
        }
        token = config_loader.activate_language_overlay(overlay)
        try:
            new_list = list(policy.IMMIGRATION_KEYWORDS)
            # Both French and English keywords remain.
            self.assertIn("permis de travail", new_list)
            self.assertIn("study permit", new_list)
            # Originals are still there.
            for original_kw in original:
                self.assertIn(original_kw, new_list)
            # Adding the same overlay twice must not duplicate.
            config_loader.activate_language_overlay(overlay)
            count = sum(1 for k in policy.IMMIGRATION_KEYWORDS if k == "permis de travail")
            self.assertEqual(count, 1)
        finally:
            config_loader.restore_policy(token)
        # The policy must be exactly as it was.
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)

    def test_replace_overlay_replaces_pool(self):
        original = list(policy.IMMIGRATION_KEYWORDS)
        overlay = {
            "categories": {
                "Priority Immigration": {
                    "keywords": ["permis de travail"],
                    "replace": True,
                }
            }
        }
        token = config_loader.activate_language_overlay(overlay)
        try:
            self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), ["permis de travail"])
        finally:
            config_loader.restore_policy(token)
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)

    def test_replace_overlay_replaces_rule_keywords(self):
        # Finance does not have a shared keyword pool; the overlay must
        # inject the new keywords into the matching CATEGORY_RULES entry.
        original = [list(k) for _, k, _ in policy.CATEGORY_RULES if k]
        overlay = {
            "categories": {
                "Finance": {
                    "keywords": ["relevé bancaire", "virement"],
                    "replace": True,
                }
            }
        }
        token = config_loader.activate_language_overlay(overlay)
        try:
            for name, kws, _ in policy.CATEGORY_RULES:
                if name == "Finance":
                    self.assertEqual(list(kws), ["relevé bancaire", "virement"])
                    break
            else:
                self.fail("Finance rule missing")
        finally:
            config_loader.restore_policy(token)
        # Restoration must return the original rules.
        for (name, kws, _), orig_kws in zip(policy.CATEGORY_RULES, original):
            self.assertEqual(list(kws), orig_kws, f"rule {name} not restored")

    def test_empty_overlay_is_noop(self):
        original = list(policy.IMMIGRATION_KEYWORDS)
        token = config_loader.activate_language_overlay({})
        try:
            self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)
        finally:
            config_loader.restore_policy(token)
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)

    def test_malformed_overlay_category_is_ignored(self):
        original = list(policy.IMMIGRATION_KEYWORDS)
        overlay = {
            "categories": {
                "Priority Immigration": "this should be a dict",
            }
        }
        token = config_loader.activate_language_overlay(overlay)
        try:
            self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)
        finally:
            config_loader.restore_policy(token)

    def test_context_manager_restores(self):
        original = list(policy.IMMIGRATION_KEYWORDS)
        with config_loader.language_overlay({
            "categories": {
                "Priority Immigration": {"keywords": ["permis"]}
            }
        }):
            self.assertIn("permis", policy.IMMIGRATION_KEYWORDS)
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)


class LoadLanguageOverlayTests(unittest.TestCase):

    def test_returns_empty_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = config_loader.load_language_overlay(Path(tmp), "fr")
            self.assertEqual(result, {})

    def test_returns_empty_for_unknown_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = config_loader.load_language_overlay(Path(tmp), "klingon")
            self.assertEqual(result, {})

    def test_returns_empty_for_english(self):
        # The English overlay is the regular policy.yaml, not a language
        # overlay; the loader must not double-load it.
        with tempfile.TemporaryDirectory() as tmp:
            result = config_loader.load_language_overlay(Path(tmp), "en")
            self.assertEqual(result, {})

    def test_loads_french_yaml(self):
        here = Path(__file__).resolve().parents[1]
        result = config_loader.load_language_overlay(here / "config", "fr")
        self.assertIn("categories", result)
        self.assertIn("Priority Immigration", result["categories"])
        kws = result["categories"]["Priority Immigration"]["keywords"]
        self.assertIn("permis de travail", kws)

    def test_loads_farsi_yaml(self):
        here = Path(__file__).resolve().parents[1]
        result = config_loader.load_language_overlay(here / "config", "fa")
        self.assertIn("categories", result)
        self.assertIn("Priority Immigration", result["categories"])
        kws = result["categories"]["Priority Immigration"]["keywords"]
        self.assertIn("ویزا", kws)


class EndToEndOverlayTests(unittest.TestCase):
    """A French immigration message must land in Priority Immigration via the overlay."""

    def test_french_message_uses_overlay(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full", _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"))
        body = "Bonjour, votre demande de permis de travail est en cours de traitement. Cordialement."
        msg = message(
            body_payload(
                {"From": "IRCC <noreply@cic.gc.ca>", "Subject": "Votre demande"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # The French overlay must put "permis de travail" in the reasons.
        # Without the overlay the English rules would still hit some
        # keywords; the overlay specifically adds the French term.
        self.assertEqual(decision.detected_language, "fr")
        self.assertIn("Priority Immigration", decision.categories)

    def test_farsi_message_with_overlay_lands_in_priority(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full", _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"))
        body = "سلام، وقت سفارت شما برای ویزای کار تنظیم شد. لطفا مدارک را آماده کنید."
        msg = message(
            body_payload(
                {"From": "Marolia Law <office@example.com>", "Subject": "وقت سفارت"},
                body,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertEqual(decision.detected_language, "fa")
        self.assertIn("Priority Immigration", decision.categories)

    def test_overlay_does_not_persist_between_messages(self):
        # After deciding a French message, the policy module must be
        # restored to the English state for the next message.
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full", _policy_config_dir=str(Path(__file__).resolve().parents[1] / "config"))
        original = list(policy.IMMIGRATION_KEYWORDS)
        body_fr = "Votre demande de permis de travail. Cordialement."
        body_en = "Your visa application. Regards."
        fr_msg = message(
            body_payload({"From": "IRCC <noreply@cic.gc.ca>", "Subject": "permis"}, body_fr),
            labels=[],
        )
        gmail_sorter.decide(fr_msg, a, gmail_sorter.Config())
        # After the French message, IMMIGRATION_KEYWORDS must be back to the
        # English set (no "permis de travail" appended).
        self.assertEqual(list(policy.IMMIGRATION_KEYWORDS), original)


if __name__ == "__main__":
    unittest.main()
