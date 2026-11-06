"""A red CI gate's failed leaf jobs reach a fix round's excerpt (#2937).

Measured on PR #2888, round 2: the only failing REQUIRED check at the head was
application.yml's fan-in `ci-gate`, whose log says only `not passed:
python-unit (failure)`. The job that failed, `unit tests (4/10)`, whose log
named `FAILED tests/unit/install/test_switches.py::test_engine_never_reads_an_app_secret`,
was never read, and the push run was red while the pull_request run at the
same sha was green: main already had the fix, and the round never merged it.

So `issueci.excerpt_at` resolves a failing Actions gate's workflow run
(`forgechecks.workflow_run_of`), excerpts that run's failed leaf jobs before
the gate, names the events each failed on, and says "merge main" when a job
is red on push and green on pull_request at the same sha.

Every log here is synthetic; nothing touches GitHub.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api import forgechecks, issueci
from swarm_api.forgechecks import RED, CiReading
from swarm_api.forgewrite import ForgeWriteError
from swarm_api.validation import IssueRef

REF = IssueRef(owner="acme", repo="widgets", number=2888)
SHA = "d" * 40
NODE = "tests/unit/install/test_switches.py::test_x"
E_LINE = "E       assert 'APP_SECRET' not in read_names"
LEAF = "unit tests (4/10)"
PUSH_RUN = 7001
PR_RUN = 7002
GATE_PUSH = 501
GATE_PR = 502


def _leaf_log(node: str = NODE, extra: str = "") -> str:
    lines = ["##[group]Run uv run pytest tests/unit -q"]
    lines += [f"tests/unit/control_plane/test_passing_{i}.py ." for i in range(40)]
    lines += [
        "=================================== FAILURES ===================================",
        "    def test_x():",
        ">       assert 'APP_SECRET' not in read_names",
        E_LINE + extra,
        "tests/unit/install/test_switches.py:12: AssertionError",
        "=========================== short test summary info ============================",
        f"FAILED {node} - AssertionError",
        "1 failed, 900 passed in 61.00s",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
    ]
    return "\n".join(lines) + "\n"


GATE_LOG = "not passed: python-unit (failure)\n##[error]Process completed with exit code 1.\n"


def _gate(check_id: int, run_id: int | None, event: str = "push", **extra: Any) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id, "name": "ci-gate", "conclusion": "failure", "event": event,
        "app": {"slug": "github-actions"}, "output": {"title": None, "summary": None, "text": None},
    }
    if run_id is not None:
        check["workflow_run_id"] = run_id
    check.update(extra)
    return check


def _job(job_id: int, name: str, conclusion: str) -> dict[str, Any]:
    return {"id": job_id, "name": name, "status": "completed", "conclusion": conclusion}


class _Writer:
    """`GitHubWriter`'s reads `excerpt_at` makes, from fixtures."""

    def __init__(self, logs: dict[int, str], jobs: dict[int, list[dict[str, Any]]],
                 at_sha: list[dict[str, Any]] | None = None, jobs_error: bool = False):
        self.logs = logs
        self.jobs = jobs
        self.at_sha = at_sha or []
        self.jobs_error = jobs_error
        self.log_reads: list[int] = []
        self.jobs_reads: list[int] = []
        self.sha_reads = 0

    def check_annotations(self, ref: IssueRef, check_id: int, token: str) -> list[dict[str, Any]]:
        return []

    def job_log_tail(self, ref: IssueRef, job_id: int, token: str, *, limit: int) -> str | None:
        self.log_reads.append(job_id)
        text = self.logs.get(job_id)
        return text[-limit:] if text else None

    def run_jobs(self, ref: IssueRef, run_id: int, token: str) -> list[dict[str, Any]]:
        self.jobs_reads.append(run_id)
        if self.jobs_error:
            raise ForgeWriteError("the jobs of Actions run: 403")
        return self.jobs.get(run_id, [])

    def actions_check_runs(self, ref: IssueRef, sha: str, token: str) -> list[dict[str, Any]]:
        self.sha_reads += 1
        return self.at_sha


def _excerpt(writer: _Writer, checks: list[dict[str, Any]], token: str | None = None) -> str:
    reading = CiReading(state=RED, failing_runs=checks)
    return issueci.excerpt_at(writer, REF, SHA, reading, token or secrets.token_hex(20), label="test")


def _push_jobs() -> list[dict[str, Any]]:
    return [_job(11, "unit tests (3/10)", "success"), _job(12, LEAF, "failure"),
            _job(GATE_PUSH, "ci-gate", "failure")]


# --------------------------------------------------------------------------
# The failing test reaches the excerpt
# --------------------------------------------------------------------------

def test_a_red_gate_excerpts_its_failed_leaf_jobs_node_id():
    writer = _Writer({12: _leaf_log(), GATE_PUSH: GATE_LOG}, {PUSH_RUN: _push_jobs()})
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN)])

    # Before #2937 the excerpt held only ci-gate's "not passed: python-unit (failure)".
    assert f"FAILED {NODE}" in excerpt
    assert E_LINE in excerpt
    assert excerpt.index(f"## {LEAF} (") < excerpt.index("## ci-gate (")
    assert f"## {LEAF} (failure on push)" in excerpt
    # A green job is not excerpted, and the gate is not read as its own leaf.
    assert "unit tests (3/10)" not in excerpt
    assert writer.jobs_reads == [PUSH_RUN]


def test_the_gate_run_is_resolved_from_its_details_url():
    url = f"https://github.com/acme/widgets/actions/runs/{PUSH_RUN}/job/{GATE_PUSH}"
    writer = _Writer({12: _leaf_log()}, {PUSH_RUN: _push_jobs()})
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, None, details_url=url)])

    assert writer.jobs_reads == [PUSH_RUN]
    assert f"FAILED {NODE}" in excerpt


def test_workflow_run_of_reads_the_stamp_then_the_url_of_this_very_job():
    url = f"https://github.com/acme/widgets/actions/runs/{PUSH_RUN}/job/{GATE_PUSH}"
    assert forgechecks.workflow_run_of({"id": GATE_PUSH, "workflow_run_id": 9}) == 9
    assert forgechecks.workflow_run_of({"id": GATE_PUSH, "details_url": url + "?pr=1"}) == PUSH_RUN
    assert forgechecks.workflow_run_of({"id": GATE_PUSH, "html_url": url}) == PUSH_RUN
    # Another job's URL, a bool stamp, nothing at all: unresolved.
    assert forgechecks.workflow_run_of({"id": 1, "details_url": url}) is None
    assert forgechecks.workflow_run_of({"id": GATE_PUSH, "workflow_run_id": True}) is None
    assert forgechecks.workflow_run_of({}) is None


# --------------------------------------------------------------------------
# Events: push vs pull_request
# --------------------------------------------------------------------------

def test_red_on_push_green_on_pull_request_says_merge_main():
    at_sha = [
        {"id": 12, "name": LEAF, "conclusion": "failure", "event": "push", "workflow_run_id": PUSH_RUN},
        {"id": 22, "name": LEAF, "conclusion": "success", "event": "pull_request",
         "workflow_run_id": PR_RUN},
    ]
    writer = _Writer({12: _leaf_log()}, {PUSH_RUN: _push_jobs()}, at_sha=at_sha)
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN)])

    assert f"## {LEAF} (failure on push)" in excerpt
    assert "passed on pull_request" in excerpt
    assert "git merge origin/main" in excerpt and "merge main" in excerpt
    # The call-out sits under the header, before any section.
    assert excerpt.index("merge main") < excerpt.index("\n## ")
    assert writer.sha_reads == 1


def test_red_on_both_events_names_both_and_no_merge_main():
    pr_jobs = [_job(22, LEAF, "failure"), _job(GATE_PR, "ci-gate", "failure")]
    writer = _Writer({12: _leaf_log(), 22: _leaf_log()},
                     {PUSH_RUN: _push_jobs(), PR_RUN: pr_jobs})
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN, "push"),
                                _gate(GATE_PR, PR_RUN, "pull_request")])

    assert f"## {LEAF} (failure on push, pull_request)" in excerpt
    assert excerpt.count(f"## {LEAF} (") == 1
    assert "merge main" not in excerpt
    assert "## ci-gate (failure on push, pull_request)" in excerpt
    # Both events are in the reading: no read of every job at the sha.
    assert writer.sha_reads == 0


# --------------------------------------------------------------------------
# Fails soft, capped, redacted
# --------------------------------------------------------------------------

def test_a_failed_jobs_read_falls_back_to_the_gate_section():
    writer = _Writer({GATE_PUSH: GATE_LOG}, {}, jobs_error=True)
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN)])

    assert "## ci-gate (failure on push)" in excerpt
    assert "not passed: python-unit (failure)" in excerpt


def test_leaf_logs_are_capped_at_max_excerpt_checks():
    extra = issueci.MAX_EXCERPT_CHECKS + 2
    jobs = [_job(100 + i, f"unit tests ({i}/10)", "failure") for i in range(extra)]
    jobs.append(_job(GATE_PUSH, "ci-gate", "failure"))
    writer = _Writer({100 + i: _leaf_log() for i in range(extra)}, {PUSH_RUN: jobs})
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN)])

    assert len(writer.log_reads) <= issueci.MAX_EXCERPT_CHECKS
    # The leaves and the gate: MAX_EXCERPT_CHECKS shown, the rest counted.
    assert f"[{extra + 1 - issueci.MAX_EXCERPT_CHECKS} more failing checks not shown]" in excerpt
    assert len(excerpt.encode("utf-8")) <= issueci.MAX_EXCERPT_BYTES


def test_a_leaf_log_credential_is_redacted():
    fake = "ghp_" + secrets.token_hex(18)
    writer = _Writer({12: _leaf_log(extra=f" GH_TOKEN={fake}")}, {PUSH_RUN: _push_jobs()})
    token = secrets.token_hex(20)
    excerpt = _excerpt(writer, [_gate(GATE_PUSH, PUSH_RUN)], token=token)

    assert f"FAILED {NODE}" in excerpt
    assert fake not in excerpt and token not in excerpt
