"""The search tool, the worker's half (docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

The agent asks for a web search through the `swarm-search` shim
(`agent_worker.searchmcp`), which writes one request per call into the spool
`work/.swarm-search/`. This module answers it, inside the worker -- the
process that holds the attempt key -- and never with the provider's key: that
is swarm-api's alone, a platform secret read per call there.

For each request it:

1. VALIDATES it: the tool is `search`, the arguments are exactly
   `searchmcp.server.ARGUMENTS`, the query is 1-400 characters with no control
   characters, the count 1-10. All of it before any request leaves.
2. SIGNS it with the attempt key (`children.AttemptKey`) and sends it with the
   worker's ID token (`children.ChildApi`) to
   `POST /v1/attempts/{attempt_id}/search` -- the git tools' authentication,
   reused (`gittools.GitTools.call`). A 409 is the fence (invariant 5):
   `SearchToolFenced` is raised, a `ChildSubmitFenced`, so the lifecycle ends
   the attempt through the one fenced exit, without touching the lease.
3. Writes swarm-api's answer, `{provider, query, results: [{title, url,
   snippet}], count}`, or its refusal's code and message, as the response.

THE CHECKS HERE ARE THE FAST ANSWER, NOT THE GATE. swarm-api re-checks the
flag, the proof, the fence, the profile and the tenant's daily cap on every
call. The per-attempt hourly budget here only spares a looping agent's round
trips.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .children import (
    AttemptKey,
    ChildApi,
    ChildApiError,
    ChildSubmitFenced,
    _read_small_json,
    _write_atomic,
)
from .gitmcp.server import MAX_REQUEST_BYTES, REQUEST_ID
from .searchmcp.server import (
    ARGUMENTS,
    DEFAULT_COUNT,
    MAX_COUNT,
    MAX_QUERY_CHARS,
    REQUIRED,
    SPOOL_DIR_NAME,
    SPOOL_ENV,
    TOOL_NAME,
)

#: Per attempt, per hour. swarm-api's cap is the tenant's per day; this only
#: keeps one looping agent from spending it in a burst.
CALLS_PER_HOUR = 60
_HOUR = 3600.0
#: The route's fenced answer. Any 409 from the route is read as this.
SEARCH_FENCED = "search_fenced"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SearchRefused(Exception):
    """A refusal: the code, a message for the agent, whether asking again may work."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "message": self.message,
                            "retryable": self.retryable}}


class SearchToolFenced(ChildSubmitFenced):
    """409 from the search route: this attempt is superseded (invariant 5)."""


@dataclass(frozen=True)
class SearchToolsConfig:
    """The attempt's fencing fields: the worker's own, never a request's."""

    tenant_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    generation: int
    calls_per_hour: int = CALLS_PER_HOUR


def validate_request(tool: Any, arguments: Any) -> tuple[str, int]:
    """(query, count), or `SearchRefused`. Shape only."""
    if tool != TOOL_NAME:
        raise SearchRefused("unknown_tool", f"the tool is {TOOL_NAME}")
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        raise SearchRefused("request_invalid", "arguments must be an object")
    unknown = sorted(set(args) - ARGUMENTS[TOOL_NAME])
    missing = sorted(REQUIRED[TOOL_NAME] - set(args))
    if unknown or missing:
        raise SearchRefused("request_invalid",
                            f"search takes {sorted(ARGUMENTS[TOOL_NAME])}; unknown {unknown}, "
                            f"missing {missing}")
    query = args.get("query")
    if not isinstance(query, str) or _CONTROL.search(query):
        raise SearchRefused("request_invalid", "query must be one line of text")
    query = " ".join(query.split())
    if not query or len(query) > MAX_QUERY_CHARS:
        raise SearchRefused("request_invalid",
                            f"query must be 1 to {MAX_QUERY_CHARS} characters")
    count = args.get("count", DEFAULT_COUNT)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise SearchRefused("request_invalid", f"count must be an integer from 1 to {MAX_COUNT}")
    return query, count


class SearchTools:
    """The spool's relay. One per attempt."""

    def __init__(
        self,
        cfg: SearchToolsConfig,
        *,
        key: AttemptKey,
        api: ChildApi,
        logger: Any,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._key = key
        self._api = api
        self._clock = clock
        self._wall = wall
        self._calls: deque[float] = deque()

    # -- the spool -----------------------------------------------------------

    @staticmethod
    def spool(work: Path) -> Path:
        return Path(work) / SPOOL_DIR_NAME

    def prepare(self, work: Path) -> dict[str, str]:
        """Create the spool; the agent's variable naming it."""
        root = self.spool(work)
        for name in ("requests", "responses"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return {SPOOL_ENV: str(root)}

    def has_requests(self, work: Path) -> bool:
        requests = self.spool(work) / "requests"
        return requests.is_dir() and any(requests.glob("*.json"))

    def tick(self, work: Path, *, limit: int | None = None) -> int:
        """Answer up to `limit` spooled requests (all when None). Returns how many.

        Raises `SearchToolFenced` after writing the agent its `fenced` answer."""
        root = self.spool(work)
        requests = root / "requests"
        if not requests.is_dir():
            return 0
        answered = 0
        for path in sorted(requests.glob("*.json")):
            if limit is not None and answered >= limit:
                break
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = root / "responses" / f"{request_id}.json"
            if response.exists():
                path.unlink(missing_ok=True)
                continue
            try:
                answer = self.answer_file(path, request_id)
            except SearchToolFenced:
                _write_atomic(response, SearchRefused(
                    "fenced", "this attempt no longer holds the task; it is ending").as_dict())
                path.unlink(missing_ok=True)
                raise
            _write_atomic(response, answer)
            path.unlink(missing_ok=True)
            answered += 1
        return answered

    def answer_file(self, path: Path, request_id: str) -> dict[str, Any]:
        data = _read_small_json(path, MAX_REQUEST_BYTES)
        if not isinstance(data, dict):
            return SearchRefused("request_invalid",
                                 f"a request is one JSON object of at most "
                                 f"{MAX_REQUEST_BYTES} bytes").as_dict()
        return self.answer(data.get("tool"), data.get("arguments"), request_id=request_id)

    def answer(self, tool: Any, arguments: Any, *, request_id: str) -> dict[str, Any]:
        """One request's answer: `{"tool", "result"}` or `{"refused"}`.

        Raises `SearchToolFenced` only. Logs codes and ids, never the query or
        a result: the query is the agent's, and the log is the platform's."""
        try:
            query, count = validate_request(tool, arguments)
            self._spend()
            result = self.call(query, count, request_id=request_id)
        except SearchRefused as refusal:
            self.log.warning("search tool refused", code=refusal.code, request_id=request_id)
            return refusal.as_dict()
        self.log.info("search tool answered", request_id=request_id,
                      results=len(result.get("results") or []))
        return {"tool": TOOL_NAME, "result": result}

    def _spend(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= _HOUR:
            self._calls.popleft()
        if len(self._calls) >= self.cfg.calls_per_hour:
            raise SearchRefused("rate_limited",
                                f"at most {self.cfg.calls_per_hour} searches an hour per attempt",
                                retryable=True)
        self._calls.append(now)

    # -- the signed call -------------------------------------------------------

    def route(self) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/search"

    def call(self, query: str, count: int, *, request_id: str) -> dict[str, Any]:
        """POST the search, signed with the attempt key; the body carries the
        attempt's fencing fields. A 409 raises `SearchToolFenced`."""
        body = json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "request_id": request_id,
            "query": query,
            "count": count,
        }, sort_keys=True).encode("utf-8")
        route = self.route()
        headers = self._key.headers("POST", route, body, now=self._wall())
        try:
            _, payload = self._api.call("POST", route, body=body, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409 or exc.code in (SEARCH_FENCED, "child_submit_fenced"):
                raise SearchToolFenced(exc.status, SEARCH_FENCED, exc.message,
                                       retryable=False) from None
            if exc.status == 404:
                raise SearchRefused("search_unavailable",
                                    "the platform's search tool is switched off; search in the "
                                    "live browser instead") from None
            if exc.retryable:
                raise SearchRefused(exc.code if exc.status else "api_unavailable",
                                    exc.message or "the platform did not answer; ask again",
                                    retryable=True) from None
            raise SearchRefused(exc.code, exc.message or exc.code) from None
        return payload
