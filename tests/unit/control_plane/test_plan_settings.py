"""The plan-review settings (docs/design/plans-pipeline.md §6.4, §7.8; lane P9).

What these tests hold:

  * DEFAULTS. With nothing set, diagrams are on from `platform`, the chat
    model is `sonnet` from `platform` and the caps are 5 / 20 / 40 / 25.00.
  * THE PLATFORM LEVEL. A platform admin's `PUT /v1/admin/settings` changes
    each; a workspace admin's is 403. A stored cap out of its range or of the
    wrong type reads as its default; a class `claude-code` does not carry is
    refused on save.
  * THE WORKSPACE LEVEL. `PUT /v1/workspaces/{tid}/plan-settings` is its
    admin's (403 for a member, 404 outside it), audited in the same
    transaction, signed once the contract lists the block, and a no-op writes
    nothing; it refuses a `caps` key, naming it as the platform's, and a class
    `claude-code` does not carry. Any member reads the caps.
  * THE RESOLUTION. The schedule's override wins over the workspace's and the
    workspace's over the platform's; `inherit` reports the level it inherited
    from.

No credentials, no network: the in-memory Firestore and the real app.
"""

from __future__ import annotations

import dataclasses
import secrets
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import plansettings, store as store_module
from swarm_api.judgment import SettingsMac, block_verifies
from swarm_api.main import create_app
from swarm_api.roles import RoleStore
from swarm_common.identity import tenant_id_for_user
from swarm_common.specsign import SETTINGS_MAC_FIELDS

from .conftest import auth_header, seed_tenant
from .test_schedule_routes import BUILT, REPO, _context, body as schedule_body
from .test_schedule_tick import seed_repo

CAROL = "carol@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/plan-settings"


def _get(client, tenant_id: str, user: str):
    return client.get(_url(tenant_id), headers=auth_header(user))


def _put(client, tenant_id: str, user: str, body: dict[str, Any]):
    return client.put(_url(tenant_id), headers=auth_header(user), json=body)


def _admin_put(client, user: str, body: dict[str, Any]):
    return client.put("/v1/admin/settings", headers=auth_header(user), json=body)


def _audit(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == plansettings.AUDIT_ACTION]


def _name_admin(client, api_context, who: str = "alice@example.com", tenant: str = "eng") -> None:
    # A role is signed with the settings key, so the app needs one to name an admin.
    api_context.settings = dataclasses.replace(api_context.settings,
                                               settings_key=secrets.token_hex(32))
    api_context.roles = RoleStore(api_context.db, api_context.settings,
                                  metrics=api_context.metrics, now=api_context.now)
    response = client.put(f"/v1/admin/workspaces/{tenant}/roles/person", headers=auth_header("root"),
                          json={"email": who, "role": "admin"})
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------

def test_with_nothing_set_diagrams_are_on_sonnet_answers_and_the_caps_are_the_owners(
        client, seeded):
    response = _get(client, "eng", "alice")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"]["diagrams"] == {"value": True, "from": "platform"}
    assert body["effective"]["chat_model_class"] == {
        "value": "sonnet", "from": "platform", "fell_through": None}
    caps = body["caps"]
    assert (caps["replans"], caps["diagram_asks"], caps["chat_turns"], caps["review_usd"]) \
        == (5, 20, 40, 25.00)
    assert caps["from"] == "platform" and caps["set_by"] is None
    assert body["plans"] == {"diagrams": None, "chat_model_class": None}
    # A member who is not its admin reads them, and may not change them.
    assert body["can_edit"] is False


def test_the_platform_keys_are_served_with_their_defaults(client, seeded):
    settings = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert settings["plan_diagrams_default"] is True
    assert settings["plan_chat_model_class"] == "sonnet"
    assert {k: settings["plan_review_caps"][k] for k in plansettings.CAP_KEYS} \
        == {"replans": 5, "diagram_asks": 20, "chat_turns": 40, "review_usd": 25.00}


def test_the_store_defaults_and_the_modules_agree():
    defaults = store_module.PLATFORM_SETTINGS_DEFAULTS
    assert defaults[plansettings.DIAGRAMS_SETTING] is plansettings.DIAGRAMS_DEFAULT
    assert defaults[plansettings.CHAT_MODEL_SETTING] == plansettings.CHAT_MODEL_DEFAULT
    assert defaults[plansettings.CAPS_SETTING] == plansettings.CAPS_DEFAULT


# --------------------------------------------------------------------------
# the platform level
# --------------------------------------------------------------------------

def test_a_platform_admin_changes_each_platform_setting(client, db, seeded):
    response = _admin_put(client, "root", {
        "plan_diagrams_default": False, "plan_chat_model_class": "haiku",
        "plan_review_caps": {"replans": 0, "review_usd": 12.5}})
    assert response.status_code == 200, response.text
    body = _get(client, "eng", "alice").json()
    assert body["effective"]["diagrams"] == {"value": False, "from": "platform"}
    assert body["effective"]["chat_model_class"]["value"] == "haiku"
    caps = body["caps"]
    # The caps named replaced theirs; the others kept their figure.
    assert (caps["replans"], caps["diagram_asks"], caps["chat_turns"], caps["review_usd"]) \
        == (0, 20, 40, 12.5)
    assert caps["set_by"] == "root@example.com" and caps["set_at"]
    # A later change of one cap keeps the earlier one.
    assert _admin_put(client, "root", {"plan_review_caps": {"chat_turns": 7}}).status_code == 200
    caps = _get(client, "eng", "alice").json()["caps"]
    assert (caps["replans"], caps["chat_turns"], caps["review_usd"]) == (0, 7, 12.5)


def test_a_workspace_admin_may_not_change_a_platform_setting(client, db, seeded, api_context):
    _name_admin(client, api_context)
    before = db.docs.get("control/settings")
    for change in ({"plan_diagrams_default": False}, {"plan_chat_model_class": "haiku"},
                   {"plan_review_caps": {"replans": 1}}):
        response = _admin_put(client, "alice", change)
        assert response.status_code == 403, response.text
    assert db.docs.get("control/settings") == before


@pytest.mark.parametrize("change", [
    {"plan_chat_model_class": "gpt-turbo"},
    {"plan_chat_model_class": "claude-opus-5-5"},
    {"plan_diagrams_default": "false"},
    {"plan_review_caps": {"replans": 21}},
    {"plan_review_caps": {"diagram_asks": -1}},
    {"plan_review_caps": {"chat_turns": 2.5}},
    {"plan_review_caps": {"review_usd": 500.01}},
    {"plan_review_caps": {"review_usd": True}},
    {"plan_review_caps": {"tokens": 5}},
    {"plan_review_caps": {}},
])
def test_a_platform_value_out_of_its_rule_is_refused_on_save(client, db, seeded, change):
    response = _admin_put(client, "root", change)
    assert response.status_code == 422, response.text
    assert "control/settings" not in db.docs


@pytest.mark.parametrize("stored, wanted", [
    ({"replans": 99, "diagram_asks": "20", "chat_turns": True, "review_usd": -1},
     {"replans": 5, "diagram_asks": 20, "chat_turns": 40, "review_usd": 25.0}),
    ({"replans": 3, "diagram_asks": 2.0, "chat_turns": 0, "review_usd": 600},
     {"replans": 3, "diagram_asks": 20, "chat_turns": 0, "review_usd": 25.0}),
    ("five", {"replans": 5, "diagram_asks": 20, "chat_turns": 40, "review_usd": 25.0}),
])
def test_a_stored_cap_out_of_range_or_of_the_wrong_type_reads_as_its_default(
        client, db, seeded, stored, wanted):
    # An admin's typo written straight to Firestore never lifts a cap.
    db.docs["control/settings"] = {"plan_review_caps": stored,
                                   "plan_chat_model_class": "gpt-turbo",
                                   "plan_diagrams_default": "no"}
    body = _get(client, "eng", "alice").json()
    assert {k: body["caps"][k] for k in plansettings.CAP_KEYS} == wanted
    assert body["effective"]["chat_model_class"]["value"] == "sonnet"
    assert body["effective"]["diagrams"]["value"] is True


# --------------------------------------------------------------------------
# the workspace level
# --------------------------------------------------------------------------

def test_a_workspace_admin_sets_the_workspace_level_audited_and_a_no_op_writes_nothing(
        client, db, seeded, api_context):
    _name_admin(client, api_context)
    response = _put(client, "eng", "alice", {"diagrams": "off", "chat_model_class": "opus"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plans"] == {"diagrams": "off", "chat_model_class": "opus"}
    assert body["effective"]["diagrams"] == {"value": False, "from": "workspace"}
    assert body["effective"]["chat_model_class"]["from"] == "workspace"
    assert body["can_edit"] is True
    (row,) = _audit(db)
    assert row["target_tenant_id"] == "eng" and row["by"] == "alice@example.com"
    assert row["detail"] == {"tenant_id": "eng", "keys": ["diagrams", "chat_model_class"],
                             "from": {"diagrams": None, "chat_model_class": None},
                             "to": {"diagrams": "off", "chat_model_class": "opus"}}
    stored = dict(db.docs["workspace_settings/eng"])
    # The same values again: nothing written, nothing audited.
    assert _put(client, "eng", "alice", {"diagrams": "off"}).status_code == 200
    assert db.docs["workspace_settings/eng"] == stored and len(_audit(db)) == 1
    # `inherit` hands it back to the platform.
    again = _put(client, "eng", "alice", {"diagrams": "inherit"}).json()
    assert again["effective"]["diagrams"] == {"value": True, "from": "platform"}
    assert len(_audit(db)) == 2


def test_the_owner_of_a_personal_workspace_sets_it(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"diagrams": "off"})
    assert response.status_code == 200, response.text
    assert db.docs[f"workspace_settings/{CAROL_TENANT}"]["plans"]["diagrams"] == "off"


def test_the_write_leaves_the_other_settings_alone(client, db, seeded):
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "merge": {"mode": "train"},
                                         "qa_flows_per_step": 7}
    assert _put(client, "eng", "root", {"diagrams": "on"}).status_code == 200
    doc = db.docs["workspace_settings/eng"]
    assert doc["merge"] == {"mode": "train"} and doc["qa_flows_per_step"] == 7


def test_a_member_who_is_not_its_admin_is_refused_and_nothing_written(client, db, seeded):
    response = _put(client, "eng", "alice", {"diagrams": "off"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert "workspace_settings/eng" not in db.docs and _audit(db) == []


def test_another_workspaces_settings_are_not_found(client, db, seeded):
    # bob is in research, not eng: neither served nor confirmed to exist.
    response = _get(client, "eng", "bob")
    assert response.status_code == 404 and "caps" not in response.text
    assert _put(client, "eng", "bob", {"diagrams": "off"}).status_code == 404
    assert "workspace_settings/eng" not in db.docs
    assert _get(client, "no-such-workspace", "root").status_code == 404


def test_the_workspace_put_refuses_caps_naming_them_as_the_platforms(client, db, seeded):
    response = _put(client, "eng", "root", {"caps": {"replans": 50}})
    assert response.status_code == 422, response.text
    assert "platform setting" in response.text
    response = _put(client, "eng", "root", {"diagrams": "on", "caps": {"replans": 50}})
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs and _audit(db) == []


@pytest.mark.parametrize("value", ["gpt-turbo", "claude-sonnet-5", "SONNET"])
def test_a_class_claude_code_does_not_carry_is_refused_at_the_workspace(
        client, db, seeded, value):
    response = _put(client, "eng", "root", {"chat_model_class": value})
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs


def test_a_stored_workspace_class_the_profile_no_longer_carries_falls_through(client, db, seeded):
    db.docs["workspace_settings/eng"] = {"plans": {"chat_model_class": "retired"}}
    model = _get(client, "eng", "alice").json()["effective"]["chat_model_class"]
    assert model["value"] == "sonnet" and model["from"] == "platform"
    assert "retired" in model["fell_through"]


def test_the_block_is_signed_once_the_contract_lists_it(client, db, seeded, api_context,
                                                        monkeypatch):
    # The contract request in this lane's PR: until `plans` is listed the
    # block is unsigned (`SIGNED` False on main); listed, every write carries
    # a MAC that verifies, and a forged block on a cut-over tenant is ignored.
    assert plansettings.SIGNED is (plansettings.MAC_BLOCK in SETTINGS_MAC_FIELDS)
    listed = MappingProxyType({**SETTINGS_MAC_FIELDS, "plans": ("diagrams", "chat_model_class")})
    monkeypatch.setattr("swarm_common.specsign.SETTINGS_MAC_FIELDS", listed)
    monkeypatch.setattr(plansettings, "SIGNED", True)
    key = secrets.token_hex(32)
    api_context.settings = dataclasses.replace(api_context.settings, settings_key=key,
                                               settings_mac_tenants=("eng",))
    assert _put(client, "eng", "root", {"diagrams": "off"}).status_code == 200
    block = db.docs["workspace_settings/eng"]["plans"]
    assert block_verifies("eng", "plans", block, (key,))
    db.docs["workspace_settings/eng"]["plans"] = {"diagrams": "on", "chat_model_class": "opus"}
    body = _get(client, "eng", "alice").json()
    assert body["effective"]["diagrams"] == {"value": True, "from": "platform"}
    assert body["effective"]["chat_model_class"]["from"] == "platform"


# --------------------------------------------------------------------------
# the resolution
# --------------------------------------------------------------------------

def _store(db, platform: dict[str, Any] | None = None) -> plansettings.PlanSettingsStore:
    return plansettings.PlanSettingsStore(db, now=lambda: NOW, mac=SettingsMac(),
                                          platform=lambda: platform or {})


def test_the_schedule_wins_over_the_workspace_and_the_workspace_over_the_platform(db):
    db.docs["workspace_settings/eng"] = {"plans": {"diagrams": "on"}}
    resolve = _store(db, {"plan_diagrams_default": False}).effective_plan_settings
    on = {"tenant_id": "eng", "plan_diagrams": "on"}
    off = {"tenant_id": "eng", "plan_diagrams": "off"}
    assert resolve("eng", off)["diagrams"] == {"value": False, "from": "schedule"}
    assert resolve("eng", on)["diagrams"] == {"value": True, "from": "schedule"}
    assert resolve("eng", None)["diagrams"] == {"value": True, "from": "workspace"}
    # No workspace choice: the platform's.
    assert resolve("research", None)["diagrams"] == {"value": False, "from": "platform"}


@pytest.mark.parametrize("schedule", [
    {"tenant_id": "eng", "plan_diagrams": "inherit"},
    {"tenant_id": "eng"},  # stored before the field: inherit
    {"tenant_id": "eng", "plan_diagrams": "sideways"},
])
def test_inherit_reports_the_level_it_inherited_from(db, schedule):
    resolve = _store(db).effective_plan_settings
    assert resolve("eng", schedule)["diagrams"] == {"value": True, "from": "platform"}
    db.docs["workspace_settings/eng"] = {"plans": {"diagrams": "off"}}
    assert resolve("eng", schedule)["diagrams"] == {"value": False, "from": "workspace"}


def test_another_tenants_schedule_is_never_read(db):
    from swarm_api.errors import NotFound

    with pytest.raises(NotFound):
        _store(db).effective_plan_settings("eng", {"tenant_id": "research", "plan_diagrams": "on"})


def test_the_record_a_run_writes_is_what_it_planned_under(db):
    effective = _store(db).effective_plan_settings("eng", None)
    assert plansettings.plan_settings_record(effective) == {
        "diagrams": True, "from": "platform", "read_at": NOW.isoformat()}


# --------------------------------------------------------------------------
# the schedule's plan_diagrams
# --------------------------------------------------------------------------

@pytest.fixture
def schedule_client(db, tokens, group_map, objects, monkeypatch) -> TestClient:
    from swarm_api import scheduletypes

    monkeypatch.setattr(scheduletypes, "executor_present", lambda entry, root=None: entry.name in BUILT)
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_repo(db, REPO, "eng")
    return TestClient(create_app(_context(db, tokens, group_map, objects)),
                      raise_server_exceptions=False)


def test_a_schedule_inherits_by_default_and_its_patch_sets_plan_diagrams(schedule_client, db):
    created = schedule_client.post("/v1/schedules", json=schedule_body(),
                                   headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    schedule = created.json()["schedule"]
    # No override named: none stored, and the resolution reads it as inherit.
    assert schedule.get("plan_diagrams", "inherit") == "inherit"
    sid = schedule["schedule_id"]
    inherited = _store(db).effective_plan_settings("eng", db.docs[f"schedules/{sid}"])
    assert inherited["diagrams"] == {"value": True, "from": "platform"}
    response = schedule_client.patch(f"/v1/schedules/{sid}", json={"revision": 1, "plan_diagrams": "off"},
                                     headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert db.docs[f"schedules/{sid}"]["plan_diagrams"] == "off"
    # Audited as any field (§6.4): the edit's entry carries it before and after.
    (edit,) = [d for p, d in db.docs.items() if p.startswith("schedule_audit/")
               and d["schedule_id"] == sid and d["action"] == "edit"]
    assert edit["detail"]["changed"] == {"plan_diagrams": {"from": None, "to": "off"}}
    bad = schedule_client.patch(f"/v1/schedules/{sid}", json={"revision": 2, "plan_diagrams": "yes"},
                                headers=auth_header("alice"))
    assert bad.status_code == 422, bad.text
    resolved = _store(db).effective_plan_settings("eng", db.docs[f"schedules/{sid}"])
    assert resolved["diagrams"] == {"value": False, "from": "schedule"}


def test_a_schedule_created_with_an_override_keeps_it(schedule_client, db):
    created = schedule_client.post("/v1/schedules", json=schedule_body(plan_diagrams="on"),
                                   headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    assert created.json()["schedule"]["plan_diagrams"] == "on"
