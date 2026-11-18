"""Build-time self-test of agent-runtime-lightpanda, run ONCE as the agent's uid.

    LIGHTPANDA_VERSION=... LIGHTPANDA_COMMIT=... LIGHTPANDA_SOURCE_URL=... \
    LIGHTPANDA_SOURCE_SHA256=... python selftest.py

Run by the Dockerfile's last RUN (bind-mounted, so it is not shipped), with
the Dockerfile's ARGs as its environment. One RUN for every check: two
check-only RUNs can write the same layer, and GKE does not stream an image
with a duplicate layer (docs/browser-startup-speed.md §2.4). It proves, in
the image as built, rather than in a tenant's attempt at 3am:

  1. /opt/swarm/lightpanda/lightpanda is a regular file owned by root, mode
     0755, so the agent's uid can run it and cannot replace it;
  2. `lightpanda version`, run with only PATH, HOME, TMPDIR and the two
     LIGHTPANDA_* switches in its environment, prints exactly
     LIGHTPANDA_VERSION;
  3. the image's ENV turns telemetry and core dumps off;
  4. /usr/share/doc/lightpanda holds the AGPL text, LICENSING.md, the font
     licence, THIRD_PARTY, and a SOURCE naming the tag, the commit and the
     mirrored source archive's URL and sha256 (AGPL sections 4 and 6;
     docs/design/lightpanda-runtime.md §2.1);
  5. there is no Chromium anywhere a browser could be found: none on PATH,
     none under the usual install roots, no Playwright browser directory;
  6. the claude CLI the profile runs is on PATH (it is the base's).

tests/unit/worker/test_lightpanda_image.py runs these functions offline
against fakes.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

BINARY = Path("/opt/swarm/lightpanda/lightpanda")
DOC = Path("/usr/share/doc/lightpanda")
#: What the image's ENV must set, and what the binary is run with.
SWITCHES = {"LIGHTPANDA_DISABLE_TELEMETRY": "true", "LIGHTPANDA_DISABLE_CORE_DUMP": "1"}
#: Executable names a Chromium or Chrome install leaves behind.
CHROMIUM_NAMES = {
    "chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "headless_shell", "chrome-headless-shell",
}
#: A Playwright browser directory: `chromium-1187`, `chromium_headless_shell-1187`.
PLAYWRIGHT_DIR = re.compile(r"^chromium(?:_headless_shell)?-\d+$")
#: Where a browser would be installed: the system's, Playwright's and the
#: base's tool roots. /proc, /sys and the workspace are not walked.
SEARCH_ROOTS = ("/usr", "/opt", "/root", "/home", "/ms-playwright", "/etc/alternatives")


def fail(message: str) -> None:
    raise SystemExit(f"agent-runtime-lightpanda self-test: {message}")


def check_owner_and_mode(path: Path, uid: int = 0, gid: int | None = 0) -> None:
    """GID None skips the group, for a fake in a test's own directory."""
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        fail(f"{path} is not a regular file")
    if st.st_uid != uid or (gid is not None and st.st_gid != gid):
        fail(f"{path} is owned by {st.st_uid}:{st.st_gid}, not {uid}:{gid}")
    if stat.S_IMODE(st.st_mode) != 0o755:
        fail(f"{path} has mode {stat.S_IMODE(st.st_mode):04o}, not 0755")


def check_version(binary: Path, want: str) -> None:
    # Nothing of the build's environment is handed on: the runner gives the
    # serve process an allow-list too (design §6.2), and the check should run
    # the binary the way it will be run.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("TMPDIR") or "/tmp",
        "TMPDIR": os.environ.get("TMPDIR") or "/tmp",
        **SWITCHES,
    }
    try:
        done = subprocess.run([str(binary), "version"], env=env, capture_output=True, text=True, timeout=60,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"`{binary} version` did not run: {exc}")
    got = done.stdout.strip()
    if done.returncode != 0 or got != want:
        fail(f"`{binary} version` exited {done.returncode} printing {got!r}; expected {want!r}"
             f" (LIGHTPANDA_VERSION). stderr: {done.stderr.strip()[:400]}")


def check_switches() -> None:
    for name, want in SWITCHES.items():
        if os.environ.get(name) != want:
            fail(f"the image's ENV sets {name}={os.environ.get(name)!r}, not {want!r}")


def check_notices(doc: Path, version: str, commit: str, source_url: str, source_sha256: str) -> None:
    license_text = _read(doc / "LICENSE")
    if "GNU AFFERO GENERAL PUBLIC LICENSE" not in license_text or "Version 3" not in license_text:
        fail(f"{doc / 'LICENSE'} is not the AGPL version 3 text")
    if "AGPL-3.0-only" not in _read(doc / "LICENSING.md"):
        fail(f"{doc / 'LICENSING.md'} does not state AGPL-3.0-only")
    if "Bitstream" not in _read(doc / "FONTS-LICENSE"):
        fail(f"{doc / 'FONTS-LICENSE'} is not the embedded fonts' licence")
    if commit not in _read(doc / "THIRD_PARTY"):
        fail(f"{doc / 'THIRD_PARTY'} does not name upstream commit {commit}")
    source = _read(doc / "SOURCE")
    for want in (f"release tag: {version}", f"upstream commit: {commit}",
                 f"Corresponding Source: {source_url}", f"sha256: {source_sha256}"):
        if want not in source:
            fail(f"{doc / 'SOURCE'} does not say {want!r}")
    for path in sorted(doc.iterdir()):
        check_owner_and_mode_0644(path)


def check_owner_and_mode_0644(path: Path) -> None:
    st = path.lstat()
    if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o644:
        fail(f"{path} is {st.st_uid} {stat.S_IMODE(st.st_mode):04o}, not root 0644")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"{path} is missing: {exc}")
    return ""


def find_chromium(roots: list[Path] | tuple[str, ...]) -> list[str]:
    """Every executable named like a Chromium under ROOTS, and every
    Playwright browser directory. Names only: an npm package such as
    chromium-bidi is a protocol library, not a browser."""
    found: list[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
            for name in dirnames:
                if PLAYWRIGHT_DIR.match(name):
                    found.append(os.path.join(dirpath, name))
            for name in filenames:
                path = os.path.join(dirpath, name)
                if name in CHROMIUM_NAMES and os.access(path, os.X_OK):
                    found.append(path)
    return sorted(found)


def check_no_chromium() -> None:
    on_path = sorted({p for p in (shutil.which(n) for n in CHROMIUM_NAMES) if p})
    installed = find_chromium(SEARCH_ROOTS)
    if on_path or installed:
        fail(f"a Chromium is in the image, which must have none: {on_path + installed}")


def check_claude() -> None:
    if not shutil.which("claude"):
        fail("the claude CLI the profile runs is not on PATH")


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} is not set; the Dockerfile's RUN passes it from its ARG")
    return value


def main() -> int:
    version = _required("LIGHTPANDA_VERSION")
    check_owner_and_mode(BINARY)
    check_version(BINARY, version)
    check_switches()
    check_notices(DOC, version, _required("LIGHTPANDA_COMMIT"), _required("LIGHTPANDA_SOURCE_URL"),
                  _required("LIGHTPANDA_SOURCE_SHA256"))
    check_no_chromium()
    check_claude()
    print(f"agent-runtime-lightpanda self-test: Lightpanda {version} at {BINARY}, notices in {DOC}, no Chromium")
    return 0


if __name__ == "__main__":
    sys.exit(main())
