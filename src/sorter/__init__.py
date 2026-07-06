"""Gmail sorter package.

Policy data and pure keyword matching live in dedicated modules so the cleanup
rules can be edited and config-driven without touching the Gmail I/O or apply
paths. The original ``gmail_sorter.py`` remains the runnable core and re-exports
these names for backwards compatibility with the companion scripts and tests.
"""

from __future__ import annotations

from . import keywords, policy, schema, lang, ai_learning, learned_weights, thread_features, sender_reputation, incremental

__all__ = ["keywords", "policy", "schema", "lang", "ai_learning", "learned_weights", "thread_features", "sender_reputation", "incremental"]
