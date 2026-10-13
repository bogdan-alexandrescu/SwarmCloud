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
  6. the display sidecar's pieces: uid 10002, Xvfb, a policy Chromium parses.
"""

from __future__ import annotations

import glob
import json
import os
import pwd
import subprocess
import sys
import tempfile

MESSAGE = (
    "This agent's browser is the live browser the person watches: connect with "
    "chromium.connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])"
)
REAL = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
MCP = "/usr/local/share/npm-global/bin/playwright-mcp"
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


if __name__ == "__main__":
    real_browser()
    stub_launches(sys.argv[1])
    helper_and_mcp()
    display_pieces()
    print("browser image self-test passed")
