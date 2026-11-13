"""A job that reads `needs.<job>.result` in its `if:` carries a status function.

WHY THIS EXISTS. GitHub adds an implicit `success()` to a job-level `if:` that
names no status function (`always()`, `cancelled()`, `failure()`, `success()`),
and a job's `success()` is false when any job ANYWHERE up its `needs:` chain
failed or was skipped -- not only its direct needs. So
`needs.python.result == 'success'` alone is not "runs when `python`
succeeded": `python` runs `if: always()` and succeeds while `python-unit` and
the lock / contract checks above it are skipped (main reuses the tree its pull
request proved), and the implicit `success()` then skips the job anyway.
That is what application.yml's `plugin-tag` did on every push to main: its
condition read the direct result and its comment said "No status function: it
runs only when `python` succeeded", and no `sc-v<version>` tag was made (found
from the runs, 2026-11-11; 0.5.86 was tagged by hand).

A job that reads a dependency's result has decided for itself what that result
must be, so the implicit `success()` can only add a wrong second rule. Each such
job therefore names a status function that is not `success()` (an explicit
`success()` is the same transitive check): `!cancelled()` for "run unless the
run was cancelled", `always()` where it must also run then.

WHAT IS NOT MODELLED. A job whose `if:` reads only a dependency's OUTPUTS, or
nothing of its needs, keeps the implicit `success()`: there a skipped ancestor
skipping it is the intended rule, and it says nothing this test could check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_fast_checks_on_lane_prs import without_lane_clause

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# `needs.python.result`, `needs.live-browser.result`, `needs.*.result`.
_READS_A_RESULT = re.compile(r"\bneeds\.(?:\*|[A-Za-z_][\w-]*)\.result\b")
# The status functions that replace the implicit `success()`; `success()`
# itself is deliberately absent (see the module docstring).
_STATUS_FUNCTION = re.compile(r"\b(?:always|cancelled|failure)\(\s*\)")


def _jobs_reading_a_result() -> list[tuple[str, str, str]]:
    """(workflow file, job id, its `if:`) for every job whose `if:` reads a result."""
    found = []
    for path in WORKFLOWS:
        workflow: dict[Any, Any] = yaml.safe_load(path.read_text())
        for job_id, job in (workflow.get("jobs") or {}).items():
            condition = job.get("if") if isinstance(job, dict) else None
            if condition is not None and _READS_A_RESULT.search(str(condition)):
                found.append((path.name, str(job_id), str(condition)))
    return found


JOBS = _jobs_reading_a_result()

# Red until the operator applies the workflow lines (a lane cannot push a
# workflow file): the fix ships beside this test as workflow-lines.patch.
PATCH_NOTE = "-- the plugin-tag fix ships as workflow-lines.patch, which the operator pushes by hand"


def test_the_jobs_were_found():
    """The test below is parametrised over these; an empty list checks nothing."""
    assert len(WORKFLOWS) >= 5, [path.name for path in WORKFLOWS]
    visited = {(name, job_id) for name, job_id, _ in JOBS}
    # The job whose skip this test was written for, and two that already had it right.
    assert ("application.yml", "plugin-tag") in visited, sorted(visited)
    assert ("accept.yml", "acceptance") in visited, sorted(visited)
    assert ("release.yml", "promote") in visited, sorted(visited)


@pytest.mark.parametrize(("workflow", "job_id", "condition"), JOBS, ids=[f"{w}:{j}" for w, j, _ in JOBS])
def test_a_job_reading_a_result_names_a_status_function(workflow: str, job_id: str, condition: str):
    assert _STATUS_FUNCTION.search(condition), (
        f"{workflow} job `{job_id}` reads a `needs.<job>.result` in its `if:` with no status function, "
        "so GitHub ANDs the implicit success(), which is false whenever any job up its needs chain was "
        f"skipped -- the job is then skipped whatever that result is. Add `!cancelled() && `: {condition} {PATCH_NOTE}"
    )


def test_plugin_tag_runs_after_a_green_python_on_a_push_to_main():
    """The fix in full for the job the finding named: the gate it had, plus `!cancelled()`."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "application.yml").read_text())
    job = workflow["jobs"]["plugin-tag"]
    assert job["needs"] == ["python"]
    # (Then the lane clause every job ends with, test_fast_checks_on_lane_prs.py.)
    condition = re.sub(r"\s+", " ", without_lane_clause(job["if"])).strip()
    condition = re.sub(r"^\$\{\{\s*(.*?)\s*\}\}$", r"\1", condition)
    assert condition == (
        "!cancelled() && github.event_name == 'push' && github.ref == 'refs/heads/main' "
        "&& needs.python.result == 'success'"
    ), f"{condition} {PATCH_NOTE}"
