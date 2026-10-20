"""The QA identity, the worker's half (docs/design/qa-identity.md §4.5; lane
QI-WORKER, part of #1649; owner decisions 2026-10-19 in §7).

A `claude-code-browser` task whose SIGNED input names `qa_identity` gets its
live browser signed in to this deployment's console as the QA service
account. swarm-api signs a short-lived IAP JWT for the attempt
(`POST /v1/attempts/{attempt_id}/qa-identity`, `swarm_api.qa_identity`),
proven by the attempt key in this heap under its own signing purpose; the
worker hands it to the gate on ctl.sock (`qa_identity`), and the gate adds it
to console requests from console frames only (`livegate/qaidentity.py`). The
agent never holds it.

WHEN. Only when the verified task's input names an identity the frozen
catalogue offers (`requested`). Anything else asks nothing: no call to
swarm-api, no ctl.sock operation, no notice.

RENEWED AT TWO-THIRDS OF ITS LIFE (Q5 (a): 15 minutes, renewed at 10), as
the egress ticket is. A transient failure (swarm-api unreachable, a 5xx other
than `qa_identity_unconfigured`, a 429, IAM Credentials not answering) is
tried again with the egress ticket's backoff; while the credential in hand is
still good, nothing changes. Once it has expired the gate is told
`{"failed": cause}` and the agent's notice says so, until a later ask
succeeds and both are cleared.

FAIL CLOSED. A permanent refusal (the grant revoked, the task fenced, the
input no longer signed for it) tells the gate `{"failed": cause}`, which drops
the credential at once, and writes the cause to the agent's notice
(`NOTICE_NAME`, the egress notice's sibling in the hand-off spool). The task
is NOT failed: the agent sees Google's sign-in page and a plain sentence
saying why, and can still report what it saw. The exception is the FIRST
ask: a refusal of `qa_identity_not_granted` or `qa_identity_unconfigured`
there fails the attempt before the agent starts (`FAILS_THE_ATTEMPT`),
because nothing it could do would be signed in.

NOTHING HERE LOGS the credential, a signature or a key. Log lines carry
codes, the `jti` and the lifetime. The credential is registered with the
logger's redaction (`register_secret`) as well, so a line that ever quoted it
would be scrubbed, and it is never written to the spool or the environment.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple
from urllib.parse import quote

from swarm_common.profiles import RUNNER_PROFILES

from .egress_ticket import RETRY_MAX_SECONDS, RETRY_SECONDS

#: `swarm_api.qa_identity.QA_IDENTITY_PURPOSE`, restated (this image does not
#: carry swarm-api); tests/unit/worker/test_qa_identity_worker.py holds them equal.
QA_IDENTITY_PURPOSE = "swarm-qa-identity-request/v1"

#: The input key the frozen catalogue declares on `claude-code-browser`
#: (contract request 106), and the identities it offers, read from it.
INPUT_KEY = "qa_identity"
IDENTITIES: tuple[str, ...] = tuple(
    getattr(RUNNER_PROFILES["claude-code-browser"].inputs.get(INPUT_KEY), "choices", None) or ()
)

#: The gate's ctl.sock operation (`livegate.ctl.OPS`, `livegate.qaidentity.OP`).
CTL_OP = "qa_identity"

#: The agent's notice, in the hand-off spool beside `handoff.EGRESS_NOTICE_NAME`.
NOTICE_NAME = "qa-identity.md"

#: Renew once this fraction of the credential's life is left: a 900 s
#: credential is renewed at 600 s (Q5 (a): renewed at 10 minutes).
RENEW_FRACTION = 1 / 3

#: Why no credential is in the gate, as the gate and the agent are told it.
#: Each is a short word the gate's cause pattern accepts.
NOT_GRANTED = "qa_identity_not_granted"
UNCONFIGURED = "qa_identity_unconfigured"
SIGN_FAILED = "qa_identity_sign_failed"
REFUSED = "qa_identity_refused"
UNREACHABLE = "qa_identity_unreachable"
GATE_REFUSED = "qa_identity_gate_refused"
NO_ATTEMPT_KEY = "no_attempt_key"
WORKER_UNPROTECTED = "worker_unprotected"

#: A first-ask refusal that fails the attempt before its agent starts (§4.5).
FAILS_THE_ATTEMPT = frozenset({NOT_GRANTED, UNCONFIGURED})

#: swarm-api's codes that say no credential will come for this attempt
#: (`swarm_api.qa_identity`'s refusals; `not_found` is also another tenant's
#: task). `qa_identity_unconfigured` is a 503 at signing, and still permanent.
_PERMANENT = {
    NOT_GRANTED: NOT_GRANTED,
    UNCONFIGURED: UNCONFIGURED,
    "qa_identity_not_requested": "qa_identity_not_requested",
    "stale_generation": "stale_generation",
    "lease_not_live": "lease_not_live",
    "not_found": "not_found",
    "bad_signature": "bad_signature",
    "spec_unverified": "spec_unverified",
}

#: A `jti` as swarm-api makes it (`secrets.token_hex(16)`); anything else is
#: not put in a log line.
_JTI = re.compile(r"[A-Za-z0-9_-]{1,64}")

_NOTICE = """\
# The console's QA sign-in is unavailable

This task asked for the live browser to be signed in to the SwarmCloud console
as its QA identity, and that sign-in is not available ({cause}: {why}). Pages
on the console will show Google's sign-in page instead of the console. Do not
type a password there and do not ask for a hand-off on the console: SwarmCloud
refuses one there, because a person's own sign-in would hand you their
workspaces. Finish what you can check without it, and say in your answer that
the console sign-in was unavailable, naming `{cause}`.
"""

_WHY = {
    NOT_GRANTED: "a platform admin has not granted this workspace the QA identity, or revoked it",
    UNCONFIGURED: "this deployment has no QA identity",
    SIGN_FAILED: "SwarmCloud could not sign the QA credential just now; it keeps trying",
    UNREACHABLE: "SwarmCloud could not be asked for the QA credential just now; it keeps trying",
    REFUSED: "SwarmCloud refused this attempt the QA credential",
    GATE_REFUSED: "the live browser did not take the QA credential",
    NO_ATTEMPT_KEY: "the worker has no attempt key to ask with",
    WORKER_UNPROTECTED: "the worker cannot protect a credential in its memory",
    "qa_identity_not_requested": "the task's signed input does not name the QA identity",
    "stale_generation": "this attempt is no longer the task's current one",
    "lease_not_live": "this attempt no longer holds the task's lease",
    "not_found": "SwarmCloud does not know this attempt's task",
    "bad_signature": "SwarmCloud did not accept this attempt's proof",
    "spec_unverified": "the task's signed spec does not verify",
}


def requested(task: Any) -> str | None:
    """The identity the task's input names, when the catalogue offers it, else None.

    Read from the VERIFIED task document. swarm-api refuses any other value at
    submission and again at signing, so anything else is read as none.
    """
    if not isinstance(task, Mapping):
        return None
    payload = task.get("input")
    value = payload.get(INPUT_KEY) if isinstance(payload, Mapping) else None
    return value if isinstance(value, str) and value in IDENTITIES else None


def notice(cause: str) -> str:
    """What the agent is told while its browser holds no QA credential."""
    return _NOTICE.format(cause=cause, why=_WHY.get(cause, "the QA identity is unavailable"))


class IssueRefused(Exception):
    """swarm-api gave nothing to hand the gate. `cause` is a word, `retryable` whether asking again may help."""

    def __init__(self, cause: str, *, retryable: bool) -> None:
        super().__init__(cause)
        self.cause = cause
        self.retryable = retryable


class Issued(NamedTuple):
    """swarm-api's answer, checked. `repr` never shows the credential."""

    host: str
    header: str
    scheme: str
    credential: str
    expires_in: int
    jti: str

    def __repr__(self) -> str:
        return f"Issued(host={self.host!r}, expires_in={self.expires_in}, jti={self.jti!r})"


def issue(cfg: Any, key: Any, api: Any, identity: str, *, now: float) -> Issued:
    """Ask swarm-api for this attempt's QA credential.

    The body is `swarm_api.qa_identity.QaIdentityRequest`: the attempt's
    tuple, the task and the identity; the signature is under
    QA_IDENTITY_PURPOSE. Raises IssueRefused.
    """
    from .handoff import sign_headers

    body = json.dumps(
        {
            "tenant_id": cfg.tenant_id,
            "task_id": cfg.task_id,
            "attempt_id": cfg.attempt_id,
            "lease_id": cfg.lease_id,
            "generation": cfg.generation,
            "identity": identity,
        }
    ).encode("utf-8")
    path = f"/v1/attempts/{quote(cfg.attempt_id, safe='')}/qa-identity"
    try:
        headers = sign_headers(key, QA_IDENTITY_PURPOSE, "POST", path, body, now=now)
        _, payload = api.call("POST", path, body=body, headers=headers)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if code in _PERMANENT:
            raise IssueRefused(_PERMANENT[code], retryable=False) from None
        if code == SIGN_FAILED:
            raise IssueRefused(SIGN_FAILED, retryable=True) from None
        retryable = getattr(exc, "retryable", None)
        status = getattr(exc, "status", None)
        if retryable is False and isinstance(status, int) and 400 <= status < 500:
            raise IssueRefused(REFUSED, retryable=False) from None
        raise IssueRefused(UNREACHABLE, retryable=True) from None
    payload = payload if isinstance(payload, Mapping) else {}
    fields = (payload.get("host"), payload.get("header"), payload.get("scheme"),
              payload.get("credential"))
    seconds = payload.get("expires_in")
    jti = payload.get("jti")
    if payload.get("identity") != identity or not all(isinstance(f, str) and f for f in fields) \
            or isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0 \
            or not isinstance(jti, str) or not _JTI.fullmatch(jti):
        raise IssueRefused(SIGN_FAILED, retryable=True)
    host, header, scheme, credential = fields
    return Issued(host, header, scheme, credential, seconds, jti)  # type: ignore[arg-type]


class QaSignIn:
    """One attempt's QA credential upkeep. Every method never raises."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        wall: Callable[[], float] = time.time,
        register_secret: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._wall = wall
        self._register_secret = register_secret
        #: The identity the task named; None until `start`, and for every
        #: task that names none.
        self.identity: str | None = None
        #: `ready` with a credential in the gate, `failed` with `cause`, or None.
        self.state: str | None = None
        self.cause: str | None = None
        self.renewals = 0
        self.jti: str | None = None
        self._renew_at = 0.0
        self._expires_at = 0.0
        self._retry_at = 0.0
        self._backoff = RETRY_SECONDS
        #: A permanent refusal: nothing more is asked this attempt.
        self._ended = False
        self._started = False

    @property
    def wanted(self) -> bool:
        return self.identity is not None

    def start(self, gate: Any, key: Any, api: Any, identity: str | None,
              unavailable: str | None = None) -> str | None:
        """The first credential, before the agent exists. Once per attempt.

        Returns the cause that must fail the attempt before its agent starts
        (FAILS_THE_ATTEMPT), else None, whatever else happened.
        `unavailable` is why nothing can be asked at all (no attempt key, an
        unprotected heap): the gate is told and the agent runs signed out.
        """
        if self._started:
            return self.cause if self._ended and self.cause in FAILS_THE_ATTEMPT else None
        self._started = True
        self.identity = identity
        if identity is None or gate is None:
            return None
        if unavailable is not None or key is None or api is None:
            self._ended = True
            self._fail(gate, unavailable or NO_ATTEMPT_KEY)
            return None
        self._ask(gate, key, api)
        if self._ended and self.cause in FAILS_THE_ATTEMPT:
            return self.cause
        return None

    def keep(self, gate: Any, key: Any, api: Any) -> str | None:
        """Renew the credential when it is due; on a poll. A new failure's cause, or None."""
        if not self.wanted or self._ended or gate is None or key is None or api is None:
            return None
        now = self._wall()
        if now < self._renew_at or now < self._retry_at:
            return None
        before = (self.state, self.cause)
        self._ask(gate, key, api)
        if self.state == "failed" and (self.state, self.cause) != before:
            return self.cause
        return None

    def notice(self) -> str | None:
        """The agent's notice while the browser holds no QA credential, else None."""
        if self.wanted and self.state == "failed" and self.cause:
            return notice(self.cause)
        return None

    # -- internals -----------------------------------------------------------------

    def _ask(self, gate: Any, key: Any, api: Any) -> None:
        assert self.identity is not None
        now = self._wall()
        try:
            issued = issue(self.cfg, key, api, self.identity, now=now)
        except IssueRefused as refused:
            if not refused.retryable:
                self._ended = True
                self._fail(gate, refused.cause)
                return
            self._retry_later(refused.cause)
            if now < self._expires_at:
                # The credential in hand still works: ask again later, change nothing.
                return
            if (self.state, self.cause) != ("failed", refused.cause):
                self._fail(gate, refused.cause)
            return
        if self._register_secret is not None:
            try:
                self._register_secret(issued.credential)
            except Exception:  # redaction is a second guard; nothing logs the value anyway
                pass
        try:
            gate.call(CTL_OP, {"host": issued.host, "header": issued.header, "scheme": issued.scheme,
                               "credential": issued.credential, "expires_in": issued.expires_in})
        except Exception as exc:
            # The gate's refusal is a word it chose, never what was sent; the
            # exception's class is all that is logged.
            refused = type(exc).__name__ == "GateRefused"
            self.log.warning("the gate did not take the QA credential", error=type(exc).__name__,
                             jti=issued.jti)
            if refused:
                self._ended = True
                self._fail(gate, GATE_REFUSED)
                return
            self._retry_later(UNREACHABLE)
            return
        renewal = self.state == "ready"
        self._expires_at = now + issued.expires_in
        self._renew_at = now + issued.expires_in * (1 - RENEW_FRACTION)
        self._retry_at = 0.0
        self._backoff = RETRY_SECONDS
        if renewal:
            self.renewals += 1
        self.state, self.cause, self.jti = "ready", None, issued.jti
        self.log.info("qa identity handed to the gate", jti=issued.jti,
                      expires_in=issued.expires_in, renewals=self.renewals)

    def _fail(self, gate: Any, cause: str) -> None:
        self.state, self.cause = "failed", cause
        self._expires_at = 0.0
        self.log.warning("qa identity unavailable; the browser is signed out of the console",
                         cause=cause)
        try:
            gate.call(CTL_OP, {"failed": cause})
        except Exception as exc:  # an expired credential is refused by the gate on its own
            self.log.warning("the gate was not told the qa identity failed", error=type(exc).__name__)

    def _retry_later(self, cause: str) -> None:
        self._retry_at = self._wall() + self._backoff
        self.log.warning("qa identity not renewed; trying again", code=cause,
                         retry_seconds=int(self._backoff))
        self._backoff = min(self._backoff * 2, RETRY_MAX_SECONDS)


__all__ = [
    "CTL_OP", "FAILS_THE_ATTEMPT", "IDENTITIES", "INPUT_KEY", "NOTICE_NAME", "QA_IDENTITY_PURPOSE",
    "IssueRefused", "Issued", "QaSignIn", "issue", "notice", "requested",
]
