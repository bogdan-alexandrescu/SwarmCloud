"""The one loader over scripts/generated/ci-run-blocks.json (issue 71).

A test never opens a file under .github/: the CI tree publishes the shell of
its `run:` blocks through scripts/gen_ci_run_blocks.py, and a test that
executes one reads it here.

    from ci_run_blocks import run_block
    script = run_block("auto-merge.yml", "enable", "gate")

`workflow` is a workflow's basename (`auto-merge.yml`) or a source's
repository-relative path (`.github/actions/release-apply/action.yml`); `job`
is the job id, None for a composite action; `step` is the step's key (its id,
else its name; see the generator's docstring).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GENERATED = REPO / "scripts" / "generated" / "ci-run-blocks.json"


@functools.lru_cache(maxsize=None)
def _files() -> dict:
    return json.loads(GENERATED.read_text())["files"]


def _entry(workflow: str) -> dict:
    files = _files()
    for key in (workflow, f".github/workflows/{workflow}"):
        if key in files:
            return files[key]
    raise KeyError(f"no run blocks published for {workflow!r}; known: {sorted(files)}")


def steps(workflow: str, job: str | None) -> dict[str, str]:
    """Every published run block of one job (or action), step key -> text."""
    entry = _entry(workflow)
    if job is None:
        published = entry.get("steps") or {}
    else:
        jobs = entry.get("jobs") or {}
        if job not in jobs:
            raise KeyError(f"{workflow} has no job {job!r} with a run block; known: {sorted(jobs)}")
        published = jobs[job]["steps"]
    return {key: value["run"] for key, value in published.items()}


def run_block(workflow: str, job: str | None, step: str) -> str:
    """The `run:` text of one step."""
    published = steps(workflow, job)
    if step not in published:
        raise KeyError(f"{workflow} job {job!r} has no run step {step!r}; known: {sorted(published)}")
    return published[step]
