"""One dispatch over an app's environments: `:run` with `environments`, the run
group, and `GET /v1/qa-runs/{group_id}` (docs/design/qa-environments.md §2.1,
§7; lane QAENV-RUN of §8; BGD's Q2 (a), 2026-10-23; part of #2136).

What these tests hold, over the real app and the in-memory Firestore:

  * `:run` with two names submits two workflows, each marked with its own
    leg only, and writes one `qa_run_groups/{group_id}` document (`qrg_` + 16
    hex) and one `qa_target_runs` row per leg carrying `environment` and
    `group_id`;
  * with no names it runs every entry;
  * every named leg is checked before ANY submission: a refusal on either leg
    (a pending host, an unknown name) submits nothing, names the leg, and
    writes no group;
  * the group is written before the first submission, each leg `pending`;
  * a later submission refused after an earlier one started answers 207, and
    the group shows that leg as not started, with why;
  * `GET /v1/qa-runs/{group_id}` answers the group in its own tenant only;
    another tenant's group answers exactly as a missing one.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from swarm_api.errors import Forbidden

from .conftest import auth_header
from .test_qa_target_environments import leg, register_legs
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL,
    CAROL_TENANT,
    TARGETS,
    allow_issues,
    api_context,
    approve,
    register,
    resolver,
    seeded,
)

GROUPS = "/v1/qa-runs"


def two_public_legs() -> list[dict[str, Any]]:
    return [leg("staging", "https://app.example.com", environment="non_production"),
            leg("production", "https://shop.example.com")]


def approved_two(client) -> dict[str, Any]:
    target = register_legs(client, two_public_legs()).json()["qa_target"]
    assert approve(client, target["id"], host="app.example.com", environment="staging").status_code == 200
    assert approve(client, target["id"], host="shop.example.com", environment="production").status_code == 200
    return target


def run(client, target_id: str, user: str = "carol", **body: Any):
    return client.post(f"{TARGETS}/{target_id}:run", json={"file": "none", **body}, headers=auth_header(user))


def docs(db: Any, collection: str) -> dict[str, dict[str, Any]]:
    return {p.split("/", 1)[1]: d for p, d in db.docs.items()
            if p.startswith(f"{collection}/") and p.count("/") == 1}


def tasks_of(db: Any, workflow_id: str) -> list[dict[str, Any]]:
    return [d for d in docs(db, "tasks").values() if d.get("workflow_id") == workflow_id]


def test_two_names_submit_two_workflows_each_with_its_one_mark(client, db, seeded):
    target = approved_two(client)
    response = run(client, target["id"], environments=["staging", "production"])
    assert response.status_code == 201, response.text
    answer = response.json()
    # MUTATION: submit only the first named leg and this is one leg.
    assert [leg_["environment"] for leg_ in answer["legs"]] == ["staging", "production"]
    assert re.fullmatch(r"qrg_[0-9a-f]{16}", answer["group_id"])
    workflows = docs(db, "workflows")
    assert len(workflows) == 2
    origins = {"staging": "https://app.example.com", "production": "https://shop.example.com"}
    for entry in answer["legs"]:
        name, workflow_id = entry["environment"], entry["workflow_id"]
        assert workflow_id in workflows and entry["state"] == "submitted"
        made = tasks_of(db, workflow_id)
        marked = [t for t in made if (t.get("metadata") or {}).get("qa_target")]
        assert marked and sorted(t["step_id"] for t in marked) == entry["browser_steps"]
        # MUTATION: stamp both workflows with the first leg's mark and production's steps name staging.
        assert all(t["metadata"]["qa_target"] == {"id": target["id"], "revision": target["revision"],
                                                   "environment": name} for t in marked)
        prompts = " ".join(t["input"]["prompt"] for t in made)
        other = origins["production" if name == "staging" else "staging"]
        assert origins[name] in prompts and other not in prompts
        row = db.docs[f"qa_target_runs/{workflow_id}"]
        # MUTATION: leave `group_id` off `record_run` and the leg's row is not linked to its group.
        assert row["environment"] == name and row["group_id"] == answer["group_id"]
        assert sorted(row["task_ids"]) == sorted(t["id"] for t in marked)
    group = db.docs[f"qa_run_groups/{answer['group_id']}"]
    # MUTATION: skip the group document and this is a KeyError.
    assert group["tenant_id"] == CAROL_TENANT and group["target_id"] == target["id"]
    assert group["revision"] == target["revision"] and group["by"] == CAROL and group["file"] == "none"
    assert [(g["environment"], g["workflow_id"], g["state"]) for g in group["legs"]] == [
        (e["environment"], e["workflow_id"], "submitted") for e in answer["legs"]]


def test_no_names_run_every_entry(client, db, seeded):
    target = approved_two(client)
    response = run(client, target["id"])
    assert response.status_code == 201, response.text
    # MUTATION: keep refusing a nameless run of a two-entry target (`qa_target_environment_required`).
    assert sorted(e["environment"] for e in response.json()["legs"]) == ["production", "staging"]
    assert len(docs(db, "workflows")) == 2


def test_a_refusal_on_either_leg_submits_nothing_and_names_the_leg(client, db, seeded):
    target = register_legs(client, two_public_legs()).json()["qa_target"]
    approve(client, target["id"], host="app.example.com", environment="staging")
    # Staging is approved and listed first; production's host is pending.
    response = run(client, target["id"], environments=["staging", "production"])
    # MUTATION: submit each leg as soon as it is checked and staging's workflow exists.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qa_target_host_pending"
    # MUTATION: drop the leg's name from the refusal and the console cannot say which leg.
    assert response.json()["detail"]["environment"] == "production"
    assert docs(db, "workflows") == {} and docs(db, "qa_target_runs") == {}
    # Refused before the group: nothing names a dispatch that never happened.
    assert docs(db, "qa_run_groups") == {}
    unknown = run(client, target["id"], environments=["staging", "qa"])
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["detail"]["environment"] == "qa"
    assert docs(db, "workflows") == {}


@pytest.mark.parametrize("names", [[], ["staging", "staging"], ["Staging"]])
def test_the_names_are_a_non_empty_list_of_distinct_names(client, db, seeded, names):
    target = approved_two(client)
    response = run(client, target["id"], environments=names)
    # MUTATION: accept an empty or repeated list and a leg runs twice, or nothing runs with a 201.
    assert response.status_code == 422, response.text
    assert docs(db, "workflows") == {}


def test_a_later_refusal_answers_207_and_the_group_says_why(client, db, seeded, api_context, monkeypatch):
    target = approved_two(client)
    real = api_context.submissions.submit_workflow
    seen: list[dict[str, Any]] = []

    def second_refused(auth, spec):
        # The group exists, every leg `pending`, before the first submission.
        (group,) = docs(db, "qa_run_groups").values()
        seen.append({g["environment"]: g["state"] for g in group["legs"]})
        if len(seen) == 2:
            raise Forbidden("the workspace is paused")
        return real(auth, spec)

    monkeypatch.setattr(api_context.submissions, "submit_workflow", second_refused)
    response = run(client, target["id"], environments=["staging", "production"])
    # MUTATION: let the refusal propagate after the first leg started and this is 403, the
    # staging workflow running with no record of the dispatch it belongs to.
    assert response.status_code == 207, response.text
    # MUTATION: write the group after the submissions and the first look finds none.
    assert seen[0] == {"staging": "pending", "production": "pending"}
    answer = response.json()
    by_name = {e["environment"]: e for e in answer["legs"]}
    assert by_name["staging"]["state"] == "submitted" and by_name["staging"]["workflow_id"]
    assert by_name["production"]["state"] == "not_started"
    assert by_name["production"]["workflow_id"] is None
    assert by_name["production"]["reason"] == {"code": "forbidden", "message": "the workspace is paused"}
    group = db.docs[f"qa_run_groups/{answer['group_id']}"]
    stored = {g["environment"]: g for g in group["legs"]}
    # MUTATION: leave the refused leg `pending` and the run page cannot tell it from one in flight.
    assert stored["production"]["state"] == "not_started"
    assert stored["production"]["reason"]["code"] == "forbidden"
    assert len(docs(db, "workflows")) == 1
    assert [r["environment"] for r in docs(db, "qa_target_runs").values()] == ["staging"]


def test_a_refusal_of_every_submission_answers_that_refusal(client, db, seeded, api_context, monkeypatch):
    target = approved_two(client)

    def refused(auth, spec):
        raise Forbidden("the workspace is paused")

    monkeypatch.setattr(api_context.submissions, "submit_workflow", refused)
    response = run(client, target["id"], environments=["staging", "production"])
    # Nothing started: the refusal is the answer, and the group says why for each leg.
    assert response.status_code == 403, response.text
    (group,) = docs(db, "qa_run_groups").values()
    assert {g["state"] for g in group["legs"]} == {"not_started"}


def test_a_one_entry_target_answers_as_it_did_with_its_group(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    allow_issues(db)
    response = run(client, target["id"])
    assert response.status_code == 201, response.text
    answer = response.json()
    # The console and the MCP tool read these; a one-leg run still answers them.
    assert answer["workflow_id"] == answer["legs"][0]["workflow_id"]
    assert answer["qa_target"] == {"id": target["id"], "revision": target["revision"]}
    assert answer["browser_steps"] == answer["legs"][0]["browser_steps"]
    # `environment`, the one-leg form a caller sent before this lane, still names one leg.
    again = run(client, target["id"], environment="production")
    assert again.status_code == 201, again.text
    both = run(client, target["id"], environment="production", environments=["production"])
    assert both.status_code == 422, both.text


# --------------------------------------------------------------------------
# GET /v1/qa-runs/{group_id}
# --------------------------------------------------------------------------


def test_the_group_reads_in_its_own_tenant_only(client, db, seeded):
    target = approved_two(client)
    answer = run(client, target["id"]).json()
    response = client.get(f"{GROUPS}/{answer['group_id']}", headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    group = response.json()["qa_run"]
    assert group["group_id"] == answer["group_id"] and group["target_id"] == target["id"]
    assert [(g["environment"], g["workflow_id"]) for g in group["legs"]] == [
        (e["environment"], e["workflow_id"]) for e in answer["legs"]]
    # Each leg carries its workflow's state, read in the same tenant.
    assert all(g["workflow_state"] for g in group["legs"])
    # MUTATION: read the group by id alone and alice, of eng, reads carol's dispatch.
    foreign = client.get(f"{GROUPS}/{answer['group_id']}", headers=auth_header("alice"))
    missing = client.get(f"{GROUPS}/qrg_{'0' * 16}", headers=auth_header("alice"))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert client.get(f"{GROUPS}/not-a-group", headers=auth_header("carol")).status_code == 404
