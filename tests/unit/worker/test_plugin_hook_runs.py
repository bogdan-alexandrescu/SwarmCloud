"""A managed plugin's hooks run through the launcher, and every run is recorded,
a failed one included (docs/design/managed-plugins.md sections 4.4, 4.5, 16).

THE LIVE FINDING (11 November 2026, the design's end-to-end test): a
claude-code task loaded two platform plugins, one with a Stop hook and one
with a Stop and a PostToolUse hook. The CLI listed both from their pinned
copies, the stream carried "Stop hook error occurred", and the attempt
summary's `plugins` record showed `hooks: {}` for both: the launcher's runs
files held no row, not even for the hook that errored.

The reproduction below installs a small plugin exactly as the worker does
(`pluginload.load`: unpack, `wrap_tree`, the env file, the runs file), takes
the environment the claude-code runner hands the CLI (`claude_code.body`,
the CLI faked) on top of the variables `cliagent.run_cli_agent` builds from
scratch, and runs each wrapped hook command the way the CLI does: a
shell-form command through `/bin/sh -c`, an exec-form one as its argv, the
hook's JSON on stdin, `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PROJECT_DIR` set, in
the agent's working directory. The launcher at `pluginload.LAUNCHER` is a
two-line shim that execs the image's `swarm-plugin-exec` with this
interpreter and `-I`, as its shebang does in the image.

THE CAUSE it found: the runner builds the CLI's environment from scratch and
never passed `SWARM_PLUGIN_ENV_DIR` on, so every launcher found no env file,
ran nothing, exited 1 ("hook error") and appended no row, because the runs
file's path is in the env file it could not read.

MUTATION: drop the `PLUGIN_ENV_DIR_ENV` pass-through in `claude_code.body`
and the reproduction fails; drop the launcher's refusal rows and the
env-file test fails; drop the tail's scrub and the secret test fails.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_tools, lightpanda, pluginload, plugins
from agent_worker.objectstore import LocalObjectStore
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "swarm-plugin-exec"
TENANT = "eng"
PROBE = "p-" + "d" * 12
VERSION = "v-" + "c" * 12
SECRET_VAR = "CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY"

#: The plugin's hook: records the variable names it was given beside its data
#: folder, then exits with the code and writes the stderr its data folder
#: names (the test's lever), echoing its secret setting when told to.
HOOK = """
import json, os, sys
data = os.environ["CLAUDE_PLUGIN_DATA"]
event = json.loads(sys.stdin.read() or "{}")
with open(os.path.join(data, "seen-" + sys.argv[1]), "w") as out:
    json.dump({"names": sorted(os.environ), "event": event.get("hook_event_name"), "cwd": os.getcwd()}, out)
def lever(name, default=""):
    try:
        with open(os.path.join(data, name)) as f:
            return f.read()
    except OSError:
        return default
text = lever("stderr")
if text:
    sys.stderr.write(text.replace("{secret}", os.environ.get(%r, "")))
sys.exit(int(lever("exit", "0")))
""" % SECRET_VAR


def _plugin_tar(tmp_path: Path) -> bytes:
    py = sys.executable
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "probe", "version": "1.0.0", "userConfig": {
            "typesafe_api_key": {"type": "string", "title": "Key", "sensitive": True}}}),
        "hooks/hooks.json": json.dumps({"hooks": {
            # Shell form, as both live plugins write their Stop hook.
            "Stop": [{"hooks": [{"type": "command",
                                 "command": f'"{py}" "${{CLAUDE_PLUGIN_ROOT}}/hook.py" stop', "timeout": 25}]}],
            # Exec form.
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
                {"type": "command", "command": py, "args": ["${CLAUDE_PLUGIN_ROOT}/hook.py", "edit"]}]}],
        }}),
        "hook.py": HOOK,
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            data = files[name].encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class Installed:
    """One installed probe plugin and the CLI's environment for its hooks."""

    def __init__(self, tmp_path: Path, started: plugins.Started, cli_env: dict[str, str], work: Path,
                 secret: str) -> None:
        self.tmp_path = tmp_path
        self.started = started
        (self.plugin,) = started.managed
        self.cli_env = cli_env
        self.work = work
        self.secret = secret
        self.data = tmp_path / "private" / "plugin-data" / PROBE

    def hooks(self) -> dict[str, Any]:
        return json.loads((self.plugin.directory / "hooks" / "hooks.json").read_text())["hooks"]

    def lever(self, *, exit_code: int = 0, stderr: str = "") -> None:
        (self.data / "exit").write_text(str(exit_code))
        (self.data / "stderr").write_text(stderr)

    def run(self, event: str, *, tool: str | None = None, env: dict[str, str] | None = None
            ) -> subprocess.CompletedProcess:
        """The hook of `event`, run the way the CLI runs it."""
        group = self.hooks()[event][0]
        (hook,) = group["hooks"]
        root = str(self.plugin.directory)
        environ = dict(self.cli_env if env is None else env)
        environ.update({"CLAUDE_PLUGIN_ROOT": root, "CLAUDE_PROJECT_DIR": str(self.work)})
        payload: dict[str, Any] = {"session_id": "s1", "transcript_path": str(self.work / "t.jsonl"),
                                   "cwd": str(self.work), "hook_event_name": event}
        if tool:
            payload.update({"tool_name": tool, "tool_input": {"file_path": str(self.work / "a.txt")}})
        if event == "Stop":
            payload["stop_hook_active"] = False
        if isinstance(hook.get("args"), list):
            argv = [hook["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in hook["args"])]
        else:
            argv = ["/bin/sh", "-c", hook["command"]]
        return subprocess.run(argv, env=environ, cwd=self.work, input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60, check=False)

    def rows(self) -> list[dict[str, Any]]:
        path = self.plugin.runs_file
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def record(self) -> dict[str, Any]:
        stream = self.tmp_path / "stream.jsonl"
        stream.write_text("")
        return plugins.record(self.started, stream)[PROBE]


def _runner_env(tmp_path: Path, monkeypatch, plugin_env: dict[str, str]) -> dict[str, str]:
    """What `claude_code.body` hands `run_cli_agent` as `extra_env` when the
    worker set the plugins' variables on the runner (`lifecycle`:
    `base.update(self._plugins.env)`)."""
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.GIT_SPOOL_ENV,
                 claude_code.SEARCH_SPOOL_ENV, claude_code.STATE_SPOOL_ENV,
                 claude_code.MCP_RELAY_ENV, claude_code.CDP_ENDPOINT_ENV,
                 claude_code.CHILDREN_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in plugin_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secrets.token_hex(16))
    monkeypatch.setenv(claude_code.CACHE_HOME_ENV, str(tmp_path / "cache"))
    monkeypatch.setenv(lightpanda.PROFILE_ENV, "claude-code")
    monkeypatch.setattr(browser_tools, "write_state", lambda *a, **k: None)
    seen: dict[str, Any] = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    work = tmp_path / "work"
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    claude_code.body(RunnerContext(
        work_dir=work, artifacts_dir=tmp_path / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Write one file, then edit it."},
    ))
    return seen


@pytest.fixture
def installed(tmp_path, monkeypatch) -> Installed:
    shim = tmp_path / "bin" / "swarm-plugin-exec"
    shim.parent.mkdir()
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{LAUNCHER}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setattr(pluginload, "LAUNCHER", str(shim))
    store = LocalObjectStore(tmp_path / "bucket")
    data = _plugin_tar(tmp_path)
    digest = hashlib.sha256(data).hexdigest()
    entry = {"name": "probe", "scope": "platform", "version_id": VERSION, "runnable_sha256": digest,
             "object": f"platform/{digest}.tar", "env": {"PROBE_CHECK": "1"}, "files": {},
             "secrets": {SECRET_VAR: {"field": "typesafe_api_key", "secret": "plugin"}},
             "skills": None, "disabled_skills": []}
    store.upload_bytes(entry["object"], data)
    task = {"id": "t1", "tenant_id": TENANT, "metadata": {"dispatch": {"plugins": {PROBE: entry}}},
            "spec_signature": secrets.token_hex(32), "spec_key_version": "spec-key/1"}
    work = tmp_path / "work"
    work.mkdir()
    secret = "ts-" + secrets.token_hex(16)
    started = plugins.start(task, work=work, checkout=None, profile="claude-code", image="img", store=store,
                            environ={}, released={(PROBE, SECRET_VAR): secret})
    seen = _runner_env(tmp_path, monkeypatch, started.env)
    assert seen["args"][seen["args"].index(claude_code.PLUGIN_DIR_FLAG) + 1] == str(started.managed[0].directory)
    # `cliagent.run_cli_agent`'s from-scratch environment, then the runner's
    # `extra_env` for names it does not set.
    cli_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "LANG": "C.UTF-8",
               "TERM": "dumb", "CI": "1", "NO_COLOR": "1", "SWARM_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
               "SWARM_WORK_DIR": str(work), "CLAUDE_CODE_OAUTH_TOKEN": secrets.token_hex(16)}
    for name, value in seen["env"].items():
        cli_env.setdefault(name, value)
    return Installed(tmp_path, started, cli_env, work, secret)


# --- the reproduction ----------------------------------------------------------


def test_the_wrapped_hooks_find_everything_the_launcher_reads(installed):
    """Each step the live run could have failed at, checked in order."""
    hooks = installed.hooks()
    stop, = hooks["Stop"][0]["hooks"]
    edit, = hooks["PostToolUse"][0]["hooks"]
    # The rewrite: the launcher first, the install id, `--`, the original
    # command with `${CLAUDE_PLUGIN_ROOT}` left for the shell / the CLI.
    assert stop["command"].startswith(f"{pluginload.LAUNCHER} {PROBE} -- ")
    assert '"${CLAUDE_PLUGIN_ROOT}/hook.py" stop' in stop["command"]
    assert edit["command"] == pluginload.LAUNCHER and edit["args"][:2] == [PROBE, "--"]
    # The launcher's interpreter: the image's venv python, isolated.
    assert LAUNCHER.read_text().startswith("#!/opt/venv/bin/python -I\n")
    # The env file the worker wrote, private to the worker's user.
    env_dir = Path(installed.started.env[pluginload.ENV_DIR_ENV])
    env_file = env_dir / f"{PROBE}.json"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # The runs file's folder exists and is writable.
    assert installed.plugin.runs_file.parent.is_dir() and os.access(installed.plugin.runs_file.parent, os.W_OK)
    # THE CAUSE: the launcher finds the env file only through this variable,
    # and the CLI's environment is built from scratch by the runner.
    assert installed.cli_env.get(pluginload.ENV_DIR_ENV) == str(env_dir), (
        "the claude-code runner does not pass SWARM_PLUGIN_ENV_DIR to the CLI, so every wrapped hook's "
        "launcher finds no env file, runs nothing and exits 1")


def test_a_wrapped_stop_hook_and_a_wrapped_post_tool_use_hook_each_append_one_row(installed):
    stop = installed.run("Stop")
    assert stop.returncode == 0, stop.stderr
    edit = installed.run("PostToolUse", tool="Write")
    assert edit.returncode == 0, edit.stderr
    rows = installed.rows()
    assert [(r["kind"], r["event"], r.get("tool"), r["exit"]) for r in rows] == [
        ("hook", "Stop", None, 0), ("hook", "PostToolUse", "Write", 0)]
    # The hooks ran, in the agent's directory, with their own setting and
    # none of the worker's, the runner's or the CLI's credentials.
    for name in ("stop", "edit"):
        seen = json.loads((installed.data / f"seen-{name}").read_text())
        assert seen["cwd"] == str(installed.work)
        assert "PROBE_CHECK" in seen["names"] and SECRET_VAR in seen["names"]
        assert not [n for n in seen["names"] if n.startswith("SWARM_")]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["names"]
    assert installed.record()["hooks"] == {"PostToolUse": {"runs": 1, "blocks": 0, "errors": 0},
                                           "Stop": {"runs": 1, "blocks": 0, "errors": 0}}


def test_a_hook_exiting_2_counts_as_a_block(installed):
    installed.lever(exit_code=2, stderr="not done yet: the file has no test\n")
    done = installed.run("Stop")
    assert done.returncode == 2 and "not done yet" in done.stderr
    (row,) = installed.rows()
    assert row["event"] == "Stop" and row["exit"] == 2 and "reason" not in row
    record = installed.record()
    assert record["hooks"] == {"Stop": {"runs": 1, "blocks": 1, "errors": 0}}
    # A block is the hook's answer, not a failure: no error tail.
    assert "last_error" not in json.dumps(record)


# --- a launcher that refuses still appends its row ----------------------------------


def test_a_launcher_that_cannot_read_its_env_file_appends_a_row_and_counts_an_error(installed):
    # The live run's environment: no SWARM_PLUGIN_ENV_DIR.
    env = {k: v for k, v in installed.cli_env.items() if k != pluginload.ENV_DIR_ENV}
    marker = installed.data / "seen-stop"
    done = installed.run("Stop", env=env)
    assert done.returncode == 1 and "nothing was run" in done.stderr and not marker.exists()
    edit = installed.run("PostToolUse", tool="Edit", env={**env, pluginload.ENV_DIR_ENV: str(installed.tmp_path)})
    assert edit.returncode == 1
    rows = installed.rows()
    assert rows == [{"kind": "hook", "event": "Stop", "exit": 1, "reason": "env_unreadable"},
                    {"kind": "hook", "event": "PostToolUse", "tool": "Edit", "exit": 1,
                     "reason": "env_unreadable"}]
    record = installed.record()
    assert record["hooks"]["Stop"] == {"runs": 1, "blocks": 0, "errors": 1,
                                       "last_error": {"exit": 1, "reason": "env_unreadable"}}
    assert record["hooks"]["PostToolUse"]["errors"] == 1


def test_a_bad_install_id_and_a_missing_command_append_their_reason(installed):
    hook = installed.hooks()["Stop"][0]["hooks"][0]
    bad = dict(hook, command=hook["command"].replace(f" {PROBE} ", " not-an-id ", 1))
    missing = dict(hook, command=f"{pluginload.LAUNCHER} {PROBE} -- {installed.tmp_path}/no-such-command")
    for changed in (bad, missing):
        environ = {**installed.cli_env, "CLAUDE_PLUGIN_ROOT": str(installed.plugin.directory)}
        subprocess.run(["/bin/sh", "-c", changed["command"]], env=environ, cwd=installed.work,
                       input=json.dumps({"hook_event_name": "Stop"}), capture_output=True, text=True,
                       timeout=60, check=False)
    rows = installed.rows()
    assert [(r["event"], r["exit"], r["reason"]) for r in rows] == [
        ("Stop", 1, "bad_install_id"), ("Stop", 127, "command_missing")]
    assert installed.record()["hooks"]["Stop"]["errors"] == 2


# --- the error tail ---------------------------------------------------------------


def test_a_failing_hooks_error_tail_is_kept_scrubbed_of_its_secret(installed):
    shaped = "ghp_" + secrets.token_hex(18)
    installed.lever(exit_code=1, stderr="x" * 900 + "\nhook failed: key {secret} rejected; token " + shaped + "\n")
    done = installed.run("Stop")
    assert done.returncode == 1
    assert installed.secret not in done.stderr
    (row,) = installed.rows()
    assert row == {"kind": "hook", "event": "Stop", "exit": 1, "ms": row["ms"]}
    last = installed.record()["hooks"]["Stop"]["last_error"]
    assert last["exit"] == 1 and "hook failed: key" in last["tail"] and "rejected" in last["tail"]
    assert len(last["tail"].encode()) <= 512
    # Nothing credential-shaped is recorded: not the registered secret, not a
    # token-shaped string the hook printed, anywhere under the plugin's runs.
    runs_dir = installed.plugin.runs_file.parent
    for path in runs_dir.iterdir():
        text = path.read_text()
        assert installed.secret not in text and shaped not in text, path.name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert installed.secret not in json.dumps(installed.record()) and shaped not in json.dumps(installed.record())


def test_a_hook_that_succeeds_after_a_failure_keeps_the_last_error(installed):
    installed.lever(exit_code=3, stderr="first failure\n")
    installed.run("Stop")
    installed.lever(exit_code=0)
    installed.run("Stop")
    stop = installed.record()["hooks"]["Stop"]
    assert stop["runs"] == 2 and stop["errors"] == 1
    assert stop["last_error"] == {"exit": 3, "tail": "first failure\n"}


def test_the_launcher_restates_the_runs_folder_the_worker_names():
    assert f'RUNS_DIR = "{pluginload.RUNS_DIR}"' in LAUNCHER.read_text()
    assert f'ERRORS_SUFFIX = "{pluginload.ERRORS_SUFFIX}"' in LAUNCHER.read_text()
    assert claude_code.PLUGIN_ENV_DIR_ENV == pluginload.ENV_DIR_ENV
