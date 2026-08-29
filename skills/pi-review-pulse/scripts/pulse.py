# Vendored from codex-review-pulse/skills/codex-review-pulse/scripts/pulse.py
#!/usr/bin/env python3
"""Codex-first default control surface for one PR-scoped wake.

This module deliberately depends only on the core GraphQL, state, checkpoint,
and exact-resolution primitives.  The run-contract, installation, lease, and
heartbeat-tick modules remain an opt-in hardened mode and are not imported by
the default path.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from checkpoint_store import checkpoint_path, load_checkpoint, save_checkpoint
from default_policy import (
    PolicyError,
    apply_policy_overrides,
    default_policy,
    parse_policy_json,
    policy_digest,
    normalize_policy,
)
from fetch_pr_state import fetch_stable_snapshot, resolve_target
from state_model import (
    DEFAULT_CODEX_LOGINS,
    canonical_repository,
    empty_checkpoint,
    evaluate_snapshot,
    freeze_batch,
    record_publication_failure,
    record_publication_success,
    record_resolved_thread,
    record_thread_outcome,
)


DEFAULT_CADENCE_SECONDS = 600
DEFAULT_MODE_SCHEMA_VERSION = 2

REARM_ACTIONS = {"WAIT_REVIEW", "REQUEST_REVIEW", "WAIT_RETRY"}
PAUSE_ACTIONS = {
    "PAUSE_BLOCKED",
    "PAUSE_CONCURRENT",
    "PAUSE_RECOVERY",
    "PAUSE_EXPIRED",
    "PAUSE_POLICY_CONFIRMATION",
}
TERMINAL_ACTIONS = {"STOP_TERMINAL", "STOP_CLOSED", "STOP_POLICY_LIMIT"}


class DefaultWakeError(RuntimeError):
    """Raised when a default wake attempts an operation after its boundary."""


def _policy_pause(state: dict[str, Any], *, now: str, operation: str) -> dict[str, Any]:
    """Pause when a supervised policy requires a user decision."""
    return _pause(
        state,
        reason_code="policy_requires_confirmation",
        now=now,
        evidence={"operation": operation, "profile": state["automation_policy"]["profile"]},
        action="PAUSE_POLICY_CONFIRMATION",
    )


def _utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Time inputs must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: str | datetime) -> str:
    return _utc(value).isoformat()


def _now(value: str | None) -> str:
    return _iso(value or datetime.now(UTC))


def _default_review_epoch() -> dict[str, Any]:
    return {
        "head_oid": None,
        "codex_eyes_seen": False,
        "codex_eyes_active": False,
        "clean_epoch_proven": False,
        "idle_observation_count": 0,
    }


def ensure_default_lifecycle(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Add and validate default lifecycle state, migrating schema version 1."""
    result = deepcopy(checkpoint)
    previous_version = result.get("default_mode_schema_version")
    if (
        isinstance(previous_version, bool)
        or previous_version not in (None, 1, DEFAULT_MODE_SCHEMA_VERSION)
    ):
        raise ValueError("Unsupported Codex-first default lifecycle schema version")
    defaults: dict[str, Any] = {
        "default_mode_schema_version": DEFAULT_MODE_SCHEMA_VERSION,
        "active_wake_id": None,
        "wake_phase": "idle",
        "wake_started_at": None,
        "wake_completed_at": None,
        "next_not_before": None,
        "scheduled_task_disposition": "PAUSED",
        "wake_count": 0,
        "failure_latch": None,
        "last_wake_id": None,
        "last_wake_result": None,
        "last_decision": None,
        "review_epoch_state": _default_review_epoch(),
        "trigger_events": {},
        "automation_policy": default_policy(),
        "automation_policy_digest": None,
        "retry_state": {
            "inline_attempts": 0,
            "wake_attempts": 0,
            "last_signature": None,
            "no_progress_attempts": 0,
        },
    }
    for key, value in defaults.items():
        result.setdefault(key, deepcopy(value))
    result["default_mode_schema_version"] = DEFAULT_MODE_SCHEMA_VERSION
    try:
        result["automation_policy"] = normalize_policy(result["automation_policy"])
    except PolicyError as error:
        raise ValueError(str(error)) from error
    result["automation_policy_digest"] = policy_digest(result["automation_policy"])
    if result.get("scheduled_task_disposition") not in {"PAUSED", "ACTIVE"}:
        raise ValueError("Scheduled task disposition is invalid")
    wake_count = result.get("wake_count")
    if not isinstance(wake_count, int) or isinstance(wake_count, bool) or wake_count < 0:
        raise ValueError("Default wake count is invalid")
    if not isinstance(result.get("trigger_events"), dict):
        raise ValueError("Default trigger events are invalid")
    retry_state = result.get("retry_state")
    if not isinstance(retry_state, dict):
        raise ValueError("Default retry state is invalid")
    for key in ("inline_attempts", "wake_attempts", "no_progress_attempts"):
        value = retry_state.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Default retry state field {key} is invalid")
    return result


def update_default_policy(
    checkpoint: dict[str, Any],
    *,
    overrides: Mapping[str, Any],
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist an explicit user-supplied policy update outside an active wake."""
    state = ensure_default_lifecycle(checkpoint)
    if state.get("active_wake_id"):
        raise DefaultWakeError("Policy cannot change while a wake is active")
    batch = state.get("active_batch")
    if (
        isinstance(batch, dict)
        and (batch.get("publication") or {}).get("status") != "succeeded"
    ):
        raise DefaultWakeError("Policy cannot change while a frozen batch is unfinished")
    if state.get("wake_phase") in {"terminal", "closed"}:
        raise DefaultWakeError("The checkpoint has reached an absorbing stop")
    try:
        policy = apply_policy_overrides(state.get("automation_policy"), overrides)
    except PolicyError as error:
        raise ValueError(str(error)) from error
    state["automation_policy"] = policy
    state["automation_policy_digest"] = policy_digest(policy)
    result = _decision(
        "POLICY_UPDATED",
        "explicit_policy_update",
        policy=deepcopy(policy),
        policy_digest=state["automation_policy_digest"],
        updated_at=_iso(now),
        mutation_occurred=False,
    )
    _set_last_result(state, result)
    return state, result


def _decision(action: str, reason_code: str, **details: Any) -> dict[str, Any]:
    disposition = "pause" if action in PAUSE_ACTIONS else "complete" if action in TERMINAL_ACTIONS else "continue"
    return {
        "next_action": action,
        "reason_code": reason_code,
        "recommended_heartbeat_disposition": disposition,
        **details,
    }


def _set_last_result(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["last_decision"] = deepcopy(result)
    state["last_wake_result"] = deepcopy(result)


def _pause(
    state: dict[str, Any],
    *,
    reason_code: str,
    now: str,
    evidence: Any = None,
    action: str = "PAUSE_BLOCKED",
    mutation_occurred: bool = False,
) -> dict[str, Any]:
    """Make pause absorbing for the current wake and persist its evidence."""
    active_wake_id = state.get("active_wake_id")
    result = _decision(
        action,
        reason_code,
        evidence=evidence,
        mutation_occurred=mutation_occurred,
    )
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "paused"
    state["wake_completed_at"] = now
    state["next_not_before"] = None
    state["active_wake_id"] = None
    state["last_wake_id"] = active_wake_id or state.get("last_wake_id")
    if not state.get("failure_latch"):
        state["failure_latch"] = {
            "reason_code": reason_code,
            "latched_at": now,
            "evidence": deepcopy(evidence),
        }
    _set_last_result(state, result)
    return result


def _terminal(
    state: dict[str, Any], *, action: str, reason_code: str, now: str, **details: Any
) -> dict[str, Any]:
    result = _decision(action, reason_code, **details)
    state["scheduled_task_disposition"] = "PAUSED"
    state["wake_phase"] = "terminal"
    state["wake_completed_at"] = now
    state["next_not_before"] = None
    state["last_wake_id"] = state.get("active_wake_id") or state.get("last_wake_id")
    state["active_wake_id"] = None
    _set_last_result(state, result)
    return result


def _require_active_wake(state: dict[str, Any], wake_id: str) -> None:
    if state.get("failure_latch"):
        raise DefaultWakeError("This wake is paused by a durable recovery latch")
    if state.get("active_wake_id") != wake_id:
        raise DefaultWakeError("The requested operation is not owned by the active wake")
    if state.get("wake_phase") in {"paused", "terminal", "completed"}:
        raise DefaultWakeError("The current wake has already reached a terminal boundary")


def _pause_confirmation(callback: Callable[[], object] | None) -> bool:
    if callback is None:
        return False
    try:
        value = callback()
    except Exception:
        return False
    if isinstance(value, dict):
        return value.get("confirmed") is True
    return value is True


def _record_default_trigger_event(
    state: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    """Persist injected trigger evidence without importing recurring state."""
    required = (
        "attempted_head_oid",
        "head_before",
        "head_after",
        "comment_node_id",
        "created_at",
    )
    if not all(isinstance(evidence.get(key), str) and evidence[key] for key in required):
        raise ValueError("Complete trigger evidence is required")
    _utc(evidence["created_at"])
    attempted_head = evidence["attempted_head_oid"]
    events = state.setdefault("trigger_events", {})
    if attempted_head in events:
        raise ValueError("A review trigger is already recorded for this head epoch")
    status = (
        "emitted"
        if evidence["head_before"] == attempted_head == evidence["head_after"]
        else "head_changed_during_trigger"
    )
    events[attempted_head] = {
        "status": status,
        "head_oid": attempted_head,
        "head_before": evidence["head_before"],
        "head_after": evidence["head_after"],
        "comment_node_id": evidence["comment_node_id"],
        "created_at": evidence["created_at"],
    }
    return events[attempted_head]


def begin_wake(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    cadence_seconds: int | None = None,
    policy_overrides: Mapping[str, Any] | None = None,
    pause_heartbeat: Callable[[], object] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Begin at most one wake, pausing the host heartbeat before PR work."""
    if not isinstance(wake_id, str) or not wake_id.strip():
        raise ValueError("A non-empty wake_id is required")
    now = _iso(now)
    state = ensure_default_lifecycle(checkpoint)

    if policy_overrides is not None:
        if state.get("wake_count", 0) > 0 or state.get("active_wake_id"):
            raise DefaultWakeError(
                "Policy overrides are only accepted on the initial wake; use configure-policy for an explicit update"
            )
        try:
            state["automation_policy"] = apply_policy_overrides(
                state.get("automation_policy"), policy_overrides
            )
        except PolicyError as error:
            raise ValueError(str(error)) from error
        state["automation_policy_digest"] = policy_digest(state["automation_policy"])

    effective_cadence = (
        state["automation_policy"]["cadence_seconds"]
        if cadence_seconds is None
        else cadence_seconds
    )
    if isinstance(effective_cadence, bool) or not isinstance(effective_cadence, int) or effective_cadence <= 0:
        raise ValueError("Cadence must be positive")

    if state.get("last_wake_id") == wake_id and state.get("last_wake_result"):
        return state, deepcopy(state["last_wake_result"])
    if state.get("wake_phase") in {"terminal", "closed"}:
        raise DefaultWakeError(
            "The checkpoint has reached an absorbing stop; an explicit user command is required to reopen it"
        )
    if state.get("active_wake_id"):
        if state["active_wake_id"] == wake_id and state.get("last_wake_result"):
            return state, deepcopy(state["last_wake_result"])
        result = _pause(
            state,
            reason_code="incomplete_wake",
            now=now,
            evidence={"active_wake_id": state["active_wake_id"]},
            action="PAUSE_RECOVERY",
        )
        return state, result
    if state.get("failure_latch"):
        result = _pause(
            state,
            reason_code="failure_latched",
            now=now,
            evidence=state["failure_latch"],
            action="PAUSE_RECOVERY",
        )
        return state, result

    policy = state["automation_policy"]
    maximum_wakes = policy.get("max_wakes")
    if maximum_wakes is not None and state.get("wake_count", 0) >= maximum_wakes:
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="maximum_wakes_reached",
            now=now,
            limit=maximum_wakes,
            wake_count=state.get("wake_count", 0),
        )
        state["last_wake_id"] = wake_id
        return state, result
    deadline_at = policy.get("deadline_at")
    if deadline_at is not None and _utc(now) >= _utc(deadline_at):
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="deadline_reached",
            now=now,
            deadline_at=deadline_at,
        )
        state["last_wake_id"] = wake_id
        return state, result

    next_not_before = state.get("next_not_before")
    if next_not_before is not None and _utc(now) < _utc(next_not_before):
        result = _pause(
            state,
            reason_code="cadence_not_elapsed",
            now=now,
            evidence={"next_not_before": next_not_before, "wake_id": wake_id},
        )
        state["last_wake_id"] = wake_id
        return state, result

    # The scheduler adapter is deliberately injected.  Without an explicit
    # confirmation, no PR snapshot or mutation is allowed for this wake.
    state["wake_count"] += 1
    state["last_wake_id"] = wake_id
    if not _pause_confirmation(pause_heartbeat):
        result = _pause(
            state,
            reason_code="heartbeat_pause_unconfirmed",
            now=now,
            evidence={"wake_id": wake_id},
        )
        state["last_wake_id"] = wake_id
        return state, result

    pending_batch = (
        isinstance(state.get("active_batch"), dict)
        and (state.get("active_batch") or {}).get("publication", {}).get("status") != "succeeded"
        and state.get("wake_phase") == "retry_waiting"
    )
    state["active_wake_id"] = wake_id
    state["wake_phase"] = "started"
    state["wake_started_at"] = now
    state["wake_completed_at"] = None
    state["next_not_before"] = None
    state["scheduled_task_disposition"] = "PAUSED"
    state["last_decision"] = None
    state["last_wake_result"] = None
    if pending_batch:
        state["wake_phase"] = "processing"
        state["last_decision"] = _decision(
            "RUN_BATCH",
            "resume_pending_batch",
            targeted_thread_ids=list(
                (state.get("active_batch") or {}).get("targeted_thread_ids") or []
            ),
        )
    result = _decision(
        "WAKE_STARTED",
        "heartbeat_paused_before_wake",
        wake_id=wake_id,
        wake_count=state["wake_count"],
        resume_pending_batch=pending_batch,
        mutation_occurred=False,
    )
    state["last_wake_result"] = deepcopy(result)
    return state, result


def _update_review_epoch(
    state: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    epoch = deepcopy(state.get("review_epoch_state") or _default_review_epoch())
    head_oid = snapshot.get("head_oid")
    if not isinstance(head_oid, str) or not head_oid:
        raise ValueError("Normalized snapshot requires head_oid")
    if epoch.get("head_oid") != head_oid:
        epoch = _default_review_epoch()
        epoch["head_oid"] = head_oid
    review_activity = snapshot.get("review_in_progress")
    eyes_active = (
        review_activity.get("active") is True
        if isinstance(review_activity, dict)
        else bool(review_activity)
    )
    if eyes_active:
        epoch["codex_eyes_seen"] = True
        epoch["codex_eyes_active"] = True
        epoch["clean_epoch_proven"] = False
        epoch["idle_observation_count"] = 0
    else:
        epoch["codex_eyes_active"] = False
        if snapshot.get("targeted_thread_ids") or snapshot.get("approval_evidence", {}).get("status") == "approved_current_head":
            epoch["idle_observation_count"] = 0
        else:
            epoch["idle_observation_count"] = int(epoch.get("idle_observation_count", 0)) + 1
        if epoch.get("codex_eyes_seen") and not snapshot.get("targeted_thread_ids"):
            epoch["clean_epoch_proven"] = True
    state["review_epoch_state"] = epoch
    return epoch


def decide_snapshot(
    state: dict[str, Any], snapshot: dict[str, Any], *, now: str
) -> dict[str, Any]:
    """Evaluate one normalized snapshot without performing I/O."""
    epoch = _update_review_epoch(state, snapshot)
    if snapshot.get("snapshot_stable") is not True:
        return _pause(
            state,
            reason_code="mixed_head_snapshot",
            now=now,
            evidence=snapshot.get("server_evidence"),
        )
    if not snapshot.get("review_activity_ok", True):
        return _pause(
            state,
            reason_code="review_activity_evidence_invalid",
            now=now,
            evidence=snapshot.get("review_in_progress"),
        )
    if snapshot.get("pull_request_state") in {"CLOSED", "MERGED"}:
        return _terminal(
            state,
            action="STOP_CLOSED",
            reason_code="pull_request_closed_or_merged",
            now=now,
        )
    review_activity = snapshot.get("review_in_progress")
    eyes_active = (
        review_activity.get("active") is True
        if isinstance(review_activity, dict)
        else bool(review_activity)
    )
    if eyes_active:
        result = _decision("WAIT_REVIEW", "codex_review_in_progress")
    elif snapshot.get("approval_evidence", {}).get("status") == "approved_current_head" and not snapshot.get("targeted_thread_ids"):
        return _terminal(
            state,
            action="STOP_TERMINAL",
            reason_code="current_head_approval_proven",
            now=now,
        )
    elif snapshot.get("targeted_thread_ids"):
        if state["automation_policy"]["profile"] == "observe-only":
            return _policy_pause(state, now=now, operation="process_review_threads")
        result = _decision(
            "RUN_BATCH",
            "targeted_work_available",
            targeted_thread_ids=list(snapshot["targeted_thread_ids"]),
        )
    elif epoch.get("clean_epoch_proven"):
        return _terminal(
            state,
            action="STOP_TERMINAL",
            reason_code="clean_review_epoch_proven",
            now=now,
        )
    elif state.get("trigger_events", {}).get(snapshot.get("head_oid"), {}).get("status") == "emitted":
        return _pause(
            state,
            reason_code="review_trigger_did_not_start",
            now=now,
            evidence=state["trigger_events"][snapshot["head_oid"]],
        )
    elif epoch.get("idle_observation_count", 0) >= 2:
        trigger_policy = state["automation_policy"]["review_trigger"]
        if trigger_policy != "auto":
            return _policy_pause(state, now=now, operation="review_trigger")
        result = _decision("REQUEST_REVIEW", "idle_boundary_reached", head_oid=snapshot.get("head_oid"))
    else:
        result = _decision("WAIT_REVIEW", "awaiting_review_epoch")
    state["wake_phase"] = "snapshotted"
    _set_last_result(state, result)
    return result


def record_snapshot(
    checkpoint: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    wake_id: str,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one normalized snapshot and its one-wake decision."""
    state = ensure_default_lifecycle(checkpoint)
    if state.get("wake_phase") == "paused":
        raise DefaultWakeError("The current wake is paused and cannot continue")
    if state.get("last_wake_id") == wake_id and state.get("active_wake_id") is None and state.get("last_wake_result"):
        return state, deepcopy(state["last_wake_result"])
    _require_active_wake(state, wake_id)
    if state.get("wake_phase") == "snapshotted" and state.get("last_decision"):
        return state, deepcopy(state["last_decision"])
    state["latest_target_snapshot"] = {
        "head_oid": snapshot.get("head_oid"),
        "targeted_unresolved_thread_ids": list(snapshot.get("targeted_thread_ids") or []),
        "reviewer_logins": list(snapshot.get("reviewer_logins") or DEFAULT_CODEX_LOGINS),
    }
    state["reviewer_logins"] = list(snapshot.get("reviewer_logins") or DEFAULT_CODEX_LOGINS)
    state["approval_logins"] = list(snapshot.get("approval_logins") or DEFAULT_CODEX_LOGINS)
    result = decide_snapshot(state, snapshot, now=_iso(now))
    if result["next_action"] in PAUSE_ACTIONS:
        state["last_wake_id"] = wake_id
    elif result["next_action"] in TERMINAL_ACTIONS:
        state["last_wake_id"] = wake_id
    else:
        state["last_snapshot"] = deepcopy(snapshot)
    return state, result


def freeze_default_batch(
    checkpoint: dict[str, Any], *, wake_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if state.get("wake_phase") == "frozen" and isinstance(state.get("active_batch"), dict):
        return state, deepcopy(state["active_batch"])
    decision = state.get("last_decision") or {}
    if decision.get("next_action") != "RUN_BATCH":
        raise DefaultWakeError("Only a RUN_BATCH decision can freeze a batch")
    snapshot = state.get("latest_target_snapshot") or {}
    head_oid = snapshot.get("head_oid")
    thread_ids = snapshot.get("targeted_unresolved_thread_ids") or []
    if not head_oid:
        raise DefaultWakeError("The normalized snapshot has no frozen head")
    state = freeze_batch(state, head_oid, thread_ids)
    state["wake_phase"] = "frozen"
    state["last_decision"] = deepcopy(decision)
    state["last_wake_result"] = deepcopy(decision)
    return state, deepcopy(state["active_batch"])


def record_default_outcome(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    thread_id: str,
    classification: str,
    reference: str | None = None,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if state["automation_policy"]["thread_resolution"] != "auto":
        result = _policy_pause(state, now=_iso(now), operation="thread_resolution")
        state["last_wake_id"] = wake_id
        return state, result
    if classification == "ambiguous":
        state.setdefault("ambiguous_outcomes", {})[thread_id] = {
            "classification": classification,
            "reference": reference,
        }
        result = _pause(
            state,
            reason_code="ambiguous_thread_outcome",
            now=_iso(now),
            evidence={"thread_id": thread_id, "reference": reference},
        )
        state["last_wake_id"] = wake_id
        return state, result
    state = record_thread_outcome(
        state,
        thread_id=thread_id,
        classification=classification,
        reference=reference,
    )
    state["wake_phase"] = "processing"
    result = {
        "next_action": "PROCESS_BATCH",
        "reason_code": "thread_outcome_recorded",
        "thread_id": thread_id,
        "classification": classification,
        "mutation_occurred": False,
    }
    _set_last_result(state, result)
    return state, result


def record_retry(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    reason_code: str,
    now: str,
    evidence: Any = None,
    signature: str | None = None,
    count_no_progress: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist a recoverable failure and make the next wake retryable."""
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise ValueError("A non-empty retry reason_code is required")
    if signature is not None and (
        not isinstance(signature, str) or not signature.strip()
    ):
        raise ValueError("A retry signature must be a non-empty string")
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    policy = state["automation_policy"]
    if policy["validation_failure"] == "pause":
        result = _policy_pause(state, now=_iso(now), operation="validation_failure")
        state["last_wake_id"] = wake_id
        return state, result
    if count_no_progress and not signature:
        raise ValueError("A failure signature is required to count no progress")
    retry_state = state.setdefault("retry_state", {})
    if count_no_progress:
        if signature and signature == retry_state.get("last_signature"):
            retry_state["no_progress_attempts"] = int(
                retry_state.get("no_progress_attempts", 0)
            ) + 1
        else:
            retry_state["no_progress_attempts"] = 1
    else:
        retry_state["no_progress_attempts"] = 0
    retry_state["last_signature"] = signature
    no_progress_attempts = int(retry_state.get("no_progress_attempts", 0))
    if count_no_progress and no_progress_attempts >= policy["no_progress_limit"]:
        result = _pause(
            state,
            reason_code="no_progress_limit_reached",
            now=_iso(now),
            evidence={
                "signature": signature,
                "attempts": no_progress_attempts,
                "limit": policy["no_progress_limit"],
                "detail": evidence,
            },
            action="PAUSE_BLOCKED",
        )
        state["last_wake_id"] = wake_id
        return state, result
    wake_attempts = int(retry_state.get("wake_attempts", 0)) + 1
    retry_limit = policy.get("retry_wake_limit")
    if retry_limit is not None and wake_attempts > retry_limit:
        result = _terminal(
            state,
            action="STOP_POLICY_LIMIT",
            reason_code="retry_wake_limit_reached",
            now=_iso(now),
            limit=retry_limit,
            retry_wake_attempts=wake_attempts,
            evidence=evidence,
        )
        state["last_wake_id"] = wake_id
        return state, result
    retry_state["wake_attempts"] = wake_attempts
    retry_state["inline_attempts"] = 0
    state["wake_phase"] = "retry_waiting"
    result = _decision(
        "WAIT_RETRY",
        reason_code,
        retry_wake_attempts=wake_attempts,
        retry_limit=retry_limit,
        evidence=evidence,
        mutation_occurred=False,
    )
    _set_last_result(state, result)
    return state, result


def resolve_default_thread(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    thread_id: str,
    graphql_call: Callable[[str, dict[str, object]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve through the existing exact resolver after a local outcome."""
    from resolve_thread import resolve_exact_thread

    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if state["automation_policy"]["thread_resolution"] != "auto":
        raise DefaultWakeError("The current policy requires confirmation before thread resolution")
    batch = state.get("active_batch")
    if not isinstance(batch, dict) or thread_id not in batch.get("targeted_thread_ids", []):
        raise DefaultWakeError("Thread is not in the active frozen batch")
    if thread_id not in batch.get("thread_outcomes", {}):
        raise DefaultWakeError("Record the thread outcome before exact resolution")
    if thread_id in batch.get("resolved_thread_ids", []):
        return state, {"id": thread_id, "isResolved": True, "alreadyResolved": True}

    def recheck_boundary() -> None:
        if state.get("active_wake_id") != wake_id or state.get("wake_phase") in {"paused", "terminal"}:
            raise DefaultWakeError("Wake boundary no longer permits PR mutation")
        if state.get("failure_latch"):
            raise DefaultWakeError("Wake is paused by a durable recovery latch")

    thread = resolve_exact_thread(
        repository=state["repository"],
        pr_number=state["pull_request_number"],
        thread_id=thread_id,
        expected_thread_ids=list(batch["targeted_thread_ids"]),
        reviewer_logins=list(batch.get("reviewer_logins") or DEFAULT_CODEX_LOGINS),
        expected_head_oid=batch.get("frozen_head_oid"),
        before_mutation=recheck_boundary,
        graphql_call=graphql_call,
    )
    state = record_resolved_thread(state, thread_id)
    result = {
        "next_action": "THREAD_RESOLVED",
        "reason_code": "exact_thread_resolution_confirmed",
        "thread_id": thread_id,
        "resolved": True,
        "mutation_occurred": not bool(thread.get("alreadyResolved")),
    }
    _set_last_result(state, result)
    return state, result


def record_default_trigger(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if state["automation_policy"]["review_trigger"] != "auto":
        try:
            policy_now = _iso(evidence.get("created_at", datetime.now(UTC).isoformat()))
        except (TypeError, ValueError):
            policy_now = _iso(datetime.now(UTC))
        result = _policy_pause(state, now=policy_now, operation="review_trigger")
        return state, result
    if (state.get("last_decision") or {}).get("next_action") != "REQUEST_REVIEW":
        raise DefaultWakeError("This wake did not authorize a review trigger")
    head_oid = state.get("last_snapshot", {}).get("head_oid")
    if head_oid and evidence.get("attempted_head_oid") != head_oid:
        raise DefaultWakeError("Review trigger evidence is for a different head")
    event = _record_default_trigger_event(state, evidence)
    if event.get("status") != "emitted":
        result = _pause(
            state,
            reason_code="trigger_head_changed",
            now=evidence["created_at"],
            evidence=event,
            action="PAUSE_RECOVERY",
        )
    else:
        state["wake_phase"] = "trigger_recorded"
        result = {
            "next_action": "REQUEST_REVIEW",
            "reason_code": "review_trigger_recorded",
            "trigger": event,
            "mutation_occurred": False,
        }
        _set_last_result(state, result)
    return state, result


def record_publication_result(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    status: str,
    now: str,
    phase: str | None = None,
    pending_paths: list[str] | None = None,
    pending_commit: str | None = None,
    published_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = ensure_default_lifecycle(checkpoint)
    _require_active_wake(state, wake_id)
    if status == "failed":
        if phase not in {"validation", "commit", "push"}:
            raise ValueError("Publication failure phase must be validation, commit, or push")
        state = record_publication_failure(
            state,
            phase=phase,
            pending_paths=pending_paths or [],
            pending_commit=pending_commit,
        )
        result = _pause(
            state,
            reason_code="publication_failed",
            now=_iso(now),
            evidence={"phase": phase, "pending_paths": pending_paths or [], "pending_commit": pending_commit},
            action="PAUSE_RECOVERY",
        )
        state["last_wake_id"] = wake_id
        return state, result
    if status != "succeeded":
        raise ValueError("Publication status must be succeeded or failed")
    if state["automation_policy"]["publication"] != "auto":
        result = _policy_pause(state, now=_iso(now), operation="aggregate_publication")
        state["last_wake_id"] = wake_id
        return state, result
    state = record_publication_success(state, published_commit=published_commit)
    state["retry_state"] = {
        "inline_attempts": 0,
        "wake_attempts": 0,
        "last_signature": None,
        "no_progress_attempts": 0,
    }
    state["wake_phase"] = "publication_succeeded"
    result = {
        "next_action": "WAIT_REVIEW",
        "reason_code": "aggregate_publication_succeeded",
        "published_commit": published_commit,
        "mutation_occurred": bool(published_commit),
    }
    _set_last_result(state, result)
    return state, result


def complete_wake(
    checkpoint: dict[str, Any],
    *,
    wake_id: str,
    now: str,
    cadence_seconds: int | None = None,
    schedule_next_wake: Callable[[str], object] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Complete exactly one wake and rearm only from its completion timestamp."""
    now = _iso(now)
    state = ensure_default_lifecycle(checkpoint)
    effective_cadence = (
        state["automation_policy"]["cadence_seconds"]
        if cadence_seconds is None
        else cadence_seconds
    )
    if isinstance(effective_cadence, bool) or not isinstance(effective_cadence, int) or effective_cadence <= 0:
        raise ValueError("Cadence must be positive")
    if state.get("last_wake_id") == wake_id and state.get("active_wake_id") is None and state.get("last_wake_result"):
        return state, deepcopy(state["last_wake_result"])
    _require_active_wake(state, wake_id)
    decision = state.get("last_decision") or {}
    action = decision.get("next_action")
    mutation_occurred = bool(decision.get("mutation_occurred"))
    if action == "RUN_BATCH":
        publication = (state.get("active_batch") or {}).get("publication") or {}
        if publication.get("status") != "succeeded":
            result = _pause(
                state,
                reason_code="batch_publication_incomplete",
                now=now,
                evidence=publication,
                action="PAUSE_RECOVERY",
            )
            state["last_wake_id"] = wake_id
            return state, result
        mutation_occurred = mutation_occurred or bool(publication.get("published_commit"))
        action = "WAIT_REVIEW"
    elif action == "REQUEST_REVIEW":
        head_oid = state.get("last_snapshot", {}).get("head_oid")
        event = state.get("trigger_events", {}).get(head_oid, {})
        if event.get("status") != "emitted":
            result = _pause(
                state,
                reason_code="review_trigger_not_confirmed",
                now=now,
                evidence=event,
            )
            state["last_wake_id"] = wake_id
            return state, result
    elif action in {"WAIT_REVIEW", "WAIT_RETRY"}:
        pass
    else:
        raise DefaultWakeError("The wake has no rearmable WAIT_REVIEW or REQUEST_REVIEW result")

    completed_at = _utc(now)
    next_not_before = (completed_at + timedelta(seconds=effective_cadence)).isoformat()
    state["wake_completed_at"] = now
    state["next_not_before"] = next_not_before
    state["active_wake_id"] = None
    result = _decision(
        action,
        "wake_completed",
        wake_id=wake_id,
        wake_completed_at=now,
        next_not_before=next_not_before,
        mutation_occurred=mutation_occurred,
    )
    if schedule_next_wake is None or not _pause_confirmation(lambda: schedule_next_wake(next_not_before)):
        return_state_result = _pause(
            state,
            reason_code="scheduled_task_reanchor_unavailable",
            now=now,
            evidence={"next_not_before": next_not_before},
            mutation_occurred=mutation_occurred,
        )
        state["next_not_before"] = next_not_before
        state["last_wake_id"] = wake_id
        return state, return_state_result

    state["scheduled_task_disposition"] = "ACTIVE"
    state["wake_phase"] = "retry_waiting" if action == "WAIT_RETRY" else "completed"
    state["last_wake_id"] = wake_id
    _set_last_result(state, result)
    return state, result


def normalize_snapshot(
    raw: dict[str, Any], evaluation: dict[str, Any], *, observed_at: str
) -> dict[str, Any]:
    """Produce the agent-facing snapshot without an intermediate observation JSON."""
    pull_request = raw["pull_request"]
    targeted_ids = set(evaluation["targeted_unresolved_thread_ids"])
    targeted_threads = [
        thread for thread in raw.get("review_threads", []) if thread.get("id") in targeted_ids
    ]
    return {
        "mode": "codex-first-default",
        "repository": raw["repository"].casefold(),
        "pull_request_number": pull_request["number"],
        "pull_request_state": pull_request["state"],
        "head_oid": pull_request["headRefOid"],
        "targeted_thread_ids": evaluation["targeted_unresolved_thread_ids"],
        "targeted_threads": targeted_threads,
        "non_target_threads": evaluation["non_target_unresolved_threads"],
        "review_in_progress": {
            "active": evaluation["codex_review_in_progress"],
            "reaction_ids": [
                item["id"] for item in evaluation["codex_review_in_progress_reactions"]
            ],
            "reactions": evaluation["codex_review_in_progress_reactions"],
        },
        "review_activity_ok": evaluation["review_activity_ok"],
        "approval_evidence": {
            "status": evaluation["approval_status"],
            "proof": evaluation["approval_proof"],
            "reaction_ids": evaluation["proven_current_head_reaction_ids"],
            "reactions": evaluation["qualifying_approval_reactions"],
            "review_ids": [
                item["id"] for item in evaluation["qualifying_current_head_approval_reviews"]
            ],
            "reviews": evaluation["qualifying_current_head_approval_reviews"],
            "diagnostic": evaluation["approval_diagnostic"],
        },
        "review_epoch_state": {
            "head_oid": pull_request["headRefOid"],
            "transition": evaluation["approval_epoch_transition"],
            "cold_start": evaluation["cold_start"],
            "proven_reaction_ids": evaluation["proven_current_head_reaction_ids"],
        },
        "server_evidence": {
            "head_before": pull_request["headRefOid"],
            "head_after": pull_request["headRefOid"],
            "head_bracketed": True,
            "observed_at": observed_at,
        },
        "snapshot_stable": True,
    }


def _checkpoint_target(checkpoint: dict[str, Any] | None) -> tuple[str, int] | None:
    if not isinstance(checkpoint, dict):
        return None
    repository = checkpoint.get("repository")
    pr_number = checkpoint.get("pull_request_number")
    if not isinstance(repository, str) or not isinstance(pr_number, int):
        return None
    if isinstance(pr_number, bool) or pr_number < 1:
        raise RuntimeError("Checkpoint pull request binding is invalid")
    try:
        return canonical_repository(repository), pr_number
    except ValueError as error:
        raise RuntimeError("Checkpoint repository binding is invalid") from error


def _resolve_command_target(
    args: argparse.Namespace,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Resolve once, or reuse an already-bound checkpoint target."""
    bound = _checkpoint_target(checkpoint)
    if bound is not None:
        bound_repository, bound_pr = bound
        if args.repo is not None and canonical_repository(args.repo) != bound_repository:
            raise RuntimeError("CLI repository does not match the checkpoint target")
        if args.pr is not None and args.pr != bound_pr:
            raise RuntimeError("CLI pull request does not match the checkpoint target")
        return bound

    try:
        owner, repo, pr_number = resolve_target(
            args.repo,
            args.pr,
            repository_path=args.repository_path,
        )
    except (RuntimeError, ValueError) as error:
        if args.repo is None or args.pr is None:
            raise RuntimeError(
                "Cannot identify a unique current pull request; run from a PR checkout or pass --repo OWNER/REPO and --pr NUMBER"
            ) from error
        raise
    return canonical_repository(f"{owner}/{repo}"), pr_number


def _assert_checkpoint_target(
    checkpoint: dict[str, Any], repository: str, pr_number: int
) -> None:
    bound = _checkpoint_target(checkpoint)
    if bound != (canonical_repository(repository), pr_number):
        raise RuntimeError("Checkpoint target does not match the requested pull request")


def _state_path(
    args: argparse.Namespace,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> Path:
    if args.state_file:
        return args.state_file
    repository, pr_number = _resolve_command_target(args, checkpoint=checkpoint)
    return checkpoint_path(repository, pr_number, repository_path=args.repository_path)


def _load_state(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
    path = _state_path(args, checkpoint=supplied_checkpoint)
    checkpoint = load_checkpoint(path)
    if checkpoint is None:
        raise RuntimeError(
            "Checkpoint does not exist; run begin-wake from the target PR checkout first"
        )
    repository, pr_number = _resolve_command_target(args, checkpoint=checkpoint)
    _assert_checkpoint_target(checkpoint, repository, pr_number)
    return path, ensure_default_lifecycle(checkpoint)


def _write(path: Path, state: dict[str, Any], result: dict[str, Any]) -> None:
    save_checkpoint(path, state)
    output = deepcopy(result)
    output["checkpoint_path"] = str(path)
    print(json.dumps(output, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one Codex-first default wake without hardened contract ceremony",
        epilog=(
            "Host handoff:\n"
            "  pause the scheduled task before begin-wake; pass --pause-confirmed "
            "after success.\n"
            "  re-anchor the next run before complete-wake --schedule-reanchored."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        help="OWNER/REPO; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument("--repository-path", default=".", help="Target checkout")
    parser.add_argument("--state-file", type=Path, help="Checkpoint override for tests")
    parser.add_argument("--now", help="UTC timestamp for deterministic tests")
    parser.add_argument("--wake-id", help="Host invocation ID")
    parser.add_argument("--reviewer-login", action="append", default=[])
    parser.add_argument("--approval-login", action="append", default=[])
    commands = parser.add_subparsers(dest="command", required=True)

    begin = commands.add_parser(
        "begin-wake",
        help="Start one wake after the host has paused the scheduled task",
    )
    begin.add_argument(
        "--pause-confirmed",
        action="store_true",
        help="Acknowledge that the host pause call succeeded; pulse.py does not perform it",
    )

    commands.add_parser("snapshot", help="Fetch and normalize one stable PR snapshot")
    commands.add_parser("freeze", help="Freeze the targeted threads from the snapshot")

    record = commands.add_parser("record", help="Persist one thread outcome")
    record.add_argument("--thread-id", required=True)
    record.add_argument("--classification", required=True, choices=["fix-now", "no-fix", "defer", "ambiguous"])
    record.add_argument("--reference")

    resolve = commands.add_parser("resolve", help="Resolve one exact frozen GraphQL thread")
    resolve.add_argument("--thread-id", required=True)

    trigger = commands.add_parser("trigger-result", help="Persist injected bracketed trigger evidence")
    trigger.add_argument("--evidence", required=True, type=Path)

    retry = commands.add_parser(
        "retry",
        help="Persist a recoverable failure and prepare a completion-relative retry",
    )
    retry.add_argument("--reason-code", required=True)
    retry.add_argument("--signature")
    retry.add_argument("--evidence", type=Path)
    retry.add_argument(
        "--no-progress",
        action="store_true",
        help="Count this retry only when the same validation failure made no progress",
    )

    commands.add_parser(
        "configure-policy",
        help="Persist explicit prompt-derived default automation policy overrides",
    )

    publication = commands.add_parser("publication-result", help="Persist aggregate publication outcome")
    publication.add_argument("--status", required=True, choices=["succeeded", "failed"])
    publication.add_argument("--phase", choices=["validation", "commit", "push"])
    publication.add_argument("--pending-path", action="append", default=[])
    publication.add_argument("--pending-commit")
    publication.add_argument("--published-commit")

    complete = commands.add_parser(
        "complete-wake",
        help="Complete the wake after the host re-anchors its next run",
    )
    complete.add_argument(
        "--schedule-reanchored",
        action="store_true",
        help="Acknowledge that the host re-anchor call succeeded; pulse.py does not perform it",
    )
    complete.add_argument("--cadence-seconds", type=int)
    parser.add_argument(
        "--policy-json",
        help="JSON object of prompt-derived policy overrides (initial wake or configure-policy)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = _now(args.now)
    policy_overrides = (
        parse_policy_json(args.policy_json) if args.policy_json is not None else None
    )
    if args.command == "begin-wake":
        if not args.wake_id:
            raise RuntimeError("--wake-id is required")
        supplied_checkpoint = load_checkpoint(args.state_file) if args.state_file else None
        repository, pr_number = _resolve_command_target(
            args,
            checkpoint=supplied_checkpoint,
        )
        path = _state_path(args, checkpoint=supplied_checkpoint)
        state = ensure_default_lifecycle(load_checkpoint(path) or {})
        if not state.get("repository"):
            state = ensure_default_lifecycle(empty_checkpoint(repository, pr_number))
        else:
            _assert_checkpoint_target(state, repository, pr_number)
        state, result = begin_wake(
            state,
            wake_id=args.wake_id,
            now=now,
            policy_overrides=policy_overrides,
            pause_heartbeat=lambda: args.pause_confirmed,
        )
        _write(path, state, result)
        return

    path, state = _load_state(args)

    if args.command == "configure-policy":
        if policy_overrides is None:
            raise RuntimeError("--policy-json is required for configure-policy")
        state, result = update_default_policy(
            state,
            overrides=policy_overrides,
            now=now,
        )
        _write(path, state, result)
        return

    if not args.wake_id:
        raise RuntimeError("--wake-id is required")

    if args.command == "snapshot":
        repository = state["repository"]
        pr_number = state["pull_request_number"]
        owner, repo_name = repository.split("/", 1)
        raw = fetch_stable_snapshot(owner, repo_name, pr_number)
        pull_request = raw["pull_request"]
        previous = state
        evaluation, state = evaluate_snapshot(
            repository=raw["repository"],
            pr_number=pr_number,
            head_oid=pull_request["headRefOid"],
            pull_request_state=pull_request["state"],
            review_threads=raw["review_threads"],
            reactions=raw["thumbs_up_reactions"],
            review_activity_reactions=raw["eyes_reactions"],
            reviews=raw["reviews"],
            reviewer_logins=args.reviewer_login or state.get("reviewer_logins") or list(DEFAULT_CODEX_LOGINS),
            approval_logins=args.approval_login or state.get("approval_logins") or list(DEFAULT_CODEX_LOGINS),
            checkpoint=previous,
            observed_at=now,
        )
        normalized = normalize_snapshot(raw, evaluation, observed_at=now)
        normalized["reviewer_logins"] = evaluation["reviewer_logins"]
        normalized["approval_logins"] = evaluation["approval_logins"]
        state["reviewer_logins"] = evaluation["reviewer_logins"]
        state["approval_logins"] = evaluation["approval_logins"]
        state, result = record_snapshot(state, normalized, wake_id=args.wake_id, now=now)
        normalized["decision"] = result
        normalized["review_epoch_state"] = deepcopy(state["review_epoch_state"])
        state["last_snapshot"] = normalized
        _write(path, state, {**normalized, "decision": result})
        return

    if args.command == "freeze":
        state, batch = freeze_default_batch(state, wake_id=args.wake_id)
        _write(path, state, {"next_action": "RUN_BATCH", "batch": batch})
        return
    if args.command == "record":
        state, result = record_default_outcome(
            state,
            wake_id=args.wake_id,
            thread_id=args.thread_id,
            classification=args.classification,
            reference=args.reference,
            now=now,
        )
        _write(path, state, result)
        return
    if args.command == "resolve":
        from resolve_thread import graphql

        state, result = resolve_default_thread(
            state,
            wake_id=args.wake_id,
            thread_id=args.thread_id,
            graphql_call=graphql,
        )
        _write(path, state, result)
        return
    if args.command == "trigger-result":
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        state, result = record_default_trigger(state, wake_id=args.wake_id, evidence=evidence)
        _write(path, state, result)
        return
    if args.command == "retry":
        evidence = (
            json.loads(args.evidence.read_text(encoding="utf-8"))
            if args.evidence is not None
            else None
        )
        state, result = record_retry(
            state,
            wake_id=args.wake_id,
            reason_code=args.reason_code,
            now=now,
            evidence=evidence,
            signature=args.signature,
            count_no_progress=args.no_progress,
        )
        _write(path, state, result)
        return
    if args.command == "publication-result":
        state, result = record_publication_result(
            state,
            wake_id=args.wake_id,
            status=args.status,
            now=now,
            phase=args.phase,
            pending_paths=args.pending_path,
            pending_commit=args.pending_commit,
            published_commit=args.published_commit,
        )
        _write(path, state, result)
        return
    if args.command == "complete-wake":
        state, result = complete_wake(
            state,
            wake_id=args.wake_id,
            now=now,
            cadence_seconds=args.cadence_seconds,
            schedule_next_wake=lambda _: args.schedule_reanchored,
        )
        _write(path, state, result)
        return
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
