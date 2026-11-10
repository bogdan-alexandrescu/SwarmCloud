"""Both attempt routes serve the account and the credential tier an attempt ran on (contract request 152).

Contract request 152 (accepted by the owner 2026-11-09,
docs/design/llm-providers.md §8.2) adds `account_id`, `credential_source` and
`credential_tier` to `Attempt`. The worker writes them later (lane LP-W1) and
the console's attempt chip reads them later (lane LP-U5); what has to hold
from the moment the fields exist is that `attempt_from_dict` reads them back
and `attempt_to_api` serves them, or the chip would read null on every
attempt that ever recorded one.

What is pinned: both routes serve the three stored values; an attempt from
before the request serves nulls; a stored tier the model would refuse is
served as null and does not make the list unreadable.

MUTATIONS: drop any of the three from `attempt_to_api`; hand the stored tier
to `Attempt` unchecked (one bad document fails the whole list).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import auth_header, seed_task, seed_tenant

CREDENTIAL = ("account_id", "credential_source", "credential_tier")
ROUTES = ("/v1/tasks/task_a/attempts", "/v1/attempts")


def a_task(db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING")


def an_attempt(db, attempt_id, **fields):
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": "task_a", "tenant_id": "eng",
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "GKE_AUTOPILOT",
        "created_at": started, "started_at": started + timedelta(seconds=10),
        "completed_at": None, "exit_code": None, "checkpoints": [],
        **fields,
    }


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("tier", ["plan", "api_key"])
def test_a_pool_attempt_serves_its_account_and_tier(client, db, route, tier):
    a_task(db)
    an_attempt(db, "att_1", account_id="acct_1", credential_source="account_pool",
               credential_tier=tier)
    response = client.get(route, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    (row,) = response.json()["attempts"]
    assert {k: row[k] for k in CREDENTIAL} == {
        "account_id": "acct_1", "credential_source": "account_pool", "credential_tier": tier,
    }


@pytest.mark.parametrize("route", ROUTES)
def test_an_attempt_from_before_the_request_serves_nulls(client, db, route):
    a_task(db)
    an_attempt(db, "att_1")
    (row,) = client.get(route, headers=auth_header("alice")).json()["attempts"]
    for name in CREDENTIAL:
        assert name in row and row[name] is None, name


@pytest.mark.parametrize("route", ROUTES)
def test_a_stored_tier_the_model_refuses_is_served_as_null(client, db, route):
    a_task(db)
    an_attempt(db, "att_1", account_id="acct_1", credential_source="account_pool",
               credential_tier="metered")
    response = client.get(route, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    (row,) = response.json()["attempts"]
    assert row["credential_tier"] is None
    assert row["account_id"] == "acct_1"
