# Vendored from codex-review-pulse/skills/codex-review-pulse/scripts/checkpoint_store.py
#!/usr/bin/env python3
"""Atomic storage for repository-local Codex Review Pulse checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from state_model import canonical_repository


def git_common_directory(repository_path: str | Path = ".") -> Path:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "Unable to locate Git common directory")
    return Path(process.stdout.strip()).resolve()


def checkpoint_path(
    repository: str,
    pr_number: int,
    *,
    repository_path: str | Path = ".",
) -> Path:
    key = f"{canonical_repository(repository)}#{pr_number}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return git_common_directory(repository_path) / "codex-review-pulse" / f"{digest}.json"


def runtime_key_digest(repository: str, pr_number: int) -> str:
    """Return the repository/PR key shared by all runtime artifacts."""
    if pr_number < 1:
        raise ValueError("Pull request number must be positive")
    key = f"{canonical_repository(repository)}#{pr_number}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def runtime_artifact_path(
    repository: str,
    pr_number: int,
    suffix: str,
    *,
    repository_path: str | Path = ".",
) -> Path:
    """Locate a PR-scoped runtime artifact under the Git common directory."""
    if not suffix or any(character in suffix for character in "/\\"):
        raise ValueError("Runtime artifact suffix must be one path component")
    digest = runtime_key_digest(repository, pr_number)
    return (
        git_common_directory(repository_path)
        / "codex-review-pulse"
        / f"{digest}.{suffix}"
    )


def load_checkpoint(path: str | Path) -> dict[str, Any] | None:
    checkpoint = Path(path)
    if not checkpoint.exists():
        return None
    with checkpoint.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("Checkpoint root must be a JSON object")
    return payload


def save_checkpoint(path: str | Path, payload: dict[str, Any]) -> None:
    """Replace a checkpoint atomically using a temporary file beside it."""
    checkpoint = Path(path)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=checkpoint.parent,
            prefix=f".{checkpoint.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, checkpoint)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
