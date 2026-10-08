"""Choosing which of your tenants the bridge acts as (#447, CLI and MCP half).

The API half is #501/#617: `X-Swarm-Tenant` selects one of the caller's
verified memberships and `GET /v1/tenants/mine` lists them. These hold the
bridge to the three things that half relies on:

* THE HEADER IS SENT ONLY WHEN A TENANT WAS CHOSEN. With no choice, the
  request is byte-for-byte what it was, and the API's first-match rule picks
  the tenant exactly as before.
* `--tenant` beats `SWARM_TENANT`, and an empty value is no choice at all.
* A refusal of the choice is the API's own sentence, unchanged: the bridge
  never decides membership itself (the header selects; it never grants).

Offline: the transport is `client._open`, replaced; nothing leaves the process.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import urllib.error

import pytest

from swarm_mcp import cli, server
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient, SwarmError

MINE = [
    {"tenant_id": "team-a", "display_name": "team-a@example.com"},
    {"tenant_id": "team-b", "display_name": "team-b@example.com"},
]


def _me(tenant_id: str) -> dict:
    return {"tenant": {"tenant_id": tenant_id}, "principal": {"email": "dev@example.com"}}


class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def wire(monkeypatch):
    """Every request the real `SwarmClient` sends, headers lowercased."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    # An explicit token, built at runtime: the tier needs one and no test here
    # is about which.
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    sent: list[dict] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        headers = {k.lower(): v for k, v in req.header_items()}
        sent.append({"method": req.get_method(), "url": req.full_url, "headers": headers})
        if req.full_url.endswith("/v1/tenants/mine"):
            return _Response(MINE)
        if req.full_url.endswith("/v1/tenants/me"):
            return _Response(_me(headers.get("x-swarm-tenant") or "team-a"))
        return _Response({"ok": True})

    monkeypatch.setattr(mcp_client, "_open", _opener)
    return sent


# --------------------------------------------------------------------------
# The client: the header, only when chosen
# --------------------------------------------------------------------------


def test_no_tenant_header_by_default(wire):
    SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks/t1")
    assert "x-swarm-tenant" not in wire[0]["headers"], wire[0]["headers"]


def test_the_header_is_sent_when_a_tenant_is_chosen(wire):
    client = SwarmClient(base_url="http://api.invalid", tenant="team-b")
    client.request("GET", "/v1/tasks/t1")
    client.request("POST", "/v1/tasks", payload={"prompt": "x"})
    assert [s["headers"].get("x-swarm-tenant") for s in wire] == ["team-b", "team-b"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_an_empty_choice_is_no_choice(wire, blank):
    """An empty header would be a request the API reads as "absent" anyway,
    but sending one at all is a difference on the wire for nothing."""
    SwarmClient(base_url="http://api.invalid", tenant=blank).request("GET", "/v1/tasks/t1")
    assert "x-swarm-tenant" not in wire[0]["headers"]


def test_the_client_does_not_read_swarm_tenant_itself(wire, monkeypatch):
    """The choice is made ONCE, by the CLI or at server start, and handed in.
    A client that read the environment on its own would make `sc` -- which
    never offered the option -- act as another tenant without saying so."""
    monkeypatch.setenv("SWARM_TENANT", "team-b")
    SwarmClient(base_url="http://api.invalid").request("GET", "/v1/tasks/t1")
    assert "x-swarm-tenant" not in wire[0]["headers"]


def test_a_refused_choice_surfaces_the_apis_error_unchanged(monkeypatch):
    sentence = (
        "X-Swarm-Tenant names a tenant you are not a verified member of. It can only "
        "select one of the tenants GET /v1/tenants/mine lists; send no X-Swarm-Tenant "
        "to use your default tenant."
    )
    body = json.dumps({"code": "tenant_not_member", "message": sentence}).encode()

    def _opener(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    monkeypatch.setattr(mcp_client, "_open", _opener)
    with pytest.raises(SwarmError) as exc:
        SwarmClient(base_url="http://api.invalid", tenant="team-z").tenants_mine()
    assert exc.value.status == 403 and exc.value.code == "tenant_not_member"
    assert sentence in str(exc.value)


def test_tenants_mine_reads_the_route(wire):
    assert SwarmClient(base_url="http://api.invalid").tenants_mine() == MINE
    assert wire[0]["method"] == "GET" and wire[0]["url"].endswith("/v1/tenants/mine")


def test_tenants_mine_refuses_an_answer_that_is_not_a_list(monkeypatch):
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    monkeypatch.setattr(mcp_client, "_open", lambda req, timeout=None: _Response({"x": 1}))
    with pytest.raises(SwarmError, match="not a list"):
        SwarmClient(base_url="http://api.invalid").tenants_mine()


# --------------------------------------------------------------------------
# The CLI: --tenant, SWARM_TENANT, and `swarm tenants`
# --------------------------------------------------------------------------


@pytest.fixture()
def built(monkeypatch):
    """The keyword arguments `main` builds its client with, and a fake client."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    seen: dict = {}

    class Fake:
        def __init__(self, *a, **k) -> None:
            seen.update(k)
            self.tenant = k.get("tenant")
            self.sent: list[tuple[str, str]] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def request(self, method, path, **kw):
            self.sent.append((method, path))
            if path == "/v1/tenants/me":
                return _me(self.tenant or "team-a")
            raise AssertionError(f"unexpected {method} {path}")

        def tenants_mine(self):
            self.sent.append(("GET", "/v1/tenants/mine"))
            return MINE

    monkeypatch.setattr(cli, "SwarmClient", Fake)
    return seen


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_the_cli_chooses_no_tenant_by_default(built):
    assert _run_cli(["tenants"])[0] == 0
    assert built.get("tenant") is None


def test_the_cli_honours_swarm_tenant(built, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "team-b")
    assert _run_cli(["tenants"])[0] == 0
    assert built["tenant"] == "team-b"


def test_the_tenant_flag_beats_swarm_tenant(built, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "team-a")
    assert _run_cli(["--tenant", "team-b", "tenants"])[0] == 0
    assert built["tenant"] == "team-b"


def test_an_empty_swarm_tenant_is_no_choice(built, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "  ")
    assert _run_cli(["tenants"])[0] == 0
    assert built.get("tenant") is None


def test_chosen_tenant_precedence():
    env = {"SWARM_TENANT": "team-a"}
    assert mcp_client.chosen_tenant("team-b", env) == "team-b"
    assert mcp_client.chosen_tenant(None, env) == "team-a"
    assert mcp_client.chosen_tenant("", env) == "team-a"
    assert mcp_client.chosen_tenant(None, {}) is None
    assert mcp_client.chosen_tenant(" team-b ", {}) == "team-b"


def test_swarm_tenants_lists_mine_and_marks_the_default(built):
    code, out, _ = _run_cli(["tenants"])
    assert code == 0
    rows = [line for line in out.splitlines() if "team-" in line and "@" in line]
    assert len(rows) == 2, out
    (current,) = [r for r in rows if r.startswith("*")]
    assert "team-a" in current and "team-a@example.com" in current
    assert not any(r.startswith("*") for r in rows if "team-b" in r)
    assert "--tenant" in out and "SWARM_TENANT" in out


def test_swarm_tenants_marks_the_chosen_one(built):
    code, out, _ = _run_cli(["--tenant", "team-b", "tenants"])
    assert code == 0
    (current,) = [line for line in out.splitlines() if line.startswith("*")]
    assert "team-b" in current
    assert "chosen" in current


def test_swarm_tenants_json(built):
    code, out, _ = _run_cli(["tenants", "--json"])
    assert code == 0
    listing = json.loads(out)
    assert listing["current"] == "team-a" and listing["chosen"] is None
    assert [(t["tenant_id"], t["current"]) for t in listing["tenants"]] == [
        ("team-a", True), ("team-b", False),
    ]


def test_swarm_tenants_for_a_personal_tenant_says_there_is_nothing_to_choose(monkeypatch):
    class Personal:
        tenant = None

        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def request(self, method, path, **kw):
            return _me("u-dev")

        def tenants_mine(self):
            return []

    monkeypatch.delenv("SWARM_TENANT", raising=False)
    monkeypatch.setattr(cli, "SwarmClient", Personal)
    code, out, _ = _run_cli(["tenants"])
    assert code == 0
    assert "u-dev" in out and "personal" in out


def test_a_refused_tenant_is_the_apis_error_on_stderr(monkeypatch):
    sentence = "X-Swarm-Tenant names a tenant you are not a verified member of."

    class Refusing:
        tenant = "team-z"

        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def request(self, method, path, **kw):
            raise SwarmError(f"GET {path} -> 403: {sentence}", status=403, code="tenant_not_member")

        def tenants_mine(self):
            return self.request("GET", "/v1/tenants/mine")

    monkeypatch.setattr(cli, "SwarmClient", Refusing)
    code, out, err = _run_cli(["--tenant", "team-z", "tenants"])
    assert code == cli.EXIT_FAIL
    assert sentence in err and out == ""


# --------------------------------------------------------------------------
# The MCP server: a `tenant` setting read once, and `swarm_tenants`
# --------------------------------------------------------------------------


class _ToolClient:
    def __init__(self, tenant=None) -> None:
        self.tenant = tenant
        self.sent: list[tuple[str, str]] = []

    def request(self, method, path, **kw):
        self.sent.append((method, path))
        if path == "/v1/tenants/me":
            return _me(self.tenant or "team-a")
        raise AssertionError(f"unexpected {method} {path}")

    def tenants_mine(self):
        self.sent.append(("GET", "/v1/tenants/mine"))
        return MINE


def test_swarm_tenants_is_advertised_read_only_and_takes_no_arguments():
    tool = next((t for t in server.TOOLS if t["name"] == "swarm_tenants"), None)
    assert tool is not None, "swarm_tenants is not in TOOLS, so no host lists it"
    assert not tool["inputSchema"].get("properties")
    assert "Read-only" in tool["description"]
    assert "SWARM_TENANT" in tool["description"]


def test_swarm_tenants_output_marks_the_current_tenant():
    client = _ToolClient()
    reply = json.loads(server._call(client, "swarm_tenants", {}))
    assert reply["current"] == "team-a" and reply["chosen"] is None
    assert reply["tenants"] == [
        {"tenant_id": "team-a", "display_name": "team-a@example.com", "current": True},
        {"tenant_id": "team-b", "display_name": "team-b@example.com", "current": False},
    ]
    assert all(method == "GET" for method, _ in client.sent), "read-only"


def test_swarm_tenants_output_with_a_chosen_tenant():
    reply = json.loads(server._call(_ToolClient("team-b"), "swarm_tenants", {}))
    assert reply["current"] == "team-b" and reply["chosen"] == "team-b"
    assert [t["tenant_id"] for t in reply["tenants"] if t["current"]] == ["team-b"]


def test_swarm_tenants_refuses_a_per_call_tenant():
    """The setting is the session's: a per-call `tenant` would let one call act
    as another tenant than the rows it is reading came from."""
    with pytest.raises(SwarmError, match="does not take"):
        server._call(_ToolClient(), "swarm_tenants", {"tenant": "team-b"})


def _serve_with(monkeypatch, lines) -> tuple[list[dict], list[dict]]:
    built: list[dict] = []

    def _factory(*a, **k):
        built.append(k)
        return _ToolClient(k.get("tenant"))

    monkeypatch.setattr(server, "SwarmClient", _factory)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server.serve(stdin=lines)
    return built, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


_CALL = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "swarm_tenants", "arguments": {}}}
) + "\n"


def test_the_server_sends_no_tenant_without_the_setting(monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    built, replies = _serve_with(monkeypatch, io.StringIO(_CALL))
    assert built == [{"tenant": None}]
    assert json.loads(replies[0]["result"]["content"][0]["text"])["chosen"] is None


def test_the_server_reads_swarm_tenant_once_at_start(monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "team-b")

    def _lines():
        # Changed AFTER the server started: a setting read per call would
        # follow it, and a session would change tenant mid-flight.
        os.environ["SWARM_TENANT"] = "team-a"
        yield _CALL

    built, replies = _serve_with(monkeypatch, _lines())
    assert built == [{"tenant": "team-b"}]
    assert json.loads(replies[0]["result"]["content"][0]["text"])["current"] == "team-b"
