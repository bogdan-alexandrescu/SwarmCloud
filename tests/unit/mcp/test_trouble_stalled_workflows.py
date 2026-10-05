"""`sc trouble` and `swarm_trouble` list the reconciler's stalled workflows (#616).

The reconciler's workflow stall check persists its findings on its pass, and
`GET /v1/workflows` serves the caller's tenant's rows as `stalled_workflows`
(`swarm_api.stalls`). These pin what the trouble list makes of them: every
row, worst first, naming workflow, step, task, age and reason; a check that
was blind is a finding; a read of the route that failed is a finding; and no
stalled workflow is no finding.

Offline: the workflow page is a dict, the control plane a fake client.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from swarm_mcp import sc
from swarm_mcp.client import SwarmError
from swarm_mcp.render import Snapshot, Style, find_trouble, render_trouble

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
WIDE = Style(width=240, color=False, unicode=False)


def row(workflow_id: str, kind: str, severity: str, age: float, **extra) -> dict:
    entry = {
        "workflow_id": workflow_id, "tenant_id": "acme", "step_id": "review",
        "task_id": f"task-{workflow_id}", "kind": kind, "severity": severity,
        "age_seconds": age, "reason": f"{kind} reason", "repaired": False, "repair": None,
    }
    entry.update(extra)
    return entry


def page(rows=None, *, check_error=None, count=None, absent=False) -> dict:
    body: dict = {"workflows": [], "next_page_token": None, "tenant_id": "acme"}
    if not absent:
        rows = list(rows or [])
        body["stalled_workflows"] = {
            "count": (None if check_error else len(rows)) if count is None else count,
            "workflows": rows,
            "truncated": False,
            "scan_truncated": False,
            "pass_at": NOW.isoformat(),
            "check_error": check_error,
        }
    return body


def stall_findings(snap: Snapshot):
    return [f for f in find_trouble(snap, WIDE) if f.where in ("workflow", "workflows")]


class TestStalledWorkflows:
    def test_each_row_is_listed_worst_first_with_its_step_task_age_and_reason(self):
        snap = Snapshot(now=NOW, workflows=page([
            row("wf_stopped", "no_progress", "bad", 3000),
            row("wf_slow", "start_overdue", "bad", 900),
            row("wf_drift", "state_drift", "note", 60, repaired=True,
                repair="stored state written as PARKED", step_id=None, task_id=None),
        ]))

        findings = stall_findings(snap)

        assert [f.what.split()[0] for f in findings] == ["wf_stopped", "wf_slow", "wf_drift"]
        first = findings[0]
        assert first.severity == "warn"
        assert "step review (task-wf_stopped)" in first.what
        assert "no_progress" in first.what and "50m" in first.what
        assert "no_progress reason" in first.what
        assert findings[-1].severity == "note"
        assert "repaired: stored state written as PARKED" in findings[-1].what
        screen = "\n".join(render_trouble(find_trouble(snap, WIDE), WIDE))
        assert "wf_stopped" in screen

    def test_no_stalled_workflow_is_no_finding(self):
        assert stall_findings(Snapshot(now=NOW, workflows=page([]))) == []

    def test_a_blind_check_is_a_finding_not_silence(self):
        snap = Snapshot(now=NOW, workflows=page(check_error="the check could not read: 503"))

        (f,) = stall_findings(snap)

        assert f.where == "workflows"
        assert f.severity == "warn"
        assert "503" in f.what

    def test_a_failed_read_is_a_finding_not_silence(self):
        snap = Snapshot(now=NOW, workflows_error="GET /v1/workflows -> 500: boom")

        (f,) = stall_findings(snap)

        assert f.severity == "down"
        assert f.what == "not read (GET /v1/workflows -> 500: boom)"

    def test_an_api_without_the_field_says_so(self):
        (f,) = stall_findings(Snapshot(now=NOW, workflows=page(absent=True)))

        assert f.severity == "note"
        assert "does not report stalled workflows" in f.what


class FakeClient:
    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, workflows):
        self.workflows = workflows
        self.asked: list[str] = []

    def request(self, method, path, **kwargs):
        self.asked.append(path)
        if path.startswith("/v1/workflows"):
            if isinstance(self.workflows, Exception):
                raise self.workflows
            return self.workflows
        return {
            "/v1/tenants/me": {"tenant": {"tenant_id": "acme"}},
            "/v1/stats": {"dispatch_paused": False},
            "/v1/capacity": {"pools": []},
            "/v1/tasks": {"tasks": []},
            "/v1/accounts": {"accounts": [{"account_id": "a", "state": "ACTIVE"}]},
            "/v1/admin/leases": {"leases": [], "active_beyond_window": 0},
        }[path.split("?")[0]]


def trouble(client):
    out = io.StringIO()
    code = sc.cmd_trouble(client, sc.build_parser().parse_args(["--width", "240", "trouble"]), out)
    return code, out.getvalue()


class TestScTroubleReadsTheWorkflowRoute:
    def test_it_reads_the_route_the_console_reads(self):
        client = FakeClient(page([]))
        trouble(client)
        assert sc.WORKFLOWS_PATH in client.asked
        assert sc.WORKFLOWS_PATH.startswith("/v1/workflows?")

    def test_stalled_workflows_reach_the_trouble_screen(self):
        code, out = trouble(FakeClient(page([row("wf_stopped", "no_progress", "bad", 3000)])))
        assert "wf_stopped" in out and "no_progress" in out
        assert code == sc.EXIT_OK

    def test_a_failed_read_is_shown_and_fails_the_command(self):
        code, out = trouble(FakeClient(SwarmError("GET /v1/workflows -> 500: boom", status=500)))
        assert "not read (GET /v1/workflows -> 500: boom)" in out
        assert code == sc.EXIT_FAIL

    def test_a_response_without_a_workflows_list_is_not_read_rather_than_zero(self):
        code, out = trouble(FakeClient({"items": []}))
        assert "not read (the workflows response carried no `workflows` list)" in out
        assert code == sc.EXIT_FAIL
