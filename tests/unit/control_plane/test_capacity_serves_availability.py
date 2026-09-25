"""/v1/capacity says whether each runner profile may be dispatched at all.

CP-3 (#85, visual QA 2026-09-25). `service.capacity()` served every runner
profile without `available` or `disabled_reason`. The client type has declared
both as optional fields since the catalogue gained them, and every screen that
reads the capacity route checks `available` -- so every one of those checks
was dead code. `codex`, disabled in the frozen catalogue since its provider
refused the tenant's credential, was drawn with headroom on Pools and on
Profile headroom, and offered as a choice on Submit.

The expectation is DERIVED from `RUNNER_PROFILES` rather than written out, so
re-enabling a profile moves both sides at once. The second test disables a
profile itself, so it keeps proving the field is served on the day the
catalogue has nothing disabled in it.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_pool, seed_tenant
from .test_blocker_ui_surface import UI, body_of, json_literal, src


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng", max_active=40, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64, active=0)
    return db


def _profiles(client) -> dict:
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()["runner_profiles"]


def test_every_profile_carries_the_catalogues_availability(client, seeded):
    served = _profiles(client)
    # Vacuity guard: an empty catalogue would pass the loop below by skipping it.
    assert set(served) == set(RUNNER_PROFILES)
    for name, profile in RUNNER_PROFILES.items():
        assert "available" in served[name], f"{name}: /v1/capacity does not say whether it may run"
        assert served[name]["available"] is profile.available, name
        assert served[name]["disabled_reason"] == profile.disabled_reason, name


def test_a_disabled_profile_is_served_disabled_with_its_reason(client, seeded, monkeypatch):
    name = sorted(RUNNER_PROFILES)[0]
    monkeypatch.setitem(
        RUNNER_PROFILES,
        name,
        dataclasses.replace(
            RUNNER_PROFILES[name],
            available=False,
            disabled_reason="switched off by this test",
        ),
    )
    served = _profiles(client)[name]
    assert served["available"] is False
    assert served["disabled_reason"] == "switched off by this test"
    # The admission block is still served: it is a true statement about the
    # pools, and the screens -- not the route -- decide not to draw it as an
    # offer. Dropping it here would make a disabled profile's pools unreadable.
    assert "admission" in served


def test_capacity_and_runtimes_agree_on_availability(client, seeded):
    """Two routes serve the same fact about one catalogue; they may not differ."""
    capacity = _profiles(client)
    response = client.get("/v1/runtimes", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    runtimes = response.json()["runtimes"]
    assert set(capacity) == set(runtimes)
    for name in capacity:
        assert capacity[name]["available"] == runtimes[name]["available"], name
        assert capacity[name]["disabled_reason"] == runtimes[name]["disabled_reason"], name


# --------------------------------------------------------------------------
# The UI's development fixture serves the same two fields
# --------------------------------------------------------------------------
#
# `fixtureCapacity` in api.ts stands in for this route when the console runs
# with no deployment. It lists the five real profiles, so the two fields it
# now carries are a copy of the frozen catalogue -- and a copy nothing compares
# is correct until the day it is not. Held here with the same strict-JSON
# reader the input-contract table uses.

needs_ui = pytest.mark.skipif(not (UI / "api.ts").is_file(), reason="apps/swarm-ui/src is not present")

TABLE = "const FIXTURE_AVAILABILITY"


@needs_ui
def test_the_ui_fixture_table_is_the_catalogues_availability():
    source = src("api.ts")
    assert TABLE in source, "api.ts declares no FIXTURE_AVAILABILITY; the fixture offers codex"
    expected = {
        name: {"available": profile.available, "disabled_reason": profile.disabled_reason}
        for name, profile in RUNNER_PROFILES.items()
    }
    assert json_literal(source, TABLE) == expected, (
        "the UI fixture's availability disagrees with the frozen catalogue; regenerate "
        "the literal from RUNNER_PROFILES"
    )


@needs_ui
def test_the_fixture_capacity_takes_availability_from_the_table():
    body = body_of(src("api.ts"), "async function fixtureCapacity(")
    listed = re.findall(r"^\s{8}'?([a-z][a-z0-9-]*)'?: \{\n\s+resource_class:", body, re.MULTILINE)
    assert listed, "could not find the runner profiles fixtureCapacity lists"
    for name in listed:
        spelled = re.escape(name)
        assert re.search(
            rf"\.\.\.FIXTURE_AVAILABILITY(\[['\"]{spelled}['\"]\]|\.{spelled}\b(?!-))", body
        ), f"fixtureCapacity lists {name!r} without taking its availability from the table"
