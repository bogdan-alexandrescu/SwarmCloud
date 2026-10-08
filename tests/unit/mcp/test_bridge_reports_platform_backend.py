"""The bridge reports the backend the PLATFORM used, not its own copy's guess.

MEASURED, 2026-10-08: PR 866 moved claude-code to GKE Autopilot and release
37757901721 deployed it -- the scheduler logged task_99a3ed51bc9749d0894e
`admission leased` on GKE_AUTOPILOT at 18:06:52Z -- yet `swarm_profiles` and
`swarm_result` both said CLOUD_RUN_JOB. Both computed the backend from the
bridge's own `swarm_common` copy, which is as old as the installed plugin tag
(sc-v0.5.24, before 866). The attempt record and `GET /v1/runtimes` are the
platform's answers; the copy is the fallback, and labelled.

`mock` is the stand-in for the drifted profile: the frozen catalogue resolves
it to CLOUD_RUN_JOB, and these attempts say GKE_AUTOPILOT, which is exactly
the disagreement the incident had.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_common.profiles import RUNNER_PROFILES, resolve_backend

from swarm_mcp import profiles as catalogue
from swarm_mcp import server
from swarm_mcp.client import SwarmError
from swarm_mcp.patches import (
    BACKEND_FROM_ATTEMPT,
    BACKEND_FROM_CATALOGUE,
    describe_task,
)

from test_follow_cursor import World, swarm, world  # noqa: F401 - fixtures

COPY_SAYS = resolve_backend(RUNNER_PROFILES["mock"]).value
PLATFORM_SAYS = "GKE_AUTOPILOT"


def test_the_premise_the_copy_and_the_platform_disagree():
    """Without this the tests below could pass by the copy agreeing."""
    assert COPY_SAYS != PLATFORM_SAYS


class _Attempts:
    """A client that serves one attempt list and records what it was asked."""

    def __init__(self, attempts: list[dict[str, Any]] | None = None, *, error: str | None = None):
        self._attempts = attempts or []
        self._error = error
        self.asked: list[tuple[str, int]] = []

    def attempts(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.asked.append((task_id, limit))
        if self._error:
            raise SwarmError(self._error)
        return self._attempts


def _task(**extra: Any) -> dict[str, Any]:
    return {"id": "task_a", "state": "SUCCEEDED", "runner_profile": "mock", "attempt_count": 1, **extra}


# --------------------------------------------------------------------------
# Task results
# --------------------------------------------------------------------------


def test_the_attempt_record_wins_over_the_bridge_copy():
    """THE MUTATION THIS CATCHES: put `backend_of(profile)` back as the
    result's backend, or read the catalogue before the attempt."""
    client = _Attempts([{"attempt_id": "att_1", "backend": PLATFORM_SAYS}])

    got = describe_task(_task(), client)

    assert got["backend"] == PLATFORM_SAYS
    assert got["backend_source"] == BACKEND_FROM_ATTEMPT == "attempt"
    assert "backend_note" not in got
    # One attempt, the newest: the route serves newest first.
    assert client.asked == [("task_a", 1)]


def test_a_last_attempt_the_api_serves_on_the_task_is_read_without_a_round_trip():
    client = _Attempts(error="must not be asked")

    got = describe_task(_task(last_attempt={"backend": PLATFORM_SAYS}), client)

    assert (got["backend"], got["backend_source"]) == (PLATFORM_SAYS, "attempt")
    assert client.asked == []


def test_a_task_with_no_attempt_falls_back_to_the_copy_and_says_so():
    """Labelled, with the reason: a reader must be able to tell a prediction
    from what ran. No round trip for a task never attempted."""
    client = _Attempts(error="must not be asked")

    got = describe_task(_task(state="QUEUED", attempt_count=0), client)

    assert got["backend"] == COPY_SAYS
    assert got["backend_source"] == BACKEND_FROM_CATALOGUE == "catalogue (bridge copy)"
    assert "no attempt yet" in got["backend_note"]
    assert "may place it elsewhere" in got["backend_note"]
    assert client.asked == []


def test_an_unreadable_attempts_route_falls_back_labelled_and_names_the_failure():
    got = describe_task(_task(), _Attempts(error="GET /v1/tasks/task_a/attempts -> 503"))

    assert got["backend"] == COPY_SAYS
    assert got["backend_source"] == "catalogue (bridge copy)"
    assert "503" in got["backend_note"]


def test_an_attempt_that_recorded_no_backend_is_not_a_backend():
    """The API serves `""` for an attempt whose backend was never written."""
    got = describe_task(_task(), _Attempts([{"attempt_id": "att_1", "backend": ""}]))

    assert (got["backend"], got["backend_source"]) == (COPY_SAYS, "catalogue (bridge copy)")


def test_no_client_falls_back_labelled():
    got = describe_task(_task())

    assert (got["backend"], got["backend_source"]) == (COPY_SAYS, "catalogue (bridge copy)")


def test_unknown_to_both_is_unknown_not_a_default():
    got = describe_task({"id": "task_y", "state": "SUCCEEDED", "runner_profile": "gone"})

    assert got["backend"] is None
    assert got["backend_source"] is None
    assert got["backend_note"]


def _attempted_on_gke(world: World) -> None:
    world.task("task_a", state="SUCCEEDED")
    world.db.docs["tasks/task_a"]["attempt_count"] = 1
    world.attempt("task_a", "att_1")
    world.db.docs["attempts/att_1"]["backend"] = PLATFORM_SAYS


def test_swarm_result_over_the_real_api_reports_the_attempts_backend(swarm, world):
    """End to end: the real client against the real application's attempt codec."""
    _attempted_on_gke(world)

    got = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))

    assert got["runner_profile"] == "mock"
    assert got["backend"] == PLATFORM_SAYS, f"the bridge copy's {COPY_SAYS} was reported"
    assert got["backend_source"] == "attempt"


def test_swarm_wait_reports_the_attempts_backend(swarm, world):
    _attempted_on_gke(world)

    got = json.loads(server._call(swarm, "swarm_wait", {"task_ids": ["task_a"], "timeout_seconds": 0}))

    [finished] = got["finished"]
    assert (finished["backend"], finished["backend_source"]) == (PLATFORM_SAYS, "attempt")


# --------------------------------------------------------------------------
# swarm_profiles
# --------------------------------------------------------------------------


class _Runtimes:
    def __init__(self, answer: Any = None, *, error: str | None = None):
        self._answer = answer
        self._error = error
        self.asked: list[str] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.asked.append(f"{method} {path}")
        if self._error:
            raise SwarmError(self._error)
        return self._answer


def _served(**over: Any) -> dict[str, Any]:
    entry = {
        "name": "mock",
        "available": True,
        "disabled_reason": None,
        "image": "agent-runtime-base",
        "backend": PLATFORM_SAYS,
        "resolved_backend": PLATFORM_SAYS,
        "provider": None,
        "secrets": ["SOME_NAME"],
        "secrets_any_of": False,
        "timeout_seconds": 600,
        "resource_class": "small",
        "resources": {"name": "small", "cpu": 1, "memory_gib": 2, "disk_gib": 1, "units": 1},
    }
    entry.update(over)
    return entry


def test_swarm_profiles_reads_the_platforms_catalogue():
    """THE MUTATION THIS CATCHES: serve `catalogue.catalogue()` again."""
    client = _Runtimes({"runtimes": {"mock": _served()}})

    got = json.loads(server._call(client, "swarm_profiles", {}))

    assert client.asked == ["GET /v1/runtimes"]
    assert got["catalogue_source"] == "platform (GET /v1/runtimes)"
    [mock] = got["profiles"]
    assert mock["backend"] == PLATFORM_SAYS
    # The disagreement is said, so a reader knows the plugin is behind.
    assert mock["bridge_copy_backend"] == COPY_SAYS
    # The same allow-list as the copy: the route's image and secrets stay out.
    assert "image" not in mock and "secrets" not in mock
    assert mock["workspace_gib"] == 1


def test_swarm_profiles_says_when_the_platform_holds_a_profile_disabled():
    reason = "disabled on the platform for a reason the copy does not know"
    client = _Runtimes({"runtimes": {"mock": _served(available=False, disabled_reason=reason)}})

    [mock] = json.loads(server._call(client, "swarm_profiles", {}))["profiles"]

    assert mock["available"] is False
    assert mock["disabled_reason"] == reason


def test_swarm_profiles_falls_back_to_the_labelled_copy_when_the_platform_is_unreachable():
    """Still the one tool that answers with the cluster down -- labelled."""
    client = _Runtimes(error="the API could not be reached")

    got = json.loads(server._call(client, "swarm_profiles", {}))

    assert {e["name"] for e in got["profiles"]} == set(RUNNER_PROFILES)
    assert got["catalogue_source"].startswith("bridge copy (swarm-mcp ")
    assert "could not be reached" in got["catalogue_note"]
    assert "platform may differ" in got["catalogue_note"]


def test_a_malformed_runtime_entry_is_named_not_dropped():
    client = _Runtimes({"runtimes": {"mock": _served(), "browser": {"name": "browser"}}})

    got = json.loads(server._call(client, "swarm_profiles", {}))

    assert [e["name"] for e in got["profiles"]] == ["mock"]
    assert got["profiles_not_read"] == ["browser"]


def test_the_bridge_copy_source_names_a_version():
    assert catalogue.bridge_copy_source().startswith("bridge copy (swarm-mcp")
