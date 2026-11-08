"""`sc accounts` and `swarm_accounts` show each account's pools, and
`sc account pool` moves an account between the workspace and Voice it pools
(voice-it-pool).

swarm-api serves `pools` (["workspace"], ["voice-it"] or both) and
`voice_it_holder` on `GET /v1/accounts`, and takes `PUT
/v1/accounts/{id}/pools` with `{"pools", "confirm_label"}`; it refuses a wrong
label or an empty set with 400 and a pool the account holds a lease of with
409 pool_in_use.
"""

from __future__ import annotations

import io
import json
import secrets
import sys
from datetime import datetime, timezone

import pytest

from swarm_mcp import render, sc, server
from swarm_mcp.client import SwarmError

NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
TENANT = "acme"
PERSON = "owner@example.com"


def _account(label: str, **extra) -> dict:
    base = {
        "account_id": f"{TENANT}:{label}",
        "owner_tenant": TENANT,
        "label": label,
        "provider": "openai",
        "state": "AVAILABLE",
        "assigned": 0,
        "windows": {},
        "observed_at": None,
        "stale": False,
    }
    base.update(extra)
    return base


def _pool() -> list[dict]:
    return [
        _account("agents", pools=["workspace"], voice_it_people=[], voice_it_holder=None),
        _account("talk", pools=["voice-it"], voice_it_people=[PERSON],
                 voice_it_holder={"voice_session_id": "vs_" + secrets.token_hex(4),
                                  "person": PERSON, "since": "2026-11-03T02:50:00+00:00"}),
        _account("both", pools=["voice-it", "workspace"], voice_it_people=[PERSON],
                 voice_it_holder=None),
    ]


class Api:
    """`GET /v1/accounts` and `PUT .../pools`; `refuse` is the SwarmError the
    pools route raises instead of answering."""

    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, refuse: SwarmError | None = None):
        self.accounts = _pool()
        self.refuse = refuse
        self.sent: list[tuple] = []

    def request(self, method, path, payload=None, **kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/accounts"):
            return {"tenant_id": TENANT, "accounts": [dict(a) for a in self.accounts]}
        if method == "PUT" and path.endswith("/pools"):
            if self.refuse is not None:
                raise self.refuse
            label = path.split("/")[3].split("%3A")[-1]
            return {"account": _account(label, pools=payload["pools"]),
                    "change": {"to_pools": payload["pools"]}}
        raise SwarmError(f"{method} {path} -> 500: unexpected", status=500)

    def writes(self):
        return [c for c in self.sent if c[0] != "GET"]


def _run(argv, api, stdin=""):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        code = args.func(api, args, out)
    finally:
        sys.stdin = old
    return code, out.getvalue()


def _row(screen: str, account_id: str) -> str:
    return next(r for r in screen.splitlines() if r.strip().startswith(account_id + " "))


# -- the listing -----------------------------------------------------------------


def test_the_listing_shows_each_accounts_pools():
    style = render.Style(width=200, color=False, unicode=False)
    screen = "\n".join(render.render_accounts(_pool(), style, NOW))
    assert "POOLS" in screen.splitlines()[0]
    assert " workspace " in _row(screen, "acme:agents") + " "
    assert "voice-it" in _row(screen, "acme:talk") and "workspace" not in _row(screen, "acme:talk")
    # Always in the served order, whatever order the API sent.
    assert "workspace,voice-it" in _row(screen, "acme:both")


def test_the_listing_names_who_holds_a_voice_it_account_now():
    style = render.Style(width=200, color=False, unicode=False)
    flat = " ".join("\n".join(render.render_accounts(_pool(), style, NOW)).split())
    assert f"acme:talk is held for Voice it by {PERSON}, taken 10m ago" in flat
    assert "acme:both is held" not in flat


def test_an_older_api_without_pools_gets_no_pools_column():
    style = render.Style(width=200, color=False, unicode=False)
    old = [{k: v for k, v in a.items() if not k.startswith(("pools", "voice_it"))} for a in _pool()]
    assert "POOLS" not in "\n".join(render.render_accounts(old, style, NOW))


def test_sc_accounts_shows_pools_and_its_json_carries_them():
    code, out = _run(["accounts", "--width", "200"], Api())
    assert code == sc.EXIT_OK
    assert "POOLS" in out and "workspace,voice-it" in out
    _, raw = _run(["accounts", "--json"], Api())
    accounts = {a["label"]: a for a in json.loads(raw)["accounts"]}
    assert accounts["both"]["pools"] == ["workspace", "voice-it"]
    assert accounts["talk"]["voice_it_holder"] == {"person": PERSON,
                                                    "since": "2026-11-03T02:50:00+00:00"}
    assert "voice_session_id" not in json.dumps(accounts)


def test_the_swarm_accounts_tool_carries_the_pools():
    text = server._call(Api(), "swarm_accounts", {"width": 120})
    assert "POOLS" in text
    assert "workspace,voice-it" in text
    assert f"held for Voice it by {PERSON}" in " ".join(text.split())


# -- sc account pool ---------------------------------------------------------------


def test_add_sends_the_union_and_the_typed_label():
    api = Api()
    code, out = _run(["account", "pool", "agents", "--add", "voice-it"], api, stdin="agents\n")
    assert code == sc.EXIT_OK
    assert api.writes() == [("PUT", "/v1/accounts/acme%3Aagents/pools",
                             {"pools": ["workspace", "voice-it"], "confirm_label": "agents"})]
    assert "now in pools workspace,voice-it" in out


def test_remove_sends_what_is_left():
    api = Api()
    _run(["account", "pool", "both", "--remove", "voice-it"], api, stdin="both\n")
    assert api.writes() == [("PUT", "/v1/accounts/acme%3Aboth/pools",
                             {"pools": ["workspace"], "confirm_label": "both"})]


def test_a_mistyped_label_sends_nothing(monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = Api()
    with pytest.raises(SwarmError) as refused:
        _run(["account", "pool", "agents", "--add", "voice-it"], api, stdin="agent\n")
    assert "nothing was changed" in str(refused.value)
    assert api.writes() == []
    # No terminal to type at, even with SWARM_ASSUME_YES set, sends nothing.
    with pytest.raises(SwarmError):
        _run(["account", "pool", "agents", "--add", "voice-it"], api, stdin="")
    assert api.writes() == []


def test_removing_the_last_pool_is_refused_without_asking_or_sending(capsys):
    api = Api()
    with pytest.raises(SwarmError) as refused:
        _run(["account", "pool", "talk", "--remove", "voice-it"], api, stdin="talk\n")
    assert "only pool" in str(refused.value) and "Nothing was sent" in str(refused.value)
    assert api.writes() == []
    assert "Type its label" not in capsys.readouterr().err


def test_a_pool_already_held_sends_nothing():
    api = Api()
    code, out = _run(["account", "pool", "both", "--add", "workspace"], api, stdin="both\n")
    assert code == sc.EXIT_OK and "already in workspace" in out
    assert api.writes() == []


def test_a_409_pool_in_use_is_printed_plainly():
    busy = SwarmError(
        "PUT /v1/accounts/acme%3Aboth/pools -> 409: the account holds a live voice-it lease",
        status=409, code="conflict", detail={"reason": "pool_in_use"},
    )
    api = Api(refuse=busy)
    with pytest.raises(SwarmError) as refused:
        _run(["account", "pool", "both", "--remove", "voice-it"], api, stdin="both\n")
    said = str(refused.value)
    assert "pool_in_use" in said and "live lease" in said
    assert "the account holds a live voice-it lease" in said
    assert refused.value.status == 409


def test_a_400_is_printed_plainly():
    bad = SwarmError("PUT /v1/accounts/acme%3Aagents/pools -> 400: confirm_label does not match",
                     status=400, code="bad_request")
    with pytest.raises(SwarmError) as refused:
        _run(["account", "pool", "agents", "--add", "voice-it"], Api(refuse=bad), stdin="agents\n")
    assert "confirm_label does not match" in str(refused.value)


def test_an_unknown_pool_or_no_change_is_a_usage_error():
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["account", "pool", "agents", "--add", "gpu"])
    api = Api()
    with pytest.raises(SwarmError) as refused:
        _run(["account", "pool", "agents"], api)
    assert "--add POOL or --remove POOL" in str(refused.value)
    assert api.sent == []
    with pytest.raises(SystemExit):
        sc.build_parser().parse_args(["account", "pool", "agents", "--add", "voice-it",
                                      "--remove", "workspace"])
