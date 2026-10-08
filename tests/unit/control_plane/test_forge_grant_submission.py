"""Submission resolves whose GitHub credential a task runs with (#780 lane OB7).

docs/onboarding.md §3.3 step 1 and decision D4, as the owner decided it on
2026-10-07: a PERSON's task on a GitHub repository runs with their own user
slot (`git-u-<hex>`) and their grant's mode, and is refused 403
REPOSITORY_NOT_GRANTED when they hold no grant; a SERVICE ACCOUNT's
submission runs with the tenant token (`git`, write); a task with no
repository carries neither field. Both fields are set before the spec is
signed, so a rewrite of either fails verification (contract request 54), and
neither is ever accepted from a request body (invariant 10).

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups, a local P-256
signer. No credentials, no emulator, no network.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import ValidationFailed
from swarm_api.gittokens import Scope, provider_suffix
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.schemas import TaskCreate
from swarm_api.validation import (
    REPOSITORY_NOT_GRANTED,
    github_repository,
    is_service_submitter,
    repository_not_granted,
)
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal, tenant_id_for_user

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_grant
from .spec_signer import LocalSpecSigner

REPO = "saga-xyz/widgets"
REPO_URL = f"https://github.com/{REPO}.git"
ALICE = "alice@saga.xyz"
SERVICE = f"swarm-acceptance@{PROJECT}.iam.gserviceaccount.com"


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def context(db, tokens, group_map, signer):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )


@pytest.fixture
def client(context) -> TestClient:
    return TestClient(create_app(context), raise_server_exceptions=False)


def _tasks(db) -> dict[str, dict[str, Any]]:
    return {
        key.split("/", 1)[1]: doc
        for key, doc in db.docs.items()
        if key.startswith("tasks/") and key.count("/") == 1
    }


def _task(repository_url: str | None = REPO_URL) -> dict[str, Any]:
    body: dict[str, Any] = {"runner_profile": "mock", "input": {"prompt": "hello"}}
    if repository_url is not None:
        body["repository_url"] = repository_url
    return body


def _workflow(repository_url: str = REPO_URL) -> dict[str, Any]:
    return {
        "on_step_failure": "continue",
        "strategy": "direct-pr",
        "repository_url": repository_url,
        "steps": [
            {"step_id": "impl", "runner_profile": "mock", "input": {"prompt": "implement"}},
            {"step_id": "docs", "runner_profile": "mock", "input": {"prompt": "document"},
             "depends_on": ["impl"]},
        ],
    }


def _service_auth() -> AuthContext:
    """A service account in `eng`, built the way the platform's own submitters
    build a context (`mergewake._submitter`): the release's acceptance suite
    and tenant automation submit as one."""
    return AuthContext(
        principal=Principal(email=SERVICE, subject="sa-1", domain="saga.xyz", groups=()),
        tenant_id="eng",
        is_admin=False,
        tenant_principal=ENG_GROUP,
    )


def _person_auth() -> AuthContext:
    return AuthContext(
        principal=Principal(email=ALICE, subject="sub-alice", domain="saga.xyz", groups=()),
        tenant_id="eng",
        is_admin=False,
        tenant_principal=ENG_GROUP,
    )


# ---------------------------------------------------------------------------
# A person with a grant runs with their own credential and their grant's mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["write", "read"])
def test_a_granted_person_runs_with_their_slot_and_their_grants_mode(client, db, signer, mode):
    seed_grant(db, "eng", ALICE, REPO, mode=mode)
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    assert doc["forge_credential"] == provider_suffix(Scope.USER, user=ALICE)
    assert doc["forge_credential"].startswith("git-u-")
    assert doc["forge_access"] == mode
    # Format 3 covers both fields, and the stored document verifies.
    assert doc["spec_format"] == 3
    assert signer.verifies(doc, task_id)
    served = response.json()["task"]
    assert served["forge_credential"] == doc["forge_credential"]
    assert served["forge_access"] == mode
    assert served["forge_credential_source"] == "the submitter's GitHub credential"


def test_the_grant_is_found_whatever_case_or_url_form_the_repository_is_written_in(client, db):
    seed_grant(db, "eng", ALICE, REPO)
    for url in ("https://github.com/Saga-XYZ/Widgets", "git@github.com:saga-xyz/widgets.git",
                "https://www.github.com/saga-xyz/widgets/"):
        response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task(url))
        assert response.status_code == 201, (url, response.text)
        assert response.json()["task"]["forge_access"] == "write"


def test_another_persons_grant_is_not_yours(client, db):
    # root is in eng too, and holds the grant; alice does not.
    seed_grant(db, "eng", "root@saga.xyz", REPO)
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 403, response.text
    assert response.json()["code"] == REPOSITORY_NOT_GRANTED


def test_a_grant_in_another_tenant_is_not_a_grant_in_this_one(client, db):
    # bob is in research; his grant there does not reach eng's alice, and
    # alice's own grant in her personal tenant is not one in eng.
    seed_grant(db, "research", "bob@saga.xyz", REPO)
    seed_grant(db, tenant_id_for_user(ALICE), ALICE, REPO)
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 403, response.text


def test_a_grant_with_no_known_mode_refuses_rather_than_writes(client, db):
    seed_grant(db, "eng", ALICE, REPO, mode="admin")
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 403, response.text
    assert not _tasks(db)


# ---------------------------------------------------------------------------
# A person without one is refused, with the exact code and words
# ---------------------------------------------------------------------------


def test_an_ungranted_person_is_refused_403_naming_the_repository(client, db, signer):
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "REPOSITORY_NOT_GRANTED"
    assert body["message"] == (
        "repository saga-xyz/widgets is not granted to you: choose it under Access"
    )
    assert body["detail"]["repository"] == REPO
    # Nothing stored, nothing signed.
    assert not _tasks(db)
    assert signer.signed == []


def test_one_ungranted_task_refuses_the_whole_batch(client, db):
    seed_grant(db, "eng", ALICE, REPO)
    batch = {"tasks": [_task(), _task("https://github.com/saga-xyz/payments")]}
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"), json=batch)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["repositories"] == ["saga-xyz/payments"]
    assert not _tasks(db)


def test_a_github_url_with_a_port_is_still_github_and_still_refused(client, db):
    # Read as "not GitHub" it would carry no credential, and the worker would
    # read the tenant token D4 withholds from a person.
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json=_task("https://github.com:443/saga-xyz/widgets"))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["repository"] == REPO


# ---------------------------------------------------------------------------
# A workflow: every step, and one refusal naming each
# ---------------------------------------------------------------------------


def test_a_granted_workflow_resolves_every_step_and_signs_it(client, db, signer):
    seed_grant(db, "eng", ALICE, REPO, mode="read")
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 2
    for task_id, doc in tasks.items():
        assert doc["forge_credential"] == provider_suffix(Scope.USER, user=ALICE)
        assert doc["forge_access"] == "read"
        assert signer.verifies(doc, task_id), task_id


def test_a_workflow_on_an_ungranted_repository_is_refused_whole_naming_each_step(client, db,
                                                                                 signer):
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == REPOSITORY_NOT_GRANTED
    assert body["detail"]["repository"] == REPO
    assert body["detail"]["steps"] == [
        {"step_id": "impl", "repository": REPO},
        {"step_id": "docs", "repository": REPO},
    ]
    assert not _tasks(db)
    assert not [key for key in db.docs if key.startswith("workflows/")]
    assert signer.signed == []


def test_the_refusal_names_each_ungranted_step_and_every_repository():
    error = repository_not_granted([("a", "o/one"), ("b", "o/two"), ("c", "o/one")])
    assert error.status_code == 403
    assert error.code == REPOSITORY_NOT_GRANTED
    assert error.message == (
        "repositories o/one, o/two are not granted to you: choose them under Access"
    )
    assert error.detail["repositories"] == ["o/one", "o/two"]
    assert [s["step_id"] for s in error.detail["steps"]] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# A service account uses the tenant token; no repository is untouched
# ---------------------------------------------------------------------------


def test_a_service_account_runs_with_the_tenant_token_and_write(context, db, signer):
    result = context.submissions.submit_tasks(_service_auth(), [TaskCreate(**_task())])
    task = result.tasks[0]
    assert task.forge_credential == "git"
    assert task.forge_access == "write"
    doc = _tasks(db)[task.id]
    assert doc["forge_credential"] == "git"
    assert doc["spec_format"] == 3
    assert signer.verifies(doc, task.id)


def test_a_repository_index_run_is_a_service_submission(context, db):
    # `repoindex` submits as the registration's creator, who need hold no
    # grant: the owner's D4 for automation runs indexing on the tenant token.
    result = context.submissions.submit_tasks(
        _person_auth(), [TaskCreate(**_task())], service_submission=True)
    assert result.tasks[0].forge_credential == "git"
    assert result.tasks[0].forge_access == "write"


def test_a_service_account_is_told_apart_from_a_person():
    assert is_service_submitter(SERVICE)
    assert is_service_submitter("123-compute@developer.gserviceaccount.com")
    assert not is_service_submitter(ALICE)
    assert not is_service_submitter("gserviceaccount.com@saga.xyz")
    assert not is_service_submitter(None)


def test_a_task_with_no_repository_carries_no_forge_credential(client, db, signer):
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task(None))
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    assert doc.get("forge_credential") is None
    assert doc.get("forge_access") is None
    # Untouched: signed at format 1, exactly as before OB7.
    assert doc["spec_format"] == 1
    assert signer.verifies(doc, task_id)
    assert response.json()["task"]["forge_credential_source"] is None


def test_a_repository_not_on_github_carries_no_forge_credential(client, db):
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json=_task("https://gitlab.com/saga-xyz/widgets.git"))
    assert response.status_code == 201, response.text
    (_, doc), = _tasks(db).items()
    assert doc.get("forge_credential") is None


def test_github_repository_reads_the_pair_and_refuses_a_github_url_naming_none():
    assert github_repository("https://github.com/o/r.git") == ("o", "r")
    assert github_repository("git@github.com:o/r.git") == ("o", "r")
    assert github_repository("https://github.com./o/r") == ("o", "r")
    assert github_repository("https://gitlab.com/o/r") is None
    assert github_repository(None) is None
    with pytest.raises(ValidationFailed):
        github_repository("https://github.com/o/r/tree/main")
    with pytest.raises(ValidationFailed):
        github_repository("https://github.com/o")


# ---------------------------------------------------------------------------
# The signature covers both fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,forged", [
    ("forge_access", "write"),
    ("forge_credential", "git"),
    ("forge_credential", "git-u-" + "0" * 16),
    ("forge_access", None),
])
def test_a_rewritten_forge_field_fails_verification(client, db, signer, field, forged):
    seed_grant(db, "eng", ALICE, REPO, mode="read")
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=_task())
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    assert signer.verifies(doc, task_id)
    tampered = copy.deepcopy(doc)
    tampered[field] = forged
    assert tampered[field] != doc[field]
    assert not signer.verifies(tampered, task_id)


# ---------------------------------------------------------------------------
# Invariant 10: never from the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("forge_credential", "git"),
    ("forge_access", "write"),
])
def test_a_task_body_naming_a_forge_field_is_refused(client, db, field, value):
    seed_grant(db, "eng", ALICE, REPO, mode="read")
    body = {**_task(), field: value}
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert field in response.text
    assert not _tasks(db)


@pytest.mark.parametrize("where", ["workflow", "step", "batch"])
def test_a_workflow_or_batch_naming_a_forge_field_is_refused(client, db, where):
    seed_grant(db, "eng", ALICE, REPO, mode="read")
    if where == "batch":
        path, body = "/v1/tasks/batch", {"tasks": [{**_task(), "forge_credential": "git"}]}
    else:
        path, body = "/v1/workflows", _workflow()
        if where == "workflow":
            body["forge_credential"] = "git"
        else:
            body["steps"][0]["forge_credential"] = "git"
    response = client.post(path, headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert not _tasks(db)
