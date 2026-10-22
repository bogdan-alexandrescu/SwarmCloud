"""The `swarm-state` tools, the worker's half (docs/design/agent-autonomy.md §3.4;
lane AU-4 STATE-READS, part of #1933).

The agent asks through the shim (`statemcp.server`), which writes one request
per call into the spool `work/.swarm-state/`. This module answers it, inside
the worker -- the process that holds the attempt key -- and never with a
credential the agent could hold.

For each request it:

1. VALIDATES it: the tool is one of `server.TOOLS`, the arguments are exactly
   that tool's (`server.ARGUMENTS`), each a short line of text or an integer.
   No tool takes a tenant, a namespace, a filter or a path, so none reaches
   swarm-api;
2. SIGNS a GET of `/v1/attempts/{attempt_id}/reads/{tool}` with the attempt
   key under `swarm-state-req/v1` (`statecall.state_request_message`), the
   path WITH its query, so neither the tuple nor an argument can be swapped
   under the proof, and sends it with the worker's ID token
   (`children.ChildApi`). A 409 is the fence (invariant 5):
   `StateReadsFenced` is raised, a `ChildSubmitFenced`, so the lifecycle
   ends the attempt through the one fenced exit without touching the lease;
3. writes swarm-api's answer, or its refusal's code and message, as the
   response.

THE CHECKS HERE ARE THE FAST ANSWER, NOT THE GATE. swarm-api re-checks the
token, the proof, the fence and the budget on every call, and scopes every
read to the lease's tenant. The per-attempt hourly budget here only spares a
looping agent's round trips.

READ-ONLY. The relay sends GETs and nothing else.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

from ..children import (
    PROOF_HEADER,
    TIMESTAMP_HEADER,
    ChildApiError,
    ChildSubmitFenced,
    _read_small_json,
    _write_atomic,
)
from ..gitmcp.server import MAX_REQUEST_BYTES, REQUEST_ID
from ..statecall import FENCED_CODES, state_request_message
from .server import ARGUMENTS, REQUIRED, SPOOL_DIR_NAME, SPOOL_ENV, TOOL_NAMES

#: Per attempt, per hour: swarm-api's `attemptreads.CALLS_PER_HOUR`, restated
#: for the fast answer; tests/unit/worker/test_state_mcp.py holds them equal.
CALLS_PER_HOUR = 120
_HOUR = 3600.0
#: One argument's longest text. Ids, a state, an instant, an artifact name.
MAX_ARGUMENT_CHARS = 512
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
STATE_READS_FENCED = "attempt_fenced"


class StateReadRefused(Exception):
    """A refusal: the code, a message for the agent, whether asking again may work."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "message": self.message,
                            "retryable": self.retryable}}


class StateReadsFenced(ChildSubmitFenced):
    """409 from the read route: this attempt is superseded (invariant 5)."""


@dataclass(frozen=True)
class StateReadsConfig:
    """The attempt's fencing fields: the worker's own, never a request's."""

    tenant_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    generation: int
    calls_per_hour: int = CALLS_PER_HOUR


def validate_request(tool: Any, arguments: Any) -> tuple[str, dict[str, str]]:
    """(tool, arguments as query text), or `StateReadRefused`. Shape only."""
    if not isinstance(tool, str) or tool not in TOOL_NAMES:
        raise StateReadRefused("unknown_tool", f"tools are {', '.join(TOOL_NAMES)}")
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        raise StateReadRefused("request_invalid", "arguments must be an object")
    unknown = sorted(set(args) - ARGUMENTS[tool])
    missing = sorted(REQUIRED[tool] - set(args))
    if unknown or missing:
        raise StateReadRefused("request_invalid",
                               f"{tool} takes {sorted(ARGUMENTS[tool])}; unknown {unknown}, "
                               f"missing {missing}")
    out: dict[str, str] = {}
    for name in sorted(args):
        value = args[name]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise StateReadRefused("request_invalid", f"{name} must be text or an integer")
        text = str(value)
        if not text or len(text) > MAX_ARGUMENT_CHARS or _CONTROL.search(text):
            raise StateReadRefused("request_invalid",
                                   f"{name} must be one line of 1 to {MAX_ARGUMENT_CHARS} "
                                   "characters")
        out[name] = text
    return tool, out


class StateReads:
    """The spool's relay. One per attempt."""

    def __init__(
        self,
        cfg: StateReadsConfig,
        *,
        sign: Callable[[bytes], str],
        api: Any,
        logger: Any,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        #: `sign(message) -> proof`: the attempt key's (`AttemptKey.sign`), so
        #: the key stays in the worker's heap and only a proof leaves.
        self.cfg = cfg
        self.log = logger
        self._sign = sign
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

        Raises `StateReadsFenced` after writing the agent its `fenced` answer."""
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
            except StateReadsFenced:
                _write_atomic(response, StateReadRefused(
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
            return StateReadRefused("request_invalid",
                                    f"a request is one JSON object of at most "
                                    f"{MAX_REQUEST_BYTES} bytes").as_dict()
        return self.answer(data.get("tool"), data.get("arguments"), request_id=request_id)

    def answer(self, tool: Any, arguments: Any, *, request_id: str) -> dict[str, Any]:
        """One request's answer: `{"tool", "result"}` or `{"refused"}`.

        Raises `StateReadsFenced` only. Logs the tool, codes and ids, never an
        argument or a result: the result is the tenant's records."""
        try:
            name, args = validate_request(tool, arguments)
            self._spend()
            payload = self.call(name, args)
        except StateReadRefused as refusal:
            self.log.warning("state read refused", code=refusal.code, request_id=request_id)
            return refusal.as_dict()
        self.log.info("state read answered", tool=name, request_id=request_id)
        return {"tool": name, "result": payload.get("result", payload)}

    def _spend(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= _HOUR:
            self._calls.popleft()
        if len(self._calls) >= self.cfg.calls_per_hour:
            raise StateReadRefused("rate_limited",
                                   f"at most {self.cfg.calls_per_hour} state reads an hour "
                                   "per attempt", retryable=True)
        self._calls.append(now)

    # -- the signed call -------------------------------------------------------

    def path(self, tool: str, args: dict[str, str]) -> str:
        """The route WITH its query: the tuple first, then the tool's arguments."""
        query = urlencode([
            ("tenant_id", self.cfg.tenant_id),
            ("task_id", self.cfg.task_id),
            ("lease_id", self.cfg.lease_id),
            ("generation", str(self.cfg.generation)),
            *sorted(args.items()),
        ])
        return (f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/reads/"
                f"{quote(tool, safe='')}?{query}")

    def call(self, tool: str, args: dict[str, str]) -> dict[str, Any]:
        """GET the read, signed under `swarm-state-req/v1`. A 409 raises
        `StateReadsFenced`; every other refusal is the agent's to read."""
        path = self.path(tool, args)
        timestamp = str(int(self._wall()))
        headers = {
            PROOF_HEADER: self._sign(state_request_message("GET", path, b"", timestamp)),
            TIMESTAMP_HEADER: timestamp,
        }
        try:
            _, payload = self._api.call("GET", path, body=None, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409 or exc.code in FENCED_CODES:
                raise StateReadsFenced(exc.status, STATE_READS_FENCED, exc.message,
                                       retryable=False) from None
            if exc.retryable:
                raise StateReadRefused(exc.code if exc.status else "api_unavailable",
                                       exc.message or "the platform did not answer; ask again",
                                       retryable=exc.code != "logs_unavailable") from None
            raise StateReadRefused(exc.code, exc.message or exc.code) from None
        return payload if isinstance(payload, dict) else {}
