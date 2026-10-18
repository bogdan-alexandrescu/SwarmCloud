"""The MCP attach, swarm-api's half (MCP-API, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.6, and owner decision Q9 of
2026-10-17: the second route that serves credential material, signed by the
worker's attempt key under its own purpose, fenced by its generation, once
per attempt. Drives the real route, `POST /v1/attempts/{attempt_id}/mcp:attach`,
over the in-memory Firestore, with the attempt key registered as a worker
registers one and the task signed as submission signs it, exactly as
test_secret_release.py drives the release. Secret Manager is an in-memory
stand-in: the one place a value lives in these tests.

The attackers these stand in for:

  * A STALE WORKER (invariant 5): 409 and nothing, and nothing is read.
  * AN AGENT IN THE POD: it cannot sign as the worker, so an unsigned call, a
    call signed by another key or under another route's purpose gets 403 and
    nothing. It can rewrite its task document, so a server name added there
    fails the spec signature; and it can read every file, so an `mcp:` grant
    is never released as one by `secrets:release`.
  * AN ADMIN WHO DISABLES A SERVER mid-queue: the next attach withholds it.
  * ANOTHER WORKSPACE: its credential rows and secrets are never read here.
  * A LOG OR DATABASE READER: a runtime-built credential is in no log line,
    no Firestore document, and no response but the attach's own.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like
a credential).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.childkey import request_message
from swarm_api.main import UNCAPTURED_ROUTES
from swarm_api.mcp_credentials import COLLECTION as CREDENTIALS, McpCredentials, RowKeys
from swarm_api.mcp_servers import FLAG, McpServers, server_id
from swarm_api.routes import attempts_mcp as attach_mod
from swarm_api.routes.attempts_mcp import (
    ATTACH_MARKS_COLLECTION,
    ATTACH_ROUTE,
    ATTACH_ROWS_COLLECTION,
    attach_message,
)
from swarm_api.routes.attempts_secrets import release_message

from .conftest import PROJECT, auth_header, seed_tenant
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    OTHER_WORKER_TOKEN,
    WORKER_TOKEN,
    Attempt,
    register,
    seed_parent,
    worker_tokens,
)
from .test_secret_release import (
    ALICE,
    ROOT,
    SIGNER,
    FakeSecretManager,
    _client,
    _personal,
    add_secret,
    release,
    sign_release,
)


def fake_credential(fill: str = "c") -> str:
    """Shaped like a bearer token, built at runtime so no literal looks like one."""
    return "mcp" + "-" + fill * 24 + secrets.token_hex(8)


class FakeSM(FakeSecretManager):
    """The release's in-memory Secret Manager, also the per-run credential writer."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def create_secret(self, tenant_id: str, secret_id: str, ttl: Any = None) -> None:
        pass

    def delete_secret(self, secret_id: str) -> None:
        self.deleted.append(secret_id)


@pytest.fixture
def sm() -> FakeSM:
    return FakeSM()


@pytest.fixture
def client(db, worker_tokens, group_map, objects, sm, monkeypatch) -> TestClient:  # noqa: F811
    monkeypatch.setenv(FLAG, "on")
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _personal(db, "u-alice", ALICE)
    application = _client(db, worker_tokens, group_map, objects, sm)
    application.app.state.mcp_credential_writer = sm
    return application


def _now() -> datetime:
    return datetime.now(timezone.utc)


def registered(db, name: str = "kb", *, tenant_id: str = "eng", mode: str = "dispatch",
               profiles: list[str] | None = None, tools: list[str] | None = None,
               enabled: bool = True) -> dict[str, Any]:
    McpServers(db, now=_now).register(
        tenant_id=tenant_id, name=name, url=f"https://{name}.example.com/mcp",
        description=f"the {name} graph", auth={"mode": mode},
        allowed_profiles=profiles or [], allowed_tools=tools or ["search"], limits=None,
        enabled=enabled, by=ROOT)
    return db.docs[f"mcp_servers/{server_id(tenant_id, name)}"]


def stored(db, sm, owner_id: str, *, name: str = "kb", tenant_id: str = "eng",
           owner_kind: str = "task", expires_in: int = 3600) -> str:
    value = fake_credential()
    McpCredentials(db, sm, now=_now, keys=RowKeys(KEY)).store(
        tenant_id=tenant_id, owner_id=owner_id, owner_kind=owner_kind,
        registration=db.docs[f"mcp_servers/{server_id(tenant_id, name)}"], value=value,
        expires_in=expires_in, by=ALICE)
    return value


def running(client, db, attempt: Attempt | None = None, *, profile: str = "claude-code",
            servers: list[str] | None = None, **extra: Any) -> Attempt:
    """A task naming `servers`, registered while STARTING, signed, then RUNNING."""
    attempt = attempt or Attempt()
    task_input: dict[str, Any] = {"prompt": "do it"}
    if servers is not None:
        task_input["mcp_servers"] = list(servers)
    doc = seed_parent(db, attempt, state="STARTING", runner_profile=profile, input=task_input,
                      submitted_by=ALICE, **extra)
    response = register(client, db, attempt)
    assert response.status_code == 201, response.text
    SIGNER.sign_document(doc, attempt.tuple.task_id)
    doc["state"] = "RUNNING"
    return attempt


def attach(client, attempt: Attempt, *, generation: int | None = None,
           token: str | None = WORKER_TOKEN, sign: bool = True,
           body: dict[str, Any] | None = None, signer: Attempt | None = None,
           purpose_message=attach_message) -> Any:
    t = attempt.tuple
    raw = json.dumps(body if body is not None else
                     {"generation": t.generation if generation is None else generation}).encode()
    path = ATTACH_ROUTE.replace("{attempt_id}", t.attempt_id)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update(sign_release(signer or attempt, path, raw, purpose_message=purpose_message))
    return client.post(path, content=raw, headers=headers)


def rows(db) -> list[dict[str, Any]]:
    return [d for p, d in sorted(db.docs.items()) if p.startswith(f"{ATTACH_ROWS_COLLECTION}/")]


def marks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{ATTACH_MARKS_COLLECTION}/")]


def mcp_reads(sm) -> list[str]:
    return [r for r in sm.reads if "-mcpc-" in r or "-wsec-" in r]


# --------------------------------------------------------------------------
# the attach, once, with its rows
# --------------------------------------------------------------------------


def test_an_attempt_is_attached_its_server_and_credential_once_with_one_row(client, db, sm):
    registration = registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)

    first = attach(client, attempt)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert body["withheld"] == []
    assert body["servers"] == [{
        "name": "kb", "url": "https://kb.example.com/mcp", "revision": 1,
        "description": "the kb graph",
        "auth": {"header": "Authorization", "scheme": "Bearer", "credential": value},
        "allowed_tools": ["search"],
        "limits": {"max_request_bytes": 1048576, "max_response_bytes": 4194304,
                   "calls_per_minute": 60, "call_timeout_seconds": 60},
    }]
    [row] = rows(db)
    assert row["task_id"] == attempt.tuple.task_id
    assert row["attempt_id"] == attempt.tuple.attempt_id
    assert row["generation"] == attempt.tuple.generation
    assert row["server_id"] == registration["server_id"] and row["revision"] == 1
    assert row["auth_mode"] == "dispatch" and row["submitted_by"] == ALICE
    assert row["secret_id"].startswith("swarm-tenant-eng-mcpc-")
    assert row["expire_at"] - row["at"] == timedelta(days=90)
    assert [m["attempt_id"] for m in marks(db)] == [attempt.tuple.attempt_id]

    # ONCE PER ATTEMPT: 410, nothing read, nothing written.
    reads = len(sm.reads)
    again = attach(client, attempt)
    assert again.status_code == 410, again.text
    assert again.json()["code"] == "mcp_attach_spent"
    assert value not in again.text
    assert len(sm.reads) == reads and len(rows(db)) == 1


def test_a_server_with_no_auth_is_attached_with_a_null_credential(client, db, sm):
    registered(db, mode="none")
    attempt = running(client, db, servers=["kb"])
    [server] = attach(client, attempt).json()["servers"]
    assert server["auth"] == {"header": None, "scheme": None, "credential": None}
    assert sm.reads == []


def test_a_task_naming_no_server_is_attached_nothing_and_marked(client, db, sm):
    registered(db)
    attempt = running(client, db)
    answer = attach(client, attempt)
    assert answer.status_code == 200, answer.text
    assert answer.json()["servers"] == [] and answer.json()["withheld"] == []
    assert sm.reads == [] and rows(db) == [] and len(marks(db)) == 1


# --------------------------------------------------------------------------
# only the worker, signed under this route's purpose; invariant 5
# --------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["unsigned", "other_key", "release_purpose", "child_purpose",
                                 "other_tenant_token", "no_token", "stale_timestamp",
                                 "unknown_attempt", "other_generation"])
def test_an_unsigned_or_wrongly_signed_call_is_403_and_gets_nothing(client, db, sm, how):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    if how == "unsigned":
        response = attach(client, attempt, sign=False)
    elif how == "other_key":
        response = attach(client, attempt, signer=Attempt())
    elif how == "release_purpose":
        response = attach(client, attempt, purpose_message=release_message)
    elif how == "child_purpose":
        response = attach(client, attempt, purpose_message=request_message)
    elif how == "other_tenant_token":
        response = attach(client, attempt, token=OTHER_WORKER_TOKEN)
    elif how == "no_token":
        response = attach(client, attempt, token=None)
    elif how == "stale_timestamp":
        t = attempt.tuple
        raw = json.dumps({"generation": t.generation}).encode()
        path = ATTACH_ROUTE.replace("{attempt_id}", t.attempt_id)
        headers = {"Authorization": f"Bearer {WORKER_TOKEN}",
                   **sign_release(attempt, path, raw, purpose_message=attach_message,
                                  at=time.time() - 3600)}
        response = client.post(path, content=raw, headers=headers)
    elif how == "unknown_attempt":
        response = attach(client, Attempt(attempt="att_nobody"))
    else:
        response = attach(client, attempt, generation=attempt.tuple.generation + 1)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "mcp_attach_unproven"
    assert value not in response.text and "kb.example.com" not in response.text
    assert sm.reads == [] and rows(db) == [] and marks(db) == []


def test_a_release_signature_does_not_verify_here_nor_an_attach_signature_there(client, db, sm):
    """Each route's purpose is its own: the control for `release_purpose` above."""
    attempt = running(client, db, servers=["kb"])
    crossed = release(client, attempt, purpose_message=attach_message)
    assert crossed.status_code == 403, crossed.text


@pytest.mark.parametrize("change", ["superseded", "lease_released", "finished", "wrong_lease"])
def test_a_stale_generation_gets_409_and_nothing(client, db, sm, change):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    t = attempt.tuple
    task = db.docs[f"tasks/{t.task_id}"]
    if change == "superseded":
        task["current_generation"] = t.generation + 1
    elif change == "lease_released":
        db.docs[f"leases/{t.lease_id}"]["released_at"] = _now()
    elif change == "finished":
        task["state"] = "SUCCEEDED"
    else:
        task["current_lease_id"] = "lease_other"
    response = attach(client, attempt)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "mcp_attach_fenced"
    assert value not in response.text and "kb.example.com" not in response.text
    assert sm.reads == [] and rows(db) == [] and marks(db) == []


def test_a_worker_superseded_while_secret_manager_answered_is_sent_nothing(client, db, sm):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    task = db.docs[f"tasks/{attempt.tuple.task_id}"]
    original = sm.access

    def supersede_then_read(version: str) -> bytes:
        task["current_generation"] = attempt.tuple.generation + 1
        return original(version)

    sm.access = supersede_then_read  # type: ignore[method-assign]
    response = attach(client, attempt)
    assert response.status_code == 409, response.text
    assert value not in response.text
    assert rows(db) == [] and marks(db) == []


@pytest.mark.parametrize("extra", ["name", "tenant_id", "server", "url"])
def test_the_body_is_the_generation_and_nothing_else(client, db, sm, extra):
    attempt = running(client, db, servers=["kb"])
    probe = fake_credential("z")
    response = attach(client, attempt, body={"generation": 1, extra: probe})
    assert response.status_code == 422, response.text
    assert probe not in response.text
    assert sm.reads == [] and rows(db) == []


def test_a_rewritten_server_list_fails_the_spec_signature(client, db, sm):
    """An agent can write its own task document: a server added there is not
    the caller's, so the signed spec is checked before anything is read."""
    registered(db)
    registered(db, "other")
    attempt = running(client, db, servers=["kb"])
    stored(db, sm, attempt.tuple.task_id, name="other")
    db.docs[f"tasks/{attempt.tuple.task_id}"]["input"]["mcp_servers"] = ["kb", "other"]
    response = attach(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "mcp_attach_spec_unverified"
    assert sm.reads == [] and rows(db) == []


# --------------------------------------------------------------------------
# step 5 and 6: withheld, by name and reason
# --------------------------------------------------------------------------


def test_a_server_disabled_since_submission_is_withheld_and_never_read(client, db, sm):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    McpServers(db, now=_now).update(tenant_id="eng", name="kb", changes={"enabled": False},
                                    by=ROOT)
    answer = attach(client, attempt)
    assert answer.status_code == 200, answer.text
    assert answer.json()["servers"] == []
    assert answer.json()["withheld"] == [{"name": "kb", "reason": "disabled"}]
    assert value not in answer.text and sm.reads == [] and rows(db) == []


def test_a_profile_no_longer_allowed_is_withheld(client, db, sm):
    registered(db, profiles=["codex"])
    attempt = running(client, db, servers=["kb"])
    stored(db, sm, attempt.tuple.task_id)
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "kb", "reason": "profile_refused"}]
    assert sm.reads == []


def test_an_unregistered_or_another_tenants_server_is_withheld(client, db, sm):
    registered(db, "theirs", tenant_id="research")
    attempt = running(client, db, servers=["theirs", "nobody"])
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "theirs", "reason": "disabled"},
                                         {"name": "nobody", "reason": "disabled"}]
    assert sm.reads == []


@pytest.mark.parametrize("how", ["missing", "expired", "destroyed"])
def test_a_dispatch_server_without_a_live_credential_is_withheld(client, db, sm, how):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    reason = "credential_missing"
    if how != "missing":
        stored(db, sm, attempt.tuple.task_id)
        row = next(d for p, d in db.docs.items() if p.startswith(f"{CREDENTIALS}/"))
        if how == "expired":
            # As swarm-api would have written it, had it been stored to end then.
            row["expires_at"] = (_now() - timedelta(seconds=1)).replace(microsecond=0)
            row["attestation"] = RowKeys(KEY).sign(row)
        else:
            row["state"] = "destroyed"
        reason = "credential_expired"
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "kb", "reason": reason}]
    assert sm.reads == []


def test_another_tenants_credential_row_is_never_read(client, db, sm):
    """A row an agent forges under its own task id, pointing at research's
    secret, is no row: the secret's NAME says whose it is."""
    registered(db)
    registered(db, tenant_id="research")
    stored(db, sm, "task_research", tenant_id="research")
    theirs = next(d for p, d in db.docs.items() if p.startswith(f"{CREDENTIALS}/task_research"))
    attempt = running(client, db, servers=["kb"])
    forged_id = f"{attempt.tuple.task_id}~{server_id('eng', 'kb')}"
    db.docs[f"{CREDENTIALS}/{forged_id}"] = dict(
        theirs, owner_id=attempt.tuple.task_id, tenant_id="eng",
        server_id=server_id("eng", "kb"))
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "kb", "reason": "credential_missing"}]
    assert sm.reads == []


def test_a_row_re_pointed_at_a_colleagues_credential_is_never_read(client, db, sm):
    """Within one workspace: an agent copies a colleague's live row under its
    own task id. The tenant boundary holds either way; the attestation is
    what keeps a per-run credential to the run it was given for (Q2)."""
    registered(db)
    stored(db, sm, "task_colleague")
    colleague = next(d for p, d in db.docs.items()
                     if p.startswith(f"{CREDENTIALS}/task_colleague"))
    attempt = running(client, db, servers=["kb"])
    db.docs[f"{CREDENTIALS}/{attempt.tuple.task_id}~{server_id('eng', 'kb')}"] = dict(
        colleague, owner_id=attempt.tuple.task_id)
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "kb", "reason": "credential_missing"}]
    assert sm.reads == []


def test_a_workflow_step_uses_its_workflows_stored_credential(client, db, sm):
    registered(db)
    attempt = running(client, db, servers=["kb"], workflow_id="wf_1", step_id="a")
    value = stored(db, sm, "wf_1", owner_kind="workflow")
    [server] = attach(client, attempt).json()["servers"]
    assert server["auth"]["credential"] == value


def test_a_child_uses_its_parents_stored_credential(client, db, sm):
    """Q7: a child names only its parent's servers and has no credential of
    its own; it is attached with the parent's."""
    registered(db)
    value = stored(db, sm, "task_the_parent")
    child = running(client, db, Attempt(task="task_child", attempt="att_child",
                                        lease="lease_child"),
                    servers=["kb"], parent_task_id="task_the_parent",
                    parent_attempt_id="att_the_parent")
    [server] = attach(client, child).json()["servers"]
    assert server["auth"]["credential"] == value


def test_a_secret_swarm_api_may_not_read_yet_is_503_with_no_name(client, db, sm):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    sm.fail = "permission_denied"
    response = attach(client, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_attach_unavailable"
    assert value not in response.text and "kb" not in response.json()["message"].split()
    assert rows(db) == [] and marks(db) == []


def test_a_version_that_cannot_be_read_is_withheld_unreadable(client, db, sm):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    stored(db, sm, attempt.tuple.task_id)
    sm.fail = "version_unavailable"
    answer = attach(client, attempt)
    assert answer.json()["withheld"] == [{"name": "kb", "reason": "unreadable"}]


# --------------------------------------------------------------------------
# §4.5: a workspace secret granted to mcp:<name>
# --------------------------------------------------------------------------


def test_an_mcp_grant_is_attached_and_never_released_by_secrets_release(client, db, sm):
    registered(db, mode="workspace_secret")
    value = fake_credential("w")
    add_secret(db, sm, "eng", "kb-key", value, grants=("mcp:kb",))
    attempt = running(client, db, servers=["kb"])
    [server] = attach(client, attempt).json()["servers"]
    assert server["auth"]["credential"] == value
    [row] = rows(db)
    assert row["auth_mode"] == "workspace_secret"
    assert row["secret_id"].startswith("swarm-tenant-eng-wsec-")

    # The same attempt's release (its own once-mark) hands out nothing, and
    # never names the `mcp:` consumer it does not derive.
    released = release(client, attempt)
    assert released.status_code == 200, released.text
    assert released.json()["secrets"] == [] and released.json()["withheld"] == []
    assert not any(c.startswith("mcp:") for c in released.json()["consumers"])
    assert value not in released.text


def test_a_key_granted_both_ways_is_released_as_the_profiles_and_attached_as_the_servers(
        client, db, sm):
    """The store's own rule (§4.5): the boundary is the attempt. The control
    for the test above: the release does release this key, to the profile."""
    registered(db, mode="workspace_secret")
    value = fake_credential("b")
    add_secret(db, sm, "eng", "kb-key", value, grants=("mcp:kb", "profile:claude-code"))
    add_secret(db, sm, "eng", "other-key", fake_credential("o"), grants=("profile:claude-code",))
    attempt = running(client, db, servers=["kb"])
    released = release(client, attempt)
    assert sorted(s["name"] for s in released.json()["secrets"]) == ["kb-key", "other-key"]
    assert attach(client, attempt).json()["servers"][0]["auth"]["credential"] == value


def test_an_unticked_or_forged_mcp_grant_is_not_attached(client, db, sm):
    registered(db, mode="workspace_secret")
    record = add_secret(db, sm, "eng", "kb-key", fake_credential(), grants=("mcp:kb",))
    grant = db.docs[f"workspace_secret_grants/{record['secret_id']}~mcp:kb"]
    grant["slot"] = "files"
    attempt = running(client, db, servers=["kb"])
    assert attach(client, attempt).json()["withheld"] == [
        {"name": "kb", "reason": "credential_missing"}]
    assert mcp_reads(sm) == []


def test_another_tenants_mcp_grant_is_never_attached(client, db, sm):
    registered(db, mode="workspace_secret")
    registered(db, tenant_id="research", mode="workspace_secret")
    theirs = add_secret(db, sm, "research", "kb-key", fake_credential("r"), grants=("mcp:kb",))
    # What an agent in eng can write: a grant under eng naming research's secret.
    db.docs[f"workspace_secret_grants/{theirs['secret_id']}~mcp:kb-forged"] = {
        "secret_id": theirs["secret_id"], "tenant_id": "eng", "consumer": "mcp:kb",
        "slot": "auth", "enabled": True,
    }
    attempt = running(client, db, servers=["kb"])
    assert attach(client, attempt).json()["withheld"] == [
        {"name": "kb", "reason": "credential_missing"}]
    assert all("swarm-tenant-research-" not in r for r in sm.reads)


# --------------------------------------------------------------------------
# the credential is nowhere but Secret Manager and the attach's answer
# --------------------------------------------------------------------------


def test_a_runtime_built_credential_is_in_no_log_document_or_other_response(
        client, db, sm, caplog):
    caplog.set_level(logging.DEBUG)
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)

    answer = attach(client, attempt)
    assert value in answer.text  # the one place it is served

    others = [
        attach(client, attempt),                                        # 410
        release(client, attempt),
        client.get("/v1/mcp-servers", headers=auth_header("alice")),
        client.get("/v1/mcp-servers/kb", headers=auth_header("alice")),
        client.post("/v1/admin/mcp-credentials/sweep", headers=auth_header("root")),
    ]
    for response in others:
        assert response.status_code < 500, response.text
        assert value not in response.text, response.request.url
    for record in caplog.records:
        assert value not in record.getMessage()
        assert value not in str(record.args or "")
    for path, doc in db.docs.items():
        assert value not in json.dumps(doc, default=str), path
    assert value.encode() in sm.payloads.values()


def test_the_attach_route_is_uncaptured_by_construction():
    assert ATTACH_ROUTE in UNCAPTURED_ROUTES
    # Q9: the submission bodies carry per-run credentials too.
    assert {"/v1/tasks", "/v1/tasks/batch", "/v1/workflows"} <= UNCAPTURED_ROUTES
    assert attach_mod.ATTACH_PURPOSE == "swarm-mcp-attach/v1"
    route = next(r for r in attach_mod.router.routes if getattr(r, "path", "") == ATTACH_ROUTE)
    assert route.body_field is None
    assert route.methods == {"POST"}


def test_the_attach_is_dark_until_the_switch_is_on(client, db, sm, monkeypatch):
    registered(db)
    attempt = running(client, db, servers=["kb"])
    value = stored(db, sm, attempt.tuple.task_id)
    monkeypatch.delenv(FLAG)
    response = attach(client, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_unavailable"
    assert value not in response.text
    assert sm.reads == [] and rows(db) == [] and marks(db) == []


def test_the_project_the_read_is_made_under_is_this_deployments(client, db, sm):
    """The stored version carries the project NUMBER; the read is rebuilt
    under this deployment's project id, as the release's is."""
    registered(db)
    attempt = running(client, db, servers=["kb"])
    stored(db, sm, attempt.tuple.task_id)
    attach(client, attempt)
    assert mcp_reads(sm) and all(r.startswith(f"projects/{PROJECT}/") for r in mcp_reads(sm))
