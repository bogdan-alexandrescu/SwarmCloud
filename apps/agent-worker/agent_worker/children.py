"""Child tasks, the worker's half (docs/design/child-tasks.md §3.1-§3.3, §6.4).

The agent never holds a credential. It writes a request into a SPOOL in its
workspace; this worker -- the non-dumpable process beside it, which holds the
attempt key -- validates it and relays it to swarm-api, signed.

    $SWARM_CHILDREN = work/.swarm-children/
      requests/<request_id>.json   the agent's, one child each (write .tmp, rename)
      responses/<request_id>.json  this worker's: {"task_id": ...} or {"refused": {...}}
      await                        the agent's: presence asks to await, then exit 0
      results/children.json        this worker's, on resume: every child's end
      results/<child_task_id>/...  this worker's, on resume: a SUCCEEDED child's artifacts

WHY A SPOOL AND NOT A LOCALHOST ENDPOINT (§2): no listening socket, nothing
from the runner image, and `work/` is checkpointed, so a resumed attempt knows
which requests were already answered.

THE ATTEMPT KEY (§3.2). An Ed25519 keypair generated in this process's heap
once `make_non_dumpable` reports PROTECTED, its public half registered with
swarm-api while the task is STARTING -- before the agent exists -- by spending
the one-use nonce the scheduler passed in SWARM_CHILD_NONCE. The private key is
never written anywhere: not the environment, a file, `work/`, a log line or an
event. A worker that cannot protect its heap registers a TOMBSTONE in the slot
instead, so the nonce the agent can read from /proc/1/environ is spent, and
offers no child path (§5 F12).

WHAT THE AGENT GETS BACK. A child's id when it is created, and -- only after
`await` parks the parent and the scheduler promotes it -- every child's end in
`results/children.json`. There is no channel that reports a running child's
progress (§1), so an agent that tries to busy-wait has nothing to wait on and
waiting always gives the slot back (invariants 1 and 4).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlencode

from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES, TaskState

from .accountlease import BrokerUnavailable, fetch_identity_token
from .errors import InputUnavailable
from .hardening import PROTECTED
from . import inputs as inputs_mod

#: The spool's directory under `work/`. `Workspace.children_dir` is the one
#: path; this is its name.
SPOOL_DIR_NAME = ".swarm-children"
#: The variable that tells an agent it has a child path, and where.
CHILDREN_ENV = "SWARM_CHILDREN"

#: `swarm_api.children.REQUEST_ID`, restated (this image does not carry
#: swarm-api); tests/unit/worker/test_child_tasks_worker.py holds them equal.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: What a request may say about its child (§6.1). Anything else is refused here
#: and by the route's strict model.
CHILD_FIELDS = frozenset(
    {"runner_profile", "resource_class", "input", "provider", "model", "timeout_seconds",
     "repository_ref"}
)
#: `swarm_api.childkey.REQUEST_PURPOSE` and the proof headers, restated.
REQUEST_PURPOSE = "swarm-child-req/v1"
PROOF_HEADER = "X-Swarm-Attempt-Proof"
TIMESTAMP_HEADER = "X-Swarm-Attempt-Timestamp"
#: The tombstone's refusal (§5 F12).
WORKER_UNPROTECTED = "worker_unprotected"
#: The guide the agent reads, written into the spool as README.md by `prepare`
#: and named by the one prompt line the CLI runners add (`prompt_line`). The
#: prompt is the one instruction channel every CLI runner shares, so it says
#: only where the guide is; the guide says everything else. docs/child-tasks-
#: for-agents.md carries the same text for people, and
#: tests/unit/worker/test_child_tasks_worker.py holds the two equal.
GUIDE_NAME = "README.md"
AGENT_GUIDE = """\
# Child tasks: how this agent submits helpers

This task may submit CHILD TASKS: other agents that run in their own
containers, on this task's tenant, while you work. `$SWARM_CHILDREN` is this
directory. There is no network call to make and no credential to use: you
write files here, and the platform's worker beside you submits them.

## Submit a child

Write ONE JSON object per child to `requests/<request_id>.json`, atomically:
write `requests/<request_id>.tmp`, then rename it to `.json`. `<request_id>`
is yours to choose, `[a-z0-9-]{1,64}`, and is the child's idempotency key:
writing the same id again never makes a second child.

    {
      "request_id": "split-tests-2",
      "runner_profile": "claude-code",
      "input": {"prompt": "Write unit tests for src/parser.py"},
      "resource_class": "standard",
      "timeout_seconds": 1800,
      "repository_ref": "main"
    }

`runner_profile` is required and names a profile, never an image or a
command. `input` is the child's input, `{"prompt": ...}` for the CLI agents.
`resource_class`, `provider`, `model`, `timeout_seconds` and `repository_ref`
are optional; nothing else is accepted. A child works on this task's
repository, runs no longer than this task, and is at most 256 KiB as a file.

## Read the answer

Within about ten seconds the worker writes `responses/<request_id>.json` and
removes the request:

    {"task_id": "task_...", "request_id": "split-tests-2"}

or a refusal:

    {"refused": {"code": "child_fan_out_exceeded", "message": "...", "retryable": false}}

`retryable: true` (`api_unavailable`) means write the same request again with
the same `request_id`. Anything else will be refused again as written. Limits:
16 children per task across all its attempts; a child cannot submit children
of its own; about four requests are answered per ten seconds.

## Wait for the children

There is no way to watch a running child. To wait, first write everything you
will need to continue into your working files (your process ends; the
platform checkpoints the work directory and restores it), then create the
empty file `await` here and EXIT 0. The task gives its slot back and sleeps
until every child has ended. Then you are started again with the same input,
and `results/children.json` exists -- that is how you know you are resuming:

    {"listing": "complete",
     "children": [{"task_id": "task_...", "request_id": "split-tests-2",
                   "state": "SUCCEEDED", "end_cause": null,
                   "parent_attempt_id": "att_...", "outputs": "staged",
                   "files": ["task_.../notes.md"]}]}

Each SUCCEEDED child's artifacts are under `results/<task_id>/`; `files` lists
them relative to `results/`. `outputs` is `staged`, `none`, or `unavailable`
with a `reason` (an expired or over-cap artifact: still information, not a
failure). A child that failed or was cancelled is listed with its `state` and
`end_cause`; deciding what to do about it is yours. `listing: unavailable`
means the platform could not list them this time.

If you exit 0 while children are still running, you are made to wait anyway:
a task never succeeds over running children. If you exit non-zero, this
attempt fails as usual and the children keep running for the next attempt.
"""


def prompt_line(spool: str) -> str:
    """The one line a CLI runner adds to the prompt when the agent has a spool."""
    return (
        f"You can submit child tasks (helper agents) and wait for their results "
        f"through files in {spool}; read {spool}/{GUIDE_NAME} first. Nothing there "
        "is required."
    )


#: `ChildPath.tick`'s default `limit`: `max_child_requests_per_tick`. A
#: sentinel, because None is the drain's "every request in the spool".
PER_TICK: Any = object()

_TIMEOUT = 10
_UA = "swarm-agent-worker/children"


def request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one request (§3.2 step 5)."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class AttemptKey:
    """The attempt key, in this process's heap only. Never serialised."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.generate()
        self.public = _b64url(
            self._private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )

    def headers(self, method: str, path: str, body: bytes, *, now: float) -> dict[str, str]:
        timestamp = str(int(now))
        signature = self._private.sign(request_message(method, path, body, timestamp))
        return {PROOF_HEADER: _b64url(signature), TIMESTAMP_HEADER: timestamp}

    def __repr__(self) -> str:  # never the key, whatever logs this object
        return "AttemptKey(<heap only>)"

    def __reduce__(self) -> Any:
        raise TypeError("an attempt key is never serialised")


# --------------------------------------------------------------------------
# The client for the worker-only routes
# --------------------------------------------------------------------------


class ChildApiError(Exception):
    """A route's refusal, or no answer. `retryable` is the caller's to act on."""

    def __init__(self, status: int | None, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class ChildSubmitFenced(ChildApiError):
    """409 child_submit_fenced: this attempt is superseded (invariant 5)."""


class ChildApi:
    """swarm-api's three worker-only child routes, over urllib.

    The ID token is minted for the API's audience from the metadata server at
    every call and never logged; an error never carries a header or a body
    this worker sent.
    """

    def __init__(
        self,
        base_url: str,
        *,
        audience: str | None = None,
        token_fetcher: Callable[[str], str] | None = None,
        timeout: int = _TIMEOUT,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._audience = (audience or self._base).rstrip("/")
        self._fetch_token = token_fetcher or fetch_identity_token
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            token = self._fetch_token(self._audience)
        except BrokerUnavailable as exc:
            raise ChildApiError(None, "api_unavailable", "no workload identity token", retryable=True) from exc
        req = urllib.request.Request(f"{self._base}{path}", data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _UA)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self._open(req, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() if hasattr(exc, "read") else b""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ChildApiError(
                None, "api_unavailable", type(exc).__name__, retryable=True
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if status >= 400:
            code = str(payload.get("code") or f"http_{status}")
            message = str(payload.get("message") or "")[:300]
            if code == "child_submit_fenced":
                raise ChildSubmitFenced(status, code, message, retryable=False)
            raise ChildApiError(status, code, message, retryable=status >= 500 or status == 429)
        return status, payload


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"refused": {"code": self.code, "message": self.message, "retryable": self.retryable}}


class ChildPath:
    """One attempt's child path: registration, the spool, the await, the staging."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        memory: Any,
        api: ChildApi | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._memory = memory
        self._api = api
        if self._api is None and cfg.swarm_api_url:
            self._api = ChildApi(cfg.swarm_api_url, audience=cfg.swarm_api_audience)
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._key: AttemptKey | None = None
        #: Why this attempt has no child path, once decided; None while it has one.
        self.unavailable: str | None = "not_registered"

    @property
    def offered(self) -> bool:
        """Whether this attempt has a child path: the variable and the spool exist."""
        return self._key is not None and self.unavailable is None

    @property
    def configured(self) -> bool:
        """Whether this deployment gave the attempt a nonce and an API to spend it at."""
        return bool(self.cfg.child_nonce) and self._api is not None

    # -- §3.2 step 3: register before the agent exists --------------------

    def register(self, note: Callable[[dict[str, Any]], Any]) -> None:
        """Spend the nonce: a key when the heap is protected, a tombstone when not.

        Called while the task is STARTING. Never raises: a registration that
        cannot be made leaves the attempt without a child path, the residual
        §3.2 states, recorded as `child_key_unregistered` so it is visible.
        """
        if not self.cfg.child_nonce:
            self.unavailable = "no_nonce"
            return
        if self._api is None:
            self.unavailable = "child_key_unregistered"
            self._note(note, "child_key_unregistered", "no SWARM_API_URL for this attempt")
            return
        protected = getattr(self._memory, "status", None) == PROTECTED
        key = AttemptKey() if protected else None
        body: dict[str, Any] = {
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "nonce": self.cfg.child_nonce,
        }
        if key is not None:
            body["public_key"] = key.public
        else:
            body.update({"key": None, "refused": WORKER_UNPROTECTED})
        path = f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/child-key"
        try:
            self._with_retry(
                lambda: self._api.call("POST", path, body=json.dumps(body).encode("utf-8"))
            )
        except ChildApiError as exc:
            self.unavailable = "child_key_unregistered"
            self._note(note, "child_key_unregistered", exc.code)
            return
        if key is None:
            self.unavailable = WORKER_UNPROTECTED
            self._note(note, "child_key_tombstone", WORKER_UNPROTECTED)
            return
        self._key = key
        self.unavailable = None
        self.log.info("child path registered for this attempt")

    def _note(self, note: Callable[[dict[str, Any]], Any], what: str, why: str) -> None:
        self.log.warning("no child path for this attempt", child_path=what, reason=why)
        try:
            note({"phase": "child_key", "child_path": what, "reason": why})
        except Exception:  # a note must never stop the attempt
            pass

    def _retry_deadline(self) -> float:
        return self._clock() + max(0, int(self.cfg.child_submit_retry_seconds))

    def _with_retry(self, fn: Callable[[], Any], *, deadline: float | None = None) -> Any:
        """Retry a retryable failure within `child_submit_retry_seconds`, never longer.

        `deadline` is the caller's remaining budget (a tick's, or a drain's):
        retries never outlast it either, so the bound a tick or a drain
        promises holds across every request it answers, not per request.
        """
        own = self._retry_deadline()
        deadline = own if deadline is None else min(deadline, own)
        delay = 1.0
        while True:
            try:
                return fn()
            except ChildApiError as exc:
                if not exc.retryable:
                    raise
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise
                self._sleep(min(delay, remaining))
                delay = min(delay * 2, 8.0)

    # -- the spool ---------------------------------------------------------

    @staticmethod
    def spool(work: Path) -> Path:
        """`Workspace.children_dir`, from the work directory alone."""
        return work / SPOOL_DIR_NAME

    def prepare(self, work: Path) -> dict[str, str]:
        """Create the spool and return the agent's variable, or nothing."""
        if not self.offered:
            return {}
        root = self.spool(work)
        for name in ("requests", "responses", "results"):
            (root / name).mkdir(parents=True, exist_ok=True)
        # Rewritten every attempt: the platform's text, never one a restored
        # checkpoint carried (a link the agent made is replaced, not followed).
        guide = root / GUIDE_NAME
        tmp = root / f".{GUIDE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(AGENT_GUIDE, encoding="utf-8")
        os.replace(tmp, guide)
        return {CHILDREN_ENV: str(root)}

    def known_children(self, work: Path) -> list[str]:
        """Child ids this task's attempts were answered with, from the spool."""
        out: list[str] = []
        responses = self.spool(work) / "responses"
        if not responses.is_dir():
            return out
        for path in sorted(responses.glob("*.json")):
            data = _read_small_json(path, 64 * 1024)
            if isinstance(data, dict) and isinstance(data.get("task_id"), str):
                out.append(data["task_id"])
        return out

    def await_requested(self, work: Path) -> bool:
        return (self.spool(work) / "await").exists()

    def clear_await(self, work: Path) -> None:
        try:
            (self.spool(work) / "await").unlink()
        except FileNotFoundError:
            pass

    def has_requests(self, work: Path) -> bool:
        """Whether the agent has spooled anything; a cheap listing, no read."""
        requests = self.spool(work) / "requests"
        return self.offered and requests.is_dir() and any(requests.glob("*.json"))

    def tick(
        self,
        work: Path,
        *,
        limit: int | None | object = PER_TICK,
        deadline: float | None = None,
    ) -> int:
        """Answer up to `limit` outstanding requests. Returns how many were answered.

        `limit` is `max_child_requests_per_tick` by default (`PER_TICK`), a
        number, or None for every request in the spool (the drain before a
        park).

        Bounded in time as well as count: one `child_submit_retry_seconds` in
        total for this call, retries included -- the remaining budget is what
        each request's retries get -- so one tick is never the long in-worker
        wait invariant 4 forbids. A caller with its own budget passes it as
        `deadline` and the tick keeps to the earlier of the two. Raises
        `ChildSubmitFenced` when the route says this attempt is superseded:
        the lifecycle ends the way a fenced worker does mid-run.
        """
        own = self._retry_deadline()
        deadline = own if deadline is None else min(deadline, own)
        if not self.offered:
            return 0
        root = self.spool(work)
        requests = root / "requests"
        responses = root / "responses"
        if not requests.is_dir():
            return 0
        cap = self.cfg.max_child_requests_per_tick if limit is PER_TICK else limit
        answered = 0
        for path in sorted(requests.glob("*.json")):
            if isinstance(cap, int) and answered >= cap:
                break
            if self._clock() >= deadline:
                break
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = responses / f"{request_id}.json"
            if response.exists():
                # Answered before a crash took the removal (§5 F1).
                path.unlink(missing_ok=True)
                continue
            answer = self._answer(path, request_id, deadline=deadline)
            _write_atomic(response, answer)
            path.unlink(missing_ok=True)
            answered += 1
        return answered

    def drain(self, work: Path) -> int:
        """Answer EVERY outstanding request before a park (§5 F3), in bounded time.

        Ticks until the spool is empty or ONE `child_submit_retry_seconds` has
        passed in total, retries included (the drain's remaining budget is
        what each tick, and each request's retries, get); whatever is left
        then is answered with the retryable `api_unavailable` refusal, without
        a call, so a park never carries a request it did not answer and the
        wait stays bounded (invariant 4).
        """
        if not self.offered:
            return 0
        deadline = self._retry_deadline()
        answered = 0
        while self._clock() < deadline:
            done = self.tick(work, limit=None, deadline=deadline)
            answered += done
            if not done or not self.has_requests(work):
                break
        root = self.spool(work)
        for path in sorted((root / "requests").glob("*.json")):
            request_id = path.stem
            if not REQUEST_ID.fullmatch(request_id) or path.is_symlink() or not path.is_file():
                continue
            response = root / "responses" / f"{request_id}.json"
            if not response.exists():
                _write_atomic(
                    response,
                    Refusal(
                        "api_unavailable",
                        "not submitted before the await; write the request again with the "
                        "same request_id",
                        retryable=True,
                    ).as_dict(),
                )
                answered += 1
            path.unlink(missing_ok=True)
        return answered

    def _answer(
        self, path: Path, request_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        refusal = self._local_refusal(path, request_id)
        if isinstance(refusal, Refusal):
            self.log.warning("child request refused by the worker", request_id=request_id,
                             code=refusal.code)
            return refusal.as_dict()
        child = refusal
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
                "request_id": request_id,
                "child": child,
            }
        ).encode("utf-8")
        route = f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/children"
        assert self._key is not None and self._api is not None

        def _submit() -> tuple[int, dict[str, Any]]:
            headers = self._key.headers("POST", route, body, now=self._wall())
            return self._api.call("POST", route, body=body, headers=headers)

        try:
            _, payload = self._with_retry(_submit, deadline=deadline)
        except ChildSubmitFenced:
            raise
        except ChildApiError as exc:
            if exc.retryable:
                return Refusal(
                    "api_unavailable",
                    "the platform did not answer; write the request again with the same request_id",
                    retryable=True,
                ).as_dict()
            return Refusal(exc.code, exc.message or exc.code).as_dict()
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        task_id = task.get("id")
        if not isinstance(task_id, str):
            return Refusal("api_unavailable", "the platform answered without a task id",
                           retryable=True).as_dict()
        self.log.info("child submitted", request_id=request_id, child_task_id=task_id,
                      created=bool(payload.get("created")))
        return {"task_id": task_id, "request_id": request_id}

    def _local_refusal(self, path: Path, request_id: str) -> Refusal | dict[str, Any]:
        """What the route would refuse anyway, refused here for free (§3.1 step 2)."""
        try:
            size = path.stat().st_size
        except OSError:
            return Refusal("request_invalid", "the request file could not be read")
        if size > self.cfg.max_child_request_bytes:
            return Refusal(
                "request_too_large",
                f"a request is at most {self.cfg.max_child_request_bytes} bytes",
            )
        data = _read_small_json(path, self.cfg.max_child_request_bytes)
        if not isinstance(data, dict):
            return Refusal("request_invalid", "a request is one JSON object")
        named = data.get("request_id", request_id)
        if named != request_id:
            return Refusal("request_invalid", "request_id must equal the file's name")
        child = {k: v for k, v in data.items() if k != "request_id"}
        unknown = sorted(set(child) - CHILD_FIELDS)
        if unknown:
            return Refusal(
                "request_invalid",
                f"unknown keys {unknown}; a child is named by runner_profile and "
                f"resource_class, never by an image, a command or a figure",
            )
        profile = RUNNER_PROFILES.get(child.get("runner_profile"))  # type: ignore[arg-type]
        if profile is None:
            return Refusal("unknown_runner_profile", "runner_profile names no profile")
        if profile.worker_action is not None:
            return Refusal(
                "worker_action_profile",
                f"{profile.name} acts on a forge with a credential no agent may steer",
            )
        if "input" in child and not isinstance(child["input"], dict):
            return Refusal("request_invalid", "input must be an object")
        return child

    # -- §3.3: the await ---------------------------------------------------

    def list_children(self) -> list[dict[str, Any]]:
        """Every child of this task, as swarm-api lists them (§6.2). Raises
        `ChildApiError` when no answer came within the retry bound."""
        assert self._key is not None and self._api is not None
        query = urlencode(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
            }
        )
        route = f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/children"
        signed = f"{route}?{query}"

        def _list() -> tuple[int, dict[str, Any]]:
            headers = self._key.headers("GET", signed, b"", now=self._wall())
            return self._api.call("GET", signed, headers=headers)

        _, payload = self._with_retry(_list)
        rows = payload.get("children")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def live_children(self, work: Path) -> list[str] | None:
        """Ids of children still running, [] for none, None when it is unknown.

        Asked only when this task has a child it knows of, so a task that never
        used the path pays nothing.
        """
        if not self.offered or not self.known_children(work):
            return []
        try:
            rows = self.list_children()
        except ChildApiError as exc:
            self.log.warning("could not list this task's children", code=exc.code)
            return None
        return [
            str(r.get("task_id"))
            for r in rows
            if _state(r.get("state")) not in TERMINAL_STATES
        ]

    # -- §3.3 step 7: stage the children's results on resume ---------------

    def stage_results(self, work: Path, *, store: Any, max_total_bytes: int) -> dict[str, Any] | None:
        """Write `results/children.json` and stage each SUCCEEDED child's artifacts.

        A lost output is information for the agent, not a reason to fail it
        (§5 F9): such a child is listed with `outputs: unavailable` and why.
        Every object key is built from THIS attempt's tenant
        (`inputs.artifact_reference` -> `artifact_key`), so another tenant's
        artifact is unreachable by construction (invariant 9).
        """
        if not self.offered or not self.known_children(work):
            return None
        results = self.spool(work) / "results"
        # The worker's own directory: anything a restored checkpoint put here
        # (a link the agent made, last attempt's files) is replaced.
        if results.is_symlink() or results.is_file():
            results.unlink()
        elif results.is_dir():
            shutil.rmtree(results)
        results.mkdir(parents=True, exist_ok=True)
        try:
            rows = self.list_children()
        except ChildApiError as exc:
            document = {"children": [], "listing": "unavailable", "reason": exc.code}
            _write_atomic(results / "children.json", document)
            return document
        budget = max(0, int(max_total_bytes))
        out = []
        for row in sorted(rows, key=lambda r: str(r.get("task_id"))):
            child_id = str(row.get("task_id") or "")
            entry: dict[str, Any] = {
                "task_id": child_id,
                "request_id": row.get("request_id"),
                "state": row.get("state"),
                "end_cause": row.get("end_cause"),
                "parent_attempt_id": row.get("parent_attempt_id"),
                "outputs": "none",
                "files": [],
            }
            if _state(row.get("state")) is TaskState.SUCCEEDED and child_id:
                staged, used, reason = self._stage_child(
                    results, child_id, row.get("artifacts") or [], store=store, budget=budget
                )
                budget -= used
                entry["files"] = staged
                if reason is not None:
                    entry["outputs"] = "unavailable"
                    entry["reason"] = reason
                else:
                    entry["outputs"] = "staged" if staged else "none"
            out.append(entry)
        document = {"children": out, "listing": "complete"}
        _write_atomic(results / "children.json", document)
        self.log.info("staged this task's children's results", children=len(out))
        return document

    def _stage_child(
        self,
        results: Path,
        child_id: str,
        artifacts: list[Any],
        *,
        store: Any,
        budget: int,
    ) -> tuple[list[str], int, str | None]:
        doc = {"state": TaskState.SUCCEEDED.value, "result_summary": {"artifacts": artifacts}}
        base = results / child_id
        staged: list[str] = []
        used = 0
        for entry in artifacts:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            try:
                destination = _safe_destination(base, name)
                reference = inputs_mod.artifact_reference(
                    doc, tenant_id=self.cfg.tenant_id, upstream_task_id=child_id, filename=name
                )
                if used + reference.size_bytes > budget:
                    return staged, used, "over the staging cap for one attempt"
                destination.parent.mkdir(parents=True, exist_ok=True)
                used += int(store.download_file(reference.key, destination))
            except InputUnavailable as exc:
                return staged, used, str(exc)[:300]
            except Exception as exc:  # an expired object, a network fault
                return staged, used, f"{type(exc).__name__}"[:300]
            staged.append(destination.relative_to(results).as_posix())
        return staged, used, None


def _state(value: Any) -> TaskState | None:
    try:
        return TaskState(value)
    except ValueError:
        return None


def _safe_destination(base: Path, name: str) -> Path:
    """`base/<name>`, refusing anything that would leave `base`."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(p in ("", ".", "..") for p in pure.parts):
        raise InputUnavailable(f"artifact name {name!r} is not a plain relative path")
    destination = base.joinpath(*pure.parts)
    resolved_base = base.resolve()
    if not str(destination.resolve()).startswith(str(resolved_base) + os.sep):
        raise InputUnavailable(f"artifact name {name!r} resolves outside its directory")
    return destination


def _read_small_json(path: Path, limit: int) -> Any:
    """Read at most `limit` bytes of JSON; None for anything unreadable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, default=str))
    os.replace(tmp, path)
