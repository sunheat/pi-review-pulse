from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "pi-review-pulse" / "SKILL.md"
SCRIPTS = ROOT / "skills" / "pi-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from state_model import empty_checkpoint  # noqa: E402
import pulse  # noqa: E402


class PiSchedulingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL.read_text(encoding="utf-8")

    def test_one_agent_run_cannot_enter_its_successor_wake(self) -> None:
        self.assertIn(
            "One pi agent run may execute exactly one pulse wake.",
            self.skill,
        )
        self.assertIn(
            "the current pi agent run is over. Return the final\n"
            "wake status immediately and make no further tool calls.",
            self.skill,
        )
        self.assertIn(
            "Only the future scheduled user message may\n"
            "start that successor wake.",
            self.skill,
        )
        self.assertIn(
            "# HARD STOP: return the final status and end this pi agent run now.",
            self.skill,
        )

    def test_begin_wake_requires_direct_checkpoint_preflight(self) -> None:
        boundary = self.skill.index("### Mandatory pi wake boundary")
        begin = self.skill.index("PULSE TARGET --wake-id WAKE_ID begin-wake", boundary)
        preflight = self.skill[boundary:begin]
        self.assertIn("active_wake_id", preflight)
        self.assertIn("failure_latch", preflight)
        self.assertIn("next_not_before", preflight)
        self.assertIn("CURRENT_UTC < next_not_before", preflight)
        self.assertIn("WAKE_ID = new opaque ID generated for this delivered message", preflight)
        self.assertIn("without calling `begin-wake`", preflight)
        self.assertIn(
            "A missing checkpoint is allowed only for the user's\n"
            "initial explicit request",
            preflight,
        )

    def test_wake_id_and_successor_prompt_cannot_reuse_prior_state(self) -> None:
        self.assertIn(
            "Never reuse a wake ID from a prompt, notebook, todo, checkpoint, or\n"
            "prior failed attempt.",
            self.skill,
        )
        handoff = self.skill[self.skill.index("## Scheduled-task handoff") :]
        self.assertIn(
            "identifies itself as a newly delivered one-shot\n"
            "   wake and repeats the checkpoint preflight and fresh-ID requirements",
            handoff,
        )

    def test_scheduler_status_cannot_confuse_pending_with_consumed(self) -> None:
        self.assertIn("`✓` means enabled, `✗` means disabled, and `!` means", self.skill)
        self.assertIn(
            "`Never run`, `Runs: 0`, and `Status: pending` means a one-shot has not fired",
            self.skill,
        )
        self.assertIn(
            "`Runs: 1` and `Status: success` means that one-shot fired successfully",
            self.skill,
        )
        self.assertIn("the normal auto-disabled state, not a failure", self.skill)
        self.assertIn("Never report an unfired one-shot as consumed", self.skill)

    def test_handoff_requires_one_shot_and_immediate_turn_end(self) -> None:
        handoff = self.skill[self.skill.index("## Scheduled-task handoff") :]
        self.assertIn("`schedule_prompt add` with `type=once`", handoff)
        self.assertIn("Do not use a fixed interval", handoff)
        self.assertIn("Require the returned non-empty `jobId`", handoff)
        self.assertIn("Make no more tool calls", handoff)
        self.assertIn("do not list or\n    inspect the successor", handoff)
        self.assertIn("Then immediately end the current agent run", handoff)
        self.assertNotIn("type=interval", handoff)


class PrematureWakeRegressionTests(unittest.TestCase):
    @staticmethod
    def _snapshot() -> dict:
        return {
            "head_oid": "HEAD1",
            "pull_request_state": "OPEN",
            "targeted_thread_ids": [],
            "review_in_progress": {"active": False},
            "review_activity_ok": True,
            "approval_evidence": {"status": "awaiting_current_head_approval"},
            "snapshot_stable": True,
            "server_evidence": {"head_before": "HEAD1", "head_after": "HEAD1"},
        }

    def test_early_wake_latches_and_neither_reused_nor_fresh_id_recovers(self) -> None:
        state, _ = pulse.begin_wake(
            empty_checkpoint("Owner/Repo", 17),
            wake_id="wake-1",
            now="2026-08-26T00:00:00+00:00",
            pause_heartbeat=lambda: True,
        )
        state, _ = pulse.record_snapshot(
            state,
            self._snapshot(),
            wake_id="wake-1",
            now="2026-08-26T00:01:00+00:00",
        )
        state, _ = pulse.complete_wake(
            state,
            wake_id="wake-1",
            now="2026-08-26T00:26:00+00:00",
            schedule_next_wake=lambda _: True,
        )

        state, blocked = pulse.begin_wake(
            state,
            wake_id="wake-2-early",
            now="2026-08-26T00:30:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(blocked["next_action"], "PAUSE_BLOCKED")
        self.assertEqual(blocked["reason_code"], "cadence_not_elapsed")
        self.assertEqual(state["failure_latch"]["reason_code"], "cadence_not_elapsed")
        self.assertEqual(state["wake_count"], 1)

        same_state, same_result = pulse.begin_wake(
            state,
            wake_id="wake-2-early",
            now="2026-08-26T00:40:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(same_result, blocked)
        self.assertEqual(same_state["wake_count"], 1)

        fresh_state, fresh_result = pulse.begin_wake(
            state,
            wake_id="wake-3-fresh",
            now="2026-08-26T00:40:00+00:00",
            pause_heartbeat=lambda: True,
        )
        self.assertEqual(fresh_result["next_action"], "PAUSE_RECOVERY")
        self.assertEqual(fresh_result["reason_code"], "failure_latched")
        self.assertEqual(fresh_state["wake_count"], 1)


if __name__ == "__main__":
    unittest.main()
