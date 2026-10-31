"""The search tool, swarm-api's half: the route's checks, the daily cap, the
provider call and the audit (docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

Owner decision 2026-10-13 ("Both: search tool + proxy"): an agent on the live
browser searches through an API, then opens the results in the live browser,
instead of typing a query into Google's page and being answered 429 /sorry
(docs/browser-egress.md §0, §3.4). This module answers

    POST /v1/attempts/{attempt_id}/search   {query, count<=10, + the attempt's fencing fields}
      -> {"provider", "query", "results": [{title, url, snippet}], "count"}

OFF BY DEFAULT. `ApiSettings.search_tool_enabled` (SEARCH_TOOL_ENABLED) is
False unless a deployment sets it. Off, the route is a 404 before anything is
read, as if it did not exist, and the child-key registration answers
`search_tool: false`, so no worker names the tool to its agent at all
(`agent_worker.searchtools`). It is a feature flag, not a refusal switch
(`swarm_api.refusals`): its off position is "there is no such tool", and a
refusal switch's off position lets the request through.

EVERY CALL, IN THIS ORDER -- the git tools' order (`swarm_api.gittools`), and
their checks, reused rather than restated:

1. **who.** `ChildService.authenticate_worker` (the tenant's worker service
   account, audience-pinned) and `ChildService.verify_proof` (the attempt key
   this attempt registered before its agent existed). Every failure is ONE
   403, `search_unproven`, with one sentence, and writes nothing.
2. **the fence and the cap (invariant 5), in one transaction.** The task must
   be RUNNING under exactly this attempt's live lease with no cancel
   requested; a stale attempt is 409 `search_fenced`, its `fenced` audit row
   committed with the check. The task's runner profile must be a live-browser
   profile (`RunnerProfile.live_browser`), read from the task document, never
   from the request (invariant 10). The tenant's queries today (UTC) are
   counted in `search_quota/{tenant}__{date}`; at the cap the answer is 429
   `search_daily_cap`. A query that passes is counted BEFORE the provider is
   called, so a provider failure still spends one: the cap bounds what the
   platform can be billed, and a failed call can be billed too.
3. **the key**, read from Secret Manager per call (`search_api_key_secret`,
   a PLATFORM secret the operator stores with
   `scripts/create-secrets.sh --search-api-key --stdin`). Never in an
   environment variable, never cached past the request, never logged.
4. **the provider**, behind `SearchProvider`. Its failures are mapped to
   codes the agent can act on (`PROVIDER_ERRORS`), and every message is built
   from a status and a code, never from the provider's body, then scrubbed of
   the key anyway.

THE AUDIT. One row per proven call in `search_audit`, ids newest first per
attempt (`gittools.audit_id`). It records the query, because an audit of
searches that does not say what was searched answers nothing; the row is the
tenant's, read only through `GET /v1/tasks/{task_id}/search-calls` under the
task's own scope. It never records the key, a result's text or a URL.

INVARIANTS. Nothing here creates infrastructure demand (1) or touches
capacity (2, 3) or the lease (5: a fenced call is refused, never released).
The tenant's own audit rows and quota document only (9). The caller names a
query and a count, never a provider, a URL or a key (10).
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode, urlparse

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from .childkey import AttemptTuple
from .children import ChildService, ChildUnauthenticated, ChildUnproven, _lease_is_live, parse_body
from .errors import ApiError, NotFound
from .gittools import REQUEST_ID, audit_id
from .store import LEASES, TASKS, Store

log = logging.getLogger(__name__)

AUDIT = "search_audit"
QUOTA = "search_quota"

#: The most results one call returns. The owner's number (count <= 10).
MAX_COUNT = 10
DEFAULT_COUNT = 5
#: A query is a line a person would type, not a document.
MAX_QUERY_CHARS = 400
MAX_BODY_BYTES = 4 * 1024
#: What one result carries back, at most: the agent pays for every character.
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 500
MAX_URL_CHARS = 2048
#: How long the provider gets. The agent's shim waits minutes; one slow
#: provider must not hold a request thread for longer than this.
PROVIDER_TIMEOUT_SECONDS = 10.0

SEARCH_FENCED = "search_fenced"
UNPROVEN_MESSAGE = "this request is not proven for this attempt"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TAG = re.compile(r"<[^>]{0,200}>")


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------


class SearchUnproven(ApiError):
    status_code = 403
    code = "search_unproven"


class SearchFenced(ApiError):
    status_code = 409
    code = SEARCH_FENCED


class SearchRefused(ApiError):
    """A refusal the agent can act on. 422 unless said otherwise; NEVER 409,
    which the worker reads as the fence and ends its attempt on."""

    status_code = 422
    code = "search_refused"

    def __init__(self, code: str, message: str, *, status: int = 422,
                 retryable: bool = False) -> None:
        super().__init__(message, detail={"retryable": retryable})
        assert status != 409, "409 is the fence's alone"
        self.code = code
        self.status_code = status

    @property
    def result(self) -> str:
        return "failed" if self.status_code >= 500 else "refused"


# --------------------------------------------------------------------------
# the provider interface
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class ProviderError(Exception):
    """A provider's failure, by KIND only. `status` is the provider's HTTP
    status when there was one. Never carries the provider's body: a body can
    echo the request, and the request carried the key."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind if status is None else f"{kind} (HTTP {status})")
        self.kind = kind
        self.status = status


#: ProviderError.kind -> (code, status, retryable, what the agent is told).
PROVIDER_ERRORS: dict[str, tuple[str, int, bool, str]] = {
    "unauthorized": ("search_provider_refused", 502, False,
                     "the search provider refused the platform's key; an operator must "
                     "rotate it. Use the live browser for this search meanwhile"),
    "rate_limited": ("search_provider_busy", 429, True,
                     "the search provider is rate limiting the platform; ask again in a minute"),
    "bad_query": ("search_query_rejected", 422, False,
                  "the search provider rejected this query; rephrase it"),
    "unavailable": ("search_provider_unavailable", 503, True,
                    "the search provider did not answer; ask again"),
    "bad_answer": ("search_provider_failed", 502, True,
                   "the search provider's answer could not be read; ask again"),
}


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, count: int, *, key: str) -> list[SearchResult]: ...


#: (method, url, headers, timeout) -> (status, body). urllib by default; a
#: test passes a fake, so no test reaches the network.
Send = Callable[[str, str, Mapping[str, str], float], tuple[int, bytes]]


def _urllib_send(method: str, url: str, headers: Mapping[str, str],
                 timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(getattr(response, "status", 200)), response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ProviderError("unavailable") from None


def _text(value: Any, limit: int) -> str:
    """Plain text from a provider's field: tags off, entities decoded, one line."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG.sub("", value))
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text[:limit]


def _web_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS or _CONTROL.search(value):
        return ""
    parsed = urlparse(value)
    return value if parsed.scheme in ("http", "https") and parsed.netloc else ""


@dataclass
class BraveSearch:
    """Brave Search API, web search (the first provider; docs/search-tool.md §2).

    `GET https://api.search.brave.com/res/v1/web/search?q=&count=` with the
    key in `X-Subscription-Token`; results at `web.results[]` as `title`,
    `url` and `description` (which carries `<strong>` highlighting, removed).
    """

    send: Send = _urllib_send
    endpoint: str = "https://api.search.brave.com/res/v1/web/search"
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    name: str = "brave"

    def search(self, query: str, count: int, *, key: str) -> list[SearchResult]:
        url = f"{self.endpoint}?{urlencode({'q': query, 'count': count})}"
        headers = {"Accept": "application/json", "X-Subscription-Token": key}
        status, raw = self.send("GET", url, headers, self.timeout)
        if status in (401, 403):
            raise ProviderError("unauthorized", status)
        if status == 429:
            raise ProviderError("rate_limited", status)
        if status in (400, 422):
            raise ProviderError("bad_query", status)
        if status != 200:
            raise ProviderError("unavailable", status)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError("bad_answer", status) from None
        web = data.get("web") if isinstance(data, dict) else None
        if web is None and isinstance(data, dict):
            return []  # a query with no web results answers without the block
        rows = web.get("results") if isinstance(web, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("bad_answer", status)
        out: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            link = _web_url(row.get("url"))
            if not link:
                continue
            out.append(SearchResult(_text(row.get("title"), MAX_TITLE_CHARS) or link, link,
                                    _text(row.get("description"), MAX_SNIPPET_CHARS)))
            if len(out) >= count:
                break
        return out


#: Provider name -> how it is built. SEARCH_PROVIDER picks one; an unknown
#: name is refused at start (`ApiSettings.from_env`), never at the first call.
PROVIDERS: dict[str, Callable[[], SearchProvider]] = {
    "brave": BraveSearch,
}


# --------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------


class KeyUnavailable(Exception):
    """The platform's search key could not be read. The message names the
    secret and the reason class, never a value."""


class KeySource(Protocol):
    def read(self) -> str: ...


@dataclass
class SecretManagerKey:
    """The key's latest version, read per call (rotation needs no restart).

    The client is built on first use, never at construction: building the
    app reaches no Google API (CLAUDE.md, Writing Python)."""

    project_id: str
    secret: str
    client: Any = None

    def read(self) -> str:
        from google.api_core import exceptions as gexc

        if self.client is None:
            from google.cloud import secretmanager

            self.client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{self.project_id}/secrets/{self.secret}/versions/latest"
        try:
            version = self.client.access_secret_version(request={"name": name})
        except (gexc.NotFound, gexc.FailedPrecondition):
            raise KeyUnavailable(f"{self.secret} has no enabled version") from None
        except gexc.PermissionDenied:
            raise KeyUnavailable(f"swarm-api may not read {self.secret}") from None
        except Exception as exc:
            raise KeyUnavailable(f"{self.secret} could not be read ({type(exc).__name__})") from None
        try:
            value = bytes(version.payload.data).decode("utf-8").strip()
        except Exception:
            value = ""
        if not value:
            raise KeyUnavailable(f"{self.secret} is empty")
        return value


# --------------------------------------------------------------------------
# the body: exactly what `agent_worker.searchtools.SearchTools.call` sends
# --------------------------------------------------------------------------


class SearchBody(BaseModel):
    # Strict: a `provider`, a `url` or a `key` in the body is a 422 (invariant 10).
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=1)
    request_id: str = Field(pattern=REQUEST_ID)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    count: StrictInt = Field(default=DEFAULT_COUNT, ge=1, le=MAX_COUNT)


def offers_search(task: Mapping[str, Any] | None) -> bool:
    """Whether the task's profile is one the search tool is for: a live-browser
    profile, read from the catalogue by the task's own profile name."""
    name = task.get("runner_profile") if isinstance(task, Mapping) else None
    profile = RUNNER_PROFILES.get(name) if isinstance(name, str) else None
    return bool(profile is not None and profile.live_browser and profile.worker_action is None)


def quota_id(tenant_id: str, now: datetime) -> str:
    return f"{tenant_id}__{now.strftime('%Y-%m-%d')}"


@dataclass
class _Call:
    attempt: AttemptTuple
    request_id: str
    query: str
    count: int
    started: float
    fields: dict[str, Any] = field(default_factory=dict)

    def row(self, result: str, code: str | None, at: datetime) -> dict[str, Any]:
        t = self.attempt
        return {
            "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
            "lease_id": t.lease_id, "generation": t.generation, "tool": "search",
            "request_id": self.request_id, "query": self.query, "count": self.count,
            "provider": None, "results": None,
            **self.fields,
            "result": result, "code": code,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "at": at,
        }


def scrub(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


@dataclass
class SearchTool:
    """The route's logic. Built per request from the app context."""

    settings: Any
    db: Any
    store: Store
    children: ChildService
    provider: SearchProvider
    key: KeySource
    now: Callable[[], datetime] = utcnow

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any,
                     provider: SearchProvider | None = None,
                     key: KeySource | None = None) -> "SearchTool":
        children = ChildService(
            settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
            verifier=verifier, limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
        )
        settings = ctx.settings
        return cls(
            settings=settings, db=ctx.db, store=ctx.store, children=children,
            provider=provider or PROVIDERS[settings.search_provider](),
            key=key or SecretManagerKey(settings.project_id, settings.search_api_key_secret),
            now=ctx.now,
        )

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "search_tool_enabled", False))

    def handle(self, attempt_id: str, raw: bytes, *, authorization: str | None,
               proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        if not self.enabled:
            # Off is "there is no such tool": nothing read, nothing written.
            raise NotFound("no such route")
        self.children._require_configured()
        if len(raw) > MAX_BODY_BYTES:
            raise SearchRefused("too_large", f"a search request is at most {MAX_BODY_BYTES} bytes")
        body: SearchBody = parse_body(SearchBody, raw)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        self._prove(body, attempt, raw=raw, authorization=authorization, proof=proof,
                    timestamp=timestamp, path=path)
        query = " ".join(_CONTROL.sub(" ", body.query).split())
        call = _Call(attempt, body.request_id, query, body.count, time.monotonic())
        key = ""
        try:
            if not query:
                raise SearchRefused("search_query_empty", "the query is empty")
            self._admit(call)
            call.fields["provider"] = self.provider.name
            try:
                key = self.key.read()
            except KeyUnavailable as exc:
                log.error("search_key_unavailable tenant=%s attempt=%s (%s)",
                          attempt.tenant_id, attempt.attempt_id, exc)
                raise SearchRefused("search_unconfigured",
                                    "the platform's search key is not available; an operator "
                                    "stores it with scripts/create-secrets.sh --search-api-key",
                                    status=503) from None
            try:
                results = self.provider.search(query, body.count, key=key)
            except ProviderError as exc:
                code, status, retryable, message = PROVIDER_ERRORS.get(
                    exc.kind, PROVIDER_ERRORS["bad_answer"])
                log.warning("search_provider_error provider=%s kind=%s status=%s tenant=%s "
                            "attempt=%s", self.provider.name, exc.kind, exc.status,
                            attempt.tenant_id, attempt.attempt_id)
                raise SearchRefused(code, message, status=status, retryable=retryable) from None
        except SearchRefused as refused:
            refused.message = scrub(refused.message, key)
            if refused.code != "search_daily_cap":  # the cap's row is in its transaction
                self._audit(call, refused.result, refused.code)
            raise
        call.fields["results"] = len(results)
        self._audit(call, "ok", None)
        return {
            "provider": self.provider.name,
            "query": query,
            "results": [r.as_dict() for r in results[:body.count]],
            "count": min(len(results), body.count),
        }

    def _prove(self, body: SearchBody, attempt: AttemptTuple, *, raw: bytes,
               authorization: str | None, proof: str | None, timestamp: str | None,
               path: str) -> None:
        """The git tools' proof (`GitTools._prove`): one 403 for every cause."""
        try:
            self.children.authenticate_worker(authorization, body.tenant_id)
            if body.attempt_id != attempt.attempt_id:
                raise ChildUnproven("the body names another attempt")
            self.children.verify_proof(attempt, method="POST", path=path, body=raw,
                                       proof=proof, timestamp=timestamp)
        except (ChildUnauthenticated, ChildUnproven):
            raise SearchUnproven(UNPROVEN_MESSAGE) from None

    def _admit(self, call: _Call) -> None:
        """The fence, the profile and the tenant's daily cap, in one transaction."""
        attempt = call.attempt
        task_ref = self.db.collection(TASKS).document(attempt.task_id)
        lease_ref = self.db.collection(LEASES).document(attempt.lease_id)
        now = self.now()
        quota_ref = self.db.collection(QUOTA).document(quota_id(attempt.tenant_id, now))
        cap = int(getattr(self.settings, "search_daily_queries_per_tenant", 0))

        @firestore.transactional
        def _apply(txn: Any) -> str:
            task_snap = _snapshot(txn.get(task_ref))
            lease_snap = _snapshot(txn.get(lease_ref))
            quota_snap = _snapshot(txn.get(quota_ref))
            task = task_snap.to_dict() if task_snap.exists else None
            lease = lease_snap.to_dict() if lease_snap.exists else None
            quota = (quota_snap.to_dict() or {}) if quota_snap.exists else {}
            audit_ref = self.db.collection(AUDIT).document(audit_id(attempt.attempt_id, now))
            if (task is None or task.get("state") != TaskState.RUNNING.value
                    or task.get("cancel_requested")
                    or not _lease_is_live(task, lease, attempt, now)):
                # Returned, not raised: a transaction that raises commits nothing.
                txn.set(audit_ref, call.row("fenced", SEARCH_FENCED, now))
                return "fenced"
            if not offers_search(task):
                return "not_offered"
            used = quota.get("queries") if quota.get("tenant_id") == attempt.tenant_id else 0
            used = used if isinstance(used, int) and used >= 0 else 0
            if used >= cap:
                txn.set(audit_ref, call.row("refused", "search_daily_cap", now))
                return "capped"
            txn.set(quota_ref, {"tenant_id": attempt.tenant_id,
                                "date": now.strftime("%Y-%m-%d"),
                                "queries": used + 1, "updated_at": now})
            return "ok"

        outcome = _apply(self.db.transaction())
        if outcome == "fenced":
            raise SearchFenced("this attempt is not RUNNING under its live lease; it was "
                               "superseded, and nothing was searched")
        if outcome == "not_offered":
            raise SearchRefused("search_not_offered",
                                "the search tool is offered only to a live-browser task")
        if outcome == "capped":
            raise SearchRefused("search_daily_cap",
                                f"this tenant has used its {cap} searches for today (UTC); "
                                "search in the live browser instead, or ask again tomorrow",
                                status=429)

    def _audit(self, call: _Call, result: str, code: str | None) -> None:
        now = self.now()
        try:
            self.db.collection(AUDIT).document(audit_id(call.attempt.attempt_id, now)).set(
                call.row(result, code, now))
        except Exception as exc:  # the call's answer still returns, as the git tools'
            log.error("search_audit_write_failed tenant=%s task=%s attempt=%s (%s)",
                      call.attempt.tenant_id, call.attempt.task_id, call.attempt.attempt_id,
                      type(exc).__name__)

    def calls_of(self, tenant_id: str, task_id: str, *,
                 submitted_by: str | None, limit: int = 500) -> list[dict[str, Any]]:
        """A task's search calls, newest first (`audit_id` inverts the clock); 404 for a task not the caller's."""
        self.store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        query = (self.db.collection(AUDIT)
                 .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                 .where(filter=FieldFilter("task_id", "==", task_id))
                 .limit(limit))
        rows = [(snap.id, snap.to_dict() or {}) for snap in query.stream()]
        rows = [(i, r) for i, r in rows if r.get("tenant_id") == tenant_id
                and r.get("task_id") == task_id]
        rows.sort(key=lambda item: item[0])
        out = []
        for _, row in rows:
            at = row.get("at")
            out.append({**{k: v for k, v in row.items() if k != "at"},
                        "at": at.isoformat() if isinstance(at, datetime) else at})
        return out


# --------------------------------------------------------------------------
# the add-on's probe: one real search, as swarm-api, for a platform admin
# --------------------------------------------------------------------------

#: The one query the probe sends. Fixed: the caller names nothing (invariant 10).
PROBE_QUERY = "SwarmCloud"


def probe(settings: Any, *, provider: SearchProvider | None = None,
          key: KeySource | None = None) -> dict[str, Any]:
    """`swarm addon enable brave-search`'s check, answered by doing what an
    agent's search does: read the key as swarm-api, then send one search of
    one result to the provider (owner pick 2026-10-31, "admin-probe": a policy
    read said nothing about whether the key works or the flag is on).

    `{"state": "met"|"unmet", "reason", "provider", "results"}`. `reason` is
    `search_tool_disabled` while SEARCH_TOOL_ENABLED is off on this revision
    (nothing is read), `search_unconfigured` when the key cannot be read, and
    a `PROVIDER_ERRORS` code when the provider refuses it. No tenant's quota
    is spent and no audit row is written: this is no tenant's search. The
    answer never carries the key, a result or the provider's body."""
    name = str(getattr(settings, "search_provider", "brave"))
    if not getattr(settings, "search_tool_enabled", False):
        return {"state": "unmet", "reason": "search_tool_disabled", "provider": name, "results": 0}
    source = key or SecretManagerKey(settings.project_id, settings.search_api_key_secret)
    try:
        value = source.read()
    except KeyUnavailable as exc:
        log.warning("search_probe key_unavailable (%s)", exc)
        return {"state": "unmet", "reason": "search_unconfigured", "provider": name, "results": 0,
                "detail": str(exc)}
    engine = provider or PROVIDERS[name]()
    try:
        results = engine.search(PROBE_QUERY, 1, key=value)
    except ProviderError as exc:
        code = PROVIDER_ERRORS.get(exc.kind, PROVIDER_ERRORS["bad_answer"])[0]
        log.warning("search_probe provider=%s kind=%s status=%s", engine.name, exc.kind, exc.status)
        return {"state": "unmet", "reason": code, "provider": engine.name, "results": 0,
                "detail": scrub(str(exc), value)}
    log.info("search_probe provider=%s results=%d", engine.name, len(results))
    return {"state": "met", "provider": engine.name, "results": len(results)}
