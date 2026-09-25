"""A tenant with no key of its own runs on a pool account it may use, and only then (#169).

THE DEFECT. Admission parked every task whose tenant held no key for its
profile's provider (`scheduler/loop.py`: "admitting this would start a
container that can only fail"). Dispatch had a branch written for exactly that
tenant (`dispatch.py`, `_pool_can_serve`: "a pool account IS this tenant's
credential"). Admission runs first, so that branch was unreachable, and the
release's GKE proof -- which runs as swarm-verify's personal tenant, with
`credentials: []` -- parked on CREDENTIAL_MISSING every time. The API made the
same judgement a third time, at submission.

WHAT A POOL ACCOUNT CAN SERVE, read from the components that act on it. It is
narrower than the dispatch comment said:

  * the tenant that owns it and the tenants it is lent to, and no other
    (`quota_broker.accounts.Account.may_serve`; invariant 9);
  * its own provider (the broker's assign route filters on it);
  * a profile that takes a subscription token. An account is a Claude
    subscription, its token fills CLAUDE_CODE_OAUTH_TOKEN, and the worker does
    not ask the pool for a profile that does not declare that name.
    `claude-code` declares it. `browser` declares ANTHROPIC_API_KEY only;
  * a deployment that has a broker at all (QUOTA_BROKER_URL).

So "can this tenant run this profile" is: the profile needs no provider, OR the
tenant registered a key for it, OR an account the tenant may use serves it.
Admission, the credential sweep and the Cloud Run Job's secret mount must all
give the same answer. The last test in this file asks every one of them, over
every combination.
"""

from __future__ import annotations

import pytest

from quota_broker.accountstore import AccountStore
from swarm_common.identity import tenant_id_for_user
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

from scheduler.dispatch import BackendRouter, CloudRunJobDispatcher
from scheduler.loop import Scheduler
from scheduler.metrics import SchedulerMetrics
from scheduler.store import SchedulerStore

from .conftest import (
    RecordingDispatcher,
    auth_header,
    scheduler_settings,
    seed_pool,
    seed_task,
    seed_tenant,
)
from .test_dispatch_manifests import FakeJobsClient

BROKER = "https://swarm-quota-broker.example.run.app"

#: The tenant under test. A personal tenant, like swarm-verify's `u-sw-c90291`.
TENANT = "u-verify"
#: A group tenant that owns accounts and may lend them.
OWNER = "eng"
#: A third tenant, for an account lent to somebody else.
BYSTANDER = "research"


def pool_settings(**overrides):
    return scheduler_settings(quota_broker_url=BROKER, **overrides)


def world(db, *, tenant_key: bool = False) -> None:
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT, credentials=("anthropic",) if tenant_key else ())
    seed_tenant(db, OWNER, credentials=("anthropic",))
    seed_tenant(db, BYSTANDER, credentials=("anthropic",))


def task(db, task_id: str, profile: str = "claude-code", **fields) -> None:
    seed_task(
        db,
        task_id=task_id,
        tenant_id=fields.pop("tenant_id", TENANT),
        runner_profile=profile,
        resource_class=RUNNER_PROFILES[profile].resource_class,
        provider=RUNNER_PROFILES[profile].provider,
        **fields,
    )


def parked_events(db, task_id: str) -> list[dict]:
    prefix = f"tasks/{task_id}/events/"
    return [
        doc for path, doc in sorted(db.docs.items())
        if path.startswith(prefix) and doc.get("type") == "parked"
    ]


def count_account_reads(db, monkeypatch) -> list[str]:
    """Every time anything opens the `accounts` collection. Installed AFTER seeding."""
    reads: list[str] = []
    real = db.collection

    def collection(path: str):
        if path == "accounts":
            reads.append(path)
        return real(path)

    monkeypatch.setattr(db, "collection", collection)
    return reads


# --------------------------------------------------------------------------
# A keyless tenant on a deployment WITH a pool
# --------------------------------------------------------------------------


@pytest.mark.parametrize("whose", ["its own", "lent to it"])
def test_a_keyless_tenant_runs_on_a_pool_account_it_may_use(
    db, make_scheduler, dispatcher, whose
):
    world(db)
    if whose == "its own":
        AccountStore(db).register(TENANT, "personal")
    else:
        AccountStore(db).register(OWNER, "team", lend_to=[TENANT])
    task(db, "task_pool")

    report = make_scheduler(settings=pool_settings()).drain()

    assert db.docs["tasks/task_pool"]["state"] == "DISPATCHED", db.docs["tasks/task_pool"]
    assert report.parked == 0
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_pool"]


def test_an_account_lent_to_another_tenant_does_not_serve_this_one(
    db, make_scheduler, dispatcher
):
    """Invariant 9. A deployment having a pool is not the pool serving everyone
    in it: `eng`'s account is lent to `research`, not to this tenant."""
    world(db)
    AccountStore(db).register(OWNER, "team", lend_to=[BYSTANDER])
    task(db, "task_nope")

    make_scheduler(settings=pool_settings()).drain()

    doc = db.docs["tasks/task_nope"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert dispatcher.dispatched == []
    assert parked_events(db, "task_nope")[-1]["detail"]["account_pool"] == (
        "no_accounts_registered"
    )


def test_an_account_of_another_provider_does_not_serve_this_one(
    db, make_scheduler, dispatcher
):
    world(db)
    AccountStore(db).register(TENANT, "personal", provider="openai")
    task(db, "task_nope")

    make_scheduler(settings=pool_settings()).drain()

    assert db.docs["tasks/task_nope"]["park_reason"] == "CREDENTIAL_MISSING"
    assert dispatcher.dispatched == []


def test_the_browser_profile_cannot_run_on_a_pool_account(db, make_scheduler, dispatcher):
    """The release's GKE proof, exactly. `browser` takes ANTHROPIC_API_KEY only,
    so the worker never asks the pool for it, and an account the tenant owns is
    no credential for it. It parks, and says which of the pool's conditions
    failed, so the proof can say what would fix it."""
    world(db)
    AccountStore(db).register(TENANT, "personal")
    task(db, "task_browser", "browser")

    make_scheduler(settings=pool_settings()).drain()

    doc = db.docs["tasks/task_browser"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert dispatcher.dispatched == []
    detail = parked_events(db, "task_browser")[-1]["detail"]
    assert detail["provider"] == "anthropic"
    assert detail["account_pool"] == "profile_takes_no_subscription"


def test_a_parked_keyless_task_is_promoted_once_an_account_is_lent(
    db, make_scheduler, dispatcher
):
    """The credential sweep asks the same question admission does. It also proves
    the pool is read afresh on every drain: the lending happens between two."""
    world(db)
    task(db, "task_waiting", state="PARKED", park_reason="CREDENTIAL_MISSING")
    scheduler = make_scheduler(settings=pool_settings())

    first = scheduler.drain()
    assert first.promoted_credentials == 0
    assert db.docs["tasks/task_waiting"]["state"] == "PARKED"

    AccountStore(db).register(OWNER, "team", lend_to=[TENANT])
    second = scheduler.drain()

    assert second.promoted_credentials == 1
    assert db.docs["tasks/task_waiting"]["state"] == "DISPATCHED"


def test_the_pool_is_read_once_per_drain_and_only_for_a_keyless_tenant(
    db, make_scheduler, dispatcher, monkeypatch
):
    """The common case -- a tenant with its own key -- costs no extra read, and
    a drain full of keyless work costs one."""
    world(db)
    seed_tenant(db, "keyed", credentials=("anthropic",))
    AccountStore(db).register(OWNER, "team", lend_to=[TENANT])
    task(db, "task_keyed", tenant_id="keyed")
    scheduler = make_scheduler(settings=pool_settings())
    reads = count_account_reads(db, monkeypatch)

    scheduler.drain()
    assert reads == [], "a tenant with its own key made admission read the pool"

    task(db, "task_a")
    task(db, "task_b")
    task(db, "task_c", state="PARKED", park_reason="CREDENTIAL_MISSING")
    scheduler.drain()

    assert len(reads) == 1, reads
    for task_id in ("task_a", "task_b", "task_c"):
        assert db.docs[f"tasks/{task_id}"]["state"] == "DISPATCHED", task_id


# --------------------------------------------------------------------------
# A keyless tenant WITHOUT a pool, and the cases that must not change
# --------------------------------------------------------------------------


def test_a_keyless_tenant_on_a_deployment_with_no_pool_parks(db, make_scheduler, dispatcher):
    world(db)
    AccountStore(db).register(TENANT, "personal")   # registered, but no broker
    task(db, "task_nopool")

    make_scheduler(settings=scheduler_settings()).drain()

    doc = db.docs["tasks/task_nopool"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING")
    assert dispatcher.dispatched == []
    # Parked BEFORE a lease: nothing was reserved (invariant 1).
    assert db.docs["pools/global"]["active"] == 0
    assert parked_events(db, "task_nopool")[-1]["detail"] == {
        "reason": "CREDENTIAL_MISSING",
        "provider": "anthropic",
        "account_pool": "no_broker_configured",
    }


@pytest.mark.parametrize("broker", [False, True])
def test_a_tenant_with_its_own_key_is_admitted_as_before(
    db, make_scheduler, dispatcher, broker
):
    world(db, tenant_key=True)
    task(db, "task_keyed")

    settings = pool_settings() if broker else scheduler_settings()
    make_scheduler(settings=settings).drain()

    assert db.docs["tasks/task_keyed"]["state"] == "DISPATCHED"


@pytest.mark.parametrize("broker", [False, True])
def test_a_profile_that_needs_no_provider_is_admitted_as_before(
    db, make_scheduler, dispatcher, broker
):
    world(db)
    task(db, "task_mock", "mock")

    settings = pool_settings() if broker else scheduler_settings()
    make_scheduler(settings=settings).drain()

    assert db.docs["tasks/task_mock"]["state"] == "DISPATCHED"


# --------------------------------------------------------------------------
# The API does not answer the question at all
# --------------------------------------------------------------------------


def test_the_api_leaves_the_credential_question_to_admission(
    client, db, make_scheduler, dispatcher
):
    """It used to park a keyless tenant's task at submission, by a third copy of
    the rule that knew nothing about the pool. A task it parked there for a
    tenant the pool serves would have waited for the credential sweep; with no
    pool, admission parks it before any lease, which is what the API's park was
    for. READY costs nothing (invariant 1)."""
    seed_pool(db, "global", hard_limit=10)
    carol = tenant_id_for_user("carol@saga.xyz")
    response = client.post(
        "/v1/tasks",
        headers=auth_header("carol"),
        json={"runner_profile": "claude-code", "input": {}},
    )
    assert response.status_code in (200, 201), response.text
    submitted = response.json()["task"]
    assert submitted["state"] == "READY"
    assert submitted.get("park_reason") is None

    AccountStore(db).register(OWNER, "team", lend_to=[carol])
    make_scheduler(settings=pool_settings()).drain()

    assert db.docs[f"tasks/{submitted['id']}"]["state"] == "DISPATCHED"


# --------------------------------------------------------------------------
# Admission and dispatch agree, on every combination
# --------------------------------------------------------------------------

#: The catalogue's profiles a pool account can run: the ones whose `secrets`
#: name CLAUDE_CODE_OAUTH_TOKEN. Written out rather than derived, so this test
#: states what it expects; tests/unit/worker/test_pool_credential_parity.py
#: holds the scheduler's rule to the worker's own decision.
TAKES_A_SUBSCRIPTION = {"claude-code"}

ACCOUNT_SETUPS = ("none", "its own", "lent to it", "lent to another", "another provider")


def expected(profile_name: str, tenant_key: bool, broker: bool, accounts: str):
    """(admitted, what the pool answered when it was not)."""
    profile = RUNNER_PROFILES[profile_name]
    if profile.provider is None or tenant_key:
        return True, None
    if not broker:
        return False, "no_broker_configured"
    if profile_name not in TAKES_A_SUBSCRIPTION:
        return False, "profile_takes_no_subscription"
    if accounts in ("its own", "lent to it"):
        return True, None
    return False, "no_accounts_registered"


def secret_refs(job) -> set[tuple[str, str]]:
    container = job.template.template.containers[0]
    return {
        (env.name, env.value_source.secret_key_ref.secret)
        for env in container.env
        if env.value_source.secret_key_ref.secret
    }


@pytest.mark.parametrize("accounts", ACCOUNT_SETUPS)
@pytest.mark.parametrize("broker", [False, True], ids=["no-pool", "pool"])
@pytest.mark.parametrize("tenant_key", [False, True], ids=["keyless", "keyed"])
@pytest.mark.parametrize("profile_name", ["mock", "claude-code", "browser"])
def test_admission_and_dispatch_agree(db, profile_name, tenant_key, broker, accounts):
    """ONE answer, asked at both call sites.

    Admission is `Scheduler._admit_one`; dispatch is the Cloud Run Job the real
    `CloudRunJobDispatcher` builds for the admitted task. For every combination:

      * admitted exactly when the rule says so;
      * admitted -> the Job names the tenant's OWN secret when the tenant has a
        key, and no secret at all when a pool account is the credential (Cloud
        Run resolves a `secretKeyRef` at Job creation, so naming a secret that
        does not exist is a Job that is never created);
      * not admitted -> PARKED on CREDENTIAL_MISSING before any lease, no Job,
        no execution, and the park says what the pool answered;
      * whatever happened, no Job names another tenant's secret (invariant 9).
    """
    from scheduler.credentials import AccountPool, CredentialSource, credential_for

    world(db, tenant_key=tenant_key)
    store = AccountStore(db)
    if accounts == "its own":
        store.register(TENANT, "personal")
    elif accounts == "lent to it":
        store.register(OWNER, "team", lend_to=[TENANT])
    elif accounts == "lent to another":
        store.register(OWNER, "team", lend_to=[BYSTANDER])
    elif accounts == "another provider":
        store.register(TENANT, "personal", provider="openai")
    task(db, "task_x", profile_name)

    settings = pool_settings() if broker else scheduler_settings()
    pool = AccountPool.for_deployment(settings, db)
    jobs = FakeJobsClient()
    gke = RecordingDispatcher()
    router = BackendRouter(
        cloud_run=CloudRunJobDispatcher(settings, client=jobs, pool=pool),
        gke=gke,
        settings=settings,
    )
    Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=router,
        metrics=SchedulerMetrics(),
        pool=pool,
    ).drain()

    profile = RUNNER_PROFILES[profile_name]
    admitted, pool_answer = expected(profile_name, tenant_key, broker, accounts)
    doc = db.docs["tasks/task_x"]
    created = [entry["job"] for entry in jobs.created]

    # The predicate itself gives the table's answer.
    tenant = SchedulerStore(db).get_tenant(TENANT)
    answer = credential_for(profile, tenant, pool)
    assert answer.runnable is admitted

    if admitted:
        assert doc["state"] == "DISPATCHED", doc
        if resolve_backend(profile) is Backend.CLOUD_RUN_JOB:
            assert len(created) == 1
            mounted = secret_refs(created[0])
            if profile.provider and tenant_key:
                assert answer.source is CredentialSource.TENANT_KEY
                assert mounted == {
                    (env, f"swarm-tenant-{TENANT}-{profile.provider}")
                    for env in profile.secrets
                }
            else:
                assert mounted == set(), (
                    "a Job for a task whose credential is not the tenant's own key "
                    "names a tenant secret"
                )
        else:
            assert created == []
            assert [d["task_id"] for d in gke.dispatched] == ["task_x"]
    else:
        assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), doc
        assert created == [] and jobs.runs == [] and gke.dispatched == []
        assert db.docs["pools/global"]["active"] == 0
        detail = parked_events(db, "task_x")[-1]["detail"]
        assert detail["provider"] == profile.provider
        assert detail["account_pool"] == pool_answer
        assert answer.pool.value == pool_answer

    for job in created:
        for _env, secret in secret_refs(job):
            assert secret.startswith(f"swarm-tenant-{TENANT}-"), secret
