"""JEV-IMAGE (owner decision 2026-10-14; part of #1030).

agent-runtime-browser carries jev-browser (TypeSafe "Jev", npm `jev-browser`)
at an exact pinned version, patched to attach to the live browser over CDP,
and unable to launch a browser of its own:

  * images/agent-runtime-browser/jev-browser/ holds the package.json and
    package-lock.json the image installs with `npm ci` into
    /opt/swarm/jev-browser, Playwright held to the image's own 1.63.0 so no
    second browser build is wanted, and no Chromium is downloaded;
  * patches/jev-browser+<version>.patch adds the attach: with
    JEV_BROWSER_CDP_ENDPOINT set, `chromium.connectOverCDP`, the browser's
    EXISTING default context (never `newContext`), no viewport, no WebMCP init
    script, and close() only disconnecting; without it, every launch and every
    Chromium download is refused;
  * bin/jev-browser-mcp is the wrapper the runner registers: it refuses to
    start without SWARM_CDP_ENDPOINT and makes that the attach endpoint.

The wrapper is run here against a fake node that prints what it was given, so
what it passes is measured, not read. The patch needs the upstream package and
a browser to run, neither of which a unit test has offline: the image's
build-time self-test (selftest.py `jev_browser`) attaches the patched package
to a real Chromium over CDP, reads Playwright's protocol log for every method
the live gate refuses, and lists the MCP server's tools through this wrapper.

MUTATION: drop the wrapper's SWARM_CDP_ENDPOINT refusal, or its `unset` of
JEV_API_URL, or the patch's `if (!attached)` guard on the WebMCP init script,
or its `throw new Error(LAUNCH_REFUSED)` in launch(), and the named test below
fails.
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
JEV_DIR = IMAGE_DIR / "jev-browser"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
SELFTEST = IMAGE_DIR / "selftest.py"
WRAPPER = JEV_DIR / "bin" / "jev-browser-mcp"
PROBE = JEV_DIR / "attach-probe.mjs"

VERSION = "0.2.0"
PATCH = JEV_DIR / "patches" / f"jev-browser+{VERSION}.patch"
INSTALL_ROOT = "/opt/swarm/jev-browser"
SERVER = f"{INSTALL_ROOT}/node_modules/jev-browser/bin/jev-browser-mcp.mjs"
ENDPOINT = "http://127.0.0.1:9222"
REFUSAL = "jev-browser here drives only the live browser the person watches"


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


def _args(steps: list[str]) -> dict[str, str]:
    return dict(re.match(r"ARG (\w+)=(\S+)$", s).groups() for s in steps if re.match(r"ARG \w+=\S+$", s))


# --- the pin -----------------------------------------------------------------------


def test_the_package_pins_jev_browser_and_the_images_playwright_exactly():
    pkg = json.loads((JEV_DIR / "package.json").read_text())
    assert pkg["private"] is True
    assert pkg["dependencies"] == {"jev-browser": VERSION, "playwright": "1.63.0"}
    # jev-browser asks for `playwright ^1.63.0`; the override holds it to the
    # Playwright whose Chromium revision the image already carries.
    assert pkg["overrides"] == {"playwright": "1.63.0"}
    args = _args(_instructions(DOCKERFILE.read_text()))
    assert args["PLAYWRIGHT_VERSION"] == pkg["dependencies"]["playwright"]
    assert args["JEV_BROWSER_VERSION"] == VERSION


def test_the_lockfile_resolves_every_package_from_the_npm_registry_with_integrity():
    lock = json.loads((JEV_DIR / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    packages = {k: v for k, v in lock["packages"].items() if k}
    assert packages["node_modules/jev-browser"]["version"] == VERSION
    assert packages["node_modules/playwright"]["version"] == "1.63.0"
    cores = {k: v["version"] for k, v in packages.items() if k.endswith("node_modules/playwright-core")}
    assert cores == {"node_modules/playwright-core": "1.63.0"}, cores  # one Playwright, the image's
    for name, entry in packages.items():
        assert entry["resolved"].startswith("https://registry.npmjs.org/"), (name, entry["resolved"])
        assert entry["integrity"].startswith("sha512-"), name
        assert not entry.get("hasInstallScript"), f"{name} runs an install script"


# --- the patch ---------------------------------------------------------------------


def _patch_files() -> dict[str, list[str]]:
    """Added lines of the patch, by the file they land in."""
    files: dict[str, list[str]] = {}
    current = ""
    for line in PATCH.read_text().splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)$", line)
        if m:
            assert m.group(1) == m.group(2), line
            current = m.group(1)
            files[current] = []
        elif line.startswith("+") and not line.startswith("+++"):
            files[current].append(line[1:])
    return files


def test_the_patch_touches_only_the_pinned_package():
    assert set(_patch_files()) == {
        "node_modules/jev-browser/src/session.mjs",
        "node_modules/jev-browser/src/index.mjs",
        "node_modules/jev-browser/bin/jev-browser-mcp.mjs",
    }
    patches = sorted(p.name for p in (JEV_DIR / "patches").iterdir())
    assert patches == [PATCH.name]


def test_the_patch_attaches_to_the_existing_context_and_never_makes_one():
    added = _patch_files()["node_modules/jev-browser/src/session.mjs"]
    text = "\n".join(added)
    start = next(i for i, ln in enumerate(added) if "static async attach(" in ln)
    end = next(i for i, ln in enumerate(added) if ln.lstrip().startswith("constructor("))
    attach = "\n".join(added[start:end])
    assert "chromium.connectOverCDP(endpoint" in attach
    assert "browser.contexts()[0]" in attach
    assert "{ attached: true }" in attach
    for forbidden in ("newContext", "viewport", "setViewportSize", "emulate", "launch(",
                      "launchPersistentContext", "context.close", "addCookies", "storageState"):
        assert forbidden not in attach, forbidden
    # launch() goes to attach with the endpoint, and refuses without it.
    assert "if (process.env.JEV_BROWSER_CDP_ENDPOINT) return JevBrowser.attach(process.env.JEV_BROWSER_CDP_ENDPOINT" in text
    assert "    throw new Error(LAUNCH_REFUSED);" in added
    assert re.search(r'^export const LAUNCH_REFUSED = ".*' + REFUSAL, text, re.M)


def test_the_patch_leaves_the_webmcp_init_script_off_an_attached_browser():
    added = _patch_files()["node_modules/jev-browser/src/session.mjs"]
    assert "    if (!attached) context.addInitScript(WEBMCP_INIT);" in added


def test_closing_an_attached_session_only_disconnects():
    added = _patch_files()["node_modules/jev-browser/src/session.mjs"]
    line = next(ln for ln in added if "if (this.attached)" in ln)
    # For a connectOverCDP browser, browser.close() drops the connection; the
    # default context's close() would close the browser itself.
    assert line.strip() == "if (this.attached) { await this.browser.close().catch(() => {}); return; }"


def test_the_mcp_server_never_downloads_a_browser_and_the_module_exports_the_refusal():
    files = _patch_files()
    mcp = files["node_modules/jev-browser/bin/jev-browser-mcp.mjs"]
    assert "  throw new Error(LAUNCH_REFUSED);" in mcp
    assert 'import { JevBrowser, LAUNCH_REFUSED } from "../src/session.mjs";' in mcp
    assert 'export { JevBrowser, TOOLS, KEYS, LAUNCH_REFUSED } from "./session.mjs";' in files[
        "node_modules/jev-browser/src/index.mjs"]


# --- the wrapper, against a fake node ----------------------------------------------


FAKE_NODE = """#!{python}
import json, os, sys
keep = ("JEV_BROWSER_CDP_ENDPOINT", "JEV_BROWSER_NO_INSTALL", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
        "JEV_BROWSER_PROFILE", "JEV_BROWSER_HEADED", "JEV_API_URL", "JEV_MODEL", "TYPESAFE_API_KEY")
print(json.dumps({{"argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in keep}}}}))
"""


@pytest.fixture
def wrapper(tmp_path) -> Path:
    node = tmp_path / "node"
    node.write_text(FAKE_NODE.format(python=sys.executable))
    node.chmod(0o755)
    text = WRAPPER.read_text()
    assert 'NODE="/usr/local/bin/node"' in text
    assert f'SERVER="{SERVER}"' in text
    copy = tmp_path / "jev-browser-mcp"
    copy.write_text(text.replace('NODE="/usr/local/bin/node"', f'NODE="{node}"'))
    copy.chmod(0o755)
    return copy


def _wrap(wrapper: Path, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("JEV_", "SWARM_CDP", "TYPESAFE", "PLAYWRIGHT_SKIP"))}
    return subprocess.run(["bash", str(wrapper), *argv], env={**base, **(env or {})},
                          capture_output=True, text=True, timeout=30, check=False)


def _passed(done: subprocess.CompletedProcess) -> dict[str, Any]:
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_without_the_live_browser_the_wrapper_refuses_to_start(wrapper):
    for env in ({}, {"SWARM_CDP_ENDPOINT": ""}, {"JEV_BROWSER_CDP_ENDPOINT": ENDPOINT}):
        done = _wrap(wrapper, env=env)
        assert done.returncode == 2, (env, done.stdout, done.stderr)
        assert "SWARM_CDP_ENDPOINT is not set" in done.stderr and REFUSAL in done.stderr
        assert done.stdout == ""  # the server never ran


def test_the_attach_endpoint_is_always_the_live_browsers(wrapper):
    seen = _passed(_wrap(wrapper, env={"SWARM_CDP_ENDPOINT": ENDPOINT,
                                       "JEV_BROWSER_CDP_ENDPOINT": "http://127.0.0.1:9333"}))
    assert seen["argv"] == [SERVER]
    assert seen["env"]["JEV_BROWSER_CDP_ENDPOINT"] == ENDPOINT


def test_the_wrapper_drops_what_would_launch_or_redirect_and_forbids_downloads(wrapper):
    key = "ts-" + "k" * 24
    seen = _passed(_wrap(wrapper, "--extra", env={
        "SWARM_CDP_ENDPOINT": ENDPOINT, "JEV_BROWSER_PROFILE": "/tmp/profile", "JEV_BROWSER_HEADED": "1",
        "JEV_API_URL": "https://elsewhere.invalid/v1", "JEV_MODEL": "other", "TYPESAFE_API_KEY": key,
    }))
    assert seen["argv"] == [SERVER, "--extra"]
    env = seen["env"]
    for name in ("JEV_BROWSER_PROFILE", "JEV_BROWSER_HEADED", "JEV_API_URL", "JEV_MODEL"):
        assert env[name] is None, name
    assert env["JEV_BROWSER_NO_INSTALL"] == "1"
    assert env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert env["TYPESAFE_API_KEY"] == key  # passed through, never printed by the wrapper


def test_the_wrapper_is_a_house_script():
    text = WRAPPER.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [ln for ln in text.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"
    assert os.access(WRAPPER, os.X_OK)
    for bash4 in ("mapfile", "declare -A", ",,}", "^^}"):
        assert bash4 not in text, bash4


# --- the image ---------------------------------------------------------------------


def _jev_run(steps: list[str]) -> str:
    runs = [s for s in steps if s.startswith("RUN ") and "jev-browser" in s and "selftest.py" not in s]
    assert len(runs) == 1, runs
    return runs[0]


def test_the_image_installs_the_lockfile_applies_the_patch_and_downloads_no_browser():
    steps = _instructions(DOCKERFILE.read_text())
    run = _jev_run(steps)
    assert "source=images/agent-runtime-browser/jev-browser" in run
    assert f"npm ci --prefix {INSTALL_ROOT}" in run
    assert "--ignore-scripts" in run and "--omit=dev" in run
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in run
    assert 'apply --verbose "/tmp/jev-browser-build/patches/jev-browser+${JEV_BROWSER_VERSION}.patch"' in run
    assert f"{INSTALL_ROOT}/bin/jev-browser-mcp" in run
    assert f"chmod -R a+rX,a-w {INSTALL_ROOT}" in run
    assert "playwright install" not in run and "npm install" not in run
    # Off PATH: the runner registers the wrapper by its path; nothing named
    # jev-browser lands in /usr/local/bin.
    assert not any("/usr/local/bin/jev" in s for s in steps)


def test_the_self_test_runs_after_the_install_and_proves_the_attach():
    steps = _instructions(DOCKERFILE.read_text())
    selftest = [i for i, s in enumerate(steps) if s.startswith("RUN ") and "selftest.py" in s]
    assert len(selftest) == 1
    assert steps.index(_jev_run(steps)) < selftest[0]
    assert 'JEV_BROWSER_VERSION="${JEV_BROWSER_VERSION}"' in steps[selftest[0]]
    text = SELFTEST.read_text()
    assert "jev_browser(os.environ.get(\"JEV_BROWSER_VERSION\"" in text
    for needle in ("SWARM_CDP_ENDPOINT is not set", "attach-probe.mjs", "pw:protocol",
                   "Emulation.setDeviceMetricsOverride", "Target.createBrowserContext", "Browser.close",
                   "LAUNCH_REFUSED", "tools/list", "--remote-debugging-port=0"):
        assert needle in text, needle
    assert PROBE.exists()
    probe = PROBE.read_text()
    assert "JevBrowser.launch()" in probe and "b.close()" in probe
