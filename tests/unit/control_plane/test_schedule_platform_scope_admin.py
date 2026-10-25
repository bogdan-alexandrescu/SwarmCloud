"""A platform-scope schedule is a platform admin's to change (PLATFORM-SCHED-ADMIN, part of #2157).

A `platform`-scope schedule reads every workspace's aggregates. Creating one,
or moving a schedule's scope TO platform, already needs a platform admin
(`schedules.check_creator`). BGD's decision of 2026-10-24, "Platform admins
only": every change to an EXISTING schedule whose STORED `scope.mode` is
`platform` needs one too -- PATCH, DELETE, `:pause` (with or without
`cancel_live`), `:resume`, `:run`, `:take-ownership` and the SD3
`:merge-mode` switch -- and `:pause-all` leaves such a schedule alone for
anyone else. Reads stay open; a workspace-scope schedule is managed as
before.

Who is who, in eng (`eng@example.com`):

* `alice` -- a member with no grant: `member`, the migration default;
* `wanda` -- the workspace's `admin` by grant; NOT a platform admin;
* `root` -- a platform admin who is also a member of eng;
* `ops` -- a platform admin in no tenant group, reaching eng only through
  `platform_admin_reach` (`X-Swarm-Tenant: eng`).

Each refusal is a 403 `platform_admin_required` that leaves every document as
it was -- schedule, audit, firings, tasks -- and is logged with who asked.
The branch is on the stored schedule, so a body naming another scope cannot
dodge it.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import refusals, roles, schedulefire, scheduletypes
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, NoForgeTokens, api_settings, seed_tenant
from .fakes import FakeFirestore
from .test_roles_gates_automate import LiveGroups
from .test_roles_store import grant
from .test_schedule_tick import NOW, FakeExecutor, seed_schedule

ALICE, WANDA = "alice@example.com", "wanda@example.com"
ROOT, OPS = "root@example.com", "ops@example.com"
GROUPS = {
    ALICE: (ENG_GROUP,),
    WANDA: (ENG_GROUP,),
    ROOT: (ADMIN_GROUP, ENG_GROUP),
    OPS: (ADMIN_GROUP,),
}
PLATFORM = "sch_00000000000a"
WORKSPACE = "sch_00000000000b"
#: A platform-scope type that merges, so `:merge-mode` has a gate to move.
MERGING = "sch_00000000000c"

#: The people a platform-scope schedule refuses, and the ones it does not.
REFUSED = ("alice", "wanda")
ADMINS = ("root", "ops")


@pytest.fixture(autouse=True)
def built_types(monkeypatch):
    monkeypatch.setattr(scheduletypes, "executor_present", lambda entry, root=None: True)
    for name in ("SCHEDULES_ENABLED", refusals.env_name("role_member_required"),
                 refusals.env_name("role_admin_required")):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def executor(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(schedulefire, "load_executor", lambda entry: fake)
    return fake


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def ctx(db):
    seed_tenant(db, "eng")
    tokens = {f"token-{e.split('@')[0]}": {"email": e, "email_verified": True,
                                           "sub": f"sub-{e}", "hd": "example.com"}
              for e in GROUPS}
    context = build_context(
        settings=api_settings(platform_admin_reach=True, settings_key=secrets.token_hex(32),
                              tenant_groups=(ENG_GROUP, RESEARCH_GROUP)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=LiveGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    context.now = lambda: NOW
    grant(context.roles, WANDA, roles.ADMIN)
    return context


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def seeded(db) -> FakeFirestore:
    """A platform-scope `cost-report` (an admin's to create), a platform-scope
    `failure-review` merging `auto` (the owner's), and a workspace-scope
    `issue-sweep`, all eng's and all owned by `wanda`, so a platform admin's
    `:take-ownership` is a change."""
    platform = seed_schedule(db, schedule_id=PLATFORM, owner=WANDA, type_="cost-report", repo_ids=())
    platform["scope"] = {"mode": "platform"}
    merging = seed_schedule(db, schedule_id=MERGING, owner=WANDA, type_="failure-review", repo_ids=(),
                            gate={"merge": "auto"})
    merging["scope"] = {"mode": "platform"}
    seed_schedule(db, schedule_id=WORKSPACE, owner=WANDA)
    return db


def h(user: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer token-{user}"}
    if user == "ops":
        headers["X-Swarm-Tenant"] = "eng"
    return headers


def state(db) -> dict[str, Any]:
    """Every document but the ones any authenticated request writes on sight,
    refused or not: `people/` (the roster) and a reaching platform admin's
    `platform_admin_acted` rows, which are written BEFORE the route runs."""
    return {k: v for k, v in db.dump().items()
            if not k.startswith("people/") and "platform_admin" not in k}


def writes(sid: str) -> list[tuple[str, str, str, Any]]:
    """Every write route on one schedule, with a body each route's model
    accepts, so a refusal is the scope's and never the body's."""
    return [
        ("edit", "PATCH", f"/v1/schedules/{sid}", {"revision": 1, "name": "renamed"}),
        ("delete", "DELETE", f"/v1/schedules/{sid}?confirm={sid}", None),
        ("pause", "POST", f"/v1/schedules/{sid}:pause", None),
        ("pause cancel_live", "POST", f"/v1/schedules/{sid}:pause", {"cancel_live": True, "confirm": sid}),
        ("resume", "POST", f"/v1/schedules/{sid}:resume", None),
        ("take-ownership", "POST", f"/v1/schedules/{sid}:take-ownership", None),
        ("run", "POST", f"/v1/schedules/{sid}:run", None),
    ]


#: The SD3 switch, on the schedule whose type merges: to `approve` is any
#: member's at once, so on a workspace-scope schedule nobody here is refused it.
MERGE_MODE = ("merge-mode", "POST", f"/v1/schedules/{MERGING}:merge-mode", {"mode": "approve", "revision": 1})


def _paused(db, sid: str) -> None:
    """`:resume` changes only a paused schedule: pause it first, so a 200 there is a change."""
    doc = db.docs[f"schedules/{sid}"]
    doc.update(state="paused", next_run_at=None, pause={"by": ROOT, "at": NOW, "reason": "paused", "code": None})


def send(client, db, sid: str, label: str, method: str, path: str, body: Any, user: str):
    if label == "resume":
        _paused(db, sid)
    kwargs: dict[str, Any] = {"headers": h(user)}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


def _ids(rows: list[tuple[str, str, str, Any]]) -> list[str]:
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# A platform-scope schedule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED)
@pytest.mark.parametrize("label,method,path,body", writes(PLATFORM) + [MERGE_MODE],
                         ids=_ids(writes(PLATFORM) + [MERGE_MODE]))
def test_a_workspace_admin_and_a_member_are_refused_every_write_to_a_platform_scope_schedule(
        client, seeded, executor, user, label, method, path, body) -> None:
    sid = MERGING if label == "merge-mode" else PLATFORM
    if label == "resume":
        _paused(seeded, sid)
    before = state(seeded)
    response = send(client, seeded, sid, label, method, path, body, user)
    assert response.status_code == 403, f"{label} by {user}: {response.text}"
    assert response.json()["code"] == "platform_admin_required"
    assert "platform admin" in response.json()["message"]
    # Nothing changed: not the schedule, no audit entry, no firing, no task.
    assert state(seeded) == before, f"{label} by {user} wrote something"
    assert executor.calls == 0 and executor.dry_runs == 0


@pytest.mark.parametrize("user", ADMINS)
@pytest.mark.parametrize("label,method,path,body", writes(PLATFORM) + [MERGE_MODE],
                         ids=_ids(writes(PLATFORM) + [MERGE_MODE]))
def test_a_platform_admin_makes_every_write_to_a_platform_scope_schedule(
        client, seeded, executor, user, label, method, path, body) -> None:
    sid = MERGING if label == "merge-mode" else PLATFORM
    before = seeded.docs[f"schedules/{sid}"]["revision"]
    response = send(client, seeded, sid, label, method, path, body, user)
    assert response.status_code == 200, f"{label} by {user}: {response.text}"
    after = seeded.docs.get(f"schedules/{sid}")
    if label == "delete":
        assert after is None
    elif label == "run":
        assert executor.calls == 1
    else:
        assert after["revision"] == before + 1, f"{label} by {user} changed nothing"
    if label == "merge-mode":
        assert after["gate"]["merge"] == "approve"


def test_the_refusal_is_logged_with_who_asked_and_what(client, seeded, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="swarm_api.routes.schedules"):
        response = client.post(f"/v1/schedules/{PLATFORM}:pause", headers=h("wanda"))
    assert response.status_code == 403, response.text
    lines = [r.getMessage() for r in caplog.records if "platform_admin_required" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert f"schedule={PLATFORM}" in lines[0] and f"email={WANDA}" in lines[0]
    assert "tenant=eng" in lines[0] and "action=pause" in lines[0]


@pytest.mark.parametrize("user", REFUSED)
def test_an_edit_naming_another_scope_does_not_dodge_the_stored_one(client, seeded, user) -> None:
    before = state(seeded)
    response = client.patch(f"/v1/schedules/{PLATFORM}", headers=h(user), json={
        "revision": 1, "scope": {"mode": "all"},
    })
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "platform_admin_required"
    assert state(seeded) == before


@pytest.mark.parametrize("user", REFUSED)
def test_reads_of_a_platform_scope_schedule_stay_open(client, seeded, user) -> None:
    for path in (f"/v1/schedules/{PLATFORM}", f"/v1/schedules/{PLATFORM}/audit",
                 f"/v1/schedules/{PLATFORM}/firings"):
        response = client.get(path, headers=h(user))
        assert response.status_code == 200, f"{path}: {response.text}"
    listed = client.get("/v1/schedules", headers=h(user)).json()["schedules"]
    assert PLATFORM in {row["schedule_id"] for row in listed}


@pytest.mark.parametrize("user", REFUSED)
def test_pause_all_leaves_a_platform_scope_schedule_to_a_platform_admin(client, seeded, user) -> None:
    response = client.post("/v1/schedules:pause-all", json={"confirm": "eng"}, headers=h(user))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["paused"] == [WORKSPACE]
    assert out["admin_required"] == sorted([PLATFORM, MERGING])
    assert out["unchanged"] == []
    assert seeded.docs[f"schedules/{PLATFORM}"]["state"] == "enabled"
    assert seeded.docs[f"schedules/{WORKSPACE}"]["state"] == "paused"


@pytest.mark.parametrize("user", ADMINS)
def test_a_platform_admins_pause_all_pauses_a_platform_scope_schedule_too(client, seeded, user) -> None:
    response = client.post("/v1/schedules:pause-all", json={"confirm": "eng"}, headers=h(user))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["paused"] == sorted([PLATFORM, MERGING, WORKSPACE]) and out["admin_required"] == []
    assert seeded.docs[f"schedules/{PLATFORM}"]["state"] == "paused"


# ---------------------------------------------------------------------------
# A workspace-scope schedule: as it was
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED + ADMINS)
@pytest.mark.parametrize("label,method,path,body", writes(WORKSPACE), ids=_ids(writes(WORKSPACE)))
def test_everyone_still_manages_a_workspace_scope_schedule(
        client, seeded, executor, user, label, method, path, body) -> None:
    response = send(client, seeded, WORKSPACE, label, method, path, body, user)
    assert response.status_code == 200, f"{label} by {user}: {response.text}"
