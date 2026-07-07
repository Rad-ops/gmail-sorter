"""Test helpers shared across the test suite.

v0.7.1: this module centralizes the small utilities that used to
be copy-pasted across the test files: a tracked sqlite3 connection
that auto-closes on tearDown, a small fake Gmail service stub for
integration tests, and the standard args() factory.

Centralizing the connection tracker means individual test modules
stop leaking ``ResourceWarning: unclosed database`` errors. The
pattern is::

    from tests.test_helpers import tracked

    class MyTests(unittest.TestCase):
        def test_something(self):
            conn = tracked(self, sqlite3.connect(":memory:"))
            ...

The module-level ``tracked(test_case, conn)`` helper uses
``addCleanup`` so it works on any ``unittest.TestCase`` subclass
without requiring a particular base class.
"""

from __future__ import annotations

import argparse
import unittest
from typing import Any


# Default kwargs for the args() factory used by decide() tests. The
# flags here mirror the v0.7 / v0.8 CLI surface so individual tests
# don't have to override every flag on every call.
DEFAULT_TEST_ARGS: dict[str, Any] = {
    "ad_threshold": 65,
    "archive_threshold": 65,
    "archive_min_age_days": 0,
    "archive_skip_unread": False,
    "trash_threshold": 90,
    "pre_2020_trash_threshold": 75,
    "stage": "classify",
    "trash_obvious_ads": False,
    "i_understand_trash": False,
    "scan": "metadata",
    "use_sender_profiles": True,
    "sender_profiles": {},
    "sender_profile_min_weight": 6,
    "sender_profile_floor": 65,
    "sender_profile_half_life_days": 180,
    "label_confidence": 50,
    "max_labels_per_message": 3,
    "cached_body_features": {},
    "relabel_run_id": "",
    "undo_relabel": "",
    "relabel_since_date": "",
    "relabel_label": "",
    "use_thread_aware": False,
    "thread_dominant_categories": {},
    "use_thread_modeling": False,
    "thread_features": {},
    "use_sender_reputation": False,
    "sender_reputation": {},
    "_embedding_backend": None,
    "category_centroids": {},
    "retries": 5,
    "retry_sleep": 5.0,
    "batch_size": 100,
    "apply_progress_every": 100,
    "max_trash_total": 0,
    "max_trash_per_domain": 0,
    "canary_limit": 0,
    "max_archive_total": 0,
    "max_archive_per_domain": 0,
    "archive_canary_limit": 0,
    "prune_empty_labels": False,
    "ai_merge_min_confidence": 0.7,
    "ai_merge_min_removal_confidence": 0.85,
    "no_ai_learning": False,
    "embedding_endpoint": "http://127.0.0.1:8080/v1/embeddings",
    "embedding_model": "local",
    "embedding_st_model": "",
    "embedding_confidence_floor": 70,
    "ai_review_threshold": 75,
    "ai_review_file": "data/label_review_packets.jsonl",
    "merge_ai_labels": False,
    "export_ai_review": False,
    "ai_review_only": False,
    "refresh_existing": False,
    "refresh_after_days": 7,
    "save_every": 250,
    "apply": False,
    "http_timeout": 120.0,
    "workers": 8,
    "sleep": 0.05,
    "disable_state_db": False,
    "use_html_body": True,
    "use_learned_weights": False,
    "learned_weights_file": "data/learned_weights.json",
    "since_history_id": "",
    "query": "in:anywhere -in:trash",
}


def make_test_args(**overrides: Any) -> argparse.Namespace:
    """Build a minimal argparse-like object for decide() tests.

    Tests that need a specific flag pass it as a keyword argument.
    The factory mirrors the v0.7 / v0.8 CLI surface so a test that
    didn't exist when the flag was added still has a sensible default
    to fall back to.
    """

    defaults = dict(DEFAULT_TEST_ARGS)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TrackedConnectionsTestCase(unittest.TestCase):
    """TestCase that auto-closes every sqlite3 connection it sees.

    v0.7.1: previously each test that opened a connection had to
    remember to call ``conn.close()`` at the end. Forgetting
    produced ``ResourceWarning: unclosed database`` noise that
    failed CI when ``-W error::ResourceWarning`` was set. The
    ``tracked()`` helper below registers a connection for cleanup
    on tearDown, so tests can be written without boilerplate and
    leaks never escape.

    Test classes that already inherit from ``unittest.TestCase``
    can use the module-level :func:`tracked` helper without
    inheriting from this class.
    """

    def tracked(self, conn: Any) -> Any:
        """Register a sqlite3 connection for cleanup at tearDown.

        Returns the connection unchanged so the call can replace
        the inline ``conn = sqlite3.connect(...)`` pattern.
        """

        self.addCleanup(self._close_quietly, conn)
        return conn

    @staticmethod
    def _close_quietly(conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            pass


def tracked(test_case: unittest.TestCase, conn: Any) -> Any:
    """Register a sqlite3 connection for cleanup on ``test_case``.

    Module-level helper for test classes that don't (or can't)
    inherit from :class:`TrackedConnectionsTestCase`. Typical use::

        from tests.test_helpers import tracked

        class MyTests(unittest.TestCase):
            def test_something(self):
                conn = tracked(self, sqlite3.connect(":memory:"))
                ...

    Returns the connection unchanged so the call can replace
    the inline ``conn = sqlite3.connect(...)`` pattern.
    """

    if not hasattr(test_case, "addCleanup"):
        raise TypeError(
            "tracked() requires a unittest.TestCase instance as its "
            "first argument; got %r" % (test_case,)
        )
    test_case.addCleanup(_close_quietly, conn)
    return conn


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - defensive
        pass
