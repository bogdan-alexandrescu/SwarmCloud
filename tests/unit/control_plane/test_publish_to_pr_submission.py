"""`publish_to_pr: <N>` on a workflow spec or a single task (#2183).

Owner approval, BGD, 2026-10-24: a follow-up lane lands its commits on an
existing pull request's branch instead of opening a replacement. swarm-api
checks the spec at submission, before anything is written, and signs what it
checked into every step's dispatch block as
`publish_to_pr: {"number": N, "task_id": <the task whose swarm/ branch is
the head>}`; the worker reads the pull request again before it starts and
before it pushes (tests/unit/worker/test_publish_to_pr.py).

Refused here, each 422 `invalid_dispatch` with nothing written: a value that
is not a positive integer, a pull request of another repository, a closed or
merged pull request, one from a fork, one whose head is not a `swarm/task_*`
branch of a task of the submitting tenant, and the shapes that cannot
publish onto one branch (two `direct-pr` agent steps, `collect`, a
`continues_task`, a `repository_ref`, a merge step).

GitHub is `forge_fakes.GitHubWrites` under the shipped `GitHubWriter`; the
tenant's token is built at runtime by `forge_fakes.AnyTenantTokens`.

MUTATIONS: drop `parse_number`'s repository comparison -- the other-repository
test is accepted. Drop the tenant read in `publishtopr.resolve` -- the
research tenant's branch is accepted. Leave the field out of
`DispatchOptions.to_metadata` -- the accepted specs' blocks lack it.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import DispatchOptions
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_task

OWNER, NAME = "octo-org", "widget-shop"
REPO = f"https://github.com/{OWNER}/{NAME}"
NUMBER = 2164
HEAD = "c" * 40
LANE_TASK = "task_" + secrets.token_hex(10)


@pytest.fixture
def writes():
    fake = forge_fakes.GitHubWrites()
    fake.pulls[NUMBER] = _pull()
    return fake


@pytest.fixture
def api_context(db, tokens, group_map, objects, writes):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge_writer=forgewrite.GitHubWriter(send=writes),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture
def lane(db):
    """The task whose lane opened pull request #NUMBER, in alice's tenant."""
    seed_task(db, task_id=LANE_TASK, tenant_id="eng", state="SUCCEEDED")
    return LANE_TASK


def _pull(**overrides) -> dict:
    pull = {
        "number": NUMBER, "html_url": f"{REPO}/pull/{NUMBER}", "title": "Mock-up v1",
        "state": "open", "merged": False, "body": "",
        "head": {"sha": HEAD, "ref": f"swarm/{LANE_TASK}",
                 "repo": {"full_name": f"{OWNER}/{NAME}"}},
        "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{NAME}"}},
    }
    pull.update(overrides)
    return pull


def _workflow(**overrides) -> dict:
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "publish_to_pr": NUMBER,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "apply the owner's answers"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input": {"prompt": "fix what is left"}},
        ],
    }
    spec.update(overrides)
    return spec


def _task(**overrides) -> dict:
    spec = {"runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO,
            "publish_to_pr": NUMBER, "input": {"prompt": "re-review"}}
    spec.update(overrides)
    return spec


def _tasks(db) -> list[str]:
    return [key for key in db.docs if key.startswith("tasks/") and LANE_TASK not in key]


def _blocks(client, db, spec) -> list[dict]:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 201, response.text
    return [db.docs[f"tasks/{step['task_id']}"]["metadata"]["dispatch"]
            for step in response.json()["workflow"]["steps"]]


def _refused(client, db, path, spec, *phrases) -> dict:
    response = client.post(path, headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    body = response.json()
    for phrase in phrases:
        assert phrase in body["message"], body["message"]
    assert _tasks(db) == [], "a refused publish_to_pr wrote a task"
    return body


# -- accepted: the controls every refusal is measured against ---------------


def test_every_step_of_an_integrate_lane_carries_the_checked_pull_request(client, db, lane):
    blocks = _blocks(client, db, _workflow())
    assert [b["publish_to_pr"] for b in blocks] == [{"number": NUMBER, "task_id": lane}] * 2
    # The fix step still builds on the implementer, and is the one integrator.
    fix = blocks[1]
    assert fix["role"] == "integrator" and fix["builds_on"] == blocks[1]["integrates"][0]


def test_a_reference_to_the_specs_own_repository_is_its_number(client, db, lane):
    for ref in (f"{OWNER}/{NAME}#{NUMBER}", f"{REPO}/pull/{NUMBER}"):
        blocks = _blocks(client, db, _workflow(publish_to_pr=ref))
        assert blocks[0]["publish_to_pr"] == {"number": NUMBER, "task_id": lane}


def test_a_single_task_carries_it(client, db, lane):
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]
    block = db.docs[f"tasks/{task_id}"]["metadata"]["dispatch"]
    assert block["publish_to_pr"] == {"number": NUMBER, "task_id": lane}


def test_without_the_field_the_block_is_the_one_stored_before(client, db, lane, writes):
    blocks = _blocks(client, db, _workflow(publish_to_pr=None))
    assert all("publish_to_pr" not in b for b in blocks)
    assert "publish_to_pr" not in DispatchOptions().to_metadata()


# -- refused: the value -------------------------------------------------------


@pytest.mark.parametrize("bad", ["two thousand", "12abc", 0, -4, 3.5, True, [NUMBER]])
def test_a_value_that_is_not_a_positive_integer_is_refused(client, db, lane, bad):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_workflow(publish_to_pr=bad))
    assert response.status_code == 422, response.text
    assert _tasks(db) == []


@pytest.mark.parametrize("ref", [f"someone-else/{NAME}#{NUMBER}",
                                 f"https://github.com/{OWNER}/other-repo/pull/{NUMBER}"])
def test_a_pull_request_of_another_repository_is_refused(client, db, lane, ref):
    _refused(client, db, "/v1/workflows", _workflow(publish_to_pr=ref),
             "not this spec's repository")


# -- refused: the pull request, read from GitHub ----------------------------


@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"state": "closed"}, "is closed"),
        ({"state": "closed", "merged": True}, "already merged"),
        ({"head": {"sha": HEAD, "ref": f"swarm/{LANE_TASK}",
                   "repo": {"full_name": f"someone/{NAME}"}}}, "comes from a fork"),
        ({"head": {"sha": HEAD, "ref": "feature/by-hand",
                   "repo": {"full_name": f"{OWNER}/{NAME}"}}}, "a lane of your tenant"),
    ],
    ids=["closed", "merged", "fork", "not-a-swarm-branch"],
)
def test_a_pull_request_a_lane_may_not_publish_onto_is_refused(
    client, db, lane, writes, overrides, words
):
    writes.pulls[NUMBER] = _pull(**overrides)
    _refused(client, db, "/v1/workflows", _workflow(), words)
    _refused(client, db, "/v1/tasks", _task(), words)


def test_another_tenants_lane_branch_is_refused_as_not_this_tenants(client, db, writes):
    seed_task(db, task_id=LANE_TASK, tenant_id="research", state="SUCCEEDED")
    _refused(client, db, "/v1/workflows", _workflow(), "a lane of your tenant")


def test_a_pull_request_that_does_not_exist_is_refused(client, db, lane, writes):
    _refused(client, db, "/v1/workflows", _workflow(publish_to_pr=NUMBER + 1),
             "is not a pull request")


# -- refused: shapes that cannot publish onto one branch -------------------


def test_two_direct_pr_steps_are_refused(client, db, lane):
    _refused(client, db, "/v1/workflows", _workflow(strategy="direct-pr"), "Send one step")


def test_collect_is_refused(client, db, lane):
    _refused(client, db, "/v1/workflows", _workflow(strategy="collect"), "'collect'")
    _refused(client, db, "/v1/tasks", _task(strategy="collect"), "'collect'")


def test_a_repository_ref_is_refused(client, db, lane):
    _refused(client, db, "/v1/workflows", _workflow(repository_ref="main"), "repository_ref")


def test_continues_task_beside_it_is_refused(client, db, lane):
    _refused(client, db, "/v1/workflows",
             _workflow(strategy="direct-pr", continues_task=lane, repository_url=None,
                       steps=[{"step_id": "fix", "runner_profile": "mock"}]),
             "two pull requests")


def test_a_merge_step_beside_it_is_refused(client, db, lane):
    spec = _workflow()
    spec["steps"].append({"step_id": "merge", "runner_profile": "merge", "depends_on": ["fix"]})
    _refused(client, db, "/v1/workflows", spec, "Remove the merge step")


def test_metadata_merge_on_is_refused_too(client, db, lane):
    """`metadata.merge: "on"` appends a merge step at submission; refused the same."""
    _refused(client, db, "/v1/workflows", _workflow(metadata={"merge": "on"}),
             "Remove the merge step")


def test_a_platform_merge_default_does_not_apply_to_it(client, db, lane):
    """`merge_by_default` appends a merge step to a workflow that OPENS a
    pull request; this one opens none, so none is appended and nothing refused."""
    db.collection("control").document("settings").set({"merge_by_default": True})
    blocks = _blocks(client, db, _workflow())
    assert len(blocks) == 2


def test_a_spec_with_no_repository_is_refused(client, db, lane):
    _refused(client, db, "/v1/tasks", _task(repository_url=None), "repository")
