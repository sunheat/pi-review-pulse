from __future__ import annotations

from copy import deepcopy
import subprocess
import sys
import unittest


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "pi-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from state_model import empty_checkpoint, record_resolved_thread  # noqa: E402
import pulse  # noqa: E402


NOW = "2026-08-26T00:00:00+00:00"


def snapshot(
    *,
    head: str = "HEAD1",
    targeted: list[str] | None = None,
    eyes: bool = False,
    approval: str = "awaiting_current_head_approval",
    stable: bool = True,
) -> dict:
    return {
        "head_oid": head,
        "pull_request_state": "OPEN",
        "targeted_thread_ids": targeted or [],
        "review_in_progress": {"active": eyes},
        "review_activity_ok": True,
        "approval_evidence": {"status": approval},
        "snapshot_stable": stable,
        "server_evidence": {"head_before": head, "head_after": head},
    }


def started(checkpoint=None, *, wake_id: str = "wake-1", now: str = NOW):
    state = checkpoint or empty_checkpoint("Owner/Repo", 17)
    return pulse.begin_wake(
        state,
        wake_id=wake_id,
        now=now,
        pause_heartbeat=lambda: True,
    )


def thread_graphql(*, head: str = "HEAD1", already_resolved: bool = False):
    def call(query: str, variables: dict[str, object]) -> dict:
        if "mutation" in query:
            return {
                "data": {
                    "resolveReviewThread": {
                        "thread": {
                            "id": variables["threadId"],
                            "isResolved": True,
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "nameWithOwner": "Owner/Repo",
                    "pullRequest": {
                        "number": 17,
                        "headRefOid": head,
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "T1",
                                    "isResolved": already_resolved,
                                    "comments": {
                                        "nodes": [
                                            {"author": {"login": "chatgpt-codex-connector"}}
                                        ]
                                    },
                                }
                            ],
                        },
                    },
                }
            }
        }

    return call


class DefaultLifecycleTests(unittest.TestCase):
    def test_schema_one_checkpoint_migrates_to_policy_schema(self) -> None:
        legacy = empty_checkpoint("Owner/Repo", 17)
        legacy["default_mode_schema_version"] = 1
        migrated = pulse.ensure_default_lifecycle(legacy)
        self.assertEqual(migrated["default_mode_schema_version"], 2)
        self.assertEqual(migrated["automation_policy"]["profile"], "autonomous")
        self.assertIsNone(migrated["automation_policy"]["max_wakes"])
        self.assertEqual(migrated["retry_state"]["wake_attempts"], 0)

    def test_default_policy_is_unbounded_but_optional_wake_limit_is_enforced(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"max_wakes": 1},
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        state, result = pulse.begin_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result["next_action"], "STOP_POLICY_LIMIT")
        self.assertEqual(result["reason_code"], "maximum_wakes_reached")
        self.assertEqual(state["wake_count"], 1)

    def test_deadline_is_optional_and_stops_before_work(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            policy_overrides={"deadline_at": "2026-08-26T00:00:00+00:00"},
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result["next_action"], "STOP_POLICY_LIMIT")
        self.assertEqual(result["reason_code"], "deadline_reached")
        self.assertEqual(state["wake_count"], 0)

    def test_recoverable_retry_resumes_the_same_frozen_batch_on_next_wake(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_validation_failure",
            now=NOW,
            signature="test-failure",
        )
        self.assertEqual(result["next_action"], "WAIT_RETRY")
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertEqual(state["wake_phase"], "retry_waiting")
        state, result = started(
            state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertTrue(result["resume_pending_batch"])
        self.assertEqual(state["last_decision"]["reason_code"], "resume_pending_batch")

    def test_duplicate_retry_recording_replays_without_consuming_limits(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"no_progress_limit": 3},
            pause_heartbeat=lambda: True,
        )
        state, first = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now=NOW,
            evidence={"step": "focused-test"},
            signature="same-failure",
            count_no_progress=True,
        )
        before_replay = deepcopy(state["retry_state"])
        replay, replay_result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now="2026-08-26T00:00:01+00:00",
            evidence={"step": "focused-test"},
            signature="same-failure",
            count_no_progress=True,
        )
        self.assertEqual(replay_result, first)
        self.assertEqual(replay["retry_state"], before_replay)
        self.assertEqual(replay["retry_state"]["wake_attempts"], 1)
        self.assertEqual(replay["retry_state"]["no_progress_attempts"], 1)

        with self.assertRaisesRegex(pulse.DefaultWakeError, "different retry recording"):
            pulse.record_retry(
                replay,
                wake_id="wake-1",
                reason_code="validation_failed",
                now=NOW,
                evidence={"step": "different-test"},
                signature="different-failure",
                count_no_progress=True,
            )
        self.assertEqual(replay["retry_state"], before_replay)

    def test_repeated_no_progress_reaches_pause_limit(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"no_progress_limit": 2},
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now=NOW,
            signature="same-failure",
            count_no_progress=True,
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_retry(
            state,
            wake_id="wake-2",
            reason_code="validation_failed",
            now="2026-08-26T00:11:00+00:00",
            signature="same-failure",
            count_no_progress=True,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "no_progress_limit_reached")

    def test_unsupported_supervised_and_confirm_modes_fail_before_wake(self) -> None:
        for overrides in (
            {"profile": "supervised"},
            {"publication": "confirm"},
            {"thread_resolution": "confirm"},
            {"review_trigger": "confirm"},
        ):
            with self.subTest(overrides=overrides):
                state = empty_checkpoint("Owner/Repo", 17)
                before = deepcopy(state)
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    pulse.begin_wake(
                        state,
                        wake_id="wake-1",
                        now=NOW,
                        policy_overrides=overrides,
                        pause_heartbeat=lambda: True,
                    )
                self.assertEqual(state, before)

    def test_unsupported_policy_update_preserves_existing_failure_latch(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, blocked = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
        )
        self.assertEqual(blocked["next_action"], "PAUSE_BLOCKED")
        before = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            pulse.update_default_policy(
                state,
                overrides={"publication": "confirm"},
                now="2026-08-26T00:02:00+00:00",
            )
        self.assertEqual(state, before)
        self.assertEqual(state["failure_latch"]["reason_code"], "scheduled_task_reanchor_unavailable")

    def test_validation_failure_policy_can_disable_automatic_retry(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"validation_failure": "pause"},
            pause_heartbeat=lambda: True,
        )
        state, result = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="validation_failed",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_POLICY_CONFIRMATION")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")

    def test_policy_update_cannot_split_an_unfinished_frozen_batch(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_retry(
            state,
            wake_id="wake-1",
            reason_code="transient_failure",
            now=NOW,
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        with self.assertRaisesRegex(pulse.DefaultWakeError, "unfinished"):
            pulse.update_default_policy(
                state,
                overrides={"max_wakes": 2},
                now=NOW,
            )

    def test_default_import_does_not_load_hardened_authority_modules(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, r'%s'); import pulse; print(','.join(sorted(name for name in ('recurring_contract','recurring_model','heartbeat_tick') if name in sys.modules)))"
                % SCRIPTS,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(process.stdout.strip(), "")

    def test_duplicate_initial_wake_replays_before_policy_override_validation(self) -> None:
        state, first = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            policy_overrides={"max_wakes": 5},
            pause_heartbeat=lambda: True,
        )
        replay, result = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            policy_overrides={"max_wakes": 5},
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(result, first)
        self.assertEqual(replay["wake_count"], 1)
        self.assertEqual(replay["automation_policy"]["max_wakes"], 5)

    def test_wake_starts_paused_and_increments_once(self) -> None:
        state, result = started()
        self.assertEqual(result["next_action"], "WAKE_STARTED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["wake_phase"], "started")
        self.assertEqual(state["wake_count"], 1)

        replay, replay_result = pulse.begin_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(replay["wake_count"], 1)
        self.assertEqual(replay_result, result)

    def test_pause_failure_blocks_snapshot_and_all_pr_mutations(self) -> None:
        state, result = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now=NOW,
            pause_heartbeat=lambda: False,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "heartbeat_pause_unconfirmed")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.record_snapshot(state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW)
        self.assertIsNone(state.get("active_batch"))

    def test_duplicate_snapshot_ignores_changed_evidence_and_wrong_wake(self) -> None:
        state, _ = started()
        state, first = pulse.record_snapshot(
            state,
            snapshot(head="HEAD1", targeted=["T1"]),
            wake_id="wake-1",
            now=NOW,
        )
        state, second = pulse.record_snapshot(
            state,
            snapshot(head="HEAD2", targeted=["T2"], eyes=True),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(first, second)
        self.assertEqual(state["wake_count"], 1)
        self.assertEqual(state["latest_target_snapshot"]["head_oid"], "HEAD1")
        self.assertEqual(state["latest_target_snapshot"]["targeted_unresolved_thread_ids"], ["T1"])
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.record_snapshot(
                state,
                snapshot(head="HEAD3", targeted=["T3"]),
                wake_id="wake-2",
                now=NOW,
            )
        self.assertEqual(state["latest_target_snapshot"]["head_oid"], "HEAD1")

    def test_completion_relative_cadence_uses_completion_not_start(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, result = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertEqual(result["next_not_before"], "2026-08-26T00:36:00+00:00")
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")

    def test_fixed_cadence_wakes_before_completion_boundary_are_absorbed(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        for index, when in enumerate(("00:10:00", "00:20:00", "00:30:00"), start=2):
            candidate = deepcopy(state)
            candidate, result = pulse.begin_wake(
                candidate,
                wake_id=f"wake-{index}",
                now=f"2026-08-26T{when}+00:00",
                pause_heartbeat=lambda: True,
            )
            self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
            self.assertEqual(result["reason_code"], "cadence_not_elapsed")
            self.assertEqual(candidate["wake_count"], 1)

    def test_pause_is_absorbing_and_recovery_id_is_not_a_default_operation(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(stable=False),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.freeze_default_batch(state, wake_id="wake-1")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.record_default_outcome(
                state,
                wake_id="wake-1",
                thread_id="T1",
                classification="no-fix",
                now=NOW,
            )
        self.assertNotIn("recovery_authorization_id", state)

    def test_eyes_wait_without_freezing_partial_threads(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(targeted=["T1"], eyes=True),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        with self.assertRaises(pulse.DefaultWakeError):
            pulse.freeze_default_batch(state, wake_id="wake-1")

    def test_eyes_disappear_with_targets_runs_and_without_targets_terminates(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"], eyes=True), wake_id="wake-1", now=NOW
        )
        # A new wake is needed after the WAIT boundary.
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_snapshot(
            state, snapshot(targeted=["T1"], eyes=False), wake_id="wake-2", now="2026-08-26T00:11:00+00:00"
        )
        self.assertEqual(result["next_action"], "RUN_BATCH")

        clean = empty_checkpoint("Owner/Repo", 17)
        clean, _ = started(clean)
        clean, _ = pulse.record_snapshot(clean, snapshot(eyes=True), wake_id="wake-1", now=NOW)
        clean, _ = pulse.complete_wake(clean, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda _: True)
        clean, _ = started(clean, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        clean, result = pulse.record_snapshot(clean, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertEqual(result["next_action"], "STOP_TERMINAL")

    def test_current_head_approval_and_historical_approval_are_distinct(self) -> None:
        approved, _ = started()
        approved, result = pulse.record_snapshot(
            approved,
            snapshot(approval="approved_current_head"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_TERMINAL")

        ambiguous, _ = started()
        ambiguous, result = pulse.record_snapshot(
            ambiguous,
            snapshot(approval="ambiguous_existing_reaction"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")

    def test_stop_terminal_rejects_a_new_wake(self) -> None:
        state, _ = started()
        state, result = pulse.record_snapshot(
            state,
            snapshot(approval="approved_current_head"),
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_TERMINAL")
        before = deepcopy(state)

        with self.assertRaisesRegex(pulse.DefaultWakeError, "absorbing stop"):
            pulse.begin_wake(
                state,
                wake_id="wake-2",
                now="2026-08-26T00:01:00+00:00",
                pause_heartbeat=lambda: True,
            )

        self.assertEqual(state, before)

    def test_stop_closed_rejects_a_new_wake(self) -> None:
        state, _ = started()
        closed_snapshot = snapshot()
        closed_snapshot["pull_request_state"] = "CLOSED"
        state, result = pulse.record_snapshot(
            state,
            closed_snapshot,
            wake_id="wake-1",
            now=NOW,
        )
        self.assertEqual(result["next_action"], "STOP_CLOSED")
        before = deepcopy(state)

        with self.assertRaisesRegex(pulse.DefaultWakeError, "absorbing stop"):
            pulse.begin_wake(
                state,
                wake_id="wake-2",
                now="2026-08-26T00:01:00+00:00",
                pause_heartbeat=lambda: True,
            )

        self.assertEqual(state, before)

    def test_no_fix_batch_can_publish_without_empty_commit(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            reference="false positive",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        state, result = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        self.assertIsNone(result["published_commit"])
        self.assertEqual((state["active_batch"]["publication"]["status"]), "succeeded")

    def test_fix_now_batch_rejects_publication_without_a_commit(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        with self.assertRaisesRegex(ValueError, "published commit"):
            pulse.record_publication_result(
                state,
                wake_id="wake-1",
                status="succeeded",
                now=NOW,
                published_commit=None,
            )
        self.assertEqual(state["active_batch"]["publication"]["status"], "not_started")

    def test_actual_resolution_survives_no_commit_publication(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            now=NOW,
        )
        state, resolved = pulse.resolve_default_thread(
            state,
            wake_id="wake-1",
            thread_id="T1",
            graphql_call=thread_graphql(),
        )
        self.assertTrue(resolved["mutation_occurred"])
        state, publication = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        self.assertTrue(publication["mutation_occurred"])
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertTrue(completed["mutation_occurred"])
        self.assertTrue(state["last_wake_result"]["mutation_occurred"])

    def test_already_resolved_noop_does_not_infer_historical_mutation(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            now=NOW,
        )
        state, resolved = pulse.resolve_default_thread(
            state,
            wake_id="wake-1",
            thread_id="T1",
            graphql_call=thread_graphql(already_resolved=True),
        )
        self.assertFalse(resolved["mutation_occurred"])
        state, publication = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        self.assertFalse(publication["mutation_occurred"])
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertFalse(completed["mutation_occurred"])

    def test_new_wake_resets_mutation_audit_before_historical_noop(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="no-fix",
            now=NOW,
        )
        state, _ = pulse.resolve_default_thread(
            state,
            wake_id="wake-1",
            thread_id="T1",
            graphql_call=thread_graphql(),
        )
        state, _ = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit=None,
        )
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertTrue(completed["mutation_occurred"])

        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertFalse(state["wake_mutation_occurred"])
        state, _ = pulse.record_snapshot(
            state,
            snapshot(head="HEAD2", targeted=["T1"]),
            wake_id="wake-2",
            now="2026-08-26T00:11:00+00:00",
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-2")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-2",
            thread_id="T1",
            classification="no-fix",
            now="2026-08-26T00:11:00+00:00",
        )
        state, resolved = pulse.resolve_default_thread(
            state,
            wake_id="wake-2",
            thread_id="T1",
            graphql_call=thread_graphql(head="HEAD2", already_resolved=True),
        )
        self.assertFalse(resolved["mutation_occurred"])
        state, _ = pulse.record_publication_result(
            state,
            wake_id="wake-2",
            status="succeeded",
            now="2026-08-26T00:11:00+00:00",
            published_commit=None,
        )
        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-2",
            now="2026-08-26T00:12:00+00:00",
            schedule_next_wake=lambda _: True,
        )
        self.assertFalse(completed["mutation_occurred"])

    def test_completed_publication_preserves_mutation_audit_flag(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(
            state, snapshot(targeted=["T1"]), wake_id="wake-1", now=NOW
        )
        state, _ = pulse.freeze_default_batch(state, wake_id="wake-1")
        state, _ = pulse.record_default_outcome(
            state,
            wake_id="wake-1",
            thread_id="T1",
            classification="fix-now",
            reference="src/example.py",
            now=NOW,
        )
        state = record_resolved_thread(state, "T1")
        state, publication = pulse.record_publication_result(
            state,
            wake_id="wake-1",
            status="succeeded",
            now=NOW,
            published_commit="abc1234",
        )
        self.assertTrue(publication["mutation_occurred"])

        state, completed = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
            schedule_next_wake=lambda _: True,
        )

        self.assertTrue(completed["mutation_occurred"])
        self.assertTrue(state["last_wake_result"]["mutation_occurred"])

    def test_trigger_is_once_per_head_and_empty_followup_pauses(self) -> None:
        state, _ = started()
        state, _ = pulse.record_snapshot(state, snapshot(), wake_id="wake-1", now=NOW)
        state, _ = pulse.complete_wake(state, wake_id="wake-1", now="2026-08-26T00:01:00+00:00", schedule_next_wake=lambda _: True)
        state, _ = started(state, wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        state, result = pulse.record_snapshot(state, snapshot(), wake_id="wake-2", now="2026-08-26T00:11:00+00:00")
        self.assertEqual(result["next_action"], "REQUEST_REVIEW")
        trigger_evidence = {
            "attempted_head_oid": "HEAD1",
            "head_before": "HEAD1",
            "head_after": "HEAD1",
            "comment_node_id": "COMMENT1",
            "created_at": "2026-08-26T00:11:00+00:00",
        }
        state, result = pulse.record_default_trigger(
            state,
            wake_id="wake-2",
            evidence=trigger_evidence,
        )
        self.assertEqual(result["reason_code"], "review_trigger_recorded")
        self.assertTrue(result["mutation_occurred"])
        replay, replay_result = pulse.record_default_trigger(
            state,
            wake_id="wake-2",
            evidence=trigger_evidence,
        )
        self.assertEqual(replay_result, result)
        self.assertEqual(replay["trigger_events"], state["trigger_events"])
        before_conflict = deepcopy(replay)
        conflicting_evidence = {**trigger_evidence, "comment_node_id": "COMMENT2"}
        with self.assertRaisesRegex(ValueError, "Conflicting review trigger evidence"):
            pulse.record_default_trigger(
                replay,
                wake_id="wake-2",
                evidence=conflicting_evidence,
            )
        self.assertEqual(replay, before_conflict)
        state, completed = pulse.complete_wake(state, wake_id="wake-2", now="2026-08-26T00:12:00+00:00", schedule_next_wake=lambda _: True)
        self.assertTrue(completed["mutation_occurred"])
        state, _ = started(state, wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        state, result = pulse.record_snapshot(state, snapshot(), wake_id="wake-3", now="2026-08-26T00:22:00+00:00")
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(result["reason_code"], "review_trigger_did_not_start")


if __name__ == "__main__":
    unittest.main()
