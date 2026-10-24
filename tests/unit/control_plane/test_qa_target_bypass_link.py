"""A QA target's challenge bypass value arrives through a one-time link
(docs/design/secrets-scopes.md §6.2, §11 Q8; lane SSC-LINKS, part of #2158).

BGD, 2026-10-23 (Q8): a value arrives only through the console form a
one-time link opens, or a terminal's --stdin; no route, tool or log takes or
prints one. The credential link gains `field`; the bypass PUT takes the
link's token as the credential's PUT does.

What these tests hold:

  * `POST .../credential-links {field: "challenge_bypass"}` mints a link to
    the bypass form, answered as the credential's (URL, path, expiry), and
    only for an entry that has a `challenge_bypass`; any other field is 422;
  * the link sets the bypass once; a credential link never sets the bypass
    and a bypass link never sets the credential, and neither is burned by
    being presented for the other field;
  * a PUT WITHOUT a link token is 403 `qa_bypass_link_required` once the
    switch is on, nothing written; the switch SHIPS OFF, and off the write
    goes through and is logged without the value;
  * a token that is presented and not this caller's live bypass link is 403
    `qa_credential_link_invalid` whatever the switch says;
  * a link document minted before `field` existed is a credential link.

Every value is built at run time with `secrets.token_hex`, never one literal.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import qatargets, refusals
from swarm_api.qatargets import QaTargets

from .conftest import auth_header
from .test_qa_target_credential import FakeQaCredentials, put, secret_value, token_of
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
SWITCH = refusals.env_name("qa_bypass_link_required")


@pytest.fixture
def store(client) -> FakeQaCredentials:
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    return fake


@pytest.fixture
def target(client, seeded, store) -> dict[str, Any]:
    response = register(client, sign_in=OWN_LOGIN, challenge_bypass=HEADER)
    assert response.status_code == 201, response.text
    return response.json()["qa_target"]


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


@pytest.fixture
def report_only(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


def bypass_value() -> str:
    """A bypass value made at run time, never one literal."""
    return "qa-bypass-" + secrets.token_hex(16)


def mint(client, target_id: str, field: str | None = None, user: str = "carol"):
    body = {} if field is None else {"field": field}
    return client.post(f"{TARGETS}/{target_id}/credential-links", headers=auth_header(user), json=body)


def put_bypass(client, target_id: str, value: str, link_token: str | None = None, user: str = "carol"):
    body: dict[str, Any] = {"value": value}
    if link_token is not None:
        body["link_token"] = link_token
    return client.put(f"{TARGETS}/{target_id}/challenge-bypass", headers=auth_header(user), json=body)


def links(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{qatargets.LINKS_COLLECTION}/")]


def bypass_set(store: FakeQaCredentials, target_id: str) -> str | None:
    return store.values.get(CAROL_TENANT, {}).get(target_id, {}).get("challenge_bypass")


# --------------------------------------------------------------------------
# The link names its field
# --------------------------------------------------------------------------


def test_a_bypass_link_answers_as_the_credentials_and_opens_the_bypass_form(client, db, target):
    response = mint(client, target["id"], "challenge_bypass")
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"url", "path", "expires_at"}
    # MUTATION: keep the credential form's path for a bypass link and this fails.
    assert response.json()["url"].startswith(
        f"https://console.example.com/qa-targets/{target['id']}/challenge-bypass?t=")
    (link,) = links(db)
    assert link["field"] == "challenge_bypass" and link["email"] == CAROL
    assert token_of(response) not in repr(link)


def test_a_link_with_no_field_is_a_credential_link(client, db, target):
    response = client.post(f"{TARGETS}/{target['id']}/credential-links", headers=auth_header("carol"))
    assert response.status_code == 201, response.text
    assert "/credential?t=" in response.json()["url"]
    (link,) = links(db)
    assert link["field"] == "credential"


def test_another_field_is_refused(client, db, target):
    # MUTATION: widen LinkBody.field to any string and this is 201.
    assert mint(client, target["id"], "password").status_code == 422
    assert links(db) == []


def test_a_target_without_a_bypass_gets_no_bypass_link(client, db, seeded, store):
    plain = register(client, sign_in=OWN_LOGIN).json()["qa_target"]
    response = mint(client, plain["id"], "challenge_bypass")
    # MUTATION: drop the `challenge_bypass` check in `mint_link` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "challenge_bypass"
    assert links(db) == []


def test_a_member_cannot_ask_for_a_bypass_link(client, db, seeded, store):
    eng = register(client, "root", repository="example-org/widgets", challenge_bypass=HEADER)
    target_id = eng.json()["qa_target"]["id"]
    assert mint(client, target_id, "challenge_bypass", "alice").status_code == 403
    assert mint(client, target_id, "challenge_bypass", "root").status_code == 201


# --------------------------------------------------------------------------
# The PUT consumes it
# --------------------------------------------------------------------------


def test_a_bypass_link_sets_the_value_once(client, db, target, store, enforced):
    token = token_of(mint(client, target["id"], "challenge_bypass"))
    value = bypass_value()
    response = put_bypass(client, target["id"], value, token)
    assert response.status_code == 200, response.text
    assert bypass_set(store, target["id"]) == value
    (link,) = links(db)
    assert link["used_at"] is not None
    rows = [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d["action"] == "qa_target_challenge_bypass_set"]
    assert rows[-1]["detail"]["via_link"] is True
    # MUTATION: skip the `used_at` check in `consume_link` and this is 200.
    replay = put_bypass(client, target["id"], bypass_value(), token)
    assert replay.status_code == 403
    assert replay.json()["code"] == "qa_credential_link_invalid"
    assert bypass_set(store, target["id"]) == value


def test_a_credential_link_never_sets_the_bypass_and_is_not_burned(client, db, target, store):
    token = token_of(mint(client, target["id"]))
    # MUTATION: drop `field` from the link comparison and this is 200.
    response = put_bypass(client, target["id"], bypass_value(), token)
    assert response.status_code == 403
    assert response.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []
    username, password = secret_value()
    assert put(client, target["id"], token, username, password).status_code == 200


def test_a_bypass_link_never_sets_the_credential_and_is_not_burned(client, db, target, store):
    token = token_of(mint(client, target["id"], "challenge_bypass"))
    username, password = secret_value()
    # MUTATION: consume the credential's link with no field and this is 200.
    response = put(client, target["id"], token, username, password)
    assert response.status_code == 403
    assert response.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []
    assert put_bypass(client, target["id"], bypass_value(), token).status_code == 200


def test_a_presented_bad_token_is_refused_whatever_the_switch(client, db, target, store, report_only):
    made_up = secrets.token_urlsafe(32)
    response = put_bypass(client, target["id"], bypass_value(), made_up)
    assert response.status_code == 403
    assert response.json()["code"] == "qa_credential_link_invalid"
    assert made_up not in response.text
    assert store.writes == []


def test_a_mistyped_value_does_not_burn_the_link(client, db, target, store, enforced):
    token = token_of(mint(client, target["id"], "challenge_bypass"))
    # A space: no header or cookie carries it unquoted.
    assert put_bypass(client, target["id"], "has a space " + secrets.token_hex(8), token).status_code == 422
    (link,) = links(db)
    assert link["used_at"] is None
    assert put_bypass(client, target["id"], bypass_value(), token).status_code == 200


# --------------------------------------------------------------------------
# The switch: a PUT without a link
# --------------------------------------------------------------------------


def test_the_switch_ships_off():
    switch = refusals.SWITCHES["qa_bypass_link_required"]
    # MUTATION: register the switch with default=True and this fails.
    assert switch.default is False
    assert qatargets.QaBypassLinkRequired.code == switch.code


def test_a_put_without_a_link_is_refused_once_the_switch_is_on(client, db, target, store, enforced):
    value = bypass_value()
    response = put_bypass(client, target["id"], value)
    # MUTATION: drop the `refusals.refuse` call in `set_challenge_bypass` and this is 200.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_bypass_link_required"
    assert value not in response.text
    assert store.writes == [] and bypass_set(store, target["id"]) is None


def test_off_a_put_without_a_link_is_written_and_logged_without_the_value(client, db, target, store,
                                                                         report_only, caplog):
    caplog.set_level(logging.DEBUG)
    value = bypass_value()
    response = put_bypass(client, target["id"], value)
    assert response.status_code == 200, response.text
    assert bypass_set(store, target["id"]) == value
    # MUTATION: drop the bypass's log line and this fails.
    assert "refusal report-only code=qa_bypass_link_required" in caplog.text
    assert (f"qa target challenge bypass set without a link tenant={CAROL_TENANT} "
            f"target={target['id']}") in caplog.text
    assert value not in caplog.text
    rows = [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d["action"] == "qa_target_challenge_bypass_set"]
    assert rows[-1]["detail"]["via_link"] is False


# --------------------------------------------------------------------------
# A link minted before `field`
# --------------------------------------------------------------------------


def test_a_link_minted_before_field_existed_sets_only_the_credential(db, target):
    start = datetime.now(timezone.utc)
    targets = QaTargets(db, now=lambda: start)
    raw = secrets.token_urlsafe(16)
    db.docs[f"{qatargets.LINKS_COLLECTION}/{qatargets._link_digest(raw)}"] = {
        "email": CAROL, "tenant_id": CAROL_TENANT, "target_id": target["id"],
        "created_at": start, "expires_at": start + qatargets.LINK_TTL, "used_at": None}
    with pytest.raises(qatargets.QaCredentialLinkInvalid):
        targets.consume_link(raw, email=CAROL, tenant_id=CAROL_TENANT, target_id=target["id"],
                             field=qatargets.LINK_BYPASS)
    targets.consume_link(raw, email=CAROL, tenant_id=CAROL_TENANT, target_id=target["id"])
