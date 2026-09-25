"""The scheduler expects a pool account exactly where the worker would ask for one.

Admission lets a tenant with no key of its own through when a pool account
serves the profile (scheduler/credentials.py, #169). The worker is what then
has to find that account, and it asks the broker only for a profile whose
`secrets` declare the subscription-token variable (`lifecycle._lease_account`,
`accountlease.ACCOUNT_TOKEN_ENV`). The scheduler cannot import the worker -- its
image does not carry it -- so it states that variable itself, as
`credentials.SUBSCRIPTION_TOKEN_ENV`.

A restatement is the defect this repository keeps having (docs/mirrored-values.md),
so this compares BEHAVIOUR as well as the constant: for every profile in the
catalogue, a keyless tenant, and a broker that would hand an account to anyone
who asked, does the real worker ask the pool, and does the scheduler expect
it to? If the two ever disagree:

  * the scheduler expects the pool and the worker does not ask it: a keyless
    tenant is admitted, a container starts, and the worker parks it on
    CREDENTIAL_MISSING at once. On every drain, because the credential sweep
    promotes it again;
  * the worker would ask and the scheduler does not expect it: a tenant the
    pool serves is parked at admission and never runs.
"""

from __future__ import annotations

import io

from agent_worker import workspace as workspace_mod
from agent_worker.accountlease import ACCOUNT_TOKEN_ENV, Assignment
from agent_worker.secrets import CredentialMissing
from quota_broker.accounts import Account
from swarm_common.models import Tenant, utcnow
from swarm_common.profiles import RUNNER_PROFILES

from conftest import PROJECT, TENANT, build_worker, seed_attempt, seed_tenant
from fakes import FakeFirestore, FakeSecretClient

ACCOUNT_ID = f"{TENANT}:personal"
ACCOUNT_SECRET = f"swarm-account-{TENANT}--personal"


class AnyoneBroker:
    """Hands an account to whoever asks, and records who did."""

    def __init__(self) -> None:
        self.asked: list[str | None] = []

    def assign(self, provider, *, exclude=()):
        self.asked.append(provider)
        return Assignment(
            account_id=ACCOUNT_ID,
            secret=ACCOUNT_SECRET,
            assignment_id="assignment-parity",
            account={"account_id": ACCOUNT_ID, "owner_tenant": TENANT, "label": "personal"},
        )

    def release(self, account_id, assignment_id, *, unusable=""):
        return 0


def worker_asks_the_pool(profile_name: str, store, tmp_path) -> bool:
    """Run the REAL worker's credential resolution for a keyless tenant."""
    (tmp_path / profile_name).mkdir(parents=True, exist_ok=True)
    db = FakeFirestore()
    seed_attempt(db, runner_profile=profile_name)
    seed_tenant(db, credentials=[])
    broker = AnyoneBroker()
    worker, _, _ = build_worker(
        db,
        store,
        tmp_path / profile_name,
        io.StringIO(),
        runner_profile=profile_name,
        secret_client=FakeSecretClient({ACCOUNT_SECRET: "sk-ant-oat01-parity"}),
    )
    worker._account_broker = broker
    worker.ws = workspace_mod.create(tmp_path / profile_name / "ws", "att_1")
    try:
        worker._build_child_env()
    except CredentialMissing:
        # No key and no account asked for: the worker's own CREDENTIAL_MISSING.
        pass
    return bool(broker.asked)


def scheduler_expects_the_pool(profile_name: str) -> bool:
    from scheduler.credentials import AccountPool, CredentialSource, credential_for

    profile = RUNNER_PROFILES[profile_name]
    tenant = Tenant(
        tenant_id=TENANT,
        kind="group",
        principal="eng@saga.xyz",
        created_at=utcnow(),
        credentials=[],
        service_account=f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com",
    )
    # An account the tenant owns, of the profile's own provider: everything
    # the pool itself can check is satisfied, so only the PROFILE decides.
    serving = [
        Account(
            account_id=ACCOUNT_ID,
            owner_tenant=TENANT,
            label="personal",
            provider=profile.provider or "anthropic",
        )
    ]
    pool = AccountPool(
        broker_url="https://swarm-quota-broker.example.run.app",
        read_accounts=lambda: serving,
    )
    return credential_for(profile, tenant, pool).source is CredentialSource.ACCOUNT_POOL


def test_the_scheduler_and_the_worker_name_the_same_token_variable():
    from scheduler.credentials import SUBSCRIPTION_TOKEN_ENV

    assert SUBSCRIPTION_TOKEN_ENV == ACCOUNT_TOKEN_ENV


def test_the_scheduler_expects_the_pool_exactly_where_the_worker_asks_it(store, tmp_path):
    visited: list[str] = []
    worker_side: set[str] = set()
    scheduler_side: set[str] = set()
    for name in sorted(RUNNER_PROFILES):
        visited.append(name)
        if worker_asks_the_pool(name, store, tmp_path):
            worker_side.add(name)
        if scheduler_expects_the_pool(name):
            scheduler_side.add(name)

    # Every profile was asked, not one (CLAUDE.md: a loop that ran once
    # reports a clean sweep over a single item).
    assert visited == sorted(RUNNER_PROFILES), visited
    assert scheduler_side == worker_side, (
        f"the scheduler expects a pool account for {sorted(scheduler_side)}, "
        f"the worker asks for one for {sorted(worker_side)}"
    )
    # Not vacuous in either direction: some profile runs on the pool, and the
    # release's GKE proof profile is not one of them.
    assert worker_side, "no profile in the catalogue can run on a pool account"
    assert "browser" not in worker_side
