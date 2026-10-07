"""CI fails when `uv.lock` disagrees with a `pyproject.toml`.

Every job that installs the workspace runs `uv sync --frozen`, which installs
exactly what `uv.lock` says WITHOUT checking it against the `pyproject.toml`
files. A lock that is stale -- a dependency added to a pyproject without
re-locking, or a hand-edited lock (#171 shipped one, verified by reading
rather than by the tool) -- therefore passed CI and failed later, on the first
`uv sync` that was not frozen (#76).

`uv lock --check` re-resolves and fails if the lock would change. It runs once,
in the `python-checks` job, before that job's sync: one place is enough, because the
lock is one file and every job reads the same one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"


def _python_steps() -> list[dict]:
    # The job that runs the step. Since 2026-10-07 `python` (`format / unit
    # tests`) only reads the results of `python-checks` and the `python-unit`
    # shards.
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["python-checks"]["steps"]


def _index(steps: list[dict], needle: str) -> list[int]:
    return [i for i, step in enumerate(steps) if needle in str(step.get("run", ""))]


def test_the_python_job_checks_the_lock_before_it_syncs_frozen():
    steps = _python_steps()
    check = _index(steps, "uv lock --check")
    assert len(check) == 1, f"expected one `uv lock --check` step in the python job, found {len(check)}"
    sync = _index(steps, "uv sync --frozen")
    assert sync, "the python job no longer runs `uv sync --frozen`; re-read this test's premise"
    assert check[0] < sync[0], "the lock must be checked before a frozen sync installs from it"
