from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "pi-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from checkpoint_store import load_checkpoint, save_checkpoint  # noqa: E402
from fetch_pr_state import select_evaluation_identities, verify_stable_head  # noqa: E402
from state_model import (  # noqa: E402
    classify_unresolved_threads,
    empty_checkpoint,
    evaluate_snapshot,
    freeze_batch,
    record_publication_failure,
    record_resolved_thread,
    record_thread_outcome,
    unique_logins,
    validate_freeze_request,
)


FIXTURE = ROOT / "tests" / "fixtures" / "review_threads.json"


class FetchIdentityTests(unittest.TestCase):
    def test_recurring_identities_come_from_the_run_contract(self) -> None:
        contract = {
            "reviewer_logins": ["contract-reviewer"],
            "approval_logins": ["contract-approver"],
        }
        self.assertEqual(
            select_evaluation_identities(
                reviewer_logins=None,
                approval_logins=None,
                run_contract=contract,
            ),
            (["contract-reviewer"], ["contract-approver"]),
        )
        with self.assertRaisesRegex(RuntimeError, "Reviewer logins"):
            select_evaluation_identities(
                reviewer_logins=["default-reviewer"],
                approval_logins=None,
                run_contract=contract,
            )


def reaction(reaction_id: str, login: str = "chatgpt-codex-connector") -> dict:
    return {
        "id": reaction_id,
        "content": "THUMBS_UP",
        "createdAt": "2026-08-25T00:00:00Z",
        "user": {"login": login},
    }


def eyes_reaction(reaction_id: str, login: str = "chatgpt-codex-connector") -> dict:
    return {
        "id": reaction_id,
        "content": "EYES",
        "createdAt": "2026-08-25T00:00:00Z",
        "user": {"login": login},
    }


def evaluate(
    *,
    head: str,
    threads: list[dict] | None = None,
    reactions: list[dict] | None = None,
    review_activity_reactions: list[dict] | None = None,
    checkpoint: dict | None = None,
    reviewer_logins: list[str] | None = None,
    approval_logins: list[str] | None = None,
    reviews: list[dict] | None = None,
) -> tuple[dict, dict]:
    return evaluate_snapshot(
        repository="Owner/Repo",
        pr_number=17,
        head_oid=head,
        pull_request_state="OPEN",
        review_threads=threads or [],
        reactions=reactions or [],
        review_activity_reactions=review_activity_reactions or [],
        reviews=reviews or [],
        reviewer_logins=reviewer_logins,
        approval_logins=approval_logins,
        checkpoint=checkpoint,
        observed_at="2026-08-25T00:00:00+00:00",
    )


class ReviewerScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.threads = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_codex_root_author_and_bot_suffix_are_targeted(self) -> None:
        logins = unique_logins(
            ["chatgpt-codex-connector", "CHATGPT-CODEX-CONNECTOR[bot]"],
            label="reviewer",
        )
        targeted, non_target = classify_unresolved_threads(self.threads, logins)
        self.assertEqual(targeted, ["T_CODEX", "T_CODEX_BOT"])
        self.assertEqual(
            [item["id"] for item in non_target],
            ["T_HUMAN", "T_UNKNOWN", "T_NO_COMMENTS"],
        )

    def test_human_and_unknown_authors_fail_closed(self) -> None:
        _, non_target = classify_unresolved_threads(
            self.threads, ["chatgpt-codex-connector"]
        )
        reasons = {item["id"]: item["reason"] for item in non_target}
        self.assertEqual(reasons["T_HUMAN"], "non_target_root_author")
        self.assertEqual(reasons["T_UNKNOWN"], "unknown_root_author")
        self.assertEqual(reasons["T_NO_COMMENTS"], "unknown_root_author")

    def test_codex_eyes_is_wait_only_review_activity_evidence(self) -> None:
        result, _ = evaluate(
            head="A",
            review_activity_reactions=[
                eyes_reaction("E1", "CHATGPT-CODEX-CONNECTOR[bot]"),
                eyes_reaction("E2", "human-reviewer"),
            ],
        )
        self.assertTrue(result["review_activity_ok"])
        self.assertTrue(result["codex_review_in_progress"])
        self.assertEqual(
            [item["id"] for item in result["codex_review_in_progress_reactions"]],
            ["E1"],
        )
        self.assertEqual(result["approval_status"], "awaiting_current_head_approval")
        self.assertFalse(result["codex_terminal"])

    def test_invalid_eyes_reaction_id_fails_activity_evidence_closed(self) -> None:
        conflicting = eyes_reaction("E1")
        other = eyes_reaction("E1")
        other["createdAt"] = "2026-08-25T00:01:00Z"
        result, _ = evaluate(
            head="A",
            review_activity_reactions=[conflicting, other],
        )
        self.assertFalse(result["review_activity_ok"])
        self.assertFalse(result["codex_review_in_progress"])
        self.assertEqual(result["invalid_review_activity_reaction_ids"], ["E1"])

    def test_human_thread_cannot_be_added_to_frozen_target_set(self) -> None:
        _, checkpoint = evaluate(head="A", threads=self.threads)
        with self.assertRaisesRegex(ValueError, "latest targeted snapshot"):
            validate_freeze_request(
                checkpoint, "A", ["T_CODEX", "T_CODEX_BOT", "T_HUMAN"]
            )

    def test_custom_reviewer_identity_is_persisted_into_batch(self) -> None:
        custom_thread = [
            {
                "id": "T_CUSTOM",
                "isResolved": False,
                "comments": {"nodes": [{"author": {"login": "Custom-Codex[bot]"}}]},
            }
        ]
        _, checkpoint = evaluate(
            head="A", threads=custom_thread, reviewer_logins=["custom-codex"]
        )
        requested = validate_freeze_request(checkpoint, "A", ["T_CUSTOM"])
        checkpoint = freeze_batch(checkpoint, "A", requested)
        self.assertEqual(
            checkpoint["active_batch"]["reviewer_logins"], ["custom-codex"]
        )

    def test_non_target_threads_are_reported_but_not_codex_terminal_blockers(self) -> None:
        human_only = [thread for thread in self.threads if thread["id"] == "T_HUMAN"]
        _, checkpoint = evaluate(head="A", threads=human_only)
        result, _ = evaluate(
            head="A", threads=human_only, reactions=[reaction("R1")], checkpoint=checkpoint
        )
        self.assertEqual(result["targeted_unresolved_thread_ids"], [])
        self.assertEqual(
            [item["id"] for item in result["non_target_unresolved_threads"]],
            ["T_HUMAN"],
        )
        self.assertTrue(result["codex_terminal"])


class ApprovalEpochTests(unittest.TestCase):
    def test_current_head_approval_terminates_without_quiet_interval(self) -> None:
        _, checkpoint = evaluate(head="A")
        result, _ = evaluate(head="A", reactions=[reaction("R1")], checkpoint=checkpoint)
        self.assertEqual(result["approval_status"], "approved_current_head")
        self.assertTrue(result["codex_terminal"])

    def test_old_approval_cannot_approve_newer_head(self) -> None:
        _, checkpoint = evaluate(head="A")
        approved, checkpoint = evaluate(
            head="A", reactions=[reaction("R1")], checkpoint=checkpoint
        )
        self.assertTrue(approved["codex_terminal"])
        newer, _ = evaluate(
            head="B", reactions=[reaction("R1")], checkpoint=checkpoint
        )
        self.assertEqual(newer["approval_status"], "ambiguous_existing_reaction")
        self.assertFalse(newer["codex_terminal"])

    def test_existing_reaction_persists_after_head_change(self) -> None:
        _, checkpoint = evaluate(head="A", reactions=[reaction("R1")])
        newer, _ = evaluate(head="B", reactions=[reaction("R1")], checkpoint=checkpoint)
        self.assertEqual(newer["approval_epoch_transition"], "head_changed")
        self.assertEqual(newer["approval_status"], "ambiguous_existing_reaction")
        self.assertEqual(newer["proven_current_head_reaction_ids"], [])

    def test_repeated_same_reaction_id_is_not_new_evidence(self) -> None:
        _, checkpoint = evaluate(head="A", reactions=[reaction("R1")])
        repeated, _ = evaluate(
            head="A",
            reactions=[reaction("R1"), reaction("R1")],
            checkpoint=checkpoint,
        )
        self.assertEqual(repeated["approval_status"], "ambiguous_existing_reaction")
        self.assertEqual(repeated["proven_current_head_reaction_ids"], [])

    def test_deleted_reaction_readded_with_new_id_is_new_epoch_evidence(self) -> None:
        _, checkpoint = evaluate(head="A", reactions=[reaction("R1")])
        awaiting, checkpoint = evaluate(head="A", reactions=[], checkpoint=checkpoint)
        self.assertEqual(awaiting["approval_status"], "awaiting_current_head_approval")
        approved, _ = evaluate(
            head="A", reactions=[reaction("R2")], checkpoint=checkpoint
        )
        self.assertEqual(approved["proven_current_head_reaction_ids"], ["R2"])
        self.assertTrue(approved["codex_terminal"])

    def test_checkpoint_persistence_survives_restart(self) -> None:
        _, checkpoint = evaluate(head="A")
        approved, checkpoint = evaluate(
            head="A", reactions=[reaction("R1")], checkpoint=checkpoint
        )
        self.assertTrue(approved["codex_terminal"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_checkpoint(path, checkpoint)
            restarted, _ = evaluate(
                head="A", reactions=[reaction("R1")], checkpoint=load_checkpoint(path)
            )
        self.assertEqual(restarted["proven_current_head_reaction_ids"], ["R1"])
        self.assertTrue(restarted["codex_terminal"])

    def test_cold_start_existing_approval_is_ambiguous(self) -> None:
        result, _ = evaluate(head="A", reactions=[reaction("R1")])
        self.assertEqual(result["approval_status"], "ambiguous_existing_reaction")
        self.assertFalse(result["codex_terminal"])

    def test_current_head_approved_review_is_direct_proof(self) -> None:
        approved_review = {
            "id": "REV1",
            "state": "APPROVED",
            "submittedAt": "2026-08-25T00:00:00Z",
            "author": {"login": "chatgpt-codex-connector"},
            "commit": {"oid": "A"},
            "url": "https://example.test/review/1",
        }
        result, _ = evaluate(head="A", reviews=[approved_review])
        self.assertEqual(result["approval_proof"], "pull_request_review")
        self.assertEqual(result["approval_status"], "approved_current_head")
        self.assertTrue(result["codex_terminal"])

    def test_approved_review_for_old_commit_does_not_approve_head(self) -> None:
        old_review = {
            "id": "REV1",
            "state": "APPROVED",
            "author": {"login": "chatgpt-codex-connector"},
            "commit": {"oid": "OLD"},
        }
        result, _ = evaluate(head="NEW", reviews=[old_review])
        self.assertEqual(result["approval_status"], "awaiting_current_head_approval")
        self.assertIsNone(result["approval_proof"])
        self.assertFalse(result["codex_terminal"])

    def test_duplicate_logins_and_reaction_ids_are_deterministic(self) -> None:
        _, checkpoint = evaluate(
            head="A",
            reviewer_logins=["Codex[bot]", "CODEX"],
            approval_logins=["Codex", "codex[bot]"],
        )
        duplicate = reaction("R1", "CODEX[bot]")
        conflict_a = reaction("R2", "codex")
        conflict_b = reaction("R2", "someone-else")
        result, _ = evaluate(
            head="A",
            reactions=[duplicate, duplicate.copy(), conflict_b, conflict_a],
            checkpoint=checkpoint,
            reviewer_logins=["Codex[bot]", "CODEX"],
            approval_logins=["Codex", "codex[bot]"],
        )
        self.assertEqual(result["reviewer_logins"], ["codex"])
        self.assertEqual(result["approval_logins"], ["codex"])
        self.assertEqual(
            [item["id"] for item in result["qualifying_approval_reactions"]], ["R1"]
        )
        self.assertEqual(result["invalid_reaction_ids"], ["R2"])


class BatchRecoveryTests(unittest.TestCase):
    def test_publication_failure_preserves_resolved_thread_recovery_state(self) -> None:
        checkpoint = empty_checkpoint("Owner/Repo", 17)
        checkpoint = freeze_batch(checkpoint, "HEAD1", ["T1", "T2", "T1"])
        checkpoint = record_thread_outcome(
            checkpoint, thread_id="T1", classification="fix-now", reference="a.py"
        )
        checkpoint = record_resolved_thread(checkpoint, "T1")
        checkpoint = record_publication_failure(
            checkpoint,
            phase="push",
            pending_paths=["b.py", "a.py", "a.py"],
            pending_commit="abc123",
        )
        batch = checkpoint["active_batch"]
        self.assertEqual(batch["frozen_head_oid"], "HEAD1")
        self.assertEqual(batch["targeted_thread_ids"], ["T1", "T2"])
        self.assertEqual(
            batch["thread_outcomes"]["T1"],
            {"classification": "fix-now", "reference": "a.py"},
        )
        self.assertEqual(batch["resolved_thread_ids"], ["T1"])
        self.assertEqual(batch["publication"]["pending_paths"], ["a.py", "b.py"])
        self.assertEqual(batch["publication"]["pending_commit"], "abc123")

    def test_failed_batch_cannot_be_overwritten(self) -> None:
        checkpoint = empty_checkpoint("Owner/Repo", 17)
        checkpoint = freeze_batch(checkpoint, "HEAD1", ["T1"])
        checkpoint = record_thread_outcome(
            checkpoint, thread_id="T1", classification="no-fix"
        )
        checkpoint = record_resolved_thread(checkpoint, "T1")
        checkpoint = record_publication_failure(checkpoint, phase="validation")
        with self.assertRaisesRegex(ValueError, "recovered first"):
            freeze_batch(checkpoint, "HEAD2", ["T2"])


class SnapshotCoherenceTests(unittest.TestCase):
    def test_head_change_across_network_reads_is_rejected(self) -> None:
        initial = {
            "nameWithOwner": "Owner/Repo",
            "pullRequest": {"number": 17, "headRefOid": "OLD"},
        }
        final = {
            "nameWithOwner": "Owner/Repo",
            "pullRequest": {"number": 17, "headRefOid": "NEW"},
        }
        with self.assertRaisesRegex(RuntimeError, "head advanced"):
            verify_stable_head(initial, final, 17)


if __name__ == "__main__":
    unittest.main()
