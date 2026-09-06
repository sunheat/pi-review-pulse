# Vendored from codex-review-pulse/skills/codex-review-pulse/scripts/default_policy.py
#!/usr/bin/env python3
"""Normalized policy for the Codex-first default remediation loop.

The agent translates a user's natural-language request into a small mapping
before passing it to this module.  This module deliberately does not parse
natural language: it validates the durable policy that is persisted in the
default checkpoint.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping


POLICY_SCHEMA_VERSION = 1

_PROFILES = {"autonomous", "observe-only"}
_UNSUPPORTED_PROFILES = {"supervised"}
_VALIDATION_FAILURES = {"repair", "pause"}
_MUTATION_POLICIES = {"auto", "never"}
_NOTIFICATION_POLICIES = {"blockers-and-terminal", "every-wake", "silent"}
_EXECUTION_MODES = {"unattended", "interactive"}

DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": POLICY_SCHEMA_VERSION,
    "profile": "autonomous",
    "execution_mode": "unattended",
    "cadence_seconds": 600,
    "max_wakes": None,
    "deadline_at": None,
    "validation_failure": "repair",
    "allow_test_changes": True,
    "publication": "auto",
    "thread_resolution": "auto",
    "review_trigger": "auto",
    "inline_retry_limit": 3,
    "retry_wake_limit": None,
    "no_progress_limit": 3,
    "notifications": "blockers-and-terminal",
}

_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "autonomous": {},
    "observe-only": {
        "execution_mode": "interactive",
        "validation_failure": "pause",
        "allow_test_changes": False,
        "publication": "never",
        "thread_resolution": "never",
        "review_trigger": "never",
        "notifications": "every-wake",
    },
}


class PolicyError(ValueError):
    """Raised when a default-mode policy cannot be normalized safely."""


def default_policy() -> dict[str, Any]:
    """Return a detached copy of the aggressive default policy."""
    return deepcopy(DEFAULT_POLICY)


def _parse_deadline(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PolicyError("deadline_at must be an ISO timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyError("deadline_at must be an ISO timestamp or null") from error
    if parsed.tzinfo is None:
        raise PolicyError("deadline_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _positive_int(value: Any, field: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        suffix = " or null" if allow_none else ""
        raise PolicyError(f"{field} must be a positive integer{suffix}")
    return value


def _non_negative_int(value: Any, field: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        suffix = " or null" if allow_none else ""
        raise PolicyError(f"{field} must be a non-negative integer{suffix}")
    return value


def normalize_policy(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and canonicalize a complete policy mapping."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise PolicyError("automation policy must be an object")

    unknown = set(raw) - set(DEFAULT_POLICY)
    if unknown:
        names = ", ".join(sorted(str(item) for item in unknown))
        raise PolicyError(f"Unknown automation policy field(s): {names}")

    profile = raw.get("profile", DEFAULT_POLICY["profile"])
    if profile in _UNSUPPORTED_PROFILES:
        raise PolicyError(
            f"profile '{profile}' is unsupported; no confirmation/resume protocol is available"
        )
    if profile not in _PROFILES:
        raise PolicyError(f"profile must be one of {sorted(_PROFILES)}")
    policy = default_policy()
    policy.update(_PROFILE_DEFAULTS[profile])
    policy.update(dict(raw))
    policy["schema_version"] = POLICY_SCHEMA_VERSION
    policy["profile"] = profile

    if policy["execution_mode"] not in _EXECUTION_MODES:
        raise PolicyError(f"execution_mode must be one of {sorted(_EXECUTION_MODES)}")
    policy["cadence_seconds"] = _positive_int(
        policy["cadence_seconds"], "cadence_seconds"
    )
    policy["max_wakes"] = _positive_int(
        policy["max_wakes"], "max_wakes", allow_none=True
    )
    policy["deadline_at"] = _parse_deadline(policy["deadline_at"])
    if policy["validation_failure"] not in _VALIDATION_FAILURES:
        raise PolicyError(
            f"validation_failure must be one of {sorted(_VALIDATION_FAILURES)}"
        )
    if not isinstance(policy["allow_test_changes"], bool):
        raise PolicyError("allow_test_changes must be boolean")
    for field in ("publication", "thread_resolution", "review_trigger"):
        if policy[field] == "confirm":
            raise PolicyError(
                f"{field}=confirm is unsupported; no confirmation/resume protocol is available"
            )
        if policy[field] not in _MUTATION_POLICIES:
            raise PolicyError(f"{field} must be one of {sorted(_MUTATION_POLICIES)}")
    policy["inline_retry_limit"] = _non_negative_int(
        policy["inline_retry_limit"], "inline_retry_limit"
    )
    policy["retry_wake_limit"] = _non_negative_int(
        policy["retry_wake_limit"], "retry_wake_limit", allow_none=True
    )
    policy["no_progress_limit"] = _positive_int(
        policy["no_progress_limit"], "no_progress_limit"
    )
    if policy["notifications"] not in _NOTIFICATION_POLICIES:
        raise PolicyError(
            f"notifications must be one of {sorted(_NOTIFICATION_POLICIES)}"
        )
    return policy


def apply_policy_overrides(
    current: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Apply explicit prompt/CLI overrides to the current persisted policy."""
    base = normalize_policy(current)
    if overrides is None:
        return base
    if not isinstance(overrides, Mapping):
        raise PolicyError("automation policy overrides must be an object")
    if "profile" in overrides:
        profile = overrides["profile"]
        base = normalize_policy({"profile": profile})
    merged = dict(base)
    merged.update(dict(overrides))
    return normalize_policy(merged)


def policy_digest(policy: Mapping[str, Any]) -> str:
    """Return a stable digest for a normalized policy."""
    normalized = normalize_policy(policy)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_policy_json(value: str) -> dict[str, Any]:
    """Parse and validate the structured policy supplied by the host agent."""
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise PolicyError("--policy-json must contain valid JSON") from error
    if not isinstance(raw, dict):
        raise PolicyError("--policy-json must contain a JSON object")
    return raw
