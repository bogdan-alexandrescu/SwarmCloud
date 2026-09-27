"""Can this tenant run this runner profile, and on which credential? Stated once.

`credential_for` is the ONE statement of the rule. Three call sites ask it and
nothing else in the control plane restates it:

  * admission (`loop.Scheduler._admit_one`), which parks CREDENTIAL_MISSING
    when it says no -- before any lease, so nothing is reserved (invariant 1);
  * the credential sweep (`loop.Scheduler._promote_credentials`), which
    returns a CREDENTIAL_MISSING park to READY when it says yes;
  * the Cloud Run Job's secret mount (`dispatch.CloudRunJobDispatcher.
    _build_job`), which names the tenant's own secret unless the answer is a
    pool account.

The API does not ask. It used to park a keyless tenant's task at submission
with its own copy of the rule, and now writes READY and leaves the question
to admission (swarm_api/service.py).

WHY THIS MODULE EXISTS (#169). Admission parked every task whose tenant held
no key for the profile's provider -- "admitting this would start a container
that can only fail" -- while dispatch had a branch for exactly that tenant:
"a pool account IS this tenant's credential". Admission ran first, so the
branch was unreachable and no keyless tenant ever ran on the account pool.
Each of the three statements read correctly on its own. They disagreed.

WHAT A POOL ACCOUNT CAN SERVE. Narrower than dispatch's comment said, and
read from the two components that act on it:

  * ONLY THE TENANT THAT OWNS IT AND THE TENANTS IT IS LENT TO. The pool is
    per-tenant with explicit lending (quota_broker/accounts.py). A deployment
    having a pool does NOT mean the pool serves every tenant in it: a personal
    tenant nobody lent an account to gets `no_accounts_registered` from the
    broker, falls back to its own secret, and has none. Invariant 9 is the
    reason. `quota_broker.accounts.accounts_serving` states this, and the
    broker's assign route calls the same function.
  * ONLY ITS OWN PROVIDER. Same function.
  * ONLY A PROFILE THAT TAKES A SUBSCRIPTION TOKEN. An account is a Claude
    subscription. Its token fills CLAUDE_CODE_OAUTH_TOKEN and nothing else, and
    the worker does not ask the pool for a profile whose `secrets` omit that
    name (`lifecycle._account_for`). `claude-code` declares it. `browser`
    declares ANTHROPIC_API_KEY only, so no account can run it, however many a
    tenant is lent.
  * ONLY ON A DEPLOYMENT WITH A BROKER. With no QUOTA_BROKER_URL no worker is
    told where the pool is (`dispatch.worker_env`).

The checks run in the worker's order, and each failure is named in the park's
event (`account_pool`), using the worker's own words for its declines, so an
operator reading a CREDENTIAL_MISSING can tell "register a key" from "lend an
account" from "this profile cannot run on an account at all".

WHAT IT DOES NOT ASK: whether an account has room now, or whether the broker
can be reached. A spent, paused or unobserved account is a wait the pool owns,
and the worker parks on it as PROVIDER_QUOTA_EXHAUSTED with the broker's own
reset instant (`lifecycle._park_no_account`). That is where the broker itself
draws the line: `no_accounts_registered` means the pool is not how this tenant
runs, and every other answer means it is, so wait.

SO AN ACCOUNT_POOL ANSWER DOES NOT END A WORKER'S WAIT. It is read from
Firestore, and it stays "yes" through a broker outage, a refused worker and a
spent window alike. Two sweeps in `loop.py` therefore also read the park's
own `next_eligible_at`:

  * the credential sweep promotes a CREDENTIAL_MISSING park answered
    ACCOUNT_POOL only once that instant has passed. A worker that could not
    reach the broker falls back to a key the tenant does not have and parks
    for an hour. Promoting it at once started a container on every drain that
    could only park again;
  * the prewarm sweep promotes a PROVIDER_QUOTA_EXHAUSTED park with no
    `provider:{p}:tenant:{t}` guard pool once that instant has passed, but not
    before. A tenant served only by a lent account has no such pool, because
    Terraform makes it from declared `providers`. The sweep used to skip
    such a task for ever.

WHICH CREDENTIAL IS REPORTED when a tenant has both a key and an account: the
key, because that is the answer that needs no read. At run time the worker asks
the pool first and falls back to the key, so a keyed tenant may still run on an
account. Nothing here depends on which one it uses. The answer decides only
whether the task may run and which secret a Cloud Run Job may name, and a keyed
tenant's own secret is always safe to name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

from quota_broker.accounts import Account, Unavailable, accounts_serving
from quota_broker.accountstore import AccountStore
from swarm_common.models import Tenant
from swarm_common.profiles import RunnerProfile

#: The variable a pool account's token fills.
#:
#: RESTATED from `agent_worker.accountlease.ACCOUNT_TOKEN_ENV`, because the
#: scheduler's image does not carry the worker and cannot import it.
#: tests/unit/worker/test_pool_credential_parity.py holds the two together by
#: asking the REAL worker, for every profile in the catalogue, whether it asks
#: the pool, and comparing that with this module's answer. Contract request 22
#: asks for one home in `swarm_common.profiles`.
SUBSCRIPTION_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


class CredentialSource(str, Enum):
    """What a task would run on, as far as admission can tell."""

    #: The profile names no provider (`mock`, `generic`).
    NOT_NEEDED = "not_needed"
    #: The tenant registered a key for the provider.
    TENANT_KEY = "tenant_key"
    #: No key, and an account the tenant owns or is lent serves the profile.
    ACCOUNT_POOL = "account_pool"
    #: Neither. The task parks CREDENTIAL_MISSING.
    MISSING = "missing"


class PoolAnswer(str, Enum):
    """What the account pool said, in the words the worker logs for each case."""

    #: The pool was not consulted: the profile needs no provider, or the
    #: tenant's own key answered first.
    NOT_ASKED = "not_asked"
    #: An account the tenant may use serves this profile's provider.
    SERVES = "serves"
    #: This deployment has no quota broker, so no worker is told of a pool.
    NO_BROKER_CONFIGURED = "no_broker_configured"
    #: The profile does not declare SUBSCRIPTION_TOKEN_ENV, so no account can
    #: supply its credential. The `browser` profile is one.
    PROFILE_TAKES_NO_SUBSCRIPTION = "profile_takes_no_subscription"
    #: No account of this provider is owned by or lent to the tenant. The
    #: broker's own spelling, taken from its enum rather than restated.
    NO_ACCOUNTS_REGISTERED = Unavailable.NO_ACCOUNTS_REGISTERED.value


@dataclass(frozen=True)
class CredentialAnswer:
    provider: str | None
    source: CredentialSource
    pool: PoolAnswer

    @property
    def runnable(self) -> bool:
        """THE PREDICATE: may this task be admitted at all."""
        return self.source is not CredentialSource.MISSING

    def park_detail(self) -> dict[str, Any]:
        """What a CREDENTIAL_MISSING park records in its `parked` event.

        `account_pool` is what scripts/prove-gke-dispatch.sh reads to say what
        would unpark the task: a key, a loan, or -- for a profile no account
        can run -- a key and nothing else.
        """
        return {"provider": self.provider, "account_pool": self.pool.value}

    def promote_detail(self) -> dict[str, Any]:
        """What the credential sweep records when it returns a park to READY."""
        return {
            "reason": "credential_available",
            "provider": self.provider,
            "credential": self.source.value,
        }


def _accounts_not_wired() -> Sequence[Account]:
    raise RuntimeError(
        "this AccountPool was built without a way to read the broker's accounts, "
        "and a keyless tenant's task needs one to know whether the pool serves it. "
        "Build it with AccountPool.for_deployment(settings, db) and hand the SAME "
        "instance to the Scheduler and to CloudRunJobDispatcher (main.build_scheduler "
        "does), so admission and the Job's secret mount read one answer."
    )


class AccountPool:
    """The broker's accounts, read the broker's way, at most once per drain.

    Read from Firestore directly, not asked of the broker over HTTP: the
    scheduler reads documents and never calls a service (settings.py,
    `quota_broker_url`), and `AccountStore` is the broker's own reader of the
    same collection in the same database, so the parsing is not restated
    either.

    READ LAZILY. Only a keyless tenant's task for a profile that takes a
    subscription token, on a deployment with a broker, ever needs it. A drain
    of keyed tenants' work reads nothing extra.

    READ ONCE PER DRAIN. `Scheduler.drain` calls `forget()` first, so a loan
    made or withdrawn between two drains is seen by the second, and every task
    inside one drain, at admission and at dispatch, is judged on the same list.
    That is also why the dispatcher must be handed the Scheduler's instance: a
    second instance would be read on a schedule nobody resets.
    """

    def __init__(
        self,
        *,
        broker_url: str,
        read_accounts: Callable[[], Sequence[Account]],
    ) -> None:
        self._configured = bool(str(broker_url or "").strip())
        self._read = read_accounts
        self._accounts: list[Account] | None = None

    @classmethod
    def for_deployment(cls, settings: Any, db: Any) -> "AccountPool":
        """The pool this deployment has: its broker setting, and its `accounts` collection."""
        return cls(
            broker_url=str(getattr(settings, "quota_broker_url", "") or ""),
            read_accounts=AccountStore(db).list,
        )

    @classmethod
    def unwired(cls, settings: Any) -> "AccountPool":
        """Knows whether a broker is configured, and raises if asked who it serves.

        What a CloudRunJobDispatcher built on its own gets. A tenant with a key
        and a profile with no provider never reach the read, so only a keyless
        tenant's Job on a deployment with a pool would, and for that one a loud
        failure is right: the alternative is a second, unsynchronised answer.
        """
        return cls(
            broker_url=str(getattr(settings, "quota_broker_url", "") or ""),
            read_accounts=_accounts_not_wired,
        )

    @property
    def configured(self) -> bool:
        return self._configured

    def forget(self) -> None:
        """Drop what was read. Called at the top of every drain."""
        self._accounts = None

    def serves(self, tenant_id: str, provider: str) -> bool:
        if self._accounts is None:
            self._accounts = list(self._read())
        return bool(accounts_serving(self._accounts, tenant_id, provider))


def credential_for(profile: RunnerProfile, tenant: Tenant, pool: AccountPool) -> CredentialAnswer:
    """THE RULE. See the module docstring for where each condition comes from."""
    provider = profile.provider
    if not provider:
        return CredentialAnswer(None, CredentialSource.NOT_NEEDED, PoolAnswer.NOT_ASKED)
    if provider in (tenant.credentials or ()):
        return CredentialAnswer(provider, CredentialSource.TENANT_KEY, PoolAnswer.NOT_ASKED)
    # From here the tenant has no key, and the pool is the only way it runs.
    # The order is the worker's (`lifecycle._account_for`).
    if not pool.configured:
        return CredentialAnswer(
            provider, CredentialSource.MISSING, PoolAnswer.NO_BROKER_CONFIGURED
        )
    if SUBSCRIPTION_TOKEN_ENV not in (profile.secrets or ()):
        return CredentialAnswer(
            provider, CredentialSource.MISSING, PoolAnswer.PROFILE_TAKES_NO_SUBSCRIPTION
        )
    if not pool.serves(tenant.tenant_id, provider):
        return CredentialAnswer(
            provider, CredentialSource.MISSING, PoolAnswer.NO_ACCOUNTS_REGISTERED
        )
    return CredentialAnswer(provider, CredentialSource.ACCOUNT_POOL, PoolAnswer.SERVES)


__all__ = [
    "SUBSCRIPTION_TOKEN_ENV",
    "AccountPool",
    "CredentialAnswer",
    "CredentialSource",
    "PoolAnswer",
    "credential_for",
]
