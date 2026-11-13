"""`GET /v1/runtimes` says which Node each runner image carries (#3929).

A brief that needs a particular Node -- `node --version` at 24.22.1 or later,
say -- had no way to learn what a `runner_profile` NAME runs on short of reading
`images/agent-runtime-base/Dockerfile`. The frozen catalogue
(`swarm_common.profiles.RUNNER_PROFILES`) has no toolchain field, so swarm-api
holds the fact in `swarm_api.runnerimages` and serves it per profile as
`node_version`.

These tests pin the three properties that make the field worth reading:

  * it equals the `ARG NODE_VERSION=` the base image's build asserts
    `node --version` against, so the pin, the build check and the catalogue
    cannot drift apart;
  * it is null for a `control_plane` profile, which never runs an image, so the
    field never claims a Node that no container of that profile carries;
  * the table names every image any profile uses, so a new image cannot be
    served without a version.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import runnerimages

from .conftest import auth_header

REPO = Path(__file__).resolve().parents[3]
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"


def _dockerfile_node_version() -> str:
    found = re.findall(
        r"^ARG NODE_VERSION=(\S+)\s*$", BASE_DOCKERFILE.read_text(), flags=re.MULTILINE
    )
    assert len(found) == 1, f"expected one `ARG NODE_VERSION=` in {BASE_DOCKERFILE}, found {found}"
    return found[0]


def _runtimes(client) -> dict:
    response = client.get("/v1/runtimes", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["runtimes"]


def test_every_profile_that_runs_an_image_serves_the_dockerfiles_node(client):
    expected = _dockerfile_node_version()
    served = _runtimes(client)
    runs_an_image = [name for name, p in RUNNER_PROFILES.items() if not p.control_plane]
    # The profiles the issue names; a rename should retarget this test, not
    # silently shrink what it checks.
    for name in ("claude-code", "claude-code-large", "claude-code-browser", "codex",
                 "browser", "indexer", "claude-code-lightpanda"):
        assert name in runs_an_image, f"{name} is no longer a profile that runs an image"
    for name in runs_an_image:
        assert "node_version" in served[name], f"{name} serves no node_version"
        assert served[name]["node_version"] == expected, (
            f"{name} serves node {served[name]['node_version']!r}; "
            f"{BASE_DOCKERFILE.relative_to(REPO)} pins {expected!r}"
        )


def test_a_control_plane_profile_serves_null(client):
    served = _runtimes(client)
    control_plane = [name for name, p in RUNNER_PROFILES.items() if p.control_plane]
    assert {"await-input", "fix-loop"} <= set(control_plane)
    for name in control_plane:
        assert "node_version" in served[name], f"{name} serves no node_version"
        assert served[name]["node_version"] is None, (
            f"{name} never runs an image, yet serves node {served[name]['node_version']!r}"
        )


def test_the_table_names_every_image_a_profile_uses():
    used = {p.image for p in RUNNER_PROFILES.values()}
    missing = used - set(runnerimages.NODE_VERSIONS)
    assert not missing, f"runner images with no Node version in swarm_api.runnerimages: {missing}"


def test_every_image_in_the_table_carries_the_bases_node():
    # agent-runtime-browser, -lightpanda and -indexer are built FROM the base by
    # digest and install no Node of their own.
    expected = _dockerfile_node_version()
    assert runnerimages.NODE_VERSIONS, "the table is empty"
    for image, version in runnerimages.NODE_VERSIONS.items():
        assert version == expected, f"{image}: {version!r} != the Dockerfile's {expected!r}"


def test_node_version_for_is_none_only_for_control_plane():
    for profile in RUNNER_PROFILES.values():
        version = runnerimages.node_version_for(profile)
        if profile.control_plane:
            assert version is None, profile.name
        else:
            assert version == runnerimages.NODE_VERSIONS[profile.image], profile.name
