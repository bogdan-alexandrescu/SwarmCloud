"""A workflow held until another pull request merges (`after_merge`).

docs/design/merging-without-a-laptop.md §4, lane 4. What these tests hold:

  1. HOLDING WRITES NO WORKFLOW AND NO AGENT. A spec with `after_merge` is a
     `held_workflows` row, HELD; no workflow, no task, no lease (invariant 1).
     A plain `POST /v1/workflows` naming `after_merge` is refused as an
     unknown field rather than started at once.
  2. RELEASED AFTER THE PREDECESSOR MERGES. The merge-wake tick leaves the row
     HELD while the predecessor's merge step has not merged, and submits it
     as its submitter once the step is SUCCEEDED with a merge recorded, or its
     train entry LANDED; a `pull_request` entry is released once GitHub
     reports it merged.
  3. BLOCKED AFTER A CLOSE. A pull request closed unmerged, or a merge step
     that failed, moves the row to BLOCKED, which holds nothing.
  4. REFUSED: a cycle, and another tenant's workflow id (in the same words
     as a missing one, invariant 9).

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, heldworkflows
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import ApiError
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.mergetrain import ENTRIES_COLLECTION, LANDED, entry_id_for
from swarm_api.metrics import ApiMetrics
from swarm_api.schemas import HeldWorkflowCreate
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_task, seed_tenant
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD,
    SWEEPER,
    SWEEPER_HEADERS,
    clock,
    tenant_tokens,
)

REPO = "https://github.com/example-org/widget-shop.git"
PR = 41


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = forge_fakes.GitHubWrites()
    gh.open_pull(PR, HEAD, ref="feature/relay")
    return gh


@pytest.fixture
def world(db, tokens, group_map, objects, github, tenant_tokens, clock):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=tenant_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate, fetch=github.fetch),
    )
    return context, TestClient(create_app(context), raise_server_exceptions=False)


def _spec(**extra) -> dict:
    spec = {
        "strategy": "direct-pr",
        "repository_url": REPO,
        "metadata": {"merge": "off"},
        "steps": [{"step_id": "implement", "runner_profile": "mock",
                   "input": {"prompt": "build on the merged change"}}],
    }
    spec.update(extra)
    return spec


def _submit(api: TestClient, user: str = "alice") -> str:
    response = api.post("/v1/workflows", headers=auth_header(user), json=_spec())
    assert response.status_code == 201, response.text
    return response.json()["workflow"]["workflow_id"]


def _hold(ctx, after_merge: list, user: str = "alice") -> dict:
    auth = ctx.authenticator.authenticate(f"Bearer token-{user}")
    return heldworkflows.hold(ctx, auth, HeldWorkflowCreate.model_validate(
        _spec(after_merge=after_merge)))


def _merge_step(db, workflow_id: str, *, state: str = "PARKED", summary=None) -> dict:
    doc = seed_task(db, task_id=f"task_merge{workflow_id[-6:]}", tenant_id="eng",
                    state=state, runner_profile="merge", workflow_id=workflow_id)
    doc["result_summary"] = summary
    return doc


def _tick(api: TestClient, tenant_id: str = "eng") -> dict:
    response = api.post(f"/v1/admin/merges/wake?tenant_id={tenant_id}", headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _row(db, row_id: str) -> dict:
    return db.docs[f"{heldworkflows.HELD_WORKFLOWS}/{row_id}"]


def _paths(db, prefix: str) -> set[str]:
    return {p for p in db.docs if p.startswith(prefix)}


# --------------------------------------------------------------------------
# 1. holding writes nothing that runs
# --------------------------------------------------------------------------

def test_a_held_spec_writes_no_workflow_and_no_agent(world, db):
    ctx, api = world
    before = _submit(api)
    workflows, tasks = _paths(db, "workflows/"), _paths(db, "tasks/")

    row = _hold(ctx, [{"workflow_id": before}])

    assert row["state"] == heldworkflows.HELD
    assert row["tenant_id"] == "eng" and row["workflow_id"] is None
    assert _paths(db, "workflows/") == workflows
    assert _paths(db, "tasks/") == tasks
    assert not _paths(db, "leases/")
    stored = _row(db, row["id"])
    assert "after_merge" not in stored["spec"]
    assert stored["after_merge"] == [{"workflow_id": before}]


def test_a_plain_workflow_submission_naming_after_merge_is_refused(world, db):
    _ctx, api = world
    response = api.post("/v1/workflows", headers=auth_header("alice"),
                        json=_spec(after_merge=[{"pull_request": PR}]))
    assert response.status_code == 422, response.text
    assert not _paths(db, "workflows/")


# --------------------------------------------------------------------------
# 2. released after the predecessor merges
# --------------------------------------------------------------------------

def test_the_tick_releases_a_row_once_the_predecessor_merge_step_merged(world, db):
    ctx, api = world
    before = _submit(api)
    step = _merge_step(db, before)
    row = _hold(ctx, [{"workflow_id": before}])

    report = _tick(api)["held_workflows"]
    assert report["waiting"] == 1 and report["released"] == 0
    assert _row(db, row["id"])["state"] == heldworkflows.HELD

    # Green but not merged is not a merge.
    step["state"] = "SUCCEEDED"
    step["result_summary"] = {"pull_request": 7}
    assert _tick(api)["held_workflows"]["blocked"] == 1
    assert _row(db, row["id"])["state"] == heldworkflows.BLOCKED


def test_a_merged_predecessor_releases_the_spec_as_its_submitter(world, db):
    ctx, api = world
    before = _submit(api)
    step = _merge_step(db, before)
    row = _hold(ctx, [{"workflow_id": before}])
    assert _tick(api)["held_workflows"]["released"] == 0

    step["state"] = "SUCCEEDED"
    step["result_summary"] = {"merged_by_this_task": True, "merge_commit": "d" * 40}
    report = _tick(api)["held_workflows"]

    assert report["released"] == 1, report
    stored = _row(db, row["id"])
    assert stored["state"] == heldworkflows.RELEASED
    released = db.docs[f"workflows/{stored['workflow_id']}"]
    assert released["tenant_id"] == "eng"
    assert released["submitted_by"] == "alice@example.com"
    # Released once: the next tick leaves it alone.
    assert _tick(api)["held_workflows"]["released"] == 0


def test_a_landed_train_entry_releases_the_row(world, db):
    ctx, api = world
    before = _submit(api)
    step = _merge_step(db, before)
    row = _hold(ctx, [{"workflow_id": before}])
    db.docs[f"{ENTRIES_COLLECTION}/{entry_id_for('eng', step['id'])}"] = {
        "tenant_id": "eng", "task_id": step["id"], "state": LANDED}

    assert _tick(api)["held_workflows"]["released"] == 1
    assert _row(db, row["id"])["state"] == heldworkflows.RELEASED


def test_a_pull_request_entry_waits_until_github_reports_it_merged(world, db, github):
    ctx, api = world
    row = _hold(ctx, [{"pull_request": PR}])
    assert _tick(api)["held_workflows"]["waiting"] == 1
    assert not _paths(db, "workflows/")

    github.pulls[PR].update(state="closed", merged=True)
    assert _tick(api)["held_workflows"]["released"] == 1
    assert _row(db, row["id"])["state"] == heldworkflows.RELEASED


# --------------------------------------------------------------------------
# 3. blocked after a close
# --------------------------------------------------------------------------

def test_a_pull_request_closed_unmerged_blocks_the_row(world, db, github):
    ctx, api = world
    row = _hold(ctx, [{"pull_request": PR}])
    github.pulls[PR]["state"] = "closed"

    assert _tick(api)["held_workflows"]["blocked"] == 1
    stored = _row(db, row["id"])
    assert stored["state"] == heldworkflows.BLOCKED
    assert stored["error"] == f"pull_request_{PR}_closed_unmerged"
    assert not _paths(db, "workflows/") and not _paths(db, "leases/")


def test_a_failed_merge_step_blocks_the_row(world, db):
    ctx, api = world
    before = _submit(api)
    _merge_step(db, before, state="FAILED")
    row = _hold(ctx, [{"workflow_id": before}])

    assert _tick(api)["held_workflows"]["blocked"] == 1
    assert _row(db, row["id"])["error"] == "predecessor_unmerged"


# --------------------------------------------------------------------------
# 4. refused
# --------------------------------------------------------------------------

def test_another_tenants_workflow_is_refused_as_a_missing_one(world, db):
    ctx, api = world
    theirs = _submit(api, user="bob")

    with pytest.raises(ApiError) as other:
        _hold(ctx, [{"workflow_id": theirs}])
    with pytest.raises(ApiError) as missing:
        _hold(ctx, [{"workflow_id": "wf_doesnotexist"}])

    assert other.value.code == missing.value.code == "after_merge_unknown"
    assert other.value.message.replace(theirs, "X") == missing.value.message.replace(
        "wf_doesnotexist", "X")
    assert not _paths(db, f"{heldworkflows.HELD_WORKFLOWS}/")


def test_a_cycle_is_refused(world, db):
    ctx, api = world
    before = _submit(api)
    first = _hold(ctx, [{"workflow_id": before}])
    second = _hold(ctx, [{"workflow_id": first["id"]}])
    # A row rewritten to wait on its own successor closes a cycle.
    _row(db, first["id"])["after_merge"] = [{"workflow_id": second["id"]}]

    with pytest.raises(ApiError) as refused:
        _hold(ctx, [{"workflow_id": second["id"]}])
    assert refused.value.code == "after_merge_refused"
    assert "cycle" in refused.value.message


def test_another_repositorys_pull_request_cannot_be_named():
    with pytest.raises(ValueError):
        HeldWorkflowCreate.model_validate(_spec(after_merge=[{"pull_request": "other/repo#5"}]))
    with pytest.raises(ValueError):
        HeldWorkflowCreate.model_validate(
            _spec(after_merge=[{"pull_request": PR, "workflow_id": "wf_abc"}]))
