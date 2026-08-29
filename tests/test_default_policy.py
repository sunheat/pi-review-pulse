from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "pi-review-pulse" / "scripts"

sys.path.insert(0, str(SCRIPTS))

from default_policy import (  # noqa: E402
    PolicyError,
    apply_policy_overrides,
    default_policy,
    normalize_policy,
    policy_digest,
)


class DefaultPolicyTests(unittest.TestCase):
    def test_default_is_unbounded_autonomous_and_mutating(self) -> None:
        policy = default_policy()
        self.assertEqual(policy["profile"], "autonomous")
        self.assertEqual(policy["execution_mode"], "unattended")
        self.assertIsNone(policy["max_wakes"])
        self.assertIsNone(policy["deadline_at"])
        self.assertEqual(policy["validation_failure"], "repair")
        self.assertTrue(policy["allow_test_changes"])
        self.assertEqual(policy["publication"], "auto")
        self.assertEqual(policy["thread_resolution"], "auto")
        self.assertEqual(policy["review_trigger"], "auto")

    def test_profile_and_explicit_overrides_are_deterministic(self) -> None:
        policy = apply_policy_overrides(
            None,
            {
                "profile": "supervised",
                "max_wakes": 5,
                "cadence_seconds": 1800,
                "allow_test_changes": True,
            },
        )
        self.assertEqual(policy["profile"], "supervised")
        self.assertEqual(policy["publication"], "confirm")
        self.assertEqual(policy["thread_resolution"], "confirm")
        self.assertEqual(policy["cadence_seconds"], 1800)
        self.assertEqual(policy["max_wakes"], 5)

    def test_deadline_is_canonicalized_and_digest_is_stable(self) -> None:
        policy = normalize_policy(
            {"deadline_at": "2026-08-27T10:00:00+10:00", "max_wakes": None}
        )
        self.assertEqual(policy["deadline_at"], "2026-08-27T00:00:00+00:00")
        self.assertEqual(policy_digest(policy), policy_digest(dict(policy)))

    def test_unknown_or_invalid_fields_fail_closed(self) -> None:
        with self.assertRaises(PolicyError):
            normalize_policy({"not_a_policy": True})
        with self.assertRaises(PolicyError):
            normalize_policy({"max_wakes": 0})
        with self.assertRaises(PolicyError):
            normalize_policy({"deadline_at": "tomorrow"})


if __name__ == "__main__":
    unittest.main()
