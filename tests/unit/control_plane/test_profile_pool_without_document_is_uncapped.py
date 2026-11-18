"""A profile's pool with no document is uncapped, and /v1/capacity says so (QA G5-04).

The console drew `provider:git:tenant:eng` -- named in `runner_profiles.merge
.pools` but absent from a complete pool listing -- three ways: `uncapped` in
the matrix, `—` (not measured) in the opened row, and as a link to a Pools row
that does not exist. The question the finding left open was server-side:
`POOL_LIMIT_UNSET` is a `needs_action` reason, so does admission refuse on a
missing per-tenant provider pool?

It does not, and this pins both halves of why. `evaluate_capacity` refuses
POOL_LIMIT_UNSET only for a pool whose DOCUMENT exists with no `hard_limit`;
a pool with no document is skipped, "unlimited by construction". And the
capacity route, reading a complete listing, files that pool under the
profile's `admission.uncapped` -- not `unread` -- which is the list the
console now draws `no pool · uncapped` and a dashed chip from. The route and
the admission check agree; the route needed no change.

The control: the same pool seeded with no `hard_limit` IS refused, and the
route moves it to `unread`, so the assertions could have come out the other way.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups.
"""

from __future__ import annotations

from swarm_common.admission import evaluate_capacity
from swarm_common.models import SlotPool
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import BlockedReason

from .conftest import auth_header, seed_pool, seed_tenant


def _per_tenant_provider_profile() -> tuple[str, str]:
    """A profile whose pools include a `provider:<p>:tenant:eng` slice, and that slice."""
    for name, profile in sorted(RUNNER_PROFILES.items()):
        if profile.provider:
            return name, f"provider:{profile.provider}:tenant:eng"
    raise AssertionError("the catalogue has no profile with a provider")


def test_admission_skips_a_pool_with_no_document_and_refuses_one_with_no_limit():
    slice_ = "provider:git:tenant:eng"
    present = {"global": SlotPool(name="global", hard_limit=10, active=0)}
    assert evaluate_capacity(present, ["global", slice_], 1) == []

    unset = {**present, slice_: SlotPool(name=slice_, hard_limit=None, active=0)}
    blockers = evaluate_capacity(unset, ["global", slice_], 1)
    assert [b["reason"] for b in blockers] == [BlockedReason.POOL_LIMIT_UNSET.value]


def test_capacity_files_a_pool_with_no_document_as_uncapped(client, db):
    name, slice_ = _per_tenant_provider_profile()
    seed_tenant(db, "eng", max_active=40, credentials=(RUNNER_PROFILES[name].provider,))
    seed_pool(db, "global", hard_limit=64)

    body = client.get("/v1/capacity", headers=auth_header("alice")).json()
    assert body["pools_complete"] is True
    profile = body["runner_profiles"][name]
    assert slice_ in profile["pools"], "the profile does not name the per-tenant slice"
    assert slice_ not in {p["name"] for p in body["pools"]}
    assert slice_ in profile["admission"]["uncapped"]
    assert slice_ not in profile["admission"]["unread"]


def test_capacity_files_a_pool_with_no_limit_as_unread_not_uncapped(client, db):
    name, slice_ = _per_tenant_provider_profile()
    seed_tenant(db, "eng", max_active=40, credentials=(RUNNER_PROFILES[name].provider,))
    seed_pool(db, "global", hard_limit=64)
    db.docs[f"pools/{slice_}"] = {"name": slice_, "hard_limit": None, "active": 0, "enabled": True}

    profile = client.get("/v1/capacity", headers=auth_header("alice")).json()["runner_profiles"][name]
    assert slice_ in profile["admission"]["unread"]
    assert slice_ not in profile["admission"]["uncapped"]
