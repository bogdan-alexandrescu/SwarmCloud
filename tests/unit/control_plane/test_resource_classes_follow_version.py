"""Every swarm-api reader of a class's size follows the current size version (SIZES-API, part of #1689).

docs/design/runner-sizes.md §4.5 and §4.8: once an admin resizes a class,
`/v1/resource-classes` (the route the console and the parity check call the
source of truth), the units `/v1/capacity` and `waiting_for` weigh a READY
task by, and the "a step may only shrink" check
(`validate_resource_class_override`) all read the CURRENT version, not the
code table. A Cloud Run profile keeps the code default whatever the version
says (owner Q3, `effective_size`).

Offline: FakeFirestore, StaticTokenVerifier. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_common.profiles import RUNNER_PROFILES, default_sizes

from swarm_api.codec import task_from_dict
from swarm_api.errors import ValidationFailed
from swarm_api.validation import validate_resource_class_override
from swarm_api.waiting import waiting_for_page

from .conftest import auth_header, seed_pool, seed_task

#: `standard` and `browser` at 16 GiB: 2 units each. The browser class's live
#: split grows its worker so the two containers still sum to the class.
GROWN = {
    "standard": {"memory_gib": 16},
    "browser": {"memory_gib": 16, "live_split": {"worker": {"memory_gib": 11}}},
}

#: `browser` alone at 16 GiB: now larger than `standard` in memory and units.
BROWSER_GROWN = {"browser": GROWN["browser"]}


def _resize(client, classes, base: int = 0) -> None:
    response = client.put("/v1/admin/runner-sizes", headers=auth_header("root"),
                          json={"base_version": base, "reason": "test", "classes": classes})
    assert response.status_code == 200, response.text


def _classes(client) -> dict:
    return client.get("/v1/resource-classes", headers=auth_header("alice")).json()["resource_classes"]


def test_with_no_version_the_route_serves_the_defaults_with_request_and_limit(client):
    served = _classes(client)
    for name, size in default_sizes().items():
        entry = served[name]
        # MUTATION: serving only `cpu` (the request) and no limit turns this red.
        assert entry["cpu_request"] == size.cpu_request
        assert entry["cpu_limit"] == size.cpu_limit
        # MUTATION: dropping the `cpu` alias old clients read turns this red.
        assert entry["cpu"] == entry["cpu_request"]
        # MUTATION: serving no version (or a version other than 0) for the defaults turns this red.
        assert entry["size_version"] == 0


def test_the_route_serves_the_new_version(client):
    _resize(client, {"standard": {"cpu_limit": 6}})
    standard = _classes(client)["standard"]
    # MUTATION: serving RESOURCE_CLASSES instead of the current version turns this red.
    assert standard["cpu_limit"] == 6
    assert standard["size_version"] == 1
    assert standard["cpu_request"] == standard["cpu"] == 4


def test_the_route_serves_the_defaults_when_the_stored_version_is_malformed(client, db):
    db.docs["control/runner_sizes"] = {"current_version": 1}
    db.docs["runner_size_versions/000001"] = {"version": 1, "classes": {"standard": {"cpu_request": "four"}}}
    standard = _classes(client)["standard"]
    # MUTATION: a malformed version raising (500) or sizing anything turns this red.
    assert standard["cpu_request"] == default_sizes()["standard"].cpu_request
    assert standard["size_version"] == 0


def test_runtimes_agree_with_the_route_after_a_resize(client):
    _resize(client, {"standard": {"cpu_limit": 6}})
    classes = _classes(client)
    runtimes = client.get("/v1/runtimes", headers=auth_header("alice")).json()["runtimes"]
    # MUTATION: /v1/runtimes still reading the code table after a resize turns this red.
    assert runtimes["claude-code"]["resources"] == classes["standard"]
    # MUTATION: a Cloud Run profile following the version turns this red (owner Q3).
    assert runtimes["mock"]["resources"]["cpu_limit"] == default_sizes()["standard"].cpu_limit


def test_capacity_weighs_a_profile_by_the_current_version(client):
    before = client.get("/v1/capacity", headers=auth_header("alice")).json()["runner_profiles"]
    assert before["claude-code"]["units"] == 1
    _resize(client, GROWN)
    after = client.get("/v1/capacity", headers=auth_header("alice")).json()["runner_profiles"]
    # MUTATION: `RESOURCE_CLASSES[...].units` in `Service.capacity` turns this red.
    assert after["claude-code"]["units"] == 2
    assert after["claude-code-browser"]["units"] == 2
    # MUTATION: a Cloud Run profile weighed by the version turns this red.
    assert after["mock"]["units"] == 1


def test_waiting_for_weighs_a_ready_task_by_the_current_version(client, db):
    seed_pool(db, "global", hard_limit=10)
    task = task_from_dict(seed_task(db, task_id="t1", tenant_id="eng", runner_profile="claude-code"))
    now = datetime.now(timezone.utc)

    def units() -> set[int]:
        reading = waiting_for_page(db, [task], as_of=now)["t1"]
        return {row["units"] for row in reading["pools"]}

    assert units() == {1}
    _resize(client, GROWN)
    # MUTATION: `required_pools` reading RESOURCE_CLASSES instead of the version turns this red.
    assert units() == {2}


def test_a_step_may_not_name_a_class_the_version_made_larger():
    claude_code = RUNNER_PROFILES["claude-code"]   # standard, GKE
    # Today `browser` is the same size as `standard`, so naming it is allowed.
    assert validate_resource_class_override(claude_code, "browser") == "browser"
    version = dict(default_sizes())
    from dataclasses import replace

    from swarm_common.profiles import LiveShare
    version["browser"] = replace(
        version["browser"], memory_gib=16,
        live_split=(LiveShare(2, 4, 11), version["browser"].live_split[1]),
    )
    with pytest.raises(ValidationFailed) as refused:
        validate_resource_class_override(claude_code, "browser", sizes=version)
    # MUTATION: comparing the code table instead of the version turns this red.
    assert "memory_gib" in refused.value.detail["larger_in"]
    # MUTATION: dropping the units dimension from the comparison turns this red.
    assert "units" in refused.value.detail["larger_in"]


def test_a_workflow_step_naming_a_grown_class_is_refused_at_submission(client):
    body = {"steps": [{"step_id": "a", "runner_profile": "claude-code", "resource_class": "browser",
                       "input": {"prompt": "x"}, "depends_on": []}]}
    # The control: at the code defaults `browser` is no larger than `standard`.
    control = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert "larger_in" not in control.text
    _resize(client, BROWSER_GROWN)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    # MUTATION: `Service` not passing the current version to the override check turns this red.
    assert response.status_code == 422, response.text
    assert "larger_in" in response.text
