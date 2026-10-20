"""swarm-api writes each workspace record's Firestore switch (#1457, design B).

The workspace job grants a job-made workspace's worker the database role only
when the record's `worker_firestore_access` says so (absent is true, today's
grant): scripts/register-tenant.sh reads it from the guard's expectation, which
copies it from the record (tests/unit/scripts/
test_register_tenant_workspace_firestore.py). swarm-api is what writes it, from
the deployment's cutover list WORKER_FIRESTORE_OFF_TENANTS (owner decision Q7:
dev first -- u-bogdan, then eng, then all), on every admin action that starts a
run for the record: an approval, a retry and a ceiling change.

Over the shipped app and an in-memory Firestore, as test_people_admin.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api import workspaces as ws
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header
from .test_people_admin import (
    CAROL_WS,
    ROOT,
    FakePool,
    FakePublisher,
    _now,
    _personal_tenant,
    _post,
    _rec,
    _record,
)


def _client(db, tokens, group_map, objects, off: tuple[str, ...]) -> TestClient:
    context = build_context(
        settings=api_settings(worker_firestore_off_tenants=off),
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
    app.state.account_pool = FakePool()
    return TestClient(app, raise_server_exceptions=False)


CASES = {
    "not-listed": ((), True),
    "another-tenant": (("u-bogdan",), True),
    "listed": (("u-bogdan", "u-carol"), False),
    "everyone": (("*",), False),
}


@pytest.mark.parametrize("off,granted", CASES.values(), ids=CASES.keys())
def test_an_approval_writes_the_records_switch(db, tokens, group_map, objects, off, granted) -> None:
    client = _client(db, tokens, group_map, objects, off)
    _record(db)
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/approve")
    assert response.status_code == 200, response.text
    # MUTATION: leave the field off the approval's patch (or write it from
    # anything but the cutover list) and this goes red.
    assert _rec(db)[ws.WORKER_FIRESTORE_ACCESS] is granted


@pytest.mark.parametrize("off,granted", CASES.values(), ids=CASES.keys())
def test_a_retry_writes_the_records_switch(db, tokens, group_map, objects, off, granted) -> None:
    client = _client(db, tokens, group_map, objects, off)
    _record(db, state="failed", decision={"by": ROOT, "at": _now(), "verdict": "approved", "reason": None},
            **{ws.WORKER_FIRESTORE_ACCESS: not granted})
    response = _post(client, f"/v1/admin/workspaces/{CAROL_WS}/retry")
    assert response.status_code == 200, response.text
    # MUTATION: leave the field off the retry's patch and the stored opposite
    # survives: red.
    assert _rec(db)[ws.WORKER_FIRESTORE_ACCESS] is granted


@pytest.mark.parametrize("off,granted", CASES.values(), ids=CASES.keys())
def test_a_ceiling_change_writes_the_records_switch(db, tokens, group_map, objects, off, granted) -> None:
    client = _client(db, tokens, group_map, objects, off)
    _record(db, state="ready", **{ws.WORKER_FIRESTORE_ACCESS: not granted})
    _personal_tenant(db)
    response = client.put(f"/v1/admin/workspaces/{CAROL_WS}/limits",
                          headers=auth_header("root"), json={"max_active": 6})
    assert response.status_code == 200, response.text
    # MUTATION: write only `limits` in set_ceiling and a ready workspace never
    # learns its tenant was cut over: red.
    assert _rec(db)[ws.WORKER_FIRESTORE_ACCESS] is granted


def test_the_switch_is_true_unless_the_tenant_is_listed() -> None:
    # MUTATION: invert the default (an empty list switching everyone off) and
    # every existing workspace loses its grant on its next run: red.
    assert ws.worker_firestore_access("u-carol", ()) is True
    assert ws.worker_firestore_access("u-carol", ("eng",)) is True
    assert ws.worker_firestore_access("u-carol", ("eng", "u-carol")) is False
    assert ws.worker_firestore_access("u-carol", ("*",)) is False


def test_the_cutover_list_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("WORKER_FIRESTORE_OFF_TENANTS", " u-bogdan, eng ,")
    # MUTATION: read another variable (or none) and this goes red.
    assert ApiSettings.from_env().worker_firestore_off_tenants == ("u-bogdan", "eng")
    monkeypatch.delenv("WORKER_FIRESTORE_OFF_TENANTS")
    assert ApiSettings.from_env().worker_firestore_off_tenants == ()
