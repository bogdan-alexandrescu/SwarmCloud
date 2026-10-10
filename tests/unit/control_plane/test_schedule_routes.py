"""The schedule routes: the tenant's own, and the admin list and actions (docs/schedules.md §7.1, lane S3).

WHAT IS HELD HERE
-----------------
* TENANT-SCOPED (§5.1, invariant 9). Another tenant's schedule id answers the
  same 404 as a missing one on EVERY route that takes an id -- the sweep
  below walks the router, so a route added tomorrow is in it -- and changes
  nothing. A firing and an audit entry are read only under their tenant.
* BY NAME (invariant 10). An unknown type is a 422 that names the available
  ones; `image`, `command`, `tenant_id` and `owner` are refused by name; a
  `params` key the type does not declare is refused by name.
* The create, edit, delete and state moves, each audited in its transaction
  (§4.8); the idempotent `client_request_id`; the per-tenant limit and name;
  `gate.merge: auto` refused to `PATCH` (`use_merge_switch`, the switch is
  S5's); a run-now submitted as the stored owner; the admin view and its
  pause, disable and enable, audited as `admin:<email>` (§5.3).
* §2.11's two switches (§7.1): pause-all, a member's over their own tenant
  only and an admin's over any, typed with the tenant id; and `:pause` with
  `cancel_live`, typed with the schedule's name, cancelling only that
  schedule's live work through the platform's own cancel.

No cloud and no emulator: the in-memory Firestore and the real app. No type's
executor is built yet (lanes S6, S10), so the tests mark the first types'
executor files present and hand the tick a fake executor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from swarm_api import schedulefire, schedules, scheduletypes
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context
from swarm_api.groups import StaticGroups
from swarm_api.auth import StaticTokenVerifier
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.routes import schedules as schedule_routes
from swarm_api.validation import SCHEDULE_METADATA_KEY
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header, seed_tenant
from .test_schedule_tick import FakeExecutor, seed_repo

NOW = datetime(2026, 10, 9, 9, 0, 30, tzinfo=timezone.utc)
REPO = "repo_00000000000000a1"
THEIR_REPO = "repo_00000000000000b2"
#: The first types' executor files, as S6 will add them.
BUILT = frozenset({"issue-sweep", "issue-plan-only", "repo-index-refresh", "observer"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def built_types(monkeypatch):
    monkeypatch.setattr(scheduletypes, "executor_present", lambda entry, root=None: entry.name in BUILT)
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)


@pytest.fixture
def executor(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(schedulefire, "load_executor", lambda entry: fake)
    return fake


def _context(db, tokens, group_map, objects, **settings: Any) -> AppContext:
    context = build_context(
        settings=api_settings(**settings),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    context.now = lambda: NOW
    return context


@pytest.fixture
def ctx(db, tokens, group_map, objects) -> AppContext:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_repo(db, REPO, "eng")
    seed_repo(db, THEIR_REPO, "research")
    return _context(db, tokens, group_map, objects)


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def body(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "nightly sweep",
        "type": "issue-sweep",
        "scope": {"mode": "repos", "repo_ids": [REPO]},
        "cron": "0 9 * * 1-5",
        "timezone": "Europe/London",
    }
    base.update(overrides)
    return base


def create(client, user: str = "alice", **overrides: Any) -> dict[str, Any]:
    response = client.post("/v1/schedules", json=body(**overrides), headers=auth_header(user))
    assert response.status_code == 201, response.text
    return response.json()["schedule"]


def audit(client, sid: str, user: str = "alice") -> list[dict[str, Any]]:
    response = client.get(f"/v1/schedules/{sid}/audit", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["audit"]


def at(ctx, moment: datetime) -> None:
    ctx.now = lambda: moment


def stored(db, sid: str) -> dict[str, Any] | None:
    return db.docs.get(f"schedules/{sid}")


def tasks(db) -> list[dict[str, Any]]:
    return [v for k, v in db.docs.items() if k.startswith("tasks/") and k.count("/") == 1]


# ---------------------------------------------------------------------------
# Create, list, read
# ---------------------------------------------------------------------------


def test_a_member_creates_a_schedule_in_their_own_tenant(client, db) -> None:
    doc = create(client)
    assert doc["schedule_id"].startswith("sch_")
    assert doc["tenant_id"] == "eng"
    assert doc["owner"] == doc["created_by"] == "alice@saga.xyz"
    assert doc["state"] == "enabled" and doc["revision"] == 1
    assert doc["next_run_at"] is not None
    assert doc["words"] and doc["tier"] in schedules.TIERS
    assert doc["spend_today"]["coverage"] == "complete"
    assert stored(db, doc["schedule_id"])["tenant_id"] == "eng"
    assert [e["action"] for e in audit(client, doc["schedule_id"])] == ["create"]

    listed = client.get("/v1/schedules", headers=auth_header("alice")).json()["schedules"]
    assert [row["schedule_id"] for row in listed] == [doc["schedule_id"]]
    one = client.get(f"/v1/schedules/{doc['schedule_id']}", headers=auth_header("alice")).json()
    assert one["schedule"]["name"] == "nightly sweep" and one["firings"] == []


def test_another_tenants_list_is_empty(client) -> None:
    create(client)
    listed = client.get("/v1/schedules", headers=auth_header("bob")).json()
    assert listed["schedules"] == [] and listed["tenant_id"] == "research"


def test_an_unknown_type_is_a_422_naming_the_available_ones(client) -> None:
    response = client.post("/v1/schedules", json=body(type="nope"), headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unknown_type"
    assert set(response.json()["detail"]["available"]) == BUILT


def test_a_type_not_built_yet_is_a_422_saying_so(client) -> None:
    response = client.post("/v1/schedules", json=body(type="epic-triage", cron="0 9 * * 1"),
                           headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "type_unavailable"
    assert "issue-sweep" in response.json()["detail"]["available"]


def test_a_params_key_the_type_does_not_declare_is_refused_by_name(client, db) -> None:
    response = client.post("/v1/schedules", json=body(params={"bogus_knob": 1}), headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_params"
    assert "bogus_knob" in [e["loc"] for e in response.json()["detail"]["errors"]]
    assert not [k for k in db.docs if k.startswith("schedules/")]


@pytest.mark.parametrize("field,value", [
    ("image", "ghcr.io/evil/image:latest"),
    ("command", ["sh", "-c", "id"]),
    ("runner_profile", "claude-code"),
    ("resources", {"cpu": "64"}),
    ("tenant_id", "research"),
    ("owner", "bob@saga.xyz"),
])
def test_a_caller_never_passes_an_image_a_command_or_a_tenant(client, db, field, value) -> None:
    response = client.post("/v1/schedules", json=body(**{field: value}), headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert field in [e["loc"] for e in response.json()["detail"]["errors"]]
    assert not [k for k in db.docs if k.startswith("schedules/")]


def test_another_tenants_repository_reads_as_unregistered(client) -> None:
    response = client.post("/v1/schedules", json=body(scope={"mode": "repos", "repo_ids": [THEIR_REPO]}),
                           headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "repository_not_registered"


def test_a_name_is_unique_within_the_tenant_only(client) -> None:
    create(client)
    again = client.post("/v1/schedules", json=body(name="Nightly Sweep"), headers=auth_header("alice"))
    assert again.status_code == 409 and again.json()["code"] == "name_taken"
    # The same name in another tenant is another schedule.
    create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})


def test_the_per_tenant_limit_holds_and_an_admin_raised_one_is_read(client, db) -> None:
    db.docs["schedule_tenants/eng"] = {"tenant_id": "eng", "limit": 1}
    create(client)
    over = client.post("/v1/schedules", json=body(name="second"), headers=auth_header("alice"))
    assert over.status_code == 422 and over.json()["code"] == "schedule_limit"
    assert over.json()["detail"]["limit"] == 1


def test_a_repeated_client_request_id_returns_the_first_schedule(client, ctx, db) -> None:
    first = client.post("/v1/schedules", json=body(client_request_id="form-1"), headers=auth_header("alice"))
    assert first.status_code == 201, first.text
    repeat = client.post("/v1/schedules", json=body(client_request_id="form-1"), headers=auth_header("alice"))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["created"] is False
    assert repeat.json()["schedule"]["schedule_id"] == first.json()["schedule"]["schedule_id"]
    assert len([k for k in db.docs if k.startswith("schedules/")]) == 1
    # After 24 hours the key is forgotten: the same body is a new create, and
    # meets the name check like any other.
    at(ctx, NOW + timedelta(hours=25))
    later = client.post("/v1/schedules", json=body(client_request_id="form-1"), headers=auth_header("alice"))
    assert later.status_code == 409 and later.json()["code"] == "name_taken"


def test_another_tenants_client_request_id_is_not_theirs(client) -> None:
    create(client, client_request_id="same")
    theirs = client.post("/v1/schedules", json=body(client_request_id="same",
                                                    scope={"mode": "repos", "repo_ids": [THEIR_REPO]}),
                         headers=auth_header("bob"))
    assert theirs.status_code == 201, theirs.text
    assert theirs.json()["schedule"]["tenant_id"] == "research"


def test_a_named_approver_must_be_a_member(client) -> None:
    response = client.post("/v1/schedules", json=body(gate={"approvers": ["bob@saga.xyz"]}),
                           headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "approver_not_member"
    ok = client.post("/v1/schedules", json=body(gate={"approvers": ["root@saga.xyz"]}),
                     headers=auth_header("alice"))
    assert ok.status_code == 201, ok.text


def test_a_pushing_type_needs_the_owners_write_grant_when_grants_are_enforced(db, tokens, group_map,
                                                                              objects) -> None:
    seed_tenant(db, "eng")
    seed_repo(db, REPO, "eng")
    client = TestClient(create_app(_context(db, tokens, group_map, objects, repository_grants_enforced=True)),
                        raise_server_exceptions=False)
    response = client.post("/v1/schedules", json=body(), headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "repository_not_granted"
    assert response.json()["detail"]["repo_ids"] == [REPO]
    # A type that does not push needs no grant.
    ok = client.post("/v1/schedules", json=body(type="repo-index-refresh", cron="0 3 * * *"),
                     headers=auth_header("alice"))
    assert ok.status_code == 201, ok.text


def test_before_the_workspace_is_ready_a_schedule_is_created_paused(ctx, client) -> None:
    ctx.submissions.workspaces.gate = True
    response = client.post("/v1/schedules", json=body(scope={"mode": "all"}), headers=auth_header("carol"))
    assert response.status_code == 201, response.text
    doc = response.json()["schedule"]
    assert doc["state"] == "paused" and doc["next_run_at"] is None
    assert doc["pause"]["code"] == "WORKSPACE_NOT_READY"


def test_a_member_cannot_create_an_owner_only_platform_scope(client) -> None:
    response = client.post("/v1/schedules", json=body(type="observer", cron="0 6 * * *",
                                                      scope={"mode": "platform"}),
                           headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "owner_required"


# ---------------------------------------------------------------------------
# The catalogue and the preview
# ---------------------------------------------------------------------------


def test_the_catalogue_lists_what_the_caller_may_create(client) -> None:
    types = {t["name"]: t for t in client.get("/v1/schedule-types", headers=auth_header("alice")).json()["types"]}
    assert set(types) == {entry.name for entry in scheduletypes.TYPES}
    assert types["issue-sweep"]["available"] is True and types["epic-triage"]["available"] is False
    assert types["observer"]["creatable_scopes"] == ["all", "repos"]
    assert types["cost-report"]["creatable_scopes"] == ["all", "repos"]
    admin = {t["name"]: t for t in client.get("/v1/schedule-types", headers=auth_header("root")).json()["types"]}
    assert admin["cost-report"]["creatable_scopes"] == ["all", "platform", "repos"]
    assert "platform" not in admin["observer"]["creatable_scopes"]


def test_the_preview_is_the_ticks_parser(client) -> None:
    response = client.post("/v1/schedules:preview", json={"cron": "0 9 * * 1-5", "timezone": "Europe/London",
                                                          "type": "issue-sweep"}, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    preview = response.json()["preview"]
    assert len(preview["next"]) == 5 and preview["words"] and preview["refusal"] is None
    fast = client.post("/v1/schedules:preview", json={"cron": "*/5 * * * *", "type": "issue-sweep"},
                       headers=auth_header("alice")).json()["preview"]
    assert fast["refusal"]["code"] == "interval_too_short"
    bad = client.post("/v1/schedules:preview", json={"cron": "61 * * * *"}, headers=auth_header("alice"))
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cron"


# ---------------------------------------------------------------------------
# Edit and delete
# ---------------------------------------------------------------------------


def test_an_edit_carries_its_revision_and_is_audited(client, ctx) -> None:
    sid = create(client)["schedule_id"]
    at(ctx, NOW + timedelta(minutes=1))
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "cron": "0 10 * * 1-5"},
                            headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["schedule"]["revision"] == 2
    entry = audit(client, sid)[0]
    assert entry["action"] == "edit" and entry["by"] == "alice@saga.xyz"
    assert entry["detail"]["changed"]["cron"] == {"from": "0 9 * * 1-5", "to": "0 10 * * 1-5"}
    stale = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "cron": "0 11 * * 1-5"},
                         headers=auth_header("alice"))
    assert stale.status_code == 409 and stale.json()["code"] == "schedule_changed"


def test_merge_auto_is_refused_to_an_edit_and_left_to_the_switch(client, db) -> None:
    sid = create(client)["schedule_id"]
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "gate": {"merge": "auto"}},
                            headers=auth_header("root"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "use_merge_switch"
    assert stored(db, sid)["gate"]["merge"] == "approve"


def test_a_gate_below_the_floor_is_refused_to_everyone(client) -> None:
    sid = create(client, type="issue-plan-only")["schedule_id"]
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "gate": {"plan": "auto"}},
                            headers=auth_header("root"))
    assert response.status_code == 422 and response.json()["code"] == "gate_below_floor"


def test_type_is_not_editable(client) -> None:
    sid = create(client)["schedule_id"]
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "type": "observer"},
                            headers=auth_header("alice"))
    assert response.status_code == 422 and "type" in [e["loc"] for e in response.json()["detail"]["errors"]]


def test_a_rename_meets_the_name_check(client) -> None:
    create(client)
    sid = create(client, name="other")["schedule_id"]
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "name": "NIGHTLY sweep"},
                            headers=auth_header("alice"))
    assert response.status_code == 409 and response.json()["code"] == "name_taken"


def test_delete_needs_the_typed_name_and_keeps_the_audit(client, db) -> None:
    sid = create(client)["schedule_id"]
    missing = client.delete(f"/v1/schedules/{sid}", headers=auth_header("alice"))
    assert missing.status_code == 422 and missing.json()["code"] == "confirmation_required"
    wrong = client.delete(f"/v1/schedules/{sid}", params={"confirm": "nightly"}, headers=auth_header("alice"))
    assert wrong.status_code == 422
    ok = client.delete(f"/v1/schedules/{sid}", params={"confirm": "nightly sweep"}, headers=auth_header("alice"))
    assert ok.status_code == 200, ok.text
    assert stored(db, sid) is None
    actions = sorted(v["action"] for k, v in db.docs.items() if k.startswith("schedule_audit/"))
    assert actions == ["create", "delete"]
    # The name is free again.
    create(client)


# ---------------------------------------------------------------------------
# State moves
# ---------------------------------------------------------------------------


def test_pause_and_resume_are_audited_newest_first(client, ctx, db) -> None:
    sid = create(client)["schedule_id"]
    at(ctx, NOW + timedelta(minutes=1))
    paused = client.post(f"/v1/schedules/{sid}:pause", json={"reason": "holiday"}, headers=auth_header("alice"))
    assert paused.status_code == 200, paused.text
    doc = stored(db, sid)
    assert doc["state"] == "paused" and doc["next_run_at"] is None
    assert doc["pause"]["reason"] == "holiday" and doc["pause"]["by"] == "alice@saga.xyz"
    at(ctx, NOW + timedelta(minutes=2))
    resumed = client.post(f"/v1/schedules/{sid}:resume", headers=auth_header("root"))
    assert resumed.status_code == 200, resumed.text
    doc = stored(db, sid)
    assert doc["state"] == "enabled" and doc["pause"] is None and doc["next_run_at"] > NOW
    assert [(e["action"], e["by"]) for e in audit(client, sid)] == [
        ("resume", "root@saga.xyz"), ("pause", "alice@saga.xyz"), ("create", "alice@saga.xyz"),
    ]


def test_resume_after_the_failure_stop_starts_the_count_again(client, db) -> None:
    sid = create(client)["schedule_id"]
    db.docs[f"schedules/{sid}"].update(state="auto_paused", consecutive_failures=3, next_run_at=None,
                                        pause={"by": "schedule-tick", "at": NOW, "reason": "x",
                                               "code": "CONSECUTIVE_FAILURES"})
    assert client.post(f"/v1/schedules/{sid}:resume", headers=auth_header("alice")).status_code == 200
    assert stored(db, sid)["consecutive_failures"] == 0 and stored(db, sid)["state"] == "enabled"


def test_take_ownership_moves_whom_firings_submit_as(client, ctx, db) -> None:
    sid = create(client)["schedule_id"]
    at(ctx, NOW + timedelta(minutes=1))
    response = client.post(f"/v1/schedules/{sid}:take-ownership", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert stored(db, sid)["owner"] == "root@saga.xyz"
    entry = audit(client, sid)[0]
    assert entry["action"] == "take_ownership"
    assert entry["detail"]["owner"] == {"from": "alice@saga.xyz", "to": "root@saga.xyz"}


def test_run_now_submits_as_the_stored_owner_and_is_in_the_history(client, ctx, db, executor) -> None:
    sid = create(client)["schedule_id"]
    at(ctx, NOW + timedelta(minutes=1))
    response = client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    firing = response.json()["firing"]
    assert firing["trigger"] == "run_now" and firing["state"] in ("created", "done")
    assert "finisher" not in firing and "recorded" not in firing
    made = tasks(db)
    assert len(made) == 1
    # As the schedule's owner, in its tenant -- never as the member who clicked.
    assert made[0]["submitted_by"] == "alice@saga.xyz" and made[0]["tenant_id"] == "eng"
    assert made[0]["metadata"][SCHEDULE_METADATA_KEY]["firing_id"] == firing["firing_id"]
    history = client.get(f"/v1/schedules/{sid}", headers=auth_header("alice")).json()["firings"]
    assert [f["firing_id"] for f in history] == [firing["firing_id"]]
    one = client.get(f"/v1/schedules/{sid}/firings/{firing['firing_id']}", headers=auth_header("alice"))
    assert one.status_code == 200 and one.json()["firing"]["firing_id"] == firing["firing_id"]
    assert audit(client, sid)[0]["action"] == "run_now"


def test_a_dry_run_creates_nothing(client, db, executor) -> None:
    sid = create(client)["schedule_id"]
    response = client.post(f"/v1/schedules/{sid}:run", json={"dry_run": True}, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    firing = response.json()["firing"]
    assert firing["skip"]["code"] == "DRY_RUN" and firing["dry_run"]["would_create"]
    assert executor.dry_runs == 1 and executor.calls == 0 and tasks(db) == []


def test_run_now_is_refused_while_schedules_are_switched_off(client, db, executor, monkeypatch) -> None:
    sid = create(client)["schedule_id"]
    monkeypatch.setenv("SCHEDULES_ENABLED", "false")
    response = client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header("alice"))
    assert response.status_code == 409 and response.json()["code"] == "schedules_disabled"
    assert executor.calls == 0


def test_a_run_body_takes_dry_run_and_nothing_else(client, executor) -> None:
    sid = create(client)["schedule_id"]
    response = client.post(f"/v1/schedules/{sid}:run", json={"image": "x"}, headers=auth_header("alice"))
    assert response.status_code == 422 and "image" in [e["loc"] for e in response.json()["detail"]["errors"]]
    assert executor.calls == 0


# ---------------------------------------------------------------------------
# Another tenant's id answers 404 on every route (§5.1, the S3 acceptance)
# ---------------------------------------------------------------------------


def _id_routes() -> list[tuple[str, str]]:
    found = []
    for route in schedule_routes.router.routes:
        if isinstance(route, APIRoute) and "{schedule_id}" in route.path and "/admin/" not in route.path:
            found.extend((method, route.path) for method in route.methods)
    return sorted(found)


ID_ROUTES = _id_routes()


def test_the_sweep_is_not_empty() -> None:
    # Every tenant id route of §7.1 this lane builds: read, edit, delete, the
    # four verbs, the history, one firing and the audit.
    assert len(ID_ROUTES) == 10, ID_ROUTES


def _send(client, method: str, path: str, sid: str, user: str):
    url = path.replace("{schedule_id}", sid).replace("{firing_id}", f"{sid}:run_now:0")
    if method in ("GET", "DELETE"):
        return client.request(method, url, headers=auth_header(user), params={"confirm": "nightly sweep"})
    payload = {"revision": 1, "name": "taken over"} if method == "PATCH" else {}
    return client.request(method, url, headers=auth_header(user), json=payload)


@pytest.mark.parametrize("method,path", ID_ROUTES, ids=[f"{m} {p}" for m, p in ID_ROUTES])
def test_another_tenants_schedule_is_a_404_on_every_route(client, db, executor, method, path) -> None:
    sid = create(client)["schedule_id"]
    before = dict(stored(db, sid))
    response = _send(client, method, path, sid, "bob")
    assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text[:200]}"
    # The same answer a missing id gets: never an oracle for another tenant.
    missing = _send(client, method, path, "sch_ffffffffffff", "bob")
    assert missing.status_code == 404
    assert response.json()["code"] == missing.json()["code"] == "not_found"
    assert response.json()["message"] == f"schedule {sid!r} not found"
    assert stored(db, sid) == before
    assert executor.calls == 0 and executor.dry_runs == 0
    assert tasks(db) == []


def test_another_tenants_firing_is_not_read_through_your_schedule(client, db, executor) -> None:
    theirs = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    firing = client.post(f"/v1/schedules/{theirs}:run", json={"dry_run": True},
                         headers=auth_header("bob")).json()["firing"]
    mine = create(client)["schedule_id"]
    # Their firing id under my schedule's id: not mine, so not found.
    response = client.get(f"/v1/schedules/{mine}/firings/{firing['firing_id']}", headers=auth_header("alice"))
    assert response.status_code == 404
    # A firing document planted under my schedule's id but stored for their
    # tenant is not served either.
    db.docs[f"schedule_firings/{mine}:1"] = {**db.docs[f"schedule_firings/{firing['firing_id']}"],
                                             "firing_id": f"{mine}:1", "schedule_id": mine}
    assert client.get(f"/v1/schedules/{mine}/firings/{mine}:1", headers=auth_header("alice")).status_code == 404
    assert client.get(f"/v1/schedules/{mine}/firings", headers=auth_header("alice")).json()["firings"] == []


# ---------------------------------------------------------------------------
# The admin view (§5.3)
# ---------------------------------------------------------------------------


def test_the_admin_list_covers_every_tenant_and_not_their_decisions(client, db) -> None:
    mine = create(client)["schedule_id"]
    theirs = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    minute = schedulefire.unix_minute(NOW)
    db.docs[f"schedule_ticks/{minute}"] = {"started_at": NOW, "fired": 2, "errors": 1, "truncated": False,
                                           "lateness": [{"firing_id": "f", "seconds": 4.5}]}
    response = client.get("/v1/admin/schedules", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    rows = {row["schedule_id"]: row for row in response.json()["schedules"]}
    assert set(rows) == {mine, theirs}
    assert rows[theirs]["tenant_id"] == "research" and rows[theirs]["words"]
    for row in rows.values():
        assert not {"params", "gate", "scope", "budget"} & set(row)
    tick = response.json()["tick"]
    assert tick["ticks"] == 1 and tick["errors"] == 1 and tick["max_lateness_seconds"] == 4.5
    one = client.get("/v1/admin/schedules", params={"tenant": "research"}, headers=auth_header("root"))
    assert [row["schedule_id"] for row in one.json()["schedules"]] == [theirs]


def test_the_admin_list_pages(client) -> None:
    for n in range(3):
        create(client, name=f"s{n}")
    first = client.get("/v1/admin/schedules", params={"limit": 2}, headers=auth_header("root")).json()
    assert len(first["schedules"]) == 2 and first["next_page_token"]
    rest = client.get("/v1/admin/schedules", params={"limit": 2, "page_token": first["next_page_token"]},
                      headers=auth_header("root")).json()
    assert len(rest["schedules"]) == 1 and rest["next_page_token"] is None


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/admin/schedules"),
    ("POST", "/v1/admin/schedules/{schedule_id}:pause"),
    ("POST", "/v1/admin/schedules/{schedule_id}:disable"),
    ("POST", "/v1/admin/schedules/{schedule_id}:enable"),
])
def test_the_admin_routes_refuse_a_member(client, db, method, path) -> None:
    sid = create(client)["schedule_id"]
    response = client.request(method, path.replace("{schedule_id}", sid), headers=auth_header("alice"), json={})
    assert response.status_code == 403, response.text
    assert stored(db, sid)["state"] == "enabled"


def test_an_admin_disables_and_only_an_admin_re_enables(client, ctx, db) -> None:
    sid = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    at(ctx, NOW + timedelta(minutes=1))
    disabled = client.post(f"/v1/admin/schedules/{sid}:disable", json={"reason": "runaway"},
                           headers=auth_header("root"))
    assert disabled.status_code == 200, disabled.text
    assert stored(db, sid)["state"] == "disabled" and stored(db, sid)["next_run_at"] is None
    refused = client.post(f"/v1/schedules/{sid}:resume", headers=auth_header("bob"))
    assert refused.status_code == 403 and refused.json()["code"] == "admin_required"
    assert client.post(f"/v1/schedules/{sid}:pause", headers=auth_header("bob")).status_code == 409
    at(ctx, NOW + timedelta(minutes=2))
    enabled = client.post(f"/v1/admin/schedules/{sid}:enable", headers=auth_header("root"))
    assert enabled.status_code == 200, enabled.text
    assert stored(db, sid)["state"] == "enabled" and stored(db, sid)["next_run_at"] is not None
    again = client.post(f"/v1/admin/schedules/{sid}:enable", headers=auth_header("root"))
    assert again.status_code == 409 and again.json()["code"] == "schedule_not_disabled"
    entries = audit(client, sid, "bob")
    assert [(e["action"], e["by"]) for e in entries[:2]] == [
        ("admin_enable", "admin:root@saga.xyz"), ("admin_disable", "admin:root@saga.xyz"),
    ]
    assert entries[1]["tenant_id"] == "research"


def test_an_admin_pause_is_the_tenants_to_lift(client, db) -> None:
    sid = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    assert client.post(f"/v1/admin/schedules/{sid}:pause", headers=auth_header("root")).status_code == 200
    assert stored(db, sid)["pause"]["by"] == "admin:root@saga.xyz"
    # An admin's enable does not override a pause...
    assert client.post(f"/v1/admin/schedules/{sid}:enable", headers=auth_header("root")).status_code == 409
    # ...and the tenant resumes it.
    assert client.post(f"/v1/schedules/{sid}:resume", headers=auth_header("bob")).status_code == 200


def test_an_admin_cannot_edit_another_tenants_schedule(client, db) -> None:
    sid = create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]
    response = client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "cron": "0 1 * * *"},
                            headers=auth_header("root"))
    assert response.status_code == 404
    assert stored(db, sid)["cron"] == "0 9 * * 1-5"


def test_an_admin_action_on_a_missing_id_is_a_404(client) -> None:
    response = client.post("/v1/admin/schedules/sch_ffffffffffff:disable", headers=auth_header("root"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# §2.11's switches: pause all, and pause and cancel live runs (§7.1)
# ---------------------------------------------------------------------------


def _theirs(client) -> str:
    return create(client, "bob", scope={"mode": "repos", "repo_ids": [THEIR_REPO]})["schedule_id"]


def _actions(db, sid: str) -> list[tuple[str, str]]:
    rows = [v for k, v in db.docs.items() if k.startswith("schedule_audit/") and v["schedule_id"] == sid]
    return sorted((r["action"], r["by"]) for r in rows)


def test_a_member_pauses_all_of_their_own_tenants_schedules_and_none_of_anothers(client, ctx, db) -> None:
    first = create(client, name="one")["schedule_id"]
    second = create(client, name="two")["schedule_id"]
    already = create(client, name="three")["schedule_id"]
    assert client.post(f"/v1/schedules/{already}:pause", headers=auth_header("alice")).status_code == 200
    theirs = _theirs(client)
    at(ctx, NOW + timedelta(minutes=1))
    response = client.post("/v1/schedules:pause-all", json={"confirm": "eng", "reason": "incident"},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": "eng", "paused": sorted([first, second]), "unchanged": [already]}
    for sid in (first, second):
        doc = stored(db, sid)
        assert doc["state"] == "paused" and doc["next_run_at"] is None
        assert doc["pause"]["reason"] == "incident" and doc["pause"]["by"] == "alice@saga.xyz"
        entry = audit(client, sid)[0]
        assert (entry["action"], entry["by"]) == ("pause_all", "alice@saga.xyz")
        assert entry["detail"]["reason"] == "incident"
    # The one already paused keeps its own pause and gets no entry.
    assert ("pause_all", "alice@saga.xyz") not in _actions(db, already)
    # Another tenant's is untouched, and alice cannot name it.
    assert stored(db, theirs)["state"] == "enabled" and _actions(db, theirs) == [("create", "bob@saga.xyz")]
    other = client.post("/v1/schedules:pause-all", json={"confirm": "research"}, headers=auth_header("alice"))
    assert other.status_code == 422
    assert stored(db, theirs)["state"] == "enabled"


@pytest.mark.parametrize("payload", [None, {}, {"confirm": ""}, {"confirm": "Eng"}, {"confirm": "eng-x"},
                                     {"confirm": "eng", "tenant_id": "research"}])
def test_pause_all_needs_the_tenant_id_typed(client, db, payload) -> None:
    sid = create(client)["schedule_id"]
    response = client.post("/v1/schedules:pause-all", json=payload, headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert stored(db, sid)["state"] == "enabled"
    assert _actions(db, sid) == [("create", "alice@saga.xyz")]


def test_an_admin_pauses_any_tenants_schedules_and_only_that_tenants(client, ctx, db) -> None:
    mine = create(client)["schedule_id"]
    theirs = _theirs(client)
    at(ctx, NOW + timedelta(minutes=1))
    response = client.post("/v1/admin/tenants/research/schedules:pause", json={"confirm": "research"},
                           headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["paused"] == [theirs]
    assert stored(db, theirs)["state"] == "paused"
    assert stored(db, theirs)["pause"]["by"] == "admin:root@saga.xyz"
    entry = audit(client, theirs, "bob")[0]
    assert (entry["action"], entry["by"], entry["tenant_id"]) == ("admin_pause_all", "admin:root@saga.xyz",
                                                                  "research")
    assert stored(db, mine)["state"] == "enabled"
    # The tenant may lift it.
    assert client.post(f"/v1/schedules/{theirs}:resume", headers=auth_header("bob")).status_code == 200


def test_the_admin_pause_all_is_typed_admin_only_and_names_a_real_tenant(client, db) -> None:
    theirs = _theirs(client)
    path = "/v1/admin/tenants/research/schedules:pause"
    member = client.post(path, json={"confirm": "research"}, headers=auth_header("bob"))
    assert member.status_code == 403, member.text
    for payload in (None, {"confirm": "eng"}, {"confirm": "researc"}):
        wrong = client.post(path, json=payload, headers=auth_header("root"))
        assert wrong.status_code == 422, wrong.text
    missing = client.post("/v1/admin/tenants/nobody/schedules:pause", json={"confirm": "nobody"},
                          headers=auth_header("root"))
    assert missing.status_code == 404
    assert stored(db, theirs)["state"] == "enabled"
    assert _actions(db, theirs) == [("create", "bob@saga.xyz")]


def _task_states(db) -> dict[str, str]:
    return {t["metadata"][SCHEDULE_METADATA_KEY]["schedule_id"]: t["state"] for t in tasks(db)}


def test_cancel_live_cancels_only_that_schedules_live_runs(client, ctx, db, executor) -> None:
    target = create(client, name="target")["schedule_id"]
    sibling = create(client, name="sibling")["schedule_id"]
    theirs = _theirs(client)
    for sid, user in ((target, "alice"), (sibling, "alice"), (theirs, "bob")):
        fired = client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header(user))
        assert fired.status_code == 200 and fired.json()["firing"]["state"] == "created", fired.text
    assert set(_task_states(db).values()) == {"READY"}
    at(ctx, NOW + timedelta(minutes=1))
    response = client.post(f"/v1/schedules/{target}:pause", json={"cancel_live": True, "confirm": "target"},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert stored(db, target)["state"] == "paused"
    assert _task_states(db) == {target: "CANCELLED", sibling: "READY", theirs: "READY"}
    [item] = response.json()["cancelled"]
    assert item["kind"] == "task" and item["result"] == "cancel_requested"
    # Both entries fall in one millisecond of the frozen clock, where the
    # audit's newest-first id breaks the tie at random: compare them as a set.
    entries = {e["action"]: e for e in audit(client, target)}
    assert set(entries) == {"create", "run_now", "pause", "cancel_live"}
    assert entries["cancel_live"]["by"] == "alice@saga.xyz" and entries["cancel_live"]["detail"]["items"] == [item]
    # The sibling is neither paused nor cancelled.
    assert stored(db, sibling)["state"] == "enabled"


def test_cancel_live_cancels_a_workflow_through_its_own_cancel(client, db, executor) -> None:
    executor.kind = "workflow"
    sid = create(client)["schedule_id"]
    assert client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header("alice")).status_code == 200
    response = client.post(f"/v1/schedules/{sid}:pause", json={"cancel_live": True, "confirm": "nightly sweep"},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert [i["kind"] for i in response.json()["cancelled"]] == ["workflow"]
    assert set(_task_states(db).values()) == {"CANCELLED"}
    workflows = [v for k, v in db.docs.items() if k.startswith("workflows/") and k.count("/") == 1]
    assert [w.get("cancel_requested") for w in workflows] == [True]


@pytest.mark.parametrize("payload", [{"cancel_live": True}, {"cancel_live": True, "confirm": "nightly"},
                                     {"cancel_live": True, "confirm": "Nightly Sweep"},
                                     {"cancel_live": "yes", "confirm": "nightly sweep"}])
def test_cancel_live_needs_the_schedules_name_typed(client, db, executor, payload) -> None:
    sid = create(client)["schedule_id"]
    assert client.post(f"/v1/schedules/{sid}:run", json={}, headers=auth_header("alice")).status_code == 200
    response = client.post(f"/v1/schedules/{sid}:pause", json=payload, headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert stored(db, sid)["state"] == "enabled"
    assert set(_task_states(db).values()) == {"READY"}
    # The control: the same switch, typed right, pauses and cancels.
    typed = client.post(f"/v1/schedules/{sid}:pause", json={"cancel_live": True, "confirm": "nightly sweep"},
                        headers=auth_header("alice"))
    assert typed.status_code == 200, typed.text
    assert stored(db, sid)["state"] == "paused" and set(_task_states(db).values()) == {"CANCELLED"}


def test_cancel_live_on_another_tenants_schedule_is_a_404_and_cancels_nothing(client, db, executor) -> None:
    theirs = _theirs(client)
    assert client.post(f"/v1/schedules/{theirs}:run", json={}, headers=auth_header("bob")).status_code == 200
    before = dict(stored(db, theirs))
    response = client.post(f"/v1/schedules/{theirs}:pause", json={"cancel_live": True, "confirm": "nightly sweep"},
                           headers=auth_header("alice"))
    missing = client.post("/v1/schedules/sch_ffffffffffff:pause",
                          json={"cancel_live": True, "confirm": "nightly sweep"}, headers=auth_header("alice"))
    assert response.status_code == missing.status_code == 404
    assert response.json()["code"] == missing.json()["code"] == "not_found"
    assert stored(db, theirs) == before
    assert set(_task_states(db).values()) == {"READY"}

