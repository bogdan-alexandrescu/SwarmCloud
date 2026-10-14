"""swarm-graph's one remote tool, `in_flight` (docs/design/context-quality.md §4.2 layer I,
§4.4; lane CTXQ-I).

The other eight tools answer from the staged snapshot, offline. Which other
lanes are editing the same files cannot be staged -- it changes by the
minute -- so `in_flight` asks swarm-api's attempt-proved route,
`POST /v1/attempts/{attempt_id}/context/inflight` (`swarm_api.attemptcontext`),
for the OTHER in-flight lanes of the task's own tenant and repository whose
territory meets the files: open pull requests, live issue-run steps, and
running tasks that declare no files.

TWO HALVES, ONE SPOOL, as `swarm-git` and `swarm-search` work. The agent's
MCP server holds no key: the attempt key lives only in the worker's heap
(`children.AttemptKey`), never in the agent's process tree. So the server
writes one request into the spool and waits; the worker's relay signs it and
sends it with the worker's ID token, and writes the answer back.

    $SWARM_GRAPH = work/.swarm-graph/
      requests/<request_id>.json   {"tool": "in_flight", "arguments": {"files"}}
      responses/<request_id>.json  the relay's: {"tool", "result": {...}} or {"refused": {...}}

* `in_flight` (the agent's side) validates the arguments, spools them, and
  turns the answer into rows paged under the 4 KiB cap (`tools.page`).
* `InFlightRelay` (the worker's side) validates again, signs, POSTs, and
  answers. A 409 is the fence: the agent is answered `fenced` and
  `InFlightFenced`, a `ChildSubmitFenced`, is raised, so the lifecycle ends
  the attempt through its one fenced exit without touching the lease
  (invariant 5).
* `RemoteGraphServer` is `GraphServer` with this tool beside the eight;
  `main` starts it (`python -m agent_worker.graphmcp.remote`), with the
  spool from `$SWARM_GRAPH`.

UNKNOWN, NEVER "NOTHING IN FLIGHT" (§4.4). With no spool (the server was
started outside a worker), no answer from the worker, no network, no proof,
or any refusal, the answer is `in_flight: "UNKNOWN"` with the reason, and no
`rows` at all -- an empty list would read as "nobody else is here", the one
answer that costs a collision. A read that worked but was not whole (a pull
request whose files were not read, a lane or a running task that declares no
files) is listed row by row as unread or undeclared, with `complete: false`;
a forge that could not be read makes `pull_requests` UNKNOWN. Only a complete
read with nothing in it is `in_flight: 0`.

A cursor is an offset into a FRESH read: the lanes can change between two
pages, which the answer's `read_at` lets the agent see.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from agent_worker.children import (
    AttemptKey,
    ChildApi,
    ChildApiError,
    ChildSubmitFenced,
    _read_small_json,
    _write_atomic,
)
from agent_worker.gitmcp.server import (  # noqa: F401  (write_request: the spool's one writer)
    MAX_REQUEST_BYTES,
    POLL_SECONDS,
    REQUEST_ID,
    submit,
    write_request,
)
from agent_worker.graphmcp import tools
from agent_worker.graphmcp.server import GraphServer

SPOOL_DIR_NAME = ".swarm-graph"
SPOOL_ENV = "SWARM_GRAPH"
TOOL_NAME = "in_flight"
#: swarm-api's `issueruns.MAX_STEP_FILES`: the most files one question names.
MAX_FILES = 60
#: How long the agent waits for the worker. The worker answers on its control
#: poll; past this the answer is UNKNOWN and the agent goes on.
WAIT_SECONDS = 60.0
#: Per attempt, per hour: only spares a looping agent's round trips.
CALLS_PER_HOUR = 60
_HOUR = 3600.0
CONTEXT_FENCED = "context_fenced"

NOT_KNOWN = ("UNKNOWN is never 'nothing in flight': the platform could not say which other "
             "lanes hold these files. Check open pull requests before editing a shared file")
UNREAD_NOTE = ("unread pull requests and undeclared lanes or tasks may hold these files; "
               "their files are not known, which is not 'no overlap'")

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Live: other open PRs, issue-run steps and running tasks of this tenant "
                   "touching these files or their callers and tests.",
    "inputSchema": {"type": "object", "required": ["files"], "properties": {
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "repo-relative paths, or dir/ prefixes"},
        "cursor": {"type": "string", "description": "next_cursor of the previous page"}}},
}
TOOLS_WITH_REMOTE: list[dict[str, Any]] = [*tools.TOOLS, TOOL]
ARGUMENTS = frozenset(TOOL["inputSchema"]["properties"])


class InFlightRefused(Exception):
    """A refusal: the code, a message for the agent, whether asking again may work."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "message": self.message,
                            "retryable": self.retryable}}


class InFlightFenced(ChildSubmitFenced):
    """409 from the in-flight route: this attempt is superseded (invariant 5)."""


# --------------------------------------------------------------------------
# the arguments, the same on both sides
# --------------------------------------------------------------------------


def validate_files(arguments: Any, *, allowed: frozenset[str] = ARGUMENTS) -> list[str]:
    """The files, normalised and de-duplicated, or `tools.ToolInputError`."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise tools.ToolInputError("arguments must be an object")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise tools.ToolInputError(f"unknown argument(s): {', '.join(unknown)}")
    raw = arguments.get("files")
    if not isinstance(raw, list) or not raw:
        raise tools.ToolInputError("files must be a non-empty list of repository paths")
    if len(raw) > MAX_FILES:
        raise tools.ToolInputError(f"files takes at most {MAX_FILES} entries")
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value) > 400 \
                or any(c in value for c in "*?[]{}\\") or any(ord(c) < 32 for c in value):
            raise tools.ToolInputError("each file is a repository path or a dir/ prefix, with "
                                       "no glob or control character")
        text = value.strip()
        prefix = text.endswith("/")
        clean = posixpath.normpath(text)
        if clean.startswith("/") or clean in (".", "..") or clean.startswith("../"):
            raise tools.ToolInputError(f"{value[:100]!r} is not a repository-relative path")
        clean = clean + "/" if prefix else clean
        if clean not in out:
            out.append(clean)
    return sorted(out)


# --------------------------------------------------------------------------
# the agent's side
# --------------------------------------------------------------------------


def unknown_answer(code: str, message: str) -> str:
    """UNKNOWN, with why, and no rows: never an empty list."""
    body = {"source": "swarm-api, live", "in_flight": "UNKNOWN",
            "reason": {"code": str(code)[:80], "message": str(message)[:600]},
            "note": NOT_KNOWN}
    return tools.dumps(body)


def error_answer(message: str) -> str:
    return tools.dumps({"source": "swarm-api, live", "error": message[:1024]})


def _rows(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """(every row in a fixed order, how many are overlaps)."""
    overlaps: list[dict[str, Any]] = []
    for pull in result.get("pull_requests") or []:
        overlaps.append({"kind": "pull_request", "number": pull.get("number"),
                         "title": pull.get("title"), "shared": pull.get("shared") or [],
                         "shared_total": pull.get("shared_total")})
    for lane in result.get("lanes") or []:
        overlaps.append({"kind": "lane", "run_id": lane.get("run_id"),
                         "issue": lane.get("issue"), "step_id": lane.get("step_id"),
                         "state": lane.get("state"), "pull_request": lane.get("pull_request"),
                         "shared": lane.get("shared") or [],
                         "shared_total": lane.get("shared_total")})
    unread: list[dict[str, Any]] = []
    for number in result.get("pull_requests_unread") or []:
        unread.append({"kind": "unread_pull_request", "number": number})
    for lane in result.get("lanes_undeclared") or []:
        unread.append({"kind": "undeclared_lane", "run_id": lane.get("run_id"),
                       "issue": lane.get("issue"), "state": lane.get("state")})
    for task in result.get("tasks_undeclared") or []:
        unread.append({"kind": "undeclared_task", "task_id": task.get("task_id"),
                       "workflow_id": task.get("workflow_id"), "step_id": task.get("step_id")})
    return overlaps + unread, len(overlaps)


def render(result: Any, offset: int) -> tuple[str, bool]:
    """swarm-api's answer as one page under the 4 KiB cap."""
    if not isinstance(result, dict) or not isinstance(result.get("pull_requests_read"), dict):
        return unknown_answer("bad_answer", "the platform's answer could not be read"), True
    rows, overlaps = _rows(result)
    pulls_read = result["pull_requests_read"]
    complete = result.get("complete") is True
    head: dict[str, Any] = {
        "source": "swarm-api, live", "read_at": result.get("read_at"),
        "repository": result.get("repository"),
        "in_flight": overlaps if (overlaps or complete) else "UNKNOWN",
        "complete": complete,
        "pull_requests": (len(result.get("pull_requests") or []) if pulls_read.get("ok")
                          else "UNKNOWN"),
        "territory": result.get("territory"),
    }
    if not pulls_read.get("ok"):
        head["pull_requests_reason"] = pulls_read.get("code")
    if not complete and not rows:
        # Nothing listed and not all of it read: UNKNOWN, and no empty `rows`.
        return tools.dumps({**head, "note": NOT_KNOWN}), False
    if not complete:
        head["note"] = UNREAD_NOTE
    # `tools.page` puts a freshness block first; a live read's freshness is
    # its `read_at`, so the block names that.
    observation = tools.Observation(block={"live": True, "read_at": result.get("read_at")},
                                    changed=frozenset(), dirty=frozenset())
    try:
        return tools.page(observation, head, rows, offset), False
    except tools.ToolInputError as exc:
        return error_answer(str(exc)), True


def in_flight(arguments: Any, *, spool: Path | None, wait_seconds: float = WAIT_SECONDS,
              poll_seconds: float = POLL_SECONDS,
              sleep: Callable[[float], None] = time.sleep,
              submitter: Callable[..., dict[str, Any]] = submit) -> tuple[str, bool]:
    """(answer text, is_error) for one `in_flight` call, through the spool."""
    try:
        files = validate_files(arguments)
        offset = tools._cursor(arguments)
    except tools.ToolInputError as exc:
        return error_answer(str(exc)), True
    if spool is None or not Path(spool).is_dir():
        return unknown_answer("no_spool", "this server was not started by a worker, so it "
                                          "cannot reach the platform"), True
    answer = submitter(Path(spool), TOOL_NAME, {"files": files}, wait_seconds=wait_seconds,
                       poll_seconds=poll_seconds, sleep=sleep)
    refused = answer.get("refused") if isinstance(answer, dict) else None
    if isinstance(refused, dict):
        return unknown_answer(str(refused.get("code") or "refused"),
                              str(refused.get("message") or "")), True
    result = answer.get("result") if isinstance(answer, dict) else None
    return render(result, offset)


# --------------------------------------------------------------------------
# the worker's side
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InFlightConfig:
    """The attempt's fencing fields: the worker's own, never a request's."""

    tenant_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    generation: int
    calls_per_hour: int = CALLS_PER_HOUR


class InFlightRelay:
    """The spool's relay, in the worker. One per attempt."""

    def __init__(self, cfg: InFlightConfig, *, key: AttemptKey, api: ChildApi, logger: Any,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.log = logger
        self._key = key
        self._api = api
        self._clock = clock
        self._wall = wall
        self._calls: deque[float] = deque()

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

        Raises `InFlightFenced` after writing the agent its `fenced` answer."""
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
            except InFlightFenced:
                _write_atomic(response, InFlightRefused(
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
            return InFlightRefused("request_invalid",
                                   f"a request is one JSON object of at most "
                                   f"{MAX_REQUEST_BYTES} bytes").as_dict()
        return self.answer(data.get("tool"), data.get("arguments"), request_id=request_id)

    def answer(self, tool: Any, arguments: Any, *, request_id: str) -> dict[str, Any]:
        """One request's answer: `{"tool", "result"}` or `{"refused"}`.

        Raises `InFlightFenced` only. Logs codes and counts, never a path."""
        try:
            if tool != TOOL_NAME:
                raise InFlightRefused("unknown_tool", f"the tool is {TOOL_NAME}")
            try:
                files = validate_files(arguments, allowed=frozenset({"files"}))
            except tools.ToolInputError as exc:
                raise InFlightRefused("request_invalid", str(exc)) from None
            self._spend()
            result = self.call(files, request_id=request_id)
        except InFlightRefused as refusal:
            self.log.warning("in_flight refused", code=refusal.code, request_id=request_id)
            return refusal.as_dict()
        self.log.info("in_flight answered", request_id=request_id,
                      pulls=len(result.get("pull_requests") or []),
                      lanes=len(result.get("lanes") or []),
                      complete=bool(result.get("complete")))
        return {"tool": TOOL_NAME, "result": result}

    def _spend(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= _HOUR:
            self._calls.popleft()
        if len(self._calls) >= self.cfg.calls_per_hour:
            raise InFlightRefused("rate_limited",
                                  f"at most {self.cfg.calls_per_hour} in_flight calls an hour "
                                  "per attempt", retryable=True)
        self._calls.append(now)

    def route(self) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/context/inflight"

    def call(self, files: list[str], *, request_id: str) -> dict[str, Any]:
        """POST the question, signed with the attempt key; the body carries the
        attempt's fencing fields and the files only. A 409 raises `InFlightFenced`."""
        body = json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "request_id": request_id,
            "files": files,
        }, sort_keys=True).encode("utf-8")
        route = self.route()
        headers = self._key.headers("POST", route, body, now=self._wall())
        try:
            _, payload = self._api.call("POST", route, body=body, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409 or exc.code in (CONTEXT_FENCED, "child_submit_fenced"):
                raise InFlightFenced(exc.status, CONTEXT_FENCED, exc.message,
                                     retryable=False) from None
            if exc.status == 404:
                raise InFlightRefused("in_flight_unavailable",
                                      "this platform does not serve in-flight context yet"
                                      ) from None
            raise InFlightRefused(exc.code if exc.status else "api_unavailable",
                                  exc.message or "the platform did not answer",
                                  retryable=bool(exc.retryable)) from None
        if not isinstance(payload, dict):
            raise InFlightRefused("bad_answer", "the platform's answer could not be read")
        return payload


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------


class RemoteGraphServer(GraphServer):
    """`GraphServer` with `in_flight` beside the eight offline tools."""

    def __init__(self, snapshot: Any, freshness: Any, *, spool: Path | None,
                 wait_seconds: float = WAIT_SECONDS) -> None:
        super().__init__(snapshot, freshness)
        self.spool = spool
        self.wait_seconds = wait_seconds

    def handle(self, message: Any) -> dict | None:
        if isinstance(message, dict) and message.get("jsonrpc") == "2.0" and "id" in message:
            method = message.get("method")
            params = message.get("params") or {}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": message["id"],
                        "result": {"tools": TOOLS_WITH_REMOTE}}
            if method == "tools/call" and isinstance(params, dict) \
                    and params.get("name") == TOOL_NAME:
                text, failed = in_flight(params.get("arguments"), spool=self.spool,
                                         wait_seconds=self.wait_seconds)
                return {"jsonrpc": "2.0", "id": message["id"],
                        "result": {"content": [{"type": "text", "text": text}],
                                   "isError": failed}}
        return super().handle(message)


def main(argv: list[str] | None = None) -> int:
    """`python -m agent_worker.graphmcp.remote --snapshot <dir> [--workdir <checkout>]`:
    `agent_worker.graphmcp`'s server, plus `in_flight` over `$SWARM_GRAPH`."""
    import os

    from agent_worker.graphmcp.freshness import Freshness
    from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError, load_freshness_record

    parser = argparse.ArgumentParser(prog="swarm-graph", description=main.__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--workdir", default=None)
    args = parser.parse_args(argv)
    try:
        record = load_freshness_record(args.snapshot)
        snapshot = Snapshot(args.snapshot,
                            manifest_digest=(record or {}).get("manifest_digest"))
    except SnapshotError as exc:
        print(f"swarm-graph: {exc}", file=sys.stderr)
        return 2
    freshness = Freshness.establish(snapshot.commit_sha, workdir=args.workdir, record=record)
    spool = os.environ.get(SPOOL_ENV)
    RemoteGraphServer(snapshot, freshness, spool=Path(spool) if spool else None).serve(
        sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
