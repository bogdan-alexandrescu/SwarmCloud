"""Every google-cloud client a service imports must be declared by that service.

WHY THIS TEST EXISTS
--------------------
`google-cloud-secret-manager` was missing from the quota broker's dependencies
for the entire life of its credential refresher. The import is lazy -- inside a
method, to keep it off the startup path -- so the service booted cleanly, served
traffic, passed every unit test, and failed only at the moment it tried to read
a secret. It reported "could not read the refresh credential" on every sweep,
with correct IAM and a correctly populated secret, and the refresher could never
have worked at all.

Nothing caught it. Unit tests inject fakes, so the real client is never
imported. Integration tests would have, and there are none for that path.
Deployment does not check. The only signal was a log line that reads like a
permissions problem.

A lazy import is a good idea -- `google-cloud-*` packages are slow to import and
a control-plane service should not pay for one it may never use -- so the answer
is not to stop doing it. The answer is to check that what is imported is
declared.

WHAT THIS DOES NOT COVER
------------------------
Only `from google.cloud import X`. That is where the failure was and where the
lazy-import pattern is used; a general import-vs-dependency checker is a much
larger thing and would mostly restate what `uv` already enforces for eager
imports.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
APPS = REPO / "apps"

#: `google.cloud.<module>` -> the distribution that provides it. Written out
#: rather than derived, because the mapping is genuinely irregular: `firestore`
#: comes from google-cloud-firestore but `run_v2` comes from google-cloud-run,
#: and `monitoring_v3` from google-cloud-monitoring.
DISTRIBUTION = {
    "firestore": "google-cloud-firestore",
    "secretmanager": "google-cloud-secret-manager",
    "storage": "google-cloud-storage",
    "run_v2": "google-cloud-run",
    "pubsub_v1": "google-cloud-pubsub",
    "monitoring_v3": "google-cloud-monitoring",
    "logging": "google-cloud-logging",
    "container_v1": "google-cloud-container",
}

_IMPORT = re.compile(r"^\s*from google\.cloud import ([a-z_0-9]+)", re.MULTILINE)


def _apps() -> list[Path]:
    return sorted(p for p in APPS.iterdir() if (p / "pyproject.toml").is_file())


def _declared(app: Path) -> set[str]:
    """Distributions this app declares, directly or through swarm-common."""
    data = tomllib.loads((app / "pyproject.toml").read_text())
    names = {
        re.split(r"[<>=!\[ ]", dep)[0].strip().lower()
        for dep in data.get("project", {}).get("dependencies", [])
    }
    # swarm-common is a path dependency; whatever it declares is available too.
    if "swarm-common" in names:
        common = APPS / "common" / "pyproject.toml"
        if common.is_file():
            shared = tomllib.loads(common.read_text())
            names |= {
                re.split(r"[<>=!\[ ]", dep)[0].strip().lower()
                for dep in shared.get("project", {}).get("dependencies", [])
            }
    return names


def _imported(app: Path) -> dict[str, list[str]]:
    """`google.cloud.<module>` -> the files importing it, tests excluded."""
    found: dict[str, list[str]] = {}
    for path in app.rglob("*.py"):
        if "test" in path.parts or path.name.startswith("test_"):
            continue
        for module in _IMPORT.findall(path.read_text()):
            found.setdefault(module, []).append(str(path.relative_to(REPO)))
    return found


@pytest.mark.parametrize("app", _apps(), ids=lambda p: p.name)
def test_every_google_cloud_import_is_a_declared_dependency(app: Path) -> None:
    declared = _declared(app)
    missing = []
    for module, files in sorted(_imported(app).items()):
        dist = DISTRIBUTION.get(module)
        assert dist is not None, (
            f"{app.name} imports google.cloud.{module}, which this test does not "
            f"know the distribution for. Add it to DISTRIBUTION -- an unmapped "
            f"module is an unchecked one."
        )
        if dist not in declared:
            missing.append(f"  google.cloud.{module} needs {dist!r}, imported by {files[0]}")

    assert not missing, (
        f"{app.name} imports google-cloud modules it does not declare:\n"
        + "\n".join(missing)
        + "\n\nThese imports are LAZY, so the service will start, serve traffic and "
        "pass every test with fakes -- then fail at the moment of use with an "
        "error that reads like a permissions problem."
    )


def test_the_module_to_distribution_map_covers_what_the_repo_actually_imports():
    """A module nobody mapped is a module nobody checks.

    The failure this guards is quiet: adding `from google.cloud import bigquery`
    would otherwise pass every assertion above by simply not being looked at.
    """
    unmapped = set()
    for app in _apps():
        unmapped |= set(_imported(app)) - set(DISTRIBUTION)
    assert not unmapped, (
        f"google.cloud modules with no known distribution: {sorted(unmapped)}. "
        "Add them to DISTRIBUTION in this file."
    )
