"""Who uses a managed plugin (docs/design/managed-plugins.md section 9.2: Remove shows its users).

    GET {base}/installs/{install_id}/usage

`workspaces`: who allows it and who runs it on every step, read from each
workspace's signed `plugins` block only. `workflows` and `schedules`: the
stored workflow specs (a `custom-prompt` schedule's `params.spec`) whose
steps name the install or get it from a default, and the schedules that run
them. `steps`: steps not finished whose dispatch block pins it. A platform
install's usage is a platform admin's, across every workspace; a workspace
install's is that workspace's members', and another workspace's member gets
404 as from the other install routes. Each list is capped and says
`truncated` when cut.

Offline: the catalogue routes' stand-ins, schedules and tasks written
straight into FakeFirestore.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import pluginstore as ps
from swarm_api.routes import plugin_config as rc

from .conftest import auth_header, seed_tenant
from .test_plugin_catalogue_routes import (  # noqa: F401  (world is a fixture)
    PLATFORM,
    _workspace,
    installed,
    world,
)


def _usage(w: dict[str, Any], install_id: str, *, base: str = PLATFORM, user: str = "root"):
    return w["client"].get(f"{base}/installs/{install_id}/usage", headers=auth_header(user))


def _allow(w: dict[str, Any], tenant_id: str, *names: str) -> None:
    response = w["client"].put(f"/v1/workspaces/{tenant_id}/plugins", headers=auth_header("root"),
                               json={"allowed": list(names)})
    assert response.status_code == 200, response.text


def _every_step(w: dict[str, Any], tenant_id: str, *names: str) -> None:
    response = w["client"].put(rc.DEFAULTS_ROUTE.format(tenant_id=tenant_id), headers=auth_header("root"),
                               json={"defaults": list(names)})
    assert response.status_code == 200, response.text


def _step(step_id: str, *, plugins: tuple[str, ...] = (), off: tuple[str, ...] = ()) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "runner_profile": "claude-code", "input": {}}
    if plugins:
        step["plugins"] = {name: {"overrides": {}} for name in plugins}
    if off:
        step["plugins_off"] = list(off)
    return step


def _schedule(w: dict[str, Any], schedule_id: str, tenant_id: str, name: str, steps: list[dict[str, Any]],
              *, kind: str = "custom-prompt") -> None:
    w["db"].docs[f"schedules/{schedule_id}"] = {
        "schedule_id": schedule_id, "tenant_id": tenant_id, "name": name, "type": kind, "state": "enabled",
        "params": {"spec": {"steps": steps}} if kind == "custom-prompt" else {}}


def _task(w: dict[str, Any], task_id: str, tenant_id: str, state: str, pins: list[str], *,
          workflow_id: str | None = "wf-1", step_id: str | None = "review") -> None:
    dispatch: dict[str, Any] = {"plugins": {name: {"version_id": "v-000000000001"} for name in pins}} if pins else {}
    w["db"].docs[f"tasks/{task_id}"] = {
        "task_id": task_id, "tenant_id": tenant_id, "state": state, "workflow_id": workflow_id,
        "step_id": step_id, "metadata": {"dispatch": dispatch}}


@pytest.fixture
def uw(world):
    seed_tenant(world["db"], "research")
    seed_tenant(world["db"], "ops")
    return world


# --- each list ----------------------------------------------------------------------


def test_a_platform_installs_workspaces_are_those_that_allow_it_or_run_it_on_every_step(uw):
    plugin = installed(uw)["install_id"]
    other = installed(uw, name="fmt-kit")["install_id"]
    _allow(uw, "eng", plugin)
    _every_step(uw, "eng", plugin)
    _allow(uw, "research", plugin, other)
    _allow(uw, "ops", other)
    # A block nobody signed names it: nobody's choice, so not a user.
    uw["db"].docs["workspace_settings/forged"] = {"plugins": {"allowed": [plugin], "defaults": [plugin]}}

    response = _usage(uw, plugin)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspaces"] == [
        {"tenant_id": "eng", "allowed": True, "every_step": True},
        {"tenant_id": "research", "allowed": True, "every_step": False},
    ]
    assert body["workflows"] == [] and body["schedules"] == [] and body["steps"] == []
    assert body["truncated"] is False


def test_stored_workflow_specs_and_their_schedules_name_it_or_get_it_from_a_default(uw):
    plugin = installed(uw)["install_id"]
    other = installed(uw, name="fmt-kit")["install_id"]
    _allow(uw, "eng", plugin)
    _every_step(uw, "eng", plugin)
    _allow(uw, "research", plugin, other)
    # eng: one step names it, one gets it from the default, one turns it off.
    _schedule(uw, "sch_a", "eng", "nightly", [_step("review", plugins=(plugin,)), _step("verify"),
                                               _step("publish", off=(plugin,))])
    # eng: no step names it, every step but one gets it from the default.
    _schedule(uw, "sch_b", "eng", "weekly", [_step("scan"), _step("quiet", off=(plugin,))])
    # research has no default: a step that does not name it does not get it.
    _schedule(uw, "sch_c", "research", "triage", [_step("sort", plugins=(other,))])
    _schedule(uw, "sch_d", "research", "lint", [_step("lint", plugins=(plugin, other))])
    # A schedule type with no stored spec names nothing.
    _schedule(uw, "sch_e", "eng", "sweep", [], kind="branch-sweep")

    body = _usage(uw, plugin).json()

    assert body["workflows"] == [
        {"tenant_id": "eng", "id": "sch_a", "name": "nightly", "kind": "schedule-template",
         "steps": ["review", "verify"], "how": "added"},
        {"tenant_id": "eng", "id": "sch_b", "name": "weekly", "kind": "schedule-template",
         "steps": ["scan"], "how": "default"},
        {"tenant_id": "research", "id": "sch_d", "name": "lint", "kind": "schedule-template",
         "steps": ["lint"], "how": "added"},
    ]
    assert body["schedules"] == [
        {"tenant_id": "eng", "schedule_id": "sch_a", "name": "nightly"},
        {"tenant_id": "eng", "schedule_id": "sch_b", "name": "weekly"},
        {"tenant_id": "research", "schedule_id": "sch_d", "name": "lint"},
    ]


def test_steps_are_the_unfinished_ones_whose_dispatch_block_pins_it(uw):
    plugin = installed(uw)["install_id"]
    other = installed(uw, name="fmt-kit")["install_id"]
    _task(uw, "t-running", "eng", "RUNNING", [plugin, other])
    _task(uw, "t-queued", "research", "QUEUED", [plugin], workflow_id=None, step_id=None)
    _task(uw, "t-done", "eng", "SUCCEEDED", [plugin])
    _task(uw, "t-cancelled", "eng", "CANCELLED", [plugin])
    _task(uw, "t-other", "eng", "RUNNING", [other])
    _task(uw, "t-none", "eng", "LEASED", [])

    body = _usage(uw, plugin).json()

    assert body["steps"] == [
        {"tenant_id": "eng", "task_id": "t-running", "workflow_id": "wf-1", "step": "review", "state": "RUNNING"},
        {"tenant_id": "research", "task_id": "t-queued", "workflow_id": None, "step": None, "state": "QUEUED"},
    ]


def test_a_workspace_installs_usage_is_its_own_workspaces_only(uw):
    plugin = installed(uw, base=_workspace("eng"))["install_id"]
    _every_step(uw, "eng", plugin)
    _schedule(uw, "sch_a", "eng", "nightly", [_step("review")])
    _task(uw, "t-eng", "eng", "RUNNING", [plugin])
    # Another workspace's documents naming the same id are not this install's users.
    _schedule(uw, "sch_x", "research", "elsewhere", [_step("review", plugins=(plugin,))])
    _task(uw, "t-research", "research", "RUNNING", [plugin])

    response = _usage(uw, plugin, base=_workspace("eng"), user="alice")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspaces"] == [{"tenant_id": "eng", "allowed": True, "every_step": True}]
    assert [(r["id"], r["how"]) for r in body["workflows"]] == [("sch_a", "default")]
    assert [r["schedule_id"] for r in body["schedules"]] == ["sch_a"]
    assert [r["task_id"] for r in body["steps"]] == ["t-eng"]


def test_a_removed_install_still_shows_the_steps_that_keep_their_copy(uw):
    plugin = installed(uw)["install_id"]
    _task(uw, "t-running", "eng", "RUNNING", [plugin])
    removed = uw["client"].delete(f"{PLATFORM}/installs/{plugin}?confirm=lint-kit", headers=auth_header("root"))
    assert removed.status_code == 200, removed.text

    body = _usage(uw, plugin).json()

    assert [r["task_id"] for r in body["steps"]] == ["t-running"]


# --- who may read it ------------------------------------------------------------------


def test_a_platform_installs_usage_is_a_platform_admins(uw):
    plugin = installed(uw)["install_id"]
    response = _usage(uw, plugin, user="alice")
    assert response.status_code in (401, 403), response.text


def test_another_workspaces_member_does_not_read_a_workspace_installs_usage(uw):
    plugin = installed(uw, base=_workspace("eng"))["install_id"]
    assert _usage(uw, plugin, base=_workspace("eng"), user="alice").status_code == 200
    response = _usage(uw, plugin, base=_workspace("eng"), user="bob")
    assert response.status_code == 404, response.text


def test_a_workspace_install_is_not_found_through_another_scope(uw):
    plugin = installed(uw, base=_workspace("eng"))["install_id"]
    assert _usage(uw, plugin, base=_workspace("eng")).status_code == 200
    assert _usage(uw, plugin).status_code == 404
    assert _usage(uw, plugin, base=_workspace("research"), user="root").status_code == 404
    platform = installed(uw, name="fmt-kit")["install_id"]
    assert _usage(uw, platform, base=_workspace("eng"), user="alice").status_code == 404


def test_an_unknown_or_built_in_install_has_no_usage(uw):
    assert _usage(uw, installed(uw)["install_id"]).status_code == 200
    assert _usage(uw, "p-0123456789ab").status_code == 404
    assert _usage(uw, "builtin:gstack").status_code in (404, 422)


# --- bounded reads ----------------------------------------------------------------------


def test_each_list_is_cut_at_the_cap_and_says_truncated(uw, monkeypatch):
    plugin = installed(uw)["install_id"]
    monkeypatch.setattr(ps, "USAGE_CAP", 2)
    for n in range(3):
        _task(uw, f"t-{n}", "eng", "RUNNING", [plugin])

    body = _usage(uw, plugin).json()

    assert [r["task_id"] for r in body["steps"]] == ["t-0", "t-1"]
    assert body["truncated"] is True


def test_a_read_that_stops_at_the_scan_bound_says_truncated(uw, monkeypatch):
    plugin = installed(uw)["install_id"]
    monkeypatch.setattr(ps, "USAGE_SCAN", 2)
    for n in range(3):
        _task(uw, f"t-{n}", "eng", "RUNNING", [] if n else [plugin])

    body = _usage(uw, plugin).json()

    assert len(body["steps"]) <= 1
    assert body["truncated"] is True
