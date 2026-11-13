"""The Node version each runner image carries (#3929).

A brief that relies on a Node release -- `node --version` at 24.22.1 or later --
needs to know what a `runner_profile` NAME runs on. The frozen catalogue
(`swarm_common.profiles.RUNNER_PROFILES`) has no toolchain field; adding one is
a contract change (requested in #3929's pull request), so until it lands this
module is where swarm-api holds the fact and `GET /v1/runtimes` serves it.

THE SOURCE IS `ARG NODE_VERSION=` in images/agent-runtime-base/Dockerfile,
which the base's build asserts `node --version` equals. The three images built
FROM the base by digest install no Node of their own (the browser image links
Playwright's driver to the base's /usr/local/bin/node), so all four carry the
base's. tests/unit/control_plane/test_runtimes_node_version.py holds every
value here equal to that ARG and requires an entry for every image a profile
names, so re-pinning the base without this table, or adding an image without
an entry, fails a test rather than serving a wrong or missing version.
"""

from __future__ import annotations

from swarm_common.profiles import RunnerProfile

# images/agent-runtime-base/Dockerfile `ARG NODE_VERSION` -- the test holds it.
_BASE_NODE = "24.22.3"

NODE_VERSIONS: dict[str, str] = {
    "agent-runtime-base": _BASE_NODE,
    "agent-runtime-browser": _BASE_NODE,
    "agent-runtime-indexer": _BASE_NODE,
    "agent-runtime-lightpanda": _BASE_NODE,
}


def node_version_for(profile: RunnerProfile) -> str | None:
    """The Node `profile`'s image carries; None for a `control_plane` profile,
    which no container runs, so its `image` is never pulled."""
    if profile.control_plane:
        return None
    return NODE_VERSIONS.get(profile.image)
