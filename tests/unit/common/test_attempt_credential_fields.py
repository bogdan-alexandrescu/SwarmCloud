"""Contract request 152: an `Attempt` records the account and the credential tier it ran on.

docs/design/llm-providers.md §8.2 (Q8), accepted by the owner on 2026-11-09
and filed in docs/contract-change-requests.md as request 152, taking up
request 13. What is held here:

  * `account_id`, `credential_source` and `credential_tier` exist, default to
    None ("not recorded"), and sit at the end of the class, after request
    111's sized fields, so no positional construction changes meaning;
  * they round-trip through the document shape (`asdict` and back);
  * an attempt document written before the change, which carries none of
    them, still loads and reads None for all three;
  * `credential_tier` is None, "plan" or "api_key", and anything else is
    refused at construction, so no reader of the record (the console's
    attempt chip, a spend query) meets a tier it cannot name.

Offline: no cloud credentials, no emulator.

MUTATION: drop the tier check from `Attempt.__post_init__` and
`test_any_other_tier_is_refused` fails; default `credential_source` to
"account_pool" and `test_the_credential_fields_default_to_none` fails.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from swarm_common import models
from swarm_common.models import Attempt

CREDENTIAL = ("account_id", "credential_source", "credential_tier")


def _attempt(**credential) -> Attempt:
    return Attempt(
        attempt_id="att_1",
        task_id="task_1",
        tenant_id="eng",
        generation=1,
        lease_id="lease_1",
        backend="GKE_AUTOPILOT",
        created_at=datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc),
        **credential,
    )


def test_the_credential_fields_default_to_none():
    attempt = _attempt()
    # MUTATION: default any of them to a value and an attempt from before the
    # change would claim an account or a tier nobody recorded.
    for name in CREDENTIAL:
        assert getattr(attempt, name) is None, name


def test_the_credential_fields_are_the_last_of_the_class():
    names = [f.name for f in dataclasses.fields(Attempt)]
    # MUTATION: insert them beside `cost_usd` and every positional
    # construction after it shifts.
    assert tuple(names[-len(CREDENTIAL):]) == CREDENTIAL
    assert names[names.index("sized_live_split") + 1:] == list(CREDENTIAL)


@pytest.mark.parametrize(
    ("source", "tier"),
    [("account_pool", "plan"), ("account_pool", "api_key")],
)
def test_a_pool_attempt_round_trips(source, tier):
    attempt = _attempt(account_id="acct_1", credential_source=source, credential_tier=tier)
    doc = dataclasses.asdict(attempt)
    assert {k: doc[k] for k in CREDENTIAL} == {
        "account_id": "acct_1",
        "credential_source": source,
        "credential_tier": tier,
    }
    assert Attempt(**doc) == attempt


def test_a_tenant_secret_attempt_round_trips_without_an_account_or_tier():
    attempt = _attempt(credential_source="tenant_secret")
    doc = dataclasses.asdict(attempt)
    assert {k: doc[k] for k in CREDENTIAL} == {
        "account_id": None,
        "credential_source": "tenant_secret",
        "credential_tier": None,
    }
    assert Attempt(**doc) == attempt


def test_an_older_document_without_the_fields_still_loads():
    doc = dataclasses.asdict(_attempt())
    for name in CREDENTIAL:
        doc.pop(name)
    # MUTATION: make any of the three required and every attempt written
    # before request 152 stops loading.
    old = Attempt(**doc)
    for name in CREDENTIAL:
        assert getattr(old, name) is None, name


def test_the_tiers_are_exactly_plan_and_api_key():
    assert models.CREDENTIAL_TIERS == ("plan", "api_key")


@pytest.mark.parametrize("tier", ["", "Plan", "API_KEY", "api-key", "key", "tenant_secret", 1, True])
def test_any_other_tier_is_refused(tier):
    # MUTATION: accept any string and the console's chip meets a tier it
    # cannot name.
    with pytest.raises(ValueError, match="credential_tier"):
        _attempt(account_id="acct_1", credential_source="account_pool", credential_tier=tier)
