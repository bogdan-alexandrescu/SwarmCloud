"""`workspace_settings/{tenant}.quality_gates.jev`: owner/admin-only, audited, off by default (JEV-LANES).

    GET  /v1/quality-gates                          the selected workspace's switches
    GET  /v1/workspaces/{tenant_id}/quality-gates   the same, by id
    PUT  /v1/workspaces/{tenant_id}/quality-gates   `{jev}`, a bool; audited

The switch decides that a lane's reply, edits and CLAUDE.md rules go to
TypeSafe under the workspace's key, so it is held like
`code_intelligence.jev` (PR 1330): only the owner, the workspace's admin or a
platform admin may change it, and every change is one audit row.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qualitygates

from .conftest import auth_header
from .test_settings_mac import configure, new_key, reference_verifies
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL_TENANT,
    api_context,
    resolver,
    seeded,
)


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/quality-gates"


def _put(client, tenant_id: str, user: str, body: dict[str, Any]):
    return client.put(_url(tenant_id), json=body, headers=auth_header(user))


def _audit(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == qualitygates.AUDIT_ACTION]


def test_the_switch_reads_off_until_set(client, db, seeded):
    response = client.get(_url("eng"), headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quality_gates"] == {"jev": False}
    assert body["defaults"] == {"quality_gates": {"jev": False}}
    assert body["can_edit"] is False
    selected = client.get("/v1/quality-gates", headers={**auth_header("alice"), "X-Swarm-Tenant": "eng"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["quality_gates"] == {"jev": False}


def test_an_admin_turns_it_on_once_audited_and_the_read_serves_it(client, db, seeded):
    response = _put(client, "eng", "root", {"jev": True})
    assert response.status_code == 200, response.text
    assert response.json()["quality_gates"] == {"jev": True} and response.json()["can_edit"] is True
    assert client.get(_url("eng"), headers=auth_header("alice")).json()["quality_gates"] == {"jev": True}
    assert db.docs["workspace_settings/eng"]["quality_gates"]["jev"] is True
    (row,) = _audit(db)
    # MUTATION: drop the audit write and this is empty.
    assert row["target_tenant_id"] == "eng" and row["by"] == "root@example.com"
    assert row["detail"] == {"tenant_id": "eng", "setting": "quality_gates.jev", "from": False, "to": True}
    # The same value again changes nothing and audits nothing.
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert len(_audit(db)) == 1
    # Off again is a second row.
    assert _put(client, "eng", "root", {"jev": False}).json()["quality_gates"] == {"jev": False}
    assert [r["detail"]["to"] for r in _audit(db)].count(False) == 1


def test_the_owner_of_a_personal_workspace_turns_it_on(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"jev": True})
    assert response.status_code == 200, response.text
    assert qualitygates.stored_jev(db.docs[f"workspace_settings/{CAROL_TENANT}"]) is True


def test_the_write_leaves_the_other_settings_alone(client, db, seeded):
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "code_intelligence": {"jev": True},
                                         "qa_flows_per_step": 7}
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    doc = db.docs["workspace_settings/eng"]
    assert doc["code_intelligence"] == {"jev": True} and doc["qa_flows_per_step"] == 7


@pytest.mark.parametrize("value", ["true", 1, None, "on"])
def test_anything_but_a_bool_is_refused_and_nothing_written(client, db, seeded, value):
    response = _put(client, "eng", "root", {"jev": value})
    # MUTATION: a plain `bool` field and "true" or 1 is stored as on.
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_member_who_is_not_its_admin_is_refused_and_nothing_written(client, db, seeded):
    response = _put(client, "eng", "alice", {"jev": True})
    # MUTATION: let any member change it and this is 200.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_caller_outside_the_workspace_is_answered_not_found(client, db, seeded):
    assert client.get(_url("eng"), headers=auth_header("bob")).status_code == 404
    assert _put(client, "eng", "bob", {"jev": True}).status_code in (403, 404)
    assert "workspace_settings/eng" not in db.docs
    assert client.get(_url("no-such-workspace"), headers=auth_header("root")).status_code == 404


@pytest.mark.parametrize("planted", ["true", 1, {"on": True}, ["jev"]])
def test_a_stored_value_that_is_not_true_reads_off(planted):
    # A direct Firestore write (the block is not yet MAC'd): only `jev is True` is on.
    assert qualitygates.stored_jev({"quality_gates": {"jev": planted}}) is False
    assert qualitygates.stored_jev({"quality_gates": planted}) is False
    assert qualitygates.stored_jev({"quality_gates": {"jev": True}}) is True


# -- signed (contract request 128) ---------------------------------------------


def test_every_write_signs_the_block_and_a_copy_onto_another_workspace_does_not_verify(
        client, db, seeded, api_context):
    key = new_key()
    configure(api_context, settings_key=key)
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    block = db.docs["workspace_settings/eng"]["quality_gates"]
    # MUTATION: drop the writer's block_mac and there is no `mac` to verify.
    assert reference_verifies(key, "eng", "quality_gates", block)
    assert not reference_verifies(key, "research", "quality_gates", block)
    assert not reference_verifies(key, "eng", "quality_gates", {**block, "jev": False})
    # Off again is signed too, over the new value.
    assert _put(client, "eng", "root", {"jev": False}).status_code == 200
    block = db.docs["workspace_settings/eng"]["quality_gates"]
    assert block["jev"] is False and reference_verifies(key, "eng", "quality_gates", block)


def test_a_forged_on_reads_off_for_a_workspace_cut_over(client, db, seeded, api_context):
    configure(api_context, settings_key=new_key(), settings_mac_tenants=("eng",))
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": {"jev": True}}
    body = client.get(_url("eng"), headers=auth_header("alice")).json()
    # MUTATION: read the block as stored and a forged "on" is served as on.
    assert body["quality_gates"] == {"jev": False}


def test_saving_the_same_value_over_an_unsigned_block_signs_it_without_an_audit_row(
        client, db, seeded, api_context):
    key = new_key()
    configure(api_context, settings_key=key)
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": {"jev": True}}
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    block = db.docs["workspace_settings/eng"]["quality_gates"]
    # MUTATION: return on an unchanged value alone and the block stays unsigned.
    assert reference_verifies(key, "eng", "quality_gates", block)
    assert _audit(db) == []
    # Signed now: the same value again writes nothing.
    before = dict(block)
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert db.docs["workspace_settings/eng"]["quality_gates"] == before


def test_a_deployment_with_no_key_writes_no_mac(client, db, seeded):
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert "mac" not in db.docs["workspace_settings/eng"]["quality_gates"]


# -- on request (the owner's pick, 2026-11-01) ---------------------------------


def test_on_request_is_accepted_signed_and_served(client, db, seeded, api_context):
    """The third value: only a step whose signed workflow switch says
    `quality_gates: on` runs the gates. Signed exactly as `true` is: the MAC
    covers `jev` whatever its value (`specsign.canonical_settings_block`
    projects the stored value of every key SETTINGS_MAC_FIELDS lists)."""
    key = new_key()
    configure(api_context, settings_key=key)
    response = _put(client, "eng", "root", {"jev": "on_request"})
    # MUTATION: keep `jev: StrictBool` and this is 422.
    assert response.status_code == 200, response.text
    assert response.json()["quality_gates"] == {"jev": "on_request"}
    block = db.docs["workspace_settings/eng"]["quality_gates"]
    assert block["jev"] == "on_request"
    assert reference_verifies(key, "eng", "quality_gates", block)
    assert not reference_verifies(key, "eng", "quality_gates", {**block, "jev": True})
    assert qualitygates.stored_jev(db.docs["workspace_settings/eng"], tenant_id="eng",
                                   mac=None) == "on_request"
    (row,) = _audit(db)
    assert row["detail"]["from"] is False and row["detail"]["to"] == "on_request"


@pytest.mark.parametrize("value", ["on-request", "ON_REQUEST", "request", "always"])
def test_no_other_word_is_accepted(client, db, seeded, value):
    response = _put(client, "eng", "root", {"jev": value})
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs
