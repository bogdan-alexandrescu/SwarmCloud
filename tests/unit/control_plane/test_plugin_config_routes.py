"""A managed plugin's settings, secrets, bindings and the workspace's defaults, over the routes.

docs/design/managed-plugins.md sections 6 and 7 (lane 6 of section 17), with
the owner's answers: plugin secrets are per workspace only, also for a
platform plugin (Q2); the workspace's owner or admin writes, a member reads
(Q3); a default a step cannot run is skipped and recorded (Q6).

    GET    {base}/{install_id}                          the form and the values
    PUT    {base}/{install_id}                          non-secret values, naming the revision read
    POST   {base}/{install_id}/secrets/{field}/links    a one-time link
    PUT    {base}/{install_id}/secrets/{field}          the value, with the link
    DELETE {base}/{install_id}/secrets/{field}          destroy it
    PUT    {base}/{install_id}/secrets/{field}/binding  a workspace key instead
    GET|PUT /v1/workspaces/{tenant_id}/plugin-defaults  the plugins on for every step

Plugins are installed by the catalogue's own routes (the stand-ins of
test_plugin_catalogue_routes); Secret Manager is an in-memory writer on
`app.state`, the one place a value lives here. Every value is built at
runtime: nothing added looks like a credential.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from swarm_api import pluginconfig as pcfg
from swarm_api import pluginstore as ps
from swarm_api.main import UNCAPTURED_ROUTES
from swarm_api.routes import plugin_config as rc
from swarm_api.routes.plugin_catalogue import _store

from .conftest import auth_header, seed_tenant
from .test_plugin_catalogue_routes import (  # noqa: F401  (world is a fixture)
    CAROL_TENANT,
    _workspace,
    installed,
    world,
)
from .test_settings_mac import configure

USER_CONFIG = [
    {"key": "level", "type": "string", "default": "warn"},
    {"key": "max_issues", "type": "number"},
    {"key": "api_key", "type": "string", "sensitive": True, "required": True},
]
OVERLAY = {"settings": [
    {"key": "token", "type": "string", "variable": "LINT_TOKEN", "sensitive": True},
    {"key": "rules", "type": "file", "variable": "LINT_RULES", "target": "file"},
    {"key": "verbose", "type": "boolean", "variable": "LINT_VERBOSE"},
]}
NODE = {"skills": [], "commands": [], "agents": [], "mcp_servers": [],
        "hooks": [{"event": "PreToolUse", "command": pcfg.LAUNCHER + " -- node ${CLAUDE_PLUGIN_ROOT}/h.js"}]}
PYTHON = {"skills": [], "commands": [], "agents": [], "mcp_servers": [],
          "hooks": [{"event": "PreToolUse", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/h.py"}]}


def value_of(n: int = 24) -> str:
    """A secret-shaped value, built at runtime."""
    return "pv-" + secrets.token_hex(n)


class FakeWriter:
    """Secret Manager, write-only, in memory."""

    def __init__(self) -> None:
        self.payloads: dict[str, list[bytes]] = {}
        self.created: list[tuple[str, str, str]] = []

    def create_secret(self, tenant_id: str, install_id: str, secret_id: str) -> None:
        self.created.append((tenant_id, install_id, secret_id))
        self.payloads.setdefault(secret_id, [])

    def add_version(self, secret_id: str, payload: bytes) -> str:
        self.payloads[secret_id].append(payload)
        return f"projects/123/secrets/{secret_id}/versions/{len(self.payloads[secret_id])}"

    def destroy_versions(self, secret_id: str) -> int:
        return len(self.payloads.pop(secret_id, []))


@pytest.fixture
def cw(world):
    w = dict(world)
    w["writer"] = FakeWriter()
    w["client"].app.state.plugin_secret_writer = w["writer"]
    seed_tenant(w["db"], "research")
    return w


def _base(tenant_id: str) -> str:
    return rc.BASE.format(tenant_id=tenant_id)


def _defaults(tenant_id: str) -> str:
    return rc.DEFAULTS_ROUTE.format(tenant_id=tenant_id)


def manifest(name: str, user_config: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"name": name, "version": "1.0.0", "description": "Lints.", "user_config": user_config or []}


def platform_plugin(w: dict[str, Any], **report: Any) -> dict[str, Any]:
    report.setdefault("manifest", manifest("lint-kit", USER_CONFIG))
    return installed(w, name=report["manifest"]["name"], **report)


def eng_plugin(w: dict[str, Any], name: str = "eng-kit", **report: Any) -> dict[str, Any]:
    report.setdefault("manifest", manifest(name, USER_CONFIG))
    return installed(w, base=_workspace("eng"), name=name, **report)


def read(w, tenant_id: str, install_id: str, user: str = "root"):
    return w["client"].get(f"{_base(tenant_id)}/{install_id}", headers=auth_header(user))


def save(w, tenant_id: str, install_id: str, values: dict[str, Any], *, revision: int = 0,
         version_id: str | None = None, user: str = "root"):
    if version_id is None:
        version_id = read(w, tenant_id, install_id).json()["form"]["version_id"]
    return w["client"].put(f"{_base(tenant_id)}/{install_id}", headers=auth_header(user),
                           json={"version_id": version_id, "revision": revision, "values": values})


def mint(w, tenant_id: str, install_id: str, field: str, user: str = "root"):
    return w["client"].post(f"{_base(tenant_id)}/{install_id}/secrets/{field}/links", headers=auth_header(user))


def token_of(minted) -> str:
    assert minted.status_code == 201, minted.text
    return parse_qs(urlsplit(minted.json()["path"]).query)["t"][0]


def store(w, tenant_id: str, install_id: str, field: str, value: str, token: str, user: str = "root"):
    return w["client"].put(f"{_base(tenant_id)}/{install_id}/secrets/{field}", headers=auth_header(user),
                           json={"value": value, "link_token": token})


def audit(db: Any, *actions: str) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items()
            if p.startswith("admin_audit/") and d.get("action") in (actions or pcfg.AUDIT_ACTIONS)]


# --- the form -------------------------------------------------------------------


def test_a_member_reads_the_form_from_the_manifest_and_cannot_edit(cw):
    done = eng_plugin(cw)
    body = read(cw, "eng", done["install_id"], "alice")
    assert body.status_code == 200, body.text
    answer = body.json()
    assert answer["form"]["source"] == "user_config"
    assert [(f["key"], f["kind"]) for f in answer["form"]["fields"]] == [
        ("level", "value"), ("max_issues", "value"), ("api_key", "secret")]
    assert answer["form"]["version_id"] == done["version_id"]
    assert answer["revision"] == 0 and answer["missing"] == ["api_key"]
    assert answer["can_edit"] is False
    assert read(cw, "eng", done["install_id"], "root").json()["can_edit"] is True


def test_a_plugin_with_no_user_config_reads_its_form_from_the_overlay(cw):
    done = platform_plugin(cw, manifest=manifest("lint-kit"), overlay=OVERLAY)
    form = read(cw, "eng", done["install_id"]).json()["form"]
    assert form["source"] == "overlay"
    assert [(f["key"], f["kind"], f["variable"]) for f in form["fields"]] == [
        ("token", "secret", "LINT_TOKEN"), ("rules", "file", "LINT_RULES"), ("verbose", "value", "LINT_VERBOSE")]


def test_a_built_in_has_no_settings(cw):
    response = read(cw, "eng", "builtin:gstack")
    assert response.status_code == 422 and response.json()["code"] == "plugin_has_no_settings"


def test_another_workspaces_private_plugin_is_not_found(cw):
    done = eng_plugin(cw)
    response = read(cw, CAROL_TENANT, done["install_id"], "carol")
    assert response.status_code == 404, response.text


# --- values, with a version check --------------------------------------------------


def test_values_save_against_the_revision_read_and_a_stale_one_is_a_conflict(cw):
    done = eng_plugin(cw)
    first = save(cw, "eng", done["install_id"], {"level": "error", "max_issues": 3})
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert first.json()["values"]["level"] == {"set": True, "value": "error"}
    assert [r["detail"]["fields"] for r in audit(cw["db"], pcfg.AUDIT_CONFIG_SET)] == [["level", "max_issues"]]

    # Someone else's save came between: refused, and nothing changed.
    stale = save(cw, "eng", done["install_id"], {"level": "info"}, revision=0)
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "config_conflict"
    assert read(cw, "eng", done["install_id"]).json()["values"]["level"]["value"] == "error"
    assert len(audit(cw["db"], pcfg.AUDIT_CONFIG_SET)) == 1

    cleared = save(cw, "eng", done["install_id"], {"max_issues": None}, revision=1)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["revision"] == 2 and cleared.json()["values"]["max_issues"] == {"set": False}


def test_values_read_against_another_version_are_refused(cw):
    done = eng_plugin(cw)
    response = save(cw, "eng", done["install_id"], {"level": "error"}, version_id="v-000000000000")
    assert response.status_code == 409 and response.json()["code"] == "form_changed"


@pytest.mark.parametrize(("values", "code"), [
    ({"api_key": "x"}, "field_write_only"), ({"nope": 1}, "field_unknown"),
    ({"max_issues": "three"}, "value_invalid"),
])
def test_a_value_the_form_does_not_take_is_refused_by_code(cw, values, code):
    done = eng_plugin(cw)
    response = save(cw, "eng", done["install_id"], values)
    assert response.status_code == 422 and response.json()["code"] == code
    assert audit(cw["db"]) == []


def test_a_platform_plugins_values_are_per_workspace(cw):
    # Q2: no setting a platform plugin shares across workspaces.
    done = platform_plugin(cw)
    assert save(cw, "eng", done["install_id"], {"level": "error"}).status_code == 200
    seed_tenant(cw["db"], CAROL_TENANT)
    cw["db"].docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": "carol@example.com"})
    theirs = read(cw, CAROL_TENANT, done["install_id"], "carol").json()
    assert theirs["values"]["level"] == {"set": False} and theirs["revision"] == 0


def test_a_settings_document_written_around_the_api_reads_as_no_values(cw):
    done = eng_plugin(cw)
    assert save(cw, "eng", done["install_id"], {"level": "error"}).status_code == 200
    path = f"workspace_settings/eng/plugin_configs/{done['install_id']}"
    cw["db"].docs[path]["fields"]["level"] = {"value": "planted"}
    answer = read(cw, "eng", done["install_id"]).json()
    assert answer["values"]["level"] == {"set": False} and answer["revision"] == 0


def test_with_no_settings_key_nothing_is_saved(cw):
    done = eng_plugin(cw)
    configure(cw["ctx"], settings_key="")
    # Unsigned, the install itself is not in the catalogue: nothing to configure.
    response = save(cw, "eng", done["install_id"], {"level": "error"}, version_id=done["version_id"])
    assert response.status_code == 404, response.text
    assert audit(cw["db"]) == [] and not any("/plugin_configs/" in p for p in cw["db"].docs)


# --- a secret: one-time link, never returned -----------------------------------------


def test_a_secret_is_stored_through_its_one_time_link_and_never_comes_back(cw, caplog):
    caplog.set_level(logging.DEBUG)
    done = eng_plugin(cw)
    install_id = done["install_id"]
    minted = mint(cw, "eng", install_id, "api_key")
    token = token_of(minted)
    assert minted.json()["path"].startswith(f"/config/plugins/{install_id}?field=api_key&t=")
    # The link's document is keyed by the token's digest: the token is in no document.
    assert not any(token in repr(d) for d in cw["db"].docs.values())

    value = value_of()
    stored = store(cw, "eng", install_id, "api_key", value, token)
    assert stored.status_code == 200, stored.text
    assert stored.json()["set"] is True and stored.json()["set_by"] == "root@example.com"
    name = pcfg.secret_name("eng", install_id, "api_key")
    assert cw["writer"].payloads == {name: [value.encode()]}
    assert cw["writer"].created == [("eng", install_id, name)]

    # Nowhere else: no response, no document, no audit row, no log line.
    responses = [stored, read(cw, "eng", install_id), read(cw, "eng", install_id, "alice")]
    assert all(value not in r.text for r in responses)
    assert not any(value in repr(d) for d in cw["db"].docs.values())
    assert value not in caplog.text and token not in caplog.text
    [row] = audit(cw["db"], pcfg.AUDIT_SECRET_SET)
    assert row["detail"] == {"tenant_id": "eng", "install_id": install_id, "version_id": done["version_id"],
                             "field": "api_key"}
    assert read(cw, "eng", install_id).json()["missing"] == []

    # ONE USE: the same link again stores nothing.
    again = store(cw, "eng", install_id, "api_key", value_of(), token)
    assert again.status_code == 403 and again.json()["code"] == "secret_link_invalid"
    assert len(cw["writer"].payloads[name]) == 1


def test_the_secret_route_is_uncaptured():
    assert rc.SECRET_ROUTE in UNCAPTURED_ROUTES


@pytest.mark.parametrize("how", ["none", "unknown", "expired", "another_plugin"])
def test_a_secret_without_its_own_live_link_is_not_stored(cw, how):
    done = eng_plugin(cw)
    install_id = done["install_id"]
    if how == "none":
        token = ""
    elif how == "unknown":
        token = secrets.token_urlsafe(32)
    elif how == "expired":
        token = token_of(mint(cw, "eng", install_id, "api_key"))
        [path] = [p for p in cw["db"].docs if p.startswith(pcfg.LINKS + "/")]
        cw["db"].docs[path]["expires_at"] -= pcfg.LINK_TTL + timedelta(seconds=1)
    else:
        token = token_of(mint(cw, "eng", install_id, "api_key"))
        install_id = platform_plugin(cw, manifest=manifest("other-kit", USER_CONFIG))["install_id"]
    response = store(cw, "eng", install_id, "api_key", value_of(), token) if token else cw["client"].put(
        f"{_base('eng')}/{install_id}/secrets/api_key", headers=auth_header("root"),
        json={"value": value_of(), "link_token": ""})
    assert response.status_code in (403, 422), response.text
    assert cw["writer"].payloads == {}
    assert audit(cw["db"], pcfg.AUDIT_SECRET_SET) == []


def test_a_link_presented_for_another_field_is_not_burnt(cw):
    done = platform_plugin(cw, manifest=manifest("lint-kit"), overlay=OVERLAY)
    install_id = done["install_id"]
    token = token_of(mint(cw, "eng", install_id, "token"))
    wrong = store(cw, "eng", install_id, "rules", "content", token)
    assert wrong.status_code == 403 and cw["writer"].payloads == {}
    right = store(cw, "eng", install_id, "token", value_of(), token)
    assert right.status_code == 200, right.text


def test_a_file_settings_digest_is_recorded_and_its_content_is_not(cw):
    done = platform_plugin(cw, manifest=manifest("lint-kit"), overlay=OVERLAY)
    install_id = done["install_id"]
    content = "rule: " + secrets.token_hex(8)
    stored = store(cw, "eng", install_id, "rules", content, token_of(mint(cw, "eng", install_id, "rules")))
    assert stored.status_code == 200, stored.text
    assert stored.json()["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert stored.json()["bytes"] == len(content.encode())
    assert not any(content in repr(d) for d in cw["db"].docs.values())


def test_a_link_is_only_for_a_secret_or_file_setting(cw):
    done = eng_plugin(cw)
    response = mint(cw, "eng", done["install_id"], "level")
    assert response.status_code == 422 and response.json()["code"] == "field_not_write_only"


def test_a_secret_is_removed_and_its_versions_destroyed(cw):
    done = eng_plugin(cw)
    install_id = done["install_id"]
    assert store(cw, "eng", install_id, "api_key", value_of(),
                 token_of(mint(cw, "eng", install_id, "api_key"))).status_code == 200
    removed = cw["client"].delete(f"{_base('eng')}/{install_id}/secrets/api_key", headers=auth_header("root"))
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"install_id": install_id, "field": "api_key", "set": False, "versions_destroyed": 1}
    assert cw["writer"].payloads == {}
    assert read(cw, "eng", install_id).json()["values"]["api_key"] == {"set": False}
    again = cw["client"].delete(f"{_base('eng')}/{install_id}/secrets/api_key", headers=auth_header("root"))
    assert again.status_code == 404 and again.json()["code"] == "secret_not_set"


# --- bindings --------------------------------------------------------------------------


def test_a_secret_bound_to_the_workspace_key_replaces_a_stored_value(cw):
    done = eng_plugin(cw, components=NODE)
    install_id = done["install_id"]
    assert store(cw, "eng", install_id, "api_key", value_of(),
                 token_of(mint(cw, "eng", install_id, "api_key"))).status_code == 200
    bound = cw["client"].put(f"{_base('eng')}/{install_id}/secrets/api_key/binding",
                             headers=auth_header("root"), json={"provider": "typesafe"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["binding"] == {"kind": "provider_key", "provider": "typesafe"}
    assert cw["writer"].payloads == {}
    assert [r["detail"]["provider"] for r in audit(cw["db"], pcfg.AUDIT_SECRET_BOUND)] == ["typesafe"]
    assert read(cw, "eng", install_id).json()["values"]["api_key"]["binding"]["provider"] == "typesafe"


@pytest.mark.parametrize(("components", "field", "provider", "code"), [
    (PYTHON, "api_key", "typesafe", "binding_needs_node"),
    (NODE, "api_key", "github", "binding_provider_refused"),
    (NODE, "level", "typesafe", "field_not_bindable"),
])
def test_a_binding_is_refused_outside_its_closed_list(cw, components, field, provider, code):
    done = eng_plugin(cw, components=components)
    response = cw["client"].put(f"{_base('eng')}/{done['install_id']}/secrets/{field}/binding",
                                headers=auth_header("root"), json={"provider": provider})
    assert response.status_code == 422 and response.json()["code"] == code
    assert audit(cw["db"], pcfg.AUDIT_SECRET_BOUND) == []


# --- defaults --------------------------------------------------------------------------


def _allow(w, tenant_id: str, *names: str) -> None:
    response = w["client"].put(f"/v1/workspaces/{tenant_id}/plugins", headers=auth_header("root"),
                               json={"allowed": list(names)})
    assert response.status_code == 200, response.text


def test_defaults_are_set_and_a_step_gets_them_with_an_incompatible_one_skipped_and_recorded(cw):
    _allow(cw, "eng", "gstack")
    runs = eng_plugin(cw, name="eng-kit", manifest=manifest("eng-kit"))
    large = eng_plugin(cw, name="big-kit", manifest=manifest("big-kit"),
                       requires={"suggested": {"hosts": [], "runtimes": []},
                                 "declared": {"profiles": ["claude-code-large"]}})
    wanted = [runs["install_id"], large["install_id"], "gstack"]
    response = cw["client"].put(_defaults("eng"), headers=auth_header("root"), json={"defaults": wanted})
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == sorted(wanted)
    member = cw["client"].get(_defaults("eng"), headers=auth_header("alice")).json()
    assert member["defaults"] == sorted(wanted) and member["can_edit"] is False
    [row] = audit(cw["db"], pcfg.AUDIT_DEFAULTS_SET)
    assert row["detail"]["from"] == [] and row["detail"]["to"] == sorted(wanted)

    ctx = cw["ctx"]
    configs = rc._configs(ctx)
    step = pcfg.step_plugins(_store(ctx), configs, tenant_id="eng", runner_profile="claude-code",
                             allowed=["gstack"])
    assert list(step["plugins"]) == [runs["install_id"]] and step["built_ins"] == ["gstack"]
    assert step["skipped"] == [{"install_id": large["install_id"], "code": "plugin_profile_unsupported"}]
    on_large = pcfg.step_plugins(_store(ctx), configs, tenant_id="eng", runner_profile="claude-code-large",
                                 allowed=["gstack"])
    assert sorted(on_large["plugins"]) == sorted([runs["install_id"], large["install_id"]])
    assert on_large["skipped"] == []


@pytest.mark.parametrize("name", ["security-audit", "w-000000000000", "p-000000000000", "not an id"])
def test_a_default_the_workspace_neither_allows_nor_installed_is_refused(cw, name):
    _allow(cw, "eng", "gstack")
    response = cw["client"].put(_defaults("eng"), headers=auth_header("root"), json={"defaults": [name]})
    assert response.status_code == 422 and response.json()["code"] == "default_not_allowed"
    assert audit(cw["db"], pcfg.AUDIT_DEFAULTS_SET) == []


def test_another_workspaces_install_is_not_a_default(cw):
    done = eng_plugin(cw)
    seed_tenant(cw["db"], CAROL_TENANT)
    cw["db"].docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": "carol@example.com"})
    response = cw["client"].put(_defaults(CAROL_TENANT), headers=auth_header("carol"),
                                json={"defaults": [done["install_id"]]})
    assert response.status_code == 422 and response.json()["code"] == "default_not_allowed"


def test_after_the_allowed_list_is_written_again_settings_keep_their_revision(cw):
    # plugins.PluginsStore.put carries `configs_digest` (and `defaults`)
    # over under a block that verifies (the submission-merge lane, on this
    # PR's question 1): the settings still read, and the next save names
    # the revision they were saved at.
    _allow(cw, "eng", "gstack")
    done = eng_plugin(cw)
    assert save(cw, "eng", done["install_id"], {"level": "error"}).status_code == 200
    _allow(cw, "eng", "gstack", "security-audit")
    assert read(cw, "eng", done["install_id"]).json()["revision"] == 1
    again = save(cw, "eng", done["install_id"], {"level": "info"}, revision=1)
    assert again.status_code == 200, again.text
    assert again.json()["values"]["level"] == {"set": True, "value": "info"}


def test_a_signed_block_whose_settings_digest_was_removed_does_not_verify(cw):
    done = eng_plugin(cw)
    assert save(cw, "eng", done["install_id"], {"level": "error"}).status_code == 200
    cw["db"].docs["workspace_settings/eng"]["plugins"].pop("configs_digest")
    # The MAC covers configs_digest: removing it is a forgery, not "no settings".
    response = save(cw, "eng", done["install_id"], {"level": "info"}, revision=1)
    assert response.status_code == 409 and response.json()["code"] == "plugins_block_unverified"


def test_defaults_planted_around_the_api_are_not_read(cw):
    _allow(cw, "eng", "gstack")
    cw["db"].docs["workspace_settings/eng"]["plugins"]["defaults"] = ["gstack"]
    assert cw["client"].get(_defaults("eng"), headers=auth_header("root")).json()["defaults"] == []


# --- who may -----------------------------------------------------------------------------


def _calls(install_id: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    base = f"{_base('eng')}/{install_id}"
    return [
        ("PUT", base, {"version_id": "v-000000000000", "revision": 0, "values": {}}),
        ("POST", base + "/secrets/api_key/links", None),
        ("PUT", base + "/secrets/api_key", {"value": "x", "link_token": "y"}),
        ("DELETE", base + "/secrets/api_key", None),
        ("PUT", base + "/secrets/api_key/binding", {"provider": "typesafe"}),
        ("PUT", _defaults("eng"), {"defaults": []}),
    ]


@pytest.mark.parametrize("index", range(6))
def test_a_member_who_is_not_the_workspaces_admin_changes_nothing(cw, index):
    done = eng_plugin(cw, components=NODE)
    method, path, body = _calls(done["install_id"])[index]
    response = cw["client"].request(method, path, json=body, headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert audit(cw["db"]) == [] and cw["writer"].payloads == {} and cw["writer"].created == []


@pytest.mark.parametrize("index", range(6))
def test_someone_outside_the_workspace_reads_and_changes_nothing(cw, index):
    done = eng_plugin(cw, components=NODE)
    assert read(cw, "eng", done["install_id"], "bob").status_code in (403, 404)
    method, path, body = _calls(done["install_id"])[index]
    response = cw["client"].request(method, path, json=body, headers=auth_header("bob"))
    assert response.status_code in (403, 404), response.text
    assert audit(cw["db"]) == [] and cw["writer"].payloads == {}


def test_a_platform_admin_who_is_not_a_member_reads_and_changes_nothing(cw):
    done = platform_plugin(cw)
    install_id = done["install_id"]
    answer = read(cw, "research", install_id, "root")
    assert answer.status_code == 200 and answer.json()["can_edit"] is False
    assert save(cw, "research", install_id, {"level": "error"}).status_code == 403
    assert mint(cw, "research", install_id, "api_key").status_code == 403
    assert cw["client"].put(_defaults("research"), headers=auth_header("root"),
                            json={"defaults": []}).status_code == 403
    assert audit(cw["db"]) == []


def test_the_owner_of_a_personal_workspace_configures_a_platform_plugin(cw):
    seed_tenant(cw["db"], CAROL_TENANT)
    cw["db"].docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": "carol@example.com"})
    done = platform_plugin(cw)
    saved = save(cw, CAROL_TENANT, done["install_id"], {"level": "error"}, user="carol")
    assert saved.status_code == 200, saved.text
    assert store(cw, CAROL_TENANT, done["install_id"], "api_key", value_of(),
                 token_of(mint(cw, CAROL_TENANT, done["install_id"], "api_key", "carol")), "carol").status_code == 200
    assert list(cw["writer"].payloads) == [pcfg.secret_name(CAROL_TENANT, done["install_id"], "api_key")]
    assert ps.INSTALL_ID.match(done["install_id"])
