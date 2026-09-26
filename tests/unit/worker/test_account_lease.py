"""A worker runs on an account it was LEASED, not on one fixed secret.

Until now every agent a tenant ran read the same
`swarm-tenant-<tenant>-anthropic` secret, which means one subscription's
five-hour window shared by all of them: the second agent slows the first and
the fourth stops it. The pool spreads them across several subscriptions. This
is the worker's half of that, and what it has to get right:

  * IT IS ACTUALLY WIRED. The pool was complete on both sides and reached zero
    agents, because nothing set QUOTA_BROKER_URL on a worker and every test
    here injected `WorkerDeps.account_broker` past the question. Two tests at
    the bottom of this file build the client from the CONFIG and read the
    dispatcher's own environment, so "the feature exists in production" is
    asserted rather than assumed.
  * BACKWARDS COMPATIBILITY IS NOT BEST-EFFORT. No broker configured, no
    account registered, or a broker that cannot be reached -- all three fall
    back to the per-tenant secret, unchanged. Adopting the pool is a decision
    an operator makes by registering an account, not something a deploy does
    to them.
  * A REFUSAL IS NOT AN OUTAGE. An unreachable broker degrades to the tenant
    secret; a 401/403/404 means the pool IS configured and this worker may not
    use it, and carrying on would put every agent back on the one shared
    subscription while the pool looked healthy and idle.
  * AN EMPTY POOL PARKS, IT DOES NOT FAIL. So does an unreadable account, and
    so does a refusal. Parking costs nothing and resumes by itself; failing
    spends one of the task's three attempts, and because `choose()` is
    deterministic it would spend all three on the same bad account.
  * THE ACCOUNT IS RELEASED ON EVERY EXIT PATH. The release lives in the
    `finally` of `run()`, so a new exit path cannot leak an assignment -- which
    would surface weeks later as an account `choose()` has quietly stopped
    picking.
  * A CREDENTIAL RELOAD RE-READS THE SAME ACCOUNT. Refreshing revokes the
    previous token, so a long attempt can lose its credential mid-run; the fix
    is to re-read that account's secret, never to move onto a different
    subscription halfway through.
  * A SUBSCRIPTION TOKEN GOES IN THE OAUTH VARIABLE AND NOWHERE ELSE.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_worker.accountlease import (
    ACCOUNT_TOKEN_ENV,
    NO_ACCOUNTS_REGISTERED,
    AccountBroker,
    Assignment,
    BrokerRefused,
    BrokerUnavailable,
    NoAccount,
    credential_env_from_account,
)
from agent_worker.errors import ExitCode, FencedError
from swarm_common.models import Lease, Task, Tenant, utcnow
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason, TaskState

from conftest import PROJECT, TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

ACCOUNT_ID = f"{TENANT}:personal"
ACCOUNT_SECRET = f"swarm-account-{TENANT}--personal"
ASSIGNMENT_ID = "assignment-0001"
TENANT_SECRET = f"swarm-tenant-{TENANT}-anthropic"
TOKEN = "sk-ant-oat01-from-the-pool"


class FakeBroker:
    """The two pool routes, without a network.

    Records every call, because the properties under test are mostly about
    WHICH calls happen -- one assignment per attempt, one release per exit,
    and no second assignment on a credential reload.
    """

    def __init__(self, outcome=None, *, assign_error=None, release_error=None,
                 outcomes=None) -> None:
        self.outcome = outcome if outcome is not None else Assignment(
            account_id=ACCOUNT_ID,
            secret=ACCOUNT_SECRET,
            assignment_id=ASSIGNMENT_ID,
            account={"account_id": ACCOUNT_ID, "owner_tenant": TENANT, "label": "personal"},
        )
        #: A queue, for the paths where one attempt is handed more than one
        #: account -- the unreadable-secret retry. Exhausted, it falls back to
        #: `self.outcome`.
        self.outcomes = list(outcomes or [])
        self.assign_error = assign_error
        self.release_error = release_error
        self.assigns: list[str | None] = []
        self.excludes: list[tuple[str, ...]] = []
        self.releases: list[str] = []
        self.unusable: list[str] = []

    def assign(self, provider, *, exclude=()):
        self.assigns.append(provider)
        self.excludes.append(tuple(exclude))
        if self.assign_error is not None:
            raise self.assign_error
        if self.outcomes:
            return self.outcomes.pop(0)
        return self.outcome

    def release(self, account_id, assignment_id, *, unusable=""):
        assert assignment_id, "a release must name the assignment it gives back"
        self.releases.append(account_id)
        self.unusable.append(unusable)
        if self.release_error is not None:
            raise self.release_error
        return 0


@pytest.fixture()
def secrets() -> FakeSecretClient:
    return FakeSecretClient({ACCOUNT_SECRET: TOKEN, TENANT_SECRET: "sk-ant-api03-tenant"})


def _pool_worker(worker_factory, secrets, broker, tmp_path, **kwargs):
    """A claude-code worker with its workspace already created.

    claude-code because it is the profile that declares CLAUDE_CODE_OAUTH_TOKEN
    and therefore the one an account can serve.
    """
    from agent_worker import workspace as workspace_mod

    worker, config, exporter = worker_factory(
        runner_profile="claude-code", secret_client=secrets, **kwargs
    )
    worker._account_broker = broker
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    return worker, config, exporter


# -- the assigned account replaces the tenant secret -----------------------


def test_an_assigned_account_supplies_the_credential(db, worker_factory, secrets, tmp_path):
    """The point of the whole change: the agent runs on the account it was
    given, and the tenant's one shared secret is not read at all."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert env[ACCOUNT_TOKEN_ENV] == TOKEN
    assert secrets.accessed == [ACCOUNT_SECRET]
    assert TENANT_SECRET not in secrets.accessed
    assert broker.assigns == ["anthropic"]


def test_a_subscription_token_never_lands_in_the_api_key_variable(
    db, worker_factory, secrets, tmp_path
):
    """claude-code declares both names as INTERCHANGEABLE inputs, not copies.
    Projecting an OAuth token into ANTHROPIC_API_KEY would offer the metered
    API a credential it refuses -- with a credential that is perfectly valid."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = _pool_worker(worker_factory, secrets, FakeBroker(), tmp_path)

    env = worker._build_child_env()

    assert env[ACCOUNT_TOKEN_ENV] == TOKEN
    assert "ANTHROPIC_API_KEY" not in env


def test_the_account_token_never_reaches_the_logs(
    db, worker_factory, secrets, tmp_path, log_stream
):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = _pool_worker(worker_factory, secrets, FakeBroker(), tmp_path)

    worker._build_child_env()
    worker.log.info(f"about to run with {TOKEN} in the message")

    assert TOKEN not in log_stream.getvalue()
    assert "***REDACTED***" in log_stream.getvalue()


def test_a_profile_that_wants_an_api_key_does_not_ask_the_pool(
    db, worker_factory, tmp_path
):
    """An account is a SUBSCRIPTION. The browser profile declares only
    ANTHROPIC_API_KEY, so an account has nothing it could put anywhere."""
    from agent_worker import workspace as workspace_mod

    seed_attempt(db, runner_profile="browser")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({TENANT_SECRET: "sk-ant-api03-tenant"})
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="browser", secret_client=secrets)
    worker._account_broker = broker
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")

    env = worker._build_child_env()

    assert broker.assigns == [], "the pool cannot serve a metered-API profile"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-tenant"


# -- backwards compatibility ----------------------------------------------


def test_with_no_broker_configured_nothing_changes(db, worker_factory, secrets, tmp_path):
    """The default. A deployment that has not adopted the pool must not make a
    single new call, and must read exactly the secret it always read."""
    from agent_worker import workspace as workspace_mod

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    worker, config, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")

    assert config.quota_broker_url is None
    env = worker._build_child_env()

    assert worker._account_broker is None
    assert secrets.accessed == [TENANT_SECRET]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-tenant"


def test_a_pool_with_no_accounts_registered_falls_back(db, worker_factory, secrets, tmp_path):
    """"No account registered" is not "no account free". The pool is not how
    this tenant runs, so the per-tenant secret still is."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(NoAccount(reason=NO_ACCOUNTS_REGISTERED))
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-tenant"
    assert secrets.accessed == [TENANT_SECRET]


def test_a_broker_that_cannot_be_reached_falls_back(db, worker_factory, secrets, tmp_path):
    """DEGRADE, DO NOT FAIL. A control-plane service being down is not the
    task's fault, and the tenant secret still works -- turning one outage into
    failed attempts across every tenant is a much larger incident."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(assign_error=BrokerUnavailable("connection refused"))
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-tenant"


def test_a_broker_client_that_raises_anything_falls_back(db, worker_factory, secrets, tmp_path):
    """Defence in depth. The fallback must not depend on the client raising
    exactly the exception this module expects."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(assign_error=RuntimeError("something unforeseen"))
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    assert worker._build_child_env()["ANTHROPIC_API_KEY"] == "sk-ant-api03-tenant"


def test_a_tenant_with_no_key_and_no_pool_still_parks_as_before(db, worker_factory):
    """The pre-existing behaviour, unchanged: an admin has not registered the
    key, so the task parks rather than failing."""
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "needs a key", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=[])
    worker, _, _ = worker_factory(runner_profile="claude-code")
    worker._account_broker = FakeBroker(NoAccount(reason=NO_ACCOUNTS_REGISTERED))

    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["park_reason"] == ParkReason.CREDENTIAL_MISSING.value


# -- an empty pool is a park, not a failure --------------------------------


def test_an_exhausted_pool_parks_the_task(db, worker_factory, secrets):
    """Every account is spent. Nobody did anything wrong, so this must not
    spend one of the task's three attempts."""
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "waits for capacity", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(NoAccount(reason="no_account_available"))

    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.PROVIDER_QUOTA_EXHAUSTED.value
    assert int(task.get("attempt_count", 1)) == 1, "a queue must not cost an attempt"
    assert db.doc("leases/lease_1")["released_at"] is not None, "the slot goes back"


def test_the_park_names_the_pool_so_the_cause_is_not_guessed_at(db, worker_factory, secrets):
    """PROVIDER_QUOTA_EXHAUSTED is also what a 429 parks as. The detail is what
    separates "the provider throttled us" from "our own pool is full"."""
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(NoAccount(reason="no_account_available"))
    worker.run()

    blocked = db.doc("tasks/task_1")["blocked_by"][0]
    assert blocked["account_pool_reason"] == "no_account_available"
    assert blocked["park_phase"] == "account_assign"


def test_the_task_wakes_when_the_window_clears_rather_than_polling(db, worker_factory, secrets):
    """`resetsAt` is the provider's own answer to "when", so a drained pool is
    scheduled back at a known instant instead of probed."""
    clears = datetime.now(timezone.utc) + timedelta(hours=2)
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(
        NoAccount(reason="no_account_available", next_reset_at=clears.isoformat())
    )
    worker.run()

    eligible = db.doc("tasks/task_1")["next_eligible_at"]
    assert abs((eligible - clears).total_seconds()) < 5


def test_a_pool_waiting_on_a_person_uses_the_fallback_delay(db, worker_factory, secrets):
    """No reset instant means nothing is waiting on a clock. A one-minute retry
    would be a dispatch, an image pull and a lease for the same answer."""
    from agent_worker.lifecycle import NO_ACCOUNT_RETRY_SECONDS

    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(NoAccount(reason="no_account_available"))
    worker.run()

    delay = db.doc("tasks/task_1")["next_eligible_at"] - datetime.now(timezone.utc)
    assert timedelta(seconds=NO_ACCOUNT_RETRY_SECONDS - 60) < delay


# -- release, on every exit path ------------------------------------------


def test_the_account_is_released_when_the_attempt_parks(db, worker_factory, secrets):
    """A real run: the account is assigned at step 6, the provider is already
    exhausted, the task parks -- and the account goes back."""
    from swarm_common.models import ProviderState

    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=[])          # NO tenant key: only the pool can serve this
    db.seed(
        f"quota/anthropic:{TENANT}",
        {"provider": "anthropic", "tenant_id": TENANT,
         "state": ProviderState.EXHAUSTED.value, "retry_after_seconds": 2400,
         "updated_at": None},
    )
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = broker

    assert worker.run() == ExitCode.PARKED
    assert broker.assigns == ["anthropic"], "assigned once"
    assert broker.releases == [ACCOUNT_ID], "and given back"


def test_a_fenced_worker_releases_the_account_it_had_taken(db, worker_factory, secrets):
    """Invariant 5: a superseded attempt exits without touching the LEASE --
    the live lease is not its own. The account is not the lease: this worker
    took it, so this worker gives it back, or the pool counts an agent that
    does not exist."""
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = broker

    # Fence on the RE-CHECK that runs after the credential is resolved, which
    # is the real shape: a reconciler bumped the generation while setup ran.
    original = worker.control.validate_generation
    calls = {"n": 0}

    def fencing_validate():
        calls["n"] += 1
        if calls["n"] == 1:
            return original()
        raise FencedError(expected=1, actual=2)

    worker.control.validate_generation = fencing_validate

    assert worker.run() == ExitCode.GENERATION_FENCED
    assert broker.releases == [ACCOUNT_ID]
    assert db.doc("leases/lease_1")["released_at"] is None, "the lease is NOT this one's"


def test_the_release_is_in_the_one_place_every_exit_runs_through(
    db, worker_factory, secrets, tmp_path
):
    """`_cleanup` is the `finally` of `run()`. Releasing at each exit site
    instead would mean a new exit path is a leaked assignment."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)
    worker._build_child_env()

    worker._cleanup()

    assert broker.releases == [ACCOUNT_ID]


def test_releasing_twice_makes_one_call(db, worker_factory, secrets, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)
    worker._build_child_env()

    worker._release_account()
    worker._release_account()

    assert broker.releases == [ACCOUNT_ID]


def test_a_failed_release_never_replaces_the_attempts_real_outcome(
    db, worker_factory, secrets, tmp_path
):
    """A failed release costs one over-counted assignment, which the reconciler
    corrects. An exception here would lose the attempt's actual result."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(release_error=BrokerUnavailable("gone"))
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)
    worker._build_child_env()

    worker._cleanup()          # must not raise


def test_nothing_is_released_when_nothing_was_taken(db, worker_factory, secrets, tmp_path):
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker(NoAccount(reason=NO_ACCOUNTS_REGISTERED))
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)
    worker._build_child_env()

    worker._cleanup()

    assert broker.releases == []


# -- the credential reload path -------------------------------------------


def test_a_reload_re_reads_the_assigned_account_not_the_tenant_secret(
    db, worker_factory, secrets, tmp_path
):
    """Refreshing an OAuth credential revokes the previous token, so a long
    attempt can lose its credential mid-run. The replacement is already in the
    SAME secret -- `access()` takes `latest` -- so re-reading it is the fix."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    worker._build_child_env()
    secrets.values[ACCOUNT_SECRET] = "sk-ant-oat01-rotated"
    env = worker._build_child_env()          # what the reload path calls

    assert env[ACCOUNT_TOKEN_ENV] == "sk-ant-oat01-rotated"
    assert secrets.accessed == [ACCOUNT_SECRET, ACCOUNT_SECRET]
    assert TENANT_SECRET not in secrets.accessed


def test_a_reload_does_not_move_the_agent_onto_a_different_account(
    db, worker_factory, secrets, tmp_path
):
    """A second assignment would count this one agent on two accounts, and
    would swap the subscription underneath a half-finished run."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    worker._build_child_env()
    worker._build_child_env()
    worker._build_child_env()

    assert broker.assigns == ["anthropic"], "assigned exactly once per attempt"


# -- an account whose secret cannot be read --------------------------------


def _other(label: str = "second") -> Assignment:
    return Assignment(
        account_id=f"{TENANT}:{label}",
        secret=f"swarm-account-{TENANT}--{label}",
        assignment_id=f"assignment-{label}",
        account={"account_id": f"{TENANT}:{label}", "owner_tenant": TENANT,
                 "label": label},
    )


def test_an_empty_account_secret_is_never_exported(db, worker_factory, tmp_path):
    """An empty secret version must read as absent. Exporting "" would start
    the agent with a credential variable set to nothing, and the failure would
    surface as an authentication error pointing nowhere."""
    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    other = _other()
    secrets = FakeSecretClient({ACCOUNT_SECRET: "   ", other.secret: TOKEN})
    broker = FakeBroker(outcomes=[FakeBroker().outcome, other])
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert env[ACCOUNT_TOKEN_ENV] == TOKEN, "it moved to an account it can read"
    assert "" not in env.values(), (
        'exporting "" would start the agent with a credential variable set to '
        "nothing, and the failure would surface as an authentication error "
        "pointing nowhere"
    )


def test_a_freshly_onboarded_account_is_handed_back_and_another_is_asked_for(
    db, worker_factory, tmp_path
):
    """THE ONBOARDING WINDOW, which is every task submitted between
    registration in the Settings page and the broker's next sweep.

    registration creates `{base}` EMPTY on purpose -- the broker publishes the
    access token on its next sweep -- and `Account.headroom()` returns 1.0 for
    a never-observed account, so `choose()` ranks that brand-new account FIRST.
    Reading it raises google NotFound, which used to travel out of
    `_account_credential_env` untouched, land in run()'s generic handler and
    FAIL the task. `choose()` is deterministic, so the retry picked the same
    account and burned all three attempts.
    """
    from google.api_core import exceptions as gexc

    class NotFoundOnce(FakeSecretClient):
        def access(self, name, version="latest"):
            if name == ACCOUNT_SECRET:
                self.accessed.append(name)
                raise gexc.NotFound("Secret Version [latest] not found.")
            return super().access(name, version)

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    other = _other()
    secrets = NotFoundOnce({other.secret: TOKEN})
    broker = FakeBroker(outcomes=[FakeBroker().outcome, other])
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert env[ACCOUNT_TOKEN_ENV] == TOKEN
    assert broker.releases == [ACCOUNT_ID], "the unusable one went straight back"
    assert "NotFound" in broker.unusable[0], "and the broker was told why"
    assert broker.excludes[1] == (ACCOUNT_ID,), "so it is not offered again"


def test_a_borrowed_account_the_worker_cannot_read_parks_rather_than_failing(
    db, worker_factory
):
    """LENDING IS THE FEATURE THAT MAKES A POOL WORTH HAVING, and as shipped it
    was the one case guaranteed to fail: the only grant of secretAccessor on
    `swarm-account-<owner>--<label>` is to the OWNER's worker service account,
    so a borrower's pod gets PermissionDenied on a secret the broker was happy
    to assign it. That used to fail the attempt, three times over.
    """
    from google.api_core import exceptions as gexc

    class Denied(FakeSecretClient):
        def access(self, name, version="latest"):
            self.accessed.append(name)
            raise gexc.PermissionDenied("Permission denied on secret")

    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=[])          # only the pool can serve this tenant
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=Denied({}))
    worker._account_broker = broker

    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert int(task.get("attempt_count", 1)) == 1, "an unreadable pool must not cost an attempt"
    assert task["park_reason"] == ParkReason.CREDENTIAL_MISSING.value, (
        "a missing IAM grant is an administrator's job, not a quota wait"
    )
    assert task["blocked_by"][0]["account_pool_reason"] == "account_unreadable"
    assert broker.releases == [ACCOUNT_ID] * 3, "every one it was given went back"


def test_the_worker_gives_up_after_a_bounded_number_of_accounts(
    db, worker_factory, tmp_path
):
    """Every miss is a Secret Manager call and a round trip while this worker
    holds a concurrency slot. Three in a row means the pool needs a person."""
    from agent_worker.lifecycle import MAX_ACCOUNT_TRIES
    from agent_worker.accountlease import NoAccountAvailable

    seed_attempt(db, runner_profile="claude-code")
    seed_tenant(db, credentials=["anthropic"])
    secrets = FakeSecretClient({ACCOUNT_SECRET: ""})
    broker = FakeBroker()
    worker, _, _ = _pool_worker(worker_factory, secrets, broker, tmp_path)

    with pytest.raises(NoAccountAvailable):
        worker._build_child_env()

    assert len(broker.assigns) == MAX_ACCOUNT_TRIES


# -- a refusal is a configuration error, not permission to carry on --------


def test_a_refused_worker_parks_instead_of_using_the_tenant_secret(
    db, worker_factory, secrets
):
    """"Not configured" must mean REFUSE. A 403 here is almost always the
    tenant's worker service account missing from the broker's run.invoker
    list, and falling back would put every agent in the fleet onto the one
    shared subscription -- the exact contention the pool removes -- while the
    pool's own dashboards showed it healthy and idle."""
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])   # a usable tenant secret EXISTS
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(
        assign_error=BrokerRefused("the quota broker refused this worker with 403")
    )

    assert worker.run() == ExitCode.PARKED
    task = db.doc("tasks/task_1")
    assert task["park_reason"] == ParkReason.CREDENTIAL_MISSING.value
    assert task["blocked_by"][0]["account_pool_reason"] == "broker_refused"
    assert TENANT_SECRET not in secrets.accessed, "it did not carry on regardless"


def test_a_stale_pool_waits_minutes_rather_than_a_quarter_of_an_hour(
    db, worker_factory, secrets
):
    """"Every reading is old" and "every account is spent" are different
    claims. The broker's own usage poll refreshes readings on its sweep, so a
    pool blocked only by staleness is minutes away, not hours."""
    from agent_worker.lifecycle import (
        NO_ACCOUNT_RETRY_SECONDS,
        STALE_READING_RETRY_SECONDS,
    )

    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    seed_tenant(db, credentials=["anthropic"])
    worker, _, _ = worker_factory(runner_profile="claude-code", secret_client=secrets)
    worker._account_broker = FakeBroker(NoAccount(reason="no_recent_reading"))
    worker.run()

    delay = db.doc("tasks/task_1")["next_eligible_at"] - datetime.now(timezone.utc)
    assert delay < timedelta(seconds=NO_ACCOUNT_RETRY_SECONDS - 60)
    assert delay > timedelta(seconds=STALE_READING_RETRY_SECONDS - 60)


# -- shaping the secret payload -------------------------------------------


def test_a_bare_payload_fills_only_the_oauth_variable():
    env = credential_env_from_account(
        f"  {TOKEN}\n",
        secret_env_names=("ANTHROPIC_API_KEY", ACCOUNT_TOKEN_ENV),
        secret_name=ACCOUNT_SECRET,
    )
    assert env == {ACCOUNT_TOKEN_ENV: TOKEN}


def test_an_account_secret_holds_one_thing_and_it_is_the_access_token():
    """The pool handles SUBSCRIPTIONS and nothing else.

    `{base}` is written by the broker -- the single writer -- as the bare
    access token, so a JSON object there describes a credential shape that does
    not exist here. It used to be accepted, and whichever of the profile's
    declared names it carried were exported, which meant an account secret
    could supply ANTHROPIC_API_KEY. That is an API-key affordance in a pool
    that has no API keys, and it handed whoever could add a secret version a
    choice the design says nobody has.
    """
    env = credential_env_from_account(
        json.dumps({ACCOUNT_TOKEN_ENV: TOKEN, "CLAUDE_CODE_BIN": "/tmp/evil"}),
        secret_env_names=("ANTHROPIC_API_KEY", ACCOUNT_TOKEN_ENV),
        secret_name=ACCOUNT_SECRET,
    )

    # Taken as the opaque token it is, into the one variable it belongs in.
    # Nothing is read OUT of it, so nothing in it can choose a binary or a
    # credential variable.
    assert list(env) == [ACCOUNT_TOKEN_ENV]
    assert "CLAUDE_CODE_BIN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_a_bare_payload_is_refused_for_a_profile_with_no_oauth_variable():
    with pytest.raises(ValueError, match="subscription token"):
        credential_env_from_account(
            TOKEN, secret_env_names=("ANTHROPIC_API_KEY",), secret_name=ACCOUNT_SECRET
        )


# -- the HTTP client ------------------------------------------------------


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capturing_urlopen(monkeypatch, body: str) -> list:
    import urllib.request

    seen: list = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_the_client_never_sends_a_tenant(monkeypatch):
    """The broker derives the tenant from the signed identity token. A tenant
    a caller can name is a tenant a caller can choose."""
    seen = _capturing_urlopen(
        monkeypatch,
        json.dumps({"account_id": ACCOUNT_ID, "secret": ACCOUNT_SECRET,
                    "assignment_id": ASSIGNMENT_ID, "account": {}}),
    )
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "id-token")

    assert broker.assign("anthropic").account_id == ACCOUNT_ID
    body = json.loads(seen[0].data.decode())
    assert body == {"provider": "anthropic"}
    assert seen[0].get_header("Authorization") == "Bearer id-token"


def test_an_assignment_with_no_id_is_refused(monkeypatch):
    """Without it this worker cannot give the hold back, so the account would
    count an agent that has already exited until the hold expired."""
    _capturing_urlopen(
        monkeypatch,
        json.dumps({"account_id": ACCOUNT_ID, "secret": ACCOUNT_SECRET}),
    )
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    with pytest.raises(BrokerUnavailable, match="without an assignment id"):
        broker.assign("anthropic")


def test_only_accounts_this_attempt_already_tried_are_excluded(monkeypatch):
    """`exclude` can only ever NARROW what this caller is offered, which is why
    it is safe to take from the body when a tenant is not."""
    seen = _capturing_urlopen(
        monkeypatch, json.dumps({"account_id": None, "reason": "no_account_available"})
    )
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    broker.assign("anthropic", exclude=(ACCOUNT_ID, ACCOUNT_ID, ""))

    assert json.loads(seen[0].data.decode()) == {
        "provider": "anthropic",
        "exclude": [ACCOUNT_ID],
    }


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_refusal_is_not_an_outage(monkeypatch, status):
    """401/403: Cloud Run's invoker IAM or the broker's identity check said no,
    which on this deployment means the tenant's worker service account is
    missing from the broker's run.invoker list. 404: the URL is not this API.
    All three are configuration, and `_lease_account` must NOT fall back."""
    import io
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, status, "no", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    with pytest.raises(BrokerRefused):
        broker.assign("anthropic")
    assert not isinstance(BrokerRefused("x"), BrokerUnavailable), (
        "a refusal must not be catchable as an outage, or the fallback would "
        "swallow it again"
    )


def test_no_account_is_a_result_and_not_an_exception(monkeypatch):
    """The whole shape of the route. An exception would be indistinguishable
    from the broker being down, and those two want opposite responses."""
    _capturing_urlopen(
        monkeypatch,
        json.dumps({"account_id": None, "reason": "no_account_available",
                    "next_reset_at": "2026-09-20T12:00:00+00:00"}),
    )
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    outcome = broker.assign("anthropic")
    assert isinstance(outcome, NoAccount)
    assert outcome.reason == "no_account_available"
    assert outcome.is_pool_absent is False


def test_an_assignment_with_no_secret_name_is_refused(monkeypatch):
    """It would otherwise become a silent fallback to the tenant credential
    while the broker's counter says an agent is on the account."""
    _capturing_urlopen(monkeypatch, json.dumps({"account_id": ACCOUNT_ID}))
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    with pytest.raises(BrokerUnavailable, match="without naming its secret"):
        broker.assign("anthropic")


def test_an_http_error_is_reported_as_unavailable(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"down")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    with pytest.raises(BrokerUnavailable):
        broker.assign("anthropic")


def test_the_release_path_escapes_an_account_id(monkeypatch):
    """`<tenant>:<label>` carries a colon, which must not be sent raw."""
    seen = _capturing_urlopen(monkeypatch, json.dumps({"assigned": 0}))
    broker = AccountBroker("https://broker.example/", logger=None,
                           token_fetcher=lambda aud: "t")

    broker.release(ACCOUNT_ID, ASSIGNMENT_ID)
    assert seen[0].full_url == (
        f"https://broker.example/v1/accounts/{TENANT}%3Apersonal/release"
    )
    assert json.loads(seen[0].data.decode()) == {"assignment_id": ASSIGNMENT_ID}


def test_a_release_names_the_assignment_and_can_say_it_was_unreadable(monkeypatch):
    """The id is what makes the release provable: `may_serve` says which
    accounts a caller COULD be assigned, not that it holds one now."""
    seen = _capturing_urlopen(monkeypatch, json.dumps({"assigned": 0}))
    broker = AccountBroker("https://broker.example", logger=None,
                           token_fetcher=lambda aud: "t")

    broker.release(ACCOUNT_ID, ASSIGNMENT_ID, unusable="NotFound: no version")

    assert json.loads(seen[0].data.decode()) == {
        "assignment_id": ASSIGNMENT_ID,
        "unusable": "NotFound: no version",
    }


def test_the_audience_defaults_to_the_broker_url(monkeypatch):
    """Cloud Run checks `aud` against the service URL, and a token minted for
    another audience is one the broker rejects."""
    _capturing_urlopen(monkeypatch, json.dumps({"account_id": None, "reason": "x"}))
    seen_audience: list[str] = []
    broker = AccountBroker(
        "https://broker.example/", logger=None,
        token_fetcher=lambda aud: seen_audience.append(aud) or "t",
    )

    broker.assign("anthropic")
    assert seen_audience == ["https://broker.example"]


def test_the_metadata_url_asks_for_the_right_audience(monkeypatch):
    """Off Google Cloud there is no metadata server; a local run falls back
    rather than failing, which is what `BrokerUnavailable` carries."""
    import urllib.request

    from agent_worker.accountlease import METADATA_IDENTITY_URL, fetch_identity_token

    seen = _capturing_urlopen(monkeypatch, "an-id-token")
    token = fetch_identity_token("https://broker.example")

    assert token == "an-id-token"
    assert seen[0].full_url.startswith(METADATA_IDENTITY_URL)
    assert "audience=https%3A%2F%2Fbroker.example" in seen[0].full_url
    assert seen[0].get_header("Metadata-flavor") == "Google"

    def refuse(req, timeout=None):
        raise OSError("no metadata server here")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(BrokerUnavailable):
        fetch_identity_token("https://broker.example")


# -- the wiring, which is what made all of the above unreachable -----------
#
# Every test above this line hands the Worker an `account_broker`. That is the
# right shape for testing the leasing logic and it is exactly what hid the
# defect that mattered most: the pool was complete on both sides, passed its
# whole suite, and ran on zero agents, because nothing ever set
# QUOTA_BROKER_URL on a worker and the broker's Cloud Run invoker list did not
# include tenant worker service accounts. These build the client from the
# CONFIG and read the dispatcher's and terraform's own output instead.


REPO = Path(__file__).resolve().parents[3]


def _scheduler_settings(**overrides):
    """The REAL SchedulerSettings, not a stand-in.

    The point of these tests is that the value travels from the scheduler's
    configuration into a worker's environment, so a double with an invented
    field would assert nothing: a `SchedulerSettings` without
    `quota_broker_url` is exactly the state that made the pool inert.
    """
    from scheduler.settings import SchedulerSettings
    from swarm_common.config import Settings

    return SchedulerSettings(
        core=Settings(
            project_id=PROJECT,
            region="us-central1",
            environment="test",
            firestore_database="swarm",
            artifact_bucket=f"{PROJECT}-swarm-artifacts",
        ),
        project_id=PROJECT,
        region="us-central1",
        **overrides,
    )


def _a_tenant():
    return Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz",
        created_at=utcnow(), credentials=["anthropic"],
        service_account=f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{TENANT}",
    )


def _a_task():
    return Task(
        id="task_1", tenant_id=TENANT, created_at=utcnow(), updated_at=utcnow(),
        state=TaskState.LEASED, runner_profile="claude-code",
        resource_class=RUNNER_PROFILES["claude-code"].resource_class, input={},
        submitted_by="alice@saga.xyz", provider="anthropic", timeout_seconds=1800,
    )


def _a_lease(task=None):
    task = task or _a_task()
    return Lease(
        lease_id="lease_1", task_id=task.id, attempt_id="att_1",
        tenant_id=task.tenant_id, generation=3, pools=["global"], units=1,
        state=TaskState.LEASED, created_at=utcnow(),
        dispatch_deadline=utcnow() + timedelta(minutes=5),
        expires_at=utcnow() + timedelta(minutes=2),
    )


def test_a_worker_builds_its_own_broker_from_the_config(db, worker_factory):
    """No injection. `WorkerConfig.quota_broker_url` is the whole switch, and
    a worker that does not build a client from it cannot use the pool however
    correct the client is."""
    from agent_worker.accountlease import AccountBroker as RealBroker

    seed_attempt(db, runner_profile="claude-code")
    worker, config, _ = worker_factory(
        runner_profile="claude-code",
        quota_broker_url="https://swarm-quota-broker.example.run.app",
        quota_broker_audience="https://swarm-quota-broker.dev.swarm.internal",
    )

    assert config.quota_broker_url == "https://swarm-quota-broker.example.run.app"
    assert isinstance(worker._account_broker, RealBroker)
    assert worker._account_broker._audience == (
        "https://swarm-quota-broker.dev.swarm.internal"
    ), "the broker declares a custom audience; a token for the URL is rejected"


def test_the_dispatcher_puts_the_broker_url_in_the_worker_environment():
    """`worker_env()` is the single source of a worker's execution environment
    for BOTH backends, so a name it does not carry is a name no worker sees."""
    from scheduler.dispatch import worker_env

    env = worker_env(
        task=_a_task(), lease=_a_lease(), tenant=_a_tenant(),
        settings=_scheduler_settings(
            quota_broker_url="https://swarm-quota-broker.example.run.app",
            quota_broker_audience="https://swarm-quota-broker.dev.swarm.internal",
        ),
    )

    assert env["QUOTA_BROKER_URL"] == "https://swarm-quota-broker.example.run.app"
    assert env["QUOTA_BROKER_AUDIENCE"] == (
        "https://swarm-quota-broker.dev.swarm.internal"
    )
    # Still identifiers and endpoints only -- invariant 10.
    assert "IMAGE" not in env and "COMMAND" not in env


def test_a_deployment_with_no_pool_omits_the_name_rather_than_blanking_it():
    """A Cloud Run execution override MERGES with the Job's own environment, so
    an empty value here would override the URL terraform baked into the Job and
    turn a wired deployment back into an unwired one."""
    from scheduler.dispatch import worker_env

    env = worker_env(
        task=_a_task(), lease=_a_lease(), tenant=_a_tenant(),
        settings=_scheduler_settings(),
    )

    assert "QUOTA_BROKER_URL" not in env
    assert "QUOTA_BROKER_AUDIENCE" not in env


def test_terraform_sets_the_broker_url_on_every_worker_job():
    """The Cloud Run Jobs path, derived rather than declared: locals.tf reads
    module.cloud_run's outputs and nothing in module.cloud_run reads
    local.jobs, so there is no cycle and no operator step."""
    locals_tf = (REPO / "terraform" / "infra" / "locals.tf").read_text()
    jobs_env = locals_tf.split("  jobs = {", 1)[1].split("\n  }", 1)[0]

    assert "QUOTA_BROKER_URL" in jobs_env
    assert 'module.cloud_run.service_urls["swarm-quota-broker"]' in jobs_env
    assert 'local.push_audiences["swarm-quota-broker"]' in jobs_env


def test_terraform_lets_a_tenant_worker_call_the_broker():
    """Cloud Run rejects a caller holding no `run.invoker` before the
    application runs, so without this the worker's POST never reaches the
    broker's own identity check -- and the client would read the 403 as an
    outage and quietly use the tenant secret instead."""
    main_tf = (REPO / "terraform" / "infra" / "main.tf").read_text()
    broker_block = main_tf.split('"swarm-quota-broker" = {', 1)[1].split("\n    }", 1)[0]

    assert "module.tenancy.worker_members" in broker_block, (
        "the two routes a WORKER calls are on this service"
    )


def test_terraform_catches_a_stale_broker_url():
    """The GKE path cannot derive the URL -- the scheduler's environment is an
    input to the module whose output the URL is -- so it is declared, and a
    declared value goes stale. A stale one is worse than an empty one: a
    request to it is REFUSED rather than unanswered, and a refused worker parks
    every task instead of falling back."""
    main_tf = (REPO / "terraform" / "infra" / "main.tf").read_text()

    assert 'check "quota_broker_url_is_wired"' in main_tf
    assert 'module.cloud_run.service_urls["swarm-quota-broker"]' in main_tf


def test_the_environment_that_exists_actually_sets_it():
    """The defect was not "there is no way to set this", it was "nothing set
    it". A mechanism with no value in it is the same outage."""
    dev = (REPO / "terraform" / "environments" / "dev" / "dev.tfvars").read_text()

    assert "quota_broker_url" in dev
    assert 'quota_broker_url = ""' not in dev


# The other half of that alarm -- that the broker actually raises it -- is in
# tests/unit/control_plane/test_account_assign.py, where the broker's own
# fixtures live.


def test_a_tenant_whose_only_credential_is_a_pool_account_can_be_dispatched():
    """The mount that made the pool unusable for the tenants it was built for.

    Cloud Run resolves a `secretKeyRef` when the JOB is created, so a template
    naming `swarm-tenant-<tenant>-anthropic` for a tenant that has no such
    secret fails the create and the tenant never starts at all -- the pool
    could not replace the per-tenant secret for anybody.

    "Pool-only" means an account the tenant OWNS or is LENT serves it
    (scheduler/credentials.py, #169). The deployment merely having a broker
    is not enough, and the dispatcher no longer decides that on its own: it
    asks the question admission asked, of the same AccountPool.
    """
    from quota_broker.accounts import Account
    from scheduler.credentials import AccountPool
    from scheduler.dispatch import CloudRunJobDispatcher

    pool_only = Tenant(
        tenant_id=TENANT, kind="group", principal="eng@saga.xyz",
        created_at=utcnow(), credentials=[],          # no key of its own
        service_account=f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
    )
    settings = _scheduler_settings(
        quota_broker_url="https://swarm-quota-broker.example.run.app"
    )
    pool = AccountPool(
        broker_url=settings.quota_broker_url,
        read_accounts=lambda: [
            Account(account_id=ACCOUNT_ID, owner_tenant=TENANT, label="personal")
        ],
    )
    job = CloudRunJobDispatcher(settings, client=object(), pool=pool)._build_job(
        RUNNER_PROFILES["claude-code"], pool_only
    )

    container = job.template.template.containers[0]
    assert [e.name for e in container.env if e.value_source.secret_key_ref.secret] == []


def test_a_tenant_with_its_own_key_still_gets_its_own_secret_mounted():
    """The pool is an addition to a tenant's options, not a replacement for
    them. Invariant 9: the tenant's OWN secret, by the one spelling the frozen
    contract defines."""
    from scheduler.dispatch import CloudRunJobDispatcher

    settings = _scheduler_settings(
        quota_broker_url="https://swarm-quota-broker.example.run.app"
    )
    job = CloudRunJobDispatcher(settings, client=object())._build_job(
        RUNNER_PROFILES["claude-code"], _a_tenant()
    )

    container = job.template.template.containers[0]
    secrets = [e.value_source.secret_key_ref.secret for e in container.env
               if e.value_source.secret_key_ref.secret]
    assert set(secrets) == {f"swarm-tenant-{TENANT}-anthropic"}


def test_the_worker_and_the_broker_spell_the_reasons_the_same_way():
    """Two components, one vocabulary, and the worker picks a DIFFERENT park
    duration for each -- so a spelling that drifts does not fail, it silently
    takes the long fallback for everything. The broker owns the enum; this is
    the assertion that the worker's copy still matches it."""
    from quota_broker.accounts import Unavailable

    from agent_worker import accountlease as al

    assert {u.value for u in Unavailable} == {
        al.NO_ACCOUNTS_REGISTERED,
        al.NO_ACCOUNT_AVAILABLE,
        al.POOL_PAUSED,
        al.NO_RECENT_READING,
    }
