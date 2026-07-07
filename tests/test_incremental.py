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


if __name__ == "__main__":
    unittest.main()
