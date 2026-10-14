"""A workspace's Code intelligence switch: GET /v1/code-intelligence and
PUT /v1/workspaces/{id}/code-intelligence (CTXQ-UI's seam; context-quality §4.7).

What these tests hold:

  * OFF BY DEFAULT. A workspace that never set it answers `jev: false` and
    `context: true`.
  * ONE DOCUMENT. The setting is the `code_intelligence` field of
    `workspace_settings/{tenant_id}`, written with a merge: TRAIN-WS's `merge`
    field in the same document survives a write.
  * WHO. The owner of a personal workspace and a platform admin change it; a
    member who is neither is refused 403 with nothing written; a caller outside
    the workspace gets the established 404.
  * AUDIT. Every change is one `admin_audit` entry; a no-op audits nothing.
  * The body forbids unknown fields and needs one known one.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

import pytest

from swarm_api import mergesettings
from swarm_api.routes import code_intelligence
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})


def _put(client, tenant_id, user, body):
    return client.put(f"/v1/workspaces/{tenant_id}/code-intelligence", headers=auth_header(user), json=body)


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _doc(db, tenant_id) -> dict:
    return db.docs.get(f"{mergesettings.COLLECTION}/{tenant_id}") or {}


def test_a_workspace_that_never_set_it_has_jev_off_and_context_on(client, seeded):
    response = client.get("/v1/code-intelligence", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["code_intelligence"]["jev"] is False
    assert body["code_intelligence"]["context"] is True
    assert body["can_edit"] is False


def test_the_owner_turns_jev_on_in_the_one_settings_document_and_is_audited(client, db, seeded):
    db.docs[f"{mergesettings.COLLECTION}/{CAROL_TENANT}"] = {"merge": {"mode": "train"}}
    response = _put(client, CAROL_TENANT, "carol", {"code_intelligence": {"jev": True}})
    assert response.status_code == 200, response.text
    assert response.json()["code_intelligence"]["jev"] is True
    assert response.json()["can_edit"] is True
    whole = _doc(db, CAROL_TENANT)
    assert whole["merge"] == {"mode": "train"}, "the write must merge, not replace the document"
    assert whole[code_intelligence.FIELD]["jev"] is True
    (entry,) = _audit(db)
    assert entry["action"] == code_intelligence.AUDIT_ACTION
    assert entry["by"] == CAROL
    assert entry["detail"] == {"tenant_id": CAROL_TENANT, "from": {"jev": False}, "to": {"jev": True}}
    # The same value again changes nothing and audits nothing.
    assert _put(client, CAROL_TENANT, "carol", {"code_intelligence": {"jev": True}}).status_code == 200
    assert len(_audit(db)) == 1


def test_a_platform_admin_changes_a_group_workspace(client, db, seeded):
    response = _put(client, "eng", "root", {"code_intelligence": {"jev": True}})
    assert response.status_code == 200, response.text
    assert _doc(db, "eng")[code_intelligence.FIELD]["jev"] is True


def test_a_member_who_is_not_owner_or_admin_is_refused_and_nothing_is_written(client, db, seeded):
    response = _put(client, "eng", "alice", {"code_intelligence": {"jev": True}})
    assert response.status_code == 403, response.text
    assert code_intelligence.FIELD not in _doc(db, "eng")
    assert _audit(db) == []


def test_a_caller_outside_the_workspace_is_answered_not_found(client, db, seeded):
    assert _put(client, CAROL_TENANT, "alice", {"code_intelligence": {"jev": True}}).status_code == 404
    assert _put(client, "no-such-workspace", "root", {"code_intelligence": {"jev": True}}).status_code == 404
    assert _audit(db) == []


@pytest.mark.parametrize("body", [
    {"code_intelligence": {}},
    {"code_intelligence": {"jev": "yes"}},
    {"code_intelligence": {"jev": True, "url": "https://example.invalid"}},
    {"jev": True},
])
def test_a_body_that_names_nothing_known_is_refused(client, seeded, body):
    assert _put(client, CAROL_TENANT, "carol", body).status_code == 422
