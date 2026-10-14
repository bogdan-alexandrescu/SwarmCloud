"""DEVB-INSTALL (owner decisions 2026-10-14; part of #1030).

dev-browser v1 (SawyerHood/dev-browser 1.0.0-rc.3) on `claude-code-browser`,
opt-in beside the Playwright MCP server, which stays registered and stays the
default, and attached to the live browser only:

  * the image carries the pinned standalone binary off PATH and a wrapper
    named `dev-browser` on PATH that always passes `--connect
    "$SWARM_CDP_ENDPOINT"` and refuses `install`, `chrome`, `--headless` and
    any other `--connect`;
  * the claude-code runner, only when the live browser exists, puts
    DEV_BROWSER_HOME under `work/`, gives the binary the live-browser-only stub
    as its Chrome, and adds one paragraph to the note.

The wrapper is run here against a fake real binary that prints its argv, so
what it passes through is measured, not read. The image's build-time
self-test (selftest.py `dev_browser`) runs the same refusals against the real
binary in the image build job.

MUTATION: drop the `--connect` from the wrapper's exec, or the `install`
refusal, or the `if endpoint:` guard around the runner's dev-browser variables,
and the named test below fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-browser"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
WRAPPER = IMAGE_DIR / "dev-browser"
SELFTEST = IMAGE_DIR / "selftest.py"
MANIFEST = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"
DESIGN_DOC = REPO / "docs" / "design" / "live-browser.md"

ENDPOINT = "http://127.0.0.1:9222"
STUB_ROOT = "/opt/swarm/live-browser-only"
VERSION = "1.0.0-rc.3"
REFUSAL = "dev-browser here drives only the live browser the person watches"


# --- the runner ----------------------------------------------------------------


def _ctx(tmp_path):
    from agent_worker.runners.base import RunnerContext

    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Compare the two pricing pages."},
    )


def _run_body(monkeypatch, tmp_path, *, browsers: str | None, endpoint: str | None) -> dict[str, Any]:
    from agent_worker.runners import claude_code

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["args"] = list(extra_args)
        seen["env"] = dict(extra_env or {})
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name, value in ((claude_code.BROWSERS_PATH_ENV, browsers), ("SWARM_CDP_ENDPOINT", endpoint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    for name in ("SWARM_GIT", "SWARM_SEARCH", "SWARM_HANDOFF"):
        monkeypatch.delenv(name, raising=False)
    claude_code.body(_ctx(tmp_path))
    return seen


def test_the_live_browser_gets_dev_browser_home_under_work_and_the_note(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=ENDPOINT)
    assert seen["env"]["DEV_BROWSER_HOME"] == str(tmp_path / ".dev-browser")
    assert Path(seen["env"]["DEV_BROWSER_HOME"]).parent == tmp_path  # work/, so it travels with it
    assert seen["env"]["DEV_BROWSER_CHROME"] == "/opt/swarm/dev-browser/live-browser-only"
    assert seen["env"]["DEV_BROWSER_SKIP_DOWNLOAD"] == "1"
    note = claude_code.LIVE_BROWSER_DEV_BROWSER_NOTE
    assert seen["prompt"].count(note) == 1
    assert claude_code.LIVE_BROWSER_NOTE in seen["prompt"]
    assert "dev-browser --help" in note
    assert "same live browser the person watches" in note
    assert "`swarm-handoff wait`" in note and "`dev-browser stop`" in note
    assert "alternative to the browser_* tools" in note


@pytest.mark.parametrize(
    "browsers, endpoint",
    [("/opt/playwright", None), (None, None), (None, ENDPOINT)],
    ids=["browser-image-no-live-browser", "base-image", "base-image-with-endpoint"],
)
def test_without_the_live_browser_there_is_no_word_of_dev_browser(monkeypatch, tmp_path, browsers, endpoint):
    seen = _run_body(monkeypatch, tmp_path, browsers=browsers, endpoint=endpoint)
    for name in ("DEV_BROWSER_HOME", "DEV_BROWSER_CHROME", "DEV_BROWSER_SKIP_DOWNLOAD"):
        assert name not in seen["env"], name
    assert "dev-browser" not in seen["prompt"]


def test_playwright_mcp_stays_registered_beside_dev_browser(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    seen = _run_body(monkeypatch, tmp_path, browsers="/opt/playwright", endpoint=ENDPOINT)
    config = json.loads(Path(seen["args"][seen["args"].index("--mcp-config") + 1]).read_text())
    assert set(config["mcpServers"]) == {claude_code.MCP_SERVER_NAME}


# --- the wrapper, against a fake real binary -----------------------------------


FAKE_REAL = """#!{python}
import json, os, sys
print(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                  "chrome": os.environ.get("DEV_BROWSER_CHROME"),
                  "skip": os.environ.get("DEV_BROWSER_SKIP_DOWNLOAD")}}))
"""


@pytest.fixture
def wrapper(tmp_path) -> Path:
    real = tmp_path / "dev-browser-bin"
    real.write_text(FAKE_REAL.format(python=sys.executable))
    real.chmod(0o755)
    text = WRAPPER.read_text()
    assert 'REAL="/opt/swarm/dev-browser/dev-browser-bin"' in text
    copy = tmp_path / "dev-browser"
    copy.write_text(text.replace('REAL="/opt/swarm/dev-browser/dev-browser-bin"', f'REAL="{real}"'))
    copy.chmod(0o755)
    return copy


def _wrap(wrapper: Path, *argv: str, endpoint: str | None = ENDPOINT, stub: bool = True,
          stdin: str = "") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_CDP_ENDPOINT", "PLAYWRIGHT_BROWSERS_PATH")}
    if endpoint is not None:
        env["SWARM_CDP_ENDPOINT"] = endpoint
    if stub:
        env["PLAYWRIGHT_BROWSERS_PATH"] = STUB_ROOT
    return subprocess.run(["bash", str(wrapper), *argv], env=env, input=stdin,
                          capture_output=True, text=True, timeout=30, check=False)


def _passed(done: subprocess.CompletedProcess) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_every_call_is_connected_to_the_live_browser(wrapper):
    seen = _passed(_wrap(wrapper, "-e", "await browser.listPages()"))
    assert seen["argv"] == ["--connect", ENDPOINT, "-e", "await browser.listPages()"]
    for sub in (["stop"], ["pages"], ["status"], ["run", "script.js"], ["stop", "default"]):
        assert _passed(_wrap(wrapper, *sub))["argv"] == ["--connect", ENDPOINT, *sub]


def test_a_heredoc_on_stdin_is_passed_through(wrapper):
    script = 'const p = await browser.getPage("main");\nconsole.log(await p.title());\n'
    seen = _passed(_wrap(wrapper, stdin=script))
    assert seen["argv"] == ["--connect", ENDPOINT]
    assert seen["stdin"] == script


def test_the_real_binary_is_given_the_stub_as_its_chrome(wrapper):
    seen = _passed(_wrap(wrapper, "-e", "1"))
    assert seen["chrome"] == "/opt/swarm/dev-browser/live-browser-only"
    assert seen["skip"] == "1"


@pytest.mark.parametrize(
    "argv",
    [
        ["install"], ["install", "--force"], ["--json", "install"],
        ["chrome"], ["chrome", "--headless"],
        ["--headless", "-e", "1"], ["-e", "1", "--headless"], ["--headless=true"], ["pages", "--headless"],
        ["--connect", "9222", "-e", "1"], ["-c", "127.0.0.1:9222"], ["--connect", "-e", "1"],
        ["--connect"], ["--connect=9222"], ["pages", "--connect", "ws://127.0.0.1:9223/devtools/browser/x"],
    ],
)
def test_a_browser_of_its_own_is_refused_toward_the_live_browser(wrapper, argv):
    done = _wrap(wrapper, *argv)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert REFUSAL in done.stderr
    assert done.stdout == ""  # the real binary never ran


def test_the_live_browsers_own_connect_is_not_doubled(wrapper):
    assert _passed(_wrap(wrapper, "--connect", ENDPOINT, "-e", "1"))["argv"] == ["--connect", ENDPOINT, "-e", "1"]
    assert _passed(_wrap(wrapper, f"--connect={ENDPOINT}", "pages"))["argv"] == ["--connect", ENDPOINT, "pages"]


def test_a_script_that_says_install_or_headless_is_not_read_as_a_flag(wrapper):
    code = "console.log('install chrome --headless')"
    assert _passed(_wrap(wrapper, "-e", code))["argv"] == ["--connect", ENDPOINT, "-e", code]
    assert _passed(_wrap(wrapper, "-e", "--headless"))["argv"] == ["--connect", ENDPOINT, "-e", "--headless"]


def test_without_the_endpoint_the_stub_means_a_refusal(wrapper):
    done = _wrap(wrapper, "-e", "1", endpoint=None)
    assert done.returncode == 2
    assert "SWARM_CDP_ENDPOINT is not set" in done.stderr and REFUSAL in done.stderr
    assert done.stdout == ""


def test_help_and_version_need_no_browser(wrapper):
    for argv in (["--help"], ["-h"], ["help", "connect"], ["--version"], ["-V"]):
        assert _passed(_wrap(wrapper, *argv, endpoint=None))["argv"] == argv


def test_without_the_stub_and_the_endpoint_it_passes_through_unconnected(wrapper):
    # The browser image with no live browser: no --connect to add, and the
    # stub as DEV_BROWSER_CHROME still fails any launch (the self-test proves it).
    seen = _passed(_wrap(wrapper, "-e", "1", endpoint=None, stub=False))
    assert seen["argv"] == ["-e", "1"]
    assert seen["chrome"] == "/opt/swarm/dev-browser/live-browser-only"


def test_the_wrapper_is_a_house_script():
    text = WRAPPER.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [ln for ln in text.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"
    assert os.access(WRAPPER, os.X_OK)
    for bash4 in ("mapfile", "declare -A", ",,}", "^^}"):
        assert bash4 not in text, bash4


# --- the image -------------------------------------------------------------------


def _instructions(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#") or (not s and not cur):
            continue
        cur.append(s.rstrip("\\").rstrip())
        if not s.endswith("\\"):
            out.append(" ".join(cur))
            cur = []
    return out


def test_the_image_pins_the_binary_by_version_and_sha256_through_the_mirror():
    steps = _instructions(DOCKERFILE.read_text())
    args = dict(re.match(r"ARG (\w+)=(\S+)$", s).groups() for s in steps if re.match(r"ARG \w+=\S+$", s))
    assert args["DEV_BROWSER_VERSION"] == VERSION
    sha = args["DEV_BROWSER_SHA256"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    obj = f"github.com/SawyerHood/dev-browser/releases/download/v{VERSION}/dev-browser-linux-x64"
    assert f"{sha}  {obj}" in MANIFEST.read_text().splitlines()
    run = [s for s in steps if s.startswith("RUN ") and "dev-browser-linux-x64" in s]
    assert len(run) == 1
    assert run[0].startswith("RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror")
    assert 'echo "${DEV_BROWSER_SHA256}  /tmp/dev-browser/dev-browser-linux-x64" | sha256sum -c -' in run[0]
    assert "/opt/swarm/dev-browser/dev-browser-bin" in run[0]
    assert "/opt/swarm/dev-browser/live-browser-only" in run[0]
    # No Node package: the binary is standalone.
    assert not any("npm install" in s and "dev-browser" in s for s in steps)


def test_the_wrapper_is_on_path_and_the_real_binary_is_not():
    steps = _instructions(DOCKERFILE.read_text())
    assert "COPY --chmod=0555 images/agent-runtime-browser/dev-browser /usr/local/bin/dev-browser" in steps
    # Every mention of the real binary is its one off-PATH location.
    text = " ".join(steps)
    assert re.findall(r"\S*dev-browser-bin\b", text) == ["/opt/swarm/dev-browser/dev-browser-bin"]


def test_the_self_test_proves_the_wrapper_and_the_pin():
    text = SELFTEST.read_text()
    assert "dev_browser(sys.argv[1]" in text
    for needle in ('["install"]', '"--headless"', '"--connect"', "SWARM_CDP_ENDPOINT is not set",
                   '"--version"', "DEV_BROWSER_CHROME", "MESSAGE in done.stdout + done.stderr"):
        assert needle in text, needle
    steps = _instructions(DOCKERFILE.read_text())
    run = [s for s in steps if s.startswith("RUN ") and "selftest.py" in s]
    assert len(run) == 1 and 'DEV_BROWSER_VERSION="${DEV_BROWSER_VERSION}"' in run[0]


def test_the_design_doc_records_why_attach_only_and_the_size():
    text = DESIGN_DOC.read_text()
    m = re.search(r"^#+ .*dev-browser \(opt-in\).*$", text, re.M)
    assert m, "docs/design/live-browser.md has a 'dev-browser (opt-in)' subsection"
    section = text[m.end():]
    nxt = re.search(r"^#{1,3} ", section, re.M)
    section = section[: nxt.start()] if nxt else section
    assert "attach" in section.lower()
    assert "not a sandbox" in " ".join(section.split())
    assert "MiB" in section  # the size delta
    assert "separate lane" in " ".join(section.split())
