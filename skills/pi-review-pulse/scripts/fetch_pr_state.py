# Vendored from codex-review-pulse/skills/codex-review-pulse/scripts/fetch_pr_state.py
#!/usr/bin/env python3
"""Fetch GitHub PR evidence and evaluate Codex-specific review state."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
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
    evaluate_snapshot,
    unique_logins,
)


def _github_cli_command() -> list[str]:
    """Return the GitHub CLI command, with a narrow network-free test hook."""
    fixture_script = os.environ.get("CODEX_REVIEW_PULSE_GH_SCRIPT")
    if fixture_script:
        return [sys.executable, fixture_script]
    return ["gh"]


def run(
    command: list[str],
    stdin: str | None = None,
    *,
    cwd: str | Path | None = None,
) -> str:
    if command and command[0] == "gh":
        command = [*_github_cli_command(), *command[1:]]
    process = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )
    if process.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{process.stderr.strip()}")
    return process.stdout


def run_json(
    command: list[str],
    stdin: str | None = None,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    try:
        return json.loads(run(command, stdin, cwd=cwd))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Command did not return JSON: {' '.join(command)}") from error


def graphql(
    query: str,
    owner: str,
    repo: str,
    number: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    command = [
        "gh", "api", "graphql", "-F", "query=@-",
        "-F", f"owner={owner}", "-F", f"repo={repo}",
        "-F", f"number={number}",
    ]
    if cursor:
        command.extend(["-F", f"cursor={cursor}"])
    payload = run_json(command, query)
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(errors)}")
    return payload


def fetch_connection(
    query: str,
    connection_name: str,
    owner: str,
    repo: str,
    number: int,
    *,
    graphql_call: Callable[[str, str, str, int, str | None], dict[str, Any]] = graphql,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = graphql_call(query, owner, repo, number, cursor)
        pull_request = payload["data"]["repository"]["pullRequest"]
        if pull_request is None:
            raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")
        connection = pull_request[connection_name]
        nodes.extend(connection.get("nodes") or [])
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return nodes
        cursor = page_info["endCursor"]
        if not cursor:
            raise RuntimeError(f"{connection_name} pagination did not return an end cursor")


META_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequest(number: $number) {
      number url title state isDraft mergeable reviewDecision
      headRefName headRefOid baseRefName updatedAt
      headRepository { nameWithOwner }
      author { login }
    }
  }
}
"""

COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId body createdAt updatedAt author { login } url }
      }
    }
  }
}
"""

REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id databaseId state body submittedAt updatedAt author { login } url
          commit { oid }
        }
      }
    }
  }
}
"""

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line diffSide
          startLine startDiffSide originalLine originalStartLine
          resolvedBy { login }
          comments(first: 100) {
            nodes { id databaseId body createdAt updatedAt author { login } url }
          }
        }
      }
    }
  }
}
"""

REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reactions(first: 100, after: $cursor, content: THUMBS_UP) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
      }
    }
  }
}
"""

EYES_REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reactions(first: 100, after: $cursor, content: EYES) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
      }
    }
  }
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        help="Canonical base repository as OWNER/REPO; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number; omitted fields are inferred from the current PR checkout",
    )
    parser.add_argument(
        "--reviewer-login", action="append", dest="reviewer_logins", metavar="LOGIN",
        help="Targeted Codex root-author login; repeat for multiple identities",
    )
    parser.add_argument(
        "--approval-login", action="append", dest="approval_logins", metavar="LOGIN",
        help="Allowed Codex approval login; repeat for multiple identities",
    )
    parser.add_argument(
        "--state-file", type=Path,
        help="Override the checkpoint path (primarily for controlled testing)",
    )
    parser.add_argument(
        "--repository-path", default=".",
        help="Target worktree used to locate the Git common directory",
    )
    parser.add_argument("--run-contract", type=Path, help="Bounded recurring run contract")
    parser.add_argument("--lease-owner-token", help="Owner token for recurring checkpoint writes")
    return parser.parse_args()


def resolve_target(
    repo_arg: str | None,
    pr_arg: int | None,
    *,
    repository_path: str | Path = ".",
) -> tuple[str, str, int]:
    """Resolve one canonical PR target, preferring explicit CLI fields."""
    repository = repo_arg
    if repository is None:
        repository = run_json(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=repository_path,
        ).get("nameWithOwner")
    if not isinstance(repository, str):
        raise RuntimeError("GitHub CLI did not identify the checkout repository")
    try:
        canonical = canonical_repository(repository)
    except ValueError as error:
        raise RuntimeError("--repo must be OWNER/REPO") from error

    number = pr_arg
    if number is None:
        pr_command = ["gh", "pr", "view"]
        if repo_arg is not None:
            pr_command.extend(["--repo", canonical])
        pr_command.extend(["--json", "number"])
        current = run_json(
            pr_command,
            cwd=repository_path,
        )
        number = current.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise RuntimeError(
            "GitHub CLI did not identify a unique current PR; pass --repo OWNER/REPO and --pr NUMBER"
        )
    owner, repo = canonical.split("/", 1)
    return owner, repo, number


def verify_stable_head(
    initial_repository: dict[str, Any],
    final_repository: dict[str, Any],
    pr_number: int,
) -> dict[str, Any]:
    """Reject connection data collected across a PR head transition."""
    if initial_repository.get("nameWithOwner") != final_repository.get("nameWithOwner"):
        raise RuntimeError("Canonical repository changed while fetching PR state")
    initial_pr = initial_repository.get("pullRequest")
    final_pr = final_repository.get("pullRequest")
    if initial_pr is None or final_pr is None:
        raise RuntimeError(
            f"Pull request disappeared while fetching state: "
            f"{initial_repository.get('nameWithOwner')}#{pr_number}"
        )
    if initial_pr.get("number") != pr_number or final_pr.get("number") != pr_number:
        raise RuntimeError("GitHub returned a different pull request number")
    if initial_pr.get("headRefOid") != final_pr.get("headRefOid"):
        raise RuntimeError("Pull request head advanced while fetching state; retry the snapshot")
    return final_pr


def verify_stable_review_artifacts(
    initial_review_threads: list[dict[str, Any]],
    final_review_threads: list[dict[str, Any]],
    initial_review_activity: list[dict[str, Any]],
    final_review_activity: list[dict[str, Any]],
    *,
    initial_approval_reactions: list[dict[str, Any]] | None = None,
    final_approval_reactions: list[dict[str, Any]] | None = None,
    initial_approval_reviews: list[dict[str, Any]] | None = None,
    final_approval_reviews: list[dict[str, Any]] | None = None,
) -> None:
    """Reject a snapshot assembled across review or approval artifact changes."""
    def fingerprint(nodes: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(
            sorted(
                json.dumps(node, sort_keys=True, separators=(",", ":"))
                for node in nodes
            )
        )

    if (
        fingerprint(initial_review_threads) != fingerprint(final_review_threads)
        or fingerprint(initial_review_activity) != fingerprint(final_review_activity)
    ):
        raise RuntimeError(
            "Review thread/activity artifacts changed while fetching state; retry the snapshot"
        )

    approval_artifact_pairs = (
        (initial_approval_reactions, final_approval_reactions),
        (initial_approval_reviews, final_approval_reviews),
    )
    if any(
        initial is not None
        and final is not None
        and fingerprint(initial) != fingerprint(final)
        for initial, final in approval_artifact_pairs
    ):
        raise RuntimeError(
            "Review approval artifacts changed while fetching state; retry the snapshot"
        )


def select_evaluation_identities(
    *,
    reviewer_logins: list[str] | None,
    approval_logins: list[str] | None,
    run_contract: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Derive recurring identities from authority, rejecting CLI drift."""
    if run_contract is None:
        return (
            unique_logins(reviewer_logins or DEFAULT_CODEX_LOGINS, label="reviewer"),
            unique_logins(approval_logins or DEFAULT_CODEX_LOGINS, label="approval"),
        )
    contract_reviewers = run_contract["reviewer_logins"]
    contract_approvers = run_contract["approval_logins"]
    if reviewer_logins and set(unique_logins(reviewer_logins, label="reviewer")) != set(
        contract_reviewers
    ):
        raise RuntimeError("Reviewer logins do not match the recurring run contract")
    if approval_logins and set(unique_logins(approval_logins, label="approval")) != set(
        contract_approvers
    ):
        raise RuntimeError("Approval logins do not match the recurring run contract")
    return list(contract_reviewers), list(contract_approvers)


def fetch_stable_snapshot(
    owner: str,
    repo: str,
    number: int,
    *,
    include_conversation: bool = True,
    graphql_call: Callable[[str, str, str, int, str | None], dict[str, Any]] = graphql,
) -> dict[str, Any]:
    """Fetch a head-bracketed, read-only PR snapshot."""
    initial_meta = graphql_call(META_QUERY, owner, repo, number, None)
    initial_repository = initial_meta["data"].get("repository")
    if initial_repository is None:
        raise RuntimeError(f"Repository not found: {owner}/{repo}")
    if initial_repository.get("pullRequest") is None:
        raise RuntimeError(f"Pull request not found: {owner}/{repo}#{number}")

    review_threads = fetch_connection(
        THREADS_QUERY, "reviewThreads", owner, repo, number,
        graphql_call=graphql_call,
    )
    thumbs_up_reactions = fetch_connection(
        REACTIONS_QUERY, "reactions", owner, repo, number,
        graphql_call=graphql_call,
    )
    eyes_reactions = fetch_connection(
        EYES_REACTIONS_QUERY, "reactions", owner, repo, number,
        graphql_call=graphql_call,
    )
    reviews = fetch_connection(
        REVIEWS_QUERY, "reviews", owner, repo, number,
        graphql_call=graphql_call,
    )
    conversation_comments = (
        fetch_connection(
            COMMENTS_QUERY, "comments", owner, repo, number,
            graphql_call=graphql_call,
        )
        if include_conversation
        else []
    )
    final_review_threads = fetch_connection(
        THREADS_QUERY, "reviewThreads", owner, repo, number,
        graphql_call=graphql_call,
    )
    final_eyes_reactions = fetch_connection(
        EYES_REACTIONS_QUERY, "reactions", owner, repo, number,
        graphql_call=graphql_call,
    )
    final_thumbs_up_reactions = fetch_connection(
        REACTIONS_QUERY, "reactions", owner, repo, number,
        graphql_call=graphql_call,
    )
    final_reviews = fetch_connection(
        REVIEWS_QUERY, "reviews", owner, repo, number,
        graphql_call=graphql_call,
    )
    verify_stable_review_artifacts(
        review_threads,
        final_review_threads,
        eyes_reactions,
        final_eyes_reactions,
        initial_approval_reactions=thumbs_up_reactions,
        final_approval_reactions=final_thumbs_up_reactions,
        initial_approval_reviews=reviews,
        final_approval_reviews=final_reviews,
    )
    final_meta = graphql_call(META_QUERY, owner, repo, number, None)
    final_repository = final_meta["data"].get("repository")
    if final_repository is None:
        raise RuntimeError(f"Repository disappeared while fetching state: {owner}/{repo}")
    pull_request = verify_stable_head(initial_repository, final_repository, number)
    return {
        "repository": final_repository["nameWithOwner"],
        "pull_request": pull_request,
        "review_threads": final_review_threads,
        "thumbs_up_reactions": final_thumbs_up_reactions,
        "eyes_reactions": final_eyes_reactions,
        "reviews": final_reviews,
        "conversation_comments": conversation_comments,
    }


def main() -> None:
    args = parse_args()
    run(["gh", "auth", "status"])
    owner, repo, number = resolve_target(
        args.repo,
        args.pr,
        repository_path=args.repository_path,
    )

    snapshot = fetch_stable_snapshot(owner, repo, number)
    pull_request = snapshot["pull_request"]
    canonical_repo = snapshot["repository"]
    review_threads = snapshot["review_threads"]
    thumbs_up_reactions = snapshot["thumbs_up_reactions"]
    eyes_reactions = snapshot["eyes_reactions"]
    conversation_comments = snapshot["conversation_comments"]
    reviews = snapshot["reviews"]

    state_path = args.state_file or checkpoint_path(
        canonical_repo, number, repository_path=args.repository_path
    )
    if bool(args.run_contract) != bool(args.lease_owner_token):
        raise RuntimeError(
            "Recurring checkpoint writes require both run contract and lease owner token"
        )
    contract = None
    if args.run_contract:
        from recurring_contract import assert_mutation_authority, load_mutation_run_contract

        contract = load_mutation_run_contract(
            args.run_contract,
            repository_path=args.repository_path,
            owner_token=args.lease_owner_token,
        )
        if (
            contract["repository"] != canonical_repo.casefold()
            or contract["pull_request_number"] != number
            or Path(contract["paths"]["checkpoint"]).resolve() != Path(state_path).resolve()
        ):
            raise RuntimeError("Run contract does not bind this checkpoint target")
    reviewer_logins, approval_logins = select_evaluation_identities(
        reviewer_logins=args.reviewer_logins,
        approval_logins=args.approval_logins,
        run_contract=contract,
    )
    previous_checkpoint = load_checkpoint(state_path)
    evaluation, next_checkpoint = evaluate_snapshot(
        repository=canonical_repo,
        pr_number=number,
        head_oid=pull_request["headRefOid"],
        pull_request_state=pull_request["state"],
        review_threads=review_threads,
        reactions=thumbs_up_reactions,
        review_activity_reactions=eyes_reactions,
        reviews=reviews,
        reviewer_logins=reviewer_logins,
        approval_logins=approval_logins,
        checkpoint=previous_checkpoint,
        observed_at=datetime.now(UTC).isoformat(),
    )
    if contract is not None:
        assert_mutation_authority(
            contract,
            contract_path=args.run_contract,
            owner_token=args.lease_owner_token,
            required_scope="recurring_execution",
            runtime_script_path=__file__,
        )
    save_checkpoint(state_path, next_checkpoint)

    result = {
        "viewer_login": run(["gh", "api", "user", "--jq", ".login"]).strip(),
        "repository": canonical_repo,
        "pull_request": pull_request,
        "checkpoint_path": str(state_path),
        "reviewer_logins": evaluation["reviewer_logins"],
        "approval_logins": evaluation["approval_logins"],
        "conversation_comments": conversation_comments,
        "reviews": reviews,
        "review_threads": review_threads,
        "targeted_unresolved_thread_ids": evaluation["targeted_unresolved_thread_ids"],
        "non_target_unresolved_threads": evaluation["non_target_unresolved_threads"],
        "thumbs_up_reactions": thumbs_up_reactions,
        "eyes_reactions": eyes_reactions,
        "codex_review_in_progress": evaluation["codex_review_in_progress"],
        "codex_review_in_progress_reactions": evaluation[
            "codex_review_in_progress_reactions"
        ],
        "review_in_progress_reaction_ids": [
            reaction["id"]
            for reaction in evaluation["codex_review_in_progress_reactions"]
        ],
        "review_activity_ok": evaluation["review_activity_ok"],
        "invalid_review_activity_reaction_ids": evaluation[
            "invalid_review_activity_reaction_ids"
        ],
        "qualifying_approval_reactions": evaluation["qualifying_approval_reactions"],
        "qualifying_current_head_approval_reviews": evaluation[
            "qualifying_current_head_approval_reviews"
        ],
        "excluded_approval_reviews": evaluation["excluded_approval_reviews"],
        "invalid_reaction_ids": evaluation["invalid_reaction_ids"],
        "approval_status": evaluation["approval_status"],
        "approval_proof": evaluation["approval_proof"],
        "approval_diagnostic": evaluation["approval_diagnostic"],
        "proven_current_head_reaction_ids": evaluation["proven_current_head_reaction_ids"],
        "approval_epoch_transition": evaluation["approval_epoch_transition"],
        "codex_terminal": evaluation["codex_terminal"],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
