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

import io
from datetime import datetime, timedelta, timezone

import pytest

from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger as worker_logger
from quota_broker.accountstore import AccountStore
from swarm_common.identity import tenant_id_for_user
from swarm_common.models import Lease
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend
from swarm_common.states import ParkReason

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


# --------------------------------------------------------------------------
# A worker's park on a task the pool serves waits for its own instant
# --------------------------------------------------------------------------
#
# Admission now lets a keyless tenant's task start (above). Two sweeps return
# the parks a WORKER then writes on it, and each answered a different question
# from the one the park was about:
#
#   * the credential sweep asked only "could this tenant run, on paper". For a
#     lent account that is yes whether or not the broker can be reached. So a
#     worker that fell back to a key the tenant does not have
#     (`_park_credential_missing`, +1h), or that the broker refused
#     (`_park_no_account`, +900s), was promoted on the very next drain and
#     started again, once a drain, for as long as the cause lasted;
#   * the prewarm sweep, the only code that returns a PROVIDER_QUOTA_EXHAUSTED
#     park to READY, skipped any task whose `provider:{p}:tenant:{t}` pool did
#     not exist, even after its instant had passed. Terraform creates that pool
#     only from a tenant's declared `providers`, and a keyless tenant declares
#     none (u-sw-c90291 has `providers = []`). So the pool's own wait -- a spent
#     window, an unobserved account, a paused one -- never ended.
#
# Driven through the worker's own ControlPlane, so the park is the document a
# real worker writes, not a hand-made one.


class LeaseKeeper(RecordingDispatcher):
    """Records what was started, and keeps each Lease so a worker can run on it."""

    def __init__(self) -> None:
        super().__init__()
        self.leases: list[Lease] = []

    def dispatch(self, *, task, lease, profile, tenant) -> str:
        self.leases.append(lease)
        return super().dispatch(task=task, lease=lease, profile=profile, tenant=tenant)


class Clock:
    """The scheduler's `now`, moved by the test rather than by waiting."""

    def __init__(self) -> None:
        self.at = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.at


def pool_scheduler(db, started: LeaseKeeper, clock: Clock) -> Scheduler:
    settings = pool_settings()
    return Scheduler(
        settings=settings,
        store=SchedulerStore(db),
        router=BackendRouter(cloud_run=started, gke=started, settings=settings),
        metrics=SchedulerMetrics(),
        now=clock,
    )


def worker_parks(db, lease: Lease, *, reason: ParkReason, until: datetime, detail: dict) -> None:
    """What `_park_credential_missing` and `_park_no_account` write, after the checkpoint."""
    control = ControlPlane(
        db,
        task_id=lease.task_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        tenant_id=lease.tenant_id,
        generation=lease.generation,
        logger=worker_logger(
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            tenant_id=lease.tenant_id,
            generation=lease.generation,
            runner_profile="claude-code",
            stream=io.StringIO(),
        ),
    )
    control.validate_generation()
    control.advance_to_running()
    control.park(reason=reason, next_eligible_at=until, detail=detail)


def admitted_on_a_loan(db) -> tuple[Scheduler, LeaseKeeper, Clock]:
    """A keyless tenant, an account lent to it, and its task started on the pool."""
    world(db)
    AccountStore(db).register(OWNER, "team", lend_to=[TENANT])
    task(db, "task_pool")
    started, clock = LeaseKeeper(), Clock()
    scheduler = pool_scheduler(db, started, clock)
    scheduler.drain()
    assert db.docs["tasks/task_pool"]["state"] == "DISPATCHED", db.docs["tasks/task_pool"]
    assert len(started.leases) == 1
    return scheduler, started, clock


#: The worker's CREDENTIAL_MISSING parks on a pool task, each with the interval
#: it sets. `broker_unreachable`: the broker timed out or answered 5xx, the
#: worker fell back to the tenant's key (`_decline_pool`), there is none, and
#: `_park_credential_missing` parks for an hour. The other two are
#: `_park_no_account`'s configuration errors, on its 900-second fallback.
WORKER_CREDENTIAL_PARKS = {
    "broker_unreachable": (timedelta(hours=1), {"provider": "anthropic"}),
    "broker_refused": (
        timedelta(seconds=900),
        {"provider": "anthropic", "account_pool_reason": "broker_refused",
         "park_phase": "account_assign"},
    ),
    "account_unreadable": (
        timedelta(seconds=900),
        {"provider": "anthropic", "account_pool_reason": "account_unreadable",
         "park_phase": "account_assign"},
    ),
}


@pytest.mark.parametrize("cause", sorted(WORKER_CREDENTIAL_PARKS))
def test_a_worker_credential_park_on_a_pool_task_waits_out_its_interval(db, cause):
    """A 40-minute broker outage used to cost one container start per task per
    drain: lease, start, restore, fail to reach the broker, find no key, park,
    and be promoted by the credential sweep on the next safety tick. Each start
    held a `max_active` slot and platform capacity, and could only park."""
    scheduler, started, clock = admitted_on_a_loan(db)
    interval, detail = WORKER_CREDENTIAL_PARKS[cause]
    until = clock.at + interval
    worker_parks(
        db, started.leases[-1], reason=ParkReason.CREDENTIAL_MISSING, until=until, detail=detail
    )
    assert db.docs["pools/global"]["active"] == 0

    for tick in range(1, 4):   # the 1-minute safety tick, three times over
        clock.at += timedelta(minutes=1)
        report = scheduler.drain()
        doc = db.docs["tasks/task_pool"]
        assert (doc["state"], doc["park_reason"]) == ("PARKED", "CREDENTIAL_MISSING"), (
            f"tick {tick}: the credential sweep promoted a worker's {cause} park "
            f"before its next_eligible_at, and admission started it again"
        )
        assert report.promoted_credentials == 0
        assert len(started.dispatched) == 1, "a container was started only to park again"
        assert db.docs["pools/global"]["active"] == 0, "a slot is held by a task that can only park"

    clock.at = until + timedelta(seconds=1)
    report = scheduler.drain()

    assert report.promoted_credentials == 1
    assert db.docs["tasks/task_pool"]["state"] == "DISPATCHED"
    assert len(started.dispatched) == 2


def test_a_key_registered_during_the_wait_promotes_the_worker_park_at_once(db):
    """Honouring the worker's instant does not delay the fix an admin makes: a
    key registered meanwhile makes the answer the tenant's own key, which the
    sweep promotes on the next drain, an hour early."""
    scheduler, started, clock = admitted_on_a_loan(db)
    worker_parks(
        db,
        started.leases[-1],
        reason=ParkReason.CREDENTIAL_MISSING,
        until=clock.at + timedelta(hours=1),
        detail={"provider": "anthropic"},
    )

    clock.at += timedelta(minutes=1)
    assert scheduler.drain().promoted_credentials == 0
    assert db.docs["tasks/task_pool"]["state"] == "PARKED"

    db.docs[f"tenants/{TENANT}"]["credentials"] = ["anthropic"]
    clock.at += timedelta(minutes=1)
    report = scheduler.drain()

    assert report.promoted_credentials == 1
    assert db.docs["tasks/task_pool"]["state"] == "DISPATCHED"
    assert len(started.dispatched) == 2


#: The worker's waits on the pool (`_park_no_account`), each parked
#: PROVIDER_QUOTA_EXHAUSTED with its own instant: the account window's reset
#: from the broker, the next usage poll (`STALE_READING_RETRY_SECONDS`), and
#: the long fallback for a DRAINING or REAUTH_REQUIRED account
#: (`NO_ACCOUNT_RETRY_SECONDS`).
POOL_WAITS = {
    "no_account_available": timedelta(minutes=30),
    "no_recent_reading": timedelta(seconds=300),
    "pool_paused": timedelta(seconds=900),
}


@pytest.mark.parametrize("cause", sorted(POOL_WAITS))
def test_a_pool_wait_ends_at_its_instant_with_no_per_tenant_provider_pool(db, cause):
    """u-sw-c90291, lent an account, runs claude-code until the account's
    five-hour window drops below the assign floor. The next task parks on the
    window's reset. It must run again once the window has reset -- and not
    before, because nothing caps this tenant's provider to stop an early
    promotion from starting a container on a guess."""
    scheduler, started, clock = admitted_on_a_loan(db)
    assert f"pools/provider:anthropic:tenant:{TENANT}" not in db.docs
    reopens = clock.at + POOL_WAITS[cause]
    worker_parks(
        db,
        started.leases[-1],
        reason=ParkReason.PROVIDER_QUOTA_EXHAUSTED,
        until=reopens,
        detail={"provider": "anthropic", "account_pool_reason": cause,
                "park_phase": "account_assign"},
    )

    # Inside prewarm's lead window, still before the instant: it waits.
    clock.at = reopens - timedelta(seconds=60)
    report = scheduler.drain()
    doc = db.docs["tasks/task_pool"]
    assert (doc["state"], doc["park_reason"]) == ("PARKED", "PROVIDER_QUOTA_EXHAUSTED")
    assert report.promoted_prewarm == 0
    assert len(started.dispatched) == 1

    clock.at = reopens + timedelta(seconds=1)
    report = scheduler.drain()

    assert report.promoted_prewarm == 1, (
        f"a {cause} park with no provider:anthropic:tenant:{TENANT} pool stayed "
        "PARKED after its next_eligible_at: nothing else returns it to READY"
    )
    assert db.docs["tasks/task_pool"]["state"] == "DISPATCHED"
    assert len(started.dispatched) == 2
