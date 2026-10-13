"""Build-time: fill a Playwright browsers directory with the live-browser stub.

    python install-browser-stub.py STUB_ROOT STUB_SOURCE

Run by images/agent-runtime-browser/Dockerfile as root (BROWSER-ENFORCE, part
of #1030). STUB_ROOT is the directory the claude-code runner hands the agent as
PLAYWRIGHT_BROWSERS_PATH when the live browser exists
(`claude_code.AGENT_BROWSERS_PATH`); STUB_SOURCE is `live-browser-only`.

WHY ASK PLAYWRIGHT RATHER THAN WRITE THE PATHS DOWN. Where Playwright looks
for Chromium depends on its version: the revision in the directory name, the
`chrome-linux64` vs `chrome-linux` platform folder, and which launches want
the headless shell (`headless=True` without a channel) and which the full
build (`headless=False`, `channel="chromium"`). Hard-coded, a Playwright bump
would leave a launch that finds no stub and fails with Playwright's own
"Executable doesn't exist ... run playwright install" -- advice that sends the
agent off installing a second browser. So each launch an agent can make is
tried against STUB_ROOT, the path Playwright names in that error is where a
stub goes, and the launch is tried again until it reaches a stub. The
Dockerfile's self-test then proves every one of those launches ends in the
stub's message.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

MISSING = re.compile(r"Executable doesn't exist at (\S+)")
#: Playwright skips its host-dependency check for a browser directory holding
#: this marker; the real install writes it, so the stub's directories do too.
MARKERS = ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED")


def main(root: str, source: str) -> int:
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    # Read by the driver when it starts, so set before sync_playwright().
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = root
    from playwright.sync_api import Error, sync_playwright

    profile = tempfile.mkdtemp()
    launches = {
        "launch(headless=True)": lambda p: p.chromium.launch(headless=True),
        "launch(headless=False)": lambda p: p.chromium.launch(headless=False),
        'launch(channel="chromium")': lambda p: p.chromium.launch(channel="chromium"),
        "launch_persistent_context(headless=True)":
            lambda p: p.chromium.launch_persistent_context(profile, headless=True),
    }
    installed: list[str] = []
    with sync_playwright() as p:
        for name, launch in launches.items():
            for _ in range(4):
                try:
                    launch(p).close()
                except Error as exc:
                    found = MISSING.search(str(exc))
                    if not found:
                        break  # it reached a stub (or failed otherwise; the self-test says which)
                    path = found.group(1)
                    if not path.startswith(root + os.sep):
                        print(f"{name}: Playwright looks outside {root}: {path}", file=sys.stderr)
                        return 1
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copyfile(source, path)
                    os.chmod(path, 0o555)
                    top = os.path.join(root, os.path.relpath(path, root).split(os.sep)[0])
                    for marker in MARKERS:
                        open(os.path.join(top, marker), "a").close()
                    installed.append(path)
                    continue
                print(f"{name}: launched a real browser from {root}", file=sys.stderr)
                return 1
            else:
                print(f"{name}: no stub reached after 4 installs", file=sys.stderr)
                return 1
    shutil.rmtree(profile, ignore_errors=True)
    if not installed:
        print(f"no launch looked under {root}", file=sys.stderr)
        return 1
    for path in sorted(set(installed)):
        print(f"stub: {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
