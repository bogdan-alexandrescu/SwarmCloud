"""A workspace's `code_intelligence.graph_delivery` setting (GS-SETTING, part of #1384).

docs/design/graph-service.md §8.1: how an attempt gets its code graph is a
STORED per-workspace setting, `staged` (today's per-pod snapshot) or
`service` (the shared `swarm-graph` service), beside `context` and
`context_ab` in the one `workspace_settings/{tenant_id}` document. What these
tests hold:

  * nothing stored is `inherit`, and the platform default is `staged`, so no
    workspace moves to the service until its owner or an admin says so;
  * the workspace's owner or a platform admin sets it, audited in the same
    transaction like the other keys; a member is 403 and a non-member 404,
    with nothing written;
  * `inherit` clears it; a PUT naming only it is accepted; any other word,
    or a URL, is a 422 (invariant 10: a caller names a mode, never a host);
  * one workspace's value never reaches another's (invariant 9), and the
    keys of the block this module does not own survive a write.

MUTATION: drop `graph_delivery` from `CodeIntelligence.as_dict` and
`test_the_owner_sets_service_and_it_is_audited` fails (no audit row, nothing
stored); make `DEFAULT_GRAPH_DELIVERY` `service` and
`test_nothing_stored_is_staged` fails.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import codeintel, judgment, refusals
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    return db


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/code-intelligence"


def _get(client, tenant_id, user):
    return client.get(_url(tenant_id), headers=auth_header(user))


def _put(client, tenant_id, user, body):
    return client.put(_url(tenant_id), headers=auth_header(user), json=body)


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _block(db, tenant_id) -> dict | None:
    return (db.docs.get(f"workspace_settings/{tenant_id}") or {}).get(codeintel.FIELD)


# --------------------------------------------------------------------------
# the names, and staged by default
# --------------------------------------------------------------------------

def test_the_names_are_the_designs():
    # graph-service.md §8.1: `code_intelligence.graph_delivery` ∈ staged | service.
    assert codeintel.DELIVERY_FIELD == "graph_delivery"
    assert codeintel.DELIVERIES == ("staged", "service")
    assert (codeintel.STAGED, codeintel.SERVICE) == ("staged", "service")
    assert codeintel.DEFAULT_GRAPH_DELIVERY == codeintel.STAGED


def test_nothing_stored_is_staged(client, db, seeded):
    body = _get(client, "eng", "alice").json()
    assert body["graph_delivery"] == "inherit"
    assert body["defaults"]["graph_delivery"] == "staged"
    assert body["effective_graph_delivery"] == "staged"
    # The pure reads agree, and reading wrote nothing.
    assert codeintel.settings_of(None).graph_delivery is None
    assert codeintel.effective_delivery(codeintel.settings_of(None)) == codeintel.STAGED
    assert _block(db, "eng") is None and _audit(db) == []


@pytest.mark.parametrize("stored", ["SERVICE", "remote", "", 1, True, None,
                                    "http://swarm-graph.example/mcp"])
def test_a_malformed_stored_value_is_unset(stored):
    settings = codeintel.settings_of({"graph_delivery": stored})
    assert settings.graph_delivery is None
    assert codeintel.effective_delivery(settings) == codeintel.STAGED


def test_a_stored_value_is_read_as_stored():
    for word in codeintel.DELIVERIES:
        settings = codeintel.settings_of({"graph_delivery": word})
        assert settings.graph_delivery == word
        assert codeintel.effective_delivery(settings) == word


# --------------------------------------------------------------------------
# who may change it, and the audit
# --------------------------------------------------------------------------

def test_the_owner_sets_service_and_it_is_audited(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"graph_delivery": "service"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["graph_delivery"] == "service"
    assert body["effective_graph_delivery"] == "service"
    assert _block(db, CAROL_TENANT)["graph_delivery"] == "service"
    (entry,) = _audit(db)
    assert entry["action"] == codeintel.AUDIT_ACTION
    assert entry["by"] == CAROL
    assert entry["detail"] == {"tenant_id": CAROL_TENANT,
                               "from": {"graph_delivery": None},
                               "to": {"graph_delivery": "service"}}


def test_an_admin_sets_it_and_the_other_keys_are_unchanged(client, db, seeded):
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng",
        codeintel.FIELD: {"jev": True, "context": "off", "context_ab": "on",
                          "jev_monthly_cap_tokens": 1000}}
    response = _put(client, "eng", "root", {"graph_delivery": "service"})
    assert response.status_code == 200, response.text
    block = _block(db, "eng")
    assert block["graph_delivery"] == "service"
    assert (block["jev"], block["context"], block["context_ab"]) == (True, "off", "on")
    assert block["jev_monthly_cap_tokens"] == 1000
    (entry,) = _audit(db)
    assert set(entry["detail"]["from"]) == {"graph_delivery"}
    # CONTROL: changing another key leaves graph_delivery where it was.
    assert _put(client, "eng", "root", {"jev": False}).status_code == 200
    assert _block(db, "eng")["graph_delivery"] == "service"


def test_a_member_cannot_set_it_and_nothing_is_written(client, db, seeded, monkeypatch):
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    response = _put(client, "eng", "alice", {"graph_delivery": "service"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == codeintel.OwnerRequired.code
    assert _block(db, "eng") is None and _audit(db) == []


def test_another_workspace_is_not_found_and_nothing_is_written(client, db, seeded):
    for tenant_id, user in (("eng", "bob"), (CAROL_TENANT, "alice")):
        response = _put(client, tenant_id, user, {"graph_delivery": "service"})
        assert response.status_code == 404, response.text
        assert "graph_delivery" not in response.text
    assert _block(db, "eng") is None and _block(db, CAROL_TENANT) is None
    assert _audit(db) == []


def test_one_workspaces_value_never_reaches_another(client, db, seeded):
    assert _put(client, "eng", "root", {"graph_delivery": "service"}).status_code == 200
    research = _get(client, "research", "root").json()
    assert research["graph_delivery"] == "inherit"
    assert research["effective_graph_delivery"] == "staged"
    assert _block(db, "research") is None


def test_inherit_clears_it_and_a_put_that_changes_nothing_audits_nothing(client, db, seeded):
    assert _put(client, "eng", "root", {"graph_delivery": "service"}).status_code == 200
    assert _put(client, "eng", "root", {"graph_delivery": "service"}).status_code == 200
    assert len(_audit(db)) == 1
    response = _put(client, "eng", "root", {"graph_delivery": "inherit"})
    assert response.status_code == 200, response.text
    assert response.json()["graph_delivery"] == "inherit"
    assert response.json()["effective_graph_delivery"] == "staged"
    assert "graph_delivery" not in _block(db, "eng")
    entry = _audit(db)[-1]
    assert entry["detail"]["from"] == {"graph_delivery": "service"}
    assert entry["detail"]["to"] == {"graph_delivery": None}
    # Explicitly staged is a stored value, distinct from inherit.
    assert _put(client, "eng", "root", {"graph_delivery": "staged"}).status_code == 200
    assert _block(db, "eng")["graph_delivery"] == "staged"
    assert len(_audit(db)) == 3


def test_the_store_reads_it_back(db, seeded):
    store = codeintel.CodeIntelligenceStore(db, now=lambda: NOW, mac=judgment.SettingsMac())
    store.put("eng", codeintel.CodeIntelligencePut(graph_delivery="service"), by="root@saga.xyz")
    assert store.get("eng").graph_delivery == "service"
    assert store.get("research").graph_delivery is None


# --------------------------------------------------------------------------
# validation (invariant 10)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"graph_delivery": "remote"},
    {"graph_delivery": "SERVICE"},
    {"graph_delivery": True},
    {"graph_delivery": None},
    {"graph_delivery": "http://swarm-graph.example/mcp"},
    {"graph_url": "http://swarm-graph.example/mcp"},
])
def test_an_invalid_body_is_refused_and_nothing_is_written(client, db, seeded, body):
    response = _put(client, "eng", "root", body)
    assert response.status_code == 422, response.text
    assert _block(db, "eng") is None and _audit(db) == []
