"""A credential or challenge-bypass write takes its entry from the link
(#2280, lane QA-CRED-ENV; docs/design/qa-environments.md §1.4).

What these tests hold, over the real app and the in-memory Firestore:

  * on a target of several entries, a PUT whose body names no `environment`
    stores on the entry the link was minted for, read by the token's digest,
    and on no other entry;
  * a body naming another entry than the link's is 403
    `qa_credential_link_invalid`, nothing is written and the link still works;
  * a link that is used, unknown, or another person's is 403 on such a
    target, never the "name the one to use" 422 that tells the console the
    link is still good;
  * the challenge bypass's PUT takes its entry from its link the same way.

Every fake credential is built at run time.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from swarm_api import qatargets

from .conftest import auth_header
from .test_qa_target_credential import (  # noqa: F401 - fixtures are used by name
    OWN_LOGIN,
    secret_value,
    store,
)
from .test_qa_target_environments import entry_of, leg, put, register_legs, token_of
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL_TENANT,
    TARGETS,
    api_context,
    resolver,
    seeded,
)

HEADER = {"kind": "header", "name": "X-QA-Bypass"}


def three_legs() -> list[dict[str, Any]]:
    return [
        leg("a", "https://app.example.com", environment="non_production", sign_in=OWN_LOGIN),
        leg("b", "https://shop.example.com", sign_in=OWN_LOGIN, challenge_bypass=HEADER),
        leg("c", "https://new.example.com", sign_in=OWN_LOGIN),
    ]


def mint(client, target_id: str, environment: str, field: str = "credential", user: str = "carol"):
    return client.post(f"{TARGETS}/{target_id}/credential-links", headers=auth_header(user),
                       json={"environment": environment, "field": field})


def put_bypass(client, target_id: str, value: str, token: str, environment: str | None = None):
    body: dict[str, Any] = {"value": value, "link_token": token}
    if environment is not None:
        body["environment"] = environment
    return client.put(f"{TARGETS}/{target_id}/challenge-bypass", headers=auth_header("carol"), json=body)


def links(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{qatargets.LINKS_COLLECTION}/")]


def three_entry_target(client) -> dict[str, Any]:
    response = register_legs(client, three_legs())
    assert response.status_code == 201, response.text
    return response.json()["qa_target"]


def test_a_put_naming_no_entry_stores_on_the_entry_its_link_was_minted_for(client, db, seeded, store):
    target = three_entry_target(client)
    token = token_of(mint(client, target["id"], "b"))
    username, password = secret_value()
    response = put(client, target["id"], token, username, password)
    # MUTATION: call `_entry_of(current, environment)` in `set_credential` again and this is 422.
    assert response.status_code == 200, response.text
    assert set(store.values[CAROL_TENANT]) == {f"{target['id']}/b"}
    assert store.values[CAROL_TENANT][f"{target['id']}/b"]["username"] == username
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert [entry_of(shown, n)["credential"]["set"] for n in ("a", "b", "c")] == [False, True, False]
    (link,) = links(db)
    assert link["used_at"] is not None
    assert password not in json.dumps(shown) and password not in json.dumps(db.docs, default=str)


def test_a_body_naming_another_entry_is_refused_and_the_link_still_works(client, db, seeded, store):
    target = three_entry_target(client)
    token = token_of(mint(client, target["id"], "b"))
    username, password = secret_value()
    refused = put(client, target["id"], token, username, password, environment="c")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []
    (link,) = links(db)
    assert link["used_at"] is None
    accepted = put(client, target["id"], token, username, password)
    assert accepted.status_code == 200, accepted.text
    assert set(store.values[CAROL_TENANT]) == {f"{target['id']}/b"}


def test_a_dead_link_on_a_target_of_several_entries_is_the_links_refusal(client, db, seeded, store):
    target = three_entry_target(client)
    token = token_of(mint(client, target["id"], "b"))
    username, password = secret_value()
    assert put(client, target["id"], token, username, password).status_code == 200
    # Used once: the second PUT is the link's 403, not "name the one to use".
    # MUTATION: fall back to `_entry_of(current, None)` for a dead link and this is 422.
    again = put(client, target["id"], token, username, password)
    assert again.status_code == 403, again.text
    assert again.json()["code"] == "qa_credential_link_invalid"
    unknown = put(client, target["id"], secrets.token_urlsafe(32), username, password)
    assert unknown.status_code == 403 and unknown.json()["code"] == "qa_credential_link_invalid"
    assert len(store.writes) == 1


def test_another_persons_link_is_refused_and_not_burned(client, db, seeded, store):
    target = three_entry_target(client)
    token = token_of(mint(client, target["id"], "b"))
    username, password = secret_value()
    for doc in links(db):
        doc["email"] = "someone-else@example.com"
    # MUTATION: drop the email from `_live_link`'s comparison and the lookup
    # answers b; `consume_link` still refuses, so this holds the lookup's own 403.
    refused = put(client, target["id"], token, username, password)
    assert refused.status_code == 403 and refused.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []
    assert all(doc["used_at"] is None for doc in links(db))


def test_a_credential_link_cannot_name_the_entry_of_a_bypass_write(client, db, seeded, store):
    target = three_entry_target(client)
    token = token_of(mint(client, target["id"], "b"))
    refused = put_bypass(client, target["id"], "qa-bypass-" + secrets.token_hex(16), token)
    assert refused.status_code == 403 and refused.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []


def test_a_bypass_put_naming_no_entry_stores_on_its_links_entry(client, db, seeded, store):
    target = three_entry_target(client)
    minted = mint(client, target["id"], "b", field="challenge_bypass")
    assert minted.status_code == 201, minted.text
    assert "&environment=b" in minted.json()["path"]
    value = "qa-bypass-" + secrets.token_hex(16)
    # A value of the wrong shape is refused before the link is checked: not burned.
    assert put_bypass(client, target["id"], "x", token_of(minted)).status_code == 422
    response = put_bypass(client, target["id"], value, token_of(minted))
    # MUTATION: call `_entry_of(current, environment)` in `set_challenge_bypass` again and this is 422.
    assert response.status_code == 200, response.text
    assert store.values[CAROL_TENANT] == {f"{target['id']}/b": {"challenge_bypass": value}}
    assert value not in json.dumps(db.docs, default=str)
