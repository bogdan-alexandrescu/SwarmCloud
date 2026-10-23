"""The worker's read of a QA target's challenge bypass (docs/design/visual-qa-any-app.md
§2.5; lane CAPTCHA-BYPASS, part of #2058).

The value travels with the target exactly as the test account's password does:
`POST /v1/attempts/{attempt_id}/qa-target` reads it from the tenant's one QA
secret at that moment and answers it beside the sign-in, for a public target
too; the `live_audit` row names the bypass's kind and never its value; a
bypass whose value is not set is not served at all. Over the real app, the
in-memory Firestore and a real attempt key (test_qa_target_attempt_route's
harness). Every value is built at run time.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from .test_live_api import (  # noqa: F401 - fixtures are used by name
    clock,
    groups,
    ldb,
    people,
    relay,
)
from .test_qa_target_attempt_route import (  # noqa: F401 - fixtures are used by name
    TARGET,
    ask,
    client,
    issued_rows,
    running,
    seed_target,
    stamp,
)
from .test_qa_target_credential import FakeQaCredentials, secret_value

HEADER = {"kind": "header", "name": "X-QA-Bypass"}


def bypass_value() -> str:
    """A bypass value made at run time, never one literal."""
    return "qa-bypass-" + secrets.token_hex(16)


def _with_bypass(db: Any, *, own_login: bool, value_set: bool = True) -> None:
    doc = seed_target(db, own_login=own_login)
    doc["challenge_bypass"] = dict(HEADER)
    doc["challenge_bypass_value"] = {"set": value_set, "set_at": None, "set_by": "root@example.com",
                                     "version": "projects/p/secrets/swarm-tenant-eng-qa/versions/2"}


def test_the_attempt_route_serves_the_bypass_beside_the_credential(client, ldb, running, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    attempt, doc = running
    _with_bypass(ldb, own_login=True)
    stamp(ldb, doc)
    username, password = secret_value()
    value = bypass_value()
    fake.values["eng"] = {TARGET: {"username": username, "password": password, "challenge_bypass": value}}
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: leave the value out of `QaTargetIssuer.issue` and the gate has nothing to add.
    assert body["challenge_bypass"] == {"kind": "header", "name": "X-QA-Bypass", "value": value}
    assert body["sign_in"]["password"] == password
    (row,) = issued_rows(ldb)
    assert row["detail"]["challenge_bypass"] == "header"
    # MUTATION: put the answer in the audit row or a log line and this fails.
    assert value not in json.dumps(ldb.docs, default=str) and value not in caplog.text


def test_a_public_target_with_a_bypass_is_served_it_and_one_without_a_value_is_not(client, ldb, running):
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    attempt, doc = running
    _with_bypass(ldb, own_login=False)
    stamp(ldb, doc)
    value = bypass_value()
    fake.values["eng"] = {TARGET: {"challenge_bypass": value}}
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    # MUTATION: read the secret only for an own login in `issue` and a public target gets no bypass.
    assert response.json()["challenge_bypass"]["value"] == value
    assert response.json()["sign_in"] == {"kind": "none"}
    ldb.docs[f"qa_targets/{TARGET}"]["challenge_bypass_value"]["set"] = False
    unset = ask(client, attempt)
    assert unset.status_code == 200, unset.text
    # MUTATION: serve the bypass whatever its value's status and the gate is handed half of one.
    assert "challenge_bypass" not in unset.json()
