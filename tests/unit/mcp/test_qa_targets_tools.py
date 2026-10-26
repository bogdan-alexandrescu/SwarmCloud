"""The QA-target tools of the sc plugin (lane VQ-6 CONSOLE-PLUGIN, part of #1933).

docs/design/visual-qa-any-app.md §2.1, §6 and §7 name three plugin tools over
VQ-2's routes (`swarm_api/routes/qatargets.py`), used exactly as they are:

    GET  /v1/qa-targets                          swarm_qa_targets
    POST /v1/qa-targets/{id}:run                 swarm_qa_run
    POST /v1/qa-targets/{id}/credential-links    swarm_qa_target_credential_link

WHAT EACH CASE HOLDS:
  * no tool takes a credential: no schema field a username or password fits
    in, an undeclared argument is refused with nothing sent, and the PUT that
    takes the credential is called by nothing in the bridge;
  * no tool returns one: every answer is copied field by field, so a value the
    API (wrongly) sent back is not forwarded;
  * `swarm_qa_target_credential_link` answers ONLY `url` and `expires_at`;
  * `swarm_qa_run` sends only `file`, the screens subset and an optional
    `browser_tool` (`playwright` or `jev`, anything else refused unsent), to
    the target's own `:run`, and a name is resolved to its id first;
  * a pending host is said as the reason a target cannot run;
  * (QAENV-SC, docs/design/qa-environments.md §7, §8, part of #2136) the list
    names each target's entries -- name, mark, host state, credential set yes
    or no, whether it can run -- with no credential field and no bypass value
    anywhere in it, and `swarm_qa_run` sends `environments` as entry names
    only, refusing anything else (a URL among it) before a request, and
    answers the run group's id and each leg's workflow.

Every fake credential is built at runtime (CLAUDE.md). Offline: no
credentials, no emulator.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

REPO = Path(__file__).resolve().parents[3]
LIST_PATH = "/v1/qa-targets"
QA_TOOLS = ("swarm_qa_targets", "swarm_qa_run", "swarm_qa_target_credential_link")
CONSOLE = "https://console.example.test"
APPROVED_ID = "qat_" + "a1" * 8
PENDING_ID = "qat_" + "b2" * 8
STUDIO_ID = "qat_" + "d4" * 8


def _target(target_id: str, name: str, *, host_state: str, **over) -> dict:
    """A target as VQ-2's `to_api` serves it."""
    return {
        "id": target_id, "tenant_id": "eng", "revision": 2,
        "name": name, "base_url": f"https://{name}.example.test/",
        "host": f"{name}.example.test", "host_state": host_state,
        "approved_by": "ops@example.test" if host_state == "approved" else None,
        "approved_at": "2026-10-22T09:00:00+00:00" if host_state == "approved" else None,
        "approved_origins": [f"https://{name}.example.test"] if host_state == "approved" else [],
        "repo_id": "repo_" + "0" * 16,
        "repository": "https://github.com/example-org/example-app",
        "sign_in": {"kind": "none"},
        "credential": {"set": False, "set_at": None, "set_by": None, "version": None},
        "authorized_by": "the app's owner", "attested_by": "vera@example.test",
        "created_by": "vera@example.test", "updated_by": "vera@example.test",
        "screens": [{"id": "home", "path": "/"}],
        **over,
    }


def _studio(planted: str | None) -> dict:
    """A target of two environments as QAENV-MODEL's `to_api` serves it: the
    per-leg fields only inside `environments`, each with its own credential
    and bypass status. `planted` (wrongly) rides in every status and spec."""
    extra = {"value": planted, "password": planted} if planted else {}

    def entry(name: str, mark: str, *, host_state: str, credential_set: bool) -> dict:
        return {
            "name": name, "base_url": "https://studio.example.test/", "host": "studio.example.test",
            "environment": mark, "sign_in": {"kind": "own_login", **extra},
            "host_state": host_state, "approved_by": "ops@example.test",
            "approved_at": "2026-10-23T09:00:00+00:00",
            "approved_origins": ["https://studio.example.test"], "idp_token_path": None,
            "credential": {"set": credential_set, "set_at": None, "set_by": None,
                           "version": "3" if credential_set else None, **extra},
            "challenge_bypass_value": {"set": True, "set_at": None, "set_by": None,
                                       "version": "1", **extra},
            "challenge_bypass": {"kind": "header", "name": "x-qa-pass", **extra},
            "discovered": None, **extra,
        }

    return {
        "id": STUDIO_ID, "tenant_id": "eng", "revision": 4, "name": "studio",
        "repo_id": "repo_" + "0" * 16,
        "repository": "https://github.com/example-org/example-app",
        "created_by": "vera@example.test", "updated_at": "2026-10-23T09:00:00+00:00",
        "screens": [{"id": "home", "path": "/"}],
        "environments": [
            entry("staging", "non_production", host_state="approved", credential_set=True),
            entry("production", "production", host_state="pending", credential_set=False),
        ],
    }


class FakeApi:
    """VQ-2's three routes the tools call, recording every request. `planted`
    is a value the fake (wrongly) puts in every answer, so a test can show it
    is not forwarded."""

    def __init__(self, *, planted: str | None = None, console: str | None = CONSOLE,
                 error: SwarmError | None = None):
        self.sent: list[tuple[str, str, object]] = []
        self.planted = planted
        self.console = console
        self.error = error
        self.link_token = secrets.token_urlsafe(32)

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        extra = {"password": self.planted, "username": self.planted} if self.planted else {}
        if (method, path) == ("GET", LIST_PATH):
            approved = _target(APPROVED_ID, "shop", host_state="approved")
            pending = _target(PENDING_ID, "blog", host_state="pending")
            if self.planted:
                approved["credential"] = {**approved["credential"], "value": self.planted}
                approved["sign_in"] = {**approved["sign_in"], **extra}
            studio = _studio(self.planted)
            return {"qa_targets": [{**approved, **extra}, pending, studio], **extra}
        if self.error is not None:
            raise self.error
        if method == "POST" and path.endswith(":run"):
            target_id = path[len(LIST_PATH) + 1:-len(":run")]
            names = (payload or {}).get("environments")
            if names is not None and len(names) != 1:
                legs = [{"environment": n, "state": "submitted", "workflow_id": f"wf_{n}",
                         "qa_target": {"id": target_id, "revision": 4, "environment": n},
                         "browser_steps": ["capture-1"], "reason": None, **extra}
                        for n in names]
                return {"group_id": "qag_" + "e5" * 8, "file": (payload or {}).get("file"),
                        "legs": legs, **extra}
            one = {"environment": (names or ["production"])[0], "state": "submitted",
                   "workflow_id": "wf_" + "c3" * 8, "qa_target": {"id": target_id, "revision": 2},
                   "browser_steps": ["capture-1"], "reason": None, **extra}
            return {"group_id": "qag_" + "f6" * 8, "legs": [one], "workflow_id": "wf_" + "c3" * 8,
                    "qa_target": {"id": target_id, "revision": 2},
                    "file": (payload or {}).get("file"),
                    "browser_steps": ["capture-1"], **extra}
        if method == "POST" and path.endswith("/credential-links"):
            target_id = path[len(LIST_PATH) + 1:-len("/credential-links")]
            link_path = f"/qa-targets/{target_id}/credential?t={self.link_token}"
            return {"url": f"{self.console}{link_path}" if self.console else None,
                    "path": link_path, "expires_at": "2026-10-22T10:15:00+00:00", **extra}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


def _tool(name: str) -> dict:
    return next(t for t in server.TOOLS if t["name"] == name)


def _call(api: FakeApi, name: str, args: dict) -> dict:
    return json.loads(server._call(api, name, args))


#: A property name that could be read as "put the credential here".
_CREDENTIALISH = re.compile(
    r"password|passwd|username|user_name|login|credential|secret|token|value|key|cookie", re.I)


# -- the schemas: nothing a credential fits in ------------------------------------


@pytest.mark.parametrize("name", QA_TOOLS)
def test_each_qa_tool_is_advertised_with_no_field_a_credential_fits_in(name):
    tool = next((t for t in server.TOOLS if t["name"] == name), None)
    # MUTATION: drop the tool's entry from `server.TOOLS` and this is None.
    assert tool is not None, f"{name} is not in TOOLS, so no host lists it"
    schema = tool["inputSchema"]
    # MUTATION: set additionalProperties to True and a `password` argument would be accepted.
    assert schema.get("additionalProperties") is False
    properties = schema.get("properties") or {}
    # MUTATION: add a `password` (or `username`) property to any of the three schemas.
    assert [p for p in properties if _CREDENTIALISH.search(p)] == []
    # `browser_tool` is an enum of two tool names (lane QA-RUN-TOOL), and
    # `environments`/`environment` are entry names (QAENV-SC), and `field` an enum
    # of the two forms a credential link opens (SSC-SC), and `flows_per_step` an
    # integer 1-30 (QA-FLOWS-CAP): no credential fits.
    assert set(properties) <= {"target", "repository", "screens", "file", "browser_tool",
                               "environments", "environment", "field", "flows_per_step"}, sorted(properties)


def test_an_undeclared_credential_argument_is_refused_with_nothing_sent():
    api = FakeApi()
    password = secrets.token_hex(12)
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_target_credential_link",
                     {"target": APPROVED_ID, "password": password})
    # MUTATION: let `_call` skip `_refuse_unknown_arguments` and the link route is called.
    assert api.sent == []
    # MUTATION: echo the arguments in the refusal and the value is repeated back.
    assert password not in str(caught.value)


def test_nothing_in_the_bridge_calls_the_route_that_takes_the_credential():
    source = (REPO / "apps/swarm-mcp/swarm_mcp/qatargets.py").read_text()
    # MUTATION: add a `client.request("PUT", ".../credential", ...)` to qatargets.py.
    assert '"PUT"' not in source and "'PUT'" not in source
    assert re.search(r"/credential[\"'?]", source) is None


# -- swarm_qa_targets ---------------------------------------------------------------


def test_the_list_copies_status_fields_only_and_forwards_no_credential():
    planted = secrets.token_hex(16)
    api = FakeApi(planted=planted)
    raw = server._call(api, "swarm_qa_targets", {})
    assert api.sent == [("GET", LIST_PATH, None)]
    # MUTATION: forward the API's target documents whole and the planted value reaches the reply.
    assert planted not in raw
    body = json.loads(raw)
    shop = next(t for t in body["qa_targets"] if t["id"] == APPROVED_ID)
    # MUTATION: copy `credential` whole and `value` rides along.
    assert "credential" not in shop
    assert "password" not in shop and "username" not in shop
    # MUTATION: list a target stored with one origin as no entries at all.
    (entry,) = shop["environments"]
    assert entry["credential_set"] is False and entry["host_state"] == "approved"
    assert shop["can_run"] is True


def test_a_pending_host_is_said_as_the_reason_the_target_cannot_run():
    body = _call(FakeApi(), "swarm_qa_targets", {})
    blog = next(t for t in body["qa_targets"] if t["id"] == PENDING_ID)
    # MUTATION: treat any host_state as runnable and this is True.
    assert blog["can_run"] is False
    assert blog["environments"][0]["can_run"] is False
    # MUTATION: drop the pending reason and the person is not told why.
    assert "platform admin" in blog["cannot_run_because"]
    assert "blog.example.test" in blog["cannot_run_because"]
    assert "pending" in body["text"] and "platform admin" in body["text"]


def test_the_list_narrows_to_one_repository():
    api = FakeApi()
    body = _call(api, "swarm_qa_targets", {"repository": "example-org/other"})
    # MUTATION: ignore `repository` and both targets are listed.
    assert body["qa_targets"] == []
    body = _call(api, "swarm_qa_targets", {"repository": "example-org/example-app"})
    assert {t["id"] for t in body["qa_targets"]} == {APPROVED_ID, PENDING_ID, STUDIO_ID}


# -- swarm_qa_run -------------------------------------------------------------------


def test_a_run_by_name_resolves_the_id_and_posts_only_file_and_screens():
    api = FakeApi()
    body = _call(api, "swarm_qa_run", {"target": "shop", "file": "none", "screens": ["home"]})
    # MUTATION: post to the name instead of the resolved id, or to another route.
    assert api.sent[-1] == ("POST", f"{LIST_PATH}/{APPROVED_ID}:run",
                            {"file": "none", "screens": ["home"]})
    assert body["workflow_id"] == "wf_" + "c3" * 8
    assert body["qa_target"] == {"id": APPROVED_ID, "revision": 2}


def test_a_run_by_id_defaults_to_filing_issues_and_sends_no_screens():
    api = FakeApi()
    _call(api, "swarm_qa_run", {"target": APPROVED_ID})
    # MUTATION: default `file` to "none", or send `screens: null`, and this differs.
    assert api.sent == [("POST", f"{LIST_PATH}/{APPROVED_ID}:run", {"file": "issues"})]


def test_a_run_forwards_no_credential_the_api_sent_back():
    planted = secrets.token_hex(16)
    raw = server._call(FakeApi(planted=planted), "swarm_qa_run", {"target": APPROVED_ID})
    # MUTATION: return the `:run` answer whole and the planted value is forwarded.
    assert planted not in raw


def test_an_unknown_target_is_refused_without_repeating_it():
    api = FakeApi()
    unknown = secrets.token_hex(10)
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_run", {"target": unknown})
    # MUTATION: post `:run` for a name the list did not hold.
    assert [s for s in api.sent if s[0] == "POST"] == []
    # MUTATION: repeat the argument in the refusal (a pasted password would be echoed).
    assert unknown not in str(caught.value)


def test_the_apis_pending_host_refusal_reaches_the_caller_as_its_own_words():
    refusal = SwarmError("POST /v1/qa-targets/x:run -> 409 qa_target_host_pending: this QA "
                         "target's host is not approved yet", status=409)
    api = FakeApi(error=refusal)
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_run", {"target": PENDING_ID})
    # MUTATION: swallow the API's refusal and answer a workflow id of None.
    assert "qa_target_host_pending" in str(caught.value)


# -- swarm_qa_run's browser_tool (lane QA-RUN-TOOL, part of #1649) ------------------


def test_a_run_that_names_jev_sends_it_in_the_run_body():
    api = FakeApi()
    _call(api, "swarm_qa_run", {"target": APPROVED_ID, "browser_tool": "jev"})
    # MUTATION: drop `browser_tool` from the payload, or send it under another key.
    assert api.sent == [("POST", f"{LIST_PATH}/{APPROVED_ID}:run",
                         {"file": "issues", "browser_tool": "jev"})]


def test_a_run_that_names_no_browser_tool_sends_no_such_key():
    api = FakeApi()
    _call(api, "swarm_qa_run", {"target": APPROVED_ID})
    # MUTATION: default `browser_tool` to "playwright" (or send null) and the
    # target's own `flows_browser_tool` no longer decides.
    assert "browser_tool" not in api.sent[-1][2]


def test_an_unknown_browser_tool_is_refused_before_any_request():
    api = FakeApi()
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_run", {"target": APPROVED_ID, "browser_tool": "chrome"})
    # MUTATION: check the value after resolving the target (a GET is sent), or not at all.
    assert api.sent == []
    # MUTATION: a refusal that does not name the field and both values.
    assert "browser_tool" in str(caught.value)
    assert "playwright" in str(caught.value) and "jev" in str(caught.value)


def test_the_run_schema_lists_exactly_playwright_and_jev():
    tool = _tool("swarm_qa_run")
    field = tool["inputSchema"]["properties"]["browser_tool"]
    # MUTATION: add a third value, drop one, or leave the field without an enum.
    assert field["type"] == "string"
    assert sorted(field["enum"]) == ["jev", "playwright"]
    # MUTATION: give the field a default, which the platform would read as a choice.
    assert "default" not in field
    assert "browser_tool" not in tool["inputSchema"].get("required", [])
    # MUTATION: a description that does not say what the field does or its default.
    assert "browser_tool" in tool["description"]
    assert "flows_browser_tool" in tool["description"]


# -- environments (QAENV-SC, docs/design/qa-environments.md §7, §8; part of #2136) --


#: What a listed entry carries (§7's checkbox: its host state, its credential
#: state, why it cannot run) and nothing else.
ENTRY_FIELDS = {"name", "environment", "base_url", "host", "host_state", "sign_in",
                "credential_set", "can_run", "cannot_run_because"}


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_list_names_each_entry_with_its_host_and_credential_state_and_mark():
    body = _call(FakeApi(), "swarm_qa_targets", {})
    studio = next(t for t in body["qa_targets"] if t["id"] == STUDIO_ID)
    # MUTATION: list the target's top-level fields only and its entries are lost.
    staging, production = studio["environments"]
    # MUTATION: copy an entry whole, or drop one of its status fields.
    assert set(staging) == ENTRY_FIELDS and set(production) == ENTRY_FIELDS
    assert (staging["name"], staging["environment"]) == ("staging", "non_production")
    assert (production["name"], production["environment"]) == ("production", "production")
    # MUTATION: read `credential.set` from the target, not the entry, and both are False.
    assert staging["credential_set"] is True and production["credential_set"] is False
    assert staging["host_state"] == "approved" and production["host_state"] == "pending"
    # MUTATION: judge the target, not each leg, and a pending production stops staging.
    assert staging["can_run"] is True and staging["cannot_run_because"] is None
    assert production["can_run"] is False and "pending" in production["cannot_run_because"]
    assert studio["can_run"] is True
    # MUTATION: a text line per target only, and the person cannot see which leg is pending.
    assert "staging" in body["text"] and "production" in body["text"]


def test_the_list_carries_no_credential_field_and_no_bypass_value():
    planted = secrets.token_hex(16)
    raw = server._call(FakeApi(planted=planted), "swarm_qa_targets", {})
    # MUTATION: forward an entry (or its credential, sign_in or bypass) whole and the
    # runtime-built fake value reaches the reply.
    assert planted not in raw
    keys = _keys(json.loads(raw))
    # MUTATION: keep the credential's status object, or the bypass's spec or status.
    assert not keys & {"credential", "challenge_bypass", "challenge_bypass_value",
                       "password", "username", "value"}, sorted(keys)


def test_a_run_sends_the_named_environments_and_nothing_else():
    api = FakeApi()
    body = _call(api, "swarm_qa_run", {"target": "studio",
                                       "environments": ["staging", "production"]})
    # MUTATION: drop `environments` from the payload, rename it, or add any other key.
    assert api.sent[-1] == ("POST", f"{LIST_PATH}/{STUDIO_ID}:run",
                            {"file": "issues", "environments": ["staging", "production"]})
    # MUTATION: answer the one-leg fields only and the group and its legs are lost.
    assert body["group_id"] == "qag_" + "e5" * 8
    assert [(leg["environment"], leg["workflow_id"]) for leg in body["legs"]] == [
        ("staging", "wf_staging"), ("production", "wf_production")]


def test_a_run_naming_no_environment_sends_no_such_key_so_every_entry_runs():
    api = FakeApi()
    _call(api, "swarm_qa_run", {"target": STUDIO_ID})
    # MUTATION: default `environments` to [] or to a fixed name and the API's
    # "every entry" default no longer decides.
    assert "environments" not in api.sent[-1][2]


@pytest.mark.parametrize("environments", [
    ["https://studio.example.test/"],
    ["Staging"],
    [],
    ["staging", "staging"],
    "staging",
    ["a" * 21],
])
def test_a_run_refuses_anything_but_entry_names_before_any_request(environments):
    api = FakeApi()
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_run", {"target": STUDIO_ID, "environments": environments})
    # MUTATION: send whatever was given (a URL among it) and let the API sort it out.
    assert api.sent == []
    assert "environments" in str(caught.value)
    # MUTATION: echo the argument in the refusal.
    assert "studio.example.test" not in str(caught.value)


def test_the_run_schema_takes_environment_names_and_no_url():
    tool = _tool("swarm_qa_run")
    properties = tool["inputSchema"]["properties"]
    field = properties["environments"]
    # MUTATION: let `environments` hold objects (a URL, a sign-in) instead of names.
    assert field["type"] == "array" and field["items"]["type"] == "string"
    assert field["items"]["pattern"] == "^[a-z0-9-]{1,20}$"
    assert "default" not in field
    # MUTATION: add a `base_url`, `url` or `origin` argument.
    assert not [p for p in properties if re.search(r"url|origin|host|sign_in", p, re.I)]
    assert "environments" in tool["description"] and "group_id" in tool["description"]


def test_a_credential_link_names_its_entry():
    api = FakeApi()
    _call(api, "swarm_qa_target_credential_link", {"target": STUDIO_ID, "environment": "staging"})
    # MUTATION: drop the entry's name and a target of several entries is refused by the API.
    assert api.sent[-1] == ("POST", f"{LIST_PATH}/{STUDIO_ID}/credential-links",
                            {"environment": "staging"})


# -- swarm_qa_target_credential_link ------------------------------------------------


def test_the_credential_link_answers_only_the_url_and_its_expiry():
    planted = secrets.token_hex(16)
    api = FakeApi(planted=planted)
    raw = server._call(api, "swarm_qa_target_credential_link", {"target": APPROVED_ID})
    assert api.sent[-1] == ("POST", f"{LIST_PATH}/{APPROVED_ID}/credential-links", None)
    body = json.loads(raw)
    # MUTATION: forward the API's answer whole and `path` (and the planted value) ride along.
    assert set(body) == {"url", "expires_at"}, sorted(body)
    assert body["url"] == f"{CONSOLE}/qa-targets/{APPROVED_ID}/credential?t={api.link_token}"
    assert body["expires_at"] == "2026-10-22T10:15:00+00:00"
    assert planted not in raw


def test_with_no_console_address_the_link_tool_says_so_and_gives_no_path():
    api = FakeApi(console=None)
    body = _call(api, "swarm_qa_target_credential_link", {"target": "shop"})
    # MUTATION: fall back to the API's `path` and the bare token reaches the transcript.
    assert body["url"] is None
    assert api.link_token not in json.dumps(body)
    assert set(body) == {"url", "expires_at", "note"}
    assert "SWARM_CONSOLE_URL" in body["note"]


# -- the seam: every route the tools call is one VQ-2 serves -------------------------


def test_every_route_the_qa_tools_call_is_served_by_swarm_api():
    from swarm_api.routes.qatargets import router

    served = {(m, r.path) for r in router.routes for m in getattr(r, "methods", ())}
    calls = {
        ("GET", "/v1/qa-targets"),
        ("POST", "/v1/qa-targets/{target_id}:run"),
        ("POST", "/v1/qa-targets/{target_id}/credential-links"),
    }
    source = (REPO / "apps/swarm-mcp/swarm_mcp/qatargets.py").read_text()
    spoken = set(re.findall(r'request\(\s*"(GET|POST)"\s*,\s*f?"(/v1[^"]*)"', source))
    # MUTATION: call a route spelled differently from VQ-2's and the shapes disagree.
    assert {(m, re.sub(r"\{[^}]+\}", "{target_id}", p)) for m, p in spoken} == calls
    assert calls <= served, sorted(calls - served)
