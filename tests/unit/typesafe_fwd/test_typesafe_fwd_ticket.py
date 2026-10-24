"""The forwarder's ticket verifier and setting against swarm-api's reference
(docs/design/workspace-secrets.md §3.5; lane SEC-INJECT-B, part of #1347).

`swarm_api.typesafe_ticket` mints the ticket; `swarm_typesafe_fwd.ticket`
verifies it with its own code (the forwarder's image carries no swarm-api).
These hold the two to one wire: the domain, the claim set and the longest
life are equal, a ticket the reference mints verifies here with the same
claims, and every ticket the reference refuses is refused here too.

The setting (`config.json` in `swarm-typesafe-fwd-config`) fails closed: a
missing, unreadable or non-JSON file, an unknown or missing key, or a value of
the wrong type is the switch OFF. Its keys are terraform's (typesafe_fwd.tf),
read from that file.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

import pytest

from swarm_api import typesafe_ticket as reference
from swarm_typesafe_fwd import ticket as fwd
from swarm_typesafe_fwd.config import CONFIG_KEYS, load_config, parse_config

FWD_TF = Path(__file__).resolve().parents[3] / "terraform" / "infra" / "typesafe_fwd.tf"


def _key() -> str:
    return "k" + secrets.token_hex(24)


def _mint(key: str, **overrides) -> str:
    claims = {"tenant_id": "acme", "task_id": "t-1", "attempt_id": "a-1", "generation": 4,
              "scope": "tenant", "secret_id": "swarm-tenant-acme-typesafe",
              "secret_version": "latest", "exp": int(time.time()) + 300}
    claims.update(overrides)
    return reference.mint_typesafe_ticket(key, **claims)


def _forge(ticket: str) -> str:
    # Change the MAC's first character, which carries six whole bits: its last
    # carries bits base64url decoding ignores, so overwriting the tail with "AA"
    # left about one ticket in a thousand verifying as itself (#2137's red run).
    payload, mac = ticket.split(".")
    return f"{payload}.{'B' if mac[0] == 'A' else 'A'}{mac[1:]}"


def test_the_wire_constants_are_the_references():
    assert fwd.TICKET_DOMAIN == reference.TICKET_DOMAIN
    assert fwd.TICKET_FIELDS == reference.TICKET_FIELDS
    assert fwd.MAX_TICKET_SECONDS == reference.MAX_TICKET_SECONDS


def test_a_reference_ticket_verifies_with_the_same_claims():
    key = _key()
    ticket = _mint(key)
    now = time.time()
    claims = reference.verify_typesafe_ticket([key], ticket, now=now)
    got = fwd.verify_ticket([key.encode()], ticket, now=now)
    assert (got.tenant_id, got.task_id, got.attempt_id, got.generation, got.scope,
            got.secret_id, got.secret_version, got.exp) == (
        claims["tenant_id"], claims["task_id"], claims["attempt_id"], claims["generation"],
        claims["scope"], claims["secret_id"], claims["secret_version"], claims["exp"])


def test_the_mac_key_is_the_secret_payload_stripped():
    key = _key()
    assert fwd.mac_key(f"  {key}\n".encode()) == key.encode()


@pytest.mark.parametrize("case", ["forged", "other_key", "expired", "too_long", "malformed"])
def test_what_the_reference_refuses_is_refused_here(case):
    key = _key()
    now = time.time()
    ticket = {
        "forged": _forge(_mint(key)),
        "other_key": _mint(_key()),
        "expired": _mint(key, exp=int(now) - 1),
        "too_long": _mint(key, exp=int(now) + 3600),
        "malformed": "not-a-ticket",
    }[case]
    with pytest.raises(reference.TicketRefused):
        reference.verify_typesafe_ticket([key], ticket, now=now)
    with pytest.raises(fwd.TicketRefused) as refused:
        fwd.verify_ticket([key.encode()], ticket, now=now)
    assert ticket not in str(refused.value)
    assert key not in str(refused.value)


@pytest.mark.parametrize("overrides", [
    {"generation": True},
    {"generation": -1},
    {"generation": "4"},
    {"scope": "group"},
    {"secret_id": "swarm-tenant-other-typesafe"},
    {"secret_id": "swarm-tenant-acme-git"},
    {"secret_id": "swarm-tenant-acme-typesafe/versions/1"},
    {"scope": "user", "secret_id": "swarm-tenant-acme-typesafe"},
    {"scope": "user", "secret_id": "swarm-tenant-u-Pat_X-typesafe"},
    {"secret_version": "0x1"},
    {"secret_version": ""},
    {"secret_version": "LATEST"},
    {"tenant_id": "../acme"},
])
def test_defence_in_depth_behind_the_mac(overrides):
    """Each of these carries a GOOD MAC: the forwarder still refuses it."""
    key = _key()
    ticket = _mint(key, **overrides)
    with pytest.raises(fwd.TicketRefused):
        fwd.verify_ticket([key.encode()], ticket, now=time.time())


def test_a_user_scope_ticket_naming_a_personal_typesafe_key_verifies():
    key = _key()
    ticket = _mint(key, tenant_id="u-pat", scope="user",
                   secret_id="swarm-tenant-u-pat-typesafe", secret_version="7")
    got = fwd.verify_ticket([key.encode()], ticket, now=time.time())
    assert (got.scope, got.secret_id, got.secret_version) == (
        "user", "swarm-tenant-u-pat-typesafe", "7")


def test_an_empty_ring_verifies_nothing():
    key = _key()
    with pytest.raises(fwd.TicketRefused):
        fwd.verify_ticket([], _mint(key), now=time.time())
    with pytest.raises(fwd.TicketRefused):
        fwd.verify_ticket([b""], _mint(key), now=time.time())


# ---------------------------------------------------------------------------
# the setting
# ---------------------------------------------------------------------------
GOOD = {"enabled": True, "tenants": ["acme"], "requests_per_minute": 60,
        "max_request_bytes": 2097152, "max_response_seconds": 120}


def test_the_keys_are_terraforms():
    block = re.search(r"typesafe_fwd_config = \{\n(.*?)\n  \}", FWD_TF.read_text(), re.S)
    assert block, f"no typesafe_fwd_config in {FWD_TF}"
    rendered = tuple(re.findall(r"^\s+(\w+)\s+=", block.group(1), re.M))
    assert tuple(CONFIG_KEYS) == rendered


def test_a_good_setting_parses():
    config = parse_config(GOOD)
    assert config.enabled and config.cause == "enabled"
    assert config.tenants == frozenset({"acme"})
    assert (config.requests_per_minute, config.max_request_bytes, config.max_response_seconds) \
        == (60, 2097152, 120)


@pytest.mark.parametrize("change", [
    {"extra": 1},
    {"enabled": "true"},
    {"enabled": 1},
    {"tenants": "acme"},
    {"tenants": [1]},
    {"requests_per_minute": 0},
    {"requests_per_minute": True},
    {"max_request_bytes": 1.5},
    {"max_response_seconds": -1},
])
def test_an_invalid_setting_fails_closed(tmp_path, change):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**GOOD, **change}))
    config = load_config(path)
    assert not config.enabled and config.cause == "config_invalid"


@pytest.mark.parametrize("missing", CONFIG_KEYS)
def test_a_missing_key_fails_closed(tmp_path, missing):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({k: v for k, v in GOOD.items() if k != missing}))
    assert load_config(path).cause == "config_invalid"


def test_an_unreadable_or_non_json_setting_fails_closed(tmp_path):
    assert load_config(tmp_path / "absent.json").cause == "config_unreadable"
    (tmp_path / "bad.json").write_text("{not json")
    assert load_config(tmp_path / "bad.json").cause == "config_invalid"


def test_a_good_file_is_on(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(GOOD))
    assert load_config(tmp_path / "config.json").enabled
