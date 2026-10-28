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
    CONSUMERS_BY_NAME,
    GRANTS_COLLECTION,
    PROFILE_NAMES,
    ROTATION_GRACE,
    SECRETS_COLLECTION,
    SecretManagerWorkspaceSecrets,
)
from swarm_common.models import PLATFORM_COMPONENTS
from swarm_common.profiles import RUNNER_PROFILES, RUNTIME_TOOLS

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
        "alice@example.com": (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "dave@example.com": (ENG_GROUP,),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
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
    owner = f"{user}@example.com"
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
    assert row["created_by"] == "root@example.com" and row["created_at"]
    assert [g["consumer"] for g in row["grants"] if g["enabled"]] == ["tool:jev-browser"]
    assert len(row["versions"]) == 1
    text = json.dumps(row)
    assert value[-6:] not in text
    # Never a length: no number in the row equals the value's length, and no key
    # names one. A substring test on the JSON text was flaky: a two-digit length
    # like "43" appears inside timestamps (main CI, 2026-10-15, PR 1361's run).
    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                assert not any(w in k.lower() for w in ("length", "len", "size", "bytes")), k
                yield from walk(v, k)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v, key)
        else:
            yield key, node
    assert [(k, v) for k, v in walk(row) if isinstance(v, int) and not isinstance(v, bool) and v == len(value)] == []


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
    # A profile's `files` slot takes any kind. claude-code-browser declares two
    # slots (contract request 117), so a grant to it names one.
    assert grant(client, "root", "eng", "login", "profile:claude-code-browser",
                 slot="files").status_code == 200
    assert grant(client, "root", "eng", "login", "profile:claude-code-browser").status_code == 422
    assert grant(client, "root", "eng", "login", "component:git").status_code == 422
    assert grant(client, "root", "eng", "login", "tool:playwright").status_code == 422
    assert grant(client, "root", "eng", "login", "profile:codex",
                 slot="typesafe").status_code == 422


def test_profiles_are_the_agent_profiles_of_the_frozen_catalogue():
    agent = {n for n, p in RUNNER_PROFILES.items()
             if p.worker_action is None and p.provider is not None}
    # Contract request 80: the profiles that declare a slot are the agent profiles.
    assert set(PROFILE_NAMES) == agent == {n for n, p in RUNNER_PROFILES.items() if p.secret_slots}
    assert {"claude-code", "claude-code-browser"} <= agent
    assert not {"merge", "post-verdict", "mock"} & set(PROFILE_NAMES)
    for name in PROFILE_NAMES:
        served = {s.name: s.kinds for s in CONSUMERS_BY_NAME[f"profile:{name}"].slots}
        assert served == {k: v.kinds for k, v in RUNNER_PROFILES[name].secret_slots.items()}


def test_tools_and_components_are_the_contracts():
    # Contract requests 81 and 82: no interim list of its own.
    assert {c for c in CONSUMERS_BY_NAME if c.startswith("tool:")} \
        == {f"tool:{t}" for t in RUNTIME_TOOLS}
    assert {c for c in CONSUMERS_BY_NAME if c.startswith("component:")} \
        == {f"component:{c}" for c in PLATFORM_COMPONENTS}
    typesafe = CONSUMERS_BY_NAME["tool:jev-browser"].slot("typesafe")
    assert typesafe is not None
    assert typesafe.kinds == RUNTIME_TOOLS["jev-browser"].secret_slots["typesafe"].kinds


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
        assert entry["by"] in ("root@example.com", "alice@example.com") and entry["at"]
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


# -- SSC-REPO-SCOPE: a registered repository's secrets for agents -------------------
#
# docs/design/secrets-scopes.md §2.1, §2.5 and §6.2 (owner decisions Q2 and Q9
# of its §11). The GitHub-admin half of Q2 is test_github_repo_admin.py's.

from swarm_api.grant_attest import attested_form  # noqa: E402
from swarm_api.repositories import repo_id_for  # noqa: E402

WIDGETS = repo_id_for("eng", "acme", "widgets")
REPO_BASE = f"/v1/repositories/{WIDGETS}/secrets"


def register(db: FakeFirestore, tenant_id: str = "eng", owner: str = "acme",
             repo: str = "widgets") -> str:
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github", "owner": owner,
        "repo": repo, "repository_url": f"https://github.com/{owner}/{repo}"}
    return repo_id


def repo_create(client, who: str, tenant: str, name: str, value: str, *,
                base: str = REPO_BASE, **extra):
    return client.post(base, headers=as_(who, tenant),
                       json={"name": name, "provider": "other", "kind": "api_key",
                             "value": value, **extra})


def test_a_repository_secret_is_bound_to_its_registration_and_kept_off_the_workspace_list(
        client, seeded, sm):
    register(seeded)
    value = fake_value()
    response = repo_create(client, "root", "eng", "test-db-password", value)
    assert response.status_code == 201, response.text
    body = response.json()["secret"]
    assert (body["scope"], body["repo_id"], body["repository"]) == \
        ("repository", WIDGETS, "acme/widgets")
    assert body["secret_id"].startswith("swarm-tenant-eng-wsec-")
    assert value not in response.text
    # The same name may exist once for the workspace and once per repository.
    assert create(client, "root", "eng", "test-db-password", fake_value("w")).status_code == 201
    assert [s["name"] for s in listing(client, "root", "eng")["secrets"]] == ["test-db-password"]
    assert listing(client, "root", "eng")["secrets"][0]["scope"] == "workspace"
    repo_list = client.get(REPO_BASE, headers=as_("dave", "eng"))
    assert repo_list.status_code == 200, repo_list.text
    assert [(s["name"], s["repo_id"]) for s in repo_list.json()["secrets"]] == \
        [("test-db-password", WIDGETS)]
    assert repo_list.json()["can_manage"] is False
    # A second of the same name for the repository is a conflict.
    again = repo_create(client, "root", "eng", "test-db-password", fake_value("x"))
    assert again.status_code == 409, again.text


def test_another_workspaces_repo_id_is_404_on_every_route(client, seeded, sm):
    register(seeded)
    theirs = register(seeded, "research", "acme", "widgets")
    assert theirs != WIDGETS
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    # bob is research's: eng's registration is not his workspace's, whatever it names.
    for method, path, body in (
            ("GET", REPO_BASE, None),
            ("POST", REPO_BASE, {"name": "y-key", "provider": "other", "kind": "api_key",
                                 "value": fake_value()}),
            ("PUT", f"{REPO_BASE}/x-key/value", {"value": fake_value()}),
            ("PUT", f"{REPO_BASE}/x-key/grants/profile:claude-code", {"enabled": True}),
            ("DELETE", f"{REPO_BASE}/x-key", {"name": "x-key"}),
            ("POST", f"{REPO_BASE}/links", {"action": "add"})):
        response = client.request(method, path, headers=as_("bob", "research"), json=body)
        assert response.status_code == 404, (method, path, response.text)
    # And a repo_id that is no registration at all.
    unknown = client.get(f"/v1/repositories/{repo_id_for('eng', 'acme', 'nope')}/secrets",
                         headers=as_("root", "eng"))
    assert unknown.status_code == 404


def test_a_member_is_403_and_their_value_never_reaches_secret_manager(client, seeded, sm):
    """dave is an eng member with no GitHub connection: neither half of Q2."""
    register(seeded)
    response = repo_create(client, "dave", "eng", "x-key", fake_value())
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "github_not_connected"
    assert sm.payloads == {}
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    for method, path, body in (
            ("PUT", f"{REPO_BASE}/x-key/value", {"value": fake_value("z")}),
            ("PUT", f"{REPO_BASE}/x-key/grants/profile:claude-code", {"enabled": True}),
            ("DELETE", f"{REPO_BASE}/x-key", {"name": "x-key"}),
            ("POST", f"{REPO_BASE}/links", {"action": "add"})):
        refused = client.request(method, path, headers=as_("dave", "eng"), json=body)
        assert refused.status_code == 403, (method, path, refused.text)


def test_a_repository_grant_is_signed_v2_with_its_repository(db, tokens, group_map, objects,
                                                             clock, sm, pool):
    key = __import__("secrets").token_urlsafe(32)
    ctx = build_context(
        settings=api_settings(console_url=CONSOLE, workspace_grant_key=key), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), now=clock,
    )
    app = create_app(ctx)
    app.state.workspace_secret_writer = sm
    app.state.account_pool = pool
    keyed = TestClient(app, raise_server_exceptions=False)
    seed_tenant(db, "eng")
    register(db)
    assert repo_create(keyed, "root", "eng", "x-key", fake_value()).status_code == 201
    response = keyed.put(f"{REPO_BASE}/x-key/grants/profile:claude-code",
                         headers=as_("root", "eng"), json={"enabled": True})
    assert response.status_code == 200, response.text
    [grant] = [d for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["repo_id"] == WIDGETS and attested_form(grant) == "v2"


def test_an_mcp_server_is_never_granted_a_repository_secret(client, seeded, sm):
    register(seeded)
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    response = client.put(f"{REPO_BASE}/x-key/grants/mcp:docs", headers=as_("root", "eng"),
                          json={"enabled": True, "slot": "auth"})
    assert response.status_code == 422, response.text
    assert not [p for p in seeded.docs if p.startswith(f"{GRANTS_COLLECTION}/")]


def test_enabling_a_repository_grant_without_a_grant_key_is_switched(client, seeded, sm,
                                                                     monkeypatch):
    """Off (the default): written, unattested, and inert (the release drops
    it: test_grant_attest.py). On: 422 grant_key_required, nothing written."""
    register(seeded)
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    path = f"{REPO_BASE}/x-key/grants/profile:claude-code"
    monkeypatch.setenv("REFUSAL_GRANT_KEY_REQUIRED", "on")
    refused = client.put(path, headers=as_("root", "eng"), json={"enabled": True})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "grant_key_required"
    assert not [p for p in seeded.docs if p.startswith(f"{GRANTS_COLLECTION}/")]
    monkeypatch.delenv("REFUSAL_GRANT_KEY_REQUIRED")
    written = client.put(path, headers=as_("root", "eng"), json={"enabled": True})
    assert written.status_code == 200, written.text
    [grant] = [d for p, d in seeded.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["attestation"] is None and grant["repo_id"] == WIDGETS


def test_a_repository_secret_is_rotated_and_deleted_on_its_own_path(client, seeded, sm):
    register(seeded)
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    assert create(client, "root", "eng", "x-key", fake_value("w")).status_code == 201
    rotated = client.put(f"{REPO_BASE}/x-key/value", headers=as_("root", "eng"),
                         json={"value": fake_value("r")})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["secret"]["repo_id"] == WIDGETS
    gone = client.request("DELETE", f"{REPO_BASE}/x-key", headers=as_("root", "eng"),
                          json={"name": "x-key"})
    assert gone.status_code == 200, gone.text
    # The workspace's own x-key is untouched.
    assert [s["name"] for s in listing(client, "root", "eng")["secrets"]] == ["x-key"]
    assert client.get(REPO_BASE, headers=as_("root", "eng")).json()["secrets"] == []


def test_a_repository_form_link_is_bound_to_its_repository(client, seeded, sm):
    register(seeded)
    minted = client.post(f"{REPO_BASE}/links", headers=as_("root", "eng"),
                         json={"action": "add"})
    assert minted.status_code == 201, minted.text
    assert minted.json()["path"].startswith(f"/config/secrets?scope=repository&repo={WIDGETS}")
    token = minted.json()["path"].rsplit("t=", 1)[1]
    # Not for the workspace's own form ...
    wrong = create(client, "root", "eng", "x-key", fake_value(), link_token=token)
    assert wrong.status_code == 403, wrong.text
    # ... and once for the repository's.
    used = repo_create(client, "root", "eng", "x-key", fake_value(), link_token=token)
    assert used.status_code == 201 and used.json()["via_link"] is True
    again = repo_create(client, "root", "eng", "y-key", fake_value(), link_token=token)
    assert again.status_code == 403


def test_disable_grants_turns_a_repositorys_grants_off_for_a_manager_only(client, seeded, sm):
    register(seeded)
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    assert client.put(f"{REPO_BASE}/x-key/grants/profile:claude-code",
                      headers=as_("root", "eng"), json={"enabled": True}).status_code == 200
    path = f"{REPO_BASE}:disable-grants"
    assert client.post(path, headers=as_("dave", "eng")).status_code == 403
    answer = client.post(path, headers=as_("root", "eng"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["grants_disabled"] == {"x-key": ["profile:claude-code"]}
    [grant] = [d for p, d in seeded.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["enabled"] is False


# -- SSC-REPO-DELETE: removing a registration disables its secrets' grants ----------
#
# secrets-scopes.md §2.1: "A removed registration disables every grant on its
# secrets in the transaction that removes it." `DELETE /v1/repositories/{id}`
# disables them and deletes in one transaction (`delete_repository`, lane
# SSC-REPO-DELETE-TXN below), and fails closed.

from swarm_api.grant_attest import GrantKeys  # noqa: E402
from swarm_api.workspace_secrets import AUDIT_REPOSITORY_GRANTS_DISABLED, WorkspaceSecrets  # noqa: E402


@pytest.fixture
def grant_key() -> str:
    return __import__("secrets").token_urlsafe(32)


@pytest.fixture
def keyed(db, tokens, group_map, objects, clock, sm, pool, grant_key) -> TestClient:
    ctx = build_context(
        settings=api_settings(console_url=CONSOLE, workspace_grant_key=grant_key), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), now=clock,
    )
    app = create_app(ctx)
    app.state.workspace_secret_writer = sm
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _granted(client, db: FakeFirestore) -> dict:
    register(db)
    assert repo_create(client, "root", "eng", "x-key", fake_value()).status_code == 201
    response = client.put(f"{REPO_BASE}/x-key/grants/profile:claude-code",
                          headers=as_("root", "eng"), json={"enabled": True})
    assert response.status_code == 200, response.text
    [grant] = [d for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["enabled"] is True
    return grant


def test_deleting_a_registration_disables_its_secrets_grants_re_signed_and_audited(
        keyed, seeded, sm, grant_key, caplog):
    _granted(keyed, seeded)
    with caplog.at_level(logging.INFO, logger="swarm_api.routes.repositories"):
        response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code == 204, response.text
    assert f"repositories/{WIDGETS}" not in seeded.docs
    [grant] = [d for p, d in seeded.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["enabled"] is False and grant["repo_id"] == WIDGETS
    assert grant["changed_by"] == "dave@example.com"
    assert GrantKeys(grant_key).check(grant) == "valid"
    [row] = [a for a in audits(seeded) if a.get("action") == AUDIT_REPOSITORY_GRANTS_DISABLED]
    assert row["detail"]["repo_id"] == WIDGETS
    assert row["detail"]["grants_disabled"] == ["profile:claude-code"]
    [line] = [r.getMessage() for r in caplog.records if "repository delete" in r.getMessage()]
    assert "grants_disabled=1" in line
    assert not any(v in caplog.text for v in (sm.payloads[k].decode() for k in sm.payloads))


def test_re_registering_a_deleted_repository_revives_no_grant(keyed, seeded, sm):
    _granted(keyed, seeded)
    assert keyed.delete(f"/v1/repositories/{WIDGETS}",
                        headers=as_("dave", "eng")).status_code == 204
    register(seeded)  # the same owner/repo, so the same repo_id
    listed = keyed.get(REPO_BASE, headers=as_("root", "eng"))
    assert listed.status_code == 200, listed.text
    [secret] = listed.json()["secrets"]
    assert all(g["enabled"] is False for g in secret["grants"]), secret["grants"]
    assert all(d["enabled"] is False for p, d in seeded.docs.items()
               if p.startswith(f"{GRANTS_COLLECTION}/"))


def test_a_disable_that_raises_leaves_the_registration_and_answers_an_error(
        keyed, seeded, sm, monkeypatch):
    _granted(keyed, seeded)

    def broken(self, **kwargs):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(WorkspaceSecrets, "delete_repository", broken)
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code >= 500, response.text
    assert f"repositories/{WIDGETS}" in seeded.docs
    [grant] = [d for p, d in seeded.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["enabled"] is True


def test_deleting_a_registration_with_no_secrets_answers_204(client, seeded, sm, caplog):
    register(seeded)
    with caplog.at_level(logging.INFO, logger="swarm_api.routes.repositories"):
        response = client.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code == 204, response.text
    assert f"repositories/{WIDGETS}" not in seeded.docs
    assert "grants_disabled=0" in caplog.text
    assert not [a for a in audits(seeded) if a.get("action") == AUDIT_REPOSITORY_GRANTS_DISABLED]


def test_another_workspaces_repo_id_is_still_404_and_touches_no_grant(keyed, seeded, sm):
    _granted(keyed, seeded)
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("bob", "research"))
    assert response.status_code == 404, response.text
    assert f"repositories/{WIDGETS}" in seeded.docs
    [grant] = [d for p, d in seeded.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert grant["enabled"] is True
    assert not [a for a in audits(seeded) if a.get("action") == AUDIT_REPOSITORY_GRANTS_DISABLED]


# -- SSC-REPO-DELETE-TXN: the disable and the removal are ONE transaction --------
#
# secrets-scopes.md §2.1 says "in the transaction that removes it". Two writes
# left a window: a grant enabled between them survived the delete, and a
# re-registration of the same repository revived it. Contention is modelled the
# way `test_cancel_retries_contention.py` models it: the REAL
# `firestore.transactional` drives a fake whose commit is aborted, and what the
# other writer committed first is visible to the retry.

from google.api_core import exceptions as gexc  # noqa: E402

from swarm_api.grant_attest import GrantPolicy  # noqa: E402


class _RemovalTransaction(RecordingTransaction):
    """Calls `db.on_removal(self)` before committing any transaction that
    deletes `repositories/{WIDGETS}`; the hook may raise to abort it."""

    def _commit(self) -> list[Any]:
        if any(op == "delete" and ref.path == f"repositories/{WIDGETS}"
               for op, ref, _ in self._buffer):
            self._db.on_removal(self)
        return super()._commit()


@pytest.fixture
def removal(seeded, monkeypatch) -> RecordingFirestore:
    seeded.on_removal = lambda txn: None
    monkeypatch.setattr(seeded, "transaction", lambda **kw: _RemovalTransaction(seeded))
    return seeded


def _repo_grant(client, name: str, consumer: str) -> None:
    response = client.put(f"{REPO_BASE}/{name}/grants/{consumer}",
                          headers=as_("root", "eng"), json={"enabled": True})
    assert response.status_code == 200, response.text


def _grants(db: FakeFirestore) -> list[dict]:
    return [d for p, d in db.docs.items() if p.startswith(f"{GRANTS_COLLECTION}/")]


def test_a_grant_enabled_while_the_delete_commits_is_disabled_by_its_retry(
        keyed, removal, sm, clock, grant_key):
    _granted(keyed, removal)
    store = WorkspaceSecrets(removal, sm, now=clock,
                             grants=GrantPolicy(keys=GrantKeys(grant_key), required=False))
    contended: list[int] = []

    def another_manager_wins_the_lock(txn) -> None:
        # The first commit loses to a manager who enabled a second consumer
        # on the same secret: their write lands, this commit aborts unapplied.
        if contended:
            return
        contended.append(1)
        store.set_grant(tenant_id="eng", name="x-key", consumer="profile:codex",
                        slot=None, enabled=True, by="root@example.com", repo_id=WIDGETS)
        txn._buffer = []
        raise gexc.Aborted("another transaction holds the lock")

    removal.on_removal = another_manager_wins_the_lock
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code == 204, response.text
    assert contended == [1]
    assert f"repositories/{WIDGETS}" not in removal.docs
    grants = _grants(removal)
    assert sorted(g["consumer"] for g in grants) == ["profile:claude-code", "profile:codex"]
    assert all(g["enabled"] is False for g in grants), grants
    assert all(GrantKeys(grant_key).check(g) == "valid" for g in grants)
    register(removal)  # the same repo_id: nothing comes back
    assert all(g["enabled"] is False for g in _grants(removal))


def test_a_grant_enabled_after_the_delete_committed_is_refused(
        keyed, removal, sm, clock, grant_key, monkeypatch):
    """The other order: the delete commits between `set_grant`'s read of the
    secret and its transaction. The enable re-reads the registration in its
    transaction, as `create` does, and finds it gone."""
    _granted(keyed, removal)
    found = WorkspaceSecrets.find
    store = WorkspaceSecrets(removal, sm, now=clock,
                             grants=GrantPolicy(keys=GrantKeys(grant_key), required=False))

    def find_then_lose_the_race(self, tenant_id, name, repo_id=None):
        doc = found(self, tenant_id, name, repo_id)
        monkeypatch.setattr(WorkspaceSecrets, "find", found)
        store.delete_repository(tenant_id="eng", repo_id=WIDGETS, by="dave@example.com")
        return doc

    monkeypatch.setattr(WorkspaceSecrets, "find", find_then_lose_the_race)
    response = keyed.put(f"{REPO_BASE}/x-key/grants/profile:codex",
                         headers=as_("root", "eng"), json={"enabled": True})
    assert response.status_code == 404, response.text
    assert f"repositories/{WIDGETS}" not in removal.docs
    assert all(g["enabled"] is False for g in _grants(removal)), _grants(removal)


def test_the_disable_and_the_removal_land_in_one_commit(keyed, removal, sm):
    _granted(keyed, removal)
    assert repo_create(keyed, "root", "eng", "y-key", fake_value("y")).status_code == 201
    _repo_grant(keyed, "y-key", "profile:codex")
    _repo_grant(keyed, "y-key", "profile:indexer")
    before = len(removal.commits)
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code == 204, response.text
    [commit] = removal.commits[before:]
    assert f"repositories/{WIDGETS}" in commit
    grant_paths = sorted(p for p in removal.docs if p.startswith(f"{GRANTS_COLLECTION}/"))
    assert len(grant_paths) == 3 and set(grant_paths) <= set(commit)
    assert sum(p.startswith(f"{AUDIT_COLLECTION}/") for p in commit) == 2
    assert all(g["enabled"] is False for g in _grants(removal))
    assert f"repositories/{WIDGETS}" not in removal.docs


def test_a_removal_that_cannot_commit_leaves_the_grants_and_the_registration(
        keyed, removal, sm):
    _granted(keyed, removal)
    before = {p: dict(d) for p, d in removal.docs.items()}

    def always_contended(txn) -> None:
        txn._buffer = []
        raise gexc.Aborted("another transaction holds the lock")

    removal.on_removal = always_contended
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code >= 500, response.text
    assert f"repositories/{WIDGETS}" in removal.docs
    [grant] = _grants(removal)
    assert grant["enabled"] is True
    assert removal.docs == before


def test_a_removal_over_firestores_write_cap_refuses_whole(keyed, removal, sm, monkeypatch):
    """One grant, its audit row and the delete are three writes: under a cap
    of two the removal is a 409 naming the count, and nothing is written."""
    import swarm_api.workspace_secrets as module

    _granted(keyed, removal)
    before = {p: dict(d) for p, d in removal.docs.items()}
    monkeypatch.setattr(module, "MAX_TRANSACTION_WRITES", 2)
    response = keyed.delete(f"/v1/repositories/{WIDGETS}", headers=as_("dave", "eng"))
    assert response.status_code == 409, response.text
    assert "3 documents" in response.text and "disable-grants" in response.text
    assert removal.docs == before


# -- SEC-MIGRATE-FORGE: forge tokens listed read-only (part of #1347) ----------------
#
# docs/design/workspace-secrets.md §6.2: the store lists `-git` and
# `-git-u-<hex>` as records of kind `forge_token`, `managed: legacy`, consumer
# `component:git` ticked and locked, from the records `gittokens.py` keeps. It
# offers no add, rotate, delete or grant on one, and no forge value is ever in
# a release.

from swarm_api.gittokens import Scope, TokenState, provider_suffix, record_for_slot  # noqa: E402
from swarm_api.grant_attest import GrantPolicy  # noqa: E402
from swarm_api.secrets_overview import GIT_TOKENS_PAGE as FORGE_PAGE  # noqa: E402
from swarm_api.workspace_secrets import (  # noqa: E402
    _grant_id,
    legacy_rows,
    release_plan,
)
from swarm_common.models import Tenant  # noqa: E402

#: A user slot's name, `git-u-<16 hex>`: derived, so every xdist worker
#: collects the same parametrize ids.
USER_SLOT = provider_suffix(Scope.USER, user="alice@example.com")


def _eng(db: FakeFirestore, *credentials: str) -> Tenant:
    doc = db.docs["tenants/eng"]
    doc["credentials"] = list(credentials)
    return Tenant(**doc)


def test_the_tenant_forge_token_is_listed_read_only_and_never_as_a_provider_key(client, seeded):
    _eng(seeded, "anthropic", "git")
    body = listing(client, "root", "eng")["platform_credentials"]
    assert [k["provider"] for k in body["runner_provider_keys"]] == ["anthropic"]
    forge = body["forge_tokens"]
    [row] = forge["tokens"]
    assert row["name"] == "git" and row["secret_id"] == "swarm-tenant-eng-git"
    assert row["kind"] == "forge_token" and row["managed"] == "legacy"
    assert row["read_only"] is True and row["locked"] is True
    assert row["consumers"] == ["component:git"] and forge["page"] == FORGE_PAGE


def test_forge_rows_come_from_the_git_token_records_never_a_value_or_another_persons(seeded):
    tenant = _eng(seeded, "anthropic", "git")
    at = START
    last4 = __import__("secrets").token_hex(2)
    own = record_for_slot("eng", Scope.TENANT, registered_by="swarm-api", now=at)
    own.kind, own.state, own.last4 = "app_installation", TokenState.ACTIVE, last4
    mine = record_for_slot("eng", Scope.USER, user="alice@example.com",
                           registered_by="alice@example.com", now=at)
    mine.kind = "app_user"
    theirs = record_for_slot("eng", Scope.USER, user="dave@example.com",
                             registered_by="dave@example.com", now=at)
    repo = record_for_slot("eng", Scope.REPOSITORY, repo_id="repo_" + "0" * 16,
                           registered_by="root@example.com", now=at)
    other = record_for_slot("research", Scope.TENANT, registered_by="swarm-api", now=at)

    forge = legacy_rows(tenant, None, None, forge_records=[own, mine, theirs, repo, other],
                        viewer_email="Alice@example.com")["forge_tokens"]

    assert forge["readable"] is True
    assert [(r["name"], r["scope"]) for r in forge["tokens"]] == [
        ("git", "tenant"), (mine.provider_suffix, "user")]
    tenant_row, user_row = forge["tokens"]
    assert tenant_row["status"] == "active" and tenant_row["token_kind"] == "app_installation"
    assert user_row["token_kind"] == "app_user" and user_row["status"] == "unverified"
    for row in forge["tokens"]:
        assert row["kind"] == "forge_token" and row["read_only"] is True and row["locked"]
        assert row["consumers"] == ["component:git"]
    text = json.dumps(forge)
    assert last4 not in text and "@example.com" not in text


@pytest.mark.parametrize("slot", ["git", USER_SLOT, "git-r-" + "a" * 16])
def test_every_write_on_a_forge_row_is_refused_and_writes_nothing(client, seeded, sm, slot):
    _eng(seeded, "anthropic", "git")
    before = {p: dict(d) for p, d in seeded.docs.items()}
    attempts = [
        create(client, "root", "eng", slot, fake_value()),
        rotate(client, "root", "eng", slot, fake_value("r")),
        delete(client, "root", "eng", slot, {"name": slot}),
        grant(client, "root", "eng", slot, "component:git"),
        grant(client, "root", "eng", slot, "profile:claude-code"),
        client.put(f"{BASE}/{slot}/use-in", headers=as_("alice", "u-alice"),
                   json={"workspaces": ["eng"]}),
        client.post(f"{BASE}/links", headers=as_("root", "eng"),
                    json={"action": "rotate", "name": slot}),
    ]
    for response in attempts:
        assert response.status_code == 422, (response.request.method, response.request.url,
                                             response.text)
        assert "read-only" in response.text and FORGE_PAGE in response.text
    assert sm.calls == [] and seeded.docs == before


def test_no_forge_value_is_ever_in_a_release(keyed, seeded, grant_key):
    """A store record granted to claude-code is released (the control); the
    same record rewritten as a forge token, or a record forged for the tenant
    `-git` secret with signed grants to claude-code and component:git, is not."""
    assert create(keyed, "root", "eng", "x-key", fake_value()).status_code == 201
    assert grant(keyed, "root", "eng", "x-key", "profile:claude-code").status_code == 200
    record = record_of(seeded, "eng", "x-key")
    consumers = ("profile:claude-code", "component:git")
    keys = GrantKeys(grant_key)
    policy = GrantPolicy(keys, required=True)

    def released() -> list[str]:
        plan = release_plan(seeded, tenant_id="eng", consumers=consumers, project_id="test",
                            grants=policy, repositories=())
        return [d.record["secret_id"] for d in plan.due]

    assert released() == [record["secret_id"]]

    seeded.docs[f"{SECRETS_COLLECTION}/{record['secret_id']}"]["kind"] = "forge_token"
    assert released() == []

    seeded.docs[f"{SECRETS_COLLECTION}/{record['secret_id']}"]["kind"] = record["kind"]
    git = "swarm-tenant-eng-git"
    seeded.docs[f"{SECRETS_COLLECTION}/{git}"] = {
        **record, "secret_id": git, "name": "git", "kind": "forge_token",
        "current_version": f"projects/test/secrets/{git}/versions/1",
    }
    for consumer in consumers:
        slot = CONSUMERS_BY_NAME[consumer].slots[0].name
        forged = {"secret_id": git, "tenant_id": "eng", "consumer": consumer,
                  "slot": slot, "enabled": True}
        forged["attestation"] = keys.sign(forged)
        seeded.docs[f"{GRANTS_COLLECTION}/{_grant_id(git, consumer)}"] = forged
    assert git not in released()
