"""The browser tool a live-browser task names, at swarm-api (BSTATUS-API; part of #1030).

Owner decision 2026-10-14, "Picker on Submit, Auto by default"; contract
request 77 declares `browser_tool` on `claude-code-browser`. Measured here:

  * the catalogue's four names are accepted and nothing else, on that profile
    only (invariant 10: a caller names an option, never a command);
  * `jev` with no TypeSafe key for the submitter or the tenant, or with no jev
    in the image, is refused 422 `browser_tool_unavailable` through its switch
    (`refusals.refuse`, default off: logged and let through);
  * the gate's report gains `browser`, checked key by key -- an unknown key
    anywhere in it is refused -- and the relay passes it on;
  * the live-session read serves `browser`: the task's choice, what the
    runner registered, the last tool used, jev with whose key, and the egress
    with this deployment's exit country.

MUTATION: drop `"browser"` from `_REPORT_OPTIONAL` in swarm_api/live.py, or
the `refusals.refuse` in `_refuse_unavailable_browser_tool`, and the named
test below fails.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api import live as live_module
from swarm_api import refusals
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
    live,
    open_session,
    people,
    relay,
    seed_running,
    session_doc,
)
from .test_provider_keys import (  # noqa: F401  (pytest fixtures, used by name)
    api_context,
    as_,
    credentials,
    fake_key,
    group_map,
    put_tenant,
    put_user,
    seeded,
)


@pytest.fixture
def keys_client(api_context):
    """The provider-key tests' app: alice, bob, dave and root, with keys."""
    from fastapi.testclient import TestClient

    from swarm_api.main import create_app

    return TestClient(create_app(api_context), raise_server_exceptions=False)


SWITCH = refusals.env_name("browser_tool_unavailable")
AT = 1_791_900_000.0

REGISTERED = {"tool_selected": "auto", "tools_available": ["playwright", "dev-browser", "jev"],
              "jev": {"enabled": True, "reason": None}}
DIRECT = {"mode": "direct", "state": "off", "last_error": None}


def _submit(client, who: str, browser_tool: Any, *, profile: str = "claude-code-browser",
            tenant: str | None = None):
    body: dict[str, Any] = {"runner_profile": profile, "input": {"prompt": "Check the page."}}
    if browser_tool is not None:
        body["input"]["browser_tool"] = browser_tool
    return client.post("/v1/tasks", headers=as_(who, tenant), json=body)


# --------------------------------------------------------------------------- #
# The input                                                                    #
# --------------------------------------------------------------------------- #


def test_the_catalogue_declares_browser_tool_on_the_browser_profile_only():
    from swarm_common.profiles import RUNNER_PROFILES

    declared = RUNNER_PROFILES["claude-code-browser"].inputs["browser_tool"]
    assert declared.kind == "string"
    assert declared.choices == ("auto", "playwright", "dev-browser", "jev")
    assert [n for n, p in RUNNER_PROFILES.items() if "browser_tool" in p.inputs] == ["claude-code-browser"]
    assert live_module.BROWSER_TOOL_CHOICES == declared.choices


@pytest.mark.parametrize("name", ["auto", "playwright", "dev-browser", None])
def test_each_named_tool_is_accepted(keys_client, seeded, name):
    response = _submit(keys_client, "alice", name)
    assert response.status_code == 201, response.text
    stored = response.json()["task"]["input"]
    assert stored.get("browser_tool") == name


@pytest.mark.parametrize("name", ["chromium", "Auto", "playwright --headless", "", 3, ["jev"]])
def test_a_name_outside_the_enum_is_refused(keys_client, seeded, name):
    response = _submit(keys_client, "alice", name)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_input"
    assert "browser_tool" in response.text


def test_another_profile_does_not_take_browser_tool(keys_client, seeded):
    response = _submit(keys_client, "alice", "playwright", profile="claude-code")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"


# --------------------------------------------------------------------------- #
# Jev needs a key                                                              #
# --------------------------------------------------------------------------- #


def test_jev_without_a_key_is_refused_when_the_switch_is_on(keys_client, seeded, monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    response = _submit(keys_client, "alice", "jev")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "browser_tool_unavailable"
    assert body["detail"]["reason"] == "no_key"
    assert "TypeSafe key" in body["message"]


def test_jev_without_a_key_is_logged_and_let_through_while_the_switch_is_off(
        keys_client, seeded, monkeypatch, caplog):
    monkeypatch.delenv(SWITCH, raising=False)
    assert refusals.SWITCHES["browser_tool_unavailable"].default is False
    caplog.set_level(logging.WARNING, logger="swarm_api.refusals")
    response = _submit(keys_client, "alice", "jev")
    assert response.status_code == 201, response.text
    assert "refusal report-only code=browser_tool_unavailable" in caplog.text


def test_jev_with_the_submitters_own_key_or_the_tenants_is_accepted(keys_client, seeded, monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    assert put_user(keys_client, "alice", fake_key()).status_code in (200, 201)
    assert _submit(keys_client, "alice", "jev").status_code == 201
    # bob has no key of his own; eng's tenant key is root's to set, and alice's
    # does not reach a member of another group.
    assert _submit(keys_client, "bob", "jev").status_code == 422
    # root's selected tenant is eng (no personal workspace), and so is dave's.
    assert put_tenant(keys_client, "root", fake_key("y")).status_code in (200, 201)
    assert _submit(keys_client, "dave", "jev").status_code == 201


def test_jev_is_refused_when_the_image_has_none(seeded, monkeypatch, db, tokens, group_map, objects,
                                                 credentials):
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker

    from .conftest import NoForgeTokens, api_settings

    monkeypatch.setenv(SWITCH, "on")
    ctx = build_context(settings=api_settings(browser_jev_installed=False), db=db,
                        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
                        credentials=credentials, waker=NullWaker(), metrics=ApiMetrics(),
                        objects=objects, forge_tokens=NoForgeTokens())
    no_jev = TestClient(create_app(ctx), raise_server_exceptions=False)
    assert put_user(no_jev, "alice", fake_key()).status_code in (200, 201)
    response = _submit(no_jev, "alice", "jev")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "not_installed"
    assert _submit(no_jev, "alice", "auto").status_code == 201


def test_the_switch_is_registered_off_and_named_by_its_code():
    switch = refusals.SWITCHES["browser_tool_unavailable"]
    assert switch.default is False and switch.env == "REFUSAL_BROWSER_TOOL_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# The report                                                                   #
# --------------------------------------------------------------------------- #


def _report(**extra: Any) -> str:
    return json.dumps({"type": "report", "origin": None, "device": None, **extra})


def _browser(**over: Any) -> dict[str, Any]:
    return {"tools": REGISTERED, "tool_last_used": {"name": "jev", "at": AT}, "egress": DIRECT, **over}


def test_a_report_with_browser_is_read_and_checked():
    parsed = live_module.parse_report(_report(browser=_browser()))
    assert parsed is not None
    assert parsed["browser"]["tools"] == REGISTERED
    assert parsed["browser"]["tool_last_used"] == {
        "name": "jev", "at": datetime.fromtimestamp(AT, tz=timezone.utc)}
    assert parsed["browser"]["egress"] == DIRECT
    # A report without it reads as before.
    assert live_module.parse_report(_report()) == {"origin": None, "device": None}


def test_an_unknown_report_key_refuses_the_report():
    assert live_module.parse_report(_report(browser=_browser(), tools=["jev"])) is None


@pytest.mark.parametrize(
    "browser",
    [
        _browser(extra=1),
        _browser(tools={**REGISTERED, "binary": "/usr/bin/sh"}),
        _browser(tools={**REGISTERED, "tools_available": ["playwright", "sh"]}),
        _browser(tools={**REGISTERED, "jev": {"enabled": True, "reason": None, "key": "x"}}),
        _browser(tools={**REGISTERED, "jev": {"enabled": False, "reason": "no_key"}}),
        _browser(tool_last_used={"name": "curl", "at": AT}),
        _browser(tool_last_used={"name": "jev", "at": "yesterday"}),
        _browser(tool_last_used={"name": "jev", "at": AT, "pid": 4}),
        _browser(egress={"mode": "residential", "state": "ok", "last_error": None, "country": "us"}),
        _browser(egress={"mode": "direct", "state": "ok", "last_error": None}),
        _browser(egress={"mode": "residential", "state": "failing", "last_error": None}),
        _browser(egress={"mode": "residential", "state": "ok", "last_error": "fine"}),
        _browser(egress={"mode": "residential", "state": "failing", "last_error": "x" * 201}),
        _browser(egress={"mode": "residential", "state": "failing", "last_error": "line\nbreak"}),
        _browser(egress={"mode": "vpn", "state": "ok", "last_error": None}),
        "browser",
    ],
)
def test_a_browser_that_does_not_hold_together_is_dropped_key_by_key(browser):
    assert live_module.clean_browser(browser) is None
    parsed = live_module.parse_report(_report(browser=browser))
    assert parsed is not None and parsed["browser"] is None


def test_a_failing_egress_error_is_kept_as_a_sentence_and_redacted_again():
    sentence = "the residential egress provider refused the connection"
    egress = {"mode": "residential", "state": "failing", "last_error": sentence}
    assert live_module.clean_browser(_browser(egress=egress))["egress"] == egress
    leaked = "password=" + "p" * 12
    cleaned = live_module.clean_browser(_browser(egress={**egress, "last_error": leaked}))
    assert cleaned is not None and "p" * 12 not in cleaned["egress"]["last_error"]


def test_the_relay_passes_browser_on():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live"))
    from swarm_live import rendezvous as rz

    assert "browser" in rz._REPORT_OPTIONAL


# --------------------------------------------------------------------------- #
# The live-session read                                                        #
# --------------------------------------------------------------------------- #


def _read(client) -> dict[str, Any]:
    response = client.get(f"/v1/tasks/{TASK}/live/session", headers=bearer("alice"))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_read_serves_the_choice_before_any_report(client, ldb):
    attempt = Attempt()
    seed_running(client, ldb, attempt, input={"prompt": "x", "browser_tool": "dev-browser"})
    served = _read(client)["browser"]
    assert served == {"tool_selected": "dev-browser", "tools_available": [], "tool_last_used": None,
                      "jev": {"enabled": False, "reason": "not_selected", "key_scope": None},
                      "egress": None}
    assert open_session(client, attempt).status_code == 201
    assert _read(client)["browser"] == served


def test_the_read_serves_what_the_gate_reported_with_the_country_and_whose_key(client, ldb, clock):
    attempt = Attempt()
    seed_running(client, ldb, attempt, input={"prompt": "x"})
    assert open_session(client, attempt).status_code == 201
    ldb.docs[f"provider_keys/{secret_name_for('eng', 'typesafe')}"] = {"set": True}
    egress = {"mode": "residential", "state": "failing",
              "last_error": "the residential egress provider cannot be reached"}
    report = live_module.parse_report(_report(browser=_browser(egress=egress)))
    assert _service(ldb, clock).record_report(_admission(), report) is True
    assert session_doc(ldb)["browser"]["tools"] == REGISTERED
    # Reported again unchanged: nothing is written.
    assert _service(ldb, clock).record_report(_admission(), report) is False

    served = _read(client)["browser"]
    assert served["tool_selected"] == "auto"
    assert served["tools_available"] == ["playwright", "dev-browser", "jev"]
    assert served["tool_last_used"] == {"name": "jev",
                                        "at": datetime.fromtimestamp(AT, tz=timezone.utc).isoformat()}
    assert served["jev"] == {"enabled": True, "reason": None, "key_scope": "tenant"}
    assert served["egress"] == {**egress, "country": "us"}


def test_a_task_with_no_live_browser_serves_no_browser(client, ldb):
    seed_running(client, ldb, Attempt(), profile="claude-code", backend="GKE_AUTOPILOT")
    assert _read(client)["browser"] is None
