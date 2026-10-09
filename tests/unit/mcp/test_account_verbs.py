"""The account verbs the API serves and nothing exposed (S10, BUILD_PROMPT_V2 2.6.1).

`PUT /v1/accounts/{id}/state` and `DELETE /v1/accounts/{id}` existed; only
`list` reached `sc`, MCP and the plugin. These hold the four verbs and `add`
to: resolving a LABEL to the caller's OWN account, refusing an ambiguous one,
sending exactly one request per verb, a removal that needs the label typed
back, and a sign-in whose pasted code is sent once and printed never.

The fake below records every request it is sent, so "nothing was sent" is a
measurement of an empty list, not an absence of evidence.
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading

import pytest

from swarm_mcp import sc, server
from swarm_mcp.client import SwarmError

TENANT = "acme"

OWN = [
    {"account_id": "acct_1", "owner_tenant": TENANT, "label": "work", "state": "AVAILABLE"},
    {"account_id": "acct_2", "owner_tenant": TENANT, "label": "spare", "state": "AVAILABLE"},
    {"account_id": "acct_3", "owner_tenant": TENANT, "label": "twin", "state": "AVAILABLE"},
    {"account_id": "acct_4", "owner_tenant": TENANT, "label": "twin", "state": "PAUSED"},
]
LENT = [{"account_id": "acct_9", "owner_tenant": "other", "label": "borrowed", "state": "AVAILABLE"}]

#: Shaped like the pasted code and the server-minted state; obviously fake.
CODE = "fake-pasted-code-0000#fake-state-1111"
STATE = "fake-state-1111"


class FakeApi:
    def __init__(self, *, exchange_error=None):
        self.sent: list[tuple[str, str, object]] = []
        self.exchange_error = exchange_error

    def request(self, method, path, payload=None, **kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("GET", "/v1/accounts"):
            return {"tenant_id": TENANT, "accounts": OWN + LENT}
        if method == "PUT" and path.endswith("/state"):
            account_id = path.split("/")[3]
            return {"account": {"account_id": account_id, "state": payload["state"], "label": "work"}}
        if method == "DELETE":
            return {"removed": path.split("/")[3], "secret": "retained"}
        if (method, path) == ("POST", "/v1/accounts/authorize"):
            return {
                "authorize_url": f"https://claude.example.test/oauth/authorize?state={STATE}",
                "state": STATE,
                "expires_in_seconds": 900,
            }
        if (method, path) == ("POST", "/v1/accounts/exchange"):
            if self.exchange_error is not None:
                raise self.exchange_error
            return {
                "account": {"account_id": "acct_new", "label": payload and "fresh", "state": "AVAILABLE"},
                "expires_at": "2026-10-01T12:00:00Z",
                "note": "stored write-only; no read path in this API can return key material",
            }
        raise SwarmError(f"{method} {path} -> 500: unexpected", status=500)

    def writes(self):
        return [(m, p, b) for m, p, b in self.sent if m != "GET"]


def run(argv, api, *, stdin=""):
    out = io.StringIO()
    args = sc.build_parser().parse_args(argv)
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        code = args.func(api, args, out)
    finally:
        sys.stdin = old
    return code, out.getvalue()


# -- label resolution ---------------------------------------------------------


def test_a_label_resolves_to_the_callers_own_account_id():
    assert sc.resolve_account(FakeApi(), "work")["account_id"] == "acct_1"


def test_an_id_resolves_to_itself():
    assert sc.resolve_account(FakeApi(), "acct_2")["label"] == "spare"


def test_an_ambiguous_label_is_refused_with_every_id_it_could_be():
    with pytest.raises(SwarmError) as refused:
        sc.resolve_account(FakeApi(), "twin")
    assert "acct_3" in str(refused.value) and "acct_4" in str(refused.value)


def test_a_lent_account_is_not_the_callers_to_change():
    with pytest.raises(SwarmError) as refused:
        sc.resolve_account(FakeApi(), "borrowed")
    assert "other" in str(refused.value) and "owner" in str(refused.value)


def test_an_unknown_label_names_the_callers_own_labels():
    with pytest.raises(SwarmError) as refused:
        sc.resolve_account(FakeApi(), "nope")
    assert "work" in str(refused.value) and "spare" in str(refused.value)


# -- each verb's request --------------------------------------------------------


@pytest.mark.parametrize("verb,state", [("pause", "PAUSED"), ("resume", "AVAILABLE"), ("drain", "DRAINING")])
def test_each_state_verb_sends_one_put_for_the_resolved_account(verb, state):
    api = FakeApi()
    code, out = run(["account", verb, "work", "--reason", "rotating"], api)
    assert code == sc.EXIT_OK, out
    assert api.writes() == [("PUT", "/v1/accounts/acct_1/state", {"state": state, "reason": "rotating"})]
    # The broker's answer, verbatim.
    assert json.dumps({"account": {"account_id": "acct_1", "state": state, "label": "work"}}, indent=2) in out


def test_drain_says_what_draining_does_from_one_constant():
    _, out = run(["account", "drain", "work"], FakeApi())
    assert sc.DRAINING_MEANS in out
    for words in ("next turn boundary", "checkpoints and parks", "new assignments"):
        assert words in sc.DRAINING_MEANS


def test_an_ambiguous_label_sends_nothing():
    api = FakeApi()
    with pytest.raises(SwarmError):
        run(["account", "pause", "twin"], api)
    assert api.writes() == []


# -- remove: typed confirmation -------------------------------------------------


def test_remove_sends_the_delete_only_when_the_label_is_typed_back(monkeypatch):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi()
    code, out = run(["account", "remove", "acct_1"], api, stdin="work\n")
    assert code == sc.EXIT_OK
    assert api.writes() == [("DELETE", "/v1/accounts/acct_1", None)]
    # Verbatim, including that the secret is kept.
    assert '"secret": "retained"' in out


@pytest.mark.parametrize("typed", ["", "y\n", "yes\n", "acct_1\n", "Work\n"])
def test_remove_refuses_anything_but_the_label_even_with_assume_yes(monkeypatch, typed):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        run(["account", "remove", "work"], api, stdin=typed)
    assert "nothing was" in str(refused.value).lower()
    assert api.writes() == []


# -- the MCP remove: confirmed by a person, never by its caller (finding 83) ----
#
# It took `confirm_label` and its refusal named the label it wanted, so the
# agent that called it could call again with that label and confirm its own
# destructive action. Now the bridge asks the host's human (MCP elicitation).


@pytest.fixture
def host(monkeypatch):
    """A host that can ask its human; `answers` is what the human gives, and
    `asked` every prompt the bridge put to them."""
    asked: list[tuple[str, dict]] = []
    answers: list = []

    def _elicit(message, schema):
        asked.append((message, schema))
        return answers.pop(0)

    monkeypatch.setattr(server, "_elicit", _elicit)
    server._HOST_CAN_ELICIT.set()
    yield asked, answers
    server._HOST_CAN_ELICIT.clear()


def _accepted(label):
    return {"jsonrpc": "2.0", "id": "x", "result": {"action": "accept", "content": {"label": label}}}


def _names_the_label(text):
    return re.search(r"\bwork\b", text) is not None


def test_the_mcp_remove_deletes_only_when_the_human_types_the_label(host):
    asked, answers = host
    api = FakeApi()
    answers.append(_accepted("work"))
    body = json.loads(server._call(api, "swarm_account_remove", {"account": "acct_1"}))
    assert body["broker_answer"] == {"removed": "acct_1", "secret": "retained"}
    assert api.writes() == [("DELETE", "/v1/accounts/acct_1", None)]
    # The PERSON is shown which account; the caller never is.
    ((message, schema),) = asked
    assert _names_the_label(message) and "acct_1" in message
    assert schema["required"] == ["label"]


@pytest.mark.parametrize(
    "answer",
    [
        _accepted("spare"),
        _accepted("Work"),
        _accepted(""),
        {"jsonrpc": "2.0", "id": "x", "result": {"action": "decline"}},
        {"jsonrpc": "2.0", "id": "x", "result": {"action": "cancel"}},
        {"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "no"}},
        None,  # nobody answered in time
    ],
    ids=["other-label", "wrong-case", "empty", "decline", "cancel", "host-error", "timeout"],
)
def test_anything_but_the_typed_label_removes_nothing_and_never_names_it(host, answer):
    _, answers = host
    api = FakeApi()
    answers.append(answer)
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_account_remove", {"account": "work"})
    assert api.writes() == []
    assert "nothing was removed" in str(refused.value)
    assert not _names_the_label(str(refused.value)), str(refused.value)


def test_a_caller_cannot_supply_the_confirmation_as_an_argument(host):
    """The argument the old tool took is refused, not read: an agent that
    passes the label it read from `swarm_accounts` confirms nothing."""
    asked, _ = host
    api = FakeApi()
    with pytest.raises(SwarmError, match="does not take"):
        server._call(api, "swarm_account_remove", {"account": "work", "confirm_label": "work"})
    assert api.sent == [] and asked == []
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_account_remove")["inputSchema"]
    assert set(schema["properties"]) == {"account"}


def test_a_host_that_cannot_ask_its_human_removes_nothing_and_names_the_terminal_command():
    server._HOST_CAN_ELICIT.clear()
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_account_remove", {"account": "work"})
    text = str(refused.value)
    assert api.writes() == []
    assert "sc account remove acct_1" in text and "nothing was removed" in text
    assert not _names_the_label(text), text


class _Host:
    """stdin and stdout of a real host: it answers the bridge's elicitation
    with what its human typed, once the bridge has asked."""

    def __init__(self, typed, *, can_ask=True):
        self.typed = typed
        self.can_ask = can_ask
        self.lines: list[dict] = []
        self.asked = threading.Event()

    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                message = json.loads(line)
                self.lines.append(message)
                if message.get("method") == "elicitation/create":
                    self.asked.set()

    def flush(self):
        pass

    def __iter__(self):
        capabilities = {"elicitation": {}} if self.can_ask else {}
        yield json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": capabilities}})
        yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "swarm_account_remove", "arguments": {"account": "work"}}})
        if self.can_ask:
            assert self.asked.wait(10), "the bridge never asked"
            request = next(m for m in self.lines if m.get("method") == "elicitation/create")
            # An unrelated response first: it is dropped, and answers nothing.
            yield json.dumps({"jsonrpc": "2.0", "id": "someone-else", "result": {}})
            yield json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "result": {"action": "accept", "content": {"label": self.typed}}})

    def answer(self):
        return next(m for m in self.lines if m.get("id") == 1)["result"]


@pytest.mark.parametrize("typed,removed", [("work", True), ("spare", False)])
def test_the_served_bridge_asks_the_host_and_acts_on_the_human_answer(monkeypatch, typed, removed):
    api = FakeApi()
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: api)
    host = _Host(typed)
    monkeypatch.setattr(sys, "stdout", host)
    try:
        server.serve(stdin=host)
    finally:
        server._HOST_CAN_ELICIT.clear()
    answer = host.answer()
    assert api.writes() == ([("DELETE", "/v1/accounts/acct_1", None)] if removed else [])
    assert answer.get("isError", False) is (not removed), answer
    assert [m["id"] for m in host.lines if "id" in m and "method" not in m].count("someone-else") == 0


def test_the_served_bridge_without_elicitation_sends_nothing(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(server, "SwarmClient", lambda *a, **k: api)
    host = _Host("work", can_ask=False)
    monkeypatch.setattr(sys, "stdout", host)
    server.serve(stdin=host)
    answer = host.answer()
    assert answer["isError"] is True and "sc account remove" in answer["content"][0]["text"]
    assert api.writes() == []
    assert not any(m.get("method") == "elicitation/create" for m in host.lines)


@pytest.mark.parametrize("tool,state", [
    ("swarm_account_pause", "PAUSED"), ("swarm_account_resume", "AVAILABLE"), ("swarm_account_drain", "DRAINING"),
])
def test_the_mcp_state_verbs_send_one_put(tool, state):
    api = FakeApi()
    body = json.loads(server._call(api, tool, {"account": "spare"}))
    assert api.writes() == [("PUT", "/v1/accounts/acct_2/state", {"state": state, "reason": ""})]
    assert body["broker_answer"]["account"]["state"] == state
    if state == "DRAINING":
        assert body["what_draining_does"] == sc.DRAINING_MEANS


def test_the_account_writers_are_not_granted_to_the_read_only_skill():
    """`sc` is read-only towards the cluster; the account verbs write to it, so
    no skill may be granted them. The grant tests hold the handlers; this holds
    the MCP names."""
    from pathlib import Path

    skill = (Path(__file__).resolve().parents[3] / "plugin" / "skills" / "sc" / "SKILL.md").read_text()
    front = skill.split("---", 2)[1]
    for tool in ("swarm_account_pause", "swarm_account_resume", "swarm_account_drain", "swarm_account_remove"):
        assert tool not in front


# -- add: the API's own OAuth flow ----------------------------------------------


@pytest.fixture
def browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(sc, "_open_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(sc, "_read_code", lambda prompt: CODE)
    return opened


def test_add_authorizes_then_exchanges_and_sends_the_code_only_in_the_exchange(browser, capsys):
    api = FakeApi()
    code, out = run(["account", "add", "--label", "fresh", "--lend-to", "beta", "--lend-to", "gamma"], api)
    assert code == sc.EXIT_OK, out
    assert api.sent == [
        ("POST", "/v1/accounts/authorize", {"label": "fresh", "lend_to": ["beta", "gamma"]}),
        ("POST", "/v1/accounts/exchange", {"state": STATE, "code": CODE}),
    ]
    assert browser == [f"https://claude.example.test/oauth/authorize?state={STATE}"]
    assert "acct_new" in out and "fresh" in out and "AVAILABLE" in out
    assert "stored write-only; no read path in this API can return key material" in out
    err = capsys.readouterr().err
    _no_secret_shown(out + err)


def test_add_never_uses_the_credential_upload_route(browser):
    api = FakeApi()
    run(["account", "add", "--label", "fresh"], api)
    assert ("POST", "/v1/accounts") not in [(m, p) for m, p, _ in api.sent]


def test_a_failed_exchange_prints_the_error_with_the_code_masked(browser, capsys):
    refusal = SwarmError(
        f"POST /v1/accounts/exchange -> 422: invalid code {CODE} for state {STATE}", status=422
    )
    api = FakeApi(exchange_error=refusal)
    with pytest.raises(SwarmError) as failed:
        run(["account", "add", "--label", "fresh"], api)
    shown = str(failed.value)
    assert "422" in shown
    _no_secret_shown(shown)
    _no_secret_shown(capsys.readouterr().err)


def test_a_missing_label_is_refused_before_any_request(browser):
    api = FakeApi()
    with pytest.raises(SystemExit):
        run(["account", "add"], api)
    with pytest.raises(SwarmError):
        run(["account", "add", "--label", "  "], api)
    assert api.sent == []


def test_an_empty_paste_sends_no_exchange(monkeypatch):
    monkeypatch.setattr(sc, "_open_browser", lambda url: True)
    monkeypatch.setattr(sc, "_read_code", lambda prompt: "   ")
    api = FakeApi()
    with pytest.raises(SwarmError):
        run(["account", "add", "--label", "fresh"], api)
    assert [p for _, p, _ in api.sent] == ["/v1/accounts/authorize"]


def test_the_code_is_read_without_echo():
    import inspect

    assert "getpass" in inspect.getsource(sc._read_code)


def _no_secret_shown(text: str) -> None:
    """The code never appears, in any part; the state never appears outside
    the authorize URL, which carries it because Anthropic's page requires it."""
    for part in (CODE, CODE.split("#")[0]):
        assert part not in text, text
    assert STATE not in text.replace(f"?state={STATE}", "?state=<url>"), text
