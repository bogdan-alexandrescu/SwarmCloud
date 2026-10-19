"""A service account may act in its OWN service tenant by naming it (HW-ACCEPT-ID).

Owner decision, 2026-10-19 (part of #1436; docs/design/human-steps.md, the
HW-ACCEPT-ID amendment). Release acceptance runs as swarm-verify, which is in
the `smoke` group, and its await-input checks were refused 403
`person_required` there on every dev run: a service account answers for no
person in a GROUP tenant, and swarm-verify could never select its own service
tenant `u-sw-c90291` -- `_with_own_workspace` skips every service identity,
and `PersonalTenants.ready` wants a `workspaces/` record that a Terraform-
written service tenant never has.

Now a service identity may name `tenant_id_for_user(its own email)` in
X-Swarm-Tenant, and only that id, when ALL of these hold:

  * `tenants/<that id>`, read through `PersonalTenants`' cached reader, is
    `kind: "user"` and its principal is EXACTLY the caller's email (the
    frozen slug drops the domain, so the id alone says nothing);
  * the caller is not a worker identity, by the frozen prefix, the configured
    prefix or the tenant's own derived worker (a worker can rewrite its own
    tenant document, contract request 87);
  * the caller is not the schedule tick, the rollup sweeper, or a listed
    (continuation-scoped) account.

Its headerless default does not change; a person's path does not change.

This is the attacker's side mostly. Every fake token is built at run time;
nothing here needs a credential, an emulator or the network.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import awaitinput
from swarm_api.auth import Authenticator, PersonalTenants, StaticTokenVerifier
from swarm_api.errors import Forbidden, UpstreamUnavailable
from swarm_api.groups import StaticGroups
from swarm_common.identity import TenantMember, tenant_id_for_user, worker_service_account_id
from swarm_common.models import Tenant

from .conftest import ENG_GROUP, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore
from .test_await_input_api import (  # noqa: F401 - fixtures
    await_enabled,
    launch,
    make_context,
    park,
    snapshot,
    url,
    verdict,
    writer,
)

PROJECT_DOMAIN = "saga-agents-staging.iam.gserviceaccount.com"
#: The acceptance suite's caller, in eng's group (as it is in smoke's on dev).
VERIFY = f"swarm-verify@{PROJECT_DOMAIN}"
VERIFY_TENANT = tenant_id_for_user(VERIFY)
#: Same local part, another project: the frozen slug may derive one id for both.
TWIN = "ci-bot@other-project.iam.gserviceaccount.com"
BOT = f"ci-bot@{PROJECT_DOMAIN}"
BOT_TENANT = tenant_id_for_user(BOT)
#: A worker identity, under its own derived id.
WORKER = f"{worker_service_account_id('eng')}@{PROJECT_DOMAIN}"
#: A listed (continuation-scoped) account.
LISTED = f"swarm-ci-fix@{PROJECT_DOMAIN}"
#: The schedule tick and the rollup sweeper.
TICK = f"swarm-schedule-tick@{PROJECT_DOMAIN}"
SWEEPER = f"swarm-rollup@{PROJECT_DOMAIN}"
PERSON = "alice@saga.xyz"


def _seed_user_tenant(db: FakeFirestore, tenant_id: str, principal: str) -> None:
    """A service tenant as Terraform writes it: `kind: user`, no workspaces/ record."""
    db.docs[f"tenants/{tenant_id}"] = asdict(Tenant(
        tenant_id=tenant_id, kind="user", principal=principal,
        created_at=datetime.now(timezone.utc), display_name=tenant_id, max_active=4,
        capacity_units=8, enabled=True, credentials=[],
    ))


def _claims(email: str) -> dict[str, Any]:
    return {"email": email, "email_verified": True, "sub": f"sub-{email}"}


EMAILS = (VERIFY, BOT, TWIN, WORKER, LISTED, TICK, SWEEPER, PERSON)


def _authenticator(db: Any, *, extra: tuple[str, ...] = (), **settings: Any) -> Authenticator:
    """Every caller above is in eng's group; `extra` adds callers, admitted too."""
    emails = EMAILS + extra
    claims = {f"t-{email}": _claims(email) for email in emails}
    base: dict[str, Any] = dict(
        allowed_users=tuple(e for e in emails if e not in (TICK, SWEEPER, PERSON)),
        tenant_service_accounts=(
            TenantMember(email=LISTED, kind="group", principal=ENG_GROUP, uid=f"sub-{LISTED}"),
        ),
        schedule_tick_users=(TICK,),
        rollup_sweeper_users=(SWEEPER,),
    )
    base.update(settings)
    return Authenticator(api_settings(**base), StaticTokenVerifier(claims),
                         StaticGroups({email: (ENG_GROUP,) for email in emails}),
                         personal=PersonalTenants(db, cache_seconds=0))


def _token(email: str) -> str:
    return f"Bearer t-{email}"


@pytest.fixture
def store() -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, "eng")
    _seed_user_tenant(db, VERIFY_TENANT, VERIFY)
    return db


# --------------------------------------------------------------------------
# the authenticator
# --------------------------------------------------------------------------

def test_a_service_account_naming_its_own_service_tenant_acts_in_it(store):
    auth = _authenticator(store)
    ctx = auth.authenticate(_token(VERIFY), None, tenant=VERIFY_TENANT)
    assert (ctx.tenant_id, ctx.tenant_principal) == (VERIFY_TENANT, VERIFY)
    assert (VERIFY_TENANT, VERIFY) in ctx.tenant_choices
    assert ctx.tenant_via == ""
    # No workspaces/ record was asked for: the Terraform-written tenant has none.
    assert f"workspaces/{VERIFY_TENANT}" not in store.docs


def test_its_headerless_default_is_still_the_group(store):
    auth = _authenticator(store)
    ctx = auth.authenticate(_token(VERIFY))
    assert (ctx.tenant_id, ctx.tenant_principal) == ("eng", ENG_GROUP)
    assert ctx.tenant_choices == (("eng", ENG_GROUP),)
    # And naming the group still selects the group.
    assert auth.authenticate(_token(VERIFY), None, tenant="eng").tenant_id == "eng"


def test_a_tenant_whose_principal_is_not_the_caller_is_refused(store):
    """The frozen slug drops the domain: ci-bot@other-project derives ci-bot's
    id, and only the full-email principal tells them apart."""
    _seed_user_tenant(store, BOT_TENANT, BOT)
    assert tenant_id_for_user(TWIN) == BOT_TENANT
    auth = _authenticator(store)
    with pytest.raises(Forbidden) as refused:
        auth.authenticate(_token(TWIN), None, tenant=BOT_TENANT)
    assert refused.value.code == "tenant_not_member"
    # The owner itself is admitted, so the refusal above is the principal's doing.
    assert auth.authenticate(_token(BOT), None, tenant=BOT_TENANT).tenant_id == BOT_TENANT


@pytest.mark.parametrize("change", [
    {"kind": "group"}, {"principal": ""}, {"principal": BOT}, {"principal": VERIFY.upper() + " "},
])
def test_a_tenant_document_that_does_not_name_the_caller_is_refused(store, change):
    store.docs[f"tenants/{VERIFY_TENANT}"].update(change)
    with pytest.raises(Forbidden) as refused:
        _authenticator(store).authenticate(_token(VERIFY), None, tenant=VERIFY_TENANT)
    assert refused.value.code == "tenant_not_member"


def test_a_missing_tenant_document_is_refused(store):
    del store.docs[f"tenants/{VERIFY_TENANT}"]
    with pytest.raises(Forbidden):
        _authenticator(store).authenticate(_token(VERIFY), None, tenant=VERIFY_TENANT)


def test_only_its_own_id_another_service_tenant_is_refused(store):
    _seed_user_tenant(store, BOT_TENANT, BOT)
    with pytest.raises(Forbidden):
        _authenticator(store).authenticate(_token(VERIFY), None, tenant=BOT_TENANT)


@pytest.mark.parametrize("configured", ["swarm-agent-worker", "other-prefix"])
def test_a_worker_identity_naming_its_own_tenant_is_refused(store, configured):
    """Even under a `kind: user` document naming the worker, at the worker's
    own derived id: a worker can rewrite its own tenant document."""
    for worker in (WORKER, f"{configured}-u-zz-123456@{PROJECT_DOMAIN}"):
        own = tenant_id_for_user(worker)
        _seed_user_tenant(store, own, worker)
        auth = _authenticator(store, extra=(worker,), tenant_service_account_prefix=configured)
        with pytest.raises(Forbidden) as refused:
            auth.authenticate(_token(worker), None, tenant=own)
        assert refused.value.code == "tenant_not_member", worker
    # The same document shape for a non-worker is admitted: the refusal is the worker rule's.
    auth = _authenticator(store, tenant_service_account_prefix=configured)
    assert auth.authenticate(_token(VERIFY), None, tenant=VERIFY_TENANT).tenant_id == VERIFY_TENANT


def test_a_listed_continuation_scoped_account_is_refused(store):
    own = tenant_id_for_user(LISTED)
    _seed_user_tenant(store, own, LISTED)
    auth = _authenticator(store)
    listed = auth.authenticate(_token(LISTED))
    assert listed.member_scope == "continuation"
    with pytest.raises(Forbidden):
        auth.authenticate(_token(LISTED), None, tenant=own)


@pytest.mark.parametrize("job", [TICK, SWEEPER])
def test_the_platforms_jobs_are_refused(store, job):
    own = tenant_id_for_user(job)
    _seed_user_tenant(store, own, job)
    auth = _authenticator(store)
    ctx = auth.authenticate(_token(job))
    assert ctx.tenant_choices == ()
    with pytest.raises(Forbidden):
        auth.authenticate(_token(job), None, tenant=own)


def test_a_person_is_unchanged_and_still_needs_the_ready_workspace_record(store):
    """A `kind: user` tenant naming a person is NOT enough for a person: their
    path keeps `PersonalTenants.ready`, which wants the workspaces/ record."""
    own = tenant_id_for_user(PERSON)
    _seed_user_tenant(store, own, PERSON)
    auth = _authenticator(store)
    ctx = auth.authenticate(_token(PERSON))
    assert ctx.tenant_id == "eng" and ctx.tenant_choices == (("eng", ENG_GROUP),)
    with pytest.raises(Forbidden):
        auth.authenticate(_token(PERSON), None, tenant=own)
    store.docs[f"workspaces/{own}"] = {"tenant_id": own, "principal": PERSON, "state": "ready"}
    assert auth.authenticate(_token(PERSON)).tenant_id == own


def test_an_unreadable_tenant_is_a_503_when_named_never_a_fall_through(store):
    class Broken:
        def collection(self, path: str):
            raise RuntimeError("firestore unavailable")

    auth = _authenticator(Broken())
    with pytest.raises(UpstreamUnavailable):
        auth.authenticate(_token(VERIFY), None, tenant=VERIFY_TENANT)
    # A request that does not name it reads nothing about it.
    assert auth.authenticate(_token(VERIFY)).tenant_id == "eng"
    assert auth.authenticate(_token(VERIFY), None, tenant="eng").tenant_id == "eng"


def test_a_request_that_does_not_name_it_reads_nothing(store):
    class Exploding:
        def collection(self, path: str):
            raise AssertionError(f"read {path}")

    auth = _authenticator(Exploding())
    assert auth.authenticate(_token(VERIFY)).tenant_id == "eng"
    with pytest.raises(Forbidden):
        auth.authenticate(_token(VERIFY), None, tenant="u-someone-else")


def test_the_answer_is_cached_through_the_same_reader(store):
    clock = [0.0]
    personal = PersonalTenants(store, cache_seconds=30.0, clock=lambda: clock[0])
    assert personal.owns_service_tenant(VERIFY) is True
    store.docs[f"tenants/{VERIFY_TENANT}"]["principal"] = BOT
    clock[0] = 29.0
    assert personal.owns_service_tenant(VERIFY) is True
    clock[0] = 31.0
    assert personal.owns_service_tenant(VERIFY) is False
    # The person answer is a separate one: a service tenant is never "ready".
    assert personal.ready(VERIFY) is False


# --------------------------------------------------------------------------
# the await-input answer, end to end
# --------------------------------------------------------------------------

@pytest.fixture
def group_map(group_map):
    return {**group_map, VERIFY: (ENG_GROUP,)}


@pytest.fixture
def tokens(tokens):
    return {**tokens, "token-verify": _claims(VERIFY)}


@pytest.fixture
def api_context(db, tokens, group_map, objects, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _seed_user_tenant(db, VERIFY_TENANT, VERIFY)
    return make_context(db, tokens, objects, StaticGroups(group_map), allowed_users=(VERIFY,))


@pytest.fixture
def client(api_context, writer):  # noqa: F811 - the fixture imported above
    from fastapi.testclient import TestClient

    from swarm_api.main import create_app

    return TestClient(create_app(api_context, input_writer=writer), raise_server_exceptions=False)


def _own() -> dict[str, str]:
    return {**auth_header("verify"), "X-Swarm-Tenant": VERIFY_TENANT}


def test_swarm_verify_answers_in_its_own_service_tenant(client, db, await_enabled):
    ids = launch(client, db, user="verify", headers={"X-Swarm-Tenant": VERIFY_TENANT})
    assert db.docs[f"tasks/{ids['review']}"]["tenant_id"] == VERIFY_TENANT
    park(db, ids["review"])
    response = client.post(url(ids), headers=_own(), json=verdict(answer_id="r1-verify"))
    assert response.status_code == 200, response.text
    assert response.json()["answered_by"] == VERIFY
    summary = db.docs[f"tasks/{ids['review']}"]["result_summary"]["input"]
    assert summary["answered_as"] == awaitinput.SERVICE


def test_swarm_verify_answering_in_a_group_is_still_person_required(
    client, db, objects, await_enabled
):
    ids = launch(client, db, user="verify")          # its default: the group
    assert db.docs[f"tasks/{ids['review']}"]["tenant_id"] == "eng"
    park(db, ids["review"])
    before = snapshot(db, objects, ids["review"])
    response = client.post(url(ids), headers=auth_header("verify"),
                           json=verdict(answer_id="r1-verify"))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "person_required"
    assert snapshot(db, objects, ids["review"]) == before
