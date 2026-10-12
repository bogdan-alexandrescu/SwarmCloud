"""The live-browser hand-off, the worker's half (docs/design/live-browser.md §4).

Lane LB-L4. Read §4.1 (the agent asks), §4.3 (the bounded wait), §4.4
(cancellation), §5.4.1 (the attempt key) and §5.4.2 (ctl.sock) before changing
anything here. swarm-api's half is `swarm_api.handoff` (lane L10); the gate's
end of ctl.sock is `agent_worker.livegate.ctl` (lane L3).

    $SWARM_HANDOFF = work/.swarm-handoff/
      requests/<id>.json   the agent's, through `swarm-handoff request` (.tmp, rename)
      devices/<id>.json    the agent's, through `swarm-handoff device <preset>`
      responses/<id>.json  this worker's: {"outcome": ..., "exit": N, ...}
      status.json          this worker's: the last control episode, for `status`
      unavailable          this worker's: present when this attempt has no live path
      README.md            this worker's: the guide the agent reads

WHY A SPOOL. As the child-task spool (`children.py`): no listener in the
agent's reach, and `work/` is checkpointed, so a request survives a park.

WHAT THE WORKER DOES WITH A REQUEST (§4.1), on its control poll:

  1. validates it -- `reason` at most 200 characters and redacted before it
     goes anywhere, `expect` a bare host `url_refusal` accepts, at most
     `max_handoffs_per_attempt` per attempt, one at a time;
  2. tells the gate to PAUSE the agent's CDP (`pause` on ctl.sock), so from
     here the agent cannot act on the browser, cooperative or not;
  3. records the wait through swarm-api's worker route, signed with the
     attempt key under `swarm-handoff/v1`. swarm-api sets `Task.human_wait`;
     the task stays RUNNING and keeps exactly the capacity it holds.

Then, on every poll while the wait is open, it reads the task:

  * `human_wait` cleared by swarm-api (a person pressed Return control, lane
    L9) -> `resume` on the gate, and the agent's `wait` exits 0 with the
    summary of §5.9 (origin and path, redacted; never who or what was typed);
  * the hold's deadline passed with nobody in control, or the gate's channel
    dropped (§5.4.2: a restarted gate ends the live path) -> the agent's `wait`
    exits 3 and the lifecycle parks: wipe, checkpoint, upload,
    PARKED(HUMAN_REQUIRED), the attempt refunded while fewer than
    `max_human_resumes` have been, release, exit (§4.3, invariant 4);
  * a cancel (the lifecycle's control poll) -> the agent's `wait` exits 4,
    the gate ends control and wipes, and the ordinary cancel path runs (§4.4).

A WORKER THAT CANNOT PROTECT ITS HEAP GETS NO LIVE PATH (§5.4.1): no tunnel
ticket and no hand-off; the agent's `swarm-handoff` exits 5. It still takes
the gate's one registration slot, so the agent cannot take it instead: an
open slot is worse than a key in an unprotected heap.

NOTHING HERE LOGS a ticket, a signature, a key, the request's reason or a URL
the person visited. Log lines carry outcomes, codes and counts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from swarm_common.models import utcnow
from swarm_common.profiles import RUNNER_PROFILES, url_refusal
from swarm_common.states import EventType, ParkReason, TaskState, assert_transition
from swarm_redaction import redact as redact_text

from .errors import ExitCode
from .hardening import PROTECTED

#: The spool's directory under `work/`. `Workspace.handoff_dir` is the one
#: path; this is its name.
SPOOL_DIR_NAME = ".swarm-handoff"
#: The variable that tells an agent it may ask for a person, and where.
HANDOFF_ENV = "SWARM_HANDOFF"
#: Where the dispatcher puts ctl.sock (`scheduler.dispatch.LIVE_GATE_CTL`,
#: lane L5), passed to the worker container as SWARM_GATE_CTL.
GATE_CTL_ENV = "SWARM_GATE_CTL"
#: The gate's CDP proxy for the agent's Playwright (§3.2), and the upload
#: directory Chromium can read (§3.1). Passed on to the agent: a path and a
#: loopback address, never a credential.
AGENT_PASSTHROUGH_ENV = ("SWARM_CDP_ENDPOINT", "SWARM_BROWSER_UPLOADS")

#: §5.4.1: each worker-only route signs a domain-separated message, so a
#: signature for one is never a signature for another. `swarm_api.handoff`
#: holds HANDOFF_PURPOSE; tests/unit/worker/test_handoff.py holds them equal.
HANDOFF_PURPOSE = "swarm-handoff/v1"
LIVE_SESSION_PURPOSE = "swarm-live-session/v1"

#: `swarm_api.validation.HUMAN_WAIT_RESUMES_METADATA_KEY`, restated (this image
#: does not carry swarm-api); the test holds them equal.
HUMAN_WAIT_RESUMES_METADATA_KEY = "human_wait_resumes"

#: `swarm_api.handoff.REQUEST_ID` and its `expect` host rule, restated.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
#: A device preset's NAME (§6.1). The list itself is the gate's: a name
#: outside it is refused there. A caller never sends dimensions.
_PRESET = re.compile(r"^[a-z0-9-]{1,32}$")

#: §4.1 step 1.
MAX_REASON_CHARS = 200
#: §5.10: hand-off requests per attempt. swarm-api enforces it as well, from
#: the task document, so a worker that miscounts cannot pester people.
MAX_HANDOFFS_PER_ATTEMPT = 3
#: §4.3: parks on HUMAN_REQUIRED that do not spend an attempt.
MAX_HUMAN_RESUMES = 2
#: One request file, as read. swarm-api refuses a body over 4096 bytes.
MAX_REQUEST_BYTES = 4096

#: §4.2: a controller's console renews every 60 s and swarm-api moves the
#: deadline on input. While a person holds control the worker waits this long
#: past the deadline for swarm-api to end the idle control itself before it
#: parks under them.
CONTROLLED_GRACE_SECONDS = 60
#: §4.1 exit 3: "write its notes into work/ and stop; the worker ends it either
#: way". How long the agent gets to do that before it is stopped. Bounded, so
#: a hold's end is never the long wait invariant 4 forbids.
EXPIRY_NOTES_SECONDS = 30
#: §4.4: how long the agent's `wait` gets to read "cancelled" before the
#: ordinary cancel path stops it.
CANCEL_ANSWER_SECONDS = 3

#: `swarm-handoff`'s exit codes (§4.1). The command restates them.
EXIT_RETURNED = 0
EXIT_EXPIRED = 3
EXIT_CANCELLED = 4
EXIT_REFUSED = 5
EXIT_STILL_WAITING = 75

#: Why an open hand-off ended without a person returning control. The
#: lifecycle parks on either.
HOLD_EXPIRED = "hold_expired"
GATE_LOST = "gate_lost"

#: Why this attempt has no live path, as the agent's refusal and the log say it.
NOT_LIVE = "not_a_live_browser_profile"
WORKER_UNPROTECTED = "worker_unprotected"
GATE_UNAVAILABLE = "gate_unavailable"
NO_ATTEMPT_KEY = "no_attempt_key"
NO_LIVE_SESSION = "no_live_session"

#: The ctl.sock operations this module sends. Every one must be in the gate's
#: `livegate.ctl.OPS`; the test holds that once L3 is importable.
#:
#: THERE IS NO `wipe` OPERATION IN L3's OPS. §5.5 says the worker tells the
#: gate `wipe` at every attempt end; the closest faithful version on L3's
#: channel is `end_control` with `wipe: true`, which the gate's handler must
#: honour by closing Chromium and removing the profile. Reported on the PR.
GATE_OPS = frozenset({"tunnel_ticket", "pause", "resume", "end_control", "device", "status"})

#: The guide the agent reads, written into the spool by `prepare`.
GUIDE_NAME = "README.md"
AGENT_GUIDE = """\
# Asking a person for help with the browser

When the browser needs something only a person can do -- a sign-in, a
two-factor prompt, a CAPTCHA -- ask for help instead of guessing:

    swarm-handoff request --reason "Sign in to GitHub as the bot account" --expect github.com
    swarm-handoff wait

`--reason` (at most 200 characters) is shown to the person; never put a
secret in it. `--expect` is the bare host they should sign in on. While you
wait, your browser connection is paused: a person is using it.

`wait` blocks for up to 9 minutes and exits:

  0   control was returned; a JSON summary (url, title, held_seconds) is on stdout.
      Look at the page and continue: you are signed in.
  75  still waiting: call `swarm-handoff wait` again.
  3   nobody came in time. Write everything you need to continue into your
      working files now; the task is parked until a person is ready, then
      started again on your restored files. The browser session is NOT kept.
  4   the task is being cancelled: stop.
  5   refused (no live path, the limit of 3 requests, or an invalid request):
      carry on without help, or fail with that reason.

`swarm-handoff status` prints the last control episode in the same shape as
`wait`. `swarm-handoff device <preset>` switches the browser to a device
preset by name (desktop, iphone-15, pixel-8, iphone-se, ipad-7).

Never copy cookies or a storage state into your files: they are refused.
"""


class GateLost(Exception):
    """ctl.sock failed: closed, timed out, or answered by something that is not the gate."""


class GateRefused(GateLost):
    """The gate answered and refused the operation."""


def _is(exc: BaseException, name: str) -> bool:
    return any(klass.__name__ == name for klass in type(exc).__mro__)


class Gate:
    """The worker's use of ctl.sock, over L3's `CtlClient`.

    L3's errors (`CtlRefused`, `CtlError`) become `GateRefused` and `GateLost`
    here, matched by name, so this module imports nothing of the gate's at
    import time and runs where the gate's module is not installed.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.registered = False

    def register(self) -> None:
        try:
            self._client.register()
        except Exception as exc:
            raise self._translate(exc) from None
        self.registered = True

    def call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if op not in GATE_OPS:
            raise GateRefused(f"{op} is not a gate operation")
        if not self.registered:
            raise GateLost("not registered")
        try:
            return dict(self._client.call(op, dict(args)) or {})
        except Exception as exc:
            raise self._translate(exc) from None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self.registered = False

    @staticmethod
    def _translate(exc: Exception) -> GateLost:
        # The exception's TYPE only: a message could quote what was sent.
        if _is(exc, "CtlRefused"):
            return GateRefused(type(exc).__name__)
        return GateLost(type(exc).__name__)


def _default_gate_client(path: str) -> Any:
    """L3's client. Imported here, not at module load, so a worker image
    built before L3 merged still starts; it then has no live path."""
    from .livegate.ctl import CtlClient

    return CtlClient(path)


def signed_message(purpose: str, method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one worker-only live route (§5.4.1).

    The child path's shape (`children.request_message`) under the route's own
    purpose, so no signature verifies for another route.
    """
    digest = hashlib.sha256(body).hexdigest()
    return f"{purpose}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_headers(
    key: Any, purpose: str, method: str, path: str, body: bytes, *, now: float
) -> dict[str, str]:
    """The proof headers for one request, signed by the attempt key in this heap.

    `key` is the child path's `AttemptKey`; the private half is read where it
    lives and never leaves this call.
    """
    from .children import PROOF_HEADER, TIMESTAMP_HEADER

    timestamp = str(int(now))
    signature = key._private.sign(signed_message(purpose, method, path, body, timestamp))
    return {PROOF_HEADER: _b64url(signature), TIMESTAMP_HEADER: timestamp}


def expect_refusal(value: str) -> str:
    """Why `value` is not a host a hand-off may name, or "" when it is."""
    if not _HOST.fullmatch(value):
        return "expect is a bare host name: lower-case letters, digits, dots and hyphens"
    return url_refusal(f"https://{value}/")


def public_url(value: Any) -> str | None:
    """§5.9: the top frame's origin and path, redacted, with no query or fragment."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return redact_text(f"{parts.scheme}://{host}{parts.path or '/'}").text


def _write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.write_text(json.dumps(document, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_request(path: Path) -> Any:
    """A spooled JSON object, or None. Never follows a link the agent made."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


class HandoffPath:
    """One attempt's live path: the gate registration, the tunnel, the hand-off spool."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        memory: Any,
        children: Any,
        live: bool | None = None,
        gate_client: Callable[[str], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = utcnow,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._memory = memory
        self._children = children
        env = os.environ if environ is None else environ
        if live is None:
            profile = RUNNER_PROFILES.get(getattr(cfg, "runner_profile", ""))
            live = bool(getattr(profile, "live_browser", False))
        self.live = live
        self._ctl_path = env.get(GATE_CTL_ENV, "")
        self._passthrough = {k: env[k] for k in AGENT_PASSTHROUGH_ENV if env.get(k)}
        self._gate_client = gate_client or _default_gate_client
        self._now = now
        self._wall = wall
        self.gate: Gate | None = None
        #: Why this attempt has no live path; None while it has one.
        self.unavailable: str | None = NOT_LIVE if not live else "not_set_up"
        #: The open hand-off: request id, when it was opened, its deadline.
        self.active: dict[str, Any] | None = None
        self.requests_made = 0
        self.max_handoffs = int(getattr(cfg, "max_handoffs_per_attempt", MAX_HANDOFFS_PER_ATTEMPT))
        self.max_resumes = int(getattr(cfg, "max_human_resumes", MAX_HUMAN_RESUMES))
        self._wiped = False

    # -- what the lifecycle asks ---------------------------------------------

    @property
    def offered(self) -> bool:
        """Whether this attempt has a live path an agent may ask through."""
        return self.live and self.unavailable is None

    @property
    def spooled(self) -> bool:
        """Whether the agent gets a spool at all: every live-browser attempt does,
        so `swarm-handoff` can say WHY it is refused rather than not exist."""
        return self.live

    def setup(self, note: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Take the gate's slot, then open the tunnel (§3.3, §5.4.2). Never raises.

        Called once the task is RUNNING (the live-session route checks the
        fence on RUNNING) and before the agent exists, so the registration is
        the first the gate sees.
        """
        if not self.live:
            return
        # FIRST, whatever else is wrong: the slot is first-registration-wins,
        # and a worker that leaves it open leaves it to the agent.
        if not self._ctl_path:
            return self._refuse(note, GATE_UNAVAILABLE, "no SWARM_GATE_CTL for this attempt")
        try:
            gate = Gate(self._gate_client(self._ctl_path))
            gate.register()
        except (GateLost, ImportError, OSError) as exc:
            return self._refuse(note, GATE_UNAVAILABLE, type(exc).__name__)
        self.gate = gate
        if getattr(self._memory, "status", None) != PROTECTED:
            # §5.4.1: no tunnel ticket and no hand-off. The slot stays taken.
            return self._refuse(note, WORKER_UNPROTECTED, "the worker's heap is not protected")
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            why = (
                getattr(self._children, "unavailable_reason", None)
                or getattr(self._children, "unavailable", None)
                or "no attempt key registered"
            )
            return self._refuse(note, NO_ATTEMPT_KEY, str(why))
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "task_id": self.cfg.task_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
            }
        ).encode("utf-8")
        path = f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/live-session"
        try:
            headers = sign_headers(key, LIVE_SESSION_PURPOSE, "POST", path, body, now=self._wall())
            _, payload = api.call("POST", path, body=body, headers=headers)
        except Exception as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            return self._refuse(note, NO_LIVE_SESSION, str(code))
        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(ticket, str) or not ticket:
            return self._refuse(note, NO_LIVE_SESSION, "no ticket in the answer")
        try:
            # The ticket goes to the gate and nowhere else: not a log line,
            # not the environment, not a file (§3.3 step 2).
            gate.call("tunnel_ticket", {"ticket": ticket, "session_id": session_id})
        except GateLost as exc:
            return self._refuse(note, GATE_UNAVAILABLE, type(exc).__name__)
        finally:
            ticket = None
        self.unavailable = None
        self.log.info("live path open for this attempt")

    def _refuse(self, note: Callable[[dict[str, Any]], Any] | None, code: str, why: str) -> None:
        self.unavailable = code
        self.log.warning("no live path for this attempt", live_path=code, reason=why)
        if note is not None:
            try:
                note({"phase": "live_session", "live_path": code, "reason": why})
            except Exception:  # a note must never stop the attempt
                pass

    def spool(self, work: Path) -> Path:
        return Path(work) / SPOOL_DIR_NAME

    def prepare(self, work: Path) -> dict[str, str]:
        """Create the spool and return the agent's variables, or nothing."""
        if not self.spooled:
            return {}
        root = self.spool(work)
        for name in ("requests", "devices", "responses"):
            (root / name).mkdir(parents=True, exist_ok=True)
        marker = root / "unavailable"
        if self.offered:
            marker.unlink(missing_ok=True)
        else:
            # Rewritten every attempt: a restored checkpoint's marker is not
            # this attempt's answer.
            _write_atomic(marker, {"code": self.unavailable})
        guide = root / GUIDE_NAME
        tmp = root / f".{GUIDE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(AGENT_GUIDE, encoding="utf-8")
        os.replace(tmp, guide)
        env = {HANDOFF_ENV: str(root)}
        if self.offered:
            env.update(self._passthrough)
        return env

    # -- the poll --------------------------------------------------------------

    def tick(self, work: Path, fetch_task: Callable[[], Mapping[str, Any]]) -> str | None:
        """Answer the spool and watch the open hand-off. Never raises.

        Returns HOLD_EXPIRED or GATE_LOST when the lifecycle must park, None
        otherwise. Bounded: one gate call per spooled file plus one status
        call, and at most one route call per request.
        """
        if not self.spooled:
            return None
        root = self.spool(work)
        try:
            self._answer_devices(root)
            if self.active is None:
                self._answer_requests(root)
            if self.active is not None:
                return self._watch(root, fetch_task)
            self._note_last_episode(root)
        except Exception as exc:  # the hand-off never crashes the attempt
            self.log.warning("hand-off poll failed", error=type(exc).__name__)
        return None

    def _respond(self, root: Path, request_id: str, document: Mapping[str, Any]) -> None:
        _write_atomic(root / "responses" / f"{request_id}.json", {"request_id": request_id, **document})

    def _refusal(self, root: Path, request_id: str, code: str, message: str) -> None:
        self._respond(
            root, request_id,
            {"outcome": "refused", "exit": EXIT_REFUSED, "code": code, "message": message},
        )
        self.log.info("hand-off request refused", code=code)

    def _answer_requests(self, root: Path) -> None:
        requests = root / "requests"
        if not requests.is_dir():
            return
        for path in sorted(requests.glob("*.json")):
            request_id = path.stem
            data = _read_request(path)
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id):
                continue
            self._open(root, request_id, data)
            if self.active is not None:
                # One at a time: a request behind an open one is answered
                # once that one ends, on a later poll.
                break

    def _open(self, root: Path, request_id: str, data: Any) -> None:
        if not self.offered:
            return self._refusal(
                root, request_id, self.unavailable or NO_LIVE_SESSION,
                "this attempt has no live browser path; carry on without help",
            )
        if not isinstance(data, dict):
            return self._refusal(root, request_id, "request_invalid", "the request is not a JSON object")
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return self._refusal(root, request_id, "reason_invalid", "reason is required")
        reason = reason.strip()
        if len(reason) > MAX_REASON_CHARS:
            return self._refusal(
                root, request_id, "reason_invalid", f"reason is at most {MAX_REASON_CHARS} characters"
            )
        # Redacted BEFORE it leaves this process (§4.1 step 1).
        reason = redact_text(reason).text[:MAX_REASON_CHARS]
        expect = data.get("expect")
        if expect is not None:
            if not isinstance(expect, str):
                return self._refusal(root, request_id, "expect_invalid", "expect is a host name")
            expect = expect.strip().lower() or None
            if expect:
                why = expect_refusal(expect)
                if why:
                    return self._refusal(root, request_id, "expect_invalid", why)
        if self.requests_made >= self.max_handoffs:
            return self._refusal(
                root, request_id, "handoff_limit",
                f"this attempt has asked for help {self.requests_made} times; the cap is "
                f"{self.max_handoffs}",
            )
        gate = self.gate
        assert gate is not None
        # §4.1 step 2: the agent's CDP is held BEFORE the wait is recorded, so
        # there is no moment a person is told to come while the agent can act.
        try:
            gate.call("pause", {"request_id": request_id, "expect": expect})
        except GateLost as exc:
            self.unavailable = GATE_UNAVAILABLE
            self.log.warning("the gate did not pause the agent", error=type(exc).__name__)
            return self._refusal(root, request_id, GATE_UNAVAILABLE, "the live browser is unavailable")
        self.requests_made += 1
        try:
            _, payload = self._route(
                f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/handoff",
                {"request_id": request_id, "reason": reason, "expect": expect},
            )
        except Exception as exc:
            code = str(getattr(exc, "code", None) or type(exc).__name__)
            self._resume_quietly()
            return self._refusal(root, request_id, code, "swarm-api did not record the request")
        wait = payload.get("human_wait") if isinstance(payload, dict) else None
        deadline = _as_datetime((wait or {}).get("deadline")) if isinstance(wait, dict) else None
        now = self._now()
        self.active = {
            "request_id": request_id,
            "opened_at": now,
            "opened_wall": self._wall(),
            "deadline": deadline,
        }
        self._respond(
            root, request_id,
            {"outcome": "waiting", "deadline": deadline.isoformat() if deadline else None},
        )
        self.log.info("hand-off opened; the agent's browser is paused for a person")

    def _route(self, path: str, fields: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            raise RuntimeError(NO_ATTEMPT_KEY)
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
                **fields,
            }
        ).encode("utf-8")
        headers = sign_headers(key, HANDOFF_PURPOSE, "POST", path, body, now=self._wall())
        return api.call("POST", path, body=body, headers=headers)

    def _watch(self, root: Path, fetch_task: Callable[[], Mapping[str, Any]]) -> str | None:
        active = self.active
        assert active is not None and self.gate is not None
        try:
            self.gate.call("status", {})
        except GateRefused:
            pass
        except GateLost:
            # §5.4.2: the channel dropped (a restarted gate refuses us for
            # good). The live path is over; the open hand-off ends as if the
            # hold had expired.
            self.unavailable = GATE_UNAVAILABLE
            return GATE_LOST
        task = fetch_task() or {}
        wait = task.get("human_wait")
        if not isinstance(wait, dict) or wait.get("request_id") != active["request_id"]:
            self._returned(root)
            return None
        deadline = _as_datetime(wait.get("deadline")) or active.get("deadline")
        if deadline is None:
            return None
        active["deadline"] = deadline
        grace = CONTROLLED_GRACE_SECONDS if wait.get("controller") else 0
        if (self._now() - deadline).total_seconds() >= grace:
            return HOLD_EXPIRED
        return None

    def _returned(self, root: Path) -> None:
        """§4.2 step 3: control came back. Resume the agent; answer `wait`."""
        active = self.active
        assert active is not None and self.gate is not None
        result: dict[str, Any] = {}
        try:
            # The gate clears every password input and restores the agent's
            # page scripts before it lets the agent's commands through.
            result = self.gate.call("resume", {"request_id": active["request_id"]})
        except GateLost as exc:
            self.log.warning("the gate did not confirm the resume", error=type(exc).__name__)
        summary = {
            "outcome": "returned",
            "exit": EXIT_RETURNED,
            "held_seconds": int(max(0.0, self._wall() - active["opened_wall"])),
            "url": public_url(result.get("url")),
            "title": redact_text(str(result.get("title") or "")[:200]).text,
        }
        self._respond(root, active["request_id"], summary)
        _write_atomic(root / "status.json", summary)
        self.active = None
        self.log.info("hand-off returned; the agent's browser is resumed",
                      held_seconds=summary["held_seconds"])

    def _note_last_episode(self, root: Path) -> None:
        """§4.2 (Q6): a person may take control unasked. `status` reports the
        last episode the gate knows of, in `wait`'s shape."""
        if not self.offered or self.gate is None:
            return
        try:
            result = self.gate.call("status", {})
        except GateLost:
            return
        episode = result.get("last_episode")
        if not isinstance(episode, dict):
            return
        _write_atomic(
            root / "status.json",
            {
                "outcome": "returned",
                "held_seconds": int(episode.get("held_seconds") or 0),
                "url": public_url(episode.get("url")),
                "title": redact_text(str(episode.get("title") or "")[:200]).text,
            },
        )

    def _answer_devices(self, root: Path) -> None:
        devices = root / "devices"
        if not devices.is_dir():
            return
        for path in sorted(devices.glob("*.json")):
            request_id = path.stem
            data = _read_request(path)
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id):
                continue
            preset = data.get("preset") if isinstance(data, dict) else None
            if not self.offered or self.gate is None:
                self._refusal(root, request_id, self.unavailable or NO_LIVE_SESSION,
                              "this attempt has no live browser path")
                continue
            if not isinstance(preset, str) or not _PRESET.fullmatch(preset):
                self._refusal(root, request_id, "preset_invalid", "a preset is named, never sized")
                continue
            try:
                self.gate.call("device", {"preset": preset})
            except GateRefused:
                self._refusal(root, request_id, "preset_refused", f"the gate refused {preset!r}")
                continue
            except GateLost:
                self._refusal(root, request_id, GATE_UNAVAILABLE, "the live browser is unavailable")
                continue
            self._respond(root, request_id, {"outcome": "applied", "exit": 0, "preset": preset})

    # -- the ends ----------------------------------------------------------------

    def end(self, work: Path | None, *, outcome: str, cause: str) -> bool:
        """End the open hand-off, if any: answer the agent, resolve the route,
        end control and wipe. Never raises. Returns whether one was open.

        `outcome` is swarm-api's (`expired`, `cancelled`, `unavailable`);
        the agent's exit follows from it. The route is resolved while the
        task is still RUNNING: swarm-api refuses it once the task has moved.
        """
        active = self.active
        self.active = None
        if active is not None:
            code = {"expired": EXIT_EXPIRED, "cancelled": EXIT_CANCELLED}.get(outcome, EXIT_REFUSED)
            if work is not None:
                try:
                    root = self.spool(work)
                    document = {"outcome": outcome, "exit": code, "cause": cause}
                    self._respond(root, active["request_id"], document)
                    _write_atomic(root / "status.json", document)
                except OSError as exc:
                    self.log.warning("could not answer the agent's wait", error=type(exc).__name__)
            try:
                self._route(
                    f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/handoff/resolve",
                    {"request_id": active["request_id"], "outcome": outcome},
                )
            except Exception as exc:
                # swarm-api clears a stale wait itself when the task moves on;
                # the park or the cancel below still happens.
                self.log.warning("the hand-off was not resolved at swarm-api",
                                 error=str(getattr(exc, "code", None) or type(exc).__name__))
            self.log.warning("hand-off ended", outcome=outcome, cause=cause)
        if cause != GATE_LOST:
            self.wipe(cause)
        return active is not None

    def wipe(self, cause: str) -> None:
        """§5.5: end control, close Chromium and remove the profile. Never raises.

        Sent once per attempt. Before the hand-off's own checkpoint on the
        paths that park or cancel from here; from `_cleanup` on every other
        exit. The profile is on a sidecar-only volume either way, never in
        `work/`, so no checkpoint has ever held it.
        """
        if self._wiped or self.gate is None or not self.gate.registered:
            return
        self._wiped = True
        try:
            self.gate.call("end_control", {"cause": cause, "wipe": True})
        except GateLost as exc:
            self.log.warning("the gate did not confirm the wipe", error=type(exc).__name__)

    def agent_exited(self, work: Path | None) -> None:
        """The agent ended on its own with a hand-off open: nobody is waited
        for any more. Resolved as `unavailable`, while the task is RUNNING."""
        if self.active is not None:
            self.end(work, outcome="unavailable", cause="agent_exited")

    def close(self) -> None:
        """The `_cleanup` backstop: wipe on every exit, then drop the channel."""
        if self.gate is None:
            return
        self.wipe("attempt_end")
        self.gate.close()

    def _resume_quietly(self) -> None:
        if self.gate is None:
            return
        try:
            self.gate.call("resume", {})
        except GateLost as exc:
            self.log.warning("the gate did not resume the agent", error=type(exc).__name__)


def park_human_required(
    control: Any,
    *,
    max_resumes: int,
    detail: dict[str, Any] | None = None,
    uploads: list[Any] | None = None,
) -> bool:
    """§4.3: PARKED on HUMAN_REQUIRED, slot given back, the attempt refunded
    while fewer than `max_resumes` have been.

    The await park's shape exactly (`ControlPlane.park_awaiting_children`).
    ONE FENCED TRANSACTION: a superseded attempt gets `FencedWriteRefused`
    with nothing written, so it can neither park the task nor refund an
    attempt (invariant 5). `next_eligible_at` is the park instant, which the
    scheduler's dead-letter sweep measures `human_park_max_seconds` from; NO
    sweep promotes it, only swarm-api's `human-ready`. `human_wait` is NOT
    written here: it is swarm-api's field alone (`Task.human_wait`). The
    worker's resolve route cleared it before this park, and `human-ready`
    clears one a failed resolve left behind.

    Built on the control plane's own fenced-transaction helpers rather than as
    a `ControlPlane` method because `control.py` is outside lane L4's files;
    the transaction is the same one the other parks run.

    Returns whether the attempt was refunded.
    """
    reason = ParkReason.HUMAN_REQUIRED
    now = utcnow()

    def _apply(txn: Any) -> tuple[bool, int]:
        task = control._fenced_task(txn, write="human park")
        current = TaskState(task.get("state"))
        assert_transition(current, TaskState.PARKED)
        index = control._read_carry_index(txn) if uploads else None
        metadata = dict(task.get("metadata") or {})
        used = metadata.get(HUMAN_WAIT_RESUMES_METADATA_KEY)
        used = used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else 0
        attempt_count = int(task.get("attempt_count", 0))
        refund = used < max_resumes and attempt_count > 0
        payload: dict[str, Any] = {
            "state": TaskState.PARKED.value,
            "park_reason": reason.value,
            "next_eligible_at": now,
            "current_lease_id": None,
            "blocked_by": [{"reason": reason.value, **(detail or {})}],
            "updated_at": now,
        }
        if refund:
            metadata[HUMAN_WAIT_RESUMES_METADATA_KEY] = used + 1
            payload["metadata"] = metadata
            payload["attempt_count"] = attempt_count - 1
        txn.update(control._task_ref(), payload)
        if uploads:
            control._record_parked_uploads(txn, index, uploads, at=now)
        return refund, used + (1 if refund else 0)

    refunded, resumes = control._run_transaction(_apply)
    # Best effort, as in every park: the event and the release below are what
    # give the slot back.
    try:
        control.record_attempt_end(exit_code=ExitCode.PARKED, error=reason.value)
    except Exception as exc:
        control._log.warning(
            "the human park landed but its attempt end was not recorded",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    control.emit(
        EventType.PARKED,
        {
            "reason": reason.value,
            "next_eligible_at": now,
            "attempt_refunded": refunded,
            HUMAN_WAIT_RESUMES_METADATA_KEY: resumes,
            **(detail or {}),
        },
    )
    control.release_lease(f"parked:{reason.value}")
    return refunded
