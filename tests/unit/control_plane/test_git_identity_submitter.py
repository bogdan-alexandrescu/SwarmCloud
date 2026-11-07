"""swarm-api records who an agent's commits name: the submitter's GitHub account (P37).

Owner decision 2026-10-06: commits name the PERSON WHO DISPATCHED the task or
workflow. Owner hard rule 2026-11-07: no commit carries an email address that
identifies a person or a company (GitHub refused pushes exposing one, GH007),
and agent commits still use the person's GitHub username. So, inside the
signed `metadata.dispatch` block, `git_identity` is

  * for a person whose GitHub account is connected in the workspace (the
    `forge_connections` record the console shows as "Connected as @login"):
    `{"name": <login>, "email": "<id>+<login>@users.noreply.github.com"}`;
  * for anyone else: absent, which the worker reads as the bot
    `SwarmCloud <swarmcloud@users.noreply.github.com>`;
  * on a workflow's steps, the workflow's submitter's;
  * on a continuation a service account submitted, the continued task's;
  * never from a request body: a caller's own `metadata.git_identity` is read
    by nothing, `metadata.dispatch` is refused (reserved), and a top-level
    identity field is refused by the schema.

`gitidentity.from_task` is the reading the worker restates
(tests/unit/worker/test_git_identity_env.py holds the two to one shape).

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
from swarm_api.forgeapp import CONNECTIONS, connection_id_for
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header

ALICE = "alice@example.com"
REPO = "https://github.com/example-org/example"
FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000001"
FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}
LOGIN = "octo-alice"
GITHUB_ID = 583231
ALICE_GITHUB = {"name": LOGIN, "email": f"{GITHUB_ID}+{LOGIN}@users.noreply.github.com"}
BOT = {"name": "SwarmCloud", "email": "swarmcloud@users.noreply.github.com"}


def _identity(db, task_id: str) -> dict:
    """Who the task's commits name, read the way the worker reads it."""
    doc = db.docs[f"tasks/{task_id}"]
    found = gitidentity.from_task(doc["metadata"], doc["submitted_by"])
    return found if found is not None else dict(gitidentity.BOT_IDENTITY)


def _record(db, task_id: str) -> dict | None:
    return db.docs[f"tasks/{task_id}"]["metadata"]["dispatch"].get("git_identity")


def _connect(db, tenant_id: str = "eng", email: str = ALICE, *, login: str = LOGIN,
             user_id=GITHUB_ID, state: str = "active") -> None:
    """The record `ForgeApp._store` writes when a person connects GitHub."""
    conn_id = connection_id_for(tenant_id, email)
    db.docs[f"{CONNECTIONS}/{conn_id}"] = {
        "connection_id": conn_id, "tenant_id": tenant_id, "user": email.lower(),
        "forge": "github", "method": "app_user", "forge_login": login,
        "forge_user_id": user_id, "state": state,
    }


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

def test_a_person_with_a_connected_github_account_commits_as_their_login(client, db):
    _connect(db)
    task_id = _submit_task(client)
    doc = db.docs[f"tasks/{task_id}"]
    assert doc["submitted_by"] == ALICE and doc["tenant_id"] == "eng"
    assert _record(db, task_id) == ALICE_GITHUB
    assert _identity(db, task_id) == ALICE_GITHUB


def test_a_person_without_a_connected_github_account_commits_as_the_bot(client, db):
    task_id = _submit_task(client)
    assert db.docs[f"tasks/{task_id}"]["submitted_by"] == ALICE
    # Nothing recorded: the worker's reading of no record is the bot.
    assert _record(db, task_id) is None
    assert _identity(db, task_id) == BOT


@pytest.mark.parametrize("change", [
    {"state": "revoked"},
    {"login": "not a login"},
    {"login": "-alice"},
    {"login": ""},
    {"user_id": None},
    {"user_id": "583231"},
    {"user_id": 0},
    {"user_id": True},
])
def test_a_revoked_or_malformed_connection_commits_as_the_bot(client, db, change):
    _connect(db, **change)
    task_id = _submit_task(client)
    assert _record(db, task_id) is None
    assert _identity(db, task_id) == BOT


def test_another_workspaces_or_another_persons_connection_is_not_used(client, db):
    _connect(db, tenant_id="research")
    _connect(db, email="bob@example.com", login="octo-bob", user_id=9)
    task_id = _submit_task(client)
    assert _identity(db, task_id) == BOT


def test_the_tokens_name_claim_never_reaches_a_commit(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-alice"] = {**tokens["token-alice"], "name": "Alice Example"}
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
    )
    client = TestClient(create_app(context), raise_server_exceptions=False)
    task_id = _submit_task(client)
    # Before 2026-11-07 this recorded {"Alice Example", alice@example.com}.
    assert _record(db, task_id) is None
    assert _identity(db, task_id) == BOT
    _connect(db)
    task_id = _submit_task(client)
    assert _record(db, task_id) == ALICE_GITHUB
    # Every step of her workflow carries her GitHub account too.
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
        assert _record(db, step["task_id"]) == ALICE_GITHUB


def test_a_workflow_step_inherits_the_workflows_submitter(client, db):
    _connect(db)
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
        assert _identity(db, step["task_id"]) == ALICE_GITHUB


# ---------------------------------------------------------------------------
# A request body naming an identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forged", [
    {"name": "Mallory", "email": "mallory@example.com"},
    {"name": "mallory", "email": "1+mallory@users.noreply.github.com"},
])
def test_metadata_naming_an_identity_is_ignored(client, db, forged):
    task_id = _submit_task(client, metadata={"git_identity": forged})
    assert _identity(db, task_id) == BOT
    _connect(db)
    task_id = _submit_task(client, metadata={"git_identity": forged})
    assert _identity(db, task_id) == ALICE_GITHUB


def test_a_workflows_metadata_naming_an_identity_is_ignored(client, db):
    response = client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json={
            "steps": [{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"},
                       "metadata": {"git_identity": {"name": "M", "email": "m@example.com"}}}],
            "metadata": {"git_identity": {"name": "Mallory", "email": "mallory@example.com"}},
        },
    )
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    assert _identity(db, step["task_id"]) == BOT
    assert _record(db, step["task_id"]) is None


def test_a_dispatch_block_naming_an_identity_is_refused(client, db):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hi"},
              "metadata": {"dispatch": {"git_identity": {
                  "name": "m", "email": "1+m@users.noreply.github.com"}}}},
    )
    assert response.status_code == 422, response.text
    assert not [key for key in db.docs if key.startswith("tasks/")]


@pytest.mark.parametrize("field", ["submitted_by", "git_identity", "author_email"])
def test_a_top_level_identity_field_writes_nothing(client, db, field):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hi"}, field: "mallory@example.com"},
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
    _connect(db)
    original = _submit_task(client, strategy="direct-pr", repository_url=REPO)
    fix = _continue(fixer_client, original)
    assert db.docs[f"tasks/{fix}"]["submitted_by"] == FIXER
    assert _record(db, fix) == ALICE_GITHUB
    assert _identity(db, fix) == ALICE_GITHUB
    # And a continuation of that continuation still names her.
    again = _continue(fixer_client, fix)
    assert _identity(db, again) == ALICE_GITHUB


def test_a_service_account_continuation_of_a_task_naming_nobody_commits_as_the_bot(
    client, fixer_client, db
):
    original = _submit_task(client, strategy="direct-pr", repository_url=REPO)
    # A task the service account itself submitted, recording no person.
    doc = db.docs[f"tasks/{original}"]
    doc["submitted_by"] = FIXER
    assert "git_identity" not in doc["metadata"]["dispatch"]
    fix = _continue(fixer_client, original)
    assert _identity(db, fix) == BOT


def test_a_continuation_of_a_task_recording_a_personal_address_commits_as_the_bot(
    client, fixer_client, db
):
    """A record written before 2026-11-07 named the submitter's own address."""
    original = _submit_task(client, strategy="direct-pr", repository_url=REPO)
    db.docs[f"tasks/{original}"]["metadata"]["dispatch"]["git_identity"] = {
        "name": "Alice Example", "email": ALICE,
    }
    fix = _continue(fixer_client, original)
    assert _record(db, fix) is None
    assert _identity(db, fix) == BOT


# ---------------------------------------------------------------------------
# The rules, as functions
# ---------------------------------------------------------------------------

def test_a_child_inherits_its_parents_github_account():
    parent_metadata = {"dispatch": {"git_identity": dict(ALICE_GITHUB)}}
    assert gitidentity.from_task(parent_metadata, ALICE) == ALICE_GITHUB
    assert gitidentity.from_task(parent_metadata, FIXER) == ALICE_GITHUB
    # A record naming an address of a person's own is never used.
    assert gitidentity.from_task(
        {"dispatch": {"git_identity": {"name": "Alice", "email": ALICE}}}, ALICE
    ) is None
    assert gitidentity.from_task({}, ALICE) is None
    assert gitidentity.from_task({}, FIXER) is None


def test_no_caller_is_named_by_an_address_of_their_own():
    assert gitidentity.for_caller(FIXER) == gitidentity.BOT_IDENTITY
    assert gitidentity.for_caller(ALICE) == gitidentity.BOT_IDENTITY
    assert gitidentity.for_caller(ALICE, ALICE_GITHUB) == ALICE_GITHUB
    # A service account is never a person, whatever it is handed.
    assert gitidentity.for_caller(FIXER, ALICE_GITHUB) == gitidentity.BOT_IDENTITY
    assert gitidentity.for_caller(ALICE, {"name": "Alice", "email": ALICE}) == gitidentity.BOT_IDENTITY


@pytest.mark.parametrize("login,user_id,expected", [
    ("octo-alice", 583231, ALICE_GITHUB),
    ("Octo-Alice", 583231, {"name": "Octo-Alice",
                            "email": "583231+Octo-Alice@users.noreply.github.com"}),
    ("a" * 39, 1, {"name": "a" * 39, "email": f"1+{'a' * 39}@users.noreply.github.com"}),
    ("a" * 40, 1, None),
    ("alice@example.com", 1, None),
    ("al ice", 1, None),
    ("octo-alice", -1, None),
    ("octo-alice", 1.5, None),
    (None, 1, None),
])
def test_a_github_identity_is_built_from_a_login_and_a_numeric_id_only(login, user_id, expected):
    assert gitidentity.github_identity(login, user_id) == expected



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
