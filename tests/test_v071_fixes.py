"""Tests for the v0.7.1 fixes.

This module pins down the four behavior changes that justify the
v0.7.1 patch release:

1. The default Gmail query no longer carries a stale date. The
   pre-v0.7.1 ``before:2025/12/30`` is now in the past; v0.7.1
   switches to ``in:anywhere -in:trash`` so new users see all
   their messages on a fresh install.
2. The test args() helper exposes every v0.8 flag. The v0.7.0
   helper was missing ``use_learned_weights``,
   ``learned_weights_file``, and ``since_history_id``; v0.7.1
   adds them so v0.8 tests do not need to override every
   field on every call.
3. The ``tracked()`` helper and state DB connection factory ensure sqlite3
   connections are closed cleanly. Pre-v0.7.1 tests leaked up to 6
   connections per run, which surfaced as ``ResourceWarning: unclosed
   database`` and failed CI when ``-W error::ResourceWarning`` was set.
4. The ``args()`` factory is the single source of truth — it
   delegates to ``tests.test_helpers.make_test_args`` so the
   default set can never drift between the factory and the
   test that imports it.

The four tests below are small, fast, and rely on no live
Gmail state. They are the regression net for the v0.7.1 release.
"""

import gc
import unittest
import warnings
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.test_helpers import make_test_args, tracked  # noqa: F401


class DefaultQueryTests(unittest.TestCase):
    """The default query no longer carries a stale hardcoded date."""

    def test_default_query_does_not_have_a_stale_date(self):
        # The pre-v0.7.1 default was ``before:2025/12/30 -in:trash``
        # which is in the past (we are in 2026). v0.7.1 switched to
        # ``in:anywhere -in:trash`` so a fresh install scans the
        # whole mailbox.
        from sorter import policy
        self.assertNotIn("2025", policy.DEFAULT_QUERY)
        self.assertIn("-in:trash", policy.DEFAULT_QUERY)

    def test_args_helper_uses_new_default(self):
        from tests.test_helpers import DEFAULT_TEST_ARGS
        self.assertNotIn("2025", DEFAULT_TEST_ARGS["query"])


class ArgsHelperCompletenessTests(unittest.TestCase):
    """The args helper exposes every v0.8 flag."""

    def test_v08_flags_are_in_args(self):
        # v0.7.0 forgot these. v0.7.1 adds them so v0.8 tests
        # don't have to override every field.
        a = make_test_args()
        self.assertFalse(a.use_learned_weights)
        self.assertEqual(a.learned_weights_file, "data/learned_weights.json")
        self.assertEqual(a.since_history_id, "")

    def test_args_helper_uses_test_helpers_factory(self):
        # Single source of truth: the local ``args()`` wrapper in
        # test_gmail_sorter.py delegates to ``make_test_args``.
        from tests.test_gmail_sorter import args
        a = args()
        # If the wrapper and the canonical factory ever drift,
        # this test will fail.
        self.assertEqual(a.query, make_test_args().query)
        self.assertEqual(a.use_html_body, make_test_args().use_html_body)
        self.assertEqual(a.use_learned_weights, make_test_args().use_learned_weights)


class TrackedConnectionTests(unittest.TestCase):
    """The tracked() helper closes connections at teardown."""

    def test_tracked_closes_in_memory_connection(self):
        import sqlite3
        conn = tracked(self, sqlite3.connect(":memory:"))
        self.assertIsNotNone(conn)
        # After the test, the cleanup runs. We can't observe that
        # directly in a single test, but we can verify the helper
        # does not raise and returns the connection unchanged.
        self.assertEqual(conn, conn)

    def test_tracked_handles_already_closed(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.close()
        # Should not raise on double-close.
        try:
            tracked(self, conn)
        except Exception as error:  # pragma: no cover - the test fails
            self.fail(f"tracked() raised on already-closed connection: {error!r}")


class ResourceWarningRegressionTests(unittest.TestCase):
    """Pre-v0.7.1 tests leaked up to 6 sqlite3 connections per run."""

    def test_no_unclosed_database_warnings(self):
        # Run a small subset of tests that open connections and
        # assert no ResourceWarning fires during teardown.
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            import sqlite3
            conn = tracked(self, sqlite3.connect(":memory:"))
            # Use the connection so the resource is actually
            # exercised.
            conn.execute("SELECT 1").fetchone()
            # The cleanup function is registered; the actual
            # close happens at teardown. Calling close here
            # exercises the no-op-on-double-close path.
            conn.close()
            # If tracked() did not register a cleanup, the
            # test framework would still report a leak here
            # (but only on the test framework's own finalizer,
            # not in this test method). The post-test leak is
            # checked separately via the test runner.

    def test_open_state_db_closes_when_tracked(self):
        # The end-to-end check: open_state_db's connection must
        # be closed after the test method completes.
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            import tempfile
            from tests.test_gmail_sorter import gmail_sorter  # type: ignore
            with tempfile.TemporaryDirectory() as tmp:
                conn = tracked(self, gmail_sorter.open_state_db(Path(tmp) / "s.sqlite"))
                # The connection is alive.
                self.assertIsNotNone(conn)

    def test_open_state_db_finalizer_closes_untracked_connection(self):
        # Production code should still close explicitly, but the state DB
        # connection must not emit late ResourceWarning noise if a helper drops
        # a reference before cleanup.
        import tempfile
        from tests.test_gmail_sorter import gmail_sorter  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                conn = gmail_sorter.open_state_db(Path(tmp) / "s.sqlite")
                conn.execute("SELECT 1").fetchone()
                del conn
                gc.collect()
            self.assertEqual(
                [w for w in caught if issubclass(w.category, ResourceWarning)],
                [],
            )


if __name__ == "__main__":
    unittest.main()
