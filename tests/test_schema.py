"""Schema migration tests for v0.7.

These tests exercise the migration scaffold in :mod:`sorter.schema` end-to-end:
- A fresh database lands at the current schema version with the v1 baseline
  tables present.
- A database that was written by an older binary (no ``body_text_excerpt``
  column, no ``first_seen`` column) migrates to the current version and the
  new columns appear.
- Migrations are idempotent: calling :func:`migrate` again is a no-op.
- The :data:`CURRENT_SCHEMA_VERSION` matches what the rest of the codebase
  reports via ``gmail_sorter.SCHEMA_VERSION``.
"""

import sqlite3
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from sorter.schema import CURRENT_SCHEMA_VERSION, migrate


class SchemaMigrationTests(unittest.TestCase):
    """Regression tests for sorter.schema.migrate()."""

    def _tracked(self, conn):
        """Register a connection for automatic cleanup at tearDown.

        v0.7.1: every test that opens a sqlite3 connection must
        register it here so the test runner closes it before
        ``-W error::ResourceWarning`` (used in CI) flags it as a leak.
        Tests that omit this trip the resource warning at the end
        of the run.
        """

        self.addCleanup(conn.close)
        return conn

    def test_current_schema_version_is_published(self):
        # The codebase and the migration module must agree on the version.
        self.assertEqual(gmail_sorter.SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 3)

    def test_fresh_db_lands_at_current_version(self):
        conn = self._tracked(sqlite3.connect(":memory:"))
        applied = migrate(conn)
        self.assertEqual(applied, CURRENT_SCHEMA_VERSION)
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        self.assertEqual([row[0] for row in rows], list(range(1, CURRENT_SCHEMA_VERSION + 1)))

    def test_fresh_db_has_v1_baseline_tables(self):
        conn = self._tracked(sqlite3.connect(":memory:"))
        migrate(conn)
        for table in (
            "messages",
            "action_ledger",
            "domain_review",
            "sender_profile",
            "message_features",
            "category_centroid",
            "schema_migrations",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            self.assertIsNotNone(row, f"missing baseline table: {table}")

    def test_migrate_is_idempotent(self):
        conn = self._tracked(sqlite3.connect(":memory:"))
        first = migrate(conn)
        second = migrate(conn)
        third = migrate(conn)
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_old_v1_db_migrates_to_current(self):
        # Simulate a state DB written by an older binary: baseline tables
        # without the v2 body_text_excerpt column or the v3 sender_profile
        # decay columns. The migrator must add them.
        conn = self._tracked(sqlite3.connect(":memory:"))
        conn.execute(
            """
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                date TEXT,
                sender TEXT,
                sender_email TEXT,
                sender_domain TEXT,
                registered_domain TEXT,
                subject TEXT,
                categories_json TEXT NOT NULL,
                planned_actions_json TEXT NOT NULL,
                ad_confidence INTEGER NOT NULL,
                protected INTEGER NOT NULL,
                perfect_ad_match INTEGER NOT NULL,
                has_attachment INTEGER NOT NULL,
                has_real_attachment INTEGER NOT NULL,
                attachment_count INTEGER NOT NULL,
                inline_attachment_count INTEGER NOT NULL,
                message_size_estimate INTEGER NOT NULL,
                review_priority TEXT,
                action_done TEXT,
                scanned_at TEXT,
                decision_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE message_features (
                message_id TEXT PRIMARY KEY,
                body_len INTEGER NOT NULL DEFAULT 0,
                body_category_hits_json TEXT NOT NULL DEFAULT '[]',
                body_unsubscribe_count INTEGER NOT NULL DEFAULT 0,
                scan_mode TEXT NOT NULL DEFAULT 'metadata',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE sender_profile (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                category TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                protected_hits INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # v2: body_text_excerpt must be added.
        # v3: first_seen, last_hits, category_diversity must be added.
        self._tracked(conn)
        applied = migrate(conn)
        self.assertEqual(applied, CURRENT_SCHEMA_VERSION)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(message_features)").fetchall()}
        self.assertIn("body_text_excerpt", columns)
        sp_columns = {row[1] for row in conn.execute("PRAGMA table_info(sender_profile)").fetchall()}
        for col in ("first_seen", "last_hits", "category_diversity"):
            self.assertIn(col, sp_columns, f"sender_profile missing {col}")

    def test_open_state_db_returns_migrated_connection(self):
        # The end-to-end path: open_state_db must leave the DB at the
        # current schema version on both fresh and pre-existing files.
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            conn = gmail_sorter.open_state_db(db_path)
            self._tracked(conn)
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertEqual(row[0], CURRENT_SCHEMA_VERSION)
            # Re-open: should be a no-op, version unchanged.
            conn2 = gmail_sorter.open_state_db(db_path)
            self._tracked(conn2)
            row2 = conn2.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertEqual(row2[0], CURRENT_SCHEMA_VERSION)

    def test_open_state_db_is_backward_compatible_with_v1_files(self):
        # Write a v1-style file by hand and confirm open_state_db migrates it.
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            seed = sqlite3.connect(str(db_path))
            seed.execute(
                """
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    date TEXT,
                    sender TEXT,
                    sender_email TEXT,
                    sender_domain TEXT,
                    registered_domain TEXT,
                    subject TEXT,
                    categories_json TEXT NOT NULL,
                    planned_actions_json TEXT NOT NULL,
                    ad_confidence INTEGER NOT NULL,
                    protected INTEGER NOT NULL,
                    perfect_ad_match INTEGER NOT NULL,
                    has_attachment INTEGER NOT NULL,
                    has_real_attachment INTEGER NOT NULL,
                    attachment_count INTEGER NOT NULL,
                    inline_attachment_count INTEGER NOT NULL,
                    message_size_estimate INTEGER NOT NULL,
                    review_priority TEXT,
                    action_done TEXT,
                    scanned_at TEXT,
                    decision_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            seed.execute(
                """
                CREATE TABLE message_features (
                    message_id TEXT PRIMARY KEY,
                    body_len INTEGER NOT NULL DEFAULT 0,
                    body_category_hits_json TEXT NOT NULL DEFAULT '[]',
                    body_unsubscribe_count INTEGER NOT NULL DEFAULT 0,
                    scan_mode TEXT NOT NULL DEFAULT 'metadata',
                    updated_at TEXT NOT NULL
                )
                """
            )
            seed.execute(
                """
                CREATE TABLE sender_profile (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    protected_hits INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            seed.commit()
            seed.close()
            conn = gmail_sorter.open_state_db(db_path)
            self._tracked(conn)
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            self.assertEqual(row[0], CURRENT_SCHEMA_VERSION)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(message_features)").fetchall()}
            self.assertIn("body_text_excerpt", cols)


if __name__ == "__main__":
    unittest.main()
