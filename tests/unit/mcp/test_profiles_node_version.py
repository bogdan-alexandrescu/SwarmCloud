"""`swarm_profiles` and `swarm profiles` say which Node each profile runs (#3929).

The runner images take Node from one pinned `NODE_IMAGE` in
images/agent-runtime-base/Dockerfile, and the build asserts `node --version`
equals the `ARG NODE_VERSION=` beside it. A brief that needs a Node feature
had no way to learn that version from the catalogue: `GET /v1/runtimes` is
served it per profile as `node_version`, and the bridge's allow-list
(`_served_entry`) dropped every field it did not name. Its own copy
(`public_profile`, what the offline `swarm profiles` and an unreachable
platform answer from) carries the same fact, held here to the Dockerfile so
the pin and what the catalogue says cannot drift.

No credentials, no emulator: the served answer comes from a stub client.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from swarm_common.profiles import RUNNER_PROFILES

from swarm_mcp import cli
from swarm_mcp import profiles as catalogue

REPO = Path(__file__).resolve().parents[3]
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"


def _pinned_node_version() -> str:
    (version,) = re.findall(
        r"^ARG NODE_VERSION=(\S+)\s*$", BASE_DOCKERFILE.read_text(), re.MULTILINE
    )
    return version


def _served(**extra: Any) -> dict[str, Any]:
    """One `/v1/runtimes` entry as the platform serves it."""
    return {
        "name": "claude-code",
        "available": True,
        "backend": "gke_autopilot",
        "resolved_backend": "gke_autopilot",
        "resource_class": "standard",
        "resources": {"cpu_request": 2, "cpu_limit": 4, "memory_gib": 8, "disk_gib": 4, "units": 2},
        "timeout_seconds": 3600,
        "provider": "anthropic",
        **extra,
    }


class _Client:
    def __init__(self, served: dict[str, Any]) -> None:
        self._served = served

    def request(self, method: str, path: str, **_: Any) -> dict[str, Any]:
        assert (method, path) == ("GET", "/v1/runtimes")
        return {"runtimes": {"claude-code": self._served}}


def _only_profile(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["catalogue_source"] == catalogue.CATALOGUE_FROM_PLATFORM
    (entry,) = answer["profiles"]
    return entry


def test_swarm_profiles_passes_the_served_node_version_through():
    # MUTATION: an allow-list without `node_version` drops it, which is #3929.
    entry = _only_profile(catalogue.served_catalogue(_Client(_served(node_version="24.22.1"))))
    assert entry["node_version"] == "24.22.1"


def test_an_older_platform_s_entry_reads_none_never_the_copy_s():
    # A platform from before #3929 serves no `node_version`. The answer is the
    # platform's or None: the copy's would be another deployment's image.
    entry = _only_profile(catalogue.served_catalogue(_Client(_served())))
    assert "node_version" in entry
    assert entry["node_version"] is None


def test_the_bridge_copy_states_the_node_the_base_dockerfile_pins():
    pinned = _pinned_node_version()
    assert catalogue.public_profile("claude-code")["node_version"] == pinned
    for name, profile in RUNNER_PROFILES.items():
        entry = catalogue.public_profile(name)
        if profile.control_plane:
            # No container runs it, so there is no Node to state.
            assert entry["node_version"] is None, name
        else:
            # Every runner image is built FROM agent-runtime-base, so each
            # carries the base's Node; a new image without an entry fails here.
            assert entry["node_version"] == pinned, name
        # The version, never the image it was looked up by (invariant 10).
        assert "image" not in entry, name


def test_the_control_plane_profiles_are_covered():
    # Guards the loop above from passing vacuously.
    assert any(p.control_plane for p in RUNNER_PROFILES.values())


def test_swarm_profiles_cli_prints_the_node_version(capsys):
    assert cli.cmd_profiles(None, argparse.Namespace(json=False)) == cli.EXIT_OK
    out = capsys.readouterr().out
    (row,) = [ln for ln in out.splitlines() if re.match(r"\s+\S+\s+claude-code\s", ln)]
    # MUTATION: a row that leaves the version out turns this red.
    assert f"node {_pinned_node_version()}" in row
