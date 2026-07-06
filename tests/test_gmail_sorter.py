import argparse
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter


def args(**overrides):
    """Build a minimal argparse-like object for policy tests."""

    defaults = {
        "ad_threshold": 65,
        "archive_threshold": 65,
        "archive_min_age_days": 0,
        "archive_skip_unread": False,
        "trash_threshold": 90,
        "pre_2020_trash_threshold": 75,
        "stage": "classify",
        "trash_obvious_ads": False,
        "scan": "metadata",
        "use_sender_profiles": True,
        "sender_profiles": {},
        "sender_profile_min_weight": 6,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def message(payload, labels=None, snippet="", size=0):
    """Create the Gmail message shape consumed by gmail_sorter.decide."""

    return {
        "id": "msg-1",
        "threadId": "thread-1",
        "labelIds": labels or [],
        "snippet": snippet,
        "internalDate": "1704067200000",
        "sizeEstimate": size,
        "payload": payload,
    }


def payload(headers, parts=None, filename="", mime_type="text/plain", body=None):
    """Build a lightweight Gmail payload with optional parts/body data."""

    return {
        "headers": [{"name": name, "value": value} for name, value in headers.items()],
        "parts": parts or [],
        "filename": filename,
        "mimeType": mime_type,
        "body": body or {},
    }


def body_payload(headers, body_text, mime_type="text/plain"):
    """Build a format=full-style payload whose body decodes to body_text."""

    import base64
    return {
        "headers": [{"name": name, "value": value} for name, value in headers.items()],
        "mimeType": mime_type,
        "body": {"data": base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii")},
    }


class GmailSorterPolicyTests(unittest.TestCase):
    """Regression tests for the cleanup policy, not the live Gmail API."""

    def test_registered_domain_groups_subdomains(self):
        # Sender reports should group noisy marketing subdomains together.
        self.assertEqual(gmail_sorter.registered_domain_for("email.linkedin.com"), "linkedin.com")

    def test_old_progress_decision_gets_new_defaults(self):
        # Progress files survive across releases even when Decision grows fields.
        decision = gmail_sorter.decision_from_dict(
            {
                "message_id": "m",
                "thread_id": "t",
                "date": "2024-01-01",
                "sender": "Sender <news@email.example.com>",
                "sender_email": "news@email.example.com",
                "sender_domain": "email.example.com",
                "subject": "Hello",
                "snippet": "",
            }
        )
        self.assertEqual(decision.registered_domain, "example.com")
        self.assertEqual(decision.message_size_estimate, 0)

    def test_immigration_mail_is_priority_and_protected(self):
        # Immigration/lawyer terms must override promotional-looking metadata.
        item = gmail_sorter.decide(
            message(
                payload(
                    {
                        "From": "Pinaz Marolia <pinaz@example-law.ca>",
                        "Subject": "IRCC work permit documents",
                        "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                    }
                ),
                snippet="Immigration application update",
            ),
            args(),
            gmail_sorter.Config(),
        )
        self.assertIn("Priority Immigration", item.categories)
        self.assertTrue(item.protected)

    def test_real_attachment_is_priority_but_inline_image_only_is_not_real(self):
        # PDFs/documents are safety signals; inline marketing images are not.
        real = payload(
            {"From": "School <registrar@example.edu>", "Subject": "Transcript", "Date": "Mon, 01 Jan 2024 00:00:00 +0000"},
            parts=[
                payload(
                    {"Content-Disposition": 'attachment; filename="transcript.pdf"'},
                    filename="transcript.pdf",
                    mime_type="application/pdf",
                    body={"attachmentId": "a1"},
                )
            ],
        )
        inline = payload(
            {"From": "Shop <promo@example.com>", "Subject": "Sale", "Date": "Mon, 01 Jan 2024 00:00:00 +0000"},
            parts=[
                payload(
                    {"Content-Disposition": 'inline; filename="hero.png"'},
                    filename="hero.png",
                    mime_type="image/png",
                    body={"attachmentId": "a2"},
                )
            ],
        )

        real_item = gmail_sorter.decide(message(real), args(), gmail_sorter.Config())
        inline_item = gmail_sorter.decide(message(inline), args(), gmail_sorter.Config())

        self.assertTrue(real_item.has_real_attachment)
        self.assertIn("Priority Attachments", real_item.categories)
        self.assertFalse(inline_item.has_real_attachment)
        self.assertTrue(inline_item.has_attachment)


class ArchiveAndLabelPolicyTests(unittest.TestCase):
    """Regression tests for the improved archive/labeling policy."""

    def _promo(self, extra_headers=None, snippet="Huge sale 50% off, shop now", subject="Flash sale 50% off ends tonight"):
        headers = {
            "From": "Deals <deals@promo.shopmail.co>",
            "Subject": subject,
            "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
        }
        headers.update(extra_headers or {})
        return message(payload(headers), labels=["CATEGORY_PROMOTIONS"], snippet=snippet)

    def test_high_score_without_bulk_signal_is_not_archived(self):
        # A one-off high-scoring subject with no bulk-mail headers and no Gmail
        # promotions label must not be archived out of the inbox.
        msg = message(
            payload(
                {
                    "From": "Person <person@shopmail.co>",
                    "Subject": "Flash sale 50% off ends tonight last chance",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="shop now new arrivals just dropped",
        )
        item = gmail_sorter.decide(msg, args(stage="archive"), gmail_sorter.Config())
        self.assertGreaterEqual(item.ad_confidence, 65)
        self.assertNotIn("archive", item.planned_actions)
        self.assertEqual(item.archive_reason, "")
        self.assertIn("archive_no_bulk_signal", item.negative_reasons)

    def test_bulk_signal_promo_is_archived_with_reason(self):
        msg = self._promo(extra_headers={"List-Unsubscribe": "<https://promo.shopmail.co/u>"})
        item = gmail_sorter.decide(msg, args(stage="archive"), gmail_sorter.Config())
        self.assertIn("archive", item.planned_actions)
        self.assertIn("list_unsubscribe_header", item.archive_reason)

    def test_archive_skips_unread_when_requested(self):
        msg = self._promo(extra_headers={"List-Unsubscribe": "<https://promo.shopmail.co/u>"})
        msg["labelIds"] = ["CATEGORY_PROMOTIONS", "UNREAD"]
        item = gmail_sorter.decide(msg, args(stage="archive", archive_skip_unread=True), gmail_sorter.Config())
        self.assertNotIn("archive", item.planned_actions)
        self.assertIn("archive_skipped_unread", item.negative_reasons)

    def test_review_catch_all_is_not_labeled(self):
        # Generic mail that only lands in the Review bucket should not create a
        # Sorter/Review label.
        msg = message(
            payload(
                {
                    "From": "Someone <someone@friendsmail.co>",
                    "Subject": "hey",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="just checking in",
        )
        item = gmail_sorter.decide(msg, args(stage="label"), gmail_sorter.Config())
        self.assertIn("Review", item.categories)
        self.assertNotIn("label:Review", item.planned_actions)
        self.assertEqual(item.planned_actions, [])

    def test_primary_category_prefers_protected_bucket(self):
        self.assertEqual(
            gmail_sorter.pick_primary_category(["Ads Promotions", "Finance", "Shopping"]),
            "Finance",
        )
        self.assertEqual(
            gmail_sorter.pick_primary_category(["Priority Immigration", "Ads Promotions"]),
            "Priority Immigration",
        )

    def test_archive_total_and_domain_caps(self):
        decisions = [
            gmail_sorter.Decision(
                message_id=f"m{i}",
                thread_id="t",
                date="2024-01-01",
                sender="Deals <deals@promo.example.com>",
                sender_email="deals@promo.example.com",
                sender_domain="promo.example.com",
                registered_domain="example.com",
                subject="Sale",
                snippet="",
                planned_actions=["label:Ads Promotions", "archive"],
            )
            for i in range(5)
        ]
        capped = argparse.Namespace(
            max_archive_total=3,
            max_archive_per_domain=0,
            archive_canary_limit=0,
            apply=False,
            stage="archive",
        )
        gmail_sorter.apply_archive_policy_caps(decisions, capped)
        archived = [d for d in decisions if "archive" in d.planned_actions]
        self.assertEqual(len(archived), 3)
        self.assertTrue(any("archive_total_cap:3" in d.negative_reasons for d in decisions))


class WordBoundaryAndSenderProfileTests(unittest.TestCase):
    """Regression tests for word-boundary keyword matching and sender profiles."""

    def test_wordlike_keywords_use_boundaries(self):
        # "exam" must not match inside "example.com"; "class" must not match
        # "classification". This is the substring bug that mislabeled mail.
        self.assertEqual(gmail_sorter.keyword_hits("news@example.com", ["exam"]), [])
        self.assertEqual(gmail_sorter.keyword_hits("classification report", ["class"]), [])
        self.assertEqual(gmail_sorter.keyword_hits("salon reminder", ["sale"]), [])
        # Genuine standalone matches still work, including multi-word phrases.
        self.assertEqual(gmail_sorter.keyword_hits("your exam results", ["exam"]), ["exam"])
        self.assertEqual(gmail_sorter.keyword_hits("limited time offer", ["limited time"]), ["limited time"])

    def test_punctuation_keywords_match_as_substrings(self):
        # "% off" starts with a non-word char so it is matched as an escaped
        # substring rather than with \b, which would not behave around %.
        self.assertEqual(gmail_sorter.keyword_hits("save 50% off today", ["% off"]), ["% off"])

    def test_sender_profile_adds_missed_category(self):
        # A statement whose subject lacks finance keywords should still be
        # labeled Finance when the sender was consistently labeled Finance
        # before. This is the self-improvement path for a re-run. The fixture
        # sender deliberately avoids finance keywords so only the profile can
        # add the category.
        profile_index = {
            "sender:updates@updates.testmail.co": {"Finance": 9},
            "domain:testmail.co": {"Finance": 3},
        }
        args_profile = args()
        args_profile.use_sender_profiles = True
        args_profile.sender_profiles = profile_index
        args_profile.sender_profile_min_weight = 6
        msg = message(
            payload(
                {
                    "From": "Updates <updates@updates.testmail.co>",
                    "Subject": "Your monthly update",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="please review your account",
        )
        item = gmail_sorter.decide(msg, args_profile, gmail_sorter.Config())
        self.assertIn("Finance", item.categories)
        self.assertTrue(any(r.startswith("sender_profile:Finance") for r in item.reasons))

    def test_sender_profile_does_not_duplicate_existing_category(self):
        profile_index = {"sender:registrar@school.testmail.co": {"Priority Studies": 9}}
        args_profile = args()
        args_profile.use_sender_profiles = True
        args_profile.sender_profiles = profile_index
        args_profile.sender_profile_min_weight = 6
        msg = message(
            payload(
                {
                    "From": "School <registrar@school.testmail.co>",
                    "Subject": "Transcript and exam schedule",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="university registrar",
        )
        item = gmail_sorter.decide(msg, args_profile, gmail_sorter.Config())
        self.assertEqual(item.categories.count("Priority Studies"), 1)

    def test_no_sender_profiles_flag_disables_assist(self):
        profile_index = {"sender:updates@updates.testmail.co": {"Finance": 9}}
        args_profile = args()
        args_profile.use_sender_profiles = False
        args_profile.sender_profiles = profile_index
        msg = message(
            payload(
                {
                    "From": "Updates <updates@updates.testmail.co>",
                    "Subject": "Your monthly update",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="please review your account",
        )
        item = gmail_sorter.decide(msg, args_profile, gmail_sorter.Config())
        self.assertNotIn("Finance", item.categories)

    def test_scan_full_uses_body_for_categorization(self):
        # The subject has no immigration keyword, but the body mentions IRCC
        # and a work permit. With --scan full the body is decoded and the
        # message is categorized as Priority Immigration, which it would miss
        # in metadata-only mode.
        headers = {
            "From": "Counsel <counsel@legal.testmail.co>",
            "Subject": "File update",
            "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
        }
        msg = message(
            body_payload(headers, "Please find your IRCC work permit and biometrics appointment enclosed."),
        )
        meta_item = gmail_sorter.decide(msg, args(scan="metadata"), gmail_sorter.Config())
        full_item = gmail_sorter.decide(msg, args(scan="full"), gmail_sorter.Config())
        self.assertNotIn("Priority Immigration", meta_item.categories)
        self.assertIn("Priority Immigration", full_item.categories)
        self.assertGreater(full_item.body_len, 0)
        self.assertTrue(any(r.startswith("body_included:") for r in full_item.reasons))

    def test_scan_metadata_does_not_read_body(self):
        # In real metadata mode the payload has no body data, so body-aware
        # categorization is a no-op and body_len stays 0.
        headers = {
            "From": "Counsel <counsel@legal.testmail.co>",
            "Subject": "File update",
            "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
        }
        msg = message(payload(headers))
        item = gmail_sorter.decide(msg, args(scan="metadata"), gmail_sorter.Config())
        self.assertEqual(item.body_len, 0)
        self.assertNotIn("Priority Immigration", item.categories)


if __name__ == "__main__":
    unittest.main()
