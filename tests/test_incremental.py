"""Tests for v0.8 Gmail History API incremental scan."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter import incremental
from sorter.incremental import (
    META_KEY_HISTORY_ID,
    HistoryEvent,
    apply_label_events,
    collect_message_ids,
    ensure_state_meta,
    get_last_history_id,
    get_meta,
    parse_history_response,
    remove_deleted_messages,
    set_last_history_id,
    set_meta,
)


class _FakeGmailService:
    """Minimal mock for the Gmail service used by fetch_all_history."""

    def __init__(self, history_records=None, history_id=0,
                 profile_history_id=0, raise_on_history=False,
                 raise_on_profile=False):
        self._history_records = history_records or []
        self._history_id = history_id
        self._profile_history_id = profile_history_id
        self._raise_on_history = raise_on_history
        self._raise_on_profile = raise_on_profile
        self._page_count_ref = [0]

    def users(self):
        return _FakeUsers(
            self._profile_history_id, self._raise_on_profile,
            self._history_records, self._history_id,
            self._raise_on_history, self._page_count_ref,
        )

    def close(self):
        pass


class _FakeUsers:
    def __init__(self, profile_history_id, raise_on_profile, history_records, history_id, raise_on_history, page_count_ref):
        self._profile_history_id = profile_history_id
        self._raise_on_profile = raise_on_profile
        self._history_records = history_records
        self._history_id = history_id
        self._raise_on_history = raise_on_history
        self._page_count_ref = page_count_ref

    def getProfile(self, userId="me"):
        return _FakeProfileRequest(self._profile_history_id, self._raise_on_profile)

    def history(self):
        return _FakeHistory(
            self._history_records, self._history_id,
            self._raise_on_history, self._page_count_ref,
        )


class _FakeProfileRequest:
    def __init__(self, profile_history_id, raise_on_profile):
        self._profile_history_id = profile_history_id
        self._raise_on_profile = raise_on_profile

    def execute(self):
        if self._raise_on_profile:
            raise Exception("profile error")
        return {"historyId": str(self._profile_history_id)}


class _FakeHistory:
    def __init__(self, records, history_id, raise_on_history, page_count_ref):
        self._records = records
        self._history_id = history_id
        self._raise_on_history = raise_on_history
        self._page_count_ref = page_count_ref

    def list(self, userId="me", startHistoryId=None, maxResults=500):
        if self._raise_on_history:
            return _FakeHistoryRequest([], self._history_id, raise_error=True)
        if isinstance(self._records, list) and self._records and isinstance(self._records[0], list):
            idx = self._page_count_ref[0]
            self._page_count_ref[0] += 1
            page = self._records[idx % len(self._records)]
            has_more = idx + 1 < len(self._records)
            return _FakeHistoryRequest(page, self._history_id, has_more=has_more)
        return _FakeHistoryRequest(self._records, self._history_id)


class _FakeHistoryRequest:
    def __init__(self, records, history_id, has_more=False, raise_error=False):
        self._records = records
        self._history_id = history_id
        self._has_more = has_more
        self._raise_error = raise_error
        self._page_token = None

    def pageToken(self, token):
        self._page_token = token
        return self

    def execute(self):
        if self._raise_error:
            raise Exception("history list error")
        result = {"history": self._records, "historyId": str(self._history_id)}
        if self._has_more:
            result["nextPageToken"] = "token_next"
        return result


class StateMetaTests(unittest.TestCase):
    def test_ensure_state_meta_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            ensure_state_meta(conn)
            ensure_state_meta(conn)  # second call is a no-op
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='state_meta'").fetchone()
            self.assertIsNotNone(row)
            conn.close()

    def test_set_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(get_meta(conn, "foo"), "")
            set_meta(conn, "foo", "bar")
            self.assertEqual(get_meta(conn, "foo"), "bar")
            # Override.
            set_meta(conn, "foo", "baz")
            self.assertEqual(get_meta(conn, "foo"), "baz")
            conn.close()

    def test_get_meta_no_db(self):
        self.assertEqual(get_meta(None, "foo"), "")

    def test_set_meta_no_db(self):
        # No-op.
        set_meta(None, "foo", "bar")

    def test_history_id_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(get_last_history_id(conn), 0)
            set_last_history_id(conn, 12345)
            self.assertEqual(get_last_history_id(conn), 12345)
            conn.close()

    def test_history_id_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            set_meta(conn, META_KEY_HISTORY_ID, "not a number")
            self.assertEqual(get_last_history_id(conn), 0)
            conn.close()


class ParseHistoryResponseTests(unittest.TestCase):
    def test_empty_response(self):
        events = parse_history_response({})
        self.assertEqual(events, [])

    def test_messages_added(self):
        response = {
            "history": [
                {
                    "id": "100",
                    "messagesAdded": [
                        {"message": {"id": "msg1"}},
                        {"message": {"id": "msg2"}},
                    ],
                }
            ]
        }
        events = parse_history_response(response)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, 100)
        self.assertEqual(events[0].messages_added, ["msg1", "msg2"])

    def test_messages_deleted(self):
        response = {
            "history": [
                {
                    "id": "200",
                    "messagesDeleted": [{"message": {"id": "msg3"}}],
                }
            ]
        }
        events = parse_history_response(response)
        self.assertEqual(events[0].messages_deleted, ["msg3"])

    def test_labels_added_and_removed(self):
        response = {
            "history": [
                {
                    "id": "300",
                    "labelsAdded": [{"message": {"id": "msg4"}, "labelIds": ["LBL_FOO"]}],
                    "labelsRemoved": [{"message": {"id": "msg5"}, "labelIds": ["LBL_BAR"]}],
                }
            ]
        }
        events = parse_history_response(response)
        self.assertEqual(events[0].labels_added, [("msg4", ["LBL_FOO"])])
        self.assertEqual(events[0].labels_removed, [("msg5", ["LBL_BAR"])])

    def test_event_with_no_changes(self):
        response = {"history": [{"id": "400"}]}
        events = parse_history_response(response)
        self.assertEqual(events[0].id, 400)
        self.assertEqual(events[0].messages_added, [])
        self.assertEqual(events[0].messages_deleted, [])

    def test_multiple_events(self):
        response = {
            "history": [
                {"id": "1", "messagesAdded": [{"message": {"id": "a"}}]},
                {"id": "2", "messagesAdded": [{"message": {"id": "b"}}]},
            ]
        }
        events = parse_history_response(response)
        self.assertEqual([e.id for e in events], [1, 2])
        self.assertEqual([e.messages_added[0] for e in events], ["a", "b"])

    def test_malformed_entry_skipped(self):
        # An entry missing 'message' must not produce a phantom id.
        response = {
            "history": [
                {
                    "id": "1",
                    "messagesAdded": [
                        {"message": {"id": "good"}},
                        {"message": {}},  # malformed
                    ],
                }
            ]
        }
        events = parse_history_response(response)
        self.assertEqual(events[0].messages_added, ["good"])


class CollectMessageIdsTests(unittest.TestCase):
    def test_collects_all_touched_ids(self):
        events = [
            HistoryEvent(id=1, messages_added=["m1"], messages_deleted=["m2"]),
            HistoryEvent(id=2, labels_added=[("m3", ["L"])], labels_removed=[("m4", ["L"])]),
        ]
        ids = collect_message_ids(events)
        self.assertEqual(ids, {"m1", "m2", "m3", "m4"})

    def test_empty(self):
        self.assertEqual(collect_message_ids([]), set())


class ApplyLabelEventsTests(unittest.TestCase):
    def test_records_action_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            events = [
                HistoryEvent(
                    id=100,
                    labels_added=[("m1", ["LBL_FOO"])],
                    labels_removed=[("m2", ["LBL_BAR"])],
                ),
            ]
            applied = apply_label_events(conn, events)
            self.assertEqual(applied, 2)
            rows = conn.execute("SELECT action, message_id, status FROM action_ledger").fetchall()
            self.assertEqual(len(rows), 2)
            actions = {row[0] for row in rows}
            self.assertEqual(actions, {"labels_added", "labels_removed"})
            conn.close()

    def test_no_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(apply_label_events(conn, []), 0)
            conn.close()

    def test_no_db(self):
        self.assertEqual(apply_label_events(None, []), 0)


class RemoveDeletedMessagesTests(unittest.TestCase):
    def test_removes_specified_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            # Seed three messages.
            for mid in ["m1", "m2", "m3"]:
                conn.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (mid, "t", "2026-07-06", "x@x.com", "x@x.com", "x", "x", "S", "[]", "[]", 0, 0, 0, 0, 0, 0, 0, 0, "normal", "no", "", "{}", "2026-07-06"),
                )
            conn.commit()
            removed = remove_deleted_messages(conn, ["m1", "m3"])
            self.assertEqual(removed, 2)
            remaining = {row[0] for row in conn.execute("SELECT message_id FROM messages").fetchall()}
            self.assertEqual(remaining, {"m2"})
            conn.close()

    def test_empty_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(remove_deleted_messages(conn, []), 0)
            conn.close()

    def test_no_db(self):
        self.assertEqual(remove_deleted_messages(None, ["m1"]), 0)


class FetchAllHistoryTests(unittest.TestCase):
    """Tests for fetch_all_history with a mock Gmail service."""

    def test_returns_empty_on_error(self):
        history, latest_id = incremental.fetch_all_history(
            _FakeGmailService(raise_on_history=True), 12345,
        )
        self.assertEqual(history, [])
        self.assertEqual(latest_id, 0)

    def test_single_page(self):
        service = _FakeGmailService(
            history_records=[{"id": 100, "messagesAdded": [{"message": {"id": "m1"}}]}],
            history_id=99999,
        )
        history, latest_id = incremental.fetch_all_history(service, 12345)
        self.assertEqual(len(history), 1)
        self.assertEqual(latest_id, 99999)

    def test_multi_page(self):
        service = _FakeGmailService(
            history_records=[
                [{"id": 100, "messagesAdded": [{"message": {"id": "m1"}}]}],
                [{"id": 200, "messagesAdded": [{"message": {"id": "m2"}}]}],
            ],
            history_id=99999,
        )
        history, latest_id = incremental.fetch_all_history(service, 12345)
        self.assertEqual(len(history), 2)
        self.assertEqual(latest_id, 99999)


class GetCurrentHistoryIdTests(unittest.TestCase):
    """Tests for get_current_history_id."""

    def test_returns_0_on_error(self):
        service = _FakeGmailService(raise_on_profile=True)
        self.assertEqual(incremental.get_current_history_id(service), 0)

    def test_returns_history_id(self):
        service = _FakeGmailService(profile_history_id=77777)
        self.assertEqual(incremental.get_current_history_id(service), 77777)


if __name__ == "__main__":
    unittest.main()
