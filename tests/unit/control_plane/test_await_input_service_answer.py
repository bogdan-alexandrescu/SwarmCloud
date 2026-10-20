"""A service tenant's own principal answers its own workflow's waiting steps (HW-SERVICE-ANSWER).

Owner decision, 2026-10-18 (part of #1436; docs/design/human-steps.md, the Q2
amendment). A service tenant is a `kind: "user"` tenant whose principal is a
service account -- `u-or-9a0792` for oracle-api, `u-sw-c90291` for
swarm-verify -- and its only member is that account, so under "only a person
answers" nobody could answer its steps. Now:

  * the tenant's OWN principal answers a step of that tenant: 200,
    `answered_by` the principal, `on_behalf_of` recorded as a LABEL;
  * the same principal is refused another tenant's step;
  * another service account claiming the tenant, a listed (continuation-
    scoped) account, a worker or agent identity: 403 `person_required`;
  * a person sending `on_behalf_of`: 422;
  * a replay with the same `answer_id` is the original 200, `replayed: true`.

The tenant is created exactly as production creates it on the service's first
submission (`Store.ensure_tenant`): the id from the frozen
`tenant_id_for_user`, `kind: "user"`, the principal its own address. Nothing
here types a tenant id in from memory.

Offline: in-memory Firestore and bucket, a local signer. Every fake token is
built at run time.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import awaitinput
from swarm_api.auth import AuthContext
from swarm_api.groups import StaticGroups
from swarm_common.identity import Principal, tenant_id_for_user, worker_service_account_id
from swarm_common.models import Tenant

from .conftest import auth_header, seed_tenant
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
#: The service tenant's principal, and its tenant id from the frozen rule.
ORACLE = f"oracle-api@{PROJECT_DOMAIN}"
ORACLE_TENANT = tenant_id_for_user(ORACLE)
#: Another service tenant's principal.
VERIFY = f"swarm-verify@{PROJECT_DOMAIN}"
VERIFY_TENANT = tenant_id_for_user(VERIFY)
#: The worker the oracle tenant's agents run as: what its MCP bridge speaks as.
WORKER = f"{worker_service_account_id(ORACLE_TENANT)}@{PROJECT_DOMAIN}"
#: Some other admitted service account, in no tenant of its own.
STRANGER = f"swarm-stranger@{PROJECT_DOMAIN}"
REVIEWER = "dana@example.org"


@pytest.fixture
def tokens(tokens):
    def claims(email: str) -> dict[str, Any]:
        return {"email": email, "email_verified": True, "sub": f"sub-{email}"}

    return {
        **tokens,
        "token-oracle": claims(ORACLE),
        "token-verify": claims(VERIFY),
        "token-worker": claims(WORKER),
        "token-stranger": claims(STRANGER),
    }


@pytest.fixture
def api_context(db, tokens, group_map, objects, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, objects, StaticGroups(group_map),
                        allowed_users=(ORACLE, VERIFY, WORKER, STRANGER))


@pytest.fixture
def client(api_context, writer):  # noqa: F811 - the fixture imported above
    from fastapi.testclient import TestClient

    from swarm_api.main import create_app

    return TestClient(create_app(api_context, input_writer=writer), raise_server_exceptions=False)


@pytest.fixture
def waiting(client, db, await_enabled) -> dict[str, Any]:  # noqa: F811
    """oracle-api's own workflow, parked at its review step."""
    ids = launch(client, db, user="oracle")
    park(db, ids["review"])
    tenant = db.docs[f"tasks/{ids['review']}"]["tenant_id"]
    assert tenant == ORACLE_TENANT
    stored = db.docs[f"tenants/{ORACLE_TENANT}"]
    assert stored["kind"] == "user" and stored["principal"] == ORACLE
    return ids


def body(answer_id: str = "r1-oracle", **extra: Any) -> dict[str, Any]:
    return {**verdict(answer_id=answer_id), **extra}


def _auth(email: str, tenant_id: str, **fields: Any) -> AuthContext:
    return AuthContext(
        principal=Principal(email=email, subject="s", domain=email.rsplit("@", 1)[1], groups=()),
        tenant_id=tenant_id, is_admin=False, tenant_principal=fields.pop("tenant_principal", email),
        **fields,
    )


def _seed_user_tenant(db, tenant_id: str, principal: str) -> None:
    db.docs[f"tenants/{tenant_id}"] = asdict(Tenant(
        tenant_id=tenant_id, kind="user", principal=principal,
        created_at=datetime.now(timezone.utc), display_name=tenant_id, max_active=4,
        capacity_units=8, enabled=True, credentials=[],
    ))


# --------------------------------------------------------------------------
# the service tenant's own principal
# --------------------------------------------------------------------------

def test_the_service_tenants_principal_answers_its_own_step_with_a_label(
    client, db, waiting
):
    response = client.post(url(waiting), headers=auth_header("oracle"),
                           json=body(on_behalf_of=REVIEWER))
    assert response.status_code == 200, response.text
    answered = response.json()
    assert answered["state"] == "SUCCEEDED" and answered["replayed"] is False
    assert answered["answered_by"] == ORACLE
    assert answered["on_behalf_of"] == REVIEWER
    assert answered["on_behalf_of_verified"] is False

    doc = db.docs[f"tasks/{waiting['review']}"]
    record = doc["metadata"]["await_input"]["answer"]
    assert record["answered_by"] == ORACLE and record["on_behalf_of"] == REVIEWER
    # The audit trail: the step's own summary and its SUCCEEDED event name both.
    assert doc["result_summary"]["input"]["answered_by"] == ORACLE
    assert doc["result_summary"]["input"]["on_behalf_of"] == REVIEWER
    assert doc["result_summary"]["input"]["answered_as"] == "service"
    events = [d for k, d in db.docs.items()
              if k.startswith(f"tasks/{waiting['review']}/events/") and d["type"] == "succeeded"]
    assert len(events) == 1
    assert events[0]["detail"]["answered_by"] == ORACLE
    assert events[0]["detail"]["on_behalf_of"] == REVIEWER

    # Read back through GET .../input.
    view = client.get(url(waiting), headers=auth_header("oracle")).json()
    assert view["status"] == "answered"
    assert view["answer"]["answered_by"] == ORACLE
    assert view["answer"]["on_behalf_of"] == REVIEWER
    assert view["answer"]["on_behalf_of_verified"] is False


def test_the_principal_is_offered_the_form_while_the_step_waits(client, waiting):
    view = client.get(url(waiting), headers=auth_header("oracle")).json()
    assert view["status"] == "waiting" and view["may_answer"] is True


def test_on_behalf_of_is_optional_and_recorded_as_absent(client, db, waiting):
    response = client.post(url(waiting), headers=auth_header("oracle"), json=body())
    assert response.status_code == 200, response.text
    assert response.json()["answered_by"] == ORACLE
    assert response.json()["on_behalf_of"] is None
    record = db.docs[f"tasks/{waiting['review']}"]["metadata"]["await_input"]["answer"]
    assert record.get("on_behalf_of") is None


def test_a_replay_with_the_same_answer_id_is_the_original_200(client, db, objects, waiting):
    first = client.post(url(waiting), headers=auth_header("oracle"),
                        json=body(on_behalf_of=REVIEWER))
    assert first.status_code == 200, first.text
    after = snapshot(db, objects, waiting["review"])
    again = client.post(url(waiting), headers=auth_header("oracle"),
                        json=body(on_behalf_of=REVIEWER))
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replayed": True}
    assert snapshot(db, objects, waiting["review"]) == after
    # Another answer_id is the established 409, naming the principal.
    other = client.post(url(waiting), headers=auth_header("oracle"), json=body("r2-oracle"))
    assert other.status_code == 409
    assert other.json()["detail"]["reason"] == "already_answered"
    assert other.json()["detail"]["answered_by"] == ORACLE


@pytest.mark.parametrize("value", [
    "not-an-address", "a@b", "two@@example.org", "sp ace@example.org", 7, "",
    "x" * 250 + "@example.org", "dana@example.org\n", "d\u0430na@example.org",
    "dana\u202e@example.org", "dana\u200b@example.org",
])
def test_on_behalf_of_must_be_an_address_of_at_most_254(client, db, objects, waiting, value):
    before = snapshot(db, objects, waiting["review"])
    response = client.post(url(waiting), headers=auth_header("oracle"),
                           json=body(on_behalf_of=value))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["position"] == "on_behalf_of"
    assert snapshot(db, objects, waiting["review"]) == before


# --------------------------------------------------------------------------
# everything else stays refused
# --------------------------------------------------------------------------

def test_the_principal_is_refused_another_tenants_step(api_context, client, db, objects, await_enabled):
    eng = launch(client, db)                     # alice's, in eng
    park(db, eng["review"])
    before = snapshot(db, objects, eng["review"])
    # Its own tenant holds no such workflow: 404, as for anyone outside eng.
    assert client.post(url(eng), headers=auth_header("oracle"), json=body()).status_code == 404
    # It cannot select eng.
    steered = client.post(url(eng), headers={**auth_header("oracle"), "X-Swarm-Tenant": "eng"},
                          json=body())
    assert steered.status_code == 403
    # And the gate itself refuses it in a group tenant, whatever context arrives.
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(api_context, _auth(ORACLE, "eng",
                                  tenant_principal="eng@example.com"), "eng")
    assert refused.value.detail["reason"] == "person_required"
    assert snapshot(db, objects, eng["review"]) == before


def test_another_service_tenants_principal_is_refused(api_context, client, db, objects, waiting):
    _seed_user_tenant(db, VERIFY_TENANT, VERIFY)
    before = snapshot(db, objects, waiting["review"])
    # Through the route: its own tenant has no such workflow, and it cannot select oracle's.
    assert client.post(url(waiting), headers=auth_header("verify"), json=body()).status_code == 404
    steered = client.post(url(waiting),
                          headers={**auth_header("verify"), "X-Swarm-Tenant": ORACLE_TENANT},
                          json=body())
    assert steered.status_code == 403
    # The gate, handed a context that claims oracle's tenant for it.
    ctx = api_context
    for forged in (_auth(VERIFY, ORACLE_TENANT),
                   _auth(VERIFY, ORACLE_TENANT, tenant_principal=ORACLE)):
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(ctx, forged, ORACLE_TENANT)
        assert refused.value.detail["reason"] == "person_required"
    assert snapshot(db, objects, waiting["review"]) == before


def test_another_service_account_in_the_same_tenant_is_refused(api_context, client, db, objects, waiting):
    before = snapshot(db, objects, waiting["review"])
    ctx = api_context
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(ctx, _auth(STRANGER, ORACLE_TENANT, tenant_principal=ORACLE),
                                  ORACLE_TENANT)
    assert refused.value.detail["reason"] == "person_required"
    assert client.post(url(waiting), headers=auth_header("stranger"), json=body()).status_code \
        in (403, 404)
    assert snapshot(db, objects, waiting["review"]) == before


def test_a_listed_continuation_scoped_identity_is_refused(api_context, client, waiting):
    ctx = api_context
    for listed in (
        _auth(STRANGER, ORACLE_TENANT, tenant_principal=ORACLE, tenant_member=STRANGER,
              member_scope="continuation"),
        # Even were the listed address the principal's own.
        _auth(ORACLE, ORACLE_TENANT, tenant_member=ORACLE, member_scope="continuation"),
    ):
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(ctx, listed, ORACLE_TENANT)
        assert refused.value.detail["reason"] == "person_required"


def test_the_tenants_worker_and_agent_identity_is_refused(api_context, client, db, objects, waiting):
    before = snapshot(db, objects, waiting["review"])
    ctx = api_context
    # The worker GSA is not the principal: a context naming oracle's tenant refuses it.
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(ctx, _auth(WORKER, ORACLE_TENANT, tenant_principal=ORACLE),
                                  ORACLE_TENANT)
    assert refused.value.detail["reason"] == "person_required"
    # A worker identity can rewrite its tenant's document. Even with a user
    # tenant naming the worker as its principal, under the worker's own
    # derived id, a worker identity is never a service tenant's principal.
    own = tenant_id_for_user(WORKER)
    _seed_user_tenant(db, own, WORKER)
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(ctx, _auth(WORKER, own), own)
    assert refused.value.detail["reason"] == "person_required"
    # And the rewrite of oracle's own document to name the worker steers nothing.
    db.docs[f"tenants/{ORACLE_TENANT}"]["principal"] = WORKER
    with pytest.raises(awaitinput.Forbidden):
        awaitinput.member_refusal(ctx, _auth(WORKER, ORACLE_TENANT), ORACLE_TENANT)
    db.docs[f"tenants/{ORACLE_TENANT}"]["principal"] = ORACLE
    # Through the route the worker's token reaches nothing of oracle's: its
    # own derived tenant holds no such workflow.
    assert client.post(url(waiting), headers=auth_header("worker"), json=body()).status_code \
        == 404
    assert snapshot(db, objects, waiting["review"]) == before


def test_the_platforms_jobs_are_still_refused(api_context, client, waiting):
    ctx = api_context
    for job in (_auth(ORACLE, ORACLE_TENANT, is_schedule_tick=True),
                _auth(ORACLE, ORACLE_TENANT, is_rollup_sweeper=True)):
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(ctx, job, ORACLE_TENANT)
        assert refused.value.detail["reason"] == "person_required"


def test_a_tenant_document_that_is_not_the_principals_refuses_it(api_context, client, db, waiting):
    ctx = api_context
    me = _auth(ORACLE, ORACLE_TENANT)
    stored = db.docs[f"tenants/{ORACLE_TENANT}"]
    for change in ({"kind": "group"}, {"principal": ""}, {"principal": VERIFY}):
        saved = dict(stored)
        stored.update(change)
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(ctx, me, ORACLE_TENANT)
        assert refused.value.detail["reason"] == "person_required", change
        stored.clear()
        stored.update(saved)
    assert awaitinput.member_refusal(ctx, me, ORACLE_TENANT) == awaitinput.SERVICE


def test_a_tenant_document_rewritten_to_name_another_principal_steers_nothing(
    api_context, client, db, waiting
):
    # oracle's document names swarm-verify, and a context claims oracle's
    # tenant for swarm-verify: the id is not the one swarm-verify's own
    # address derives, so it is not swarm-verify's service tenant.
    db.docs[f"tenants/{ORACLE_TENANT}"]["principal"] = VERIFY
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(api_context, _auth(VERIFY, ORACLE_TENANT), ORACLE_TENANT)
    assert refused.value.detail["reason"] == "person_required"


@pytest.mark.parametrize("fields", [
    {"tenant_id": "eng"},                                   # acting in another tenant
    {"tenant_principal": "eng@example.com"},                   # resolved to another principal
    {"tenant_via": "platform_admin"},                       # reached, not owned
])
def test_a_context_that_is_not_the_principal_acting_in_its_own_tenant_is_refused(
    api_context, client, waiting, fields
):
    me = _auth(ORACLE, ORACLE_TENANT)
    forged = AuthContext(**{**{f: getattr(me, f) for f in me.__dataclass_fields__}, **fields})
    with pytest.raises(awaitinput.Forbidden) as refused:
        awaitinput.member_refusal(api_context, forged, ORACLE_TENANT)
    assert refused.value.detail["reason"] == "person_required"


def test_a_person_sending_on_behalf_of_is_422(client, db, objects, await_enabled):
    eng = launch(client, db)
    park(db, eng["review"])
    before = snapshot(db, objects, eng["review"])
    response = client.post(url(eng), headers=auth_header("alice"),
                           json={**verdict(), "on_behalf_of": REVIEWER})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["position"] == "on_behalf_of"
    assert snapshot(db, objects, eng["review"]) == before
    # Without it, the person's answer is as before, and names no one else.
    ok = client.post(url(eng), headers=auth_header("alice"), json=verdict())
    assert ok.status_code == 200, ok.text
    assert ok.json()["answered_by"] == "alice@example.com" and ok.json()["on_behalf_of"] is None


@pytest.mark.parametrize("configured", ["other-prefix", "swarm-agent-worker"])
def test_a_worker_is_refused_whatever_the_configured_prefix(
    db, tokens, group_map, objects, client, waiting, configured
):
    # The frozen worker names, and the configured ones, are never a
    # service tenant's principal -- even under a user tenant document that
    # names the worker, at the worker's own derived id.
    ctx = make_context(db, tokens, objects, StaticGroups(group_map),
                       allowed_users=(ORACLE, VERIFY, WORKER, STRANGER),
                       tenant_service_account_prefix=configured)
    for worker in (WORKER, f"{configured}-u-zz-123456@{PROJECT_DOMAIN}"):
        own = tenant_id_for_user(worker)
        _seed_user_tenant(db, own, worker)
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(ctx, _auth(worker, own), own)
        assert refused.value.detail["reason"] == "person_required", worker
    assert awaitinput.member_refusal(ctx, _auth(ORACLE, ORACLE_TENANT), ORACLE_TENANT) \
        == awaitinput.SERVICE
