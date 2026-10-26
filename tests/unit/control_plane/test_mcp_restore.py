"""A workflow's caller re-stores its per-run MCP credentials (MCP-KEY-PARK).

Design: docs/design/registered-mcp-servers.md §4.10, and BGD's decision of
2026-10-26 (~22:55Z), for Product Oracle's stalled runs: five Code map runs
waited about 4.7 h on an account, were promoted at 22:45Z and FAILED
`mcp_credential_expired` at once, because their 7200 s credentials had lapsed
at 20:00Z. Now such a step parks CREDENTIAL_MISSING with `credential: "mcp"`
(tests/unit/scheduler/test_mcp_admission.py), and its caller, who is online,
re-stores the credential:

    PUT /v1/workflows/{workflow_id}/mcp-credentials
    {"mcp_credentials": {<server>: {"value", "expires_in"}}}

  * it REPLACES the credential of each server the body names and leaves the
    others untouched; a server no step names is 422, before anything is
    stored;
  * only the submitter or an admin of the workspace may call it (403); a
    workflow of another tenant is 404, as GET answers; a finished workflow is
    409 `workflow_finished`;
  * it writes a new secret version and a new live row (the create path's
    writer), answers `{"stored": [{"server", "expires_at"}], "promoted": n}`,
    and returns each step parked on that server to READY in the same request;
  * no value is in the answer, a document, an event or a log.

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

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import UNCAPTURED_ROUTES, create_app
from swarm_api.mcp_credentials import COLLECTION as CREDENTIALS
from swarm_api.mcp_credentials import PARK_CREDENTIAL
from swarm_api.mcp_servers import FLAG, server_id
from swarm_api.metrics import ApiMetrics
from swarm_api.refusals import env_name
from swarm_api.roles import ADMIN, Resolution
from swarm_api.routes.workflows import MCP_CREDENTIALS_ROUTE
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings
from .conftest import auth_header, seed_tenant
from .spec_signer import KEY_VERSION
from .test_child_tasks_api import KEY, worker_tokens  # noqa: F401  (a fixture)
from .test_mcp_submit import (
    RecordingSM,
    cred,
    credential_rows,
    every_document,
    fake_credential,
    registered,
    step,
    tasks,
    workflow_body,
    workflows,
)
from .test_secret_release import ALICE, SIGNER, _personal

DAVE = "dave@example.com"


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """The suite's people, and `dave`: a member of eng who submitted nothing."""
    return {
        ALICE: (ENG_GROUP,),
        "bob@example.com": (RESEARCH_GROUP,),
        "carol@example.com": (),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
        DAVE: (ENG_GROUP,),
    }


@pytest.fixture
def sm() -> RecordingSM:
    return RecordingSM()


@pytest.fixture
def waker() -> NullWaker:
    return NullWaker()


@pytest.fixture
def app_ctx(db, worker_tokens, group_map, objects, sm, waker, monkeypatch):  # noqa: F811
    monkeypatch.setenv(FLAG, "on")
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _personal(db, "u-alice", ALICE)
    ctx = build_context(
        settings=api_settings(**{"child_key": KEY, "spec_signing_key_version": KEY_VERSION,
                                 "spec_verify_keys": json.dumps({KEY_VERSION: SIGNER.public_pem()})}),
        db=db, verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=waker, metrics=ApiMetrics(),
        objects=objects, signer=SIGNER, forge_tokens=NoForgeTokens(),
    )
    ctx.submissions.mcp_credential_writer = sm
    application = create_app(ctx)
    application.state.workspace_secret_reader = sm
    application.state.mcp_credential_writer = sm
    return ctx, TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def client(app_ctx) -> TestClient:
    return app_ctx[1]


def submit(client, db, servers: dict[str, list[str]], credentials: dict[str, Any]) -> str:
    """A workflow of one step per entry, each naming its servers; its id."""
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=workflow_body(
        [step(step_id, names) for step_id, names in servers.items()], credentials))
    assert response.status_code == 201, response.text
    return response.json()["workflow"]["workflow_id"]


def put(client, workflow_id: str, credentials: dict[str, Any], user: str = "alice") -> Any:
    return client.put(f"/v1/workflows/{workflow_id}/mcp-credentials",
                      headers=auth_header(user), json={"mcp_credentials": credentials})


def row(db, workflow_id: str, name: str = "kb") -> dict[str, Any]:
    return db.docs[f"{CREDENTIALS}/{workflow_id}~{server_id('eng', name)}"]


def step_task(db, step_id: str) -> dict[str, Any]:
    [doc] = [t for t in tasks(db) if t["step_id"] == step_id]
    return doc


def park_on_mcp(doc: dict[str, Any], name: str = "kb") -> None:
    """The park the scheduler writes when the workflow's credential lapsed
    (`loop.mcp_park_blocked_by`), in the shape a caller reads it."""
    doc.update({
        "state": "PARKED", "park_reason": "CREDENTIAL_MISSING",
        "blocked_by": [{"reason": "CREDENTIAL_MISSING", "credential": "mcp",
                        "code": "mcp_credential_expired", "server": name,
                        "expired_at": "2026-10-26T20:00:00+00:00",
                        "parked_at": "2026-10-26T22:45:00+00:00"}],
    })


def events(db, task_id: str, kind: str) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [d for p, d in sorted(db.docs.items())
            if p.startswith(prefix) and d.get("type") == kind]


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_the_route_is_uncaptured_and_the_park_marker_is_the_schedulers():
    """Its body carries credentials, so no body capture may read it; and the
    marker a park names is the scheduler's own, restated here."""
    from scheduler.mcp_admission import MCP_CREDENTIAL

    assert MCP_CREDENTIALS_ROUTE in UNCAPTURED_ROUTES
    assert PARK_CREDENTIAL == MCP_CREDENTIAL


def test_a_restore_replaces_the_row_and_answers_its_new_expiry(client, db, sm, caplog):
    registered(db)
    first = fake_credential("a")
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(first, 120)})
    before = dict(row(db, workflow_id))
    fresh = fake_credential("b")

    with caplog.at_level(logging.INFO):
        response = put(client, workflow_id, {"kb": cred(fresh, 7200)})

    assert response.status_code == 200, response.text
    answer = response.json()
    after = row(db, workflow_id)
    assert answer == {"stored": [{"server": "kb", "expires_at": after["expires_at"].isoformat()}],
                      "promoted": 0}
    assert after["state"] == "live" and after["owner_kind"] == "workflow"
    assert after["secret_id"] != before["secret_id"]
    assert after["expires_at"] > before["expires_at"]
    assert [p for _, p in sm.added] == [first.encode("ascii"), fresh.encode("ascii")]
    # Never echoed: not in the answer, a document or a log line.
    for value in (first, fresh):
        assert value not in response.text and value not in every_document(db)
        assert value not in caplog.text
    assert f"mcp credential stored tenant=eng owner={workflow_id}" in caplog.text


def test_a_server_the_body_does_not_name_is_untouched(client, db, sm):
    registered(db)
    registered(db, "crm")
    workflow_id = submit(client, db, {"a": ["kb", "crm"]},
                         {"kb": cred(fake_credential("a")), "crm": cred(fake_credential("c"))})
    crm = dict(row(db, workflow_id, "crm"))

    response = put(client, workflow_id, {"kb": cred(fake_credential("b"))})

    assert response.status_code == 200, response.text
    assert [s["server"] for s in response.json()["stored"]] == ["kb"]
    assert row(db, workflow_id, "crm") == crm


@pytest.mark.parametrize("credentials, code", [
    ({"other": cred(fake_credential("o"))}, "mcp_credential_unexpected"),
    ({"kb": cred(fake_credential("k"), 30)}, "mcp_credential_shape"),
    ({"kb": cred("short")}, "mcp_credential_shape"),
], ids=["server-no-step-names", "expires-in-too-short", "value-too-short"])
def test_a_body_the_create_path_would_refuse_is_refused_before_anything_is_stored(
    client, db, sm, credentials, code
):
    registered(db)
    registered(db, "other")
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    rows, added = [dict(r) for r in credential_rows(db)], list(sm.added)

    response = put(client, workflow_id, credentials)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert [dict(r) for r in credential_rows(db)] == rows and sm.added == added
    for entry in credentials.values():
        assert entry["value"] not in response.text


# --------------------------------------------------------------------------
# Who may call it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("switch, code", [
    (None, "forbidden"), ("on", "mcp_credential_restore_forbidden"),
], ids=["switch-off", "switch-on"])
def test_another_member_of_the_workspace_is_refused(client, db, sm, monkeypatch, switch, code):
    """Refused either way. The new code ships behind its switch (observer
    proposal I, `swarm_api.refusals`): off, the established `forbidden`."""
    if switch is not None:
        monkeypatch.setenv(env_name("mcp_credential_restore_forbidden"), switch)
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    before = dict(row(db, workflow_id))

    response = put(client, workflow_id, {"kb": cred(fake_credential("d"))}, user="dave")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == code
    assert row(db, workflow_id) == before


def test_a_workflow_of_another_tenant_reads_as_not_found(client, db, sm):
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    before = dict(row(db, workflow_id))

    response = put(client, workflow_id, {"kb": cred(fake_credential("e"))}, user="bob")

    assert response.status_code == 404, response.text
    assert client.get(f"/v1/workflows/{workflow_id}",
                      headers=auth_header("bob")).status_code == 404
    assert row(db, workflow_id) == before


def test_a_platform_admin_may_restore(client, db, sm):
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})

    response = put(client, workflow_id, {"kb": cred(fake_credential("r"))}, user="root")

    assert response.status_code == 200, response.text


def test_an_admin_of_the_workspace_may_restore(app_ctx, client, db, sm, monkeypatch):
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    ctx = app_ctx[0]
    monkeypatch.setattr(ctx.roles, "try_resolve",
                        lambda auth: Resolution(role=ADMIN, source="override")
                        if auth.email == DAVE else None)

    response = put(client, workflow_id, {"kb": cred(fake_credential("w"))}, user="dave")

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("ended", ["SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"])
@pytest.mark.parametrize("switch, code", [
    (None, "conflict"), ("on", "workflow_finished"),
], ids=["switch-off", "switch-on"])
def test_a_finished_workflow_answers_409(client, db, sm, monkeypatch, ended, switch, code):
    """409 either way; `workflow_finished` once its switch is on, and the
    established `conflict` with the same message until then."""
    if switch is not None:
        monkeypatch.setenv(env_name("workflow_finished"), switch)
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    step_task(db, "a")["state"] = ended
    before = dict(row(db, workflow_id))

    response = put(client, workflow_id, {"kb": cred(fake_credential("f"))})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert "has finished" in response.json()["message"]
    assert row(db, workflow_id) == before


# --------------------------------------------------------------------------
# The park it ends
# --------------------------------------------------------------------------


def test_a_restore_returns_the_steps_parked_on_that_server_to_ready(client, db, sm, waker):
    registered(db)
    registered(db, "crm")
    workflow_id = submit(client, db, {"a": ["kb"], "b": ["crm"], "c": ["kb"]},
                         {"kb": cred(fake_credential("a"), 120),
                          "crm": cred(fake_credential("c"), 120)})
    park_on_mcp(step_task(db, "a"), "kb")
    park_on_mcp(step_task(db, "b"), "crm")
    # Parked CREDENTIAL_MISSING for something a re-store does not fix.
    step_task(db, "c").update({"state": "PARKED", "park_reason": "CREDENTIAL_MISSING",
                                "blocked_by": [{"reason": "CREDENTIAL_MISSING",
                                                "provider": "anthropic"}]})

    response = put(client, workflow_id, {"kb": cred(fake_credential("k"), 7200)})

    assert response.status_code == 200, response.text
    assert response.json()["promoted"] == 1
    a, b, c = step_task(db, "a"), step_task(db, "b"), step_task(db, "c")
    assert (a["state"], a["park_reason"], a["blocked_by"]) == ("READY", None, [])
    [ready] = events(db, a["id"], "ready")
    assert ready["detail"] == {"reason": "mcp_credential_restored",
                               "park_reason": "CREDENTIAL_MISSING", "servers": ["kb"]}
    assert (b["state"], b["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert (c["state"], c["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert events(db, b["id"], "ready") == [] and events(db, c["id"], "ready") == []
    # The scheduler is woken, so admission asks the whole question again.
    assert ("mcp_credential_restored", {"tenant_id": "eng", "workflow_id": workflow_id}) \
        in waker.calls
    # Nothing leased here: READY holds nothing (invariant 1).
    assert [p for p in db.docs if p.startswith("leases/")] == []


def test_the_restored_row_is_what_the_scheduler_admits_on(client, db, sm):
    """The row the PUT writes is the one the scheduler's MCP answer reads:
    an expired workflow row parks the step, and after the re-store the same
    answer is runnable."""
    from swarm_common.models import Task
    from swarm_common.profiles import RUNNER_PROFILES
    from swarm_common.states import TaskState

    from scheduler.mcp_admission import McpRegistrations

    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"), 60)})
    doc = step_task(db, "a")
    task = Task(id=doc["id"], tenant_id="eng", created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc), state=TaskState.READY,
                runner_profile="claude-code", resource_class="standard",
                input=dict(doc["input"]), submitted_by=ALICE, workflow_id=workflow_id)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    answer = McpRegistrations(db).answer(task, RUNNER_PROFILES["claude-code"], now=later)
    assert not answer.runnable and not answer.terminal
    assert [r.why for r in answer.waits_for_credential] == ["expired"]

    assert put(client, workflow_id, {"kb": cred(fake_credential("n"), 3600)}).status_code == 200

    assert McpRegistrations(db).answer(task, RUNNER_PROFILES["claude-code"], now=later).runnable
    assert len(workflows(db)) == 1
    assert secrets.compare_digest(row(db, workflow_id)["owner_id"], workflow_id)


def test_the_park_detail_is_served_where_a_caller_reads_it(client, db, sm):
    """The contract for callers: `blocked_by[]` and `waiting.detail` on
    `GET /v1/tasks/{id}` both carry `reason`, `credential`, `code`, `server`
    and `expired_at`, and nothing about a value."""
    registered(db)
    workflow_id = submit(client, db, {"a": ["kb"]}, {"kb": cred(fake_credential("a"))})
    doc = step_task(db, "a")
    park_on_mcp(doc, "kb")

    response = client.get(f"/v1/tasks/{doc['id']}", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    served = response.json()["task"]
    [block] = served["blocked_by"]
    expected = {"reason": "CREDENTIAL_MISSING", "credential": "mcp",
                "code": "mcp_credential_expired", "server": "kb"}
    assert {k: block[k] for k in expected} == expected
    assert block["expired_at"] == "2026-10-26T20:00:00+00:00"
    waiting = served["waiting"]
    assert waiting["reason"] == "CREDENTIAL_MISSING"
    assert {k: waiting["detail"][k] for k in expected} == expected
    assert datetime.fromisoformat(waiting["detail"]["expired_at"].replace("Z", "+00:00")) \
        == datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
    assert waiting["detail"]["restore_route"] == \
        f"PUT /v1/workflows/{workflow_id}/mcp-credentials"
