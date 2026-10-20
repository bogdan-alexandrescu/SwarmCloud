"""swarm-api mints the attempt TypeSafe ticket (docs/design/workspace-secrets.md
§3.5, the `inject` delivery mode; lane SEC-INJECT-A, part of #1347).

`POST /v1/attempts/{attempt_id}/typesafe-ticket`, called by the worker with the
attempt key's signature under this route's OWN purpose, fenced exactly like the
egress ticket: the task is RUNNING under this attempt's live lease at this
generation and no cancel was requested. swarm-api decides WHICH key the
forwarder will spend -- the submitter's confirmed personal key, else the
tenant's, `provider_keys.resolve_provider_key`'s order -- and puts only that
secret's id and version in the ticket, never a value. It refuses (403, by
code) when the workspace's judgment backend is not TypeSafe or no key is set.

Drives the real route over the in-memory Firestore with the helpers of
test_live_api.py. Every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import refusals, typesafe_ticket
from swarm_api.settings import ApiSettings
from swarm_api.typesafe_ticket import (
    MAX_TICKET_SECONDS,
    TICKET_DOMAIN,
    TICKET_FIELDS,
    TicketRefused,
    mint_typesafe_ticket,
    typesafe_ticket_message,
    verify_typesafe_ticket,
)
from swarm_common.models import Tenant

from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    TASK,
    WORKER_TOKEN,
    Attempt,
    _b64url,
    clock,
    groups,
    ldb,
    make_client,
    people,
    relay,
    seed_running,
)
from .test_egress_ticket_api import sign as egress_sign

#: Built at runtime, never one literal (CLAUDE.md).
FWD_KEY = "-".join(("typesafe", "fwd", "key")) + "-" + secrets.token_hex(16)
PATH = f"/v1/attempts/{ATTEMPT}/typesafe-ticket"
TENANT_SECRET = "swarm-tenant-eng-typesafe"
ALICE_SECRET = "swarm-tenant-u-alice-typesafe"

SWITCHES = ("typesafe_fenced", "typesafe_unproven", "typesafe_unauthenticated",
            "typesafe_backend_off", "typesafe_no_key")


def sign(attempt: Attempt, body: bytes, *, path: str = PATH) -> dict[str, str]:
    stamp = str(int(time.time()))
    sig = attempt.private.sign(typesafe_ticket_message("POST", path, body, stamp))
    return {"X-Swarm-Attempt-Proof": _b64url(sig), "X-Swarm-Attempt-Timestamp": stamp}


def ask(client, attempt: Attempt, *, signed: bool = True, **over: Any):
    body = attempt.body(**over)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    if signed:
        headers.update(sign(attempt, body))
    return client.post(PATH, content=body, headers=headers)


def claims(ticket: str) -> dict[str, Any]:
    payload = ticket.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def set_key(db, secret_id: str, *, version: int = 3, tenant_id: str = "eng") -> None:
    db.docs[f"provider_keys/{secret_id}"] = {
        "tenant_id": tenant_id, "provider": "typesafe", "set": True,
        "version": f"projects/local/secrets/{secret_id}/versions/{version}",
        "set_by": "root@example.com", "set_at": datetime.now(timezone.utc),
    }


def choose_typesafe(db, tenant_id: str = "eng") -> None:
    db.docs[f"workspace_settings/{tenant_id}"] = {"tenant_id": tenant_id,
                                                   "judgment_backend": "typesafe"}


def personal_tenant(db, tenant_id: str, principal: str) -> None:
    db.docs[f"tenants/{tenant_id}"] = asdict(Tenant(
        tenant_id=tenant_id, kind="user", principal=principal,
        created_at=datetime.now(timezone.utc), display_name=tenant_id,
        service_account=f"swarm-agent-worker-{tenant_id}@p.iam.gserviceaccount.com",
        namespace=f"swarm-tenant-{tenant_id}",
    ))


@pytest.fixture(autouse=True)
def refusals_on(request, monkeypatch):
    """A NEW refusal ships off (swarm_api.refusals). The tests that assert this
    route's codes turn their switches on; `test_with_the_switches_off_*` does not."""
    if request.node.name.startswith("test_with_the_switches_off"):
        return
    for code in SWITCHES:
        monkeypatch.setenv(refusals.env_name(code), "on")


@pytest.fixture
def make(ldb, people, groups, objects, clock, relay):
    def _make(**settings: Any):
        return make_client(ldb, people, groups, objects, clock, relay, **settings)
    return _make


def _ready(make, ldb, **settings: Any):
    """A RUNNING attempt in `eng`, which chose TypeSafe and has a tenant key."""
    client = make(typesafe_fwd_key=FWD_KEY, **settings)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt)
    choose_typesafe(ldb)
    set_key(ldb, TENANT_SECRET)
    return client, attempt


@pytest.fixture
def ready(make, ldb):
    return _ready(make, ldb)


# -- the wire ----------------------------------------------------------------


def test_the_ticket_is_macd_under_its_own_domain_and_verifies_with_the_previous_key():
    old = "-".join(("old", "fwd")) + secrets.token_hex(16)
    exp = int(time.time()) + 300
    ticket = mint_typesafe_ticket(old, tenant_id="eng", task_id=TASK, attempt_id=ATTEMPT,
                                  generation=1, scope="tenant", secret_id=TENANT_SECRET,
                                  secret_version="3", exp=exp)
    # A key ring that still holds the previous key verifies a ticket minted
    # just before a rotation, for that ticket's life.
    got = verify_typesafe_ticket([FWD_KEY, old], ticket, now=time.time())
    assert set(got) == TICKET_FIELDS
    assert TICKET_DOMAIN == b"swarm-typesafe-ticket/v1\0"
    with pytest.raises(TicketRefused):
        verify_typesafe_ticket([FWD_KEY], ticket, now=time.time())
    with pytest.raises(TicketRefused):
        verify_typesafe_ticket([old], ticket, now=exp + 1)


def test_a_forged_or_swapped_payload_does_not_verify():
    exp = int(time.time()) + 300
    ticket = mint_typesafe_ticket(FWD_KEY, tenant_id="eng", task_id=TASK, attempt_id=ATTEMPT,
                                  generation=1, scope="tenant", secret_id=TENANT_SECRET,
                                  secret_version="3", exp=exp)
    payload, mac = ticket.split(".")
    forged = dict(claims(ticket), secret_id="swarm-tenant-research-typesafe")
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    swapped = base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + mac
    for bad in (swapped, payload + "." + _b64url(b"\0" * 32), payload, "", None, 7):
        with pytest.raises(TicketRefused):
            verify_typesafe_ticket([FWD_KEY], bad, now=time.time())


def test_an_egress_ticket_key_and_domain_never_make_a_typesafe_ticket():
    from swarm_api.egress import EGRESS_TICKET_DOMAIN, mint_egress_ticket

    assert EGRESS_TICKET_DOMAIN != TICKET_DOMAIN
    egress = mint_egress_ticket(FWD_KEY, tenant_id="eng", task_id=TASK, attempt_id=ATTEMPT,
                                generation=1, exp=int(time.time()) + 300)
    with pytest.raises(TicketRefused):
        verify_typesafe_ticket([FWD_KEY], egress, now=time.time())


# -- minting ------------------------------------------------------------------


def test_no_forwarder_key_is_a_503_and_never_a_ticket(make, ldb):
    assert ApiSettings.__dataclass_fields__["typesafe_fwd_key"].default == ""
    client = make()
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    choose_typesafe(ldb)
    set_key(ldb, TENANT_SECRET)
    response = ask(client, attempt)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "typesafe_unavailable"
    assert "ticket" not in response.json()


def test_the_tenant_key_is_named_by_id_and_version_and_never_carried(ready):
    client, attempt = ready
    response = ask(client, attempt)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"ticket", "expires_in", "scope"}
    assert body["scope"] == "tenant"
    assert 60 <= body["expires_in"] <= MAX_TICKET_SECONDS
    got = verify_typesafe_ticket([FWD_KEY], body["ticket"], now=time.time())
    assert set(got) == TICKET_FIELDS
    assert (got["tenant_id"], got["task_id"], got["attempt_id"], got["generation"]) == (
        "eng", TASK, ATTEMPT, 2)
    assert (got["scope"], got["secret_id"], got["secret_version"]) == ("tenant", TENANT_SECRET, "3")
    assert got["exp"] <= int(time.time()) + body["expires_in"] + 1
    assert FWD_KEY not in response.text


def test_a_group_task_never_spends_the_submitters_own_key(ready, ldb):
    """Q3: a personal key reaches a group workspace's task only by per-secret
    opt-in, which this key does not have, so the group's own key is named."""
    client, attempt = ready
    personal_tenant(ldb, "u-alice", "alice@example.com")
    set_key(ldb, ALICE_SECRET, version=7, tenant_id="u-alice")
    response = ask(client, attempt)
    assert response.status_code == 201, response.text
    got = claims(response.json()["ticket"])
    assert (got["scope"], got["secret_id"]) == ("tenant", TENANT_SECRET)
    assert ALICE_SECRET not in response.text
    assert got["tenant_id"] == "eng"


def test_a_group_task_with_only_the_submitters_key_is_refused(ready, ldb):
    client, attempt = ready
    del ldb.docs[f"provider_keys/{TENANT_SECRET}"]
    personal_tenant(ldb, "u-alice", "alice@example.com")
    set_key(ldb, ALICE_SECRET, version=7, tenant_id="u-alice")
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "typesafe_no_key"


def test_a_namesakes_personal_key_is_never_spent(ready, ldb):
    """`u-alice` names alice@partner.com, not the submitter alice@example.com:
    the frozen slug drops the domain, and only the principal says whose it is."""
    client, attempt = ready
    personal_tenant(ldb, "u-alice", "alice@partner.com")
    set_key(ldb, ALICE_SECRET, tenant_id="u-alice")
    got = claims(ask(client, attempt).json()["ticket"])
    assert (got["scope"], got["secret_id"]) == ("tenant", TENANT_SECRET)


def test_another_tenants_key_is_never_named(ready, ldb):
    client, attempt = ready
    del ldb.docs[f"provider_keys/{TENANT_SECRET}"]
    set_key(ldb, "swarm-tenant-research-typesafe", tenant_id="research")
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "typesafe_no_key"
    assert "ticket" not in response.json()


def test_no_key_set_is_refused_and_nothing_is_minted(ready, ldb):
    client, attempt = ready
    ldb.docs[f"provider_keys/{TENANT_SECRET}"]["set"] = False
    response = ask(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "typesafe_no_key"
    assert "ticket" not in response.json()


@pytest.mark.parametrize("backend", [None, "internal-jev", "internal-laya", "nonsense"])
def test_a_workspace_that_did_not_choose_typesafe_is_refused(ready, ldb, backend):
    client, attempt = ready
    if backend is None:
        del ldb.docs["workspace_settings/eng"]
    else:
        ldb.docs["workspace_settings/eng"]["judgment_backend"] = backend
    response = ask(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "typesafe_backend_off"
    assert "ticket" not in response.json()


def _cut_over(make, ldb):
    """eng's settings are verified (ApiSettings.settings_mac_tenants) under a
    runtime key (ApiSettings.settings_key): the key, and a ready attempt."""
    key = "settings-" + secrets.token_hex(24)
    return key, _ready(make, ldb, settings_key=key, settings_mac_tenants=("eng",))


@pytest.mark.parametrize("mac", [None, "bad", "other-tenant"])
def test_a_forged_typesafe_choice_mints_nothing_once_cut_over(make, ldb, mac):
    # #1457: any tenant's worker can write workspace_settings/eng directly.
    from swarm_api import judgment

    key, (client, attempt) = _cut_over(make, ldb)
    stored = ldb.docs["workspace_settings/eng"]
    assert stored["judgment_backend"] == "typesafe" and "mac" not in stored
    if mac == "bad":
        stored["mac"] = {"v": 1, "key": judgment.key_id(key),
                         "sig": base64.b64encode(secrets.token_bytes(32)).decode()}
    elif mac == "other-tenant":
        # A MAC swarm-api really made, for research's identical choice.
        stored["mac"] = judgment.sign_block(key, tenant_id="research", block="top",
                                            fields={"judgment_backend": "typesafe"})
    response = ask(client, attempt)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "typesafe_backend_off"
    assert "ticket" not in response.json()


def test_a_signed_typesafe_choice_still_mints_once_cut_over(make, ldb):
    from swarm_api import judgment

    key, (client, attempt) = _cut_over(make, ldb)
    ldb.docs["workspace_settings/eng"]["mac"] = judgment.sign_block(
        key, tenant_id="eng", block="top", fields={"judgment_backend": "typesafe"})
    response = ask(client, attempt)
    assert response.status_code == 201, response.text
    assert verify_typesafe_ticket([FWD_KEY], response.json()["ticket"], now=time.time())


def test_before_the_cutover_an_unsigned_choice_mints_as_it_did(make, ldb):
    client, attempt = _ready(make, ldb, settings_key="settings-" + secrets.token_hex(24))
    assert ask(client, attempt).status_code == 201


def test_the_cutover_in_the_environment_alone_does_not_reach_the_ticket(ready, monkeypatch):
    # ISO-API-SETTINGS: the cutover is ApiSettings', read once at start; a
    # variable set on a process whose settings do not list eng changes nothing.
    from swarm_api import judgment

    monkeypatch.setenv(judgment.SETTINGS_KEY_ENV, "settings-" + secrets.token_hex(24))
    monkeypatch.setenv(judgment.SETTINGS_MAC_TENANTS_ENV, "eng")
    client, attempt = ready
    assert ask(client, attempt).status_code == 201


def test_a_stale_generation_is_fenced_and_nothing_is_minted(ready):
    client, attempt = ready
    response = ask(client, attempt, generation=1)
    assert response.status_code in (403, 409), response.text
    assert "ticket" not in response.json()


def test_a_task_that_is_not_running_is_fenced(ready, ldb):
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["state"] = "PARKED"
    response = ask(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "typesafe_fenced"


def test_a_cancel_requested_is_fenced(ready, ldb):
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    response = ask(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "typesafe_fenced"


def test_without_the_attempt_proof_nothing_is_minted(ready):
    client, attempt = ready
    response = ask(client, attempt, signed=False)
    assert response.status_code == 403
    assert response.json()["code"] == "typesafe_unproven"
    assert "ticket" not in response.json()


def test_another_purposes_signature_is_not_this_routes(ready):
    """A live-session or an egress-ticket signature over the same body and
    path never verifies here."""
    client, attempt = ready
    body = attempt.body()
    for headers in (attempt.sign(PATH, body), egress_sign(attempt, body, path=PATH)):
        response = client.post(PATH, content=body, headers={
            "Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json",
            **headers})
        assert response.status_code == 403, response.text
        assert "ticket" not in response.json()


def test_a_wrong_key_signature_is_refused(ready):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    client, attempt = ready
    body = attempt.body()
    stamp = str(int(time.time()))
    sig = Ed25519PrivateKey.generate().sign(typesafe_ticket_message("POST", PATH, body, stamp))
    response = client.post(PATH, content=body, headers={
        "Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json",
        "X-Swarm-Attempt-Proof": _b64url(sig), "X-Swarm-Attempt-Timestamp": stamp})
    assert response.status_code == 403
    assert "ticket" not in response.json()


def test_another_tenants_worker_is_refused(ready):
    client, attempt = ready
    body = attempt.body(tenant_id="research")
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}
    headers.update(sign(attempt, body))
    response = client.post(PATH, content=body, headers=headers)
    assert response.status_code == 403
    assert "ticket" not in response.json()


def test_the_body_must_name_the_paths_attempt(ready):
    client, attempt = ready
    response = ask(client, attempt, attempt_id="att_other")
    assert response.status_code == 422
    assert "ticket" not in response.json()


def test_the_ticket_life_is_clamped(make, ldb):
    client = make(typesafe_fwd_key=FWD_KEY, typesafe_ticket_seconds=86400)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    choose_typesafe(ldb)
    set_key(ldb, TENANT_SECRET)
    response = ask(client, attempt)
    assert response.status_code == 201
    assert response.json()["expires_in"] == MAX_TICKET_SECONDS


def test_no_log_line_carries_the_ticket_or_the_mac_key(ready, caplog):
    client, attempt = ready
    with caplog.at_level(logging.DEBUG):
        response = ask(client, attempt)
    assert response.status_code == 201
    ticket = response.json()["ticket"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    for part in (ticket, *ticket.split("."), FWD_KEY):
        assert part not in text


def test_swarm_api_reads_no_secret_value_to_mint():
    """The ticket names a secret; swarm-api never reads one for it."""
    source = inspect.getsource(typesafe_ticket)
    for call in ("access_secret_version", "SecretManagerServiceClient", ".access("):
        assert call not in source


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("SWARM_TYPESAFE_FWD_KEY", FWD_KEY)
    monkeypatch.setenv("TYPESAFE_TICKET_SECONDS", "240")
    settings = ApiSettings.from_env()
    assert settings.typesafe_fwd_key == FWD_KEY
    assert settings.typesafe_ticket_seconds == 240
    assert FWD_KEY not in repr(settings)


def test_every_refusal_has_a_switch_that_ships_off():
    for code in SWITCHES:
        assert code in refusals.SWITCHES
        assert refusals.SWITCHES[code].default is False


def test_with_the_switches_off_a_wrong_backend_is_the_established_forbidden(ready, ldb):
    client, attempt = ready
    del ldb.docs["workspace_settings/eng"]
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert "ticket" not in response.json()


def test_with_the_switches_off_no_key_is_the_established_forbidden(ready, ldb):
    client, attempt = ready
    del ldb.docs[f"provider_keys/{TENANT_SECRET}"]
    response = ask(client, attempt)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert "ticket" not in response.json()


def test_with_the_switches_off_a_fenced_attempt_is_the_established_conflict(ready, ldb):
    client, attempt = ready
    ldb.docs[f"tasks/{TASK}"]["state"] = "PARKED"
    response = ask(client, attempt)
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_with_the_switches_off_a_ticket_is_still_minted_when_everything_holds(ready):
    client, attempt = ready
    assert ask(client, attempt).status_code == 201


def test_only_this_secrets_numbered_version_is_pinned(ready, ldb):
    """A status record whose version names another secret, or no number, is
    never copied into a ticket: the forwarder reads `latest` of the SAME
    secret instead."""
    client, attempt = ready
    record = ldb.docs[f"provider_keys/{TENANT_SECRET}"]
    for stored in ("projects/local/secrets/swarm-tenant-research-typesafe/versions/9",
                   f"projects/local/secrets/{TENANT_SECRET}/versions/latest",
                   f"projects/local/secrets/{TENANT_SECRET}/versions/9/../1", None):
        record["version"] = stored
        got = claims(ask(client, attempt).json()["ticket"])
        assert (got["secret_id"], got["secret_version"]) == (TENANT_SECRET, "latest"), stored
