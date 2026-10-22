"""JEV-RUNNER (owner decision 2026-10-14; part of #1030).

A live-browser task gets jev-browser's MCP tools beside Playwright MCP and
dev-browser when its submitter or tenant has a TypeSafe key, and the key
reaches ONLY the jev server's process:

  * the worker resolves the key (`agent_worker.jevkey`, swarm-api's order:
    the submitter's confirmed personal key, else the tenant's), writes it to a
    0600 file in private/ and hands the runner the PATH;
  * the claude-code runner registers `jev` only with the live browser AND a
    key file AND the binary in the image, through `jev_launch.py`, which puts
    the key in the server's environment and nowhere else.

Every fake key is built at runtime. MUTATION: drop the `endpoint and` from
`write_mcp_config`'s jev guard, or the `os.access` check in `_jev_key_file`,
or the `principal == email` check in `confirmed_personal_tenant`, and a named
test below fails.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO / "docs" / "design" / "live-browser.md"
LIFECYCLE = REPO / "apps" / "agent-worker" / "agent_worker" / "lifecycle.py"
ENDPOINT = "http://127.0.0.1:9222"


def _fake_key() -> str:
    return "ts" + "-" + secrets.token_hex(24)


# --- fakes ---------------------------------------------------------------------


class _Snap:
    def __init__(self, data: dict | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, db: "_Db", path: str) -> None:
        self._db, self._path = db, path

    def get(self, **_kw: Any) -> _Snap:
        return _Snap(self._db.docs.get(self._path))


class _Coll:
    def __init__(self, db: "_Db", name: str) -> None:
        self._db, self._name = db, name

    def document(self, doc_id: str) -> _Doc:
        return _Doc(self._db, f"{self._name}/{doc_id}")


class _Db:
    def __init__(self, docs: dict[str, dict]) -> None:
        self.docs = docs

    def collection(self, name: str) -> _Coll:
        return _Coll(self, name)


class _Secrets:
    def __init__(self, values: dict[str, str], denied: tuple[str, ...] = ()) -> None:
        self.values, self.denied, self.reads = values, denied, []

    def access(self, name: str) -> str:
        self.reads.append(name)
        if name in self.denied:
            raise PermissionError("403")
        return self.values[name]


def _logger():
    from agent_worker.logs import StructuredLogger

    stream = io.StringIO()
    return StructuredLogger(stream=stream, component="test"), stream


def _world(*, user_key: str | None, tenant_key: str | None, principal: str = "bogdan@example.com"):
    docs: dict[str, dict] = {
        "tenants/u-bogdan": {"tenant_id": "u-bogdan", "kind": "user", "principal": principal},
    }
    values = {}
    if user_key is not None:
        docs["provider_keys/swarm-tenant-u-bogdan-typesafe"] = {"set": True}
        values["swarm-tenant-u-bogdan-typesafe"] = user_key
    if tenant_key is not None:
        docs["provider_keys/swarm-tenant-eng-typesafe"] = {"set": True}
        values["swarm-tenant-eng-typesafe"] = tenant_key
    return _Db(docs), values


def _prepare(tmp_path, db, client, *, endpoint=ENDPOINT, submitted_by="bogdan@example.com"):
    from agent_worker import jevkey

    log, stream = _logger()
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    env = jevkey.prepare(endpoint=endpoint, db=db, client=client, tenant_id="eng",
                         submitted_by=submitted_by, private_dir=private, logger=log)
    return env, stream.getvalue()


# --- which key ------------------------------------------------------------------


def test_the_order_is_swarm_apis_over_every_combination():
    from agent_worker import jevkey
    from swarm_api.provider_keys import resolve_provider_key

    user, tenant = "swarm-tenant-u-bogdan-typesafe", "swarm-tenant-eng-typesafe"
    seen = 0
    for personal, has_user, has_tenant in itertools.product(("u-bogdan", None), (0, 1), (0, 1)):
        present = {s for s, on in ((user, has_user), (tenant, has_tenant)) if on}
        for task_tenant in ("eng", "u-bogdan"):
            mine = jevkey.candidates(task_tenant, personal, present)
            theirs = resolve_provider_key(task_tenant, personal, "typesafe", present)
            assert (mine[0][1] if mine else None) == theirs, (task_tenant, personal, present)
            seen += 1
    assert seen == 16


def test_the_submitters_own_key_wins(tmp_path):
    user_key, tenant_key = _fake_key(), _fake_key()
    db, values = _world(user_key=user_key, tenant_key=tenant_key)
    env, logs = _prepare(tmp_path, db, _Secrets(values))
    path = Path(env["SWARM_JEV_KEY_FILE"])
    assert path.read_text() == user_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent.name == "private"  # never checkpointed, never uploaded
    assert '"scope": "user"' in logs


def test_a_namesakes_personal_tenant_is_not_the_submitters(tmp_path):
    user_key, tenant_key = _fake_key(), _fake_key()
    db, values = _world(user_key=user_key, tenant_key=tenant_key, principal="bogdan@partner.com")
    env, _ = _prepare(tmp_path, db, _Secrets(values))
    assert Path(env["SWARM_JEV_KEY_FILE"]).read_text() == tenant_key


def test_an_unreadable_personal_key_falls_to_the_tenant_key_and_says_so(tmp_path):
    user_key, tenant_key = _fake_key(), _fake_key()
    db, values = _world(user_key=user_key, tenant_key=tenant_key)
    env, logs = _prepare(tmp_path, db, _Secrets(values, denied=("swarm-tenant-u-bogdan-typesafe",)))
    assert Path(env["SWARM_JEV_KEY_FILE"]).read_text() == tenant_key
    assert "jev_key_unreadable" in logs and "PermissionError" in logs


def test_no_key_means_no_file_and_no_error(tmp_path):
    db, values = _world(user_key=None, tenant_key=None)
    client = _Secrets(values)
    env, logs = _prepare(tmp_path, db, client)
    assert env == {}
    assert client.reads == []
    assert "jev_key_absent" in logs
    assert not list((tmp_path / "private").iterdir())


def test_without_the_live_browser_no_key_is_read(tmp_path):
    db, values = _world(user_key=_fake_key(), tenant_key=_fake_key())
    client = _Secrets(values)
    env, _ = _prepare(tmp_path, db, client, endpoint=None)
    assert env == {} and client.reads == []


def test_a_broken_read_costs_the_jev_tools_not_the_attempt(tmp_path):
    class _Broken(_Db):
        def collection(self, name):
            raise RuntimeError("firestore down")

    env, logs = _prepare(tmp_path, _Broken({}), _Secrets({}))
    assert env == {}
    assert "jev_key_unavailable" in logs and "RuntimeError" in logs


def test_the_key_is_never_logged(tmp_path):
    key = _fake_key()
    db, values = _world(user_key=key, tenant_key=None)
    env, logs = _prepare(tmp_path, db, _Secrets(values))
    assert env and key not in logs
    assert key not in json.dumps(env)


def test_the_lifecycle_hands_the_runner_the_path_from_private():
    text = LIFECYCLE.read_text()
    assert "jevkey_mod.prepare(" in text
    assert "private_dir=ws.private" in text


# --- the runner -----------------------------------------------------------------


def _ctx(tmp_path, browser_tool=None):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Fill in the signup form.",
                 **({"browser_tool": browser_tool} if browser_tool else {})},
    )


@pytest.fixture
def jev_bin(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    server = tmp_path / "bin" / "jev-browser-mcp"
    server.parent.mkdir()
    server.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, sys\n"
        "key = os.environ.get('TYPESAFE_API_KEY', '')\n"
        "print(json.dumps({'argv': sys.argv[1:], 'key_sha': hashlib.sha256(key.encode()).hexdigest(),"
        " 'endpoint': os.environ.get('SWARM_CDP_ENDPOINT'),"
        " 'no_install': os.environ.get('JEV_BROWSER_NO_INSTALL')}))\n"
    )
    server.chmod(0o755)
    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(server))
    return server


def _run_body(monkeypatch, tmp_path, *, endpoint, key_file, backend=None, browser_tool=None, route=True):
    from agent_worker.runners import claude_code

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        if "--mcp-config" in seen["args"]:
            seen["config_text"] = Path(seen["args"][seen["args"].index("--mcp-config") + 1]).read_text()
            seen["config"] = json.loads(seen["config_text"])
        seen["key_file_existed"] = bool(key_file) and Path(key_file).exists()
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    for name, value in (("SWARM_CDP_ENDPOINT", endpoint), ("SWARM_JEV_KEY_FILE", key_file)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # SWARM_STATE too: an agent container exports it, and the runner then
    # registers `swarm-state` beside the browser tools under test.
    for name in ("SWARM_GIT", "SWARM_SEARCH", "SWARM_HANDOFF", "SWARM_STATE", "JEV_API_URL"):
        monkeypatch.delenv(name, raising=False)
    if backend is None:
        monkeypatch.delenv("SWARM_JEV_BACKEND", raising=False)
    else:
        monkeypatch.setenv("SWARM_JEV_BACKEND", backend)
    # What the dispatcher sends an internal backend while the tailnet egress is
    # on (scheduler.judgment.jev_dispatch); `route=False` is the egress off,
    # where the runner registers no jev (JEV-OFF, #1345).
    if backend in ("internal-jev", "internal-laya") and route:
        monkeypatch.setenv("JEV_API_URL", "https://decision-api-staging.taile0bda9.ts.net/"
                                          + backend.split("-", 1)[1] + "/v1/decide")
    claude_code.body(_ctx(tmp_path, browser_tool))
    return seen


def _key_file(tmp_path, key: str) -> str:
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    path = private / "jev-typesafe.key"
    path.write_text(key)
    path.chmod(0o600)
    return str(path)


def test_the_live_browser_with_a_key_registers_jev_beside_playwright(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    key = _fake_key()
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=_key_file(tmp_path, key))
    servers = seen["config"]["mcpServers"]
    assert set(servers) == {claude_code.MCP_SERVER_NAME, "jev"}
    jev = servers["jev"]
    assert jev["command"] == sys.executable
    assert jev["args"][:2] == ["-I", str(claude_code.JEV_LAUNCH)]
    assert jev["args"][-1] == str(jev_bin)
    assert jev["env"] == {"SWARM_CDP_ENDPOINT": ENDPOINT, "JEV_BROWSER_NO_INSTALL": "1"}
    assert seen["prompt"].count(claude_code.BROWSER_TOOL_STRENGTH["jev"]) == 1
    # The key is in no file and no environment the CLI or the agent is given.
    assert key not in seen["config_text"]
    assert key not in json.dumps(seen["env"])
    assert "SWARM_JEV_KEY_FILE" not in seen["env"]
    config = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_the_key_file_is_removed_when_the_agent_has_finished(monkeypatch, tmp_path, jev_bin):
    path = _key_file(tmp_path, _fake_key())
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=path)
    assert seen["key_file_existed"]  # every CLI launch could read it
    assert not Path(path).exists()


def test_no_key_means_no_jev_and_no_word_of_it(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None)
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME}
    assert "jev" not in seen["prompt"] and "TypeSafe" not in seen["prompt"]


def test_a_key_without_the_live_browser_registers_nothing(monkeypatch, tmp_path, jev_bin):
    seen = _run_body(monkeypatch, tmp_path, endpoint=None, key_file=_key_file(tmp_path, _fake_key()))
    assert "config" not in seen
    assert "TypeSafe" not in seen["prompt"]


# --- the backend decides the gate (JUDGE-SELECT) --------------------------------


def test_internal_jev_with_no_key_registers_jev_and_writes_no_key_file(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None, backend="internal-jev")
    servers = seen["config"]["mcpServers"]
    assert set(servers) == {claude_code.MCP_SERVER_NAME, "jev"}
    jev = servers["jev"]
    # The wrapper itself, started directly: no launcher, no key, no env block
    # (it reads the CLI's environment, which carries the backend and URL).
    assert jev == {"type": "stdio", "command": str(jev_bin), "args": []}
    assert "TYPESAFE_API_KEY" not in json.dumps(jev)
    assert seen["prompt"].count(claude_code.BROWSER_TOOL_STRENGTH_JEV_INTERNAL) == 1
    assert claude_code.BROWSER_TOOL_STRENGTH["jev"] not in seen["prompt"]
    assert "TypeSafe" not in seen["prompt"]
    assert not (tmp_path / "private").exists()


def test_internal_laya_registers_jev(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None, backend="internal-laya")
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME, "jev"}


def test_typesafe_with_a_key_registers_jev_with_the_key_file(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    key = _fake_key()
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT,
                     key_file=_key_file(tmp_path, key), backend="typesafe")
    jev = seen["config"]["mcpServers"]["jev"]
    assert jev["command"] == sys.executable
    assert jev["args"][:2] == ["-I", str(claude_code.JEV_LAUNCH)]
    assert key not in seen["config_text"]
    assert seen["prompt"].count(claude_code.BROWSER_TOOL_STRENGTH["jev"]) == 1


def test_typesafe_without_a_key_does_not_register_jev(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None, backend="typesafe")
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME}
    assert "jev" not in seen["prompt"]


def test_an_unset_backend_with_no_key_does_not_register_jev(monkeypatch, tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None, backend=None)
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME}


def test_an_internal_backend_never_reads_or_writes_a_key(tmp_path):
    for backend in ("internal-jev", "internal-laya"):
        db, values = _world(user_key=_fake_key(), tenant_key=_fake_key())
        client = _Secrets(values)
        from agent_worker import jevkey

        log, _ = _logger()
        private = tmp_path / backend
        private.mkdir()
        env = jevkey.prepare(endpoint=ENDPOINT, db=db, client=client, tenant_id="eng",
                             submitted_by="bogdan@example.com", private_dir=private,
                             logger=log, backend=backend)
        assert env == {} and client.reads == [] and not list(private.iterdir())


def test_write_mcp_config_needs_the_endpoint_for_jev(tmp_path):
    from agent_worker.runners import claude_code

    path = claude_code.write_mcp_config(tmp_path / "cfg", None, jev_key_file="/x/key")
    assert "jev" not in json.loads(path.read_text())["mcpServers"]


def test_a_missing_binary_skips_jev_and_logs_jev_unavailable(monkeypatch, tmp_path, capsys):
    from agent_worker.runners import claude_code

    monkeypatch.setattr(claude_code, "JEV_MCP_BIN", str(tmp_path / "absent"))
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=_key_file(tmp_path, _fake_key()))
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME}
    assert "TypeSafe" not in seen["prompt"]
    assert "jev_unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("tool", ["playwright", "dev-browser"])
@pytest.mark.parametrize("backend, with_key", [("internal-jev", False), ("internal-laya", False),
                                                ("typesafe", True)])
def test_browser_tool_playwright_or_dev_browser_never_registers_jev(
        monkeypatch, tmp_path, jev_bin, tool, backend, with_key):
    from agent_worker.runners import claude_code

    key_file = _key_file(tmp_path, _fake_key()) if with_key else None
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=key_file,
                     backend=backend, browser_tool=tool)
    assert set(seen["config"]["mcpServers"]) == {claude_code.MCP_SERVER_NAME}


def test_browser_tool_jev_registers_jev_on_an_internal_backend(monkeypatch, tmp_path, jev_bin):
    seen = _run_body(monkeypatch, tmp_path, endpoint=ENDPOINT, key_file=None,
                     backend="internal-jev", browser_tool="jev")
    assert "jev" in seen["config"]["mcpServers"]


def test_the_internal_strength_line_names_no_typesafe_egress():
    from agent_worker.runners import claude_code

    assert "TypeSafe" not in claude_code.BROWSER_TOOL_STRENGTH_JEV_INTERNAL
    assert "TypeSafe" in claude_code.BROWSER_TOOL_STRENGTH["jev"]


# --- the launcher, against a fake server -----------------------------------------


def test_the_launcher_puts_the_key_in_the_servers_environment_only(tmp_path, jev_bin):
    import hashlib

    from agent_worker.runners import claude_code

    key = _fake_key()
    path = _key_file(tmp_path, key)
    env = {"PATH": os.environ.get("PATH", ""), "SWARM_CDP_ENDPOINT": ENDPOINT,
           "JEV_BROWSER_NO_INSTALL": "1"}
    done = subprocess.run([sys.executable, "-I", str(claude_code.JEV_LAUNCH), path, str(jev_bin)],
                          env=env, capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(done.stdout)
    assert out["key_sha"] == hashlib.sha256(key.encode()).hexdigest()
    assert out["endpoint"] == ENDPOINT and out["no_install"] == "1"
    assert key not in done.stdout and key not in done.stderr
    assert Path(path).exists()  # the next CLI launch reads it again


def test_the_launcher_refuses_a_missing_file_without_starting_the_server(tmp_path, jev_bin):
    from agent_worker.runners import claude_code

    done = subprocess.run([sys.executable, "-I", str(claude_code.JEV_LAUNCH),
                           str(tmp_path / "nope"), str(jev_bin)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 1
    assert "cannot read the TypeSafe key file" in done.stderr
    assert done.stdout == ""


# --- the design doc ---------------------------------------------------------------


def test_the_design_doc_has_the_jev_subsection():
    text = DESIGN_DOC.read_text()
    assert "### 3.2.5 jev-browser" in text
    section = text.split("### 3.2.5 jev-browser", 1)[1].split("### 3.3", 1)[0]
    for phrase in ("api.typesafe.ai", "jev_launch.py", "private/", "jev_unavailable",
                   "typesafe_user_keys.tf", "same uid"):
        assert phrase in section, phrase


def test_the_status_route_names_jev_as_what_reads_the_key():
    from swarm_api.routes.provider_keys import CONSUMERS

    assert CONSUMERS["typesafe"] == ["jev"]
