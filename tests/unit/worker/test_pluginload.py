"""A step's managed plugins at the start of a step (`agent_worker.pluginload`).

docs/design/managed-plugins.md section 4 (lane 8), held offline against a
filesystem object store and tars built here:

  * a valid signed set loads every plugin from its pinned copy, unpacked
    read-only under private/, every hook and MCP server command wrapped by
    the launcher, the runner handed paths only;
  * a digest mismatch, a missing object, an object outside the workspace's
    or the platform's folder, an escaping entry, a manifest that names
    another plugin, two plugins of one name, and a set on an unsigned task
    each refuse the attempt with a fixed code and sentence;
  * settings reach the plugin with no keychain: `userConfig` options for the
    runner's settings (a sensitive one as a placeholder), each secret as a
    0600 file the launcher alone reads, never in the runner's environment;
  * the record counts the launcher's runs, never their text;
  * the entry swarm-api signs (`pluginconfig.step_entry`) is the entry this
    worker reads.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import secrets
import stat
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import pluginload, plugins
from agent_worker.errors import ConfigError
from agent_worker.objectstore import LocalObjectStore

TENANT = "eng"
BELAY = "p-" + "a" * 12
ENFORCE = "w-" + "b" * 12
VERSION = "v-" + "c" * 12
REPO = Path(__file__).resolve().parents[3]


def _tar(files: dict[str, str | bytes], *, links: dict[str, str] | None = None) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            data = files[name].encode() if isinstance(files[name], str) else files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o555 if name.endswith(".sh") else 0o444
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return out.getvalue()


def _plugin_files(name: str, *, user_config: dict | None = None) -> dict[str, str]:
    manifest: dict[str, Any] = {"name": name, "version": "1.0.0"}
    if user_config is not None:
        manifest["userConfig"] = user_config
    return {
        ".claude-plugin/plugin.json": json.dumps(manifest),
        "hooks/hooks.json": json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": 'node "${CLAUDE_PLUGIN_ROOT}/hook.mjs" stop',
                                 "timeout": 25}]}],
            "PostToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/hook.mjs", "edit"]}]}],
        }}),
        ".mcp.json": json.dumps({"mcpServers": {"probe": {"command": "node",
                                                          "args": ["${CLAUDE_PLUGIN_ROOT}/server.mjs"]},
                                                "remote": {"url": "https://mcp.invalid/x"}}}),
        "hook.mjs": "process.exit(0);\n",
        "server.mjs": "process.exit(0);\n",
        "skills/check/SKILL.md": "---\nname: check\ndescription: checks\n---\nCheck it.\n",
    }


def _entry(install_id: str, name: str, data: bytes, *, scope: str | None = None,
           **extra: Any) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    scope = scope or ("platform" if install_id.startswith("p-") else "workspace")
    obj = f"platform/{digest}.tar" if scope == "platform" else f"tenants/{TENANT}/{digest}.tar"
    return {"name": name, "scope": scope, "version_id": VERSION, "runnable_sha256": digest, "object": obj,
            "env": {}, "files": {}, "secrets": {}, "skills": None, "disabled_skills": [], **extra}


def _task(block: dict[str, Any] | None, *, signed: bool = True) -> dict[str, Any]:
    task: dict[str, Any] = {"id": "t1", "tenant_id": TENANT, "metadata": {"dispatch": {}}}
    if block is not None:
        task["metadata"]["dispatch"]["plugins"] = block
    if signed:
        task["spec_signature"] = secrets.token_hex(32)
        task["spec_key_version"] = "spec-key/1"
    return task


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "bucket")


def _put(store: LocalObjectStore, entry: dict[str, Any], data: bytes) -> None:
    store.upload_bytes(entry["object"], data)


def _load(task: dict[str, Any], tmp_path: Path, store: LocalObjectStore, **kwargs: Any):
    return pluginload.load(task, private_dir=tmp_path / "private", store=store, environ={},
                           managed_settings=tmp_path / "no-managed-settings.json", **kwargs)


# --- a valid set ----------------------------------------------------------------


def test_a_valid_signed_set_loads_every_plugin_from_its_pinned_copy(tmp_path, store):
    belay_tar, enforce_tar = _tar(_plugin_files("jev-belay")), _tar(_plugin_files("jev-enforce"))
    belay, enforce = _entry(BELAY, "jev-belay", belay_tar), _entry(ENFORCE, "jev-enforce", enforce_tar)
    _put(store, belay, belay_tar)
    _put(store, enforce, enforce_tar)
    loaded = _load(_task({BELAY: belay, ENFORCE: enforce}), tmp_path, store)
    assert [p.entry.install_id for p in loaded.plugins] == [BELAY, ENFORCE]
    private = tmp_path / "private"
    for p in loaded.plugins:
        assert p.directory == private / "plugins" / p.entry.install_id
        assert (p.directory / "skills" / "check" / "SKILL.md").read_text().startswith("---")
        # Read-only, and the downloaded tar is gone.
        assert not (p.directory / "hook.mjs").stat().st_mode & stat.S_IWUSR
        assert not (p.directory.stat().st_mode & stat.S_IWUSR)
        assert not (private / "plugins" / f"{p.entry.runnable_sha256}.tar").exists()
        assert (private / "plugin-data" / p.entry.install_id).is_dir()
    # The runner is handed paths, never a value.
    assert loaded.env == {"SWARM_PLUGIN_LOAD": str(private / "plugins" / "load.json"),
                          "SWARM_PLUGIN_ENV_DIR": str(private / "plugin-env")}
    load_file = json.loads((private / "plugins" / "load.json").read_text())
    assert [(p["name"], p["dir"]) for p in load_file["plugins"]] == [
        ("jev-belay", str(private / "plugins" / BELAY)), ("jev-enforce", str(private / "plugins" / ENFORCE))]


def test_every_hook_and_server_command_runs_through_the_launcher(tmp_path, store):
    data = _tar(_plugin_files("probe"))
    entry = _entry(BELAY, "probe", data)
    _put(store, entry, data)
    (plugin,) = _load(_task({BELAY: entry}), tmp_path, store).plugins
    hooks = json.loads((plugin.directory / "hooks" / "hooks.json").read_text())["hooks"]
    (stop,) = hooks["Stop"][0]["hooks"]
    assert stop["command"] == f'{pluginload.LAUNCHER} {BELAY} -- node "${{CLAUDE_PLUGIN_ROOT}}/hook.mjs" stop'
    (edit,) = hooks["PostToolUse"][0]["hooks"]
    assert edit["command"] == pluginload.LAUNCHER
    assert edit["args"] == [BELAY, "--", "node", "${CLAUDE_PLUGIN_ROOT}/hook.mjs", "edit"]
    servers = json.loads((plugin.directory / ".mcp.json").read_text())["mcpServers"]
    assert servers["probe"] == {"command": pluginload.LAUNCHER,
                                "args": [BELAY, "--mcp", "probe", "--", "node", "${CLAUDE_PLUGIN_ROOT}/server.mjs"]}
    assert servers["remote"] == {"url": "https://mcp.invalid/x"}


def test_a_command_already_wrapped_is_left_as_it_is():
    hook = {"type": "command", "command": f"{pluginload.LAUNCHER} {BELAY} -- node x.mjs"}
    assert pluginload.wrap_hooks({"Stop": [{"hooks": [hook]}]}, BELAY) == {"Stop": [{"hooks": [hook]}]}


def test_the_launcher_path_is_the_one_swarm_api_names():
    from swarm_api import pluginconfig

    assert pluginload.LAUNCHER == pluginconfig.LAUNCHER
    assert pluginload.OPTION_PREFIX == pluginconfig.OPTION_PREFIX


# --- refusals ------------------------------------------------------------------------


def _refused(excinfo: pytest.ExceptionInfo, code: str) -> None:
    exc = excinfo.value
    assert isinstance(exc, ConfigError) and exc.code == code
    assert str(exc) == pluginload.REFUSALS[code]


def test_a_digest_mismatch_refuses_the_attempt_with_a_fixed_code(tmp_path, store):
    data = _tar(_plugin_files("jev-belay"))
    entry = _entry(BELAY, "jev-belay", data)
    store.upload_bytes(entry["object"], _tar(_plugin_files("jev-belay")) + b"\0" * 512)
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}), tmp_path, store)
    _refused(excinfo, "plugin_digest_mismatch")
    assert excinfo.value.detail() == {"code": "plugin_digest_mismatch", "install_id": BELAY, "version_id": VERSION}
    # Nothing was unpacked, and the bytes are gone.
    assert not (tmp_path / "private" / "plugins" / BELAY).exists()
    assert not list((tmp_path / "private" / "plugins").glob("*.tar"))


def test_an_unsigned_set_refuses_the_attempt_with_a_fixed_code(tmp_path, store):
    data = _tar(_plugin_files("jev-belay"))
    entry = _entry(BELAY, "jev-belay", data)
    _put(store, entry, data)
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}, signed=False), tmp_path, store)
    _refused(excinfo, "plugins_unsigned")
    assert not (tmp_path / "private" / "plugins").exists()


def test_the_start_of_a_step_refuses_an_unsigned_set_before_anything_is_linked(tmp_path, store):
    data = _tar(_plugin_files("jev-belay"))
    entry = _entry(BELAY, "jev-belay", data)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ConfigError):
        plugins.start(_task({BELAY: entry}, signed=False), work=work, checkout=None,
                      profile="claude-code", image="img", store=store, environ={})


def test_a_missing_object_refuses_the_attempt(tmp_path, store):
    entry = _entry(BELAY, "jev-belay", _tar(_plugin_files("jev-belay")))
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}), tmp_path, store)
    _refused(excinfo, "plugin_object_missing")


def test_an_object_in_another_workspaces_folder_is_refused_before_any_download(tmp_path, store):
    data = _tar(_plugin_files("jev-enforce"))
    entry = _entry(ENFORCE, "jev-enforce", data)
    entry["object"] = f"tenants/other/{entry['runnable_sha256']}.tar"
    store.upload_bytes(entry["object"], data)
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({ENFORCE: entry}), tmp_path, store)
    _refused(excinfo, "plugin_object_foreign")


@pytest.mark.parametrize("files,links", [
    ({"../escape.txt": "x", ".claude-plugin/plugin.json": '{"name": "jev-belay"}'}, None),
    ({".claude-plugin/plugin.json": '{"name": "jev-belay"}'}, {"out": "../../etc/passwd"}),
    ({".claude-plugin/plugin.json": '{"name": "jev-belay"}', "A.md": "a", "a.md": "b"}, None),
])
def test_an_entry_that_escapes_or_collides_is_refused(tmp_path, store, files, links):
    data = _tar(files, links=links)
    entry = _entry(BELAY, "jev-belay", data)
    _put(store, entry, data)
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}), tmp_path, store)
    _refused(excinfo, "plugin_entry_refused")
    assert not (tmp_path / "escape.txt").exists()


def test_a_copy_naming_another_plugin_is_refused(tmp_path, store):
    data = _tar(_plugin_files("something-else"))
    entry = _entry(BELAY, "jev-belay", data)
    _put(store, entry, data)
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}), tmp_path, store)
    _refused(excinfo, "plugin_manifest_mismatch")
    assert not (tmp_path / "private" / "plugins" / BELAY).exists()


def test_two_plugins_of_one_name_are_refused(tmp_path, store):
    data = _tar(_plugin_files("jev-belay"))
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: _entry(BELAY, "jev-belay", data), ENFORCE: _entry(ENFORCE, "jev-belay", data)}),
              tmp_path, store)
    _refused(excinfo, "plugin_name_duplicate")


@pytest.mark.parametrize("change", [
    {"env": {"NODE_OPTIONS": "--require /tmp/x.js"}},
    {"env": {"PATH": "/tmp"}},
    {"scope": "workspace"},
    {"runnable_sha256": "not-a-digest"},
    {"secrets": {"TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:elsewhere"}}},
])
def test_a_set_not_in_the_signed_shape_is_refused(tmp_path, store, change):
    entry = {**_entry(BELAY, "jev-belay", b"x"), **change}
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _load(_task({BELAY: entry}), tmp_path, store)
    assert excinfo.value.code in ("plugin_set_invalid", "plugin_object_foreign")


def test_an_image_that_turns_sideloading_off_refuses_the_attempt(tmp_path, store):
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"disableSideloadFlags": True}))
    entry = _entry(BELAY, "jev-belay", b"x")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        pluginload.load(_task({BELAY: entry}), private_dir=tmp_path / "private", store=store, environ={},
                        managed_settings=managed)
    _refused(excinfo, "plugin_sideload_disabled")


def test_a_step_with_no_set_loads_nothing_and_reads_no_bucket(tmp_path):
    assert pluginload.load(_task(None, signed=False), private_dir=tmp_path, store=None, environ={}) is None
    work = tmp_path / "work"
    work.mkdir()
    assert plugins.start(_task(None, signed=False), work=work, checkout=None, profile="claude-code",
                         image="img") is None


def test_the_bucket_is_the_named_one_or_terraforms_default():
    spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
    placeholders = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(placeholders)
    project = placeholders.PROJECT
    assert pluginload.bucket_name({"PROJECT_ID": project}) == f"{project}-swarm-plugins"
    assert pluginload.bucket_name({"PROJECT_ID": project, "SWARM_PLUGINS_BUCKET": "named-swarm-x"}) == "named-swarm-x"
    assert pluginload.bucket_name({}) is None
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        pluginload.default_store({})
    assert excinfo.value.code == "plugins_bucket_unknown"


# --- settings and secrets ---------------------------------------------------------


USER_CONFIG = {
    "typesafe_api_key": {"type": "string", "title": "Key", "sensitive": True, "required": True},
    "threshold": {"type": "number", "title": "Threshold"},
    "shadow": {"type": "boolean", "title": "Shadow"},
}


def test_settings_reach_the_plugin_without_a_keychain(tmp_path, store):
    data = _tar(_plugin_files("jev-belay", user_config=USER_CONFIG))
    secret = "ts-" + secrets.token_hex(16)
    rules = "Never edit generated files.\n"
    entry = _entry(BELAY, "jev-belay", data,
                   env={"CLAUDE_PLUGIN_OPTION_THRESHOLD": "0.7", "CLAUDE_PLUGIN_OPTION_SHADOW": "true",
                        "JEV_BELAY_CHECK": "1"},
                   files={"JEV_RULES_FILE": {"field": "rules", "sha256": hashlib.sha256(rules.encode()).hexdigest()}},
                   secrets={"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": {"field": "typesafe_api_key",
                                                                      "secret": "plugin"}})
    _put(store, entry, data)
    work = tmp_path / "work"
    work.mkdir()
    started = plugins.start(
        _task({BELAY: entry}), work=work, checkout=None, profile="claude-code", image="img", store=store,
        environ={}, released={(BELAY, "CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY"): secret,
                              (BELAY, "JEV_RULES_FILE"): rules})
    (plugin,) = started.managed
    # The CLI's options: the non-secret values typed, the secret a placeholder.
    assert plugin.options == {"typesafe_api_key": pluginload.PLACEHOLDER, "threshold": 0.7, "shadow": True}
    load_file = Path(started.env["SWARM_PLUGIN_LOAD"])
    assert secret not in load_file.read_text()
    # The launcher's file: values, and the secret's and the rules file's PATHS.
    env_file = Path(started.env["SWARM_PLUGIN_ENV_DIR"]) / f"{BELAY}.json"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    document = json.loads(env_file.read_text())
    assert secret not in env_file.read_text()
    assert document["env"]["JEV_BELAY_CHECK"] == "1"
    secret_path = Path(document["secrets"]["CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY"])
    assert secret_path.read_text() == secret and stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert Path(document["files"]["JEV_RULES_FILE"]).read_text() == rules
    # Under private/, never under the checkpointed work/.
    assert (tmp_path / "private") in secret_path.parents and work not in secret_path.parents
    # The runner's environment holds paths only.
    assert all(secret not in v for v in started.env.values())
    assert set(started.env) == {"SWARM_PLUGIN_LOAD", "SWARM_PLUGIN_ENV_DIR"}


def test_a_file_whose_content_is_not_the_signed_one_is_not_handed_over(tmp_path, store):
    data = _tar(_plugin_files("jev-enforce"))
    entry = _entry(ENFORCE, "jev-enforce", data,
                   files={"JEV_RULES_FILE": {"field": "rules", "sha256": hashlib.sha256(b"signed").hexdigest()}})
    _put(store, entry, data)
    (plugin,) = _load(_task({ENFORCE: entry}), tmp_path, store,
                      released={(ENFORCE, "JEV_RULES_FILE"): "changed since"}).plugins
    assert plugin.secrets_missing == ("JEV_RULES_FILE",)
    document = json.loads((tmp_path / "private" / "plugin-env" / f"{ENFORCE}.json").read_text())
    assert document["files"] == {}


def test_a_typesafe_binding_brings_the_adapter_and_writes_the_key_file_only(tmp_path, store):
    data = _tar(_plugin_files("jev-enforce"))
    key = "ts-" + secrets.token_hex(16)
    entry = _entry(ENFORCE, "jev-enforce", data, env={"JEV_ENFORCE_MODE": "end"},
                   secrets={"TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:typesafe"}})
    _put(store, entry, data)
    asked: list[str] = []

    def provider_key(provider: str) -> str:
        asked.append(provider)
        return key

    (plugin,) = _load(_task({ENFORCE: entry}), tmp_path, store, provider_key=provider_key).plugins
    assert asked == ["typesafe"] and plugin.adapter
    document = json.loads((tmp_path / "private" / "plugin-env" / f"{ENFORCE}.json").read_text())
    adapter = document["adapter"]
    assert adapter["variables"] == ["TYPESAFE_API_KEY"] and adapter["module"] == pluginload.JEV_ADAPTER
    assert Path(adapter["key_file"]).read_text() == key
    # The calls file the worker already settles (judge/account.py).
    assert adapter["calls_file"] == str(tmp_path / "private" / "jev-gates.calls")
    # The key is no plain secret of the plugin: only the adapter sets it.
    assert document["secrets"] == {} and key not in json.dumps(document)


def test_a_secret_nobody_delivered_leaves_the_plugin_loaded_and_counted(tmp_path, store):
    data = _tar(_plugin_files("jev-enforce"))
    entry = _entry(ENFORCE, "jev-enforce", data,
                   secrets={"TYPESAFE_API_KEY": {"field": "key", "binding": "provider_key:typesafe"}})
    _put(store, entry, data)
    (plugin,) = _load(_task({ENFORCE: entry}), tmp_path, store).plugins
    assert plugin.secrets_missing == ("TYPESAFE_API_KEY",) and not plugin.adapter
    assert pluginload.record(plugin, [])["secrets_missing"] == 1


def test_the_entry_swarm_api_signs_is_the_entry_this_worker_loads(tmp_path, store):
    from swarm_api import pluginconfig

    data = _tar(_plugin_files("jev-belay", user_config=USER_CONFIG))
    digest = hashlib.sha256(data).hexdigest()
    install = {"install_id": BELAY, "name": "jev-belay", "scope": "platform"}
    version = {"version_id": VERSION, "runnable_sha256": digest, "object": f"platform/{digest}.tar",
               "manifest": {"user_config": [
                   {"key": "typesafe_api_key", "type": "string", "sensitive": True, "required": True},
                   {"key": "threshold", "type": "number"}]}}
    config = {"fields": {"typesafe_api_key": {"binding": {"kind": "provider_key", "provider": "typesafe"}},
                         "threshold": {"value": 0.5}}}
    signed, missing = pluginconfig.step_entry(install, version, config)
    assert missing is None
    store.upload_bytes(signed["object"], data)
    key = "ts-" + secrets.token_hex(16)
    (plugin,) = _load(_task({BELAY: signed}), tmp_path, store, provider_key=lambda _p: key).plugins
    assert plugin.options == {"typesafe_api_key": pluginload.PLACEHOLDER, "threshold": 0.5}
    assert plugin.adapter


# --- the record ----------------------------------------------------------------


def test_the_record_counts_the_launchers_runs_and_names_nothing_else(tmp_path, store):
    data = _tar(_plugin_files("jev-belay"))
    entry = _entry(BELAY, "jev-belay", data)
    _put(store, entry, data)
    work = tmp_path / "work"
    work.mkdir()
    started = plugins.start(_task({BELAY: entry}), work=work, checkout=None, profile="claude-code",
                            image="img", store=store, environ={})
    (plugin,) = started.managed
    rows = [{"kind": "hook", "event": "Stop", "exit": 2, "ms": 10},
            {"kind": "hook", "event": "Stop", "exit": 0, "ms": 10},
            {"kind": "hook", "event": "PostToolUse", "tool": "Edit", "exit": 1, "ms": 5},
            {"kind": "mcp", "server": "probe", "event": "start"}]
    plugin.runs_file.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "jev-belay:check"}}]}}) + "\n")
    record = plugins.record(started, stream)
    assert record == {BELAY: {
        "managed": True, "name": "jev-belay", "scope": "platform", "version_id": VERSION,
        "runnable_sha256": entry["runnable_sha256"],
        "hooks": {"PostToolUse": {"runs": 1, "blocks": 0, "errors": 1},
                  "Stop": {"runs": 2, "blocks": 1, "errors": 0}},
        "mcp_servers_started": ["probe"], "skill_runs": {"check": 1},
    }}
