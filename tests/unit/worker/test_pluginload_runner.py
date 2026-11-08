"""The managed plugins past the loader: the runner, the launcher, the Jev adapter.

docs/design/managed-plugins.md sections 4.2 to 4.4 and 6.2 (lane 8):

  * the runner reads the worker's load file and passes `--plugin-dir` per
    plugin, the plugins' `userConfig` options through its own `--settings`
    (no keychain), and each skill the step turned off as a deny rule; a
    folder outside the load file's own folder is left out;
  * a managed jev-belay or jev-enforce replaces the built-in Jev gate for
    its step, so the step never runs both (the owner's answer to Q14);
  * `swarm-plugin-exec` gives a plugin's command its own settings and
    secrets and nothing of the agent's environment, scrubs every secret from
    what the command prints, and records counts, never text;
  * `jev-adapter.mjs` sets the TypeSafe key in a bound plugin's node process
    alone, through the adapter the baked gates' launcher also uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_worker import pluginload
from agent_worker.runners import claude_code

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "swarm-plugin-exec"
ADAPTER = REPO / "images" / "agent-runtime-base" / "jev-gates" / "bin" / "jev-adapter.mjs"
DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
BELAY = "p-" + "a" * 12
ENFORCE = "w-" + "b" * 12
OTHER = "w-" + "c" * 12


def _plugin(root: Path, name: str) -> Path:
    folder = root / name
    (folder / ".claude-plugin").mkdir(parents=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
    return folder


def _load_file(private: Path, plugins: list[dict[str, Any]]) -> Path:
    path = private / "plugins" / "load.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": plugins}))
    return path


def _settings(tmp_path: Path) -> dict[str, Any]:
    return json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())


@pytest.fixture
def loaded(tmp_path, monkeypatch) -> Path:
    private = tmp_path / "private"
    belay = _plugin(private / "plugins", "jev-belay")
    probe = _plugin(private / "plugins", "probe")
    path = _load_file(private, [
        {"install_id": BELAY, "name": "jev-belay", "dir": str(belay),
         "options": {"threshold": 0.7, "typesafe_api_key": pluginload.PLACEHOLDER}, "disabled_skills": []},
        {"install_id": ENFORCE, "name": "probe", "dir": str(probe), "options": {},
         "disabled_skills": ["check", "not a skill;rm"]},
    ])
    monkeypatch.setenv(claude_code.PLUGIN_LOAD_ENV, str(path))
    return path


# --- the runner --------------------------------------------------------------


def test_the_runner_reads_the_variable_the_worker_sets():
    assert claude_code.PLUGIN_LOAD_ENV == pluginload.LOAD_ENV


def test_every_managed_plugin_is_passed_as_a_plugin_dir(loaded):
    plugins = claude_code.managed_plugins()
    root = loaded.parent.resolve()
    assert claude_code.plugin_dir_args(plugins) == [
        "--plugin-dir", str(root / "jev-belay"), "--plugin-dir", str(root / "probe")]


def test_no_load_file_is_no_plugin_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(claude_code.PLUGIN_LOAD_ENV, raising=False)
    assert claude_code.plugin_dir_args(claude_code.managed_plugins()) == []
    assert claude_code.managed_plugins({claude_code.PLUGIN_LOAD_ENV: str(tmp_path / "absent.json")}) == []
    settings = _settings(tmp_path)
    assert "pluginConfigs" not in settings


def test_a_folder_outside_the_load_files_own_is_left_out(tmp_path):
    private = tmp_path / "private"
    inside = _plugin(private / "plugins", "probe")
    outside = _plugin(tmp_path / "elsewhere", "stray")
    path = _load_file(private, [
        {"name": "stray", "dir": str(outside), "options": {}, "disabled_skills": []},
        {"name": "probe", "dir": str(private / "plugins" / ".." / ".." / "elsewhere" / "stray"),
         "options": {}, "disabled_skills": []},
        {"name": "Not A Name", "dir": str(inside), "options": {}, "disabled_skills": []},
        {"name": "probe", "dir": str(inside), "options": {}, "disabled_skills": []},
    ])
    assert [p["dir"] for p in claude_code.managed_plugins({claude_code.PLUGIN_LOAD_ENV: str(path)})] == [
        str(inside.resolve())]


def test_settings_reach_the_plugin_through_the_runners_settings_without_a_keychain(tmp_path, loaded):
    settings = _settings(tmp_path)
    # Only a plugin with options gets an entry; a sensitive one is the
    # placeholder the launcher replaces, never a value.
    assert settings["pluginConfigs"] == {
        "jev-belay@inline": {"options": {"threshold": 0.7, "typesafe_api_key": pluginload.PLACEHOLDER}}}
    # A skill the step turned off is denied; a malformed name is dropped.
    assert "Skill(probe:check)" in settings["permissions"]["deny"]
    assert not any("rm" in rule for rule in settings["permissions"]["deny"])


# --- the managed Jev gate replaces the built-in one ---------------------------


@pytest.fixture
def gates(tmp_path, monkeypatch) -> Path:
    launcher = tmp_path / "swarm-jev-gate.mjs"
    launcher.write_text("")
    config = tmp_path / "jev-gates.json"
    config.write_text("{}")
    monkeypatch.setattr(claude_code, "JEV_GATES_LAUNCHER", str(launcher))
    monkeypatch.setenv(claude_code.JEV_GATES_CONFIG_ENV, str(config))
    return config


def _gate_modes(settings: dict[str, Any]) -> list[str]:
    commands = [h["command"] for group in settings["hooks"].get("Stop", []) for h in group["hooks"]]
    commands += [h["command"] for group in settings["hooks"].get("PostToolUse", []) for h in group["hooks"]]
    return [c.rsplit(" ", 1)[1] for c in commands if claude_code.JEV_GATES_LAUNCHER in c]


def _with(tmp_path: Path, monkeypatch, names: list[str], *, adapter: bool = True) -> None:
    private = tmp_path / "private"
    path = _load_file(private, [{"name": n, "dir": str(_plugin(private / "plugins", n)), "options": {},
                                 "disabled_skills": [], "adapter": adapter} for n in names])
    monkeypatch.setenv(claude_code.PLUGIN_LOAD_ENV, str(path))


def test_without_a_managed_jev_plugin_the_built_in_gates_all_run(tmp_path, monkeypatch, gates):
    _with(tmp_path, monkeypatch, ["probe"])
    assert sorted(_gate_modes(_settings(tmp_path))) == ["belay", "enforce-edit", "enforce-stop"]


def test_a_managed_jev_belay_replaces_the_built_in_belay_for_its_step(tmp_path, monkeypatch, gates):
    _with(tmp_path, monkeypatch, ["jev-belay"])
    assert sorted(_gate_modes(_settings(tmp_path))) == ["enforce-edit", "enforce-stop"]


def test_a_managed_jev_enforce_replaces_both_built_in_enforce_hooks(tmp_path, monkeypatch, gates):
    _with(tmp_path, monkeypatch, ["jev-enforce"])
    assert _gate_modes(_settings(tmp_path)) == ["belay"]


def test_both_managed_jev_plugins_leave_no_built_in_gate(tmp_path, monkeypatch, gates):
    _with(tmp_path, monkeypatch, ["jev-belay", "jev-enforce"])
    settings = _settings(tmp_path)
    assert _gate_modes(settings) == []
    assert "Stop" not in settings["hooks"] or all(
        claude_code.JEV_GATES_LAUNCHER not in h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"])


def test_a_keyless_managed_jev_plugin_keeps_the_built_in_gate(tmp_path, monkeypatch, gates):
    # Its adapter got no TypeSafe key, so its hooks fail open: the step keeps
    # the built-in gates rather than running with no Jev gate at all.
    _with(tmp_path, monkeypatch, ["jev-belay", "jev-enforce"], adapter=False)
    assert sorted(_gate_modes(_settings(tmp_path))) == ["belay", "enforce-edit", "enforce-stop"]


def test_a_load_file_without_the_adapter_flag_replaces_no_gate(tmp_path, monkeypatch, gates):
    private = tmp_path / "private"
    path = _load_file(private, [{"name": "jev-belay", "dir": str(_plugin(private / "plugins", "jev-belay")),
                                 "options": {}, "disabled_skills": [], "adapter": "yes"}])
    monkeypatch.setenv(claude_code.PLUGIN_LOAD_ENV, str(path))
    assert sorted(_gate_modes(_settings(tmp_path))) == ["belay", "enforce-edit", "enforce-stop"]


# --- swarm-plugin-exec --------------------------------------------------------

#: The plugin's command: says which variables it was given, a digest of the
#: secret it got, and prints the secret itself, which must not get out.
CHILD = """
import hashlib, json, os, sys
secret = os.environ.get("CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY", "")
print(json.dumps({"names": sorted(os.environ), "digest": hashlib.sha256(secret.encode()).hexdigest(),
                  "rules": os.environ.get("JEV_RULES_FILE"), "data": os.environ.get("CLAUDE_PLUGIN_DATA")}))
print("the secret is " + secret)
sys.stderr.write("stderr " + secret + "\\n")
"""


def _env_file(tmp_path: Path, secret: str) -> tuple[Path, dict[str, Any]]:
    private = tmp_path / "private"
    env_dir = private / "plugin-env"
    env_dir.mkdir(parents=True)
    secret_file = private / "plugin-secrets" / BELAY / "key"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(secret)
    os.chmod(secret_file, 0o600)
    rules = private / "plugin-files" / BELAY / "rules"
    rules.parent.mkdir(parents=True)
    rules.write_text("Never edit generated files.\n")
    document = {
        "install_id": BELAY, "env": {"JEV_BELAY_CHECK": "1"},
        "secrets": {"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": str(secret_file)},
        "files": {"JEV_RULES_FILE": str(rules)},
        "data_dir": str(private / "plugin-data" / BELAY),
        "runs_file": str(private / "plugin-runs" / f"{BELAY}.jsonl"),
        "drop": ["OTHER_PLUGIN_SETTING"],
    }
    (private / "plugin-runs").mkdir()
    path = env_dir / f"{BELAY}.json"
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    return env_dir, document


def _launch(env_dir: Path, args: list[str], extra: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess:
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(env_dir.parent),
               pluginload.ENV_DIR_ENV: str(env_dir), **extra}
    return subprocess.run([sys.executable, "-I", str(LAUNCHER), *args], env=environ, input=stdin,
                          capture_output=True, text=True, timeout=60, check=False)


def test_a_secret_reaches_the_plugins_command_and_never_its_output(tmp_path):
    secret = "ts-" + secrets.token_hex(16)
    agent_token = secrets.token_hex(20)
    env_dir, document = _env_file(tmp_path, secret)
    done = _launch(env_dir, [BELAY, "--", sys.executable, "-c", CHILD], {
        "GITHUB_TOKEN": agent_token, "SWARM_TASK_ID": "t1", "OTHER_PLUGIN_SETTING": "theirs",
        "ANTHROPIC_API_KEY": secrets.token_hex(20), "KEEP_ME": "yes",
    }, stdin=json.dumps({"hook_event_name": "Stop"}))
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.splitlines()[0])
    # The plugin got its own secret, setting, file path and data folder.
    assert report["digest"] == hashlib.sha256(secret.encode()).hexdigest()
    assert "JEV_BELAY_CHECK" in report["names"] and report["rules"] == document["files"]["JEV_RULES_FILE"]
    assert report["data"] == document["data_dir"] and Path(report["data"]).is_dir()
    # Nothing of the agent's credentials, the worker's variables or another
    # plugin's settings; an ordinary variable passes.
    for name in ("GITHUB_TOKEN", "SWARM_TASK_ID", "OTHER_PLUGIN_SETTING", "ANTHROPIC_API_KEY",
                 pluginload.ENV_DIR_ENV):
        assert name not in report["names"]
    assert "KEEP_ME" in report["names"]
    # The secret never gets out on either stream.
    assert secret not in done.stdout and secret not in done.stderr
    assert "the secret is " in done.stdout and "stderr " in done.stderr
    # The record is counts: the event and the exit code, never argv or text.
    runs = Path(document["runs_file"]).read_text()
    (row,) = [json.loads(line) for line in runs.splitlines()]
    assert row["kind"] == "hook" and row["event"] == "Stop" and row["exit"] == 0
    assert stat.S_IMODE(Path(document["runs_file"]).stat().st_mode) == 0o600
    assert secret not in runs and "the secret is" not in runs and CHILD.strip()[:20] not in runs


def test_a_server_run_is_counted_by_its_start_and_exit(tmp_path):
    env_dir, document = _env_file(tmp_path, "ts-" + secrets.token_hex(16))
    done = _launch(env_dir, [BELAY, "--mcp", "probe", "--", sys.executable, "-c", "raise SystemExit(3)"], {})
    assert done.returncode == 3
    rows = [json.loads(line) for line in Path(document["runs_file"]).read_text().splitlines()]
    assert [(r["kind"], r["server"], r["event"]) for r in rows] == [("mcp", "probe", "start"), ("mcp", "probe", "exit")]
    assert rows[1]["exit"] == 3


def test_a_launcher_without_its_plugins_file_runs_nothing(tmp_path):
    env_dir, _ = _env_file(tmp_path, "ts-" + secrets.token_hex(16))
    marker = tmp_path / "ran"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    done = _launch(env_dir, [OTHER, "--", *command], {})
    assert done.returncode == 1 and "nothing was run" in done.stderr and not marker.exists()
    done = _launch(env_dir, ["not-an-id", "--", *command], {})
    assert done.returncode == 1 and not marker.exists()


def test_the_image_installs_the_launcher_and_the_adapter_where_the_worker_names_them():
    text = DOCKERFILE.read_text()
    assert f"swarm-plugin-exec {pluginload.LAUNCHER}" in text
    assert f"jev-adapter.mjs {pluginload.JEV_ADAPTER}" in text
    assert LAUNCHER.read_text().startswith("#!/opt/venv/bin/python -I\n")
    assert os.access(LAUNCHER, os.X_OK)


# --- the Jev adapter ----------------------------------------------------------


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if not found:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the adapter cannot run; it must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


PROBE = """
const refused = await fetch("http://127.0.0.1:9/elsewhere").then(() => false,
  (e) => String(e.message).includes("refused a request that is not to TypeSafe's API"));
console.log(JSON.stringify({
  key: process.env.TYPESAFE_API_KEY || null,
  other: process.env.UNBOUND || null,
  redirect: process.env.JEV_BASE_URL || null,
  config: process.env.SWARM_JEV_ADAPTER_CONFIG || null,
  guarded: refused,
}));
"""


def _adapt(node: str, tmp_path: Path, key: str | None) -> tuple[dict[str, Any], Path]:
    key_file = tmp_path / "key"
    if key is not None:
        key_file.write_text(key)
    counts = tmp_path / "counts.jsonl"
    config = tmp_path / "env.json"
    config.write_text(json.dumps({"install_id": ENFORCE, "adapter": {
        "kind": "typesafe", "key_file": str(key_file), "variables": ["TYPESAFE_API_KEY"],
        "calls_file": str(tmp_path / "calls"), "counts_file": str(counts), "hook": "managed_plugin",
        "redaction_python": sys.executable, "hard_stop_paths": []}}))
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SWARM_JEV_ADAPTER_CONFIG": str(config),
               "JEV_BASE_URL": "http://127.0.0.1:9/elsewhere", "UNBOUND": "kept"}
    done = subprocess.run([node, "--import", ADAPTER.as_uri(), "--input-type=module", "-e", PROBE],
                          env=environ, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), counts


def test_the_adapter_sets_the_key_in_the_bound_variable_and_guards_fetch(node, tmp_path):
    key = "ts-" + secrets.token_hex(16)
    seen, counts = _adapt(node, tmp_path, key)
    assert seen == {"key": key, "other": "kept", "redirect": None, "config": None, "guarded": True}
    (row,) = [json.loads(line) for line in counts.read_text().splitlines()]
    assert row == {"hook": "managed_plugin", "calls": 0, "dropped": 0, "denied": 0, "withheld": 0}
    assert key not in counts.read_text()


def test_without_a_readable_key_the_adapter_sets_nothing(node, tmp_path):
    seen, counts = _adapt(node, tmp_path, None)
    assert seen["key"] is None and seen["guarded"] is False and not counts.exists()


class _Jev(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 -- http.server's spelling
        type(self).seen.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
        out = b'{"answers": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_args: Any) -> None:
        return


SEND = """
const res = await fetch("https://api.typesafe.ai/v1/systemone", { method: "POST", body: JSON.stringify({
  state: { files: [{ path: "secrets/prod.env", content: process.env.STOPPED_TEXT },
                   { path: "src/app.py", content: "ok" }],
           note: "with " + process.env.MY_PASSPHRASE } }) });
await res.json();
"""


def test_the_adapter_filters_hard_stop_content_and_registered_secrets_as_the_baked_gate_does(node, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _Jev)
    _Jev.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        passphrase, stopped = "pp-" + secrets.token_hex(8), "stop-" + secrets.token_hex(8)
        shim = tmp_path / "shim"
        shim.write_text(json.dumps({"shim_url": f"http://127.0.0.1:{server.server_port}/v1/systemone",
                                    "token": secrets.token_hex(16)}))
        counts = tmp_path / "counts.jsonl"
        config = tmp_path / "env.json"
        config.write_text(json.dumps({"install_id": ENFORCE, "adapter": {
            "kind": "typesafe", "key_file": str(shim), "variables": ["TYPESAFE_API_KEY"],
            "calls_file": str(tmp_path / "calls"), "counts_file": str(counts), "hook": "managed_plugin",
            "redaction_python": sys.executable, "secret_env_names": ["MY_PASSPHRASE"],
            "hard_stop_paths": ["secrets/"]}}))
        environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SWARM_JEV_ADAPTER_CONFIG": str(config),
                   "MY_PASSPHRASE": passphrase, "STOPPED_TEXT": stopped}
        done = subprocess.run([node, "--import", ADAPTER.as_uri(), "--input-type=module", "-e", SEND],
                              env=environ, capture_output=True, text=True, timeout=60, check=False)
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr
    (body,) = _Jev.seen
    assert passphrase not in body and "<redacted>" in body
    assert stopped not in body and "secrets/prod.env" in body and '"ok"' in body
    (row,) = [json.loads(line) for line in counts.read_text().splitlines()]
    assert row["calls"] == 1 and row["denied"] == 1


def test_the_baked_gates_and_the_adapter_share_one_definition():
    gate = (ADAPTER.parent / "swarm-jev-gate.mjs").read_text()
    assert "export function adaptProcess(" in gate
    assert gate.count("guardedFetch(") == 2  # its definition and adaptProcess's one call
    assert 'import { adaptProcess, credential } from "./swarm-jev-gate.mjs";' in ADAPTER.read_text()
