# Vendored from codex-review-pulse/skills/codex-review-pulse/scripts/resolve_thread.py
#!/usr/bin/env python3
"""Resolve one frozen GitHub review thread after fail-closed PR-scope checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from checkpoint_store import checkpoint_path, load_checkpoint, save_checkpoint
from state_model import (
    DEFAULT_CODEX_LOGINS,
    canonical_repository,
    normalize_login,
    record_resolved_thread,
    unique_logins,
    validate_checkpoint,
)


VERIFY_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequest(number: $number) {
      number headRefOid
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved
          comments(first: 1) { nodes { author { login } } }
        }
      }
    }
  }
}
"""

MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def _github_cli_command() -> list[str]:
    """Return the GitHub CLI command, with a narrow network-free test hook."""
    fixture_script = os.environ.get("CODEX_REVIEW_PULSE_GH_SCRIPT")
    if fixture_script:
        return [sys.executable, fixture_script]
    return ["gh"]


def graphql(query: str, variables: dict[str, object]) -> dict[str, Any]:
    command = [*_github_cli_command(), "api", "graphql", "-F", "query=@-"]
    for name, value in variables.items():
        if value is not None:
            command.extend(["-F", f"{name}={value}"])
    process = subprocess.run(
        command, input=query, capture_output=True, text=True
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip())
    try:
        payload: dict[str, Any] = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("GitHub GraphQL response was not JSON") from error
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    return payload


def fetch_pr_threads(
    repository: str,
    pr_number: int,
    *,
    graphql_call: Callable[[str, dict[str, object]], dict[str, Any]] = graphql,
) -> tuple[str, int, str, list[dict[str, Any]]]:
    owner, repo = repository.split("/", 1)
    cursor: str | None = None
    nodes: list[dict[str, Any]] = []
    canonical_name: str | None = None
    actual_number: int | None = None
    head_oid: str | None = None
    while True:
        payload = graphql_call(
            VERIFY_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number, "cursor": cursor},
        )
        repository_node = payload["data"].get("repository")
        if repository_node is None:
            raise RuntimeError(f"Repository not found: {repository}")
        pull_request = repository_node.get("pullRequest")
        if pull_request is None:
            raise RuntimeError(f"Pull request not found: {repository}#{pr_number}")
        canonical_name = repository_node["nameWithOwner"]
        actual_number = pull_request["number"]
        page_head_oid = pull_request["headRefOid"]
        if head_oid is not None and page_head_oid != head_oid:
            raise RuntimeError("Pull request head advanced while verifying thread scope")
        head_oid = page_head_oid
        connection = pull_request["reviewThreads"]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError("reviewThreads pagination did not return an end cursor")
    if canonical_name is None or actual_number is None or head_oid is None:
        raise RuntimeError("GitHub did not return pull request identity")
    return canonical_name, actual_number, head_oid, nodes


def verify_thread_scope(
    *,
    requested_repository: str,
    requested_pr: int,
    actual_repository: str,
    actual_pr: int,
    thread_id: str,
    expected_thread_ids: list[str],
    review_threads: list[dict[str, Any]],
    reviewer_logins: list[str],
    expected_head_oid: str | None = None,
    actual_head_oid: str | None = None,
) -> dict[str, Any]:
    """Verify repository, PR, expected set, and global thread ownership."""
    if canonical_repository(actual_repository) != canonical_repository(requested_repository):
        raise RuntimeError("GitHub returned a different canonical repository")
    if actual_pr != requested_pr:
        raise RuntimeError("GitHub returned a different pull request number")
    if expected_head_oid is not None and actual_head_oid != expected_head_oid:
        raise RuntimeError("Pull request head does not match the frozen batch head")
    expected = set(expected_thread_ids)
    if not expected:
        raise RuntimeError("An intended frozen thread set is required")
    if thread_id not in expected:
        raise RuntimeError("Requested thread is not in the expected thread set")
    by_id = {
        thread.get("id"): thread
        for thread in review_threads
        if isinstance(thread.get("id"), str)
    }
    missing = sorted(expected - set(by_id))
    if missing:
        raise RuntimeError(
            "Expected thread set contains IDs outside the requested PR: "
            + ", ".join(missing)
        )
    reviewer_keys = set(unique_logins(reviewer_logins, label="reviewer"))
    non_target_ids = []
    for expected_id in sorted(expected):
        comments = (by_id[expected_id].get("comments") or {}).get("nodes") or []
        root_login = None
        if comments:
            root_login = normalize_login(
                (comments[0].get("author") or {}).get("login")
            )
        if root_login not in reviewer_keys:
            non_target_ids.append(expected_id)
    if non_target_ids:
        raise RuntimeError(
            "Expected thread set contains non-target root authors: "
            + ", ".join(non_target_ids)
        )
    return by_id[thread_id]


def resolve_exact_thread(
    *,
    repository: str,
    pr_number: int,
    thread_id: str,
    expected_thread_ids: list[str],
    reviewer_logins: list[str] | None = None,
    expected_head_oid: str | None = None,
    before_mutation: Callable[[], None] | None = None,
    graphql_call: Callable[[str, dict[str, object]], dict[str, Any]] = graphql,
) -> dict[str, Any]:
    actual_repository, actual_pr, actual_head_oid, review_threads = fetch_pr_threads(
        repository, pr_number, graphql_call=graphql_call
    )
    verified = verify_thread_scope(
        requested_repository=repository,
        requested_pr=pr_number,
        actual_repository=actual_repository,
        actual_pr=actual_pr,
        thread_id=thread_id,
        expected_thread_ids=expected_thread_ids,
        review_threads=review_threads,
        reviewer_logins=reviewer_logins or list(DEFAULT_CODEX_LOGINS),
        expected_head_oid=expected_head_oid,
        actual_head_oid=actual_head_oid,
    )
    if verified.get("isResolved") is True:
        return {"id": thread_id, "isResolved": True, "alreadyResolved": True}

    if before_mutation is not None:
        before_mutation()
    payload = graphql_call(MUTATION, {"threadId": thread_id})
    thread = payload["data"]["resolveReviewThread"]["thread"]
    if thread["id"] != thread_id or thread["isResolved"] is not True:
        raise RuntimeError("GitHub did not confirm the requested thread as resolved")
    return thread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("thread_id")
    parser.add_argument("--repo", required=True, help="Canonical base repository as OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number")
    parser.add_argument(
        "--reviewer-login", action="append", default=[],
        help="Targeted Codex root-author login; repeat for multiple identities",
    )
    parser.add_argument("--state-file", type=Path, help="Override the checkpoint path")
    parser.add_argument("--repository-path", default=".", help="Target worktree")
    parser.add_argument("--run-contract", type=Path, help="Bounded recurring run contract")
    parser.add_argument("--lease-owner-token", help="Owner token for recurring thread resolution")
    return parser.parse_args()


def select_resolution_context(
    *,
    checkpoint: dict[str, Any] | None,
    explicit_expected_ids: list[str],
    configured_reviewer_logins: list[str],
    thread_id: str,
) -> tuple[list[str], list[str], str | None]:
    """Select a persisted, head-bound active batch for exact resolution."""
    batch = (checkpoint or {}).get("active_batch")
    if isinstance(batch, dict):
        if explicit_expected_ids:
            raise RuntimeError("Explicit expected IDs cannot override an active frozen batch")
        epoch = (checkpoint or {}).get("approval_epoch") or {}
        if batch.get("frozen_head_oid") != epoch.get("head_oid"):
            raise RuntimeError("Frozen batch head does not match the latest checkpoint head")
        if thread_id not in batch.get("thread_outcomes", {}):
            raise RuntimeError("Record the thread outcome before exact resolution")
        batch_reviewers = batch.get("reviewer_logins") or list(DEFAULT_CODEX_LOGINS)
        if configured_reviewer_logins:
            configured = unique_logins(configured_reviewer_logins, label="reviewer")
            if set(configured) != set(batch_reviewers):
                raise RuntimeError("Reviewer logins do not match the active frozen batch")
        return (
            batch.get("targeted_thread_ids") or [],
            batch_reviewers,
            batch.get("frozen_head_oid"),
        )

    raise RuntimeError(
        "An active frozen batch checkpoint is required before exact resolution"
    )


def main() -> None:
    args = parse_args()
    if args.pr < 1:
        raise RuntimeError("--pr must be positive")
    path = args.state_file or checkpoint_path(
        args.repo, args.pr, repository_path=args.repository_path
    )
    checkpoint = load_checkpoint(path)
    if checkpoint is not None:
        validate_checkpoint(checkpoint, args.repo, args.pr)
    if bool(args.run_contract) != bool(args.lease_owner_token):
        raise RuntimeError("Recurring resolution requires both run contract and lease owner token")
    contract = None
    if args.run_contract:
        from recurring_contract import assert_mutation_authority, load_mutation_run_contract

        contract = load_mutation_run_contract(
            args.run_contract,
            repository_path=args.repository_path,
            owner_token=args.lease_owner_token,
        )
        if (
            contract["repository"] != args.repo.casefold()
            or contract["pull_request_number"] != args.pr
            or Path(contract["paths"]["checkpoint"]).resolve() != Path(path).resolve()
        ):
            raise RuntimeError("Run contract does not bind this checkpoint target")
        assert_mutation_authority(
            contract,
            contract_path=args.run_contract,
            owner_token=args.lease_owner_token,
            required_scope="resolve_threads",
            runtime_script_path=__file__,
        )

    expected_ids, reviewer_logins, expected_head_oid = select_resolution_context(
        checkpoint=checkpoint,
        explicit_expected_ids=[],
        configured_reviewer_logins=args.reviewer_login,
        thread_id=args.thread_id,
    )

    def recheck_mutation_authority() -> None:
        if contract is not None:
            assert_mutation_authority(
                contract,
                contract_path=args.run_contract,
                owner_token=args.lease_owner_token,
                required_scope="resolve_threads",
                runtime_script_path=__file__,
            )

    thread = resolve_exact_thread(
        repository=args.repo,
        pr_number=args.pr,
        thread_id=args.thread_id,
        expected_thread_ids=expected_ids,
        reviewer_logins=reviewer_logins,
        expected_head_oid=expected_head_oid,
        before_mutation=recheck_mutation_authority,
    )
    batch = (checkpoint or {}).get("active_batch")
    if isinstance(batch, dict) and args.thread_id in batch.get("targeted_thread_ids", []):
        checkpoint = record_resolved_thread(checkpoint, args.thread_id)
        save_checkpoint(path, checkpoint)
    print(json.dumps(thread))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
