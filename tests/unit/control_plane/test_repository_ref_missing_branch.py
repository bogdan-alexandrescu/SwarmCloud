"""A `repository_ref` branch the forge does not have is refused at submission (#2726).

THE DEFECT. wf_81c4f2da was submitted with `repository_ref:
swarm/task_77d63ca81cbb423ba839`, the branch of a pull request that had merged
and been deleted. Admission checked only the ref's shape (F4,
`check_repository_ref`), so the build step took a lease, waited for a GKE pod
and failed in the worker's clone, and the workflow's other steps were
cancelled. The submission now asks the forge ONCE, with the tenant's own
GitHub read, and answers 422 `repository_ref_not_found` before any task is
stored -- and admits as before whenever the forge does not answer.

Offline: the real routes over FakeFirestore, with a fake token reader and a
fake GitHub client injected through `build_context`.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import NoForgeCredential
from swarm_api.forgewrite import (
    ForgeUnreachable,
    ForgeWriteForbidden,
    ForgeWriteNotFound,
    ForgeWriteUnauthorized,
    PullSnapshot,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import OfflineSecretMetadata, api_settings, auth_header

REPO = "https://github.com/example-org/example.git"
OTHER_REPO = "https://github.com/example-org/widgets.git"
MISSING = "swarm/task_x"
FULL = "b7bda42e" + "0123456789abcdef" * 2
# The shape `new_id("task")` mints: a branch `swarm/<this>` names a task.
OWN_TASK = "task_" + "77d63ca81cbb423ba839"
OTHER_TASK = "task_" + "0123456789abcdef0123"
#: `refusals.env_name("repository_ref_not_found")`.
SWITCH = "REFUSAL_REPOSITORY_REF_NOT_FOUND"


class FakeTokens:
    """The tenant's forge credential: one value built at runtime, recorded so
    a test can prove it was never printed."""

    def __init__(self, error: Exception | None = None) -> None:
        self.value = secrets.token_hex(20)
        self.error = error
        self.asked: list[tuple[str, str | None]] = []

    def token_for(self, tenant, *, person=None) -> str:
        self.asked.append((tenant.tenant_id, person))
        if self.error is not None:
            raise self.error
        return self.value


class FakeForge:
    """GitHub, as the admission check reads it: which branches and tags exist
    per repository, and the pull requests it can read."""

    def __init__(self, *, branches=(), tags=(), error: Exception | None = None,
                 readable: bool = True, pulls: dict[int, PullSnapshot] | None = None) -> None:
        self.branches = set(branches)
        self.tags = set(tags)
        self.error = error
        self.readable = readable
        self.pulls = pulls or {}
        self.calls: list[tuple[str, str, str]] = []
        self.tokens: list[str] = []

    def _record(self, what: str, ref, name: str, token: str) -> None:
        self.calls.append((what, ref.repository, name))
        self.tokens.append(token)
        if self.error is not None:
            raise self.error

    def branch_head(self, ref, branch: str, token: str) -> str | None:
        self._record("branch", ref, branch, token)
        return FULL if branch in self.branches else None

    def tag_head(self, ref, tag: str, token: str) -> str | None:
        self._record("tag", ref, tag, token)
        return FULL if tag in self.tags else None

    def default_branch(self, ref, token: str) -> str:
        self._record("repository", ref, "", token)
        if not self.readable:
            raise ForgeWriteNotFound(f"the repository {ref.repository} was not found")
        return "main"

    def read_pull(self, ref, number: int, token: str) -> PullSnapshot:
        self._record("pull", ref, str(number), token)
        if number not in self.pulls:
            raise ForgeWriteNotFound(f"{ref.repository}#{number} was not found")
        return self.pulls[number]


def _client(db, tokens, group_map, objects, *, forge_tokens, forge) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge_writer=forge,
    )
    if forge is None:
        # No forge reader configured: `build_context` would supply GitHub's.
        ctx.submissions._forge_writer = None
    app = create_app(ctx)
    app.state.secret_metadata = OfflineSecretMetadata()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_client(db, tokens, group_map, objects):
    def _make(*, forge_tokens=None, forge=None) -> TestClient:
        return _client(db, tokens, group_map, objects,
                       forge_tokens=forge_tokens or FakeTokens(), forge=forge)
    return _make


def _workflow(client, ref: str, *, steps: int = 1, repository_url: str = REPO):
    return client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "repository_url": repository_url,
            "repository_ref": ref,
            "steps": [
                {"step_id": f"s{i}", "runner_profile": "mock",
                 **({"depends_on": [f"s{i - 1}"]} if i else {})}
                for i in range(steps)
            ],
        },
    )


def _tasks(client, *specs: tuple[str, str]):
    return client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={"tasks": [
            {"runner_profile": "mock", "repository_url": url, "repository_ref": ref}
            for url, ref in specs
        ]},
    )


def _task(client, ref: str):
    return client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "repository_url": REPO, "repository_ref": ref},
    )


def _stored(db) -> list[dict[str, Any]]:
    return [d for k, d in db.docs.items() if k.startswith("tasks/") and k.count("/") == 1]


def _nothing_written(db) -> bool:
    return not any(k.startswith("tasks/") for k in db.docs)


def _assert_refused(response, db, ref: str) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "repository_ref_not_found"
    assert body["detail"]["field"] == "repository_ref"
    assert body["detail"]["repository_ref"] == ref
    assert ref in body["message"]
    assert _nothing_written(db), "nothing may be written on a refusal"
    return body


def test_a_workflow_on_a_missing_branch_is_refused_before_any_task(make_client, db):
    forge = FakeForge(branches={"main"})
    client = make_client(forge=forge)

    body = _assert_refused(_workflow(client, MISSING, steps=3), db, MISSING)

    assert "example-org/example" in body["message"]
    assert ("branch", "example-org/example", MISSING) in forge.calls
    assert ("tag", "example-org/example", MISSING) in forge.calls


def test_a_task_on_a_missing_branch_is_refused(make_client, db):
    client = make_client(forge=FakeForge(branches={"main"}))

    _assert_refused(_task(client, MISSING), db, MISSING)
    _assert_refused(_tasks(client, (REPO, "main"), (OTHER_REPO, MISSING)), db, MISSING)


def test_the_forge_is_asked_once_per_submission(make_client, db):
    forge = FakeForge(branches={"main", "release/2026-10"})
    client = make_client(forge=forge)

    response = _workflow(client, "release/2026-10", steps=3)

    assert response.status_code == 201, response.text
    assert forge.calls == [("branch", "example-org/example", "release/2026-10")]
    assert len(_stored(db)) == 3

    forge.calls.clear()
    response = _tasks(client, (REPO, "main"), (REPO, "main"), (OTHER_REPO, "main"),
                      (REPO, "release/2026-10"))

    assert response.status_code == 201, response.text
    assert sorted(forge.calls) == sorted([
        ("branch", "example-org/example", "main"),
        ("branch", "example-org/widgets", "main"),
        ("branch", "example-org/example", "release/2026-10"),
    ])


def test_full_sha_and_tag_are_admitted(make_client, db):
    forge = FakeForge(branches={"main"}, tags={"v1.2.3"})
    client = make_client(forge=forge)

    assert _workflow(client, FULL).status_code == 201
    assert _task(client, FULL).status_code == 201
    assert forge.calls == [], "a full sha is fetched by sha, never looked up"

    response = _workflow(client, "v1.2.3")
    assert response.status_code == 201, response.text
    assert forge.calls == [("branch", "example-org/example", "v1.2.3"),
                           ("tag", "example-org/example", "v1.2.3")]

    forge.calls.clear()
    response = _task(client, FULL[:12])
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert "abbreviated" in response.json()["message"]
    assert forge.calls == []


@pytest.mark.parametrize(
    "case",
    ["unreachable", "timeout", "unauthorized", "forbidden", "no_credential", "no_reader",
     "unreadable_repository"],
)
def test_admission_fails_open_when_the_forge_does_not_answer(make_client, db, case):
    forge_tokens = FakeTokens()
    forge: FakeForge | None = FakeForge(branches={"main"})
    if case == "unreachable":
        forge.error = ForgeUnreachable("GitHub answered HTTP 502 for the branch")
    elif case == "timeout":
        forge.error = TimeoutError("timed out")
    elif case == "unauthorized":
        forge.error = ForgeWriteUnauthorized("GitHub refused the credential")
    elif case == "forbidden":
        forge.error = ForgeWriteForbidden("GitHub forbade the read")
    elif case == "no_credential":
        forge_tokens = FakeTokens(error=NoForgeCredential("no forge credential"))
    elif case == "no_reader":
        forge = None
    elif case == "unreadable_repository":
        forge.readable = False
    client = make_client(forge_tokens=forge_tokens, forge=forge)

    assert _workflow(client, MISSING).status_code == 201
    assert _task(client, MISSING).status_code == 201
    assert len(_stored(db)) == 2


def _seed_published(db, task_id: str, *, tenant_id: str, number: int) -> None:
    from .conftest import seed_task

    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED")
    doc["repository_url"] = REPO
    doc["result_summary"] = {"git": {"pull_request": {
        "number": number, "url": f"https://github.com/example-org/example/pull/{number}"}}}


def _pull(number: int, *, merged: bool, state: str) -> PullSnapshot:
    return PullSnapshot(number=number, url=f"https://github.com/example-org/example/pull/{number}",
                        head_sha=FULL, head_ref=f"swarm/{OWN_TASK}", body="",
                        state=state, merged=merged)


@pytest.mark.parametrize(("merged", "state", "word"),
                         [(True, "closed", "merged"), (False, "closed", "closed")])
def test_a_swarm_task_branch_names_its_pull_request_and_continues_task(
        make_client, db, merged, state, word):
    _seed_published(db, OWN_TASK, tenant_id="eng", number=1205)
    forge = FakeForge(branches={"main"}, pulls={1205: _pull(1205, merged=merged, state=state)})
    client = make_client(forge=forge)
    ref = f"swarm/{OWN_TASK}"

    response = _workflow(client, ref)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "repository_ref_not_found"
    message = body["message"]
    assert OWN_TASK in message and "1205" in message and word in message
    assert "continues_task" in message and "main" in message
    assert body["detail"]["task_id"] == OWN_TASK
    assert body["detail"]["pull_request"] == 1205
    assert not any(k.startswith("tasks/") and OWN_TASK not in k for k in db.docs)


def test_another_tenants_swarm_task_branch_reveals_nothing(make_client, db):
    _seed_published(db, OTHER_TASK, tenant_id="research", number=77)
    forge = FakeForge(branches={"main"}, pulls={77: _pull(77, merged=True, state="closed")})
    client = make_client(forge=forge)
    ref = f"swarm/{OTHER_TASK}"

    response = _workflow(client, ref)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "repository_ref_not_found"
    assert "77" not in body["message"].replace(OTHER_TASK, "")
    assert "merged" not in body["message"]
    assert "task_id" not in body["detail"] and "pull_request" not in body["detail"]
    assert not any(c[0] == "pull" for c in forge.calls)


def test_no_token_is_printed(make_client, db, caplog):
    caplog.set_level(logging.DEBUG)
    _seed_published(db, OWN_TASK, tenant_id="eng", number=1205)
    forge_tokens = FakeTokens()
    forge = FakeForge(branches={"main"}, pulls={1205: _pull(1205, merged=True, state="closed")})
    client = make_client(forge_tokens=forge_tokens, forge=forge)

    refused = _workflow(client, f"swarm/{OWN_TASK}")
    forge.error = ForgeUnreachable("GitHub answered HTTP 503 for the branch")
    admitted = _task(client, MISSING)

    assert refused.status_code == 422 and admitted.status_code == 201
    assert forge.tokens and set(forge.tokens) == {forge_tokens.value}
    for text in (refused.text, admitted.text, caplog.text):
        assert forge_tokens.value not in text


def test_off_the_refusal_is_logged_and_the_submission_admitted(make_client, db, caplog,
                                                                  monkeypatch):
    """A new refusal ships report-only (refusals.py): with its switch unset the
    missing branch is logged under its code and the workflow goes on."""
    monkeypatch.delenv(SWITCH, raising=False)
    caplog.set_level(logging.WARNING, logger="swarm_api.refusals")
    client = make_client(forge=FakeForge(branches={"main"}))

    response = _workflow(client, MISSING)

    assert response.status_code == 201, response.text
    assert "code=repository_ref_not_found" in caplog.text
    assert len(_stored(db)) == 1


@pytest.fixture(autouse=True)
def _switched_on(monkeypatch):
    """The refusal this file is about, switched on as `REFUSAL_<CODE>=on`
    turns it on in a deployment; the test above unsets it."""
    monkeypatch.setenv(SWITCH, "on")


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
