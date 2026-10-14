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
     the stub, ends in the stub's message (DEVB-INSTALL, part of #1030);
  9. jev-browser is the pinned JEV_BROWSER_VERSION on the image's Playwright,
     its wrapper refuses to start without SWARM_CDP_ENDPOINT, the patched
     module exports the attach and refuses a launch, the attach drives a real
     Chromium's existing context over CDP without one method the live gate
     refuses and leaves the browser running when it closes, and the MCP
     server lists its tools through the wrapper (JEV-IMAGE, part of #1030).
"""

from __future__ import annotations

import glob
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

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
#: jev-browser (JEV-IMAGE): the npm install, its wrapper, and the probe beside
#: this file that attaches the patched package to a Chromium over CDP.
JEV_ROOT = "/opt/swarm/jev-browser"
JEV_WRAPPER = f"{JEV_ROOT}/bin/jev-browser-mcp"
JEV_PACKAGE = f"{JEV_ROOT}/node_modules/jev-browser"
JEV_PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jev-browser", "attach-probe.mjs")
JEV_REFUSAL = "jev-browser here drives only the live browser the person watches"
#: The MCP server's tools, as jev-browser 0.2.0 registers them.
JEV_TOOLS = {
    "browser_open", "browser_do", "browser_check", "browser_choose", "browser_snapshot", "browser_act",
    "browser_screenshot", "browser_close", "browser_read", "browser_webmcp_tools", "browser_webmcp_call",
}
#: What an attach must never send: each is refused by the live gate
#: (livegate/cdppolicy.py DENIED) and would mean a new context, a device of its
#: own, the remote browser closed, or the cookie jar touched.
JEV_GATE_REFUSED = (
    "Target.createBrowserContext", "Browser.close", "Emulation.setDeviceMetricsOverride",
    "Emulation.clearDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled", "Emulation.setVisibleSize",
    "Network.setUserAgentOverride", "Network.setExtraHTTPHeaders", "Network.getCookies",
    "Network.getAllCookies", "Storage.getCookies", "Network.setCookies", "Storage.setCookies",
    "Network.clearBrowserCookies", "Storage.clearCookies", "Target.sendMessageToTarget",
)
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


def _cdp_chromium(profile: str) -> tuple[subprocess.Popen, str]:
    """The gate's Chromium, headless, on a CDP port of its choosing."""
    from agent_worker.livegate.__main__ import find_chromium

    proc = subprocess.Popen(
        [find_chromium(REAL), "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--no-zygote",
         "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = os.path.join(profile, "DevToolsActivePort")
    for _ in range(300):
        if os.path.exists(port_file) and open(port_file, encoding="utf-8").read().strip():
            return proc, f"http://127.0.0.1:{open(port_file, encoding='utf-8').read().split()[0]}"
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.kill()
    check(False, "a Chromium with a CDP port started for the jev-browser attach")
    raise AssertionError  # unreachable: check() exits


def _cdp_pages(endpoint: str) -> list[str]:
    with urllib.request.urlopen(f"{endpoint}/json/list", timeout=10) as resp:
        return [t["id"] for t in json.load(resp) if t.get("type") == "page"]


def _mcp_tools(env: dict[str, str]) -> set[str]:
    """tools/list over the wrapper's stdio, newline-delimited JSON-RPC."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "swarm-selftest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen([JEV_WRAPPER], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    assert proc.stdin and proc.stdout
    try:
        for msg in lines:
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for _ in range(10):
            line = proc.stdout.readline()
            if not line:
                break
            reply = json.loads(line)
            if reply.get("id") == 2:
                return {t["name"] for t in reply["result"]["tools"]}
    finally:
        proc.kill()
        proc.wait(timeout=30)
    return set()


def jev_browser(version: str) -> None:
    check(bool(version), "JEV_BROWSER_VERSION names the pinned version")
    import playwright

    with open(f"{JEV_PACKAGE}/package.json", encoding="utf-8") as fh:
        check(json.load(fh)["version"] == version, f"jev-browser is {version}")
    with open(f"{JEV_ROOT}/node_modules/playwright-core/package.json", encoding="utf-8") as fh:
        core = json.load(fh)["version"]
    check(core == playwright.__version__, f"jev-browser's Playwright is the image's {playwright.__version__}: {core}")
    check(not os.path.exists(f"{JEV_ROOT}/node_modules/jev-browser/node_modules/playwright-core"),
          "no second Playwright under jev-browser")
    check(os.access(JEV_WRAPPER, os.X_OK) and shutil.which("jev-browser-mcp") is None,
          f"the wrapper is {JEV_WRAPPER}, off PATH")

    home = tempfile.mkdtemp()
    base = {**os.environ, "HOME": home}
    for name in ("SWARM_CDP_ENDPOINT", "JEV_BROWSER_CDP_ENDPOINT", "DISPLAY"):
        base.pop(name, None)
    done = subprocess.run([JEV_WRAPPER], env=base, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    check(done.returncode == 2 and "SWARM_CDP_ENDPOINT is not set" in done.stderr and JEV_REFUSAL in done.stderr,
          f"the wrapper refuses without SWARM_CDP_ENDPOINT: {done.returncode} {done.stderr[-400:]!r}")

    # The patched module: the attach is exported, and a launch is refused.
    code = (
        "const m = await import(process.argv[1]);"
        "if (typeof m.JevBrowser.attach !== 'function' || !m.LAUNCH_REFUSED) process.exit(3);"
        "try { await m.JevBrowser.launch(); console.log('LAUNCHED'); }"
        "catch (e) { console.log(e.message === m.LAUNCH_REFUSED ? 'LAUNCH_REFUSED' : e.message); }"
    )
    done = subprocess.run(["node", "--input-type=module", "-e", code, f"{JEV_PACKAGE}/src/index.mjs"],
                          env=base, capture_output=True, text=True, timeout=60)
    check(done.returncode == 0 and done.stdout.strip() == "LAUNCH_REFUSED",
          f"the patched module exports the attach and refuses a launch: {done.stdout[-300:]!r} {done.stderr[-300:]!r}")

    # The attach, against a real Chromium over CDP, the way the live gate's
    # proxy presents it; Playwright's protocol log is every method it sent.
    profile = tempfile.mkdtemp()
    proc, endpoint = _cdp_chromium(profile)
    try:
        before = _cdp_pages(endpoint)
        env = {**base, "JEV_BROWSER_CDP_ENDPOINT": endpoint, "DEBUG": "pw:protocol"}
        done = subprocess.run(["node", JEV_PROBE, f"{JEV_PACKAGE}/src/index.mjs"], env=env,
                              capture_output=True, text=True, timeout=180)
        check(done.returncode == 0, "the attach probe ran" if done.returncode == 0
              else f"the attach probe ran: {done.stdout[-400:]!r} {done.stderr[-1200:]!r}")
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        check(seen == {"attached": True, "contexts": 1, "viewport": None, "title": "jev attach ok",
                       "snapshot_has_button": True, "webmcp_tools": 0, "contexts_after": 1,
                       "connected_after_close": False},
              f"jev-browser drives the existing context with no viewport and no WebMCP script: {seen}")
        sent = set(re.findall(r'SEND . \{"id":\d+,"method":"([A-Za-z]+\.[A-Za-z]+)"', done.stderr))
        check(bool(sent), f"the protocol log was read ({len(sent)} methods)")
        refused = sorted(sent.intersection(JEV_GATE_REFUSED))
        check(not refused, f"the attach sends no method the live gate refuses: {refused}")
        # The one gate-refused method Playwright sends on every attach, with an
        # empty user agent, and whose failure it swallows (crNetworkManager).
        agents = re.findall(r'"method":"Emulation.setUserAgentOverride","params":\{"userAgent":"([^"]*)"', done.stderr)
        check(all(a == "" for a in agents), f"no user agent is set: {agents}")
        check(proc.poll() is None and _cdp_pages(endpoint) == before,
              "close() disconnected and left the browser and its tab running")

        tools = _mcp_tools({**base, "SWARM_CDP_ENDPOINT": endpoint})
        check(tools == JEV_TOOLS, f"the MCP server lists its {len(JEV_TOOLS)} tools through the wrapper: {sorted(tools)}")
    finally:
        proc.kill()
        proc.wait(timeout=30)


if __name__ == "__main__":
    real_browser()
    stub_launches(sys.argv[1])
    helper_and_mcp()
    display_pieces()
    no_codex()
    dev_browser(sys.argv[1], os.environ.get("DEV_BROWSER_VERSION", ""))
    jev_browser(os.environ.get("JEV_BROWSER_VERSION", ""))
    print("browser image self-test passed")
