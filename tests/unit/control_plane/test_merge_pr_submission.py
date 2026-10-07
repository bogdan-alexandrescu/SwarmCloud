"""A merge-only workflow naming a pull request and its head sha (#352, MS0 question 2).

Owner decision 2026-10-07 (triage Q1): the caller names the pull request with
`merge_pr: {number, head_sha}` on the workflow submission; the repository is
the tenant's registered one -- the one `repository_url` names, or the
tenant's only registration when it names none -- and the workflow is the one
`merge` step, whose signed `merge_target` names the number and the head. The
merge itself is the merge step's existing gate (required checks green at that
head), so what this file holds is what is refused at SUBMISSION, before
anything is written:

  * a head sha that is not the pull request's current head;
  * a pull request not in the tenant's registered repository (another
    repository named, none registered, several and none named, a fork);
  * a pull request that is closed or already merged;
  * any step besides the one merge step.

GitHub is `forge_fakes.GitHubWrites` under the shipped `GitHubWriter`; the
tenant's token is built at runtime by `forge_fakes.AnyTenantTokens`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, repositories
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import MERGE_STEP_MAX_ATTEMPTS
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header

OWNER, NAME = "octo-org", "widget-shop"
REPO = f"https://github.com/{OWNER}/{NAME}"
NUMBER = 41
HEAD = "c" * 40
OLDER = "e" * 40


@pytest.fixture
def writes():
    fake = forge_fakes.GitHubWrites()
    fake.pulls[NUMBER] = _pull()
    return fake


@pytest.fixture
def forge_tokens():
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def api_context(db, tokens, group_map, objects, forge_tokens, writes):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge_writer=forgewrite.GitHubWriter(send=writes),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


def _pull(**overrides) -> dict:
    pull = {
        "number": NUMBER, "html_url": f"{REPO}/pull/{NUMBER}", "title": "Sort by price",
        "state": "open", "merged": False, "body": "",
        "head": {"sha": HEAD, "ref": "feature/price-sort",
                 "repo": {"full_name": f"{OWNER}/{NAME}"}},
        "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{NAME}"}},
    }
    pull.update(overrides)
    return pull


def _register(db, tenant_id: str, owner: str, repo: str, default_branch: str = "main") -> None:
    """A registration as `repositories.register` stores it, written directly."""
    repo_id = repositories.repo_id_for(tenant_id, owner, repo)
    db.collection(repositories.COLLECTION).document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "repository": f"{owner}/{repo}",
        "repository_url": repositories.repository_url(owner, repo),
        "default_branch": default_branch, "default_branch_source": "forge",
    })


def _spec(**overrides) -> dict:
    spec = {
        "strategy": "direct-pr",
        "merge_pr": {"number": NUMBER, "head_sha": HEAD},
        "steps": [{"step_id": "merge", "runner_profile": "merge"}],
    }
    spec.update(overrides)
    return spec


def _post(client, spec, user: str = "alice"):
    return client.post("/v1/workflows", headers=auth_header(user), json=spec)


def _tasks(db) -> list[str]:
    return [key for key in db.docs if key.startswith("tasks/")]


def _refused(client, db, spec, *phrases: str, status: int = 422) -> dict:
    response = _post(client, spec)
    assert response.status_code == status, response.text
    body = response.json()
    for phrase in phrases:
        assert phrase in body["message"], body["message"]
    assert _tasks(db) == [], "a refused merge_pr wrote a task"
    return body


# --------------------------------------------------------------------------
# Accepted: the control every refusal below is measured against
# --------------------------------------------------------------------------

def test_the_tenants_only_registered_repository_is_merged_at_the_named_head(client, db, writes):
    _register(db, "eng", OWNER, NAME)
    response = _post(client, _spec())
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    assert step["runner_profile"] == "merge"
    assert step["depends_on"] == []
    task = db.docs[f"tasks/{step['task_id']}"]
    assert task["repository_url"] == REPO
    assert task["max_attempts"] == MERGE_STEP_MAX_ATTEMPTS
    assert task["metadata"]["dispatch"]["merge_target"] == {
        "number": NUMBER, "head_sha": HEAD, "base": "main",
    }
    assert task["metadata"]["dispatch"]["strategy"] == "direct-pr"
    # The pull request was read once, with the tenant's own token.
    (read,) = [(m, u, h) for m, u, h, _ in writes.calls if m == "GET"]
    assert read[1].endswith(f"/repos/{OWNER}/{NAME}/pulls/{NUMBER}")


def test_the_named_registered_repository_is_used_when_the_tenant_has_several(client, db):
    _register(db, "eng", OWNER, NAME)
    _register(db, "eng", OWNER, "gadget-shop")
    response = _post(client, _spec(repository_url=f"{REPO}.git"))
    assert response.status_code == 201, response.text
    (step,) = response.json()["workflow"]["steps"]
    assert db.docs[f"tasks/{step['task_id']}"]["repository_url"] == REPO


def test_the_token_is_in_no_response(client, db, forge_tokens):
    _register(db, "eng", OWNER, NAME)
    accepted = _post(client, _spec())
    refused = _post(client, _spec(merge_pr={"number": NUMBER, "head_sha": OLDER}))
    assert accepted.status_code == 201 and refused.status_code == 422
    for token in forge_tokens.issued.values():
        assert token not in accepted.text and token not in refused.text


# --------------------------------------------------------------------------
# The head
# --------------------------------------------------------------------------

def test_a_head_sha_that_is_not_the_current_head_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    body = _refused(client, db, _spec(merge_pr={"number": NUMBER, "head_sha": OLDER}),
                    f"#{NUMBER}", "current head", HEAD)
    assert body["detail"]["current_head_sha"] == HEAD


@pytest.mark.parametrize("sha", ["C" * 40, "c" * 39, "main", ""])
def test_a_head_sha_that_is_not_a_full_lowercase_sha_is_refused_by_the_schema(client, db, sha):
    _register(db, "eng", OWNER, NAME)
    response = _post(client, _spec(merge_pr={"number": NUMBER, "head_sha": sha}))
    assert response.status_code == 422, response.text
    assert _tasks(db) == []


# --------------------------------------------------------------------------
# The repository
# --------------------------------------------------------------------------

def test_a_repository_the_tenant_did_not_register_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(repository_url=f"https://github.com/{OWNER}/other-shop"),
             "not a repository your tenant registered")


def test_another_tenants_registration_does_not_count(client, db):
    _register(db, "research", OWNER, NAME)
    _refused(client, db, _spec(repository_url=REPO), "not a repository your tenant registered")


def test_no_registered_repository_is_refused(client, db):
    _refused(client, db, _spec(), "registered no repository")


def test_several_registered_and_none_named_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _register(db, "eng", OWNER, "gadget-shop")
    _refused(client, db, _spec(), "registered 2 repositories", "repository_url")


def test_a_pull_request_not_in_the_registered_repository_is_refused(client, db, writes):
    _register(db, "eng", OWNER, NAME)
    del writes.pulls[NUMBER]
    _refused(client, db, _spec(), f"#{NUMBER}", f"{OWNER}/{NAME}")


def test_a_pull_request_from_a_fork_is_refused(client, db, writes):
    _register(db, "eng", OWNER, NAME)
    writes.pulls[NUMBER] = _pull(head={"sha": HEAD, "ref": "main",
                                       "repo": {"full_name": f"someone/{NAME}"}})
    _refused(client, db, _spec(), "fork", f"someone/{NAME}")


def test_a_pull_request_on_another_base_than_the_registered_default_is_refused(
    client, db, writes
):
    _register(db, "eng", OWNER, NAME, default_branch="develop")
    _refused(client, db, _spec(), "main", "develop")


# --------------------------------------------------------------------------
# The pull request's state
# --------------------------------------------------------------------------

def test_a_closed_pull_request_is_refused(client, db, writes):
    _register(db, "eng", OWNER, NAME)
    writes.pulls[NUMBER] = _pull(state="closed")
    _refused(client, db, _spec(), "closed")


def test_a_merged_pull_request_is_refused(client, db, writes):
    _register(db, "eng", OWNER, NAME)
    writes.pulls[NUMBER] = _pull(state="closed", merged=True)
    _refused(client, db, _spec(), "already merged")


# --------------------------------------------------------------------------
# The steps, and what may stand beside merge_pr
# --------------------------------------------------------------------------

def test_an_agent_step_beside_the_merge_step_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(steps=[
        {"step_id": "fix", "runner_profile": "mock", "input": {"prompt": "x"}},
        {"step_id": "merge", "runner_profile": "merge", "depends_on": ["fix"]},
    ]), "one merge step")


def test_an_agent_step_alone_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(steps=[
        {"step_id": "fix", "runner_profile": "mock", "input": {"prompt": "x"}},
    ]), "one merge step")


def test_two_merge_steps_are_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(steps=[
        {"step_id": "merge", "runner_profile": "merge"},
        {"step_id": "merge-2", "runner_profile": "merge"},
    ]), "one merge step")


def test_merge_pr_beside_continues_task_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(continues_task="task_" + "0" * 20), "continues_task")


def test_merge_pr_with_a_repository_ref_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(repository_ref="main"), "repository_ref")


def test_merge_pr_under_another_strategy_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(strategy="integrate"), "direct-pr")


def test_merge_pr_with_ci_fix_rounds_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(metadata={"merge_fix_rounds": 2}), "merge_fix_rounds")


def test_merge_pr_with_merge_off_is_refused(client, db):
    _register(db, "eng", OWNER, NAME)
    _refused(client, db, _spec(metadata={"merge": "off"}), "off")


def test_a_tenant_with_no_forge_credential_is_told_so(client, db, forge_tokens):
    from swarm_api.forge import GIT_PROVIDER

    _register(db, "eng", OWNER, NAME)
    forge_tokens.missing.add(f"swarm-tenant-eng-{GIT_PROVIDER}")
    response = _post(client, _spec())
    assert response.status_code == 409, response.text
    assert _tasks(db) == []
