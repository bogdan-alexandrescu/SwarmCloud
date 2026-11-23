"""A QA target leg declares a list of extra origins, and a platform admin
approves the ones they name (the ledger's 'QA target extra origins' item).

A leg could reach its base origin and one `sign_in.extra_origin` alone, so an
app whose API lives on another host was refused after sign-in. A leg now
declares `extra_origins`, each `https://host[:port]` held to the base URL's
host rule, none twice, none its base origin; its non-base origins
(`sign_in.extra_origin` and `extra_origins`) are at most three, the gate's
four in all. Held here:

* each refusal of the list;
* `:approve-host` approves only the extra origins it names, a 409 for one the
  leg does not declare, and its audit row lists every approved origin;
* any change of the list sets the leg back to pending, as a host change
  does, the bypass value removed first;
* the worker's route serves exactly the approved origins, and the IdP token
  path only while `sign_in.extra_origin` is one of them.

Driven through the routes with test_qa_targets's fixtures: no cloud
credential and no emulator.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_api import qatargets

from .conftest import auth_header
from .test_qa_target_credential import FakeQaCredentials
from .test_qa_targets import (  # noqa: F401  (api_context, resolver and seeded are fixtures)
    CAROL_TENANT,
    PUBLIC,
    TARGETS,
    api_context,
    approve,
    audit_rows,
    register,
    resolver,
    seeded,
    target_docs,
)

BASE = "https://shop.example.com"
IDP = "https://auth.example.com"
API = "https://api.example.com"
FILES = "https://files.example.com:8443"
CDN = "https://cdn.example.com"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
_SIGN_IN = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me", "extra_origin": IDP}


@pytest.fixture
def hosts(resolver):
    for host in ("api.example.com", "files.example.com", "cdn.example.com"):
        resolver.answers[host] = [PUBLIC]
    return resolver


@pytest.fixture
def store(client) -> FakeQaCredentials:
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    return fake


def _registered(client, **over: Any) -> dict[str, Any]:
    response = register(client, **{"sign_in": _SIGN_IN, "extra_origins": [API, FILES], **over})
    assert response.status_code == 201, response.text
    return response.json()["qa_target"]


def _approval_row(db: Any) -> dict[str, Any]:
    return [r for r in audit_rows(db) if r["action"] == "qa_target_host_approved"][-1]


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def test_a_leg_declares_its_extra_origins(client, db, seeded, hosts):
    target = _registered(client, extra_origins=[API, "https://Files.Example.com:8443"])
    # MUTATION: leave `extra_origins` out of the leg's fields and the registration is refused (422).
    assert target["extra_origins"] == [API, FILES]
    assert target["host_state"] == "pending"


@pytest.mark.parametrize("extra", [
    "https://api.example.com",
    ["http://api.example.com"],
    ["https://api.example.com/v1"],
    ["https://api.example.com?x=1"],
    ["https://user@api.example.com"],
    ["https://10.0.0.1"],
    ["https://localhost"],
    ["https://unknown.example.com"],
    [7],
    [API, API],
    [API, "https://API.example.com"],
    [BASE],
    ["https://shop.example.com:443"],
    [IDP],
    [API, FILES, CDN],
])
def test_a_bad_extra_origins_list_is_refused(client, db, seeded, hosts, extra):
    response = register(client, sign_in=_SIGN_IN, extra_origins=extra)
    # MUTATION: drop any one of `_extra_origins`' checks and its case is stored (201).
    assert response.status_code == 422, (extra, response.text)
    assert response.json()["detail"]["field"] == "extra_origins"
    assert target_docs(db) == {}


def test_three_extra_origins_without_a_login_origin_are_accepted(client, db, seeded, hosts):
    response = register(client, extra_origins=[API, FILES, CDN])
    # MUTATION: count the base origin among the three and this is refused.
    assert response.status_code == 201, response.text


def test_an_entry_of_environments_declares_its_own(client, db, seeded, hosts):
    response = register(client, base_url=None, environments=[
        {"name": "production", "base_url": BASE, "extra_origins": [API]},
        {"name": "staging", "base_url": "https://app.example.com", "environment": "non_production"},
    ])
    assert response.status_code == 201, response.text
    entries = {e["name"]: e for e in response.json()["qa_target"]["environments"]}
    # MUTATION: leave `extra_origins` out of `_entry_api` and the console cannot read it back.
    assert entries["production"]["extra_origins"] == [API]
    assert "extra_origins" not in entries["staging"]


# --------------------------------------------------------------------------
# The approval
# --------------------------------------------------------------------------


def test_an_approval_covers_only_the_extra_origins_it_names(client, db, seeded, hosts):
    target = _registered(client)
    response = approve(client, target["id"], extra_origins=[FILES])
    assert response.status_code == 200, response.text
    # MUTATION: approve every declared origin and API is reached unapproved.
    assert response.json()["qa_target"]["approved_origins"] == [BASE, FILES]
    (doc,) = target_docs(db).values()
    # MUTATION: serve the declared list instead of the approved one and API and IDP are served.
    assert qatargets.QaTargetIssuer._answer(doc)["origins"] == [BASE, FILES]


def test_an_undeclared_extra_origin_is_refused(client, db, seeded, hosts):
    target = _registered(client)
    for named in ([CDN], [API, CDN], [IDP]):
        response = approve(client, target["id"], extra_origins=named)
        # MUTATION: approve what is named without reading the leg's list and this is 200.
        assert response.status_code == 409, (named, response.text)
    (doc,) = target_docs(db).values()
    assert doc["host_state"] == "pending" and doc["approved_origins"] == []


def test_the_audit_row_lists_every_approved_origin(client, db, seeded, hosts):
    target = _registered(client)
    response = approve(client, target["id"], extra_origin=IDP, extra_origins=[API, FILES],
                       idp_token_path=TOKEN_PATH)
    assert response.status_code == 200, response.text
    row = _approval_row(db)
    # MUTATION: write only the base and the login origin into the row and nobody sees API approved.
    assert row["detail"]["origins"] == [BASE, IDP, API, FILES]
    assert row["detail"]["idp_token_path"] == TOKEN_PATH
    (doc,) = target_docs(db).values()
    served = qatargets.QaTargetIssuer._answer(doc)
    assert served["origins"] == [BASE, IDP, API, FILES]
    assert served["idp_token_path"] == TOKEN_PATH


def test_the_token_path_is_served_only_with_the_login_origin_approved(client, db, seeded, hosts):
    target = _registered(client)
    response = approve(client, target["id"], extra_origins=[API], idp_token_path=TOKEN_PATH)
    # MUTATION: accept the token path beside any extra origin and it lands on the API host.
    assert response.status_code == 422, response.text
    assert approve(client, target["id"], extra_origins=[API]).status_code == 200
    (path, doc), = target_docs(db).items()
    # A stored approval of two origins whose second is not the login's.
    doc[qatargets.IDP_TOKEN_FIELD] = TOKEN_PATH
    served = qatargets.QaTargetIssuer._answer(doc)
    assert served["origins"] == [BASE, API]
    # MUTATION: serve the path on `len(approved_origins) == 2` and it rides onto the API host.
    assert "idp_token_path" not in served


# --------------------------------------------------------------------------
# A change of the list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [[API], [FILES, API], [API, CDN], None])
def test_a_change_of_the_list_resets_the_approval(client, db, seeded, hosts, extra):
    target = _registered(client)
    assert approve(client, target["id"], extra_origin=IDP, extra_origins=[API],
                   idp_token_path=TOKEN_PATH).status_code == 200
    response = client.patch(f"{TARGETS}/{target['id']}", json={"extra_origins": extra},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    changed = response.json()["qa_target"]
    # MUTATION: compare only the host and `sign_in.extra_origin` in `patch` and the approval survives.
    assert changed["host_state"] == "pending"
    assert changed["approved_origins"] == [] and changed["approved_idp_token_path"] is None
    row = [r for r in audit_rows(db) if r["action"] == "qa_target_changed"][-1]
    assert row["detail"]["host_reset"] is True


@pytest.mark.parametrize("extra", [[API], [FILES, API], [API, CDN], None])
def test_the_change_row_carries_the_lists_old_and_new_values(client, db, seeded, hosts, extra):
    """Epic 21: the patch's audit row names the list's values before and
    after, not only that `extra_origins` changed."""
    target = _registered(client)
    response = client.patch(f"{TARGETS}/{target['id']}", json={"extra_origins": extra},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    row = [r for r in audit_rows(db) if r["action"] == "qa_target_changed"][-1]
    assert row["detail"]["extra_origins"] == {"from": [API, FILES], "to": extra or []}


def test_an_unchanged_list_puts_no_values_in_the_change_row(client, db, seeded, hosts):
    target = _registered(client)
    response = client.patch(f"{TARGETS}/{target['id']}", json={"name": "Shop again"},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    row = [r for r in audit_rows(db) if r["action"] == "qa_target_changed"][-1]
    assert "extra_origins" not in row["detail"]


def test_an_approval_naming_an_overlong_extra_origin_is_a_422(client, db, seeded, hosts):
    """Epic 21: `ApproveBody.extra_origins` bounds each entry, as
    `extra_origin` is bounded, not only the list's length."""
    target = _registered(client)
    before = [dict(r) for r in audit_rows(db)]
    response = approve(client, target["id"],
                       extra_origins=["https://" + "a" * 300 + ".example.com"])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert [dict(r) for r in audit_rows(db)] == before


def test_another_change_keeps_the_approval(client, db, seeded, hosts):
    target = _registered(client)
    assert approve(client, target["id"], extra_origins=[API]).status_code == 200
    response = client.patch(f"{TARGETS}/{target['id']}", json={"name": "Shop app"},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    assert response.json()["qa_target"]["approved_origins"] == [BASE, API]


def test_a_change_of_the_list_removes_the_bypass_value_first(client, db, seeded, hosts, store):
    target = _registered(client, challenge_bypass={"kind": "header", "name": "X-QA-Bypass"})
    value = "qa-bypass-" + secrets.token_hex(16)
    put = client.put(f"{TARGETS}/{target['id']}/challenge-bypass", headers=auth_header("carol"),
                     json={"value": value})
    assert put.status_code == 200, put.text
    response = client.patch(f"{TARGETS}/{target['id']}", json={"extra_origins": [API]},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    # MUTATION: leave the list out of the host reset and the value is sent to an origin nobody set it for.
    assert "challenge_bypass" not in store.values[CAROL_TENANT].get(target["id"], {})
    assert response.json()["qa_target"]["challenge_bypass_value"]["set"] is False


def test_a_stored_target_with_only_a_login_origin_is_unchanged(client, db, seeded, hosts):
    response = register(client, sign_in=_SIGN_IN)
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    assert "extra_origins" not in target
    assert approve(client, target["id"], extra_origin=IDP, idp_token_path=TOKEN_PATH).status_code == 200
    (doc,) = target_docs(db).values()
    served = qatargets.QaTargetIssuer._answer(doc)
    assert served["origins"] == [BASE, IDP] and served["idp_token_path"] == TOKEN_PATH
