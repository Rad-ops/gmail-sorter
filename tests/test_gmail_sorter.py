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
        "trash_threshold": 90,
        "pre_2020_trash_threshold": 75,
        "stage": "classify",
        "trash_obvious_ads": False,
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


if __name__ == "__main__":
    unittest.main()
