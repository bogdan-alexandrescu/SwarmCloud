"""swarm-api accepts a step's additional repositories by name, and gates, pins and signs them.

docs/design/multi-repository-steps.md §4.1-§4.3 and §6 row 2 (lane MR-API,
part of #1434), with the owner's decisions on PR 1461 (2026-10-17): the gate
is registered AND `readable_by_tasks`; a repository only its own `git-r-`
token can read is refused; refs are pinned at submission and signed; only GKE
agent profiles take them; a step's list replaces the workflow's (`[]` is
none); a task with no primary repository may read them. The two limits are
PLATFORM SETTINGS an admin edits at run time (`/v1/admin/settings`), read at
every submission: the tests below change the setting and watch the very next
submission's answer change, with nothing redeployed. The feature flag
`ApiSettings.additional_repositories_enabled` defaults off.

#1434's check 3 -- "a repository the tenant has not registered is refused at
submission" -- is `test_an_unregistered_repository_is_refused_and_nothing_is_written`.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups, a local P-256
signer, and GitHub as a function over URLs. No credentials, no emulator, no
network. Every token is built at runtime.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import NoForgeCredential, SlotValue
from swarm_api.forgewrite import GitHubWriter
from swarm_api.gittokens import Scope, provider_suffix
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.repositories import repo_id_for
from swarm_api.schemas import TaskCreate
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal
from swarm_common.models import ADDITIONAL_REPOSITORIES_CEILING
from swarm_common.specsign import SPEC_FORMAT

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_grant, seed_tenant
from .spec_signer import LocalSpecSigner

ALICE = "alice@example.com"
SERVICE = f"swarm-acceptance@{PROJECT}.iam.gserviceaccount.com"
PRIMARY = "example-org/widgets"
WEB = "acme/web"
SDK = "acme/sdk"
INFRA = "acme/infra"
GIB_KIB = 1024 * 1024


def _sha(seed: str) -> str:
    return (seed * 40)[:40]


class FakeGitHub:
    """api.github.com as a function: `repos/<o>/<r>` and its `commits` list.

    `branches[(repo, ref)]` is the commit a ref names, `sizes[repo]` the KiB
    GitHub reports, `unreadable` the repositories that answer 404 to every
    token. Every call is recorded with the token it carried, so a test can
    show a pin was read once, and with which slot's token."""

    def __init__(self) -> None:
        self.branches: dict[tuple[str, str], str] = {}
        self.sizes: dict[str, int] = {}
        self.unreadable: set[str] = set()
        #: (repository, ref) -> the commit the ref names AFTER its first read:
        #: a branch that moves while a submission is being resolved.
        self.moves: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, repository: str, *, ref: str = "main", commit: str | None = None,
            size_kib: int = 1024) -> str:
        commit = commit or hashlib.sha1(repository.encode()).hexdigest()
        self.branches[(repository, ref)] = commit
        self.sizes[repository] = size_kib
        return commit

    def send(self, method: str, url: str, headers: dict[str, str], body: Any,
             timeout: float) -> tuple[int, bytes, dict[str, str]]:
        assert method == "GET"
        token = headers.get("Authorization", "").split()[-1]
        self.calls.append((url, token))
        path = url.split("https://api.github.com/repos/", 1)[1]
        owner, repo, *rest = path.split("/")
        repository = f"{owner}/{repo}"
        if repository in self.unreadable or repository not in self.sizes:
            return 404, b'{"message": "Not Found"}', {}
        if not rest:
            return 200, json.dumps({"size": self.sizes[repository],
                                    "default_branch": "main"}).encode(), {}
        ref = rest[0].split("sha=", 1)[1].split("&", 1)[0]
        commit = self.branches.get((repository, ref))
        if commit is None and len(ref) == 40:
            commit = ref if ref in self.branches.values() else None
        if commit is None:
            return 404, b'{"message": "No commit found"}', {}
        if (repository, ref) in self.moves:
            self.branches[(repository, ref)] = self.moves.pop((repository, ref))
        return 200, json.dumps([{"sha": commit}]).encode(), {}

    def commit_reads(self, repository: str) -> int:
        return sum(1 for url, _ in self.calls if f"/repos/{repository}/commits" in url)


class FakeTokens:
    """Each slot's value, minted at runtime; `read_slot` is what the pin uses."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.reads: list[str] = []

    def value_of(self, provider: str) -> str:
        return self.values.setdefault(provider, "ghp_" + secrets.token_hex(18))

    def read_slot(self, tenant, provider: str, *, known: bool = True) -> SlotValue:
        self.reads.append(provider)
        return SlotValue(value=self.value_of(provider))

    def token_for(self, tenant, *, person=None) -> str:
        raise NoForgeCredential("no issue reads in this test")


@pytest.fixture
def github() -> FakeGitHub:
    hub = FakeGitHub()
    hub.add(WEB)
    hub.add(SDK)
    hub.add(INFRA)
    return hub


@pytest.fixture
def forge_tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def flags() -> dict[str, Any]:
    return {"additional_repositories_enabled": True, "repository_grants_enforced": True}


@pytest.fixture
def context(db, tokens, group_map, signer, github, forge_tokens, flags):
    return build_context(
        settings=api_settings(**flags),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
        forge_tokens=forge_tokens,
        forge_writer=GitHubWriter(send=github.send),
    )


@pytest.fixture
def client(context) -> TestClient:
    return TestClient(create_app(context), raise_server_exceptions=False)


def register(db, repository: str, *, tenant_id: str = "eng", readable: bool = True,
             archived: bool = False, default_branch: str = "main") -> str:
    owner, repo = repository.split("/")
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo,
        "repository_url": f"https://github.com/{owner}/{repo}",
        "default_branch": default_branch, "archived": archived,
        "readable_by_tasks": readable,
    }
    return repo_id


@pytest.fixture
def ready(db):
    """WEB, SDK and INFRA registered in eng, readable by tasks, granted to alice."""
    for repository in (WEB, SDK, INFRA):
        register(db, repository)
        seed_grant(db, "eng", ALICE, repository, mode="read")


def _tasks(db) -> dict[str, dict[str, Any]]:
    return {key.split("/", 1)[1]: doc for key, doc in db.docs.items()
            if key.startswith("tasks/") and key.count("/") == 1}


def _task(extra: list[dict[str, Any]] | None, *, profile: str = "claude-code",
          repository_url: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"runner_profile": profile, "input": {"prompt": "read them"}}
    if extra is not None:
        body["additional_repositories"] = extra
    if repository_url is not None:
        body["repository_url"] = repository_url
    return body


def _post(client, body: dict[str, Any], user: str = "alice"):
    return client.post("/v1/tasks", headers=auth_header(user), json=body)


def _set(client, **settings: Any):
    response = client.put("/v1/admin/settings", headers=auth_header("root"), json=settings)
    assert response.status_code == 200, response.text
    return response.json()


def _service_auth() -> AuthContext:
    return AuthContext(
        principal=Principal(email=SERVICE, subject="sa-1", domain="example.com", groups=()),
        tenant_id="eng", is_admin=False, tenant_principal=ENG_GROUP,
    )


# ---------------------------------------------------------------------------
# What is stored, signed and served
# ---------------------------------------------------------------------------


def test_a_persons_task_pins_each_repository_with_their_slot_and_signs_it_at_format_4(
        client, db, signer, github, forge_tokens, ready):
    response = _post(client, _task([{"repository": WEB}, {"repository": SDK, "ref": "main",
                                                           "name": "the-sdk"}]))
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    alice_slot = provider_suffix(Scope.USER, user=ALICE)
    assert doc["additional_repositories"] == [
        {"name": "web", "repo_id": repo_id_for("eng", "acme", "web"), "repository": WEB,
         "repository_url": "https://github.com/acme/web", "ref": "main",
         "commit": github.branches[(WEB, "main")], "forge_credential": alice_slot,
         "forge_credential_source": "person", "size_kib": 1024},
        {"name": "the-sdk", "repo_id": repo_id_for("eng", "acme", "sdk"), "repository": SDK,
         "repository_url": "https://github.com/acme/sdk", "ref": "main",
         "commit": github.branches[(SDK, "main")], "forge_credential": alice_slot,
         "forge_credential_source": "person", "size_kib": 1024},
    ]
    # Read with the slot the worker will clone with, and with nothing else.
    assert set(forge_tokens.reads) == {alice_slot}
    assert {token for _, token in github.calls} == {forge_tokens.values[alice_slot]}
    # Signed over the list (contract request 96): a rewritten commit fails.
    # At 4, the oldest format that covers the list, not at SPEC_FORMAT: a
    # later format (5, contract request 104) adds fields this task does not set.
    assert doc["spec_format"] == 4 <= SPEC_FORMAT
    assert signer.verifies(doc, task_id)
    doc["additional_repositories"][0]["commit"] = _sha("f")
    assert not signer.verifies(doc, task_id)


def test_the_registrations_spelling_is_stored_whatever_case_the_caller_typed(
        client, db, ready):
    response = _post(client, _task([{"repository": "ACME/Web"}]))
    assert response.status_code == 201, response.text
    (_, doc), = _tasks(db).items()
    (entry,) = doc["additional_repositories"]
    assert (entry["repository"], entry["repository_url"], entry["name"]) == (
        WEB, "https://github.com/acme/web", "web")


def test_a_task_with_no_primary_repository_may_read_additional_ones(client, db, ready):
    """Owner's Q8: allowed. It has no forge credential of its own."""
    response = _post(client, _task([{"repository": WEB}]))
    assert response.status_code == 201, response.text
    (_, doc), = _tasks(db).items()
    assert doc.get("repository_url") is None
    assert [e["name"] for e in doc["additional_repositories"]] == ["web"]


def test_the_response_serves_the_pin_and_no_url_and_no_token(client, db, forge_tokens, ready):
    response = _post(client, _task([{"repository": WEB, "ref": "main"}]))
    assert response.status_code == 201, response.text
    (task_id, _), = _tasks(db).items()
    for body in (response.text, client.get(f"/v1/tasks/{task_id}",
                                           headers=auth_header("alice")).text):
        assert "https://github.com/acme/web" not in body
        for value in forge_tokens.values.values():
            assert value not in body
    served = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice")).json()["task"]
    (entry,) = served["additional_repositories"]
    assert entry == {"name": "web", "repository": WEB,
                     "repo_id": repo_id_for("eng", "acme", "web"), "ref": "main",
                     "commit": hashlib.sha1(WEB.encode()).hexdigest(), "path": "../repos/web",
                     "forge_credential_source": "person"}


def test_a_task_that_names_none_is_signed_as_before_and_reads_nothing(client, db, github, ready):
    for extra in (None, []):
        response = _post(client, _task(extra))
        assert response.status_code == 201, response.text
    docs = list(_tasks(db).values())
    assert len(docs) == 2
    for doc in docs:
        assert doc.get("additional_repositories") is None
        assert doc["spec_format"] < 4
    assert github.calls == []


def test_a_service_submission_reads_with_the_tenants_slot(context, db, github, forge_tokens):
    seed_tenant(db, "eng", credentials=("git",))
    register(db, WEB)
    result = context.submissions.submit_tasks(
        _service_auth(), [TaskCreate.model_validate(_task([{"repository": WEB}]))])
    (task,) = result.tasks
    (entry,) = task.additional_repositories
    assert entry["forge_credential"] == "git"
    assert entry["forge_credential_source"] == "tenant"
    assert forge_tokens.reads == ["git"]


def test_the_token_is_in_no_log_line(client, db, forge_tokens, ready, caplog):
    caplog.set_level(logging.DEBUG)
    assert _post(client, _task([{"repository": WEB}])).status_code == 201
    assert forge_tokens.values
    for value in forge_tokens.values.values():
        assert value not in caplog.text


# ---------------------------------------------------------------------------
# The gate: registered, readable by tasks, granted (#1434 check 3)
# ---------------------------------------------------------------------------


def test_an_unregistered_repository_is_refused_and_nothing_is_written(client, db, github):
    seed_grant(db, "eng", ALICE, WEB)
    response = _post(client, _task([{"repository": WEB}]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "repository_not_registered"
    assert WEB in response.json()["message"]
    assert _tasks(db) == {}
    assert github.calls == []


def test_another_tenants_registration_is_no_registration(client, db, github):
    register(db, WEB, tenant_id="research")
    seed_grant(db, "eng", ALICE, WEB)
    response = _post(client, _task([{"repository": WEB}]))
    assert response.json()["code"] == "repository_not_registered"
    assert _tasks(db) == {}


@pytest.mark.parametrize("why", [{"readable": False}, {"archived": True}])
def test_a_registration_tasks_may_not_read_is_refused(client, db, github, why):
    register(db, WEB, **why)
    seed_grant(db, "eng", ALICE, WEB)
    response = _post(client, _task([{"repository": WEB}]))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "repository_not_readable_by_tasks"
    assert _tasks(db) == {}
    assert github.calls == []


def test_a_person_with_no_grant_is_refused_naming_every_repository(client, db, github):
    register(db, WEB)
    register(db, SDK)
    response = _post(client, _task([{"repository": WEB}, {"repository": SDK}]))
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "REPOSITORY_NOT_GRANTED"
    assert body["detail"]["repositories"] == [SDK, WEB]
    assert _tasks(db) == {}
    assert github.calls == []


@pytest.mark.parametrize("repository", [
    "https://github.com/acme/web", "git@github.com:acme/web.git", "acme", "acme/web/extra",
])
def test_a_url_or_anything_but_owner_repo_is_refused_by_shape(client, db, ready, repository):
    response = _post(client, _task([{"repository": repository}]))
    assert response.status_code == 422, response.text
    assert _tasks(db) == {}


def test_a_caller_cannot_send_a_slot_a_commit_or_a_url(client, db, ready):
    for key, value in (("forge_credential", "git"), ("commit", _sha("a")),
                       ("repository_url", "https://github.com/acme/web")):
        response = _post(client, _task([{"repository": WEB, key: value}]))
        assert response.status_code == 422, (key, response.text)
    assert _tasks(db) == {}


# ---------------------------------------------------------------------------
# What GitHub says: unreadable refs and repositories, the size budget
# ---------------------------------------------------------------------------


def test_a_repository_the_slot_cannot_read_is_refused_unreadable(client, db, github, ready):
    """A repository only its own `git-r-` token reads answers 404 to the
    person's slot: refused (owner's Q2), never read with the git-r- slot."""
    github.unreadable.add(WEB)
    response = _post(client, _task([{"repository": WEB}]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "additional_repository_unreadable"
    assert _tasks(db) == {}


def test_a_ref_that_names_no_commit_is_refused_unreadable(client, db, ready):
    response = _post(client, _task([{"repository": WEB, "ref": "no-such-branch"}]))
    assert response.json()["code"] == "additional_repository_unreadable"
    assert _tasks(db) == {}


def test_an_abbreviated_sha_is_refused_before_any_read(client, db, github, ready):
    response = _post(client, _task([{"repository": WEB, "ref": "b7bda42e"}]))
    assert response.status_code == 422, response.text
    assert github.calls == []


def test_a_full_sha_ref_is_pinned_to_itself(client, db, github, ready):
    commit = github.branches[(WEB, "main")]
    response = _post(client, _task([{"repository": WEB, "ref": commit}]))
    assert response.status_code == 201, response.text
    (_, doc), = _tasks(db).items()
    assert doc["additional_repositories"][0]["commit"] == commit


def test_the_size_budget_is_the_platform_setting_and_moves_without_a_deploy(
        client, db, github, ready):
    github.sizes[WEB] = 4 * GIB_KIB
    github.sizes[SDK] = 3 * GIB_KIB
    both = _task([{"repository": WEB}, {"repository": SDK}])
    response = _post(client, both)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "additional_repositories_too_large"
    assert response.json()["detail"]["budget_gib"] == 6
    assert _tasks(db) == {}

    _set(client, additional_repositories_budget_gib=8)
    assert _post(client, both).status_code == 201
    _set(client, additional_repositories_budget_gib=1)
    assert _post(client, _task([{"repository": WEB}])).json()["detail"]["budget_gib"] == 1


# ---------------------------------------------------------------------------
# The count limit is a platform setting, read at every submission
# ---------------------------------------------------------------------------


def _many(db, github, count: int) -> list[dict[str, Any]]:
    names = []
    for i in range(count):
        repository = f"acme/svc{i}"
        github.add(repository, commit=_sha(str(i)))
        register(db, repository)
        seed_grant(db, "eng", ALICE, repository, mode="read")
        names.append({"repository": repository})
    return names


def test_the_count_limit_defaults_to_5_and_names_itself(client, db, github):
    six = _many(db, github, 6)
    response = _post(client, _task(six))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "too_many_additional_repositories"
    assert body["detail"]["limit"] == 5
    assert "at most 5" in body["message"]
    assert _tasks(db) == {}
    assert github.calls == []
    assert _post(client, _task(six[:5])).status_code == 201


def test_changing_the_setting_changes_the_next_refusal_without_a_deploy(client, db, github):
    six = _many(db, github, 6)
    assert _post(client, _task(six)).json()["code"] == "too_many_additional_repositories"
    _set(client, max_additional_repositories=7)
    assert _post(client, _task(six)).status_code == 201
    _set(client, max_additional_repositories=2)
    response = _post(client, _task(six[:3]))
    assert response.json()["detail"]["limit"] == 2
    assert "at most 2" in response.json()["message"]


def test_a_list_over_the_contract_ceiling_gets_the_same_refusal(client, db, github):
    _set(client, max_additional_repositories=ADDITIONAL_REPOSITORIES_CEILING)
    eleven = [{"repository": f"acme/r{i}"} for i in range(ADDITIONAL_REPOSITORIES_CEILING + 1)]
    response = _post(client, _task(eleven))
    assert response.json()["code"] == "too_many_additional_repositories"
    assert response.json()["detail"]["limit"] == ADDITIONAL_REPOSITORIES_CEILING


# ---------------------------------------------------------------------------
# Names and duplicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [
    [{"repository": WEB}, {"repository": WEB, "name": "web2"}],
    [{"repository": WEB}, {"repository": "Acme/Web", "name": "web2"}],
    [{"repository": WEB}, {"repository": SDK, "name": "web"}],
])
def test_one_repository_or_one_directory_twice_is_refused(client, db, github, ready, extra):
    response = _post(client, _task(extra))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "additional_repository_duplicate"
    assert github.calls == []


def test_the_tasks_own_repository_is_not_an_additional_one(client, db, github, ready):
    seed_grant(db, "eng", ALICE, PRIMARY)
    register(db, PRIMARY)
    response = _post(client, _task([{"repository": PRIMARY}],
                                   repository_url=f"https://github.com/{PRIMARY}"))
    assert response.json()["code"] == "additional_repository_duplicate"
    assert _tasks(db) == {}


@pytest.mark.parametrize("name", ["..", ".hidden", "Web", "a/b", "x" * 64])
def test_a_directory_name_outside_the_contract_is_refused(client, db, ready, name):
    response = _post(client, _task([{"repository": WEB, "name": name}]))
    assert response.status_code == 422, response.text
    assert _tasks(db) == {}


# ---------------------------------------------------------------------------
# Profiles (owner's Q4): GKE agent profiles only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["mock"])
def test_a_cloud_run_profile_is_refused(client, db, github, ready, profile):
    response = _post(client, _task([{"repository": WEB}], profile=profile))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "additional_repositories_unsupported_profile"
    assert _tasks(db) == {}
    assert github.calls == []


def test_which_profiles_take_them_is_read_from_the_catalogue():
    from swarm_api.extrarepos import INDEXER_PROFILE, takes_additional_repositories
    from swarm_api.repoindex import INDEXER_PROFILE as REPOINDEX_INDEXER
    from swarm_common.profiles import RUNNER_PROFILES

    assert INDEXER_PROFILE == REPOINDEX_INDEXER
    taking = sorted(n for n, p in RUNNER_PROFILES.items() if takes_additional_repositories(p))
    assert taking == [
        "browser", "claude-code", "claude-code-browser", "claude-code-large", "claude-code-lightpanda",
        "codex", "generic",
    ]


# ---------------------------------------------------------------------------
# Workflows: one pin per submission; a step's list replaces the workflow's
# ---------------------------------------------------------------------------


def _workflow(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "on_step_failure": "continue",
        "additional_repositories": [{"repository": WEB}],
        "steps": [
            {"step_id": "map", "runner_profile": "claude-code", "input": {"prompt": "map"}},
            {"step_id": "plan", "runner_profile": "claude-code", "input": {"prompt": "plan"},
             "depends_on": ["map"]},
            {"step_id": "own", "runner_profile": "claude-code", "input": {"prompt": "own"},
             "additional_repositories": [{"repository": SDK}]},
            {"step_id": "none", "runner_profile": "mock", "input": {"prompt": "none"},
             "additional_repositories": []},
        ],
    }
    spec.update(overrides)
    return spec


def _by_step(db) -> dict[str, dict[str, Any]]:
    return {doc["step_id"]: doc for doc in _tasks(db).values()}


def test_every_step_of_a_workflow_reads_one_commit_and_a_step_list_replaces(
        client, db, github, ready, signer):
    first = github.branches[(WEB, "main")]
    github.moves[(WEB, "main")] = _sha("9")
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert response.status_code == 201, response.text
    steps = _by_step(db)
    assert [e["commit"] for e in steps["map"]["additional_repositories"]] == [first]
    assert [e["commit"] for e in steps["plan"]["additional_repositories"]] == [first]
    # The control: the branch did move after that one read.
    assert github.branches[(WEB, "main")] == _sha("9") != first
    assert github.commit_reads(WEB) == 1
    assert [e["repository"] for e in steps["own"]["additional_repositories"]] == [SDK]
    assert steps["none"].get("additional_repositories") is None
    for task_id, doc in _tasks(db).items():
        assert signer.verifies(doc, task_id)
        assert (doc["spec_format"] == 4) == bool(doc.get("additional_repositories"))


def test_a_cloud_run_step_inheriting_the_workflows_list_is_refused(client, db, ready):
    spec = _workflow(steps=[
        {"step_id": "map", "runner_profile": "claude-code", "input": {"prompt": "map"}},
        {"step_id": "mock", "runner_profile": "mock", "input": {"prompt": "x"}},
    ])
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "additional_repositories_unsupported_profile"
    assert body["detail"]["step_id"] == "mock"
    assert _tasks(db) == {}


def test_a_refused_step_writes_no_task_of_the_workflow(client, db, ready):
    spec = _workflow()
    spec["steps"][2]["additional_repositories"] = [{"repository": "acme/unregistered"}]
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
    assert response.json()["code"] == "repository_not_registered"
    assert response.json()["detail"]["step_id"] == "own"
    assert _tasks(db) == {}
    assert not any(key.startswith("workflows/") for key in db.docs)


# ---------------------------------------------------------------------------
# The feature flag, off by default
# ---------------------------------------------------------------------------


def test_the_flag_is_off_by_default():
    from swarm_api.settings import ApiSettings

    assert ApiSettings.__dataclass_fields__["additional_repositories_enabled"].default is False


def test_the_flag_reads_its_environment_variable(monkeypatch):
    from swarm_api.settings import ApiSettings

    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert ApiSettings.from_env().additional_repositories_enabled is False
    monkeypatch.setenv("ADDITIONAL_REPOSITORIES_ENABLED", "true")
    assert ApiSettings.from_env().additional_repositories_enabled is True


@pytest.mark.parametrize("flags", [{"additional_repositories_enabled": False,
                                    "repository_grants_enforced": True}])
def test_off_a_named_repository_is_refused_and_an_empty_list_is_not(client, db, github, ready):
    response = _post(client, _task([{"repository": WEB}]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "additional_repositories_disabled"
    assert _tasks(db) == {}
    assert github.calls == []
    assert _post(client, _task([])).status_code == 201
    workflow = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert workflow.json()["code"] == "additional_repositories_disabled"


# ---------------------------------------------------------------------------
# The two settings on /v1/admin/settings
# ---------------------------------------------------------------------------


def test_the_settings_route_serves_both_limits_at_their_defaults(client):
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert body["max_additional_repositories"] == 5
    assert body["additional_repositories_budget_gib"] == 6
    changed = _set(client, max_additional_repositories=3, additional_repositories_budget_gib=9)
    assert changed["max_additional_repositories"] == 3
    assert changed["additional_repositories_budget_gib"] == 9
    assert changed["merge_by_default"] is False


@pytest.mark.parametrize("change", [
    {"max_additional_repositories": 0},
    {"max_additional_repositories": ADDITIONAL_REPOSITORIES_CEILING + 1},
    {"max_additional_repositories": "5"},
    {"max_additional_repositories": True},
    {"additional_repositories_budget_gib": 0},
    {"additional_repositories_budget_gib": 21},
    {"additional_repositories_budget_gib": 2.5},
])
def test_a_limit_out_of_its_range_is_refused(client, change):
    response = client.put("/v1/admin/settings", headers=auth_header("root"), json=change)
    assert response.status_code == 422, response.text
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert body["max_additional_repositories"] == 5
    assert body["additional_repositories_budget_gib"] == 6


def test_only_an_admin_sets_the_limits(client):
    response = client.put("/v1/admin/settings", headers=auth_header("alice"),
                          json={"max_additional_repositories": 10})
    assert response.status_code == 403


@pytest.mark.parametrize("stored", [0, 21, "7", True, 3.0, None])
def test_a_malformed_stored_limit_reads_as_its_default(db, client, stored):
    db.docs["control/settings"] = {"max_additional_repositories": stored,
                                   "additional_repositories_budget_gib": stored}
    body = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert body["max_additional_repositories"] == 5
    assert body["additional_repositories_budget_gib"] == 6


def test_the_route_and_the_store_accept_the_same_ranges():
    from swarm_api.schemas import PlatformSettingsRequest
    from swarm_api.store import PLATFORM_SETTINGS_DEFAULTS, PLATFORM_SETTINGS_RANGES

    for key, (low, high) in PLATFORM_SETTINGS_RANGES.items():
        bounds = {type(m).__name__: m for m in PlatformSettingsRequest.model_fields[key].metadata}
        assert (bounds["Ge"].ge, bounds["Le"].le) == (low, high), key
        assert low <= PLATFORM_SETTINGS_DEFAULTS[key] <= high
    assert PLATFORM_SETTINGS_RANGES["max_additional_repositories"][1] == \
        ADDITIONAL_REPOSITORIES_CEILING


def test_the_worker_reads_the_budget_from_the_key_swarm_api_serves():
    from swarm_api import extrarepos
    from swarm_api.store import SETTINGS_DOC

    worker = pytest.importorskip("agent_worker.extrarepos")
    assert worker.BUDGET_SETTING == extrarepos.BUDGET_SETTING
    assert worker.SETTINGS_DOC == SETTINGS_DOC
