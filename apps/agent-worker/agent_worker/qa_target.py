"""The QA target, the worker's half (VQ-4; docs/design/visual-qa-any-app.md §2.2, §2.3).

A visual-QA run's browser steps are stamped by swarm-api with
`metadata.qa_target = {id, revision}` (VQ-2, `swarm_api.qatargets`); nothing
about the target travels in the step's input (§1.1). For every
`claude-code-browser` attempt (`PROFILE`, the profile VQ-1 renders browser
steps with) the worker, once the gate is registered and before the runner
starts (`HandoffPath.prepare`), asks swarm-api for its attempt's target
(`POST /v1/attempts/{attempt_id}/qa-target`), proven by the attempt key under
this route's own signing purpose. swarm-api reads the mark, takes the tenant
from the lease, checks the target is that tenant's and its host approved,
and answers 204 when the agent has none (the common case), else the target's
public settings and, for an app with its own login, the test account's
username and password read from Secret Manager at that moment.

THE WORKER HANDS IT TO THE GATE AND FORGETS IT. ctl.sock `qa_sign_in`
(`livegate/qatarget.py`): the gate holds the agent, signs in on a tab of its
own, and scrubs the password from everything bound for the agent. The worker
drops its copy as soon as the gate has answered, registers the password with
the logger's redaction as a second guard, and LOGS ONLY THE TARGET ID AND
THE OUTCOME: never the username, the password, a selector or swarm-api's
message.

THE FIRST SIGN-IN IS AWAITED BEFORE THE AGENT EXISTS. The gate's verb returns
at once (a sign-in takes up to a minute; ctl.sock answers within 10 s), and
the worker polls it (`{poll: true}`) for at most `FIRST_WAIT_SECONDS`, so the
agent's first connection finds the browser signed in rather than held.

`swarm-handoff sign-in` (§2.3, the session was lost) writes
`sign-ins/<id>.json` in the hand-off spool; on its next poll the worker asks
swarm-api again and the gate signs in again. At most `MAX_SIGN_INS` an
attempt, then `qa_sign_in_exhausted`. A sign-in that ended on MFA or a
CAPTCHA (`qa_sign_in_challenge`, §2.4) is answered with the sentence that
says so; it is never handed to a person.

THE WORKER READS NO METADATA. Whether an attempt has a target is swarm-api's
answer, never the worker's reading of the task's `metadata`, which the
step-spec signature does not cover (tests/unit/common/test_specsign_covers.py):
so this design needs no frozen-contract change. Every other profile asks
nothing.

FAIL SIGNED OUT, NEVER FAIL THE TASK. A refusal that shows there IS a target
(a pending host, a credential not set or unreadable, a target changed since
the run started: `_TARGET_CAUSES`) leaves the browser signed out of it and
writes the cause to the agent's notice (`NOTICE_NAME`, beside the egress and
QA-identity notices in the spool): public screens of the same target still
run and are reported (§2.4). Any other failure of the first ask (swarm-api
unreachable, a fenced attempt) says nothing about a target, so nothing is
written for an agent that most likely has none; it is logged, and
`swarm-handoff sign-in` asks again.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: `swarm_api.qatargets.QA_TARGET_PURPOSE`, restated (this image does not
#: carry swarm-api); tests/unit/worker/test_qa_target_signin.py holds them equal.
QA_TARGET_PURPOSE = "swarm-qa-target-request/v1"
#: The profile whose attempts ask (`swarm_api.qaspec.CAPTURE_PROFILE`).
PROFILE = "claude-code-browser"
#: The gate's ctl.sock operation (`livegate.qatarget.OP`, `livegate.ctl.OPS`).
CTL_OP = "qa_sign_in"
#: `livegate.qatarget.MAX_SIGN_INS` (§2.3).
MAX_SIGN_INS = 3
#: The agent's notice, in the hand-off spool.
NOTICE_NAME = "qa-target.md"
#: Where `swarm-handoff sign-in` writes its requests, in the spool.
SIGN_INS_DIR = "sign-ins"
#: How long the first sign-in is awaited before the runner starts: the gate's
#: own bound is 30 s for the form and 30 s after submit, plus the return.
FIRST_WAIT_SECONDS = 75.0
POLL_SECONDS = 0.5

SIGNED_IN = "signed_in"
SIGNING_IN = "signing_in"
READY = "ready"
FINAL = frozenset({SIGNED_IN, "challenge", "failed"})

#: Why there is no signed-in session, as the agent and the log are told it.
CHALLENGE = "qa_sign_in_challenge"
EXHAUSTED = "qa_sign_in_exhausted"
UNREACHABLE = "qa_target_unreachable"
REFUSED = "qa_target_refused"
MALFORMED = "qa_target_malformed"
GATE_REFUSED = "qa_target_gate_refused"
GATE_UNREACHABLE = "qa_target_gate_unreachable"
SIGN_IN_TIMEOUT = "qa_sign_in_timeout"
NO_ATTEMPT_KEY = "no_attempt_key"
NONE = "qa_target_none"

#: swarm-api's codes that say asking again in this attempt will not help.
_PERMANENT = frozenset({
    "qa_target_host_pending", "qa_credential_unavailable", "not_found", "conflict",
    "stale_generation", "lease_not_live", "bad_signature", "validation_failed",
})
#: Refusals that only an attempt WITH a target gets (module docstring).
_TARGET_CAUSES = frozenset({"qa_target_host_pending", "qa_credential_unavailable", "conflict"})
_CAUSE = re.compile(r"[a-z0-9_]{1,64}")
_REQUEST_ID = re.compile(r"[a-z0-9-]{1,64}")
_TARGET_ID = re.compile(r"qat_[0-9a-f]{16}")

_WHY = {
    CHALLENGE: ("the target's login asks for MFA or a CAPTCHA; register a test account "
                "without one"),
    EXHAUSTED: f"this attempt has signed in {MAX_SIGN_INS} times, the most an attempt may",
    "qa_sign_in_failed": "the login page did not reach its signed-in state after the gate submitted it",
    "qa_sign_in_no_form": "the gate did not find the login form's username and password fields",
    "qa_sign_in_busy": "a person was in control of the browser, so the gate did not sign in",
    "qa_sign_in_browser_gone": "the live browser closed during the sign-in",
    SIGN_IN_TIMEOUT: "the sign-in did not finish in time",
    "qa_target_host_pending": "a platform admin has not approved the target's host",
    "qa_credential_unavailable": "SwarmCloud cannot read the workspace's QA credential",
    "conflict": "the target changed since this run started, or its credential is not set",
    "not_found": "SwarmCloud does not know this attempt's QA target",
    "stale_generation": "this attempt is no longer the task's current one",
    "lease_not_live": "this attempt no longer holds the task's lease",
    "bad_signature": "SwarmCloud did not accept this attempt's proof",
    UNREACHABLE: "SwarmCloud could not be asked for the QA target just now",
    REFUSED: "SwarmCloud refused this attempt its QA target",
    MALFORMED: "SwarmCloud's answer was not a QA target the gate can use",
    GATE_REFUSED: "the live browser did not take the QA target",
    GATE_UNREACHABLE: "the live browser could not be reached to take the QA target",
    NO_ATTEMPT_KEY: "the worker has no attempt key to ask with",
    "worker_unprotected": "the worker cannot protect a credential in its memory",
}

_NOTICE = """\
# The QA target's sign-in did not succeed

This run's QA target signs in with its own login, and the live browser is not
signed in ({cause}: {why}). Pages that need a signed-in user will show the
login page. Never type a password there and never ask for a hand-off to sign
in: the live gate signs in, and only it holds the test account.
{retry}Check and report everything you can without it (the public screens),
and say in your answer that the sign-in did not succeed, naming `{cause}`.
"""
_RETRY = ("Run `swarm-handoff sign-in` to have the gate try again (at most {n} sign-ins an "
          "attempt).\n")


def reason(cause: str) -> str:
    return _WHY.get(cause, "the QA target's sign-in is unavailable")


def notice(cause: str) -> str:
    retry = "" if cause in (CHALLENGE, EXHAUSTED) else _RETRY.format(n=MAX_SIGN_INS)
    return _NOTICE.format(cause=cause, why=reason(cause), retry=retry)


class IssueRefused(Exception):
    """swarm-api gave no target to hand the gate. `cause` is a short word."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def issue(cfg: Any, key: Any, api: Any, *, now: float) -> dict[str, Any] | None:
    """Ask swarm-api for this attempt's QA target: its answer, or None on 204.

    The body is `swarm_api.qatargets.QaTargetRequest`, the attempt's tuple and
    the task; the signature is under QA_TARGET_PURPOSE. Raises IssueRefused,
    whose cause is a code and never swarm-api's message."""
    from .handoff import sign_headers

    body = json.dumps({
        "tenant_id": cfg.tenant_id, "task_id": cfg.task_id, "attempt_id": cfg.attempt_id,
        "lease_id": cfg.lease_id, "generation": cfg.generation,
    }).encode("utf-8")
    path = f"/v1/attempts/{quote(cfg.attempt_id, safe='')}/qa-target"
    try:
        headers = sign_headers(key, QA_TARGET_PURPOSE, "POST", path, body, now=now)
        status, payload = api.call("POST", path, body=body, headers=headers)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if code in _PERMANENT:
            raise IssueRefused(code) from None
        status_code = getattr(exc, "status", None)
        if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
            raise IssueRefused(REFUSED) from None
        raise IssueRefused(UNREACHABLE) from None
    if status == 204:
        return None
    if not isinstance(payload, Mapping):
        raise IssueRefused(MALFORMED)
    return dict(payload)


def handoff_args(answer: Mapping[str, Any]) -> dict[str, Any]:
    """swarm-api's answer as the gate's `qa_sign_in` takes it: the credential
    moved out of `sign_in` into `credential`. Raises ValueError when malformed."""
    sign_in = answer.get("sign_in")
    target_id = answer.get("target_id")
    if not isinstance(sign_in, Mapping) or not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        raise ValueError("not a QA target")
    served = {k: v for k, v in sign_in.items() if k not in ("username", "password")}
    exclusions = answer.get("exclusions")
    args: dict[str, Any] = {
        "target_id": target_id,
        "base_url": answer.get("base_url"),
        "origins": list(answer.get("origins") or []),
        "environment": answer.get("environment") or "production",
        "rate": dict(answer.get("rate") or {}),
        "read_posts": list(answer.get("read_posts") or []),
        "exclusions": {"paths": list((exclusions or {}).get("paths") or [])
                       if isinstance(exclusions, Mapping) else []},
        "sign_in": served,
        # swarm-api serves no per-step flag yet: every step is read-only (§3).
        "mutating": answer.get("mutating") is True,
    }
    if served.get("kind") == "own_login":
        username, password = sign_in.get("username"), sign_in.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not password:
            raise ValueError("an own login with no credential")
        args["credential"] = {"username": username, "password": password}
    return args


def _write_atomic(path: Path, document: Mapping[str, Any] | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    text = document if isinstance(document, str) else json.dumps(dict(document))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class QaTargetSignIn:
    """One attempt's QA target upkeep. Every public method never raises."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = time.sleep,
        register_secret: Callable[[str], None] | None = None,
        first_wait_seconds: float = FIRST_WAIT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._wall = wall
        self._sleep = sleep
        self._register_secret = register_secret
        self._first_wait = first_wait_seconds
        self._poll_seconds = poll_seconds
        #: Whether this attempt asks at all (its profile), and what swarm-api
        #: has shown: True a target, False none (204), None not known yet.
        self.wanted = getattr(cfg, "runner_profile", None) == PROFILE
        self.has_target: bool | None = None
        self.target_id: str | None = None
        #: The gate's state for the target (`ready`, `signing_in`, `signed_in`,
        #: `challenge`, `failed`), or None while nothing was handed over.
        self.state: str | None = None
        self.cause: str | None = None
        self.sign_ins = 0
        self._own_login = False
        self._started = False
        #: `swarm-handoff sign-in` requests waiting on the sign-in running now.
        self._pending: list[str] = []

    def notice(self) -> str | None:
        if self.wanted and self.has_target and self.state in ("challenge", "failed") and self.cause:
            return notice(self.cause)
        return None

    # -- what the hand-off path calls ---------------------------------------------

    def start(self, gate: Any, key: Any, api: Any, root: Path, unavailable: str | None = None) -> None:
        """The first ask and sign-in, before the runner. Once per attempt."""
        if self._started:
            return
        self._started = True
        if not self.wanted or gate is None:
            return
        try:
            (root / SIGN_INS_DIR).mkdir(parents=True, exist_ok=True)
            if unavailable is not None or key is None or api is None:
                self._fail(unavailable or NO_ATTEMPT_KEY)
            else:
                self._ask(gate, key, api)
                self._await_first(gate)
        except Exception as exc:  # the QA target never crashes the attempt
            self.log.warning("qa target start failed", error=type(exc).__name__)
        self._write_notice(root)

    def keep(self, gate: Any, key: Any, api: Any, root: Path) -> None:
        """On each poll: follow a running sign-in, answer `swarm-handoff sign-in`."""
        try:
            if self.state == SIGNING_IN and gate is not None:
                self._poll(gate)
            if self.state != SIGNING_IN:
                self._answer_pending(root)
            self._answer_requests(gate, key, api, root)
        except Exception as exc:  # nor on a poll
            self.log.warning("qa target upkeep failed", error=type(exc).__name__)
        self._write_notice(root)

    # -- internals ------------------------------------------------------------------

    def _ask(self, gate: Any, key: Any, api: Any) -> None:
        try:
            answer = issue(self.cfg, key, api, now=self._wall())
        except IssueRefused as refused:
            if refused.cause in _TARGET_CAUSES:
                self.has_target = True
            self._fail(refused.cause)
            return
        if answer is None:
            # swarm-api: this attempt's agent has no target (the common case).
            self.wanted = False
            self.has_target = False
            return
        self.has_target = True
        try:
            args = handoff_args(answer)
        except ValueError:
            self._fail(MALFORMED)
            return
        finally:
            answer.clear()
        self.target_id = args["target_id"]
        self._own_login = "credential" in args
        if self._own_login and self._register_secret is not None:
            try:
                self._register_secret(args["credential"]["password"])
            except Exception:  # redaction is a second guard; nothing logs the value anyway
                pass
        try:
            result = gate.call(CTL_OP, args)
        except Exception as exc:
            # The exception's class only: a message could quote what was sent.
            refused = type(exc).__name__ == "GateRefused"
            self._fail(GATE_REFUSED if refused else GATE_UNREACHABLE)
            return
        finally:
            # Forgotten once the gate has answered (§2.2 step 2).
            credential = args.pop("credential", None)
            if isinstance(credential, dict):
                credential.clear()
            args.clear()
        if self._own_login:
            self.sign_ins += 1
        self._apply(result)
        self.log.info("qa target handed to the gate", target_id=self.target_id, state=self.state,
                      sign_ins=self.sign_ins)

    def _await_first(self, gate: Any) -> None:
        deadline = self._wall() + self._first_wait
        while self.state == SIGNING_IN:
            if self._wall() >= deadline:
                self.log.warning("qa target sign-in still running as the runner starts",
                                 target_id=self.target_id)
                return
            self._sleep(self._poll_seconds)
            self._poll(gate)

    def _poll(self, gate: Any) -> None:
        try:
            result = gate.call(CTL_OP, {"poll": True})
        except Exception as exc:
            self.log.warning("the gate did not say how the qa sign-in went", error=type(exc).__name__,
                             target_id=self.target_id)
            return
        before = self.state
        self._apply(result)
        if self.state != before and self.state in FINAL:
            self.log.info("qa target sign-in ended", target_id=self.target_id, state=self.state,
                          cause=self.cause)

    def _apply(self, result: Any) -> None:
        result = result if isinstance(result, Mapping) else {}
        state = result.get("qa_sign_in")
        cause = result.get("cause")
        self.state = state if state in (READY, SIGNING_IN, *FINAL) else "failed"
        self.cause = cause if isinstance(cause, str) and _CAUSE.fullmatch(cause) else None
        if self.state == "failed" and self.cause is None:
            self.cause = "qa_sign_in_failed"

    def _fail(self, cause: str) -> None:
        self.state, self.cause = "failed", cause
        if self.has_target:
            self.log.warning("qa target unavailable; the browser is signed out of the target",
                             target_id=self.target_id, cause=cause)
        else:
            self.log.warning("qa target not known for this attempt", cause=cause)

    def _respond(self, root: Path, request_id: str) -> None:
        if self.state == SIGNED_IN:
            document = {"outcome": "applied", "exit": 0, "signed_in": True}
        else:
            cause = self.cause or ("qa_sign_in_failed" if self.wanted else NONE)
            document = {"outcome": "refused", "exit": 5, "code": cause, "message": reason(cause)}
        _write_atomic(root / "responses" / f"{request_id}.json", {"request_id": request_id, **document})

    def _refuse(self, root: Path, request_id: str, cause: str, message: str) -> None:
        _write_atomic(root / "responses" / f"{request_id}.json",
                      {"request_id": request_id, "outcome": "refused", "exit": 5, "code": cause,
                       "message": message})

    def _answer_pending(self, root: Path) -> None:
        pending, self._pending = self._pending, []
        for request_id in pending:
            self._respond(root, request_id)

    def _answer_requests(self, gate: Any, key: Any, api: Any, root: Path) -> None:
        folder = root / SIGN_INS_DIR
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            request_id = path.stem
            path.unlink(missing_ok=True)
            if not _REQUEST_ID.fullmatch(request_id):
                continue
            if not self.wanted or gate is None:
                self._refuse(root, request_id, NONE, "this attempt has no QA target to sign in to")
                continue
            if self.has_target and not self._own_login and self.state != "failed":
                self._refuse(root, request_id, NONE, "this QA target has no login: its pages are public")
                continue
            if self.state == SIGNING_IN:
                self._pending.append(request_id)
                continue
            if self.cause == CHALLENGE:
                self._respond(root, request_id)
                continue
            if self.sign_ins >= MAX_SIGN_INS:
                self.state, self.cause = "failed", EXHAUSTED
                self._respond(root, request_id)
                continue
            if key is None or api is None:
                self._refuse(root, request_id, NO_ATTEMPT_KEY, reason(NO_ATTEMPT_KEY))
                continue
            self._ask(gate, key, api)
            if self.state == SIGNING_IN:
                self._pending.append(request_id)
            else:
                self._respond(root, request_id)

    def _write_notice(self, root: Path) -> None:
        target = root / NOTICE_NAME
        text = self.notice()
        try:
            if text is None:
                target.unlink(missing_ok=True)
            else:
                _write_atomic(target, text)
        except OSError as exc:
            self.log.warning("could not write the qa target notice", error=type(exc).__name__)


__all__ = [
    "CTL_OP", "MAX_SIGN_INS", "NOTICE_NAME", "PROFILE", "QA_TARGET_PURPOSE", "SIGN_INS_DIR",
    "IssueRefused", "QaTargetSignIn", "handoff_args", "issue", "notice", "reason",
]
