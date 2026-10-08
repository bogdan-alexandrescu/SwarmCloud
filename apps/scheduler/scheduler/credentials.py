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

THE GITHUB CONNECTION (docs/onboarding.md §3.3 step 2; #780, lane OB6). A
task that works a repository as its submitter reads that person's user slot
`git-u-<hex>`, kept fresh by swarm-api's refresh sweep. When the sweep could
not refresh it, the connection document says `refresh_failed`, and a worker
started on it holds a slot to clone with a token GitHub no longer takes. So
`credential_for`, given the task and a `ForgeConnections`, also answers "no"
when the task needs a user slot and the submitter's connection is not
`active`: `refresh_failed`, `revoked`, missing, or a state this module does
not know. Admission parks it CREDENTIAL_MISSING before any lease, and the
credential sweep asks the same question and returns it to READY once the
connection is `active` again.

Only admission and the sweep pass the task. The Cloud Run Job's secret mount
does not: it runs after the lease is reserved, and nothing about the forge is
decided in dispatch.py. It names the provider's secret, never the git one.

WHICH TASK NEEDS A USER SLOT is `needs_user_slot`, and only there. It reads
the task's `forge_credential` (contract request 54, request E of
docs/onboarding.md §3.3; owner decision 2026-10-07, lane OB5), which swarm-api
writes at submission and the spec signature covers: a `git-u-<hex>` suffix is
a person's user slot, and the connection is asked about. `git` -- or None, a
task written before the field -- is the tenant token, and a `git-r-<hex>`
repository token is not a person's either: neither asks about any connection,
exactly as before onboarding. Until OB5 this was derived the "Without it" way,
from the signed `submitted_by` and that person's grant; the grant is now the
worker's to re-read (§3.3 step 4), and the scheduler reads none.

The scheduler does not verify the signature, and need not: a task whose
`forge_credential` was rewritten fails the worker's spec check before any
credential is read. What a rewrite could do here is park a task, or admit one
the worker then refuses -- never hand a worker a credential.

The document shapes are OB3's (`swarm_api.forgeapp`) and §3.1's, read by id
and never written. The id recipes are RESTATED below because the scheduler's
image does not carry swarm-api; tests/unit/control_plane/
test_forge_connection_admission.py holds them to swarm-api's own functions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from quota_broker.accounts import Account, Unavailable, accounts_serving
from quota_broker.accountstore import AccountStore
from swarm_common.models import Task, Tenant
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


class ForgeAnswer(str, Enum):
    """What the submitter's GitHub connection said, for a task that was asked about it."""

    #: Not asked: the caller passed no task (the Job's secret mount), or the
    #: provider answer had already said no.
    NOT_ASKED = "not_asked"
    #: The task needs no user slot (`needs_user_slot`): its signed
    #: `forge_credential` is `git`, None or a repository token.
    NO_USER_SLOT = "no_user_slot"
    #: The connection is `active`: the refresh sweep keeps its token fresh.
    ACTIVE = "active"
    #: swarm-api's sweep could not refresh the token. The person reconnects.
    REFRESH_FAILED = "refresh_failed"
    #: The person disconnected.
    REVOKED = "revoked"
    #: A grant names the submitter, and no connection of theirs exists in the
    #: tenant.
    CONNECTION_MISSING = "connection_missing"
    #: The document's `state` is none of OB3's three. Not runnable: a state
    #: nobody here knows is not evidence of a token that works.
    UNRECOGNISED = "unrecognised"


#: The only connection answers that let a task run.
_FORGE_RUNNABLE = frozenset({ForgeAnswer.NOT_ASKED, ForgeAnswer.NO_USER_SLOT, ForgeAnswer.ACTIVE})


@dataclass(frozen=True)
class CredentialAnswer:
    provider: str | None
    source: CredentialSource
    pool: PoolAnswer
    forge: ForgeAnswer = ForgeAnswer.NOT_ASKED

    @property
    def runnable(self) -> bool:
        """THE PREDICATE: may this task be admitted at all."""
        return self.source is not CredentialSource.MISSING and self.forge in _FORGE_RUNNABLE

    def park_detail(self) -> dict[str, Any]:
        """What a CREDENTIAL_MISSING park records in its `parked` event.

        `account_pool` is what scripts/prove-gke-dispatch.sh reads to say what
        would unpark the task: a key, a loan, or -- for a profile no account
        can run -- a key and nothing else. `forge_connection`, present only
        when the connection was asked about, names the GitHub half: a person
        who must reconnect.
        """
        detail: dict[str, Any] = {"provider": self.provider, "account_pool": self.pool.value}
        if self.forge is not ForgeAnswer.NOT_ASKED:
            detail["forge_connection"] = self.forge.value
        return detail

    def promote_detail(self) -> dict[str, Any]:
        """What the credential sweep records when it returns a park to READY."""
        detail: dict[str, Any] = {
            "reason": "credential_available",
            "provider": self.provider,
            "credential": self.source.value,
        }
        if self.forge is not ForgeAnswer.NOT_ASKED:
            detail["forge_connection"] = self.forge.value
        return detail


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


# --------------------------------------------------------------------------
# The GitHub connection (OB6)
# --------------------------------------------------------------------------

#: `swarm_api.forgeapp.CONNECTIONS` and `GRANTS`, and OB3's connection states
#: (`ACTIVE`, `REFRESH_FAILED`, `REVOKED`). Restated: see the module docstring.
CONNECTIONS = "forge_connections"
GRANTS = "forge_grants"
_STATES = {
    "active": ForgeAnswer.ACTIVE,
    "refresh_failed": ForgeAnswer.REFRESH_FAILED,
    "revoked": ForgeAnswer.REVOKED,
}
#: `swarm_api.gittokens.FORGE` and `swarm_api.repositories.FORGE_HOST`.
_FORGE = "github"
_FORGE_HOST = "github.com"
#: `swarm_api.validation.MERGE_FORGE_HOSTS`: the hosts whose repositories
#: the API names by `owner/repo` and registers under a `repo_id`.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _hex16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _user_key(email: str) -> str:
    return (email or "").strip().lower()


def user_hash(email: str) -> str:
    """`swarm_api.forgeapp.user_hash`: 16 hex of sha256 of the lower-cased email."""
    return _hex16(_user_key(email))


def connection_id_for(tenant_id: str, email: str) -> str:
    """`swarm_api.forgeapp.connection_id_for`: `conn_` + 16 hex of tenant + user + forge."""
    return "conn_" + _hex16(tenant_id + _user_key(email) + _FORGE)


def repo_id_for(tenant_id: str, owner: str, repo: str) -> str:
    """`swarm_api.repositories.repo_id_for`: tenant + `github.com/` + lower-cased owner/repo."""
    return "repo_" + _hex16(f"{tenant_id}{_FORGE_HOST}/{f'{owner}/{repo}'.lower()}")


def github_repository(repository_url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` of a GitHub repository URL, else None.

    `swarm_api.validation.merge_repository`'s reading: https or scp-style ssh,
    a GitHub host with no port, exactly two path parts, `.git` dropped.
    """
    text = (repository_url or "").strip()
    if text.startswith("git@"):
        text = "ssh://" + text.replace(":", "/", 1)
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if host not in _GITHUB_HOSTS or port is not None:
        return None
    path = [part for part in parts.path.strip("/").split("/") if part]
    if len(path) != 2:
        return None
    owner, repo = path
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return (owner, repo) if owner and repo else None


def grant_id_for(tenant_id: str, email: str, repo_id: str) -> str:
    """§3.1: `forge_grants/{tenant_id}__{user_hash}__{repo_id}`."""
    return f"{tenant_id}__{user_hash(email)}__{repo_id}"


class ForgeConnections:
    """The submitter's grant and connection, read by id, at most once per drain each.

    Read lazily: a task with no GitHub repository reads nothing, and a
    submitter with no grant reads no connection. `forget()` is called at the
    top of every run, as `AccountPool.forget` is, so a reconnect made between
    two drains is seen by the second.

    TENANT-SCOPED (invariant 9). Both ids already contain the tenant, and a
    document is still taken only when its own `tenant_id` is the task's: a
    document that says otherwise is not this tenant's grant or connection.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._docs: dict[tuple[str, str], dict[str, Any] | None] = {}

    def forget(self) -> None:
        self._docs = {}

    def _read(self, collection: str, doc_id: str, tenant_id: str) -> dict[str, Any] | None:
        key = (collection, doc_id)
        if key not in self._docs:
            snap = self._db.collection(collection).document(doc_id).get()
            data = snap.to_dict() if snap.exists else None
            self._docs[key] = data if data and data.get("tenant_id") == tenant_id else None
        return self._docs[key]

    def grant(self, task: Task, repo_id: str) -> dict[str, Any] | None:
        """The submitter's grant for this repository in the task's tenant, or None."""
        return self._read(
            GRANTS, grant_id_for(task.tenant_id, task.submitted_by, repo_id), task.tenant_id
        )

    def connection_state(self, task: Task) -> ForgeAnswer:
        """The submitter's connection in the task's tenant, as a `ForgeAnswer`."""
        doc = self._read(
            CONNECTIONS, connection_id_for(task.tenant_id, task.submitted_by), task.tenant_id
        )
        if doc is None or _user_key(str(doc.get("user") or "")) != _user_key(task.submitted_by):
            return ForgeAnswer.CONNECTION_MISSING
        return _STATES.get(str(doc.get("state") or ""), ForgeAnswer.UNRECOGNISED)


#: A user slot's suffix, `swarm_api.gittokens.provider_suffix(Scope.USER, ...)`:
#: its hex is the person's `user_hash`. Anchored, like `FORGE_CREDENTIAL`.
_USER_SLOT = re.compile(r"git-u-([0-9a-f]{16})")


def user_slot_hash(task: Task) -> str | None:
    """The `user_hash` of the user slot the task's `forge_credential` names, or None."""
    match = _USER_SLOT.fullmatch(task.forge_credential or "")
    return match.group(1) if match else None


def needs_user_slot(task: Task, forge: ForgeConnections | None = None) -> bool:
    """THE ONE PLACE that decides whether a task runs on its submitter's user slot.

    The task's signed `forge_credential` (contract request 54): a `git-u-`
    suffix needs the slot; `git`, None or a `git-r-` repository token is the
    tenant's fallback and does not. Reads no document: `forge` is kept so no
    caller changes, and is not used.
    """
    del forge
    return user_slot_hash(task) is not None


def forge_answer(task: Task, forge: ForgeConnections) -> ForgeAnswer:
    """What the task's GitHub credential says about running it now.

    The connection is the submitter's (`submitted_by`, signed), and the slot
    must be theirs: swarm-api names a person's task by that person's own slot,
    so a slot whose hex is not the submitter's has no connection this task
    may rely on, and parks as one missing.
    """
    hashed = user_slot_hash(task)
    if hashed is None:
        return ForgeAnswer.NO_USER_SLOT
    if hashed != user_hash(task.submitted_by):
        return ForgeAnswer.CONNECTION_MISSING
    return forge.connection_state(task)


def credential_for(
    profile: RunnerProfile,
    tenant: Tenant,
    pool: AccountPool,
    *,
    task: Task | None = None,
    forge: ForgeConnections | None = None,
) -> CredentialAnswer:
    """THE RULE. See the module docstring for where each condition comes from.

    `task` and `forge` are passed by admission and the credential sweep, and
    by nobody else: the GitHub connection is asked about only when both are
    given, and only once the provider half has said yes.
    """
    answer = _provider_answer(profile, tenant, pool)
    if task is None or forge is None or not answer.runnable:
        return answer
    return CredentialAnswer(answer.provider, answer.source, answer.pool, forge_answer(task, forge))


def _provider_answer(profile: RunnerProfile, tenant: Tenant, pool: AccountPool) -> CredentialAnswer:
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
    "ForgeAnswer",
    "ForgeConnections",
    "PoolAnswer",
    "credential_for",
    "forge_answer",
    "needs_user_slot",
]
