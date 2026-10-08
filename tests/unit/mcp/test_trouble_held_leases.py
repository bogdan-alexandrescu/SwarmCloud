"""`sc trouble` and `swarm_trouble` list leases held past their TTL (#532, 5b).

The console's Overview "Needs a look" has counted them since the reconciler
began persisting `held_past_ttl`; `find_trouble` read no lease at all, so the
terminal and the MCP tool said "nothing wrong right now" over a lease held
five hours past its TTL. These pin the three answers the read can give: some
held (count, age, task, reason), none (no finding), and not read (a finding,
never silence).

Offline: the lease page is a dict, the control plane a fake client.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from swarm_mcp import render, sc
from swarm_mcp.client import SwarmError
from swarm_mcp.render import Snapshot, Style, find_trouble, render_trouble

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
WIDE = Style(width=200, color=False, unicode=False)


def lease(task_id: str, *, past_minutes: float | None, last_error: str | None = None, **extra):
    """One `/v1/admin/leases` row. `past_minutes=None` is a lease inside its TTL."""
    expires = NOW + timedelta(minutes=5) if past_minutes is None else NOW - timedelta(minutes=past_minutes)
    row = {
        "lease_id": f"lease-{task_id}",
        "task_id": task_id,
        "tenant_id": "acme",
        "dispatch_state": "DISPATCHED",
        "created_at": (NOW - timedelta(hours=6)).isoformat(),
        "expires_at": expires.isoformat(),
        "released": False,
        "expired": past_minutes is not None,
        "dispatch_overdue": False,
        "silent_seconds": 3600,
        "heartbeat_ever": True,
        "last_error": last_error,
    }
    row.update(extra)
    return row


def page(*rows, beyond: int = 0):
    return {
        "leases": list(rows),
        "evaluated_at": NOW.isoformat(),
        "thresholds": {"heartbeat_grace_seconds": 90, "lease_timeout_seconds": 120},
        "active_only": True,
        "active_beyond_window": beyond,
    }


def held_findings(snap: Snapshot):
    return [f for f in find_trouble(snap, WIDE) if f.where in ("held", "leases")]


class TestHeldLeases:
    def test_held_leases_render_with_count_age_and_the_longest_held_task(self):
        snap = Snapshot(
            now=NOW,
            leases=page(
                lease("task_short", past_minutes=20, last_error="short one"),
                lease("task_longest", past_minutes=42, last_error="kill not confirmed\nsecond line"),
                lease("task_alive", past_minutes=None),
            ),
        )
        findings = held_findings(snap)
        assert len(findings) == 1
        f = findings[0]
        assert f.where == "held"
        assert f.severity == "warn"
        # Two of three leases, not three: a lease inside its TTL is not held.
        assert f.what.startswith("2 lease(s) past their TTL")
        assert "longest held 42m past it (task_longest): kill not confirmed" in f.what
        assert "second line" not in f.what
        screen = "\n".join(render_trouble(find_trouble(snap, WIDE), WIDE))
        assert "! held" in screen
        assert "task_longest" in screen

    def test_held_leases_sort_among_the_existing_findings_worst_first(self):
        snap = Snapshot(
            now=NOW,
            stats={"dispatch_paused": True},
            tasks=render.Listing([{"state": "FAILED", "task_id": "task_f"}]),
            leases=page(lease("task_x", past_minutes=30)),
        )
        severities = [f.severity for f in find_trouble(snap, WIDE)]
        assert severities == sorted(severities, key=render.SEVERITIES.index)
        assert [f.where for f in find_trouble(snap, WIDE)][:2] == ["dispatch", "held"]

    def test_a_lease_with_no_error_says_what_its_heartbeat_says(self):
        snap = Snapshot(
            now=NOW,
            leases=page(lease("task_boot", past_minutes=16, heartbeat_ever=False)),
        )
        (f,) = held_findings(snap)
        assert "(task_boot): its worker never beat" in f.what

    def test_zero_held_leases_render_nothing(self):
        snap = Snapshot(now=NOW, leases=page(lease("task_alive", past_minutes=None)))
        assert held_findings(snap) == []
        assert held_findings(Snapshot(now=NOW, leases=page())) == []

    def test_live_leases_beyond_the_window_are_said_not_to_be_checked(self):
        snap = Snapshot(now=NOW, leases=page(lease("task_alive", past_minutes=None), beyond=3))
        (f,) = held_findings(snap)
        assert f.where == "leases"
        assert "3 more live lease(s) beyond the 1 read were not checked" in f.what


class TestAnUnreadableLeaseRead:
    def test_a_failed_read_is_a_not_read_finding_and_never_silence(self):
        snap = Snapshot(now=NOW, leases_error="GET /v1/admin/leases -> 500: boom")
        (f,) = held_findings(snap)
        assert f.where == "leases"
        assert f.severity == "down"
        assert f.what == "not read (GET /v1/admin/leases -> 500: boom)"

    def test_a_refused_read_is_still_a_finding_but_only_a_note(self):
        snap = Snapshot(now=NOW, leases_error="GET /v1/admin/leases -> 403: admin only", leases_refused=True)
        (f,) = held_findings(snap)
        assert f.severity == "note"
        assert f.what.startswith("not read (GET /v1/admin/leases -> 403")


class FakeClient:
    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, leases):
        self.leases = leases
        self.asked: list[str] = []

    def request(self, method, path, **kwargs):
        self.asked.append(path)
        if path.startswith("/v1/admin/leases"):
            if isinstance(self.leases, Exception):
                raise self.leases
            return self.leases
        return {
            "/v1/tenants/me": {"tenant": {"tenant_id": "acme"}},
            "/v1/stats": {"dispatch_paused": False},
            "/v1/capacity": {"pools": []},
            "/v1/tasks": {"tasks": []},
            "/v1/accounts": {"accounts": [{"account_id": "a", "state": "ACTIVE"}]},
            # `sc trouble` reads the reconciler's stalled workflows too (#616).
            "/v1/workflows": {
                "workflows": [],
                "stalled_workflows": {"count": 0, "workflows": [], "check_error": None},
            },
        }[path.split("?")[0]]


def trouble(client):
    out = io.StringIO()
    code = sc.cmd_trouble(client, sc.build_parser().parse_args(["--width", "200", "trouble"]), out)
    return code, out.getvalue()


class TestScTroubleReadsTheConsolesLeaseRoute:
    def test_it_reads_the_route_and_window_the_console_reads(self):
        client = FakeClient(page())
        trouble(client)
        assert sc.LEASES_PATH in client.asked
        assert sc.LEASES_PATH == "/v1/admin/leases?active_only=true&limit=200"

    def test_held_leases_reach_the_trouble_screen(self):
        code, out = trouble(FakeClient(page(lease("task_held", past_minutes=300, last_error="kill unconfirmed"))))
        assert "1 lease(s) past their TTL, longest held 5h 00m past it (task_held): kill unconfirmed" in out
        assert code == sc.EXIT_OK

    def test_a_403_is_a_note_and_does_not_fail_the_command(self):
        code, out = trouble(FakeClient(SwarmError("GET /v1/admin/leases -> 403: admin only", status=403)))
        assert "leases" in out and "not read (GET /v1/admin/leases -> 403" in out
        assert "admin-gated" in out
        assert code == sc.EXIT_OK

    def test_a_failed_read_is_shown_and_fails_the_command(self):
        code, out = trouble(FakeClient(SwarmError("GET /v1/admin/leases -> 500: boom", status=500)))
        assert "not read (GET /v1/admin/leases -> 500: boom)" in out
        assert code == sc.EXIT_FAIL

    def test_a_response_without_a_leases_list_is_not_read_rather_than_zero(self):
        code, out = trouble(FakeClient({"items": []}))
        assert "not read (the leases response carried no `leases` list)" in out
        assert code == sc.EXIT_FAIL
