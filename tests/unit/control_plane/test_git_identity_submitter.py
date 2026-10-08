"""swarm-api records who an agent's commits name, from the authenticated submitter (P37).

Owner decision 2026-10-06: commits carry the name and email of the PERSON WHO
DISPATCHED the task or workflow. Who that is, per task, is the signed
`submitted_by` plus -- inside the signed `metadata.dispatch` block, and only
when there is more to say than that address -- `git_identity = {"name",
"email"}`. `gitidentity.from_task` is the reading the worker restates
(tests/unit/worker/test_git_identity_env.py holds the two to one shape):

  * from the verified token -- its `name` claim, else the email's local part;
  * on a workflow's steps, the workflow's submitter;
  * on a continuation a service account submitted, the person the continued
    task names, else the bot `SwarmCloud <swarmcloud@users.noreply.github.com>`;
  * never from a request body: a caller's own `metadata.git_identity` is read
    by nothing, `metadata.dispatch` is refused (reserved), and a top-level
    identity field is refused by the schema.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_common.identity import TenantMember

from swarm_api import gitidentity
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header

ALICE = "alice@saga.xyz"
REPO = "https://github.com/saga-xyz/example"
FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000001"
FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}


def _identity(db, task_id: str) -> dict:
    """Who the task's commits name, read the way the worker reads it."""
    doc = db.docs[f"tasks/{task_id}"]
    found = gitidentity.from_task(doc["metadata"], doc["submitted_by"])
    return found if found is not None else dict(gitidentity.BOT_IDENTITY)


def _record(db, task_id: str) -> dict | None:
    return db.docs[f"tasks/{task_id}"]["metadata"]["dispatch"].get("git_identity")


def _submit_task(client, user: str = "alice", **body) -> str:
    response = client.post(
        "/v1/tasks", headers=auth_header(user),
        json={"runner_profile": "mock", "input": {"prompt": "hi"}, **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


# ---------------------------------------------------------------------------
# A person's submission
# ---------------------------------------------------------------------------

def test_a_task_submitted_by_a_person_records_that_person(client, db):
    task_id = _submit_task(client)
    assert db.docs[f"tasks/{task_id}"]["submitted_by"] == ALICE
    # The fixture's token carries no `name` claim, as an IAP assertion never
    # does, so the signed submitter says it all and the block is unchanged.
    assert _identity(db, task_id) == {"name": "alice", "email": ALICE}
    assert _record(db, task_id) is None


def test_the_tokens_name_claim_is_the_commit_name(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-alice"] = {**tokens["token-alice"], "name": "Alice Example"}
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
    )
    client = TestClient(create_app(context), raise_server_exceptions=False)
    task_id = _submit_task(client)
    assert _record(db, task_id) == {"name": "Alice Example", "email": ALICE}
    assert _identity(db, task_id) == {"name": "Alice Example", "email": ALICE}
    # Every step of her workflow carries the name too.
    response = client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={"steps": [
            {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}},
            {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "b"},
             "depends_on": ["a"]},
        ]},
    )
    assert response.status_code == 201, response.text
    steps = response.json()["workflow"]["steps"]
    assert len(steps) == 2
    for step in steps:
        assert _record(db, step["task_id"]) == {"name": "Alice Example", "email": ALICE}


def test_a_workflow_step_inherits_the_workflows_submitter(client, db):
    response = client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}},
                {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "b"},
                 "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    steps = response.json()["workflow"]["steps"]
    assert len(steps) == 2
    for step in steps:
        assert _identity(db, step["task_id"]) == {"name": "alice", "email": ALICE}


# ---------------------------------------------------------------------------
# A request body naming an identity
# ---------------------------------------------------------------------------

def test_metadata_naming_an_identity_is_ignored(client, db):
    task_id = _submit_task(
        client, metadata={"git_identity": {"name": "Mallory", "email": "mallory@saga.xyz"}},
    )
    assert _identity(db, task_id) == {"name": "alice", "email": ALICE}


def test_a_workflows_metadata_naming_an_identity_is_ignored(client, db):
    response = client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={
            "steps": [{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"},
                       "metadata": {"git_identity": {"name": "M", "email": "m@saga.xyz"}}}],
            "metadata": {"git_identity": {"name": "Mallory", "email": "mallory@saga.xyz"}},
        },
    )
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    assert _identity(db, step["task_id"]) == {"name": "alice", "email": ALICE}
    assert _record(db, step["task_id"]) is None


def test_a_dispatch_block_naming_an_identity_is_refused(client, db):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hi"},
              "metadata": {"dispatch": {"git_identity": {"name": "M", "email": "m@saga.xyz"}}}},
    )
    assert response.status_code == 422, response.text
    assert not [key for key in db.docs if key.startswith("tasks/")]


@pytest.mark.parametrize("field", ["submitted_by", "git_identity", "author_email"])
def test_a_top_level_identity_field_writes_nothing(client, db, field):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hi"}, field: "mallory@saga.xyz"},
    )
    assert response.status_code == 422, response.text
    assert not [key for key in db.docs if key.startswith("tasks/")]


# ---------------------------------------------------------------------------
# A service account's continuation
# ---------------------------------------------------------------------------

@pytest.fixture
def fixer_client(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-fixer"] = {"email": FIXER, "sub": FIXER_UID, "email_verified": True}
    listing = TenantMember(email=FIXER, kind="group", principal=ENG_GROUP, uid=FIXER_UID)
    context = build_context(
        settings=api_settings(tenant_service_accounts=(listing,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _continue(fixer_client, continues: str):
    response = fixer_client.post(
        "/v1/workflows", headers=FIXER_HEADERS,
        json={
            "steps": [{"step_id": "ci-fix", "runner_profile": "mock",
                       "input": {"prompt": "fix the failing test"}}],
            "strategy": "direct-pr",
            "continues_task": continues,
        },
    )
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    return step["task_id"]


def test_a_service_account_continuation_commits_as_the_original_submitter(
    client, fixer_client, db
):
    original = _submit_task(client, strategy="direct-pr", repository_url=REPO)
    fix = _continue(fixer_client, original)
    assert db.docs[f"tasks/{fix}"]["submitted_by"] == FIXER
    assert _record(db, fix) == {"name": "alice", "email": ALICE}
    assert _identity(db, fix) == {"name": "alice", "email": ALICE}
    # And a continuation of that continuation still names her.
    again = _continue(fixer_client, fix)
    assert _identity(db, again) == {"name": "alice", "email": ALICE}


def test_a_service_account_continuation_of_a_task_naming_nobody_commits_as_the_bot(
    client, fixer_client, db
):
    original = _submit_task(client, strategy="direct-pr", repository_url=REPO)
    # A task the service account itself submitted, recording no person.
    doc = db.docs[f"tasks/{original}"]
    doc["submitted_by"] = FIXER
    assert "git_identity" not in doc["metadata"]["dispatch"]
    fix = _continue(fixer_client, original)
    assert _identity(db, fix) == {
        "name": "SwarmCloud", "email": "swarmcloud@users.noreply.github.com",
    }


# ---------------------------------------------------------------------------
# The rules, as functions
# ---------------------------------------------------------------------------

def test_a_child_inherits_its_parents_person():
    parent_metadata = {"dispatch": {"git_identity": {"name": "Alice Example", "email": ALICE}}}
    assert gitidentity.from_task(parent_metadata, ALICE) == {
        "name": "Alice Example", "email": ALICE,
    }
    # A record naming someone else than a human submitter is not trusted.
    assert gitidentity.from_task(
        {"dispatch": {"git_identity": {"name": "M", "email": "m@saga.xyz"}}}, ALICE
    ) == {"name": "alice", "email": ALICE}
    assert gitidentity.from_task({}, FIXER) is None


def test_a_service_account_caller_is_never_named():
    assert gitidentity.for_caller(FIXER) == gitidentity.BOT_IDENTITY
    assert gitidentity.for_caller(ALICE, "Alice <x>\n") == {"name": "Alice x", "email": ALICE}



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
