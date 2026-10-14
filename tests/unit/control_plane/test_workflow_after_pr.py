"""A workflow can wait for named pull requests to merge before its steps start (SPEED-D3).

Owner decision 2026-10-14 (delivery speed plan, D3): RELAY-ROLL, DEVB-GATE and
EGRESS-GATE each sat until an operator noticed that the pull request they
depended on had merged. A workflow spec now says so itself:

    "after": {"pr": 41}                 # or a list: [{"pr": 41}, {"pr": 42}]

What these tests hold:

  1. SUBMISSION. Every root step of a workflow with `after` is written PARKED
     on DEPENDENCY_INCOMPLETE, carrying `metadata.after_prs` -- the
     workflow's own repository and the numbers -- so the whole workflow waits:
     its other steps already wait on the roots. No lease, no pool count
     (invariant 1). `after` without a GitHub repository, with a number out of
     range, or with more than MAX_AFTER_PRS entries is refused, and a caller
     cannot write `metadata.after_prs` itself.
  2. PARKED WITH NO LEASE WHILE THE PR IS OPEN. The scheduler holds the park
     through every drain while the gate is not `merged`, beside an ungated
     workflow the same drain admits (the control).
  3. RELEASED ON MERGE. The merge-wake tick (`POST /v1/admin/merges/wake`,
     every minute per tenant) reads each pull request with the tenant's own
     `-git` token and, once every one is merged, marks the gate `merged`. It
     never moves the task; the scheduler's dependency sweep promotes it.
  4. FAILED ON CLOSE-UNMERGED. A pull request closed without merging marks
     the gate `closed`; the scheduler dead-letters the root naming the pull
     request, and the workflow fails.
  5. RESTART-SAFE. Every fact lives on the task document: a fresh API context
     and a fresh scheduler, between every step, reach the same answers.
  6. The console's and `sc workflows`' line: the workflow serves
     `after_pull_requests.text`, "waiting for PR #41 to merge".

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, issueci, prgate
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.states import ParkReason

from scheduler import loop as loop_mod

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_pool, seed_task, seed_tenant
from .test_merge_wake import (  # noqa: F401 -- fixtures, used by name
    HEAD,
    NOW,
    SWEEPER,
    SWEEPER_HEADERS,
    Clock,
    clock,
    tenant_tokens,
)

REPO = "https://github.com/octo-org/widget-shop.git"
PR = 41
OTHER_PR = 42


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = forge_fakes.GitHubWrites()
    gh.open_pull(PR, HEAD, ref="feature/relay")
    gh.open_pull(OTHER_PR, "c" * 40, ref="feature/gate")
    return gh


def _client(db, tokens, group_map, objects, github, tenant_tokens, clock) -> TestClient:
    """A whole API context over `db`: a fresh one is a restarted swarm-api."""
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
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def boot(db, tokens, group_map, objects, github, tenant_tokens, clock):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")

    def _boot() -> TestClient:
        return _client(db, tokens, group_map, objects, github, tenant_tokens, clock)

    return _boot


@pytest.fixture
def api(boot) -> TestClient:
    return boot()


def _spec(after=None, **overrides) -> dict:
    spec = {
        "strategy": "direct-pr",
        "repository_url": REPO,
        "metadata": {"merge": "off"},
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "roll the relay"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "input": {"prompt": "review it"}},
        ],
    }
    if after is not None:
        spec["after"] = after
    spec.update(overrides)
    return spec


def _submit(api: TestClient, spec: dict) -> dict:
    response = api.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    return response.json()["workflow"]


def _task_of(db, workflow: dict, step_id: str) -> dict:
    (step,) = [s for s in workflow["steps"] if s["step_id"] == step_id]
    return db.docs[f"tasks/{step['task_id']}"]


def _tick(api: TestClient, tenant_id: str = "eng") -> dict:
    response = api.post(f"/v1/admin/merges/wake?tenant_id={tenant_id}", headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _gate(task: dict) -> dict:
    return task["metadata"][prgate.AFTER_PRS_METADATA_KEY]


def _leases(db) -> list[str]:
    return [p for p in db.docs if p.startswith("leases/")]


def _open_pools(db) -> None:
    """A global pool with room: whatever the drain promotes, it admits."""
    seed_pool(db, "global", hard_limit=10)


def _merge(github, number: int = PR) -> None:
    github.pulls[number].update(state="closed", merged=True)


def _later(clock: Clock, seconds: int = issueci.CI_READ_SECONDS) -> None:
    clock.now = clock.now + timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# 1. submission
# --------------------------------------------------------------------------

def test_after_parks_every_root_step_with_the_gate_and_no_lease(db, api):
    workflow = _submit(api, _spec(after={"pr": PR}))

    root = _task_of(db, workflow, "implement")
    assert root["state"] == "PARKED"
    assert root["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert root["depends_on"] == []
    assert _gate(root) == {
        "repository": "octo-org/widget-shop",
        "pull_requests": [PR],
        "merged": [],
        "state": prgate.STATE_WAITING,
    }
    assert root["current_lease_id"] is None
    # A dependant waits on its root as it always did, and carries no gate.
    review = _task_of(db, workflow, "review")
    assert review["state"] == "PARKED"
    assert prgate.AFTER_PRS_METADATA_KEY not in review["metadata"]
    assert _leases(db) == []


def test_a_list_of_pull_requests_is_one_gate_sorted_and_deduplicated(db, api):
    workflow = _submit(api, _spec(after=[{"pr": OTHER_PR}, {"pr": PR}, {"pr": OTHER_PR}]))
    assert _gate(_task_of(db, workflow, "implement"))["pull_requests"] == [PR, OTHER_PR]


def test_without_after_the_root_is_ready_as_before(db, api):
    """The control: the same spec without `after` is not held."""
    workflow = _submit(api, _spec())
    root = _task_of(db, workflow, "implement")
    assert root["state"] == "READY"
    assert prgate.AFTER_PRS_METADATA_KEY not in root["metadata"]


@pytest.mark.parametrize("after", [
    {"pr": 0}, {"pr": -3}, {"pr": "41"}, {"pr": True}, {"number": 41}, {"pr": 41, "repo": "x/y"},
    [], [{"pr": n} for n in range(1, prgate.MAX_AFTER_PRS + 2)],
])
def test_a_malformed_after_is_refused(api, after):
    response = api.post("/v1/workflows", headers=auth_header("alice"), json=_spec(after=after))
    assert response.status_code == 422, response.text


def test_after_without_a_repository_is_refused(api):
    spec = _spec(after={"pr": PR}, strategy="collect")
    spec.pop("repository_url")
    response = api.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    assert "after" in response.json()["message"]


def test_after_on_a_repository_that_is_not_on_github_is_refused(api):
    spec = _spec(after={"pr": PR}, strategy="collect",
                 repository_url="https://gitlab.com/octo-org/widget-shop.git")
    response = api.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    assert "after" in response.json()["message"]


def test_a_caller_cannot_write_the_gate_through_metadata(api):
    spec = _spec(metadata={"merge": "off", prgate.AFTER_PRS_METADATA_KEY: {"state": "merged"}})
    response = api.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    assert prgate.AFTER_PRS_METADATA_KEY in response.text


# --------------------------------------------------------------------------
# 2. parked with no lease while the pull request is open
# --------------------------------------------------------------------------

def test_an_open_pull_request_keeps_the_workflow_parked_with_no_lease(
    db, api, make_scheduler, clock,
):
    _open_pools(db)
    gated = _submit(api, _spec(after={"pr": PR}))
    control = _submit(api, _spec())

    for _ in range(3):
        assert _tick(api)["after_prs"]["waiting"] in (0, 1)
        report = make_scheduler(now=clock).drain()
        _later(clock)

    root = _task_of(db, gated, "implement")
    assert root["state"] == "PARKED"
    assert root["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    assert root["current_lease_id"] is None and root["attempt_count"] == 0
    assert _gate(root)["state"] == prgate.STATE_WAITING
    assert _gate(root)["checked_at"] is not None
    assert report.held_pr_gates >= 1
    # The control proves the drain would have admitted it: the ungated
    # workflow's root left READY in the same drains.
    assert _task_of(db, control, "implement")["state"] not in ("READY", "PARKED")
    held = [p for p in _leases(db) if db.docs[p].get("task_id") == root["id"]]
    assert held == []


def test_the_tick_reads_the_pull_request_with_the_tenants_token_at_most_once_per_interval(
    db, api, github, tenant_tokens, clock,
):
    _submit(api, _spec(after={"pr": PR}))

    body = _tick(api)
    assert body["after_prs"]["visited"] == 1 and body["after_prs"]["waiting"] == 1
    reads = [u for m, u, _, _ in github.calls if m == "GET"]
    assert reads and all("/repos/octo-org/widget-shop/pulls/41" in u for u in reads)
    assert tenant_tokens.asked == ["swarm-tenant-eng-git"]

    _later(clock, issueci.CI_READ_SECONDS - 1)
    assert _tick(api)["after_prs"]["skipped"] == 1
    assert len([u for m, u, _, _ in github.calls if m == "GET"]) == len(reads)


def test_one_merged_of_two_still_waits_and_names_the_other(db, api, github, make_scheduler, clock):
    workflow = _submit(api, _spec(after=[{"pr": PR}, {"pr": OTHER_PR}]))
    _merge(github, PR)

    _tick(api)
    make_scheduler(now=clock).drain()

    root = _task_of(db, workflow, "implement")
    assert root["state"] == "PARKED"
    assert _gate(root)["merged"] == [PR]
    assert _gate(root)["state"] == prgate.STATE_WAITING
    served = api.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice"))
    wait = served.json()["workflow"]["after_pull_requests"]
    assert wait["pending"] == [OTHER_PR]
    assert wait["text"] == f"waiting for PR #{OTHER_PR} to merge"


# --------------------------------------------------------------------------
# 3. released on merge
# --------------------------------------------------------------------------

def test_a_merged_pull_request_releases_the_workflow(db, api, github, make_scheduler, clock):
    seed_pool(db, "global", hard_limit=0)
    workflow = _submit(api, _spec(after={"pr": PR}))
    make_scheduler(now=clock).drain()
    assert _task_of(db, workflow, "implement")["state"] == "PARKED"

    _merge(github)
    body = _tick(api)

    assert body["after_prs"]["released"] == 1, body
    root = _task_of(db, workflow, "implement")
    # The tick marks; it never moves the task (READY is the scheduler's).
    assert root["state"] == "PARKED"
    assert _gate(root)["state"] == prgate.STATE_MERGED
    assert _gate(root)["merged"] == [PR]
    assert _gate(root)["released_at"] == clock.now

    report = make_scheduler(now=clock).drain()

    root = _task_of(db, workflow, "implement")
    assert root["state"] == "READY" and root["park_reason"] is None
    assert report.promoted_dependencies >= 1
    # READY alone: the pool is at 0, so nothing was leased (invariant 1).
    assert root["current_lease_id"] is None and _leases(db) == []
    # The dependant still waits for its parent, not for the pull request.
    assert _task_of(db, workflow, "review")["state"] == "PARKED"


def test_a_released_gate_is_not_read_again(db, api, github, clock):
    _submit(api, _spec(after={"pr": PR}))
    _merge(github)
    _tick(api)
    before = len(github.calls)

    _later(clock)
    body = _tick(api)

    assert body["after_prs"]["visited"] == 0
    assert len(github.calls) == before


# --------------------------------------------------------------------------
# 4. failed on close-unmerged
# --------------------------------------------------------------------------

def test_a_pull_request_closed_unmerged_fails_the_workflow(db, api, github, make_scheduler, clock):
    seed_pool(db, "global", hard_limit=0)
    workflow = _submit(api, _spec(after=[{"pr": PR}, {"pr": OTHER_PR}]))
    github.pulls[OTHER_PR]["state"] = "closed"

    body = _tick(api)
    assert body["after_prs"]["closed"] == 1, body
    assert _gate(_task_of(db, workflow, "implement"))["closed"] == OTHER_PR

    make_scheduler(now=clock).drain()
    make_scheduler(now=clock).drain()

    root = _task_of(db, workflow, "implement")
    assert root["state"] == "DEAD_LETTERED"
    assert f"#{OTHER_PR}" in root["last_error"]
    assert "closed without being merged" in root["last_error"]
    assert root["current_lease_id"] is None and _leases(db) == []
    assert _task_of(db, workflow, "review")["state"] == "CANCELLED"
    served = api.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice"))
    assert served.json()["workflow"]["state"] in ("FAILED", "DEAD_LETTERED")
    wait = served.json()["workflow"]["after_pull_requests"]
    assert wait["state"] == prgate.STATE_CLOSED
    assert wait["text"] == f"PR #{OTHER_PR} was closed without being merged"


# --------------------------------------------------------------------------
# 5. restart-safe
# --------------------------------------------------------------------------

def test_the_gate_survives_a_restart_of_swarm_api_and_the_scheduler(
    db, boot, github, make_scheduler, clock,
):
    """Every step on a fresh API context and a fresh scheduler: nothing about
    the wait lives in a process."""
    seed_pool(db, "global", hard_limit=0)
    workflow = _submit(boot(), _spec(after={"pr": PR}))

    _tick(boot())
    make_scheduler(now=clock).drain()
    assert _task_of(db, workflow, "implement")["state"] == "PARKED"

    _merge(github)
    _later(clock)
    _tick(boot())
    # A scheduler that never saw the wait, started after the tick.
    make_scheduler(now=clock).drain()
    assert _task_of(db, workflow, "implement")["state"] == "READY"

    # A second tick and drain after the release change nothing more.
    _later(clock)
    assert _tick(boot())["after_prs"]["visited"] == 0
    make_scheduler(now=clock).drain()
    assert _task_of(db, workflow, "implement")["state"] == "READY"


def test_a_gate_the_scheduler_cannot_read_holds_rather_than_releases(db, make_scheduler, clock):
    """The document is tenant-writable: an unreadable gate fails closed."""
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_gated", tenant_id="eng", state="PARKED",
                    park_reason=ParkReason.DEPENDENCY_INCOMPLETE.value)
    doc["metadata"] = {prgate.AFTER_PRS_METADATA_KEY: {"state": "maybe"}}

    make_scheduler(now=clock).drain()

    assert db.docs["tasks/task_gated"]["state"] == "PARKED"


# --------------------------------------------------------------------------
# isolation and parity
# --------------------------------------------------------------------------

def test_another_tenants_gate_is_never_read_with_this_tenants_token(
    db, api, github, tenant_tokens,
):
    """Invariant 9: the tick names its tenant, and reads that tenant's only."""
    response = api.post("/v1/workflows", headers=auth_header("bob"), json=_spec(after={"pr": PR}))
    assert response.status_code == 201, response.text

    body = _tick(api, "eng")

    assert body["after_prs"]["visited"] == 0
    assert tenant_tokens.asked == []
    assert [u for m, u, _, _ in github.calls if m == "GET"] == []


def test_the_scheduler_and_reconciler_restate_the_gate_exactly():
    from reconciler import model as reconciler_model

    assert loop_mod.AFTER_PRS_METADATA_KEY == prgate.AFTER_PRS_METADATA_KEY
    assert loop_mod.AFTER_PRS_MERGED == prgate.STATE_MERGED
    assert loop_mod.AFTER_PRS_CLOSED == prgate.STATE_CLOSED
    assert reconciler_model.AFTER_PRS_METADATA_KEY == prgate.AFTER_PRS_METADATA_KEY
    assert reconciler_model.AFTER_PRS_MERGED == prgate.STATE_MERGED
