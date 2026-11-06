"""A service submission's run gets an App installation token minted for it
(DAYONE-SERVICE-APP-TOKEN; part of #2688; lesson 43).

The first team's smoke and the console's verify runs are submitted as
swarm-api's own service account (`routes.setup.runner_identity`), so they
resolve their credential by the service rule (`forge.service_slot`). Day One's
verify passes only when each says `app_installation`. What these tests hold,
over the in-memory Firestore with no credentials, no network and no emulator:

  * submission: a platform service account's task on a repository whose
    owner has the App installed for THIS tenant is labelled
    `app_installation`, on the tenant's `-git` slot (the frozen
    `forge_credential` shape has no other name; a personal tenant's owner's
    slot is test_service_slot_app.py's), with write;
  * the fallback: no installation, an owner reached through a person's
    token, another tenant's installation, FORGE_INSTALLATION_READS off, a
    person's own task, or an index run submitted as a person keep today's
    service rule;
  * the scope: the mint asks for that ONE repository and exactly
    `contents` and `pull_requests` (write for a write, read for a read), and
    is never held: two releases are two mints;
  * the broker releases the minted token to the run's current attempt,
    recomputing the decision from the signed fields, reads no slot to do
    so, and answers `app_installation_unavailable` (the worker keeps the
    slot) when it cannot mint;
  * no leak: the token is in no log line, no header, no event and no
    Firestore document.

Every token is built at runtime (`secrets.token_hex`).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import credbroker, forge_installation, forgeapp
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge_installation import InstallationTokens, RunMintRefused
from swarm_api.gittokens import secret_name_for
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.schemas import TaskCreate
from swarm_api.validation import FORGE_SOURCE_METADATA_KEY
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal

from .conftest import ENG_GROUP, PROJECT, api_settings, seed_grant, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401 -- worker_tokens is a fixture
    KEY,
    Attempt,
    WORKER_TOKEN,
    register,
    seed_parent,
    worker_tokens,
)

SERVICE = f"swarm-api@{PROJECT}.iam.gserviceaccount.com"
ALICE = "alice@example.com"
OWNER, REPO = "acme", "widgets"
REPO_URL = f"https://github.com/{OWNER}/{REPO}.git"
PATH = "/v1/attempts/forge-credential"
INSTALLATION = 5501
T0 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
SIGNER = LocalSpecSigner()


def _value() -> str:
    return "ghs_" + secrets.token_hex(18)


def _enable(db, tenant_id: str, owner: str, installation: int | None, *,
            method: str | None = None, user: str = ALICE) -> None:
    """A `forge_orgs` document as access.py writes an enabled owner."""
    doc: dict[str, Any] = {
        "tenant_id": tenant_id, "user": user, "owner": owner.lower(), "owner_login": owner,
        "installation_id": installation,
        "install_state": "installed" if method is None else "token",
    }
    if method is not None:
        doc["method"] = method
    db.collection(forge_installation.ORGS).document(
        f"{tenant_id}__{forgeapp.user_hash(user)}__{owner.lower()}").set(doc)


# --------------------------------------------------------------------------
# submission: which tasks are labelled for a minted token
# --------------------------------------------------------------------------


def _context(db, tokens, group_map, **settings: Any):
    return build_context(
        settings=api_settings(**settings),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=LocalSpecSigner(),
    )


def _auth(email: str, tenant_id: str = "eng") -> AuthContext:
    return AuthContext(
        principal=Principal(email=email, subject=f"sub-{email}",
                            domain=email.rsplit("@", 1)[-1], groups=()),
        tenant_id=tenant_id, is_admin=False, tenant_principal=ENG_GROUP)


def _spec() -> TaskCreate:
    return TaskCreate(runner_profile="mock", input={"prompt": "smoke"},
                      repository_url=REPO_URL)


def test_a_service_accounts_task_on_an_installed_owner_is_labelled_app_installation(
        db, tokens, group_map):
    seed_tenant(db, "eng", credentials=("git",))
    _enable(db, "eng", OWNER, INSTALLATION)
    context = _context(db, tokens, group_map)

    task = context.submissions.submit_tasks(_auth(SERVICE), [_spec()]).tasks[0]

    # MUTATION: drop the installation check in `_resolve_forge` and this says "tenant".
    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "app_installation"
    # The tenant's `-git` slot: the frozen shape names no other.
    assert task.forge_credential == "git"
    assert task.forge_access == "write"


@pytest.mark.parametrize("how", ["none", "pat", "other_tenant", "switch_off"])
def test_without_an_installation_the_service_rule_is_unchanged(db, tokens, group_map, how):
    seed_tenant(db, "eng", credentials=("git",))
    settings: dict[str, Any] = {}
    if how == "pat":
        _enable(db, "eng", OWNER, None, method="pat")
    elif how == "other_tenant":
        _enable(db, "research", OWNER, INSTALLATION, user="bob@example.com")
    elif how == "switch_off":
        _enable(db, "eng", OWNER, INSTALLATION)
        settings["forge_installation_reads"] = False
    context = _context(db, tokens, group_map, **settings)

    task = context.submissions.submit_tasks(_auth(SERVICE), [_spec()]).tasks[0]

    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "tenant"
    assert task.forge_credential == "git"
    assert task.forge_access == "write"


def test_an_index_run_submitted_as_a_person_keeps_the_slot(db, tokens, group_map):
    # `repoindex` submits as the person who registered the repository with
    # `service_submission=True`: a service RULE, not a platform service
    # account, and the broker would refuse it a minted token.
    seed_tenant(db, "eng", credentials=("git",))
    _enable(db, "eng", OWNER, INSTALLATION)
    context = _context(db, tokens, group_map)

    task = context.submissions.submit_tasks(
        _auth(ALICE), [_spec()], service_submission=True).tasks[0]

    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "tenant"


def test_a_persons_own_task_is_never_labelled(db, tokens, group_map):
    seed_tenant(db, "eng", credentials=("git",))
    seed_grant(db, "eng", ALICE, f"{OWNER}/{REPO}")
    _enable(db, "eng", OWNER, INSTALLATION)
    context = _context(db, tokens, group_map)

    task = context.submissions.submit_tasks(_auth(ALICE), [_spec()]).tasks[0]

    assert task.forge_credential.startswith("git-u-")
    assert task.metadata.get(FORGE_SOURCE_METADATA_KEY) != "app_installation"


def test_the_label_is_reserved_from_callers(db, tokens, group_map):
    seed_tenant(db, "eng", credentials=("git",))
    context = _context(db, tokens, group_map)
    spec = TaskCreate(runner_profile="mock", input={"prompt": "smoke"},
                      repository_url=REPO_URL,
                      metadata={FORGE_SOURCE_METADATA_KEY: "app_installation"})
    with pytest.raises(Exception) as refused:
        context.submissions.submit_tasks(_auth(SERVICE), [spec])
    assert FORGE_SOURCE_METADATA_KEY in str(getattr(refused.value, "message", refused.value))


# --------------------------------------------------------------------------
# the mint: one repository, contents and pull requests, never held
# --------------------------------------------------------------------------


class Mints:
    """`POST /app/installations/{id}/access_tokens`, every call recorded."""

    def __init__(self, *, status: int = 201) -> None:
        self.status = status
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.issued: list[str] = []

    def __call__(self, method, url, headers, body, timeout) -> forgeapp.HttpAnswer:
        self.calls.append((method, url, json.loads(body or b"{}")))
        if self.status != 201:
            return forgeapp.HttpAnswer(self.status, {}, json.dumps(
                {"message": "Resource not accessible by integration"}).encode("utf-8"))
        token = _value()
        self.issued.append(token)
        expires = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return forgeapp.HttpAnswer(201, {}, json.dumps(
            {"token": token, "expires_at": expires}).encode("utf-8"))


def _installations(db, mints: Mints) -> InstallationTokens:
    jwt = "jwt." + secrets.token_hex(20)
    return InstallationTokens(db, app_jwt=lambda: jwt, send=mints,
                              clock=lambda: T0.timestamp())


def test_a_write_run_token_is_one_repository_with_contents_and_pull_requests_write(db):
    _enable(db, "eng", OWNER, INSTALLATION)
    mints = Mints()
    token = _installations(db, mints).run_token("eng", OWNER, REPO, write=True)
    assert token == mints.issued[-1]
    (method, url, body), = mints.calls
    assert method == "POST"
    assert url.endswith(f"/app/installations/{INSTALLATION}/access_tokens")
    # MUTATION: drop `repositories` and the token reaches every repository installed.
    assert body["repositories"] == [REPO]
    # MUTATION: add a permission (workflows, administration) and this is not equal.
    assert body["permissions"] == {"contents": "write", "pull_requests": "write"}


def test_a_read_run_token_asks_read_only(db):
    _enable(db, "eng", OWNER, INSTALLATION)
    mints = Mints()
    _installations(db, mints).run_token("eng", OWNER, REPO, write=False)
    assert mints.calls[0][2]["permissions"] == {"contents": "read", "pull_requests": "read"}


def test_a_run_token_is_never_held(db):
    _enable(db, "eng", OWNER, INSTALLATION)
    mints = Mints()
    tokens = _installations(db, mints)
    first = tokens.run_token("eng", OWNER, REPO, write=True)
    second = tokens.run_token("eng", OWNER, REPO, write=True)
    assert len(mints.calls) == 2 and first != second
    # Nor does the read cache ever hand it out.
    tokens.token_for_repo("eng", OWNER, REPO)
    assert mints.calls[-1][2]["permissions"] == dict(forge_installation.READ_PERMISSIONS)


def test_no_installation_or_a_refused_mint_is_a_typed_refusal(db, caplog):
    with pytest.raises(RunMintRefused) as none:
        _installations(db, Mints()).run_token("eng", OWNER, REPO, write=True)
    assert none.value.reason == forge_installation.MINT_NO_INSTALLATION
    _enable(db, "eng", OWNER, INSTALLATION)
    with pytest.raises(RunMintRefused) as refused:
        _installations(db, Mints(status=403)).run_token("eng", OWNER, REPO, write=True)
    assert refused.value.status == 403


# --------------------------------------------------------------------------
# the broker: released to the run's current attempt
# --------------------------------------------------------------------------


class FakeMinter:
    """`InstallationTokens.run_token`, recorded; mints a fresh value per call."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, str, str, bool]] = []
        self.issued: list[str] = []

    def token_for_repo(self, tenant_id: str, owner: str, repo: str) -> str | None:
        return None

    def run_token(self, tenant_id: str, owner: str, repo: str, *, write: bool) -> str:
        self.calls.append((tenant_id, owner, repo, write))
        if self.refuse is not None:
            raise RunMintRefused(self.refuse)
        token = _value()
        self.issued.append(token)
        return token


class SecretStore:
    """The slot reader; `asked` proves the App path reads no slot."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def read_slot(self, tenant, provider: str):
        self.asked.append(tenant.secret_name(provider))
        raise AssertionError("the App path reads no slot")


def _client(db, worker_tokens, group_map, store, minter, **settings: Any) -> TestClient:
    values = {
        "child_key": KEY,
        "forge_broker": False,
        "spec_signing_key_version": KEY_VERSION,
        "spec_verify_keys": json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        **settings,
    }
    ctx = build_context(
        settings=api_settings(**values),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        forge_tokens=store,
        forge_installations=minter,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _service_task(client, db, attempt: Attempt, *, submitted_by: str = SERVICE,
                  forge_access: str = "write") -> dict[str, Any]:
    doc = seed_parent(db, attempt, state="STARTING", submitted_by=submitted_by,
                      repository_url=REPO_URL, forge_credential="git",
                      forge_access=forge_access,
                      metadata={FORGE_SOURCE_METADATA_KEY: "app_installation"})
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    doc["state"] = "RUNNING"
    SIGNER.sign_document(doc, doc["id"])
    return doc


def _ask(client, attempt: Attempt, *, access: str = "write") -> Any:
    t = attempt.tuple
    raw = json.dumps({
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "access": access,
    }).encode()
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    headers.update(attempt.sign("POST", PATH, raw))
    return client.post(PATH, content=raw, headers=headers)


@pytest.fixture
def store() -> SecretStore:
    return SecretStore()


@pytest.mark.parametrize("broker_on", [False, True])
def test_the_broker_mints_the_service_runs_token_for_its_one_repository(
        db, worker_tokens, group_map, store, broker_on):
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter, forge_broker=broker_on)
    attempt = Attempt()
    _service_task(client, db, attempt)

    response = _ask(client, attempt)

    # MUTATION: keep FORGE_BROKER's early 503 for every task and the off case fails.
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["token"] == minter.issued[-1]
    # The name the worker checks the answer against: the task's signed slot.
    assert answer["secret"] == secret_name_for("eng", "git")
    assert minter.calls == [("eng", OWNER, REPO, True)]
    assert store.asked == []


def test_a_read_asks_a_read_mint(db, worker_tokens, group_map, store):
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter)
    attempt = Attempt()
    _service_task(client, db, attempt)
    assert _ask(client, attempt, access="read").status_code == 200
    assert minter.calls == [("eng", OWNER, REPO, False)]


def test_a_write_on_a_task_signed_for_read_mints_nothing(db, worker_tokens, group_map, store):
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter)
    attempt = Attempt()
    _service_task(client, db, attempt, forge_access="read")
    response = _ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "grant_read_only"
    assert minter.calls == []


def test_a_persons_task_with_the_label_rewritten_in_mints_nothing(
        db, worker_tokens, group_map, store):
    # The label is not signed; the decision is recomputed from `submitted_by`,
    # which is. FORGE_BROKER off, so Alice's task is refused as before.
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter)
    attempt = Attempt()
    _service_task(client, db, attempt, submitted_by=ALICE)
    response = _ask(client, attempt)
    assert response.status_code == 503
    assert response.json()["code"] == "forge_credential_unavailable"
    assert minter.calls == [] and store.asked == []


def test_a_rewritten_submitter_fails_the_spec_check(db, worker_tokens, group_map, store):
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter)
    attempt = Attempt()
    doc = _service_task(client, db, attempt, submitted_by=ALICE)
    doc["submitted_by"] = SERVICE  # an agent rewriting the signed field
    response = _ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "spec_unverified"
    assert minter.calls == []


@pytest.mark.parametrize("minter", [FakeMinter(refuse="no_installation"), None],
                         ids=["refused", "no_minter"])
def test_no_mint_is_app_installation_unavailable_and_the_worker_keeps_its_slot(
        db, worker_tokens, group_map, store, minter):
    seed_tenant(db, "eng", credentials=("git",))
    client = _client(db, worker_tokens, group_map, store, minter, forge_broker=True)
    attempt = Attempt()
    _service_task(client, db, attempt)
    response = _ask(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "app_installation_unavailable"
    assert store.asked == []


def test_the_token_is_in_no_log_header_event_or_document(
        db, worker_tokens, group_map, store, caplog):
    seed_tenant(db, "eng", credentials=("git",))
    minter = FakeMinter()
    client = _client(db, worker_tokens, group_map, store, minter)
    attempt = Attempt()
    _service_task(client, db, attempt)
    with caplog.at_level(logging.DEBUG):
        response = _ask(client, attempt)
    assert response.status_code == 200
    token = minter.issued[-1]
    for name, value in response.headers.items():
        assert token not in name and token not in value
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in repr(record.args)
    # Every Firestore document -- tasks, leases, events, audits.
    for key, doc in db.docs.items():
        assert token not in repr(doc), key
    released = [r.getMessage() for r in caplog.records if r.name == credbroker.log.name]
    assert any("decision=app_installation" in m and f"{OWNER}/{REPO}" in m
               for m in released), released
