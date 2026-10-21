"""`swarm_profiles` shows the runner size version the platform serves (SIZES-MCP, part of #1689).

docs/design/runner-sizes.md §4.5, §4.7 and §6 (SIZES-MCP): since runner sizes
became an admin setting, a class's CPU request, CPU limit, memory and units
are whatever the CURRENT size version says, and `GET /v1/runtimes` serves each
profile's `resources` from it -- the current version for a GKE profile, the
code defaults (version 0) for a Cloud Run one. The bridge read `cpu`,
`memory_gib` and `disk_gib` out of that and dropped the rest, so a session
choosing a profile saw no CPU limit, no units and no size version, and could
not tell a resized class from the code table.

The resize goes through the REAL swarm-api admin route into an in-memory
Firestore and is read back through the real `/v1/runtimes`: the defect is a
disagreement between the bridge and the route, and a fake would agree with
whoever wrote it. No credentials, no emulator.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.profiles import RUNNER_PROFILES, default_sizes, effective_size, resolve_backend

from swarm_mcp import profiles as catalogue
from swarm_mcp import server
from swarm_mcp.client import SwarmError

from control_plane.conftest import ADMIN_GROUP, ENG_GROUP, api_settings, seed_tenant
from control_plane.fakes import FakeFirestore

#: Built at run time, never a literal (CLAUDE.md, "Nothing you add may look
#: like a credential").
ADMIN_TOKEN = "t-" + secrets.token_hex(8)
MEMBER_TOKEN = "t-" + secrets.token_hex(8)

#: Version 1 lets `standard` burst to 6 only.
VERSION_1 = {"standard": {"cpu_limit": 6}}
#: Version 2 grows `standard` and `browser` to 16 GiB, which is 2 units each;
#: `standard` keeps version 1's limit of 6 (a field the body does not name
#: keeps its value). The browser's live split grows its worker so the two
#: containers still sum to the class.
VERSION_2 = {
    "standard": {"memory_gib": 16},
    "browser": {"memory_gib": 16, "live_split": {"worker": {"memory_gib": 11}}},
}


class _Api:
    """The bridge's `request` seam over the real swarm-api application."""

    def __init__(self, api: TestClient, token: str) -> None:
        self._api = api
        self._headers = {"Authorization": f"Bearer {token}"}
        self.asked: list[str] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:  # noqa: ARG002
        self.asked.append(f"{method} {path}")
        response = self._api.request(method, path, json=payload, headers=self._headers)
        if response.status_code >= 400:
            raise SwarmError(f"{method} {path} -> {response.status_code}", status=response.status_code)
        return response.json()


@pytest.fixture()
def api() -> TestClient:
    db = FakeFirestore()
    identities = {
        ADMIN_TOKEN: ("root@example.com", (ADMIN_GROUP, ENG_GROUP)),
        MEMBER_TOKEN: ("alice@example.com", (ENG_GROUP,)),
    }
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier({
            token: {"email": email, "email_verified": True, "sub": f"sub-{email}", "hd": "example.com"}
            for token, (email, _) in identities.items()
        }),
        groups=StaticGroups({email: groups for email, groups in identities.values()}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    seed_tenant(db, "eng")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _resize(api: TestClient, classes: dict[str, Any], base: int) -> None:
    response = api.put(
        "/v1/admin/runner-sizes",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"base_version": base, "reason": "SIZES-MCP test", "classes": classes},
    )
    assert response.status_code == 200, response.text


def _profiles(client: Any) -> dict[str, Any]:
    return json.loads(server._call(client, "swarm_profiles", {}))


def _entry(listing: dict[str, Any], name: str) -> dict[str, Any]:
    (entry,) = [e for e in listing["profiles"] if e["name"] == name]
    return entry


def test_the_premise_claude_code_follows_the_setting_and_mock_does_not():
    """Without this the tests below could pass on a profile that never follows."""
    assert resolve_backend(RUNNER_PROFILES["claude-code"]).value == "GKE_AUTOPILOT"
    assert resolve_backend(RUNNER_PROFILES["mock"]).value == "CLOUD_RUN_JOB"
    assert RUNNER_PROFILES["claude-code"].resource_class == "standard"
    assert RUNNER_PROFILES["mock"].resource_class == "standard"


def test_swarm_profiles_shows_version_2s_sizes_for_a_gke_profile(api):
    _resize(api, VERSION_1, base=0)
    _resize(api, VERSION_2, base=1)
    client = _Api(api, MEMBER_TOKEN)

    listing = _profiles(client)

    assert client.asked == ["GET /v1/runtimes"]
    assert listing["catalogue_source"] == catalogue.CATALOGUE_FROM_PLATFORM
    code = _entry(listing, "claude-code")
    # MUTATION: `_served_entry` dropping the served `size_version` turns this red.
    assert code["size_version"] == 2
    # MUTATION: `_served_entry` dropping the served `cpu_limit` (or reading it
    # from the bridge's RESOURCE_CLASSES, which says 8) turns this red.
    assert code["cpu_limit"] == 6
    # MUTATION: `_served_entry` dropping the served `cpu_request` turns this red.
    assert code["cpu_request"] == 4
    # MUTATION: `cpu` read from anything but the served request turns this red.
    assert code["cpu"] == code["cpu_request"]
    # MUTATION: `memory_gib` read from the bridge's copy (8) turns this red.
    assert code["memory_gib"] == 16
    # MUTATION: `_served_entry` dropping the served `units`, or the copy's
    # code-table 1 standing in for them, turns this red.
    assert code["units"] == 2
    # MUTATION: `workspace_gib` no longer read from the served `disk_gib` turns this red.
    assert code["workspace_gib"] == 4


def test_swarm_profiles_shows_a_cloud_run_profile_at_the_code_defaults(api):
    """Owner Q3: a Cloud Run profile does not follow a resize, and says so."""
    _resize(api, VERSION_1, base=0)
    _resize(api, VERSION_2, base=1)

    mock = _entry(_profiles(_Api(api, MEMBER_TOKEN)), "mock")

    default = default_sizes()["standard"]
    # MUTATION: the bridge stamping the platform's current version on every
    # entry instead of the entry's own served `size_version` turns this red.
    assert mock["size_version"] == 0
    # MUTATION: the bridge applying version 2 to a Cloud Run profile turns these red.
    assert mock["cpu_limit"] == default.cpu_limit
    assert mock["memory_gib"] == default.memory_gib
    assert mock["units"] == default.units


def test_with_no_version_written_every_served_profile_reads_version_0(api):
    listing = _profiles(_Api(api, MEMBER_TOKEN))

    entries = listing["profiles"]
    # Counted, so an empty listing cannot pass the loop below vacuously.
    assert len(entries) == len(RUNNER_PROFILES), [e["name"] for e in entries]
    for entry in entries:
        profile = RUNNER_PROFILES[entry["name"]]
        size = effective_size(profile.resource_class, None, resolve_backend(profile))
        # MUTATION: dropping `size_version` (or serving None for the defaults) turns this red.
        assert entry["size_version"] == 0, entry["name"]
        # MUTATION: dropping `cpu_limit` from the served allow-list turns this red.
        assert entry["cpu_limit"] == size.cpu_limit, entry["name"]
        assert entry["cpu_request"] == size.cpu_request, entry["name"]


class _Runtimes:
    """A platform that serves one fixed `/v1/runtimes` answer."""

    def __init__(self, runtimes: dict[str, Any]) -> None:
        self._runtimes = runtimes

    def request(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ARG002
        if path == "/v1/runtimes":
            return {"runtimes": self._runtimes}
        raise SwarmError(f"{method} {path} -> 404", status=404)


def _served(resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "claude-code", "available": True, "resolved_backend": "GKE_AUTOPILOT",
        "resource_class": "standard", "provider": "anthropic", "timeout_seconds": 3600,
        "resources": resources,
    }


def test_a_platform_older_than_size_versions_is_said_not_guessed():
    """A deployment from before SIZES-API serves `cpu` alone. Its limit, units
    and version were never served, so they are None -- not the bridge's code
    table, which is a different deployment's answer."""
    old = _served({"cpu": 4, "memory_gib": 8, "disk_gib": 4})

    code = _entry(catalogue.served_catalogue(_Runtimes({"claude-code": old})), "claude-code")

    # MUTATION: `cpu_request` not falling back to the served `cpu` turns this red.
    assert code["cpu_request"] == code["cpu"] == 4
    # MUTATION: guessing the limit (the request, or the code table's 8) turns this red.
    assert code["cpu_limit"] is None
    # MUTATION: guessing units or a version for a platform that served none turns these red.
    assert code["units"] is None
    assert code["size_version"] is None


def test_a_platform_that_drops_the_cpu_alias_still_reads_the_request():
    """`cpu` is kept "for one release" (design §4.5); the bridge reads the
    request by its own name first, so the alias going away changes nothing."""
    new = _served({"cpu_request": 3, "cpu_limit": 6, "memory_gib": 6, "disk_gib": 4,
                   "units": 1, "size_version": 5})

    code = _entry(catalogue.served_catalogue(_Runtimes({"claude-code": new})), "claude-code")

    # MUTATION: reading `cpu` alone (absent here) turns this red.
    assert code["cpu"] == code["cpu_request"] == 3
    assert code["cpu_limit"] == 6
    assert code["size_version"] == 5


def test_the_bridge_copy_says_its_sizes_are_the_code_defaults():
    """With the platform unreachable the copy still answers, at version 0,
    and says an admin may have resized a GKE profile's class since."""
    class _Down:
        def request(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ARG002
            raise SwarmError("the API could not be reached")

    listing = _profiles(_Down())

    assert listing["catalogue_source"].startswith("bridge copy (swarm-mcp")
    entries = listing["profiles"]
    assert len(entries) == len(RUNNER_PROFILES)
    for entry in entries:
        profile = RUNNER_PROFILES[entry["name"]]
        size = effective_size(profile.resource_class, None, resolve_backend(profile))
        # MUTATION: `public_profile` serving no `size_version` (or one other
        # than 0, the code defaults) turns this red.
        assert entry["size_version"] == 0, entry["name"]
        # MUTATION: `public_profile` serving no `cpu_limit`/`cpu_request`/`units` turns these red.
        assert entry["cpu_limit"] == size.cpu_limit, entry["name"]
        assert entry["cpu_request"] == entry["cpu"] == size.cpu_request, entry["name"]
        assert entry["units"] == size.units, entry["name"]
    # MUTATION: a note that does not say the sizes are the code defaults turns this red.
    assert "code defaults" in listing["catalogue_note"], listing["catalogue_note"]
    assert "size version 0" in listing["catalogue_note"], listing["catalogue_note"]
