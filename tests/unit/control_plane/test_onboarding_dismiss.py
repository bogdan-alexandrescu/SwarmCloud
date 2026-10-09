"""`POST /v1/onboarding/dismiss` keeps the checklist hidden per person
(docs/onboarding.md §3.1-§3.2; #780).

What is held here, offline:

  * dismissing sets `dismissed_at` on the caller's own
    `onboarding/{tenant}__{user_hash}` document, and `GET /v1/onboarding`
    then serves `dismissed: true` with every step's state exactly as before
    -- hiding the checklist stops nothing;
  * `{dismissed: false}` clears it, and the checklist is served shown again;
  * every read and write is the caller's, in the caller's tenant
    (invariant 9): another member's document and another tenant's are
    untouched, and a document at the caller's id that names anyone else is
    not read as theirs;
  * the body forbids every field it does not name, so a tenant or a user
    cannot be named on it.
"""

from __future__ import annotations

import copy
from typing import Any

from swarm_api import forgeapp

from .conftest import auth_header

ALICE = "alice@saga.xyz"
ROOT = "root@saga.xyz"
BOB = "bob@saga.xyz"


def _doc_id(tenant: str, email: str) -> str:
    return f"onboarding/{tenant}__{forgeapp.user_hash(email)}"


def _view(client, user: str = "alice") -> dict[str, Any]:
    answer = client.get("/v1/onboarding", headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()


def _states(view: dict[str, Any]) -> list[tuple[str, str]]:
    return [(s["step"], s["state"]) for s in view["steps"]]


def test_a_fresh_checklist_is_not_dismissed(client):
    view = _view(client)
    assert view["dismissed"] is False and view["dismissed_at"] is None


def test_dismiss_hides_the_checklist_and_keeps_every_step(client, db):
    before = _view(client)
    answer = client.post("/v1/onboarding/dismiss", headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["dismissed"] is True and body["dismissed_at"]
    assert body["tenant_id"] == "eng"
    doc = db.docs[_doc_id("eng", ALICE)]
    assert doc["tenant_id"] == "eng" and doc["user"] == ALICE
    assert doc["user_hash"] == forgeapp.user_hash(ALICE)
    assert doc["dismissed_at"] is not None
    after = _view(client)
    assert after["dismissed"] is True and after["dismissed_at"]
    assert _states(after) == _states(before)
    assert after["next_step"] == before["next_step"]


def test_an_explicit_dismissed_true_is_the_same_as_none(client, db):
    answer = client.post("/v1/onboarding/dismiss", json={"dismissed": True},
                         headers=auth_header("alice"))
    assert answer.status_code == 200 and answer.json()["dismissed"] is True
    assert _view(client)["dismissed"] is True


def test_undismiss_clears_it(client, db):
    assert client.post("/v1/onboarding/dismiss",
                       headers=auth_header("alice")).status_code == 200
    answer = client.post("/v1/onboarding/dismiss", json={"dismissed": False},
                         headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["dismissed"] is False and answer.json()["dismissed_at"] is None
    assert db.docs[_doc_id("eng", ALICE)]["dismissed_at"] is None
    view = _view(client)
    assert view["dismissed"] is False and view["dismissed_at"] is None


def test_another_members_and_another_tenants_documents_are_untouched(client, db):
    seeded = {
        _doc_id("eng", ROOT): {"tenant_id": "eng", "user": ROOT,
                               "user_hash": forgeapp.user_hash(ROOT),
                               "dismissed_at": "2026-10-01T00:00:00+00:00"},
        _doc_id("research", BOB): {"tenant_id": "research", "user": BOB,
                                   "user_hash": forgeapp.user_hash(BOB),
                                   "dismissed_at": None},
        # Alice's hash under a tenant she does not act in.
        _doc_id("research", ALICE): {"tenant_id": "research", "user": ALICE,
                                     "user_hash": forgeapp.user_hash(ALICE),
                                     "dismissed_at": "2026-10-01T00:00:00+00:00"},
    }
    for key, value in seeded.items():
        db.docs[key] = copy.deepcopy(value)
    assert client.post("/v1/onboarding/dismiss",
                       headers=auth_header("alice")).status_code == 200
    assert client.post("/v1/onboarding/dismiss", json={"dismissed": False},
                       headers=auth_header("alice")).status_code == 200
    for key, value in seeded.items():
        assert db.docs[key] == value, key
    # Alice's dismissal is hers: root in the same tenant, and bob in
    # another, see their own state.
    assert client.post("/v1/onboarding/dismiss",
                       headers=auth_header("alice")).status_code == 200
    assert _view(client, "root")["dismissed"] is True, "root's own seeded document"
    assert _view(client, "bob")["dismissed"] is False


def test_a_document_at_the_callers_id_naming_someone_else_is_not_theirs(client, db):
    db.docs[_doc_id("eng", ALICE)] = {"tenant_id": "research", "user": BOB,
                                      "dismissed_at": "2026-10-01T00:00:00+00:00"}
    assert _view(client)["dismissed"] is False
    answer = client.post("/v1/onboarding/dismiss", headers=auth_header("alice"))
    assert answer.status_code == 404
    assert db.docs[_doc_id("eng", ALICE)]["user"] == BOB


def test_the_body_names_no_tenant_and_no_user(client, db):
    for body in ({"dismissed": True, "tenant_id": "research"},
                 {"dismissed": True, "user": BOB},
                 {"dismissed": "maybe"}):
        answer = client.post("/v1/onboarding/dismiss", json=body, headers=auth_header("alice"))
        assert answer.status_code == 422, body
    assert not db.dump("onboarding/")


def test_dismiss_needs_a_signed_in_caller(client, db):
    answer = client.post("/v1/onboarding/dismiss")
    assert answer.status_code == 401
    assert not db.dump("onboarding/")
