"""A managed plugin's settings form, value checks, step entries and defaults, as functions.

docs/design/managed-plugins.md sections 4.4, 6 and 7 (lane 6 of section 17):
the form from a manifest's `userConfig`, the overlay's declared settings
after it, the deny list, the checks a value passes, the signed set's entry
for one plugin and a step's plugin set, where a default the step cannot run
is skipped and recorded (the owner's answer to Q6) and a plugin the step
itself names is refused. The routes are in test_plugin_config_routes.py.

Pure: no Firestore, no clock, no network.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from swarm_api import pluginconfig as pcfg
from swarm_api import pluginstore as ps

INSTALL = "w-0123456789ab"
OTHER = "w-ba9876543210"


def _version(*, user_config: list[dict[str, Any]] | None = None, overlay: dict[str, Any] | None = None,
             requires: dict[str, Any] | None = None, components: dict[str, Any] | None = None,
             version_id: str = "v-0123456789ab") -> dict[str, Any]:
    return {"version_id": version_id, "manifest": {"name": "lint-kit", "user_config": user_config or []},
            "overlay": overlay, "requires": requires or {"suggested": {"hosts": [], "runtimes": []},
                                                         "declared": None},
            "components": components or {"hooks": [], "mcp_servers": []},
            "runnable_sha256": "0" * 64, "object": "tenants/eng/" + "0" * 64 + ".tar"}


MANIFEST_FIELDS = [
    {"key": "level", "type": "string", "title": "Level", "default": "warn"},
    {"key": "max_issues", "type": "number", "required": True},
    {"key": "strict", "type": "boolean"},
    {"key": "rules_dir", "type": "directory"},
    {"key": "api.key", "type": "string", "sensitive": True, "required": True, "default": "never-shown"},
    {"key": "tags", "type": "string", "multiple": True},
]


# --- the form -------------------------------------------------------------------


def test_the_form_follows_the_manifests_user_config():
    fields = pcfg.form_fields(_version(user_config=MANIFEST_FIELDS))
    assert [f["key"] for f in fields] == ["level", "max_issues", "strict", "rules_dir", "api.key", "tags"]
    by = {f["key"]: f for f in fields}
    assert {k: by[k]["kind"] for k in by} == {
        "level": "value", "max_issues": "value", "strict": "value", "rules_dir": "path",
        "api.key": "secret", "tags": "value"}
    # The launcher's variable for a userConfig field (section 4.4).
    assert by["api.key"]["variable"] == "CLAUDE_PLUGIN_OPTION_API_KEY"
    assert by["level"]["variable"] == "CLAUDE_PLUGIN_OPTION_LEVEL"
    # A secret's default never reaches the form; it may be bound to a workspace key.
    assert by["api.key"]["default"] is None and by["api.key"]["bindable"] == ["typesafe"]
    assert by["level"]["default"] == "warn" and by["level"]["bindable"] == []
    assert by["tags"]["multiple"] is True and by["max_issues"]["required"] is True
    assert pcfg.form({"install_id": INSTALL}, _version(user_config=MANIFEST_FIELDS))["source"] == "user_config"


def test_a_field_the_intake_job_would_not_have_written_is_left_out():
    fields = pcfg.form_fields(_version(user_config=[
        {"key": "ok", "type": "string"},
        {"key": "ok", "type": "number"},          # a second of the same key
        {"key": "../evil", "type": "string"},     # not a key
        {"key": "odd", "type": "object"},         # not a type
        "not a mapping",
    ]))
    assert [(f["key"], f["type"]) for f in fields] == [("ok", "string")]


OVERLAY = {"settings": [
    {"key": "token", "type": "string", "variable": "LINT_TOKEN", "sensitive": True},
    {"key": "rules", "type": "file", "variable": "LINT_RULES", "target": "file"},
    {"key": "config_path", "type": "file", "variable": "LINT_CONFIG"},
    {"key": "verbose", "type": "boolean", "variable": "LINT_VERBOSE"},
    {"key": "path", "type": "string", "variable": "PATH"},               # on the deny list
    {"key": "pyhome", "type": "string", "variable": "PYTHONHOME"},       # on the deny list
    {"key": "claude", "type": "string", "variable": "CLAUDE_CODE_X"},    # on the deny list
]}


def test_a_plugin_with_no_user_config_gets_its_form_from_the_overlay():
    version = _version(overlay=OVERLAY)
    fields = pcfg.form_fields(version)
    assert [(f["key"], f["kind"], f["variable"], f["target"]) for f in fields] == [
        ("token", "secret", "LINT_TOKEN", "env"),
        ("rules", "file", "LINT_RULES", "file"),
        ("config_path", "path", "LINT_CONFIG", "env"),
        ("verbose", "value", "LINT_VERBOSE", "env"),
    ]
    # A declared secret or file is required: the plugin has nothing to fall back on.
    assert [f["key"] for f in fields if f["required"]] == ["token", "rules"]
    assert pcfg.form({"install_id": INSTALL}, version)["source"] == "overlay"


def test_an_overlay_adds_fields_and_never_replaces_the_manifests():
    version = _version(user_config=[{"key": "level", "type": "string"}], overlay={"settings": [
        {"key": "level", "type": "number", "variable": "LINT_LEVEL"},
        {"key": "extra", "type": "string", "variable": "LINT_EXTRA"},
    ]})
    fields = pcfg.form_fields(version)
    assert [(f["key"], f["source"], f["type"]) for f in fields] == [
        ("level", "user_config", "string"), ("extra", "overlay", "string")]
    assert pcfg.form({"install_id": INSTALL}, version)["source"] == "both"


@pytest.mark.parametrize("name", ["PATH", "HOME", "NODE_OPTIONS", "LD_PRELOAD", "PYTHONPATH", "SWARM_X",
                                  "GOOGLE_APPLICATION_CREDENTIALS", "GIT_SSH_COMMAND", "CLAUDE_CONFIG_DIR"])
def test_the_deny_list(name):
    assert pcfg.variable_denied(name)


def test_a_launcher_variable_is_not_denied():
    assert not pcfg.variable_denied("CLAUDE_PLUGIN_OPTION_LEVEL") and not pcfg.variable_denied("LINT_TOKEN")


# --- values -------------------------------------------------------------------------


def _field(key: str, version: Mapping[str, Any]) -> dict[str, Any]:
    return {f["key"]: f for f in pcfg.form_fields(version)}[key]


@pytest.mark.parametrize(("key", "value"), [
    ("level", "error"), ("max_issues", 3), ("max_issues", 2.5), ("strict", False),
    ("rules_dir", "config/lint"), ("tags", ["a", "b"]),
])
def test_a_value_of_the_fields_type_is_accepted(key, value):
    assert pcfg.check_value(_field(key, _version(user_config=MANIFEST_FIELDS)), value) == value


@pytest.mark.parametrize(("key", "value", "code"), [
    ("max_issues", "3", "value_invalid"), ("max_issues", True, "value_invalid"),
    ("max_issues", float("inf"), "value_invalid"), ("strict", "yes", "value_invalid"),
    ("level", "x" * 4097, "value_invalid"), ("tags", "a", "value_invalid"),
    ("rules_dir", "/etc", "path_invalid"), ("rules_dir", "../up", "path_invalid"),
    ("rules_dir", "a/../../b", "path_invalid"), ("rules_dir", "~/x", "path_invalid"),
    ("rules_dir", "C:/x", "path_invalid"), ("api.key", "anything", "field_write_only"),
])
def test_a_value_not_of_the_fields_type_is_refused_without_repeating_it(key, value, code):
    with pytest.raises(pcfg.ConfigRefused) as refused:
        pcfg.check_value(_field(key, _version(user_config=MANIFEST_FIELDS)), value)
    assert refused.value.code == code
    assert refused.value.detail == {"field": key}


def test_a_file_setting_is_write_only():
    with pytest.raises(pcfg.ConfigRefused) as refused:
        pcfg.check_value(_field("rules", _version(overlay=OVERLAY)), "content")
    assert refused.value.code == "field_write_only"


def test_the_environment_text_of_a_value():
    assert [pcfg.env_text(v) for v in (True, False, 3, 2.5, "x", ["a", 1])] == [
        "true", "false", "3", "2.5", "x", '["a",1]']


# --- the signed set's entry (section 8) ---------------------------------------------


def test_a_step_entry_carries_values_secret_references_and_file_digests_never_a_value():
    version = _version(user_config=MANIFEST_FIELDS, overlay=OVERLAY)
    config = {"fields": {
        "max_issues": {"value": 5}, "tags": {"value": ["a"]},
        "api.key": {"secret": {"kind": "plugin", "set_at": "t", "set_by": "x"}},
        "token": {"binding": {"kind": "provider_key", "provider": "typesafe"}},
        "rules": {"file": {"sha256": "f" * 64, "bytes": 9}},
    }}
    entry, missing = pcfg.step_entry({"install_id": INSTALL, "name": "lint-kit", "scope": "workspace"},
                                     version, config, overrides={"level": "error"})
    assert missing is None and entry is not None
    assert entry["env"] == {"CLAUDE_PLUGIN_OPTION_LEVEL": "error", "CLAUDE_PLUGIN_OPTION_MAX_ISSUES": "5",
                            "CLAUDE_PLUGIN_OPTION_TAGS": '["a"]'}
    assert entry["secrets"] == {
        "CLAUDE_PLUGIN_OPTION_API_KEY": {"field": "api.key", "target": "env", "secret": "plugin"},
        "LINT_TOKEN": {"field": "token", "target": "env", "binding": "provider_key:typesafe"}}
    assert entry["files"] == {"LINT_RULES": {"field": "rules", "sha256": "f" * 64}}


def test_a_required_setting_with_no_value_makes_no_entry():
    version = _version(user_config=MANIFEST_FIELDS)
    entry, missing = pcfg.step_entry({"install_id": INSTALL}, version, {"fields": {"max_issues": {"value": 1}}})
    assert (entry, missing) == (None, "api.key")


@pytest.mark.parametrize(("overrides", "code"), [
    ({"api.key": "x"}, "override_refused"), ({"nope": 1}, "field_unknown"),
    ({"max_issues": "many"}, "value_invalid"),
])
def test_a_step_overrides_only_non_secret_values(overrides, code):
    with pytest.raises(pcfg.ConfigRefused) as refused:
        pcfg.step_entry({"install_id": INSTALL}, _version(user_config=MANIFEST_FIELDS), {}, overrides=overrides)
    assert refused.value.code == code


def test_a_binding_needs_every_hook_and_server_on_node():
    node = {"hooks": [{"command": pcfg.LAUNCHER + " --env X -- node ${CLAUDE_PLUGIN_ROOT}/h.js"}],
            "mcp_servers": [{"command": "/usr/bin/node"}]}
    assert pcfg.node_only(_version(components=node))
    python = {"hooks": [{"command": "python3 hook.py"}], "mcp_servers": []}
    assert not pcfg.node_only(_version(components=python))
    overlaid = _version(components={"hooks": [], "mcp_servers": []},
                        overlay={"hooks": {"hooks": {"PreToolUse": [{"hooks": [{"command": "bash x.sh"}]}]}}})
    assert not pcfg.node_only(overlaid)


# --- a step's plugin set: defaults (section 7) ---------------------------------------


class _Store:
    def __init__(self, installs: dict[str, tuple[dict[str, Any], dict[str, Any]]]) -> None:
        self.installs = installs

    def install(self, install_id: str) -> dict[str, Any] | None:
        found = self.installs.get(install_id)
        return None if found is None else found[0]

    def current(self, install: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.installs[install["install_id"]][1]


class _Configs:
    def __init__(self, defaults: list[str], configs: dict[str, dict[str, Any]] | None = None) -> None:
        self._defaults = defaults
        self._configs = configs or {}

    def defaults(self, tenant_id: str) -> list[str]:
        return list(self._defaults)

    def config(self, tenant_id: str, install_id: str) -> dict[str, Any]:
        return self._configs.get(install_id, {"fields": {}})


def _install(install_id: str, **over: Any) -> dict[str, Any]:
    return {"install_id": install_id, "name": "kit-" + install_id[-4:], "scope": ps.WORKSPACE,
            "tenant_id": "eng", "state": ps.STATE_ACTIVE, **over}


LARGE_ONLY = {"suggested": {"hosts": [], "runtimes": []}, "declared": {"profiles": ["claude-code-large"]}}


def test_every_default_reaches_the_step_with_the_workspaces_values():
    store = _Store({INSTALL: (_install(INSTALL), _version(user_config=[{"key": "level", "type": "string"}]))})
    configs = _Configs([INSTALL, "gstack"], {INSTALL: {"fields": {"level": {"value": "error"}}}})
    got = pcfg.step_plugins(store, configs, tenant_id="eng", runner_profile="claude-code", allowed=["gstack"])
    assert list(got["plugins"]) == [INSTALL] and got["built_ins"] == ["gstack"] and got["skipped"] == []
    assert got["plugins"][INSTALL]["env"] == {"CLAUDE_PLUGIN_OPTION_LEVEL": "error"}


def test_a_default_the_step_cannot_run_is_skipped_and_recorded_not_refused():
    # The owner's answer to Q6.
    store = _Store({
        INSTALL: (_install(INSTALL), _version(requires=LARGE_ONLY)),
        OTHER: (_install(OTHER), _version(requires={"suggested": {}, "declared": {"runtimes": ["ruby"]}})),
    })
    got = pcfg.step_plugins(store, _Configs([INSTALL, OTHER, "gstack"]), tenant_id="eng",
                            runner_profile="claude-code", allowed=["gstack"])
    assert got["plugins"] == {} and got["built_ins"] == ["gstack"]
    assert got["skipped"] == [{"install_id": INSTALL, "code": "plugin_profile_unsupported"},
                              {"install_id": OTHER, "code": "plugin_runtime_missing", "runtime": "ruby"}]


def test_on_a_profile_that_loads_no_plugins_every_default_is_skipped():
    store = _Store({INSTALL: (_install(INSTALL), _version())})
    got = pcfg.step_plugins(store, _Configs([INSTALL, "gstack"]), tenant_id="eng", runner_profile="codex",
                            allowed=["gstack"])
    assert (got["plugins"], got["built_ins"]) == ({}, [])
    assert [(s["install_id"], s["code"]) for s in got["skipped"]] == [
        (INSTALL, "profile_cannot_load_plugins"), ("gstack", "profile_cannot_load_plugins")]


def test_a_default_with_a_missing_setting_a_removed_one_and_one_no_longer_allowed_are_skipped():
    removed = "w-aaaaaaaaaaaa"
    store = _Store({
        INSTALL: (_install(INSTALL), _version(user_config=MANIFEST_FIELDS)),
        removed: (_install(removed, state=ps.STATE_REMOVED), _version()),
    })
    got = pcfg.step_plugins(store, _Configs([INSTALL, removed, "gstack"]), tenant_id="eng",
                            runner_profile="claude-code", allowed=[])
    assert got["skipped"] == [
        {"install_id": INSTALL, "code": "plugin_setting_missing", "field": "max_issues"},
        {"install_id": removed, "code": "plugin_removed"},
        {"install_id": "gstack", "code": "plugin_not_allowed"}]


def test_a_plugin_the_step_itself_adds_and_cannot_run_is_refused():
    store = _Store({INSTALL: (_install(INSTALL), _version(requires=LARGE_ONLY))})
    with pytest.raises(pcfg.ConfigRefused) as refused:
        pcfg.step_plugins(store, _Configs([]), tenant_id="eng", runner_profile="claude-code", allowed=[],
                          added={INSTALL: None})
    assert refused.value.code == "plugin_profile_unsupported"


def test_a_default_turned_off_for_the_step_is_neither_run_nor_recorded():
    store = _Store({INSTALL: (_install(INSTALL), _version(requires=LARGE_ONLY))})
    got = pcfg.step_plugins(store, _Configs([INSTALL]), tenant_id="eng", runner_profile="claude-code",
                            allowed=[], off=[INSTALL])
    assert got == {"plugins": {}, "built_ins": [], "skipped": []}


def test_another_workspaces_private_plugin_is_never_in_a_step():
    store = _Store({INSTALL: (_install(INSTALL, tenant_id="research"), _version())})
    got = pcfg.step_plugins(store, _Configs([INSTALL]), tenant_id="eng", runner_profile="claude-code",
                            allowed=[])
    assert got["plugins"] == {} and got["skipped"] == [{"install_id": INSTALL, "code": "plugin_not_found"}]


def test_a_step_runs_at_most_eight_plugins():
    ids = [f"w-{i:012x}" for i in range(9)]
    store = _Store({i: (_install(i), _version()) for i in ids})
    with pytest.raises(pcfg.ConfigRefused) as refused:
        pcfg.step_plugins(store, _Configs(ids), tenant_id="eng", runner_profile="claude-code", allowed=[])
    assert refused.value.code == "too_many_plugins"


# --- the secret's name (contract request 142) ------------------------------------------


def test_a_plugin_secret_is_named_by_the_frozen_contract_per_workspace_plugin_and_field():
    name = pcfg.secret_name("eng", INSTALL, "api.key")
    assert name.startswith("swarm-tenant-eng-plg-")
    assert len({name, pcfg.secret_name("research", INSTALL, "api.key"), pcfg.secret_name("eng", OTHER, "api.key"),
                pcfg.secret_name("eng", INSTALL, "token")}) == 4
    assert pcfg.plugin_consumer(INSTALL) == "plugin:" + INSTALL
