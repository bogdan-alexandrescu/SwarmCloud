"""The worker's git tests pass under an exported git identity (HERMETIC-GIT-ENV, part of #1822).

Git puts GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME and
GIT_COMMITTER_EMAIL above `git -c user.name=... -c user.email=...`. Every
SwarmCloud container exports all four, so the push-check tests below, which
commit as a named identity and assert on the author the check reports, saw
the container's author instead and failed in every container while passing in
CI, which exports none. CI never exercised that, so this test exports a
foreign identity itself and runs them under it, in a child pytest: the
autouse fixture in this directory's conftest.py has to clear it there exactly
as it does in a container.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# The tests that failed in the container on 2026-10-21 (README-BACKENDS, #1841).
COMMITTING_TESTS = (
    "tests/unit/worker/test_harvest.py::test_the_push_check_passes_a_history_the_worker_wrote",
    "tests/unit/worker/test_harvest.py::test_the_push_check_refuses_a_commit_the_worker_did_not_write",
    "tests/unit/worker/test_publish_as_the_person.py::test_the_person_and_the_bot_are_both_the_workers_own",
    "tests/unit/worker/test_publish_as_the_person.py::test_a_third_identitys_commit_is_still_refused",
    "tests/unit/worker/test_publish_as_the_person.py::test_without_also_own_the_person_alone_is_the_worker",
)


def test_the_committing_tests_pass_under_a_foreign_git_identity():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(
        GIT_AUTHOR_NAME="Exported Author",
        GIT_AUTHOR_EMAIL="exported-author@example.invalid",
        GIT_COMMITTER_NAME="Exported Committer",
        GIT_COMMITTER_EMAIL="exported-committer@example.invalid",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider",
         "-p", "no:xdist", *COMMITTING_TESTS],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert " passed" in result.stdout and "failed" not in result.stdout, result.stdout[-2000:]
