from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "provenance" / "pr1-default-policy-pulse-repairs.json"


class CandidateProvenanceTests(unittest.TestCase):
    def test_default_policy_and_pulse_record_matches_candidate(self) -> None:
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(set(record), {"upstream", "mappings", "conformance_results"})
        self.assertEqual(
            record["upstream"],
            {
                "repository": "sunheat/codex-review-pulse",
                "commit": "d0a1a0073d15aa5eb88c511f3d77ffc77baee93b",
            },
        )

        mappings = {item["destination_path"]: item for item in record["mappings"]}
        self.assertEqual(
            set(mappings),
            {
                "skills/pi-review-pulse/scripts/default_policy.py",
                "skills/pi-review-pulse/scripts/pulse.py",
            },
        )
        expected_sources = {
            "skills/pi-review-pulse/scripts/default_policy.py": {
                "path": "skills/codex-review-pulse/scripts/default_policy.py",
                "blob": "759f6b14430fa879ee418d591c243f8a69c6f180",
                "sha256": "84b63ba7a37ff09e761600325929fe6b3df4516d140131ce0adc89a733fb0cb1",
            },
            "skills/pi-review-pulse/scripts/pulse.py": {
                "path": "skills/codex-review-pulse/scripts/pulse.py",
                "blob": "0302824aa14bd7e936c2ee6503eab41d8782ea68",
                "sha256": "28e0b4c19a528a4d3d39a84d26d79fd0cf2c5d92b43066c3b4b5d54b84c71b6e",
            },
        }
        for destination, expected in expected_sources.items():
            with self.subTest(destination=destination):
                mapping = mappings[destination]
                self.assertEqual(mapping["source_path"], expected["path"])
                self.assertEqual(mapping["source_git_blob_oid"], expected["blob"])
                self.assertEqual(mapping["source_content_sha256"], expected["sha256"])
                self.assertRegex(mapping["source_git_blob_oid"], r"^[0-9a-f]{40}$")
                self.assertRegex(mapping["source_content_sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(mapping["pi_local_adaptations"])
                self.assertTrue(mapping["exclusions"])
                actual = hashlib.sha256((ROOT / destination).read_bytes()).hexdigest()
                self.assertEqual(mapping["destination_content_sha256"], actual)

        self.assertEqual(
            record["conformance_results"],
            [
                {
                    "command": "python3 -m unittest discover -s tests -v",
                    "result": "passed",
                    "exit_code": 0,
                },
                {
                    "command": "python3 -m compileall -q skills/pi-review-pulse/scripts tests",
                    "result": "passed",
                    "exit_code": 0,
                },
                {
                    "command": "candidate-only git diff --no-index --check against /tmp/pi-pr1-local-vab0gsp5/before",
                    "result": "passed",
                    "exit_code": 0,
                },
                {
                    "command": "git diff --check",
                    "result": "unchanged-preexisting-failure",
                    "exit_code": 2,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
