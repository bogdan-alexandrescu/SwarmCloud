"""A submission names registered MCP servers and carries their per-run credentials (MCP-SUBMIT, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.3 (how a step names a
server, and every refusal by status and code), §4.4 (the per-run credential
at rest) and the owner's decisions of 2026-10-17 (Q2 a per-run credential
dies with its task or at `expires_in`; Q7 a child uses only its parent's
servers, with its parent's stored credential).

Drives the real routes (`POST /v1/tasks`, `/v1/tasks/batch`, `/v1/workflows`
and the child route) over the in-memory Firestore, with the MCP-API lane's
registrations, its in-memory Secret Manager and its attach, so "attached with
the parent's credential" is the attach's own answer. The schedule checks
are the pure `schedules.build_schedule` and `apply_edit`.

The attackers these stand in for:

  * A CALLER NAMING AN ARBITRARY SERVER: only a registered, enabled server of
    its own tenant, allowed for the profile, is accepted, and the refusal
    comes before anything is stored.
  * A LOG, DATABASE OR RESPONSE READER: a runtime-built credential is in no
    4xx body, no task or workflow document and no response, and reaches
    Secret Manager through exactly one `add_version`, under a TTL.
  * AN AGENT WRITING A CHILD'S SPEC: it may name only servers its parent
    named, and gets no credential of its own.
  * A SCHEDULE, which has no caller online to mint a credential: a
    `dispatch` server is refused at creation and on edit.

Every fake value is built at runtime (CLAUDE.md: nothing added may look like
a credential).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import schedules
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.errors import ValidationFailed
from swarm_api.mcp_credentials import COLLECTION as CREDENTIALS
from swarm_api.mcp_servers import FLAG, McpServers, server_id

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    KEY,
    Attempt,
    register,
    seed_parent,
    submit,
    worker_tokens,
)
from .test_mcp_attach import FakeSM, attach
from .spec_signer import KEY_VERSION
from .test_secret_release import ALICE, ROOT, SIGNER, _personal

REPO = "https://github.com/acme/widgets"


def fake_credential(fill: str = "c") -> str:
    """Shaped like a bearer token, built at runtime so no literal looks like one."""
    return "mcp" + "-" + fill * 24 + secrets.token_hex(8)


class RecordingSM(FakeSM):
    """The in-memory Secret Manager, recording every create (with its TTL) and add."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[str, str, timedelta]] = []
        self.added: list[tuple[str, bytes]] = []

    def create_secret(self, tenant_id: str, secret_id: str, ttl: Any = None) -> None:
        self.created.append((tenant_id, secret_id, ttl))

    def add_version(self, secret_id: str, payload: bytes) -> str:
        self.added.append((secret_id, payload))
        return super().add_version(secret_id, payload)


@pytest.fixture
def sm() -> RecordingSM:
    return RecordingSM()


def _client(db, worker_tokens, group_map, objects, sm, **settings: Any) -> TestClient:  # noqa: F811
    """The secret release's context, with submissions signed by the key the
    attach verifies, and the in-memory Secret Manager as both the per-run
    credential writer (submission) and the reader (attach)."""
    ctx = build_context(
        settings=api_settings(**{"child_key": KEY, "spec_signing_key_version": KEY_VERSION,
                                 "spec_verify_keys": json.dumps({KEY_VERSION: SIGNER.public_pem()}),
                                 **settings}), db=db, verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects, signer=SIGNER,
        forge_tokens=NoForgeTokens(),
    )
    ctx.submissions.mcp_credential_writer = sm
    application = create_app(ctx)
    application.state.workspace_secret_reader = sm
    application.state.mcp_credential_writer = sm
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def client(db, worker_tokens, group_map, objects, sm, monkeypatch) -> TestClient:  # noqa: F811
    monkeypatch.setenv(FLAG, "on")
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _personal(db, "u-alice", ALICE)
    return _client(db, worker_tokens, group_map, objects, sm)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def registered(db, name: str = "kb", *, tenant_id: str = "eng", mode: str = "dispatch",
               profiles: list[str] | None = None, enabled: bool = True) -> dict[str, Any]:
    McpServers(db, now=_now).register(
        tenant_id=tenant_id, name=name, url=f"https://{name}.example.com/mcp",
        description=f"the {name} graph", auth={"mode": mode},
        allowed_profiles=profiles or [], allowed_tools=["search"], limits=None,
        enabled=enabled, by=ROOT)
    return db.docs[f"mcp_servers/{server_id(tenant_id, name)}"]


def task_body(servers: list[str] | None = None, credentials: dict[str, Any] | None = None,
              *, profile: str = "claude-code") -> dict[str, Any]:
    body: dict[str, Any] = {"runner_profile": profile, "input": {"prompt": "look it up"},
                            "repository_url": REPO}
    if servers is not None:
        body["input"]["mcp_servers"] = servers
    if credentials is not None:
        body["mcp_credentials"] = credentials
    return body


def cred(value: str, expires_in: Any = 3600) -> dict[str, Any]:
    return {"value": value, "expires_in": expires_in}


def post_task(client, body: dict[str, Any], user: str = "alice") -> Any:
    return client.post("/v1/tasks", headers=auth_header(user), json=body)


def workflow_body(steps: list[dict[str, Any]], credentials: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    body: dict[str, Any] = {"repository_url": REPO, "steps": steps}
    if credentials is not None:
        body["mcp_credentials"] = credentials
    return body


def step(step_id: str, servers: list[str] | None = None, *, depends_on: list[str] | None = None
         ) -> dict[str, Any]:
    payload: dict[str, Any] = {"prompt": f"do {step_id}"}
    if servers is not None:
        payload["mcp_servers"] = servers
    return {"step_id": step_id, "runner_profile": "claude-code", "input": payload,
            "depends_on": depends_on or []}


def tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def workflows(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("workflows/") and p.count("/") == 1]


def credential_rows(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{CREDENTIALS}/")]


def every_document(db) -> str:
    return json.dumps({p: d for p, d in db.docs.items()}, default=str)


def nothing_stored(db, sm) -> None:
    assert tasks(db) == [] and workflows(db) == []
    assert credential_rows(db) == [] and sm.added == [] and sm.created == []


# --------------------------------------------------------------------------
# §4.3: the refusals, each before anything is stored
# --------------------------------------------------------------------------


def test_an_unregistered_name_is_422_before_anything_is_stored(client, db, sm):
    registered(db, "kb", tenant_id="research")  # another tenant's: not ours
    registered(db, "off", enabled=False)
    value = fake_credential()
    for name in ("nobody", "kb", "off"):
        response = post_task(client, task_body([name], {name: cred(value)}))
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "mcp_server_unregistered"
        assert value not in response.text
    nothing_stored(db, sm)


def test_a_profile_the_registration_does_not_allow_is_403(client, db, sm):
    registered(db, profiles=["codex"])
    value = fake_credential()
    response = post_task(client, task_body(["kb"], {"kb": cred(value)}))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "mcp_server_profile_refused"
    assert value not in response.text
    nothing_stored(db, sm)


def test_a_dispatch_server_with_no_credential_is_422(client, db, sm):
    registered(db)
    response = post_task(client, task_body(["kb"]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_missing"
    nothing_stored(db, sm)


@pytest.mark.parametrize("case", ["not_named", "mode_none", "mode_workspace_secret"])
def test_an_unexpected_credential_is_422(client, db, sm, case):
    registered(db)
    registered(db, "plain", mode="none")
    registered(db, "vault", mode="workspace_secret")
    value = fake_credential()
    if case == "not_named":
        body = task_body(["kb"], {"kb": cred(fake_credential("d")), "plain": cred(value)})
    elif case == "mode_none":
        body = task_body(["plain"], {"plain": cred(value)})
    else:
        body = task_body(["vault"], {"vault": cred(value)})
    response = post_task(client, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_unexpected"
    assert value not in response.text
    nothing_stored(db, sm)


def _malformed(case: str) -> dict[str, Any]:
    if case == "short":
        return cred("x" * 7)
    if case == "long":
        return cred("x" * (8 * 1024 + 1))
    if case == "newline":
        return cred(fake_credential() + "\n" + fake_credential())
    if case == "not_ascii":
        return cred(fake_credential() + "é")
    if case == "not_a_string":
        return cred(12345678901234)
    if case == "expires_too_soon":
        return cred(fake_credential(), 59)
    if case == "expires_too_late":
        return cred(fake_credential(), 86401)
    return cred(fake_credential(), "3600")


@pytest.mark.parametrize("case", ["short", "long", "newline", "not_ascii", "not_a_string",
                                  "expires_too_soon", "expires_too_late", "expires_a_string"])
def test_a_malformed_credential_is_422_and_its_value_is_never_echoed(client, db, sm, case):
    registered(db)
    entry = _malformed(case)
    response = post_task(client, task_body(["kb"], {"kb": entry}))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_shape"
    if isinstance(entry["value"], str) and len(entry["value"]) >= 8:
        assert entry["value"] not in response.text
    nothing_stored(db, sm)


@pytest.mark.parametrize("field", ["header", "url", "secret_id"])
def test_a_credential_entry_takes_value_and_expires_in_and_nothing_else(client, db, sm, field):
    registered(db)
    value = fake_credential()
    response = post_task(client, task_body(["kb"], {"kb": {**cred(value), field: value}}))
    assert response.status_code == 422, response.text
    assert value not in response.text
    nothing_stored(db, sm)


def test_a_credential_is_kept_exactly_as_sent_with_its_spaces(client, db, sm):
    """The request models strip whitespace from every string; a credential is
    not a string to tidy, so a leading or trailing space reaches Secret
    Manager as the caller sent it."""
    registered(db)
    value = " " + fake_credential() + " "
    response = post_task(client, task_body(["kb"], {"kb": cred(value)}))
    assert response.status_code == 201, response.text
    assert [payload for _, payload in sm.added] == [value.encode("ascii")]


# --------------------------------------------------------------------------
# The feature ships dark: off, nothing about MCP is accepted and nothing changes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("what", ["servers", "credentials", "both"])
def test_off_every_mcp_submission_is_refused_mcp_unavailable(client, db, sm, monkeypatch, what):
    registered(db, mode="none")
    monkeypatch.setenv(FLAG, "off")
    value = fake_credential()
    body = task_body(["kb"] if what != "credentials" else None,
                     {"kb": cred(value)} if what != "servers" else None)
    response = post_task(client, body)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_unavailable"
    assert value not in response.text
    nothing_stored(db, sm)
    # The same workflow step, refused the same way.
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=workflow_body([step("a", ["kb"])]))
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_unavailable"
    nothing_stored(db, sm)


def test_off_a_submission_naming_no_server_is_unchanged(client, db, sm, monkeypatch):
    monkeypatch.setenv(FLAG, "off")
    response = post_task(client, task_body())
    assert response.status_code == 201, response.text
    assert len(tasks(db)) == 1 and sm.added == []


def test_with_no_child_key_a_named_server_is_mcp_unavailable(db, worker_tokens, group_map,  # noqa: F811
                                                             objects, sm, monkeypatch):
    """No child key, no attempt key, no attach (§4.6): refused at submission
    rather than admitted to fail."""
    monkeypatch.setenv(FLAG, "on")
    seed_tenant(db, "eng")
    application = _client(db, worker_tokens, group_map, objects, sm, child_key="")
    registered(db, mode="none")
    response = post_task(application, task_body(["kb"]))
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "mcp_unavailable"
    assert tasks(db) == []


# --------------------------------------------------------------------------
# §4.4: the credential's one path, into Secret Manager under a TTL
# --------------------------------------------------------------------------


def test_a_task_stores_its_credential_once_with_a_ttl_and_nowhere_else(client, db, sm):
    registered(db)
    registered(db, "plain", mode="none")
    value = fake_credential()
    response = post_task(client, task_body(["kb", "plain"], {"kb": cred(value, 600)}))
    assert response.status_code == 201, response.text
    assert value not in response.text
    [task] = tasks(db)
    assert task["input"]["mcp_servers"] == ["kb", "plain"]
    assert "mcp_credentials" not in task
    # Exactly one Secret Manager write of the value, under a TTL.
    [(secret_id, payload)] = sm.added
    assert payload == value.encode("ascii")
    [(tenant_id, created_id, ttl)] = sm.created
    assert (tenant_id, created_id) == ("eng", secret_id)
    assert secret_id.startswith("swarm-tenant-eng-mcpc-")
    assert ttl == timedelta(seconds=600) + timedelta(minutes=5)
    # The row names the secret and is owned by the task.
    [row] = credential_rows(db)
    assert row["owner_id"] == task["id"] and row["owner_kind"] == "task"
    assert row["server_id"] == server_id("eng", "kb") and row["secret_id"] == secret_id
    # In no Firestore document at all: tasks, events, rows, audit.
    assert value not in every_document(db)
    # And not in the task as any later read serves it.
    read = client.get(f"/v1/tasks/{task['id']}", headers=auth_header("alice"))
    assert read.status_code == 200 and value not in read.text


def test_the_submission_result_says_when_each_credential_expires_and_nothing_else(
        client, db, sm, monkeypatch):
    """§4.3's echo: `{server, expires_at}` per stored credential, never the
    value, its length, a prefix or a hash. Read off the service's result,
    which the route answers from."""
    registered(db)
    value = fake_credential()
    service = client.app.state.ctx.submissions
    results: list[Any] = []
    original = service.submit_tasks

    def recording(*args: Any, **kwargs: Any) -> Any:
        results.append(original(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(service, "submit_tasks", recording)
    response = post_task(client, task_body(["kb"], {"kb": cred(value, 900)}))
    assert response.status_code == 201, response.text
    [result] = results
    [task] = result.tasks
    [echo] = result.mcp_credentials[task.id]
    assert set(echo) == {"server", "expires_at"} and echo["server"] == "kb"
    assert value not in json.dumps(result.mcp_credentials)


def test_a_batch_stores_one_credential_per_task(client, db, sm):
    registered(db)
    first, second = fake_credential("a"), fake_credential("b")
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"), json={"tasks": [
        task_body(["kb"], {"kb": cred(first)}), task_body(["kb"], {"kb": cred(second)}),
    ]})
    assert response.status_code == 201, response.text
    assert first not in response.text and second not in response.text
    assert sorted(p for _, p in sm.added) == sorted([first.encode(), second.encode()])
    owners = sorted(r["owner_id"] for r in credential_rows(db))
    assert owners == sorted(t["id"] for t in tasks(db))
    assert first not in every_document(db) and second not in every_document(db)


def test_a_refused_batch_entry_stores_nothing_for_any_entry(client, db, sm):
    registered(db)
    good = fake_credential("a")
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"), json={"tasks": [
        task_body(["kb"], {"kb": cred(good)}), task_body(["nobody"]),
    ]})
    assert response.status_code == 422, response.text
    assert good not in response.text
    nothing_stored(db, sm)


# --------------------------------------------------------------------------
# Workflows: one map for the submission, serving every step
# --------------------------------------------------------------------------


def test_a_workflows_map_is_stored_once_and_serves_every_step(client, db, sm):
    registered(db)
    value = fake_credential()
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=workflow_body(
        [step("a", ["kb"]), step("b", ["kb"], depends_on=["a"])], {"kb": cred(value)}))
    assert response.status_code == 201, response.text
    assert value not in response.text
    [workflow] = workflows(db)
    [row] = credential_rows(db)
    assert row["owner_kind"] == "workflow" and row["owner_id"] == workflow["workflow_id"]
    assert [p for _, p in sm.added] == [value.encode("ascii")]
    assert value not in every_document(db)

    # Every step is attached the one stored credential.
    for index, task in enumerate(sorted(tasks(db), key=lambda t: t["step_id"])):
        attempt = Attempt(task=task["id"], attempt=f"att_wf_{index}", lease=f"lease_wf_{index}")
        stored_input, stored_wf = dict(task["input"]), task["workflow_id"]
        doc = seed_parent(db, attempt, state="STARTING", runner_profile="claude-code",
                          input=stored_input, workflow_id=stored_wf, submitted_by=ALICE)
        assert register(client, db, attempt).status_code == 201
        SIGNER.sign_document(doc, task["id"])
        doc["state"] = "RUNNING"
        answer = attach(client, attempt)
        assert answer.status_code == 200, answer.text
        [server] = answer.json()["servers"]
        assert server["name"] == "kb" and server["auth"]["credential"] == value


def test_a_workflow_step_naming_a_server_the_map_lacks_is_refused(client, db, sm):
    registered(db)
    registered(db, "other")
    value = fake_credential()
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=workflow_body(
        [step("a", ["kb"]), step("b", ["other"])], {"kb": cred(value)}))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_missing"
    assert value not in response.text
    nothing_stored(db, sm)


def test_a_workflow_map_key_no_step_names_is_unexpected(client, db, sm):
    registered(db)
    registered(db, "other")
    value = fake_credential()
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=workflow_body(
        [step("a", ["kb"])], {"kb": cred(fake_credential("d")), "other": cred(value)}))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_unexpected"
    assert value not in response.text
    nothing_stored(db, sm)


def test_a_workflow_step_naming_an_unregistered_server_is_refused(client, db, sm):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=workflow_body([step("a", ["nobody"])]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_server_unregistered"
    nothing_stored(db, sm)


# --------------------------------------------------------------------------
# Q7: a child names only its parent's servers, and uses its parent's credential
# --------------------------------------------------------------------------


def _running_parent(client, db, servers: list[str]) -> Attempt:
    attempt = Attempt(task="task_mcp_parent", attempt="att_mcp_parent", lease="lease_mcp_parent")
    doc = seed_parent(db, attempt, state="STARTING", runner_profile="claude-code",
                      input={"prompt": "parent", "mcp_servers": servers}, submitted_by=ALICE)
    assert register(client, db, attempt).status_code == 201
    SIGNER.sign_document(doc, attempt.tuple.task_id)
    doc["state"] = "RUNNING"
    return attempt


def _store_for(db, sm, owner_id: str, name: str = "kb") -> str:
    from swarm_api.mcp_credentials import McpCredentials, RowKeys

    value = fake_credential("p")
    McpCredentials(db, sm, now=_now, keys=RowKeys(KEY)).store(
        tenant_id="eng", owner_id=owner_id, owner_kind="task",
        registration=db.docs[f"mcp_servers/{server_id('eng', name)}"], value=value,
        expires_in=3600, by=ALICE)
    return value


def _child(servers: list[str]) -> dict[str, Any]:
    return {"runner_profile": "claude-code", "input": {"prompt": "help", "mcp_servers": servers}}


def test_a_child_cannot_add_a_server_its_parent_did_not_name(client, db, sm):
    registered(db)
    registered(db, "other", mode="none")
    parent = _running_parent(client, db, ["kb"])
    _store_for(db, sm, parent.tuple.task_id)
    response = submit(client, parent, child=_child(["kb", "other"]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_server_not_inherited"
    assert [t for t in tasks(db) if t.get("parent_task_id")] == []


def test_a_child_whose_parent_rewrote_its_own_server_list_cannot_add_one(client, db, sm):
    """The parent's list is read from its SIGNED spec: an agent that edits its
    own task document to name another server fails the parent's signature."""
    registered(db)
    registered(db, "other", mode="none")
    parent = _running_parent(client, db, ["kb"])
    db.docs[f"tasks/{parent.tuple.task_id}"]["input"]["mcp_servers"] = ["kb", "other"]
    response = submit(client, parent, child=_child(["other"]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_server_not_inherited"
    assert [t for t in tasks(db) if t.get("parent_task_id")] == []


def test_a_child_naming_its_parents_server_is_attached_with_the_parents_credential(
        client, db, sm):
    registered(db)
    parent = _running_parent(client, db, ["kb"])
    value = _store_for(db, sm, parent.tuple.task_id)
    added_before = len(sm.added)
    response = submit(client, parent, child=_child(["kb"]))
    assert response.status_code == 201, response.text
    child_id = response.json()["task"]["id"]
    child_doc = db.docs[f"tasks/{child_id}"]
    assert child_doc["parent_task_id"] == parent.tuple.task_id
    assert child_doc["input"]["mcp_servers"] == ["kb"]
    # No credential of its own: nothing stored for the child.
    assert len(sm.added) == added_before
    assert [r["owner_id"] for r in credential_rows(db)] == [parent.tuple.task_id]
    assert value not in every_document(db)

    child = Attempt(task=child_id, attempt="att_mcp_child", lease="lease_mcp_child")
    seed_parent(db, child, state="STARTING", runner_profile="claude-code",
                input=dict(child_doc["input"]), parent_task_id=parent.tuple.task_id,
                parent_attempt_id=parent.tuple.attempt_id, submitted_by=ALICE)
    assert register(client, db, child).status_code == 201
    SIGNER.sign_document(db.docs[f"tasks/{child_id}"], child_id)
    db.docs[f"tasks/{child_id}"]["state"] = "RUNNING"
    answer = attach(client, child)
    assert answer.status_code == 200, answer.text
    [server] = answer.json()["servers"]
    assert server["name"] == "kb" and server["auth"]["credential"] == value


def test_a_child_naming_a_dispatch_server_its_parent_has_no_live_credential_for_is_refused(
        client, db, sm):
    registered(db)
    parent = _running_parent(client, db, ["kb"])
    response = submit(client, parent, child=_child(["kb"]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_missing"
    assert [t for t in tasks(db) if t.get("parent_task_id")] == []


def test_a_child_naming_no_server_is_unchanged(client, db, sm):
    parent = _running_parent(client, db, [])
    response = submit(client, parent, child={"runner_profile": "mock", "input": {"prompt": "x"}})
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# Schedules: no caller online, so no `dispatch` server, at creation or on edit
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _schedule_body(servers: list[str] | None, *, credentials: dict[str, Any] | None = None
                   ) -> schedules.ScheduleCreate:
    spec: dict[str, Any] = {"repository_url": REPO, "steps": [step("s0", servers)]}
    if credentials is not None:
        spec["mcp_credentials"] = credentials
    return schedules.parse_create({
        "name": "nightly kb digest", "type": "custom-prompt",
        "scope": {"mode": "repos", "repo_ids": ["repo_a"]},
        "cron": "0 9 * * 1-5", "params": {"spec": spec},
    })


def _build(body: schedules.ScheduleCreate, modes: dict[str, str] | None) -> dict[str, Any]:
    return schedules.build_schedule(
        body, tenant_id="eng", actor=ALICE, now=NOW, registered_repo_ids=["repo_a"],
        is_admin=True, is_owner=True, mcp_modes=modes)


def test_a_schedule_naming_a_dispatch_server_is_refused_at_creation():
    with pytest.raises(ValidationFailed) as caught:
        _build(_schedule_body(["kb"]), {"kb": "dispatch"})
    assert caught.value.code == "mcp_credential_missing"


@pytest.mark.parametrize("mode", ["none", "workspace_secret"])
def test_a_schedule_naming_a_none_or_workspace_secret_server_is_accepted(mode):
    doc = _build(_schedule_body(["kb"]), {"kb": mode})
    assert doc["params"]["spec"]["steps"][0]["input"]["mcp_servers"] == ["kb"]


def test_a_schedule_whose_servers_were_not_read_is_refused():
    """Fail closed: a caller that did not read the registrations cannot have
    checked that none is a `dispatch` server."""
    with pytest.raises(ValidationFailed) as caught:
        _build(_schedule_body(["kb"]), None)
    assert caught.value.code == "mcp_credential_missing"


def test_a_schedule_carrying_a_credential_is_refused_and_never_stores_it():
    value = fake_credential()
    with pytest.raises(ValidationFailed) as caught:
        _build(_schedule_body(["kb"], credentials={"kb": cred(value)}), {"kb": "none"})
    assert caught.value.code == "mcp_credential_unexpected"
    assert value not in str(caught.value) and value not in json.dumps(
        getattr(caught.value, "detail", None) or {}, default=str)


def test_a_schedule_naming_no_server_is_unchanged():
    doc = _build(_schedule_body(None), None)
    assert "mcp_servers" not in doc["params"]["spec"]["steps"][0]["input"]
    assert "mcp_credentials" not in doc["params"]["spec"]


def test_an_edit_adding_a_dispatch_server_is_refused():
    stored = _build(_schedule_body(None), None)
    spec = json.loads(json.dumps(stored["params"]["spec"]))
    spec["steps"][0]["input"]["mcp_servers"] = ["kb"]
    patch = schedules.parse_patch({"revision": 1, "params": {"spec": spec}})
    with pytest.raises(ValidationFailed) as caught:
        schedules.apply_edit(stored, patch, actor=ALICE, now=NOW, registered_repo_ids=["repo_a"],
                             is_admin=True, is_owner=True, mcp_modes={"kb": "dispatch"})
    assert caught.value.code == "mcp_credential_missing"
    edited = schedules.apply_edit(stored, patch, actor=ALICE, now=NOW,
                                  registered_repo_ids=["repo_a"], is_admin=True, is_owner=True,
                                  mcp_modes={"kb": "none"})
    assert edited["params"]["spec"]["steps"][0]["input"]["mcp_servers"] == ["kb"]


def test_an_edit_carrying_a_credential_is_refused():
    stored = _build(_schedule_body(None), None)
    spec = json.loads(json.dumps(stored["params"]["spec"]))
    spec["mcp_credentials"] = {"kb": cred(fake_credential())}
    patch = schedules.parse_patch({"revision": 1, "params": {"spec": spec}})
    with pytest.raises(ValidationFailed) as caught:
        schedules.apply_edit(stored, patch, actor=ALICE, now=NOW, registered_repo_ids=["repo_a"],
                             is_admin=True, is_owner=True, mcp_modes={"kb": "none"})
    assert caught.value.code == "mcp_credential_unexpected"


def test_a_fire_naming_a_dispatch_server_is_refused_by_the_submission(client, db, sm):
    """A firing goes through `submit_workflow` like any submission: a stored
    spec that somehow names a `dispatch` server has no map, and is refused."""
    registered(db)
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=workflow_body([step("s0", ["kb"])]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "mcp_credential_missing"
    nothing_stored(db, sm)


def test_the_pure_dispatch_spelling_is_the_registrys():
    """`validation` restates `mcp_servers.MODE_DISPATCH` to stay import-free."""
    from swarm_api.mcp_servers import MODE_DISPATCH
    from swarm_api.validation import MCP_DISPATCH_MODE

    assert MCP_DISPATCH_MODE == MODE_DISPATCH


def test_no_dump_of_a_request_carries_a_credential():
    """`exclude=True`: a spec stored as JSON or logged whole holds none."""
    from swarm_api.schemas import TaskCreate, WorkflowCreate

    value = fake_credential()
    task = TaskCreate.model_validate(task_body(["kb"], {"kb": cred(value)}))
    flow = WorkflowCreate.model_validate(workflow_body([step("a", ["kb"])], {"kb": cred(value)}))
    assert task.mcp_credentials["kb"].value == value
    for model in (task, flow):
        assert value not in json.dumps(model.model_dump(mode="json"))
        assert value not in model.model_dump_json() and value not in repr(model)


def test_the_service_reads_the_modes_a_schedule_is_checked_against(client, db):
    """`SubmissionService.mcp_auth_modes`: the tenant's enabled registrations
    only, by name, so the schedule routes can pass `mcp_modes` in one call."""
    registered(db)
    registered(db, "plain", mode="none")
    registered(db, "off", enabled=False)
    registered(db, "theirs", tenant_id="research")
    modes = client.app.state.ctx.submissions.mcp_auth_modes("eng")
    assert modes == {"kb": "dispatch", "plain": "none"}
    with pytest.raises(ValidationFailed) as caught:
        _build(_schedule_body(["plain", "kb"]), modes)
    assert caught.value.code == "mcp_credential_missing"
    assert caught.value.detail["servers"] == ["kb"]
