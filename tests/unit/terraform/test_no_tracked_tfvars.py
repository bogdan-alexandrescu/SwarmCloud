"""No deployment's tfvars is tracked: only placeholder examples are
(docs/design/generic-repository.md sections 1.2 C10 and 2.2, part of #2688).

A tracked `*.tfvars` is a real deployment's configuration in the repository.
The only ones allowed are the terraform test fixtures under tests/terraform/.
The workflows lane untracked the last three real files, together with the CI
change that fetches each environment's tfvars from the private configuration,
so KNOWN_EXCEPTIONS is empty. It may only shrink: an exception that is no
longer tracked fails here until it is removed, so a removed file cannot
quietly come back.

Every root a tfvars feeds has a tracked `*.tfvars.example` with placeholders
instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ROOT / "terraform" / "environments"

FIXTURES = "tests/terraform/"

# Empty since the workflows lane untracked the three real files.
KNOWN_EXCEPTIONS: frozenset[str] = frozenset()


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z", "--", pattern],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def test_no_tfvars_is_tracked_outside_the_fixtures_and_the_known_exceptions() -> None:
    tracked = _tracked("*.tfvars")
    assert tracked, "git ls-files found no *.tfvars at all; the scan did not run"
    outside = [p for p in tracked if not p.startswith(FIXTURES)]
    assert sorted(set(outside) - KNOWN_EXCEPTIONS) == [], (
        "a deployment's tfvars is tracked; its real values belong in the private "
        "config prefix, and a placeholder copy is a *.tfvars.example"
    )


def test_the_exception_list_only_shrinks() -> None:
    tracked = set(_tracked("*.tfvars"))
    gone = sorted(KNOWN_EXCEPTIONS - tracked)
    assert gone == [], f"no longer tracked, so remove from KNOWN_EXCEPTIONS: {gone}"


def test_every_root_has_a_tracked_placeholder_example() -> None:
    environments = sorted(d.name for d in ENVIRONMENTS.iterdir() if d.is_dir())
    assert environments, "no environment directory found under terraform/environments"
    expected = {"terraform/bootstrap/terraform.tfvars.example"} | {
        f"terraform/environments/{env}/{env}.tfvars.example" for env in environments
    }
    tracked = set(_tracked("*.tfvars.example"))
    assert sorted(expected - tracked) == []
