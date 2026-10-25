"""`sc accounts` says which accounts can take an agent and why not, and
`sc account max-agents` sets the cap (#2222, ACCOUNTS-API-SC).

POOL-ACCOUNT-CAP (PR 2231) and POOL-DUP-DETECT (PR 2247) gave the broker's
listing `max_agents`, `can_take_new_agent`, `blocked_by`, `next_eligible_at`,
the organisation and the duplicate marks, and `sc` showed none of them: the
screen read "available" beside an account the pool would not hand out.

Another workspace's account is never named (POOL-DUP-FIX's rule), on the
screen or in the JSON, even when an older API sends the name.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

import pytest

from swarm_mcp import render, sc
from swarm_mcp.client import SwarmError

NOW = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
TENANT = "acme"
FOREIGN = "globex:secret-label"


def _account(label: str, **extra) -> dict:
    base = {
        "account_id": f"{TENANT}:{label}",
        "owner_tenant": TENANT,
        "label": label,
        "provider": "anthropic",
        "state": "AVAILABLE",
        "reason": "",
        "lend_to": [],
        "assigned": 0,
        "windows": {
            "five_hour": {"utilization": 0.2, "resets_at": "2026-10-24T15:10:00+00:00", "reset": False},
            "seven_day": {"utilization": 0.5, "resets_at": "2026-10-28T12:00:00+00:00", "reset": False},
        },
        "observed_at": "2026-10-24T13:55:00+00:00",
        "stale": False,
        "max_agents": 6,
        "can_take_new_agent": True,
        "blocked_by": [],
        "next_eligible_at": None,
        "organisation": {"name": "Saga", "plan": "Team"},
        "duplicate_of": None,
        "duplicate_reason": None,
        "possible_duplicate": None,
    }
    base.update(extra)
    return base


POOL = [
    _account("open", assigned=2),
    _account("weekly", can_take_new_agent=False, next_eligible_at="2026-10-25T18:59:00+00:00",
             blocked_by=[{"window": "seven_day", "used": 0.97, "floor": 0.05,
                          "resets_at": "2026-10-25T18:59:00+00:00", "aged_reading": False}]),
    _account("full", assigned=6, can_take_new_agent=False,
             blocked_by=[{"cap": 6, "max_agents": 6}]),
    _account("fresh", assigned=1, can_take_new_agent=False, observed_at=None, stale=True,
             blocked_by=[{"first_reading": "pending", "assigned": 1, "max_before_reading": 1}]),
    # An OLDER API that names another workspace's account: `sc` must not.
    _account("twin", can_take_new_agent=False, duplicate_of=FOREIGN,
             duplicate_reason=f"registered on the same organisation as {FOREIGN}",
             blocked_by=[{"duplicate_of": FOREIGN,
                          "reason": f"registered on the same organisation as {FOREIGN}"}]),
    _account("lookalike", possible_duplicate={"of": FOREIGN, "reason": f"identical to {FOREIGN}"},
             blocked_by=[{"possible_duplicate": FOREIGN, "advisory": True,
                          "reason": f"identical readings on two polls with {FOREIGN}"}]),
    _account("own-pair", can_take_new_agent=False, duplicate_of=f"{TENANT}:open",
             duplicate_reason=f"registered on the same organisation as {TENANT}:open",
             blocked_by=[{"duplicate_of": f"{TENANT}:open",
                          "reason": f"registered on the same organisation as {TENANT}:open"}]),
]


class Api:
    """The API: `GET /v1/accounts` answers `accounts`, and the cap route
    answers the account with the number it was given."""

    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, accounts=None):
        self.accounts = POOL if accounts is None else accounts
        self.sent: list[tuple] = []

    def request(self, method, path, payload=None, **kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/accounts"):
            return {"tenant_id": TENANT, "accounts": [dict(a) for a in self.accounts]}
        if method == "PUT" and path.endswith("/max-agents"):
            return {"account": dict(_account("open"), max_agents=payload["max_agents"])}
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


def _screen() -> str:
    style = render.Style(width=200, color=False, unicode=False)
    listing = render.Listing([dict(a) for a in POOL], tenant_id=TENANT)
    return "\n".join(render.render_accounts(listing, style, NOW))


# -- the screen ------------------------------------------------------------------


def test_each_account_says_how_many_agents_it_holds_of_its_cap():
    screen = _screen()
    assert "AGENTS" in screen
    line = next(row for row in screen.splitlines() if row.strip().startswith("acme:open "))
    assert "2 of 6" in line
    assert "6 of 6" in next(r for r in screen.splitlines() if r.strip().startswith("acme:full "))


def test_each_account_says_whether_it_can_take_a_new_agent():
    screen = _screen().splitlines()
    assert "NEW AGENT" in screen[0]
    row: dict[str, str] = {}
    for r in screen[1:]:
        if r.strip() and r.split()[0].startswith("acme:"):
            # The table's row, not the words under the table that name it too.
            row.setdefault(r.split()[0], r)
    assert " yes " in row["acme:open"]
    assert " no " in row["acme:weekly"]
    assert " no " in row["acme:full"]
    # An advisory mark does not refuse: the pool still hands it out.
    assert " yes " in row["acme:lookalike"]


@pytest.mark.parametrize("label,words", [
    ("weekly", "7-day window under its floor (3% left, floor 5%), clears 1d"),
    ("full", "full, 6 of 6 agents"),
    ("fresh", "first reading pending"),
    ("twin", "duplicate withheld: " + render.ANOTHER_WORKSPACE),
    ("lookalike", "possible duplicate, still given agents"),
    ("own-pair", f"duplicate withheld: registered on the same organisation as {TENANT}:open"),
])
def test_what_blocks_each_account_is_said_in_words(label, words):
    flat = " ".join(_screen().split())
    assert words in flat, flat


def test_a_refused_account_is_said_to_be_refused():
    flat = " ".join(_screen().split())
    assert "acme:weekly cannot take a new agent:" in flat
    assert "acme:lookalike cannot take" not in flat


def test_another_workspaces_account_is_never_named_on_the_screen():
    assert "globex" not in _screen()


def test_an_older_broker_without_the_fields_keeps_its_table():
    """No column of dashes that would read as "no" for every account."""
    style = render.Style(width=200, color=False, unicode=False)
    old = [{k: v for k, v in _account("open").items()
            if k not in ("max_agents", "can_take_new_agent", "blocked_by")}]
    screen = "\n".join(render.render_accounts(old, style, NOW))
    assert "NEW AGENT" not in screen and "AGENTS" not in screen


# -- the JSON --------------------------------------------------------------------


def test_the_json_carries_the_new_fields():
    _, out = _run(["accounts", "--json"], Api())
    accounts = {a["label"]: a for a in json.loads(out)["accounts"]}
    weekly = accounts["weekly"]
    for field in ("max_agents", "can_take_new_agent", "blocked_by", "next_eligible_at",
                  "organisation", "duplicate_of", "duplicate_reason", "possible_duplicate"):
        assert field in weekly, field
    assert weekly["max_agents"] == 6
    assert weekly["can_take_new_agent"] is False
    assert weekly["next_eligible_at"] == "2026-10-25T18:59:00+00:00"
    assert weekly["blocked_by"][0]["window"] == "seven_day"
    assert weekly["organisation"] == {"name": "Saga", "plan": "Team"}
    assert accounts["own-pair"]["duplicate_of"] == f"{TENANT}:open"


def test_the_json_never_names_another_workspaces_account():
    _, out = _run(["accounts", "--json"], Api())
    assert "globex" not in out
    accounts = {a["label"]: a for a in json.loads(out)["accounts"]}
    assert accounts["twin"]["duplicate_of"] is None
    assert accounts["twin"]["duplicate_reason"] == render.ANOTHER_WORKSPACE
    assert accounts["lookalike"]["possible_duplicate"] == {
        "of": None, "reason": render.ANOTHER_WORKSPACE_READINGS}


def test_a_blocked_by_entry_carries_only_known_fields():
    smuggled = _account("x", blocked_by=[{"cap": 6, "max_agents": 6, "access_token_len": 108}],
                        organisation={"name": "Saga", "plan": "Team", "id": "org-uuid"})
    public = render.public_account(smuggled, TENANT)
    assert public["blocked_by"] == [{"cap": 6, "max_agents": 6}]
    assert public["organisation"] == {"name": "Saga", "plan": "Team"}


def test_a_listing_that_did_not_say_whose_it_is_names_no_duplicate():
    public = render.public_account(_account("own-pair"), None)
    assert public["duplicate_of"] is None


# -- sc account max-agents --------------------------------------------------------


def test_max_agents_puts_the_number_when_the_label_is_typed_back():
    api = Api([_account("open")])
    code, out = _run(["account", "max-agents", "open", "3"], api, stdin="open\n")
    assert code == sc.EXIT_OK
    assert api.writes() == [("PUT", "/v1/accounts/acme%3Aopen/max-agents", {"max_agents": 3})]
    assert "at most 3 agents" in out


@pytest.mark.parametrize("typed", ["", "y\n", "yes\n", "acme:open\n", "Open\n"])
def test_max_agents_refuses_anything_but_the_label_even_with_assume_yes(monkeypatch, typed):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = Api([_account("open")])
    with pytest.raises(SwarmError) as refused:
        _run(["account", "max-agents", "open", "3"], api, stdin=typed)
    assert "nothing was changed" in str(refused.value)
    assert api.writes() == []


def test_max_agents_json_carries_the_answer():
    _, out = _run(["account", "max-agents", "open", "4", "--json"], Api([_account("open")]), stdin="open\n")
    body = json.loads(out)
    assert body["requested_max_agents"] == 4
    assert body["answer"]["account"]["max_agents"] == 4


def test_max_agents_is_beside_the_other_account_writes():
    args = sc.build_parser().parse_args(["account", "max-agents", "open", "5"])
    assert args.func is sc.cmd_account_max_agents


@pytest.mark.parametrize("n", ["many", "2.5", ""])
def test_max_agents_that_is_not_a_number_sends_nothing(n):
    api = Api([_account("open")])
    with pytest.raises(SwarmError) as refused:
        _run(["account", "max-agents", "open", n], api, stdin="open\n")
    assert "nothing was sent" in str(refused.value)
    assert api.sent == []
