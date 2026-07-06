"""Optional YAML policy overrides (v0.7 with per-language overlays).

The keyword lists and rules in :mod:`sorter.policy` are the defaults. A user can
drop a ``config/policy.yaml`` next to the sorter to override specific keyword
groups or thresholds without editing code. v0.7 adds two per-language overlays:

  * ``config/policy.fr.yaml`` — French keywords appended to the matching English
    category list when a message is detected as French.
  * ``config/policy.fa.yaml`` — Farsi keywords appended to the matching English
    category list when a message is detected as Farsi.

The overlays are **additive** by default: a French keyword for "permis de
travail" extends :data:`policy.IMMIGRATION_KEYWORDS` so a French IRCC email
scores high on Priority Immigration exactly the way an English IRCC email
does. An overlay entry ``replace: true`` switches to a per-language
*replacement* (e.g. the Farsi "social" list could replace the English one
when the user has curated a smaller Farsi-only vocabulary). Replacement is
opt-in because the sorter never wants to lose English coverage on
code-mixed mail.

The loader is intentionally permissive: a missing file, a missing key, or a
malformed YAML section is logged and ignored so the tool keeps running on
the built-in defaults. The active language overlay is applied to the policy
*at run time* by :func:`activate_language_overlay`, not eagerly on import,
so the right overlay is picked per message inside ``decide()`` and reverted
afterward.

Requires PyYAML only if a policy file is actually present; the core tool
does not hard-depend on YAML for normal runs.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import policy

log = logging.getLogger("sorter.config")


# Files the loader looks for, in this order. Each file is optional.
POLICY_FILES = {
    "en": "policy.yaml",
    "fr": "policy.fr.yaml",
    "fa": "policy.fa.yaml",
}


def load_policy_overrides(path: Path) -> dict[str, Any]:
    """Read config/policy.yaml into a dict, or return {} when unavailable.

    This is the English overrides file. v0.7 keeps the original name so
    users who already curated ``policy.yaml`` keep their overrides working.
    """

    if not path.exists():
        return {}
    return _read_yaml(path)


def load_language_overlay(config_dir: Path, language: str) -> dict[str, Any]:
    """Read ``config/policy.<lang>.yaml`` for the given language, or ``{}``.

    Unknown language codes return ``{}`` so the caller can fall back to the
    English keyword set. The function is silent on missing files: the
    language-specific file is opt-in.
    """

    if language not in POLICY_FILES or language == "en":
        return {}
    path = config_dir / POLICY_FILES[language]
    if not path.exists():
        return {}
    return _read_yaml(path)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("policy file %s found but PyYAML is not installed; using built-in defaults.", path)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as error:  # pragma: no cover - defensive
        log.warning("Failed to parse %s: %s; using built-in defaults.", path, error)
        return {}
    if not isinstance(data, dict):
        log.warning("%s did not parse to a mapping; using built-in defaults.", path)
        return {}
    return data


def apply_overrides(overrides: dict[str, Any]) -> None:
    """Mutate sorter.policy lists/defaults in place from a parsed overrides dict.

    Supported keys (all optional): any keyword-group name matching a module
    attribute on :mod:`sorter.policy` (e.g. ``immigration_keywords``) replaces
    that list, and ``thresholds`` updates :data:`sorter.policy.DEFAULTS`.
    """

    if not overrides:
        return
    for key, value in overrides.items():
        if key == "thresholds" and isinstance(value, dict):
            policy.DEFAULTS.update({k: v for k, v in value.items() if isinstance(v, (int, float))})
            continue
        attr = key.upper()
        if hasattr(policy, attr) and isinstance(value, list):
            setattr(policy, attr, [str(item) for item in value])
            log.info("policy override: %s = %d items", attr, len(value))


# A small registry mapping policy attribute name -> attribute on sorter.policy.
# We resolve the actual attribute object at apply time so the overlay survives
# the apply_overrides call on the English file (which may have replaced the
# list contents).
_KEYWORD_GROUPS: tuple[str, ...] = (
    "AD_SUBJECT_KEYWORDS",
    "AD_BODY_KEYWORDS",
    "AD_SENDER_KEYWORDS",
    "TRANSACTIONAL_KEYWORDS",
    "IMMIGRATION_KEYWORDS",
    "STUDIES_KEYWORDS",
)


# Categories the overlay can extend. The overlay's "categories" key maps a
# category name to a {keywords: [...], replace: bool} block. The category
# name is matched against CATEGORY_RULES[*][0].
def _category_keyword_attr(category: str) -> str | None:
    """Return the policy attribute name to extend for a category, or None.

    Some categories share a single keyword pool (Priority Immigration ->
    IMMIGRATION_KEYWORDS). For categories that don't have a shared pool we
    extend the corresponding ``CATEGORY_RULES`` entry by *injecting* the new
    keywords directly into the rule. That mutation is tracked in
    :func:`activate_language_overlay` so we can restore the previous state.
    """

    mapping = {
        "Priority Immigration": "IMMIGRATION_KEYWORDS",
        "Priority Studies": "STUDIES_KEYWORDS",
        "Finance": None,
        "Receipts Orders": None,
        "Account Security": None,
        "Travel": None,
        "Health": None,
        "Government Legal": None,
        "Work School": None,
        "Social": None,
        "Subscriptions": None,
        "Shopping": None,
        "Job Search": None,
        "Housing": None,
        "Utilities": None,
        "Insurance": None,
        "Crypto Finance Risk": None,
        "Old Account Evidence": None,
    }
    return mapping.get(category)


def _restore_rules() -> list[tuple[tuple, tuple]]:
    """Return a deep copy of the current CATEGORY_RULES for restoration."""

    return [(name, list(keywords), list(exclusions)) for name, keywords, exclusions in policy.CATEGORY_RULES]


def activate_language_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply a language overlay to sorter.policy and return a restoration token.

    The overlay dict format::

        categories:
          Priority Immigration:
            keywords: ["permis de travail", "étude"]
            replace: false   # default; append to the existing list
          Finance:
            keywords: ["virement", "relevé"]
            replace: true    # replace this category's keywords for this language

    For categories that have a shared keyword pool
    (:data:`_category_keyword_attr` returns a string), the new keywords are
    appended to the existing list (or replace it). For categories without a
    shared pool, the new keywords are injected into the matching
    ``CATEGORY_RULES`` entry's keyword list.

    The return value is a token that :func:`restore_policy` uses to undo the
    overlay. The sorter is single-threaded for the duration of one message
    so a save/restore around ``decide()`` is enough.
    """

    if not overlay:
        return {"categories": {}, "rules_snapshot": (), "keyword_pool_snapshots": {}}
    rules_snapshot = _restore_rules()
    keyword_pool_snapshots: dict[str, list[str]] = {}
    restored: dict[str, Any] = {"categories": {}, "rules_snapshot": rules_snapshot, "keyword_pool_snapshots": keyword_pool_snapshots}

    categories = overlay.get("categories") or {}
    if not isinstance(categories, dict):
        log.warning("language overlay 'categories' is not a mapping; ignoring")
        return restored

    for category, spec in categories.items():
        if not isinstance(spec, dict):
            continue
        new_keywords = [str(k) for k in spec.get("keywords") or []]
        replace = bool(spec.get("replace", False))
        attr = _category_keyword_attr(category)
        if attr and hasattr(policy, attr):
            current = list(getattr(policy, attr) or [])
            keyword_pool_snapshots.setdefault(attr, current)
            if replace:
                setattr(policy, attr, new_keywords)
                restored["categories"][category] = ("pool_replace", attr, current)
            else:
                setattr(policy, attr, current + [k for k in new_keywords if k not in current])
                restored["categories"][category] = ("pool_extend", attr, current)
            continue
        # Otherwise inject into CATEGORY_RULES.
        for index, (name, keywords, exclusions) in enumerate(policy.CATEGORY_RULES):
            if name != category:
                continue
            new_rules = [(n, list(k), list(e)) for n, k, e in policy.CATEGORY_RULES]
            current = list(new_rules[index][1])
            if replace:
                new_rules[index] = (name, new_keywords, exclusions)
            else:
                merged = current + [k for k in new_keywords if k not in current]
                new_rules[index] = (name, merged, exclusions)
            policy.CATEGORY_RULES[:] = new_rules
            restored["categories"][category] = ("rule_inject", index, list(current))
            break

    return restored


def restore_policy(token: dict[str, Any]) -> None:
    """Undo a language overlay previously applied with :func:`activate_language_overlay`."""

    if not token:
        return
    keyword_pool_snapshots: dict[str, list[str]] = token.get("keyword_pool_snapshots") or {}
    for attr, snapshot in keyword_pool_snapshots.items():
        if hasattr(policy, attr):
            setattr(policy, attr, list(snapshot))
    rules_snapshot = token.get("rules_snapshot") or ()
    if rules_snapshot:
        policy.CATEGORY_RULES[:] = [tuple(entry) for entry in rules_snapshot]


@contextmanager
def language_overlay(overlay: dict[str, Any]) -> Iterator[None]:
    """Context manager that activates an overlay and restores policy on exit.

    The sorter's ``decide()`` is not safe to interrupt, but a small
    save/restore around a single message keeps the policy module in a
    well-defined state for the next message — even if the next message is
    in a different language.
    """

    token = activate_language_overlay(overlay)
    try:
        yield
    finally:
        restore_policy(token)


__all__ = [
    "POLICY_FILES",
    "load_policy_overrides",
    "load_language_overlay",
    "apply_overrides",
    "activate_language_overlay",
    "restore_policy",
    "language_overlay",
]
