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
from swarm_api.routes import setup as setup_routes
from swarm_api.setup_github import GitHubSetupApi
from swarm_api.specsigning import SpecSignature
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, NoForgeTokens, api_settings, auth_header
from .fakes import FakeFirestore

ROOT = "root@example.com"
ALICE = "alice@example.com"
CAROL = "carol@example.com"
WORKSPACE_ADMIN = "wadmin@example.com"
TEAM_GROUP = "platform-team@example.com"
SECOND_TEAM = "second-team@example.com"
DEPLOYER = f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"
DOCTOR = f"swarm-doctor@{PROJECT}.iam.gserviceaccount.com"
ENV = "test"
ENGINE_DOC = f"swarm-install/{ENV}"
#: Where swarm-api keeps the team documents: a collection under the env
#: document, so a path real Firestore accepts (audit 3, A2).
TEAMS_PREFIX = f"swarm-install/{ENV}/teams/"
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
        #: `GET /users/<login>`'s `type` by login; any other login is an
        #: organisation. `users_status` other than 200 is GitHub refusing.
        self.account_types: dict[str, str] = {}
        self.users_status = 200

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
        if url.startswith("https://api.github.com/users/"):
            if self.users_status != 200:
                return HttpAnswer(self.users_status, {}, b"{}")
            login = url.rsplit("/", 1)[1]
            kind = self.account_types.get(login, "Organization")
            return HttpAnswer(200, {}, json.dumps({"login": login, "type": kind}).encode())
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

    def submit_task(self, *, tenant_id, group, repository, prompt, label,
                    strategy=setup_routes.SMOKE_STRATEGY):
        task_id = f"task-{len(self.tasks) + 1}"
        self.tasks.append({"id": task_id, "tenant": tenant_id, "group": group,
                           "repository": repository, "prompt": prompt, "label": label,
                           "strategy": strategy})
        self.states[task_id] = "running"
        return task_id

    def submit_workflow(self, *, tenant_id, group, repository, label):
        workflow_id = f"wf-{len(self.workflows) + 1}"
        self.workflows.append({"id": workflow_id, "tenant": tenant_id, "group": group,
                               "repository": repository, "label": label})
        self.states[workflow_id] = "running"
        return workflow_id

    def task_state(self, *, tenant_id, task_id):
        return self.states[task_id]

    def workflow_state(self, *, tenant_id, workflow_id):
        return self.states[workflow_id]


class PathCheckingFirestore(FakeFirestore):
    """The shared fake, refusing what google-cloud-firestore's `verify_path`
    refuses: a collection path has an odd number of segments, a document
    path an even one (audit 3, A2: `console/teams` passed the shared fake and
    500'd on the real client). Only this module's routes run on it."""

    @staticmethod
    def _check(path: str, *, collection: bool) -> None:
        parts = path.split("/")
        if not all(parts) or (len(parts) % 2 == 1) != collection:
            kind = "collection" if collection else "document"
            raise ValueError(f"A {kind} must have an {'odd' if collection else 'even'} "
                             f"number of path elements: {path!r}")

    def collection(self, path: str):
        self._check(path, collection=True)
        return super().collection(path)

    def document(self, path: str):
        self._check(path, collection=False)
        return super().document(path)


@pytest.fixture
def db() -> PathCheckingFirestore:
    return PathCheckingFirestore()


@pytest.fixture
def runs(setup_app) -> FakeRuns:
    """The app's own fake runs (built by `setup_app`, so a file that imports
    `setup_app` alone needs no further fixture)."""
    return setup_app.state.setup_runs


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        # The installer (the platform owner) is in the teams it creates: the
        # first team's check refuses a group they are not in (finding 27).
        ROOT: (ADMIN_GROUP, ENG_GROUP, TEAM_GROUP, SECOND_TEAM),
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
        # The real submission path signs each run's spec: a test that sends
        # `TeamRuns` through it (`setup_runs = None`) needs the key.
        signer=signer,
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
            "group", "state", "reason", "detail"}


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
    team = db.docs[f"{TEAMS_PREFIX}{team_id}"]
    assert team["group"] == TEAM_GROUP
    assert team["workspace"] == team_id
    assert team["first_admin"] == WORKSPACE_ADMIN
    assert team["invites"] == ["bob@example.com"]


class PassingAppCheck:
    """`ForgeApp.check_app_key`'s answer shape, passing."""

    def check_app_key(self) -> dict[str, Any]:
        return {"ok": True, "app_id": "4242", "slug": "swarmcloud", "reason": None}


def install_app(db, github, repositories: tuple[str, ...] = (f"{ORG}/demo", f"{ORG}/api")):
    """The App created and installed on ORG, covering `repositories`, and its
    self-check passed (infra pass 3 has landed)."""
    db.docs[f"swarm-install/{ENV}/console/github_app"] = {"id": 4242, "installed_on": ORG,
                                                          "self_check": {"ok": True}}
    github.installations = [{"id": 77, "account": {"login": ORG, "type": "Organization"},
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
    return db.docs[f"{TEAMS_PREFIX}{team}"]


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
    second = create_team(setup_client, group=SECOND_TEAM)
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
                           "label": "swarm-install-smoke", "strategy": "collect"}]
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


VERIFY_DOC = f"swarm-install/{ENV}/console/verify_runs"


def start_verify(client: TestClient):
    return client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))


def verify_started(setup_client, db, github) -> str:
    """The first team, and its two console verify runs started: task-1, wf-1."""
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    assert start_verify(setup_client).status_code == 200
    return team


@pytest.mark.parametrize("raw", ["FAILED", "CANCELLED"])
def test_a_failed_verify_task_starts_again_and_keeps_the_workflow(setup_client, db, github,
                                                                  runs, raw) -> None:
    """BGD, 2026-10-30: "swarm-api restarts a failed key". A recorded task
    that failed or was cancelled counts as missing: a fresh task replaces its
    id and the running workflow is kept."""
    from swarm_api.routes.setup import run_state

    team = verify_started(setup_client, db, github)
    runs.states["task-1"] = run_state(raw)
    answer = start_verify(setup_client)
    assert answer.status_code == 200, answer.text
    expected = {"console_task": "task-2", "console_workflow": "wf-1"}
    assert answer.json()["verify_runs"] == expected and answer.json()["started"] is True
    assert answer.json()["retries"] == {"console_task": 1, "console_workflow": 0}
    assert db.docs[VERIFY_DOC]["verify_runs"] == expected
    assert db.docs[VERIFY_DOC]["retries"] == {"console_task": 1, "console_workflow": 0}
    assert len(runs.tasks) == 2 and len(runs.workflows) == 1
    assert runs.tasks[1]["tenant"] == team and runs.tasks[1]["label"] == "swarm-install-verify"


def test_a_failed_verify_workflow_starts_again_and_keeps_the_task(setup_client, db, github,
                                                                  runs) -> None:
    verify_started(setup_client, db, github)
    runs.states["wf-1"] = "failed"
    answer = start_verify(setup_client)
    assert answer.status_code == 200, answer.text
    expected = {"console_task": "task-1", "console_workflow": "wf-2"}
    assert answer.json()["verify_runs"] == expected
    assert db.docs[VERIFY_DOC]["verify_runs"] == expected
    assert db.docs[VERIFY_DOC]["retries"] == {"console_task": 0, "console_workflow": 1}
    assert len(runs.tasks) == 1 and len(runs.workflows) == 2


@pytest.mark.parametrize("state", ["running", "succeeded"])
def test_a_going_or_succeeded_verify_run_is_left_alone(setup_client, db, github, runs,
                                                       state) -> None:
    verify_started(setup_client, db, github)
    runs.states["task-1"] = state
    runs.states["wf-1"] = state
    answer = start_verify(setup_client)
    assert answer.status_code == 200, answer.text
    assert answer.json()["verify_runs"] == {"console_task": "task-1", "console_workflow": "wf-1"}
    assert answer.json()["started"] is False
    assert db.docs[VERIFY_DOC]["verify_runs"] == {"console_task": "task-1",
                                                  "console_workflow": "wf-1"}
    assert len(runs.tasks) == 1 and len(runs.workflows) == 1


def test_a_verify_run_that_spent_its_retries_starts_nothing(setup_client, db, github,
                                                            runs) -> None:
    """Two fresh starts per key; the third failure is answered with why, and
    nothing is started, not even the other key's failed run."""
    verify_started(setup_client, db, github)
    for retry in (2, 3):
        runs.states[f"task-{retry - 1}"] = "failed"
        assert start_verify(setup_client).json()["verify_runs"]["console_task"] == f"task-{retry}"
    runs.states["task-3"] = "failed"
    runs.states["wf-1"] = "failed"
    answer = start_verify(setup_client)
    assert answer.status_code == 409, answer.text
    assert "task-3" in answer.text and "2 fresh starts" in answer.text
    assert len(runs.tasks) == 3 and len(runs.workflows) == 1
    assert db.docs[VERIFY_DOC]["verify_runs"] == {"console_task": "task-3",
                                                  "console_workflow": "wf-1"}
    assert db.docs[VERIFY_DOC]["retries"] == {"console_task": 2, "console_workflow": 0}


def finished_install(setup_client, db, github, pool, runs) -> str:
    """Every console step done and `finish` posted: the App, the first team,
    its pool account signed in and its smoke passed. Answers the team."""
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    db.docs[f"swarm-install/{ENV}/console/accounts"] = {"pool-1": {"owner": ALICE}}
    assert setup_client.post("/v1/admin/setup/finish",
                             headers=auth_header("root")).status_code == 409
    runs.states["task-1"] = "succeeded"
    return team


def test_finish_starts_the_console_verify_runs(setup_client, db, github, pool, runs) -> None:
    """Audit 5b, B1: nothing tells the person to press a button, and swarm
    install's verify waits for the console's two runs; `finish` starts them
    itself, as the first team, and answers their ids. Again, it starts none."""
    team = finished_install(setup_client, db, github, pool, runs)
    done = setup_client.post("/v1/admin/setup/finish", headers=auth_header("root"))
    assert done.status_code == 200, done.text
    expected = {"console_task": "task-2", "console_workflow": "wf-1"}
    assert db.docs[VERIFY_DOC]["verify_runs"] == expected
    assert done.json()["verify_runs"]["verify_runs"] == expected
    assert runs.tasks[1]["tenant"] == team and runs.tasks[1]["label"] == "swarm-install-verify"
    again = setup_client.post("/v1/admin/setup/finish", headers=auth_header("root"))
    assert again.status_code == 200 and again.json()["verify_runs"]["started"] is False
    assert len(runs.tasks) == 2 and len(runs.workflows) == 1


def test_after_finish_each_read_restarts_a_failed_verify_run(setup_client, db, github, pool,
                                                             runs) -> None:
    """Audit 5b, B1: once handed over, the page's read restarts a failed key,
    up to MAX_VERIFY_RETRIES, then starts nothing and still answers."""
    finished_install(setup_client, db, github, pool, runs)
    assert setup_client.post("/v1/admin/setup/finish",
                             headers=auth_header("root")).status_code == 200
    for retry in (3, 4):
        runs.states[f"task-{retry - 1}"] = "failed"
        view = setup_client.get("/v1/admin/setup", headers=auth_header("root"))
        assert view.status_code == 200, view.text
        assert view.json()["verify_runs"]["verify_runs"]["console_task"] == f"task-{retry}"
    assert db.docs[VERIFY_DOC]["retries"] == {"console_task": 2, "console_workflow": 0}
    runs.states["task-4"] = "failed"
    view = setup_client.get("/v1/admin/setup", headers=auth_header("root"))
    assert view.status_code == 200, view.text
    assert view.json()["verify_runs"]["verify_runs"]["console_task"] == "task-4"
    assert len(runs.tasks) == 4 and len(runs.workflows) == 1


def test_finish_starts_verify_runs_it_could_not_start_on_the_next_read(
        setup_client, db, github, pool, runs) -> None:
    """A `console/finished` written before `finish` started the runs (an older
    swarm-api) gets them on the page's next read; before finish, a read
    starts nothing."""
    finished_install(setup_client, db, github, pool, runs)
    setup_client.get("/v1/admin/setup", headers=auth_header("root"))
    assert VERIFY_DOC not in db.docs
    db.docs[f"swarm-install/{ENV}/console/finished"] = {"at": "2026-10-30T00:00:00Z", "by": ROOT}
    view = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()
    assert view["verify_runs"]["verify_runs"] == {"console_task": "task-2",
                                                  "console_workflow": "wf-1"}


def test_the_team_row_keeps_the_members_page_and_the_workspace_id(setup_client, db,
                                                                   directory) -> None:
    """Audit 5b, M5 and M6: every team row serves its group's members page
    (the invites made with the team get their link too) and its workspace's
    `w-` id, which the workspace retry route takes."""
    team = create_team(setup_client, invites=["bob@example.com"]).json()["team"]
    row = page_team(setup_client, team)
    assert row["add_members_url"] == "https://admin.google.com/ac/groups/0idplatformteam/members"
    assert row["workspace_id"] == db.docs[f"workspaces/{team}"]["workspace_id"]
    assert row["workspace_id"].startswith("w-")


def test_a_failed_first_team_workspace_is_retried_by_its_workspace_id(setup_client, db) -> None:
    """Audit 5b, M6: asking for the team again answers 409 WORKSPACE_FAILED;
    the row's `workspace_id` retried through the workspace route re-runs it."""
    team = create_team(setup_client).json()["team"]
    db.docs[f"workspaces/{team}"]["state"] = "failed"
    again = create_team(setup_client)
    assert again.status_code == 409 and "WORKSPACE_FAILED" in again.text
    row = page_team(setup_client, team)
    assert row["workspace_state"] == "failed"
    retried = setup_client.post(f"/v1/admin/workspaces/{row['workspace_id']}/retry",
                                headers=auth_header("root"))
    assert retried.status_code == 200, retried.text
    assert page_team(setup_client, team)["workspace_state"] == "approved"


def test_verify_runs_wait_for_a_granted_repository(setup_client, db, github) -> None:
    install_app(db, github, repositories=())
    create_team(setup_client)
    answer = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert answer.status_code == 409
    assert f"swarm-install/{ENV}/console/verify_runs" not in db.docs


def test_an_installation_with_no_repository_is_read_again_until_one_lands(
        setup_client, db, github, pool, runs) -> None:
    """An installation that covers no repository yet (a new organisation) is
    not a grant: the page reads it again, and once a repository is added the
    grant lands and the smoke starts."""
    install_app(db, github, repositories=())
    team = create_team(setup_client).json()["team"]
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    row = page_team(setup_client, team)
    assert row["grant"]["state"] == "waiting_for_repositories"
    assert row.get("smoke") is None and runs.tasks == []
    github.repositories = [f"{ORG}/demo"]
    row = page_team(setup_client, team)
    assert row["grant"]["state"] == "granted"
    assert team_doc(db, team)["grant"]["repositories"] == [f"{ORG}/demo"]
    assert row["smoke"]["state"] == "running"
    assert runs.tasks[0]["repository"] == f"{ORG}/demo"


class RecordingSubmissions:
    """`SubmissionService`'s two submit calls, recorded and then handed to
    the REAL service, so a spec it refuses is refused here too (audit 4d,
    F1/F2: a recorder that accepted any spec hid a workflow every real
    submission answered 422)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[tuple[str, Any, Any]] = []

    def submit_tasks(self, auth, specs):
        self.calls.append(("task", auth, specs))
        return self.real.submit_tasks(auth, specs)

    def submit_workflow(self, auth, spec):
        self.calls.append(("workflow", auth, spec))
        return self.real.submit_workflow(auth, spec)


def test_team_runs_submit_as_the_team_never_as_a_person(setup_app, setup_client, db,
                                                        github) -> None:
    """Decision 3, lesson 43: the runs are submitted in the team's tenant as
    swarm-api's own service account, a service submitter, so the existing
    submission path runs them on the team's own slot and never on the
    admin's (or anyone's) GitHub connection."""
    from swarm_api.validation import is_service_submitter

    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    real = setup_app.state.ctx
    submissions = RecordingSubmissions(real.submissions)
    ctx = type("Ctx", (), {"submissions": submissions, "settings": real.settings})()
    team_runs = setup_routes.TeamRuns(ctx)
    assert team_runs.submit_task(tenant_id=team, group=TEAM_GROUP,
                                 repository=f"{ORG}/demo", prompt="p", label="l")
    assert team_runs.submit_workflow(tenant_id=team, group=TEAM_GROUP,
                                     repository=f"{ORG}/demo", label="l")
    for kind, auth, spec in submissions.calls:
        assert auth.email == f"swarm-api@{PROJECT}.iam.gserviceaccount.com"
        assert is_service_submitter(auth.email)
        assert auth.tenant_id == team and auth.tenant_principal == TEAM_GROUP
        assert auth.is_admin is False
        found = spec[0] if kind == "task" else spec
        assert found.repository_url == f"https://github.com/{ORG}/demo"
    assert [kind for kind, _, _ in submissions.calls] == ["task", "workflow"]
    assert [s.step_id for s in submissions.calls[1][2].steps] == ["write", "open"]


def _stored_dispatch(db, task_id: str) -> dict[str, Any]:
    return dict((db.docs[f"tasks/{task_id}"].get("metadata") or {}).get("dispatch") or {})


def test_the_console_verify_runs_are_admitted_by_the_real_submission_service(
        setup_app, setup_client, db, github) -> None:
    """Audit 4d, F1 and F2: `TeamRuns` through the real `SubmissionService`.
    The workflow (whose `open` step builds on `write`'s branch) is admitted,
    as `integrate` with `open` its integrator, and the task is stored as
    `direct-pr`: each ends in the pull request verify requires. Before, the
    workflow was refused 422 (`collect` pushes no branch) and the task was a
    `collect` task whose git tools refuse every push."""
    setup_app.state.setup_runs = None
    install_app(db, github)
    create_team(setup_client)
    answer = start_verify(setup_client)
    assert answer.status_code == 200, answer.text
    ids = answer.json()["verify_runs"]
    assert set(ids) == {"console_task", "console_workflow"} and all(ids.values())
    assert db.docs[VERIFY_DOC]["verify_runs"] == ids
    assert _stored_dispatch(db, ids["console_task"])["strategy"] == "direct-pr"
    steps = [d for k, d in db.docs.items() if k.count("/") == 1 and k.startswith("tasks/")
             and d.get("workflow_id") == ids["console_workflow"]]
    roles_ = {(d["metadata"]["dispatch"]["strategy"], d["metadata"]["dispatch"].get("role"))
              for d in steps}
    assert roles_ == {("integrate", "contributor"), ("integrate", "integrator")}


def test_the_smoke_is_admitted_by_the_real_submission_service(setup_app, setup_client, db,
                                                              github, pool) -> None:
    """The first team's smoke through the real `SubmissionService`: admitted,
    and stored `collect` (its prompt asks for no pull request)."""
    setup_app.state.setup_runs = None
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    smoke = page_team(setup_client, team)["smoke"]
    assert smoke["state"] == "running", smoke
    assert _stored_dispatch(db, smoke["task_id"])["strategy"] == "collect"


def _before_pass_3(db) -> None:
    """The App installed, its self-check answering `app_id_not_configured`:
    infra pass 3 has not rendered GITHUB_APP_ID yet."""
    db.docs[f"swarm-install/{ENV}/console/github_app"]["self_check"] = {
        "ok": False, "reason": "app_id_not_configured"}


def test_the_smoke_waits_for_infra_pass_3(setup_client, db, github, pool, runs) -> None:
    """Audit 4d, F4: before pass 3 no installation token can be minted, so a
    smoke started then fails to clone. It reads `waiting_for_infra_pass_3`,
    nothing is submitted or recorded, and it starts on the first read after
    the self-check passes."""
    install_app(db, github)
    _before_pass_3(db)
    team = create_team(setup_client).json()["team"]
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    smoke = page_team(setup_client, team)["smoke"]
    assert smoke["state"] == "waiting_for_infra_pass_3", smoke
    assert "infra pass 3" in smoke["message"]
    assert runs.tasks == [] and team_doc(db, team).get("smoke") is None
    restart = setup_client.post("/v1/admin/setup/smoke", headers=auth_header("root"))
    assert restart.json()["smoke"]["state"] == "waiting_for_infra_pass_3"
    assert runs.tasks == []
    db.docs[f"swarm-install/{ENV}/console/github_app"]["self_check"] = {"ok": True}
    assert page_team(setup_client, team)["smoke"]["state"] == "running"
    assert len(runs.tasks) == 1


def test_one_persons_smoke_waits_for_infra_pass_3(setup_client, db, github, pool,
                                                  runs) -> None:
    seed_sized_engine(db)
    install_app(db, github)
    _before_pass_3(db)
    tenant = personal_tenant()
    page(setup_client)
    pool.accounts.append({"account_id": f"{tenant}:pool-1", "owner_tenant": tenant,
                          "label": "pool-1"})
    assert page(setup_client)["personal"]["smoke"]["state"] == "waiting_for_infra_pass_3"
    assert runs.tasks == []


def test_the_verify_runs_wait_for_infra_pass_3(setup_client, db, github, runs) -> None:
    """Audit 4d, F4: `start_verify_runs` starts nothing before pass 3 and
    says so (409, `waiting_for_infra_pass_3`); nothing is recorded, so the
    engine waits rather than reading a run doomed to fail."""
    install_app(db, github)
    _before_pass_3(db)
    create_team(setup_client)
    early = start_verify(setup_client)
    assert early.status_code == 409, early.text
    assert early.json()["detail"]["state"] == "waiting_for_infra_pass_3"
    assert runs.tasks == [] and runs.workflows == [] and VERIFY_DOC not in db.docs
    db.docs[f"swarm-install/{ENV}/console/github_app"]["self_check"] = {"ok": True}
    assert start_verify(setup_client).status_code == 200
    assert len(runs.tasks) == 1 and len(runs.workflows) == 1


REDEEM = "/v1/setup/links/redeem"


def _mint_pool_link(client: TestClient, owner: str = ALICE) -> str:
    minted = client.post("/v1/admin/setup/links", headers=auth_header("root"), json={
        "kind": "pool-account", "label": "pool-1", "owner": owner})
    assert minted.status_code == 201, minted.text
    return minted.json()["path"].rsplit("/", 1)[1]


def _link_used(db) -> bool:
    links = [d for k, d in db.docs.items() if k.startswith("setup_links/")]
    assert len(links) == 1
    return links[0].get("used_at") is not None


def test_a_pool_link_is_not_spent_before_its_workspace_is_ready(setup_client, db) -> None:
    """Audit 4d, F6: the sign-in a pool link leads to names its workspace in
    X-Swarm-Tenant, refused until the workspace is `ready`. So the redeem
    answers 409 (retry) WITHOUT consuming the link while it is `approved`,
    and opens it once it is ready."""
    team = create_team(setup_client).json()["team"]
    assert db.docs[f"workspaces/{team}"]["state"] != "ready"
    token = _mint_pool_link(setup_client)
    early = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert early.status_code == 409, early.text
    assert early.json()["detail"] == {**early.json()["detail"], "workspace": team,
                                      "retry": True}
    assert "still unused" in early.json()["message"]
    assert not _link_used(db)
    db.docs[f"workspaces/{team}"]["state"] = "ready"
    opened = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["tenant"] == team and _link_used(db)


def test_one_persons_pool_link_waits_for_the_personal_workspace(setup_client, db) -> None:
    """F6 in one-person mode: the form shows straight after the hand-off,
    while swarm-workspace-apply is still making the personal workspace."""
    seed_sized_engine(db)
    tenant = personal_tenant()
    token = _mint_pool_link(setup_client, owner=ROOT)
    early = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("root"))
    assert early.status_code == 409, early.text
    assert early.json()["detail"]["workspace"] == tenant
    assert not _link_used(db)
    db.docs.setdefault(f"workspaces/{tenant}", {})["state"] = "ready"
    opened = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("root"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["tenant"] == tenant and _link_used(db)


# --------------------------------------------------------------------------
# the install's sizing and mode (lane DAYONE-SETUP-API-3, decision 7)
# --------------------------------------------------------------------------

PERSONAL_DOC = f"swarm-install/{ENV}/console/personal"
FINISHED_DOC = f"swarm-install/{ENV}/console/finished"


def sizing(mode: str = "individual", members: int = 1) -> dict[str, Any]:
    """`state.outputs["sizing"]` as the INTERFACE SHEET writes it."""
    return {
        "mode": mode, "members": members,
        "pool_limits": {"global": 4 * members, "default_tenant": 2, "provider_tenant": 2,
                        "resource_classes": {"small": 2}, "runner_profiles": {"claude-code": 2},
                        "backends": {"gke": 2}},
        "service_max_instances": {"swarm-api": 2, "swarm-scheduler": 1,
                                  "swarm-quota-broker": 1, "swarm-reconciler": 1},
        "pool_accounts": max(1, members // 4), "budget_usd": 150 * members,
    }


def seed_sized_engine(db, mode: str = "individual", members: int = 1,
                      installer: str | None = ROOT) -> dict[str, Any]:
    doc = seed_engine_document(db)
    doc.update({"sizing": sizing(mode, members), "installer": installer})
    db.docs[ENGINE_DOC] = dict(doc)
    return doc


def personal_tenant(email: str = ROOT) -> str:
    from swarm_common.identity import tenant_id_for_user

    return tenant_id_for_user(email)


def page(client: TestClient) -> dict[str, Any]:
    response = client.get("/v1/admin/setup", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_page_serves_the_installs_sizing_read_only(setup_client, db) -> None:
    """The status route serves the engine's `sizing` (mode, members,
    pool_limits, service_max_instances, pool_accounts, budget_usd) and the
    mode; the engine's document is not written."""
    written = seed_sized_engine(db, mode="team", members=12)
    body = page(setup_client)
    assert body["mode"] == "team"
    assert body["sizing"] == sizing("team", 12)
    assert body["steps"]["first_team"]["needed"] is True
    assert body["steps"]["invites"]["needed"] is True
    assert body["personal"] is None
    assert db.docs[ENGINE_DOC] == written


def test_an_engine_document_with_no_sizing_reads_as_a_team(setup_client, db) -> None:
    seed_engine_document(db)
    body = page(setup_client)
    assert body["sizing"] is None and body["mode"] == "team"
    assert PERSONAL_DOC not in db.docs


def test_the_sizing_view_carries_only_its_named_fields(setup_client, db) -> None:
    doc = seed_sized_engine(db, mode="team", members=3)
    doc["sizing"] = {**doc["sizing"], "stray": "x"}
    db.docs[ENGINE_DOC] = doc
    assert set(page(setup_client)["sizing"]) == {
        "mode", "members", "pool_limits", "service_max_instances", "pool_accounts",
        "budget_usd"}


def test_one_person_needs_no_first_team_and_no_invites(setup_client, db) -> None:
    """Decision 7: for one person the first workspace is the installer's
    personal workspace, filed on the page's read, and the first-team and
    invites steps say they are not needed."""
    seed_sized_engine(db)
    body = page(setup_client)
    assert body["mode"] == "individual"
    for step in ("first_team", "invites"):
        assert body["steps"][step] == {"needed": False, "state": "not_needed",
                                       "reason": "not needed for one person"}
    tenant = personal_tenant()
    assert body["personal"]["workspace"] == tenant
    assert body["personal"]["owner"] == ROOT
    assert db.docs[PERSONAL_DOC]["workspace"] == tenant
    assert db.docs[f"workspaces/{tenant}"]["kind"] == "personal"
    assert body["teams"] == []


def test_one_person_refuses_a_team_and_an_invite(setup_client, db) -> None:
    seed_sized_engine(db)
    team = create_team(setup_client)
    assert team.status_code == 409, team.text
    assert "not needed for one person" in team.text
    invite = setup_client.post("/v1/admin/setup/teams/platform-team/invites",
                               json={"email": CAROL}, headers=auth_header("root"))
    assert invite.status_code == 409, invite.text
    assert not [k for k in db.docs if k.startswith(TEAMS_PREFIX)]


def test_one_persons_smoke_runs_in_the_personal_workspace(setup_client, db, github, pool,
                                                          runs) -> None:
    """The smoke runs once the installer's pool account lands in their
    personal workspace, on a repository of the personal grant, recorded on
    `console/personal.smoke`."""
    seed_sized_engine(db)
    install_app(db, github)
    tenant = personal_tenant()
    body = page(setup_client)
    assert body["personal"]["grant"]["repositories"] == [f"{ORG}/api", f"{ORG}/demo"]
    assert body["personal"].get("smoke") is None and runs.tasks == []
    pool.accounts.append({"account_id": f"{tenant}:pool-1", "owner_tenant": tenant,
                          "label": "pool-1"})
    smoke = page(setup_client)["personal"]["smoke"]
    assert smoke["state"] == "running" and smoke["task_id"] == "task-1"
    assert runs.tasks[0]["tenant"] == tenant and runs.tasks[0]["group"] == ROOT
    runs.states["task-1"] = "succeeded"
    assert page(setup_client)["personal"]["smoke"]["state"] == "succeeded"
    assert db.docs[PERSONAL_DOC]["smoke"]["state"] == "succeeded"


def test_one_persons_pool_account_link_is_the_installers(setup_client, db) -> None:
    seed_sized_engine(db)
    headers = auth_header("root")
    other = setup_client.post("/v1/admin/setup/links", headers=headers, json={
        "kind": "pool-account", "label": "pool-1", "owner": ALICE})
    assert other.status_code == 403, other.text
    mine = setup_client.post("/v1/admin/setup/links", headers=headers, json={
        "kind": "pool-account", "label": "pool-1", "owner": ROOT})
    assert mine.status_code == 201, mine.text
    links = [d for k, d in db.docs.items() if k.startswith("setup_links/")]
    assert [link["tenant_id"] for link in links] == [personal_tenant()]
    git = setup_client.post("/v1/admin/setup/links", headers=headers, json={
        "kind": "team-git", "label": personal_tenant(), "owner": ROOT})
    assert git.status_code == 201, git.text
    assert db.docs[PERSONAL_DOC]["progress"]["git_link"]["owner"] == ROOT


def test_one_person_finishes_on_the_personal_smoke(setup_app, setup_client, db, github, pool,
                                                   runs) -> None:
    """`finish` for one person waits for the App, a pool account signed in to
    the personal workspace and that workspace's smoke; never for a team."""
    seed_sized_engine(db)
    headers = auth_header("root")
    early = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert early.status_code == 409
    assert set(early.json()["detail"]["missing"]) == {
        "github_app_installed", "github_app_self_check", "pool_account_signed_in",
        "personal_smoke"}
    setup_app.state.forge_app = PassingAppCheck()
    install_app(db, github)
    assert setup_client.post("/v1/admin/setup/github-app/self-check",
                             headers=headers).json()["ok"] is True
    assert setup_client.post("/v1/admin/setup/links", headers=headers, json={
        "kind": "pool-account", "label": "pool-1", "owner": ROOT}).status_code == 201
    tenant = personal_tenant()
    pool.accounts.append({"account_id": f"{tenant}:pool-1", "owner_tenant": tenant,
                          "label": "pool-1"})
    smoking = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert smoking.status_code == 409
    assert smoking.json()["detail"]["missing"] == ["personal_smoke"]
    assert FINISHED_DOC not in db.docs
    assert runs.tasks[0]["tenant"] == tenant
    runs.states["task-1"] = "succeeded"
    done = setup_client.post("/v1/admin/setup/finish", headers=headers)
    assert done.status_code == 200, done.text
    assert db.docs[FINISHED_DOC]["by"] == ROOT
    assert not [k for k in db.docs if k.startswith(TEAMS_PREFIX)]


def test_one_persons_failed_smoke_starts_again_on_request(setup_client, db, github, pool,
                                                         runs) -> None:
    seed_sized_engine(db)
    install_app(db, github)
    tenant = personal_tenant()
    page(setup_client)
    pool.accounts.append({"account_id": f"{tenant}:pool-1", "owner_tenant": tenant,
                          "label": "pool-1"})
    page(setup_client)
    runs.states["task-1"] = "failed"
    assert page(setup_client)["personal"]["smoke"]["state"] == "failed"
    again = setup_client.post("/v1/admin/setup/smoke", headers=auth_header("root"))
    assert again.status_code == 200, again.text
    assert again.json()["workspace"] == tenant
    assert again.json()["smoke"]["task_id"] == "task-2"
    assert runs.tasks[1]["tenant"] == tenant


def test_one_persons_verify_runs_run_in_the_personal_workspace(setup_client, db, github,
                                                               runs) -> None:
    seed_sized_engine(db)
    install_app(db, github)
    answer = setup_client.post("/v1/admin/setup/verify-runs", headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["verify_runs"] == {"console_task": "task-1", "console_workflow": "wf-1"}
    tenant = personal_tenant()
    assert runs.tasks[0]["tenant"] == tenant and runs.workflows[0]["tenant"] == tenant
    assert runs.tasks[0]["group"] == ROOT
    # Recorded as for a team: the engine reads the same two id keys.
    assert db.docs[VERIFY_DOC]["verify_runs"] == {"console_task": "task-1",
                                                  "console_workflow": "wf-1"}
    assert db.docs[VERIFY_DOC]["repository"] == f"{ORG}/api"


def test_one_person_with_no_installer_recorded_uses_the_platform_owner(setup_client,
                                                                      db) -> None:
    seed_sized_engine(db, installer=None)
    assert page(setup_client)["personal"]["owner"] == ROOT


# --------------------------------------------------------------------------
# DAYONE-SETUP-API-6: the groups probe's shape (N6), a signed-in account (N7),
# the enforced grant (N11), the verify runs' `retries` (3a)
# --------------------------------------------------------------------------


class _TestClientHttp:
    """`api_client.Http` over the real app: the engine's request reaches the
    real route as the deployer (the bearer it minted is swapped for the
    deployer's static token, which is what IAP would have verified)."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.sent: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, body):
        from swarm_install.api_client import Response

        self.sent.append(dict(body))
        path = url.removeprefix(f"https://{HOSTNAME}")
        found = self.client.request(method, path, json=dict(body),
                                    headers=auth_header("deployer"))
        return Response(found.status_code, found.json())


class _Minter:
    def generate_id_token(self, service_account, audience, *, include_email):
        return secrets.token_hex(16)


def _engine_client(setup_client: TestClient):
    from swarm_install.api_client import ApiClient

    http = _TestClientHttp(setup_client)
    return ApiClient(project=PROJECT, base_url=f"https://{HOSTNAME}", audience="aud",
                     minter=_Minter(), http=http), http


def test_the_engines_groups_probe_reads_the_real_routes_answer(setup_app, setup_client,
                                                               directory) -> None:
    """Finding N6, a contract test: `ApiClient.groups_probe` against the real
    route. The route probes exactly the `{group, member}` pairs asked, names
    each by the name asked, and answers the mode it reads groups in."""
    client, http = _engine_client(setup_client)
    directory.unreadable.add(SECOND_TEAM)
    answer = client.groups_probe([{"group": TEAM_GROUP, "member": ALICE},
                                  {"group": ENG_GROUP, "member": CAROL},
                                  {"group": SECOND_TEAM, "member": ROOT}])
    assert http.sent == [{"groups": [{"group": TEAM_GROUP, "member": ALICE},
                                     {"group": ENG_GROUP, "member": CAROL},
                                     {"group": SECOND_TEAM, "member": ROOT}]}]
    assert answer.mode == setup_app.state.ctx.settings.groups_auth
    assert answer.of(TEAM_GROUP).state == "met"
    assert (answer.of(ENG_GROUP).state, answer.of(ENG_GROUP).reason) == ("unmet", "not_a_member")
    assert (answer.of(SECOND_TEAM).state,
            answer.of(SECOND_TEAM).reason) == ("unmet", "unreadable")
    asked = {(member, groups) for member, groups in directory.asked}
    assert asked == {(ALICE, (TEAM_GROUP,)), (CAROL, (ENG_GROUP,)), (ROOT, (SECOND_TEAM,))}


def test_the_groups_probe_answers_admin_role_mode(setup_app, setup_client) -> None:
    """`identity.groups-reader-role` sees `mode == admin_role` when swarm-api
    runs in it."""
    from dataclasses import replace

    ctx = setup_app.state.ctx
    setup_app.state.ctx = replace(ctx, settings=replace(ctx.settings, groups_auth="admin_role"))
    client, _ = _engine_client(setup_client)
    assert client.groups_probe([{"group": TEAM_GROUP, "member": ROOT}]).mode == "admin_role"


def test_a_lookup_failures_reason_is_named_never_its_body() -> None:
    from swarm_api.groups import GroupLookupError
    from swarm_api.routes.setup import probe_reason

    assert probe_reason(GroupLookupError("cloud identity returned 403: {...}")) == \
        "permission_denied"
    assert probe_reason(GroupLookupError("groups:lookup returned no name for x")) == \
        "group_not_found"
    assert probe_reason(RuntimeError("delegation refused: unauthorized_client")) == \
        "unauthorized_client"
    assert probe_reason(RuntimeError("swarm-api@p may not sign as itself")) == "sign_jwt_denied"
    assert probe_reason(GroupLookupError("unreadable")) == "unreadable"


def test_the_doctor_reads_the_registered_groups_through_results(setup_client,
                                                                directory) -> None:
    """With no pairs the route still probes every registered group (the
    doctor's question), in the same `results` shape."""
    client, _ = _engine_client(setup_client)
    directory.unreadable.add(ENG_GROUP)
    answer = client.groups_probe(())
    assert answer.of(ENG_GROUP).state == "unmet"
    assert answer.of("research@example.com").state == "met"


def test_a_pool_account_in_the_pool_shows_as_signed_in_on_the_page(setup_client, db,
                                                                   pool) -> None:
    """Finding N7: the page's poll stamps `signed_in_at` on each labelled
    account the first team's pool holds, so "Hand over to swarm install"
    (which calls `/finish`) appears without `/finish` being called first."""
    team = create_team(setup_client).json()["team"]
    assert setup_client.post("/v1/admin/setup/links", headers=auth_header("root"), json={
        "kind": "pool-account", "label": "pool-1", "owner": ALICE}).status_code == 201
    body = page(setup_client)
    assert body["accounts"]["pool-1"].get("signed_in_at") is None
    pool.accounts.append({"account_id": f"{team}:pool-1", "owner_tenant": team,
                          "label": "pool-1"})
    body = page(setup_client)
    stamped = body["accounts"]["pool-1"]["signed_in_at"]
    assert stamped and body["accounts"]["pool-1"]["owner"] == ALICE
    assert "link_hash" not in body["accounts"]["pool-1"]
    assert page(setup_client)["accounts"]["pool-1"]["signed_in_at"] == stamped


def test_one_persons_pool_account_shows_as_signed_in_on_the_page(setup_client, db,
                                                                 pool) -> None:
    seed_sized_engine(db)
    assert setup_client.post("/v1/admin/setup/links", headers=auth_header("root"), json={
        "kind": "pool-account", "label": "pool-1", "owner": ROOT}).status_code == 201
    tenant = personal_tenant()
    pool.accounts.append({"account_id": f"{tenant}:pool-1", "owner_tenant": tenant,
                          "label": "pool-1"})
    assert page(setup_client)["accounts"]["pool-1"]["signed_in_at"]


def _grant_mode(setup_app, tenant: str, email: str, repository: str) -> str | None:
    from swarm_api.repositories import repo_id_for

    owner, repo = repository.split("/")
    return setup_app.state.ctx.submissions._grant_mode(tenant, email,
                                                       repo_id_for(tenant, owner, repo))


def test_the_first_teams_grant_is_enforced_for_its_members(setup_app, setup_client, db,
                                                           github) -> None:
    """Finding N11: the team-level grant is the one submission reads, so a
    teammate with no grant of their own may write to each repository the App
    installation covers, in the first team's workspace and nowhere else."""
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    assert team_doc(db, team)["grant"]["enforced_at"]
    for repository in (f"{ORG}/api", f"{ORG}/demo"):
        assert _grant_mode(setup_app, team, ALICE, repository) == "write"
    assert _grant_mode(setup_app, team, ALICE, f"{ORG}/elsewhere") is None
    assert _grant_mode(setup_app, "another-team", ALICE, f"{ORG}/api") is None


def test_a_grant_recorded_before_enforcement_is_enforced_on_the_next_read(
        setup_app, setup_client, db, github) -> None:
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    for key in [k for k in db.docs if k.startswith("workspace_repository_grants/")]:
        del db.docs[key]
    grant = dict(team_doc(db, team)["grant"])
    grant.pop("enforced_at")
    team_doc(db, team)["grant"] = grant
    assert _grant_mode(setup_app, team, ALICE, f"{ORG}/api") is None
    page_team(setup_client, team)
    assert _grant_mode(setup_app, team, ALICE, f"{ORG}/api") == "write"
    assert team_doc(db, team)["grant"]["enforced_at"]


def test_one_persons_grant_is_enforced_in_the_personal_workspace(setup_app, setup_client, db,
                                                                 github) -> None:
    seed_sized_engine(db)
    install_app(db, github)
    page(setup_client)
    assert _grant_mode(setup_app, personal_tenant(), ROOT, f"{ORG}/demo") == "write"


def test_verify_runs_hold_only_the_two_ids_and_retries_has_its_own_field(
        setup_client, db, github, runs) -> None:
    """3a: `verify_runs` is exactly the INTERFACE SHEET's two id keys; the
    counts are `retries`, written from the first start."""
    verify_started(setup_client, db, github)
    doc = db.docs[VERIFY_DOC]
    assert doc["verify_runs"] == {"console_task": "task-1", "console_workflow": "wf-1"}
    assert doc["retries"] == {"console_task": 0, "console_workflow": 0}


def test_an_old_document_with_counts_inside_verify_runs_is_still_read(setup_client, db,
                                                                      github, runs) -> None:
    """A document SETUP-API-5 wrote keeps its counts inside `verify_runs`:
    they are read as the fallback, and the next write moves them out."""
    verify_started(setup_client, db, github)
    db.docs[VERIFY_DOC] = {**{k: v for k, v in db.docs[VERIFY_DOC].items() if k != "retries"},
                           "verify_runs": {"console_task": "task-1", "console_workflow": "wf-1",
                                           "console_task_retries": 2}}
    runs.states["task-1"] = "failed"
    refused = start_verify(setup_client)
    assert refused.status_code == 409 and "2 fresh starts" in refused.text
    db.docs[VERIFY_DOC]["verify_runs"]["console_task_retries"] = 1
    answer = start_verify(setup_client)
    assert answer.status_code == 200, answer.text
    assert db.docs[VERIFY_DOC]["verify_runs"] == {"console_task": "task-2",
                                                  "console_workflow": "wf-1"}
    assert db.docs[VERIFY_DOC]["retries"] == {"console_task": 2, "console_workflow": 0}


# --------------------------------------------------------------------------
# audit 3 (DAYONE-SETUP-API-7)
# --------------------------------------------------------------------------


def test_the_fake_refuses_the_paths_real_firestore_refuses(db) -> None:
    with pytest.raises(ValueError):
        db.collection(f"swarm-install/{ENV}/console/teams")
    with pytest.raises(ValueError):
        db.document(f"swarm-install/{ENV}/console")
    db.collection(f"swarm-install/{ENV}/teams")
    db.document(f"{TEAMS_PREFIX}platform-team")


def test_the_page_and_the_first_team_load_on_real_firestore_paths(setup_client, db,
                                                                  github) -> None:
    """A2: the status route lists the teams on every load, and the first team
    is created, read and granted at a path real Firestore accepts."""
    seed_engine_document(db)
    install_app(db, github)
    assert page(setup_client)["teams"] == []
    team = create_team(setup_client)
    assert team.status_code == 202, team.text
    team_id = team.json()["team"]
    assert [row["id"] for row in page(setup_client)["teams"]] == [team_id]
    assert db.docs[f"{TEAMS_PREFIX}{team_id}"]["group"] == TEAM_GROUP


def test_the_install_record_names_team_documents_at_a_valid_path() -> None:
    from swarm_api import install_record as ir

    record = ir.InstallRecord(PathCheckingFirestore(), ENV)
    assert record.team_path("t1") == f"{TEAMS_PREFIX}t1"
    assert record.teams() == []
    ir.check_fields(record.team_path("t1"), ENV, {"group": TEAM_GROUP})
    with pytest.raises(ir.RecordFieldRefused):
        ir.check_fields(f"swarm-install/{ENV}/console/teams/t1", ENV, {"group": TEAM_GROUP})


@pytest.mark.parametrize("age, running", [(timedelta(seconds=5), True),
                                          (timedelta(hours=2), False)])
def test_the_clis_epoch_seconds_heartbeat_reads_as_running(age, running) -> None:
    """A7: the engine writes `lease.heartbeat` as epoch seconds (a float);
    a fresh one reads as running, a stale one does not."""
    from swarm_api.routes import setup as routes

    now = datetime.now(timezone.utc)
    beat = (now - age).timestamp()
    assert routes._cli_running({"lease": {"heartbeat": beat}}, now) is running
    assert routes._cli_running({"lease": {"heartbeat": int(beat)}}, now) is running


def test_the_page_says_the_cli_is_running_on_an_epoch_heartbeat(setup_client, db) -> None:
    import time

    doc = seed_engine_document(db)
    db.docs[ENGINE_DOC] = {**doc, "lease": {"holder": "laptop", "heartbeat": time.time(),
                                             "expires": time.time() + 300}}
    body = page(setup_client)
    assert body["cli_running"] is True, body


def _app_installed(db, tenant: str, owner: str) -> bool:
    """The submission path's own answer (`SubmissionService._app_installed`,
    over the real `installation_record_in`), with installation reads on."""
    from types import SimpleNamespace

    from swarm_api.service import SubmissionService

    probe = SimpleNamespace(_settings=SimpleNamespace(forge_installation_reads=True),
                            _store=SimpleNamespace(db=db))
    return SubmissionService._app_installed(probe, tenant, owner)


def test_the_first_teams_grant_records_the_app_installation_for_its_workspace(
        setup_client, db, github) -> None:
    """A4: the grant also writes the team workspace's own `forge_orgs`
    document, so a service submission for that workspace is minted an
    installation token; no other workspace gains one."""
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    assert _app_installed(db, team, ORG) is True
    assert _app_installed(db, "another-team", ORG) is False
    orgs = [v for k, v in db.docs.items() if k.startswith("forge_orgs/")]
    assert [(o["tenant_id"], o["owner"], o["installation_id"], o["install_state"],
             o["owner_type"]) for o in orgs] == [(team, ORG, 77, "installed", "Organization")]
    assert team_doc(db, team)["grant"]["installation_recorded_at"]


def test_a_grant_enforced_before_the_installation_was_recorded_records_it_on_the_next_read(
        setup_client, db, github) -> None:
    install_app(db, github)
    team = create_team(setup_client).json()["team"]
    for key in [k for k in db.docs if k.startswith("forge_orgs/")]:
        del db.docs[key]
    grant = dict(team_doc(db, team)["grant"])
    grant.pop("installation_recorded_at")
    team_doc(db, team)["grant"] = grant
    assert _app_installed(db, team, ORG) is False
    page_team(setup_client, team)
    assert _app_installed(db, team, ORG) is True


def test_one_persons_grant_records_the_app_installation_for_the_personal_workspace(
        setup_client, db, github) -> None:
    seed_sized_engine(db)
    install_app(db, github)
    page(setup_client)
    assert _app_installed(db, personal_tenant(), ORG) is True


# --------------------------------------------------------------------------
# no workspace is approved before infra pass 3 turns publishing on
# (DAYONE-MANIFEST-STEPS-2)
# --------------------------------------------------------------------------


def publishing_off(setup_app) -> FakePublisher:
    """swarm-api as infra passes 1 and 2 deploy it: `workspace_apply_publish`
    is false until infra pass 3 (`github.pass-3`), so an approval would only
    be logged `workspace_stuck` (reason `publishing_off`)."""
    publisher = setup_app.state.workspace_publisher
    publisher.enabled = False
    return publisher


def test_the_first_team_waits_for_publishing(setup_app, setup_client, db) -> None:
    publisher = publishing_off(setup_app)
    seed_sized_engine(db, mode="team", members=5)
    step = page(setup_client)["steps"]["first_team"]
    assert step["needed"] is True and step["state"] == "waiting", step
    assert "infra pass 3" in step["reason"], step
    refused = create_team(setup_client, first_admin=WORKSPACE_ADMIN)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "publishing_off", refused.text
    assert not [k for k in db.docs if k.startswith("workspaces/") or k.startswith(TEAMS_PREFIX)]
    assert publisher.messages == []
    # Infra pass 3's revision serves `workspace_apply_publish`: the team is approved and published.
    publisher.enabled = True
    assert page(setup_client)["steps"]["first_team"]["state"] == "to_do"
    created = create_team(setup_client, first_admin=WORKSPACE_ADMIN)
    assert created.status_code == 202, created.text
    assert len(publisher.messages) == 1, publisher.messages


def test_one_persons_workspace_waits_for_publishing(setup_app, setup_client, db) -> None:
    publisher = publishing_off(setup_app)
    seed_sized_engine(db)
    tenant = personal_tenant()
    body = page(setup_client)
    assert body["personal"] == {"owner": ROOT, "workspace": None, "reason": "publishing_off"}
    assert PERSONAL_DOC not in db.docs and f"workspaces/{tenant}" not in db.docs
    link = setup_client.post("/v1/admin/setup/links",
                             json={"kind": "pool-account", "label": "a1", "owner": ROOT},
                             headers=auth_header("root"))
    assert link.status_code == 409 and link.json()["code"] == "publishing_off", link.text
    assert f"workspaces/{tenant}" not in db.docs and publisher.messages == []
    publisher.enabled = True
    assert page(setup_client)["personal"]["workspace"] == tenant
    assert len(publisher.messages) == 1, publisher.messages
