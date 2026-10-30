"""Admin › Setup's routes: who may call them, and what they read
(docs/design/day-one-plan.md §5.2, §5.3; lane DAYONE-SETUP-API-1).

Every `/v1/admin/setup/*` route is a platform admin's; a workspace admin is
refused like any member. The engine's deployer identity is admitted on the
hook and the groups probe only, swarm-doctor on the health write and the
probe. The engine's document `swarm-install/<env>` is read and never written.

The fixtures here (`setup_client`, the fakes) are imported by the other three
setup test files.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa, utils
from fastapi.testclient import TestClient

from swarm_api import publish_workspace as pw
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import UpstreamUnavailable
from swarm_api.forgeapp import HttpAnswer
from swarm_api.groups import GroupLookupError, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.setup_github import GitHubSetupApi
from swarm_api.specsigning import SpecSignature
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, NoForgeTokens, api_settings, auth_header

ROOT = "root@example.com"
ALICE = "alice@example.com"
CAROL = "carol@example.com"
WORKSPACE_ADMIN = "wadmin@example.com"
TEAM_GROUP = "platform-team@example.com"
DEPLOYER = f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"
DOCTOR = f"swarm-doctor@{PROJECT}.iam.gserviceaccount.com"
ENV = "test"
ENGINE_DOC = f"swarm-install/{ENV}"
HOSTNAME = "swarm.example.com"
ORG = "example-org"
HOOKS_URL = "https://hooks.example.com/github"

#: The settings MAC key the first admin's grant is signed with; built at run time.
SETTINGS_KEY = secrets.token_hex(32)
SIGNING_VERSION = (f"projects/{PROJECT}/locations/us-central1/keyRings/swarm-dev-specs/"
                   "cryptoKeys/step-spec/cryptoKeyVersions/1")

#: Every `/v1/admin/setup/*` route, with a body that passes validation.
ROUTES: list[tuple[str, str, Any]] = [
    ("GET", "/v1/admin/setup", None),
    ("POST", "/v1/admin/setup/github-app/manifest", {}),
    ("POST", "/v1/admin/setup/github-app/conversion", {"code": "c0de", "state": "st4te"}),
    ("GET", "/v1/admin/setup/github-app/installation", None),
    ("POST", "/v1/admin/setup/github-app/self-check", None),
    ("POST", "/v1/admin/setup/github-app/hook", None),
    ("POST", "/v1/admin/setup/links", {"kind": "addon-secret", "label": "tailscale",
                                       "owner": ROOT}),
    ("POST", "/v1/admin/setup/teams", {"group": TEAM_GROUP}),
    ("POST", "/v1/admin/setup/finish", None),
    ("POST", "/v1/admin/setup/teams/platform-team/invites", {"email": CAROL}),
    ("POST", "/v1/admin/setup/smoke", None),
    ("POST", "/v1/admin/setup/verify-runs", None),
    ("PUT", "/v1/admin/setup/health", {"run_at": "2026-10-29T12:00:00Z"}),
    ("GET", "/v1/admin/setup/health", None),
    ("POST", "/v1/admin/setup/health/groups-probe", None),
]


class FakePublisher:
    def __init__(self) -> None:
        self.enabled = True
        self.messages: list[dict[str, str]] = []

    def publish(self, workspace_id: str, mode: str, request_id: str) -> bool:
        self.messages.append(pw.message_data(workspace_id, mode, request_id))
        return True


class InProcessSigner:
    """Cloud KMS's AsymmetricSign, in process (as test_workspace_kinds_record)."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())

    def sign(self, digest: bytes) -> SpecSignature:
        der = self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return SpecSignature(signature=der, key_version=SIGNING_VERSION)

    def verify_keys(self) -> str:
        return json.dumps({SIGNING_VERSION: self.private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()})


class RecordingDirectory(StaticGroups):
    """StaticGroups that records each question and can fail like an outage."""

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        super().__init__(mapping)
        self.asked: list[tuple[str, tuple[str, ...]]] = []
        self.unreadable: set[str] = set()

    def groups_for(self, member_email, candidate_groups, *, fresh=False):
        self.asked.append((member_email, tuple(candidate_groups)))
        if any(g in self.unreadable for g in candidate_groups):
            raise GroupLookupError("unreadable")
        return super().groups_for(member_email, candidate_groups, fresh=fresh)

    def group_resource_name(self, group_email: str) -> str:
        """`CloudIdentityGroups.group_resource_name`: `groups:lookup`'s name."""
        return f"groups/0id{group_email.split('@')[0].replace('-', '')}"

    def join(self, email: str, group: str) -> None:
        """The person was added in Google Admin: the Groups API now says so."""
        self._mapping[email.lower()] = (*self._mapping.get(email.lower(), ()), group)


class FakeSecrets:
    """Secret Manager's add/list/read for the setup flow, recorded."""

    def __init__(self) -> None:
        self.versions: dict[str, list[str]] = {}
        self.created: dict[str, dict[str, str]] = {}
        self.fail = False
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def add_version(self, secret_id: str, value: str, *,
                    create_labels: dict[str, str] | None = None) -> None:
        if self.fail:
            raise UpstreamUnavailable(f"{secret_id} could not be written")
        if create_labels is not None:
            self.created.setdefault(secret_id, dict(create_labels))
        self.versions.setdefault(secret_id, []).append(value)

    def has_enabled_version(self, secret_id: str) -> bool:
        return bool(self.versions.get(secret_id))

    def private_key(self) -> str:
        return self.key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode()


class FakeGitHubSend:
    """`forgeapp.HttpSend` over a script of answers, every request recorded."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.conversion: dict[str, Any] = {}
        self.installations: list[dict[str, Any]] = []
        #: What `GET /installation/repositories` lists, by full name.
        self.repositories: list[str] = []
        #: Every installation token minted, built at runtime.
        self.minted: list[str] = []

    def __call__(self, method, url, headers, body, timeout) -> HttpAnswer:
        self.requests.append({"method": method, "url": url, "headers": dict(headers),
                              "body": body})
        if "/app-manifests/" in url:
            return HttpAnswer(201, {}, json.dumps(self.conversion).encode())
        if url.endswith("/app/installations"):
            return HttpAnswer(200, {}, json.dumps(self.installations).encode())
        if url.endswith("/access_tokens") and "/app/installations/" in url:
            self.minted.append(f"ghs_{secrets.token_hex(18)}")
            return HttpAnswer(201, {}, json.dumps({"token": self.minted[-1]}).encode())
        if "/installation/repositories" in url:
            rows = [{"full_name": name} for name in self.repositories]
            return HttpAnswer(200, {}, json.dumps({"total_count": len(rows),
                                                   "repositories": rows}).encode())
        if url.endswith("/app/hook/config"):
            return HttpAnswer(200, {}, b'{"url": "x", "content_type": "json"}')
        return HttpAnswer(404, {}, b"{}")


class FakePool:
    """The broker's sign-in pair, recorded (the faked broker)."""

    def __init__(self) -> None:
        self.begun: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []

    def begin_sign_in(self, *, owner_tenant, label, provider, lend_to):
        state = secrets.token_urlsafe(32)
        self.begun.append({"owner_tenant": owner_tenant, "label": label, "state": state})
        return {"url": "https://claude.example/authorize", "state": state}

    def finish_sign_in(self, *, state, code, expected_owner):
        pending = next(b for b in self.begun if b["state"] == state)
        if pending["owner_tenant"] != expected_owner:
            raise AssertionError("expected_owner differs from the pending owner")
        account = {"account_id": f"{pending['owner_tenant']}:{pending['label']}",
                   "owner_tenant": pending["owner_tenant"], "label": pending["label"]}
        self.finished.append(account)
        self.accounts.append(account)
        return {"account": account, "expires_at": None}

    def list_accounts(self, tenant_id):
        return {"accounts": [a for a in self.accounts if a["owner_tenant"] == tenant_id]}


class FakeRuns:
    """`setup.TeamRuns`: the first team's runs, recorded, states scripted."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.workflows: list[dict[str, Any]] = []
        self.states: dict[str, str] = {}

    def submit_task(self, *, tenant_id, group, repository, prompt, label):
        task_id = f"task-{len(self.tasks) + 1}"
        self.tasks.append({"id": task_id, "tenant": tenant_id, "group": group,
                           "repository": repository, "prompt": prompt, "label": label})
        self.states[task_id] = "running"
        return task_id

    def submit_workflow(self, *, tenant_id, group, repository, label):
        workflow_id = f"wf-{len(self.workflows) + 1}"
        self.workflows.append({"id": workflow_id, "tenant": tenant_id, "group": group,
                               "repository": repository, "label": label})
        return workflow_id

    def task_state(self, *, tenant_id, task_id):
        return self.states[task_id]


@pytest.fixture
def runs(setup_app) -> FakeRuns:
    """The app's own fake runs (built by `setup_app`, so a file that imports
    `setup_app` alone needs no further fixture)."""
    return setup_app.state.setup_runs


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        ROOT: (ADMIN_GROUP, ENG_GROUP),
        ALICE: (ENG_GROUP, TEAM_GROUP),
        CAROL: (),
        WORKSPACE_ADMIN: (TEAM_GROUP,),
    }


@pytest.fixture
def tokens(group_map) -> dict[str, dict[str, Any]]:
    found = {
        f"token-{email.split('@')[0]}": {"email": email, "email_verified": True,
                                         "sub": f"sub-{email}", "hd": "example.com"}
        for email in group_map
    }
    for name, email in (("deployer", DEPLOYER), ("doctor", DOCTOR)):
        found[f"token-{name}"] = {"email": email, "email_verified": True,
                                  "sub": f"sub-{name}"}
    return found


@pytest.fixture
def directory(group_map) -> RecordingDirectory:
    return RecordingDirectory(group_map)


@pytest.fixture
def fake_secrets() -> FakeSecrets:
    return FakeSecrets()


@pytest.fixture
def github() -> FakeGitHubSend:
    return FakeGitHubSend()


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def setup_app(db, tokens, group_map, objects, directory, fake_secrets, github, pool):
    signer = InProcessSigner()
    context = build_context(
        settings=api_settings(
            platform_owner=ROOT, settings_key=SETTINGS_KEY, spec_signing_key_version=SIGNING_VERSION,
            spec_verify_keys=signer.verify_keys(), console_url=f"https://{HOSTNAME}",
            setup_github_org=ORG, setup_github_hooks_url=HOOKS_URL,
            # Read the workspace registry on every request, so a team the
            # workspace job finishes mid-test is selectable at once.
            workspace_registry_cache_seconds=0.0),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(context)
    app.state.workspace_publisher = FakePublisher()
    app.state.workspace_directory = directory
    app.state.workspace_approval_signer = signer
    app.state.setup_secrets = fake_secrets
    app.state.setup_github_api = GitHubSetupApi(send=github)
    app.state.account_pool = pool
    app.state.setup_runs = FakeRuns()
    return app


@pytest.fixture
def setup_client(setup_app) -> TestClient:
    return TestClient(setup_app, raise_server_exceptions=False)


def seed_engine_document(db) -> dict[str, Any]:
    doc = {
        "schema_version": 1, "release": "1.0.0", "region": "us-central1",
        "hostname": HOSTNAME, "stages": [None] * 16,
        "lease": {"holder": "laptop", "heartbeat": datetime.now(timezone.utc),
                  "expires": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "hand_off_at": "2026-10-29T12:00:00Z", "ready_at": None,
    }
    db.docs[ENGINE_DOC] = dict(doc)
    return doc


def create_team(client: TestClient, group: str = TEAM_GROUP, **extra: Any):
    return client.post("/v1/admin/setup/teams", json={"group": group, **extra},
                       headers=auth_header("root"))


def _call(client: TestClient, method: str, path: str, body: Any, headers: dict[str, str]):
    return client.request(method, path, json=body, headers=headers)


# --------------------------------------------------------------------------


def test_platform_admin_only(setup_client, db) -> None:
    """A workspace admin, a member and a person with no workspace each get 403
    on every route, and nothing is written under `swarm-install/`."""
    response = create_team(setup_client, first_admin=WORKSPACE_ADMIN)
    assert response.status_code in (200, 202), response.text
    assert response.json()["first_admin"]["granted"] is True, response.json()
    before = {k: v for k, v in db.docs.items() if k.startswith("swarm-install/")}
    for user in ("wadmin", "alice", "carol"):
        for method, path, body in ROUTES:
            answer = _call(setup_client, method, path, body, auth_header(user))
            assert answer.status_code == 403, (user, method, path, answer.text)
    after = {k: v for k, v in db.docs.items() if k.startswith("swarm-install/")}
    assert after == before


def test_a_platform_admin_reads_the_page(setup_client, db) -> None:
    seed_engine_document(db)
    response = setup_client.get("/v1/admin/setup", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"]["release"] == "1.0.0"
    assert body["cli_running"] is True
    assert {a["name"] for a in body["addons"]} >= {"tailscale", "ci"}


def test_reads_engine_document_never_writes_it(setup_client, db, github) -> None:
    """Through the whole console flow, the engine's document is byte-for-byte
    what the engine wrote."""
    written = seed_engine_document(db)
    headers = auth_header("root")
    assert setup_client.get("/v1/admin/setup", headers=headers).status_code == 200
    start = setup_client.post("/v1/admin/setup/github-app/manifest", json={}, headers=headers)
    assert start.status_code == 200, start.text
    assert create_team(setup_client).status_code in (200, 202)
    setup_client.post("/v1/admin/setup/links", headers=headers,
                      json={"kind": "pool-account", "label": "pool-1", "owner": ALICE})
    setup_client.put("/v1/admin/setup/health", headers=auth_header("doctor"),
                     json={"run_at": "2026-10-29T12:00:00Z"})
    setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert db.docs[ENGINE_DOC] == written


def test_groups_probe_answers_with_api_identity(setup_client, directory) -> None:
    """The doctor's and stage 8's re-probe asks swarm-api's own directory, one
    question per registered group, and answers met/unmet per group -- never a
    member list."""
    directory.unreadable.add(ENG_GROUP)
    for user in ("doctor", "deployer", "root"):
        directory.asked.clear()
        response = setup_client.post("/v1/admin/setup/health/groups-probe",
                                     headers=auth_header(user))
        assert response.status_code == 200, (user, response.text)
        body = response.json()
        states = {row["group"]: row["state"] for row in body["groups"]}
        assert states[ENG_GROUP] == "unmet"
        assert states["research@example.com"] == "met"
        assert body["met"] is False
        assert all(len(groups) == 1 for _, groups in directory.asked)
        assert {g for _, (g,) in directory.asked} >= {ENG_GROUP, "research@example.com"}
        assert set().union(*(row.keys() for row in body["groups"])) <= {
            "group", "state", "reason"}


def test_engine_identity_admitted_only_on_hook_and_probe(setup_client, fake_secrets,
                                                         db) -> None:
    """`swarm-tf-deployer@<project>` gets through on the hook and the groups
    probe, and 403 on every other route (review-fix finding 2)."""
    db.docs[f"swarm-install/{ENV}/console/github_app"] = {"id": 4242, "slug": "swarmcloud"}
    fake_secrets.add_version("swarm-github-app-webhook-secret", secrets.token_hex(16))
    admitted = {("POST", "/v1/admin/setup/github-app/hook"),
                ("POST", "/v1/admin/setup/health/groups-probe")}
    for method, path, body in ROUTES:
        answer = _call(setup_client, method, path, body, auth_header("deployer"))
        if (method, path) in admitted:
            assert answer.status_code == 200, (method, path, answer.text)
        else:
            assert answer.status_code == 403, (method, path, answer.text)


def test_doctor_alone_writes_health(setup_client, db) -> None:
    report = {"run_at": "2026-10-29T12:00:00Z", "checks": [{"id": "api.run", "result": "met"}],
              "findings": []}
    refused = setup_client.put("/v1/admin/setup/health", json=report,
                               headers=auth_header("root"))
    assert refused.status_code == 403
    written = setup_client.put("/v1/admin/setup/health", json=report,
                               headers=auth_header("doctor"))
    assert written.status_code == 200, written.text
    assert db.docs[f"swarm-install/{ENV}/health/latest"]["checks"] == report["checks"]
    read = setup_client.get("/v1/admin/setup/health", headers=auth_header("root"))
    assert read.json()["latest"]["checks"] == report["checks"]
    for method, path, body in ROUTES:
        if (method, path) in {("PUT", "/v1/admin/setup/health"),
                              ("POST", "/v1/admin/setup/health/groups-probe")}:
            continue
        answer = _call(setup_client, method, path, body, auth_header("doctor"))
        assert answer.status_code == 403, (method, path)


def test_a_team_writes_its_workspace_and_team_document(setup_client, db) -> None:
    response = create_team(setup_client, first_admin=WORKSPACE_ADMIN,
                           invites=["Bob@example.com"])
    assert response.status_code == 202, response.text
    team_id = response.json()["team"]
    assert db.docs[f"workspaces/{team_id}"]["kind"] == "group"
    team = db.docs[f"swarm-install/{ENV}/console/teams/{team_id}"]
    assert team["group"] == TEAM_GROUP
    assert team["workspace"] == team_id
    assert team["first_admin"] == WORKSPACE_ADMIN
    assert team["invites"] == ["bob@example.com"]


class PassingAppCheck:
    """`ForgeApp.check_app_key`'s answer shape, passing."""

    def check_app_key(self) -> dict[str, Any]:
        return {"ok": True, "app_id": "4242", "slug": "swarmcloud", "reason": None}


def install_app(db, github, repositories: tuple[str, ...] = (f"{ORG}/demo", f"{ORG}/api")):
    """The App created and installed on ORG, covering `repositories`."""
    db.docs[f"swarm-install/{ENV}/console/github_app"] = {"id": 4242, "installed_on": ORG}
    github.installations = [{"id": 77, "account": {"login": ORG},
                             "repository_selection": "selected"}]
    github.repositories = list(repositories)


def test_finish_waits_for_every_console_step(setup_app, setup_client, db, github, pool,
                                             runs) -> None:
    """`console/finished` is written only once the App is installed and
    self-checked, the first team exists, a pool account signed in to it and
    the first team's smoke passed."""
    headers = auth_header("root")
    early = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert early.status_code == 409
    assert set(early.json()["detail"]["missing"]) == {
        "github_app_installed", "github_app_self_check", "first_team", "pool_account_signed_in",
        "first_team_smoke"}
    setup_app.state.forge_app = PassingAppCheck()
    install_app(db, github)
    assert setup_client.post("/v1/admin/setup/github-app/self-check",
                             headers=headers).json()["ok"] is True
    team = create_team(setup_client).json()["team"]
    assert setup_client.post("/v1/admin/setup/links", headers=headers, json={
        "kind": "pool-account", "label": "pool-1", "owner": ALICE}).status_code == 201
    waiting = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert waiting.json()["detail"]["missing"] == ["pool_account_signed_in", "first_team_smoke"]
    assert f"swarm-install/{ENV}/console/finished" not in db.docs
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    smoking = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert smoking.status_code == 409
    assert smoking.json()["detail"]["missing"] == ["first_team_smoke"]
    assert f"swarm-install/{ENV}/console/finished" not in db.docs
    runs.states["task-1"] = "succeeded"
    done = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert done.status_code == 200, done.text
    assert db.docs[f"swarm-install/{ENV}/console/finished"]["by"] == ROOT
    assert db.docs[f"swarm-install/{ENV}/console/accounts"]["pool-1"]["signed_in_at"]


# --------------------------------------------------------------------------
# the first team, live (lane DAYONE-SETUP-API-2)
# --------------------------------------------------------------------------


def team_doc(db, team: str) -> dict[str, Any]:
    return db.docs[f"swarm-install/{ENV}/console/teams/{team}"]


def page_team(client: TestClient, team: str) -> dict[str, Any]:
    response = client.get("/v1/admin/setup", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["teams"] if row["id"] == team)


def test_first_team_is_granted_the_installations_repositories(setup_client, db,
                                                               github) -> None:
    """Decision 3: creating the first team records a team-level grant over the
    repositories the App installation covers, shown on the page; the
    installation token it read them with is in no answer and no document."""
    install_app(db, github)
    response = create_team(setup_client)
    assert response.status_code == 202, response.text
    team = response.json()["team"]
    grant = response.json()["grant"]
    assert grant["state"] == "granted"
    assert grant["repositories"] == [f"{ORG}/api", f"{ORG}/demo"]
    assert grant["installation_id"] == 77 and grant["mode"] == "write"
    assert team_doc(db, team)["grant"]["repositories"] == grant["repositories"]
    assert page_team(setup_client, team)["grant"]["state"] == "granted"
    assert github.minted, "no installation token was minted"
    stored = json.dumps({k: v for k, v in db.docs.items()}, default=str)
    for token in github.minted:
        assert token not in stored and token not in response.text


def test_grant_waits_for_the_app_and_lands_on_a_later_read(setup_client, db, github) -> None:
    team = create_team(setup_client).json()["team"]
    assert team_doc(db, team)["grant"]["state"] == "waiting_for_app"
    install_app(db, github)
    assert page_team(setup_client, team)["grant"]["state"] == "granted"
    assert team_doc(db, team)["grant"]["repositories"] == [f"{ORG}/api", f"{ORG}/demo"]


def test_a_second_team_gets_no_grant(setup_client, db, github) -> None:
    install_app(db, github)
    create_team(setup_client)
    second = create_team(setup_client, group="second-team@example.com")
    assert second.status_code in (200, 202), second.text
    assert second.json()["grant"] is None
    assert "grant" not in team_doc(db, second.json()["team"])


def test_the_team_row_is_read_live(setup_client, db, directory, pool) -> None:
    """The row is not the creation snapshot: the workspace's state now, each
    invitee's membership per the Groups API now, the team's pool accounts."""
    team = create_team(setup_client, invites=["bob@example.com"]).json()["team"]
    row = page_team(setup_client, team)
    assert row["members"] == [{"email": "bob@example.com", "member": False}]
    assert row["pool_accounts"] == []
    db.docs[f"workspaces/{team}"]["state"] = "ready"
    directory.join("bob@example.com", TEAM_GROUP)
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    row = page_team(setup_client, team)
    assert row["workspace_state"] == "ready"
    assert row["members"] == [{"email": "bob@example.com", "member": True}]
    assert row["pool_accounts"] == ["pool-1"]


def test_an_unanswered_membership_is_unknown_not_absent(setup_client, directory) -> None:
    team = create_team(setup_client, invites=["bob@example.com"]).json()["team"]
    directory.unreadable.add(TEAM_GROUP)
    assert page_team(setup_client, team)["members"] == [
        {"email": "bob@example.com", "member": None}]


def test_an_invite_answers_the_google_admin_members_page(setup_client, db, directory) -> None:
    """Decision 4: no Groups admin grant. The invite is recorded and answered
    with the group's members page in Google Admin; the row polls the Groups
    API until the invitee is in."""
    created = create_team(setup_client, invites=["bob@example.com"]).json()
    team = created["team"]
    members_page = "https://admin.google.com/ac/groups/0idplatformteam/members"
    assert created["invites"] == [{"email": "bob@example.com", "member": False,
                                   "add_members_url": members_page}]
    answer = setup_client.post(f"/v1/admin/setup/teams/{team}/invites",
                               json={"email": "Dana@example.com"}, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["add_members_url"] == members_page
    assert answer.json()["member"] is False
    assert team_doc(db, team)["invites"] == ["bob@example.com", "dana@example.com"]
    directory.join("dana@example.com", TEAM_GROUP)
    members = {m["email"]: m["member"] for m in page_team(setup_client, team)["members"]}
    assert members == {"bob@example.com": False, "dana@example.com": True}
    missing = setup_client.post("/v1/admin/setup/teams/nobody/invites",
                                json={"email": "dana@example.com"}, headers=auth_header("root"))
    assert missing.status_code == 404


def test_smoke_runs_when_the_first_pool_account_lands(setup_client, db, github, pool,
                                                      runs) -> None:
    """Stage 12: no smoke before a pool account; once one lands, ONE small
    task on a granted repository, as the first team, recorded on
    `teams/<id>.smoke` and read live until it ends."""
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    assert page_team(setup_client, team).get("smoke") is None
    assert runs.tasks == []
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    smoke = page_team(setup_client, team)["smoke"]
    assert smoke["state"] == "running" and smoke["task_id"] == "task-1"
    assert runs.tasks == [{"id": "task-1", "tenant": team, "group": TEAM_GROUP,
                           "repository": f"{ORG}/api", "prompt": runs.tasks[0]["prompt"],
                           "label": "swarm-install-smoke"}]
    page_team(setup_client, team)
    assert len(runs.tasks) == 1, "a second read started a second smoke"
    runs.states["task-1"] = "succeeded"
    assert page_team(setup_client, team)["smoke"]["state"] == "succeeded"
    assert team_doc(db, team)["smoke"]["state"] == "succeeded"


def test_a_failed_smoke_starts_again_only_on_request(setup_client, db, github, pool,
                                                     runs) -> None:
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    page_team(setup_client, team)
    runs.states["task-1"] = "failed"
    assert page_team(setup_client, team)["smoke"]["state"] == "failed"
    page_team(setup_client, team)
    assert len(runs.tasks) == 1
    again = setup_client.post("/v1/admin/setup/smoke", headers=auth_header("root"))
    assert again.status_code == 200, again.text
    assert again.json()["smoke"] == {**again.json()["smoke"], "state": "running",
                                     "task_id": "task-2"}


def test_verify_runs_start_as_the_first_team_and_are_recorded(setup_client, db, github,
                                                              runs) -> None:
    """The console's two verify runs, one task and one workflow, as the first
    team on a granted repository; their ids are recorded as `verify_runs`
    (the INTERFACE SHEET's keys) for the engine, and answered."""
    early = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert early.status_code == 409
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    answer = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    expected = {"console_task": "task-1", "console_workflow": "wf-1"}
    assert answer.json()["verify_runs"] == expected
    doc = db.docs[f"swarm-install/{ENV}/console/verify_runs"]
    assert doc["verify_runs"] == expected and doc["repository"] == f"{ORG}/api"
    assert runs.tasks[0]["tenant"] == team and runs.workflows[0]["tenant"] == team
    assert setup_client.get("/v1/admin/setup",
                            headers=auth_header("root")).json()["verify_runs"]["verify_runs"] \
        == expected
    again = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert again.json() == {**again.json(), "verify_runs": expected, "started": False}
    assert len(runs.tasks) == 1 and len(runs.workflows) == 1


def test_verify_runs_wait_for_a_granted_repository(setup_client, db, github) -> None:
    install_app(db, github, repositories=())
    create_team(setup_client)
    answer = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert answer.status_code == 409
    assert f"swarm-install/{ENV}/console/verify_runs" not in db.docs


class _Submitted:
    def __init__(self, ident: str) -> None:
        self.id = ident
        self.workflow_id = ident


class RecordingSubmissions:
    """`SubmissionService`'s two submit calls, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    def submit_tasks(self, auth, specs):
        self.calls.append(("task", auth, specs))
        return type("Result", (), {"tasks": [_Submitted("t-1")]})()

    def submit_workflow(self, auth, spec):
        self.calls.append(("workflow", auth, spec))
        return type("Submission", (), {"workflow": _Submitted("w-1")})()


def test_team_runs_submit_as_the_team_never_as_a_person() -> None:
    """Decision 3, lesson 43: the runs are submitted in the team's tenant as
    swarm-api's own service account, a service submitter, so the existing
    submission path runs them on the team's own slot and never on the
    admin's (or anyone's) GitHub connection."""
    from swarm_api.routes import setup as setup_routes
    from swarm_api.validation import is_service_submitter

    submissions = RecordingSubmissions()
    ctx = type("Ctx", (), {"submissions": submissions,
                           "settings": api_settings()})()
    team_runs = setup_routes.TeamRuns(ctx)
    assert team_runs.submit_task(tenant_id="platform-team", group=TEAM_GROUP,
                                 repository=f"{ORG}/demo", prompt="p", label="l") == "t-1"
    assert team_runs.submit_workflow(tenant_id="platform-team", group=TEAM_GROUP,
                                     repository=f"{ORG}/demo", label="l") == "w-1"
    for kind, auth, spec in submissions.calls:
        assert auth.email == f"swarm-api@{PROJECT}.iam.gserviceaccount.com"
        assert is_service_submitter(auth.email)
        assert auth.tenant_id == "platform-team" and auth.tenant_principal == TEAM_GROUP
        assert auth.is_admin is False
        found = spec[0] if kind == "task" else spec
        assert found.repository_url == f"https://github.com/{ORG}/demo"
    assert [kind for kind, _, _ in submissions.calls] == ["task", "workflow"]
    assert [s.step_id for s in submissions.calls[1][2].steps] == ["write", "open"]
