"""The gate holds a leg of four approved origins: each one is the target's,
a fifth host is not (the ledger's 'QA target extra origins' item).

swarm-api serves a leg's `origins` as exactly what a platform admin approved:
the base origin, the login's `sign_in.extra_origin` when named, then each
named `extra_origins` entry, at most four in all (`MAX_ORIGINS`, unchanged).
The gate treats every served origin as the target's and nothing else; the
IdP token path rides on the login's origin, which swarm-api serves first
after the base.

Pure: `QaTarget`, no Chromium, no network, no cloud credential.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from agent_worker.livegate import qatarget as gate_qt

ORIGIN = "https://app.example.com"
IDP = "https://sso.example.net"
API = "https://api.example.com"
FILES = "https://files.example.com"
FIFTH = "https://pay.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"


def _args(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target_id": "qat_" + secrets.token_hex(8), "base_url": f"{ORIGIN}/home",
        "origins": [ORIGIN, IDP, API, FILES], "environment": "non_production",
        "rate": {"navigation_ms": 1000, "requests_per_s": 10}, "read_posts": [],
        "exclusions": {"paths": []}, "mutating": True,
        "sign_in": {"kind": "own_login", "login_url": f"{ORIGIN}/login", "signed_in_selector": "#me"},
        # Built at runtime: nothing here is a literal credential.
        "credential": {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                       "password": "pw-" + secrets.token_hex(12)},
    }
    args.update(over)
    return args


def _target(**over: Any) -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda e, f: None, wall=lambda: 1_700_000_000.0)
    target.handle(_args(**over))
    return target


def test_a_leg_of_four_origins_allows_each_and_refuses_a_fifth_host():
    target = _target()
    assert gate_qt.MAX_ORIGINS == 4
    for origin in (ORIGIN, IDP, API, FILES):
        # MUTATION: keep only the base and one extra origin as the target's and the API host is refused.
        assert target.judge("POST", f"{origin}/v1/items", top_document=False).action == "continue", origin
    # MUTATION: treat any https origin as the target's and the fifth host is written to.
    assert target.judge("POST", f"{FIFTH}/v1/items", top_document=False).action == "refuse"


def test_a_hand_off_of_five_origins_is_refused():
    target = gate_qt.QaTarget(lambda e, f: None)
    # MUTATION: raise MAX_ORIGINS and a fifth origin is taken.
    with pytest.raises(gate_qt.QaTargetRefused) as caught:
        target.handle(_args(origins=[ORIGIN, IDP, API, FILES, FIFTH]))
    assert caught.value.code == "origins"


def test_the_token_path_rides_on_the_login_origin_beside_other_extra_origins():
    target = _target(idp_token_path=TOKEN_PATH, mutating=False, environment="production")
    # MUTATION: refuse a token path when more than one extra origin is approved and this hand-off is refused.
    assert target.idp_token_url == f"{IDP}{TOKEN_PATH}"
    target.signing_in = True
    target.finished(gate_qt.STATE_SIGNED_IN)
    assert target.judge("POST", f"{IDP}{TOKEN_PATH}", top_document=False).action == "continue"
    # MUTATION: put the path on every extra origin and the API host's same path is written to.
    assert target.judge("POST", f"{API}{TOKEN_PATH}", top_document=False).action == "refuse"
