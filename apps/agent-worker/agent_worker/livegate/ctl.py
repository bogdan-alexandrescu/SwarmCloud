"""ctl.sock: the worker's channel to the live gate, which the agent cannot use (L3).

docs/design/live-browser.md §5.4.2. `ctl.sock` sits on `/run/swarm-gate`, a
volume both containers mount, so the agent can connect to it too. What makes
it the worker's:

FIRST REGISTRATION WINS. The worker, after `make_non_dumpable`, generates a
32-byte key in its heap (`CtlClient.register`) and registers it. The native
sidecar is up first (the dispatcher's startup probe waits for this socket),
and the agent is spawned later (`Worker._prepare`), so the slot is taken
before the agent exists -- the same argument as the attempt key. A second
registration is refused and recorded as `gate_ctl_refused`.

EVERY LATER MESSAGE IS MAC'D AND COUNTED. A command carries a sequence number
greater than every one accepted before and an HMAC-SHA256 under the key over
a domain-separated canonical encoding of `(seq, op, args)`. A forged MAC, a
replayed or reordered counter and an operation not in `OPS` are refused.
Replies are MAC'd the same way under a different domain, so a worker talking
to anything but the gate -- an agent that swapped the socket file for its own
-- gets `CtlError`, not a forged answer.

A GATE THAT RESTARTS REFUSES EVERY REGISTRATION. The native sidecar has
`restartPolicy: Always`, so the gate can restart while the agent is alive, and
a fresh process would otherwise reopen the slot for whoever connects first.
On its first start the gate creates `marker` (exclusively) on its own
sidecar-only volume; a gate that finds it there is a restart: it still listens
(the startup probe is satisfied) but accepts no registration at all, and
records `gate_restarted`. The gate's entrypoint reads `CtlServer.restarted`
and keeps Chromium closed. A marker that cannot be written is treated the same
way: a gate that cannot prove it is the first never opens the slot. The
worker sees its channel drop and treats the live path as ended for the
attempt (§5.4.2); it does not re-key.

NOTHING CARRIED HERE IS LOGGED. The tunnel ticket (§3.3, step 2) and the
egress ticket (browser-egress.md §2.5) travel over this channel; log lines and records carry the operation's outcome and a
refusal's cause, never `args`, a key or a MAC.

The gate side (`CtlServer`) is asyncio, like the rest of the gate. The worker
side (`CtlClient`) is blocking, like the worker lifecycle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .session import OPS as SESSION_OPS

log = logging.getLogger(__name__)

KEY_BYTES = 32

#: The operations the worker may ask of the gate. Anything else is refused.
#:
#: * `tunnel_ticket` -- the one-use ticket swarm-api minted (§3.3); the gate
#:   opens the tunnel with it as its first message.
#: * `pause` / `resume` -- the agent's CDP is held for a hand-off (§4.1 step 2)
#:   and released on return (§4.2 step 3).
#: * `end_control` -- the task was cancelled or the hold expired (§4.3, §4.4).
#: * `device` -- a preset by name, from `swarm-handoff device` (§6.1, Q13).
#: * `status` -- what the gate is doing, for `swarm-handoff status`.
#: * `session_export` / `session_import` -- a signed-in session carried gate
#:   to gate (session.py, §5.5.1). Named there, once; the worker sends them
#:   (browser_login.py), and before L3m this list refused both.
#: * `egress_ticket` -- the attempt egress ticket swarm-api minted, or why none
#:   can be had (`failed`); the gate's local forwarder presents it to
#:   swarm-egress on every CONNECT and refuses all of them without it
#:   (egress.py, docs/browser-egress.md §2.5; EGRESS-GATE).
#: * `browser_tools` -- what the runner registered for the task's browser
#:   tool, forwarded from the hand-off spool, which the gate reports
#:   (agent_worker/browser_tools.py; BSTATUS-API).
OPS = frozenset({"tunnel_ticket", "pause", "resume", "end_control", "device", "status",
                 "egress_ticket", "browser_tools"}) | SESSION_OPS

#: The first start's marker, in the sidecar's own /tmp (§3.1; the dispatcher
#: mounts that volume in the sidecar only). Directly in /tmp, not a subdirectory:
#: a directory nothing created would make every first start look like a restart.
DEFAULT_MARKER = "/tmp/swarm-gate.started"

#: One message is a line of JSON no longer than this.
MAX_LINE = 64 * 1024

_DOMAIN = b"swarm-gate-ctl/v1\0"
_COMMAND = _DOMAIN + b"command\0"
_REPLY = _DOMAIN + b"reply\0"

Handler = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]
Record = Callable[[str, Mapping[str, str]], None]


class CtlError(Exception):
    """The channel failed: closed, timed out, or answered by something that is not the gate."""


class CtlRefused(CtlError):
    """The gate refused the registration or the command."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _mac(key: bytes, domain: bytes, value: Mapping[str, Any]) -> str:
    return hmac.new(key, domain + _canonical(value), hashlib.sha256).hexdigest()


def _line(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


# --------------------------------------------------------------------------
# The gate's side
# --------------------------------------------------------------------------


class CtlServer:
    """The gate's end of ctl.sock: one registration, then MAC'd commands."""

    def __init__(self, path: str, marker: str, handler: Handler, record: Record) -> None:
        self._path = path
        self._marker = marker
        self._handler = handler
        self._record = record
        self._key: bytes | None = None
        self._last_seq = 0
        self._server: asyncio.base_events.Server | None = None
        self.restarted = False

    @property
    def registered(self) -> bool:
        return self._key is not None

    async def start(self) -> None:
        self.restarted = not self._first_start()
        if self.restarted:
            log.warning("live ctl: gate restarted; no registration will be accepted")
        # Whatever is at the path -- the last process's socket, or one an
        # agent put in its place -- is not this process's listener.
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._serve, path=self._path, limit=MAX_LINE)
        # The worker reaches it through the pod's fsGroup (a group both
        # containers carry); nobody outside the pod can see the volume.
        os.chmod(self._path, 0o660)

    def _first_start(self) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            os.close(os.open(self._marker, flags, 0o600))
        except FileExistsError:
            self._record("gate_restarted", {})
            return False
        except OSError:
            self._record("gate_restarted", {"cause": "marker_unwritable"})
            return False
        return True

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _refuse(self, cause: str) -> None:
        self._record("gate_ctl_refused", {"cause": cause})
        log.warning("live ctl: refused cause=%s", cause)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self._refuse("malformed")
                    return
                if not raw:
                    return
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError
                except ValueError:
                    self._refuse("malformed")
                    writer.write(_line({"ok": False, "error": "malformed"}))
                    return
                reply, keep = await self._handle(message)
                writer.write(_line(reply))
                await writer.drain()
                if not keep:
                    return
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _handle(self, message: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if "register" in message:
            reply = self._register(message.get("register"))
            # A refused registration ends its connection; an accepted one is
            # the worker's channel.
            return reply, reply["ok"] is True

        if self._key is None:
            self._refuse("unregistered")
            return {"ok": False, "error": "refused"}, False
        seq, op, args, mac = (message.get(k) for k in ("seq", "op", "args", "mac"))
        if (not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(op, str)
                or not isinstance(args, dict) or not isinstance(mac, str)):
            self._refuse("malformed")
            return {"ok": False, "error": "refused"}, False
        expected = _mac(self._key, _COMMAND, {"seq": seq, "op": op, "args": args})
        if not hmac.compare_digest(expected, mac):
            self._refuse("bad_mac")
            return {"ok": False, "error": "refused"}, False
        # From here the sender holds the key: answers are signed.
        if seq <= self._last_seq:
            self._refuse("replay")
            return self._signed(seq, False, error="replay"), True
        self._last_seq = seq
        if op not in OPS:
            self._refuse("unknown_op")
            return self._signed(seq, False, error="unknown_op"), True
        try:
            result = dict(await self._handler(op, args))
        except Exception as exc:  # the handler's failure is the worker's to see, not a crash
            log.warning("live ctl: op=%s failed (%s)", op, type(exc).__name__)
            return self._signed(seq, False, error="failed"), True
        log.info("live ctl: op=%s done", op)
        return self._signed(seq, True, result=result), True

    def _register(self, encoded: Any) -> dict[str, Any]:
        if self.restarted:
            self._refuse("restarted")
            return {"ok": False, "error": "refused"}
        if self._key is not None:
            self._refuse("already_registered")
            return {"ok": False, "error": "refused"}
        try:
            key = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (binascii.Error, ValueError):
            key = b""
        if len(key) != KEY_BYTES:
            self._refuse("bad_key")
            return {"ok": False, "error": "refused"}
        self._key = key
        log.info("live ctl: worker registered")
        return {"ok": True}

    def _signed(self, seq: int, ok: bool, *, result: Mapping[str, Any] | None = None,
                error: str | None = None) -> dict[str, Any]:
        assert self._key is not None
        body: dict[str, Any] = {"seq": seq, "ok": ok}
        if ok:
            body["result"] = dict(result or {})
        else:
            body["error"] = error
        body["mac"] = _mac(self._key, _REPLY, body)
        return body


# --------------------------------------------------------------------------
# The worker's side
# --------------------------------------------------------------------------


class CtlClient:
    """The worker's end: register once, then send MAC'd commands on one connection.

    One connection for the attempt: reconnecting would mean trusting whatever
    socket is at the path now. The key never leaves this object's heap except
    in the registration line.
    """

    def __init__(self, path: str, *, timeout: float = 10.0) -> None:
        self._path = path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._key: bytes | None = None
        self._seq = 0

    def register(self) -> None:
        if self._key is not None:
            raise CtlError("already registered")
        key = secrets.token_bytes(KEY_BYTES)
        self._connect()
        reply = self._exchange({"register": base64.b64encode(key).decode()})
        if reply.get("ok") is not True:
            self.close()
            raise CtlRefused("the gate refused the registration")
        self._key = key

    def sign(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """The next command, numbered and MAC'd. `call` sends one."""
        if self._key is None:
            raise CtlError("not registered")
        self._seq += 1
        body = {"seq": self._seq, "op": op, "args": dict(args)}
        return {**body, "mac": _mac(self._key, _COMMAND, body)}

    def call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Send `op` and return the gate's result. `CtlRefused` if refused."""
        message = self.sign(op, args)
        assert self._key is not None
        reply = self._exchange(message)
        mac = reply.pop("mac", None)
        if (not isinstance(mac, str) or reply.get("seq") != message["seq"]
                or not hmac.compare_digest(_mac(self._key, _REPLY, reply), mac)):
            self.close()
            raise CtlError("the reply was not signed by the gate")
        if reply.get("ok") is not True:
            raise CtlRefused(f"the gate refused {op}: {reply.get('error')}")
        result = reply.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(self._path)
        except OSError as exc:
            sock.close()
            raise CtlError(f"gate unreachable: {exc.strerror or type(exc).__name__}") from None
        self._sock = sock
        self._file = sock.makefile("rwb")

    def _exchange(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if self._file is None:
            raise CtlError("channel closed")
        try:
            self._file.write(_line(message))
            self._file.flush()
            raw = self._file.readline(MAX_LINE + 1)
        except OSError as exc:
            self.close()
            raise CtlError(f"channel failed: {type(exc).__name__}") from None
        if not raw:
            self.close()
            raise CtlError("channel closed by the gate")
        try:
            reply = json.loads(raw)
        except ValueError:
            self.close()
            raise CtlError("malformed reply") from None
        if not isinstance(reply, dict):
            self.close()
            raise CtlError("malformed reply")
        return reply

    def close(self) -> None:
        for closable in (self._file, self._sock):
            if closable is not None:
                try:
                    closable.close()
                except OSError:
                    pass
        self._file = None
        self._sock = None
