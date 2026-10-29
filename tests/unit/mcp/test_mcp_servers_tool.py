"""The bridge carries a step's registered MCP servers and its per-run
credentials, and nothing else of the feature (MCP-SC, part of #1433).

docs/design/registered-mcp-servers.md §4.3 and §4.11, build plan row MCP-SC:

* `swarm_dispatch` (one task, or each task of `tasks`) and `swarm_workflow`
  send `input.mcp_servers` -- a declared input of claude-code and codex,
  contract request 88 -- and a top-level `mcp_credentials` map to swarm-api,
  exactly as given. swarm-api is the authority on every rule after that.
* NO TOOL SCHEMA ACCEPTS A REGISTRATION OR A STORE VALUE: registering a server
  and storing a long-lived credential are the console's and the API's, never a
  session's. The one MCP-servers tool reads.
* THE PLUGIN'S LOG OF A DISPATCH HOLDS NO CREDENTIAL: not the reply, not the
  run copy or `launched.jsonl` a dispatch writes, not a refusal -- even when
  the API answering it echoed the map back, which swarm-api never does.

Every fake credential is built at runtime (`secrets.token_hex`), so nothing
in this file looks like one.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

import pytest

from swarm_mcp import sc, server, workflows
from swarm_mcp.client import SwarmError

_PACKAGE = Path(server.__file__).resolve().parent
_REPO = Path(__file__).resolve().parents[3]


def _credential() -> str:
    return "kbtok-" + secrets.token_hex(24)


class RecordingApi:
    """A control plane that RECORDS each body it was sent and, unlike
    swarm-api, ECHOES every body back in its answer: the worst API this
    bridge could meet, so a credential that reaches a reply or a file got
    there through this bridge's own handling of the answer."""

    def __init__(self, servers: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.dispatched: list[dict[str, Any]] = []
        self.batches: list[list[dict[str, Any]]] = []
        self.servers = servers or []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        if method == "POST" and path == "/v1/tasks":
            return {"task": {"task_id": "task_1", "state": "QUEUED", "tenant_id": "eng", **payload}}
        if method == "POST" and path == "/v1/workflows":
            steps = [
                {"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                 "runner_profile": s.get("runner_profile"), "input": s.get("input")}
                for s in payload["steps"]
            ]
            return {"workflow": {"workflow_id": "wf_1", "tenant_id": "eng", "steps": steps, **payload},
                    "dispatch": {"strategy": "collect"}, "tenant_id": "eng"}
        if method == "GET" and path == "/v1/mcp-servers":
            return {"tenant_id": "eng", "can_manage": False, "servers": self.servers,
                    "last_attached_complete": True, "egress_addresses": ["203.0.113.7"],
                    "note": "swarm-api never calls a registered URL"}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        self.dispatched.append(kwargs)
        return {"task_id": f"task_{len(self.dispatched)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.batches.append(list(payloads))
        return [{"task_id": f"task_b{i}", "state": "QUEUED", **p} for i, p in enumerate(payloads, start=1)]


def _posted(api: RecordingApi, path: str) -> list[Any]:
    return [payload for method, path_, payload in api.sent if method == "POST" and path_ == path]


def _files_holding(text: str, *roots: Path) -> list[str]:
    found = []
    visited = 0
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                visited += 1
                if text in path.read_text(encoding="utf-8", errors="replace"):
                    found.append(str(path))
    assert visited, f"nothing was written under {roots}: the search proves nothing"
    return found


# --------------------------------------------------------------------------
# Passed through
# --------------------------------------------------------------------------


def test_swarm_dispatch_sends_mcp_servers_and_the_credential_map_as_given():
    api = RecordingApi()
    value = _credential()
    server._call(api, "swarm_dispatch", {
        "prompt": "ask kb", "profile": "claude-code",
        "inputs": {"mcp_servers": ["kb"]},
        "mcp_credentials": {"kb": {"value": value, "expires_in": 3600}},
    })
    (body,) = _posted(api, "/v1/tasks")
    assert body["input"]["mcp_servers"] == ["kb"]
    assert body["input"]["prompt"] == "ask kb"
    assert body["runner_profile"] == "claude-code"
    assert body["mcp_credentials"] == {"kb": {"value": value, "expires_in": 3600}}
    assert value not in json.dumps(body["input"]), "the credential travelled inside input"


def test_a_dispatch_without_credentials_is_sent_exactly_as_before():
    """Dark means unchanged: a dispatch naming no credential takes the path
    every dispatch took before this lane, and carries no `mcp_credentials`."""
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "plain", "profile": "claude-code"})
    assert not _posted(api, "/v1/tasks")
    (sent,) = api.dispatched
    assert "mcp_credentials" not in sent and sent["prompt"] == "plain"


def test_each_task_of_a_batch_carries_its_own_credentials():
    api = RecordingApi()
    first, second = _credential(), _credential()
    server._call(api, "swarm_dispatch", {"tasks": [
        {"prompt": "a", "inputs": {"mcp_servers": ["kb"]},
         "mcp_credentials": {"kb": {"value": first, "expires_in": 600}}},
        {"prompt": "b", "inputs": {"mcp_servers": ["notes"]},
         "mcp_credentials": {"notes": {"value": second, "expires_in": 900}}},
        {"prompt": "c"},
    ]})
    (batch,) = api.batches
    assert batch[0]["input"]["mcp_servers"] == ["kb"]
    assert batch[0]["mcp_credentials"] == {"kb": {"value": first, "expires_in": 600}}
    assert batch[1]["mcp_credentials"] == {"notes": {"value": second, "expires_in": 900}}
    assert "mcp_credentials" not in batch[2]


def test_a_workflow_step_names_servers_and_the_workflow_carries_one_map():
    api = RecordingApi()
    value = _credential()
    server._call(api, "swarm_workflow", {
        "steps": [
            {"step_id": "ask", "prompt": "ask kb", "inputs": {"mcp_servers": ["kb"]}},
            {"step_id": "write", "prompt": "record it", "depends_on": ["ask"],
             "inputs": {"mcp_servers": ["kb"]}},
        ],
        "mcp_credentials": {"kb": {"value": value, "expires_in": 3600}},
    })
    (body,) = _posted(api, "/v1/workflows")
    assert [s["input"]["mcp_servers"] for s in body["steps"]] == [["kb"], ["kb"]]
    assert body["mcp_credentials"] == {"kb": {"value": value, "expires_in": 3600}}
    assert value not in json.dumps(body["steps"])


def test_a_whole_spec_takes_the_credential_map_beside_it_never_inside_it():
    api = RecordingApi()
    value = _credential()
    spec = {"title": "kb run", "steps": [
        {"step_id": "ask", "prompt": "ask kb", "inputs": {"mcp_servers": ["kb"]}}]}
    created = json.loads(server._call(api, "swarm_workflow", {
        "spec": spec, "spec_digest": workflows.spec_digest(spec),
        "mcp_credentials": {"kb": {"value": value, "expires_in": 120}},
    }))
    assert created["spec_digest_checked"] is True, "the map is not part of what the digest covers"
    (body,) = _posted(api, "/v1/workflows")
    assert body["mcp_credentials"] == {"kb": {"value": value, "expires_in": 120}}

    inside = {**spec, "mcp_credentials": {"kb": {"value": value, "expires_in": 120}}}
    for tool, args in (
        ("swarm_workflow", {"spec": inside}),
        ("swarm_workflow_launch", {"spec": inside}),
    ):
        with pytest.raises(SwarmError) as refused:
            server._call(RecordingApi(), tool, args)
        text = str(refused.value)
        assert "mcp_credentials" in text and "beside" in text, text
        assert value not in text


def test_a_workflow_without_credentials_sends_no_map():
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"steps": [{"step_id": "a", "prompt": "a"}]})
    (body,) = _posted(api, "/v1/workflows")
    assert "mcp_credentials" not in body


@pytest.mark.parametrize("given,rule", [
    ("not-a-map", "an object"),
    ({"kb": "bare"}, "{value, expires_in}"),
    ({"kb": {"value": None, "expires_in": 60}}, "{value, expires_in}"),
    ({"kb": {"value": "VALUE", "expires_in": "soon"}}, "{value, expires_in}"),
    ({"kb": {"value": "VALUE", "expires_in": True}}, "{value, expires_in}"),
    ({"kb": {"value": "VALUE", "expires_in": 60, "header": "X-Key"}}, "{value, expires_in}"),
    ({"other": {"value": "VALUE", "expires_in": 60}}, "does not name"),
    ({f"s{i}": {"value": "VALUE", "expires_in": 60} for i in range(5)}, "at most 4"),
])
def test_a_malformed_map_is_refused_unsent_and_the_refusal_never_repeats_a_value(given, rule):
    value = _credential()
    given = json.loads(json.dumps(given).replace("VALUE", value))
    for tool, args in (
        ("swarm_dispatch", {"prompt": "x", "inputs": {"mcp_servers": ["kb"]}, "mcp_credentials": given}),
        ("swarm_workflow", {"steps": [{"step_id": "a", "prompt": "x", "inputs": {"mcp_servers": ["kb"]}}],
                            "mcp_credentials": given}),
        ("swarm_dispatch", {"tasks": [{"prompt": "x", "inputs": {"mcp_servers": ["kb"]},
                                       "mcp_credentials": given}]}),
    ):
        api = RecordingApi()
        with pytest.raises(SwarmError) as refused:
            server._call(api, tool, args)
        text = str(refused.value)
        assert rule in text, (tool, text)
        assert value not in text
        assert not api.dispatched and not api.batches and not _posted(api, "/v1/tasks") \
            and not _posted(api, "/v1/workflows"), "a refused map was sent"


# --------------------------------------------------------------------------
# No tool registers a server or stores a credential
# --------------------------------------------------------------------------


def _paths(schema: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(schema, dict):
        for key, sub in (schema.get("properties") or {}).items():
            out.append(f"{prefix}{key}")
            out.extend(_paths(sub, f"{prefix}{key}."))
        if isinstance(schema.get("items"), dict):
            out.extend(_paths(schema["items"], f"{prefix}[]."))
        if isinstance(schema.get("additionalProperties"), dict):
            out.extend(_paths(schema["additionalProperties"], f"{prefix}*."))
    return out


#: A registration's fields (swarm_api.routes.mcp_servers ServerCreate) and a
#: store write's: no tool's schema may take one, at any depth.
_REGISTRATION_OR_STORE = {
    "allowed_profiles", "allowed_tools", "auth", "header", "scheme", "transport",
    "limits", "secret", "secret_value", "consumer", "grants",
}


#: SSC-SC's tools: each mints a one-time console link or reads status, and
#: none has a field a value fits in (tests/unit/mcp/test_secret_links.py).
_SECRET_LINK_AND_READ_TOOLS = frozenset({
    "swarm_secret_set_link", "swarm_actions_secret_link", "swarm_secrets",
    "swarm_platform_secrets",
})


def test_no_tool_schema_accepts_a_registration_or_a_store_value():
    credential_paths = []
    for tool in server.TOOLS:
        for path in _paths(tool.get("inputSchema") or {}):
            leaf = path.rsplit(".", 1)[-1]
            assert leaf not in _REGISTRATION_OR_STORE, f"{tool['name']} takes `{path}`"
            if leaf == "value" or "mcp_credentials" in path:
                credential_paths.append(f"{tool['name']}:{path}")
        # Config › Secrets' link and read tools (SSC-SC, secrets-scopes.md §6.4)
        # are named for secrets and take no value: test_secret_links holds them.
        if tool["name"] in _SECRET_LINK_AND_READ_TOOLS:
            continue
        assert not re.search(r"mcp.*(register|create|update|set)|secret", tool["name"]), tool["name"]
    # The one credential a tool takes is the per-run map, passed to the API.
    assert sorted(credential_paths) == sorted([
        "swarm_dispatch:mcp_credentials",
        "swarm_dispatch:mcp_credentials.*.value",
        "swarm_dispatch:mcp_credentials.*.expires_in",
        "swarm_dispatch:tasks.[].mcp_credentials",
        "swarm_dispatch:tasks.[].mcp_credentials.*.value",
        "swarm_dispatch:tasks.[].mcp_credentials.*.expires_in",
        "swarm_workflow:mcp_credentials",
        "swarm_workflow:mcp_credentials.*.value",
        "swarm_workflow:mcp_credentials.*.expires_in",
    ]), credential_paths


def test_the_bridge_only_reads_the_mcp_server_registry_and_never_writes_a_workspace_secret():
    """Read off the source, because a write could hide behind any tool: the
    package names `/v1/mcp-servers` only in a GET, and the workspace secret
    store only as its one-time link mint (SSC-SC, secrets-scopes.md §6.4),
    which takes no value: never a route that stores one."""
    sources = {p.name: p.read_text(encoding="utf-8") for p in _PACKAGE.glob("*.py")}
    reads = [name for name, text in sources.items() if "/v1/mcp-servers" in text]
    assert reads == ["sc.py"], reads
    assert re.findall(r'request\(\s*"(\w+)",\s*"/v1/mcp-servers', sources["sc.py"]) == ["GET"]
    named = {name: sorted(set(re.findall(r"/v1/workspace/secrets[^\"'\s]*", text)))
             for name, text in sources.items() if "/v1/workspace/secrets" in text}
    assert named == {"secrets.py": ["/v1/workspace/secrets/links"]}, named


def test_swarm_mcp_servers_lists_by_name_and_reads_only():
    api = RecordingApi(servers=[
        {"name": "kb", "url": "https://kb.example.com/mcp", "description": "the graph",
         "auth": {"mode": "dispatch", "header": "Authorization", "scheme": "Bearer"},
         "allowed_profiles": ["claude-code"], "allowed_tools": ["search"],
         "enabled": True, "last_attached_at": None, "consumer": "mcp:kb"},
        {"name": "old", "url": "https://old.example.com/mcp", "auth": {"mode": "none"},
         "allowed_profiles": ["codex"], "allowed_tools": [], "enabled": False},
    ])
    listing = json.loads(server._call(api, "swarm_mcp_servers", {}))
    assert [(method, path) for method, path, _ in api.sent] == [("GET", "/v1/mcp-servers")]
    names = [s["name"] for s in listing["servers"]]
    assert names == ["kb", "old"]
    kb = listing["servers"][0]
    assert kb["auth_mode"] == "dispatch" and kb["needs_credential"] is True
    assert kb["allowed_tools"] == ["search"] and kb["enabled"] is True
    assert listing["servers"][1]["allowed_tools"] == "every tool"
    assert listing["egress_addresses"] == ["203.0.113.7"]
    assert "mcp_servers" in listing["use"] and "mcp_credentials" in listing["use"]
    assert "kb" in listing["text"] and "disabled" in listing["text"]


def test_sc_mcp_servers_is_the_same_read_in_a_terminal(monkeypatch):
    api = RecordingApi(servers=[{"name": "kb", "url": "https://kb.example.com/mcp",
                                 "auth": {"mode": "workspace_secret"}, "allowed_profiles": ["codex"],
                                 "allowed_tools": [], "enabled": True}])
    parsed = sc.build_parser().parse_args(["mcp-servers", "--json"])
    out = io.StringIO()
    assert parsed.func(api, parsed, out) == 0
    assert json.loads(out.getvalue())["servers"][0]["name"] == "kb"
    parsed = sc.build_parser().parse_args(["mcp-servers"])
    out = io.StringIO()
    assert parsed.func(api, parsed, out) == 0
    assert "kb" in out.getvalue() and "workspace_secret" in out.getvalue()


def test_the_sc_command_grants_the_view_by_name():
    text = (_REPO / "plugin" / "commands" / "sc.md").read_text(encoding="utf-8")
    assert "Bash(sc mcp-servers:*)" in text


# --------------------------------------------------------------------------
# The plugin's log of a dispatch holds no credential
# --------------------------------------------------------------------------


def test_no_reply_run_copy_or_record_of_a_dispatch_holds_the_credential(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    value = _credential()
    api = RecordingApi()
    replies = [
        server._call(api, "swarm_dispatch", {
            "prompt": "ask kb", "label": "kb lane", "inputs": {"mcp_servers": ["kb"]},
            "mcp_credentials": {"kb": {"value": value, "expires_in": 3600}}}),
        server._call(api, "swarm_dispatch", {"tasks": [
            {"prompt": "batch kb", "inputs": {"mcp_servers": ["kb"]},
             "mcp_credentials": {"kb": {"value": value, "expires_in": 3600}}}]}),
        server._call(api, "swarm_workflow", {
            "title": "kb flow",
            "steps": [{"step_id": "ask", "prompt": "ask kb", "inputs": {"mcp_servers": ["kb"]}}],
            "mcp_credentials": {"kb": {"value": value, "expires_in": 3600}}}),
    ]
    # CONTROL: the credential did reach the API, every time, so a search
    # that finds nothing below found nothing because nothing was written.
    assert sum(value in json.dumps(p) for _, _, p in api.sent) == 2 and \
        value in json.dumps(api.batches)
    for reply in replies:
        assert value not in reply
        assert "mcp_credentials" not in reply or '"server": "kb"' in reply
    assert not _files_holding(value, project, Path(tempfile.gettempdir()))
    record = list(project.rglob("launched.jsonl"))
    assert record, "the dispatch wrote no record, so the search above searched nothing that matters"


@pytest.mark.parametrize("servers", [
    "VALUE",
    ["VALUE"],
    ["kb", {"value": "VALUE"}],
])
def test_a_credential_put_in_mcp_servers_by_mistake_is_refused_without_repeating_it(servers):
    """The frozen rule repeats up to 40 characters of a refused value, which
    for a name is what a caller needs to see. A credential pasted where a
    server's name goes is the one value this list must never repeat."""
    value = _credential()
    servers = json.loads(json.dumps(servers).replace("VALUE", value))
    api = RecordingApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "x", "inputs": {"mcp_servers": servers}})
    text = str(refused.value)
    assert "mcp_servers" in text and "not repeated" in text, text
    assert value[:12] not in text
    assert not api.dispatched and not _posted(api, "/v1/tasks")
