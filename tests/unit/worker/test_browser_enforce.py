"""BROWSER-ENFORCE (owner decisions 2026-10-13; part of #1030).

task_7cda0d5335f049b4b442: on `claude-code-browser` the agent launched its own
hidden headless Chromium with Playwright, so the person watching the live
browser saw nothing of its work. This lane makes the live browser the only
browser that works for the agent, gives the agent native tools already
connected to it, and slims the image (docs/browser-startup-speed.md §2.3, §9):

  * the agent's `PLAYWRIGHT_BROWSERS_PATH` is a directory of stubs, so any
    `chromium.launch()` / `launch_persistent_context()` fails fast with a
    message naming `connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])`; the
    gate and the `browser` runner keep /opt/playwright;
  * the claude-code runner registers the Playwright MCP server with
    `--cdp-endpoint $SWARM_CDP_ENDPOINT`, only when the live browser exists;
  * `from swarm_browser import live` returns (playwright, browser, context)
    connected over CDP;
  * the agent's PATH puts /opt/venv/bin first on the browser image, so its
    `python` is the one with Playwright and the helper;
  * the image installs no headless shell (`--no-shell`) and no second Node.

MUTATION: drop `--no-shell` from the Dockerfile, or the `--mcp-config` from
claude_code.body, or the stub path from the agent's env, and the named test
below fails; the image's own build-time self-test (selftest.py, run by the
Dockerfile) is what proves the stub's message reaches a real launch error.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-browser"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
STUB = IMAGE_DIR / "live-browser-only"
SELFTEST = IMAGE_DIR / "selftest.py"
INSTALL_STUB = IMAGE_DIR / "install-browser-stub.py"
HELPER = IMAGE_DIR / "swarm_browser.py"
SPEED_DOC = REPO / "docs" / "browser-startup-speed.md"
DESIGN_DOC = REPO / "docs" / "design" / "live-browser.md"

MESSAGE = (
    "This agent's browser is the live browser the person watches: connect with "
    "chromium.connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])"
)
ENDPOINT = "http://127.0.0.1:9222"


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions, continuations joined, comments and heredoc bodies dropped."""
    out: list[str] = []
    current = ""
    heredoc = None
    for raw in text.splitlines():
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if raw.lstrip().startswith("#") or (not raw.strip() and not current):
            continue
        current += raw.rstrip("\\").strip() + " "
        if raw.rstrip().endswith("\\"):
            continue
        out.append(current.strip())
        marker = re.search(r"<<-?['\"]?([A-Za-z_]\w*)['\"]?", current)
        if marker:
            heredoc = marker.group(1)
        current = ""
    return out


def _dockerfile() -> list[str]:
    return _instructions(DOCKERFILE.read_text(encoding="utf-8"))


# --- the image -----------------------------------------------------------------


def test_the_image_installs_chromium_without_the_headless_shell():
    installs = [s for s in _dockerfile() if "playwright install" in s]
    assert len(installs) == 1, installs
    assert re.search(r"playwright install --with-deps --no-shell chromium\b", installs[0]), installs[0]


def test_playwrights_bundled_node_is_replaced_by_the_images_node():
    # The wheel's own 121 MB `driver/node` is deleted in the SAME RUN that
    # installs it (a later RUN would only hide it under a whiteout), and the
    # driver runs on the base's Node 24 through a link at the same name, so no
    # process needs PLAYWRIGHT_NODEJS_PATH -- the runner's environment is built
    # by exact name and would not carry it.
    run = next(s for s in _dockerfile() if "playwright install" in s)
    assert 'ln -s /usr/local/bin/node "${driver}/node"' in run, run
    assert run.index("playwright install") < run.index('rm -f "${driver}/node"') < run.index("ln -s")


def test_the_stub_is_installed_where_the_runner_points_the_agent():
    from agent_worker.runners.claude_code import AGENT_BROWSERS_PATH

    steps = " ".join(_dockerfile())
    assert f"install-browser-stub.py {AGENT_BROWSERS_PATH} " in steps
    assert AGENT_BROWSERS_PATH != "/opt/playwright"
    # The gate's and the `browser` runner's path stays the image's ENV.
    assert re.search(r"^ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright", DOCKERFILE.read_text(), re.M)


def test_the_stub_prints_the_message_and_fails(tmp_path):
    done = subprocess.run(["sh", str(STUB), "--headless", "--remote-debugging-pipe"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode != 0
    assert MESSAGE in done.stderr
    assert "from swarm_browser import live" in done.stderr


def test_playwright_mcp_is_pinned_to_an_exact_version():
    from agent_worker.runners.claude_code import PLAYWRIGHT_MCP_BIN

    text = DOCKERFILE.read_text(encoding="utf-8")
    pin = re.search(r"^ARG PLAYWRIGHT_MCP_VERSION=(\S+)$", text, re.M)
    assert pin and re.fullmatch(r"\d+\.\d+\.\d+", pin.group(1)), pin
    steps = " ".join(_dockerfile())
    assert 'npm install -g --no-audit --no-fund "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"' in steps
    assert PLAYWRIGHT_MCP_BIN == "/usr/local/share/npm-global/bin/playwright-mcp"


def test_the_helper_is_on_the_agents_python_path():
    steps = _dockerfile()
    assert any(s.startswith("COPY ") and "images/agent-runtime-browser/swarm_browser.py" in s
               and s.endswith("/opt/venv/lib/python3.11/site-packages/swarm_browser.py")
               for s in steps), steps


def test_the_self_tests_run_once_as_the_agent_user_after_everything_is_installed():
    steps = _dockerfile()
    runs = [i for i, s in enumerate(steps) if s.startswith("RUN ") and "selftest.py" in s]
    assert len(runs) == 1, runs
    users = [i for i, s in enumerate(steps) if s.startswith("USER ")]
    assert users and users[-1] < runs[0] and steps[users[-1]] == "USER swarm:swarm"
    # The self-test is the image's last RUN: nothing installed after it is untested.
    assert runs[0] == max(i for i, s in enumerate(steps) if s.startswith("RUN "))
    # Every check is in that one RUN: two check-only RUNs can write the same
    # layer, and GKE does not stream an image with a duplicate (PR 1239).
    assert not any("python - <<" in s for s in steps if s.startswith("RUN "))


def test_the_self_test_proves_every_launch_fails_with_the_message():
    text = SELFTEST.read_text(encoding="utf-8")
    for call in ("launch(headless=True", "launch(headless=False", 'channel="chromium"',
                 "launch_persistent_context("):
        assert call in text, call
    assert "connect_over_cdp" in text and "from swarm_browser import live" in text
    assert "find_chromium" in text  # the gate's own binary is still there
    assert "chromium_headless_shell" in text  # and the shell is not
    assert "--cdp-endpoint" in text  # the MCP server takes the flag the runner passes


# --- the runner ----------------------------------------------------------------


def _ctx(tmp_path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Check the settings page renders."},
    )


def _run_body(monkeypatch, tmp_path, *, browsers: str | None, endpoint: str | None) -> dict[str, Any]:
    from agent_worker.runners import claude_code

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        seen["spec"] = spec
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    # The runner git tools register an MCP server of their own when the
    # attempt has `SWARM_GIT`, which a SwarmCloud container running these
    # tests does: these cases are about the browser tools alone.
    monkeypatch.delenv(claude_code.GIT_SPOOL_ENV, raising=False)
    # Likewise `SWARM_HANDOFF`, which a SwarmCloud container on the browser
    # runner carries: with it set, `live_browser_refusal` takes every case for
    # a live-browser attempt and refuses the ones that model an image with no
    # live browser. These cases name their browser and endpoint themselves.
    monkeypatch.delenv(claude_code.HANDOFF_ENV, raising=False)
    # And the image's browsers directory is fake here, except on a runner
    # where `/opt/playwright` really exists: there PW-TESTRUNNER's directory
    # is prepared and its note follows the browser note these cases read.
    # That note is not what these cases are about.
    monkeypatch.setattr(claude_code, "prepare_test_browsers", lambda *_a, **_k: None)
    for name, value in ((claude_code.BROWSERS_PATH_ENV, browsers), ("SWARM_CDP_ENDPOINT", endpoint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    claude_code.body(_ctx(tmp_path))
    return seen


def _mcp_config(seen: dict[str, Any]) -> dict[str, Any] | None:
    args = seen["args"]
    if "--mcp-config" not in args:
        return None
    return json.loads(Path(args[args.index("--mcp-config") + 1]).read_text(encoding="utf-8"))


def test_the_runner_registers_playwright_mcp_on_the_live_browser(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=ENDPOINT)
    config = _mcp_config(seen)
    assert config == {
        "mcpServers": {
            "playwright": {
                "type": "stdio",
                "command": claude_code.PLAYWRIGHT_MCP_BIN,
                # The client tag (BSTATUS-API): the gate attributes and strips it.
                # QA-KIT: auto-named screenshots land in the task's artifacts.
                "args": ["--cdp-endpoint", ENDPOINT + "/client/playwright",
                         "--output-dir", str(tmp_path / "artifacts" / "screenshots")],
            }
        }
    }
    # Written beside the settings file in work/, never in the checkout.
    path = Path(seen["args"][seen["args"].index("--mcp-config") + 1])
    assert path.parent == tmp_path / claude_code.SETTINGS_DIR_NAME
    assert path.stat().st_mode & 0o077 == 0


def test_the_agents_own_launch_finds_only_the_stub(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=ENDPOINT)
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == claude_code.AGENT_BROWSERS_PATH
    assert seen["env"]["SWARM_CDP_ENDPOINT"] == ENDPOINT
    assert claude_code.LIVE_BROWSER_NOTE in seen["prompt"]
    note = claude_code.LIVE_BROWSER_NOTE
    assert "from swarm_browser import live" in note and "browser_navigate" in note
    assert "headless=True" not in note


def test_a_blocked_site_is_handed_to_a_person_not_swapped():
    """task_9d2890cce74147f985ff (2026-10-13): Google answered a CAPTCHA and the
    agent switched to Bing without asking, because the BROWSER-BRIEF hand-off
    rule was lost when BROWSER-ENFORCE replaced the note."""
    from agent_worker.runners import claude_code

    note = claude_code.LIVE_BROWSER_NOTE
    assert "CAPTCHA" in note and "sign-in" in note
    assert "swarm-handoff request" in note and "swarm-handoff wait" in note
    assert "--expect" in note
    assert "do not switch to a different site" in note


def test_the_note_says_wait_and_do_not_poll_the_browser():
    """HANDOFF-POLICY (part of #1030), task_2ac5d7e22a51460995a2: after
    `swarm-handoff wait` the agent ran `curl /json/version` in a loop."""
    from agent_worker.runners import claude_code

    note = claude_code.LIVE_BROWSER_NOTE
    assert "wait, do not poll the browser" in note
    assert "exit 75" in note


def test_the_agents_python_is_the_venv_with_playwright(monkeypatch, tmp_path):

    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=ENDPOINT)
    assert seen["spec"].path_prefix == ("/opt/venv/bin",)
    seen = _run_body(monkeypatch, tmp_path, browsers=None, endpoint=None)
    assert seen["spec"].path_prefix == ()


def test_the_cli_runner_puts_the_prefix_first_on_path(monkeypatch, tmp_path):
    import dataclasses

    from agent_worker.runners import claude_code, cliagent

    launched: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_child(argv, *, env, **kwargs):
        launched.update(env)
        raise Stop

    monkeypatch.setattr(cliagent, "run_child", fake_child)
    # claude-code reads its stream as it is written, for the context ceiling
    # (#2524), so its start goes through the watched loop.
    monkeypatch.setattr(cliagent, "_run_watched", fake_child)
    binary = tmp_path / "claude"
    binary.write_text("")
    monkeypatch.setenv(claude_code.SPEC.binary_env, str(binary))
    monkeypatch.setenv(claude_code.SPEC.key_env, "sk-ant-" + "x" * 40)
    (tmp_path / "artifacts").mkdir()
    spec = dataclasses.replace(claude_code.SPEC, path_prefix=("/opt/venv/bin",))
    with pytest.raises(Stop):
        cliagent.run_cli_agent(_ctx(tmp_path), spec)
    assert launched["PATH"] == "/opt/venv/bin:" + cliagent.BASE_PATH


def test_no_live_browser_means_no_mcp_and_the_real_browsers(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    # The browser image without a live sidecar: the old headless behaviour.
    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=None)
    assert _mcp_config(seen) is None
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/playwright"
    assert seen["prompt"].endswith(claude_code.BROWSER_NOTE)


def test_the_base_image_gets_no_browser_tools(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, browsers=None, endpoint=ENDPOINT)
    assert _mcp_config(seen) is None
    assert "PLAYWRIGHT_BROWSERS_PATH" not in seen["env"]


# --- the `browser` runner on an image with no headless shell -------------------


def test_the_browser_runner_launches_full_chromium_headless():
    from agent_worker.runners import browser

    calls: list[dict[str, Any]] = []
    fake = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kw: calls.append(kw)))
    browser._launch(fake, {})
    assert calls[0]["headless"] is True and calls[0]["channel"] == "chromium"


# --- the helper ----------------------------------------------------------------


def _load_helper(monkeypatch, connect):
    """swarm_browser with a fake `playwright.sync_api`: unit tests have no Chromium."""
    stopped: list[bool] = []

    class FakePlaywright:
        def __init__(self):
            self.chromium = types.SimpleNamespace(connect_over_cdp=connect)

        def stop(self):
            stopped.append(True)

    class Starter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Starter  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    spec = importlib.util.spec_from_file_location("swarm_browser_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, stopped


def test_the_helper_connects_to_the_endpoint_and_returns_its_context(monkeypatch):
    asked: list[tuple[str, dict]] = []
    context = object()

    def connect(endpoint, **kw):
        asked.append((endpoint, kw))
        return types.SimpleNamespace(contexts=[context])

    helper, stopped = _load_helper(monkeypatch, connect)
    monkeypatch.setenv("SWARM_CDP_ENDPOINT", ENDPOINT)
    p, browser, ctx = helper.live()
    assert asked[0][0] == ENDPOINT and ctx is context and browser.contexts == [context]
    assert p.chromium.connect_over_cdp is connect and not stopped


def test_the_helper_names_the_variable_when_there_is_no_live_browser(monkeypatch):
    helper, _ = _load_helper(monkeypatch, lambda *a, **k: None)
    monkeypatch.delenv("SWARM_CDP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="SWARM_CDP_ENDPOINT"):
        helper.live()


def test_the_helper_stops_playwright_when_the_connection_fails(monkeypatch):
    def connect(endpoint, **kw):
        raise ConnectionError("refused")

    helper, stopped = _load_helper(monkeypatch, connect)
    with pytest.raises(ConnectionError):
        helper.live(ENDPOINT)
    assert stopped == [True]


# --- the docs ------------------------------------------------------------------


def test_the_speed_doc_records_the_slimmed_image():
    text = SPEED_DOC.read_text(encoding="utf-8")
    section = text[text.index("### 2.3"):text.index("### 2.4")]
    assert "BROWSER-ENFORCE" in section
    assert "--no-shell" in section and "codex" in section


def test_the_design_says_the_agents_own_launch_is_refused():
    text = DESIGN_DOC.read_text(encoding="utf-8")
    section = text[text.index("### 3.2"):text.index("### 3.3")]
    assert "cannot be prevented" not in section
    assert "live-browser-only" in section and "--cdp-endpoint" in section
