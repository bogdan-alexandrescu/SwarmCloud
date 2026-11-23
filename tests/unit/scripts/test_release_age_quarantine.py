"""A 14-day release-age quarantine holds for uv and npm (RELEASE-AGE).

Audit "Add a 14-day release-age quarantine for uv and npm", part of #60. A
resolution only takes releases at least 14 days old, a rolling window: uv's
[tool.uv] exclude-newer takes a duration and records only the span in
uv.lock; npm's min-release-age in apps/swarm-ui/.npmrc is a number of days.
Installs from the lockfiles (`uv sync --frozen`, `npm ci`) are unaffected.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DAYS = 14


def test_uv_resolves_only_distributions_at_least_14_days_old():
    uv = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["uv"]
    assert uv.get("exclude-newer") == f"{DAYS} days", (
        "[tool.uv] exclude-newer must be the rolling 14-day window, not a fixed date"
    )


def test_uv_lock_records_the_same_span_so_lock_check_stays_green():
    # `uv lock --check` (CI) fails when the lock's span differs from the config's.
    options = tomllib.loads((REPO / "uv.lock").read_text()).get("options", {})
    assert options.get("exclude-newer-span") == f"P{DAYS}D", (
        "uv.lock must record exclude-newer-span = \"P14D\": run `uv lock`"
    )


def test_npm_resolves_only_versions_at_least_14_days_old_for_swarm_ui():
    npmrc = REPO / "apps" / "swarm-ui" / ".npmrc"
    settings = [
        line.strip() for line in npmrc.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    ages = [s for s in settings if re.match(r"min-release-age\s*=", s)]
    assert ages == [f"min-release-age={DAYS}"], settings
    # `before` wins over min-release-age within one source: a fixed date here
    # would silently replace the rolling window.
    assert not any(re.match(r"before\s*=", s) for s in settings), settings


# Issue #189: the deployed images build from the per-app locks, not the root
# one, so every project with its own uv.lock carries the same window.
def _tracked_uv_locks() -> list[Path]:
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*uv.lock"],
        cwd=REPO, check=True, capture_output=True, text=True,
    ).stdout
    return sorted(REPO / p for p in out.split("\0") if p.endswith("uv.lock"))


def test_every_tracked_uv_lock_records_the_14_day_span():
    locks = _tracked_uv_locks()
    # The root lock and the per-app ones: an empty list would pass vacuously.
    assert len(locks) > 1, locks
    missing = [
        str(lock.relative_to(REPO)) for lock in locks
        if tomllib.loads(lock.read_text()).get("options", {}).get(
            "exclude-newer-span") != f"P{DAYS}D"
    ]
    assert not missing, (
        f"these locks lack exclude-newer-span = \"P{DAYS}D\": run `uv lock` "
        f"in their directory: {missing}"
    )


def test_every_project_with_its_own_lock_sets_the_window():
    wrong = []
    for lock in _tracked_uv_locks():
        project = tomllib.loads((lock.parent / "pyproject.toml").read_text())
        window = project.get("tool", {}).get("uv", {}).get("exclude-newer")
        if window != f"{DAYS} days":
            wrong.append((str(lock.parent.relative_to(REPO)), window))
    assert not wrong, wrong


UI = REPO / "apps" / "swarm-ui"


def test_console_image_has_no_unlocked_npm_install_fallback():
    # `npm ci || npm install` let a lock mismatch resolve afresh, and without
    # apps/swarm-ui/.npmrc in the build context at that step, outside the window.
    dockerfile = (REPO / "images" / "swarm-ui" / "Dockerfile").read_text()
    runs = [
        line for line in dockerfile.splitlines()
        if not line.lstrip().startswith("#") and "npm" in line
    ]
    assert any(re.search(r"\bnpm ci\b", line) for line in runs), runs
    assert not any(re.search(r"\bnpm (install|i)\b", line) for line in runs), runs
    assert not any("||" in line for line in runs if "npm ci" in line), runs


def test_console_dependencies_are_exact_pins():
    import json

    manifest = json.loads((UI / "package.json").read_text())
    ranged = {
        name: spec
        for section in ("dependencies", "devDependencies")
        for name, spec in manifest.get(section, {}).items()
        if spec.startswith(("^", "~"))
    }
    assert not ranged, ranged


def test_console_lock_root_entry_equals_package_json():
    import json

    manifest = json.loads((UI / "package.json").read_text())
    lock = json.loads((UI / "package-lock.json").read_text())
    root = lock["packages"][""]
    for section in ("dependencies", "devDependencies"):
        assert root.get(section, {}) == manifest.get(section, {}), section
    for name, spec in manifest["dependencies"].items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == spec, name
    for name, spec in manifest["devDependencies"].items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == spec, name
