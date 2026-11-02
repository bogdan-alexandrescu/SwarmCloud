"""swarm-api admits codex and serves it available (contract request 130, part of #2688).

The owner accepted request 130 on 2026-11-01 holding codex unavailable "until
the pool lanes land", and chose on 2026-11-02 to turn it on once they had: the
codex runner on a plan, the worker leasing an openai plan, the quota broker
leasing and refreshing openai accounts. Before this change a codex submission
was refused as disabled and /v1/capacity served it `available: false`.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_pool, seed_tenant


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng", max_active=40, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=64, active=0)
    return db


def test_the_catalogue_offers_codex_with_no_disabled_reason():
    codex = RUNNER_PROFILES["codex"]
    assert codex.available is True
    assert codex.disabled_reason == ""


def test_capacity_serves_codex_available(client, seeded):
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    served = response.json()["runner_profiles"]["codex"]
    assert served["available"] is True
    assert served["disabled_reason"] == ""


def test_a_codex_submission_is_accepted_where_it_was_refused(client, seeded):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "codex", "input": {"prompt": "say hello"}},
    )
    # Was 422 with detail.disabled true, before admission or any lease.
    assert response.status_code in (200, 201), response.text
    task = response.json()["task"]
    assert task["runner_profile"] == "codex"
    # The profile, not the caller, decides the provider (invariant 10).
    assert task["provider"] == "openai"
