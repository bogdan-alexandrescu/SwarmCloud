"""accept.yml runs the cross-tenant Firestore probe on every run, report-only until ISO_PROBE_REQUIRED.

Owner decision, 2026-10-20 (lane ISO-ACCEPT-WIRE, part of #1457): the probe
tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py is
red until the last tenant is cut over, by design. It runs in every accepting
run as `acceptance (tenant-isolation)`. While the repository variable
ISO_PROBE_REQUIRED is not "true" its step may fail and the job still concludes
green, so `report` (scripts/accept-report.sh, which reads red from `needs` and
the failing jobs from the run's job list) can neither open nor update the
acceptance issue for it. Once the variable is "true", a red probe fails the
job and the report files it. docs/ci.md, "The cross-tenant Firestore probe is
report-only until the cutover".
"""

from __future__ import annotations

import re

import pytest

from .test_acceptance_target import _tenants, _unquote
from .test_release_acceptance_job import ACCEPT_JOBS, PEAK, _gh
from .test_release_reuses_ci_images import _code, _needs, _workflow

JOB = "tenant-isolation"
REQUIRED = "${{ vars.ISO_PROBE_REQUIRED != 'true' }}"


def _jobs() -> dict:
    return _workflow("accept.yml")["jobs"]


def _probe_step(job: dict) -> dict:
    steps = [s for s in job["steps"] if "verify-remote.sh" in _code(s.get("run", ""))]
    assert len(steps) == 1, f"the job runs verify-remote.sh {len(steps)} times"
    return steps[0]


def test_the_job_runs_the_tenant_isolation_group_through_verify_remote():
    job = _jobs()[JOB]
    # MUTATION: rename the job -- the acceptance issue names failing jobs by name.
    assert job["name"] == "acceptance (tenant-isolation)"
    # MUTATION: point it at another group.
    assert _code(_probe_step(job)["run"]) == './scripts/verify-remote.sh "acceptance/tenant-isolation"'
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


def test_it_is_an_accepting_job_beside_wave_two():
    job = _jobs()[JOB]
    # MUTATION: drop `acceptance` from needs -- wave 1's peak is already smoke's ceiling.
    assert {"target", "smoke", "sandbox", "acceptance"} <= set(_needs(job))
    condition = str(job["if"])
    for clause in ("!cancelled()", "needs.target.outputs.accept == 'true'", "needs.smoke.result == 'success'",
                   "needs.sandbox.result == 'success'"):
        assert clause in condition, clause
    assert "needs.acceptance.result" not in condition, "a red first wave must not hide the probe's result"
    assert job["concurrency"] == {"group": "accept-dev-tenant-isolation", "cancel-in-progress": True}
    smoke = _tenants()["smoke"]
    ceiling = min(int(_unquote(smoke["max_active"])), int(_unquote(smoke["capacity_units"])))
    wave_two = sum(PEAK[g] for g in _jobs()[ACCEPT_JOBS[1]]["strategy"]["matrix"]["group"])
    assert wave_two + 1 <= ceiling, f"wave 2 ({wave_two}) and the probe's one agent exceed smoke's {ceiling}"


@pytest.mark.parametrize(
    ("value", "report_only"),
    [("", True), ("false", True), ("1", True), ("yes", True), ("true", False)],
)
def test_the_probe_is_report_only_unless_iso_probe_required_is_true(value, report_only):
    job = _jobs()[JOB]
    step = _probe_step(job)
    # MUTATION: `continue-on-error: true` (never required), or drop it (red from day one).
    assert step.get("continue-on-error") == REQUIRED
    assert _gh(step["continue-on-error"], **{"vars.ISO_PROBE_REQUIRED": value}) is report_only
    # A job-level continue-on-error would hide a required red from `report` too.
    assert "continue-on-error" not in job
    others = [s for s in job["steps"] if s is not step and s.get("continue-on-error")]
    assert all("cancelled()" in str(s.get("if", "")) for s in others), "only the cancel step may swallow a failure"


def test_report_only_says_so_and_the_report_still_needs_the_job():
    jobs = _jobs()
    job = jobs[JOB]
    note = [s for s in job["steps"] if "::warning" in _code(s.get("run", ""))]
    # MUTATION: delete the warning step -- report-only must still show the red.
    assert len(note) == 1
    condition = str(note[0]["if"])
    assert "steps.probe.outcome == 'failure'" in condition
    assert "vars.ISO_PROBE_REQUIRED != 'true'" in condition
    assert re.search(r"GITHUB_STEP_SUMMARY", _code(note[0]["run"]))
    # MUTATION: drop the job from report's needs -- once required, a red probe
    # would then file nothing.
    assert JOB in _needs(jobs["report"])
