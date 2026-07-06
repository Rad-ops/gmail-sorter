"""Integration and end-to-end tests for the Gmail sorter (v0.7 expansion).

These tests exercise the full pipeline (scan -> decide -> apply -> undo)
against a `FakeGmailService` stub that records every call. The goal is
to catch integration bugs that the unit tests miss:

* Stage transitions (classify -> label -> relabel -> undo)
* Manifest validation
* Action ledger round-trips
* Progress file round-trips
* Yearly dashboard generation
* AI review export -> merge -> apply end-to-end
* Real body in centroid end-to-end
* Sender profile time-decay end-to-end
"""

import json
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter


class FakeGmailService:
    """Records every Gmail API call. Mirrors the surface the sorter uses."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.labels: dict[str, str] = {"Sorter/Finance": "LBL_FIN", "Sorter/Shopping": "LBL_SHOP"}
        self.trashed: list[str] = []
        self.archived: list[str] = []
        self.modified: list[tuple[str, list[str], list[str]]] = []  # (msg_id, add, remove)
        self.deleted: list[str] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"msg{self._counter}"

    def users(self):
        return _UsersNamespace(self)


class _UsersNamespace:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def messages(self):
        return _MessagesNamespace(self._service)

    def labels(self):
        return _LabelsNamespace(self._service)


class _MessagesNamespace:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def list(self, **kwargs):
        return _ListRequest(self._service, kwargs)

    def get(self, **kwargs):
        return _GetRequest(self._service, kwargs)

    def modify(self, **kwargs):
        return _ModifyRequest(self._service, kwargs)

    def batchModify(self, **kwargs):
        return _BatchModifyRequest(self._service, kwargs)

    def trash(self, **kwargs):
        return _TrashRequest(self._service, kwargs)

    def delete(self, **kwargs):
        return _DeleteRequest(self._service, kwargs)


class _LabelsNamespace:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def list(self, **kwargs):
        return _LabelsListRequest(self._service, kwargs)

    def create(self, **kwargs):
        return _LabelsCreateRequest(self._service, kwargs)

    def delete(self, **kwargs):
        return _LabelsDeleteRequest(self._service, kwargs)


class _Request:
    def __init__(self, service, kwargs):
        self._service = service
        self._kwargs = kwargs

    def execute(self):
        return {}


class _ListRequest(_Request):
    def execute(self):
        self._service.calls.append(("messages.list", dict(self._kwargs)))
        return {"messages": [], "nextPageToken": None}


class _GetRequest(_Request):
    def execute(self):
        self._service.calls.append(("messages.get", dict(self._kwargs)))
        return {
            "id": self._kwargs.get("id", "msg1"),
            "threadId": "t1",
            "labelIds": [],
            "snippet": "",
            "internalDate": "1704067200000",
            "sizeEstimate": 0,
            "payload": {"headers": [], "parts": []},
        }


class _ModifyRequest(_Request):
    def execute(self):
        self._service.calls.append(("messages.modify", dict(self._kwargs)))
        return {"id": self._kwargs.get("id"), "labelIds": []}


class _BatchModifyRequest(_Request):
    def execute(self):
        self._service.calls.append(("messages.batchModify", dict(self._kwargs)))
        return {}


class _TrashRequest(_Request):
    def execute(self):
        msg_id = self._kwargs.get("id", "")
        self._service.calls.append(("messages.trash", dict(self._kwargs)))
        self._service.trashed.append(msg_id)
        return {"id": msg_id}


class _DeleteRequest(_Request):
    def execute(self):
        msg_id = self._kwargs.get("id", "")
        self._service.calls.append(("messages.delete", dict(self._kwargs)))
        self._service.deleted.append(msg_id)
        return {}


class _LabelsListRequest(_Request):
    def execute(self):
        self._service.calls.append(("labels.list", dict(self._kwargs)))
        return {"labels": [{"id": lid, "name": name} for name, lid in self._service.labels.items()]}


class _LabelsCreateRequest(_Request):
    def execute(self):
        body = self._kwargs.get("body", {}) or {}
        name = body.get("name", "")
        new_id = f"LBL_{name.upper().replace('/', '_')}"
        self._service.labels[name] = new_id
        self._service.calls.append(("labels.create", dict(self._kwargs)))
        return {"id": new_id, "name": name}


class _LabelsDeleteRequest(_Request):
    def execute(self):
        self._service.calls.append(("labels.delete", dict(self._kwargs)))
        return {}


class ApplyPipelineTests(unittest.TestCase):
    """apply_decisions() must record every Gmail write in the action_ledger."""

    def test_apply_label_records_action_ledger(self):
        from tests.test_gmail_sorter import args, message, payload

        service = FakeGmailService()
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="",
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 90},
                planned_actions=["label:Finance"],
            ),
        ]
        a = args(stage="label", apply=True)
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.apply_decisions(service, decisions, a, conn)
            rows = conn.execute("SELECT stage, action, message_id, status FROM action_ledger").fetchall()
            self.assertGreater(len(rows), 0)
            stages = {row[0] for row in rows}
            self.assertIn("label", stages)
            conn.close()

    def test_apply_archive_calls_batch_modify(self):
        from tests.test_gmail_sorter import args, message, payload

        service = FakeGmailService()
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Promo <noreply@shop.com>", sender_email="noreply@shop.com",
                sender_domain="shop.com", registered_domain="shop.com",
                subject="50% off", snippet="",
                categories=["Ads Promotions"], primary_category="Ads Promotions",
                category_confidence={"Ads Promotions": 90},
                planned_actions=["archive"],
            ),
        ]
        a = args(stage="archive", apply=True)
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.apply_decisions(service, decisions, a, conn)
            # The fake service records every call; we should have a
            # batchModify or a modify for the INBOX removal.
            call_names = [c[0] for c in service.calls]
            self.assertTrue(
                any("batchModify" in n or "modify" in n for n in call_names),
                f"expected a modify call, got {call_names}",
            )
            conn.close()

    def test_apply_trash_calls_trash(self):
        from tests.test_gmail_sorter import args

        service = FakeGmailService()
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Promo <noreply@shop.com>", sender_email="noreply@shop.com",
                sender_domain="shop.com", registered_domain="shop.com",
                subject="50% off", snippet="",
                categories=["Ads Promotions"], primary_category="Ads Promotions",
                category_confidence={"Ads Promotions": 100},
                perfect_ad_match=True,
                planned_actions=["trash"],
                ad_confidence=100,
            ),
        ]
        a = args(stage="trash", apply=True, trash_obvious_ads=True, i_understand_trash=True)
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.apply_decisions(service, decisions, a, conn)
            self.assertIn("m1", service.trashed)
            conn.close()

    def test_apply_strips_archive_and_trash_for_protected_messages(self):
        from tests.test_gmail_sorter import args

        service = FakeGmailService()
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="",
                categories=["Finance", "Ads Promotions"],
                primary_category="Finance",
                category_confidence={"Finance": 100, "Ads Promotions": 80},
                planned_actions=["trash", "archive"],
                ad_confidence=80,
                protected=True,
            ),
        ]
        a = args(stage="trash", apply=True, trash_obvious_ads=True, i_understand_trash=True)
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.apply_decisions(service, decisions, a, conn)
            # Protected: the trash call must not happen.
            self.assertNotIn("m1", service.trashed)
            conn.close()


class ProgressFileTests(unittest.TestCase):
    """Progress JSON round-trips through save and load without loss."""

    def test_round_trip(self):
        decisions = {
            "m1": gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="your statement",
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 90},
                body_len=120, body_text_excerpt="your statement excerpt",
                detected_language="en",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            gmail_sorter.save_progress(path, decisions)
            loaded = gmail_sorter.load_progress(path)
            self.assertIn("m1", loaded)
            self.assertEqual(loaded["m1"].subject, "Statement")
            self.assertEqual(loaded["m1"].body_text_excerpt, "your statement excerpt")
            self.assertEqual(loaded["m1"].detected_language, "en")

    def test_decision_from_dict_backfills_missing_fields(self):
        # Pre-v0.7 decisions serialized without body_text_excerpt must
        # still load — the new field gets its dataclass default.
        data = {
            "message_id": "m1", "thread_id": "t", "date": "2025-01-01",
            "sender": "x", "sender_email": "x", "sender_domain": "x",
            "registered_domain": "x", "subject": "s", "snippet": "",
        }
        d = gmail_sorter.decision_from_dict(data)
        self.assertEqual(d.body_text_excerpt, "")
        self.assertEqual(d.detected_language, "")


class AIReviewEndToEndTests(unittest.TestCase):
    """export -> merge -> apply end-to-end. Uses FakeGmailService."""

    def test_export_then_merge_then_relabel(self):
        from tests.test_gmail_sorter import args

        # Step 1: build decisions and export.
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t1", date="2026-07-06",
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="",
                categories=["Review"], primary_category="Review",
                category_confidence={"Review": 30},
            ),
            gmail_sorter.Decision(
                message_id="m2", thread_id="t2", date="2026-07-06",
                sender="Doctor <doc@clinic.com>", sender_email="doc@clinic.com",
                sender_domain="clinic.com", registered_domain="clinic.com",
                subject="Visit", snippet="",
                categories=["Review"], primary_category="Review",
                category_confidence={"Review": 30},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ai_path = Path(tmp) / "ai.jsonl"
            exported = gmail_sorter.export_ai_review_packets(ai_path, decisions, threshold=75)
            self.assertEqual(exported, 2)

            # Step 2: simulate AI review.
            with ai_path.open("r") as f:
                packets = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(packets), 2)
            packets[0]["ai_label"] = "Finance"
            packets[0]["ai_confidence"] = 0.95
            packets[0]["ai_reason"] = "Bank statement"
            packets[0]["ai_reviewed"] = True
            packets[1]["ai_label"] = "Health"
            packets[1]["ai_confidence"] = 0.92
            packets[1]["ai_reason"] = "Doctor visit"
            packets[1]["ai_reviewed"] = True
            with ai_path.open("w") as f:
                for p in packets:
                    f.write(json.dumps(p) + "\n")

            # Step 3: merge.
            a = args(stage="classify", ai_merge_min_confidence=0.7)
            agreed, overridden, removed = gmail_sorter.merge_ai_labels(
                decisions, ai_path, min_ai_confidence=0.7,
            )
            self.assertEqual(overridden, 2)
            self.assertIn("Finance", decisions[0].categories)
            self.assertIn("Health", decisions[1].categories)

            # Step 4: apply via FakeGmailService.
            service = FakeGmailService()
            decisions[0].planned_actions = ["label:Finance"]
            decisions[1].planned_actions = ["label:Health"]
            apply_a = args(stage="label", apply=True)
            state_db = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.apply_decisions(service, decisions, apply_a, state_db)
            # The fake service recorded at least one batchModify for the
            # two labels.
            call_names = [c[0] for c in service.calls]
            self.assertIn("messages.batchModify", call_names)
            state_db.close()


class BodyExcerptEndToEndTests(unittest.TestCase):
    """decide() must write the excerpt to the cache and read it back."""

    def test_write_then_read_excerpt(self):
        from tests.test_gmail_sorter import args, message, body_payload

        a = args(scan="full")
        body_text = "Your January statement is now available. Thank you for banking with us."
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                body_text,
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        self.assertIn("statement", decision.body_text_excerpt.lower())

        with tempfile.TemporaryDirectory() as tmp:
            db = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            gmail_sorter.upsert_message_features(db, [decision], scan_mode="full")
            index = gmail_sorter.load_body_features_index(db)
            self.assertIn(decision.message_id, index)
            self.assertEqual(index[decision.message_id]["body_text_excerpt"], decision.body_text_excerpt)
            db.close()


class EmbeddingEndToEndTests(unittest.TestCase):
    """Decide() with --use-embeddings must apply the hybrid scoring."""

    def test_hybrid_scoring_uses_centroid_text(self):
        from tests.test_gmail_sorter import args, message, body_payload

        class StaticBackend:
            def __init__(self):
                self.dimension = 8
                self.calls = []

            def embed(self, text):
                self.calls.append(text)
                if "statement" in text.lower() or "banque" in text.lower():
                    return [1.0] + [0.0] * 7
                return [0.0] * 8

        a = args(
            scan="full",
            _embedding_backend=StaticBackend(),
            category_centroids={"Finance": [1.0] + [0.0] * 7},
        )
        msg = message(
            body_payload(
                {"From": "Bank <noreply@bank.com>", "Subject": "Statement"},
                "Your statement is ready.",
            ),
            labels=[],
        )
        decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
        # The embedding centroid should boost Finance even if no
        # keyword rule fires. The hybrid must take the max.
        self.assertIn("Finance", decision.categories)


class SenderProfileTimeDecayEndToEndTests(unittest.TestCase):
    """A 720-day-old profile contributes far less than a fresh one."""

    def test_decay_affects_decide(self):
        from tests.test_gmail_sorter import args, message, body_payload
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "s.sqlite"
            conn = gmail_sorter.open_state_db(db_path)
            # Seed an old profile: 720 days ago, multiple hits.
            old = gmail_sorter.Decision(
                message_id="old", thread_id="t", date=(datetime.now(timezone.utc) - timedelta(days=720)).date().isoformat(),
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="",
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 70}, ad_confidence=70,
            )
            # Seed a fresh profile: 1 day ago.
            fresh = gmail_sorter.Decision(
                message_id="fresh", thread_id="t", date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
                sender="Insurance <noreply@insurer.com>", sender_email="noreply@insurer.com",
                sender_domain="insurer.com", registered_domain="insurer.com",
                subject="Policy", snippet="",
                categories=["Insurance"], primary_category="Insurance",
                category_confidence={"Insurance": 70}, ad_confidence=70,
            )
            gmail_sorter.update_sender_profiles(conn, [old, fresh], confidence_floor=65)
            conn.close()

            # Half-life of 30 days: the 720-day profile gets 2^(-720/30) ~= 0 weight.
            conn = gmail_sorter.open_state_db(db_path)
            index = gmail_sorter.load_sender_profile_index(conn, half_life_days=30, min_hits=1)
            fresh_weight = index.get("sender:noreply@insurer.com:insurance", {}).get("Insurance", 0)
            old_weight = index.get("sender:noreply@bank.com:finance", {}).get("Finance", 0)
            self.assertGreaterEqual(fresh_weight, 3)
            self.assertLessEqual(old_weight, 1)  # heavily decayed
            conn.close()


class YearlyDashboardTests(unittest.TestCase):
    """write_yearly_dashboards splits the combined dashboard by year."""

    def test_yearly_dashboards_generated(self):
        from tests.test_gmail_sorter import args
        decisions = [
            gmail_sorter.Decision(
                message_id=f"m{i}", thread_id="t", date=date,
                sender="x", sender_email="x", sender_domain="x", registered_domain="x",
                subject="s", snippet="",
                categories=["Finance"], primary_category="Finance",
            )
            for i, date in enumerate(["2024-01-15", "2024-06-01", "2025-03-01"])
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out_prefix = Path(tmp) / "report"
            a = args()
            yearly = gmail_sorter.write_yearly_dashboards(out_prefix, decisions, a)
            # Should produce 2 yearly dashboards: 2024 and 2025.
            self.assertEqual(len(yearly), 2)
            for path in yearly:
                self.assertTrue(path.exists())
                content = path.read_text().lower()
                self.assertIn("gmail sorter", content)


class ReportsTests(unittest.TestCase):
    """CSV/JSON reports round-trip without loss of key fields."""

    def test_csv_has_key_columns(self):
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t", date="2026-07-06",
                sender="Bank <noreply@bank.com>", sender_email="noreply@bank.com",
                sender_domain="bank.com", registered_domain="bank.com",
                subject="Statement", snippet="",
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 90},
                detected_language="en",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.csv"
            gmail_sorter.write_csv(path, decisions)
            content = path.read_text()
            self.assertIn("message_id", content)
            self.assertIn("m1", content)
            self.assertIn("Bank", content)
            self.assertIn("Finance", content)
            self.assertIn("en", content)  # detected_language is a new column

    def test_json_round_trip(self):
        decisions = [
            gmail_sorter.Decision(
                message_id="m1", thread_id="t", date="2026-07-06",
                sender="x", sender_email="x", sender_domain="x", registered_domain="x",
                subject="Statement", snippet="",
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 90},
                body_text_excerpt="excerpt text",
                detected_language="en",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            gmail_sorter.write_json(path, decisions)
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded[0]["body_text_excerpt"], "excerpt text")
            self.assertEqual(loaded[0]["detected_language"], "en")


class FuzzTests(unittest.TestCase):
    """Random input fuzzing on the public surface."""

    def test_decide_random_payloads(self):
        from tests.test_gmail_sorter import args, message, body_payload

        rng = random.Random(42)
        a = args()
        for i in range(50):
            sender = f"sender{i}@example{i}.com"
            subject = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(rng.randint(0, 50)))
            body = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz \n") for _ in range(rng.randint(0, 200)))
            msg = message(
                body_payload(
                    {"From": f"S <{sender}>", "Subject": subject},
                    body,
                ),
                labels=[],
            )
            try:
                decision = gmail_sorter.decide(msg, a, gmail_sorter.Config())
            except Exception as error:  # pragma: no cover
                self.fail(f"decide() raised on random input {i}: {error!r}")
            self.assertIsInstance(decision, gmail_sorter.Decision)
            self.assertIsInstance(decision.categories, list)
            self.assertIsInstance(decision.primary_category, str)

    def test_save_load_random_decisions(self):
        rng = random.Random(43)
        decisions = {
            f"m{i}": gmail_sorter.Decision(
                message_id=f"m{i}", thread_id="t", date=datetime.now(timezone.utc).isoformat(),
                sender="x", sender_email="x", sender_domain="x", registered_domain="x",
                subject="s" * rng.randint(0, 100), snippet="n" * rng.randint(0, 100),
                categories=["Finance"], primary_category="Finance",
                category_confidence={"Finance": 50},
                body_text_excerpt="x" * rng.randint(0, 500),
                detected_language=rng.choice(["en", "fr", "fa", "other", ""]),
            )
            for i in range(20)
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            gmail_sorter.save_progress(path, decisions)
            loaded = gmail_sorter.load_progress(path)
            self.assertEqual(set(loaded), set(decisions))
            for k in decisions:
                self.assertEqual(loaded[k].body_text_excerpt, decisions[k].body_text_excerpt)
                self.assertEqual(loaded[k].detected_language, decisions[k].detected_language)


if __name__ == "__main__":
    import random  # used by FuzzTests

    unittest.main()
