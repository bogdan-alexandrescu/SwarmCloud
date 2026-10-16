"""The service's ticket verifier against swarm-api's reference
(docs/design/graph-service.md §3.2-§3.3; lane GS-SVC, part of #1384).

`swarm_api.graph_ticket` mints the ticket and holds the reference verifier;
`swarm_graph.ticket` verifies it with its own code (the service's image carries
no swarm-api). These hold the two to one wire, as test_typesafe_fwd_ticket.py
holds the TypeSafe pair: the domain, the claim set and the longest life are
equal, a ticket the reference mints verifies here with the same claims, and
every ticket the reference refuses is refused here too. Then §3.3 step 2: a
ticket MAC'd with the right key whose claims are not the exact shapes a path
is built from is refused before any path exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from graphsvc_fixtures import REPO_ID, TENANT, mint, new_key

from swarm_api import graph_ticket as reference
from swarm_graph import ticket as svc

RAW = b'{"a manifest": 1}'


def test_the_wire_constants_are_the_references():
    assert svc.TICKET_DOMAIN == reference.TICKET_DOMAIN
    assert svc.TICKET_FIELDS == reference.TICKET_FIELDS
    assert svc.MAX_TICKET_SECONDS == reference.MAX_TICKET_SECONDS


def test_a_reference_ticket_verifies_with_the_same_claims():
    key = new_key()
    ticket = mint(key, raw=RAW)
    now = time.time()
    claims = reference.verify_graph_ticket([key], ticket, now=now)
    got = svc.verify_graph_ticket([key.encode()], ticket, now=now)
    assert {name: getattr(got, name) for name in svc.TICKET_FIELDS - {"nonce"}} == {
        name: claims[name] for name in svc.TICKET_FIELDS - {"nonce"}}
    assert got.tenant_id == TENANT and got.repo_id == REPO_ID


def test_the_mac_key_is_the_secret_payload_stripped():
    key = new_key()
    assert svc.mac_key(f"  {key}\n".encode()) == key.encode()
    # swarm-api strips SWARM_GRAPH_TICKET_KEY the same way, so a payload with
    # a trailing newline mints tickets this service verifies.
    ticket = mint(key, raw=RAW)
    assert svc.verify_graph_ticket([svc.mac_key(f"{key}\n".encode())], ticket, now=time.time())


@pytest.mark.parametrize("case", ["forged", "other_key", "expired", "too_long", "malformed"])
def test_what_the_reference_refuses_is_refused_here(case):
    key = new_key()
    now = time.time()
    ticket = {
        "forged": mint(key, raw=RAW)[:-2] + "AA",
        "other_key": mint(new_key(), raw=RAW),
        "expired": mint(key, raw=RAW, exp=int(now) - 1),
        "too_long": mint(key, raw=RAW, exp=int(now) + 3600),
        "malformed": "not-a-ticket",
    }[case]
    with pytest.raises(reference.TicketRefused):
        reference.verify_graph_ticket([key], ticket, now=now)
    with pytest.raises(svc.TicketRefused) as refused:
        svc.verify_graph_ticket([key.encode()], ticket, now=now)
    assert refused.value.reason == ("expired" if case == "expired" else "invalid")
    assert ticket not in str(refused.value)


def _forge(key: str, claims: dict) -> str:
    """A ticket with any claims, MAC'd with the right key: what only a key
    holder could make, so the shape checks are the last line."""
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    payload = b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    mac = hmac.new(key.encode(), svc.TICKET_DOMAIN + payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{b64(mac)}"


GOOD = {"tenant_id": TENANT, "repo_id": REPO_ID, "task_id": "t-1", "attempt_id": "a-1",
        "generation": 1, "index_sha": "a" * 40, "manifest_digest": "sha256:" + "b" * 64,
        "nonce": "0123456789abcdef"}


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "../other"), ("tenant_id", "eng/repos/x"), ("tenant_id", ""),
    ("tenant_id", "Eng"), ("repo_id", "repo_../x"), ("repo_id", "repo_0123"),
    ("index_sha", "a" * 39), ("index_sha", "../" + "a" * 37),
    ("manifest_digest", "b" * 64), ("manifest_digest", "sha256:" + "B" * 64),
    ("nonce", "zz"), ("task_id", ""), ("attempt_id", 7), ("generation", -1),
    ("generation", True),
])
def test_a_maced_ticket_with_a_misshapen_claim_is_refused_before_any_path(field, value):
    key = new_key()
    claims = dict(GOOD, exp=int(time.time()) + 300, **{field: value})
    good = _forge(key, dict(GOOD, exp=int(time.time()) + 300))
    assert svc.verify_graph_ticket([key.encode()], good, now=time.time()).tenant_id == TENANT
    with pytest.raises(svc.TicketRefused) as refused:
        svc.verify_graph_ticket([key.encode()], _forge(key, claims), now=time.time())
    assert refused.value.reason == "invalid"


def test_an_extra_or_missing_field_is_refused():
    key = new_key()
    exp = int(time.time()) + 300
    with pytest.raises(svc.TicketRefused):
        svc.verify_graph_ticket([key.encode()], _forge(key, dict(GOOD, exp=exp, path="x")),
                                now=time.time())
    short = {k: v for k, v in GOOD.items() if k != "repo_id"}
    with pytest.raises(svc.TicketRefused):
        svc.verify_graph_ticket([key.encode()], _forge(key, dict(short, exp=exp)),
                                now=time.time())


def test_the_previous_key_verifies_for_a_tickets_life_after_a_rotation():
    now = [1000.0]
    old, new = new_key(), new_key()
    ring = svc.KeyRing(old.encode(), clock=lambda: now[0])
    assert ring.rotate(new.encode())
    assert not ring.rotate(new.encode())
    assert ring.keys() == [new.encode(), old.encode()]
    now[0] += svc.PREVIOUS_KEY_SECONDS - 1
    assert old.encode() in ring.keys()
    now[0] += 2
    assert ring.keys() == [new.encode()]
    assert "redacted" in repr(ring) and new not in repr(ring)


@pytest.mark.parametrize("header,expected", [
    (None, None), ("", None), ("Basic abc", None), ("Bearer ", None),
    ("Bearer abc.def", "abc.def"), ("bearer  abc.def ", "abc.def"),
])
def test_the_bearer_header(header, expected):
    assert svc.bearer(header) == expected
