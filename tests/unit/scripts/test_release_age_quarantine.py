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
