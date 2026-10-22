"""A QA target's test credential (docs/design/visual-qa-any-app.md §2.1, §7;
VQ-Q2; lane VQ-2 TARGETS, part of #1933).

What these tests hold:

  * a workspace admin asks for a one-time link, and the answer is ONLY its URL
    and its expiry; a member cannot ask;
  * the link is single use, bound to the email that asked for it and to its
    target, and expires after 15 minutes; a link presented by anyone else, for
    another target, or late, is 403 `qa_credential_link_invalid` and is not
    burned;
  * the value, built at run time with `secrets.token_hex`, appears in no
    response, no log line and no Firestore write: the target records only
    `credential: {set, set_at, set_by, version}`;
  * it is stored in the tenant's one secret, `swarm-tenant-<tenant>-qa`, as a
    map by target id; the older version is disabled; nothing here creates the
    secret or touches its IAM policy, so no worker account is ever bound;
  * until lane VQ-3 declares the secret, a read or write is 503
    `qa_credential_unavailable`;
  * deleting a target removes its entry from the map first.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from swarm_api import qatargets
from swarm_api.main import UNCAPTURED_ROUTES
from swarm_api.qatargets import QaCredentialUnavailable, QaTargets, SecretManagerQaCredentials

from .conftest import auth_header
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL,
    CAROL_TENANT,
    TARGETS,
    api_context,
    approve,
    register,
    resolver,
    seeded,
)

OWN_LOGIN = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me"}


class FakeQaCredentials:
    """The tenant's QA secret, in memory: a map per tenant, every write kept."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def read(self, tenant_id: str) -> dict[str, Any]:
        if self.fail:
            raise QaCredentialUnavailable(qatargets.UNAVAILABLE_MESSAGE)
        return json.loads(json.dumps(self.values.get(tenant_id, {})))

    def write(self, tenant_id: str, values: dict[str, Any]) -> str:
        if self.fail:
            raise QaCredentialUnavailable(qatargets.UNAVAILABLE_MESSAGE)
        self.values[tenant_id] = json.loads(json.dumps(values))
        self.writes.append((tenant_id, self.values[tenant_id]))
        return (f"projects/p/secrets/swarm-tenant-{tenant_id}-qa/versions/{len(self.writes)}")


@pytest.fixture
def store(client) -> FakeQaCredentials:
    fake = FakeQaCredentials()
    client.app.state.qa_credential_store = fake
    return fake


@pytest.fixture
def target(client, seeded, store) -> dict[str, Any]:
    response = register(client, sign_in=OWN_LOGIN)
    assert response.status_code == 201, response.text
    return response.json()["qa_target"]


def secret_value() -> tuple[str, str]:
    """A username and a password made at run time, never one literal."""
    return "qa-" + secrets.token_hex(4), secrets.token_hex(16)


def mint(client, target_id: str, user: str = "carol"):
    return client.post(f"{TARGETS}/{target_id}/credential-links", headers=auth_header(user))


def token_of(response) -> str:
    return response.json()["url"].split("t=", 1)[1]


def put(client, target_id: str, token: str, username: str, password: str, user: str = "carol"):
    return client.put(f"{TARGETS}/{target_id}/credential", headers=auth_header(user),
                      json={"username": username, "password": password, "link_token": token})


# --------------------------------------------------------------------------
# The link
# --------------------------------------------------------------------------


def test_a_link_answers_only_its_url_and_expiry(client, db, target):
    response = mint(client, target["id"])
    assert response.status_code == 201, response.text
    # MUTATION: return the link document from `mint_link` and this has more keys.
    assert set(response.json()) == {"url", "path", "expires_at"}
    assert response.json()["url"].startswith(f"https://console.example.com/qa-targets/{target['id']}/credential?t=")
    token = token_of(response)
    links = {p: d for p, d in db.docs.items() if p.startswith("qa_target_credential_links/")}
    # The token is never stored: only its digest names the document.
    assert len(links) == 1 and token not in json.dumps(links, default=str)
    (link,) = links.values()
    assert link["email"] == CAROL and link["target_id"] == target["id"]
    # MUTATION: make LINK_TTL ten minutes and this fails.
    assert link["expires_at"] - link["created_at"] == timedelta(minutes=15)


def test_a_member_cannot_ask_for_a_link(client, db, seeded, store):
    eng_target = register(client, "root", repository="example-org/widgets", sign_in=OWN_LOGIN)
    target_id = eng_target.json()["qa_target"]["id"]
    # MUTATION: drop `require_workspace_admin` from `mint_credential_link` and this is 201.
    assert mint(client, target_id, "alice").status_code == 403
    assert mint(client, target_id, "root").status_code == 201


def test_a_link_sets_the_credential_and_records_only_its_status(client, db, target, store):
    username, password = secret_value()
    response = put(client, target["id"], token_of(mint(client, target["id"])), username, password)
    assert response.status_code == 200, response.text
    status = response.json()["credential"]
    assert status["set"] is True and status["set_by"] == CAROL
    assert status["version"].endswith("/versions/1")
    # Stored in the tenant's one secret, a map by target id (VQ-Q2).
    assert store.values == {CAROL_TENANT: {target["id"]: {"username": username, "password": password}}}
    stored = db.docs[f"qa_targets/{target['id']}"]["credential"]
    assert set(stored) == {"set", "set_at", "set_by", "version"}
    rows = [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d["action"] == "qa_target_credential_set"]
    # MUTATION: drop the `_audit` call from `set_credential` and this is [].
    assert len(rows) == 1 and rows[0]["detail"] == {"version": status["version"]}


def test_the_value_is_in_no_response_log_line_or_document(client, db, target, store, caplog):
    caplog.set_level(logging.DEBUG)
    username, password = secret_value()
    bodies = [mint(client, target["id"])]
    bodies.append(put(client, target["id"], token_of(bodies[0]), username, password))
    bodies.append(client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")))
    bodies.append(client.get(TARGETS, headers=auth_header("carol")))
    # A refused write (the link is spent) must not echo the body either.
    bodies.append(put(client, target["id"], token_of(bodies[0]), username, password))
    # A malformed one: the validation handler must not echo it.
    bodies.append(client.put(f"{TARGETS}/{target['id']}/credential", headers=auth_header("carol"),
                             json={"username": username, "password": password}))
    assert [b.status_code for b in bodies] == [201, 200, 200, 200, 403, 422]
    assert store.values[CAROL_TENANT][target["id"]]["password"] == password
    for value in (password, username):
        # MUTATION: return the stored entry from `set_credential` and this fails.
        assert not any(value in b.text for b in bodies)
        # MUTATION: log the body in `set_credential` and this fails.
        assert value not in caplog.text
        # MUTATION: write `values` onto the target document and this fails.
        assert value not in json.dumps(db.docs, default=str)


def test_the_credential_route_is_never_captured():
    # MUTATION: drop CREDENTIAL_ROUTE from main.UNCAPTURED_ROUTES and this fails.
    assert "/v1/qa-targets/{target_id}/credential" in UNCAPTURED_ROUTES


def test_a_password_is_stored_as_typed(client, target, store):
    username, password = secret_value()
    padded = f" {password} "
    assert put(client, target["id"], token_of(mint(client, target["id"])), username, padded).status_code == 200
    # MUTATION: make CredentialWrite a StrictModel, which strips, and this fails.
    assert store.values[CAROL_TENANT][target["id"]]["password"] == padded


def test_a_link_is_single_use(client, db, target, store):
    token = token_of(mint(client, target["id"]))
    assert put(client, target["id"], token, *secret_value()).status_code == 200
    response = put(client, target["id"], token, *secret_value())
    # MUTATION: skip the `used_at` check in `consume_link` and this is 200.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "qa_credential_link_invalid"
    assert len(store.writes) == 1


def test_a_write_without_a_link_is_refused(client, target, store):
    username, password = secret_value()
    response = client.put(f"{TARGETS}/{target['id']}/credential", headers=auth_header("carol"),
                          json={"username": username, "password": password})
    # MUTATION: make `link_token` optional in CredentialWrite and this is 200.
    assert response.status_code == 422, response.text
    assert store.writes == []


def test_a_link_for_another_target_is_refused_and_not_burned(client, db, target, store):
    other = register(client, name="Shop staging", sign_in=OWN_LOGIN).json()["qa_target"]
    token = token_of(mint(client, target["id"]))
    response = put(client, other["id"], token, *secret_value())
    # MUTATION: drop `target_id` from the link comparison and this is 200.
    assert response.status_code == 403, response.text
    assert store.writes == []
    # Not burned: its own target still takes it.
    assert put(client, target["id"], token, *secret_value()).status_code == 200


def _clocked(db: Any, start: datetime) -> tuple[QaTargets, list[datetime]]:
    moment = [start]
    return QaTargets(db, now=lambda: moment[0]), moment


def _seed_link(db: Any, target_id: str, start: datetime) -> str:
    """A link whose token the test holds: `mint_link` stores only its digest."""
    raw = secrets.token_urlsafe(16)
    db.docs[f"qa_target_credential_links/{qatargets._link_digest(raw)}"] = {
        "email": CAROL, "tenant_id": CAROL_TENANT, "target_id": target_id,
        "created_at": start, "expires_at": start + qatargets.LINK_TTL, "used_at": None}
    return raw


def test_a_link_is_bound_to_the_email_that_asked(db, target):
    start = datetime.now(timezone.utc)
    targets, _ = _clocked(db, start)
    raw = _seed_link(db, target["id"], start)
    # MUTATION: drop `email` from the link comparison and this does not raise.
    with pytest.raises(qatargets.QaCredentialLinkInvalid):
        targets.consume_link(raw, email="mallory@example.com", tenant_id=CAROL_TENANT,
                             target_id=target["id"])
    # Not burned by someone else presenting it.
    targets.consume_link(raw, email=CAROL, tenant_id=CAROL_TENANT, target_id=target["id"])


def test_a_link_expires_after_fifteen_minutes(db, target):
    start = datetime.now(timezone.utc)
    targets, moment = _clocked(db, start)
    raw = _seed_link(db, target["id"], start)
    moment[0] = start + timedelta(minutes=15, seconds=1)
    # MUTATION: skip the `expires_at` check in `consume_link` and this does not raise.
    with pytest.raises(qatargets.QaCredentialLinkInvalid):
        targets.consume_link(raw, email=CAROL, tenant_id=CAROL_TENANT, target_id=target["id"])
    # CONTROL: inside its life it is accepted.
    moment[0] = start + timedelta(minutes=14)
    targets.consume_link(raw, email=CAROL, tenant_id=CAROL_TENANT, target_id=target["id"])


def test_a_public_target_takes_no_credential(client, db, seeded, store):
    public = register(client).json()["qa_target"]
    response = put(client, public["id"], token_of(mint(client, public["id"])), *secret_value())
    assert response.status_code == 422, response.text
    assert store.writes == []


def test_an_unreachable_secret_is_503_and_records_nothing(client, db, target, store):
    store.fail = True
    response = put(client, target["id"], token_of(mint(client, target["id"])), *secret_value())
    # MUTATION: record the status before the write in `set_credential` and set is True.
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "qa_credential_unavailable"
    assert db.docs[f"qa_targets/{target['id']}"]["credential"]["set"] is False


def test_deleting_a_target_removes_its_credential_first(client, db, target, store):
    keep = register(client, name="Shop staging", sign_in=OWN_LOGIN).json()["qa_target"]
    for t in (target, keep):
        assert put(client, t["id"], token_of(mint(client, t["id"])), *secret_value()).status_code == 200
    assert client.delete(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).status_code == 204
    # MUTATION: drop the map rewrite from `QaTargets.delete` and the password outlives it.
    assert set(store.values[CAROL_TENANT]) == {keep["id"]}


def test_a_delete_that_cannot_remove_the_credential_deletes_nothing(client, db, target, store):
    put(client, target["id"], token_of(mint(client, target["id"])), *secret_value())
    store.fail = True
    response = client.delete(f"{TARGETS}/{target['id']}", headers=auth_header("carol"))
    assert response.status_code == 503, response.text
    assert f"qa_targets/{target['id']}" in db.docs


# --------------------------------------------------------------------------
# Secret Manager, as swarm-api's own account reaches it (V-1, V-2)
# --------------------------------------------------------------------------


class _Version:
    def __init__(self, name: str) -> None:
        self.name = name


class _Payload:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Access:
    def __init__(self, data: bytes) -> None:
        self.payload = _Payload(data)


class FakeSecretClient:
    """Only the four calls V-1 and V-2 allow and the disable. No create_secret
    and no IAM call exist on it: calling either is an AttributeError."""

    def __init__(self) -> None:
        self.versions: list[tuple[str, bytes, str]] = []
        self.refuse_access: Exception | None = None
        self.refuse_disable = False
        self.disabled: list[str] = []

    def access_secret_version(self, request: dict[str, Any]) -> _Access:
        if self.refuse_access is not None:
            raise self.refuse_access
        enabled = [v for v in self.versions if v[2] == "ENABLED"]
        if not enabled:
            raise gexc.NotFound("no versions")
        return _Access(enabled[-1][1])

    def add_secret_version(self, request: dict[str, Any]) -> _Version:
        name = f"{request['parent']}/versions/{len(self.versions) + 1}"
        self.versions.append((name, bytes(request["payload"].data), "ENABLED"))
        return _Version(name)

    def list_secret_versions(self, request: dict[str, Any]) -> list[_Version]:
        return [_Version(n) for n, _, state in self.versions if state == "ENABLED"]

    def disable_secret_version(self, request: dict[str, Any]) -> None:
        if self.refuse_disable:
            raise gexc.PermissionDenied("secretVersionAdder cannot disable")
        self.disabled.append(request["name"])
        self.versions = [(n, d, "DISABLED" if n == request["name"] else s) for n, d, s in self.versions]


def test_secret_manager_writes_the_tenants_one_secret_and_disables_the_older_version():
    client = FakeSecretClient()
    sm = SecretManagerQaCredentials("proj", client=client)
    # Declared and empty (lane VQ-3): no map yet.
    assert sm.read("eng") == {}
    _, first = secret_value()
    _, second = secret_value()
    v1 = sm.write("eng", {"qat_a": {"username": "u", "password": first}})
    v2 = sm.write("eng", {"qat_a": {"username": "u", "password": second}})
    # VQ-Q2: one secret per tenant, named by the frozen spelling.
    assert v1 == "projects/proj/secrets/swarm-tenant-eng-qa/versions/1"
    # MUTATION: drop `_disable_older` and the first version stays enabled.
    assert client.disabled == [v1]
    assert v2.endswith("/versions/2")
    assert sm.read("eng") == {"qat_a": {"username": "u", "password": second}}


def test_a_refused_disable_is_logged_and_not_the_writes_failure(caplog):
    client = FakeSecretClient()
    client.refuse_disable = True
    sm = SecretManagerQaCredentials("proj", client=client)
    sm.write("eng", {})
    _, password = secret_value()
    # MUTATION: let the PermissionDenied from the disable escape and this raises.
    sm.write("eng", {"qat_a": {"username": "u", "password": password}})
    assert "older versions not disabled" in caplog.text
    assert password not in caplog.text


@pytest.mark.parametrize("refusal", [gexc.PermissionDenied("no V-2"), gexc.ServiceUnavailable("down")])
def test_an_unreadable_secret_is_qa_credential_unavailable(refusal):
    client = FakeSecretClient()
    client.refuse_access = refusal
    # MUTATION: read every GoogleAPICallError as "no map" and this returns {}.
    with pytest.raises(QaCredentialUnavailable):
        SecretManagerQaCredentials("proj", client=client).read("eng")


def test_an_undeclared_secret_cannot_be_written():
    class Undeclared(FakeSecretClient):
        def add_secret_version(self, request: dict[str, Any]) -> _Version:
            raise gexc.NotFound("no such secret")

    # MUTATION: fall back to creating the secret and this succeeds -- and would
    # need a project-wide create that the owner did not grant (VQ-Q2).
    with pytest.raises(QaCredentialUnavailable):
        SecretManagerQaCredentials("proj", client=Undeclared()).write("eng", {})
