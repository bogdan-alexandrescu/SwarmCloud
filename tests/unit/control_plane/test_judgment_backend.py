"""Each workspace chooses Jev's judgment backend (JUDGE-SELECT).

Owner decisions 2026-10-14: Jev's judgments go to one of three backends --
"Internal Jev" (THE DEFAULT), "Internal Laya", both Saga's own decision API
reached over the cluster's Tailscale egress, or "TypeSafe" (api.typesafe.ai,
which needs a TypeSafe key and receives page text). A workspace's owner or a
platform admin chooses; every choice is an `admin_audit` entry.

What these tests hold:

  * the SETTING: default internal-jev with nothing stored; only the owner of a
    personal workspace or a platform admin may change it; a member of a group
    workspace may read it and may not change it; another workspace is a 404;
    every change is audited in the same write; an unknown value is a 422;
  * AVAILABILITY, as a matrix: internal backends exactly when the platform
    renders INTERNAL_DECISION_API_URL (the tailnet egress switch), TypeSafe
    exactly when a TypeSafe key resolves for the caller in that workspace;
  * the STATUS the Submit picker and the strip read (`GET /v1/judgment`, and
    `jev` on `GET /v1/provider-keys/typesafe`): `backend`, its label and its
    availability with a reason.

MUTATION: drop the owner/admin check in `put_judgment_backend` and
`test_a_group_member_cannot_change_the_backend` fails; drop the audit write in
`JudgmentSettings.set` and `test_every_change_is_audited` fails; make
`availability` ignore `internal_enabled` and the matrix fails.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.judgment import (
    AUDIT_JUDGMENT_SET,
    BACKENDS,
    DEFAULT_BACKEND,
    INTERNAL_DECISION_SERVICE,
    INTERNAL_JEV,
    INTERNAL_LAYA,
    LABELS,
    SETTINGS_COLLECTION,
    TYPESAFE_BACKEND,
    availability,
    jev_status,
)
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore
from .test_provider_keys import fake_key, seed_personal

ROUTE = "/v1/workspaces/{}/judgment-backend"


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "dave@saga.xyz": (ENG_GROUP,),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture(params=[True, False], ids=["egress-on", "egress-off"])
def internal_on(request) -> bool:
    return request.param


def _client(db, tokens, group_map, objects, *, internal: bool) -> TestClient:
    ctx = build_context(
        settings=api_settings(
            internal_decision_api_url=INTERNAL_DECISION_SERVICE if internal else ""),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def client(db, tokens, group_map, objects) -> TestClient:
    return _client(db, tokens, group_map, objects, internal=True)


@pytest.fixture
def seeded(db: FakeFirestore) -> FakeFirestore:
    seed_tenant(db, "eng")
    seed_personal(db, "alice")
    return db


def as_(who: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(who)
    if tenant is not None:
        headers[TENANT_HEADER] = tenant
    return headers


def audits(db: FakeFirestore) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith(f"{AUDIT_COLLECTION}/")]


def put(client, who: str, tenant_id: str, backend: str, header: str | None = None):
    return client.put(ROUTE.format(tenant_id), headers=as_(who, header),
                      json={"judgment_backend": backend})


# -- the values ---------------------------------------------------------------

def test_the_three_backends_and_the_default_are_the_owners():
    assert BACKENDS == ("internal-jev", "internal-laya", "typesafe")
    assert DEFAULT_BACKEND == INTERNAL_JEV == "internal-jev"
    assert (INTERNAL_LAYA, TYPESAFE_BACKEND) == ("internal-laya", "typesafe")
    assert LABELS[INTERNAL_JEV] == "Jev (Internal)"
    assert LABELS[TYPESAFE_BACKEND] == "Jev (TypeSafe)"
    assert INTERNAL_DECISION_SERVICE == "http://decision-api.swarm-system.svc.cluster.local"


# -- availability: a pure matrix ------------------------------------------------

@pytest.mark.parametrize("internal_enabled", [True, False])
@pytest.mark.parametrize("typesafe_key", [True, False])
def test_availability_matrix(internal_enabled, typesafe_key):
    table = availability(internal_enabled=internal_enabled, typesafe_key=typesafe_key)
    assert set(table) == set(BACKENDS)
    for backend in (INTERNAL_JEV, INTERNAL_LAYA):
        available, reason = table[backend]
        assert available is internal_enabled
        assert (reason is None) is internal_enabled
        if not internal_enabled:
            assert "tailnet" in reason
    available, reason = table[TYPESAFE_BACKEND]
    assert available is typesafe_key
    assert (reason is None) is typesafe_key
    if not typesafe_key:
        assert "TypeSafe key" in reason


def test_the_status_says_the_chosen_backend_its_label_and_whether_it_works():
    status = jev_status(TYPESAFE_BACKEND, internal_enabled=True, typesafe_key=False)
    assert status["backend"] == "typesafe"
    assert status["label"] == "Jev (TypeSafe)"
    assert status["available"] is False and "TypeSafe key" in status["reason"]
    assert status["default"] == DEFAULT_BACKEND
    assert [o["backend"] for o in status["options"]] == list(BACKENDS)
    internal = jev_status(INTERNAL_JEV, internal_enabled=True, typesafe_key=False)
    assert internal["available"] is True and internal["reason"] is None


# -- the setting --------------------------------------------------------------

def test_nothing_stored_reads_as_internal_jev(client, seeded):
    response = client.get(ROUTE.format("eng"), headers=as_("alice", "eng"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["judgment_backend"] == "internal-jev"
    assert body["jev"]["backend"] == "internal-jev"
    assert body["jev"]["set_by"] is None
    assert body["can_edit"] is False


def test_the_owner_of_a_personal_workspace_chooses_its_backend(client, seeded, db):
    response = put(client, "alice", "u-alice", "internal-laya")
    assert response.status_code == 200, response.text
    assert response.json()["judgment_backend"] == "internal-laya"
    assert db.docs[f"{SETTINGS_COLLECTION}/u-alice"]["judgment_backend"] == "internal-laya"
    body = client.get(ROUTE.format("u-alice"), headers=as_("alice")).json()
    assert body["judgment_backend"] == "internal-laya"
    assert body["jev"]["set_by"] == "alice@saga.xyz"
    assert body["can_edit"] is True


def test_a_group_member_cannot_change_the_backend(client, seeded, db):
    response = put(client, "alice", "eng", "typesafe", header="eng")
    assert response.status_code == 403, response.text
    assert f"{SETTINGS_COLLECTION}/eng" not in db.docs
    assert audits(db) == []


def test_a_platform_admin_chooses_any_workspaces_backend(client, seeded, db):
    for tenant_id in ("eng", "u-alice"):
        response = put(client, "root", tenant_id, "typesafe", header="eng")
        assert response.status_code == 200, (tenant_id, response.text)
        assert db.docs[f"{SETTINGS_COLLECTION}/{tenant_id}"]["judgment_backend"] == "typesafe"


def test_another_workspace_is_not_found(client, seeded):
    seed_personal(seeded, "dave")
    # dave reads and writes alice's personal workspace: neither is his.
    assert client.get(ROUTE.format("u-alice"), headers=as_("dave")).status_code == 404
    assert put(client, "dave", "u-alice", "typesafe").status_code in (403, 404)
    assert client.get(ROUTE.format("nope"), headers=as_("root")).status_code == 404


def test_an_unknown_backend_or_an_extra_field_is_refused(client, seeded, db):
    assert put(client, "alice", "u-alice", "openai").status_code == 422
    response = client.put(ROUTE.format("u-alice"), headers=as_("alice"),
                          json={"judgment_backend": "typesafe", "url": "https://x.invalid"})
    assert response.status_code == 422
    assert f"{SETTINGS_COLLECTION}/u-alice" not in db.docs


def test_every_change_is_audited(client, seeded, db):
    assert put(client, "alice", "u-alice", "typesafe").status_code == 200
    assert put(client, "root", "eng", "internal-laya", header="eng").status_code == 200
    entries = sorted(audits(db), key=lambda a: a["by"])
    assert [a["action"] for a in entries] == [AUDIT_JUDGMENT_SET] * 2
    assert [(a["by"], a["detail"]["tenant_id"], a["detail"]["judgment_backend"],
             a["detail"]["previous"]) for a in entries] == [
        ("alice@saga.xyz", "u-alice", "typesafe", "internal-jev"),
        ("root@saga.xyz", "eng", "internal-laya", "internal-jev"),
    ]


# -- availability, through the routes --------------------------------------------

def test_availability_follows_the_egress_switch_and_the_key(db, tokens, group_map, objects,
                                                            internal_on):
    seed_tenant(db, "eng")
    seed_personal(db, "alice")
    client = _client(db, tokens, group_map, objects, internal=internal_on)
    options = {o["backend"]: o for o in client.get(
        "/v1/judgment", headers=as_("alice")).json()["jev"]["options"]}
    assert options["internal-jev"]["available"] is internal_on
    assert options["internal-laya"]["available"] is internal_on
    assert options["typesafe"]["available"] is False
    stored = client.put("/v1/provider-keys/typesafe/user", headers=as_("alice"),
                        json={"api_key": fake_key()})
    assert stored.status_code == 200, stored.text
    options = {o["backend"]: o for o in client.get(
        "/v1/judgment", headers=as_("alice")).json()["jev"]["options"]}
    assert options["typesafe"]["available"] is True
    assert options["internal-jev"]["available"] is internal_on


def test_the_selected_workspaces_status_is_on_the_judgment_route(client, seeded):
    put(client, "alice", "u-alice", "typesafe")
    body = client.get("/v1/judgment", headers=as_("alice")).json()
    assert body["tenant_id"] == "u-alice"
    assert body["jev"]["backend"] == "typesafe"
    assert body["jev"]["label"] == "Jev (TypeSafe)"
    assert body["jev"]["available"] is False  # no key yet
    assert body["can_edit"] is True


def test_the_typesafe_key_status_carries_the_jev_backend(client, seeded):
    body = client.get("/v1/provider-keys/typesafe", headers=as_("alice")).json()
    assert body["jev"]["backend"] == "internal-jev"
    assert body["jev"]["available"] is True
    assert body["jev"]["label"] == "Jev (Internal)"
