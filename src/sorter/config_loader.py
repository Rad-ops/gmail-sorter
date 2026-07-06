"""Optional YAML policy overrides.

The keyword lists and rules in :mod:`sorter.policy` are the defaults. A user can
drop a ``config/policy.yaml`` next to the sorter to override specific keyword
groups or thresholds without editing code. The loader is intentionally
permissive: a missing file, a missing key, or a malformed YAML section is
logged and ignored so the tool keeps running on the built-in defaults.

Requires PyYAML only if a policy.yaml file is actually present; the core tool
does not hard-depend on YAML for normal runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import policy

log = logging.getLogger("sorter.config")


def load_policy_overrides(path: Path) -> dict[str, Any]:
    """Read config/policy.yaml into a dict, or return {} when unavailable."""

    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("policy.yaml found at %s but PyYAML is not installed; using built-in defaults.", path)
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
