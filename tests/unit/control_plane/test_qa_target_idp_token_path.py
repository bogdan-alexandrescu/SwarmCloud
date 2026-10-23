"""A QA target's IdP token path is set by a platform admin where the extra
origin is approved, and nowhere else (QA-IDP-POST; part of #2067).

BGD decided on 2026-10-23: the live gate lets exactly `POST
<extra_origin><idp_token_path>` through once its own sign-in has completed
(livegate/qatarget.py). That widens a security gate, so the path is held here
to what only the platform admin's `:approve-host` sets:

* it is approved with the extra origin, and refused without one, from a
  non-admin, or when it is not one plain path;
* the worker's route serves it beside the approved origins, and serves none
  when the extra origin was not approved;
* a workspace admin's change cannot set it, and a change of host or extra
  origin, or an approval that does not name it, takes it away.

Driven through the routes with test_qa_targets's fixtures: no cloud
credential and no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qatargets

from .conftest import auth_header
from .test_qa_targets import (  # noqa: F401  (resolver and seeded are fixtures)
    TARGETS,
    api_context,
    approve,
    audit_rows,
    register,
    resolver,
    seeded,
    target_docs,
)

IDP = "https://auth.example.com"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"
_SIGN_IN = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me", "extra_origin": IDP}


def _registered(client) -> dict[str, Any]:
    response = register(client, sign_in=_SIGN_IN)
    assert response.status_code == 201, response.text
    return response.json()["qa_target"]


def test_a_platform_admin_approves_the_token_path_with_the_extra_origin(client, db, seeded):
    target = _registered(client)
    response = approve(client, target["id"], extra_origin=IDP, idp_token_path=TOKEN_PATH)
    assert response.status_code == 200, response.text
    # MUTATION: leave `idp_token_path` out of `ApproveBody` (422) or of the stored approval and this fails.
    assert response.json()["qa_target"]["approved_idp_token_path"] == TOKEN_PATH
    (doc,) = target_docs(db).values()
    served = qatargets.QaTargetIssuer._answer(doc)
    # MUTATION: leave it out of `QaTargetIssuer._answer` and the gate never lets the refresh through.
    assert served["idp_token_path"] == TOKEN_PATH
    assert served["origins"] == ["https://shop.example.com", IDP]
    row = [r for r in audit_rows(db) if r["action"] == "qa_target_host_approved"][-1]
    # MUTATION: leave it out of the audit row and nobody can tell who widened the gate.
    assert row["detail"]["idp_token_path"] == TOKEN_PATH


def test_a_non_admin_cannot_set_it(client, db, seeded):
    target = _registered(client)
    # carol administers her own workspace, not the platform.
    response = approve(client, target["id"], user="carol", extra_origin=IDP, idp_token_path=TOKEN_PATH)
    # MUTATION: guard the route with the workspace admin instead of `admin_auth` and this is 200.
    assert response.status_code == 403, response.text
    (doc,) = target_docs(db).values()
    assert doc.get("approved_idp_token_path") is None


def test_it_is_refused_without_an_extra_origin(client, db, seeded):
    target = _registered(client)
    response = approve(client, target["id"], idp_token_path=TOKEN_PATH)
    # MUTATION: store it without the extra origin named and the base origin's path would be the one let through.
    assert response.status_code == 422, response.text
    (doc,) = target_docs(db).values()
    assert doc["host_state"] == "pending" and doc.get("approved_idp_token_path") is None


def test_it_is_refused_on_a_target_that_declares_no_extra_origin(client, db, seeded):
    target = register(client).json()["qa_target"]
    response = approve(client, target["id"], idp_token_path=TOKEN_PATH)
    # MUTATION: accept it on a target with one origin and this is 200.
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("path", [
    "realms/token", "/realms/token?x=1", "/realms/token#f", "/realms/../token", "/realms/./token",
    "/realms//token", "//evil.example/token", "/realms/token/", "/realms/%74oken", "/realms/to ken",
    "/", "", "/*", "/realms/*/token", "/realms/tok[e]n", "/" + "a" * 600,
    "https://auth.example.com/realms/token",
])
def test_a_token_path_that_is_not_one_plain_path_is_refused(client, db, seeded, path):
    target = _registered(client)
    response = approve(client, target["id"], extra_origin=IDP, idp_token_path=path)
    # MUTATION: accept any string (or a glob, a query, a dot segment) and this is 200.
    assert response.status_code == 422, (path, response.text)
    (doc,) = target_docs(db).values()
    assert doc["host_state"] == "pending" and doc.get("approved_idp_token_path") is None


def test_a_workspace_admins_change_cannot_set_it(client, db, seeded):
    target = _registered(client)
    approve(client, target["id"], extra_origin=IDP)
    for body in ({"approved_idp_token_path": TOKEN_PATH}, {"idp_token_path": TOKEN_PATH},
                 {"sign_in": {**_SIGN_IN, "idp_token_path": TOKEN_PATH}}):
        response = client.patch(f"{TARGETS}/{target['id']}", json=body, headers=auth_header("carol"))
        # MUTATION: accept it as a registration field and the target's owner widens the gate themselves.
        assert response.status_code == 422, (body, response.text)
    response = register(client, name="Other", sign_in={**_SIGN_IN, "idp_token_path": TOKEN_PATH})
    assert response.status_code == 422, response.text
    response = register(client, name="Other", idp_token_path=TOKEN_PATH)
    assert response.status_code == 422, response.text
    (doc,) = target_docs(db).values()
    assert doc.get("approved_idp_token_path") is None


def test_moving_the_extra_origin_takes_it_away(client, db, seeded):
    target = _registered(client)
    approve(client, target["id"], extra_origin=IDP, idp_token_path=TOKEN_PATH)
    moved = {**_SIGN_IN, "extra_origin": "https://app.example.com"}
    changed = client.patch(f"{TARGETS}/{target['id']}", json={"sign_in": moved},
                           headers=auth_header("carol")).json()["qa_target"]
    # MUTATION: leave it out of the host reset in `QaTargets.patch` and the path rides onto the new origin.
    assert changed["approved_idp_token_path"] is None
    (doc,) = target_docs(db).values()
    assert "idp_token_path" not in qatargets.QaTargetIssuer._answer(doc)


def test_another_change_keeps_it(client, db, seeded):
    target = _registered(client)
    approve(client, target["id"], extra_origin=IDP, idp_token_path=TOKEN_PATH)
    changed = client.patch(f"{TARGETS}/{target['id']}", json={"name": "Shop app"},
                           headers=auth_header("carol")).json()["qa_target"]
    # CONTROL for the test above: a rename is not a new origin.
    assert changed["host_state"] == "approved"
    assert changed["approved_idp_token_path"] == TOKEN_PATH


def test_an_approval_that_does_not_name_it_takes_it_away(client, db, seeded):
    target = _registered(client)
    approve(client, target["id"], extra_origin=IDP, idp_token_path=TOKEN_PATH)
    approved = approve(client, target["id"], extra_origin=IDP).json()["qa_target"]
    # MUTATION: keep the previous approval's path and an admin cannot take the exception back.
    assert approved["approved_idp_token_path"] is None
    (doc,) = target_docs(db).values()
    assert "idp_token_path" not in qatargets.QaTargetIssuer._answer(doc)


def test_a_target_approved_without_it_is_served_as_before(client, db, seeded):
    target = _registered(client)
    approve(client, target["id"], extra_origin=IDP)
    (doc,) = target_docs(db).values()
    served = qatargets.QaTargetIssuer._answer(doc)
    # MUTATION: serve `idp_token_path: None` and the worker hands the gate a null it refuses.
    assert "idp_token_path" not in served
