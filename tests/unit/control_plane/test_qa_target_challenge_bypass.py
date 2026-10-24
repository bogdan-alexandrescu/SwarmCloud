"""A QA target's challenge bypass, swarm-api's half (docs/design/visual-qa-any-app.md
§2.5; lane CAPTCHA-BYPASS, part of #2058; BGD, 2026-10-23: "a QA target may carry
a challenge bypass its owner configures, and only the gate applies it, only on
the target's own origins").

What these tests hold:

  * the spec, `challenge_bypass: {kind: header | cookie, name}`, refuses a
    reserved header name (`Authorization`, `Cookie`, `Host`, `Proxy-*`) and a
    bypass on a target with no host; a valid one is stored as `{kind, name}`;
  * ONLY A WORKSPACE ADMIN sets the value, on `PUT .../challenge-bypass`; a
    member is 403 and nothing is written;
  * the value is stored where the credential is, the tenant's one QA secret,
    in the target's own entry beside the username and password (neither write
    drops the other), and the target records only
    `challenge_bypass_value: {set, set_at, set_by, version}`, audited;
  * THE VALUE IS NEVER SERVED: no response, log line or Firestore write
    carries it (only the worker's attempt route answers it, as it answers the
    password: test_qa_target_bypass_attempt.py);
  * removing the bypass, or changing the host, removes the value from the
    secret, so it never reaches a host its owner did not configure it for.

Every value is built at run time with `secrets.token_hex`, never one literal.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

import pytest

from swarm_api import qaspec

from .conftest import auth_header
from .test_qa_target_credential import FakeQaCredentials, mint, put, secret_value, token_of
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL,
    CAROL_TENANT,
    TARGETS,
    api_context,
    register,
    resolver,
    seeded,
)

HEADER = {"kind": "header", "name": "X-QA-Bypass"}
OWN_LOGIN = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me"}


@pytest.fixture
def store(client) -> FakeQaCredentials:
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    return fake


def bypass_value() -> str:
    """A bypass value made at run time, never one literal."""
    return "qa-bypass-" + secrets.token_hex(16)


def put_bypass(client, target_id: str, value: str, user: str = "carol"):
    return client.put(f"{TARGETS}/{target_id}/challenge-bypass", headers=auth_header(user),
                      json={"value": value})


def bypass_rows(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d["action"] == "qa_target_challenge_bypass_set"]


# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Authorization", "cookie", "HOST", "Proxy-Authorization", "proxy-anything"])
def test_a_reserved_header_name_is_refused(client, db, seeded, store, name):
    response = register(client, challenge_bypass={"kind": "header", "name": name})
    # MUTATION: drop the reserved-name check in `qaspec.challenge_bypass` and these are 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "challenge_bypass"
    assert not [p for p in db.docs if p.startswith("qa_targets/")]


@pytest.mark.parametrize("raw", [
    {"kind": "header", "name": "X-QA Bypass"},       # a space is no header name
    {"kind": "header", "name": "X-QA-Bypass:"},      # nor is a colon
    {"kind": "header", "name": ""},
    {"kind": "query", "name": "bypass"},             # only header or cookie
    {"kind": "header", "name": "X-QA-Bypass", "value": "never here"},  # the value is the PUT's
    "X-QA-Bypass",
])
def test_a_malformed_bypass_is_refused(raw):
    target = {"base_url": "https://shop.example.com", "challenge_bypass": raw}
    # MUTATION: accept any mapping in `qaspec.challenge_bypass` and these pass.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.challenge_bypass(target)


@pytest.mark.parametrize("base_url", [None, "", "https:///nohost", "https://10.0.0.1"])
def test_a_bypass_on_a_target_with_no_host_is_refused(base_url):
    target = {"challenge_bypass": dict(HEADER)}
    if base_url is not None:
        target["base_url"] = base_url
    # MUTATION: skip the host check in `qaspec.challenge_bypass` and a bypass with nowhere to go passes.
    with pytest.raises(qaspec.QaSpecError) as refused:
        qaspec.challenge_bypass(target)
    assert "host" in refused.value.message


def test_a_valid_bypass_is_stored_as_its_kind_and_name_and_its_value_is_not_set(client, db, seeded, store):
    response = register(client, challenge_bypass={"kind": "cookie", "name": "qa_bypass"})
    assert response.status_code == 201, response.text
    served = response.json()["qa_target"]
    # MUTATION: drop "challenge_bypass" from qatargets.SPEC_FIELDS and it is neither stored nor served.
    assert served["challenge_bypass"] == {"kind": "cookie", "name": "qa_bypass"}
    assert served["challenge_bypass_value"] == {"set": False, "set_at": None, "set_by": None, "version": None}
    assert qaspec.challenge_bypass({"base_url": "https://shop.example.com",
                                    "challenge_bypass": dict(HEADER)}) == HEADER
    assert qaspec.challenge_bypass({"base_url": "https://shop.example.com"}) is None


# --------------------------------------------------------------------------
# The value: an admin sets it, nobody reads it
# --------------------------------------------------------------------------


def test_only_a_workspace_admin_sets_the_value(client, db, seeded, store):
    created = register(client, "root", repository="example-org/widgets", challenge_bypass=HEADER)
    target_id = created.json()["qa_target"]["id"]
    value = bypass_value()
    # MUTATION: drop `require_workspace_admin` from the challenge-bypass PUT and this is 200.
    assert put_bypass(client, target_id, value, "alice").status_code == 403
    assert store.writes == [] and bypass_rows(db) == []
    assert put_bypass(client, target_id, value, "root").status_code == 200
    assert store.values["eng"][target_id]["challenge_bypass"] == value


def test_a_target_without_a_bypass_takes_no_value(client, db, seeded, store):
    target_id = register(client).json()["qa_target"]["id"]
    response = put_bypass(client, target_id, bypass_value())
    # MUTATION: skip the spec check in `set_challenge_bypass` and this is 200.
    assert response.status_code == 422, response.text
    assert store.writes == []


@pytest.mark.parametrize("value", ["short", "has a space " + "x" * 16, "semi;colon" + "x" * 16,
                                   "line\nbreak" + "x" * 16])
def test_a_value_that_no_header_or_cookie_can_carry_is_refused(client, db, seeded, store, value):
    target_id = register(client, challenge_bypass=HEADER).json()["qa_target"]["id"]
    response = put_bypass(client, target_id, value)
    # MUTATION: drop the value pattern in `set_challenge_bypass` and these are 200.
    assert response.status_code == 422, response.text
    assert store.writes == []


def test_the_value_is_stored_beside_the_credential_and_neither_write_drops_the_other(client, db, seeded, store):
    target_id = register(client, sign_in=OWN_LOGIN, challenge_bypass=HEADER).json()["qa_target"]["id"]
    username, password = secret_value()
    value = bypass_value()
    assert put(client, target_id, token_of(mint(client, target_id)), username, password).status_code == 200
    response = put_bypass(client, target_id, value)
    assert response.status_code == 200, response.text
    # MUTATION: overwrite the target's entry in `set_challenge_bypass` and the password is gone.
    assert store.values[CAROL_TENANT][target_id] == {"username": username, "password": password,
                                                     "challenge_bypass": value}
    username2, password2 = secret_value()
    assert put(client, target_id, token_of(mint(client, target_id)), username2, password2).status_code == 200
    # MUTATION: overwrite the target's entry in `set_credential` and the bypass is gone.
    assert store.values[CAROL_TENANT][target_id]["challenge_bypass"] == value
    status = response.json()["challenge_bypass_value"]
    assert status["set"] is True and status["set_by"] == CAROL and status["version"].endswith("/versions/2")
    rows = bypass_rows(db)
    # MUTATION: drop the `_audit` call from `set_challenge_bypass` and this is [].
    # `via_link` is False: this PUT sent no link, which the switch lets through
    # while off (SSC-LINKS; test_qa_target_bypass_link.py).
    assert len(rows) == 1 and rows[0]["detail"] == {"version": status["version"], "kind": "header",
                                                    "name": "X-QA-Bypass", "via_link": False}


def test_the_value_is_in_no_response_log_line_or_document(client, db, seeded, store, caplog):
    caplog.set_level(logging.DEBUG)
    target_id = register(client, challenge_bypass=HEADER).json()["qa_target"]["id"]
    value = bypass_value()
    bodies = [put_bypass(client, target_id, value),
              client.get(f"{TARGETS}/{target_id}", headers=auth_header("carol")),
              client.get(TARGETS, headers=auth_header("carol")),
              # A refused write must not echo its body either.
              client.put(f"{TARGETS}/{target_id}/challenge-bypass", headers=auth_header("carol"),
                         json={"value": value, "extra": True})]
    assert [b.status_code for b in bodies] == [200, 200, 200, 422]
    assert store.values[CAROL_TENANT][target_id]["challenge_bypass"] == value
    for b in bodies[1:3]:
        doc = b.json().get("qa_target") or b.json()["qa_targets"][0]
        # MUTATION: serve the stored record without `bypass_status` and this has more keys.
        assert set(doc["challenge_bypass_value"]) == {"set", "set_at", "set_by", "version"}
        assert doc["challenge_bypass_value"]["set"] is True
    # MUTATION: return the stored entry from `set_challenge_bypass` and this fails.
    assert not any(value in b.text for b in bodies)
    # MUTATION: log the body in `set_challenge_bypass` and this fails.
    assert value not in caplog.text
    # MUTATION: write the value onto the target document or its audit row and this fails.
    assert value not in json.dumps(db.docs, default=str)


def test_removing_the_bypass_or_changing_the_host_removes_its_value(client, db, seeded, store, resolver):
    target_id = register(client, challenge_bypass=HEADER).json()["qa_target"]["id"]
    assert put_bypass(client, target_id, bypass_value()).status_code == 200
    moved = client.patch(f"{TARGETS}/{target_id}", headers=auth_header("carol"),
                         json={"base_url": "https://new.example.com"})
    assert moved.status_code == 200, moved.text
    # MUTATION: keep the value on a host change in `patch` and it is sent to a host nobody set it for.
    assert "challenge_bypass" not in store.values[CAROL_TENANT].get(target_id, {})
    assert moved.json()["qa_target"]["challenge_bypass_value"]["set"] is False
    assert put_bypass(client, target_id, bypass_value()).status_code == 200
    cleared = client.patch(f"{TARGETS}/{target_id}", headers=auth_header("carol"),
                           json={"challenge_bypass": None})
    assert cleared.status_code == 200, cleared.text
    # MUTATION: keep the value when the bypass is removed in `patch` and it outlives its spec.
    assert "challenge_bypass" not in store.values[CAROL_TENANT].get(target_id, {})
    assert "challenge_bypass" not in cleared.json()["qa_target"]


def test_deleting_a_target_with_only_a_bypass_removes_its_entry(client, db, seeded, store):
    target_id = register(client, challenge_bypass=HEADER).json()["qa_target"]["id"]
    assert put_bypass(client, target_id, bypass_value()).status_code == 200
    assert client.delete(f"{TARGETS}/{target_id}", headers=auth_header("carol")).status_code == 204
    # MUTATION: remove the secret's entry only when a credential is set in `delete` and this fails.
    assert target_id not in store.values[CAROL_TENANT]


# --------------------------------------------------------------------------
# Out of every capture (lane BYPASS-UNCAPTURED, part of #2058)
# --------------------------------------------------------------------------


def test_the_bypass_route_is_never_captured():
    """The PUT's body is the bypass value itself, so it is named beside the
    credential's in `main.UNCAPTURED_ROUTES`: a body capture added to the app
    later must skip it (BGD, 2026-10-23)."""
    from swarm_api.main import UNCAPTURED_ROUTES
    from swarm_api.routes.qatargets import BYPASS_ROUTE, CREDENTIAL_ROUTE

    # MUTATION: drop qatargets.BYPASS_ROUTE from main.UNCAPTURED_ROUTES and this fails.
    assert BYPASS_ROUTE in UNCAPTURED_ROUTES
    # MUTATION: drop qatargets.CREDENTIAL_ROUTE from main.UNCAPTURED_ROUTES and this fails.
    assert CREDENTIAL_ROUTE in UNCAPTURED_ROUTES
