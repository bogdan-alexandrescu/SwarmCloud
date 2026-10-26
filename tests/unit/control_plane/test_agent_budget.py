"""The per-agent budget's settings, refusals and resolution (BUDGET-CAP, part of #2459).

BGD, 2026-10-26: a workspace admin sets a per-agent budget in tokens or
dollars; a step may only lower it; a platform admin's ceiling caps it; an
attempt's budget is min(step, workspace, ceiling), each unit on its own; and
with nothing set, nothing changes.

    GET/PUT /v1/workspaces/{tenant_id}/budget   `{budget: {unit, amount}}`, audited
    PUT     /v1/admin/settings                  `agent_budget_ceiling: {usd, tokens}`
    POST    /v1/tasks, /v1/workflows            `budget: {usd: N}` or `{tokens: N}`

What these hold:

  * resolution is the min per unit, and mixed units are each a limit;
  * a ceiling alone, with no workspace default and no step budget, is no budget;
  * a step budget above the workspace default or the ceiling is 422
    `budget_above_limit`, naming both, and nothing is stored;
  * a workspace default above the ceiling is 422 `budget_above_ceiling`,
    naming the ceiling, and nothing is written;
  * both refusals ship ON (`refusals.SWITCHES`, BGD's answer to question 5,
    2026-10-26: "enforce at once"), so with no `REFUSAL_*` variable set the
    422s are served; switched off, the step still runs under the lower limit
    and the default is still capped;
  * the resolved budget is pinned inside the SIGNED dispatch block;
  * with no default and no step budget the stored dispatch block has no
    `budget` key at all.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import budget, refusals
from swarm_api.budget import Limits, resolve

from .conftest import auth_header, seed_tenant


@pytest.fixture(autouse=True)
def _eng(db):
    """alice's group workspace, which root (a platform admin) administers."""
    seed_tenant(db, "eng")


@pytest.fixture
def refusals_on(monkeypatch):
    """Both switches on, as `REFUSAL_<CODE>=on` turns them on in a deployment."""
    for code in (budget.ABOVE_LIMIT, budget.ABOVE_CEILING):
        monkeypatch.setenv(refusals.env_name(code), refusals.ON)


@pytest.fixture
def refusals_off(monkeypatch):
    """Both switches off, as `REFUSAL_<CODE>=off` turns a default-on one off."""
    for code in (budget.ABOVE_LIMIT, budget.ABOVE_CEILING):
        monkeypatch.setenv(refusals.env_name(code), refusals.OFF)


@pytest.fixture
def refusals_unset(monkeypatch):
    """Neither variable set: what the first deploy runs, before terraform
    names either switch."""
    for code in (budget.ABOVE_LIMIT, budget.ABOVE_CEILING):
        monkeypatch.delenv(refusals.env_name(code), raising=False)


def _tasks(db) -> list[dict[str, Any]]:
    return [doc for key, doc in db.docs.items() if key.startswith("tasks/") and key.count("/") == 1]


def _audit(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == budget.AUDIT_ACTION]


def _put_default(client, tenant_id: str, user: str, value: Any):
    return client.put(f"/v1/workspaces/{tenant_id}/budget", json={"budget": value},
                      headers=auth_header(user))


def _put_ceiling(client, ceiling: dict[str, Any]):
    return client.put("/v1/admin/settings", json={"agent_budget_ceiling": ceiling},
                      headers=auth_header("root"))


def _submit(client, **extra: Any):
    body = {"runner_profile": "mock", "input": {"prompt": "hello"}, **extra}
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


def _dispatch(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["metadata"]["dispatch"]


# --------------------------------------------------------------------------
# Resolution: min, and mixed units
# --------------------------------------------------------------------------


def test_resolution_is_the_least_of_the_three_in_each_unit():
    resolved = resolve(Limits(usd=3.0), Limits(usd=5.0), Limits(usd=4.0))
    # MUTATION: take the step's figure whatever the others say and this is 3 -> still
    # 3; so the workspace's own lower figure must win when the step names none.
    assert resolved == Limits(usd=3.0)
    assert resolve(budget.NONE, Limits(usd=5.0), Limits(usd=4.0)) == Limits(usd=4.0)
    assert resolve(budget.NONE, Limits(tokens=15_000_000), budget.NONE) == Limits(tokens=15_000_000)


def test_limits_in_different_units_are_each_kept():
    resolved = resolve(Limits(usd=1.0), Limits(tokens=15_000_000), Limits(usd=10.0, tokens=20_000_000))
    # MUTATION: resolve only the step's unit and the workspace's tokens are lost.
    assert resolved == Limits(usd=1.0, tokens=15_000_000)


def test_no_step_budget_and_no_workspace_default_is_no_budget_whatever_the_ceiling():
    # MUTATION: let the ceiling alone turn budgets on and this is a limit.
    assert resolve(budget.NONE, budget.NONE, Limits(usd=10.0)) is None
    assert resolve(budget.NONE, budget.NONE, budget.NONE) is None


def test_both_refusals_ship_on():
    # BGD, 2026-10-26 (question 5, "enforce at once"): unlike a new refusal's
    # usual report-only start, these two are enforced from the first deploy.
    for code in (budget.ABOVE_LIMIT, budget.ABOVE_CEILING):
        assert refusals.SWITCHES[code].default is True, code
        assert refusals.enforced(code, environ={}), code
        assert not refusals.enforced(code, environ={refusals.env_name(code): refusals.OFF}), code


def test_a_step_may_lower_but_never_raise(refusals_on):
    budget.refuse_step_above(Limits(usd=1.0), Limits(usd=2.0), Limits(usd=5.0))
    # Another unit is not "above": both stay limits.
    budget.refuse_step_above(Limits(usd=50.0), Limits(tokens=1_000), budget.NONE)
    with pytest.raises(budget.BudgetAboveLimit):
        budget.refuse_step_above(Limits(usd=3.0), Limits(usd=2.0), budget.NONE)
    with pytest.raises(budget.BudgetAboveLimit):
        budget.refuse_step_above(Limits(tokens=30), budget.NONE, Limits(tokens=20))


# --------------------------------------------------------------------------
# The workspace default and the platform ceiling
# --------------------------------------------------------------------------


def test_the_default_reads_absent_until_an_admin_sets_it_audited(client, db):
    read = client.get("/v1/workspaces/eng/budget", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    assert read.json()["budget"] is None and read.json()["can_edit"] is False
    response = _put_default(client, "eng", "root", {"unit": "tokens", "amount": 15_000_000})
    assert response.status_code == 200, response.text
    assert response.json()["budget"] == {"unit": "tokens", "amount": 15_000_000}
    assert db.docs["workspace_settings/eng"]["budget"]["unit"] == "tokens"
    (row,) = _audit(db)
    assert row["detail"] == {"tenant_id": "eng", "from": {}, "to": {"tokens": 15_000_000}}
    # The same default again audits nothing; null clears it.
    assert _put_default(client, "eng", "root", {"unit": "tokens", "amount": 15_000_000}).status_code == 200
    assert len(_audit(db)) == 1
    cleared = _put_default(client, "eng", "root", None)
    assert cleared.status_code == 200 and cleared.json()["budget"] is None


def test_a_member_who_is_not_its_admin_cannot_set_it(client, db):
    response = _put_default(client, "eng", "alice", {"unit": "usd", "amount": 5})
    assert response.status_code == 403, response.text
    assert "workspace_settings/eng" not in db.docs


@pytest.mark.parametrize("value", [
    {"unit": "usd", "amount": 0}, {"unit": "usd", "amount": -1}, {"unit": "tokens", "amount": 2.5},
    {"unit": "euro", "amount": 5}, {"unit": "usd", "amount": True}, {"unit": "usd"},
    {"unit": "usd", "amount": 5, "extra": 1},
])
def test_a_malformed_default_is_refused_and_nothing_written(client, db, value):
    response = _put_default(client, "eng", "root", value)
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs


def test_a_default_above_the_ceiling_is_refused_on_save_naming_the_ceiling(client, db, refusals_on):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    response = _put_default(client, "eng", "root", {"unit": "usd", "amount": 25})
    # MUTATION: skip `refuse_default_above_ceiling` and this is 200.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == budget.ABOVE_CEILING
    assert "$10.00" in response.json()["message"]
    assert "workspace_settings/eng" not in db.docs
    # At or under it, and in the other unit, is accepted.
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 10}).status_code == 200
    assert _put_default(client, "eng", "root", {"unit": "tokens", "amount": 10**9}).status_code == 200


def test_the_ceiling_is_replaced_whole_and_cleared_by_an_empty_map(client):
    assert _put_ceiling(client, {"usd": 10}).json()[budget.CEILING_SETTING] == {"usd": 10.0}
    # MUTATION: write the map as sent and the old usd survives the merge.
    assert _put_ceiling(client, {"tokens": 5_000}).json()[budget.CEILING_SETTING] == {"tokens": 5_000}
    assert _put_ceiling(client, {}).json()[budget.CEILING_SETTING] == {}
    assert _put_ceiling(client, {"usd": 0}).status_code == 422


def test_a_malformed_stored_default_is_no_limit_never_a_guess():
    assert budget.stored_default({"unit": "usd", "amount": "5"}) == budget.NONE
    assert budget.stored_default({"unit": "tokens", "amount": -3}) == budget.NONE
    assert budget.stored_default({"unit": "usd", "amount": 2}) == Limits(usd=2.0)


# --------------------------------------------------------------------------
# Submission: refused, resolved, pinned -- or nothing at all
# --------------------------------------------------------------------------


def test_with_no_default_and_no_step_budget_the_dispatch_block_is_unchanged(client, db):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    response = _submit(client)
    assert response.status_code == 201, response.text
    (doc,) = _tasks(db)
    # MUTATION: write an empty budget block for every task and this fails: a
    # task with no budget stores exactly the block it stored before #2459.
    assert budget.DISPATCH_KEY not in _dispatch(doc)
    assert "budget" not in response.json()["task"]["dispatch"]


def test_a_step_budget_in_a_workspace_with_no_default_is_pinned(client, db):
    response = _submit(client, budget={"usd": 1})
    assert response.status_code == 201, response.text
    (doc,) = _tasks(db)
    pinned = _dispatch(doc)["budget"]
    assert pinned["usd"] == 1.0 and "tokens" not in pinned
    assert pinned["from"] == {"step": {"usd": 1.0}, "workspace": {}, "ceiling": {}}
    # The task's read shows it, for the console's agent row.
    assert response.json()["task"]["dispatch"]["budget"]["usd"] == 1.0


def test_the_workspace_default_reaches_a_task_that_names_none(client, db):
    assert _put_default(client, "eng", "root", {"unit": "tokens", "amount": 15_000_000}).status_code == 200
    assert _submit(client).status_code == 201
    (doc,) = _tasks(db)
    # MUTATION: never read the workspace default and the task has no budget.
    assert _dispatch(doc)["budget"]["tokens"] == 15_000_000


def test_mixed_units_are_both_pinned(client, db):
    assert _put_default(client, "eng", "root", {"unit": "tokens", "amount": 15_000_000}).status_code == 200
    assert _submit(client, budget={"usd": 2}).status_code == 201
    (doc,) = _tasks(db)
    pinned = _dispatch(doc)["budget"]
    assert (pinned["usd"], pinned["tokens"]) == (2.0, 15_000_000)


def test_a_step_that_raises_its_limit_is_refused_naming_both(client, db, refusals_on):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 2}).status_code == 200
    response = _submit(client, budget={"usd": 5})
    # MUTATION: skip `refuse_step_above` and this is 201 with a $2 limit.
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == budget.ABOVE_LIMIT
    assert "$2.00" in body["message"] and "$10.00" in body["message"]
    assert body["detail"]["workspace_default"] == {"usd": 2.0}
    assert body["detail"]["ceiling"] == {"usd": 10.0}
    assert _tasks(db) == []


def test_with_no_switch_set_a_step_above_its_limit_is_refused(client, db, refusals_unset):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 2}).status_code == 200
    response = _submit(client, budget={"usd": 5})
    # MUTATION: ship `budget_above_limit` report-only and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == budget.ABOVE_LIMIT
    assert _tasks(db) == []


def test_with_no_switch_set_a_default_above_the_ceiling_is_refused(client, db, refusals_unset):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    response = _put_default(client, "eng", "root", {"unit": "usd", "amount": 25})
    # MUTATION: ship `budget_above_ceiling` report-only and this is 200.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == budget.ABOVE_CEILING
    assert "workspace_settings/eng" not in db.docs


def test_a_step_above_the_ceiling_is_refused_with_no_default(client, db, refusals_on):
    assert _put_ceiling(client, {"tokens": 1_000_000}).status_code == 200
    response = _submit(client, budget={"tokens": 2_000_000})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == budget.ABOVE_LIMIT
    assert _tasks(db) == []


@pytest.mark.parametrize("value", [{}, {"usd": 1, "tokens": 5}, {"usd": 0}, {"tokens": 1.5},
                                   {"usd": "1"}, {"dollars": 1}])
def test_a_malformed_step_budget_is_refused(client, db, value):
    response = _submit(client, budget=value)
    assert response.status_code == 422, response.text
    assert _tasks(db) == []


def test_a_workflow_step_budget_is_checked_and_pinned_per_step(client, db, refusals_on):
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 3}).status_code == 200
    steps = [
        {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}, "budget": {"usd": 1}},
        {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "b"}, "depends_on": ["a"]},
    ]
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": steps})
    assert response.status_code == 201, response.text
    by_step = {doc["step_id"]: _dispatch(doc).get("budget") for doc in _tasks(db)}
    assert by_step["a"]["usd"] == 1.0
    assert by_step["b"]["usd"] == 3.0
    raised = [{**steps[0], "budget": {"usd": 4}}, steps[1]]
    refused = client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": raised})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == budget.ABOVE_LIMIT
    assert refused.json()["detail"]["step_id"] == "a"


def test_off_a_step_that_asks_for_more_still_runs_under_the_lower_limit(client, db, refusals_off):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 2}).status_code == 200
    response = _submit(client, budget={"usd": 5})
    assert response.status_code == 201, response.text
    (doc,) = _tasks(db)
    # MUTATION: take the step's figure over the min and this is 5.
    assert _dispatch(doc)["budget"]["usd"] == 2.0


def test_off_a_default_above_the_ceiling_is_saved_and_capped(client, db, refusals_off):
    assert _put_ceiling(client, {"usd": 10}).status_code == 200
    assert _put_default(client, "eng", "root", {"unit": "usd", "amount": 25}).status_code == 200
    assert _submit(client).status_code == 201
    (doc,) = _tasks(db)
    assert _dispatch(doc)["budget"]["usd"] == 10.0
