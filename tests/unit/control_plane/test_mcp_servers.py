"""Registered MCP servers: the registration API and the per-run credential sweep (MCP-API).

Design: docs/design/registered-mcp-servers.md §4.2, §4.4, §4.5 and §4.11, part
of #1433, with the owner's decisions of 2026-10-17 (Q1 admins register for a
group workspace, Q2 a per-run credential dies at task end or its expiry, Q6
public HTTPS only). Drives the real routes over the in-memory Firestore, with
Secret Manager an in-memory stand-in. Offline: no credentials, no emulator.

The attackers these stand in for:

  * ANOTHER WORKSPACE: its registrations are a 404 here, by any name.
  * A MEMBER WHO IS NOT AN ADMIN (Q1): reads, never writes.
  * A CALLER WHO TYPES AN INTERNAL ADDRESS (Q6): http, an IP literal, userinfo,
    another port, localhost or a reserved name is a 422, and the refusal
    never repeats the URL (a person can paste a token into one).
  * A LOG OR DATABASE READER: a runtime-built per-run credential is in no log
    line, no Firestore document and no response of these routes.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like
a credential).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.mcp_credentials import (
    COLLECTION as CREDENTIALS,
    McpCredentials,
    McpCredentialShape,
    RowKeys,
    check_credential,
    secret_prefix,
)
from swarm_api.mcp_servers import (
    COLLECTION as SERVERS,
    FLAG,
    McpServers,
    mcp_servers_enabled,
    server_id,
)
from swarm_api.workspace_secrets import (
    GRANTS_COLLECTION,
    WorkspaceSecrets,
    attempt_consumers,
    mcp_consumers,
)
from swarm_common.models import parse_consumer
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_task, seed_tenant
from .test_child_tasks_api import KEY, _client, worker_tokens  # noqa: F401  (a fixture)

ROOT = "root@saga.xyz"

#: The rows' attestation key: the test deployment's `swarm-child-key`.
KEYS = RowKeys(KEY)


def fake_credential(fill: str = "c") -> str:
    """Shaped like a bearer token, built at runtime so no literal looks like one."""
    return "mcp" + "-" + fill * 24 + secrets.token_hex(8)


@pytest.fixture
def on(monkeypatch) -> None:
    monkeypatch.setenv(FLAG, "on")


@pytest.fixture
def app_client(db, worker_tokens, group_map, objects, on) -> TestClient:  # noqa: F811
    """The real app, with a `swarm-child-key`, as a deployment that offers
    the attempt path (and so this feature) has."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return _client(db, worker_tokens, group_map, objects)


def kb(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "kb", "url": "https://kb.example.com/mcp",
                            "description": "the decisions graph",
                            "auth": {"mode": "dispatch"}}
    body.update(extra)
    return body


def servers_in(db) -> dict[str, dict[str, Any]]:
    return {p: d for p, d in db.docs.items() if p.startswith(f"{SERVERS}/")}


def audits(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{AUDIT_COLLECTION}/")
            and str(d.get("action", "")).startswith("mcp_server_")]


# --------------------------------------------------------------------------
# register, read, update; never delete
# --------------------------------------------------------------------------


def test_an_admin_registers_a_server_normalised_and_audited(app_client, db):
    response = app_client.post("/v1/mcp-servers", headers=auth_header("root"),
                               json=kb(url="https://KB.Example.com:443/mcp"))
    assert response.status_code == 201, response.text
    server = response.json()["server"]
    assert server["url"] == "https://kb.example.com/mcp"
    assert server["server_id"] == server_id("eng", "kb")
    assert server["tenant_id"] == "eng" and server["revision"] == 1
    assert server["transport"] == "streamable-http"
    assert server["auth"] == {"mode": "dispatch", "header": "Authorization", "scheme": "Bearer"}
    assert server["limits"] == {"max_request_bytes": 1048576, "max_response_bytes": 4194304,
                                "calls_per_minute": 60, "call_timeout_seconds": 60}
    assert server["consumer"] == "mcp:kb"
    assert server["created_by"] == ROOT
    [audit] = audits(db)
    assert audit["action"] == "mcp_server_registered"
    assert audit["detail"] == {"tenant_id": "eng", "server_id": server["server_id"],
                               "name": "kb", "url": "https://kb.example.com/mcp",
                               "auth_mode": "dispatch", "revision": 1}

    # A member reads it; nobody can register the same name twice.
    listed = app_client.get("/v1/mcp-servers", headers=auth_header("alice"))
    assert listed.status_code == 200, listed.text
    assert [s["name"] for s in listed.json()["servers"]] == ["kb"]
    assert listed.json()["can_manage"] is False
    again = app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb())
    assert again.status_code == 409, again.text


def test_another_tenants_server_is_404_by_any_route(app_client, db):
    McpServers(db, now=lambda: datetime.now(timezone.utc)).register(
        tenant_id="research", name="kb", url="https://kb.example.com/mcp", description="",
        auth={"mode": "none"}, allowed_profiles=[], allowed_tools=[], limits=None,
        enabled=True, by="bob@saga.xyz")
    before = json.dumps(servers_in(db), default=str)
    assert app_client.get("/v1/mcp-servers/kb", headers=auth_header("root")).status_code == 404
    assert app_client.get("/v1/mcp-servers", headers=auth_header("root")).json()["servers"] == []
    patched = app_client.patch("/v1/mcp-servers/kb", headers=auth_header("root"),
                               json={"enabled": False})
    assert patched.status_code == 404, patched.text
    assert json.dumps(servers_in(db), default=str) == before
    # The control: the owner's own member does see it.
    assert app_client.get("/v1/mcp-servers/kb", headers=auth_header("bob")).status_code == 200


def test_a_non_admin_write_is_403_and_writes_nothing(app_client, db):
    response = app_client.post("/v1/mcp-servers", headers=auth_header("alice"), json=kb())
    assert response.status_code == 403, response.text
    assert servers_in(db) == {} and audits(db) == []
    app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb())
    patched = app_client.patch("/v1/mcp-servers/kb", headers=auth_header("alice"),
                               json={"url": "https://elsewhere.example.com/mcp"})
    assert patched.status_code == 403, patched.text
    assert servers_in(db)[f"{SERVERS}/{server_id('eng', 'kb')}"]["url"] == \
        "https://kb.example.com/mcp"


def test_a_person_manages_their_own_personal_workspace(client, db, on):
    """Q1: a personal workspace is managed by its person."""
    from .test_secret_release import _personal

    _personal(db, "u-carol", "carol@saga.xyz")
    response = client.post("/v1/mcp-servers", headers=auth_header("carol"), json=kb())
    assert response.status_code == 201, response.text
    assert response.json()["server"]["tenant_id"] == "u-carol"


@pytest.mark.parametrize("url", [
    "http://kb.example.com/mcp",
    "https://10.0.0.7/mcp",
    "https://[::1]/mcp",
    "https://127.1/mcp",
    "https://kb.example.com:8443/mcp",
    "https://localhost/mcp",
    "https://metadata.google.internal/computeMetadata",
    "https://kb.svc.cluster.local/mcp",
    "https://kb/mcp",
    "https://kb.example.com/mcp?key=1",
    "https://kb.example.com/mcp#frag",
])
def test_a_url_that_is_not_public_https_on_443_is_422(app_client, db, url):
    response = app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb(url=url))
    assert response.status_code == 422, response.text
    assert servers_in(db) == {} and audits(db) == []


def test_a_userinfo_url_is_422_and_the_refusal_never_repeats_it(app_client, db):
    token = fake_credential("u")
    response = app_client.post("/v1/mcp-servers", headers=auth_header("root"),
                               json=kb(url=f"https://user:{token}@kb.example.com/mcp"))
    assert response.status_code == 422, response.text
    assert "userinfo" in response.text
    assert token not in response.text
    assert servers_in(db) == {}


@pytest.mark.parametrize("name", ["playwright", "jev", "swarm-git", "swarm-anything",
                                  "KB", "1kb", "k" * 32, "kb_x"])
def test_a_reserved_or_misspelt_name_is_422(app_client, db, name):
    response = app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb(name=name))
    assert response.status_code == 422, response.text
    assert servers_in(db) == {}


@pytest.mark.parametrize("extra", [
    {"tenant_id": "research"}, {"credential": "x" * 12}, {"image": "evil"},
    {"auth": {"mode": "dispatch", "header": "Cookie"}},
    {"auth": {"mode": "dispatch", "header": "Proxy-Authorization"}},
    {"auth": {"mode": "dispatch", "value": "x" * 12}},
    {"allowed_profiles": ["merge"]},
    {"allowed_tools": ["no spaces"]},
    {"limits": {"calls_per_minute": 601}},
    {"transport": "sse"},
])
def test_a_body_names_only_what_a_registration_holds(app_client, db, extra):
    response = app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb(**extra))
    assert response.status_code == 422, response.text
    assert servers_in(db) == {}


def test_there_is_no_delete_a_server_is_disabled(app_client, db):
    app_client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb())
    response = app_client.delete("/v1/mcp-servers/kb", headers=auth_header("root"))
    assert response.status_code == 405, response.text
    assert len(servers_in(db)) == 1


def test_every_write_has_its_audit_row_in_the_same_transaction(app_client, db, monkeypatch):
    """Each write is ONE commit holding the registration and its audit row,
    so neither can be stored without the other."""
    commits: list[list[str]] = []
    original = db.transaction

    def recording(**kwargs: Any):
        txn = original(**kwargs)
        commit = txn._commit

        def _commit():
            commits.append([ref.path for _, ref, _ in txn._buffer])
            return commit()

        txn._commit = _commit
        return txn

    monkeypatch.setattr(db, "transaction", recording)
    root = auth_header("root")
    assert app_client.post("/v1/mcp-servers", headers=root, json=kb()).status_code == 201
    assert app_client.patch("/v1/mcp-servers/kb", headers=root,
                            json={"description": "the graph"}).status_code == 200
    assert app_client.patch("/v1/mcp-servers/kb", headers=root,
                            json={"enabled": False}).status_code == 200
    assert app_client.patch("/v1/mcp-servers/kb", headers=root,
                            json={"enabled": True}).status_code == 200

    doc_path = f"{SERVERS}/{server_id('eng', 'kb')}"
    writes = [c for c in commits if doc_path in c]
    assert len(writes) == 4, commits
    for paths in writes:
        assert sum(p.startswith(f"{AUDIT_COLLECTION}/") for p in paths) == 1, paths
    actions = [a["action"] for a in sorted(audits(db), key=lambda a: a["detail"]["revision"])]
    assert actions == ["mcp_server_registered", "mcp_server_updated",
                       "mcp_server_disabled", "mcp_server_enabled"]
    assert [a["detail"]["revision"] for a in audits(db)] == sorted(
        a["detail"]["revision"] for a in audits(db))
    stored = db.docs[doc_path]
    assert stored["revision"] == 4 and stored["enabled"] is True
    assert stored["updated_by"] == ROOT


def test_a_patch_checks_what_it_changes_and_keeps_the_rest(app_client, db):
    root = auth_header("root")
    app_client.post("/v1/mcp-servers", headers=root,
                    json=kb(limits={"calls_per_minute": 30}, allowed_tools=["search"]))
    bad = app_client.patch("/v1/mcp-servers/kb", headers=root,
                           json={"url": "https://192.168.0.4/mcp"})
    assert bad.status_code == 422, bad.text
    good = app_client.patch("/v1/mcp-servers/kb", headers=root,
                            json={"limits": {"call_timeout_seconds": 120}})
    assert good.status_code == 200, good.text
    server = good.json()["server"]
    assert server["limits"]["calls_per_minute"] == 30
    assert server["limits"]["call_timeout_seconds"] == 120
    assert server["allowed_tools"] == ["search"]
    assert server["url"] == "https://kb.example.com/mcp" and server["revision"] == 2


def test_the_feature_is_dark_until_the_switch_is_on(client, db, monkeypatch):
    """Design §6: nothing changes while SWARM_MCP_SERVERS is off."""
    seed_tenant(db, "eng")
    monkeypatch.delenv(FLAG, raising=False)
    assert mcp_servers_enabled() is False
    for value in ("", "off", "true", "1", "yes"):
        assert mcp_servers_enabled({FLAG: value}) is False
    assert mcp_servers_enabled({FLAG: "on"}) is True
    response = client.post("/v1/mcp-servers", headers=auth_header("root"), json=kb())
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_unavailable"
    assert servers_in(db) == {} and audits(db) == []


# --------------------------------------------------------------------------
# §4.5: `mcp:<name>` in the workspace secrets store
# --------------------------------------------------------------------------


class FakeSecretManager:
    """Secret Manager in memory, for both stores' writers."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.created: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.fail_delete = False

    def create_secret(self, tenant_id: str, secret_id: str, ttl: Any = None) -> None:
        self.created[secret_id] = ttl

    def add_version(self, secret_id: str, payload: bytes) -> str:
        version = f"projects/123456789/secrets/{secret_id}/versions/1"
        self.payloads[version] = payload
        return version

    def disable_version(self, version: str) -> None:
        pass

    def destroy_versions(self, secret_id: str) -> int:
        return 0

    def delete_secret(self, secret_id: str) -> None:
        from swarm_api.errors import UpstreamUnavailable

        if self.fail_delete:
            raise UpstreamUnavailable("injected")
        self.deleted.append(secret_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _register(db, tenant_id: str = "eng", name: str = "kb", *, mode: str = "dispatch",
              enabled: bool = True) -> dict[str, Any]:
    McpServers(db, now=_now).register(
        tenant_id=tenant_id, name=name, url=f"https://{name}.example.com/mcp", description="",
        auth={"mode": mode}, allowed_profiles=[], allowed_tools=[], limits=None,
        enabled=enabled, by=ROOT)
    return db.docs[f"{SERVERS}/{server_id(tenant_id, name)}"]


def test_an_mcp_grant_names_a_registration_of_this_workspace_only(db):
    from swarm_api.errors import ValidationFailed

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _register(db, "eng", "kb", mode="workspace_secret")
    _register(db, "research", "theirs", mode="workspace_secret")
    store = WorkspaceSecrets(db, FakeSecretManager(), now=_now)
    from .test_secret_release import _tenant

    store.create(tenant=_tenant(db, "eng"), name="kb-key", provider="other", kind="api_key",
                 payload=fake_credential().encode(), by=ROOT, via_link=False)
    record = store.set_grant(tenant_id="eng", name="kb-key", consumer="mcp:kb", slot=None,
                             enabled=True, by=ROOT)
    assert [(g["consumer"], g["slot"]) for g in record["grants"]] == [("mcp:kb", "auth")]
    for consumer in ("mcp:theirs", "mcp:nobody", "mcp:playwright", "mcp:"):
        with pytest.raises(ValidationFailed):
            store.set_grant(tenant_id="eng", name="kb-key", consumer=consumer, slot=None,
                            enabled=True, by=ROOT)
    with pytest.raises(ValidationFailed):
        store.set_grant(tenant_id="eng", name="kb-key", consumer="mcp:kb", slot="files",
                        enabled=True, by=ROOT)
    grants = [p for p in db.docs if p.startswith(f"{GRANTS_COLLECTION}/")]
    assert len(grants) == 1


def test_the_mcp_consumers_are_the_workspaces_enabled_registrations(db):
    _register(db, "eng", "kb")
    _register(db, "eng", "off", enabled=False)
    _register(db, "research", "theirs")
    listed = [c.to_api() for c in mcp_consumers(db, "eng")]
    assert [c["consumer"] for c in listed] == ["mcp:kb"]
    assert listed[0]["slots"] == [{"slot": "auth", "kinds": ["api_key", "bearer_token"],
                                   "means": listed[0]["slots"][0]["means"]}]
    assert listed[0]["grantable"] is True


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
@pytest.mark.parametrize("with_servers", [False, True])
def test_attempt_consumers_never_derives_an_mcp_consumer(profile, with_servers):
    """§4.5, the held invariant: the release is handed only the consumers
    this derives, so as long as none is `mcp:` it cannot release an MCP
    credential as a file."""
    inputs: dict[str, Any] = {"prompt": "x"}
    if with_servers:
        inputs["mcp_servers"] = ["kb", "decisions"]
    for browser_tool in (None, "jev", "playwright", "auto"):
        if browser_tool:
            inputs["browser_tool"] = browser_tool
        derived = attempt_consumers({"runner_profile": profile, "input": inputs})
        assert all(parse_consumer(c)[0] != "mcp" for c in derived), derived


# --------------------------------------------------------------------------
# §4.4: the per-run credential at rest, and the sweep
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value,expires_in", [
    ("short", 3600), ("x" * 8193, 3600), ("two\nlines-xxxx", 3600), ("tab\there-xxxx", 3600),
    ("café-not-ascii", 3600), (12345678, 3600),
    ("x" * 12, 59), ("x" * 12, 86401), ("x" * 12, True), ("x" * 12, "3600"),
])
def test_a_malformed_credential_is_refused_without_echoing_it(value, expires_in):
    with pytest.raises(McpCredentialShape) as refused:
        check_credential(value, expires_in)
    assert refused.value.status_code == 422
    if isinstance(value, str):
        assert value not in str(refused.value)


def credentials(db, sm, *, now=_now, keys: RowKeys = KEYS) -> McpCredentials:
    return McpCredentials(db, sm, now=now, keys=keys)


def _store(db, sm, *, owner_id: str, owner_kind: str = "task", tenant_id: str = "eng",
           name: str = "kb", expires_in: int = 3600, value: str | None = None) -> dict[str, Any]:
    registration = db.docs[f"{SERVERS}/{server_id(tenant_id, name)}"]
    credentials(db, sm, now=_now).store(
        tenant_id=tenant_id, owner_id=owner_id, owner_kind=owner_kind,
        registration=registration, value=value or fake_credential(), expires_in=expires_in,
        by=ROOT)
    return db.docs[f"{CREDENTIALS}/{owner_id}~{registration['server_id']}"]


def test_a_stored_credential_is_one_secret_with_a_ttl_and_a_row_without_the_value(db, caplog):
    caplog.set_level(logging.DEBUG)
    sm = FakeSecretManager()
    _register(db)
    value = fake_credential("s")
    registration = db.docs[f"{SERVERS}/{server_id('eng', 'kb')}"]
    answer = credentials(db, sm, now=_now).store(
        tenant_id="eng", owner_id="task_1", owner_kind="task", registration=registration,
        value=value, expires_in=600, by=ROOT)
    assert set(answer) == {"server", "expires_at"} and answer["server"] == "kb"
    [(secret_id, ttl)] = sm.created.items()
    assert secret_id.startswith(secret_prefix("eng")) and len(secret_id) == len(
        secret_prefix("eng")) + 16
    assert ttl == timedelta(seconds=600) + timedelta(minutes=5)
    assert list(sm.payloads.values()) == [value.encode()]
    row = db.docs[f"{CREDENTIALS}/task_1~{registration['server_id']}"]
    assert row["state"] == "live" and row["secret_id"] == secret_id
    for path, doc in db.docs.items():
        assert value not in json.dumps(doc, default=str), path
    for record in caplog.records:
        assert value not in record.getMessage()


def test_the_sweep_destroys_a_terminal_tasks_secret_and_marks_the_row(app_client, db):
    sm = FakeSecretManager()
    app_client.app.state.mcp_credential_writer = sm
    _register(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    seed_task(db, task_id="task_live", tenant_id="eng", state="RUNNING")
    done = _store(db, sm, owner_id="task_done")
    live = _store(db, sm, owner_id="task_live")

    response = app_client.post("/v1/admin/mcp-credentials/sweep", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json() == {"examined": 2, "destroyed": 1, "failed": 0, "kept": 1}
    assert sm.deleted == [done["secret_id"]]
    row = db.docs[f"{CREDENTIALS}/task_done~{done['server_id']}"]
    assert row["state"] == "destroyed" and row["destroyed_reason"] == "owner_terminal"
    assert isinstance(row["destroyed_at"], datetime)
    assert db.docs[f"{CREDENTIALS}/task_live~{live['server_id']}"]["state"] == "live"

    # Idempotent: a destroyed row is not examined again.
    again = app_client.post("/v1/admin/mcp-credentials/sweep", headers=auth_header("root"))
    assert again.json()["examined"] == 1 and sm.deleted == [done["secret_id"]]


def test_the_sweep_destroys_an_expired_credential_of_a_running_task(db):
    sm = FakeSecretManager()
    _register(db)
    seed_task(db, task_id="task_live", tenant_id="eng", state="RUNNING")
    row = _store(db, sm, owner_id="task_live", expires_in=60)
    later = credentials(db, sm, now=lambda: _now() + timedelta(seconds=61))
    assert later.sweep()["destroyed"] == 1
    assert db.docs[f"{CREDENTIALS}/task_live~{row['server_id']}"]["destroyed_reason"] == "expired"


def test_a_workflows_credential_lives_until_its_last_task_ends(db):
    sm = FakeSecretManager()
    _register(db)
    seed_task(db, task_id="step_a", tenant_id="eng", state="SUCCEEDED", workflow_id="wf_1")
    step_b = seed_task(db, task_id="step_b", tenant_id="eng", state="QUEUED",
                       workflow_id="wf_1")
    _store(db, sm, owner_id="wf_1", owner_kind="workflow")
    store = credentials(db, sm, now=_now)
    assert store.sweep()["kept"] == 1 and sm.deleted == []
    step_b["state"] = "FAILED"
    assert store.sweep()["destroyed"] == 1 and len(sm.deleted) == 1


def test_a_parents_credential_lives_while_a_child_still_runs(db):
    """Q7: a child uses its parent's stored credential."""
    sm = FakeSecretManager()
    _register(db)
    seed_task(db, task_id="parent", tenant_id="eng", state="SUCCEEDED")
    child = seed_task(db, task_id="child", tenant_id="eng", state="RUNNING")
    child["parent_task_id"] = "parent"
    _store(db, sm, owner_id="parent")
    store = credentials(db, sm, now=_now)
    assert store.sweep()["kept"] == 1
    child["state"] = "SUCCEEDED"
    assert store.sweep()["destroyed"] == 1


def test_a_failed_delete_leaves_the_row_live_for_the_next_sweep(db):
    sm = FakeSecretManager()
    sm.fail_delete = True
    _register(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    row = _store(db, sm, owner_id="task_done")
    assert credentials(db, sm, now=_now).sweep()["failed"] == 1
    assert db.docs[f"{CREDENTIALS}/task_done~{row['server_id']}"]["state"] == "live"


def test_a_rewritten_row_never_makes_the_sweep_delete_another_secret(db):
    sm = FakeSecretManager()
    _register(db)
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    row = _store(db, sm, owner_id="task_done")
    for target in ("swarm-tenant-research-mcpc-" + "0" * 16, "swarm-tenant-eng-wsec-" + "0" * 16,
                   "swarm-github-app-key"):
        row.update(state="live", secret_id=target)
        credentials(db, sm, now=_now).sweep()
        assert sm.deleted == [], target


def test_a_forged_row_for_another_tenants_secret_is_not_deleted(db):
    """What an agent of eng can write: a row naming research, consistent in
    every field the sweep reads, expired. Unattested, so nothing is deleted;
    the row is marked so it is not examined again."""
    sm = FakeSecretManager()
    victim = "swarm-tenant-research-mcpc-" + secrets.token_hex(8)
    db.docs[f"{CREDENTIALS}/x~y"] = {
        "owner_id": "x", "owner_kind": "task", "tenant_id": "research", "server_id": "y",
        "secret_id": victim, "version": f"projects/1/secrets/{victim}/versions/1",
        "expires_at": _now() - timedelta(hours=1), "state": "live",
        "attestation": "0" * 64,
    }
    report = credentials(db, sm).sweep()
    assert sm.deleted == [] and report["failed"] == 1
    assert db.docs[f"{CREDENTIALS}/x~y"]["destroyed_reason"] == "unattested"


def test_a_row_re_pointed_at_a_colleagues_secret_fails_its_attestation(db):
    sm = FakeSecretManager()
    _register(db)
    mine = _store(db, sm, owner_id="task_mine")
    theirs = _store(db, sm, owner_id="task_theirs")
    assert KEYS.holds(mine) and KEYS.holds(theirs)
    forged = dict(mine, secret_id=theirs["secret_id"], version=theirs["version"])
    assert not KEYS.holds(forged)
    assert not RowKeys(fake_credential("k")).holds(mine)
    # A rotation: the previous key still verifies a row it signed.
    assert RowKeys(fake_credential("n"), KEY).holds(mine)


def test_without_a_key_nothing_is_stored_and_the_sweep_deletes_nothing(db):
    sm = FakeSecretManager()
    registration = _register(db)
    from swarm_api.mcp_servers import McpUnavailable

    with pytest.raises(McpUnavailable):
        credentials(db, sm, keys=RowKeys("")).store(
            tenant_id="eng", owner_id="t", owner_kind="task", registration=registration,
            value=fake_credential(), expires_in=600, by=ROOT)
    assert sm.created == {} and sm.payloads == {}
    seed_task(db, task_id="task_done", tenant_id="eng", state="SUCCEEDED")
    _store(db, sm, owner_id="task_done")
    assert credentials(db, sm, keys=RowKeys("")).sweep()["kept"] == 1
    assert sm.deleted == []


def test_a_credential_stored_before_its_task_is_written_survives_a_sweep(db):
    """The submission stores the credential, then writes the task: a sweep in
    between must not take an owner it cannot find for a finished one."""
    sm = FakeSecretManager()
    _register(db)
    _store(db, sm, owner_id="task_not_yet")
    _store(db, sm, owner_id="wf_not_yet", owner_kind="workflow")
    assert credentials(db, sm).sweep() == {"examined": 2, "destroyed": 0, "failed": 0, "kept": 2}
    assert sm.deleted == []


def test_a_member_cannot_run_the_sweep(app_client):
    response = app_client.post("/v1/admin/mcp-credentials/sweep", headers=auth_header("alice"))
    assert response.status_code == 403, response.text
