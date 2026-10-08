"""The plugin half of personal workspaces (docs/workspaces.md §6.2; #847, W8).

`swarm_setup_workspace`, the `workspace` and `claude_account` steps in
`sc setup` and /sc:setup, and the submission refusal printed with the command
that finishes setup. These hold it to:

  * the tool posting exactly one request (or loan request) and then reading
    the record once -- it never waits or polls (CLAUDE.md lanes);
  * the tool taking an action and nothing else: no image, command or
    resource field (invariant 10);
  * the wizard offering the request only when nothing was requested, skipping
    it when the workspace is ready, and printing `requested (w-…)`;
  * a 403 WORKSPACE_NOT_READY or NO_CLAUDE_ACCOUNT on every submission -- a
    task, a workflow, an issue run -- printed as `✕ 403 <code>: <message>`,
    the API's message word for word, then the setup command, and non-zero.

The refusals are made by the REAL client from a 403 body shaped as
swarm-api's `ApiError.to_payload` makes it, so the message the test expects
is the one a person would read. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
from pathlib import Path

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import sc, server
from swarm_mcp.client import SwarmClient, SwarmError
from swarm_mcp.invocation import terminal_command

TENANT = "u-example"
USER = "example-user@example.com"
LOGIN = "example-user"
REPO = "example-user/example-api"
#: A workspace id as the API mints it (`w-` and six hex), made at runtime.
WID = "w-" + secrets.token_hex(3)

NOT_READY = (
    "Your SwarmCloud workspace is waiting for an admin's approval, so no task or "
    "workflow can start. Finish setup: create your workspace."
)
NO_ACCOUNT = (
    "No Claude account yet. Add your own, or ask an admin to lend you one, and then "
    "try again."
)
REFUSALS = {"WORKSPACE_NOT_READY": NOT_READY, "NO_CLAUDE_ACCOUNT": NO_ACCOUNT}


def _step(name, mark, **evidence):
    """One checklist step; `mark` is its checklist state, since the workspace
    step's evidence carries the record's own `state`."""
    return {"step": name, "state": mark, "code": None, "copy": None, "checked_at": None,
            "evidence": evidence, "issues": [], "required": True}


#: The record's state, as the API's checklist marks it (#868's onboarding.py).
_CHECKLIST_STATE = {"none": "todo", "requested": "in_progress", "approved": "in_progress",
                    "applying": "in_progress", "needs_owner": "in_progress",
                    "denied": "failed", "failed": "failed", "ready": "done"}


class FakeApi:
    """swarm-api's workspace, onboarding and access routes, in memory. GitHub
    is already connected with one write grant, so the wizard's GitHub half
    runs straight through and the test reads the workspace half."""

    def __init__(self, *, state="none", has_account=False, loan=None, submit_error=None):
        self.sent: list[tuple[str, str, object]] = []
        self.state = state
        self.has_account = has_account
        self.loan = loan
        self.submit_error = submit_error
        self.repo_id = sc.repo_id_for(TENANT, REPO)

    def writes(self):
        return [(m, p, b) for m, p, b in self.sent if m != "GET"]

    def record(self):
        if self.state == "none":
            return {"state": "none", "setup_url": "https://console.invalid/setup#workspace",
                    "setup_command": "/sc:setup"}
        return {"state": self.state, "workspace_id": WID, "tenant_id": TENANT,
                "request_id": "req-1", "requested_at": "2026-10-08T09:00:00+00:00",
                "requested_via": "plugin", "decision": None, "limits": {"max_active": 8},
                "steps": ({"identity": {"state": "done", "at": "2026-10-08T09:05:00+00:00"}}
                          if self.state == "applying" else {}),
                "failure": None, "ready_at": None, "request_again_at": None,
                "setup_url": "https://console.invalid/setup#workspace",
                "setup_command": "/sc:setup",
                # A field the plugin has no use for must not reach a reply.
                "principal": USER}

    def view(self):
        shown = self.record()
        steps = [
            _step("signed_in", "done", email=USER, tenant_id=TENANT),
            _step("workspace", _CHECKLIST_STATE[self.state], **{
                k: shown.get(k) for k in ("state", "workspace_id", "requested_at", "decision",
                                          "steps", "failure", "ready_at", "request_again_at",
                                          "setup_url", "setup_command")}),
            _step("claude_account",
                  "done" if self.has_account
                  else "in_progress" if self.loan == "requested" else "todo",
                  own=int(self.has_account), lent=0, provider_key=False,
                  loan_request=self.loan,
                  setup_url="https://console.invalid/setup#claude-account",
                  setup_command="/sc:setup"),
            _step("github_connected", "done", via="user", forge_login=LOGIN),
            _step("orgs_enabled", "done"),
            _step("repos_chosen", "done"),
            _step("access_verified", "done"),
        ]
        before = next((s["step"] for s in steps if s["state"] != "done"), None)
        steps.append(_step("ready", "done" if before is None else "todo"))
        nxt = next((s["step"] for s in steps if s["state"] != "done"), None)
        return {"tenant_id": TENANT, "user": USER, "steps": steps, "next_step": nxt,
                "complete": nxt is None}

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/onboarding"):
            return self.view()
        if (method, path) == ("GET", "/v1/workspace"):
            return self.record()
        if (method, path) == ("POST", "/v1/workspace"):
            if self.state in ("none", "denied"):
                self.state = "requested"
            return self.record()
        if (method, path) == ("POST", "/v1/workspace/loan-request"):
            self.loan = "requested"
            return {"state": "requested", "workspace_id": WID, "request_id": "loan-1",
                    "requested_at": "2026-10-08T09:01:00+00:00"}
        if (method, path) == ("GET", "/v1/access/orgs"):
            return {"owners": [{"owner": LOGIN, "owner_type": "User", "installation_id": 11,
                                "install_state": "installed", "sso": "unknown",
                                "enabled": True, "install_url": None}],
                    "orgs_listed": True, "install_url": None, "tenant_id": TENANT}
        grant = {"repo_id": self.repo_id, "repository": REPO, "owner": LOGIN, "mode": "write",
                 "can_push": True, "checks": {}}
        if (method, path) == ("GET", "/v1/access"):
            return {"tenant_id": TENANT,
                    "connection": {"state": "active", "forge_login": LOGIN, "method": "app_user"},
                    "orgs": [{"owner": LOGIN, "enabled": True}], "grants": [grant]}
        if method == "POST" and path == f"/v1/access/grants/{self.repo_id}/verify":
            checks = {c: {"state": "ok"} for c in ("clone", "push", "pull_request")}
            return {"grant": {**grant, "checks": checks}, "failures": [], "passed": True,
                    "tenant_id": TENANT}
        if method == "POST" and path in ("/v1/tasks", "/v1/tasks/batch", "/v1/workflows",
                                         "/v1/runs"):
            raise self.submit_error
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


def _client(api: FakeApi) -> SwarmClient:
    """A SwarmClient whose transport is `api`: the methods under test are real."""
    client = object.__new__(SwarmClient)
    client.request = api.request  # type: ignore[method-assign]
    return client


def _api_refusal(code: str, message: str, detail: dict) -> SwarmError:
    """The SwarmError the real client raises for swarm-api's 403 body."""
    body = json.dumps({"code": code, "message": message, "detail": detail}).encode()

    def _opener(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    # Its own patch, undone on return: the test's `monkeypatch` also holds
    # conftest's isolation, which must outlive this.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SWARM_ID_TOKEN", ".".join(secrets.token_hex(4) for _ in range(3)))
        mp.setattr(mcp_client, "_open", _opener)
        try:
            SwarmClient(base_url="http://api.invalid").request("POST", "/v1/tasks", payload={})
        except SwarmError as exc:
            return exc
    raise AssertionError("the opener answered 403; the client raised nothing")


def _detail(code: str) -> dict:
    if code == "WORKSPACE_NOT_READY":
        return {"workspace_id": WID, "state": "requested",
                "setup_url": "https://console.invalid/setup#workspace",
                "setup_command": "/sc:setup"}
    return {"setup_url": "https://console.invalid/setup#claude-account",
            "setup_command": "/sc:setup"}


@pytest.fixture()
def no_waiting(monkeypatch):
    """Any sleep inside a workspace tool call is a defect: it polls."""
    import time

    def _refuse(_seconds):
        raise AssertionError("swarm_setup_workspace waited inside the tool")

    monkeypatch.setattr(time, "sleep", _refuse)


# -- the bridge tool ----------------------------------------------------------------


def test_the_tool_posts_one_request_and_then_reads_the_record_once(no_waiting):
    api = FakeApi()
    body = json.loads(server._call(_client(api), "swarm_setup_workspace", {"action": "request"}))

    assert api.sent == [("POST", "/v1/workspace", {"via": "plugin"}),
                        ("GET", "/v1/workspace", None)]
    assert body["workspace"]["state"] == "requested"
    assert body["workspace"]["workspace_id"] == WID
    assert body["line"] == f"requested ({WID}) — waiting for an admin"
    assert "principal" not in body["workspace"], "only the fields the plugin shows travel"
    assert USER not in json.dumps(body)


def test_the_tool_posts_a_loan_request_and_then_reads_the_record_once(no_waiting):
    api = FakeApi(state="requested")
    body = json.loads(server._call(_client(api), "swarm_setup_workspace", {"action": "loan"}))

    assert api.sent == [("POST", "/v1/workspace/loan-request", {"via": "plugin"}),
                        ("GET", "/v1/workspace", None)]
    assert body["loan"]["state"] == "requested"
    assert body["workspace"]["workspace_id"] == WID


def test_the_tool_takes_an_action_and_nothing_else():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_setup_workspace")["inputSchema"]
    assert set(schema["properties"]) == {"action"}
    assert schema["properties"]["action"]["enum"] == ["request", "loan"]
    assert schema["required"] == ["action"]

    api = FakeApi()
    with pytest.raises(SwarmError, match="does not take"):
        server._call(_client(api), "swarm_setup_workspace",
                     {"action": "request", "image": "evil:latest"})
    with pytest.raises(SwarmError, match="request.*loan"):
        server._call(_client(api), "swarm_setup_workspace", {"action": "approve"})
    assert api.sent == []


def test_the_status_tool_draws_both_new_steps_as_the_api_returns_them():
    api = FakeApi(state="requested", loan="requested")
    body = json.loads(server._call(_client(api), "swarm_setup_status", {}))

    names = [s["step"] for s in body["steps"]]
    assert names[:3] == ["signed_in", "workspace", "claude_account"]
    assert f"workspace         requested ({WID}) — waiting for an admin" in body["checklist"]
    assert "claude_account    loan requested" in body["checklist"]
    assert body["next_step"] == "workspace"


# -- the wizard -------------------------------------------------------------------------


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
    code = sc.run_setup(_client(api), out, ask=ask, open_browser=lambda _url: True,
                        sleep=lambda _s: None, clock=lambda: 0.0)
    return code, out.getvalue(), ask


def test_setup_offers_the_request_when_nothing_was_requested_and_prints_its_id():
    api = FakeApi(has_account=True)
    code, text, ask = _wizard(api, "y")

    assert ask.asked[0].startswith("Request your workspace now?")
    assert ask.asked[0].rstrip().endswith("[Y/n]")
    assert ("POST", "/v1/workspace", {"via": "plugin"}) in api.sent
    assert f"requested ({WID})" in text
    # Then on to GitHub, which the workspace does not hold up.
    writes = [p for _, p, _ in api.writes()]
    assert writes == ["/v1/workspace", f"/v1/access/grants/{api.repo_id}/verify"]
    assert code == sc.EXIT_TROUBLE, "not ready until an admin approves"


def test_setup_requests_on_a_blank_answer_and_not_on_no():
    api = FakeApi(has_account=True)
    _wizard(api, "")
    assert ("POST", "/v1/workspace", {"via": "plugin"}) in api.sent

    api = FakeApi(has_account=True)
    _, text, _ = _wizard(api, "n")
    assert not any(p == "/v1/workspace" for m, p, _ in api.sent if m == "POST")
    assert "no task or workflow can start" in text


def test_setup_skips_the_request_when_the_workspace_is_ready():
    api = FakeApi(state="ready", has_account=True)
    code, text, ask = _wizard(api)

    assert not any("workspace" in prompt.lower() for prompt in ask.asked)
    assert not any("/v1/workspace" in p for _, p, _ in api.sent)
    assert f"[x] workspace         ready ({WID})" in text
    assert code == sc.EXIT_OK


@pytest.mark.parametrize("state", ["requested", "approved", "applying", "needs_owner"])
def test_a_waiting_or_applying_record_prints_its_state_and_id_and_moves_on(state):
    api = FakeApi(state=state, has_account=True)
    _, text, ask = _wizard(api)

    assert not any("Request your workspace" in prompt for prompt in ask.asked)
    assert not any(p.startswith("/v1/workspace") for m, p, _ in api.sent if m == "POST")
    assert f"Workspace {state.replace('_', ' ')} ({WID})" in text
    # GitHub is still verified: the wait does not stop the rest of setup.
    assert ("POST", f"/v1/access/grants/{api.repo_id}/verify", {}) in api.sent


def test_no_claude_account_offers_a_loan_or_skip():
    api = FakeApi(state="ready")
    _, text, ask = _wizard(api, "loan")
    assert any(prompt.rstrip().endswith("[loan/skip]") for prompt in ask.asked)
    assert ("POST", "/v1/workspace/loan-request", {"via": "plugin"}) in api.sent
    assert "claude_account    loan requested" in text

    api = FakeApi(state="ready")
    _, text, _ = _wizard(api, "skip")
    assert not any(p == "/v1/workspace/loan-request" for _, p, _ in api.sent)
    assert "https://console.invalid/setup#claude-account" in text


def test_a_claude_account_already_held_or_a_loan_already_asked_is_not_offered_again():
    for api in (FakeApi(state="ready", has_account=True),
                FakeApi(state="ready", loan="requested")):
        _, _, ask = _wizard(api)
        assert not any("[loan/skip]" in prompt for prompt in ask.asked)
        assert not any(p == "/v1/workspace/loan-request" for _, p, _ in api.sent)


# -- the refusal on every submission ------------------------------------------------


def _speak(*messages):
    import contextlib

    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server.serve(stdin=stdin)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


SUBMISSIONS = [
    ("swarm_dispatch", {"prompt": "fix the flaky test"}),
    ("swarm_workflow", {"steps": [{"step_id": "a", "prompt": "a"}]}),
    ("swarm_run_issue", {"issue": "example-user/example-api#5"}),
]


@pytest.mark.parametrize("code", sorted(REFUSALS))
@pytest.mark.parametrize("tool,arguments", SUBMISSIONS, ids=[t for t, _ in SUBMISSIONS])
def test_a_plugin_submission_refused_for_setup_prints_the_message_and_the_command(
        monkeypatch, tool, arguments, code):
    refusal = _api_refusal(code, REFUSALS[code], _detail(code))
    api = FakeApi(submit_error=refusal)
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: _client(api))

    replies = _speak({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": tool, "arguments": arguments}})

    result = replies[0]["result"]
    assert result["isError"] is True
    lines = result["content"][0]["text"].splitlines()
    assert lines[0] == f"✕ 403 {code}: {REFUSALS[code]}", "the API's message, word for word"
    assert lines[1:] == ["  Finish setup with /sc:setup."]


@pytest.mark.parametrize("code", sorted(REFUSALS))
def test_sc_run_refused_for_setup_prints_the_message_and_sc_setup_and_exits_non_zero(
        monkeypatch, capsys, code):
    refusal = _api_refusal(code, REFUSALS[code], _detail(code))
    api = FakeApi(submit_error=refusal)

    class _Held:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return _client(api)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sc, "SwarmClient", _Held)
    exit_code = sc.main(["run", "--issue", "example-user/example-api#5"], out=io.StringIO())

    err = capsys.readouterr().err.splitlines()
    assert exit_code != 0
    assert err[0] == f"✕ 403 {code}: {REFUSALS[code]}"
    assert err[1:] == [f"  Finish setup with `{terminal_command('sc setup')}`."]
    assert ("POST", "/v1/runs") in [(m, p) for m, p, _ in api.sent]


def test_another_403_is_not_dressed_as_a_setup_refusal():
    other = _api_refusal("TENANT_FORBIDDEN", "Not your tenant.", {})
    assert sc.setup_refusal_text(other, "/sc:setup") is None
    assert "Finish setup" not in server._tool_error_text(other)


# -- /sc:setup ------------------------------------------------------------------------


def _setup_md() -> str:
    return (Path(__file__).resolve().parents[3] / "plugin" / "commands" / "setup.md").read_text()


def test_setup_md_grants_the_workspace_tool_under_both_prefixes():
    front = _setup_md().split("---", 2)[1]
    assert "mcp__swarmcloud__swarm_setup_workspace" in front
    assert "mcp__plugin_sc_swarmcloud__swarm_setup_workspace" in front


def test_setup_md_puts_the_workspace_then_the_claude_account_after_where_it_stands():
    body = _setup_md().split("---", 2)[2]
    stands = body.index("**Where it stands.**")
    workspace = body.index("**Your workspace.**")
    account = body.index("**A Claude account.**")
    github = body.index("**Connect GitHub**")
    assert stands < workspace < account < github
    assert "swarm_setup_workspace" in body[workspace:account]
    assert "`loan`" in body[account:github]
