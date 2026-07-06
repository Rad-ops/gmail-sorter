"""SQLite schema migrations for the Gmail sorter state database.

The state database is opened in many places (classify, decide, apply, relabel,
trash rescue audit). Adding a column to an existing table the lazy way — inline
``ALTER TABLE`` at the call site — leads to drift, lost migrations on resume,
and confusing errors when a row written by a newer binary is read by an older
one.

This module is the single place where the schema is defined and evolved.
``migrate()`` is idempotent: it inspects the current state of the database and
applies every pending migration in order, no matter which version of the binary
created the file.

The migration log is stored in the ``schema_migrations`` table:

  - ``version INTEGER PRIMARY KEY`` — monotonic, applied at most once
  - ``applied_at TEXT NOT NULL``  — ISO-8601 UTC timestamp

The current schema version is exposed as :data:`CURRENT_SCHEMA_VERSION`.
``open_state_db`` calls :func:`migrate` before returning the connection, so a
fresh database and a database from any prior release both end up at the same
shape.

Migrations are pure-Python and side-effect-free except for their ``ALTER
TABLE`` / ``CREATE TABLE`` statements on the passed-in connection, so they are
trivial to test against an in-memory SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("sorter.schema")

CURRENT_SCHEMA_VERSION = 3

# Each migration is a callable that takes a connection and applies its DDL.
# Migrations must be idempotent (use IF NOT EXISTS) and must check the current
# state before mutating so they are safe to re-run.
_MIGRATIONS: dict[int, callable] = {}


def _register(version: int):
    """Decorator that registers a migration at a specific schema version."""

    def wrap(fn):
        _MIGRATIONS[version] = fn
        return fn

    return wrap


@_register(1)
def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """v1: the original baseline.

    The v1 schema is "the tables the pre-v0.7 ``open_state_db`` inlined".
    On a fresh database, :func:`_ensure_core_tables` creates these. On a
    database that predates the migration scaffold, those tables already
    exist; this migration is a no-op that just records v1 as applied.
    """


@_register(2)
def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """v2: persist a bounded cleaned-body excerpt to message_features.

    The embedding centroid learning in v0.6 used ``subject + snippet +
    body_category_hits`` for its text. That misses the actual body semantics,
    so the centroids never learned what a Finance message *sounds like*. The
    fix is to persist a privacy-bounded (4000 char) cleaned body excerpt in
    ``message_features.body_text_excerpt`` and embed the real text on the next
    scan.
    """

    conn.execute(
        "ALTER TABLE message_features ADD COLUMN body_text_excerpt TEXT"
    )
    log.info("schema v2: added message_features.body_text_excerpt")


@_register(3)
def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """v3: sender profile time-decay and distinct-categories.

    Adds ``first_seen``, ``last_hits``, and a derived ``category_diversity``
    column so the v0.7 sender profile work can apply a half-life decay and
    surface noisy senders in the dashboard. ``category_diversity`` is a
    computed column managed by the application; the schema reserves the slot
    but does not enforce it.
    """

    for column, ddl_type in (
        ("first_seen", "TEXT"),
        ("last_hits", "TEXT"),
        ("category_diversity", "INTEGER NOT NULL DEFAULT 0"),
    ):
        # SQLite ALTER TABLE ADD COLUMN is idempotent only via PRAGMA check.
        existing = conn.execute("PRAGMA table_info(sender_profile)").fetchall()
        names = {row[1] for row in existing}
        if column not in names:
            conn.execute(f"ALTER TABLE sender_profile ADD COLUMN {column} {ddl_type}")
            log.info("schema v3: added sender_profile.%s", column)


def _ensure_migrations_table(conn: sqlite3.Connection) -> set[int]:
    """Create the migrations ledger and return the set of applied versions."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def _applied_schema_version(conn: sqlite3.Connection) -> int:
    """Read the highest applied version, or 0 when none have been applied."""

    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or row[0] is None:
        return 0
    return int(row[0])


def _ensure_core_tables(conn: sqlite3.Connection) -> None:
    """Create the v1 baseline tables so a fresh database lands at v1.

    The pre-v0.7 ``open_state_db`` inlined these ``CREATE TABLE IF NOT
    EXISTS`` statements. They stay here as the v0 baseline so a brand-new
    state DB has a deterministic shape, and so any future migration that
    assumes the v1 tables can rely on them.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
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
        CREATE TABLE IF NOT EXISTS action_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            stage TEXT NOT NULL,
            action TEXT NOT NULL,
            message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_review (
            domain TEXT PRIMARY KEY,
            registered_domain TEXT,
            status TEXT NOT NULL DEFAULT 'unreviewed',
            note TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sender_profile (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sender_profile_kind ON sender_profile(kind, category)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_features (
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
        CREATE TABLE IF NOT EXISTS category_centroid (
            category TEXT PRIMARY KEY,
            embedding_json TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )


def migrate(conn: sqlite3.Connection, target_version: int = CURRENT_SCHEMA_VERSION) -> int:
    """Apply every pending migration up to ``target_version``.

    Returns the new applied schema version. Safe to call multiple times; the
    ledger in ``schema_migrations`` ensures each version is applied at most
    once.
    """

    _ensure_migrations_table(conn)
    _ensure_core_tables(conn)
    applied = _applied_schema_version(conn)
    if applied >= target_version:
        return applied

    now = datetime.now(timezone.utc).isoformat()
    for version in sorted(_MIGRATIONS):
        if version <= applied or version > target_version:
            continue
        _MIGRATIONS[version](conn)
        # For a pre-existing v1 file the v1 migration is a no-op; still
        # record v1 so the migration ledger reflects the full path.
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now),
        )
        applied = version
    conn.commit()
    log.info("schema migrated to v%d", applied)
    return applied


__all__ = ["CURRENT_SCHEMA_VERSION", "migrate"]
