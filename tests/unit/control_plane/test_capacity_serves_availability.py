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

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_pool, seed_tenant


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
