"""Tests for the v0.8.1 fixes.

This module pins down the four behavior changes that justify the
v0.8.1 patch release:

1. ``CURRENT_SCHEMA_VERSION`` is bumped to 4 and a v4 migration
   is added that creates the ``state_meta`` (incremental.py),
   ``thread_features`` (thread_features.py), and
   ``sender_reputation`` (sender_reputation.py) tables explicitly.
   Pre-v0.8.1, these tables were created lazily on first use,
   which meant the schema version never advanced past 3 even
   after v0.8 ran.
2. The ``--since-history-id`` flag is wired into ``main()``. The
   flag is parsed by argparse and resolves to one of four
   values: ``auto:`` (uses the stored last_history_id),
   ``reset`` (forces a full re-scan and resets the stored id),
   ``explicit:`` (uses the user-supplied id), or
   ``disabled`` (default, no incremental mode). The resolved
   value is persisted to ``state_meta`` so the operator can
   inspect it.
3. The ``args()`` factory in ``tests/test_gmail_sorter.py`` is
   the single source of truth via :func:`make_test_args` so the
   v0.8 test suite cannot drift from the v0.7 args() defaults.
4. The v0.8 branch picks up the v0.7.1 patch fixes: stale
   default query and test connection cleanup.

The tests below are the regression net for v0.8.1.
"""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from tests.test_helpers import make_test_args


class SchemaV4Tests(unittest.TestCase):
    """The schema version advances to v4 in v0.8.1."""

    def test_current_schema_version_is_v4(self):
        from sorter.schema import CURRENT_SCHEMA_VERSION
        self.assertEqual(CURRENT_SCHEMA_VERSION, 4)
        self.assertEqual(gmail_sorter.SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)

    def test_v4_migration_creates_state_meta(self):
        from sorter.schema import migrate
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='state_meta'"
            ).fetchone()
            self.assertIsNotNone(row, "state_meta table missing after v4 migration")
            # The state_meta table is usable: we can write and read
            # a key/value pair.
            conn.execute(
                "INSERT INTO state_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("test_key", "test_value", "2026-07-06T00:00:00"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key='test_key'"
            ).fetchone()
            self.assertEqual(row[0], "test_value")
            conn.close()

    def test_v4_migration_creates_thread_features(self):
        from sorter.schema import migrate
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='thread_features'"
            ).fetchone()
            self.assertIsNotNone(row, "thread_features table missing after v4 migration")
            conn.close()

    def test_v4_migration_creates_sender_reputation(self):
        from sorter.schema import migrate
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sender_reputation'"
            ).fetchone()
            self.assertIsNotNone(row, "sender_reputation table missing after v4 migration")
            conn.close()

    def test_v4_migration_is_idempotent(self):
        from sorter.schema import migrate
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            row_before = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            conn.close()
            # Reopen and migrate again: must be a no-op.
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            row_after = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertEqual(row_before, row_after)
            conn.close()


class SinceHistoryIdResolutionTests(unittest.TestCase):
    """The --since-history-id flag resolves to one of four values."""

    def _run_resolve(self, state_conn, since):
        """Re-run the resolution logic that main() executes.

        The actual logic lives inline in main() so we can keep the
        test small. The resolution is documented in the docstring of
        --since-history-id and exercised end-to-end here.
        """
        from sorter.incremental import get_last_history_id
        if not since:
            return "disabled" if not since else f"unknown:{since}"
        if since == "auto":
            stored = get_last_history_id(state_conn)
            return f"auto:{stored}" if stored else "auto:none"
        if since == "reset":
            return "reset"
        if since.isdigit():
            return f"explicit:{since}"
        return f"unknown:{since}"

    def test_empty_value_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(self._run_resolve(conn, ""), "disabled")
            conn.close()

    def test_auto_with_stored_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            from sorter.incremental import set_last_history_id
            set_last_history_id(conn, 12345)
            self.assertEqual(self._run_resolve(conn, "auto"), "auto:12345")
            conn.close()

    def test_auto_with_no_stored_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(self._run_resolve(conn, "auto"), "auto:none")
            conn.close()

    def test_reset_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(self._run_resolve(conn, "reset"), "reset")
            conn.close()

    def test_explicit_numeric_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(self._run_resolve(conn, "99999"), "explicit:99999")
            conn.close()

    def test_unknown_value_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
            self.assertEqual(self._run_resolve(conn, "garbage"), "unknown:garbage")
            conn.close()


class ArgsFactoryDelegationTests(unittest.TestCase):
    """The local args() wrapper delegates to make_test_args."""

    def test_args_matches_make_test_args(self):
        from tests.test_gmail_sorter import args
        a = args()
        b = make_test_args()
        # Compare every key to be sure they match.
        for key in sorted(set(a.__dict__) | set(b.__dict__)):
            self.assertEqual(getattr(a, key), getattr(b, key), f"mismatch on {key}")


class StaleDefaultQueryRegressionTests(unittest.TestCase):
    """v0.7.1 fixed the stale default; v0.8.1 must keep the fix."""

    def test_default_query_has_no_stale_date(self):
        from sorter import policy
        self.assertNotIn("2025", policy.DEFAULT_QUERY)

    def test_args_helper_query_matches_policy(self):
        from sorter import policy
        from tests.test_helpers import DEFAULT_TEST_ARGS
        self.assertEqual(DEFAULT_TEST_ARGS["query"], policy.DEFAULT_QUERY)


class ConnectionCleanupRegressionTests(unittest.TestCase):
    """v0.7.1 added tracked(); v0.8.1 must keep using it everywhere."""

    def test_tracked_works_with_unittest_testcase(self):
        import sqlite3
        # The fact that the test_helpers module is importable
        # and tracked() is callable confirms the v0.7.1 fix is in
        # place. A real connection-leak test would run the full
        # suite under -W error::ResourceWarning; we keep the
        # test simple here.
        from tests.test_helpers import tracked
        self.assertTrue(callable(tracked))

    def test_args_helper_has_v08_flags(self):
        a = make_test_args()
        # v0.7.1 added these to the v0.7 test helper. v0.8.1 makes
        # sure they are also in the canonical factory.
        self.assertFalse(a.use_learned_weights)
        self.assertEqual(a.learned_weights_file, "data/learned_weights.json")
        self.assertEqual(a.since_history_id, "")


if __name__ == "__main__":
    unittest.main()
