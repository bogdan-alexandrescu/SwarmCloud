"""Leasing an account from the pool, and giving it back.

WHAT CHANGES HERE. Until now a worker read exactly one secret --
`swarm-tenant-<tenant>-<provider>` -- and every agent for a tenant ran on the
same subscription. That is one account's five-hour window shared by every
concurrent agent the tenant runs, so the second agent slows the first and the
fourth stops it. The account pool exists to spread those agents across several
subscriptions; this module is the worker's half of it.

THE SHAPE OF THE CALL, AND WHY IT IS THIS SHAPE
-----------------------------------------------
Two POSTs, no state, no retry loop:

    POST /v1/accounts/assign     {provider, exclude}
                                 -> {account_id, assignment_id, secret, ...}
    POST /v1/accounts/{id}/release  {assignment_id, unusable}
                                 -> {assigned}

The broker answers "no account for you" with a **200**, not an error, and this
client preserves that distinction all the way to the caller. `accounts.choose`
returning None means every account the tenant may use is spent or paused --
nobody did anything wrong, and the correct response is to park the task, which
costs nothing and resumes by itself. An exception here would be
indistinguishable from the broker being unreachable, and those two want
opposite responses: park versus carry on with the per-tenant secret.

So `assign()` returns an `Assignment` or a `reason`, and raises only when the
broker could not be reached or did not answer in this shape.

A REFUSAL IS NOT AN OUTAGE
--------------------------
`BrokerUnavailable` and `BrokerRefused` are deliberately different types
because the caller must do opposite things with them. A connection that was
refused, a 5xx, a timeout: the broker is down, the per-tenant secret still
works, and degrading to it turns one control-plane outage into nothing. A 401,
a 403 or a 404: the pool IS configured -- somebody set a URL -- and this worker
is not allowed to use it, or is pointed at something that is not the broker.
Treating that as permission to carry on is the "not configured means
accept-anything" mistake wearing a different hat: every agent would run on the
one shared tenant subscription, which is the exact contention the pool exists
to remove, while the pool's own dashboards showed it healthy and idle. So a
refusal parks the task and names the cause, and somebody has to fix it.

AN ASSIGNMENT HAS AN ID, AND THE RELEASE CARRIES IT
---------------------------------------------------
The broker counts agents per account by recording a HOLD, and the hold's id
comes back with the assignment. Releasing names that id, so a release can only
ever give back the assignment it was issued for -- an account lent to several
tenants cannot have its counter driven to zero by a caller that never held it.
A hold also expires on its own, which is what makes a SIGKILLed worker cost a
few stale minutes rather than a permanently inflated count.

NO KEY MATERIAL CROSSES THIS WIRE
---------------------------------
`secret` is a Secret Manager secret NAME. The worker reads the value itself,
under its own service account, over Google's authorized path -- the same way it
has always read its tenant's credential. The broker never returns a token, so
this response is no more sensitive than the account listing an operator already
reads, and a log line carrying the whole body cannot leak a credential.

The name always points at `{base}`, the half that holds ONLY the access token.
The `{base}-refresh` half holds the pair and is readable by the broker alone,
because a pod that could mint successors forever is a pod whose compromise does
not expire.

AUTHENTICATION IS THE METADATA SERVER'S ID TOKEN
------------------------------------------------
The broker identifies its caller by the Google-signed OIDC token in the
Authorization header and derives the tenant from the service-account email in
it -- `worker_sa_pattern` pins the project, so a similarly named service
account in somebody else's project is not this tenant. The worker therefore
sends the token the metadata server mints for the broker's audience and sends
nothing else: it never names its own tenant, because a tenant a caller can name
is a tenant a caller can choose.

Standard library only, on purpose. `urllib` is what `forge.py` already uses for
GitHub; adding an HTTP dependency to the agent runtime image to make two POSTs
would be paid for on every image pull, forever.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

#: Where a Cloud Run or GKE workload asks for an identity token. `audience` is
#: what the receiving service checks, so it must be the broker's URL: a token
#: minted for any other audience is one `WorkerIdentity` will reject, and a
#: token minted with no audience at all is one google-auth does not check the
#: `aud` claim of -- which is the hole BROKER_AUDIENCE exists to close.
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

_TIMEOUT = 10
_UA = "swarmcloud-agent-worker"

#: The environment variable an account's access token fills.
#:
#: An account is a Claude SUBSCRIPTION, so its credential is an OAuth token and
#: belongs in the OAuth variable. Putting it in ANTHROPIC_API_KEY would offer a
#: subscription token to the metered API, which refuses it -- and the runner's
#: own `_credential_env` disambiguates by value prefix precisely because the two
#: are not interchangeable. A runner profile that does not declare this name
#: cannot run on a pool account at all, and asking for one would be a request
#: for a credential it has no variable to put.
ACCOUNT_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: The broker's answer when this tenant has NO account it may use -- not one
#: that is busy, not one that is paused: none at all. The pool is not how this
#: deployment runs, so the caller must fall back to the per-tenant secret. Every
#: deployment that has never registered an account gets this answer and behaves
#: exactly as it did before the pool existed.
NO_ACCOUNTS_REGISTERED = "no_accounts_registered"

#: Accounts exist for this tenant and none can take a new agent right now.
#: A WAIT. The caller parks; `next_reset_at` says until when if a window is
#: what is blocking.
NO_ACCOUNT_AVAILABLE = "no_account_available"

#: Every account this tenant may use is paused, draining or needs
#: re-authentication. Waiting on a PERSON, not on a clock, so there is no
#: instant to wake at and polling faster would only ask the same question.
POOL_PAUSED = "pool_paused"

#: Accounts exist and every one of them was last observed too long ago to
#: trust. NOT the same claim as "they are spent": nothing says they are. The
#: broker's usage poll refreshes readings on its own sweep, so this clears by
#: itself in minutes -- which is why it must not be parked for the same long
#: fallback as a genuinely exhausted pool.
NO_RECENT_READING = "no_recent_reading"

#: Client-side, never from the broker: the pool is configured and refused us.
BROKER_REFUSED = "broker_refused"

#: Client-side, never from the broker: an account was assigned and no account
#: this worker was offered could have its secret read.
ACCOUNT_UNREADABLE = "account_unreadable"

#: HTTP statuses that mean "configured, and refusing you" rather than "down".
#: 401/403: the invoker IAM or the identity check said no. 404: the URL is not
#: this API. Everything else -- 5xx, 429, a timeout, a refused connection -- is
#: an outage, and an outage degrades to the tenant secret.
REFUSAL_STATUSES = frozenset({401, 403, 404})


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached, or did not answer in this shape.

    Deliberately NOT raised when the broker says there is no account: that is a
    200 with a reason, and the caller's response to it is different. Collapsing
    the two would make an outage look like an empty pool, and every task would
    quietly run on the wrong credential instead of parking.

    Also deliberately NOT raised when the broker REFUSED the caller -- see
    `BrokerRefused`. The caller falls back to the tenant secret on this one, so
    anything that lands here is something the caller will carry on through.
    """


class BrokerRefused(RuntimeError):
    """The pool is configured and this worker is not allowed to use it.

    401 and 403: Cloud Run's invoker IAM or the broker's own identity check
    rejected the token -- typically the tenant's worker service account is
    missing from the broker's `run.invoker` list, or the audience does not
    match. 404: the URL names something that is not this API.

    NOT a subclass of `BrokerUnavailable`, and that is the whole point. These
    are configuration errors, and a configuration error must never be read as
    permission to carry on: the caller parks and names the cause, so somebody
    sees it. An unreachable broker is invisible-but-harmless; a refused one is
    invisible-and-wrong, because every agent silently piles back onto the one
    shared tenant subscription the pool exists to get them off.
    """


class AccountUnreadable(RuntimeError):
    """The account was assigned and its secret cannot be read.

    Three real causes, and none of them is the task's fault:

      * a freshly onboarded account whose `{base}` secret has no version yet --
        `scripts/account.sh` creates it empty on purpose and the broker
        publishes the access token on its next sweep, so there is a window
        between `account.sh add` and that sweep;
      * an account lent by another tenant whose secret this worker's service
        account was never granted `secretmanager.secretAccessor` on;
      * an empty or malformed version.

    Raised so the caller can hand THIS account back, ask for a different one,
    and park if there is none -- rather than failing the attempt, which would
    burn one of its three tries and, because `choose()` is deterministic, burn
    the next two on the same unusable account.
    """

    def __init__(self, account_id: str, detail: str) -> None:
        super().__init__(f"account {account_id} cannot be read: {detail}")
        self.account_id = account_id
        self.detail = detail


class NoAccountAvailable(RuntimeError):
    """The pool is how this tenant runs, and it has nothing to give right now.

    Raised only when the tenant HAS accounts and none can take a new agent --
    never when it has none at all, which is a fallback rather than a wait. The
    caller parks; it does not fail. Parking costs nothing, keeps the task's
    three attempts for real failures, and the scheduler brings it back by
    itself when `next_eligible_at` passes.
    """

    def __init__(self, decision: "NoAccount", provider: str) -> None:
        super().__init__(
            f"the {provider} account pool cannot serve this attempt: {decision.reason}"
        )
        self.decision = decision
        self.provider = provider


@dataclass(frozen=True)
class Assignment:
    """One account, as handed to a worker. `secret` is a NAME, never a value.

    `assignment_id` identifies the HOLD the broker recorded, not the account.
    It is what the release carries, so giving an account back can only ever
    give back this assignment -- and it is what lets the broker expire a hold
    whose worker was killed before it could release anything.
    """

    account_id: str
    secret: str
    assignment_id: str = ""
    account: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return str(self.account.get("label") or self.account_id)

    @property
    def owner_tenant(self) -> str:
        return str(self.account.get("owner_tenant") or "")


@dataclass(frozen=True)
class NoAccount:
    """Why no account was assigned, and when one is next expected.

    `next_reset_at` is an ISO instant or None. None does NOT mean "soon" and it
    does not mean one thing: an all-paused pool is waiting on a person, while a
    pool whose readings have all gone stale is waiting on the broker's next
    usage poll. `reason` is what separates them, and the caller picks a
    different wait for each -- inventing one retry time for both would either
    wake a paused pool up to the same answer or leave a merely-stale one asleep
    for a quarter of an hour.
    """

    reason: str
    next_reset_at: str | None = None

    @property
    def is_pool_absent(self) -> bool:
        return self.reason == NO_ACCOUNTS_REGISTERED


def fetch_identity_token(audience: str, *, timeout: int = _TIMEOUT) -> str:
    """A Google-signed OIDC token for `audience`, from the metadata server."""
    url = f"{METADATA_IDENTITY_URL}?audience={quote(audience, safe='')}&format=full"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Metadata-Flavor", "Google")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            token = response.read().decode("utf-8").strip()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        # Off Google Cloud there is no metadata server, which is an ordinary
        # local run rather than a fault. The caller falls back.
        raise BrokerUnavailable(f"no workload identity token available: {exc}") from exc
    if not token:
        raise BrokerUnavailable("the metadata server returned an empty identity token")
    return token


class AccountBroker:
    """The worker's client for the two pool routes.

    Holds no state between calls and performs no retries. A retry here would be
    a worker holding a concurrency slot while it waits on a control-plane
    service; the platform's answer to "try again later" is to park, which gives
    the slot back first.
    """

    def __init__(
        self,
        base_url: str,
        *,
        logger: Any,
        audience: str | None = None,
        token_fetcher: Any = None,
        timeout: int = _TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._log = logger
        # Cloud Run checks the token's `aud` against the service URL unless a
        # custom audience is configured, so the URL is the right default and an
        # override exists for deployments that set one.
        self._audience = (audience or self._base).rstrip("/")
        self._fetch_token = token_fetcher or fetch_identity_token
        self._timeout = timeout

    # -- the two calls ----------------------------------------------------

    def assign(
        self, provider: str, *, exclude: Any = ()
    ) -> Assignment | NoAccount:
        """Ask for an account for THIS worker's tenant.

        The tenant is never sent. The broker derives it from the service
        account in the identity token, which is the only version of the answer
        a caller cannot influence.

        `exclude` names accounts this attempt has ALREADY been given and could
        not use -- a fresh account whose secret has no version yet, or one lent
        by a tenant that never granted this worker access to its secret.
        Sending it is safe in a way naming a tenant is not: it can only ever
        narrow the caller's own options, never widen them, and without it
        `choose()` is deterministic and would hand back the same unusable
        account until the task ran out of attempts.
        """
        body: dict[str, Any] = {"provider": provider}
        excluded = sorted({str(a) for a in exclude if a})
        if excluded:
            body["exclude"] = excluded
        payload = self._post("/v1/accounts/assign", body)
        account_id = payload.get("account_id")
        if not account_id:
            return NoAccount(
                reason=str(payload.get("reason") or NO_ACCOUNT_AVAILABLE),
                next_reset_at=payload.get("next_reset_at") or None,
            )
        secret = payload.get("secret")
        if not isinstance(secret, str) or not secret:
            # An assignment whose secret name is missing would silently become
            # a fallback to the tenant credential while the broker's counter
            # says an agent is on the account. Refusing is loud and correct.
            raise BrokerUnavailable(
                f"the broker assigned {account_id} without naming its secret"
            )
        assignment_id = payload.get("assignment_id")
        if not isinstance(assignment_id, str) or not assignment_id:
            # Refused for the same reason: without it this worker cannot give
            # the hold back, so the account would count an agent that has
            # already exited until the hold expired.
            raise BrokerUnavailable(
                f"the broker assigned {account_id} without an assignment id"
            )
        account = payload.get("account")
        return Assignment(
            account_id=str(account_id),
            secret=secret,
            assignment_id=assignment_id,
            account=account if isinstance(account, dict) else {},
        )

    def release(
        self, account_id: str, assignment_id: str, *, unusable: str = ""
    ) -> Any:
        """Give this assignment back. Naming the hold, not just the account.

        `unusable` is set when the account was handed over and could not be
        used -- an unreadable secret. The broker records it against (account,
        THIS tenant) so the next agent is not sent at the same wall, and so an
        operator can see that an account nobody can read is not the same thing
        as an account nobody wants.
        """
        path = f"/v1/accounts/{quote(account_id, safe='')}/release"
        body: dict[str, Any] = {"assignment_id": assignment_id}
        if unusable:
            body["unusable"] = unusable[:200]
        payload = self._post(path, body)
        return payload.get("assigned")

    # -- transport --------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self._base}{path}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self._fetch_token(self._audience)}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _UA)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in REFUSAL_STATUSES:
                # CONFIGURED AND REFUSING. Not an outage, and the caller must
                # not carry on as though the pool were merely unused.
                raise BrokerRefused(
                    f"the quota broker refused this worker with {exc.code} on "
                    f"{path}: {detail}"
                ) from exc
            raise BrokerUnavailable(
                f"the quota broker answered {exc.code} on {path}: {detail}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            host = urlparse(url).hostname or self._base
            raise BrokerUnavailable(f"could not reach the quota broker at {host}: {exc}") from exc
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise BrokerUnavailable(f"the quota broker answered {path} with non-JSON") from exc
        if not isinstance(parsed, dict):
            raise BrokerUnavailable(f"the quota broker answered {path} with {type(parsed).__name__}")
        return parsed


def credential_env_from_account(
    payload: str,
    *,
    secret_env_names: tuple[str, ...],
    secret_name: str,
) -> dict[str, str]:
    """Shape an account secret's payload into the child's environment.

    ONE KIND OF CREDENTIAL, ONE VARIABLE. An account is a Claude SUBSCRIPTION
    and `{base}` holds only its access token, written as a bare string by the
    broker -- the single writer. There is no second shape to accept: this
    function used to also read a JSON object and export whichever of the
    profile's declared names it carried, which meant an account secret could
    supply ANTHROPIC_API_KEY. The pool does not handle API keys, so that branch
    described a credential kind that does not exist here and gave whoever could
    write a secret version a choice the design says nobody has.

    The token goes to ACCOUNT_TOKEN_ENV alone, never to every declared name.
    The two names claude-code declares are interchangeable inputs, not copies:
    projecting a subscription token into ANTHROPIC_API_KEY as well would hand
    the metered API a credential it refuses.
    """
    stripped = (payload or "").strip()
    if not stripped:
        raise ValueError(f"account secret {secret_name} is empty")

    if ACCOUNT_TOKEN_ENV not in secret_env_names:
        raise ValueError(
            f"this runner does not accept a subscription token: it declares "
            f"{', '.join(secret_env_names) or 'no credential variables'} and an "
            f"account supplies {ACCOUNT_TOKEN_ENV}"
        )
    return {ACCOUNT_TOKEN_ENV: stripped}


__all__ = [
    "ACCOUNT_TOKEN_ENV",
    "ACCOUNT_UNREADABLE",
    "BROKER_REFUSED",
    "METADATA_IDENTITY_URL",
    "NO_ACCOUNTS_REGISTERED",
    "NO_ACCOUNT_AVAILABLE",
    "NO_RECENT_READING",
    "POOL_PAUSED",
    "REFUSAL_STATUSES",
    "AccountBroker",
    "AccountUnreadable",
    "Assignment",
    "BrokerRefused",
    "BrokerUnavailable",
    "NoAccount",
    "NoAccountAvailable",
    "credential_env_from_account",
    "fetch_identity_token",
]
