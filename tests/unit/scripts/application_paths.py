"""What application.yml's `changes` job counts as a change reaching the workflow.

Until 2026-10-07 this was application.yml's `on.pull_request.paths`, and tests
that hold "a pull request changing only X runs actionlint and X's tests" read
that list. The workflow now runs on every pull request -- its `ci-gate` job is
the required check, and a filtered-out workflow reports nothing -- and the
list moved, comments and all, into the `changes` job's `APP_PATHS`. A job
there is skipped unless a changed file matches it.

The rule is the step's own: a line is a path, or `<dir>/**` for everything
under `<dir>/`; `#` lines are commentary. test_ui_changes_gate.py runs the
step itself and holds this reading of it to what the step does.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"


def changes_job() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["changes"]


def diff_step() -> dict:
    found = [s for s in changes_job()["steps"] if s.get("id") == "diff"]
    assert len(found) == 1, "the changes job has no single step with id `diff`"
    return found[0]


def app_paths() -> list[str]:
    """APP_PATHS, one pattern per entry, commentary dropped."""
    text = str((diff_step().get("env") or {}).get("APP_PATHS") or "")
    patterns = [line.strip() for line in text.splitlines()]
    patterns = [p for p in patterns if p and not p.startswith("#")]
    assert len(patterns) > 10, f"APP_PATHS reads as {patterns}: the list is long, an empty read is a bug"
    return patterns


def reaches(path: str) -> bool:
    """Whether a pull request changing `path` alone runs application.yml's jobs."""
    for pattern in app_paths():
        if pattern.endswith("/**"):
            stem = pattern[: -len("**")]
            if path.startswith(stem) and len(path) > len(stem):
                return True
        elif path == pattern:
            return True
    return False
