"""`swarm_api.wire_types` is every console shape the routes serve (part of #72).

The console's types are GENERATED from these models (`scripts/gen_api_types.py`
writes `apps/swarm-ui/src/generated/api-types.ts`, and
`tests/unit/scripts/test_api_types_drift.py` holds that file to the models). So
each model is the one place its wire shape is declared, and these tests hold it
to what the routes actually serve, in both directions:

  * every key a route serves is a field of the model -- a field the console
    cannot see is a feature nobody can reach;
  * every REQUIRED field of the model is in every served payload -- a required
    field nobody sends renders as an em dash forever.

`Task` first (with the summary listing), then the table that replaced the
regex reading of `types.ts` in `test_ui_api_field_contract.py`: the attempts,
leases, heartbeats, task page, stats, pools, runner profiles, quota, tenants,
`me`, dispatch switch, workflow, providers and events. One populated
deployment (`seeded`) serves every one of them; every ROW of a list is
checked, not only the first. The TypeScript side is generated, and `tsc` holds
the console's narrowing to it.

The models are attached as DOCUMENTED responses only (`responses=`, never
`response_model=`), so the tests at the end show each route still serves the
dict its codec function builds, unchanged.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter

from swarm_api import wire_types
from swarm_api.routes import (
    accounts,
    admin,
    leases,
    outcomes,
    platform,
    tasks,
    tenants,
    workflows,
)
from swarm_api.codec import (
    SUMMARY_DROPPED_KEYS,
    attempt_from_dict,
    attempt_to_api,
    lease_from_dict,
    lease_to_api,
    pool_from_dict,
    pool_to_api,
    quota_from_dict,
    quota_to_api,
    task_from_dict,
    task_to_api,
    tenant_from_dict,
    tenant_to_api,
)
from swarm_api.wire_types import Task, TaskEnvelope
from swarm_common.models import Attempt, Lease, ProviderState, QuotaState
from swarm_common.states import EventType, TaskState

from .conftest import auth_header, seed_pool, seed_task, seed_tenant

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
FIELDS = set(Task.model_fields)
REQUIRED = {name for name, field in Task.model_fields.items() if field.is_required()}


@pytest.fixture
def served(client, db):
    """One populated task as each route serves it: optional fields carry
    values, so "served" and "not served" are distinguishable."""
    seed_tenant(db, "eng", credentials=("anthropic",))
    task = seed_task(db, task_id="task_1", tenant_id="eng", state="RUNNING",
                     provider="anthropic", workflow_id="wf_1")
    task.update({
        "model": "claude-opus-4",
        "started_at": NOW - timedelta(minutes=5),
        "next_eligible_at": NOW + timedelta(minutes=5),
        "last_error": "dispatch_failed (attempt att_1)",
        "result_summary": {"artifacts": ["out.txt"], "exit_code": 0},
        "latest_checkpoint": "gs://swarm-artifacts-example-project/checkpoints/task_1/3",
        "repository_url": "https://github.com/example-org/example-repo",
        "repository_ref": "main",
        "metadata": {"title": "a task", "dispatch": {"strategy": "collect",
                                                      "carrier": "checkpoints"}},
        "blocked_by": [{"reason": "TENANT_LIMIT", "pool": "tenant:eng", "limit": 20, "active": 20}],
        "step_id": "s1",
        "current_lease_id": "lease_1",
        "current_generation": 3,
        "input": {"prompt": "do the thing"},
    })

    def get(path: str) -> dict:
        response = client.get(path, headers=auth_header("alice"))
        assert response.status_code == 200, response.text
        return response.json()

    def only(rows: list[dict], what: str) -> dict:
        assert rows, f"{what} served no task; the comparison would be vacuous"
        return rows[0]

    return {
        "GET /v1/tasks/{id}": get("/v1/tasks/task_1")["task"],
        "GET /v1/tasks": only(get("/v1/tasks?limit=50")["tasks"], "GET /v1/tasks"),
        "GET /v1/tasks?view=summary": only(
            get("/v1/tasks?limit=50&view=summary")["tasks"], "GET /v1/tasks?view=summary"),
        "task": task,
    }


def test_the_model_declares_every_key_a_task_route_serves(served):
    problems = [
        f"{route}: {sorted(set(row) - FIELDS)}"
        for route, row in served.items() if route != "task" and set(row) - FIELDS
    ]
    assert not problems, (
        "these routes serve task keys `wire_types.Task` does not declare, so the console's "
        f"generated type cannot see them: {problems}. Declare them there and run "
        "`uv run python scripts/gen_api_types.py`."
    )


def test_every_required_field_is_in_every_served_task(served):
    problems = [
        f"{route}: {sorted(REQUIRED - set(row))}"
        for route, row in served.items() if route != "task" and REQUIRED - set(row)
    ]
    assert not problems, (
        f"`wire_types.Task` requires fields these routes do not serve: {problems}. "
        "Make them optional there (a default), or serve them."
    )


def test_every_key_task_to_api_builds_is_declared(served):
    """The codec directly, both views, so a key reaches the model even before a
    route test seeds the data that makes it appear."""
    task = task_from_dict(served["task"])
    full = task_to_api(task)
    summary = task_to_api(task, summary=True)
    assert set(full) - FIELDS == set(), sorted(set(full) - FIELDS)
    assert set(summary) == set(full) - set(SUMMARY_DROPPED_KEYS)
    assert REQUIRED <= set(summary), sorted(REQUIRED - set(summary))


def test_the_trouble_board_fields_are_declared():
    """`types.ts` once declared 21 of `task_to_api`'s 31 keys, and the ten it
    omitted were the ones the remaining screens are built on."""
    for field in ("last_error", "result_summary", "latest_checkpoint", "next_eligible_at",
                  "metadata", "input", "model", "timeout_seconds", "repository_ref"):
        assert field in FIELDS, f"wire_types.Task does not declare {field}"


def test_the_route_documents_the_shape_without_filtering_what_it_serves(served):
    """`responses=` documents; `response_model=` would validate and drop every
    key the model lacks. The detail must still carry keys only a route adds."""
    detail = served["GET /v1/tasks/{id}"]
    assert "attempts_read" in detail and "links" in detail
    assert TaskEnvelope.model_fields["task"].annotation is Task


# --------------------------------------------------------------------------
# Every other shape: one deployment's worth of real data, served by the real
# routes (moved here from test_ui_api_field_contract.py, whose behaviour
# tests import it).
# --------------------------------------------------------------------------

@pytest.fixture
def seeded(client, db):
    """Every shape the console reads, in one populated deployment.

    Populated rather than minimal on purpose: a payload whose optional fields
    are all null cannot distinguish "the API serves this key" from "the API
    does not", which is the assertion half that was missing when five spend
    fields served null for months.
    """
    tenant = seed_tenant(db, "eng", credentials=("anthropic",))
    seed_tenant(db, "research")
    for name, limit, active in (
        ("global", 40, 12), ("resource:standard", 20, 4), ("runner:mock", 10, 1),
        ("backend:cloudrun", 30, 5), ("provider:anthropic", 6, 6),
        ("provider:anthropic:tenant:eng", 4, 4),
    ):
        seed_pool(db, name, hard_limit=limit, active=active)
    seed_pool(db, "resource:large", hard_limit=4, active=4, enabled=False)

    task = seed_task(db, task_id="task_1", tenant_id="eng", state="RUNNING",
                     provider="anthropic", workflow_id="wf_1")
    # Every optional field carries a value, so "served" and "not served" are
    # distinguishable for each of them.
    task.update(
        {
            "model": "claude-opus-4",
            "started_at": NOW - timedelta(minutes=5),
            "next_eligible_at": NOW + timedelta(minutes=5),
            "last_error": "dispatch_failed (attempt att_1)",
            "result_summary": {"artifacts": ["out.txt"], "exit_code": 0},
            "latest_checkpoint": "gs://swarm-artifacts-example-project/checkpoints/task_1/3",
            "repository_url": "https://github.com/example-org/agent-swarm-infra",
            "repository_ref": "main",
            "metadata": {"dispatch": {"strategy": "integrate", "carrier": "branches",
                                      "role": "integrator", "integrates": ["task_0"]}},
            "blocked_by": [{"reason": "TENANT_LIMIT", "pool": "tenant:eng",
                            "limit": 20, "active": 20}],
            "step_id": "s1",
            "current_lease_id": "lease_1",
            "current_generation": 3,
            "input": {"prompt": "do the thing"},
        }
    )

    db.docs["attempts/att_1"] = asdict(
        Attempt(
            attempt_id="att_1", task_id="task_1", tenant_id="eng", generation=3,
            lease_id="lease_1", backend="cloudrun", created_at=NOW - timedelta(minutes=6),
            execution_name="executions/task_1-3", started_at=NOW - timedelta(minutes=5),
            completed_at=NOW - timedelta(minutes=1), exit_code=0, error=None,
            peak_rss_bytes=6_400_000_000, peak_disk_bytes=1_200_000_000,
            oom_near_miss=True, checkpoints=["gs://swarm-artifacts-example-project/checkpoints/task_1/3"],
            input_tokens=12_345, output_tokens=678, cache_read_input_tokens=900,
            cache_creation_input_tokens=100, cost_usd=0.0642028,
        )
    )

    lease = Lease(
        lease_id="lease_1", task_id="task_1", attempt_id="att_1", tenant_id="eng",
        generation=3, pools=["global", "tenant:eng"], units=1, state=TaskState.DISPATCHED,
        created_at=NOW - timedelta(minutes=6), dispatch_deadline=NOW + timedelta(minutes=4),
        expires_at=NOW + timedelta(minutes=30), heartbeat_at=NOW - timedelta(seconds=30),
        released_at=None, release_reason=None,
    )
    doc = asdict(lease)
    doc["state"] = lease.state.value
    db.docs["leases/lease_1"] = doc

    quota = QuotaState(
        provider="anthropic", tenant_id="eng", state=ProviderState.THROTTLED,
        updated_at=NOW, configured_hard_max=50, adaptive_target=6,
        quota_derived_limit=8, requests_remaining=12, tokens_remaining=90_000,
        reset_at=NOW + timedelta(minutes=20), cooldown_until=None,
        last_429_at=NOW - timedelta(minutes=2), retry_after_seconds=30,
        success_count=140, rate_limit_count=3,
    )
    qdoc = asdict(quota)
    qdoc["state"] = quota.state.value
    db.docs["quota/anthropic:eng"] = qdoc

    db.docs["tasks/task_1/events/ev_1"] = {
        "event_id": "ev_1", "task_id": "task_1", "tenant_id": "eng",
        "type": next(iter(EventType)).value, "at": NOW - timedelta(minutes=6),
        "attempt_id": "att_1", "lease_id": "lease_1", "generation": 3,
        "detail": {"note": "admitted"},
    }

    db.docs["workflows/wf_1"] = {
        "workflow_id": "wf_1", "tenant_id": "eng", "created_at": NOW - timedelta(minutes=10),
        "updated_at": NOW, "state": TaskState.RUNNING.value, "submitted_by": "alice@example.com",
        "steps": [{"step_id": "s1", "runner_profile": "mock", "input": {"prompt": "a"},
                   "depends_on": [], "resource_class": "standard",
                   "input_from": {"s0": "out.txt"}, "timeout_seconds": 900,
                   "task_id": "task_1"}],
        "on_step_failure": "continue", "priority": 3, "cancel_requested": False,
    }

    return {"client": client, "tenant": tenant, "db": db}


def get_json(client, path: str, user: str = "root") -> dict[str, Any]:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def rows(items: list[Any], path: str) -> list[dict[str, Any]]:
    assert items, f"{path} served no rows; a field comparison against nothing passes vacuously"
    return items


def served_shapes(client) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Shape name -> every payload of that shape the seeded deployment serves,
    each labelled with where it came from."""
    capacity = get_json(client, "/v1/capacity", "alice")
    leases = get_json(client, "/v1/admin/leases")
    # The tenant-scoped heartbeat read (archived issue 179), as a member who is not an admin.
    heartbeats = get_json(client, "/v1/leases", "alice")
    attempts = get_json(client, "/v1/tasks/task_1/attempts", "alice")
    events = get_json(client, "/v1/tasks/task_1/events", "alice")
    tasks = get_json(client, "/v1/tasks?limit=200", "alice")
    workflow = get_json(client, "/v1/workflows/wf_1", "alice")
    providers = get_json(client, "/v1/providers", "alice")
    quota = get_json(client, "/v1/admin/quota")
    tenants = get_json(client, "/v1/admin/tenants")

    profiles = capacity["runner_profiles"]
    admissions = [(f"/v1/capacity .runner_profiles.{name}.admission", p["admission"])
                  for name, p in sorted(profiles.items()) if p.get("admission")]

    def each(items: list[Any], path: str) -> list[tuple[str, dict[str, Any]]]:
        return [(f"{path}[{i}]", row) for i, row in enumerate(rows(items, path))]

    return {
        "Capacity": [("/v1/capacity", capacity)],
        "Pool": each(capacity["pools"], "/v1/capacity .pools"),
        "RunnerProfile": [(f"/v1/capacity .runner_profiles.{name}", p)
                          for name, p in sorted(profiles.items())],
        "ProfileAdmission": admissions,
        "ProfileBlocker": [(f"{where}.blockers[{i}]", b) for where, a in admissions
                           for i, b in enumerate(a["blockers"])],
        "Counterfactual": [(f"{where}.counterfactual[{i}]", c) for where, a in admissions
                           for i, c in enumerate(a["counterfactual"])],
        "TaskPage": [("/v1/tasks", tasks)],
        "AttemptPage": [("/v1/tasks/{id}/attempts", attempts)],
        "AttemptRow": each(attempts["attempts"], "/v1/tasks/{id}/attempts .attempts"),
        "LeasePage": [("/v1/admin/leases", leases)],
        "LeaseRow": each(leases["leases"], "/v1/admin/leases .leases"),
        "LeaseHeartbeatPage": [("/v1/leases", heartbeats)],
        "LeaseHeartbeat": each(heartbeats["heartbeats"], "/v1/leases .heartbeats"),
        "QuotaPage": [("/v1/admin/quota", quota)],
        "QuotaState": each(quota["quota"], "/v1/admin/quota .quota") + [
            (f"/v1/providers .providers[{i}].quota", p["quota"])
            for i, p in enumerate(providers["providers"]) if p.get("quota")
        ],
        "TenantPage": [("/v1/admin/tenants", tenants)],
        "Tenant": each(tenants["tenants"], "/v1/admin/tenants .tenants"),
        "Stats": [("/v1/stats", get_json(client, "/v1/stats", "alice")),
                  ("/v1/stats?scope=platform (admin)",
                   get_json(client, "/v1/stats?scope=platform"))],
        "Me": [("/v1/tenants/me", get_json(client, "/v1/tenants/me", "alice"))],
        "DispatchControl": [("/v1/admin/dispatch", get_json(client, "/v1/admin/dispatch"))],
        "WorkflowEnvelope": [("/v1/workflows/{id}", workflow)],
        "Workflow": [("/v1/workflows/{id} .workflow", workflow["workflow"])],
        "ProvidersPage": [("/v1/providers", providers)],
        "ProviderEntry": each(providers["providers"], "/v1/providers .providers"),
        "TaskEventPage": [("/v1/tasks/{id}/events", events)],
        "TaskEvent": each(events["events"], "/v1/tasks/{id}/events .events"),
    }


#: Every shape of the table, and its model. A shape added to `served_shapes`
#: without a model here fails `test_every_served_shape_has_a_model`.
MODELS: dict[str, type[BaseModel]] = {
    name: getattr(wire_types, name) for name in (
        "Capacity", "Pool", "RunnerProfile", "ProfileAdmission", "ProfileBlocker",
        "Counterfactual", "TaskPage", "AttemptPage", "AttemptRow", "LeasePage", "LeaseRow",
        "LeaseHeartbeatPage", "LeaseHeartbeat", "QuotaPage", "QuotaState", "TenantPage",
        "Tenant", "Stats", "Me", "DispatchControl", "WorkflowEnvelope", "Workflow", "ProvidersPage",
        "ProviderEntry", "TaskEventPage", "TaskEvent",
    )
}


def undeclared_keys(model: type[BaseModel], payload: dict[str, Any]) -> list[str]:
    """Keys the payload carries that the model does not declare."""
    return sorted(set(payload) - set(model.model_fields))


def missing_required(model: type[BaseModel], payload: dict[str, Any]) -> list[str]:
    """Required model fields the payload does not carry."""
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    return sorted(required - set(payload))


@pytest.fixture
def shapes(seeded) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    return served_shapes(seeded["client"])


def test_every_served_shape_has_a_model_and_a_payload(shapes):
    assert set(shapes) == set(MODELS), sorted(set(shapes) ^ set(MODELS))
    empty = sorted(name for name, served in shapes.items() if not served)
    assert not empty, f"the seeded deployment serves no {empty}; their checks would be vacuous"


@pytest.mark.parametrize("name", sorted(MODELS))
def test_the_model_declares_every_key_its_routes_serve(shapes, name):
    problems = [f"{where}: {undeclared_keys(MODELS[name], payload)}"
                for where, payload in shapes[name] if undeclared_keys(MODELS[name], payload)]
    assert not problems, (
        f"these serve {name} keys `wire_types.{name}` does not declare, so the console's "
        f"generated type cannot see them: {problems}. Declare them there and run "
        "`uv run python scripts/gen_api_types.py`."
    )


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_required_field_is_in_every_served_payload(shapes, name):
    problems = [f"{where}: {missing_required(MODELS[name], payload)}"
                for where, payload in shapes[name] if missing_required(MODELS[name], payload)]
    assert not problems, (
        f"`wire_types.{name}` requires fields these do not serve: {problems}. Make them "
        "optional there (a default), or serve them."
    )


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_served_payload_validates_against_its_model(shapes, name):
    """The scalar types too: a count served as a string, or a timestamp as a
    number, would be a generated type that lies."""
    for where, payload in shapes[name]:
        try:
            MODELS[name].model_validate(payload)
        except Exception as exc:  # pydantic.ValidationError, named in the message
            raise AssertionError(f"{where} does not validate as wire_types.{name}: {exc}") from exc


def test_the_two_checks_fail_on_drift():
    """Both directions really fail: an extra served key, and a required field missing."""
    row = {name: None for name in wire_types.LeaseRow.model_fields}
    assert undeclared_keys(wire_types.LeaseRow, row) == []
    assert missing_required(wire_types.LeaseRow, row) == []
    assert undeclared_keys(wire_types.LeaseRow, {**row, "state": "DISPATCHED"}) == ["state"]
    del row["dispatch_state"]
    assert missing_required(wire_types.LeaseRow, row) == ["dispatch_state"]


# --------------------------------------------------------------------------
# Documented, never filtered
# --------------------------------------------------------------------------

#: Every route a model documents: (router module, method, path) -> the model of its 200.
DOCUMENTED: dict[tuple[Any, str, str], type[BaseModel]] = {
    (tasks, "GET", "/v1/tasks/{task_id}"): TaskEnvelope,
    (tasks, "GET", "/v1/tasks"): wire_types.TaskPage,
    (tasks, "GET", "/v1/tasks/{task_id}/attempts"): wire_types.AttemptPage,
    (tasks, "GET", "/v1/tasks/{task_id}/events"): wire_types.TaskEventPage,
    (admin, "GET", "/v1/admin/leases"): wire_types.LeasePage,
    (leases, "GET", "/v1/leases"): wire_types.LeaseHeartbeatPage,
    (admin, "GET", "/v1/admin/quota"): wire_types.QuotaPage,
    (admin, "GET", "/v1/admin/tenants"): wire_types.TenantPage,
    (admin, "GET", "/v1/admin/dispatch"): wire_types.DispatchControl,
    (platform, "GET", "/v1/stats"): wire_types.Stats,
    (platform, "GET", "/v1/capacity"): wire_types.Capacity,
    (platform, "GET", "/v1/providers"): wire_types.ProvidersPage,
    (tenants, "GET", "/v1/tenants/me"): wire_types.Me,
    (workflows, "GET", "/v1/workflows/{workflow_id}"): wire_types.WorkflowEnvelope,
    (outcomes, "GET", "/v1/outcomes"): wire_types.Outcomes,
    (platform, "GET", "/v1/runtimes"): wire_types.RuntimesPage,
    (accounts, "GET", "/v1/accounts"): wire_types.AccountsPage,
}


def test_each_documented_route_documents_its_model_and_validates_nothing(client):
    """`responses=` documents; `response_model=` would validate and drop every
    key the model lacks. Each route is documented in the served OpenAPI
    document, and none has a model as its response model (a `-> dict`
    annotation is a plain dict, which keeps every key)."""
    document = client.app.openapi()
    schemas = document["components"]["schemas"]
    problems = []
    for (module, method, path), model in DOCUMENTED.items():
        matches = [r for r in module.router.routes
                   if isinstance(r, APIRoute) and r.path == path and method in r.methods]
        assert len(matches) == 1, f"{module.__name__} has {len(matches)} routes {method} {path}"
        response_model = matches[0].response_model
        if isinstance(response_model, type) and issubclass(response_model, BaseModel):
            problems.append(f"{method} {path} has response_model={response_model.__name__}")
        content = document["paths"][path][method.lower()]["responses"]["200"]["content"]
        ref = content["application/json"]["schema"].get("$ref", "")
        if ref != f"#/components/schemas/{model.__name__}":
            problems.append(f"{method} {path} documents {ref or 'nothing'}, not {model.__name__}")
    assert not problems, problems
    for name in MODELS:
        assert name in schemas, f"wire_types.{name} is not in the OpenAPI document"


def _json(value: dict[str, Any]) -> dict[str, Any]:
    """As the route serialises a dict: pydantic's JSON mode."""
    return TypeAdapter(dict[str, Any]).dump_python(value, mode="json")


def test_the_codec_built_rows_are_served_unchanged(seeded, shapes):
    """Each documented route still serves the dict its codec function builds:
    same keys, same values, plus only the keys the route adds beside it."""
    db = seeded["db"]

    attempt = shapes["AttemptRow"][0][1]
    built = _json(attempt_to_api(attempt_from_dict(db.docs["attempts/att_1"])))
    assert attempt == built

    lease = shapes["LeaseRow"][0][1]
    built = _json(lease_to_api(lease_from_dict(db.docs["leases/lease_1"]),
                               task=task_from_dict(db.docs["tasks/task_1"])))
    # The admin route adds `silent_seconds` (against its own clock),
    # `heartbeat_ever` and the task's `last_error` beside the row.
    assert {k: lease[k] for k in built} == built
    assert set(lease) - set(built) == {"silent_seconds", "heartbeat_ever", "last_error"}

    for where, pool in shapes["Pool"]:
        built = _json(pool_to_api(pool_from_dict(pool["name"], db.docs[f"pools/{pool['name']}"])))
        assert {k: pool[k] for k in built} == built, where
        assert set(pool) - set(built) <= {"lowered_by"}, where

    quota = shapes["QuotaState"][0][1]
    built = _json(quota_to_api(quota_from_dict(db.docs["quota/anthropic:eng"])))
    assert {k: quota[k] for k in built} == built
    assert set(quota) - set(built) <= {"feeds_pool"}

    for where, tenant in shapes["Tenant"]:
        built = _json(tenant_to_api(tenant_from_dict(db.docs[f"tenants/{tenant['tenant_id']}"])))
        assert {k: tenant[k] for k in built} == built, where
        assert set(tenant) - set(built) <= {"workspace_kind", "workspace_id"}, where


# --------------------------------------------------------------------------
# The outcome ledger, the runtimes catalogue and the account listing
# (part of #72). These replace the readings of `outcomes.ts` and `types.ts`
# that test_outcomes_ui_field_contract.py, test_runtimes_screen.py and
# test_account_listing_is_honest.py made: each payload is walked against its
# model at EVERY depth, both directions, and every nested model must have
# been reached with a value at least once, so no part of a shape is checked
# only against `null` or `[]`.
# --------------------------------------------------------------------------


def _members(annotation: Any) -> list[Any]:
    """The non-None members of a union, or the annotation itself."""
    if get_origin(annotation) in (Union, UnionType):
        return [a for a in get_args(annotation) if a is not type(None)]
    return [annotation]


def _walk_value(annotation: Any, value: Any, where: str, seen: set[str]) -> list[str]:
    if value is None:
        return []
    members = _members(annotation)
    if len(members) > 1:
        tried = []
        for member in members:
            trial: set[str] = set()
            problems = _walk_value(member, value, where, trial)
            if not problems:
                seen |= trial
                return []
            tried.append(problems)
        models = [m for m in members if isinstance(m, type) and issubclass(m, BaseModel)]
        return min(tried, key=len) if models else []
    (annotation,) = members
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, dict):
            return [f"{where}: {value!r:.60} is not an object"]
        return walk(annotation, value, where, seen)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is list and args and isinstance(value, list):
        return [p for i, item in enumerate(value)
                for p in _walk_value(args[0], item, f"{where}[{i}]", seen)]
    if origin is dict and len(args) == 2 and isinstance(value, dict):
        return [p for key, item in value.items()
                for p in _walk_value(args[1], item, f"{where}.{key}", seen)]
    return []


def walk(model: type[BaseModel], payload: dict[str, Any], where: str,
         seen: set[str]) -> list[str]:
    """Every key the payload carries is declared, every required field is
    carried, at every depth the model describes. `seen` collects every model
    reached with a value."""
    seen.add(model.__name__)
    problems = [f"{where}.{key}: served and not declared by wire_types.{model.__name__}"
                for key in undeclared_keys(model, payload)]
    problems += [f"{where}.{key}: required by wire_types.{model.__name__} and not served"
                 for key in missing_required(model, payload)]
    for name, field in model.model_fields.items():
        if name in payload:
            problems += _walk_value(field.annotation, payload[name], f"{where}.{name}", seen)
    return problems


def nested_models(model: type[BaseModel], out: set[str] | None = None) -> set[str]:
    """Every model `model` reaches through its fields, itself included."""
    out = set() if out is None else out
    if model.__name__ in out:
        return out
    out.add(model.__name__)

    def visit(annotation: Any) -> None:
        for member in _members(annotation):
            if isinstance(member, type) and issubclass(member, BaseModel):
                nested_models(member, out)
            for arg in get_args(member):
                visit(arg)

    for field in model.model_fields.values():
        visit(field.annotation)
    return out


def test_the_walk_fails_on_drift_at_depth():
    """Both directions, one level down: a nested extra key and a nested
    missing field are each reported with their path."""
    bucket = {"start": "a", "end": "b", "state": "sealed", "in_progress": False,
              "unread_reason": None, "submitted": 1, "ended": 1, "succeeded": 1,
              "failed": 0, "dead_lettered": 0, "cancelled": None, "rate": None,
              "failure_classes": None, "cost": {"sum_usd": None, "attempts": 1}}
    problems = walk(wire_types.OutcomeBucket, bucket, "b", set())
    assert problems == ["b.cost.reporting: required by wire_types.CostCell and not served"]
    bucket["cost"] = {"sum_usd": None, "attempts": 1, "reporting": 0, "extra": 1}
    problems = walk(wire_types.OutcomeBucket, bucket, "b", set())
    assert problems == ["b.cost.extra: served and not declared by wire_types.CostCell"]


@pytest.fixture
def outcome_payloads(db, tokens, group_map) -> dict[str, dict[str, Any]]:
    """The outcome ledger as the Timeline asks for it: the fixture week of
    test_outcomes_route, through the real route, every way the page reads it
    (moved here from test_outcomes_ui_field_contract.py)."""
    from fastapi.testclient import TestClient

    from swarm_api import outcomes as module
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app

    from . import test_outcomes_route as route_tests

    clock = route_tests.Clock()
    route_tests.seed_week(db)
    ctx = route_tests._context(db, tokens, StaticGroups(group_map), clock)
    api = TestClient(create_app(ctx), raise_server_exceptions=False)
    week = dict(route_tests.WEEK)
    out = {
        "tenant": route_tests.ok(api, "alice", compare="previous", group="submitted_by", **week),
        "filtered": route_tests.ok(
            api, "alice", profile=["mock", "claude-code"], submitted_by="alice@example.com",
            kind="all", bucket="day", **week,
        ),
        "platform_excluding": route_tests.ok(
            api, "root", exclude_tenant="research", group="tenant_id", compare="previous", **week,
        ),
        "platform_including": route_tests.ok(api, "root", tenant=["research", "eng"], **week),
        "range": route_tests.ok(api, "alice", tz="UTC", since="2026-09-20", until="2026-09-24"),
        # kind=standalone: `workflows_failed` does not apply, and its counts are null.
        "standalone": route_tests.ok(api, "alice", kind="standalone", **week),
    }
    fresh = type(db)()
    route_tests.seed_week(fresh)
    starved = route_tests._context(fresh, tokens, StaticGroups(group_map), clock)
    starved.outcomes = module.Outcomes(
        store=starved.store, rollups=starved.rollups, metrics=starved.metrics, now=clock,
        derive_read_budget=1,
    )
    out["unread"] = route_tests.ok(
        TestClient(create_app(starved), raise_server_exceptions=False), "alice", **week,
    )
    assert out["unread"]["totals"]["complete"] is False, "the budget did not leave a bucket unread"
    return out


def test_every_outcomes_payload_is_exactly_the_model_at_every_depth(outcome_payloads):
    seen: set[str] = set()
    problems = [f"[{name}] {p}" for name, body in outcome_payloads.items()
                for p in walk(wire_types.Outcomes, body, "Outcomes", seen)]
    assert not problems, (
        "GET /v1/outcomes and wire_types.Outcomes disagree:\n  " + "\n  ".join(problems[:40])
    )
    unreached = sorted(nested_models(wire_types.Outcomes) - seen)
    assert not unreached, (
        f"these models were never reached with a value, so their shape is unproven: {unreached}"
    )
    for name, body in outcome_payloads.items():
        wire_types.Outcomes.model_validate(body)


def test_the_outcomes_unions_are_exactly_the_routes_values(outcome_payloads):
    """A value the route sends and the generated union lacks is drawn as
    nothing; one it never sends is dead code (the unions outcomes.ts
    switched on, now generated from these)."""
    from swarm_api import outcomes as module

    def values(enum: Any) -> set[str]:
        return {m.value for m in enum}

    assert values(wire_types.FailureClassKey) == set(module.FAILURE_KEYS)
    assert values(wire_types.CancelCauseKey) == set(module.CANCEL_KEYS)
    assert values(wire_types.LedgerBucketSize) == set(module.BUCKETS)
    assert values(wire_types.GroupBy) == set(module.GROUPS)
    assert values(wire_types.UnreadReason) == set(module._REASON_ORDER)
    assert values(wire_types.BucketState) == {"sealed", "open", "unread"}
    states = {b["state"] for body in outcome_payloads.values() for b in body["buckets"]}
    assert states <= values(wire_types.BucketState)


def test_the_outcomes_route_declares_every_parameter_the_timeline_sends(client):
    """The query `outcomesQuery` builds (contract.shapes.test.ts asserts the
    console sends exactly these) is one the route declares, in the OpenAPI
    document, with `tz` among them and no default the page relies on."""
    document = client.app.openapi()
    parameters = {p["name"]: p for p in document["paths"]["/v1/outcomes"]["get"]["parameters"]
                  if p["in"] == "query"}
    sent = {"tz", "span", "since", "until", "bucket", "scope", "tenant", "exclude_tenant",
            "profile", "submitted_by", "kind", "group", "compare", "section"}
    assert sent <= set(parameters), sorted(sent - set(parameters))
    # None of them is required by FastAPI: `tz` is refused by the route's own
    # validation (one 422 shape), so the page must always send it.
    assert not any(p.get("required") for p in parameters.values())


def test_a_missing_tz_is_refused(client):
    """Why the page always sends `tz`: the route requires it."""
    response = client.get("/v1/outcomes?span=7d", headers=auth_header("alice"))
    assert response.status_code == 422, response.text


@pytest.fixture
def runtimes_payload(client) -> dict[str, Any]:
    response = client.get("/v1/runtimes", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runtimes"], "the route served no runtimes; the comparison would be vacuous"
    return body


def test_the_runtimes_payload_is_exactly_the_model_at_every_depth(runtimes_payload):
    seen: set[str] = set()
    problems = walk(wire_types.RuntimesPage, runtimes_payload, "/v1/runtimes", seen)
    assert not problems, problems
    assert nested_models(wire_types.RuntimesPage) <= seen, sorted(
        nested_models(wire_types.RuntimesPage) - seen)
    wire_types.RuntimesPage.model_validate(runtimes_payload)


class _ListingPool:
    """The broker's real listing, as swarm-api's proxy reads it."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def list_accounts(self, tenant_id: str, include_reserved: bool = False) -> dict[str, Any]:
        return dict(self._payload)

    def holds(self, account_id: str) -> dict[str, Any]:
        return {"holds": []}


@pytest.fixture
def account_listing(api_context, broker) -> dict[str, Any]:
    """`GET /v1/accounts` as swarm-api serves it, rebuilt from what the real
    broker serves for a tenant with an account it reported unreadable, one
    lent account, and a document the store cannot parse."""
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import create_app as create_broker
    from swarm_api.main import create_app as create_api

    class _Identity:
        def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
            return "eng", False

    store = AccountStore(broker.db)
    store.register("eng", "mine")
    store.register("research", "lent", lend_to=["eng"])
    now = datetime.now(timezone.utc)
    broker.db.document("accounts/eng:mine").update({
        "unreadable_by": {"eng": now - timedelta(minutes=5)},
        "windows": {"five_hour": {"utilization": 0.4, "resets_at": now + timedelta(hours=2)}},
        "observed_at": now,
    })
    broker.db.document("accounts/eng:half-written").set(
        {"account_id": "eng:half-written", "owner_tenant": "eng"})
    served = TestClient(create_broker(broker, identity=_Identity(), account_store=store),
                        raise_server_exceptions=False).get("/v1/accounts")
    assert served.status_code == 200, served.text

    app = create_api(api_context)
    app.state.account_pool = _ListingPool(served.json())
    response = TestClient(app, raise_server_exceptions=False).get(
        "/v1/accounts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["accounts"]) == 2, body
    return body


def test_the_account_listing_is_exactly_the_model_at_every_depth(account_listing):
    seen: set[str] = set()
    problems = walk(wire_types.AccountsPage, account_listing, "/v1/accounts", seen)
    assert not problems, problems
    assert {"AccountsPage", "Account", "AccountWindow", "RefreshSweep"} <= seen
    wire_types.AccountsPage.model_validate(account_listing)
    assert any(a["unreadable_now"] for a in account_listing["accounts"]), (
        "no account carried a live unreadable report; `unreadable_now` is checked only empty")
    assert account_listing["unreadable_document_count"] == 1
