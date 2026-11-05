"""QA flows that write say what they write, behind a workspace switch (part of #2688).

BGD, 2026-11-05, "declared writes plus a switch, on by default"
(docs/design/qa-environments.md §6):

  * a flow with `mutating: true` carries `writes`, one plain sentence of at
    most `qaspec.WRITES_MAX` characters; a save without a non-empty one is
    refused 422 `qa_flow_writes_required`, with a fixed sentence, through its
    switch, which ships on;
  * the writing flow's step brief states the sentence as the limit of what it
    may write, and its report repeats it word for word under "This flow wrote";
  * the workspace's `writing_flows` QA setting, default on, admin-only to
    change: off, every flow renders read-only and a save of a mutating flow is
    refused 422 `qa_writing_flows_off`;
  * a production leg is unchanged: refused at a one-leg save, and rendered no
    mutating flow on a target of several legs, `writes` or not;
  * a mutating flow stored before the rule, with no `writes`, renders read-only.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qasettings, qaspec, refusals

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

WRITES = "One order for the cheapest plan, in the shop's test catalogue."


def _flow(**over: Any) -> dict[str, Any]:
    return {"id": "checkout", "goal": "buy the cheapest plan", "start_path": "/pricing", "mutating": True,
            "writes": WRITES, **over}


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_0123456789abcdef", "name": "Shop", "base_url": "https://shop.example.com",
        "repository": "https://github.com/example-org/shop", "environment": "non_production",
        "screens": [{"id": "home", "path": "/"}], "flows": [_flow()],
    }
    target.update(over)
    return target


def _steps(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in spec["steps"]}


def _settings_url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/qa-settings"


def _writing_off(client, tenant_id: str = CAROL_TENANT, user: str = "carol"):
    return client.put(_settings_url(tenant_id), json={"writing_flows": False}, headers=auth_header(user))


def _run(client, target_id: str):
    return client.post(f"{TARGETS}/{target_id}:run", json={"file": "none"}, headers=auth_header("carol"))


def _step_ids(db: Any, workflow_id: str) -> set[str]:
    return {d["step_id"] for p, d in db.docs.items() if p.startswith("tasks/") and d.get("workflow_id") == workflow_id}


# --------------------------------------------------------------------------
# The save
# --------------------------------------------------------------------------


@pytest.mark.parametrize("writes", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_a_mutating_flow_without_writes_is_refused_with_a_fixed_sentence(client, db, seeded, writes):
    flow = _flow()
    if writes is None:
        del flow["writes"]
    else:
        flow["writes"] = writes
    response = register(client, environment="non_production", flows=[flow])
    # MUTATION: drop `_check_writing_flows` from `QaTargets._build` and this is 201.
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["detail"]["reason"] == "qa_flow_writes_required"
    assert body["message"] == (
        "A flow that writes (mutating: true) must say what it writes in `writes`, one plain sentence. "
        "Nothing was saved.")
    assert not [p for p in db.docs if p.startswith("qa_targets/")]


def test_a_mutating_flow_with_writes_is_saved_and_served(client, db, seeded):
    response = register(client, environment="non_production", flows=[_flow()])
    assert response.status_code == 201, response.text
    assert response.json()["qa_target"]["flows"][0]["writes"] == WRITES


@pytest.mark.parametrize("writes", ["x" * 301, "two\nlines", "a `backtick`", 7],
                         ids=["too-long", "two-lines", "backtick", "not-text"])
def test_a_writes_that_is_not_one_bounded_line_is_refused(client, db, seeded, writes):
    response = register(client, environment="non_production", flows=[_flow(writes=writes)])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_target_invalid"


def test_the_refusal_ships_on_and_off_saves_a_flow_that_reads_only(client, db, seeded, monkeypatch):
    assert refusals.SWITCHES["qa_flow_writes_required"].default is True
    assert refusals.enforced("qa_flow_writes_required", environ={})
    monkeypatch.setenv(refusals.env_name("qa_flow_writes_required"), "off")
    response = register(client, environment="non_production", flows=[_flow(writes=None)])
    assert response.status_code == 201, response.text
    created = response.json()["qa_target"]
    approve(client, created["id"])
    run = _run(client, created["id"])
    assert run.status_code == 201, run.text
    # Saved, it still never writes: no `flow-checkout` step, the flow in a read-only batch.
    steps = _step_ids(db, run.json()["workflow_id"])
    assert "flow-checkout" not in steps and "flows-1" in steps


# --------------------------------------------------------------------------
# The brief and the report
# --------------------------------------------------------------------------


def test_the_writing_flows_brief_states_its_writes_as_the_limit_and_its_report_repeats_it() -> None:
    steps = _steps(qaspec.render(_target(), file="none"))
    brief = steps["flow-checkout"]["prompt"]
    # MUTATION: render `_safety` without `declared` and the sentence is gone from the brief.
    assert f'which is the limit of what it may write: "{WRITES}" Write nothing else' in brief
    assert f"`this_flow_wrote` to this sentence, word for word: {WRITES}" in brief
    report = steps["flow-report"]["prompt"]
    # MUTATION: drop `wrote` from `_flow_report_prompt` and the report says nothing it wrote.
    assert f'the line "This flow wrote: {WRITES}", word for word' in report


def test_a_read_only_flow_says_no_writes() -> None:
    target = _target(flows=[{"id": "look", "goal": "find pricing", "start_path": "/",
                             "writes": "Nothing, it only reads."}])
    steps = _steps(qaspec.render(target, file="none"))
    assert "This flow wrote" not in steps["flow-report"]["prompt"]
    assert "Submit no form" in steps["flows-1"]["prompt"]


def test_a_stored_mutating_flow_without_writes_renders_read_only() -> None:
    flow = {k: v for k, v in _flow().items() if k != "writes"}
    spec = qaspec.render(_target(flows=[flow]), file="none")
    steps = _steps(spec)
    # MUTATION: keep `mutating` in `_flows` when `writes` is missing and flow-checkout renders.
    assert "flow-checkout" not in steps
    assert "Submit no form" in steps["flows-1"]["prompt"]
    assert "This flow wrote" not in steps["flow-report"]["prompt"]
    # The gate's view says the same: the leg, as a run reads it, holds the flow read-only.
    stored = qaspec.leg(_target(flows=[flow]), writing_flows=True)
    assert stored["flows"][0]["mutating"] is False


# --------------------------------------------------------------------------
# The workspace switch
# --------------------------------------------------------------------------


def test_writing_flows_is_on_by_default_and_served(client, db, seeded):
    response = client.get(_settings_url(CAROL_TENANT), headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    assert response.json()["writing_flows"] is True
    assert response.json()["defaults"]["writing_flows"] is True


def test_an_admin_turns_it_off_once_audited(client, db, seeded):
    response = client.put(_settings_url("eng"), json={"writing_flows": False}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["writing_flows"] is False
    assert db.docs[f"{qasettings.COLLECTION}/eng"][qasettings.WRITING_FIELD] is False
    # The flows-per-step setting is untouched.
    assert response.json()["qa_flows_per_step"] == qasettings.DEFAULT
    assert client.put(_settings_url("eng"), json={"writing_flows": False},
                      headers=auth_header("root")).status_code == 200
    audits = [d for p, d in db.docs.items() if p.startswith("admin_audit/")
              and d.get("action") == qasettings.WRITING_AUDIT_ACTION]
    assert len(audits) == 1 and audits[0]["detail"] == {"tenant_id": "eng", "from": True, "to": False}


def test_a_member_who_is_not_its_admin_may_not_change_it(client, db, seeded):
    response = client.put(_settings_url("eng"), json={"writing_flows": False}, headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert f"{qasettings.COLLECTION}/eng" not in db.docs


@pytest.mark.parametrize("body", [{}, {"writing_flows": "false"}, {"writing_flows": 0}],
                         ids=["neither", "string", "int"])
def test_a_put_naming_no_setting_or_a_non_bool_is_refused(client, db, seeded, body):
    response = client.put(_settings_url(CAROL_TENANT), json=body, headers=auth_header("carol"))
    assert response.status_code == 422, response.text


def test_off_refuses_saving_a_mutating_flow_with_its_own_code(client, db, seeded):
    assert _writing_off(client).status_code == 200
    response = register(client, environment="non_production", flows=[_flow()])
    # MUTATION: skip the `writing_flows` read in `_check_writing_flows` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_writing_flows_off"
    assert response.json()["message"] == (
        "This workspace's QA settings turn writing flows off, so no flow may be mutating. A workspace "
        "admin can turn writing flows on. Nothing was saved.")
    # A read-only flow is still saved.
    assert register(client, environment="non_production",
                    flows=[{"id": "look", "goal": "find pricing", "start_path": "/"}]).status_code == 201


def test_off_renders_no_mutating_flow_for_a_target_saved_while_on(client, db, seeded):
    created = register(client, environment="non_production", flows=[_flow()]).json()["qa_target"]
    approve(client, created["id"])
    on = _run(client, created["id"])
    assert on.status_code == 201, on.text
    assert "flow-checkout" in _step_ids(db, on.json()["workflow_id"])
    assert _writing_off(client).status_code == 200
    off = _run(client, created["id"])
    assert off.status_code == 201, off.text
    steps = _step_ids(db, off.json()["workflow_id"])
    # MUTATION: build `_runnable_view` with `_view` (no `writing_flows`) and flow-checkout renders.
    assert "flow-checkout" not in steps and "flows-1" in steps


def test_off_turns_every_flow_of_a_leg_read_only() -> None:
    view = qaspec.leg(_target(), writing_flows=False)
    assert [f["mutating"] for f in view["flows"]] == [False]
    assert "flow-checkout" not in _steps(qaspec.render(view, file="none"))
    # None, what a save checks, is the leg as stored.
    assert qaspec.leg(_target())["flows"][0]["mutating"] is True


# --------------------------------------------------------------------------
# Production legs are unchanged
# --------------------------------------------------------------------------


def test_a_production_target_still_refuses_a_mutating_flow_with_writes(client, db, seeded):
    response = register(client, flows=[_flow()])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_mutating_flow_on_production"
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(environment="production"))
    assert err.value.code == "qa_mutating_flow_on_production"


@pytest.mark.parametrize("writing_flows", [None, True])
def test_a_production_leg_of_several_still_renders_no_mutating_flow(writing_flows) -> None:
    target = {
        "id": "qat_0123456789abcdef", "name": "Shop", "repository": "https://github.com/example-org/shop",
        "screens": [{"id": "home", "path": "/"}], "flows": [_flow()],
        "environments": [
            {"name": "prod", "environment": "production", "base_url": "https://shop.example.com"},
            {"name": "same", "environment": "non_production", "base_url": "https://shop.example.com"},
            {"name": "stage", "environment": "non_production", "base_url": "https://staging.shop.example.com"},
        ],
    }
    for name in ("prod", "same"):
        assert qaspec.leg(target, name, writing_flows=writing_flows)["flows"] == []
    stage = qaspec.leg(target, "stage", writing_flows=writing_flows)
    assert stage["flows"][0]["mutating"] is True and stage["flows"][0]["writes"] == WRITES
