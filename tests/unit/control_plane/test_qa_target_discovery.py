"""A QA target's discovery, started and stored by swarm-api
(docs/design/visual-qa-any-app.md §4.1; lane QA-DISCOVER, part of #1933).

BGD, 2026-10-23, after `ai-studio-staging-crawl` (qat_2c0948220748fda5)
could not be started from the console: "build discovery, so a crawl target is
started entirely from the console". What these tests hold, over the real app
and the in-memory Firestore:

  * `POST /v1/qa-targets/{id}:discover` submits ONE workflow, the member's
    own, whose one browser step carries the reserved QA-target mark exactly
    as `:run` sets it and is named by a `qa_target_runs` record; a viewer is
    refused before anything is written; a second call while one runs is 409
    naming the running workflow; a pending host is refused;
  * when the discovery step SUCCEEDS, the `task_finished` push (the
    platform's completion hook, `POST /v1/admin/tasks/finished`) reads its
    `screens.json` and stores it on the target as `discovered`, validated as
    strictly as `screens`: an off-origin path and an excluded one dropped,
    more than 60 cut to 60, duplicate ids refused; `:run` then renders from
    it;
  * a failed or refused discovery stores only `discovery_error`, and keeps
    the list an earlier success stored;
  * `:run` on a discover target with nothing discovered says to press Run
    discovery.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from swarm_api import qaspec, refusals, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ADMIN_GROUP,
    ENG_GROUP,
    PROJECT,
    RESEARCH_GROUP,
    NoForgeTokens,
    api_settings,
    auth_header,
)
from .test_qa_targets import (  # noqa: F401 -- pytest fixtures, used by name
    CAROL,
    CAROL_TENANT,
    CLASS_MODELS,
    CONSOLE,
    TARGETS,
    approve,
    register,
    resolver,
    seeded,
)
from .test_roles_store import grant

BUCKET = f"swarm-artifacts-{PROJECT}"
VIC = "vic@example.com"
DISCOVER = {"max_pages": 60, "max_depth": 2}


@pytest.fixture
def api_context(db, tokens, group_map, objects):
    """test_qa_targets' context, with a settings key so a role can be granted."""
    return build_context(
        settings=api_settings(worker_class_models=dict(CLASS_MODELS), console_url=CONSOLE,
                              settings_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def group_map():
    return {
        "alice@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "carol@example.com": (),
        VIC: (ENG_GROUP,),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


def crawl_target(client, db, **over: Any) -> dict[str, Any]:
    """carol's approved target that discovers its screens and lists none."""
    body = {"screens": None, "discover": DISCOVER, "exclusions": {"paths": ["/admin/*"]}, **over}
    response = register(client, **body)
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    assert approve(client, target["id"]).status_code == 200
    return target


def discover(client, target_id: str, user: str = "carol", headers: dict[str, str] | None = None):
    return client.post(f"{TARGETS}/{target_id}:discover", json={},
                       headers={**auth_header(user), **(headers or {})})


def workflows(db) -> list[str]:
    return [p for p in db.docs if p.startswith("workflows/")]


def tasks_of(db, workflow_id: str) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and d.get("workflow_id") == workflow_id]


def stored(db, target_id: str) -> dict[str, Any]:
    return db.docs[f"qa_targets/{target_id}"]


def finish(client, db, objects, workflow_id: str, *, state: str = "SUCCEEDED",
           document: Any = None) -> dict[str, Any]:
    """The discovery step ends `state`, its `screens.json` uploaded, and the
    worker's `task_finished` wake is pushed to swarm-api."""
    [task] = tasks_of(db, workflow_id)
    task["state"] = state
    entries = []
    if document is not None:
        text = document if isinstance(document, str) else json.dumps(document)
        key = f"tenants/{task['tenant_id']}/tasks/{task['id']}/attempts/att_1/artifacts/screens.json"
        objects.put(key, text.encode("utf-8"))
        entries.append({"name": "screens.json", "bytes": len(text.encode("utf-8")),
                        "uri": f"gs://{BUCKET}/{key}"})
    task["result_summary"] = {"artifacts": entries, "logs": {}}
    response = client.post("/v1/admin/tasks/finished", headers=auth_header("root"), json={
        "message": {"attributes": {"reason": "task_finished", "task_id": task["id"],
                                   "tenant_id": task["tenant_id"]}}})
    assert response.status_code == 200, response.text
    return task


def route_list(routes: list[dict[str, Any]]) -> dict[str, Any]:
    """`swarm_qa discover`'s document: format 1, groups of routes."""
    groups = {f"pages-{n + 1}": routes[i:i + 8] for n, i in enumerate(range(0, len(routes), 8))}
    return {"format": 1, "groups": groups, "mockups": [], "discovered": {"skipped": {}}}


# --------------------------------------------------------------------------
# :discover
# --------------------------------------------------------------------------


def test_discover_submits_one_marked_workflow_as_the_members_own(client, db, seeded):
    target = crawl_target(client, db)
    response = discover(client, target["id"])
    # MUTATION: drop the route and this is 404/405.
    assert response.status_code == 201, response.text
    answer = response.json()
    assert set(answer) == {"workflow_id", "qa_target"}
    assert answer["qa_target"] == {"id": target["id"], "revision": target["revision"]}
    # MUTATION: submit the workflow twice, or a run's, and this is not one workflow of one step.
    assert workflows(db) == [f"workflows/{answer['workflow_id']}"]
    [task] = tasks_of(db, answer["workflow_id"])
    assert task["step_id"] == "discover"
    assert task["tenant_id"] == CAROL_TENANT and task["submitted_by"] == CAROL
    # MUTATION: submit without the mark and the gate is handed no target to sign in to.
    assert task["metadata"]["qa_target"] == answer["qa_target"]
    # MUTATION: skip `record_run` and the worker's route serves this step nothing.
    run_doc = db.docs[f"qa_target_runs/{answer['workflow_id']}"]
    assert run_doc["target_id"] == target["id"] and run_doc["task_ids"] == [task["id"]]
    # The target says a discovery runs, and which: the console's "Discovering…".
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    # MUTATION: record nothing on the target and the console cannot link the running workflow.
    assert shown["discovering"]["workflow_id"] == answer["workflow_id"]


def test_a_viewer_cannot_discover_and_nothing_is_written(client, db, seeded, monkeypatch, api_context):
    monkeypatch.setenv(refusals.env_name("role_member_required"), "on")
    response = register(client, user="root", repository="example-org/widgets", screens=None,
                        discover=DISCOVER, name="Widgets")
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    assert target["tenant_id"] == "eng"
    approve(client, target["id"], host="shop.example.com")
    grant(api_context.roles, VIC, roles.VIEWER)
    before = workflows(db)
    response = discover(client, target["id"], user="vic")
    # MUTATION: drop `require_member` from the route and a viewer starts a discovery.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_member_required"
    assert workflows(db) == before
    assert "discovering" not in stored(db, target["id"])


def test_a_second_discovery_while_one_runs_is_409_naming_it(client, db, seeded):
    target = crawl_target(client, db)
    first = discover(client, target["id"]).json()["workflow_id"]
    response = discover(client, target["id"])
    # MUTATION: skip the running check and a second workflow is submitted.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qa_discovery_running"
    assert first in response.json()["message"]
    assert response.json()["detail"]["workflow_id"] == first
    assert workflows(db) == [f"workflows/{first}"]


def test_a_discovery_on_a_pending_host_is_refused(client, db, seeded):
    response = register(client, screens=None, discover=DISCOVER)
    target = response.json()["qa_target"]
    response = discover(client, target["id"])
    # MUTATION: skip the host check for a discovery and it browses an unapproved host.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qa_target_host_pending"
    assert workflows(db) == []


def test_a_target_that_does_not_discover_is_refused(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = discover(client, target["id"])
    # MUTATION: render a discovery for a target with no `discover` and this is 500 or 201.
    assert response.status_code == 422, response.text
    assert workflows(db) == []


def test_another_workspaces_target_answers_as_a_missing_one(client, db, seeded):
    target = crawl_target(client, db)
    # MUTATION: read the target by id alone and alice starts carol's discovery.
    assert discover(client, target["id"], user="alice").status_code == 404
    assert workflows(db) == []


# --------------------------------------------------------------------------
# Storing the result
# --------------------------------------------------------------------------


def test_a_successful_discovery_stores_a_validated_list_and_run_renders_from_it(client, db, seeded, objects):
    target = crawl_target(client, db)
    workflow_id = discover(client, target["id"]).json()["workflow_id"]
    routes = [{"id": "home", "path": "/", "ready": "body"},
              {"id": "off", "path": "https://elsewhere.example.com/x", "ready": "body"},
              {"id": "proto", "path": "//elsewhere.example.com/y", "ready": "body"},
              {"id": "logout", "path": "/logout", "ready": "body"},
              {"id": "admin-users", "path": "/admin/users", "ready": "body"}]
    routes += [{"id": f"page-{n}", "path": f"/page/{n}", "ready": "body"} for n in range(62)]
    finish(client, db, objects, workflow_id, document=route_list(routes))
    doc = stored(db, target["id"])
    ids = [s["id"] for s in doc["discovered"]]
    # MUTATION: keep an off-origin route and `off`/`proto` are stored.
    assert "off" not in ids and "proto" not in ids
    # MUTATION: skip the exclusions and `/logout` (a default) and `/admin/*` (the target's) are stored.
    assert "logout" not in ids and "admin-users" not in ids
    # MUTATION: store every route and this is 63.
    assert len(ids) == qaspec.MAX_SCREENS == 60
    assert ids[0] == "home" and ids[-1] == "page-58"
    assert all(set(s) == {"id", "path"} for s in doc["discovered"])
    assert doc["discovered_workflow"] == workflow_id
    assert doc["discovered_by"] == CAROL
    assert doc["discovered_at"] is not None
    # MUTATION: leave the running record and the console says Discovering forever.
    assert doc.get("discovering") is None
    assert doc.get("discovery_error") is None
    # MUTATION: bump the revision on the store and a run already started is refused its target.
    assert doc["revision"] == target["revision"]

    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert len(shown["discovered"]) == 60 and shown["discovered_workflow"] == workflow_id

    response = client.post(f"{TARGETS}/{target['id']}:run", json={"file": "none"},
                           headers=auth_header("carol"))
    # MUTATION: store nothing and `:run` refuses with qa_target_no_screens.
    assert response.status_code == 201, response.text
    prompts = " ".join(t["input"]["prompt"] for t in tasks_of(db, response.json()["workflow_id"])
                       if t["step_id"].startswith("capture-"))
    assert '"/page/58"' in prompts and '"/page/59"' not in prompts


def test_duplicate_ids_are_refused_and_the_earlier_list_is_kept(client, db, seeded, objects):
    target = crawl_target(client, db)
    first = discover(client, target["id"]).json()["workflow_id"]
    finish(client, db, objects, first, document=route_list([{"id": "home", "path": "/"}]))
    assert [s["id"] for s in stored(db, target["id"])["discovered"]] == ["home"]
    second = discover(client, target["id"])
    assert second.status_code == 201, second.text
    finish(client, db, objects, second.json()["workflow_id"],
           document=route_list([{"id": "home", "path": "/"}, {"id": "home", "path": "/again"}]))
    doc = stored(db, target["id"])
    # MUTATION: accept duplicate ids and the second list replaces the first.
    assert doc["discovered"] == [{"id": "home", "path": "/"}]
    assert doc["discovered_workflow"] == first
    assert "share an id" in doc["discovery_error"]
    assert doc.get("discovering") is None


def test_a_failed_discovery_stores_only_its_error(client, db, seeded, objects):
    target = crawl_target(client, db)
    workflow_id = discover(client, target["id"]).json()["workflow_id"]
    finish(client, db, objects, workflow_id, state="FAILED",
           document=route_list([{"id": "home", "path": "/"}]))
    doc = stored(db, target["id"])
    # MUTATION: read the artifact of a failed step and a list is stored.
    assert "discovered" not in doc
    assert "FAILED" in doc["discovery_error"]
    assert doc.get("discovering") is None
    # The next one may start.
    assert discover(client, target["id"]).status_code == 201


def test_a_discovery_that_wrote_no_screens_says_so(client, db, seeded, objects):
    target = crawl_target(client, db)
    workflow_id = discover(client, target["id"]).json()["workflow_id"]
    finish(client, db, objects, workflow_id)
    doc = stored(db, target["id"])
    assert "discovered" not in doc
    # MUTATION: treat a missing screens.json as an empty success and nothing says why.
    assert "screens.json" in doc["discovery_error"]


def test_an_empty_discovery_stores_nothing(client, db, seeded, objects):
    target = crawl_target(client, db)
    workflow_id = discover(client, target["id"]).json()["workflow_id"]
    finish(client, db, objects, workflow_id, document=route_list([{"id": "logout", "path": "/logout"}]))
    doc = stored(db, target["id"])
    # MUTATION: store an empty list and `:run` then fails with no screens.
    assert "discovered" not in doc
    assert doc["discovery_error"]


def test_a_finished_discovery_is_settled_on_read_when_no_wake_came(client, db, seeded, objects):
    target = crawl_target(client, db)
    workflow_id = discover(client, target["id"]).json()["workflow_id"]
    [task] = tasks_of(db, workflow_id)
    task["state"] = "DEAD_LETTERED"
    # No push: the scheduler or the reconciler ended it and rang nothing.
    shown = client.get(TARGETS, headers=auth_header("carol")).json()["qa_targets"][0]
    # MUTATION: settle only on the push and the console says Discovering forever.
    assert shown.get("discovering") is None
    assert "DEAD_LETTERED" in shown["discovery_error"]


def test_a_wake_for_another_task_changes_nothing(client, db, seeded, objects):
    target = crawl_target(client, db)
    discover(client, target["id"])
    before = dict(stored(db, target["id"]))
    response = client.post("/v1/admin/tasks/finished", headers=auth_header("root"), json={
        "message": {"attributes": {"reason": "task_finished", "task_id": "task_unrelated"}}})
    assert response.status_code == 200
    assert stored(db, target["id"]) == before


# --------------------------------------------------------------------------
# :run before discovery
# --------------------------------------------------------------------------


def test_run_before_discovery_says_to_press_run_discovery(client, db, seeded):
    target = crawl_target(client, db)
    response = client.post(f"{TARGETS}/{target['id']}:run", json={"file": "none"},
                           headers=auth_header("carol"))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_target_no_screens"
    # MUTATION: keep the old "run its discovery first" and nobody knows where.
    assert "Run discovery" in response.json()["message"]
    assert ":discover" in response.json()["message"]
    assert workflows(db) == []


# --------------------------------------------------------------------------
# The validator, alone
# --------------------------------------------------------------------------


BASE = {"base_url": "https://shop.example.com", "exclusions": {"paths": ["/admin/*"]}}


def test_the_validator_keeps_a_same_origin_absolute_url_as_its_path():
    found = qaspec.discovered_screens(BASE, route_list([
        {"id": "about", "path": "https://shop.example.com/about"},
        {"id": "port", "path": "https://shop.example.com:8443/x"},
        {"id": "http", "path": "http://shop.example.com/y"},
    ]))
    # MUTATION: compare hosts only and the other port or scheme is kept.
    assert found == [{"id": "about", "path": "/about"}]


@pytest.mark.parametrize("document", [
    None, [], "x", {"format": 2, "groups": {}}, {"format": 1, "groups": []},
    {"format": 1, "groups": {"g": "x"}},
])
def test_the_validator_refuses_a_document_that_is_not_a_route_list(document):
    with pytest.raises(qaspec.QaSpecError):
        qaspec.discovered_screens(BASE, document)


@pytest.mark.parametrize("route", [
    {"id": "Bad Id", "path": "/"}, {"id": "a--b", "path": "/"}, {"path": "/"},
    {"id": "x", "path": "/has space"}, {"id": "x"},
])
def test_the_validator_refuses_an_ill_formed_route(route):
    # MUTATION: drop the per-route checks and an id the capture tool refuses is stored.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.discovered_screens(BASE, route_list([{"id": "home", "path": "/"}, route]))
