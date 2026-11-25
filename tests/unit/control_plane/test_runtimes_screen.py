"""`GET /v1/runtimes` existed and nothing rendered it.

THE DEFECT CLASS, in its other direction. This repository keeps producing seams
built at both ends with nothing in the middle. `/v1/runtimes` was the mirror
image of a client calling a route the server lacks: the route was written,
tested field for field against the frozen catalogue, and then no screen read
it -- so the facts it publishes stayed in `swarm_common.profiles`, which the
person calling the API over HTTP does not have.

This file once read the console's source as text. Part of #72 moved each check
to the side that owns it:

  * the TypeScript type field for field against the payload -- the generated
    `Runtime`/`ResourceClassSpec` (`wire_types.RuntimesPage`), held to what the
    route serves by `test_wire_types.py`, and `tsc` holds the console's types
    to the generated ones;
  * the route is read by a loader, a screen, a reachable hash; the screen reads
    every published field, never coalesces a failed read to 0, branches on
    `secrets_any_of`, and keeps its help glyph; the fixture exercises the
    branches live data never would -- the console's `screens.source.test.ts`;
  * the screen and its fixture restate no frozen catalogue name -- the same
    file, against the generated `constants/runtimes.json`;
  * the client calls `/v1/runtimes` -- `contract.shapes.test.ts`, with fetch
    stubbed; that the API serves it and documents it is checked here.

What stays here is the API's half.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

from .conftest import auth_header
from .openapi_reads import has_path, operation

ROUTE = "/v1/runtimes"


def test_the_route_the_console_calls_is_served_and_documented(client):
    """The API half of the seam: the console's loader calls `/v1/runtimes`
    (contract.shapes.test.ts), and this API serves it with its documented shape."""
    assert has_path(client, ROUTE), f"{ROUTE} is not in the OpenAPI document"
    content = operation(client, ROUTE)["responses"]["200"]["content"]
    assert content["application/json"]["schema"]["$ref"] == "#/components/schemas/RuntimesPage"
    response = client.get(ROUTE, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert response.json()["runtimes"], "the route served no runtimes"


def test_the_live_catalogue_never_splits_declared_from_resolved_backend(client):
    """Why the development fixture has to carry the declared/resolved split
    (its half is in screens.source.test.ts): the shipped catalogue never draws it."""
    served = client.get(ROUTE, headers=auth_header("alice")).json()["runtimes"]
    assert served, "the route served no runtimes; this test would compare nothing"
    assert all(r["backend"] == r["resolved_backend"] for r in served.values()), (
        "a shipped profile now declares a backend the platform resolves; the console "
        "fixture's note should be revisited"
    )


@pytest.mark.parametrize("scale", [1, 3])
def test_a_catalogue_that_gains_entries_needs_no_edit_here(client, monkeypatch, scale):
    """The property the screen's comparisons depend on.

    Every 'what sets it apart' line is computed across the response, so the
    screen has to keep working for a catalogue it has never seen. This asserts
    the API half of that -- the route publishes whatever the frozen catalogue
    holds -- which is what makes the client half safe to write without a name
    in it.
    """
    from swarm_common.profiles import RunnerProfile

    for n in range(scale):
        name = f"probe-{n}"
        monkeypatch.setitem(
            RUNNER_PROFILES,
            name,
            RunnerProfile(
                name=name,
                image=f"probe-image-{n}",
                resource_class=next(iter(RESOURCE_CLASSES)),
                backend=Backend.CLOUD_RUN_JOB,
                runner_argv=("python", "-m", "agent_worker.runners.mock"),
            ),
        )

    served = client.get(ROUTE, headers=auth_header("alice")).json()["runtimes"]
    for n in range(scale):
        assert f"probe-{n}" in served, "the route did not publish an added profile"
        assert served[f"probe-{n}"]["image"] == f"probe-image-{n}"
