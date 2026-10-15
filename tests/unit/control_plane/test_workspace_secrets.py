"""The workspace secrets store's records, grants and management API (SEC-STORE-API).

Design: docs/design/workspace-secrets.md §2 and §5, phase 1 of #1347. The
attackers these tests stand in for:

  * A LOG OR DATABASE READER -- a value never appears in a response, a log
    line, a Firestore document or an error message, on the success path or
    on a refusal (where a value is echoed by accident).
  * ANOTHER WORKSPACE'S MEMBER -- a secret of another tenant is a 404 on every
    route, whatever its name; no route takes a tenant.
  * A GROUP MEMBER WHO IS NOT AN ADMIN -- reads names and grants, writes
    nothing (owner decision Q4), and their refused value never reaches Secret
    Manager.
  * A CALLER NAMING AN IMAGE OR A COMMAND AS A CONSUMER -- 422: a grant names a
    catalogue entry (CONTRACT.md invariant 10).

Owner decisions held here: Q3 (a personal secret's opt-in list), Q4 (who
writes), Q5 (the previous version stays enabled 24 h, then the sweep disables
and never destroys it), Q7 (provider keys and pool accounts listed read-only).

Every fake value is built at runtime (CLAUDE.md: nothing added may look like a
credential). Offline: FakeFirestore, an in-memory Secret Manager stand-in and
the account-pool fake.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import (
    BROWSER_TOOLS,
    CONSUMERS_BY_NAME,
    GRANTS_COLLECTION,
    PROFILE_NAMES,
    ROTATION_GRACE,
    SECRETS_COLLECTION,
    SecretManagerWorkspaceSecrets,
)
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore, FakeTransaction
from .test_account_api import FakePool, account

BASE = "/v1/workspace/secrets"
CONSOLE = "https://console.example.test"
START = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def fake_value(fill: str = "q") -> str:
    """Shaped like an API key, built at runtime so no literal looks like one."""
    return "sk" + "-" + fill * 40


# -- the stand-ins -------------------------------------------------------------


class RecordingTransaction(FakeTransaction):
    """Records the document paths each committed transaction wrote, so a test
    can say a record and its audit row landed in ONE transaction."""

    def _commit(self) -> list[Any]:
        self._db.commits.append([ref.path for _op, ref, _data in self._buffer])
        return super()._commit()


class RecordingFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[list[str]] = []

    def transaction(self, **kwargs: Any) -> RecordingTransaction:
        return RecordingTransaction(self)


class FakeSecretManager:
    """Secret Manager, in memory: versions, their states, their payloads.

    The ONE place a value may live in these tests, as Secret Manager is the
    one place it may live in production. No IAM, as the real writer has none."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}             # secret id -> tenant
        self.payloads: dict[str, bytes] = {}          # version name -> bytes
        self.states: dict[str, str] = {}              # version name -> state
        self.calls: list[tuple[str, str]] = []

    def create_secret(self, tenant_id: str, secret_id: str) -> None:
        self.calls.append(("create_secret", secret_id))
        self.secrets[secret_id] = tenant_id

    def add_version(self, secret_id: str, payload: bytes) -> str:
        assert secret_id in self.secrets
        version = f"projects/test/secrets/{secret_id}/versions/" \
                  f"{sum(1 for v in self.payloads if v.startswith(f'projects/test/secrets/{secret_id}/')) + 1}"
        self.calls.append(("add_version", secret_id))
        self.payloads[version] = payload
        self.states[version] = "ENABLED"
        return version

    def disable_version(self, version: str) -> None:
        self.calls.append(("disable_version", version))
        self.states[version] = "DISABLED"

    def destroy_versions(self, secret_id: str) -> int:
        self.calls.append(("destroy_versions", secret_id))
        prefix = f"projects/test/secrets/{secret_id}/"
        live = [v for v, s in self.states.items() if v.startswith(prefix) and s != "DESTROYED"]
        for version in live:
            self.states[version] = "DESTROYED"
            self.payloads.pop(version, None)
        return len(live)

    def versions_of(self, secret_id: str) -> dict[str, str]:
        prefix = f"projects/test/secrets/{secret_id}/"
        return {v: s for v, s in self.states.items() if v.startswith(prefix)}


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def db() -> RecordingFirestore:
    return RecordingFirestore()


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice and dave in eng, bob in research, root an admin in eng."""
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "dave@saga.xyz": (ENG_GROUP,),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture
def pool() -> FakePool:
    return FakePool(account("eng", "team-max"))


@pytest.fixture
def client(db, tokens, group_map, objects, clock, sm, pool) -> TestClient:
    ctx = build_context(
        settings=api_settings(console_url=CONSOLE), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), now=clock,
    )
    app = create_app(ctx)
    app.state.workspace_secret_writer = sm
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def seed_personal(db: FakeFirestore, user: str) -> None:
    tenant_id = f"u-{user}"
    owner = f"{user}@saga.xyz"
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"].update(kind="user", principal=owner, display_name=owner)
    db.docs[f"workspaces/{tenant_id}"] = {
        "tenant_id": tenant_id, "workspace_id": f"w-{user[:6]:0<6}",
        "principal": owner, "state": "ready",
    }


@pytest.fixture
def seeded(db: RecordingFirestore) -> RecordingFirestore:
    seed_tenant(db, "eng", credentials=("anthropic",))
    seed_tenant(db, "research")
    seed_personal(db, "alice")
    seed_personal(db, "bob")
    return db


def as_(who: str, tenant: str) -> dict[str, str]:
    return {**auth_header(who), TENANT_HEADER: tenant}


def create(client, who: str, tenant: str, name: str, value: str | None = None, *,
           kind: str = "api_key", provider: str = "typesafe", **extra):
    body: dict[str, Any] = {"name": name, "provider": provider, "kind": kind, **extra}
    if value is not None:
        body["value"] = value
    return client.post(BASE, headers=as_(who, tenant), json=body)


def rotate(client, who: str, tenant: str, name: str, value: str, **extra):
    return client.put(f"{BASE}/{name}/value", headers=as_(who, tenant),
                      json={"value": value, **extra})


def grant(client, who: str, tenant: str, name: str, consumer: str, enabled: bool = True, **extra):
    return client.put(f"{BASE}/{name}/grants/{consumer}", headers=as_(who, tenant),
                      json={"enabled": enabled, **extra})


def delete(client, who: str, tenant: str, name: str, body: dict | None):
    kwargs: dict[str, Any] = {"headers": as_(who, tenant)}
    if body is not None:
        kwargs["json"] = body
    return client.request("DELETE", f"{BASE}/{name}", **kwargs)


def listing(client, who: str, tenant: str) -> dict:
    response = client.get(BASE, headers=as_(who, tenant))
    assert response.status_code == 200, response.text
    return response.json()


def audits(db: FakeFirestore) -> list[dict]:
    return sorted((doc for path, doc in db.docs.items()
                   if path.startswith(f"{AUDIT_COLLECTION}/")), key=lambda e: e["at"])


def record_of(db: FakeFirestore, tenant: str, name: str) -> dict:
    found = [d for p, d in db.docs.items() if p.startswith(f"{SECRETS_COLLECTION}/")
             and d.get("tenant_id") == tenant and d.get("name") == name]
    assert len(found) == 1, found
    return found[0]


# -- the value is in Secret Manager and nowhere else ----------------------------------


def test_a_value_reaches_secret_manager_and_no_response_log_or_document(client, seeded, sm,
                                                                        caplog):
    value = fake_value("q")
    rotated = fake_value("r")
    password = "pw" + "z" * 30
    caplog.set_level(logging.DEBUG)
    bodies = [
        create(client, "root", "eng", "typesafe-key", value),
        rotate(client, "root", "eng", "typesafe-key", rotated),
        create(client, "root", "eng", "staging-login", kind="basic_auth", provider="other",
               fields={"username": "ci-bot", "password": password}),
        grant(client, "root", "eng", "typesafe-key", "tool:jev-browser"),
        client.get(BASE, headers=as_("alice", "eng")),
        client.get(BASE, headers=as_("root", "eng")),
        # Refusals: the paths where a value is echoed by accident.
        create(client, "alice", "eng", "member-try", value),
        create(client, "root", "eng", "typesafe-key", value),
        create(client, "root", "eng", "bad-shape", value + "\n" + value),
        create(client, "root", "eng", "bad-kind", kind="basic_auth", provider="other",
               fields={value: password}),
        create(client, "root", "eng", "no-int", kind="basic_auth", provider="other",
               fields={value: 7, "password": password}),
        create(client, "root", "eng", "extra", value, tenant_id="research"),
        rotate(client, "root", "research", "typesafe-key", value),
        create(client, "root", "eng", value[:60], value),
    ]
    assert [b.status_code for b in bodies[:6]] == [201, 200, 201, 200, 200, 200]
    for response in bodies:
        for secret in (value, rotated, password):
            assert secret not in response.text, response.request.url
    for secret in (value, rotated, password):
        assert secret not in caplog.text
        assert secret not in repr(seeded.docs)
    # ...and it IS in Secret Manager, which is where it belongs.
    stored = set(sm.payloads.values())
    assert value.encode() in stored and rotated.encode() in stored
    assert json.loads(next(p for p in stored if p.startswith(b"{"))) == {
        "username": "ci-bot", "password": password}


def test_the_secret_id_is_the_frozen_spelling_with_a_random_wsec_suffix(client, seeded):
    assert create(client, "root", "eng", "typesafe-key", fake_value()).status_code == 201
    secret_id = record_of(seeded, "eng", "typesafe-key")["secret_id"]
    prefix = "swarm-tenant-eng-wsec-"
    assert secret_id.startswith(prefix)
    suffix = secret_id[len(prefix):]
    assert len(suffix) == 16 and int(suffix, 16) >= 0
    # The display name is never in the resource name (§2.1).
    assert "typesafe-key" not in secret_id


def test_the_list_serves_records_and_grants_never_a_value_or_a_length(client, seeded):
    value = fake_value()
    create(client, "root", "eng", "typesafe-key", value)
    grant(client, "root", "eng", "typesafe-key", "tool:jev-browser")
    body = listing(client, "alice", "eng")
    assert body["can_manage"] is False
    [row] = body["secrets"]
    assert row["name"] == "typesafe-key" and row["kind"] == "api_key"
    assert row["created_by"] == "root@saga.xyz" and row["created_at"]
    assert [g["consumer"] for g in row["grants"] if g["enabled"]] == ["tool:jev-browser"]
    assert len(row["versions"]) == 1
    text = json.dumps(row)
    assert str(len(value)) not in text and value[-6:] not in text


def test_the_production_writer_binds_no_one_and_reads_nothing():
    """§3.3: the tenant's worker account holds no read on a -wsec- secret.
    `credentials.SecretManagerCredentials` binds it; this writer must not."""

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __getattr__(self, name: str):
            def call(request: dict) -> Any:
                self.calls.append(name)
                if name == "add_secret_version":
                    return type("V", (), {"name": request["parent"] + "/versions/1"})()
                if name == "list_secret_versions":
                    return [type("V", (), {"name": request["parent"] + "/versions/1"})()]
                return None
            return call

    client = Client()
    writer = SecretManagerWorkspaceSecrets("p", client=client)
    writer.create_secret("eng", "swarm-tenant-eng-wsec-0123456789abcdef")
    writer.add_version("swarm-tenant-eng-wsec-0123456789abcdef", ("v" * 20).encode())
    writer.disable_version("projects/p/secrets/swarm-tenant-eng-wsec-0123456789abcdef/versions/1")
    writer.destroy_versions("swarm-tenant-eng-wsec-0123456789abcdef")
    assert client.calls == ["create_secret", "add_secret_version", "disable_secret_version",
                            "list_secret_versions", "destroy_secret_version"]
    assert not [c for c in client.calls if "iam" in c.lower() or "access" in c.lower()]
    assert not [m for m in dir(SecretManagerWorkspaceSecrets)
                if "access" in m or "read" in m or "iam" in m.lower()]


# -- the catalogue -------------------------------------------------------------------


def test_a_consumer_outside_the_catalogue_is_a_422(client, seeded):
    create(client, "root", "eng", "typesafe-key", fake_value())
    for consumer in ("profile:nope", "image:agent-runtime-browser", "agent-runtime-browser",
                     "command:jev-browser-mcp", "tool:jev", "profile:merge", "profile:mock"):
        response = grant(client, "root", "eng", "typesafe-key", consumer)
        assert response.status_code == 422, (consumer, response.text)
    assert not [p for p in seeded.docs if p.startswith(f"{GRANTS_COLLECTION}/")]


def test_a_kind_its_slot_refuses_and_a_locked_or_slotless_consumer_are_422(client, seeded):
    create(client, "root", "eng", "login", kind="basic_auth", provider="other",
           fields={"username": "ci-bot", "password": "pw" + "z" * 30})
    # jev-browser reads an API key in its typesafe slot.
    assert grant(client, "root", "eng", "login", "tool:jev-browser").status_code == 422
    # A profile's `files` slot takes any kind.
    assert grant(client, "root", "eng", "login", "profile:claude-code-browser").status_code == 200
    assert grant(client, "root", "eng", "login", "component:git").status_code == 422
    assert grant(client, "root", "eng", "login", "tool:playwright").status_code == 422
    assert grant(client, "root", "eng", "login", "profile:codex",
                 slot="typesafe").status_code == 422


def test_profiles_are_the_agent_profiles_of_the_frozen_catalogue():
    agent = {n for n, p in RUNNER_PROFILES.items()
             if p.worker_action is None and p.provider is not None}
    assert set(PROFILE_NAMES) == agent
    assert {"claude-code", "claude-code-browser"} <= agent
    assert not {"merge", "post-verdict", "mock"} & set(PROFILE_NAMES)


def test_tools_are_the_frozen_browser_tool_choices():
    choices = set(RUNNER_PROFILES["claude-code-browser"].inputs["browser_tool"].choices)
    assert choices - {"auto"} == set(BROWSER_TOOLS)
    assert {f"tool:{t}" for t in BROWSER_TOOLS.values()} \
        == {c for c in CONSUMERS_BY_NAME if c.startswith("tool:")}


def test_the_consumers_route_serves_the_catalogue(client, seeded):
    response = client.get(f"{BASE}/consumers", headers=as_("alice", "eng"))
    assert response.status_code == 200
    rows = {c["consumer"]: c for c in response.json()["consumers"]}
    assert rows["tool:jev-browser"]["grantable"] is True
    assert rows["component:git"]["grantable"] is False
    assert set(rows) == set(CONSUMERS_BY_NAME)


# -- isolation ---------------------------------------------------------------------


def test_another_tenants_secret_is_a_404_on_every_route(client, seeded, sm):
    create(client, "root", "eng", "typesafe-key", fake_value())
    calls_before = list(sm.calls)
    # bob is in research only; research has no secret of that name.
    assert listing(client, "bob", "research")["secrets"] == []
    root_in_research = {**auth_header("root"), TENANT_HEADER: "research"}
    # root is an admin but not a research member: the tenant switch refuses.
    assert client.get(BASE, headers=root_in_research).status_code in (403, 404)
    seed_tenant(seeded, "ops")
    for who, tenant in (("bob", "research"), ("bob", "u-bob")):
        assert rotate(client, who, tenant, "typesafe-key", fake_value("w")).status_code in (403, 404)
        assert grant(client, who, tenant, "typesafe-key", "profile:codex").status_code in (403, 404)
        assert delete(client, who, tenant, "typesafe-key",
                      {"name": "typesafe-key"}).status_code in (403, 404)
    # In bob's own workspace he manages, so the refusal is the 404 itself.
    assert rotate(client, "bob", "u-bob", "typesafe-key", fake_value("w")).status_code == 404
    assert grant(client, "bob", "u-bob", "typesafe-key", "profile:codex").status_code == 404
    assert delete(client, "bob", "u-bob", "typesafe-key",
                  {"name": "typesafe-key"}).status_code == 404
    assert sm.calls == calls_before
    assert record_of(seeded, "eng", "typesafe-key")["state"] == "active"


def test_no_body_takes_a_tenant_an_image_or_a_command(client, seeded, sm):
    for extra in ({"tenant_id": "research"}, {"image": "agent-runtime-base"},
                  {"command": "jev-browser-mcp"}):
        assert create(client, "root", "eng", "x-key", fake_value(), **extra).status_code == 422
    assert sm.calls == []


# -- who writes (Q4) -----------------------------------------------------------------


def test_a_members_write_in_a_group_workspace_is_403_and_stores_nothing(client, seeded, sm):
    create(client, "root", "eng", "typesafe-key", fake_value())
    calls_before = list(sm.calls)
    audit_count = len(audits(seeded))
    refused = [
        create(client, "alice", "eng", "mine", fake_value("m")),
        rotate(client, "alice", "eng", "typesafe-key", fake_value("m")),
        grant(client, "alice", "eng", "typesafe-key", "tool:jev-browser"),
        delete(client, "alice", "eng", "typesafe-key", {"name": "typesafe-key"}),
        client.put(f"{BASE}/typesafe-key/use-in", headers=as_("alice", "eng"),
                   json={"workspaces": []}),
        client.post(f"{BASE}/links", headers=as_("alice", "eng"), json={"action": "add"}),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused), [r.text for r in refused]
    assert sm.calls == calls_before
    assert len(audits(seeded)) == audit_count
    # She still reads names and grants.
    assert [s["name"] for s in listing(client, "alice", "eng")["secrets"]] == ["typesafe-key"]


def test_a_personal_workspace_is_managed_by_its_person(client, seeded):
    response = create(client, "alice", "u-alice", "my-typesafe", fake_value())
    assert response.status_code == 201, response.text
    assert response.json()["secret"]["scope"] == "personal"
    assert listing(client, "alice", "u-alice")["can_manage"] is True
    assert grant(client, "alice", "u-alice", "my-typesafe", "tool:jev-browser").status_code == 200
    # Nobody else can even select it.
    assert client.get(BASE, headers=as_("root", "u-alice")).status_code in (403, 404)


# -- Q3: the personal opt-in list ----------------------------------------------------


def test_a_personal_secret_opts_in_to_group_workspaces_its_person_is_in(client, seeded):
    create(client, "alice", "u-alice", "my-typesafe", fake_value())
    url = f"{BASE}/my-typesafe/use-in"
    ok = client.put(url, headers=as_("alice", "u-alice"), json={"workspaces": ["eng", "eng"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["secret"]["use_in_workspaces"] == ["eng"]
    for bad in (["research"], ["u-bob"], ["u-alice"], ["nowhere"]):
        response = client.put(url, headers=as_("alice", "u-alice"), json={"workspaces": bad})
        assert response.status_code == 422, (bad, response.text)
    assert record_of(seeded, "u-alice", "my-typesafe")["use_in_workspaces"] == ["eng"]
    # A group's secret carries no such list.
    create(client, "root", "eng", "team-key", fake_value())
    response = client.put(f"{BASE}/team-key/use-in", headers=as_("root", "eng"),
                          json={"workspaces": []})
    assert response.status_code == 422


# -- delete ------------------------------------------------------------------------


def test_delete_without_the_typed_name_is_422_and_destroys_nothing(client, seeded, sm):
    create(client, "root", "eng", "typesafe-key", fake_value())
    for body in (None, {}, {"name": "typesafe"}, {"name": "TYPESAFE-KEY"}, {"name": ""}):
        response = delete(client, "root", "eng", "typesafe-key", body)
        assert response.status_code == 422, (body, response.text)
    assert "destroy_versions" not in [c[0] for c in sm.calls]
    assert record_of(seeded, "eng", "typesafe-key")["state"] == "active"


def test_delete_with_the_typed_name_destroys_versions_and_disables_grants(client, seeded, sm):
    create(client, "root", "eng", "typesafe-key", fake_value())
    grant(client, "root", "eng", "typesafe-key", "tool:jev-browser")
    secret_id = record_of(seeded, "eng", "typesafe-key")["secret_id"]
    response = delete(client, "root", "eng", "typesafe-key", {"name": "typesafe-key"})
    assert response.status_code == 200, response.text
    assert response.json()["grants_disabled"] == ["tool:jev-browser"]
    assert set(sm.versions_of(secret_id).values()) == {"DESTROYED"}
    grant_doc = seeded.docs[f"{GRANTS_COLLECTION}/{secret_id}~tool:jev-browser"]
    assert grant_doc["enabled"] is False          # disabled, never deleted (§2.2)
    assert listing(client, "root", "eng")["secrets"] == []
    # The name is free again.
    assert create(client, "root", "eng", "typesafe-key", fake_value("n")).status_code == 201


# -- rotation (Q5) -------------------------------------------------------------------


def test_a_rotation_keeps_the_old_version_enabled_until_the_24h_sweep(client, seeded, sm, clock):
    create(client, "root", "eng", "typesafe-key", fake_value("a"))
    secret_id = record_of(seeded, "eng", "typesafe-key")["secret_id"]
    [first] = sm.versions_of(secret_id)
    response = rotate(client, "root", "eng", "typesafe-key", fake_value("b"))
    assert response.status_code == 200, response.text
    current = response.json()["secret"]["current_version"]
    assert current != first
    assert sm.versions_of(secret_id) == {first: "ENABLED", current: "ENABLED"}

    sweep = "/v1/admin/workspace-secrets/rotation-sweep"
    clock.at = START + ROTATION_GRACE - timedelta(seconds=1)
    assert client.post(sweep, headers=auth_header("root")).json()["disabled"] == 0
    assert sm.versions_of(secret_id)[first] == "ENABLED"

    clock.at = START + ROTATION_GRACE + timedelta(seconds=1)
    result = client.post(sweep, headers=auth_header("root")).json()
    assert result["disabled"] == 1 and result["failed"] == []
    assert sm.versions_of(secret_id) == {first: "DISABLED", current: "ENABLED"}
    assert "destroy_versions" not in [c[0] for c in sm.calls]
    states = {v["version"]: v["state"] for v in record_of(seeded, "eng", "typesafe-key")["versions"]}
    assert states == {first: "disabled", current: "enabled"}
    # A second sweep finds nothing more to do.
    assert client.post(sweep, headers=auth_header("root")).json()["disabled"] == 0


def test_the_rotation_sweep_is_an_admins(client, seeded):
    response = client.post("/v1/admin/workspace-secrets/rotation-sweep", headers=auth_header("alice"))
    assert response.status_code == 403


# -- the audit -----------------------------------------------------------------------


def test_every_write_has_its_audit_row_in_the_same_transaction(client, seeded, clock):
    seeded.commits.clear()
    assert create(client, "root", "eng", "typesafe-key", fake_value("a")).status_code == 201
    assert rotate(client, "root", "eng", "typesafe-key", fake_value("b")).status_code == 200
    assert grant(client, "root", "eng", "typesafe-key", "tool:jev-browser").status_code == 200
    assert grant(client, "root", "eng", "typesafe-key", "tool:jev-browser",
                 enabled=False).status_code == 200
    assert create(client, "alice", "u-alice", "mine", fake_value("c")).status_code == 201
    assert client.put(f"{BASE}/mine/use-in", headers=as_("alice", "u-alice"),
                      json={"workspaces": ["eng"]}).status_code == 200
    clock.at = START + ROTATION_GRACE + timedelta(minutes=1)
    assert client.post("/v1/admin/workspace-secrets/rotation-sweep",
                       headers=auth_header("root")).json()["disabled"] == 1
    assert delete(client, "root", "eng", "typesafe-key",
                  {"name": "typesafe-key"}).status_code == 200

    actions = [e["action"] for e in audits(seeded)]
    assert actions == [
        "workspace_secret_created", "workspace_secret_rotated",
        "workspace_secret_grant_enabled", "workspace_secret_grant_disabled",
        "workspace_secret_created", "workspace_secret_use_in_set",
        "workspace_secret_version_disabled", "workspace_secret_deleted",
    ]
    writes = [c for c in seeded.commits
              if any(p.startswith(("workspace_secret", f"{AUDIT_COLLECTION}/")) for p in c)]
    assert len(writes) == len(actions)
    for paths in writes:
        audit_rows = [p for p in paths if p.startswith(f"{AUDIT_COLLECTION}/")]
        records = [p for p in paths if p.startswith("workspace_secret")]
        assert len(audit_rows) == 1 and records, paths
    for entry in audits(seeded):
        assert entry["by"] in ("root@saga.xyz", "alice@saga.xyz") and entry["at"]
        assert entry["detail"]["secret_id"] == entry["target_secret_id"]


# -- Q7: the platform's other credentials, read-only -------------------------------------


def test_provider_keys_and_pool_accounts_are_listed_read_only_without_values(client, seeded,
                                                                            pool):
    marker = "cred" + "-" + "k" * 30
    pool.accounts["eng:team-max"]["credential"] = marker
    body = listing(client, "alice", "eng")["platform_credentials"]
    [key] = body["runner_provider_keys"]
    assert key["secret_id"] == "swarm-tenant-eng-anthropic" and key["read_only"] is True
    assert "profile:claude-code" in key["consumers"]
    [acct] = body["pool_accounts"]["accounts"]
    assert acct["account_id"] == "eng:team-max" and acct["read_only"] is True
    assert marker not in json.dumps(body)


def test_an_unreadable_pool_reads_as_unreadable_not_empty(client, seeded, pool):
    pool._error = RuntimeError("broker down " + "e" * 10)
    body = listing(client, "alice", "eng")["platform_credentials"]["pool_accounts"]
    assert body["readable"] is False and "e" * 10 not in body["reason"]


# -- the one-time link ----------------------------------------------------------------


def test_a_form_link_is_single_use_and_bound_to_its_minter(client, seeded):
    response = client.post(f"{BASE}/links", headers=as_("root", "eng"), json={"action": "add"})
    assert response.status_code == 201, response.text
    token = response.json()["path"].split("&t=")[1]
    assert token not in repr(seeded.docs)
    # Someone else presenting it is refused, and does not burn it.
    assert create(client, "alice", "u-alice", "x-key", fake_value(),
                  link_token=token).status_code == 403
    first = create(client, "root", "eng", "x-key", fake_value(), link_token=token)
    assert first.status_code == 201 and first.json()["via_link"] is True
    assert create(client, "root", "eng", "y-key", fake_value(), link_token=token).status_code == 403
