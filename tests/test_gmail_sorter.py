import argparse
import json
import sqlite3
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
        "label_confidence": 50,
        "max_labels_per_message": 3,
        "cached_body_features": {},
        "relabel_run_id": "",
        "undo_relabel": "",
        "relabel_since_date": "",
        "relabel_label": "",
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


class FakeLabel:
    def __init__(self, name, lid, messages_total=0):
        self._name = name
        self._id = lid
        self._messages_total = messages_total

    def get(self, key, default=None):
        return {"name": self._name, "id": self._id, "messagesTotal": self._messages_total}.get(key, default)

    def __getitem__(self, key):
        return {"name": self._name, "id": self._id, "messagesTotal": self._messages_total}[key]


class FakeBatchModifyRequest:
    def __init__(self, captured, body):
        self.captured = captured
        self.body = body

    def execute(self):
        self.captured.append(self.body)
        return {}


class FakeLabelsListRequest:
    def __init__(self, labels):
        self.labels = labels

    def execute(self):
        return {"labels": self.labels}


class FakeLabelsDeleteRequest:
    def __init__(self, name, captured):
        self.name = name
        self.captured = captured

    def execute(self):
        self.captured.append(self.name)
        return {}


class FakeLabelCreateRequest:
    def __init__(self, name, name_to_id):
        self.name = name
        self.name_to_id = name_to_id

    def execute(self):
        self.name_to_id[self.name] = f"id-{self.name.replace('/', '_')}"
        return {"id": self.name_to_id[self.name], "name": self.name}


class FakeGmailService:
    """Minimal Gmail service stub for relabel tests."""

    def __init__(self, labels):
        self._labels = labels  # list[FakeLabel]
        self.batch_modify_calls = []
        self.delete_calls = []
        self.created = {}

    def users(self):
        return self

    def labels(self):
        return self

    def list(self, userId="me"):
        return FakeLabelsListRequest(self._labels)

    def create(self, userId="me", body=None):
        return FakeLabelCreateRequest(body["name"], self.created)

    def delete(self, userId="me", id=None):
        name = next(l._name for l in self._labels if l._id == id)
        return FakeLabelsDeleteRequest(name, self.delete_calls)

    def messages(self):
        return self

    def batchModify(self, userId="me", body=None):
        return FakeBatchModifyRequest(self.batch_modify_calls, body)


class RelabelStageTests(unittest.TestCase):
    """Regression tests for the relabel stage's label diff."""

    def _decision(self, mid, existing_sorter_ids, categories):
        return gmail_sorter.Decision(
            message_id=mid,
            thread_id="t",
            date="2024-01-01",
            sender="S <s@x.testmail.co>",
            sender_email="s@x.testmail.co",
            sender_domain="x.testmail.co",
            registered_domain="testmail.co",
            subject="sub",
            snippet="",
            existing_labels=list(existing_sorter_ids),
            categories=categories,
        )

    def test_relabel_diffs_and_only_touches_sorter_namespace(self):
        # existing labels: Sorter/Finance (stale) + a user label "Receipts"
        # (id user-receipts, must NOT be removed). Desired: Sorter/Ads Promotions.
        service = FakeGmailService(
            labels=[
                FakeLabel("Sorter/Finance", "sl-fin"),
                FakeLabel("Sorter/Ads Promotions", "sl-ads"),
                FakeLabel("Receipts", "user-receipts", messages_total=5),
            ]
        )
        ns = argparse.Namespace(
            retries=1,
            retry_sleep=0,
            batch_size=10,
            apply_progress_every=1,
            stage="relabel",
        )
        decisions = [self._decision("m1", ["sl-fin", "user-receipts"], ["Ads Promotions"])]
        gmail_sorter.apply_relabel(service, decisions, ns, state_conn=None)

        # Exactly one batchModify carrying add Sorter/Ads Promotions and remove
        # Sorter/Finance, and never the user Receipts label.
        self.assertEqual(len(service.batch_modify_calls), 1)
        call = service.batch_modify_calls[0]
        self.assertIn("sl-ads", call.get("addLabelIds", []))
        self.assertIn("sl-fin", call.get("removeLabelIds", []))
        self.assertNotIn("user-receipts", call.get("removeLabelIds", []))
        self.assertEqual(decisions[0].action_done, "yes")

    def test_relabel_clears_all_sorter_labels_when_desired_is_empty(self):
        # A message that now only lands in a catch-all bucket should have all its
        # Sorter/* labels removed and none added.
        service = FakeGmailService(labels=[FakeLabel("Sorter/Review", "sl-rev", messages_total=1)])
        ns = argparse.Namespace(retries=1, retry_sleep=0, batch_size=10, apply_progress_every=1, stage="relabel")
        decisions = [self._decision("m1", ["sl-rev"], ["Review"])]  # Review is a non-label catch-all
        gmail_sorter.apply_relabel(service, decisions, ns, state_conn=None)
        self.assertEqual(len(service.batch_modify_calls), 1)
        call = service.batch_modify_calls[0]
        self.assertIn("sl-rev", call.get("removeLabelIds", []))
        self.assertNotIn("addLabelIds", call)

    def test_relabel_no_op_when_already_correct(self):
        service = FakeGmailService(labels=[FakeLabel("Sorter/Finance", "sl-fin", messages_total=1)])
        ns = argparse.Namespace(retries=1, retry_sleep=0, batch_size=10, apply_progress_every=1, stage="relabel")
        decisions = [self._decision("m1", ["sl-fin"], ["Finance"])]
        gmail_sorter.apply_relabel(service, decisions, ns, state_conn=None)
        self.assertEqual(service.batch_modify_calls, [])

    def test_prune_empty_sorter_labels(self):
        service = FakeGmailService(
            labels=[
                FakeLabel("Sorter/Finance", "sl-fin", messages_total=0),
                FakeLabel("Sorter/Ads Promotions", "sl-ads", messages_total=3),
                FakeLabel("Inbox", "inbox", messages_total=0),  # system label, must be ignored
            ]
        )
        pruned = gmail_sorter.prune_empty_sorter_labels(service, retries=1, retry_sleep=0)
        self.assertEqual(pruned, ["Sorter/Finance"])
        self.assertEqual(len(service.delete_calls), 1)


class ConfidenceAndBodyCleaningTests(unittest.TestCase):
    """Tests for per-category confidence, label caps, and body cleaning."""

    def test_low_confidence_category_is_dropped(self):
        # A message that only weakly matches one keyword for a non-protected
        # category should be dropped when below --label-confidence.
        msg = message(
            payload(
                {
                    "From": "Shop <shop@retail.testmail.co>",
                    "Subject": "cart reminder",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="your cart is waiting",
        )
        item = gmail_sorter.decide(msg, args(label_confidence=80), gmail_sorter.Config())
        self.assertNotIn("Shopping", item.categories)

    def test_high_confidence_category_is_kept(self):
        # Multiple finance keywords + a bank domain should keep Finance.
        msg = message(
            payload(
                {
                    "From": "Bank <statements@bank.testmail.co>",
                    "Subject": "Your statement and invoice for payment",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="payroll receipt tax",
        )
        item = gmail_sorter.decide(msg, args(label_confidence=50), gmail_sorter.Config())
        self.assertIn("Finance", item.categories)
        self.assertGreater(item.category_confidence.get("Finance", 0), 50)

    def test_max_labels_per_message_caps_optional_labels(self):
        # A message matching several optional categories but no protected ones
        # should be capped at --max-labels-per-message.
        msg = message(
            payload(
                {
                    "From": "Shop <shop@retail.testmail.co>",
                    "Subject": "cart wishlist store coupon discount",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
            snippet="store shop retailer coupon",
        )
        item = gmail_sorter.decide(msg, args(max_labels_per_message=1, label_confidence=0), gmail_sorter.Config())
        labelable = gmail_sorter.labelable_categories(item.categories)
        self.assertLessEqual(len(labelable), 1)

    def test_clean_body_strips_quotes_and_footer(self):
        raw = (
            "Hi, your IRCC work permit is ready.\n"
            "> On Monday the promo shop wrote:\n"
            "> 50% off sale ends tonight shop now\n"
            "Regards,\n"
            "Unsubscribe here: https://example.com/u\n"
            "Sent from my iPhone\n"
        )
        cleaned = gmail_sorter.clean_body_text(raw)
        self.assertIn("IRCC", cleaned)
        self.assertNotIn("50% off", cleaned)
        self.assertNotIn("Unsubscribe", cleaned)
        self.assertNotIn("Sent from my iPhone", cleaned)


class RelabelUndoAndResumeTests(unittest.TestCase):
    """Tests for undo relabel and resume-via-ledger (items 9 and 13)."""

    def _decision(self, mid, existing_sorter_ids, categories):
        return gmail_sorter.Decision(
            message_id=mid,
            thread_id="t",
            date="2024-01-01",
            sender="S <s@x.testmail.co>",
            sender_email="s@x.testmail.co",
            sender_domain="x.testmail.co",
            registered_domain="testmail.co",
            subject="sub",
            snippet="",
            existing_labels=list(existing_sorter_ids),
            categories=categories,
        )

    def _service_with_labels(self, labels):
        return FakeGmailService(labels=labels)

    def test_undo_relabel_reverses_adds_and_removes(self):
        service = self._service_with_labels(
            [FakeLabel("Sorter/Finance", "sl-fin"), FakeLabel("Sorter/Ads Promotions", "sl-ads")]
        )
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE action_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, stage TEXT, action TEXT, message_id TEXT, status TEXT, detail TEXT)"
        )
        run_id = "20260706T120000"
        # Simulate a prior relabel that removed sl-fin and added sl-ads.
        detail = json.dumps({"run_id": run_id, "removed": ["sl-fin"], "added": ["sl-ads"], "previous_labels": ["Sorter/Finance"]})
        conn.execute("INSERT INTO action_ledger (created_at, stage, action, message_id, status, detail) VALUES (?, 'relabel', 'relabel', 'm1', 'success', ?)", ("2026-07-06T12:00:00", detail))
        conn.commit()
        ns = argparse.Namespace(retries=1, retry_sleep=0, batch_size=10, apply_progress_every=1, apply=True)
        code = gmail_sorter.undo_relabel(service, run_id, ns, state_conn=conn)
        self.assertEqual(code, 0)
        # Undo should add sl-fin back and remove sl-ads.
        self.assertEqual(len(service.batch_modify_calls), 1)
        call = service.batch_modify_calls[0]
        self.assertIn("sl-fin", call.get("addLabelIds", []))
        self.assertIn("sl-ads", call.get("removeLabelIds", []))
        conn.close()

    def test_resume_skips_already_applied_messages(self):
        service = self._service_with_labels(
            [FakeLabel("Sorter/Finance", "sl-fin"), FakeLabel("Sorter/Ads Promotions", "sl-ads")]
        )
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "state.sqlite")
            run_id = "20260706T120000"
            detail = json.dumps({"run_id": run_id, "removed": [], "added": ["sl-ads"], "previous_labels": []})
            conn.execute("INSERT INTO action_ledger (created_at, stage, action, message_id, status, detail) VALUES (?, 'relabel', 'relabel', 'm1', 'success', ?)", ("2026-07-06T12:00:00", detail))
            conn.commit()
            ns = argparse.Namespace(
                retries=1, retry_sleep=0, batch_size=10, apply_progress_every=1, stage="relabel", relabel_run_id=run_id
            )
            decisions = [
                self._decision("m1", ["sl-fin"], ["Ads Promotions"]),  # already applied
                self._decision("m2", ["sl-fin"], ["Ads Promotions"]),  # needs apply
            ]
            gmail_sorter.apply_relabel(service, decisions, ns, state_conn=conn)
            conn.close()
        # Only m2 should have been sent to Gmail.
        self.assertEqual(len(service.batch_modify_calls), 1)
        self.assertIn("m2", service.batch_modify_calls[0]["ids"])


class BodyFeatureCacheTests(unittest.TestCase):
    """Tests for cached body-feature reuse (item 12)."""

    def test_cached_body_hits_apply_without_fresh_body(self):
        # A metadata-only message (no body data) whose body features are cached
        # should still get the cached body category applied.
        cached = {
            "msg-1": {
                "body_len": 500,
                "body_category_hits": ["Priority Immigration"],
                "body_unsubscribe_count": 1,
            }
        }
        ns = args(scan="full", cached_body_features=cached)
        msg = message(
            payload(
                {
                    "From": "Counsel <counsel@legal.testmail.co>",
                    "Subject": "File update",
                    "Date": "Mon, 01 Jan 2024 00:00:00 +0000",
                }
            ),
        )
        item = gmail_sorter.decide(msg, ns, gmail_sorter.Config())
        self.assertIn("Priority Immigration", item.categories)
        self.assertTrue(any(r.startswith("cached_body:") for r in item.reasons))


if __name__ == "__main__":
    unittest.main()
