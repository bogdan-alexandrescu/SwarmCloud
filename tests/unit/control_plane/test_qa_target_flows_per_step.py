"""How many flows a live-browser step runs: the run's choice, else the workspace's (QA-FLOWS-CAP, part of #2136).

BGD, 2026-10-26: the batch size is configurable when a run starts and as a
per-workspace default.

    POST /v1/qa-targets/{id}:run                   optional `flows_per_step`, 1-30
    GET  /v1/qa-settings                           the selected workspace's QA settings
    GET  /v1/workspaces/{tenant_id}/qa-settings    the same, by id
    PUT  /v1/workspaces/{tenant_id}/qa-settings    `qa_flows_per_step`, 1-30; audited

What these hold:

  * the run's `flows_per_step` wins over the workspace's `qa_flows_per_step`,
    which wins over 15, and the run's record (and its group) says which it used;
  * outside 1-30 is refused at the run and at the setting, with nothing written;
  * a member who is not the workspace's admin is refused 403 at the PUT, a
    caller outside it is answered 404, and every change is one audit row;
  * a stored value no swarm-api path wrote (a worker's direct write, outside
    1-30) is read as the default, never acted on;
  * 150 flows register through the route, 151 are refused naming the bound.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from swarm_api import qasettings, qaspec

from .conftest import auth_header
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL_TENANT,
    TARGETS,
    api_context,
    approve,
    register,
    resolver,
    seeded,
)


def _flows(n: int) -> list[dict[str, Any]]:
    return [{"id": f"frame-{i:03d}", "goal": f"reach frame state {i}", "start_path": "/"} for i in range(1, n + 1)]


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/qa-settings"


def _put(client, tenant_id: str, user: str, body: dict[str, Any]):
    return client.put(_url(tenant_id), json=body, headers=auth_header(user))


def run(client, target_id: str, user: str = "carol", **body: Any):
    return client.post(f"{TARGETS}/{target_id}:run", json=body, headers=auth_header(user))


def _batch_steps(db: Any, workflow_id: str) -> list[str]:
    return sorted(d["step_id"] for p, d in db.docs.items() if p.startswith("tasks/")
                  and d.get("workflow_id") == workflow_id and re.fullmatch(r"flows-\d+", d["step_id"]))


def _audit(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == qasettings.AUDIT_ACTION]


@pytest.fixture
def target(client, seeded) -> dict[str, Any]:
    response = register(client, flows=_flows(40))
    assert response.status_code == 201, response.text
    created = response.json()["qa_target"]
    approve(client, created["id"])
    return created


# --------------------------------------------------------------------------
# The run's value, the workspace's, the default
# --------------------------------------------------------------------------


def test_a_run_with_neither_runs_15_a_step_and_records_it(client, db, target):
    response = run(client, target["id"], file="none")
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow_id"]
    # 40 flows at 15: three batch steps.
    assert _batch_steps(db, workflow_id) == ["flows-1", "flows-2", "flows-3"]
    record = db.docs[f"qa_target_runs/{workflow_id}"]
    # MUTATION: leave the value off `record_run` and the run cannot say what it used.
    assert record["flows_per_step"] == 15
    assert record["flows_per_step_from"] == "default"
    group = db.docs[f"qa_run_groups/{response.json()['group_id']}"]
    assert group["flows_per_step"] == 15


def test_the_workspace_setting_is_used_when_the_run_names_none(client, db, target):
    assert _put(client, CAROL_TENANT, "carol", {"qa_flows_per_step": 8}).status_code == 200
    response = run(client, target["id"], file="none")
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow_id"]
    # MUTATION: never read the workspace setting and this is three steps of 15.
    assert len(_batch_steps(db, workflow_id)) == 5
    record = db.docs[f"qa_target_runs/{workflow_id}"]
    assert (record["flows_per_step"], record["flows_per_step_from"]) == (8, "workspace")


def test_the_runs_value_wins_over_the_workspace_setting(client, db, target):
    assert _put(client, CAROL_TENANT, "carol", {"qa_flows_per_step": 8}).status_code == 200
    response = run(client, target["id"], file="none", flows_per_step=20)
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow_id"]
    # MUTATION: let the workspace setting override the run's and this is five steps.
    assert _batch_steps(db, workflow_id) == ["flows-1", "flows-2"]
    record = db.docs[f"qa_target_runs/{workflow_id}"]
    assert (record["flows_per_step"], record["flows_per_step_from"]) == (20, "run")


@pytest.mark.parametrize("value", [0, 31, "15", 2.5, True])
def test_a_run_outside_1_to_30_is_refused_and_starts_nothing(client, db, target, value):
    response = run(client, target["id"], file="none", flows_per_step=value)
    # MUTATION: type `RunBody.flows_per_step` as any int and 0 or 31 is 201.
    assert response.status_code == 422, response.text
    assert not [p for p in db.docs if p.startswith("workflows/")]


def test_a_leg_over_the_step_bound_is_refused_with_the_smallest_batch_that_fits(client, db, seeded):
    created = register(client, flows=_flows(150)).json()["qa_target"]
    approve(client, created["id"])
    response = run(client, created["id"], file="none", flows_per_step=1)
    # MUTATION: drop qaspec's step check and the submission refuses it unexplained.
    assert response.status_code == 422, response.text
    assert "flows_per_step" in response.json()["message"]
    assert not [p for p in db.docs if p.startswith("workflows/")]
    assert run(client, created["id"], file="none").status_code == 201


# --------------------------------------------------------------------------
# The cap, through the route
# --------------------------------------------------------------------------


def test_150_flows_register_and_151_are_refused_naming_the_bound(client, db, seeded):
    ok = register(client, flows=_flows(150))
    # MUTATION: leave MAX_FLOWS at 10 and this is 422.
    assert ok.status_code == 201, ok.text
    refused = register(client, name="Shop 2", flows=_flows(151))
    assert refused.status_code == 422, refused.text
    assert str(qaspec.MAX_FLOWS) in refused.json()["message"]


# --------------------------------------------------------------------------
# The workspace setting
# --------------------------------------------------------------------------


def test_the_setting_reads_15_until_set_and_says_its_bounds(client, db, seeded):
    response = client.get(_url("eng"), headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["qa_flows_per_step"] == 15
    assert body["defaults"] == {"qa_flows_per_step": 15}
    assert body["bounds"] == {"qa_flows_per_step": {"min": 1, "max": 30}}
    assert body["can_edit"] is False
    selected = client.get("/v1/qa-settings", headers={**auth_header("alice"), "X-Swarm-Tenant": "eng"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["qa_flows_per_step"] == 15


def test_an_admin_sets_it_once_audited_and_the_read_serves_it(client, db, seeded):
    response = _put(client, "eng", "root", {"qa_flows_per_step": 20})
    assert response.status_code == 200, response.text
    assert response.json()["qa_flows_per_step"] == 20 and response.json()["can_edit"] is True
    assert client.get(_url("eng"), headers=auth_header("alice")).json()["qa_flows_per_step"] == 20
    (row,) = _audit(db)
    # MUTATION: drop the audit write and this is empty.
    assert row["target_tenant_id"] == "eng" and row["by"] == "root@example.com"
    assert row["detail"] == {"tenant_id": "eng", "from": 15, "to": 20}
    # The same value again changes nothing and audits nothing.
    assert _put(client, "eng", "root", {"qa_flows_per_step": 20}).status_code == 200
    assert len(_audit(db)) == 1


def test_the_owner_of_a_personal_workspace_sets_it(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"qa_flows_per_step": 3})
    assert response.status_code == 200, response.text
    assert db.docs[f"workspace_settings/{CAROL_TENANT}"][qasettings.FIELD] == 3


@pytest.mark.parametrize("value", [0, 31, "15", 2.5, True, None])
def test_a_setting_outside_1_to_30_is_refused_and_nothing_written(client, db, seeded, value):
    response = _put(client, "eng", "root", {"qa_flows_per_step": value})
    # MUTATION: drop the Field bounds on `QaSettingsPut` and 0 or 31 is stored.
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_member_who_is_not_its_admin_is_refused_and_nothing_written(client, db, seeded):
    response = _put(client, "eng", "alice", {"qa_flows_per_step": 4})
    # MUTATION: let any member change it and this is 200.
    assert response.status_code == 403, response.text
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_caller_outside_the_workspace_is_answered_not_found(client, db, seeded):
    # bob is in research, not eng: the established 404, never the setting.
    assert client.get(_url("eng"), headers=auth_header("bob")).status_code == 404
    assert _put(client, "eng", "bob", {"qa_flows_per_step": 4}).status_code in (403, 404)
    assert "workspace_settings/eng" not in db.docs
    assert client.get(_url("no-such-workspace"), headers=auth_header("root")).status_code == 404


@pytest.mark.parametrize("planted", [0, 31, 500, "15", True, 2.5])
def test_a_stored_value_no_swarm_api_path_wrote_is_read_as_the_default(client, db, target, planted):
    # A worker's direct Firestore write (tenant-settings-integrity.md §1.3):
    # the setting is not yet MAC'd, so its range is the reader's own check.
    db.docs[f"workspace_settings/{CAROL_TENANT}"] = {"tenant_id": CAROL_TENANT, qasettings.FIELD: planted}
    response = run(client, target["id"], file="none")
    assert response.status_code == 201, response.text
    record = db.docs[f"qa_target_runs/{response.json()['workflow_id']}"]
    # MUTATION: read the stored value unchecked and 500 reaches the render.
    assert (record["flows_per_step"], record["flows_per_step_from"]) == (15, "default")
