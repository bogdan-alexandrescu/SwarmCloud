"""Config › Secrets from the sc plugin: links and reads only (SSC-SC, part of #2158).

docs/design/secrets-scopes.md §6.4, with BGD's answer Q8 of its §11: a value
arrives only through the console form a one-time link opens, or a terminal's
--stdin. NO TOOL TAKES A VALUE. The routes are on main and used as they are:

    POST /v1/workspace/secrets/links                 swarm_secret_set_link (workspace, personal)
    POST /v1/repositories/{repo_id}/secrets/links    swarm_secret_set_link (repository)
    POST /v1/repositories/{repo_id}/actions/links    swarm_actions_secret_link
    GET  /v1/secrets/overview                        swarm_secrets
    GET  /v1/admin/platform-secrets                  swarm_platform_secrets
    POST /v1/qa-targets/{id}/credential-links        swarm_qa_target_credential_link (`field`)

Held here, §9's SSC-SC row:

  * no tool's schema accepts a value field: every schema forbids undeclared
    properties, no property is named like a value, and every string property
    is an enum or a pattern a value does not fit (a free string is a field a
    value fits in). A value handed over anyway is refused, nothing sent;
  * each link tool returns a URL and its expiry and nothing else, even when
    the API (wrongly) answers more;
  * `url` is null without a console origin, with a note and never the
    path, which carries the link's token;
  * `swarm_platform_secrets` passes the route's 403 through unchanged.

Every fake value is built at runtime (CLAUDE.md). Offline: no credentials,
no emulator.
"""

from __future__ import annotations

import json
import re
import secrets

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmError

CONSOLE = "https://console.example.test"
REPO_ID = "repo_" + "0123456789abcdef"
TARGET_ID = "qat_" + "fedcba9876543210"
PERSONAL = "u-alice"

LINK_TOOLS = ("swarm_secret_set_link", "swarm_actions_secret_link",
              "swarm_qa_target_credential_link")
NEW_TOOLS = ("swarm_secret_set_link", "swarm_actions_secret_link", "swarm_secrets",
             "swarm_platform_secrets")
TOOLS = NEW_TOOLS + ("swarm_qa_target_credential_link",)

#: A property name that reads as "put the value here".
_VALUEISH = re.compile(r"value|secret_value|password|passwd|token|api_?key|credential|"
                       r"material|payload|body|plaintext|ciphertext|encrypted", re.IGNORECASE)


def _value() -> str:
    """A fake secret value, unique per test so a leak is unambiguous."""
    return "v" + secrets.token_urlsafe(30)[1:]


def _leaked(value: str, *texts: str) -> list[str]:
    joined = "\n".join(texts)
    return [value[i:i + 8] for i in range(len(value) - 7) if value[i:i + 8] in joined]


class FakeApi:
    """The routes this lane calls, recording `(tenant, method, path, payload)`.
    `planted` is a value the fake (wrongly) puts in every answer and row, so a
    test can show it is not forwarded."""

    def __init__(self, *, planted: str | None = None, console: str | None = CONSOLE,
                 tenant: str | None = "eng", error: SwarmError | None = None,
                 personal: str | None = PERSONAL):
        self.sent: list[tuple[str | None, str, str, object]] = []
        self.planted = planted
        self.console = console
        self.tenant = tenant
        self.error = error
        self.personal = personal
        self.link_token = secrets.token_urlsafe(32)

    def registration(self, owner, repo):
        self.sent.append((self.tenant, "GET", "/v1/repositories", None))
        if f"{owner}/{repo}".lower() == "acme/app":
            return {"repo_id": REPO_ID, "owner": "acme", "repo": "app"}
        return None

    def _link(self, path: str, extra: dict) -> dict:
        link = f"{path}&t={self.link_token}"
        return {"url": f"{self.console}{link}" if self.console else None, "path": link,
                "expires_at": "2026-10-24T09:10:00+00:00", **extra}

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((self.tenant, method, path, payload))
        if self.error is not None:
            raise self.error
        extra = {"value": self.planted} if self.planted else {}
        if (method, path) == ("POST", "/v1/workspace/secrets/links"):
            return self._link(f"/settings/workspace/secrets?action={payload['action']}",
                              {"action": payload["action"], "name": payload.get("name"),
                               **extra})
        if (method, path) == ("POST", f"/v1/repositories/{REPO_ID}/secrets/links"):
            return self._link(f"/config/secrets?scope=repository&repo={REPO_ID}",
                              {"action": payload["action"], **extra})
        if (method, path) == ("POST", f"/v1/repositories/{REPO_ID}/actions/links"):
            return self._link(f"/config/secrets?scope=repository&repo={REPO_ID}&dest=actions",
                              {"name": payload["name"],
                               "environment": payload.get("environment"), **extra})
        if (method, path) == ("POST", f"/v1/qa-targets/{TARGET_ID}/credential-links"):
            return self._link(f"/repositories/qa?target={TARGET_ID}", extra)
        if method == "GET" and path.startswith("/v1/secrets/overview?"):
            return self._overview(path, extra)
        if (method, path) == ("GET", "/v1/admin/platform-secrets"):
            return {"secrets": [{"secret_id": "swarm-merge-app-key", "family": "platform",
                                 "managed_by": "terraform", "written_by": "create-secrets.sh",
                                 "status": "set", "reason": None, "enabled_versions": 1,
                                 "newest_enabled_at": "2026-10-20T00:00:00+00:00",
                                 "rotation_in_grace": False, **extra}],
                    "counts": {"set": 1}, "accounts_unread": False, "accounts_refused": 0,
                    "accounts_truncated": False, "read_at": "2026-10-24T09:00:00+00:00",
                    **extra}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)

    def _overview(self, path: str, extra: dict) -> dict:
        scope = re.search(r"scope=(\w+)", path).group(1)
        row = {"section": "agents", "store": "workspace_secrets", "name": "test-db-password",
               "scope": scope, "repo_id": REPO_ID if scope == "repository" else None,
               "repository": "acme/app" if scope == "repository" else None,
               "kind": "env", "status": "set", "set_at": "2026-10-24T08:00:00+00:00",
               "set_by": "alice@example.test", "consumers": ["profile:claude-code"],
               "shadowed_by": [{"scope": "personal", "person": "bob@example.test",
                                "where": "agents of bob@example.test", "slot": "env",
                                **extra}],
               "shadows": [], "can_manage": True, "writer": "/v1/workspace/secrets",
               "last4": "abcd", "length": 40, **extra}
        return {"tenant_id": self.tenant, "scope": scope, "scopes": ["workspace", "personal",
                                                                      "repository"],
                "repo_id": REPO_ID if scope == "repository" else None,
                "repository": "acme/app" if scope == "repository" else None,
                "personal_tenant_id": self.personal if scope == "personal" else None,
                "can_manage": True, "rows": [row],
                "not_listed": [{"store": "platform_secrets", "why": "Admin's"}],
                "note": "status only; no route in this API returns a secret's value", **extra}


def _tool(name: str) -> dict:
    return next(t for t in server.TOOLS if t["name"] == name)


def _call(api: FakeApi, name: str, args: dict) -> dict:
    return json.loads(server._call(api, name, args))


# -- no tool's schema accepts a value field ---------------------------------------


@pytest.mark.parametrize("name", TOOLS)
def test_no_tool_schema_accepts_a_value_field(name):
    schema = _tool(name)["inputSchema"]
    properties = schema.get("properties") or {}
    assert schema.get("additionalProperties") is False, "an undeclared field must be refused"
    for prop, spec in properties.items():
        assert not _VALUEISH.search(prop), prop
        assert spec.get("type") == "string", (prop, spec)


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_every_new_tool_string_is_an_enum_or_a_narrow_pattern(name):
    """A free string is a field a value fits in; an enum or a pattern a value
    does not match is not. (`swarm_qa_target_credential_link`'s `target` is a
    name looked up in the workspace and refused unrepeated, as before.)"""
    for prop, spec in (_tool(name)["inputSchema"].get("properties") or {}).items():
        assert spec.get("enum") or spec.get("pattern"), (prop, spec)


def test_no_bridge_tool_anywhere_takes_a_secret_value():
    offending = [(t["name"], p) for t in server.TOOLS
                 for p in (t["inputSchema"].get("properties") or {})
                 if p.lower() in {"value", "secret_value", "encrypted_value", "plaintext",
                                  "password", "challenge_bypass_value"}]
    assert offending == [], offending


@pytest.mark.parametrize("name,args", [
    ("swarm_secret_set_link", {"scope": "workspace", "action": "add"}),
    ("swarm_actions_secret_link", {"repository": "acme/app", "name": "MERGE_APP_PRIVATE_KEY"}),
    ("swarm_secrets", {"scope": "workspace"}),
    ("swarm_platform_secrets", {}),
    ("swarm_qa_target_credential_link", {"target": TARGET_ID}),
])
@pytest.mark.parametrize("field", ["value", "secret_value", "encrypted_value"])
def test_a_value_handed_to_any_tool_is_refused_and_nothing_is_sent(name, args, field):
    value = _value()
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, name, {**args, field: value})
    assert api.sent == []
    assert _leaked(value, str(exc.value)) == []


# -- each link tool returns a URL and its expiry and nothing else ------------------


@pytest.mark.parametrize("name,args", [
    ("swarm_secret_set_link", {"scope": "workspace", "action": "add"}),
    ("swarm_secret_set_link", {"scope": "personal", "action": "rotate", "name": "gh-pat"}),
    ("swarm_secret_set_link", {"scope": "repository", "action": "add",
                               "repository": "acme/app"}),
    ("swarm_actions_secret_link", {"repository": "acme/app", "name": "MERGE_APP_PRIVATE_KEY",
                                   "environment": "repo-secrets"}),
    ("swarm_qa_target_credential_link", {"target": TARGET_ID, "field": "challenge_bypass"}),
])
def test_each_link_tool_answers_only_the_url_and_its_expiry(name, args):
    planted = _value()
    api = FakeApi(planted=planted)
    reply = server._call(api, name, args)
    body = json.loads(reply)
    assert set(body) == {"url", "expires_at"}, sorted(body)
    assert body["url"].startswith(CONSOLE + "/")
    assert body["expires_at"] == "2026-10-24T09:10:00+00:00"
    assert _leaked(planted, reply) == [], "a value the API sent back is not forwarded"


@pytest.mark.parametrize("name,args", [
    ("swarm_secret_set_link", {"scope": "workspace", "action": "add"}),
    ("swarm_actions_secret_link", {"repository": REPO_ID, "name": "DEPLOY_KEY"}),
    ("swarm_qa_target_credential_link", {"target": TARGET_ID}),
])
def test_without_a_console_origin_url_is_null_and_no_path_is_handed_out(name, args):
    api = FakeApi(console=None)
    reply = server._call(api, name, args)
    body = json.loads(reply)
    assert body["url"] is None
    assert set(body) == {"url", "expires_at", "note"}, sorted(body)
    assert api.link_token not in reply, "the path's token is not handed out bare"
    assert "SWARM_CONSOLE_URL" in body["note"]


# -- swarm_secret_set_link: the route each scope reaches ---------------------------


def test_a_workspace_link_posts_the_store_route_in_the_selected_workspace():
    api = FakeApi()
    _call(api, "swarm_secret_set_link", {"scope": "workspace", "action": "add"})
    assert api.sent == [("eng", "POST", "/v1/workspace/secrets/links", {"action": "add"})]


def test_a_rotate_link_names_its_secret():
    api = FakeApi()
    _call(api, "swarm_secret_set_link",
          {"scope": "workspace", "action": "rotate", "name": "test-db-password"})
    assert api.sent == [("eng", "POST", "/v1/workspace/secrets/links",
                         {"action": "rotate", "name": "test-db-password"})]


def test_a_personal_link_is_minted_in_the_callers_own_personal_workspace():
    api = FakeApi()
    _call(api, "swarm_secret_set_link", {"scope": "personal", "action": "add"})
    assert api.sent == [
        ("eng", "GET", "/v1/secrets/overview?scope=personal", None),
        (PERSONAL, "POST", "/v1/workspace/secrets/links", {"action": "add"}),
    ]
    assert api.tenant == "eng", "the bridge's own client still acts in the selected workspace"


def test_a_personal_link_without_a_personal_workspace_is_refused_before_any_mint():
    api = FakeApi(personal=None)
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_secret_set_link", {"scope": "personal", "action": "add"})
    assert [s[1] for s in api.sent] == ["GET"]
    assert "personal workspace" in str(exc.value)


@pytest.mark.parametrize("repository", ["acme/app", "https://github.com/Acme/App.git", REPO_ID])
def test_a_repository_link_resolves_the_registration_and_posts_its_route(repository):
    api = FakeApi()
    _call(api, "swarm_secret_set_link",
          {"scope": "repository", "action": "add", "repository": repository})
    assert api.sent[-1] == ("eng", "POST", f"/v1/repositories/{REPO_ID}/secrets/links",
                            {"action": "add"})


def test_an_unregistered_repository_is_refused_and_nothing_is_minted():
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_secret_set_link",
                     {"scope": "repository", "action": "add", "repository": "acme/other"})
    assert [s[1] for s in api.sent] == ["GET"]
    assert "not registered" in str(exc.value)


@pytest.mark.parametrize("args", [
    {"scope": "workspace", "action": "add", "name": "test-db-password"},
    {"scope": "workspace", "action": "rotate"},
    {"scope": "repository", "action": "add"},
    {"scope": "workspace", "action": "add", "repository": "acme/app"},
    {"scope": "elsewhere", "action": "add"},
    {"scope": "workspace", "action": "delete"},
])
def test_a_link_the_api_would_refuse_is_refused_before_anything_is_sent(args):
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_secret_set_link", args)
    assert api.sent == []


@pytest.mark.parametrize("tool,args,field", [
    ("swarm_secret_set_link", {"scope": "workspace", "action": "rotate"}, "name"),
    ("swarm_secret_set_link", {"scope": "repository", "action": "add"}, "repository"),
    ("swarm_actions_secret_link", {"repository": "acme/app"}, "name"),
    ("swarm_actions_secret_link", {"name": "DEPLOY_KEY"}, "repository"),
    ("swarm_actions_secret_link", {"repository": "acme/app", "name": "X"}, "environment"),
])
def test_a_value_pasted_into_a_name_field_is_refused_without_being_repeated(tool, args, field):
    value = _value() + "/=+"
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, tool, {**args, field: value})
    assert api.sent == []
    assert _leaked(value, str(exc.value)) == []


# -- swarm_actions_secret_link -------------------------------------------------------


def test_the_actions_link_posts_name_and_environment_to_the_actions_route():
    api = FakeApi()
    _call(api, "swarm_actions_secret_link",
          {"repository": "acme/app", "name": "MERGE_APP_PRIVATE_KEY",
           "environment": "repo-secrets"})
    assert api.sent[-1] == ("eng", "POST", f"/v1/repositories/{REPO_ID}/actions/links",
                            {"name": "MERGE_APP_PRIVATE_KEY", "environment": "repo-secrets"})


def test_a_repository_level_actions_link_names_no_environment():
    api = FakeApi()
    _call(api, "swarm_actions_secret_link", {"repository": REPO_ID, "name": "DEPLOY_KEY"})
    assert api.sent == [("eng", "POST", f"/v1/repositories/{REPO_ID}/actions/links",
                         {"name": "DEPLOY_KEY"})]


@pytest.mark.parametrize("name", ["GITHUB_TOKEN_X", "github_x", "1ABC", "A-B", ""])
def test_a_name_github_refuses_is_refused_before_anything_is_sent(name):
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_actions_secret_link", {"repository": REPO_ID, "name": name})
    assert api.sent == []


# -- swarm_secrets ---------------------------------------------------------------------


def test_swarm_secrets_reads_the_overview_and_forwards_allow_listed_fields_only():
    planted = _value()
    api = FakeApi(planted=planted)
    reply = server._call(api, "swarm_secrets", {"scope": "repository", "repository": "acme/app"})
    assert api.sent[-1] == ("eng", "GET",
                            f"/v1/secrets/overview?scope=repository&repo_id={REPO_ID}", None)
    assert _leaked(planted, reply) == []
    body = json.loads(reply)
    assert body["scope"] == "repository" and body["repository"] == "acme/app"
    (row,) = body["rows"]
    assert row["name"] == "test-db-password" and row["status"] == "set"
    assert row["shadowed_by"][0]["where"] == "agents of bob@example.test"
    assert "last4" not in row and "length" not in row and "value" not in row
    assert "test-db-password" in body["text"] and "shadowed" in body["text"]


def test_swarm_secrets_workspace_scope_sends_no_repo_id():
    api = FakeApi()
    _call(api, "swarm_secrets", {"scope": "workspace"})
    assert api.sent == [("eng", "GET", "/v1/secrets/overview?scope=workspace", None)]


@pytest.mark.parametrize("args", [{"scope": "repository"},
                                  {"scope": "personal", "repository": "acme/app"}])
def test_swarm_secrets_refuses_a_repository_named_with_the_wrong_scope(args):
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_secrets", args)
    assert api.sent == []


# -- swarm_platform_secrets ---------------------------------------------------------------


def test_swarm_platform_secrets_reads_the_admin_route_field_by_field():
    planted = _value()
    api = FakeApi(planted=planted)
    reply = server._call(api, "swarm_platform_secrets", {})
    assert api.sent == [("eng", "GET", "/v1/admin/platform-secrets", None)]
    assert _leaked(planted, reply) == []
    body = json.loads(reply)
    assert body["secrets"][0]["secret_id"] == "swarm-merge-app-key"
    assert body["counts"] == {"set": 1}
    assert "swarm-merge-app-key" in body["text"]


def test_swarm_platform_secrets_passes_the_routes_403_through_unchanged():
    refusal = SwarmError("GET /v1/admin/platform-secrets -> 403: admin_required", status=403)
    api = FakeApi(error=refusal)
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_platform_secrets", {})
    assert exc.value is refusal
    assert exc.value.status == 403


# -- swarm_qa_target_credential_link gains `field` ---------------------------------------


@pytest.mark.parametrize("field", ["credential", "challenge_bypass"])
def test_the_qa_link_sends_the_field_it_names(field):
    api = FakeApi()
    _call(api, "swarm_qa_target_credential_link", {"target": TARGET_ID, "field": field})
    assert api.sent == [("eng", "POST", f"/v1/qa-targets/{TARGET_ID}/credential-links",
                         {"field": field})]


def test_the_qa_link_without_a_field_sends_what_it_sent_before():
    api = FakeApi()
    _call(api, "swarm_qa_target_credential_link", {"target": TARGET_ID})
    assert api.sent == [("eng", "POST", f"/v1/qa-targets/{TARGET_ID}/credential-links", None)]


def test_the_qa_link_refuses_an_unknown_field_without_repeating_it():
    value = _value()
    api = FakeApi()
    with pytest.raises(SwarmError) as exc:
        server._call(api, "swarm_qa_target_credential_link", {"target": TARGET_ID,
                                                               "field": value})
    assert api.sent == []
    assert _leaked(value, str(exc.value)) == []


def test_the_qa_link_schema_offers_the_two_fields():
    spec = _tool("swarm_qa_target_credential_link")["inputSchema"]["properties"]["field"]
    assert spec["enum"] == ["credential", "challenge_bypass"]


# -- the seam: every route `secrets.py` calls is one swarm-api serves ----------------------


def test_every_route_the_secret_tools_call_is_served_by_swarm_api():
    from pathlib import Path

    from swarm_api.routes import actions_secrets, admin, repository_secrets, secrets_overview
    from swarm_api.routes import workspace_secrets

    def shape(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path.split("?")[0])

    served = {(m, shape(r.path))
              for module in (actions_secrets, admin, repository_secrets, secrets_overview,
                             workspace_secrets)
              for r in module.router.routes for m in getattr(r, "methods", ())}
    source = (Path(server.__file__).with_name("secrets.py")).read_text()
    spoken = {(m, shape(p)) for m, p in
              re.findall(r'request\(\s*"(GET|POST)"\s*,\s*f?"(/v1[^"]*)"', source)}
    # MUTATION: call a route spelled differently from swarm-api's and this goes red.
    assert spoken == {
        ("POST", "/v1/workspace/secrets/links"),
        ("POST", "/v1/repositories/{}/secrets/links"),
        ("POST", "/v1/repositories/{}/actions/links"),
        ("GET", "/v1/secrets/overview"),
        ("GET", "/v1/admin/platform-secrets"),
    }
    assert spoken <= served, sorted(spoken - served)
