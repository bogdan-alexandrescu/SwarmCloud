"""The plugin shows workspace roles and changes them only at a person's own
terminal (ROLES-MCP, part of #1518).

docs/design/workspace-roles.md §4.8, "The sc plugin", and owner answer Q11:

* `swarm_tenants` and `sc whoami` show each workspace's role and `via`.
* `swarm_roles` is a READ-ONLY MCP tool: one `GET /v1/workspace/roles`.
* `sc roles set|clear|default` require the person to type the email and the
  role back at a prompt read from the TERMINAL (`/dev/tty`), never from
  arguments or stdin, so a script or an agent cannot pre-supply it. A mismatch
  sends nothing -- not even a read.
* NO MCP TOOL GRANTS OR REVOKES A ROLE. An agent driving a person's own plugin
  session must not be able to raise anyone, so no tool's schema takes a role,
  and no handler reaches a role-write route.
* A refusal's code and message are printed as the server sent them.

Nothing here needs a credential or a deployment: every transport is a fake
that records what it was asked.
"""

from __future__ import annotations

import ast
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_mcp import sc, server
from swarm_mcp.client import SwarmError, tenant_listing

_PACKAGE = Path(server.__file__).resolve().parent
_REPO = Path(__file__).resolve().parents[3]

#: Every route that writes a role (swarm_api/routes/roles.py's docstring):
#: the workspace's own and the platform admin's. Matched as substrings of the
#: string constants in this package's source.
_ROLE_WRITE_PATHS = (
    "/v1/workspace/roles/person",
    "/v1/workspace/roles/default",
    "/v1/admin/workspaces/",
)

_ROSTER = {
    "tenant_id": "eng", "kind": "group", "default_role": "member",
    "default_source": "migration",
    "me": {"email": "carol@example.com", "role": "viewer", "source": "override",
           "granted_by": "alice@example.com", "granted_at": "2026-10-20T09:14:03.120Z",
           "via": "member"},
    "people": [
        {"email": "alice@example.com", "role": "admin", "source": "override",
         "granted_by": "ops@example.com", "granted_at": "2026-10-19T08:00:00Z",
         "granted_via": "platform_admin"},
        {"email": "carol@example.com", "role": "viewer", "source": "override",
         "granted_by": "alice@example.com", "granted_at": "2026-10-20T09:14:03.120Z"},
    ],
    "roles": [
        {"name": "viewer", "summary": "Sees everything in this workspace; changes nothing."},
        {"name": "member", "summary": "Submits, approves, answers, cancels, and manages "
                                      "schedules, repositories and accounts."},
        {"name": "admin", "summary": "A member who also manages secrets, MCP servers, "
                                     "provider keys and people's roles."},
    ],
    "can_manage": False,
    "audit": [{"action": "workspace_role_granted", "by": "alice@example.com",
               "at": "2026-10-20T09:14:03.120Z", "target_email": "carol@example.com",
               "detail": {"from": "member", "to": "viewer", "via": "console"}}],
}


class RecordingApi:
    """A control plane that records every call it is asked to make."""

    def __init__(self, *, refuse: SwarmError | None = None) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.tenant = None
        self.refuse = refuse

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if self.refuse is not None and method != "GET":
            raise self.refuse
        if (method, path) == ("GET", "/v1/workspace/roles"):
            return json.loads(json.dumps(_ROSTER))
        if (method, path) == ("GET", "/v1/tenants/me"):
            return {"tenant": {"tenant_id": "eng"}, "principal": {"email": "carol@example.com"},
                    "role": {"name": "viewer", "source": "override"}}
        if (method, path) == ("GET", "/v1/tenants/mine"):
            return [
                {"tenant_id": "u-carol", "display_name": "carol@example.com", "kind": "user",
                 "via": "personal", "role": {"name": "admin", "source": "personal"},
                 "is_default": True, "default_source": "personal"},
                {"tenant_id": "eng", "display_name": "eng@example.com", "via": "member",
                 "role": {"name": "viewer", "source": "override"}, "is_default": False},
                {"tenant_id": "ops", "display_name": "ops@example.com", "via": "platform_admin",
                 "role": {"name": "admin", "source": "platform_admin"}, "is_default": False},
            ]
        if path.startswith("/v1/workspace/roles/"):
            return {"email": (payload or {}).get("email"), "role": (payload or {}).get("role"),
                    "previous_role": "member", "source": "override",
                    "granted_at": "2026-10-20T10:00:00Z", "member": True}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def tenants_mine(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v1/tenants/mine")


# --------------------------------------------------------------------------
# swarm_tenants carries each workspace's role and via
# --------------------------------------------------------------------------


def test_swarm_tenants_carries_each_workspaces_role_and_via():
    api = RecordingApi()
    listing = json.loads(server._call(api, "swarm_tenants", {}))
    rows = {row["tenant_id"]: row for row in listing["tenants"]}
    assert set(rows) == {"u-carol", "eng", "ops"}, rows
    assert rows["u-carol"]["role"] == {"name": "admin", "source": "personal"}
    assert rows["u-carol"]["via"] == "personal"
    assert rows["eng"]["role"] == {"name": "viewer", "source": "override"}
    assert rows["eng"]["via"] == "member"
    assert rows["ops"]["role"] == {"name": "admin", "source": "platform_admin"}
    assert rows["ops"]["via"] == "platform_admin"
    assert {method for method, _, _ in api.sent} == {"GET"}, api.sent


def test_an_api_older_than_roles_leaves_role_and_via_absent_not_guessed():
    class Older(RecordingApi):
        def tenants_mine(self):
            return [{"tenant_id": "eng", "display_name": "eng@example.com"}]

    (row,) = tenant_listing(Older())["tenants"]
    assert row["role"] is None and row["via"] is None, row


def test_sc_whoami_shows_the_role_and_via_of_the_workspace_it_acts_in(monkeypatch):
    api = RecordingApi()

    class _Ctx:
        def __enter__(self):
            return api

        def __exit__(self, *exc):
            return None

    from types import SimpleNamespace

    monkeypatch.setattr(sc, "_resolve", lambda args: SimpleNamespace(
        context="qa", url="http://api.invalid", source="test", current=True,
        client_id="", front_door=False))
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: _Ctx())
    monkeypatch.setenv("SWARM_TENANT", "eng")
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    out = io.StringIO()
    sc.cmd_whoami(None, SimpleNamespace(json=True, context=None), out)
    shown = json.loads(out.getvalue())
    assert shown["tenant"] == "eng", shown
    assert shown["role"] == "viewer" and shown["role_source"] == "override", shown
    assert shown["via"] == "member", shown
    out = io.StringIO()
    sc.cmd_whoami(None, SimpleNamespace(json=False, context=None), out)
    line = next(row for row in out.getvalue().splitlines() if row.startswith("role "))
    assert "viewer" in line and "override" in line and "member" in line, out.getvalue()


# --------------------------------------------------------------------------
# swarm_roles: the roster, one GET
# --------------------------------------------------------------------------


def test_swarm_roles_returns_the_roster_and_calls_only_get_workspace_roles():
    api = RecordingApi()
    view = json.loads(server._call(api, "swarm_roles", {}))
    assert [(method, path) for method, path, _ in api.sent] == [("GET", "/v1/workspace/roles")]
    assert view["tenant_id"] == "eng" and view["default_role"] == "member"
    assert view["me"]["role"] == "viewer" and view["me"]["via"] == "member"
    assert [p["email"] for p in view["people"]] == ["alice@example.com", "carol@example.com"]
    assert view["audit"][0]["action"] == "workspace_role_granted"
    assert [r["name"] for r in view["roles"]] == ["viewer", "member", "admin"]
    assert view["can_manage"] is False
    assert "sc roles set" in view["change"], view["change"]
    assert "alice@example.com" in view["text"] and "viewer" in view["text"]


def test_swarm_roles_takes_no_argument():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_roles"]
    assert tool["inputSchema"] == {"type": "object", "additionalProperties": False,
                                   "properties": {}}


def test_sc_roles_is_the_same_read_in_a_terminal():
    for argv in (["roles"], ["roles", "list"], ["roles", "--json"], ["roles", "list", "--json"]):
        api = RecordingApi()
        parsed = sc.build_parser().parse_args(argv)
        out = io.StringIO()
        assert parsed.func(api, parsed, out) == sc.EXIT_OK, argv
        assert [(m, p) for m, p, _ in api.sent] == [("GET", "/v1/workspace/roles")], argv
        if "--json" in argv:
            assert json.loads(out.getvalue())["default_role"] == "member"
        else:
            assert "alice@example.com" in out.getvalue() and "default" in out.getvalue()


# --------------------------------------------------------------------------
# sc roles set|clear|default: typed at the terminal, or nothing is sent
# --------------------------------------------------------------------------

_CHANGES = [
    (["roles", "set", "dave@example.com", "viewer"], "dave@example.com viewer",
     ("PUT", "/v1/workspace/roles/person"), {"email": "dave@example.com", "role": "viewer"}),
    (["roles", "clear", "dave@example.com"], "dave@example.com default",
     ("DELETE", "/v1/workspace/roles/person"), {"email": "dave@example.com"}),
    (["roles", "default", "viewer"], "default viewer",
     ("PUT", "/v1/workspace/roles/default"), {"role": "viewer"}),
]


@pytest.fixture()
def terminal(monkeypatch, tmp_path):
    """Point the confirmation's terminal at a file this test writes, or at
    nothing. `None` is "no terminal": the path does not exist."""

    def use(typed: str | None) -> None:
        path = tmp_path / "tty"
        if typed is None:
            monkeypatch.setattr(sc, "TERMINAL", str(tmp_path / "no-such-terminal"))
        else:
            path.write_text(typed + "\n", encoding="utf-8")
            monkeypatch.setattr(sc, "TERMINAL", str(path))

    return use


def _change(argv: list[str], api: RecordingApi) -> tuple[int | None, str, SwarmError | None]:
    parsed = sc.build_parser().parse_args(argv)
    out = io.StringIO()
    try:
        return parsed.func(api, parsed, out), out.getvalue(), None
    except SwarmError as exc:
        return None, out.getvalue(), exc


@pytest.mark.parametrize("argv,phrase,route,body", _CHANGES, ids=lambda v: str(v))
def test_typed_at_the_terminal_the_change_is_sent_once(terminal, argv, phrase, route, body):
    """THE CONTROL: with the phrase typed at the terminal, exactly one request
    goes, so the refusals below are refusals and not a command that never sends."""
    terminal(phrase)
    api = RecordingApi()
    code, _, error = _change(argv, api)
    assert error is None and code == sc.EXIT_OK, error
    ((method, path, payload),) = api.sent
    assert (method, path) == route
    assert payload == {**body, "via": "cli"}


@pytest.mark.parametrize("argv,phrase,route,body", _CHANGES, ids=lambda v: str(v))
def test_without_the_typed_confirmation_nothing_is_sent(terminal, monkeypatch, argv, phrase,
                                                         route, body):
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    wrong = {"", "yes", phrase.upper(), phrase.replace("dave", "eve"),
             phrase.rsplit(" ", 1)[0] + " admin", phrase.split(" ", 1)[0]} - {phrase}
    assert len(wrong) >= 5, wrong
    for typed in sorted(wrong):
        terminal(typed)
        api = RecordingApi()
        code, _, error = _change(argv, api)
        assert error is not None and "nothing was sent" in str(error), (typed, code)
        assert api.sent == [], (typed, api.sent)


@pytest.mark.parametrize("argv,phrase,route,body", _CHANGES, ids=lambda v: str(v))
def test_the_confirmation_supplied_on_stdin_sends_nothing(terminal, monkeypatch, argv, phrase,
                                                          route, body):
    """Stdin is where a script or an agent would put it. With no terminal to
    read, the right phrase on stdin changes nothing."""
    terminal(None)
    monkeypatch.setattr("sys.stdin", io.StringIO(phrase + "\n"))
    api = RecordingApi()
    code, _, error = _change(argv, api)
    assert error is not None and "nothing was sent" in str(error), code
    assert "terminal" in str(error)
    assert api.sent == []


def test_the_confirmation_cannot_be_supplied_as_an_argument():
    for argv in (["roles", "set", "dave@example.com", "viewer", "--confirm", "dave@example.com viewer"],
                 ["roles", "set", "dave@example.com", "viewer", "--yes"],
                 ["roles", "default", "viewer", "-y"]):
        with pytest.raises(SystemExit):
            sc.build_parser().parse_args(argv)


@pytest.mark.parametrize("argv", [["roles", "set", "dave@example.com", "owner"],
                                  ["roles", "default", "admin"]])
def test_a_role_that_cannot_be_given_is_refused_before_anything_is_typed(terminal, argv):
    terminal("anything")
    api = RecordingApi()
    _, _, error = _change(argv, api)
    assert error is not None and "nothing was sent" in str(error)
    assert api.sent == []


def test_a_refusal_prints_the_servers_code_and_message(terminal):
    terminal("dave@example.com admin")
    refusal = SwarmError(
        "PUT /v1/workspace/roles/person -> 403: changing roles in eng needs admin",
        status=403, code="role_admin_required")
    api = RecordingApi(refuse=refusal)
    _, _, error = _change(["roles", "set", "dave@example.com", "admin"], api)
    assert error is not None
    assert "role_admin_required" in str(error) and "needs admin" in str(error), error
    assert error.code == "role_admin_required" and error.status == 403


# --------------------------------------------------------------------------
# No MCP tool grants or revokes a role
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


def test_no_tool_schema_takes_a_role_to_give():
    scanned = 0
    for tool in server.TOOLS:
        scanned += 1
        name = tool["name"]
        assert not re.search(r"role.*(set|grant|revoke|clear|default|change|update)"
                             r"|(set|grant|revoke|clear|change)_?roles?", name), name
        for path in _paths(tool.get("inputSchema") or {}):
            leaf = path.rsplit(".", 1)[-1]
            assert leaf not in {"role", "default_role", "roles"}, f"{name} takes `{path}`"
    assert scanned > 50, f"scanned {scanned} tools; the registry was not read"
    assert "swarm_roles" in {t["name"] for t in server.TOOLS}


def _functions_naming(tree: ast.AST, needles: tuple[str, ...]) -> set[str]:
    """The top-level functions whose body holds a string constant containing
    any of `needles`, and `<module>` for one outside every function."""
    found: set[str] = set()
    for top in getattr(tree, "body", []):
        holder = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
        for node in ast.walk(top):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and any(n in node.value for n in needles):
                found.add(holder)
    return found


def _names_used(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    return names


def test_no_handler_reaches_a_role_write_route():
    """Read off the source: the write paths are named in ONE function of
    `sc.py`, reached only from the three typed `sc roles` handlers, and no
    other module -- `server.py` above all -- names that function, those
    handlers or the paths."""
    trees = {p.name: ast.parse(p.read_text(encoding="utf-8")) for p in _PACKAGE.glob("*.py")}
    assert len(trees) > 20, sorted(trees)
    holders = {name: _functions_naming(tree, _ROLE_WRITE_PATHS) for name, tree in trees.items()}
    holders = {name: found for name, found in holders.items() if found}
    assert holders == {"sc.py": {"<module>"}}, holders
    # The module-level constant is read by one function, and that function by
    # the three typed handlers only.
    sc_tree = trees["sc.py"]
    readers = {
        top.name for top in sc_tree.body
        if isinstance(top, ast.FunctionDef) and "ROLE_WRITES" in _names_used(top)
    }
    assert readers == {"_send_role_change"}, readers
    callers = {
        top.name for top in sc_tree.body
        if isinstance(top, ast.FunctionDef) and "_send_role_change" in _names_used(top)
    }
    assert callers == {"cmd_roles_set", "cmd_roles_clear", "cmd_roles_default"}, callers
    forbidden = {"ROLE_WRITES", "_send_role_change", "cmd_roles_set", "cmd_roles_clear",
                 "cmd_roles_default"}
    for name, tree in trees.items():
        if name == "sc.py":
            continue
        assert not forbidden & _names_used(tree), (name, forbidden & _names_used(tree))


def test_the_elicitation_route_and_every_read_tool_send_no_role_write():
    """The handlers that touch roles at all, run: only GETs leave."""
    for tool, args in (("swarm_roles", {}), ("swarm_tenants", {})):
        api = RecordingApi()
        server._call(api, tool, args)
        assert api.sent and all(method == "GET" for method, _, _ in api.sent), (tool, api.sent)
        assert not [p for _, p, _ in api.sent if any(w in p for w in _ROLE_WRITE_PATHS)]


# --------------------------------------------------------------------------
# The plugin's surfaces: the read is granted, the writes are told
# --------------------------------------------------------------------------


@pytest.mark.parametrize("relative", ["plugin/commands/sc.md", "plugin/skills/sc/SKILL.md"])
def test_the_surfaces_grant_the_roster_read_and_never_a_role_change(relative):
    text = (_REPO / relative).read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert "Bash(sc roles)" in front and "Bash(sc roles list:*)" in front, relative
    assert not re.search(r"Bash\([^)]*roles(:\*| \*|\s+(set|clear|default))", front), relative
    assert "sc roles set" in text, f"{relative} does not tell the person how to change a role"
