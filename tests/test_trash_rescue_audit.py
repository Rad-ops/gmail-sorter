import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
import trash_rescue_audit


def decision(**overrides):
    """Build a sorter Decision with safe defaults for rescue-audit tests."""

    data = {
        "message_id": "m1",
        "thread_id": "t1",
        "date": "2024-01-01",
        "sender": "Promo <promo@example.com>",
        "sender_email": "promo@example.com",
        "sender_domain": "example.com",
        "registered_domain": "example.com",
        "subject": "Sale",
        "snippet": "",
        "ad_confidence": 100,
        "reasons": ["perfect_ad_match"],
        "negative_reasons": [],
        "planned_actions": ["trash"],
        "protected": False,
        "perfect_ad_match": True,
    }
    data.update(overrides)
    return gmail_sorter.Decision(**data)


def message(headers, snippet="", labels=None, parts=None):
    """Build the Gmail message shape used by trash_rescue_audit.audit_message."""

    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": labels or ["TRASH"],
        "snippet": snippet,
        "internalDate": "1704067200000",
        "sizeEstimate": 1234,
        "payload": {
            "headers": [{"name": name, "value": value} for name, value in headers.items()],
            "parts": parts or [],
            "body": {},
            "mimeType": "text/plain",
            "filename": "",
        },
    }


class TrashRescueAuditTests(unittest.TestCase):
    """Regression tests for rescue rules and permanent-delete gates."""

    def test_immigration_message_becomes_rescue_review(self):
        # Priority terms should pull a message back for human review.
        audit = trash_rescue_audit.audit_message(
            decision(subject="IRCC work permit update"),
            message(
                {
                    "From": "Pinaz Marolia <pinaz@law.example>",
                    "Subject": "IRCC work permit update",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                },
                snippet="Please review the attached immigration application update.",
            ),
        )
        self.assertEqual(audit.recommended_action, "rescue_review")
        self.assertGreaterEqual(audit.deep_risk_score, 45)

    def test_clear_marketing_stays_keep_trash(self):
        # Obvious newsletter language plus unsubscribe evidence can stay Trash.
        audit = trash_rescue_audit.audit_message(
            decision(),
            message(
                {
                    "From": "Deals <deals@shop.example>",
                    "Subject": "Flash sale 50% off",
                    "List-Unsubscribe": "<https://shop.example/unsubscribe>",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                },
                snippet="Shop now. Manage preferences or unsubscribe.",
            ),
        )
        self.assertEqual(audit.recommended_action, "keep_trash")

    def test_real_attachment_becomes_rescue_review(self):
        # Attachments are treated as durable evidence until reviewed.
        audit = trash_rescue_audit.audit_message(
            decision(subject="Document"),
            message(
                {
                    "From": "Registrar <registrar@example.edu>",
                    "Subject": "Transcript document",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                },
                parts=[
                    {
                        "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="transcript.pdf"'}],
                        "filename": "transcript.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "a1"},
                        "parts": [],
                    }
                ],
            ),
        )
        self.assertEqual(audit.recommended_action, "rescue_review")
        self.assertTrue(audit.has_real_attachment)

    def test_import_model_results_can_promote_rescue_review(self):
        # Model review can only make the workflow more cautious here.
        audit = trash_rescue_audit.audit_message(
            decision(),
            message(
                {
                    "From": "Sender <sender@example.com>",
                    "Subject": "Follow up",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                },
                snippet="Following up on the document.",
            ),
        )
        audit.recommended_action = "keep_trash"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "model.jsonl"
            path.write_text(
                '{"message_id":"m1","decision":"rescue_review","confidence":0.91,"reason":"human document follow-up"}\n',
                encoding="utf-8",
            )
            imported = trash_rescue_audit.import_model_results(path, [audit])
        self.assertEqual(imported, 1)
        self.assertEqual(audit.recommended_action, "rescue_review")
        self.assertEqual(audit.model_decision, "rescue_review")

    def test_local_llm_prompt_requires_strict_json(self):
        # The local pipeline depends on parseable JSONL, not prose.
        packet = {"message_id": "m1", "subject": "IRCC update"}
        prompt = trash_rescue_audit.local_llm_prompt(packet)
        self.assertIn("Return exactly one JSON object", prompt)
        self.assertIn('"message_id": "m1"', prompt)

    def test_permanent_delete_requires_both_100_percent_gates(self):
        # Permanent delete needs script confidence and model confidence together.
        audit = trash_rescue_audit.audit_message(
            decision(),
            message(
                {
                    "From": "Deals <deals@shop.example>",
                    "Subject": "Flash sale 50% off",
                    "List-Unsubscribe": "<https://shop.example/unsubscribe>",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                },
                snippet="Shop now. Manage preferences or unsubscribe.",
            ),
        )
        audit.model_decision = "keep_trash"
        audit.model_confidence = 0.99
        self.assertEqual(trash_rescue_audit.permanent_delete_candidates([audit]), [])
        audit.model_confidence = 1.0
        self.assertEqual(trash_rescue_audit.permanent_delete_candidates([audit]), [audit])

    def test_missing_gmail_message_detection(self):
        # Stale progress entries should be summarized, not crash the audit.
        class Response:
            status = 404

        class Error(Exception):
            resp = Response()

        self.assertTrue(trash_rescue_audit.is_missing_gmail_message_error(Error("not found")))
        self.assertTrue(trash_rescue_audit.is_missing_gmail_message_error(Exception("Requested entity was not found.")))

    def test_gmail_404_is_not_retried(self):
        # 404 is not retryable; Gmail is saying the ID is gone.
        class Response:
            status = 404

        class Error(Exception):
            resp = Response()

        class Request:
            calls = 0

            def execute(self):
                self.calls += 1
                raise Error("Requested entity was not found.")

        request = Request()
        with self.assertRaises(Error):
            gmail_sorter.execute_with_retries(request, retries=8, retry_sleep=0)
        self.assertEqual(request.calls, 1)


if __name__ == "__main__":
    unittest.main()
