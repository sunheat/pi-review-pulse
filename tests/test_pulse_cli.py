from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PULSE = ROOT / "skills" / "pi-review-pulse" / "scripts" / "pulse.py"
SCRIPTS = ROOT / "skills" / "pi-review-pulse" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from checkpoint_store import checkpoint_path, load_checkpoint  # noqa: E402


NOW = "2026-08-26T00:00:00+00:00"


FAKE_GH = r'''
import json
import os
from pathlib import Path
import sys


NOW = "2026-08-26T00:00:00+00:00"
fixture_path = Path(os.environ["PULSE_FAKE_GH_FIXTURE"])
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
counts_path = Path(os.environ["PULSE_FAKE_GH_COUNTS"])


def output(payload):
    print(json.dumps(payload))


def page(connection, nodes):
    return {
        "data": {
            "repository": {
                "nameWithOwner": fixture["repository"],
                "pullRequest": {
                    "number": fixture["number"],
                    "headRefOid": fixture["head_oid"],
                    connection: {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    },
                },
            }
        }
    }


def review_thread(thread):
    return {
        "id": thread["id"],
        "isResolved": thread.get("is_resolved", False),
        "comments": {
            "nodes": [
                {
                    "id": thread["id"] + "-comment",
                    "author": {"login": thread.get("root_author", "chatgpt-codex-connector")},
                    "url": "https://example.test/thread/" + thread["id"],
                }
            ]
        },
    }


arguments = sys.argv[1:]
if arguments[:2] == ["repo", "view"]:
    output({"nameWithOwner": fixture["repository"]})
    raise SystemExit(0)
if arguments[:2] == ["pr", "view"]:
    output({"nameWithOwner": fixture["repository"], "number": fixture.get("number")})
    raise SystemExit(0)
if arguments[:3] != ["api", "graphql", "-F"]:
    print("unsupported fake gh command", file=sys.stderr)
    raise SystemExit(2)

query = sys.stdin.read()
if "resolveReviewThread" in query:
    thread_id = next(
        value.split("=", 1)[1]
        for value in arguments
        if value.startswith("threadId=")
    )
    count = int(counts_path.read_text(encoding="utf-8")) if counts_path.exists() else 0
    counts_path.write_text(str(count + 1), encoding="utf-8")
    output({"data": {"resolveReviewThread": {"thread": {"id": thread_id, "isResolved": True}}}})
elif "reviewThreads" in query:
    output(page("reviewThreads", [review_thread(thread) for thread in fixture.get("threads", [])]))
elif "content: THUMBS_UP" in query:
    output(page("reactions", fixture.get("thumbs_up", [])))
elif "content: EYES" in query:
    output(page("reactions", fixture.get("eyes", [])))
elif "reviews(first" in query:
    output(page("reviews", fixture.get("reviews", [])))
elif "comments(first" in query:
    output(page("comments", fixture.get("comments", [])))
else:
    output({
        "data": {
            "repository": {
                "nameWithOwner": fixture["repository"],
                "pullRequest": {
                    "number": fixture["number"],
                    "state": fixture.get("state", "OPEN"),
                    "headRefOid": fixture["head_oid"],
                    "headRefName": "feature/test",
                    "baseRefName": "main",
                    "updatedAt": NOW,
                    "headRepository": {"nameWithOwner": fixture["repository"]},
                    "author": {"login": "owner"},
                },
            }
        }
    })
'''


class CliHarness:
    def __init__(self, test_case: unittest.TestCase, *, fixture: dict | None = None) -> None:
        self.directory = tempfile.TemporaryDirectory()
        test_case.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(self.checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.fake_bin = root / "bin"
        self.fake_bin.mkdir()
        (self.fake_bin / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
        (self.fake_bin / "gh.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake_gh.py" %*\r\n',
            encoding="utf-8",
        )
        self.fixture_path = root / "fixture.json"
        self.counts_path = root / "mutation-count.txt"
        self.write_fixture(fixture or self.default_fixture())

    @staticmethod
    def default_fixture() -> dict:
        return {
            "repository": "Owner/Repo",
            "number": 17,
            "head_oid": "HEAD1",
            "state": "OPEN",
            "threads": [],
            "thumbs_up": [],
            "eyes": [],
            "reviews": [],
            "comments": [],
        }

    def write_fixture(self, fixture: dict) -> None:
        self.fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    def read_fixture(self) -> dict:
        return json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def mutation_count(self) -> int:
        return int(self.counts_path.read_text(encoding="utf-8")) if self.counts_path.exists() else 0

    def run(self, *command: str, wake_id: str = "wake-1", now: str = NOW) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = str(self.fake_bin) + os.pathsep + environment.get("PATH", "")
        environment["PULSE_FAKE_GH_FIXTURE"] = str(self.fixture_path)
        environment["PULSE_FAKE_GH_COUNTS"] = str(self.counts_path)
        environment["CODEX_REVIEW_PULSE_GH_SCRIPT"] = str(self.fake_bin / "fake_gh.py")
        arguments = [
            sys.executable,
            str(PULSE),
            "--repository-path",
            str(self.checkout),
            "--wake-id",
            wake_id,
            "--now",
            now,
            *command,
        ]
        return subprocess.run(
            arguments,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def json_output(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assert_success(result)
        return json.loads(result.stdout)

    @staticmethod
    def assert_success(result: subprocess.CompletedProcess[str]) -> None:
        if result.returncode != 0:
            raise AssertionError(f"CLI failed: {result.stderr}\n{result.stdout}")

    def begin_and_snapshot(self, *, thread: dict | None = None) -> Path:
        if thread is not None:
            fixture = self.read_fixture()
            fixture["threads"] = [thread]
            self.write_fixture(fixture)
        self.json_output(self.run("begin-wake", "--pause-confirmed"))
        self.json_output(self.run("snapshot"))
        path = checkpoint_path("owner/repo", 17, repository_path=self.checkout)
        if not path.exists():
            raise AssertionError(f"Checkpoint was not created: {path}")
        return path


class PulseCliTests(unittest.TestCase):
    def test_host_confirmation_flags_are_public_in_help(self) -> None:
        root_help = subprocess.run(
            [sys.executable, str(PULSE), "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        begin_help = subprocess.run(
            [sys.executable, str(PULSE), "begin-wake", "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        complete_help = subprocess.run(
            [sys.executable, str(PULSE), "complete-wake", "--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        root_help = " ".join(root_help.split())
        self.assertIn("--pause-confirmed", root_help)
        self.assertIn("--schedule-reanchored", root_help)
        self.assertIn("retry", root_help)
        self.assertIn("configure-policy", root_help)
        self.assertIn("--pause-confirmed", begin_help)
        self.assertIn("--schedule-reanchored", complete_help)

    def test_prompt_policy_is_persisted_on_initial_wake_and_can_be_updated(self) -> None:
        harness = CliHarness(self)
        initial = harness.json_output(
            harness.run(
                "--policy-json",
                '{"max_wakes": 5, "allow_test_changes": false}',
                "begin-wake",
                "--pause-confirmed",
            )
        )
        self.assertEqual(initial["next_action"], "WAKE_STARTED")
        harness.json_output(harness.run("snapshot"))
        harness.json_output(
            harness.run("complete-wake", "--schedule-reanchored", now="2026-08-26T00:01:00+00:00")
        )
        updated = harness.json_output(
            harness.run(
                "--policy-json",
                '{"max_wakes": 8, "notifications": "every-wake"}',
                "configure-policy",
            )
        )
        self.assertEqual(updated["next_action"], "POLICY_UPDATED")
        state = load_checkpoint(checkpoint_path("owner/repo", 17, repository_path=harness.checkout))
        self.assertEqual(state["automation_policy"]["max_wakes"], 8)
        self.assertFalse(state["automation_policy"]["allow_test_changes"])
        self.assertEqual(state["automation_policy"]["notifications"], "every-wake")

    def test_infers_pr_target_and_reuses_one_persisted_wake(self) -> None:
        harness = CliHarness(self)
        begin = harness.run("begin-wake", "--pause-confirmed")
        harness.json_output(begin)
        path = checkpoint_path("owner/repo", 17, repository_path=harness.checkout)
        self.assertTrue(path.exists())
        self.assertTrue(str(path).startswith(str(harness.checkout / ".git")))

        first = harness.json_output(harness.run("snapshot"))
        second = harness.json_output(harness.run("snapshot", now="2026-08-26T00:01:00+00:00"))
        self.assertEqual(first["decision"], second["decision"])
        state = load_checkpoint(path)
        self.assertIsNotNone(state)
        self.assertEqual(state["repository"], "owner/repo")
        self.assertEqual(state["pull_request_number"], 17)
        self.assertEqual(state["wake_count"], 1)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(harness.checkout), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "",
        )

    def test_exact_resolve_requires_outcome_and_uses_one_mutation(self) -> None:
        harness = CliHarness(self)
        path = harness.begin_and_snapshot(
            thread={"id": "T1", "root_author": "chatgpt-codex-connector"}
        )
        harness.json_output(harness.run("freeze"))
        not_ready = harness.run("resolve", "--thread-id", "T1")
        self.assertNotEqual(not_ready.returncode, 0)
        self.assertEqual(harness.mutation_count(), 0)

        harness.json_output(
            harness.run(
                "record",
                "--thread-id",
                "T1",
                "--classification",
                "no-fix",
            )
        )
        state = load_checkpoint(path)
        self.assertEqual(state["active_batch"]["thread_outcomes"]["T1"]["classification"], "no-fix")
        resolved = harness.json_output(harness.run("resolve", "--thread-id", "T1"))
        self.assertTrue(resolved["resolved"])
        self.assertEqual(harness.mutation_count(), 1)
        replay = harness.json_output(harness.run("resolve", "--thread-id", "T1"))
        self.assertTrue(replay["alreadyResolved"])
        self.assertEqual(harness.mutation_count(), 1)

    def test_exact_resolve_fails_closed_for_head_author_and_batch_drift(self) -> None:
        cases = (
            ("head", {"head_oid": "HEAD2", "threads": [{"id": "T1", "root_author": "chatgpt-codex-connector"}]}, "T1"),
            ("author", {"threads": [{"id": "T1", "root_author": "human"}]}, "T1"),
            ("batch", {"threads": [{"id": "T1", "root_author": "chatgpt-codex-connector"}]}, "T2"),
        )
        for name, change, requested_id in cases:
            with self.subTest(case=name):
                harness = CliHarness(self)
                harness.begin_and_snapshot(
                    thread={"id": "T1", "root_author": "chatgpt-codex-connector"}
                )
                harness.json_output(harness.run("freeze"))
                harness.json_output(
                    harness.run(
                        "record",
                        "--thread-id",
                        "T1",
                        "--classification",
                        "no-fix",
                    )
                )
                fixture = harness.read_fixture()
                fixture.update(change)
                harness.write_fixture(fixture)
                result = harness.run("resolve", "--thread-id", requested_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(harness.mutation_count(), 0)

    def test_completion_is_relative_and_requires_host_reanchor_confirmation(self) -> None:
        paused = CliHarness(self)
        paused.begin_and_snapshot()
        result = paused.json_output(
            paused.run(
                "complete-wake",
                now="2026-08-26T00:26:00+00:00",
            )
        )
        self.assertEqual(result["next_action"], "PAUSE_BLOCKED")
        state = load_checkpoint(checkpoint_path("owner/repo", 17, repository_path=paused.checkout))
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["next_not_before"], "2026-08-26T00:36:00+00:00")

        reanchored = CliHarness(self)
        reanchored.begin_and_snapshot()
        result = reanchored.json_output(
            reanchored.run(
                "complete-wake",
                "--schedule-reanchored",
                now="2026-08-26T00:26:00+00:00",
            )
        )
        self.assertEqual(result["next_action"], "WAIT_REVIEW")
        state = load_checkpoint(checkpoint_path("owner/repo", 17, repository_path=reanchored.checkout))
        self.assertEqual(state["scheduled_task_disposition"], "ACTIVE")
        self.assertEqual(state["next_not_before"], "2026-08-26T00:36:00+00:00")

    def test_terminal_cli_result_stays_paused(self) -> None:
        fixture = CliHarness.default_fixture()
        fixture["reviews"] = [
            {
                "id": "R1",
                "state": "APPROVED",
                "commit": {"oid": "HEAD1"},
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
        harness = CliHarness(self, fixture=fixture)
        harness.json_output(harness.run("begin-wake", "--pause-confirmed"))
        result = harness.json_output(harness.run("snapshot"))
        self.assertEqual(result["decision"]["next_action"], "STOP_TERMINAL")
        state = load_checkpoint(checkpoint_path("owner/repo", 17, repository_path=harness.checkout))
        self.assertEqual(state["scheduled_task_disposition"], "PAUSED")
        self.assertEqual(state["wake_phase"], "terminal")

    def test_missing_current_pr_stops_without_guessing(self) -> None:
        harness = CliHarness(self)
        fixture = harness.read_fixture()
        fixture["number"] = None
        harness.write_fixture(fixture)
        result = harness.run("begin-wake", "--pause-confirmed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot identify a unique current pull request", result.stderr)


if __name__ == "__main__":
    unittest.main()
