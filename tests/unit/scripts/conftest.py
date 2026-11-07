"""Wiring for the script tests in tests/unit/scripts.

scripts/lib/common.sh has no default project, region or zone (DAYONE-DESAGA-2;
docs/design/generic-repository.md, section 2.2): sourcing it with any of them
unset is an error. And once a project is named, it reads that deployment's
never-touch list from config/<env>/deny-list.json and stops when it cannot
(section 2.4). So this package names the placeholder deployment of section 3.1
-- `example-project`, a public region and zone -- and serves it the placeholder
list, scripts/lib/deny-list.example.json, the way config_fetch reads a list
offline: from a SWARM_DEPLOY_CONFIG_DIR laid out as <dir>/<env>/deny-list.json,
owned by this user and not group- or world-writable, with a cache of its own.
No test here names, or reads the list of, a real deployment. A test about the
defaults or the loader itself scrubs these (test_common_defaults.py,
test_shared_deny_list.py).

Package-scoped, not set on os.environ at import: an import-time default reached
every test in the worker, the in-process apps included, and quota-broker's
create_app reads PROJECT_ID to decide whether it has a secret store. Package
scope also covers the module-scoped fixtures here that run a script, which a
function-scoped fixture would set up too late for.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_DENY_LIST = REPO / "scripts" / "lib" / "deny-list.example.json"

#: The placeholder deployment (docs/design/generic-repository.md, section 3.1).
PLACEHOLDER_DEPLOYMENT = {
    "PROJECT_ID": "example-project",
    "REGION": "us-central1",
    "ZONE": "us-central1-a",
}


def write_placeholder_deny_list(config_dir: Path, project: str, envs: list[str]) -> None:
    """The example list, written for PROJECT, under CONFIG_DIR/<env>/ for each env."""
    body = json.loads(EXAMPLE_DENY_LIST.read_text())
    body["project"] = project
    for env in envs:
        target = config_dir / env
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        path = target / "deny-list.json"
        path.write_text(json.dumps(body, indent=2) + "\n")
        path.chmod(0o600)


@pytest.fixture(scope="package", autouse=True)
def _scripts_name_their_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    root = tmp_path_factory.mktemp("placeholder-deployment")
    root.chmod(0o700)
    # dev and prod are the environments the repository's tfvars examples name;
    # an ENVIRONMENT the caller already set gets its list too.
    envs = sorted({"dev", "prod", os.environ.get("ENVIRONMENT") or "dev"})
    write_placeholder_deny_list(root / "config", PLACEHOLDER_DEPLOYMENT["PROJECT_ID"], envs)
    (root / "cache").mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as mp:
        for name, value in PLACEHOLDER_DEPLOYMENT.items():
            mp.setenv(name, value)
        mp.setenv("SWARM_DEPLOY_CONFIG_DIR", str(root / "config"))
        mp.setenv("XDG_CACHE_HOME", str(root / "cache"))
        yield
