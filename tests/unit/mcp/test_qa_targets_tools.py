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
  * `swarm_qa_run` sends only `file` and the screens subset, to the target's
    own `:run`, and a name is resolved to its id first;
  * a pending host is said as the reason a target cannot run.

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
            return {"qa_targets": [{**approved, **extra}, pending], **extra}
        if self.error is not None:
            raise self.error
        if method == "POST" and path.endswith(":run"):
            target_id = path[len(LIST_PATH) + 1:-len(":run")]
            return {"workflow_id": "wf_" + "c3" * 8,
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
    assert set(properties) <= {"target", "repository", "screens", "file"}, sorted(properties)


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
    assert set(shop["credential"]) == {"set", "set_at", "set_by", "version"}
    assert "password" not in shop and "username" not in shop
    assert shop["can_run"] is True and shop["host_state"] == "approved"


def test_a_pending_host_is_said_as_the_reason_the_target_cannot_run():
    body = _call(FakeApi(), "swarm_qa_targets", {})
    blog = next(t for t in body["qa_targets"] if t["id"] == PENDING_ID)
    # MUTATION: treat any host_state as runnable and this is True.
    assert blog["can_run"] is False
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
    assert {t["id"] for t in body["qa_targets"]} == {APPROVED_ID, PENDING_ID}


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
