"""The catalogue serves what a live-browser agent's container gets, not the pod's total (#3118).

`claude-code-browser` runs as a live-browser pod of two containers, the
worker and the display, whose memories `swarm_common.profiles`' live split
holds to the class's (`ResourceSize.live_split`, design runner-sizes §4.3).
`GET /v1/runtimes` and `swarm_profiles` served only the class's
`memory_gib`, the pod's total: on 3 November 2026 they read 10 GiB while the
agent's container was killed at 5, so a caller sized a step for 10 and was
OOM-killed at 5.

Now each profile's entry carries, beside `resources` (the CLASS, which stays
field-identical to `/v1/resource-classes`), `agent_memory_gib` (what a step's agent
gets: the worker's share on a live pod, the whole size otherwise),
`display_memory_gib` (the display's share, null with no display) and
`pod_memory_gib` (the total, which `resources.memory_gib` stays, since admission and the
units charge it). The display is on exactly when swarm-api has a relay
address: Terraform sets SWARM_LIVE_URL and the scheduler's
LIVE_BROWSER_SIDECAR from the one `live_relay_enabled` (terraform/infra
locals.tf), so neither is on without the other.

The resize goes through the REAL admin route into an in-memory Firestore and
is read back through the real `/v1/runtimes`. No credentials, no emulator.
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
from swarm_common.profiles import RUNNER_PROFILES, default_sizes

from swarm_mcp import cli
from swarm_mcp import profiles as catalogue
from swarm_mcp import server
from swarm_mcp.client import SwarmError

from control_plane.conftest import ADMIN_GROUP, ENG_GROUP, api_settings, seed_tenant
from control_plane.fakes import FakeFirestore

#: Built at run time, never a literal (CLAUDE.md, "Nothing you add may look
#: like a credential").
ADMIN_TOKEN = "t-" + secrets.token_hex(8)
MEMBER_TOKEN = "t-" + secrets.token_hex(8)

#: The live deployment's browser class on 3 November 2026: 10 GiB, the worker
#: and the display 5 each (the display keeps its default 5).
TEN_GIB = {"browser": {"memory_gib": 10, "live_split": {"worker": {"memory_gib": 5}}}}

#: Any relay address: only whether one is set matters to the catalogue.
RELAY = "ws://10.0.0.9:8080"

LIVE = "claude-code-browser"


class _Api:
    """The bridge's `request` seam over the real swarm-api application."""

    def __init__(self, api: TestClient, token: str) -> None:
        self._api = api
        self._headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:  # noqa: ARG002
        response = self._api.request(method, path, json=payload, headers=self._headers)
        if response.status_code >= 400:
            raise SwarmError(f"{method} {path} -> {response.status_code}", status=response.status_code)
        return response.json()


def _app(relay: str) -> TestClient:
    db = FakeFirestore()
    identities = {
        ADMIN_TOKEN: ("root@example.com", (ADMIN_GROUP, ENG_GROUP)),
        MEMBER_TOKEN: ("alice@example.com", (ENG_GROUP,)),
    }
    ctx = build_context(
        settings=api_settings(live_relay_url=relay),
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


@pytest.fixture()
def live() -> TestClient:
    return _app(RELAY)


@pytest.fixture()
def unlit() -> TestClient:
    return _app("")


def _resize(api: TestClient, classes: dict[str, Any]) -> None:
    response = api.put(
        "/v1/admin/runner-sizes",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"base_version": 0, "reason": "#3118 test", "classes": classes},
    )
    assert response.status_code == 200, response.text


def _runtimes(api: TestClient) -> dict[str, Any]:
    response = api.get("/v1/runtimes", headers={"Authorization": f"Bearer {MEMBER_TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()["runtimes"]


def _entry(listing: dict[str, Any], name: str) -> dict[str, Any]:
    (entry,) = [e for e in listing["profiles"] if e["name"] == name]
    return entry


def test_the_premise_the_live_profile_is_on_the_split_class_and_browser_is_not_live():
    """Without this the tests below could pass on a profile with no split."""
    assert RUNNER_PROFILES[LIVE].live_browser is True
    assert RUNNER_PROFILES[LIVE].resource_class == "browser"
    assert default_sizes()["browser"].live_split is not None
    assert RUNNER_PROFILES["browser"].live_browser is False
    assert RUNNER_PROFILES["browser"].resource_class == "browser"


def _memory(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry's three memory figures beside its class's `memory_gib`."""
    out = {k: entry[k] for k in ("agent_memory_gib", "display_memory_gib", "pod_memory_gib")}
    return {**out, "memory_gib": entry["resources"]["memory_gib"]}


def test_the_route_serves_the_worker_share_as_the_agent_s_memory(live):
    _resize(live, TEN_GIB)

    resources = _memory(_runtimes(live)[LIVE])

    # MUTATION: serving the class's total (10) as what the agent gets, the
    # defect of #3118, turns this red.
    assert resources["agent_memory_gib"] == 5
    # MUTATION: dropping the display's share, or serving the worker's, turns this red.
    assert resources["display_memory_gib"] == 5
    # The total stays, under its own name and the old one: admission and the
    # units charge it.
    assert resources["pod_memory_gib"] == resources["memory_gib"] == 10
    assert resources["agent_memory_gib"] + resources["display_memory_gib"] == resources["pod_memory_gib"]


def test_with_no_version_the_route_serves_the_default_split(live):
    worker, display = default_sizes()["browser"].live_split

    resources = _memory(_runtimes(live)[LIVE])

    # MUTATION: a literal 5 (the live deployment's) instead of the split turns this red.
    assert resources["agent_memory_gib"] == worker.memory_gib
    assert resources["display_memory_gib"] == display.memory_gib
    assert resources["pod_memory_gib"] == default_sizes()["browser"].memory_gib


def test_a_pod_with_no_display_gives_the_agent_the_whole_size(live, unlit):
    _resize(live, TEN_GIB)
    _resize(unlit, TEN_GIB)

    # The `browser` profile runs no live display: one container, all of it.
    plain = _memory(_runtimes(live)["browser"])
    # MUTATION: applying the split to every profile on the class turns this red.
    assert plain["agent_memory_gib"] == plain["pod_memory_gib"] == 10
    assert plain["display_memory_gib"] is None

    # With no relay the scheduler renders no display sidecar either.
    dark = _memory(_runtimes(unlit)[LIVE])
    # MUTATION: serving the split whatever the deployment runs turns this red.
    assert dark["agent_memory_gib"] == dark["pod_memory_gib"] == 10
    assert dark["display_memory_gib"] is None


def test_every_profile_serves_the_three_numbers(live):
    runtimes = _runtimes(live)

    # Counted, so an empty listing cannot pass the loop vacuously.
    assert len(runtimes) == len(RUNNER_PROFILES)
    for name, entry in runtimes.items():
        resources = _memory(entry)
        assert resources["pod_memory_gib"] == resources["memory_gib"], name
        if name == LIVE:
            continue
        assert resources["agent_memory_gib"] == resources["memory_gib"], name
        assert resources["display_memory_gib"] is None, name


def test_swarm_profiles_serves_what_the_agent_gets(live):
    _resize(live, TEN_GIB)

    listing = json.loads(server._call(_Api(live, MEMBER_TOKEN), "swarm_profiles", {}))

    assert listing["catalogue_source"] == catalogue.CATALOGUE_FROM_PLATFORM
    entry = _entry(listing, LIVE)
    # MUTATION: `_served_entry` dropping the served agent memory turns this red.
    assert entry["agent_memory_gib"] == 5
    assert entry["display_memory_gib"] == 5
    assert entry["pod_memory_gib"] == entry["memory_gib"] == 10


def test_a_platform_older_than_the_split_is_said_not_guessed():
    """A deployment from before #3118 serves no agent memory: the bridge says
    None rather than offering the total as what the agent gets."""
    old = {
        "name": LIVE, "available": True, "resolved_backend": "GKE_AUTOPILOT",
        "resource_class": "browser", "provider": "anthropic", "timeout_seconds": 3600,
        "resources": {"cpu_request": 4, "cpu_limit": 8, "memory_gib": 10, "disk_gib": 4,
                      "units": 2, "size_version": 1},
    }

    class _Runtimes:
        def request(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ARG002
            return {"runtimes": {LIVE: old}}

    entry = _entry(catalogue.served_catalogue(_Runtimes()), LIVE)

    # MUTATION: filling the agent's memory in from the served total turns this red.
    assert entry["agent_memory_gib"] is None
    assert entry["display_memory_gib"] is None
    assert entry["pod_memory_gib"] == 10


def test_the_bridge_copy_serves_the_default_split_for_the_live_profile():
    worker, display = default_sizes()["browser"].live_split

    copy = catalogue.public_profile(LIVE)

    # MUTATION: the copy serving the class's total as the agent's turns this red.
    assert copy["agent_memory_gib"] == worker.memory_gib
    assert copy["display_memory_gib"] == display.memory_gib
    assert copy["pod_memory_gib"] == copy["memory_gib"] == default_sizes()["browser"].memory_gib
    plain = catalogue.public_profile("browser")
    assert plain["agent_memory_gib"] == plain["memory_gib"]
    assert plain["display_memory_gib"] is None


def test_the_tool_description_says_which_number_to_size_a_step_by():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_profiles"]

    # MUTATION: a description that names only "memory" leaves a caller reading
    # the pod's total as the agent's, which is #3118.
    assert "agent_memory_gib" in tool["description"]


def test_swarm_profiles_cli_prints_the_agent_s_memory_beside_the_pod_s(capsys):
    worker, display = default_sizes()["browser"].live_split

    class _Args:
        json = False

    cli.cmd_profiles(None, _Args())

    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if f" {LIVE} " in ln]
    # MUTATION: printing the class's `memory_gib` alone turns this red.
    assert f"agent {worker.memory_gib} GiB" in line
    assert f"display {display.memory_gib}" in line
