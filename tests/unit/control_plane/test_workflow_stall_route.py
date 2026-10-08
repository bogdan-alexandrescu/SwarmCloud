"""`GET /v1/workflows` serves the reconciler's stalled workflows (#616).

The console's Overview "Needs a look" takes its Workflows check from this
route (`apps/swarm-ui/src/checks.ts` `workflowCheck`), and `sc trouble` reads
it too. The reconciler's pass persists what its workflow stall check found
(`stalled_workflows`, `workflow_check`); this route serves the caller's own
tenant's rows from the LATEST pass, with a count.

Pinned: the caller's rows and no other tenant's (invariant 9); the newest pass
wins; a check that could not read the workflows is served as `check_error`,
never as zero; no pass at all, or a read of the passes that fails, is the
same -- an unknown count with a reason, and the list route still answers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .conftest import auth_header, seed_tenant

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def row(workflow_id: str, tenant_id: str, kind: str = "no_progress", **extra: Any) -> dict:
    entry = {
        "workflow_id": workflow_id, "tenant_id": tenant_id, "step_id": "review",
        "task_id": f"task-{workflow_id}", "kind": kind,
        "severity": "bad" if kind in ("no_progress", "start_overdue") else "note",
        "age_seconds": 3000.0, "reason": "no step has changed state for 50m",
        "repaired": False, "repair": None,
    }
    entry.update(extra)
    return entry


def seed_pass(db, pass_id: str, *, at: datetime, rows: list[dict], read_error: str | None = None,
              check: bool = True) -> None:
    doc: dict[str, Any] = {
        "pass_id": pass_id,
        "started_at": at.isoformat(),
        "finished_at": (at + timedelta(seconds=4)).isoformat(),
        "stalled_workflows": rows,
    }
    if check:
        doc["workflow_check"] = {
            "examined": 3, "truncated": False, "repaired": 0, "read_error": read_error,
        }
    else:
        del doc["stalled_workflows"]
    db.docs[f"reconciler_passes/{pass_id}"] = doc


def stalled(client) -> dict:
    response = client.get("/v1/workflows", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["stalled_workflows"]


def test_the_callers_stalled_workflows_are_served_with_a_count(db, client):
    seed_tenant(db, "eng")
    seed_pass(db, "pass_1", at=NOW, rows=[
        row("wf_mine", "eng"),
        row("wf_theirs", "research"),
        row("wf_mine_too", "eng", kind="dependencies_met", repaired=True, repair="promoted to READY"),
    ])

    body = stalled(client)

    assert body["count"] == 2
    assert [r["workflow_id"] for r in body["workflows"]] == ["wf_mine", "wf_mine_too"]
    assert body["check_error"] is None
    assert body["pass_at"] == NOW.isoformat()
    # Another tenant's row is not served, not even its id.
    assert "wf_theirs" not in str(body)


def test_the_newest_pass_wins(db, client):
    seed_tenant(db, "eng")
    seed_pass(db, "pass_old", at=NOW - timedelta(minutes=5), rows=[row("wf_old", "eng")])
    seed_pass(db, "pass_new", at=NOW, rows=[])

    body = stalled(client)

    assert body["count"] == 0
    assert body["workflows"] == []


def test_a_check_that_could_not_read_is_unknown_not_zero(db, client):
    seed_tenant(db, "eng")
    seed_pass(db, "pass_1", at=NOW, rows=[], read_error="ServiceUnavailable: 503")

    body = stalled(client)

    assert body["count"] is None
    assert "503" in body["check_error"]


def test_no_pass_is_unknown_not_zero(db, client):
    seed_tenant(db, "eng")

    body = stalled(client)

    assert body["count"] is None
    assert body["check_error"]


def test_a_pass_from_before_the_check_is_unknown_not_zero(db, client):
    seed_tenant(db, "eng")
    seed_pass(db, "pass_1", at=NOW, rows=[], check=False)

    body = stalled(client)

    assert body["count"] is None
    assert "stall check" in body["check_error"]


def test_a_failed_read_of_the_passes_is_reported_and_the_list_still_answers(db, client, monkeypatch):
    seed_tenant(db, "eng")
    real = db.collection

    def collection(path: str):
        if path == "reconciler_passes":
            raise RuntimeError("deadline exceeded")
        return real(path)

    monkeypatch.setattr(db, "collection", collection)

    body = stalled(client)

    assert body["count"] is None
    assert "deadline exceeded" in body["check_error"]
