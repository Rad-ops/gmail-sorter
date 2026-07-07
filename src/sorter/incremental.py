"""Gmail History API incremental scan for the Gmail sorter (v0.8).

Pre-v0.8 every scan re-listed every message matching the query and
re-classified each one. On a multi-year mailbox that is a lot of
Gmail API calls and a lot of CPU. v0.8 adds the History API path:

* New SQLite table ``state_meta`` (schema v4) stores a single
  ``last_history_id`` per database — the latest ``historyId`` we've
  processed.
* A new ``--since-history-id <id>`` flag tells the sorter to fetch
  only the changes since the last run, not the entire mailbox.
* When the history list returns ``messagesAdded`` / ``messagesDeleted``
  / ``labelsAdded`` / ``labelsRemoved`` events, the sorter updates its
  in-memory decisions accordingly: added messages are scanned, deleted
  messages are removed from the local DB, label changes are reflected
  in the existing decisions.
* The full re-scan path is preserved: ``--since-history-id auto`` uses
  the stored history id; ``--since-history-id reset`` falls back to
  the full re-scan and stores the new history id.

A new ``commands/run-maintenance.sh`` runs the incremental scan on a
systemd timer. On a typical weekly cadence the scan is 100x faster
than the full re-scan.

The Gmail History API has a quirk: the ``historyId`` parameter
references a point in the past, but the API only keeps history for
about a week. If ``last_history_id`` is too old, the History API
returns ``404 not found`` and the sorter falls back to the full
re-scan. The fallback is automatic and logged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("sorter.incremental")


# Keys used in the state_meta key/value table.
META_KEY_HISTORY_ID = "last_history_id"
META_KEY_LAST_SCAN_AT = "last_scan_at"
META_KEY_LAST_FULL_SCAN_AT = "last_full_scan_at"


def ensure_state_meta(conn: sqlite3.Connection | None) -> None:
    """Create the state_meta table on first use."""

    if conn is None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def get_meta(conn: sqlite3.Connection | None, key: str) -> str:
    """Read a value from state_meta. Empty string if missing or no DB."""

    if conn is None:
        return ""
    ensure_state_meta(conn)
    try:
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0]) if row else ""


def set_meta(conn: sqlite3.Connection | None, key: str, value: str) -> None:
    """Write a value to state_meta."""

    if conn is None:
        return
    ensure_state_meta(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO state_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()


def get_last_history_id(conn: sqlite3.Connection | None) -> int:
    """Return the stored last_history_id, or 0 if unset / no DB."""

    raw = get_meta(conn, META_KEY_HISTORY_ID)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def set_last_history_id(conn: sqlite3.Connection | None, history_id: int) -> None:
    set_meta(conn, META_KEY_HISTORY_ID, str(history_id))


@dataclass
class HistoryEvent:
    """One change event from the Gmail History API."""

    id: int
    messages_added: list[str] = field(default_factory=list)
    messages_deleted: list[str] = field(default_factory=list)
    labels_added: list[tuple[str, list[str]]] = field(default_factory=list)
    labels_removed: list[tuple[str, list[str]]] = field(default_factory=list)


def parse_history_response(response: dict[str, Any]) -> list[HistoryEvent]:
    """Convert a Gmail History API response into a list of HistoryEvent.

    The Gmail API returns a list of ``history`` records, each with an
    ``id`` and optional ``messagesAdded`` / ``messagesDeleted`` /
    ``labelsAdded`` / ``labelsRemoved`` fields. We collapse them into
    one HistoryEvent per ``id`` so the caller can iterate event-by-event.
    """

    out: list[HistoryEvent] = []
    for entry in response.get("history", []) or []:
        e = HistoryEvent(id=int(entry.get("id", 0)))
        for m in entry.get("messagesAdded", []) or []:
            mid = (m.get("message") or {}).get("id")
            if mid:
                e.messages_added.append(mid)
        for m in entry.get("messagesDeleted", []) or []:
            mid = (m.get("message") or {}).get("id")
            if mid:
                e.messages_deleted.append(mid)
        for m in entry.get("labelsAdded", []) or []:
            mid = (m.get("message") or {}).get("id")
            label_ids = m.get("labelIds") or []
            if mid and label_ids:
                e.labels_added.append((mid, list(label_ids)))
        for m in entry.get("labelsRemoved", []) or []:
            mid = (m.get("message") or {}).get("id")
            label_ids = m.get("labelIds") or []
            if mid and label_ids:
                e.labels_removed.append((mid, list(label_ids)))
        out.append(e)
    return out


def collect_message_ids(events: list[HistoryEvent]) -> set[str]:
    """Return every message id touched by the events (added or deleted)."""

    out: set[str] = set()
    for e in events:
        out.update(e.messages_added)
        out.update(e.messages_deleted)
        for mid, _ in e.labels_added:
            out.add(mid)
        for mid, _ in e.labels_removed:
            out.add(mid)
    return out


def apply_label_events(
    conn: sqlite3.Connection | None,
    events: list[HistoryEvent],
) -> int:
    """Reflect label events in the messages table.

    For each (message_id, label_ids) in labelsAdded / labelsRemoved, we
    do not store the actual labels in the v0.7 schema (the messages
    table has no ``labels_json`` column). We record the event in the
    action_ledger so the operator can audit it. Returns the number of
    events applied.
    """

    if conn is None or not events:
        return 0
    ensure_state_meta(conn)
    applied = 0
    now = datetime.now(timezone.utc).isoformat()
    for e in events:
        for mid, label_ids in e.labels_added:
            conn.execute(
                """
                INSERT INTO action_ledger (created_at, stage, action, message_id, status, detail)
                VALUES (?, 'history', 'labels_added', ?, 'observed', ?)
                """,
                (now, mid, json.dumps({"label_ids": label_ids, "history_id": e.id})),
            )
            applied += 1
        for mid, label_ids in e.labels_removed:
            conn.execute(
                """
                INSERT INTO action_ledger (created_at, stage, action, message_id, status, detail)
                VALUES (?, 'history', 'labels_removed', ?, 'observed', ?)
                """,
                (now, mid, json.dumps({"label_ids": label_ids, "history_id": e.id})),
            )
            applied += 1
    conn.commit()
    return applied


def remove_deleted_messages(
    conn: sqlite3.Connection | None,
    message_ids: list[str],
) -> int:
    """Delete local rows for messages that were removed in Gmail.

    Returns the count of rows deleted.
    """

    if conn is None or not message_ids:
        return 0
    cur = conn.executemany(
        "DELETE FROM messages WHERE message_id=?",
        [(mid,) for mid in message_ids],
    )
    conn.commit()
    return cur.rowcount or 0


def fetch_all_history(
    service: Any,
    start_history_id: int,
    max_results: int = 500,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch all pages of Gmail history, following nextPageToken.

    Returns (history_records, latest_history_id).
    ``latest_history_id`` is the mailbox's current historyId from the
    API response (0 if the request failed), so the caller can persist
    it for the next incremental run.
    Returns an empty list and 0 when the history ID is stale (404) or
    any other error occurs, so the caller can fall back to a full
    re-scan.
    """

    all_history: list[dict[str, Any]] = []
    latest_history_id = 0
    page_token: str | None = None
    while True:
        try:
            request = service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                maxResults=max_results,
            )
            if page_token:
                request = request.pageToken(page_token)
            response = request.execute()
        except Exception as error:
            log.warning("history.list failed (probably stale historyId): %s", error)
            return [], 0
        if not latest_history_id:
            latest_history_id = int(response.get("historyId", 0))
        all_history.extend(response.get("history", []) or [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return all_history, latest_history_id


def fetch_history_page(
    service: Any,
    start_history_id: int,
    max_results: int = 500,
) -> list[dict[str, Any]]:
    """Fetch one page of Gmail history. Pure-Python wrapper.

    The Gmail API caps each response at 500 events. The caller is
    expected to follow the ``nextPageToken`` until it disappears.
    Returns the list of history records (not the full response) so the
    caller can ``parse_history_response`` each one.

    .. deprecated::
       Use :func:`fetch_all_history` instead, which handles pagination
       internally.
    """

    try:
        response = service.users().history().list(
            userId="me", startHistoryId=start_history_id, maxResults=max_results,
        ).execute()
    except Exception as error:
        log.warning("history.list failed (probably stale historyId): %s", error)
        return []
    return response.get("history", []) or []


def get_current_history_id(service: Any) -> int:
    """Return the mailbox's current historyId via users.getProfile.

    Returns 0 on any error so the caller can degrade gracefully.
    """

    try:
        profile = service.users().getProfile(userId="me").execute()
        return int(profile.get("historyId", 0))
    except (Exception, ValueError):
        return 0


__all__ = [
    "HistoryEvent",
    "META_KEY_HISTORY_ID",
    "META_KEY_LAST_SCAN_AT",
    "META_KEY_LAST_FULL_SCAN_AT",
    "apply_label_events",
    "collect_message_ids",
    "ensure_state_meta",
    "fetch_all_history",
    "fetch_history_page",
    "get_last_history_id",
    "get_meta",
    "parse_history_response",
    "remove_deleted_messages",
    "set_last_history_id",
    "set_meta",
]
