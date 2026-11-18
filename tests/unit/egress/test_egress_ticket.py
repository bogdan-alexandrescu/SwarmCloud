"""The attempt egress ticket (docs/browser-egress.md §2.2 hop 3, §3.1; lane EGRESS-SVC).

swarm-api mints it for one attempt of one task of one tenant, at one fencing
generation, with an expiry; swarm-egress verifies it on every CONNECT. These
hold the scoping: a ticket names exactly those fields, a changed field breaks
the MAC, an expired ticket and one claiming more life than the platform ever
mints are refused, and a key rotation does not refuse the tickets minted just
before it.

Every key is built at runtime (`secrets.token_bytes`): nothing here is a
credential.
"""

from __future__ import annotations

import base64
import json
import secrets

import pytest

from swarm_egress.ticket import (
    MAX_TICKET_SECONDS,
    TICKET_FIELDS,
    Ticket,
    TicketRefused,
    mint_ticket,
    verify_ticket,
)

NOW = 1_800_000_000.0


def _key() -> bytes:
    return secrets.token_hex(32).encode()


def _mint(key: bytes, **over) -> str:
    claims = dict(tenant_id="eng", task_id="task_1", attempt_id="att_1", generation=3,
                  exp=int(NOW) + 300)
    claims.update(over)
    return mint_ticket(key, **claims)


def _payload(ticket: str) -> dict:
    payload = ticket.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _forge(ticket: str, **over) -> str:
    """The same MAC over a payload with `over` changed: what an agent that
    edits a ticket it was handed would send."""
    body = _payload(ticket)
    body.update(over)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return payload + "." + ticket.split(".")[1]


def test_a_ticket_names_the_attempt_and_its_generation_and_nothing_else():
    key = _key()
    ticket = verify_ticket([key], _mint(key), now=NOW)
    assert ticket == Ticket(tenant_id="eng", task_id="task_1", attempt_id="att_1",
                            generation=3, exp=int(NOW) + 300)
    assert set(_payload(_mint(key))) == TICKET_FIELDS
    assert TICKET_FIELDS == {"tenant_id", "task_id", "attempt_id", "generation", "exp", "nonce"}


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "research"),
    ("task_id", "task_2"),
    ("attempt_id", "att_2"),
    ("generation", 4),
    ("exp", int(NOW) + 600),
])
def test_changing_any_scoped_field_breaks_the_mac(field, value):
    key = _key()
    with pytest.raises(TicketRefused) as refused:
        verify_ticket([key], _forge(_mint(key), **{field: value}), now=NOW)
    assert refused.value.cause == "bad_mac"


def test_an_expired_ticket_is_refused():
    key = _key()
    ticket = _mint(key, exp=int(NOW) + 60)
    assert verify_ticket([key], ticket, now=NOW + 59).attempt_id == "att_1"
    with pytest.raises(TicketRefused) as refused:
        verify_ticket([key], ticket, now=NOW + 60)
    assert refused.value.cause == "expired"


def test_a_ticket_claiming_more_life_than_the_platform_mints_is_refused():
    key = _key()
    with pytest.raises(TicketRefused) as refused:
        verify_ticket([key], _mint(key, exp=int(NOW) + MAX_TICKET_SECONDS + 120), now=NOW)
    assert refused.value.cause == "too_long"


def test_another_key_is_refused_and_the_previous_key_is_accepted_during_a_rotation():
    old, new = _key(), _key()
    ticket = _mint(old)
    with pytest.raises(TicketRefused):
        verify_ticket([new], ticket, now=NOW)
    assert verify_ticket([new, old], ticket, now=NOW).generation == 3


@pytest.mark.parametrize("raw", [None, "", "x", "a.b.c", "!!.!!", "a" * 5000])
def test_garbage_is_refused_without_an_exception_other_than_the_refusal(raw):
    with pytest.raises(TicketRefused):
        verify_ticket([_key()], raw, now=NOW)


def test_an_extra_or_missing_field_is_refused_even_under_a_good_mac():
    key = _key()
    from swarm_egress import ticket as module

    body = {"tenant_id": "eng", "task_id": "t", "attempt_id": "a", "generation": 1,
            "exp": int(NOW) + 60, "nonce": "n", "zone": "isp"}
    with pytest.raises(TicketRefused) as refused:
        verify_ticket([key], module._signed(key, body), now=NOW)
    assert refused.value.cause == "malformed"
    del body["zone"], body["nonce"]
    with pytest.raises(TicketRefused):
        verify_ticket([key], module._signed(key, body), now=NOW)


@pytest.mark.parametrize("generation", [True, -1, "3", 1.5])
def test_a_generation_that_is_not_a_non_negative_integer_is_refused(generation):
    key = _key()
    from swarm_egress import ticket as module

    body = {"tenant_id": "eng", "task_id": "t", "attempt_id": "a", "generation": generation,
            "exp": int(NOW) + 60, "nonce": "n"}
    with pytest.raises(TicketRefused):
        verify_ticket([key], module._signed(key, body), now=NOW)


def test_the_message_of_a_refusal_never_carries_the_ticket():
    key = _key()
    ticket = _mint(key)
    with pytest.raises(TicketRefused) as refused:
        verify_ticket([_key()], ticket, now=NOW)
    assert ticket not in str(refused.value)
    assert ticket.split(".")[1] not in repr(refused.value)


def test_swarm_api_mints_what_swarm_egress_verifies():
    """The two ends restate one wire (swarm-api cannot import swarm-egress);
    this holds them together, both ways round."""
    from swarm_api.egress import EGRESS_TICKET_DOMAIN, mint_egress_ticket
    from swarm_egress.ticket import TICKET_DOMAIN

    assert EGRESS_TICKET_DOMAIN == TICKET_DOMAIN
    key = secrets.token_hex(32)
    minted = mint_egress_ticket(key, tenant_id="eng", task_id="task_1", attempt_id="att_1",
                                generation=7, exp=int(NOW) + 300)
    ticket = verify_ticket([key.encode()], minted, now=NOW)
    assert (ticket.tenant_id, ticket.task_id, ticket.attempt_id, ticket.generation) == (
        "eng", "task_1", "att_1", 7)


def test_an_egress_ticket_is_never_a_live_ticket_and_the_reverse():
    """Two platform keys, two MAC domains: even under one key value, neither
    ticket verifies as the other."""
    from swarm_api.live import TICKET_PURPOSE, verify_ticket as verify_live
    from swarm_egress.ticket import TICKET_DOMAIN

    assert TICKET_DOMAIN != TICKET_PURPOSE
    key = secrets.token_hex(32)
    egress = _mint(key.encode())
    with pytest.raises(Exception):
        verify_live([key], egress, now=NOW)
