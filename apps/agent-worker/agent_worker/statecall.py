"""The worker's signed client for its state routes (docs/design/tenant-settings-integrity.md §4).

WHY. A tenant worker's service account can read and write any document in
the `swarm` database whose id it can name, and its agent can mint that
account's token, so one tenant's agent can change what the control plane
does for another (#1457, §1). Design B moves every read and write the worker
makes behind swarm-api's `/v1/attempts/{attempt_id}/state/...` routes and
then removes the role. This module is the worker's half of the wire: one
attempt's tuple, the attempt key that signs for it, and the retry budget.
`control.RemoteControlPlane` speaks `ControlPlane`'s methods over it.

THREE PROOFS ON EVERY CALL (§4.1):

  * an ID token for swarm-api's audience, minted per call by the transport
    (`children.ChildApi`, the metadata server). It proves the TENANT;
  * the one-use registration nonce, spent ONCE, by `open`: it registers the
    attempt key, so `state/open` is the first state call and every later one
    is signed;
  * the attempt proof, Ed25519 over
    `swarm-state-req/v1 \\n METHOD \\n path \\n sha256(body) \\n timestamp`,
    by the key generated in this process's heap after `make_non_dumpable`.
    It proves the WORKER of this attempt, not its agent. The prefix is not
    the child routes' `swarm-child-req/v1`, so neither kind of proof can be
    replayed as the other. A GET signs its path WITH the query string, so
    the tuple cannot be swapped under a captured proof.

WHAT AN ANSWER BECOMES. The caller acts on worker errors, never on HTTP:

  * 409 `attempt_fenced` (or, at `open`, a key slot already taken or a
    registration window already closed): this attempt no
    longer owns its task -> `FencedWriteRefused`, nothing written
    (invariant 5). The caller exits without running or continuing the agent
    and makes no release call;
  * 5xx, 429 and no answer: retried with the child path's backoff (1 s,
    doubling to 8 s) until the call's budget is spent, then
    `google.api_core.exceptions.ServiceUnavailable`, which the lifecycle
    already reads as an outage: 69 before the runner, the mid-run outage
    rule after;
  * 401 -> `Unauthenticated` and 403 -> `PermissionDenied`, the refusals the
    lifecycle already reads as "cannot start" (78) at the generation check;
  * anything else -> `StateRefused`, a `ControlPlaneError` naming the code.

IDEMPOTENCY. A call whose answer was lost is sent again with the SAME body:
transitions, parks, finishes and retries carry a `request_id` the caller
builds once per write, events an `event_id`; swarm-api answers a repeat
without writing it twice (§4.2). Only the proof and its timestamp are new.

Nothing here logs, stores or raises with the token, the nonce, the proof or a
request body.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from .children import PROOF_HEADER, TIMESTAMP_HEADER, AttemptKey, ChildApiError
from .errors import ControlPlaneError, FencedWriteRefused

#: Domain separation from the child routes' `swarm-child-req/v1` (§4.1).
#: swarm-api's `childkey.state_request_message` is the verifier; the tests
#: hold this restatement to it.
STATE_REQUEST_PURPOSE = "swarm-state-req/v1"

#: The child path's backoff (`ChildPath._with_retry`): the first wait, and the
#: longest. Doubling between them.
RETRY_FIRST_SECONDS = 1.0
RETRY_CAP_SECONDS = 8.0

#: The 409 codes that mean "this attempt no longer owns its task": the
#: fence; a key slot this generation already registered (§4.3: "it can
#: neither win the race nor register later"); and a registration window that
#: has closed -- the task left LEASED, DISPATCHED and STARTING, or its lease
#: is not live -- which `state/open` meets only when another owner moved it.
FENCED_CODES = frozenset({"attempt_fenced", "child_key_taken", "child_key_window_closed"})


def state_request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one state request (§4.1)."""
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"{STATE_REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}"
    ).encode("utf-8")


def new_request_id() -> str:
    """One write's idempotency id, `[a-z0-9-]{1,64}` as the routes require."""
    return uuid.uuid4().hex


def jsonable(value: Any) -> Any:
    """`json.dumps`'s `default`: an instant as ISO 8601, an enum as its value."""
    if isinstance(value, datetime):
        return value.isoformat()
    inner = getattr(value, "value", None)
    if isinstance(inner, (str, int)):
        return inner
    return str(value)


def encode_body(body: Mapping[str, Any]) -> bytes:
    """The exact bytes sent and signed. Compact, so a retry sends the same ones."""
    return json.dumps(body, separators=(",", ":"), default=jsonable).encode("utf-8")


class StateRefused(ControlPlaneError):
    """A route refused the call for a reason that is not the fence or an
    outage: a 422 strict-model refusal, a transition the route refuses, a
    403 on an upstream id the task does not declare."""

    def __init__(self, status: int | None, code: str, message: str) -> None:
        super().__init__(f"swarm-api refused the state call ({status} {code}): {message}")
        self.status = status
        self.code = code


def _unavailable(message: str) -> Exception:
    """An outage, as the lifecycle's outage rules already read one."""
    try:
        from google.api_core import exceptions as core_exceptions  # lazy: grpc
    except ImportError:  # pragma: no cover - the image always carries it
        return ControlPlaneError(message)
    return core_exceptions.ServiceUnavailable(message)


def _refused_identity(status: int, message: str) -> Exception:
    """A 401 or 403 on the token or the proof: a refusal, not an outage."""
    try:
        from google.api_core import exceptions as core_exceptions  # lazy: grpc
    except ImportError:  # pragma: no cover - the image always carries it
        return ControlPlaneError(message)
    if status == 401:
        return core_exceptions.Unauthenticated(message)
    return core_exceptions.PermissionDenied(message)


class StateCall:
    """One attempt's state routes: its tuple, its key, its retry budget."""

    def __init__(
        self,
        api: Any,
        *,
        tenant_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        generation: int,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        #: The transport: `call(method, path, *, body, headers) -> (status,
        #: payload)`, raising `ChildApiError` -- `children.ChildApi`.
        self._api = api
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.lease_id = lease_id
        self.generation = generation
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._key: AttemptKey | None = None
        self.prefix = f"/v1/attempts/{quote(attempt_id, safe='')}/state"

    @property
    def key(self) -> AttemptKey | None:
        """The key `open` registered; None until it has."""
        return self._key

    @property
    def opened(self) -> bool:
        return self._key is not None

    def tuple_fields(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "lease_id": self.lease_id,
            "generation": self.generation,
        }

    # -- the calls --------------------------------------------------------

    def open(
        self,
        key: AttemptKey,
        *,
        nonce: str,
        unprotected: bool,
        restore_attempt_id: str | None,
        deadline_seconds: float,
    ) -> dict[str, Any]:
        """`state/open`: spend the nonce on `key`, read the attempt's documents.

        Unsigned: it registers the key every later proof is checked against.
        Called again with the SAME key is answered as the first was, which is
        how the restore's attempt is asked for once the task names it.
        """
        body: dict[str, Any] = {
            **self.tuple_fields(),
            "nonce": nonce,
            "public_key": key.public,
            "unprotected": unprotected,
        }
        if restore_attempt_id:
            body["restore_attempt_id"] = restore_attempt_id
        raw = encode_body(body)
        answer = self._send("POST", f"{self.prefix}/open", raw, deadline_seconds,
                            write="state/open", signed=False)
        self._key = key
        return answer

    def post(self, op: str, fields: Mapping[str, Any], *, deadline_seconds: float) -> dict[str, Any]:
        """A signed POST to `state/<op>`, the tuple added to `fields`."""
        raw = encode_body({**self.tuple_fields(), **fields})
        return self._send("POST", f"{self.prefix}/{op}", raw, deadline_seconds, write=op)

    def get(
        self, op: str, params: Mapping[str, Any] | None = None, *, deadline_seconds: float
    ) -> dict[str, Any]:
        """A signed GET of `state/<op>`; the proof covers the query string."""
        query = urlencode({**self.tuple_fields(),
                           **{k: v for k, v in (params or {}).items() if v is not None}})
        return self._send("GET", f"{self.prefix}/{op}?{query}", b"", deadline_seconds, write=op)

    # -- the wire -----------------------------------------------------------

    def _headers(self, method: str, path: str, raw: bytes) -> dict[str, str]:
        if self._key is None:
            raise ControlPlaneError(
                "state/open has not registered this attempt's key; no state call is "
                "made before it (docs/design/tenant-settings-integrity.md §4.3)"
            )
        timestamp = str(int(self._wall()))
        return {
            PROOF_HEADER: self._key.sign(state_request_message(method, path, raw, timestamp)),
            TIMESTAMP_HEADER: timestamp,
        }

    def _send(
        self,
        method: str,
        path: str,
        raw: bytes,
        deadline_seconds: float,
        *,
        write: str,
        signed: bool = True,
    ) -> dict[str, Any]:
        deadline = self._clock() + max(0.0, float(deadline_seconds))
        delay = RETRY_FIRST_SECONDS
        while True:
            # Signed again on every try: the timestamp must be inside swarm-api's
            # skew window, and a retry long after the first try would not be.
            headers = self._headers(method, path, raw) if signed else {}
            try:
                _status, payload = self._api.call(
                    method, path, body=raw if method == "POST" else None, headers=headers
                )
                return payload if isinstance(payload, dict) else {}
            except ChildApiError as exc:
                if exc.retryable:
                    remaining = deadline - self._clock()
                    if remaining > 0:
                        self._sleep(min(delay, remaining))
                        delay = min(delay * 2, RETRY_CAP_SECONDS)
                        continue
                    raise _unavailable(
                        f"swarm-api's state/{write} did not answer within "
                        f"{deadline_seconds:g}s ({exc.code})"
                    ) from None
                raise self._refusal(exc, write=write) from None

    def _refusal(self, exc: ChildApiError, *, write: str) -> Exception:
        status = exc.status
        if status == 409 and exc.code in FENCED_CODES:
            reason = f"swarm-api: {exc.message or exc.code}"
            fenced = FencedWriteRefused(self.generation, -1, reason, write=write)
            #: swarm-api's sentence, for a caller that re-raises it as a plain
            #: `FencedError` (the gate's).
            fenced.reason = reason  # type: ignore[attr-defined]
            return fenced
        if status in (401, 403) and exc.code in ("attempt_unauthenticated", "attempt_unproven",
                                                 "child_key_unproven", "http_401", "http_403"):
            return _refused_identity(
                status, f"swarm-api refused state/{write}: {exc.code}"
            )
        return StateRefused(status, exc.code, exc.message)
