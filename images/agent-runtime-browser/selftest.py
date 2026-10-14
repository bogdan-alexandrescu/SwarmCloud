"""Build-time self-test of agent-runtime-browser, run ONCE as the agent's uid.

    python selftest.py STUB_ROOT

Run by the Dockerfile's last RUN (bind-mounted, so it is not shipped). One RUN
for every check: two check-only RUNs can write the same layer, and GKE does
not stream an image with a duplicate layer (PR 1239, docs/browser-startup-
speed.md §2.4). It proves, in the image as built, rather than in a tenant's
attempt at 3am:

  1. the full Chromium launches headless with `channel="chromium"` from
     /opt/playwright, the way the `browser` runner launches it -- now that
     the headless shell is not installed (`--no-shell`) and the driver runs on
     the base's Node through `driver/node`;
  2. the gate's own binary is where `livegate.find_chromium` looks, and no
     `chromium_headless_shell-*` is left beside it;
  3. every launch an agent can make against STUB_ROOT -- the agent's
     PLAYWRIGHT_BROWSERS_PATH when the live browser exists -- fails with the
     message naming `connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])`;
  4. the helper imports and Playwright has the CDP connect it uses;
  5. the Playwright MCP server the claude-code runner registers runs, and
     takes `--cdp-endpoint`;
  6. the display sidecar's pieces: uid 10002, Xvfb, a policy Chromium parses;
  7. codex is NOT in the image -- it is built FROM agent-runtime-base-nocodex
     (BROWSER-NO-CODEX, owner decision 2026-10-13) -- and every other tool the
     base carries is;
  8. `dev-browser` on PATH is the wrapper, the real binary is off PATH and
     reports the pinned DEV_BROWSER_VERSION, `dev-browser install`,
     `--headless` and a foreign `--connect` are refused, a run without
     SWARM_CDP_ENDPOINT while the stub is active fails toward the live
     browser, and the real binary's own launch, with DEV_BROWSER_CHROME on
     the stub, ends in the stub's message (DEVB-INSTALL, part of #1030).
"""

from __future__ import annotations

import glob
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile

MESSAGE = (
    "This agent's browser is the live browser the person watches: connect with "
    "chromium.connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])"
)
REAL = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
MCP = "/usr/local/share/npm-global/bin/playwright-mcp"
# What the base gives every agent, with codex left out: the agent CLI this
# image's profile runs, the runtimes, and every tool of the base's own toolbox
# smoke (images/agent-runtime-base/Dockerfile; a unit test holds the two lists
# together). Leaving codex out must take nothing else with it.
TOOLS = (
    "claude", "node", "npm", "python", "uv", "git",
    "gh", "gcloud", "gke-gcloud-auth-plugin", "kubectl", "terraform", "checkov",
    "trivy", "shellcheck", "make", "docker",
)
CODEX_PACKAGE = "/usr/local/share/npm-global/lib/node_modules/@openai/codex"
#: dev-browser (DEVB-INSTALL): the wrapper on PATH, the real binary off it,
#: and the stub the real binary is given as its Chrome.
DEV_BROWSER = "/usr/local/bin/dev-browser"
DEV_BROWSER_BIN = "/opt/swarm/dev-browser/dev-browser-bin"
DEV_BROWSER_STUB = "/opt/swarm/dev-browser/live-browser-only"
#: What the wrapper prints on every refusal.
DEV_BROWSER_REFUSAL = "dev-browser here drives only the live browser the person watches"
POLICIES = ("/etc/opt/chrome/policies/managed/swarm.json", "/etc/chromium/policies/managed/swarm.json")


def check(ok: bool, what: str) -> None:
    if not ok:
        print(f"browser image self-test FAILED: {what}", file=sys.stderr)
        raise SystemExit(1)
    print(f"ok: {what}")


def real_browser() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium", chromium_sandbox=False,
                                    args=["--disable-dev-shm-usage", "--no-zygote"])
        page = browser.new_page()
        page.set_content("<h1>swarm browser runtime ok</h1>")
        text = page.inner_text("h1")
        browser.close()
    check(text == "swarm browser runtime ok", "full Chromium launches headless as a non-root user")

    from agent_worker.livegate.__main__ import find_chromium

    gate = find_chromium(REAL)
    check(os.access(gate, os.X_OK), f"the gate's Chromium is {gate}")
    check(not glob.glob(os.path.join(REAL, "chromium_headless_shell-*")), "no headless shell installed")
    import playwright

    node = os.path.join(os.path.dirname(playwright.__file__), "driver", "node")
    check(os.path.realpath(node) == "/usr/local/bin/node", "the driver runs on the image's Node")


def stub_launches(stub_root: str) -> None:
    # A child, so the driver starts with the agent's PLAYWRIGHT_BROWSERS_PATH.
    code = r"""
import sys, tempfile
from playwright.sync_api import Error, sync_playwright
profile = tempfile.mkdtemp()
with sync_playwright() as p:
    for name, launch in (
        ("launch(headless=True)", lambda: p.chromium.launch(headless=True)),
        ("launch(headless=False)", lambda: p.chromium.launch(headless=False)),
        ("launch()", lambda: p.chromium.launch()),
        ('launch(channel="chromium")', lambda: p.chromium.launch(channel="chromium")),
        ("launch_persistent_context(headless=True)",
         lambda: p.chromium.launch_persistent_context(profile, headless=True)),
        ("launch_persistent_context(headless=False)",
         lambda: p.chromium.launch_persistent_context(profile, headless=False)),
    ):
        try:
            launch().close()
        except Error as exc:
            print(name, "\x00", str(exc).replace("\n", "\\n"))
            continue
        print(name, "\x00", "LAUNCHED")
"""
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": stub_root}
    env.pop("DISPLAY", None)
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          timeout=300)
    check(done.returncode == 0, f"the stub launches ran: {done.stderr[-2000:]}")
    lines = [line for line in done.stdout.splitlines() if "\x00" in line]
    check(len(lines) == 6, f"six launches tried, got {len(lines)}")
    for line in lines:
        name, result = (part.strip() for part in line.split("\x00", 1))
        check(MESSAGE in result, f"{name} fails toward the live browser: {result[:600]}")


def helper_and_mcp() -> None:
    from playwright.sync_api import sync_playwright
    from swarm_browser import live

    check(callable(live), "from swarm_browser import live")
    with sync_playwright() as p:
        check(callable(getattr(p.chromium, "connect_over_cdp", None)), "chromium.connect_over_cdp exists")
    home = tempfile.mkdtemp()
    done = subprocess.run([MCP, "--help"], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HOME": home})
    check(done.returncode == 0 and "--cdp-endpoint" in done.stdout,
          f"{MCP} --help offers --cdp-endpoint: {done.stdout[-400:]}{done.stderr[-400:]}")


def display_pieces() -> None:
    check(pwd.getpwnam("swarm-display").pw_uid == 10002, "swarm-display is uid 10002")
    check(os.access("/usr/bin/Xvfb", os.X_OK), "Xvfb is installed")
    for path in POLICIES:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
    check(True, "both Chromium policies parse")


def no_codex() -> None:
    found = shutil.which("codex")
    check(found is None and not os.path.exists(CODEX_PACKAGE),
          f"codex is absent (on PATH: {found}; package: {os.path.exists(CODEX_PACKAGE)})")
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    check(not missing, f"{len(TOOLS)} tools of the base are present (missing: {missing})")


def _dev_browser(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


def dev_browser(stub_root: str, version: str) -> None:
    check(bool(version), "DEV_BROWSER_VERSION names the pinned version")
    check(shutil.which("dev-browser") == DEV_BROWSER, f"dev-browser on PATH is the wrapper {DEV_BROWSER}")
    check(os.access(DEV_BROWSER_BIN, os.X_OK), f"the real binary is {DEV_BROWSER_BIN}")
    check(shutil.which("dev-browser-bin") is None, "the real binary is not on PATH")
    check(os.access(DEV_BROWSER_STUB, os.X_OK), f"the stub dev-browser launches is {DEV_BROWSER_STUB}")
    home = tempfile.mkdtemp()
    base = {**os.environ, "HOME": home, "DEV_BROWSER_HOME": os.path.join(home, "dev-browser")}
    base.pop("SWARM_CDP_ENDPOINT", None)
    base.pop("DISPLAY", None)

    done = _dev_browser([DEV_BROWSER_BIN, "--version"], base)
    check(done.returncode == 0 and done.stdout.strip() == f"dev-browser {version}",
          f"the real binary is dev-browser {version}: {done.stdout.strip()!r} {done.stderr[-300:]!r}")
    done = _dev_browser([DEV_BROWSER, "--version"], base)
    check(done.returncode == 0 and version in done.stdout, f"the wrapper passes --version through: {done.stdout!r}")

    # The live browser's variables as the claude-code runner sets them; the
    # endpoint answers nothing here, and nothing below gets as far as it.
    live = {**base, "PLAYWRIGHT_BROWSERS_PATH": stub_root, "SWARM_CDP_ENDPOINT": "http://127.0.0.1:9"}
    for argv, what in (
        (["install"], "`dev-browser install`"),
        (["install", "--force"], "`dev-browser install --force`"),
        (["chrome"], "`dev-browser chrome`"),
        (["--headless", "-e", "1"], "`dev-browser --headless`"),
        (["--connect", "9222", "-e", "1"], "`dev-browser --connect 9222`"),
        (["--connect", "-e", "1"], "a bare `--connect`"),
    ):
        done = _dev_browser([DEV_BROWSER, *argv], live)
        check(done.returncode == 2 and DEV_BROWSER_REFUSAL in done.stderr,
              f"{what} is refused toward the live browser: {done.returncode} {done.stderr[-400:]!r}")

    no_endpoint = {k: v for k, v in live.items() if k != "SWARM_CDP_ENDPOINT"}
    done = _dev_browser([DEV_BROWSER, "-e", "1"], no_endpoint)
    check(done.returncode == 2 and "SWARM_CDP_ENDPOINT is not set" in done.stderr
          and DEV_BROWSER_REFUSAL in done.stderr,
          f"a run without SWARM_CDP_ENDPOINT fails toward the live browser: {done.stderr[-400:]!r}")

    # The real binary, run by its path, launching: DEV_BROWSER_CHROME is the
    # stub, so the launch fails with the stub's message. Then stop the daemon
    # it started, so nothing outlives this RUN.
    own = {**base, "DEV_BROWSER_CHROME": DEV_BROWSER_STUB, "DEV_BROWSER_SKIP_DOWNLOAD": "1"}
    done = _dev_browser([DEV_BROWSER_BIN, "--headless", "-e", "1"], own)
    _dev_browser([DEV_BROWSER_BIN, "stop"], own)
    check(done.returncode != 0 and MESSAGE in done.stdout + done.stderr,
          f"the real binary's own launch ends in the stub: {(done.stdout + done.stderr)[-600:]!r}")


if __name__ == "__main__":
    real_browser()
    stub_launches(sys.argv[1])
    helper_and_mcp()
    display_pieces()
    no_codex()
    dev_browser(sys.argv[1], os.environ.get("DEV_BROWSER_VERSION", ""))
    print("browser image self-test passed")
