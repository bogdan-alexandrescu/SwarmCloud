"""Jev's unavailability is served, not silent (lane JEV-OFF; part of #1345).

Measured 2026-10-15: `browser_tool: jev` on u-bogdan, whose judgment backend
is internal-jev, got `mcp_servers: jev -> failed`. The dispatcher sends no
JEV_API_URL while the tailnet egress is off, the jev wrapper refuses, and the
agent drove Playwright -- while the Submit picker offered Jev and the live
chip read "Jev". Measured here, at swarm-api:

  * `judgment.unavailable_code` names why the workspace's backend cannot run
    jev -- `no_route` for an internal backend with no INTERNAL_DECISION_API_URL
    (the dispatcher's own condition for JEV_API_URL), `no_key` for TypeSafe
    with no key -- read off `judgment.availability`, never a second table;
  * the live read's `browser.jev` carries that reason for a task that chose
    jev (or auto) before the gate has reported what the runner registered;
  * a gate report whose registration says `no_route` is kept, not dropped.

MUTATION: return None from `unavailable_code`, or drop the judgment read from
`LiveService._browser_view`, and the named tests below fail.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import judgment
from swarm_api import live as live_module
from swarm_api.gittokens import secret_name_for

from .test_live_api import (  # noqa: F401  (pytest fixtures, used by name)
    TASK,
    Attempt,
    _admission,
    _service,
    bearer,
    client,
    clock,
    groups,
    ldb,
    make_client,
    open_session,
    people,
    relay,
    seed_running,
)


def _read(client) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def routed_client(ldb, people, groups, objects, clock, relay):
    """The same app with the tailnet egress on (INTERNAL_DECISION_API_URL set)."""
    return make_client(ldb, people, groups, objects, clock, relay,
                       internal_decision_api_url=judgment.INTERNAL_DECISION_SERVICE)


# --------------------------------------------------------------------------- #
# The reason, from the availability table                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "backend, internal, key, code",
    [
        ("internal-jev", False, True, "no_route"),
        ("internal-laya", False, True, "no_route"),
        ("internal-jev", True, False, None),
        ("internal-laya", True, False, None),
        ("typesafe", True, False, "no_key"),
        ("typesafe", False, False, "no_key"),
        ("typesafe", False, True, None),
    ],
)
def test_the_unavailable_code_follows_the_availability_table(backend, internal, key, code):
    assert judgment.unavailable_code(backend, internal_enabled=internal, typesafe_key=key) == code
    available, _ = judgment.availability(internal_enabled=internal, typesafe_key=key)[backend]
    assert available == (code is None)


def test_every_code_is_one_the_live_read_serves():
    for code in judgment.UNAVAILABLE_CODES.values():
        assert code in live_module.JEV_REASONS


# --------------------------------------------------------------------------- #
# The live read                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("selected", ["jev", "auto"])
def test_internal_jev_without_the_tailnet_reads_jev_off_no_route(client, ldb, selected):
    seed_running(client, ldb, Attempt(), input={"prompt": "x", "browser_tool": selected})
    assert _read(client)["browser"]["jev"] == {"enabled": False, "reason": "no_route", "key_scope": None}


def test_internal_jev_with_the_tailnet_reads_no_reason(routed_client, ldb):
    seed_running(routed_client, ldb, Attempt(), input={"prompt": "x", "browser_tool": "jev"})
    assert _read(routed_client)["browser"]["jev"] == {"enabled": False, "reason": None, "key_scope": None}


def test_typesafe_without_a_key_reads_jev_off_no_key_and_with_one_no_reason(routed_client, ldb):
    seed_running(routed_client, ldb, Attempt(), input={"prompt": "x", "browser_tool": "jev"})
    ldb.docs["workspace_settings/eng"] = {"judgment_backend": "typesafe"}
    assert _read(routed_client)["browser"]["jev"]["reason"] == "no_key"
    ldb.docs[f"provider_keys/{secret_name_for('eng', 'typesafe')}"] = {"set": True}
    assert _read(routed_client)["browser"]["jev"]["reason"] is None


def test_playwright_still_reads_not_selected(client, ldb):
    seed_running(client, ldb, Attempt(), input={"prompt": "x", "browser_tool": "playwright"})
    assert _read(client)["browser"]["jev"]["reason"] == "not_selected"


def test_a_registration_reporting_no_route_is_kept_and_served(client, ldb, clock):
    attempt = Attempt()
    seed_running(client, ldb, attempt, input={"prompt": "x", "browser_tool": "jev"})
    assert open_session(client, attempt).status_code == 201
    tools = {"tool_selected": "jev", "tools_available": ["playwright"],
             "jev": {"enabled": False, "reason": "no_route"}}
    browser = {"tools": tools, "tool_last_used": None,
               "egress": {"mode": "direct", "state": "off", "last_error": None}}
    assert live_module.clean_browser(browser) is not None
    report = live_module.parse_report(
        json.dumps({"type": "report", "origin": None, "device": None, "browser": browser}))
    assert report is not None and report["browser"] is not None
    assert _service(ldb, clock).record_report(_admission(), report) is True
    served = _read(client)["browser"]
    assert served["tools_available"] == ["playwright"]
    assert served["jev"] == {"enabled": False, "reason": "no_route", "key_scope": None}
