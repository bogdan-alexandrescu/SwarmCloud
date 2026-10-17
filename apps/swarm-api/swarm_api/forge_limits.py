"""GitHub's rate limit, read once for every module that asks GitHub (#1483).

A RATE-LIMITED ANSWER IS NOT AN ANSWER. GitHub answers a spent primary or
secondary rate limit with 403 -- the status it also uses for "you may not"
-- and sometimes 429, with `X-RateLimit-Remaining: 0`, a `Retry-After`, or
a message that says "rate limit". Read as a refusal, it told the owner on
2026-10-17 to "ask for write access" to a repository they administer, while
GitHub was answering 403 with `X-RateLimit-Remaining: 0` for their user.
So every check that decides MISSING or PERMISSION_MISSING from a 403 asks
`rate_limit` first, and a limited answer is FORGE_UNREACHABLE and `unknown`.

`gittokens` (the token probe), `access` (Work › Access) and `onboarding`
(the checklist, which reads the probe's words) import this one reading; the
classification is not restated anywhere else.

THE QUOTA LEDGER (QUOTA-THROTTLE, #1483). Measured 2026-10-17: background
reads -- the merge wake every minute per park, the after-PR gates, the
stranded-PR sweep, the index poll -- kept asking GitHub while one person's
5,000 calls an hour were spent, and kept the window pinned at zero (no
backoff: ~25-50 refused calls a minute). So every GitHub answer swarm-api's
request paths receive (`forgewrite.GitHubWriter._call`, `forge.GitHubIssues
._get`, the index poll's conditional read, forgeperms' reader, the
installation mint) updates `QUOTAS`, ONE in-process ledger keyed by the
credential's non-secret identity -- `installation:<id>`, `tenant:<id>` (the
tenant's `-git` slot), `connection:<conn_id>` (a person's own connection),
`app` (the App JWT) -- and never by the token. The identity a request acts
as is set around it with `acting_as`; a request outside one updates nothing.

Background callers ask `QUOTAS.defer` before they read: under
`forge_quota_defer_percent` (settings, default 20) of the limit left, or
inside a hold a rate-limited answer set, they skip this tick's reads and log
one line with the identity, what is left and the reset. A person's action --
a console button, a refresh, a lane's publish -- never asks, so it is never
deferred: the reserve below the threshold is kept for exactly those.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping

log = logging.getLogger(__name__)

#: GitHub's words for both limits: "API rate limit exceeded for user ID
#: ...", "You have exceeded a secondary rate limit ...".
_MESSAGE = re.compile(r"rate limit", re.IGNORECASE)

#: How much of a refusal's body is read for its message: GitHub's error
#: bodies are a few hundred bytes.
_MESSAGE_BYTES = 65536

#: The words a probe records for a rate-limited read (`gittokens._Forge.get`),
#: and that `onboarding` recognises in a stored probe error. The reset, when
#: GitHub gave one, follows as "until HH:MM UTC".
SPENT = "with the rate limit spent"
_SPENT = re.compile(re.escape(SPENT) + r"(?: until (\d{2}:\d{2} UTC))?")


@dataclass(frozen=True)
class RateLimit:
    """A rate-limited answer: its status and, when GitHub said, when the
    limit resets (UTC)."""

    status: int
    reset: datetime | None


def _headers(answer: Any) -> dict[str, str]:
    raw = getattr(answer, "headers", None) or {}
    try:
        items = dict(raw).items()
    except (TypeError, ValueError):
        return {}
    return {str(k).lower(): str(v).strip() for k, v in items}


def _message(answer: Any) -> str:
    body = getattr(answer, "body", b"") or b""
    try:
        data = json.loads(bytes(body)[:_MESSAGE_BYTES].decode("utf-8"))
    except (TypeError, ValueError):
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else ""


def _reset(headers: dict[str, str], now: datetime) -> datetime | None:
    """`X-RateLimit-Reset` (epoch seconds) when GitHub sent it, else
    `Retry-After` (seconds, or an HTTP date)."""
    epoch = headers.get("x-ratelimit-reset", "")
    if epoch.isdigit():
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    after = headers.get("retry-after", "")
    if after.isdigit():
        try:
            return now + timedelta(seconds=int(after))
        except OverflowError:
            return None
    if after:
        try:
            at = parsedate_to_datetime(after)
        except (TypeError, ValueError, IndexError):
            return None
        return at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return None


def rate_limit(answer: Any, *, now: datetime | None = None) -> RateLimit | None:
    """The limit `answer` reports, or None when it is not rate-limited.

    `answer` is anything with `status`, `headers` and `body` -- an
    `forgeapp.HttpAnswer`, or a probe transport's response. Rate-limited is:
    a 429; a 403 with `X-RateLimit-Remaining: 0`; a 403 or 429 with
    `Retry-After`; a 403 whose message says "rate limit" (primary or
    secondary). Any other 403 is GitHub's real refusal and stays one."""
    try:
        status = int(answer.status)
    except (AttributeError, TypeError, ValueError):
        return None
    if status not in (403, 429):
        return None
    headers = _headers(answer)
    limited = (status == 429 or headers.get("x-ratelimit-remaining") == "0"
               or bool(headers.get("retry-after"))
               or bool(_MESSAGE.search(_message(answer))))
    if not limited:
        return None
    return RateLimit(status, _reset(headers, now or datetime.now(timezone.utc)))


def is_rate_limited(answer: Any) -> bool:
    """True when `answer` is GitHub limiting requests, not refusing them."""
    return rate_limit(answer) is not None


def until(limit: RateLimit | None) -> str | None:
    """The reset as people read it, "03:13 UTC", or None when GitHub gave none."""
    if limit is None or limit.reset is None:
        return None
    return limit.reset.astimezone(timezone.utc).strftime("%H:%M UTC")


def spent(limit: RateLimit | None) -> str:
    """A probe's words for a limited read, with the reset when known."""
    when = until(limit)
    return SPENT + (f" until {when}" if when else "")


def limited_copy(when: str | None) -> str:
    """The sentence a person reads for a rate-limited check. It names the
    reset and says nothing about permissions: none was checked."""
    if when:
        return (f"GitHub is limiting requests for this account until {when}; nothing was "
                "checked. Try again after that.")
    return ("GitHub is limiting requests for this account; nothing was checked. Try again "
            "in a few minutes.")


def copy_for(limit: RateLimit | None) -> str:
    return limited_copy(until(limit))


def copy_in(*texts: Any) -> str | None:
    """`limited_copy` for the first probe text that records a rate-limited
    read (`spent`), or None when none does."""
    for text in texts:
        if isinstance(text, str):
            found = _SPENT.search(text)
            if found:
                return limited_copy(found.group(1))
    return None


# --------------------------------------------------------------------------
# the quota ledger (QUOTA-THROTTLE)
# --------------------------------------------------------------------------

#: Background reads stop below this percent of a credential's limit. 20: on
#: 2026-10-17 a person's 5,000/h was spent by background reads, and what was
#: left could not carry their own push tests and lane publishes. A fifth of
#: the window (1,000 calls of 5,000) is what a person's own actions spent in
#: the busiest measured hour, so it is kept for them. Settings
#: `forge_quota_defer_percent` (FORGE_QUOTA_DEFER_PERCENT) overrides it; 0
#: turns the deferral off (a refused answer still holds, see DEFAULT_HOLD).
DEFAULT_DEFER_PERCENT = 20
#: How long a rate-limited answer that named no reset holds its credential:
#: one minute, the background ticks' own period, so the next tick asks once
#: rather than every park asking.
DEFAULT_HOLD = timedelta(seconds=60)
#: A quota GitHub gave no reset for is trusted for an hour, its window.
_WINDOW = timedelta(hours=1)
#: The App JWT's identity: the installation mints and forgeperms' App reads.
APP_CREDENTIAL = "app"

_ACTING: ContextVar[str | None] = ContextVar("forge_quota_credential", default=None)


def tenant_credential(tenant_id: str) -> str:
    """The identity of a tenant's `-git` slot."""
    return f"tenant:{tenant_id}"


def installation_credential(installation_id: int | str) -> str:
    """The identity of an App installation's tokens: one quota per installation."""
    return f"installation:{installation_id}"


def connection_credential(connection_id: str) -> str:
    """The identity of a person's own connection (`forgeapp.connection_id_for`)."""
    return f"connection:{connection_id}"


@contextmanager
def acting_as(credential: str | None) -> Iterator[None]:
    """Every GitHub answer received inside updates `credential`'s quota."""
    mark = _ACTING.set(credential or None)
    try:
        yield
    finally:
        _ACTING.reset(mark)


def acting() -> str | None:
    return _ACTING.get()


@dataclass(frozen=True)
class Answer:
    """A GitHub answer in the shape `rate_limit` reads, for a transport that
    answers a tuple."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class Quota:
    """One credential's quota on one GitHub resource, as GitHub last said it."""

    credential: str
    limit: int
    remaining: int
    reset_at: datetime | None
    resource: str
    observed_at: datetime

    def below(self, percent: float) -> bool:
        return self.limit > 0 and self.remaining * 100 < percent * self.limit

    def until(self) -> datetime:
        return self.reset_at or self.observed_at + _WINDOW

    def to_api(self) -> dict[str, Any]:
        """What a person may read: counts and times. No token, no secret."""
        return {"limit": self.limit, "remaining": self.remaining,
                "reset_at": self.reset_at.isoformat() if self.reset_at else None,
                "resource": self.resource, "observed_at": self.observed_at.isoformat()}


@dataclass(frozen=True)
class Deferral:
    """Why a background read of `credential` waits, and until when."""

    credential: str
    until: datetime
    quota: Quota | None

    def line(self) -> str:
        """The one log line's fields: the identity, what is left, the reset."""
        left = (f"{self.quota.remaining}/{self.quota.limit}" if self.quota is not None
                else "rate-limited")
        return (f"credential={self.credential} remaining={left} "
                f"reset={self.until.astimezone(timezone.utc).strftime('%H:%M:%S UTC')}")


class QuotaDeferred(Exception):
    """A background read not made: its credential's quota is low or held.
    Constant text; `deferral` says whose and until when."""

    def __init__(self, deferral: Deferral) -> None:
        super().__init__("deferred: GitHub quota")
        self.deferral = deferral


def _count(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


class QuotaLedger:
    """Every credential's last-read quota, and the holds rate-limited answers set."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotas: dict[str, dict[str, Quota]] = {}
        self._holds: dict[str, datetime] = {}

    def clear(self) -> None:
        with self._lock:
            self._quotas.clear()
            self._holds.clear()

    def observe(self, credential: str, headers: Any, *,
                now: datetime | None = None) -> Quota | None:
        """Record `X-RateLimit-Limit / -Remaining / -Reset / -Resource`."""
        found = _headers(_Headers(headers))
        limit, remaining = (_count(found.get("x-ratelimit-limit")),
                            _count(found.get("x-ratelimit-remaining")))
        if not credential or limit is None or remaining is None:
            return None
        at = _aware(now or _now())
        reset = _count(found.get("x-ratelimit-reset"))
        try:
            reset_at = datetime.fromtimestamp(reset, tz=timezone.utc) if reset else None
        except (OverflowError, OSError, ValueError):
            reset_at = None
        resource = found.get("x-ratelimit-resource") or "core"
        quota = Quota(credential, limit, remaining, reset_at, resource[:32], at)
        with self._lock:
            self._quotas.setdefault(credential, {})[quota.resource] = quota
        return quota

    def observe_answer(self, answer: Any, *, credential: str | None = None,
                       now: datetime | None = None) -> RateLimit | None:
        """Record `answer` for `credential` (else the one `acting_as` set) and
        answer `rate_limit(answer)`. A rate-limited answer also holds the
        credential until the reset GitHub named (DEFAULT_HOLD when none)."""
        at = _aware(now or _now())
        who = credential or acting()
        if who:
            self.observe(who, getattr(answer, "headers", None), now=at)
        limited = rate_limit(answer, now=at)
        if limited is not None and who:
            self.hold(who, limited.reset or at + DEFAULT_HOLD)
        return limited

    def hold(self, key: str, until: datetime) -> None:
        """Defer `key` -- a credential, or a sweep (`sweep_key`) -- until `until`."""
        until = _aware(until)
        with self._lock:
            if self._holds.get(key) is None or self._holds[key] < until:
                self._holds[key] = until

    def held(self, key: str, *, now: datetime) -> datetime | None:
        """Until when `key` is held, or None."""
        with self._lock:
            until = self._holds.get(key)
        return until if until is not None and until > _aware(now) else None

    def quota(self, credential: str) -> Quota | None:
        """The credential's `core` quota when read, else the lowest one read."""
        with self._lock:
            found = dict(self._quotas.get(credential) or {})
        if not found:
            return None
        if "core" in found:
            return found["core"]
        return min(found.values(), key=lambda q: q.remaining / q.limit if q.limit else 1.0)

    def defer(self, credential: str, percent: float, *, now: datetime) -> Deferral | None:
        """A Deferral when a background read of `credential` must wait: a
        hold is live, or its quota is under `percent` and has not reset."""
        at = _aware(now)
        quota = self.quota(credential)
        held = self.held(credential, now=at)
        if held is not None:
            return Deferral(credential, held, quota)
        if quota is not None and percent > 0 and quota.below(percent) and quota.until() > at:
            return Deferral(credential, quota.until(), quota)
        return None

    def quotas(self, credentials: list[str]) -> list[Quota]:
        """The quotas read for `credentials`, in that order; unread ones are left out."""
        found = [self.quota(c) for c in credentials]
        return [q for q in found if q is not None]


class _Headers:
    """`_headers` reads `.headers`: this lends it a bare mapping."""

    def __init__(self, headers: Any) -> None:
        self.headers = headers


#: The swarm-api process's one ledger.
QUOTAS = QuotaLedger()


def sweep_key(sweep: str, tenant_id: str) -> str:
    """The hold key of one background sweep of one tenant (`QUOTAS.hold`)."""
    return f"sweep:{sweep}:{tenant_id}"


def defer_percent(ctx: Any) -> float:
    """`forge_quota_defer_percent` from the context's settings, else the default."""
    value = getattr(getattr(ctx, "settings", None), "forge_quota_defer_percent", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
        return float(value)
    return float(DEFAULT_DEFER_PERCENT)


def log_deferred(what: str, tenant_id: str, deferral: Deferral) -> None:
    """The one line a deferred background read logs."""
    log.info("github quota: %s tenant=%s deferred %s", what, tenant_id, deferral.line())
