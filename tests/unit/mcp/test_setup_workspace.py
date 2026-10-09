"""The plugin half of personal workspaces (#847, lane W8; docs/workspaces.md §6.2).

`swarm_setup_workspace`, the `workspace` and `claude_account` steps of
`sc setup` and `/sc:setup`, and the submission gate's refusal printed by every
submission verb. These hold it to:

  * the tool posts AT MOST ONCE (the request or the loan request) and then
    reads the record once: no polling, no wait, whatever the record says;
  * the tool's reply carries the workspace id and never the tenant id, which
    is derived from the person's email;
  * `sc setup` offers the request when there is no record, skips it when the
    workspace is ready, says where a waiting one stands and moves on to
    GitHub, and offers a loan (or a skip) when there is no Claude account;
  * a 403 WORKSPACE_NOT_READY or NO_CLAUDE_ACCOUNT on `sc run` and on the
    dispatch, workflow and issue tools prints `✕ 403 <code>: <the API's
    message, word for word>` and the setup command, and `sc` exits non-zero;
  * setup.md grants the tool under both of the plugin's prefixes.

The fake records every request, so "nothing was sent" is a measurement of a
list. The refusal bodies are built and parsed by the client's own functions,
so the sentence tested is the one a real 403 produces. Offline: no
credentials, no emulator.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from swarm_mcp import client as client_mod
from swarm_mcp import sc, server
from swarm_mcp.client import SwarmClient, SwarmError

USER = "example-user@example.com"
TENANT = "eng"
WID = "w-3f9a2c"

NOT_READY_MESSAGE = (
    "Your SwarmCloud workspace is waiting for an admin's approval, so no task or workflow "
    "can start. Finish setup: create your workspace.")
NO_ACCOUNT_MESSAGE = (
    "No Claude account yet. Add your own, or ask an admin to lend you one, and then try "
    "again.")


def _record(state: str, **over):
    base = {"state": state, "setup_url": "https://console.example.com/setup#workspace",
            "setup_command": "/sc:setup"}
    if state == "none":
        return {**base, **over}
    return {**base, "workspace_id": WID, "tenant_id": "u-example-user", "request_id": "req-1",
            "requested_at": "2026-10-08T10:00:00+00:00", "requested_via": "plugin",
            "decision": None, "limits": {"max_active": 8}, "steps": {}, "failure": None,
            "ready_at": None, "request_again_at": None, **over}


def _step(name, state, evidence=None, **more):
    return {"step": name, "state": state, "code": None, "copy": None, "checked_at": None,
            "evidence": {**(evidence or {}), **more}, "issues": []}


def _refusal(method: str, path: str, body: dict) -> SwarmError:
    """A 403 exactly as `SwarmClient._send` raises it from this body."""
    raw = json.dumps(body)
    code, detail = client_mod._error_fields(raw)  # noqa: SLF001
    return SwarmError(f"{method} {path} -> 403: {client_mod._explain(403, raw)}",  # noqa: SLF001
                      status=403, code=code, detail=detail)


NOT_READY = {"code": "WORKSPACE_NOT_READY", "message": NOT_READY_MESSAGE,
             "detail": {"workspace_id": WID, "state": "requested",
                        "setup_url": "https://console.example.com/setup#workspace",
                        "setup_command": "/sc:setup"}}
NO_ACCOUNT = {"code": "NO_CLAUDE_ACCOUNT", "message": NO_ACCOUNT_MESSAGE,
              "detail": {"setup_url": "https://console.example.com/setup#claude-account",
                         "setup_command": "/sc:setup"}}


class FakeApi:
    """The workspace routes and the onboarding checklist, over in-memory state.

    GitHub is already connected (the GitHub half is test_setup_and_access.py's),
    so the wizard's later steps run on the same fake without a browser."""

    def __init__(self, state="none", *, claude=None, refuse=None):
        self.sent: list[tuple[str, str, object]] = []
        self.record = _record(state)
        self.claude = claude or {"own": 0, "lent": 0, "provider_key": False, "loan_request": None}
        self.refuse = refuse or {}

    def writes(self):
        return [(m, p, b) for m, p, b in self.sent if m != "GET"]

    def _view(self):
        ws_state = {"none": "todo", "ready": "done", "denied": "failed",
                    "failed": "failed"}.get(self.record["state"], "in_progress")
        has = self.claude.get("own") or self.claude.get("lent")
        steps = [
            _step("signed_in", "done", email=USER, tenant_id=TENANT),
            {**_step("workspace", ws_state, {k: v for k, v in self.record.items()
                                             if k != "tenant_id"}), "required": True},
            {**_step("claude_account", "done" if has else
                     "in_progress" if self.claude.get("loan_request") == "requested" else "todo",
                     self.claude), "required": True},
            _step("github_connected", "done", via="user", forge_login="example-user"),
            _step("orgs_enabled", "done"),
            _step("repos_chosen", "done"),
            _step("access_verified", "done"),
        ]
        nxt = next((s["step"] for s in steps if s["state"] != "done"), None)
        steps.append(_step("ready", "done" if nxt is None else "todo"))
        return {"tenant_id": TENANT, "user": USER, "steps": steps, "next_step": nxt,
                "complete": nxt is None}

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) in self.refuse:
            raise self.refuse[(method, path)]
        if (method, path) == ("GET", "/v1/onboarding"):
            return self._view()
        if (method, path) == ("GET", "/v1/workspace"):
            return dict(self.record)
        if (method, path) == ("POST", "/v1/workspace"):
            if self.record["state"] == "none":
                self.record = _record("requested")
            return dict(self.record)
        if (method, path) == ("POST", "/v1/workspace/loan-request"):
            self.claude = {**self.claude, "loan_request": "requested"}
            return {"state": "requested", "workspace_id": self.record.get("workspace_id"),
                    "request_id": "loan-1", "requested_at": "2026-10-08T10:05:00+00:00"}
        if (method, path) == ("GET", "/v1/access"):
            return {"tenant_id": TENANT, "connection": {"state": "active"}, "orgs": [],
                    "grants": []}
        if (method, path) == ("GET", "/v1/access/orgs"):
            return {"owners": [{"owner": "example-user", "install_state": "installed",
                                "enabled": True}], "orgs_listed": True, "install_url": None}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


def _answers(*lines):
    queue = list(lines)
    asked: list[str] = []

    def ask(prompt):
        asked.append(prompt)
        return queue.pop(0) if queue else ""

    ask.asked = asked
    return ask


def _wizard(api, *answers):
    out = io.StringIO()
    ask = _answers(*answers)
    code = sc.run_setup(api, out, ask=ask, open_browser=lambda url: True, browser=False,
                        sleep=lambda s: None, clock=lambda: 0.0)
    return code, out.getvalue(), ask


# -- the bridge tool ---------------------------------------------------------------


def test_the_tool_requests_once_then_reads_once_and_never_polls():
    api = FakeApi("none")
    body = json.loads(server._call(api, "swarm_setup_workspace", {"action": "request"}))
    assert api.sent == [("POST", "/v1/workspace", {"via": "plugin"}), ("GET", "/v1/workspace", None)]
    assert body["action"] == "request"
    assert body["workspace"]["state"] == "requested"
    assert body["workspace"]["workspace_id"] == WID
    assert body["text"] == f"requested ({WID}) — waiting for an admin"
    assert body["setup_command"] == "/sc:setup"
    # The tenant id is derived from the email: a name, never in a reply.
    assert "tenant_id" not in body["workspace"]
    assert "u-example-user" not in json.dumps(body)


def test_the_tool_reads_an_applying_record_once_without_waiting_for_the_job():
    api = FakeApi("applying")
    api.record["steps"] = {"A1": {"state": "done"}, "A2": {"state": "done"},
                           "A3": {"state": "running"}}
    body = json.loads(server._call(api, "swarm_setup_workspace", {}))
    assert api.sent == [("GET", "/v1/workspace", None)]
    assert body["text"] == f"being set up ({WID}) — 2 of 9 steps done"


def test_the_tool_posts_the_loan_request_then_reads_the_record():
    api = FakeApi("ready")
    body = json.loads(server._call(api, "swarm_setup_workspace", {"action": "loan"}))
    assert api.writes() == [("POST", "/v1/workspace/loan-request", {"via": "plugin"})]
    assert [p for m, p, _ in api.sent] == ["/v1/workspace/loan-request", "/v1/workspace"]
    assert body["loan_request"]["state"] == "requested"
    assert body["workspace"]["state"] == "ready"


def test_an_unknown_action_or_argument_sends_nothing():
    api = FakeApi("none")
    with pytest.raises(SwarmError, match="status, request or loan"):
        server._call(api, "swarm_setup_workspace", {"action": "approve"})
    with pytest.raises(SwarmError, match="does not take"):
        server._call(api, "swarm_setup_workspace", {"action": "request", "tenant": "eng"})
    assert api.sent == []


def test_a_workspace_route_refusal_reads_its_code_and_sentence():
    too_soon = "Your workspace request was not approved, and it can be asked for again later."
    api = FakeApi("denied", refuse={("POST", "/v1/workspace"): _refusal(
        "POST", "/v1/workspace", {"code": "WORKSPACE_REQUEST_TOO_SOON", "message": too_soon,
                                  "detail": {"request_again_at": "2026-10-09T10:00:00+00:00"}})})
    with pytest.raises(SwarmError) as raised:
        server._call(api, "swarm_setup_workspace", {"action": "request"})
    assert str(raised.value) == f"WORKSPACE_REQUEST_TOO_SOON: {too_soon}"


def test_the_tool_is_served_and_setup_md_grants_it_on_both_names():
    assert "swarm_setup_workspace" in {tool["name"] for tool in server.TOOLS}
    text = (Path(__file__).resolve().parents[3] / "plugin" / "commands" / "setup.md").read_text()
    front, body = text.split("---", 2)[1:]
    assert "mcp__swarmcloud__swarm_setup_workspace" in front
    assert "mcp__plugin_sc_swarmcloud__swarm_setup_workspace" in front
    # The two steps come after "Where it stands" and before GitHub, in order.
    where, workspace, claude, github = (body.index(s) for s in (
        "**Where it stands.**", "**Workspace**", "**Claude account**", "**Connect GitHub**"))
    assert where < workspace < claude < github


# -- sc setup ----------------------------------------------------------------------


def test_setup_offers_the_request_when_there_is_none_and_prints_it_requested():
    api = FakeApi("none")
    code, text, ask = _wizard(api, "", "skip")
    assert any("Request your workspace now?" in q for q in ask.asked)
    assert api.writes()[0] == ("POST", "/v1/workspace", {"via": "plugin"})
    assert [p for m, p, _ in api.writes()].count("/v1/workspace") == 1
    assert f"workspace         requested ({WID}) — waiting for an admin" in text
    # Skipped the loan: nothing posted for it.
    assert not any(p == "/v1/workspace/loan-request" for _, p, _ in api.sent)
    assert "claude account: skipped" in text
    assert code == sc.EXIT_TROUBLE, "a required step is still to do"


def test_setup_declined_requests_nothing_and_offers_no_loan():
    api = FakeApi("none")
    _, text, ask = _wizard(api, "n")
    assert api.writes() == []
    assert not any("loan/skip" in q for q in ask.asked)
    assert "request the workspace first" in text


def test_setup_skips_the_request_when_the_workspace_is_ready():
    api = FakeApi("ready", claude={"own": 1, "lent": 0, "provider_key": False,
                                   "loan_request": None})
    code, text, ask = _wizard(api)
    assert not any("Request your workspace" in q for q in ask.asked)
    assert not any("loan" in q for q in ask.asked)
    assert not any(p.startswith("/v1/workspace") for _, p, _ in api.sent)
    assert f"[x] workspace         ready ({WID})" in text
    assert "[x] claude_account    own (1)" in text
    assert code == sc.EXIT_OK, text


def test_setup_says_where_a_waiting_workspace_stands_and_moves_on_to_github():
    api = FakeApi("applying")
    _, text, ask = _wizard(api, "loan")
    assert not any("Request your workspace" in q for q in ask.asked)
    assert f"Your workspace is being set up ({WID})" in text
    assert not any(p == "/v1/workspace" for m, p, _ in api.sent if m == "POST")
    # The loan was asked for, once, and the GitHub steps ran after it.
    assert api.writes() == [("POST", "/v1/workspace/loan-request", {"via": "plugin"})]
    assert "claude account    loan requested" in text
    assert ("GET", "/v1/access/orgs", None) in api.sent


def test_setup_does_not_offer_a_loan_already_requested():
    api = FakeApi("ready", claude={"own": 0, "lent": 0, "provider_key": False,
                                   "loan_request": "requested"})
    _, text, ask = _wizard(api)
    assert not any("loan/skip" in q for q in ask.asked)
    assert "[~] claude_account    loan requested" in text
    assert api.writes() == []


def test_setup_prints_a_denials_reason_and_a_failures_copy():
    reason = "Please use the eng team space for the migration work."
    denied = sc.workspace_line(_record("denied", decision={"verdict": "denied",
                                                           "reason": reason, "at": None}))
    assert denied == f"not approved ({WID}): {reason}"
    failed = sc.workspace_line(_record("failed", failure={"code": "NAMESPACE_APPLY_FAILED",
                                                          "step": "A7", "copy": "x"}))
    assert failed == f"stopped ({WID}) at Namespace"
    assert sc.workspace_line(_record("none")) == "not requested"


# -- the submission gate's refusal -------------------------------------------------


@pytest.mark.parametrize("body", [NOT_READY, NO_ACCOUNT], ids=["not-ready", "no-account"])
def test_sc_run_prints_the_refusal_word_for_word_and_exits_non_zero(body, monkeypatch, capsys):
    refused = _refusal("POST", "/v1/runs", body)

    class _Api:
        sent: list = []

        def request(self, method, path, payload=None, **_kw):
            self.sent.append((method, path))
            raise refused

    api = _Api()
    client = object.__new__(SwarmClient)
    client.request = api.request  # type: ignore[method-assign]

    class _Ctx:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sc, "SwarmClient", _Ctx)
    code = sc.main(["run", "--issue", "o/r#5"])
    err = capsys.readouterr().err
    assert code == sc.EXIT_FAIL
    lines = err.strip().splitlines()
    assert lines[0] == f"✕ 403 {body['code']}: {body['message']}"
    assert lines[1].startswith("  Finish setup with `") and "sc setup" in lines[1]
    assert "swarm doctor" not in err, "an API answer is not a connection failure"
    assert len(api.sent) == 1


@pytest.mark.parametrize("tool,path", [
    ("swarm_dispatch", "/v1/tasks"),
    ("swarm_workflow", "/v1/workflows"),
    ("swarm_run_issue", "/v1/runs"),
])
@pytest.mark.parametrize("body", [NOT_READY, NO_ACCOUNT], ids=["not-ready", "no-account"])
def test_every_submitting_tool_fails_with_the_refusal_and_the_setup_command(tool, path, body):
    text = server._tool_error_text(_refusal("POST", path, body))
    assert text == f"✕ 403 {body['code']}: {body['message']}\n  Finish setup with /sc:setup."


def test_any_other_403_is_printed_as_it_was():
    other = _refusal("POST", "/v1/tasks", {"code": "forbidden", "message": "Not yours."})
    assert sc.workspace_refusal_text(other, "/sc:setup") is None
    assert server._tool_error_text(other) == str(other)
