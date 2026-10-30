"""Optional shallow clone of the task's repository.

Shallow and single-branch, because an agent needs the working tree, not the
history, and a full clone of a large monorepo is minutes of wall clock and
gigabytes of an ephemeral disk that the resource class does not have.

The URL comes from the task document, which means it came from an authenticated
caller, which means it is not trusted here. Three things are enforced:

* the scheme is `https` or `ssh` -- never `file://`, never `ext::`, which git
  will happily use to execute an arbitrary command;
* the URL cannot begin with `-`, which would make git parse it as an option
  (`--upload-pack=...` is the classic remote-code-execution shape);
* credentials never appear in argv. A token goes into a 0600 credential file,
  because argv is world-readable through /proc.

The credential file lives in `workspace/private/`, not in the workspace `tmp/`
directory, and it is deleted as soon as the clone finishes. `tmp/` is the
directory the worker hands the agent as `TMPDIR`, so a credential left there is
one `cat $TMPDIR/.git-credentials` away from any prompt injection in the
repository that was just cloned. Same uid, so the mode bits are not a boundary
either way -- what changes is that the agent is never told the path and the file
is gone before the agent starts.

The token itself is the tenant's own, resolved from
`swarm-tenant-<tenant>-git`, never a platform-wide one: a single token that can
clone every tenant's repositories would make one malicious repository in one
tenant a credential compromise for all of them (invariant 9).

And it goes only to the forge it was issued for (#307). The URL is the task
submitter's claim, so `_write_credentials` stores the token only for an https
URL whose host passes `forge.may_receive_forge_token` -- the same rule the
issue fetch uses -- and writes nothing for any other host. A clone of another
host runs without a credential, as a public read: a public repository there
still clones, and a private one fails with an error that says the credential
was withheld and why. Refusing outright would gain nothing (no credential
leaves either way) and would turn away public repositories that work today.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse, urlunparse, quote

from swarm_redaction import redact as redact_text

from .egress import EGRESS_PROBE_CAP_SECONDS, target_of
from .forge import may_receive_forge_token
from .procman import run_child

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._\-/]{1,255}$")
ALLOWED_SCHEMES = ("https", "ssh")

#: A DNS hostname. Checked after parsing rather than trusted from it: the
#: scp-style rewrite below BUILDS a `ssh://` URL out of caller text, so a host
#: that came through that path has never been validated by anything else.
_SAFE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")

#: Characters a repository path may contain. Deliberately narrower than RFC 3986
#: allows: no real repository path needs anything outside this, and the set is
#: what stops the scp rewrite from smuggling a shell fragment into the path
#: component of a URL that then looks structurally valid.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/-]*$")


class GitError(RuntimeError):
    #: The failed try's `clone_phase_timings`, set by the clone functions on
    #: the way out, so `retry_clone` can record a try that failed (#742).
    #: Empty when the failure was not a clone's, or git wrote no trace. Each
    #: instance gets its own dict, so no timing can leak between failures.
    phases: dict[str, Any]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.phases = {}


class GitTransient(GitError):
    """A git network step failed in a way the next try may not meet (#623).

    A connect or read timeout, a DNS failure, a reset connection or a 5xx from
    the forge: github.com did not answer, which says nothing about the
    repository. A `GitError`, so every `except GitError` still catches it;
    `retry_clone` and `_maybe_clone` are the callers that tell it apart.
    `tries` is how many times `retry_clone` ran the clone before giving up.
    """

    def __init__(self, message: str, *, tries: int = 1) -> None:
        super().__init__(message)
        self.tries = tries


class PushRejected(GitError):
    """The forge refused a push because the branch holds commits HEAD does not
    descend from: a non-fast-forward, or a `--force-with-lease` whose expected
    sha was no longer the branch's (#1438).

    A `GitError`, so every `except GitError` still catches it. The publish
    path is the one caller that tells it apart: a retry whose task branch
    holds only its own earlier attempt's worker commits publishes onto it
    (`own_branch_tip`), and anything else fails exactly as before.
    """


# ---------------------------------------------------------------------------
# Transient or permanent: what git's own words say about a failed clone
# ---------------------------------------------------------------------------
#
# MEASURED 2026-10-05 (#623): task_943349914a88 and task_cb020e97d585 failed
# for good on attempt 1 of 3 with "Failed to connect to github.com port 443
# after 134 s". git reports a network failure only as text on stderr, with
# exit 128 for every kind of failure, so the text is all there is to read.
#
# PERMANENT is read FIRST and wins: a missing repository, refused
# authentication, a ref that is not there, a 4xx other than 408/429, a
# certificate the host could not prove. Asking again meets each unchanged,
# and git often adds "the remote end hung up unexpectedly" after one of them,
# which on its own would read as a dropped connection.

_GIT_PERMANENT = re.compile(
    "|".join(
        (
            r"repository not found",
            r"repository '[^']*' not found",
            r"does not appear to be a git repository",
            r"authentication failed",
            r"could not read (?:username|password)",
            r"terminal prompts disabled",
            r"permission denied",
            r"remote branch \S+ not found",
            r"couldn't find remote ref",
            r"not our ref",
            r"returned error: (?!408|429)4\d\d",
            r"\bhttp (?!408|429)4\d\d\b",
            r"ssl certificate problem",
            r"certificate verify failed",
        )
    ),
    re.IGNORECASE,
)

_GIT_TRANSIENT = re.compile(
    "|".join(
        (
            r"failed to connect to \S+ port \d+",
            r"couldn't connect to server",
            r"could not resolve (?:host|proxy)",
            r"temporary failure in name resolution",
            r"name or service not known",
            r"timed out",
            r"connection (?:reset|refused|closed)",
            r"network is unreachable",
            r"no route to host",
            r"returned error: (?:408|429|5\d\d)",
            r"\bhttp (?:408|429|5\d\d)\b",
            r"rpc failed",
            r"early eof",
            r"unexpected disconnect",
            r"remote end hung up unexpectedly",
            r"transfer closed with outstanding read data",
            r"empty reply from server",
            # The reason phrase alone, as a push's sideband carries it
            # ("remote: Internal Server Error", #2833), with no status code.
            r"internal server error",
            r"bad gateway",
            r"service (?:temporarily )?unavailable",
            r"gateway time-?out",
            r"(?:recv|send) failure",
            r"gnutls recv error",
            r"ssl_error_syscall",
        )
    ),
    re.IGNORECASE,
)


def transient_git_failure(text: str) -> bool:
    """True when a failed git network step's message says the forge did not answer.

    Connect, timeout, DNS, a dropped connection and a 5xx are transient; a
    missing repository, refused authentication and a bad ref are not, and
    win over any transport complaint in the same message.
    """
    if not text or _GIT_PERMANENT.search(text):
        return False
    return bool(_GIT_TRANSIENT.search(text))


#: The waits between tries of a clone that failed transiently: 10 s, then
#: 30 s, so THREE tries in all and 40 s asleep at most. Not a third wait of
#: 60 s: that would put the sleeping past `max_in_worker_retry_delay_seconds`
#: (45 s), the platform's bound on an in-worker wait (invariant 4: a worker
#: never sleeps through a long wait). Past these tries the forge has been down
#: for minutes -- each try can itself take a connect timeout of 2+ minutes --
#: and the attempt ends retryably so the scheduler's retry, with the capacity
#: released, does the longer wait.
CLONE_RETRY_WAITS_SECONDS: tuple[float, ...] = (10.0, 30.0)


def retry_clone(
    call: Callable[[], Any],
    *,
    destination: Path,
    max_wait_seconds: float,
    remaining_seconds: Callable[[], float],
    logger: Any,
    sleep: Callable[[float], Any] = time.sleep,
    on_retry: Callable[[], Any] | None = None,
    waits: Sequence[float] = CLONE_RETRY_WAITS_SECONDS,
    record: list[dict[str, Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """`call()`, tried again after each of `waits` while it raises `GitTransient`.

    Anything else, a permanent `GitError` above all, is raised at once. A
    wait is not slept when the sleeping so far plus it would pass
    `max_wait_seconds`, or when it would not end before the step's deadline
    (`remaining_seconds()`): the `GitTransient` goes to the caller, which ends
    the attempt retryably. The one that ends it carries `tries`.

    `destination` is emptied before each retry: a clone that failed part way
    may have left files there, and `git clone` refuses a folder that is not
    empty -- a permanent-looking failure the retry itself would have caused.
    `on_retry` runs after each wait, before the next try (the worker's
    heartbeat).

    EVERY TRY IS APPENDED TO `record`, the failed ones included (#742), in
    order, as `try_record` builds it. Measured 2026-10-06: a clone hung about
    134 s before a retry succeeded, and with only the successful try's
    timings on record the worst case was missing from every clone figure.
    """
    tries = 0
    waited = 0.0
    while True:
        tries += 1
        started = clock()
        try:
            result = call()
        except GitTransient as exc:
            if record is not None:
                record.append(try_record(clock() - started, error=exc))
            exc.tries = tries
            if tries > len(waits):
                raise
            wait = float(waits[tries - 1])
            if waited + wait > max_wait_seconds or wait >= remaining_seconds():
                raise
            logger.warning(
                "the clone could not reach the forge; retrying it in-process",
                attempt=tries,
                attempts=len(waits) + 1,
                wait_seconds=wait,
                reason=str(exc)[-300:],
            )
            sleep(wait)
            waited += wait
            _empty_directory(Path(destination))
            if on_retry is not None:
                on_retry()
        except Exception as exc:
            if record is not None:
                record.append(try_record(clock() - started, error=exc))
            raise
        else:
            if record is not None:
                record.append(try_record(clock() - started, result=result))
            return result


def try_record(
    seconds: float, *, result: Any = None, error: BaseException | None = None
) -> dict[str, Any]:
    """One clone try, as `clone_timed` lists it (#742). Numbers and a class name only.

    `connect_seconds` is git's own connect time for the try
    (`clone_phase_timings`), None when curl never connected -- the stalled
    connect #742 is about lands there, and `seconds` (the try's wall time,
    as this process measured it) then says how long it stalled. `ok` is the
    outcome; `error_class` the exception's class (`GitTransient`, `GitError`)
    on a failed try, None on one that cloned. Never the message: git's words
    can carry a URL.

    `probe_peer`, `git_peer` and `peer_pinned` (#721, P27; `PeerPin.observe`):
    the address the egress probe reached, the address git's curl connected
    to, and whether this try was pinned to the probe's. None, None, False on
    a try that left no phases.
    """
    phases = getattr(error if error is not None else result, "phases", None)
    if not isinstance(phases, dict):
        phases = {}
    return {
        "connect_seconds": phases.get("connect_seconds"),
        "ok": error is None,
        "error_class": type(error).__name__ if error is not None else None,
        "seconds": round(max(0.0, float(seconds)), 3),
        "probe_peer": phases.get("probe_peer"),
        "git_peer": phases.get("git_peer"),
        "peer_pinned": bool(phases.get("peer_pinned")),
    }


@dataclass(frozen=True)
class CloneResult:
    path: Path
    url: str
    ref: str | None
    commit: str | None
    duration_seconds: float
    #: True when the clone succeeded and the repository had no commits, which
    #: is why `commit` is None. Established positively (no object at all), so
    #: a `rev-parse` that failed for any other reason is never read as "empty".
    empty: bool = False
    #: Where the clone's time went, from git's own traces (`clone_phase_timings`):
    #: numbers and git's version, nothing read from the wire. Empty when git
    #: wrote no trace this function could read.
    phases: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Where a clone's time goes (#667, lane OB1)
# ---------------------------------------------------------------------------
#
# MEASURED 2026-10-06 (chunk-1 observer): 229 clones in a day, median 37.8 s,
# p90 70.9 s, clustered at ~38 s and ~71 s at every load level -- one depth-1
# clone of a 33 MB tree, no retry, no NAT errors. A fixed stall, not bandwidth,
# and nothing said which part of the clone it sat in.
#
# git says, if asked. Every clone and fetch runs with two traces, each to its
# own file in the worker's private directory:
#
#   GIT_TRACE2_EVENT   JSON lines with absolute UTC times: each process's
#                      start and exit, the index-pack child that receives the
#                      pack, and the `unpack_trees` region that is the checkout;
#   GIT_TRACE_CURL +   curl's own lines (`Trying`, `Connected to`, the TLS
#   GIT_TRACE_PACKET   handshake, each request sent) and the protocol's packets
#                      (`packfile`, the first `PACK` byte), stamped HH:MM:SS
#                      in the process's local time, which TZ=UTC makes UTC.
#
# THE WIRE TRACE IS NEVER LOGGED, uploaded or kept. It holds the request
# headers -- git redacts `Authorization` by default (GIT_TRACE_REDACT, set
# explicitly here), and a header git does not know to redact is still a
# header -- and the refs the forge advertised. `clone_phase_timings` reads both
# files in this process, keeps only durations, counts and git's version, and
# the files are removed before the clone function returns, on every path.
# GIT_TRACE_CURL_NO_DATA keeps the pack's bytes out of the file: a 33 MB pack
# dumped as text into a memory-backed workspace would cost more than the
# clone it measures.

#: The most of either trace file that is read. A clone's trace is tens of
#: kilobytes; this bounds a repository that advertises a great many refs.
CLONE_TRACE_MAX_BYTES = 8 * 1024 * 1024

_TRACE_EVENTS_NAME = ".git-trace-events"
_TRACE_WIRE_NAME = ".git-trace-wire"

#: `HH:MM:SS.uuuuuu file.c:NNN   message`, git's classic trace line.
_WIRE_LINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{6})\s+\S+:\d+\s+(.*)$")
_CURL_TRYING = re.compile(r"^== Info:\s+Trying\b")
_CURL_CONNECTED = re.compile(r"^== Info:\s+Connected to\b")
#: The address in curl's `Connected to github.com (140.82.112.3) port 443`.
#: Kept only once `ipaddress` accepts it (`_peer_ip`): nothing else of the
#: line leaves the parser.
_CURL_CONNECTED_PEER = re.compile(r"^== Info:\s+Connected to \S+ \(([0-9A-Fa-f:.]{2,45})\)")
_CURL_TLS = re.compile(r"^== Info:\s+(?:SSL connection using|TLSv|ALPN|SSL certificate verify)")
#: One per request: the header block's own line, `=> Send header, N bytes`.
#: Never the header lines themselves, which follow it as `=> Send header: ...`.
_CURL_REQUEST = re.compile(r"^=> Send header, ")
_PKT_PACKFILE = re.compile(r"^packet:\s+\S+<\s+packfile\s*$")
_PKT_PACK = re.compile(r"^packet:\s+\S+<\s+(?:\\1)?PACK\b")
_PACK_RECEIVERS = ("index-pack", "unpack-objects")
_GIT_VERSION = re.compile(r"^[0-9][0-9A-Za-z._-]{0,39}$")


def _trace2_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 3)


def clone_phase_timings(events_text: str, wire_text: str) -> dict[str, Any]:
    """Durations of a clone's phases, from its trace2 events and its wire trace.

    Returns only what both ends of were seen, each in seconds rounded to the
    millisecond, plus counts and git's version:

      total_seconds        the first git process's start to the last one's exit;
      dns_seconds          that start to curl's first `Trying` (name resolution,
                           and the few milliseconds git takes to start curl);
      connect_seconds      the first `Trying` to `Connected to` -- a stall on an
                           unreachable address and a fall back to the next one
                           lands here, and `connect_tries` counts the tries;
      tls_seconds          `Connected to` to the first request sent, when curl
                           reported a TLS handshake in between;
      negotiation_seconds  the first request (or the first process start, with
                           no curl) to the server's `packfile` section: the ref
                           advertisement, ls-refs and the want/have rounds;
      pack_wait_seconds    `packfile` to the first byte of the pack: the forge
                           counting and compressing objects before it sends any;
      pack_seconds         `packfile` to the pack's receiver (index-pack or
                           unpack-objects) exiting: the wait and the transfer;
      checkout_seconds     the top-level `unpack_trees` regions, summed.

    and `git_peer`, the address of curl's first `Connected to` (#721, P27),
    only when it parses as an IP address.

    Never raises, and returns {} for a trace it cannot read. Nothing in the
    result is text from the trace except a version string that matches
    `_GIT_VERSION`: the wire trace holds request headers.
    """
    try:
        return _clone_phase_timings(events_text or "", wire_text or "")
    except Exception:  # noqa: BLE001 -- a timing is never worth a failed clone
        return {}


def _clone_phase_timings(events_text: str, wire_text: str) -> dict[str, Any]:
    starts: list[datetime] = []
    exits: list[datetime] = []
    children: dict[tuple[str, Any], bool] = {}
    pack_end: datetime | None = None
    pack_child_start: datetime | None = None
    checkout = 0.0
    checkout_seen = False
    open_checkout: dict[str, datetime] = {}
    version: str | None = None
    for line in events_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        sid = event.get("sid")
        at = _trace2_time(event.get("time"))
        if not isinstance(sid, str) or at is None:
            continue
        kind = event.get("event")
        top = "/" not in sid
        if kind == "version" and top and version is None:
            exe = event.get("exe")
            if isinstance(exe, str) and _GIT_VERSION.match(exe):
                version = exe
        elif kind == "start" and top:
            starts.append(at)
        elif kind in ("exit", "atexit") and top:
            exits.append(at)
        elif kind == "child_start":
            argv = event.get("argv")
            receives = isinstance(argv, list) and any(
                isinstance(arg, str) and arg in _PACK_RECEIVERS for arg in argv
            )
            children[(sid, event.get("child_id"))] = receives
            if receives and pack_child_start is None:
                pack_child_start = at
        elif kind == "child_exit":
            if children.get((sid, event.get("child_id"))):
                pack_end = at if pack_end is None or at > pack_end else pack_end
        elif top and event.get("category") == "unpack_trees" and event.get("label") == "unpack_trees":
            if kind == "region_enter":
                open_checkout.setdefault(sid, at)
            elif kind == "region_leave" and sid in open_checkout:
                spent = _seconds(open_checkout.pop(sid), at)
                if spent is not None:
                    checkout += spent
                    checkout_seen = True

    start = min(starts) if starts else None
    end = max(exits) if exits else None

    trying: datetime | None = None
    connected: datetime | None = None
    first_request: datetime | None = None
    tls_seen = False
    packfile: datetime | None = None
    pack_data: datetime | None = None
    first_packet: datetime | None = None
    tries = connections = requests = 0
    git_peer: str | None = None
    for line in wire_text.splitlines():
        match = _WIRE_LINE.match(line)
        if match is None or start is None:
            continue
        hour, minute, second, micro, message = match.groups()
        try:
            at = start.replace(
                hour=int(hour), minute=int(minute), second=int(second), microsecond=int(micro)
            )
        except ValueError:
            continue
        # The wire trace carries a time of day only. Anchored on the day the
        # first process started; a line half a day "before" it is past midnight.
        if at < start - timedelta(hours=12):
            at += timedelta(days=1)
        if _CURL_TRYING.match(message):
            tries += 1
            trying = trying or at
        elif _CURL_CONNECTED.match(message):
            connections += 1
            connected = connected or at
            if connections == 1:
                peer = _CURL_CONNECTED_PEER.match(message)
                git_peer = _peer_ip(peer.group(1)) if peer else None
        elif _CURL_TLS.match(message):
            if connected is not None and first_request is None:
                tls_seen = True
        elif _CURL_REQUEST.match(message):
            requests += 1
            first_request = first_request or at
        elif message.startswith("packet:"):
            first_packet = first_packet or at
            if packfile is None and _PKT_PACKFILE.match(message):
                packfile = at
            elif pack_data is None and _PKT_PACK.match(message):
                pack_data = at

    negotiated = packfile or pack_data or pack_child_start
    phases: dict[str, Any] = {
        "total_seconds": _seconds(start, end),
        "dns_seconds": _seconds(start, trying),
        "connect_seconds": _seconds(trying, connected),
        "tls_seconds": _seconds(connected, first_request) if tls_seen else None,
        "negotiation_seconds": _seconds(first_request or first_packet or start, negotiated),
        "pack_wait_seconds": _seconds(packfile, pack_data),
        "pack_seconds": _seconds(negotiated, pack_end),
        "checkout_seconds": round(checkout, 3) if checkout_seen else None,
    }
    out: dict[str, Any] = {key: value for key, value in phases.items() if value is not None}
    if not out:
        return {}
    if tries:
        out["connect_tries"] = tries
    if connections:
        out["connections"] = connections
    if requests:
        out["http_requests"] = requests
    if version:
        out["git_version"] = version
    if git_peer:
        out["git_peer"] = git_peer
    return out


def _peer_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


@dataclass(frozen=True)
class _CloneTrace:
    """The two trace files one clone writes, in the worker's private directory."""

    events: Path
    wire: Path

    @classmethod
    def create(cls, private_dir: Path) -> "_CloneTrace":
        trace = cls(private_dir / _TRACE_EVENTS_NAME, private_dir / _TRACE_WIRE_NAME)
        # Unlinked first: git appends, so a file left by an earlier try (or a
        # link planted at the name) would be read as part of this clone.
        trace.discard()
        return trace

    def env(self, env: dict[str, str]) -> dict[str, str]:
        return {
            **env,
            "GIT_TRACE2_EVENT": str(self.events),
            "GIT_TRACE_CURL": str(self.wire),
            "GIT_TRACE_CURL_NO_DATA": "1",
            "GIT_TRACE_PACKET": str(self.wire),
            "GIT_TRACE_REDACT": "1",
            # The wire trace's clock is local time; this makes it UTC, the
            # trace2 events' clock, so the two can be put on one timeline.
            "TZ": "UTC",
        }

    def _read(self, path: Path) -> str:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return ""
        try:
            with os.fdopen(fd, "rb") as handle:
                return handle.read(CLONE_TRACE_MAX_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def collect(self) -> dict[str, Any]:
        """The timings, and both files removed. Never raises."""
        try:
            return clone_phase_timings(self._read(self.events), self._read(self.wire))
        finally:
            self.discard()

    def discard(self) -> None:
        for path in (self.events, self.wire):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def validate_repository_url(url: str) -> str:
    if not url or url.startswith("-"):
        raise GitError("repository url must not be empty or start with '-'")
    # Any whitespace or control character, not just a newline. The scp rewrite
    # below turns caller text into an `ssh://` URL by string concatenation, so
    # `git@ext::sh -c id` would otherwise become a structurally valid URL whose
    # path is a shell fragment. Nothing in a real repository URL is a space.
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise GitError("repository url must not contain whitespace or control characters")
    if url.startswith("git@") and ":" in url:
        # scp-style syntax; rewrite to ssh:// so it goes through one code path
        host, _, path = url[4:].partition(":")
        url = f"ssh://git@{host}/{path}"
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise GitError(
            f"repository scheme {parsed.scheme!r} is not allowed; "
            f"use one of {', '.join(ALLOWED_SCHEMES)}"
        )
    if not parsed.netloc:
        raise GitError("repository url has no host")
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:                    # a non-numeric port
        raise GitError(f"repository url has an invalid port: {exc}") from exc
    if not hostname or not _SAFE_HOST.match(hostname):
        raise GitError(f"repository host {hostname!r} is not a valid hostname")
    if port is not None and not 1 <= port <= 65535:
        raise GitError(f"repository port {port} is out of range")
    if not _SAFE_PATH.match(parsed.path):
        raise GitError("repository path contains characters that are not allowed in a git path")
    return url


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _SAFE_REF.match(ref):
        raise GitError(f"repository ref {ref!r} contains unsupported characters")
    if ref.startswith("-") or ".." in ref:
        raise GitError(f"repository ref {ref!r} is not a valid git ref")
    return ref


def _credential_host(url: str) -> str | None:
    """The host to store the forge token for, or None when `url` gets none.

    None unless the URL is https, carries no port and no userinfo, and its
    host passes `forge.may_receive_forge_token` (#307). The port and userinfo
    refusals match `forge.parse_repo`: github.com serves git on 443 alone, and
    `https://github.com@evil.example/` is a URL whose host is evil.example.

    The host is returned as the URL spells it, not lower-cased: git matches a
    stored credential's host case-sensitively against the URL it is fetching,
    so an entry for `github.com` would not answer a clone of `GitHub.com`.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or "@" in parsed.netloc:
        return None
    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    if not may_receive_forge_token(parsed.hostname):
        return None
    return parsed.netloc


def _withheld_note(url: str, token: str | None) -> str:
    """The sentence a failed git command carries when it ran without the
    tenant's credential because of the host rule -- so a private repository on
    another host fails with the reason, not with a bare authentication error."""
    if not token or _credential_host(url) is not None:
        return ""
    host = urlparse(url).hostname or "this host"
    return (
        f" (git ran without the tenant's git credential: it is sent only to "
        f"github.com, and {host} is not github.com)"
    )


def _write_credentials(url: str, token: str, private_dir: Path) -> Path | None:
    """Store `https://x-access-token:<token>@host` for git's `store` helper.

    Returns None, and writes nothing, when `url`'s host is not one the token
    may go to (`_credential_host`, #307). Every caller adds the `store` helper
    only for a returned path, so git then has no credential to send anywhere --
    including to a host a redirect sends it to, because git asks its helpers
    again for the redirect target's host and this file names one host only.

    `private_dir` is the worker's own scratch directory, never the one the agent
    is given as TMPDIR, and `shallow_clone` removes the file in a `finally`.
    """
    host = _credential_host(url)
    if host is None:
        return None
    cred_file = private_dir / ".git-credentials"
    entry = urlunparse(
        (
            "https",
            f"x-access-token:{quote(token, safe='')}@{host}",
            "",
            "",
            "",
            "",
        )
    )
    # O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600 from the first byte (#259 review).
    # `private_dir` shares a uid with the agent, so a link planted at this
    # name would have had `write_text` write the token wherever it pointed,
    # and a file created with the default mode is readable until the chmod.
    # Whatever is at the name is unlinked first -- unlink never follows a
    # link -- and O_EXCL then refuses anything that appears in between.
    #
    # Any OSError here is a GitError (#346): a folder the agent planted at the
    # name fails unlink with EISDIR, and every caller lets anything but a
    # GitError escape the publish as a crash. The message names the error
    # class and errno only -- never the path, never what was being written.
    try:
        private_dir.mkdir(parents=True, exist_ok=True)
        try:
            cred_file.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(
            cred_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            data = (entry + "\n").encode()
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except OSError:
            # No caller is handed the path to remove, so a part-written
            # token is removed here.
            cred_file.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or -1, "no errno")
        raise GitError(
            f"could not write the git credential file ({type(exc).__name__}: {code})"
        ) from None
    return cred_file


def _clone_env(
    url: str, token: str | None, private_dir: Path, logger: Any
) -> tuple[dict[str, str], list[str], Path | None]:
    """The environment, the `-c` options and the credential file a clone runs with.

    Shared by `shallow_clone` and `clone_at_commit`, so the pinned clone holds
    the token exactly as the ordinary one does. The caller removes the file
    (`_remove_credentials`) on every path, including a failure.
    """
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private_dir),
        "GIT_TERMINAL_PROMPT": "0",            # never block waiting for a password
        "GIT_ASKPASS": "/bin/true",
        "GIT_CONFIG_NOSYSTEM": "1",
        # /dev/null, not just HOME: `GIT_CONFIG_NOSYSTEM` disables /etc/gitconfig
        # but NOT `~/.gitconfig`, and pointing the global file at /dev/null is
        # what stops an inherited user config from carrying an `insteadOf`, a
        # proxy or a credential helper into a command that holds the token.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
    }
    config_args: list[str] = ["-c", "protocol.version=2", "-c", "advice.detachedHead=false"]
    cred_file: Path | None = None
    if token:
        cred_file = _write_credentials(url, token, private_dir)
        if cred_file is None:
            logger.info(
                "git credential withheld: the host is not the forge the token was issued for",
                host=urlparse(url).hostname,
            )
        else:
            config_args += ["-c", f"credential.helper=store --file={cred_file}"]
    return env, config_args, cred_file


def _remove_credentials(cred_file: Path | None, logger: Any) -> None:
    # The clone is the only thing that ever needs this file. Leaving it on
    # disk for the length of the attempt is what turns a prompt injection in
    # the cloned repository into a stolen token.
    if cred_file is not None:
        try:
            cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))


def _run_git_steps(
    steps: Sequence[Sequence[str]],
    *,
    url: str,
    token: str | None,
    private_dir: Path,
    env: dict[str, str],
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    label: str = "git",
) -> float:
    """Run each argv in turn; raise `GitError` naming the first that fails. Returns seconds."""
    total = 0.0
    for index, argv in enumerate(steps):
        result = run_child(
            list(argv),
            cwd=private_dir,
            env=env,
            stdout_path=logs_dir / f"{label}-{index}.out.log",
            stderr_path=logs_dir / f"{label}-{index}.err.log",
            timeout_seconds=timeout_seconds,
            grace_seconds=10,
            max_stdout_bytes=1 * 1024 * 1024,
            max_stderr_bytes=1 * 1024 * 1024,
            logger=logger,
        )
        total += result.duration_seconds
        if result.timed_out:
            # Transient: the step's own timeout is a forge that answered too
            # slowly, which the next try may not meet (#623).
            raise GitTransient(f"{label} step {index} timed out after {timeout_seconds}s")
        if result.exit_code != 0:
            tail = (logs_dir / f"{label}-{index}.err.log").read_text(errors="replace")[-2000:]
            message = (
                f"{label} step {index} failed with exit {result.exit_code}"
                f"{_withheld_note(url, token)}: {tail.strip()}"
            )
            # Classified on git's own words only, never on the withheld note.
            raise GitTransient(message) if transient_git_failure(tail) else GitError(message)
    return total


#: The most a clone waits for the egress probe (#721 (a)). The probe's own
#: cap: it ends by then anyway, counted from process start, so by the time the
#: clone asks the wait left is shorter. A backstop, not a schedule.
EGRESS_CLONE_WAIT_SECONDS = EGRESS_PROBE_CAP_SECONDS


def await_egress(egress: Any, url: str, logger: Any) -> bool:
    """Wait, bounded, for the egress probe to reach the clone's host (#721 (a)).

    True once the host answered. False -- at once -- with no probe, for a
    host the probe does not cover (a `file://` origin, for one), or when the
    probe ends without an answer or the bound passes: the clone then runs
    exactly as it did before the probe existed. Never raises.
    """
    if egress is None:
        return False
    try:
        target = target_of(url)
        if not egress.covers(target):
            return False
        started = time.monotonic()
        ready = bool(egress.wait(target, EGRESS_CLONE_WAIT_SECONDS))
        logger.info(
            "egress probe: the clone waited for the forge's path to open"
            if ready else "egress probe: no answer; cloning as before",
            host=target[0] if target else None,
            waited_seconds=round(time.monotonic() - started, 3),
        )
        return ready
    except Exception as exc:  # a measurement never stops a clone
        logger.warning("egress probe: wait failed; cloning as before",
                       error=type(exc).__name__)
        return False


#: A connect at least this long had a SYN dropped (#721, P27): the kernel
#: retransmits an unanswered SYN after 1 s, so a connect that took a second or
#: more waited for at least one retransmit. The chunk-3 stalls were 7.1-35.6 s;
#: a healthy connect to GitHub takes tens of milliseconds.
CLONE_CONNECT_STALL_SECONDS = 1.0

#: The schemes whose clone goes through curl, so `http.curloptResolve` applies.
_CURL_SCHEMES = ("https", "http")


def connect_stalled(phases: Any) -> bool:
    """True when a clone try's connect stalled: it took `CLONE_CONNECT_STALL_SECONDS`
    or more, or curl tried and never connected at all."""
    if not isinstance(phases, dict):
        return False
    seconds = phases.get("connect_seconds")
    if isinstance(seconds, (int, float)):
        return float(seconds) >= CLONE_CONNECT_STALL_SECONDS
    return bool(phases.get("connect_tries"))


class PeerPin:
    """Pin a clone's next try to the address the egress probe reached (#721, P27).

    MEASURED 2026-10-06 (chunk-3 observer, P27): the probe connected 11.7-39.6 s
    after process start, and git's own connect still stalled in 5 of 7 clones
    (7.1-35.6 s). Each try therefore records `probe_peer` (the probe's
    `getpeername()`, `EgressProbe.peer`) and `git_peer` (curl's `Connected to`,
    `clone_phase_timings`). WHEN GIT'S CONNECT STALLED AND ITS ADDRESS IS NOT
    THE PROBE'S -- a different address, or none because it never connected --
    every later try of this clone runs with
    `git -c http.curloptResolve=<host>:<port>:<probe ip>`. That is curl's
    CURLOPT_RESOLVE: the URL keeps its host name, so the certificate is still
    verified against it and the Host header is unchanged; only the lookup is
    answered from the probe. A `-c` lives for one command, so nothing outlives
    the clone. NEVER PINNED when the probe never answered (there is no address
    it proved reachable), for an ssh or local clone (not curl), or when the
    stalled connect was to the probe's own address (a pin would change nothing).

    One per clone, shared by its tries (`retry_clone`): a try reads
    `config_args()` before it runs and hands its phases to `observe` after.
    Never raises.
    """

    def __init__(self, egress: Any, url: str | None) -> None:
        self._egress = egress
        self._target: tuple[str, int] | None = None
        self._curl = False
        try:
            self._target = target_of(url)
            self._curl = urlparse(str(url)).scheme in _CURL_SCHEMES
        except Exception:  # noqa: BLE001 -- no target is no pin
            self._target = None
        self.resolve: str | None = None

    @property
    def pinned(self) -> bool:
        return self.resolve is not None

    def probe_peer(self) -> str | None:
        if self._egress is None or self._target is None:
            return None
        try:
            peer = self._egress.peer(self._target)
        except Exception:  # noqa: BLE001 -- a measurement never stops a clone
            return None
        return _peer_ip(peer) if isinstance(peer, str) else None

    def config_args(self) -> list[str]:
        """The `-c` this try runs with: the pin, once one is decided, else none."""
        return ["-c", f"http.curloptResolve={self.resolve}"] if self.resolve else []

    def observe(self, phases: Any) -> dict[str, Any]:
        """Record one try's peers; decide the pin for the tries after it.

        Returns `{probe_peer, git_peer, peer_pinned}` for the try just run,
        `peer_pinned` saying whether IT ran pinned.
        """
        pinned_now = self.pinned
        git_peer = phases.get("git_peer") if isinstance(phases, dict) else None
        probe_peer = self.probe_peer()
        if (
            not pinned_now
            and self._curl
            and self._target is not None
            and probe_peer is not None
            and git_peer != probe_peer
            and connect_stalled(phases)
        ):
            host, port = self._target
            address = f"[{probe_peer}]" if ":" in probe_peer else probe_peer
            self.resolve = f"{host}:{port}:{address}"
        return {"probe_peer": probe_peer, "git_peer": git_peer, "peer_pinned": pinned_now}


#: `git config` arguments that make the clone's `origin` track every branch, as
#: an ordinary clone does. `--single-branch` keeps the TRANSFER to one ref, and
#: it also leaves `remote.origin.fetch` mapping only that ref -- so afterwards a
#: `git fetch origin main` in a clone of `lane/x` updates FETCH_HEAD and never
#: `refs/remotes/origin/main`. An `origin/main` that exists for any other reason
#: (an earlier explicit fetch, a restored checkpoint's `.git`) then stays where
#: it was, and `git merge origin/main` merges that stale main without a word:
#: measured on F571b, PR #575 (#453). The wildcard changes nothing the clone
#: transfers; it makes a later fetch by branch name move that branch's
#: remote-tracking ref, which is what an agent told to fetch main relies on.
TRACK_EVERY_BRANCH = (
    "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
)


def shallow_clone(
    *,
    url: str,
    ref: str | None,
    destination: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    token: str | None = None,
    git_binary: str = "git",
    egress: Any = None,
    peers: PeerPin | None = None,
    history_days: int | None = None,
) -> CloneResult:
    """Clone `url` at `ref` into `destination`, shallow and single-branch.

    `history_days` (an index run, `indexrun.clone_history_days`, G4-06):
    after the one-commit clone, deepen it to the `history_days` before the
    head commit, and one parent past them, so the extractor's hot spots and
    co-change read real history (`deepen_history`). A deepen that fails leaves the clone
    one commit deep, which the extractor reports as having no history.

    `peers` (#721, P27) is the clone's `PeerPin`, shared across its tries:
    this try runs with its pin, if one is decided, and its phases gain
    `probe_peer`, `git_peer` and `peer_pinned`.

    `private_dir` is the worker's own scratch directory (`workspace/private/`).
    git's HOME and the credential file both live there rather than in the
    directory the agent is handed as TMPDIR, and the credential file is removed
    before this function returns, on every path including a failure.
    """
    url = validate_repository_url(url)
    ref = validate_ref(ref)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    # Before the credential file exists, so a wait does not lengthen its life.
    await_egress(egress, url, logger)

    env, config_args, cred_file = _clone_env(url, token, private_dir, logger)
    if peers is not None:
        config_args += peers.config_args()

    is_sha = bool(ref and _SHA_RE.match(ref))
    if is_sha:
        # A shallow clone cannot target a bare commit, so fetch it explicitly.
        steps = [
            [git_binary, *config_args, "init", "--quiet", str(destination)],
            [git_binary, *config_args, "-C", str(destination), "remote", "add", "origin", url],
            [
                git_binary, *config_args, "-C", str(destination),
                "fetch", "--depth", "1", "--no-tags", "origin", ref,
            ],
            [git_binary, *config_args, "-C", str(destination), "checkout", "--quiet", "FETCH_HEAD"],
        ]
    else:
        clone = [git_binary, *config_args, "clone", "--depth", "1", "--no-tags", "--single-branch"]
        if ref:
            clone += ["--branch", ref]
        clone += ["--", url, str(destination)]
        steps = [
            clone,
            [git_binary, *config_args, "-C", str(destination), *TRACK_EVERY_BRANCH],
        ]

    # Every step is traced into the same two files, so a by-sha clone's
    # fetch and checkout land on one timeline (`clone_phase_timings`).
    trace = _CloneTrace.create(private_dir)
    phases: dict[str, Any] = {}
    failed: GitError | None = None
    try:
        total = _run_git_steps(
            steps, url=url, token=token, private_dir=private_dir, env=trace.env(env),
            logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
        )
        if history_days:
            # Inside the try: the credential file is still there, and the
            # `finally` below removes it after the deepen as after the clone.
            total += deepen_history(
                destination, history_days, git_binary=git_binary, config_args=config_args,
                url=url, token=token, private_dir=private_dir, env=trace.env(env),
                logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
            )
    except GitError as exc:
        failed = exc
        raise
    finally:
        _remove_credentials(cred_file, logger)
        phases = trace.collect()
        if peers is not None:
            phases.update(peers.observe(phases))
        if failed is not None:
            # A failed try is timed too (#742): `retry_clone` records it.
            failed.phases = phases

    commit = _read_head(destination, private_dir, logs_dir, logger, git_binary)
    empty = commit is None and _holds_no_objects(
        destination, private_dir, logs_dir, logger, git_binary
    )
    logger.info(
        "repository cloned", url=url, ref=ref, commit=commit, empty=empty,
        seconds=round(total, 2), phases=phases,
    )
    return CloneResult(
        path=destination, url=url, ref=ref, commit=commit, duration_seconds=total, empty=empty,
        phases=phases,
    )


def history_fetch_argv(
    git_binary: str, config_args: Sequence[str], destination: Path, head_sha: str,
    head_time: int, days: int,
) -> list[str]:
    """`git fetch --shallow-since=<head - days> origin <head>`.

    Bounded by date, not by count: the window the extractor reads
    (`repo_index_extract.HISTORY_DAYS`, anchored at the head commit), and
    nothing older. The cost is the trees and blobs of the window's commits.
    """
    since = datetime.fromtimestamp(head_time - days * 86_400, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return [git_binary, *config_args, "-C", str(destination), "fetch", "--quiet", "--no-tags",
            f"--shallow-since={since}", "origin", head_sha]


def history_parent_argv(
    git_binary: str, config_args: Sequence[str], destination: Path, head_sha: str,
) -> list[str]:
    """`git fetch --deepen=1 origin <head>`: one commit past the window.

    `--shallow-since` stops AT the oldest commit inside the window, without
    its parent, and git shows a commit without its parent as adding every
    file it holds. One more generation makes every commit in the window a
    real diff; the extractor skips the boundary commit, now outside it.
    """
    return [git_binary, *config_args, "-C", str(destination), "fetch", "--quiet", "--no-tags",
            "--deepen=1", "origin", head_sha]


def deepen_history(
    destination: Path,
    days: int,
    *,
    git_binary: str,
    config_args: Sequence[str],
    url: str,
    token: str | None,
    private_dir: Path,
    env: dict[str, str],
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
) -> float:
    """Deepen a one-commit clone to `days` of history before its head. Seconds taken.

    Never raises: a forge that refuses or times out the deepen leaves the
    clone with what it fetched, and the warning says so; the extractor
    reads the history it finds (`window_covered`, `available`). The
    checkout is not touched: a fetch moves no branch and no working file.
    """
    head = run_child(
        [git_binary, "-C", str(destination), "show", "-s", "--format=%H %ct", "HEAD"],
        cwd=private_dir,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private_dir),
             "GIT_CONFIG_NOSYSTEM": "1"},
        stdout_path=logs_dir / "git-history-head.out.log",
        stderr_path=logs_dir / "git-history-head.err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    text = ""
    if head.exit_code == 0:
        text = (logs_dir / "git-history-head.out.log").read_text(errors="replace").strip()
    parts = text.split()
    if len(parts) != 2 or not _FULL_SHA_RE.match(parts[0]) or not parts[1].isdigit():
        logger.warning("clone history: the head commit could not be read; the clone stays "
                       "one commit deep")
        return head.duration_seconds
    argv = history_fetch_argv(git_binary, config_args, destination, parts[0], int(parts[1]), days)
    try:
        seconds = _run_git_steps(
            [argv], url=url, token=token, private_dir=private_dir, env=env, logs_dir=logs_dir,
            timeout_seconds=timeout_seconds, logger=logger, label="git-history",
        )
        # Still shallow: the history goes back past the window. Not shallow:
        # the window holds the whole history, root commit included.
        if (destination / ".git" / "shallow").exists():
            seconds += _run_git_steps(
                [history_parent_argv(git_binary, config_args, destination, parts[0])],
                url=url, token=token, private_dir=private_dir, env=env, logs_dir=logs_dir,
                timeout_seconds=timeout_seconds, logger=logger, label="git-history-parent",
            )
    except GitError as exc:  # GitTransient included
        # The class only: git's own words are in logs/git-history*-0.err.log.
        logger.warning("clone history: the deepen failed; the extractor reports the "
                       "history the clone holds", days=days, error=type(exc).__name__)
        return head.duration_seconds
    logger.info("clone history deepened", days=days, seconds=round(seconds, 2))
    return head.duration_seconds + seconds


#: How many commits back from the step's base `fetch_base_history` deepens a
#: depth-1 clone so `graphstage` can prove the index commit an ancestor
#: (lane GRAPH-ANCESTRY, part of #2540). The index is promoted from main's
#: head and a lane clones main's head a little later, so the gap is the
#: commits merged between the two: main takes about 70 a day here, and 200
#: covers a promoted index up to roughly three days old. Deeper buys proofs
#: for indexes that stale only at the price of every step's start-up.
ANCESTRY_FETCH_DEPTH = 200
#: The fetch's whole wall-clock limit: a step's start-up never waits longer
#: for a proof than this, and a fetch cut off is a reason, not a failure.
ANCESTRY_FETCH_TIMEOUT_SECONDS = 20.0
#: How long a timed-out fetch has, after SIGTERM, to remove its own
#: `.git/shallow.lock` before SIGKILL: git exits on SIGTERM at once.
ANCESTRY_FETCH_GRACE_SECONDS = 5.0


class _QuietLog:
    """The logger `fetch_base_history` hands `_clone_env`: its caller
    records the outcome as a reason, so nothing here logs."""

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    error = warning = info


def fetch_base_history(
    checkout: Path,
    *,
    url: str,
    base_sha: str,
    private_dir: Path,
    token: str | None = None,
    depth: int = ANCESTRY_FETCH_DEPTH,
    timeout_seconds: float = ANCESTRY_FETCH_TIMEOUT_SECONDS,
    git_binary: str = "git",
) -> str | None:
    """Deepen a shallow `checkout` to `depth` commits behind `base_sha`. None
    when the fetch landed, else a short reason (never git's own words, which
    may name the URL).

    `git fetch --depth=<n> <url> <base>`: base's own first `n` generations and
    nothing else -- no other branch, no tags, no FETCH_HEAD, no ref moved, no
    working file touched. `url` is the caller's, already validated
    (`validate_repository_url`); this does not read the clone's `origin`,
    which a restored checkpoint's `.git` could have rewritten.

    `token`, when given, is held exactly as a clone holds it (`_clone_env`):
    a 0600 file scoped to the forge's host, removed before this returns. The
    helper list is reset first (`credential.helper=`), so a helper planted in
    the clone's own `.git/config` is never handed it, and `http.proxy=`
    turns off any proxy that config names (lane GRAPH-TOKEN-2563).

    Unlike a clone, this runs git INSIDE a checkout whose `.git/config` a
    restored checkpoint carries, and some of that config no `-c` can undo:
    `url.<base>.insteadOf` is multi-valued (a `-c` adds a rule, never removes
    one, and an empty one rewrites EVERY URL to its base), and an
    `http.<url>.*` or `credential.<url>.*` entry outranks the unscoped `-c`
    by URL specificity. So the clone's config is read first
    (`_token_unsafe_config`) and, when it sets anything under `url.`,
    `http.` or `credential.`, the token is withheld: the fetch goes on
    anonymously, which a public repository still answers, and a failure
    says why the token was not used.
    """
    if not _FULL_SHA_RE.match(base_sha or ""):
        return "the base is not a full commit sha"
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    withheld: str | None = None
    if token:
        scan_env, _, _ = _clone_env(url, None, private_dir, _QuietLog())
        withheld = _token_unsafe_config(checkout, scan_env, git_binary)
        if withheld is not None:
            token = None
    try:
        env, config_args, cred_file = _clone_env(url, token, private_dir, _QuietLog())
    except GitError:
        # `_write_credentials` could not hold the token safely; no file is left.
        return "the forge token could not be held for the history fetch"
    if cred_file is not None:
        config_args = ["-c", "credential.helper=", *config_args]
    note = f" (forge token withheld: {withheld})" if withheld else ""
    argv = [git_binary, *config_args, "-c", "http.proxy=",
            "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
            "-C", str(checkout), "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--no-recurse-submodules", f"--depth={int(depth)}", "--", url, base_sha]
    # Through `run_child`, never `subprocess.run(timeout=)`: that SIGKILLs git,
    # and a `--depth` fetch holds `.git/shallow.lock` while the pack arrives,
    # which SIGKILL skips cleaning up -- every deepen the agent tried after a
    # timed-out fetch then failed on "shallow.lock: File exists". SIGTERM to
    # the group first lets git remove its own locks.
    try:
        done = run_child(argv, cwd=private_dir, env=env,
                         stdout_path=private_dir / "git-ancestry.out.log",
                         stderr_path=private_dir / "git-ancestry.err.log",
                         timeout_seconds=timeout_seconds,
                         grace_seconds=ANCESTRY_FETCH_GRACE_SECONDS,
                         max_stdout_bytes=64 * 1024, max_stderr_bytes=64 * 1024,
                         logger=_QuietLog())
    except OSError as exc:
        return f"the history fetch did not start ({type(exc).__name__}){note}"
    finally:
        _remove_credentials(cred_file, _QuietLog())
    if done.timed_out:
        if done.killed:
            # Git ignored SIGTERM, so its lock outlived it. The fetch's whole
            # process group is reaped by now: nothing else holds that lock.
            (Path(checkout) / ".git" / "shallow.lock").unlink(missing_ok=True)
        return f"the history fetch timed out after {timeout_seconds:g} s{note}"
    if done.exit_code != 0:
        return f"the history fetch failed (git exit {done.exit_code}){note}"
    return None


#: Config sections in a checkout's own `.git/config` that could send a
#: credential somewhere other than the repository's forge, or hand it to a
#: program, and that a `-c` on the command line cannot reliably undo:
#: `url.*.insteadOf` rewrites, `http.<url>.proxy` (and every other
#: `http.` key), and `credential.<url>.helper`. A `remote.` other than
#: `origin` is refused too (`_token_unsafe_config`).
_TOKEN_UNSAFE_SECTIONS = ("url.", "http.", "credential.")
#: The most of `git config --list --name-only` that is read; a list that
#: reaches it is refused rather than read in part.
_CONFIG_SCAN_MAX_BYTES = 256 * 1024


def _token_unsafe_config(checkout: Path, env: dict[str, str], git_binary: str) -> str | None:
    """Why the checkout's own git config makes holding a token unsafe, or None.

    Reads `git config --list --name-only --includes` with the global and
    system files already off (`_clone_env`), so what is listed is the
    clone's own config and whatever it includes. Only key NAMES are read --
    never a value, which may itself be a URL or a secret -- and the reason
    names the section, never the key. A config git cannot read is unsafe."""
    out = Path(env["HOME"]) / "git-ancestry-config.out.log"
    try:
        done = run_child([git_binary, "-C", str(checkout), "config", "--list", "--name-only",
                          "--includes"],
                         cwd=Path(env["HOME"]), env=env, stdout_path=out,
                         stderr_path=Path(env["HOME"]) / "git-ancestry-config.err.log",
                         timeout_seconds=10.0, grace_seconds=2.0,
                         max_stdout_bytes=_CONFIG_SCAN_MAX_BYTES, max_stderr_bytes=16 * 1024,
                         logger=_QuietLog())
        names = out.read_bytes() if done.exit_code == 0 and not done.timed_out else None
        out.unlink(missing_ok=True)
    except OSError as exc:
        return f"the clone's git config could not be read ({type(exc).__name__})"
    if names is not None and len(names) >= _CONFIG_SCAN_MAX_BYTES:
        # Cut off at the cap: a name past it would go unread.
        return "the clone's git config is too large to read"
    if names is None:
        return "the clone's git config could not be read"
    sections: set[str] = set()
    for name in names.decode("utf-8", "replace").splitlines():
        lowered = name.lower()
        sections.update(s for s in _TOKEN_UNSAFE_SECTIONS if lowered.startswith(s))
        # `git fetch <url>` reads a remote NAMED `<url>` when one is
        # configured, and its `url`/`proxy` would then decide where the
        # fetch goes. The worker's clone configures `origin` alone.
        if lowered.startswith("remote.") and not lowered.startswith("remote.origin."):
            sections.add("remote.")
    if sections:
        return "the clone's git config sets " + ", ".join(f"{s}*" for s in sorted(sections))
    return None


def clone_at_commit(
    *,
    url: str,
    branch: str | None,
    commit: str,
    destination: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    token: str | None = None,
    git_binary: str = "git",
    egress: Any = None,
    peers: PeerPin | None = None,
) -> CloneResult:
    """Check out exactly `commit` of `url` into `destination` (the workflow base pin).

    A downstream workflow step starts from the commit its upstream steps
    started from, not from wherever `branch` has moved since (docs/workflows.md,
    "The base pin"). First a shallow fetch of the sha itself, which GitHub
    serves; if the server refuses a fetch by sha, a full fetch of `branch`
    followed by a checkout of the sha, which works whenever the commit is
    still in that branch's history.

    Raises `GitError` when neither lands the commit, after emptying
    `destination` again, so the caller can clone the branch tip into it as
    it always did. `commit` must be a full 40-character sha: an abbreviation
    is not a pin.
    """
    url = validate_repository_url(url)
    branch = validate_ref(branch)
    if not _FULL_SHA_RE.match(commit or ""):
        raise GitError(f"refusing to pin to {str(commit)[:60]!r}: not a full commit sha")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    # Before the credential file exists, so a wait does not lengthen its life.
    await_egress(egress, url, logger)

    env, config_args, cred_file = _clone_env(url, token, private_dir, logger)
    if peers is not None:
        # The address pin (#721, P27, `PeerPin`), not the base pin.
        config_args += peers.config_args()
    g = [git_binary, *config_args, "-C", str(destination)]
    setup = [
        [git_binary, *config_args, "init", "--quiet", str(destination)],
        [*g, "remote", "add", "origin", url],
    ]
    by_sha = [
        [*g, "fetch", "--depth", "1", "--no-tags", "origin", commit],
        [*g, "checkout", "--quiet", commit],
    ]
    total = 0.0
    trace = _CloneTrace.create(private_dir)
    env = trace.env(env)
    phases: dict[str, Any] = {}
    failed: GitError | None = None
    try:
        total += _run_git_steps(
            setup, url=url, token=token, private_dir=private_dir, env=env,
            logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
            label="git-pin",
        )
        try:
            total += _run_git_steps(
                by_sha, url=url, token=token, private_dir=private_dir, env=env,
                logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
                label="git-pin-sha",
            )
        except GitTransient:
            # The forge did not answer, which is not a refusal of the sha: a
            # full fetch of the branch would meet the same outage. Raised, so
            # `retry_clone` asks again (#623).
            raise
        except GitError as exc:
            logger.info(
                "the forge refused a fetch by sha; fetching the branch's history instead",
                branch=branch, commit=commit, error=str(exc)[:300],
            )
            # Not shallow: the pinned commit is somewhere in the branch's past,
            # and a depth would have to guess how far.
            by_branch = [
                [*g, "fetch", "--no-tags", "origin", f"refs/heads/{branch}" if branch else "HEAD"],
                [*g, "checkout", "--quiet", commit],
            ]
            total += _run_git_steps(
                by_branch, url=url, token=token, private_dir=private_dir, env=env,
                logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
                label="git-pin-branch",
            )
    except GitError as exc:
        failed = exc
        _empty_directory(destination)
        raise
    finally:
        _remove_credentials(cred_file, logger)
        phases = trace.collect()
        if peers is not None:
            phases.update(peers.observe(phases))
        if failed is not None:
            # A failed try is timed too (#742): `retry_clone` records it.
            failed.phases = phases

    head = _read_head(destination, private_dir, logs_dir, logger, git_binary)
    if head != commit:
        _empty_directory(destination)
        raise GitError(f"the pinned checkout landed on {head!r}, not {commit}")
    logger.info(
        "repository cloned at the pinned commit", url=url, ref=branch, commit=head,
        seconds=round(total, 2), phases=phases,
    )
    return CloneResult(
        path=destination, url=url, ref=branch, commit=head, duration_seconds=total,
        phases=phases,
    )


def _empty_directory(path: Path) -> None:
    """Remove everything inside `path`, keeping `path`: a clone needs it empty."""
    for child in list(path.iterdir()) if path.is_dir() else []:
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _read_head(
    destination: Path, tmp: Path, logs_dir: Path, logger: Any, git_binary: str
) -> str | None:
    result = run_child(
        [git_binary, "-C", str(destination), "rev-parse", "HEAD"],
        cwd=tmp,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1"},
        stdout_path=logs_dir / "git-head.out.log",
        stderr_path=logs_dir / "git-head.err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    if result.exit_code != 0:
        return None
    text = (logs_dir / "git-head.out.log").read_text(errors="replace").strip()
    return text or None


def _holds_no_objects(
    destination: Path, tmp: Path, logs_dir: Path, logger: Any, git_binary: str
) -> bool:
    """True when the fresh clone holds no object at all: an empty repository.

    Asked only after `rev-parse HEAD` failed, and it must answer "yes" before
    the clone counts as empty. Reading a failed `rev-parse` as "empty" would
    let a transient failure turn a real repository into one whose whole tree
    the publish folds into a parentless commit. Objects rather than refs,
    because a clone of a pinned sha is a detached HEAD with no ref either
    (measured: `for-each-ref` is empty there too), while any clone that
    fetched something has a loose object or a pack.
    """
    result = run_child(
        [git_binary, "-C", str(destination), "count-objects", "-v"],
        cwd=tmp,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1"},
        stdout_path=logs_dir / "git-objects.out.log",
        stderr_path=logs_dir / "git-objects.err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    if result.exit_code != 0:
        return False
    counts: dict[str, str] = {}
    for line in (logs_dir / "git-objects.out.log").read_text(errors="replace").splitlines():
        key, _, value = line.partition(":")
        counts[key.strip()] = value.strip()
    return counts.get("count") == "0" and counts.get("in-pack") == "0"


# ---------------------------------------------------------------------------
# Harvest -- getting the agent's work back OUT of the workspace
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. Until this was written the loop was open at the far end. A
# worker cloned a repository into `work/repo`, the agent edited and committed
# into it, `checkpoint.py` archived the whole of `work/` to GCS every two
# minutes -- and then nothing ever read that archive except a retry of the same
# attempt. The commits were durable and unreachable at the same time. Asked
# "how does an agent's work get merged", the honest answer was "it does not".
#
# Two things are separated here on purpose, because they have very different
# blast radii:
#
#   HARVEST  is read-only. It asks git what changed and writes a patch into
#            `artifacts/`. It needs no credential at all and runs on EVERY exit
#            path, including a park and a crash.
#
#   PUBLISH  pushes a branch and opens a pull request. It needs a token with
#            write permission, runs only when the agent exited on its own, and
#            is refused outright unless the forge itself confirms the push bit.
#
# The split is what lets the capability ship complete while the permission
# stays a separate decision: with today's read-only token the harvest branch is
# the live path, not a placeholder for one.

#: Fields inside a `git log` record, and records inside the stream. Chosen
#: because neither byte can occur in a commit subject, an author name or a
#: path -- a newline can occur in all three, so line-splitting the output is
#: the bug this avoids.
_FS = "\x1f"
_RS = "\x1e"


@dataclass(frozen=True)
class CommitSummary:
    sha: str
    subject: str
    author: str
    committed_at: str
    files_changed: int
    insertions: int
    deletions: int
    binary_files: int


#: The most per-file rows `summarize_work` keeps in `result_summary.git.files`.
#: 500 because a row is about 100 bytes of JSON, so a full list stays near 50
#: KB -- well inside a Firestore document's 1 MiB, which also carries the
#: commits, the dirty list and everything else in the result summary -- while
#: covering every change a reviewer reads file by file: a change past 500
#: files is a vendored tree or a generated one, and the viewer windows the
#: patch itself for those. `files_truncated` says when the cap was hit, so a
#: reader never takes the first 500 for the whole change.
MAX_FILES_LISTED = 500

#: `git diff --name-status` letters, folded to the four the viewer draws.
#: `T` (a type change, file <-> symlink) is a modification of the path; `C`
#: never appears, because copy detection is not asked for. Anything else git
#: may add later reads as `M` rather than as a letter no reader knows.
_FILE_STATUS = {"A": "A", "M": "M", "D": "D", "R": "R", "T": "M"}


@dataclass(frozen=True)
class FileChange:
    """One file of the harvested diff: repository paths and counts, never content."""

    path: str
    #: The path before a rename, else None.
    old_path: str | None
    #: One of A, M, D, R.
    status: str
    #: Both None on a binary file: git prints "-" for its counts, and a zero
    #: would claim the file changed no lines rather than that lines do not apply.
    insertions: int | None
    deletions: int | None
    binary: bool

    def as_record(self, scrub: Any) -> dict[str, Any]:
        """The `result_summary.git.files` row, both paths passed through `scrub`
        -- a path is agent-chosen text bound for Firestore, like the dirty list."""
        return {
            "path": scrub(self.path),
            "old_path": scrub(self.old_path) if self.old_path is not None else None,
            "status": self.status,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "binary": self.binary,
        }


@dataclass(frozen=True)
class WorkSummary:
    """What the agent did to the repository, as facts rather than a guess."""

    base: str | None
    head: str | None
    commits: tuple[CommitSummary, ...]
    #: Paths git reports as changed-but-uncommitted, from `status --porcelain`.
    #: The common case by far: most agents edit and never commit.
    dirty: tuple[str, ...]
    dirty_truncated: bool
    patch_name: str | None
    patch_bytes: int
    #: True when a patch was produced but discarded for exceeding the cap. A
    #: TRUNCATED patch is never written: it would apply cleanly and silently
    #: drop the rest of the change, which is worse than having no patch.
    patch_omitted: bool
    insertions: int
    deletions: int
    #: One row per file of the patch's diff, at most `MAX_FILES_LISTED`. None
    #: when there was no base to diff against, which is "not measured", not
    #: "no files"; an empty tuple is a measured empty diff.
    files: tuple[FileChange, ...] | None = None
    files_truncated: bool = False
    #: The paths this step's OWN work changed (#2337): every non-merge commit
    #: on HEAD's first-parent line since `base`, each against its first
    #: parent, and the uncommitted work. A `git merge origin/main` (or of
    #: another lane's branch) puts that branch's files in `files` and in the
    #: patch, but none of them here. At most `MAX_FILES_LISTED`; None when
    #: not measured (no base, or git could not list it), never a guess.
    own_files: tuple[str, ...] | None = None
    own_files_truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.commits and not self.dirty


def _git_env(private_dir: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private_dir),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "GIT_CONFIG_NOSYSTEM": "1",
        # See `shallow_clone`: NOSYSTEM leaves `~/.gitconfig` in play, so the
        # global file is pinned to /dev/null on every git the worker runs.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
        # Every command that reaches this env runs in the clone AFTER the
        # agent has had the run of it, so its refs -- including `refs/replace/*`
        # -- are the agent's to write. `git replace` substitutes an object
        # wherever git reads one through the object database: `cat-file`,
        # `diff`, `show`, `log` all follow it (measured with git 2.40.1: a
        # `git replace <blob> <clean-blob>` made `git diff` print the clean
        # content for a blob that still held the real one). Replacement is
        # read-side only -- it never touches the tree a commit points at, so
        # `commit-tree`, pack-objects and the eventual push always send the
        # REAL object. Without this, the per-commit secret scan
        # (`lifecycle._first_leaking_commit`) could be shown a clean
        # replacement for a blob that holds a registered secret, pass the
        # commit as non-leaking, and then push the real, secret-holding blob
        # unfolded (#259 review, M1). `GIT_NO_REPLACE_OBJECTS=1` makes every
        # git command below read the real object instead.
        "GIT_NO_REPLACE_OBJECTS": "1",
        # The same for `.git/info/grafts`, which the agent can also write:
        # `rev-list --first-parent` and `merge-base` follow a graft even with
        # replacement off, so a grafted parent made the replay's list skip a
        # commit whose tree then shipped inside the next kept one (#259
        # re-review). An empty graft file means no grafts. The replay also
        # checks the chain itself against each commit's recorded parent
        # (`lifecycle._first_leaking_commit`), which covers `.git/shallow`.
        "GIT_GRAFT_FILE": "/dev/null",
        # And for a commit-graph (#259, third review): git takes a commit's
        # ROOT TREE from `objects/info/commit-graph` when one is present, so
        # a graph whose entry for a commit names a different tree made
        # `git diff` compare trees the push never sends. The scans run in a
        # repository the worker made (`prepare_publish_repo`), which has no
        # graph the agent wrote; this switches graphs off for every worker
        # git as well, so none is read even where one exists. Command-scope
        # configuration outranks any repository's own `core.commitGraph`.
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "core.commitGraph",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "fetch.writeCommitGraph",
        "GIT_CONFIG_VALUE_1": "false",
        # `GIT_GRAFT_FILE=/dev/null` above makes git open a graft file, which
        # succeeds, and print its eight-line graft-deprecation advice on every
        # command (#808). The advice key only decides whether that message
        # prints: grafts stay off. Command-scope configuration outranks the
        # clone's `.git/config`, so the agent cannot turn the hint back on.
        "GIT_CONFIG_KEY_2": "advice.graftFileDeprecated",
        "GIT_CONFIG_VALUE_2": "false",
    }


#: Passed to every git invocation below. `core.hooksPath` is the one that
#: matters: hooks are not transferred by a clone, but the AGENT can write
#: `.git/hooks/pre-commit` in the workspace, and a harvest that ran it would
#: hand arbitrary code a worker-side execution point after the sandbox was
#: supposed to be finished with. Both belts are worn -- `--no-verify` on the
#: commands that accept it, and a hooks path that cannot contain anything.
#:
#: The same reasoning covers two programs that are hooks in all but name, both
#: read from the clone's `.git/config`, which the agent can write:
#:
#: * `core.fsmonitor` -- set to a path, git documents it as the fsmonitor
#:   hook command and runs it on every index refresh: measured at ten runs
#:   across the `add`, `diff --cached`, `commit`, `reset --soft` and `status`
#:   a publish makes, in a scratch repository with git 2.40.1.
#: * `commit.gpgSign` makes every worker commit run `gpg.program`, and what
#:   that program prints becomes a `gpgsig` header on the worker's own commit
#:   -- text the agent chose, in an object the worker pushes.
#:
#: NOT CLOSED HERE, and not closable by listing keys: a filter driver the
#: agent defines (`filter.<name>.clean`) and names in `.gitattributes` runs on
#: `git add`; diff drivers and `log.showSignature` are the same class. That
#: class is closed by WHERE the worker's git runs at publish: every command
#: from the reap onwards -- the commit of uncommitted work included
#: (`mirror_worktree`, #259 M1) -- runs in the repository `prepare_publish_repo`
#: builds, whose configuration the worker wrote. The harvest (`summarize_work`)
#: runs there too, after the reap (#259 item 3), and HEAD is read from the
#: clone's files as data (`read_head_as_data`, #259 B1). The one git process
#: left in the clone is the fetch's `upload-pack`, hardened by
#: `_UPLOAD_PACK_ENV` and `_UPLOAD_PACK_CONFIG`.
_NO_HOOKS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
]


#: Prepended to every git invocation that carries the tenant token -- the push,
#: and the integrator's fetch-and-merge. It is meaningful ONLY because those
#: commands run in a worker-owned publish repository (`prepare_publish_repo`),
#: never in the repository the agent worked in.
#:
#: The reason the repository has to be worker-owned rather than the clone with
#: overrides bolted on: `url.<host>.insteadOf` / `pushInsteadOf` rewrites the
#: destination of a push -- including a URL passed explicitly on the command
#: line -- and there is NO `-c` that disables URL rewriting, nor a way to
#: enumerate every `url.*` key a repository config (or a config it `include`s)
#: might carry. So the credential's destination is safe only because the
#: repository the worker built has no such key in it. This is the filter-driver
#: class `_NO_HOOKS` names as "not closable by listing keys".
#:
#: The overrides below are belt to that suspenders: each neutralises a setting a
#: `-c` CAN override. The worker's own credential helper is appended by the
#: caller AFTER the empty `credential.helper` here, which discards any
#: accumulated helper list before the worker's is added (git treats an empty
#: value as a reset).
_TOKEN_SAFE = [
    *_NO_HOOKS,                                 # no repository hook, fsmonitor or gpg program runs
    "-c", "protocol.version=2",
    "-c", "credential.helper=",                 # reset: no inherited helper survives
    "-c", "http.sslVerify=true",                # only over verified TLS
    "-c", "http.proxy=",                        # no proxy may sit in front of the forge
    "-c", "http.extraHeader=",                  # no injected header rides with the request
    # No submodule recursion on a token-bearing fetch. git's default for
    # `fetch.recurseSubmodules` is `on-demand`, which fetches submodules named in
    # a contributor branch's `.gitmodules` -- an untrusted file -- and would
    # carry the credential to whatever hosts it lists. The integrator's fetch
    # wants the branch, never its submodules; `submodule.recurse=false` covers
    # the merge and any command that would otherwise recurse.
    "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false",
]


def _worker_identity(name: str, email: str) -> list[str]:
    """The `-c` arguments that make the worker the author AND committer.

    `user.*` alone is not enough, and was all this module passed. git reads a
    commit's author from `author.name`/`author.email` BEFORE `user.*`, and its
    committer from `committer.*` before `user.*`; `-c user.name` overrides
    `user.name` and nothing else. The clone's `.git/config` is the agent's to
    write, so an agent that set `author.name=Claude` there made every commit
    the worker created -- its auto-commit, its fold, the integrator's merges --
    Claude's. A command-line `-c` of the SAME key outranks the repository's
    value, so all six are set. Measured with git 2.40.1.

    One case this does not reach: a `git commit` that concludes a cherry-pick
    takes its author from `CHERRY_PICK_HEAD` regardless of configuration.
    `--author` overrides that, so every worker `git commit` passes
    `_author_option` as well.
    """
    return [
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "-c", f"author.name={name}",
        "-c", f"author.email={email}",
        "-c", f"committer.name={name}",
        "-c", f"committer.email={email}",
    ]


def _author_option(name: str, email: str) -> str:
    """`--author` in the explicit `Name <email>` form. Without the angle
    brackets git treats the value as a pattern and searches history for an
    author to copy, which is the opposite of what is wanted here."""
    return f"--author={name} <{email}>"


def _git_text(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str]:
    """Run a git command and return its exit code with its stdout as text.

    A thin wrapper over `_git_text_full` for the many callers that only need
    the text, never whether the capture was truncated.
    """
    code, text, _truncated = _git_text_full(
        argv,
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug=slug,
        timeout_seconds=timeout_seconds,
        logger=logger,
        max_bytes=max_bytes,
    )
    return code, text


def _git_text_full(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str, bool]:
    """Run a git command; return its exit code, stdout as text, and whether the
    capture was truncated.

    Output goes to a file rather than a pipe because `run_child` is the only
    thing in this worker that knows how to kill a process group on a timeout,
    and reusing it is what keeps a wedged git from outliving the attempt.

    TRUNCATION IS DECIDED FROM THE CAPTURE, NEVER FROM THE DECODED TEXT'S
    LENGTH (#259 review, M2). `StreamCapture` caps at `max_bytes` on the raw
    stream and sets `stdout_truncated` the moment it starts dropping bytes; a
    caller that instead re-measured `len(text.encode(...))` after `read_text`
    could be fooled, because reading in TEXT mode with the default `newline`
    applies universal-newline translation: every `\r\n` in the capture becomes
    one `\n`, which can decode-and-reencode SHORTER than the byte cap the
    capture actually hit, so a diff too big to scan whole could read as
    "under the cap" and pass as scanned.

    THE FILE IS READ WITH `newline=""` FOR THE SAME REASON `\r` MUST NOT
    DISAPPEAR (#259 review, M2). Universal-newline translation also turns a
    LONE `\r` inside a line (no `\n` after it) into a `\n`, which splits that
    one line into two. The publish scan (`lifecycle._DiffLeakScanner`) reads
    only lines starting with `+`; a line the translation split in two loses its `+`
    prefix on the second half, so a credential that started after an embedded
    `\r` was read as un-prefixed context and never scanned. `newline=""`
    disables the translation: whatever bytes the stream carried are what this
    function hands back.
    """
    out = logs_dir / f"git-{slug}.out.log"
    result = run_child(
        argv,
        cwd=repo,
        env=_git_env(private_dir),
        stdout_path=out,
        stderr_path=logs_dir / f"git-{slug}.err.log",
        timeout_seconds=timeout_seconds,
        grace_seconds=5,
        max_stdout_bytes=max_bytes,
        max_stderr_bytes=256 * 1024,
        logger=logger,
    )
    if result.timed_out:
        raise GitError(f"git {slug} timed out after {timeout_seconds}s")
    try:
        raw_size = out.stat().st_size
    except OSError:
        raw_size = 0
    try:
        with out.open("r", newline="", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    truncated = result.stdout_truncated or raw_size >= max_bytes
    return result.exit_code, text, truncated


#: The most bytes `_git_stream` hands its consumer in one call.
GIT_STREAM_CHUNK_BYTES = 1024 * 1024


def _git_stream(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    consume: Callable[[bytes], None],
) -> int:
    """Run a git command and hand ALL of its stdout to `consume`, in chunks.

    OWNER DECISION, 2026-09-29 (#259): the leak scans read every byte of a
    diff however big it is, and nothing of it is stored. A capture to a file
    has to be capped -- the workspace is memory-backed tmpfs -- and a cap is
    either a refusal of every big change or a hole in the scan. So stdout
    goes to a PIPE this process reads while git writes: `run_child` (still
    the one thing that kills a wedged git's process group on the deadline)
    is pointed at `/dev/fd/<write end>`, its pump reopens that as its sink,
    and a reader thread passes each read of at most `GIT_STREAM_CHUNK_BYTES`
    to `consume`. Memory is one chunk plus whatever `consume` keeps.

    The reader never stops draining, even once `consume` has seen enough or
    raised: a pipe nobody reads would block the pump, and git behind it,
    until the deadline. An exception from `consume` is re-raised here after
    the child has been reaped.
    """
    read_fd, write_fd = os.pipe()
    failure: list[BaseException] = []
    consumed = [0]

    def drain() -> None:
        with os.fdopen(read_fd, "rb", buffering=0) as source:
            while True:
                try:
                    chunk = source.read(GIT_STREAM_CHUNK_BYTES)
                except OSError as exc:
                    # FAIL CLOSED: a stream this side could not read is a
                    # stream the scan did not see.
                    failure.append(GitError(f"git {slug}: the output could not be read ({exc})"))
                    return
                if not chunk:
                    return
                consumed[0] += len(chunk)
                if failure:
                    continue
                try:
                    consume(chunk)
                except BaseException as exc:  # re-raised on the caller's thread
                    failure.append(exc)

    reader = threading.Thread(target=drain, name=f"git-{slug}-stream", daemon=True)
    reader.start()
    try:
        result = run_child(
            argv,
            cwd=repo,
            env=_git_env(private_dir),
            stdout_path=Path(f"/dev/fd/{write_fd}"),
            stderr_path=logs_dir / f"git-{slug}.err.log",
            timeout_seconds=timeout_seconds,
            grace_seconds=5,
            # Never reached: the pipe is not storage, and the scan must see
            # every byte. The cap exists only because the capture takes one.
            max_stdout_bytes=1 << 62,
            max_stderr_bytes=256 * 1024,
            logger=logger,
        )
    finally:
        # The pump reopened the pipe through /dev/fd and has closed its copy;
        # closing this one is what lets the reader see the end of the stream.
        os.close(write_fd)
        # With a deadline: a pump that never closed its end would otherwise
        # hold this call, and the publish, past the git command's own.
        reader.join(timeout=max(timeout_seconds, 1))
    if reader.is_alive():
        raise GitError(f"git {slug}: its output was still open after {timeout_seconds}s")
    if failure:
        raise failure[0]
    if result.timed_out:
        raise GitError(f"git {slug} timed out after {timeout_seconds}s")
    # FAIL CLOSED: every byte the pump wrote must have reached the consumer,
    # or the scan passed over output it never read.
    if consumed[0] != result.stdout_bytes:
        raise GitError(
            f"git {slug}: {result.stdout_bytes} bytes were written and "
            f"{consumed[0]} read; the stream was not scanned whole"
        )
    return result.exit_code if result.exit_code is not None else -1


def _parse_log(stream: str) -> tuple[list[CommitSummary], int, int]:
    """Parse one `git log --numstat` stream into commits plus totals."""
    commits: list[CommitSummary] = []
    total_add = 0
    total_del = 0
    for chunk in stream.split(_RS):
        if not chunk.strip():
            continue
        # Split on the FIELD separator, not on lines. The header looks like
        # one line and is not: `%an` is an author name, and a name can contain
        # a newline, which pushes `%aI` onto the next line and leaves the
        # header three fields short. Reading `lines[0]` therefore drops the
        # whole commit -- silently, because a short header is indistinguishable
        # from a malformed one. Found by the test, not by review.
        parts = chunk.split(_FS, 3)
        if len(parts) < 4:
            continue
        sha, subject, author, rest = parts
        when, _, numstat = rest.partition("\n")
        adds = dels = files = binary = 0
        for row in numstat.split("\n"):
            row = row.strip()
            if not row:
                continue
            cells = row.split("\t")
            if len(cells) < 3:
                continue
            files += 1
            # git prints "-" for both counts on a binary file. Counting those
            # as zero would be right; counting them as a file that changed
            # nothing would not, so they are reported separately.
            if cells[0] == "-" or cells[1] == "-":
                binary += 1
                continue
            try:
                adds += int(cells[0])
                dels += int(cells[1])
            except ValueError:
                continue
        commits.append(
            CommitSummary(
                sha=sha.strip(),
                subject=subject,
                author=author,
                committed_at=when,
                files_changed=files,
                insertions=adds,
                deletions=dels,
                binary_files=binary,
            )
        )
        total_add += adds
        total_del += dels
    return commits, total_add, total_del


def _parse_file_changes(numstat: str, name_status: str) -> list[FileChange]:
    """Join `git diff -z -M --numstat` and `--name-status` output into rows.

    `-z` on both: without it git C-quotes a path holding a tab, a newline or a
    non-ASCII byte, and a rename prints as `old => new` in one field. With it,
    a numstat record is `adds TAB dels TAB path NUL`, or for a rename
    `adds TAB dels TAB NUL old NUL new NUL`; a name-status record is
    `letter NUL path NUL`, or `R<score> NUL old NUL new NUL`. The two are
    joined on the new path rather than by position, so a reordering in some
    future git costs a status letter, not a wrong one.

    Only NUL-TERMINATED fields are read: the piece after the last NUL is
    either empty or the tail of a capture cut at its byte cap, and reading a
    cut path would list a file that does not exist.
    """
    statuses: dict[str, str] = {}
    fields = name_status.split("\0")[:-1]
    i = 0
    while i + 1 < len(fields):
        letter = fields[i]
        if not letter:
            break
        if letter[0] in "RC":
            if i + 2 >= len(fields):
                break
            statuses[fields[i + 2]] = letter[0]
            i += 3
        else:
            statuses[fields[i + 1]] = letter[0]
            i += 2

    rows: list[FileChange] = []
    fields = numstat.split("\0")[:-1]
    i = 0
    while i < len(fields):
        head = fields[i]
        if not head:
            break
        cells = head.split("\t", 2)
        if len(cells) < 3:
            break
        adds, dels, path = cells
        old_path: str | None = None
        step = 1
        if path == "":
            # A rename: the two paths follow as their own NUL-ended fields.
            if i + 2 >= len(fields):
                break
            old_path, path = fields[i + 1], fields[i + 2]
            step = 3
        i += step
        binary = adds == "-" or dels == "-"
        try:
            insertions = None if binary else int(adds)
            deletions = None if binary else int(dels)
        except ValueError:
            continue
        letter = statuses.get(path, "R" if old_path is not None else "M")
        status = _FILE_STATUS.get(letter, "M")
        rows.append(
            FileChange(
                path=path,
                old_path=old_path if status == "R" else None,
                status=status,
                insertions=insertions,
                deletions=deletions,
                binary=binary,
            )
        )
    return rows


def summarize_work(
    *,
    repo: Path,
    base: str | None,
    private_dir: Path,
    logs_dir: Path,
    patch_path: Path,
    max_patch_bytes: int,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    max_dirty_listed: int = 200,
    empty_base: bool = False,
) -> WorkSummary:
    """Describe what the agent did, and write one applicable patch.

    Read-only with respect to the repository's history: nothing is committed,
    nothing is pushed, no credential is needed or used. The one mutation is
    `add --intent-to-add`, explained at its call below.

    `base` is the commit the clone landed on. Without it there is nothing to
    diff against and only the dirty list can be reported -- which is still
    worth having, so this degrades rather than raising.

    `empty_base` says the missing base is KNOWN to be nothing: the repository
    was empty when it was cloned, so every commit reachable from HEAD is the
    agent's. Without it, an empty clone whose agent committed everything and
    left nothing dirty summarised as "changed nothing", and the publish
    returned before its commits were replayed (#259) -- the one case where a
    missing base must not mean "no commits to list".
    """
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git repository")

    g = [git_binary, *_NO_HOOKS]

    def run(argv: list[str], slug: str, cap: int = 4 * 1024 * 1024) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=cap,
        )

    code, head_text = run([*g, "rev-parse", "HEAD"], "harvest-head")
    head = head_text.strip() or None if code == 0 else None

    # `--intent-to-add` records untracked paths in the index WITHOUT staging
    # their content, which is the only way to make a single `git diff` cover
    # files the agent created. It touches the index and nothing else: no
    # commit, no working tree change, no reachable object. It runs after the
    # final checkpoint on every path that reaches here, so it cannot alter
    # what a resumed attempt restores.
    run([*g, "add", "--all", "--intent-to-add", "--", "."], "harvest-intent")

    status_code, status_text = run([*g, "status", "--porcelain=v1", "--"], "harvest-status")
    dirty_all: list[str] = []
    if status_code == 0:
        for line in status_text.splitlines():
            if len(line) > 3:
                dirty_all.append(line[3:].strip())
    dirty = tuple(dirty_all[:max_dirty_listed])

    commits: list[CommitSummary] = []
    adds = dels = 0
    span = f"{base}..HEAD" if base else ("HEAD" if empty_base else None)
    if span and head and base != head:
        log_code, log_text = run(
            [
                *g, "log", "--numstat", "--no-color", "--no-merges", "--no-ext-diff",
                "--no-textconv", f"--format={_RS}%H{_FS}%s{_FS}%an{_FS}%aI",
                span,
            ],
            "harvest-log",
        )
        if log_code == 0:
            commits, adds, dels = _parse_log(log_text)
        else:
            # A shallow clone whose base is not an ancestor of HEAD -- a rebase
            # inside the workspace, most likely. Not an error: the patch below
            # still describes the whole change, so say what is missing rather
            # than failing the harvest.
            logger.warning("could not list commits against the clone base", base=base)

    # Per-file counts of the SAME diff the patch below is taken from, so a
    # reader can say "12 files +886 -866" and draw the file list before it
    # reads the patch -- and still can when the patch was over its cap and
    # discarded. `-M` because a rename is what a reviewer wants to see rather
    # than a delete beside an add; the same `--no-ext-diff --no-textconv` as
    # the patch, for the same reason (#259). Paths only: neither command
    # prints a line of content.
    files: tuple[FileChange, ...] | None = None
    files_truncated = False
    if base:
        diff_args = ["--no-color", "--no-ext-diff", "--no-textconv", "-z", "-M", base, "--"]
        numstat_code, numstat_text, numstat_cut = _git_text_full(
            [*g, "diff", "--numstat", *diff_args],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="harvest-numstat",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        status_code_, status_listing = run([*g, "diff", "--name-status", *diff_args], "harvest-name-status")
        if numstat_code == 0:
            rows = _parse_file_changes(numstat_text, status_listing if status_code_ == 0 else "")
            files = tuple(rows[:MAX_FILES_LISTED])
            # A capture cut at its byte cap ends mid-list: the rows parsed are
            # true, but they are not all of them.
            files_truncated = len(rows) > MAX_FILES_LISTED or numstat_cut
        else:
            logger.warning("could not list the changed files against the clone base", base=base)

    def run_full(argv: list[str], slug: str) -> tuple[int, str, bool]:
        return _git_text_full(
            argv, repo=repo, private_dir=private_dir, logs_dir=logs_dir, slug=slug,
            timeout_seconds=timeout_seconds, logger=logger,
        )

    own_files, own_files_truncated = _own_files(run_full, g, base, head)

    patch_name: str | None = None
    patch_bytes = 0
    omitted = False
    if base:
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        # `--binary` so the patch survives an image or a lockfile; diffing
        # against `base` rather than HEAD is deliberate, because it makes ONE
        # patch that carries committed and uncommitted work together. That is
        # what a reader wants: agents that edit without committing are the
        # common case, not the exception.
        # `--no-ext-diff --no-textconv`: the clone's `.git/config` and
        # `.gitattributes` are the agent's, and `git diff` runs the
        # `diff.external` program or `diff.<driver>.textconv` filter they
        # name -- agent code executing in the worker after the sandbox was
        # finished with, and a patch reading whatever that program printed
        # (#259, the hostile-config test).
        diff_code, _ = _git_text(
            [*g, "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", base, "--"],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="harvest-diff",
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=max_patch_bytes + 1,
        )
        produced = logs_dir / "git-harvest-diff.out.log"
        if diff_code == 0 and produced.exists():
            size = produced.stat().st_size
            if size == 0:
                pass
            elif size > max_patch_bytes:
                omitted = True
                patch_bytes = size
                logger.warning("patch exceeds the cap and was not kept", bytes=size)
            else:
                patch_path.write_bytes(produced.read_bytes())
                patch_name = patch_path.name
                patch_bytes = size

    return WorkSummary(
        base=base,
        head=head,
        commits=tuple(commits),
        dirty=dirty,
        dirty_truncated=len(dirty_all) > len(dirty),
        patch_name=patch_name,
        patch_bytes=patch_bytes,
        patch_omitted=omitted,
        insertions=adds,
        deletions=dels,
        files=files,
        files_truncated=files_truncated,
        own_files=own_files,
        own_files_truncated=own_files_truncated,
    )


def _own_files(
    run: Any, g: list[str], base: str | None, head: str | None
) -> tuple[tuple[str, ...] | None, bool]:
    """`(paths, truncated)` this step's own work changed since `base` (#2337).

    The commits are HEAD's FIRST-PARENT line back to `base`, merges left
    out: a merge's second parent is the branch merged in, so main's commits
    and another lane's never reach the walk, and the merge commit itself is
    skipped, so what it brought in counts for nothing. Each kept commit is
    diffed against its first parent (`git log --name-only`), `--no-renames`
    so a file moved out of the UI still names its UI path. The uncommitted
    work (`git diff HEAD`, after the harvest's `--intent-to-add`) is the
    step's own too. A lane that fast-forwarded onto main has main's commits
    on its first-parent line: those count as its own, which gates more, never
    less. `(None, False)` when either listing fails: not measured.
    `run` is `_git_text_full` bound to the repository.
    """
    if not base or not head:
        return None, False
    found: dict[str, None] = {}
    cut = False
    if base != head:
        code, text, log_cut = run(
            [*g, "log", "--first-parent", "--no-merges", "--no-renames", "--name-only",
             "--no-color", "--no-ext-diff", "--no-textconv", "--format=", "-z",
             f"{base}..HEAD", "--"],
            "harvest-own-files",
        )
        if code != 0:
            return None, False
        found.update(dict.fromkeys(_nul_paths(text)))
        cut = log_cut
    code, text, diff_cut = run(
        [*g, "diff", "--name-only", "--no-renames", "--no-color", "--no-ext-diff",
         "--no-textconv", "-z", "HEAD", "--"],
        "harvest-own-dirty",
    )
    if code != 0:
        return None, False
    found.update(dict.fromkeys(_nul_paths(text)))
    paths = list(found)
    # A capture cut at its byte cap ends mid-list: true rows, not all of them.
    return tuple(paths[:MAX_FILES_LISTED]), len(paths) > MAX_FILES_LISTED or cut or diff_cut


def _nul_paths(text: str) -> list[str]:
    """The paths of a `-z` name listing; `git log` puts a newline between commits."""
    return [piece.lstrip("\n") for piece in text.split("\0") if piece.lstrip("\n")]


def hide_from_git(
    *,
    repo: Path,
    name: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> bool:
    """Make the top-level `name` in the checkout invisible to git. True when it is.

    For the `./artifacts` link the worker makes in the checkout (#226): with a
    repository attached the agent starts in `work/repo`, so `./artifacts` is
    there, and a link git could see would be in the harvest's patch, the
    auto-commit and the pushed branch -- a symlink to this attempt's own
    directory, in the tenant's repository.

    `.git/info/exclude`, never `.gitignore`: the exclude file is local to this
    clone, is never committed and never pushed, so the repository the agent
    works on is left exactly as it arrived. `/name`, anchored, so a directory
    of the same name deeper in the tree is not hidden.

    Then ASKED, not assumed. A repository's own `.gitignore` outranks the
    exclude file, and a `!/artifacts` there un-hides the link; so the answer is
    git's (`check-ignore`), and False tells the caller to take the link away
    rather than let it into the diff. Idempotent: a resumed attempt restores
    the exclude file from its checkpoint, and the entry is not written twice.

    Refuses to write through a symlink at `.git/info` or at the exclude file:
    the agent of an earlier attempt could have put one there, and the checkpoint
    that restored it keeps only links that stay inside `work/`.
    """
    repo = Path(repo)
    git_dir = repo / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return False
    info = git_dir / "info"
    exclude = info / "exclude"
    if info.is_symlink() or exclude.is_symlink():
        return False
    pattern = f"/{name}"
    try:
        info.mkdir(exist_ok=True)
        existing = exclude.read_text(errors="replace") if exclude.exists() else ""
        if pattern not in existing.splitlines():
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{separator}# SwarmCloud: the link to this attempt's artifacts "
                    f"directory, never part of the work\n{pattern}\n"
                )
    except OSError as exc:
        logger.warning("could not write the checkout's git exclude file", error=str(exc))
        return False
    try:
        code, _ = _git_text(
            [git_binary, *_NO_HOOKS, "check-ignore", "--quiet", "--", name],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="hide-artifacts-link",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
    except GitError as exc:
        logger.warning("could not ask git whether the link is hidden", error=str(exc))
        return False
    return code == 0


@dataclass(frozen=True)
class PushResult:
    branch: str
    head: str
    remote_url: str
    #: The commit the branch was pushed on top of, for the PR body.
    base: str | None
    auto_committed: bool
    forced: bool = False


def commit_dirty(
    *,
    repo: Path,
    message: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    base: str | None = None,
    left_unstaged: list[str] | None = None,
) -> str | None:
    """Commit whatever the agent left uncommitted. Returns the new sha, or None.

    WHY THIS IS NOT OPTIONAL ON THE PUBLISH PATH. A push transfers commits, so
    an agent that edited twenty files and never ran `git commit` would produce
    a branch identical to its base and a pull request with an empty diff. That
    is the most common agent behaviour, so treating it as the exception would
    make the feature useless most of the time.

    It is safe specifically because of where it writes: a branch named after
    the task, which only this attempt pushes, created fresh from the clone
    base. It never runs against a branch a human shares.

    WHERE IT RUNS on the publish path: in the worker's clean publish
    repository, after `mirror_worktree` has copied the agent's working tree
    into it -- never in the agent's clone, whose `.git/config` can define a
    filter driver that `git add` would run (#259 M1). `repo` can be any
    repository; the harvest tests still call it on a clone directly.

    `--no-verify` and a null hooks path: the agent can write `.git/hooks/*` in
    its own workspace, and a commit that ran them would be arbitrary code
    executing under the worker after the runner has already exited.

    WHAT THE AGENT'S OWN COMMITS REMOVED STAYS REMOVED (#2536). `base` is the
    publish base. A path one of the agent's commits since it deleted, that
    HEAD does not hold and that is still on disk untracked, is NOT staged: it
    is appended to `left_unstaged` instead. Measured on task_6e4f6aa4: the
    fix agent ran `git rm --cached scratchpad/runbook-cf-step2/guards.log`,
    which keeps the file on disk, and committed the removal; `add --all`
    staged the untracked file again, the worker's commit put it back, and the
    fix's net diff was empty while the step read SUCCEEDED. `git rm --cached`
    is how a file is taken out of a repository and kept locally, so the
    agent's commit is the statement of intent and the file on disk is not.
    A path HEAD holds again was re-added by a later commit, and an untracked
    path no agent commit deleted is new work: both are staged as before. With
    no `base` nothing is held back, which is the behaviour before #2536.
    """
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    # `add --all` after the harvest's intent-to-add is what turns those
    # placeholder index entries into real staged content.
    removed = _untracked_paths_agent_commits_removed(run, git_binary, base, repo) if base else []
    add_code, _ = run([*g, "add", "--all", "--", "."], "publish-add")
    if add_code != 0:
        raise GitError("could not stage the agent's changes")
    if removed:
        # Unstaged again from a file, not the command line: the list is the
        # agent's and its length is not bounded. HEAD does not hold any of
        # them, so resetting each to HEAD takes it out of the index.
        listing = Path(private_dir) / "publish-left-unstaged.paths"
        listing.write_bytes(b"".join(p.encode() + b"\0" for p in removed))
        try:
            reset_code, _ = run(
                [git_binary, *_NO_HOOKS, "--literal-pathspecs", "reset", "-q",
                 f"--pathspec-from-file={listing}", "--pathspec-file-nul"],
                "publish-left-unstaged",
            )
        finally:
            listing.unlink(missing_ok=True)
        if reset_code != 0:
            raise GitError("could not leave unstaged the files the agent's commits removed")
        logger.info("left unstaged: files the agent's commits removed from the index",
                    paths=removed[:20], count=len(removed))
        if left_unstaged is not None:
            left_unstaged.extend(removed)

    # Nothing staged means nothing to commit, and `git commit` exits 1 for
    # that. It is not a failure -- the agent may have committed everything
    # itself -- so it is distinguished here rather than raised.
    diff_code, _ = run([*g, "diff", "--cached", "--quiet"], "publish-staged")
    if diff_code == 0:
        return None

    # `--author` as well as the identity keys: this commit concludes a
    # cherry-pick the agent left in progress, if there is one, and git would
    # otherwise give it the picked commit's author (see `_worker_identity`).
    commit_code, _ = run(
        [
            *g, "commit", "--no-verify", _author_option(author_name, author_email),
            "--message", message,
        ],
        "publish-commit",
    )
    if commit_code != 0:
        raise GitError("could not commit the agent's uncommitted changes")

    code, text = run([git_binary, *_NO_HOOKS, "rev-parse", "HEAD"], "publish-head")
    return text.strip() if code == 0 else None


def _untracked_paths_agent_commits_removed(
    run: Any, git_binary: str, base: str, repo: Path
) -> list[str]:
    """The paths `commit_dirty` leaves unstaged (#2536), sorted.

    Deleted by a commit in `base..HEAD` (renames read as a delete and an
    add), absent from HEAD's tree, and on disk as a file or a link that `add
    --all` would stage. On disk, not "untracked" by `ls-files --others`: in
    the publish repository the harvest's `add --intent-to-add` has already
    put every new file in the index, so the index cannot tell them apart. A listing git cannot
    make -- a base this repository does not hold -- is none: the auto-commit
    then stages as it did before, and `Worker._publish_git`'s check that the
    auto-commit did not undo the agent's commits still stands behind it.
    """
    span = "HEAD" if base == EMPTY_CLONE_BASE else f"{base}..HEAD"
    g = [git_binary, *_NO_HOOKS]

    def names(argv: list[str], slug: str) -> set[str] | None:
        code, text = run(argv, slug)
        if code != 0:
            return None
        return {n.strip("\n") for n in text.split("\0") if n.strip("\n")}

    deleted = names(
        [*g, "log", "--no-renames", "--diff-filter=D", "--name-only", "-z", "--format=", span],
        "publish-agent-deletions",
    )
    if not deleted:
        return []
    held = names([*g, "ls-tree", "-r", "-z", "--name-only", "HEAD"], "publish-head-paths")
    if held is None:
        return []
    return sorted(
        path for path in deleted - held
        if (repo / path).is_symlink() or (repo / path).is_file()
    )


def fold_agent_commits(
    *,
    repo: Path,
    base: str | None,
    keep: str | None,
    message: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    merged: str = "",
) -> int:
    """Replace every commit made since `base` with ONE commit the worker makes.

    Returns how many commits were replaced, or 0 when there was nothing to
    replace -- no commits at all, or only `keep`, the worker's own commit from
    `commit_dirty`.

    WHY THE WORKER WRITES EVERY COMMIT IT PUSHES. Nothing this platform puts on
    a forge may carry Claude attribution (the owner's rule, 2026-09-25). The
    worker's own commits never did; the AGENT'S could. The claude-code runner
    starts Claude Code with HOME set to the attempt's workspace and no settings
    of its own, so an agent that runs `git commit` follows Claude Code's
    default instruction to add a `Co-Authored-By: Claude` trailer and a
    "Generated with Claude Code" line -- and in a container with no git
    identity it may commit AS Claude. A settings file cannot fix this from the
    image: HOME is overridden, a setting governs only the tool's instruction
    and not the model's choice of identity, and it covers one runner. Folding
    here covers every runner, and is checkable against a real remote.

    The tree is not touched: `reset --soft` keeps the index and the working
    tree exactly as the agent left them, so the one commit carries all of its
    work, committed or not. What the agent committed is still recorded -- the
    harvest ran first, and `result_summary.git.commits` lists each commit's
    subject and author -- it is only not pushed as the agent wrote it.

    An unknown `base` REFUSES rather than degrading. Without it there is no way
    to tell the agent's commits from the repository's own, and pushing whatever
    HEAD holds is the unchecked push this exists to prevent.

    `EMPTY_CLONE_BASE` is not unknown. The repository had no commits when it
    was cloned, so every commit in it is the agent's, and they are replaced by
    one worker commit with no parent.

    `merged`, when given, is the default branch's commit the agent's work
    merged, proven on the forge by the caller (`Worker._scan_floor`). The one
    commit then has it as its SECOND PARENT, so a lane that merged `main` --
    a merge step's conflict fix round above all -- is still a merge once
    folded, and its pull request's merge base moves to `main` (#1321). It is
    ignored unless HEAD contains it and `base` does not.

    `keep` is left alone only when it is the sole commit AND it reads back as
    the worker's. The sha alone does not prove that: a commit that concludes a
    cherry-pick the agent left in progress is authored as the picked commit
    was, whoever ran `git commit`. `commit_dirty` passes `--author` against
    exactly that, and this check is what folds the commit anyway if some other
    route lends it an author.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot replace the agent's "
            "commits with its own; nothing was pushed rather than commits whose "
            "author and message the worker did not write"
        )
    base = (base or "").strip()
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-fold-born")
        if code != 0:
            # Still no commit at all: nothing was made, so nothing to replace.
            return 0
        span = "HEAD"
    else:
        span = f"{base}..HEAD"

    code, text = run([*g, "rev-list", "--count", span], "publish-fold-count")
    if code != 0:
        raise GitError("could not count the commits made since the clone base")
    try:
        count = int(text.strip() or "0")
    except ValueError as exc:
        raise GitError(f"could not read the commit count {text.strip()[:40]!r}") from exc
    if count == 0:
        return 0
    if count == 1 and keep:
        code, head = run([*g, "rev-parse", "HEAD"], "publish-fold-head")
        if code == 0 and head.strip() == keep:
            author, committer = _worker_idents(run, g)
            if not _foreign(_read_commit(run, g, keep), author, committer):
                # The only commit is the worker's own, from `commit_dirty`.
                return 0

    if empty:
        # `reset --soft` needs a commit to rewind to and there is none, so the
        # parentless commit is made from the index directly. The index is
        # everything the agent left, committed or not: `commit_dirty` staged
        # what was uncommitted, and the rest is what HEAD already holds.
        code, tree = run([*g, "write-tree"], "publish-fold-tree")
        if code != 0 or not tree.strip():
            raise GitError("could not write the agent's work as a tree")
        code, made = run([*g, "commit-tree", tree.strip(), "-m", message], "publish-fold-root")
        if code != 0 or not _SHA_RE.match(made.strip()):
            raise GitError("could not commit the agent's work as the worker")
        code, _ = run([*g, "reset", "--soft", made.strip()], "publish-fold-reset")
        if code != 0:
            raise GitError("could not move the branch to the worker's commit")
    else:
        merged = merged.strip()
        if merged:
            if not _SHA_RE.match(merged):
                merged = ""
            else:
                code, _ = run([*g, "merge-base", "--is-ancestor", merged, "HEAD"],
                              "publish-fold-merged")
                inside, _ = run([*g, "merge-base", "--is-ancestor", merged, base],
                                "publish-fold-redundant")
                if code != 0 or inside == 0:
                    merged = ""
        code, _ = run([*g, "reset", "--soft", base], "publish-fold-reset")
        if code != 0:
            raise GitError("could not rewind to the clone base to replace the agent's commits")

        # Commits that add up to no change leave nothing staged; HEAD is the base.
        code, _ = run([*g, "diff", "--cached", "--quiet"], "publish-fold-staged")
        if merged:
            # A merge of the default branch: one commit with two parents, so
            # `git commit` (one parent) is not enough. The tree is the index,
            # everything the agent left, exactly as the commit below takes it.
            code, tree = run([*g, "write-tree"], "publish-fold-tree")
            if code != 0 or not tree.strip():
                raise GitError("could not write the agent's work as a tree")
            code, made = run(
                [*g, "commit-tree", tree.strip(), "-p", base, "-p", merged, "-m", message],
                "publish-fold-merge",
            )
            if code != 0 or not _SHA_RE.match(made.strip()):
                raise GitError("could not commit the agent's work as the worker")
            code, _ = run([*g, "reset", "--soft", made.strip()], "publish-fold-reset")
            if code != 0:
                raise GitError("could not move the branch to the worker's commit")
        elif code != 0:
            code, _ = run(
                [
                    *g, "commit", "--no-verify", _author_option(author_name, author_email),
                    "--message", message,
                ],
                "publish-fold-commit",
            )
            if code != 0:
                raise GitError("could not commit the agent's work as the worker")
    logger.info("folded the agent's commits into one worker commit", replaced=count)
    return count


def verify_merged_default_branch(
    *,
    repo: Path,
    base: str | None,
    tip: str | None,
    default_branch: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> str:
    """Refuse a merge step's conflict fix round that did not merge the default branch (#1321).

    Returns the merge base it accepted. Raises `GitError` naming the merge
    base it found otherwise.

    A conflict fix round exists to move its pull request's merge base to the
    default branch's head: `git merge-base <tip> HEAD` must be `tip`, the
    head fetched from the FORGE (`Worker._default_branch_tip`), never the
    agent's `origin/main`. Measured 2026-10-14 (PR 1316, PR 1304): rounds
    whose "Merge ... origin/main" commit had one parent left the merge base at
    the branch's fork point, GitHub still saw the conflict and listed every
    file main had changed, and the step failed after spending both rounds.

    ONE OTHER ANSWER IS ACCEPTED: `main` moved on while the round ran. Then
    the merge base is the main commit the round merged -- a parent, after the
    first, of a merge on HEAD's first-parent line since `base` -- and the
    branch stands on main as the round found it; whether main's newer
    commits conflict too is the merge step's next reading, not this round's
    failure.

    `repo` is the worker's publish repository, after the replay or the fold:
    what is checked is what would be pushed.
    """
    g = [git_binary, *_NO_HOOKS]
    repo = Path(repo)

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    tip = (tip or "").strip()
    if not _SHA_RE.match(tip):
        raise GitError(
            f"refusing to publish a conflict fix round: {default_branch}'s head could not "
            "be read from the forge, so the worker cannot show that this round merged it "
            "(its merge base is unknown)"
        )
    code, found = run([*g, "merge-base", tip, "HEAD"], "publish-round-merge-base")
    merge_base = found.strip() if code == 0 else ""
    if merge_base == tip:
        logger.info("the conflict fix round's branch stands on the default branch's head",
                    merge_base=merge_base)
        return merge_base
    floor = (base or "").strip()
    if merge_base and _SHA_RE.match(floor):
        code, text = run(
            [*g, "rev-list", "--first-parent", "--merges", "--parents", f"{floor}..HEAD"],
            "publish-round-merges",
        )
        merged = {
            other for line in (text.splitlines() if code == 0 else [])
            for other in line.split()[2:]
        }
        if merge_base in merged:
            logger.info("the conflict fix round merged the default branch, which has moved on "
                        "since", merge_base=merge_base, tip=tip)
            return merge_base
    shown = merge_base[:12] if merge_base else "none"
    raise GitError(
        f"refusing to publish a conflict fix round whose branch is not on {default_branch}: "
        f"its merge base with {default_branch}'s head {tip[:12]} is {shown}, so the pull "
        f"request would still conflict. Merge {default_branch} with `git merge --no-ff "
        f"origin/{default_branch}` -- a real two-parent merge, not a squash, rebase or "
        "cherry-pick -- and commit it"
    )


#: The clone base recorded for a repository that had NO commits when it was
#: cloned. `git clone` of an empty repository succeeds and lands on nothing, so
#: there is no sha to record -- but that is a known state, not a lost one, and
#: treating it as unknown refused to publish a new project's first commit. Not
#: hexadecimal, so it can never be read as a sha.
EMPTY_CLONE_BASE = "empty"

#: Case-insensitive fragments only attribution produces, checked in every
#: commit message the worker is about to push. Not the bare word "claude": the
#: runner profile is `claude-code`, and a worker message may name it.
ATTRIBUTION_MARKERS = (
    "co-authored-by",
    "generated with",
    "anthropic.com",
    "claude.com/claude-code",
    "claude.ai/code",
)


@dataclass(frozen=True)
class _Commit:
    sha: str
    author: tuple[str, str]
    committer: tuple[str, str]
    signed: bool
    message: str


def _split_ident(value: str) -> tuple[str, str]:
    """`Name <email> 1700000000 +0000` -> ("Name", "email")."""
    lt = value.find("<")
    gt = value.find(">", lt + 1)
    if lt < 0 or gt < 0:
        return value.strip(), ""
    return value[:lt].strip(), value[lt + 1 : gt].strip()


def _parse_commit_object(sha: str, raw: str) -> _Commit:
    """Read a commit from `git cat-file commit` output: the object as stored,
    which is exactly what a push transfers. No pretty format, mailmap or
    `log.*` setting from the agent's config stands between it and the bytes."""
    header, _, message = raw.partition("\n\n")
    author = committer = ("", "")
    signed = False
    for line in header.split("\n"):
        if line.startswith("author "):
            author = _split_ident(line[len("author "):])
        elif line.startswith("committer "):
            committer = _split_ident(line[len("committer "):])
        elif line.startswith("gpgsig"):
            # `gpgsig` and `gpgsig-sha256`. Continuation lines start with a
            # space, so they never match a header name here.
            signed = True
    return _Commit(sha=sha, author=author, committer=committer, signed=signed, message=message)


def _read_commit(run: Any, g: list[str], sha: str) -> _Commit:
    code, raw = run([*g, "cat-file", "commit", sha], f"publish-read-{sha[:12]}")
    if code != 0:
        raise GitError(f"could not read commit {sha[:12]}")
    return _parse_commit_object(sha, raw)


def _worker_idents(run: Any, g: list[str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """The author and committer git WILL write for the worker, asked of git.

    Asked rather than restated because git normalises an identity -- it trims
    surrounding punctuation and whitespace -- so comparing a commit against the
    configured string would refuse every push from a worker whose configured
    name ends in, say, a full stop. `g` carries `_worker_identity`, which
    outranks anything the agent put in the clone's config.
    """
    idents = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        code, text = run([*g, "var", var], f"publish-{var.lower().replace('_', '-')}")
        if code != 0:
            raise GitError(f"git could not state the worker's identity ({var})")
        idents.append(_split_ident(text.strip()))
    return idents[0], idents[1]


def _foreign(
    commit: _Commit,
    author: tuple[str, str],
    committer: tuple[str, str],
    *,
    also: Sequence[tuple[tuple[str, str], tuple[str, str]]] = (),
) -> list[str]:
    """Every reason `commit` is not one the worker wrote. Empty when it is.

    `also` is any other (author, committer) pair the worker writes as, each
    asked of git by `_worker_idents` (#765: the task's person, with the bot
    kept for a branch an earlier attempt pushed). A commit must carry ONE of
    the pairs whole: the worker never writes one identity's author with
    another's committer, so a mixed pair is not its.
    """
    problems = []
    own = [(author, committer), *also]
    if (commit.author, commit.committer) not in own:
        if all(commit.author != mine for mine, _ in own):
            problems.append(f"authored by {commit.author[0]} <{commit.author[1]}>")
        if all(commit.committer != mine for _, mine in own):
            problems.append(f"committed by {commit.committer[0]} <{commit.committer[1]}>")
        if not problems:
            problems.append(
                f"authored by {commit.author[0]} <{commit.author[1]}> but committed by "
                f"{commit.committer[0]} <{commit.committer[1]}>, a pair the worker never writes"
            )
    if commit.signed:
        # The worker never signs (`commit.gpgSign=false` on every call), so a
        # signature is text from a program the worker did not choose.
        problems.append("signed, and the worker never signs")
    lowered = commit.message.lower()
    found = [marker for marker in ATTRIBUTION_MARKERS if marker in lowered]
    if found:
        problems.append(f"its message carries attribution ({', '.join(found)})")
    return problems


def published_paths(
    *,
    repo: Path,
    base: str | None,
    head: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> list[str] | None:
    """Every path the published branch changes against the clone base, sorted.

    Read for a step that declares a territory (TERRITORY-WARN, part of #1468):
    the pushed head against `base`, with renames as a deletion and an addition
    so both halves are compared. A repository cloned with no commit
    (`EMPTY_CLONE_BASE`) changes every path its head holds. None when git
    could not say, which the caller never reads as "nothing changed".
    """
    if not head:
        return None
    if base == EMPTY_CLONE_BASE:
        argv = ["ls-tree", "-r", "--name-only", "-z", head]
    elif base and _SHA_RE.match(base.strip()):
        argv = ["diff", "--name-only", "--no-renames", "-z", base.strip(), head, "--"]
    else:
        return None
    code, text = _git_text(
        [git_binary, *_NO_HOOKS, *argv],
        repo=Path(repo),
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-territory",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if code != 0:
        return None
    return sorted({path for path in text.split("\0") if path.strip()})


def verify_worker_authorship(
    *,
    repo: Path,
    base: str | None,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    also_own: Sequence[tuple[str, str]] = (),
) -> int:
    """Refuse a push that would add a commit the worker did not write.

    Returns how many commits were checked. Raises `GitError`, naming the
    commit and every reason, when one of them is not the worker's.

    THE FOLD MAKES THE PROPERTY; THIS CHECKS IT, where the work leaves. Every
    way an agent's identity or attribution reached a pushed commit so far was
    a route nobody had thought of -- a config key read before the one the
    worker set, a cherry-pick's author -- and each was closed where it was
    made. This does not depend on knowing the route: it reads the commits the
    push is about to add and refuses any that is not authored AND committed by
    the worker, is signed, or carries an attribution marker.

    WHICH COMMITS: the first-parent chain from HEAD down to the clone base.
    For `direct-pr` and a contributor that is everything the branch adds -- the
    fold or the auto-commit. For an integrator it is also every merge commit;
    the contributor commits those merges bring in are their second parents,
    and each was checked by its own worker when it pushed. A contributor branch
    pushed before this check existed is therefore not re-checked here.

    WHO THE WORKER IS (#765, owner decision 2026-10-07). `author_name` and
    `author_email` are the identity this publish writes as -- the task's
    person when it has one -- and `also_own` every other identity the worker
    has written this branch as: the bot, `WorkerConfig.git_author_*`, for a
    branch an earlier attempt pushed before its person was known. Nobody else
    joins the set; a third identity's commit is refused as before. A
    contributor's person on its own branch is never asked about here: those
    commits are an integrator's second parents.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot tell which commits "
            "this push would add; nothing was pushed"
        )
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-verify-born")
        if code != 0:
            return 0
        span = "HEAD"
    else:
        span = f"{(base or '').strip()}..HEAD"

    code, text = run([*g, "rev-list", "--first-parent", span], "publish-verify-list")
    if code != 0:
        raise GitError("could not list the commits this push would add")
    shas = text.split()
    if not shas:
        return 0

    author, committer = _worker_idents(run, g)
    also = [
        _worker_idents(run, [git_binary, *_NO_HOOKS, *_worker_identity(name, email)])
        for name, email in also_own
        if (name, email) != (author_name, author_email)
    ]
    for sha in shas:
        problems = _foreign(_read_commit(run, g, sha), author, committer, also=also)
        if problems:
            raise GitError(
                f"refusing to push commit {sha[:12]}: {'; '.join(problems)}. The "
                f"worker pushes only commits it wrote, as {author[0]} <{author[1]}>"
            )
    logger.info("every commit this push adds is the worker's", commits=len(shas))
    return len(shas)


#: A full sha-1 object name. The clean repository's fetch names each commit it
#: wants by sha, never by a ref the agent could point elsewhere, and a
#: refspec's source must be the whole name.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


#: The environment `git upload-pack` runs with IN THE AGENT'S CLONE -- the one
#: git process that still reads the clone after the reap (#259, owner item 4).
#: It reads the clone's `.git/config`, so every key that names a program and
#: that upload-pack or its `pack-objects` child could reach is neutralised
#: here or in `_UPLOAD_PACK_CONFIG`. Checked key by key against the git
#: documentation and source for git 2.55.0 -- the version CI runs (the Actions
#: runner's `git version`, 2026-09-29) -- and against 2.40.1, the oldest this
#: worker has been measured with:
#:
#: * no system file and no global file, so the only configuration read is the
#:   clone's and the command line's;
#: * grafts, replace refs and (below) the commit-graph off: see `_git_env`.
#:   The graft file here is a path that cannot exist, not /dev/null (#808):
#:   git prints its graft-deprecation advice whenever the graft file OPENS,
#:   and upload-pack never loads `advice.*` -- its config callback does not
#:   chain to git's default one (git 2.39.5 measured, master read), so
#:   `advice.graftFileDeprecated=false` cannot reach it from the command line,
#:   the environment or any config file. `/dev/null/no-grafts` fails to open
#:   with ENOTDIR, which git's `fopen_or_warn` skips silently, and a graft
#:   file that does not open means no grafts -- exactly what /dev/null gave;
#: * `GIT_NO_LAZY_FETCH`: a clone the agent marked a partial clone
#:   (`extensions.partialClone`, `remote.<name>.promisor`) makes a git that
#:   misses an object FETCH it from the remote the clone's config names, with
#:   that config's `core.sshCommand`, `remote.<name>.uploadpack` or credential
#:   helper -- a program of the agent's, run as the worker (the #259 B1
#:   reproduction, on 2.40.1 and 2.50.1). git 2.55 documents the variable
#:   (git(1): "tells Git not to lazily fetch missing objects from the promisor
#:   remote on demand"); older gits do not know it, so
#:   `GIT_ALLOW_PROTOCOL=none` is the belt that holds there: it allows only a
#:   protocol named "none", overriding every `protocol.<name>.allow` in the
#:   clone's config, so no transport opens at all;
#: * `GIT_SSH_COMMAND` and `GIT_PROXY_COMMAND` outrank `core.sshCommand` and
#:   `core.gitProxy`. The environment is needed for the proxy: `core.gitProxy`
#:   is multi-valued and FIRST match wins, so a repository value would beat a
#:   command-line one. `false` is the worker's own program and connects to
#:   nothing.
_UPLOAD_PACK_ENV = [
    "GIT_GRAFT_FILE=/dev/null/no-grafts",
    "GIT_NO_REPLACE_OBJECTS=1",
    "GIT_CONFIG_NOSYSTEM=1",
    "GIT_CONFIG_GLOBAL=/dev/null",
    "GIT_NO_LAZY_FETCH=1",
    "GIT_ALLOW_PROTOCOL=none",
    "GIT_SSH_COMMAND=false",
    "GIT_PROXY_COMMAND=false",
    "GIT_TERMINAL_PROMPT=0",
    "GIT_ASKPASS=/bin/true",
]

#: The command-line configuration for the same process. Command scope outranks
#: the clone's own file for every single-valued key.
#:
#: * `core.hooksPath`, `core.fsmonitor`: upload-pack runs no hook and reads no
#:   index today; pinned so a future git that does finds nothing;
#: * `core.alternateRefsCommand` is run through the shell to list an
#:   alternate's tips; `true` lists none;
#: * `core.sshCommand`, `protocol.allow`, `credential.helper`: the transport
#:   half of the lazy-fetch path above, pinned on the command line as well;
#: * NOT SET: `uploadpack.packObjectsHook`. git-config(1): it "is only
#:   respected when it is specified in protected configuration" -- system,
#:   global or command line -- precisely so that fetching from an untrusted
#:   repository cannot run it. With the system and global files off and no
#:   command-line value, nothing sets it. There is no value that disables it:
#:   any value set here IS protected and would be run.
#: * `pack.*` names no program in git-config(1), so nothing there to pin.
_UPLOAD_PACK_CONFIG = [
    "-c", "core.commitGraph=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.alternateRefsCommand=true",
    "-c", "core.sshCommand=false",
    "-c", "protocol.allow=never",
    "-c", "credential.helper=",
]


def prepare_publish_repo(
    *,
    source_repo: Path,
    work_head: str | None,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    base: str | None = None,
    git_binary: str = "git",
    local_branch: str = "swarm-publish",
    allow_unknown_base: bool = False,
    confirm_on_origin: Callable[[list[str]], list[str]] | None = None,
) -> Path:
    """Build the worker-owned CLEAN repository the publish decides and pushes from.

    TWO GUARANTEES LIVE HERE, and both come from where the repository is, not
    from a list of settings to switch off.

    THE TOKEN (#219). The push and the integrator's merge authenticate with
    the tenant's token, and a token that reaches many repositories makes where
    it is allowed to go a tenant-isolation guarantee (invariant 9). Run inside
    the repository the agent edited, those commands honour its `.git/config`,
    which the agent can write: a `credential.helper` there is handed the
    token, and a `url.<host>.insteadOf` / `pushInsteadOf` redirects the
    authenticated push to a host of the agent's choosing. No `-c` disables
    `insteadOf`, so the token-bearing commands run in a repository whose
    configuration the worker wrote. The remote is never taken from here either;
    the caller passes the validated `repository_url` to `push_branch` /
    `merge_branches`.

    THE LEAK SCANS (#259, owner decision 2026-09-29). Three security reviews
    each found the per-commit and final-tree scans bypassable through state
    the agent writes under its clone's `.git`: `refs/replace/*`,
    `info/grafts`, `shallow`, and a forged `objects/info/commit-graph`, from
    which `git diff` takes a commit's root tree. Each was closed on its own
    and the next review found another. So the kept-commit list, both scans,
    the fold, the replay, the authorship check and the push all run HERE,
    and nothing the agent wrote under `.git` reaches them:

    * the repository is `git init`-ed fresh under an unpredictable name in
      the worker's private scratch (outside `work/`), so its config, hooks,
      refs and object store are the worker's;
    * NO ALTERNATES. The agent's objects are FETCHED in, with
      `transfer.fsckObjects=true`: every object arrives as bytes git checks
      and hashes, and the connectivity check proves each commit's real tree
      and parents are present. An object store borrowed through
      `objects/info/alternates` would bring the agent's
      `objects/info/commit-graph` with it;
    * the commits wanted are named by SHA -- `work_head` or the clone's HEAD,
      and the clone base the worker recorded
      before the agent ran -- never by a ref the agent could repoint;
    * the fetch's upload side runs in the clone (git has no other way to read
      a repository it does not own) with grafts, replacement and the
      commit-graph switched off and its SHALLOW boundary taken from a file
      the worker writes here, holding the recorded base -- never from the
      agent's `.git/shallow` as such. That file is read only AS DATA, as a
      question: its full-sha lines are put to `confirm_on_origin` (swarm-api's
      `git_fetch` `confirm`, answered from the forge, #2413), and only the
      shas ORIGIN says it has join the boundary (#2559). History under a
      commit origin has is origin's, so cutting the walk there hides nothing
      the agent wrote; a line origin does not confirm is never added, and the
      fetch then fails as before. `upload-pack` is the one git command git
      documents as safe to run in an untrusted repository: it runs no hook,
      and `uploadpack.packObjectsHook` is honoured only from configuration
      the agent cannot write. Whatever the agent's store or config does to
      the upload side can only make the fetch fail -- a missing or
      inconsistent object fails the check here, and nothing is published --
      never change what an object this side reads contains;
    * every git command here runs with `core.commitGraph=false`, no graft
      file and no replacement (`_git_env`), and the repository has no graph,
      grafts or replace refs of the agent's in it anyway.

    `work_head`, when given, is the commit to publish; when None it is the
    clone's HEAD, read from its files as data (`read_head_as_data`; the
    agent's processes are reaped before this runs, so it cannot move). `base` is the clone base the
    worker recorded (`EMPTY_CLONE_BASE` for an empty repository); it is
    fetched too, so the fold and the final-tree scan have its tree, and
    anything else refuses: without it there is no telling the agent's
    commits from the repository's. The work is checked out on
    `local_branch`, so the index holds the final tree the fold commits and
    the integrator's merges have a working tree.
    """
    source_repo = Path(source_repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    empty = base == EMPTY_CLONE_BASE
    floor = "" if empty else (base or "").strip()
    # A base nobody knows: allowed only for the harvest (`allow_unknown_base`),
    # which then fetches HEAD alone, as a root, so the dirty list can still be
    # read here rather than in the clone. There are no commits to list and no
    # patch without a base, and the publish refuses before it pushes anything.
    unknown = not empty and not _FULL_SHA_RE.match(floor)
    if unknown and not allow_unknown_base:
        raise GitError(
            "the clone base is unknown, so the worker cannot tell the agent's "
            "commits from the repository's; nothing was pushed"
        )

    target = (work_head or "").strip()
    if not target:
        # READ AS DATA, never `git rev-parse` in the clone (#259 B1): see
        # `read_head_as_data`. Raises, with the reason, on a HEAD it will
        # not follow.
        target = read_head_as_data(source_repo) or ""
    # AN EMPTY CLONE THE AGENT NEVER COMMITTED IN has no commit to fetch: its
    # HEAD is unborn. That is not an error now that the worker's commit of the
    # uncommitted work is made HERE (`mirror_worktree`, #259 M1) rather than in
    # the clone first -- the repository is left on an unborn `local_branch`,
    # and that commit becomes its root. Anywhere else an unresolvable HEAD
    # still refuses.
    unborn = empty and not target and not (work_head or "").strip()
    if not unborn and not _OBJECT_ID.match(target):
        raise GitError("could not resolve the commit to publish from the clone")
    if unknown:
        floor = target

    # WHERE the repository lives is part of the guarantee, not an
    # implementation detail. `private_dir` shares a uid with the agent
    # (workspace.py: "not a permission boundary"), so during its run the agent
    # could create any path under it -- including a SYMLINK at a name it can
    # predict. A constant name was exactly that hole: `shutil.rmtree` refuses
    # to delete a symlink and, with `ignore_errors=True`, swallows the refusal;
    # `mkdir(exist_ok=True)` then no-ops on the surviving link; and `git init`
    # at that path re-initialises the AGENT's repository behind it. `mkdtemp`
    # creates the directory with an unpredictable name and O_EXCL semantics,
    # generated now -- after the agent has exited -- so the worker never lands
    # on, nor follows, anything the agent could have planted.
    publish_dir = Path(tempfile.mkdtemp(dir=str(private_dir), prefix="publish-"))

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=publish_dir,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    init_code, _ = _git_text(
        [git_binary, *_NO_HOOKS, "init", "--quiet", str(publish_dir)],
        repo=private_dir,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-init",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if init_code != 0:
        raise GitError("could not initialise the worker's publish repository")

    if unborn:
        head_code, _ = run(
            [git_binary, *_NO_HOOKS, "symbolic-ref", "HEAD", f"refs/heads/{local_branch}"],
            "publish-unborn",
        )
        if head_code != 0:
            raise GitError("could not set up the publish repository's branch")
        logger.info("clean publish repository prepared on an unborn branch", branch=local_branch)
        return publish_dir

    # The upload side's shallow boundary is the base the worker recorded, not
    # the agent's `.git/shallow`: a line the agent added there would make git
    # treat a commit mid-history as a root, and one it removed would send the
    # walk into history the clone never had. The first only drops objects
    # (the connectivity check then fails the fetch), the second only fails
    # the pack -- but neither is the agent's to decide.
    #
    # ORIGIN may widen it (#2559). An agent whose swarm-git fetch went
    # unanswered fetches a sibling `--depth=N` in its own shell and merges it;
    # the clone then lacks the parents of that sibling's cut-off, and a
    # boundary of the base alone sends the walk into history the clone never
    # had, failing every such publish. A cut-off origin CONFIRMS it has is
    # safe to stop at: everything under it is origin's history, and the
    # agent's content descends from it, so the walk still reaches every
    # commit the agent wrote. A line origin does not confirm -- one the agent
    # forged to cut its own key commit out of the walk -- is never added.
    if empty:
        shallow_file = "/dev/null"
    else:
        confirmed = _origin_confirmed_shallow(
            source_repo, floor=floor, confirm_on_origin=confirm_on_origin, logger=logger
        )
        boundary = publish_dir / ".git" / "swarm-source-shallow"
        boundary.write_text("".join(f"{sha}\n" for sha in [floor, *confirmed]))
        shallow_file = str(boundary)
    upload_pack = " ".join(
        [
            "env",
            *_UPLOAD_PACK_ENV,
            f"GIT_SHALLOW_FILE={shlex.quote(shallow_file)}",
            shlex.quote(git_binary),
            *_UPLOAD_PACK_CONFIG,
            # The wants are shas, not advertised refs.
            "-c", "uploadpack.allowAnySHA1InWant=true",
            "upload-pack",
        ]
    )
    refspecs = [f"+{target}:refs/swarm/head"]
    if not empty and not unknown:
        refspecs.append(f"+{floor}:refs/swarm/base")
    fetch_code, _ = run(
        [
            git_binary, *_NO_HOOKS,
            "-c", "transfer.fsckObjects=true",
            "-c", "fetch.fsckObjects=true",
            "-c", "fetch.recurseSubmodules=false",
            "-c", "submodule.recurse=false",
            "-c", "gc.auto=0",
            "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-shallow",
            f"--upload-pack={upload_pack}",
            "--", str(source_repo), *refspecs,
        ],
        "publish-fetch",
    )
    if fetch_code != 0:
        # The fixed text first, verbatim: failure causes and the failure
        # review fingerprint on it. Then git's own reason (#2559: six failures
        # said only this, and which of the two it was had to be reproduced).
        raise GitError(
            "could not fetch the agent's work into the worker's clean repository "
            "(an object failed git's checks, or the clone's history is incomplete); "
            "nothing was pushed" + _stderr_tail(logs_dir / "git-publish-fetch.err.log")
        )

    checkout_code, _ = run(
        [git_binary, *_NO_HOOKS, "checkout", "--quiet", "-f", "-B", local_branch, target],
        "publish-checkout",
    )
    if checkout_code != 0:
        raise GitError("could not check out the agent's work in the publish repository")

    logger.info("clean publish repository prepared", branch=local_branch)
    return publish_dir


#: The most of the agent's `.git/shallow` the publish reads, and the most of
#: its lines it asks origin about (#2559). A clone that fetched a few siblings
#: `--depth=N` holds a handful of cut-offs; more is past any real one, and
#: reading more would let the agent make the worker hold an arbitrary file.
#: The line cap is swarm-api's `gitexec.MAX_HAVES`, which `confirm` answers
#: (gittools.MAX_CONFIRM; not imported here, gittools imports this module).
_SHALLOW_MAX_BYTES = 64 * 1024
_SHALLOW_MAX_LINES = 16
#: The most of git's stderr a publish-fetch failure carries.
_STDERR_TAIL_CHARS = 500


def _origin_confirmed_shallow(
    source_repo: Path,
    *,
    floor: str,
    confirm_on_origin: Callable[[list[str]], list[str]] | None,
    logger: Any,
) -> list[str]:
    """The clone's shallow cut-offs that origin says it has (#2559).

    `.git/shallow` is read AS DATA (`_read_in_git_dir`: O_NOFOLLOW at every
    component, a regular file only, capped), never through git in the clone.
    Only full 40-hex lines are asked, the floor and duplicates dropped, at
    most `_SHALLOW_MAX_LINES`; the answer is kept only where it names a sha
    that was asked. A confirmer that raises answers nothing."""
    if confirm_on_origin is None:
        return []
    raw = _read_in_git_dir(source_repo, "shallow", cap=_SHALLOW_MAX_BYTES)
    if not raw:
        return []
    asked: list[str] = []
    for line in raw.decode("ascii", "replace").splitlines():
        sha = line.strip()
        if _FULL_SHA_RE.match(sha) and sha != floor and sha not in asked:
            asked.append(sha)
            if len(asked) == _SHALLOW_MAX_LINES:
                break
    if not asked:
        return []
    try:
        answer = confirm_on_origin(list(asked))
    except Exception as exc:  # noqa: BLE001 - an unanswered question widens nothing
        logger.warning("origin did not confirm the clone's shallow lines",
                       asked=len(asked), error=type(exc).__name__)
        return []
    said = set(answer) if isinstance(answer, (list, tuple, set)) else set()
    confirmed = [sha for sha in asked if sha in said]
    logger.info("clone shallow lines confirmed on origin", asked=len(asked),
                confirmed=len(confirmed))
    return confirmed


def _stderr_tail(path: Path) -> str:
    """`; git: <the redacted end of git's stderr>`, or "" when it said nothing."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4 * _STDERR_TAIL_CHARS))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = " ".join(redact_text(text).text.split())[-_STDERR_TAIL_CHARS:]
    return f"; git: {tail}" if tail else ""


def _empty_worktree(root: Path) -> None:
    """Delete everything in `root` except its top-level `.git`, WITHOUT recursion.

    The tree is the checkout `prepare_publish_repo` just wrote, so it is the
    worker's -- but it has the agent's commits' shape, and on Python 3.11
    `shutil.rmtree` recurses one frame per level (see `workspace.walk_tree`).
    Links are unlinked, never followed.
    """
    folders: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        here = stack.pop()
        with os.scandir(here) as entries:
            for entry in entries:
                if here == root and entry.name == ".git":
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    folders.append(path)
                    stack.append(path)
                else:
                    path.unlink()
    # Deepest first: a folder is listed after the folder that holds it.
    for folder in reversed(folders):
        folder.rmdir()


def _copy_file(source: Path, dest: Path, mode: int) -> None:
    """A regular file, by hard link where the filesystem allows it, else by copy.

    The workspace is memory-backed tmpfs (CLAUDE.md, workspace storage), and
    `private/` and `work/` are on the same one, so a byte copy of a large
    working tree would double what the attempt holds in memory at the very end
    of it. A hard link costs nothing and is safe here: the agent's processes
    are reaped before this runs, so nothing writes through the other name, and
    git in the publish repository never writes a working-tree file in place --
    a checkout unlinks the old entry and creates a new one. A filesystem that
    refuses the link (EXDEV, EPERM, EMLINK) gets a byte copy instead.
    """
    try:
        os.link(source, dest, follow_symlinks=False)
        return
    except OSError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd_in = os.open(source, flags)
    try:
        # O_EXCL: the destination folder was emptied a moment ago, so a name
        # that already exists is a bug, never something to write through.
        fd_out = os.open(
            dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(mode) | stat.S_IWUSR | stat.S_IRUSR,
        )
        try:
            while True:
                chunk = os.read(fd_in, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(fd_out, view)
                    view = view[written:]
            # Only the execute bit reaches git, but the file keeps its mode
            # as the agent left it, as the hard link would.
            os.fchmod(fd_out, stat.S_IMODE(mode))
        finally:
            os.close(fd_out)
    finally:
        os.close(fd_in)


#: The most of any one file the agent wrote that `read_agent_excludes` reads.
#: An exclude file is a list of patterns; a megabyte of them is far past any
#: real one, and reading more would hand the agent a way to make the worker
#: hold an arbitrary file in memory.
AGENT_EXCLUDE_MAX_BYTES = 1024 * 1024

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _read_capped_fd(fd: int, cap: int = AGENT_EXCLUDE_MAX_BYTES) -> bytes | None:
    """The contents of a REGULAR file open as `fd`, up to `cap`; None past it."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _read_in_git_dir(
    clone: Path, *names: str, cap: int = AGENT_EXCLUDE_MAX_BYTES
) -> bytes | None:
    """`<clone>/.git/<names...>`, opened one component at a time with
    O_NOFOLLOW, so a link at `.git`, at a folder below it or at the file
    itself is refused rather than read through. O_NONBLOCK so a FIFO the
    agent put there cannot hang the worker; only a regular file is read,
    and at most `cap` bytes of it."""
    fds: list[int] = []
    try:
        fds.append(os.open(clone / ".git", os.O_RDONLY | _DIRECTORY | _NOFOLLOW))
        for folder in names[:-1]:
            fds.append(os.open(folder, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fds[-1]))
        fds.append(os.open(names[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=fds[-1]))
        return _read_capped_fd(fds[-1], cap)
    except OSError:
        return None
    finally:
        for fd in reversed(fds):
            os.close(fd)


#: A ref or HEAD file holds one line. 4 KiB is far past any real one.
_REF_FILE_MAX_BYTES = 4096
#: `packed-refs` of a depth-1, single-branch, tag-free clone holds a handful of
#: lines; an agent can add more. 64 MiB is past any repository's real refs and
#: bounds what the worker will hold.
_PACKED_REFS_MAX_BYTES = 64 * 1024 * 1024
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _branch_ref_name_ok(name: str) -> bool:
    """A `refs/heads/...` name the worker will open, component by component:
    nothing that climbs (`..`), is empty, starts with a dot, or carries a
    control character -- git's own rules, in the part that matters for
    reading a path safely."""
    if not name.startswith("refs/heads/") or name.endswith("/") or name.endswith(".lock"):
        return False
    for part in name.split("/"):
        if not part or part.startswith(".") or ".." in part:
            return False
        if any(ord(ch) < 0x20 or ch == "\x7f" for ch in part):
            return False
    return True


def is_branch_ref_name(name: object) -> bool:
    """True for a `refs/heads/...` name the worker would read (`_branch_ref_name_ok`)."""
    return isinstance(name, str) and _branch_ref_name_ok(name)


def read_head_as_data(clone: Path) -> str | None:
    """The object id the clone's HEAD names, read from its FILES, never by git.

    WHY (#259 final review, B1; owner decision 2026-09-29). The worker used
    to ask `git rev-parse HEAD^{commit}` in the clone, after the reap. The
    agent can make its clone a partial clone (`extensions.partialClone`, a
    promisor remote) and point its branch at an object the clone lacks; git
    then LAZY-FETCHES that object through a transport the agent chose --
    `remote.<name>.uploadpack`, `core.sshCommand`, an `ext::` URL -- running
    its program as the worker, after the reap, and a process it starts lives
    on into the push. No git process runs in the clone after the reap now,
    except the hardened `upload-pack` of the fetch (`_UPLOAD_PACK_ENV`).

    HOW. `.git/HEAD`, then the ref it names, each opened component by
    component with O_NOFOLLOW, regular files only, capped:

    * a 40- or 64-hex object id (a detached HEAD) is the answer;
    * `ref: refs/heads/<name>` is followed ONCE: to the loose ref file, else
      to its line in `packed-refs`. A symbolic ref anywhere else -- or a ref
      file that is itself symbolic -- raises `GitError`: the worker follows
      no ref it would have to take the agent's word about;
    * a branch that exists nowhere is an unborn HEAD, and None is returned
      (the caller decides whether that is an empty clone or a refusal).

    Whether the object exists is not asked here -- asking is exactly what
    ran the agent's program. The object-checked fetch decides, and a missing
    object fails it: nothing is published.
    """
    raw = _read_in_git_dir(clone, "HEAD", cap=_REF_FILE_MAX_BYTES)
    if raw is None:
        raise GitError("the clone's .git/HEAD could not be read as a regular file")
    head = raw.decode("ascii", "replace").strip()
    if _OBJECT_ID.match(head):
        return head
    if not head.startswith("ref:"):
        raise GitError("the clone's HEAD is neither an object id nor a symbolic ref")
    name = head[4:].strip()
    if not _branch_ref_name_ok(name):
        raise GitError(
            "the clone's HEAD is a symbolic ref outside refs/heads/; the worker "
            "follows no other ref, and nothing was pushed"
        )
    fd = _open_ref_file(clone, *name.split("/"), what="the branch the clone's HEAD names")
    if fd is not None:
        try:
            loose = _read_capped_fd(fd, _REF_FILE_MAX_BYTES)
        finally:
            os.close(fd)
        if loose is None:
            raise GitError(
                "the branch the clone's HEAD names is a file past "
                f"{_REF_FILE_MAX_BYTES} bytes; the worker follows no such ref, and "
                "nothing was pushed"
            )
        value = loose.decode("ascii", "replace").strip()
        if _OBJECT_ID.match(value):
            return value
        raise GitError(
            "the branch the clone's HEAD names is not an object id (a symbolic "
            "ref, or damaged); the worker follows no further ref, and nothing was pushed"
        )
    fd = _open_ref_file(clone, "packed-refs", what="the clone's packed-refs")
    if fd is None:
        return None
    try:
        return _packed_ref(fd, name.encode("utf-8", "surrogateescape"))
    finally:
        os.close(fd)


#: `read_head_branch_as_data`'s answer for a HEAD that names a commit, not a branch.
DETACHED_HEAD = "HEAD"


def read_head_branch_as_data(clone: Path) -> str:
    """The branch the clone's HEAD is on (`refs/heads/<name>`), or
    `DETACHED_HEAD`, read from `.git/HEAD` as `read_head_as_data` reads it:
    one regular file, opened component by component with O_NOFOLLOW, never
    by git (#2094).

    The worker reads it twice: right after its own clone, before any agent
    process exists, for the branch the step was given; and after the reap,
    for the branch the agent left HEAD on. A HEAD naming anything outside
    `refs/heads/`, or one that cannot be read, raises `GitError`.
    """
    raw = _read_in_git_dir(clone, "HEAD", cap=_REF_FILE_MAX_BYTES)
    if raw is None:
        raise GitError("the clone's .git/HEAD could not be read as a regular file")
    head = raw.decode("ascii", "replace").strip()
    if _OBJECT_ID.match(head):
        return DETACHED_HEAD
    if not head.startswith("ref:"):
        raise GitError("the clone's HEAD is neither an object id nor a symbolic ref")
    name = head[4:].strip()
    if not _branch_ref_name_ok(name):
        raise GitError("the clone's HEAD is a symbolic ref outside refs/heads/")
    return name


#: Where the clone's `origin` tracking refs live. The worker's clone tracks
#: every branch (`TRACK_EVERY_BRANCH`), so an agent that fetched a branch by
#: name has its tip here; a branch the agent created locally has none.
ORIGIN_REFS_PREFIX = "refs/remotes/origin/"
#: How many loose refs under `ORIGIN_REFS_PREFIX`, and how deep, the worker
#: will walk before it stops trusting the answer. A clone holds one per
#: fetched branch; past these the agent made them, and the caller is told
#: it cannot be read rather than handed a partial set.
_ORIGIN_REFS_MAX_ENTRIES = 10_000
_ORIGIN_REFS_MAX_DEPTH = 32


def read_origin_ref_as_data(clone: Path, branch: str) -> str | None:
    """The object id `refs/remotes/origin/<name>` holds for `branch`
    (`refs/heads/<name>`), or None when the clone has no such ref (#2094).

    Read as `read_head_as_data` reads a branch -- the loose file opened
    component by component with O_NOFOLLOW, else its line in `packed-refs`
    -- never by git, so no config or hook of the agent's runs. A ref that
    exists but cannot be read as an object id raises `GitError`.
    """
    if not _branch_ref_name_ok(branch):
        raise GitError("not a branch name the worker reads")
    name = ORIGIN_REFS_PREFIX + branch[len("refs/heads/"):]
    fd = _open_ref_file(clone, *name.split("/"), what="the branch's origin tracking ref")
    if fd is not None:
        try:
            loose = _read_capped_fd(fd, _REF_FILE_MAX_BYTES)
        finally:
            os.close(fd)
        value = (loose or b"").decode("ascii", "replace").strip()
        if _OBJECT_ID.match(value):
            return value
        raise GitError("the branch's origin tracking ref is not an object id")
    fd = _open_ref_file(clone, "packed-refs", what="the clone's packed-refs")
    if fd is None:
        return None
    try:
        return _packed_ref(fd, name.encode("utf-8", "surrogateescape"))
    finally:
        os.close(fd)


def origin_tips_as_data(clone: Path) -> set[str]:
    """Every object id a `refs/remotes/origin/*` ref of the clone holds, loose
    or packed, read as data and never by git (#2094).

    The loose refs are walked with O_NOFOLLOW at every level; a link, a FIFO
    or anything but a folder or a regular file is skipped, and a symbolic ref
    (`origin/HEAD`) names no tip of its own. More than
    `_ORIGIN_REFS_MAX_ENTRIES` entries, or a tree deeper than
    `_ORIGIN_REFS_MAX_DEPTH`, raises `GitError`: a partial set would read as
    "no origin tip" exactly where the agent made it partial.
    """
    tips: set[str] = set()
    seen = 0

    def walk(fd: int, depth: int) -> None:
        nonlocal seen
        if depth > _ORIGIN_REFS_MAX_DEPTH:
            raise GitError("the clone's origin tracking refs are nested too deep to read")
        for entry in os.listdir(fd):
            seen += 1
            if seen > _ORIGIN_REFS_MAX_ENTRIES:
                raise GitError("the clone holds too many origin tracking refs to read")
            mode = os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                sub = os.open(entry, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                try:
                    walk(sub, depth + 1)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(mode):
                ref = os.open(entry, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=fd)
                try:
                    raw = _read_capped_fd(ref, _REF_FILE_MAX_BYTES)
                finally:
                    os.close(ref)
                value = (raw or b"").decode("ascii", "replace").strip()
                if _OBJECT_ID.match(value):
                    tips.add(value)

    fds: list[int] = []
    try:
        try:
            fds.append(os.open(clone / ".git", os.O_RDONLY | _DIRECTORY | _NOFOLLOW))
            for folder in ORIGIN_REFS_PREFIX.rstrip("/").split("/"):
                fds.append(os.open(folder, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fds[-1]))
        except FileNotFoundError:
            pass
        else:
            walk(fds[-1], 0)
    except OSError as exc:
        raise GitError(
            "the clone's origin tracking refs could not be read "
            f"({errno.errorcode.get(exc.errno or 0, 'error')})"
        ) from None
    finally:
        for opened in reversed(fds):
            os.close(opened)
    fd = _open_ref_file(clone, "packed-refs", what="the clone's packed-refs")
    if fd is not None:
        try:
            tips |= _packed_refs_under(fd, ORIGIN_REFS_PREFIX.encode())
        finally:
            os.close(fd)
    return tips


def _open_ref_file(clone: Path, *names: str, what: str) -> int | None:
    """`<clone>/.git/<names...>` open for reading, None when it does not exist.

    Opened as `_read_in_git_dir` opens a file: one component at a time, with
    O_NOFOLLOW and O_NONBLOCK. A ref that EXISTS but is not a regular file --
    a link at it or at a folder above it, a FIFO, a directory -- raises
    `GitError` (#346). It used to read as absent, and the caller then took the
    branch from `packed-refs`: the agent could hide its loose ref behind a link
    and have the worker publish whatever older line `packed-refs` held. Only a
    component that is missing (ENOENT) is absence.
    """
    fds: list[int] = []
    try:
        try:
            fds.append(os.open(clone / ".git", os.O_RDONLY | _DIRECTORY | _NOFOLLOW))
            for folder in names[:-1]:
                fds.append(
                    os.open(folder, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fds[-1])
                )
            fd = os.open(names[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=fds[-1])
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GitError(
                f"{what} is not a regular file reached without a link "
                f"({errno.errorcode.get(exc.errno or 0, 'error')}); nothing was pushed"
            ) from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise GitError(f"{what} exists but is not a regular file; nothing was pushed")
        return fd
    finally:
        for opened in reversed(fds):
            os.close(opened)


def _packed_ref(fd: int, name: bytes) -> str | None:
    """The object id `packed-refs`, open as `fd`, gives `name`, or None.

    STREAMED (#346). The whole file -- up to `_PACKED_REFS_MAX_BYTES` -- used to
    be read, decoded and split into a list of every line before one was
    matched. It is read in 64 KiB chunks now, line by line, and reading stops
    at the matching line or the cap. No line longer than `_REF_FILE_MAX_BYTES`
    is held: a ref line is an object id and a name, and an over-long line
    cannot be the one asked for, so it is skipped as it arrives.
    """
    total = 0
    pending = b""
    skipping = False
    while total <= _PACKED_REFS_MAX_BYTES:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for line in lines:
            if skipping:
                skipping = False
                continue
            oid = _packed_line_oid(line, name)
            if oid is not None:
                return oid
        if len(pending) > _REF_FILE_MAX_BYTES:
            pending, skipping = b"", True
    if not skipping and total <= _PACKED_REFS_MAX_BYTES:
        return _packed_line_oid(pending, name)
    return None


def _packed_refs_under(fd: int, prefix: bytes) -> set[str]:
    """The object ids of every `packed-refs` line, open as `fd`, whose name
    starts with `prefix`, streamed as `_packed_ref` streams. A file past
    `_PACKED_REFS_MAX_BYTES` raises `GitError` rather than answer in part."""
    found: set[str] = set()
    total = 0
    pending = b""
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _PACKED_REFS_MAX_BYTES:
            raise GitError("the clone's packed-refs is too large to read")
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for line in lines:
            _packed_line_under(line, prefix, found)
    _packed_line_under(pending, prefix, found)
    return found


def _packed_line_under(line: bytes, prefix: bytes, found: set[str]) -> None:
    line = line.rstrip(b"\r")
    if not line or line[:1] in (b"#", b"^"):
        return
    oid, _, ref = line.partition(b" ")
    if not ref.strip().startswith(prefix):
        return
    text = oid.decode("ascii", "replace")
    if _OBJECT_ID.match(text):
        found.add(text)


def _packed_line_oid(line: bytes, name: bytes) -> str | None:
    """The object id of one `packed-refs` line when it names `name`, else None."""
    line = line.rstrip(b"\r")
    if not line or line[:1] in (b"#", b"^"):
        return None
    oid, _, ref = line.partition(b" ")
    if ref.strip() != name:
        return None
    text = oid.decode("ascii", "replace")
    return text if _OBJECT_ID.match(text) else None


_CONFIG_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)\s*(?:"((?:[^"\\]|\\.)*)")?\s*\](.*)$')
_CONFIG_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=(.*))?$")


def _config_value(raw: str) -> str:
    """A git-config value as git reads it: quotes joined, `\\"`, `\\\\`,
    `\\n` and `\\t` unescaped, a `#` or `;` outside quotes ending it."""
    out: list[str] = []
    quoted = False
    i = 0
    raw = raw.strip()
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip() if not quoted else "".join(out)


def _config_excludes_file(text: str) -> str | None:
    """`core.excludesFile` from the text of a git config file, parsed WITHOUT git.

    Only the file itself: an `[include]` or `[includeIf]` it names is not
    followed, because following it is reading another file the agent chose.
    The last value wins, as in git. A value continued onto the next line
    with a trailing backslash is not supported and reads as absent.
    """
    section: str | None = None
    value: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        header = _CONFIG_SECTION.match(stripped)
        if header:
            name, sub, rest = header.group(1), header.group(2), header.group(3)
            section = name.lower() if sub is None and "." not in name else None
            stripped = rest.strip()
            if not stripped or stripped[0] in "#;":
                continue
        if section != "core":
            continue
        key = _CONFIG_KEY.match(stripped)
        if not key or key.group(1).lower() != "excludesfile":
            continue
        if stripped.endswith("\\"):
            value = None
            continue
        value = _config_value(key.group(2) or "")
    return value or None


def read_agent_excludes(*, clone: Path, workspace_root: Path, home: Path, logger: Any) -> bytes:
    """The patterns the agent hid files with, read as DATA (#259, owner item 2).

    When the worker committed in the clone, git honoured the clone's
    `.git/info/exclude` and the `core.excludesFile` its config named, so a
    file the agent kept out of git stayed out of the pull request. The commit
    is now made in the worker's own repository, which reads neither -- so
    both are read here, WITHOUT running git in the clone, and handed to
    `mirror_worktree` for that repository's own exclude file:

    * `.git/info/exclude`, opened component by component with O_NOFOLLOW and
      read to `AGENT_EXCLUDE_MAX_BYTES` at most;
    * `core.excludesFile`, parsed out of `.git/config` in Python
      (`_config_excludes_file`). A leading `~/` is the agent's HOME (`home`,
      which is `work/`); a relative path is relative to the checkout. The
      file is honoured only when it lies inside the workspace AND no link is
      on the way to it -- its real path is its literal path -- and it is read
      with the same cap. Anything else is ignored and logged.

    The `core.excludesFile` patterns come first and `info/exclude` second,
    which is git's own order of precedence between the two. Returns b"" when
    there is nothing to honour.
    """
    clone = Path(clone)
    parts: list[bytes] = []

    config = _read_in_git_dir(clone, "config")
    named = _config_excludes_file(config.decode("utf-8", "surrogateescape")) if config else None
    if named:
        if named.startswith("~/"):
            path = Path(home) / named[2:]
        elif named.startswith("~"):
            path = None
        else:
            path = Path(named) if os.path.isabs(named) else clone / named
        honoured = False
        if path is not None:
            literal = os.path.normpath(os.path.abspath(path))
            real = os.path.realpath(literal)
            # "No link on the way" is asked BELOW the workspace root: the
            # root's own path may pass through a link the platform made (a
            # macOS /var, a mounted volume), which is not the agent's.
            root_literal = os.path.normpath(os.path.abspath(workspace_root))
            root_real = os.path.realpath(root_literal)
            inside = False
            for root in (root_literal, root_real):
                if (literal + os.sep).startswith(root + os.sep) and literal != root:
                    expected = os.path.normpath(
                        os.path.join(root_real, os.path.relpath(literal, root))
                    )
                    inside = real == expected
                    break
            if inside:
                try:
                    fd = os.open(real, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
                except OSError:
                    fd = -1
                if fd >= 0:
                    try:
                        data = _read_capped_fd(fd)
                    finally:
                        os.close(fd)
                    if data is not None:
                        parts.append(data)
                        honoured = True
        if not honoured:
            logger.warning(
                "the agent's core.excludesFile was not honoured: it is outside the "
                "workspace, reached through a link, missing, not a regular file or "
                "over the size cap; files it lists may be published"
            )

    exclude = _read_in_git_dir(clone, "info", "exclude")
    if exclude:
        parts.append(exclude)
    return b"\n".join(part.rstrip(b"\n") for part in parts if part.strip())


def mirror_worktree(
    *,
    source: Path,
    dest: Path,
    logger: Any,
    hidden_names: Sequence[str] = (),
    agent_excludes: bytes = b"",
) -> int:
    """Make `dest`'s working tree the agent's working tree, file for file (#259 M1).

    WHY. The worker commits whatever the agent left uncommitted, and it used
    to do that with `git add --all` / `git commit` in the AGENT's clone. Those
    commands read the clone's `.git/config` and the tree's `.gitattributes`,
    both the agent's to write, so a filter driver the agent defined
    (`filter.<name>.clean`) and named in `.gitattributes` ran as the worker,
    after the reap, a step away from the tenant token. `_NO_HOOKS` cannot
    close that class by listing keys. So the files are brought HERE, into the
    worker's clean publish repository (`prepare_publish_repo`), and staged and
    committed with the worker's configuration alone (owner decision,
    2026-09-28). A `.gitattributes` in the tree is data here: it names a
    filter or diff driver this repository does not define, which git treats
    as no filter and no driver.

    WHAT IS COPIED. Everything under `source` except:

    * `.git`, at ANY depth. At the top it is the agent's repository; below it,
      a folder holding a `.git` is a repository the agent made or populated
      (a submodule checkout, a nested clone), and git would read that
      repository's refs and config to record it. Such a folder is created
      EMPTY here -- an unpopulated submodule reads as unchanged, and anything
      else empty is invisible to git -- and nothing inside it is copied.
      Accepted by the owner on 2026-09-28: before, git in the clone recorded
      such a folder as an embedded gitlink;
    * anything that is not a regular file, a folder or a link (a FIFO, a
      socket, a device), which git does not track either.

    REGULAR FILES ARE HARD-LINKED, with a byte copy only where the filesystem
    refuses the link (`_copy_file`; accepted by the owner on 2026-09-28): the
    workspace is memory-backed, and a copy would double it at the end of the
    attempt.

    A LINK IS COPIED AS A LINK, its target text read with `readlink` and
    never followed: a link out of the workspace is published as the link
    git would have recorded, never as what it points at. Folders are walked
    from an explicit stack, not by recursion (`workspace.walk_tree` says why)
    and never through a link.

    `hidden_names` are top-level names the worker hides from git in the clone
    (`hide_from_git`, the `./artifacts` link, #226). They are NOT COPIED: an
    exclude pattern alone does not hide them, because the tree's own
    `.gitignore` -- copied in, and read -- outranks `.git/info/exclude`, and a
    `!/artifacts` there un-hid the worker's link (#346). A name that is not
    in the tree cannot be published by any pattern. They are still written to
    this repository's own `.git/info/exclude`, which the worker owns, AFTER
    `agent_excludes` -- the patterns of the agent's own `.git/info/exclude`
    and `core.excludesFile`, read as data by `read_agent_excludes` -- so a
    `!` pattern of the agent's cannot un-hide the worker's names (in one
    exclude file the last matching pattern wins).

    `dest`'s existing working tree -- the checkout of the agent's last commit
    -- is emptied first, so a file the agent deleted is deleted here too.
    Returns how many entries were copied. Raises `GitError` when the tree
    cannot be copied whole: a partial copy would publish a partial change.
    """
    source = Path(source)
    dest = Path(dest)
    git_dir = dest / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise GitError("the publish repository has no .git of its own")
    if source.is_symlink() or not source.is_dir():
        # The checkout's own folder, replaced by a link, would have the copy
        # read wherever the link points.
        raise GitError("the agent's checkout is not a folder; nothing was pushed")
    try:
        _empty_worktree(dest)
    except OSError as exc:
        raise GitError(
            f"could not clear the publish repository's working tree ({type(exc).__name__})"
        ) from exc

    # The worker's own configuration for this repository. `_git_env` pins the
    # global config to /dev/null, but git still reads its default attributes
    # and ignore files from $HOME/.config/git -- and HOME is `private/`,
    # which shares a uid with the agent. Pointing both at /dev/null leaves
    # the tree's own `.gitignore` and `.gitattributes` as the only ones read.
    config = git_dir / "config"
    try:
        with config.open("a", encoding="utf-8") as handle:
            handle.write("[core]\n\tattributesFile = /dev/null\n\texcludesFile = /dev/null\n")
        if hidden_names or agent_excludes:
            info = git_dir / "info"
            info.mkdir(exist_ok=True)
            with (info / "exclude").open("ab") as handle:
                if agent_excludes:
                    handle.write(b"\n" + agent_excludes.rstrip(b"\n") + b"\n")
                for name in hidden_names:
                    handle.write(f"/{name}\n".encode("utf-8", "surrogateescape"))
    except OSError as exc:
        raise GitError(
            f"could not write the publish repository's own settings ({type(exc).__name__})"
        ) from exc

    copied = 0
    nested = 0
    hidden = set(hidden_names)
    stack: list[tuple[Path, Path]] = [(source, dest)]
    try:
        while stack:
            here, there = stack.pop()
            with os.scandir(here) as entries:
                listing = list(entries)
            for entry in listing:
                if entry.name == ".git":
                    continue
                if here == source and entry.name in hidden:
                    # The worker's own top-level names: see the docstring.
                    continue
                src = Path(entry.path)
                dst = there / entry.name
                if entry.is_symlink():
                    os.symlink(os.readlink(src), dst)
                    copied += 1
                elif entry.is_dir(follow_symlinks=False):
                    dst.mkdir()
                    copied += 1
                    if os.path.lexists(src / ".git"):
                        # A repository of its own: see the docstring.
                        nested += 1
                        continue
                    stack.append((src, dst))
                elif entry.is_file(follow_symlinks=False):
                    _copy_file(src, dst, entry.stat(follow_symlinks=False).st_mode)
                    copied += 1
    except OSError as exc:
        raise GitError(
            "could not copy the agent's working tree into the publish repository "
            f"({type(exc).__name__}); nothing was pushed"
        ) from exc
    if nested:
        logger.warning(
            "folders holding a repository of their own were published empty",
            count=nested,
        )
    logger.info("agent working tree copied into the publish repository", entries=copied)
    return copied


#: The waits between tries of a push that git's own words said the forge did
#: not answer (`transient_git_failure`; G1, #1225; widened by #2833): 5 s,
#: 10 s, then 20 s, so FOUR tries in all and 35 s asleep at most. One 5 s
#: wait was too short: MEASURED 2026-10-30 (task_982546a3b645480887e3,
#: task_3ed834efdaa14baab798), GitHub answered both tries 500 and the step
#: ended FAILED with its work only in `swarm-work.patch`. Not longer: the
#: publish runs after the agent with the lease held, and the sleeping stays
#: under `max_in_worker_retry_delay_seconds` (45 s), the platform's bound on
#: an in-worker wait (invariant 4), as the clone's waits do
#: (`CLONE_RETRY_WAITS_SECONDS`). Past these the push raises `PushUnreachable`
#: and the attempt ends retryably (`Worker._publish_unreachable`), so the
#: scheduler's retry, with the capacity released, does the longer wait.
PUSH_RETRY_WAITS_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0)
#: Tries of one push in all: the first, and one more after each wait.
PUSH_TRIES = len(PUSH_RETRY_WAITS_SECONDS) + 1


class PushUnreachable(GitTransient):
    """A push the forge did not answer through every try (#2833).

    A 5xx, a timeout or a dropped connection on each of `tries` pushes. A
    `GitError`, so every `except GitError` still catches it; the publish is
    the caller that tells it apart, and fails the ATTEMPT retryably for it
    rather than the step for good: nothing says the next try meets it.
    """


def push_branch(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
    protected: tuple[str, ...] = (),
    lease: str | None = None,
) -> str:
    """Push HEAD to `refs/heads/<branch>` on `url`. Returns the pushed sha.

    `repo` MUST be a worker-owned publish repository (`prepare_publish_repo`),
    never the clone the agent worked in: this carries the tenant token, and a
    repository whose `.git/config` the agent can write could redirect it
    (`url.*.insteadOf`) or hand it to a helper the agent added. `_TOKEN_SAFE` is
    belt to that suspenders; the guarantee is the repository.

    THREE REFUSALS, none of which the agent can talk its way past:

    * the branch must carry `branch_prefix`. The worker derives the name from
      the task id; the agent never supplies it and this re-checks the derived
      value rather than trusting the caller;
    * the branch must not be one of `protected` -- in practice the repository's
      own default branch, read back from the forge rather than assumed to be
      `main`;
    * the push is never forced. A non-fast-forward means something other than
      this attempt moved the branch, and the correct response to that is to
      report it, not to win. It is raised as `PushRejected`. One mover is
      recognised by the caller, never by a force (#2179): a branch this task
      continues that gained only merges of the default branch -- GitHub's
      "Update branch", the measured case -- takes this attempt's commits
      replayed onto the moved tip (`base_merge_tip`, `replay_onto_tip`), and
      that push is again a plain one, a fast-forward over the fetched tip.
      Since #1821 (owner decision 2026-10-27) any other mover that still
      descends from the clone base is replayed onto the same way
      (`moved_branch_tip`), with no side of a conflict taken from the tip:
      a conflict is still reported, never won.

    `lease` IS THE ONE EXCEPTION, AND IT IS NOT A FORCE (#1438). It is a sha
    the caller has just proved holds only this task's own earlier worker
    commits (`own_branch_tip`); the push is `--force-with-lease` to exactly
    that sha, so the forge replaces the branch only while it still points
    there. Anything that moved it since is refused, as `PushRejected`, as any
    non-fast-forward is. The default None pushes as above.

    A PUSH THE FORGE DID NOT ANSWER IS TRIED AGAIN, WITH BACKOFF (G1, #1225;
    #2833). git says a reset connection or a 5xx only as text on stderr, so
    the text is read with the clone's classifier (`transient_git_failure`): a
    refusal, a non-fast-forward, a missing repository or refused
    authentication is raised at once, as before. A transient failure is tried
    again after each of `PUSH_RETRY_WAITS_SECONDS`, and one that outlasts
    them all is raised as `PushUnreachable`. Pushing again is safe because the
    push is never forced: a try that landed before the connection dropped
    makes the next one a no-op.
    """
    branch = validate_ref(branch) or ""
    if not branch:
        raise GitError("refusing to push without a branch name")
    if not branch.startswith(branch_prefix):
        raise GitError(f"refusing to push to {branch!r}: outside {branch_prefix!r}")
    if branch in protected:
        raise GitError(f"refusing to push to the protected branch {branch!r}")
    if lease is not None and not _FULL_SHA_RE.match(lease):
        raise GitError("refusing a leased push without the full sha it replaces")
    # Never a bare `--force`: with a lease the forge compares the branch with
    # the sha the caller verified, inside the push itself.
    lease_args = [f"--force-with-lease=refs/heads/{branch}:{lease}"] if lease else []

    url = validate_repository_url(url)
    repo = Path(repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    cred_file = _write_credentials(url, token, private_dir)
    # `_TOKEN_SAFE` first (its trailing `credential.helper=` RESETS the helper
    # list), then the worker's own helper -- so the worker's is the only helper
    # git can consult for this token. This runs in a worker-owned publish repo.
    # No helper at all for a host the token may not go to (#307).
    config_args = [*_TOKEN_SAFE]
    if cred_file is not None:
        config_args += ["-c", f"credential.helper=store --file={cred_file}"]
    try:
        for attempt in range(1, PUSH_TRIES + 1):
            # `--no-follow-tags`: the refspec names one branch, but
            # `push.followTags` in the clone's config -- the agent's to set --
            # would push every annotated tag reachable from it as well, with a
            # tagger and a message the agent chose. A swarm attempt has no tag
            # to publish.
            code, status = _git_text(
                [
                    git_binary, *config_args, "push", "--no-verify", "--no-follow-tags",
                    "--porcelain", *lease_args, "--", url, f"HEAD:refs/heads/{branch}",
                ],
                repo=repo,
                private_dir=private_dir,
                logs_dir=logs_dir,
                slug="publish-push",
                timeout_seconds=timeout_seconds,
                logger=logger,
            )
            if code == 0:
                break
            tail = ""
            err = logs_dir / "git-publish-push.err.log"
            if err.exists():
                tail = err.read_text(errors="replace")[-1200:].strip()
            message = f"push failed with exit {code}{_withheld_note(url, token)}: {tail}"
            if _PUSH_REJECTED_RE.search(f"{status}\n{tail}"):
                raise PushRejected(message)
            if not transient_git_failure(tail):
                raise GitError(message)
            if attempt >= PUSH_TRIES:
                raise PushUnreachable(
                    f"{message} (the forge did not answer {attempt} tries)", tries=attempt
                )
            wait = PUSH_RETRY_WAITS_SECONDS[attempt - 1]
            logger.warning(
                "the push could not reach the forge; trying it again",
                attempt=attempt,
                attempts=PUSH_TRIES,
                wait_seconds=wait,
                exit_code=code,
            )
            time.sleep(wait)
    finally:
        # Same discipline as the clone: the credential exists for the length of
        # one git invocation and no longer. The runner has already exited by
        # the time this runs, but a park can bring another one back into the
        # same workspace, so "nobody is looking right now" is not a guarantee.
        try:
            if cred_file is not None:
                cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))

    code, text = _git_text(
        [git_binary, *_NO_HOOKS, "rev-parse", "HEAD"],
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-pushed-head",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    head = text.strip() if code == 0 else ""
    logger.info("branch pushed", branch=branch, head=head or "unknown")
    return head


#: git's words for a push the forge refused because the branch is not an
#: ancestor of HEAD: the porcelain status line (`[rejected] (fetch first)`,
#: `(non-fast-forward)`, `(stale info)` for a lease that no longer holds) and
#: the hint on stderr the 2026-10-16 retries logged ("Updates were rejected
#: because the remote contains work that you do not have locally"). A
#: refusal for any other reason -- a protected branch rule, a hook, refused
#: authentication -- stays a plain `GitError`.
_PUSH_REJECTED_RE = re.compile(
    r"\[rejected\]|non-fast-forward|\(fetch first\)|\(stale info\)"
    r"|remote contains work that you do\s+not have locally"
)

#: The line `Worker._worker_commit_message` ends every commit the worker makes
#: with. A commit carrying it for ANOTHER task's id was not made for this one,
#: whoever's identity it carries.
_MADE_FOR_TASK_RE = re.compile(r"Made by the worker for task (\S+?)\.?$", re.MULTILINE)


def own_branch_tip(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    task_id: str,
    base: str | None,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
    also_own: Sequence[tuple[str, str]] = (),
) -> tuple[str | None, str]:
    """The tip of this task's own branch on `url`, when only its worker wrote it.

    Returns `(sha, "")` when `branch` is exactly `<branch_prefix><task_id>`,
    stands on the clone `base`, and every commit its first-parent line adds
    over `base` is one the worker wrote (`_foreign` finds nothing, the check
    `verify_worker_authorship` runs before every push) and names no other
    task. Otherwise `(None, why)`, and the caller fails as it always did.

    WHY (#1438, 2026-10-16): attempt 1 pushed `swarm/task_<id>` and crashed
    before its terminal write; the reclaimed task's attempt 2 published from
    a fresh publish repository at the same base, so its push was a
    non-fast-forward against its own first attempt's commit, and the task
    failed with its work on the forge and no pull request. A branch whose
    name is this task's and whose commits are all this task's worker's was
    written by no one else, so replacing it loses nobody's work.

    The first-parent line, as `verify_worker_authorship` reads it: an
    integrator's merge commits are on it, and the contributor commits they
    bring in were checked by their own workers when they pushed. `repo` MUST
    be the worker-owned publish repository: the fetch carries the token.
    """
    if branch != f"{branch_prefix}{task_id}":
        return None, f"{branch!r} is not this task's own branch"
    tip = fetch_branch_tip(
        repo=repo, url=url, branch=branch, token=token, private_dir=private_dir,
        logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
        git_binary=git_binary, branch_prefix=branch_prefix,
    )
    if tip is None:
        return None, f"could not fetch {branch} to read what it holds"
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if base == EMPTY_CLONE_BASE:
        span = tip
    elif base and _FULL_SHA_RE.match(base.strip()):
        base = base.strip()
        code, _ = run([*g, "merge-base", "--is-ancestor", base, tip], "own-branch-ancestor")
        if code != 0:
            return None, f"{branch} at {tip[:12]} does not stand on the clone base {base[:12]}"
        span = f"{base}..{tip}"
    else:
        return None, "the clone base is unknown, so the branch's commits cannot be told apart"
    why = _not_this_tasks_worker(
        run, g, span=span, task_id=task_id, author_name=author_name,
        author_email=author_email, also_own=also_own, git_binary=git_binary,
    )
    if why:
        return None, f"{why} on {branch}"
    return tip, ""


def _not_this_tasks_worker(
    run: Callable[[list[str], str], tuple[int, str]],
    g: list[str],
    *,
    span: str,
    task_id: str,
    author_name: str,
    author_email: str,
    also_own: Sequence[tuple[str, str]],
    git_binary: str,
    must_name: bool = False,
) -> str:
    """Why a commit on `span`'s first-parent line is not this task's worker's; "" when all are.

    The one predicate `own_branch_tip` and `own_moved_head` share: written
    whole by one of the worker's identities (`_foreign`, the check
    `verify_worker_authorship` runs before every push) and naming no other
    task. `must_name` also requires the task's own "Made by the worker for
    task <id>." line, for a branch other tasks' workers write too.
    """
    code, text = run([*g, "rev-list", "--first-parent", span], "own-branch-list")
    if code != 0:
        return "could not list the commits"
    author, committer = _worker_idents(run, g)
    also = [
        _worker_idents(run, [git_binary, *_NO_HOOKS, *_worker_identity(name, email)])
        for name, email in also_own
        if (name, email) != (author_name, author_email)
    ]
    for sha in text.split():
        commit = _read_commit(run, g, sha)
        problems = _foreign(commit, author, committer, also=also)
        found = _MADE_FOR_TASK_RE.findall(commit.message)
        named = [other for other in found if other != task_id]
        if named:
            problems.append(f"it was made for task {named[0]}")
        elif must_name and task_id not in found:
            problems.append(f"it does not name task {task_id}")
        if problems:
            return f"commit {sha[:12]} is not this task's worker's: {'; '.join(problems)}"
    return ""


def own_moved_head(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    task_id: str,
    started: str | None,
    now: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
    also_own: Sequence[tuple[str, str]] = (),
) -> tuple[bool, str]:
    """Whether `branch` moved from `started` to `now` only by this task's own worker commits.

    `(True, "")` when the fetched tip is `now`, `started` is under it, and
    every first-parent commit `started..now` adds was written by this
    task's worker (`_not_this_tasks_worker`) AND names this task: a pull
    request's branch carries other tasks' worker commits, so naming no
    other task is not enough there. Otherwise `(False, why)`.

    WHY (#2693, measured): task_ffc37f64's generation 1, fenced, pushed onto
    PR #2613 (a273bb75 -> 7eb64482), and generation 5 -- holding the same
    work on a273bb75 -- refused its own publish `head_moved`. A head moved
    only by this task's own earlier generation was moved by nobody else.

    `repo` MUST be the worker-owned publish repository: the fetch carries
    the token (`fetch_branch_tip`).
    """
    started = (started or "").strip()
    if not _FULL_SHA_RE.match(started):
        return False, "the head this step started from is unknown"
    tip = fetch_branch_tip(
        repo=repo, url=url, branch=branch, token=token, private_dir=private_dir,
        logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
        git_binary=git_binary, branch_prefix=branch_prefix,
    )
    if tip is None:
        return False, f"could not fetch {branch} to read what moved it"
    if tip != now:
        return False, f"{branch} is at {tip[:12]}, not the head the forge reported {now[:12]}"
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    code, _ = run([*g, "merge-base", "--is-ancestor", started, tip], "own-moved-ancestor")
    if code != 0:
        return False, f"{branch} was rewritten: {started[:12]} is not under {tip[:12]}"
    why = _not_this_tasks_worker(
        run, g, span=f"{started}..{tip}", task_id=task_id, author_name=author_name,
        author_email=author_email, also_own=also_own, git_binary=git_binary, must_name=True,
    )
    return (False, why) if why else (True, "")


#: The most default-branch merges `base_merge_tip` walks down from a moved
#: tip. The merge step's bound on GitHub's updates of a pull request's head
#: (`merge.MERGE_MAX_HEAD_UPDATES`), restated because merge.py imports this
#: module; a test holds the two together. A longer line is not an update, it
#: is a branch someone keeps writing to.
BASE_MERGE_MAX_STEPS = 12


def base_merge_tip(
    *,
    repo: Path,
    url: str,
    branch: str,
    default_branch: str,
    token: str,
    base: str | None,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
    max_steps: int = BASE_MERGE_MAX_STEPS,
) -> tuple[str | None, str]:
    """The moved tip of `branch` on `url`, when only merges of the default branch moved it.

    Returns `(sha, "")` when every commit on the fetched tip's first-parent
    line down to the clone `base` is a two-parent merge whose second parent
    is reachable from the fetched `default_branch`, within `max_steps` of
    them. Otherwise `(None, why)`, and the caller fails as it always did.

    WHY (#2179, measured 2026-10-23): a CI-fix round for PR #2093 cloned the
    pull request's branch at bfdc51ad; GitHub's "Update branch" then put a
    merge of main on it (c903caca, parents bfdc51ad and main's 3c6bba9b), and
    the round's push, never forced, was refused as a non-fast-forward. Such a
    merge adds only what the default branch already published, so the
    round's commits can go on top of it and the push stays a fast-forward.
    This is the walk the merge step makes over GitHub's own updates
    (`merge._updates_onto`), read from git rather than the forge API.

    `repo` MUST be the worker-owned publish repository: both fetches carry
    the token, each with the credential written for that one invocation
    (`fetch_branch_tip`). `branch` must carry `branch_prefix`; the default
    branch is only read.
    """
    branch = validate_ref(branch) or ""
    if not branch or not branch.startswith(branch_prefix):
        raise GitError(f"refusing to fetch {branch!r}: outside {branch_prefix!r}")
    default_branch = validate_ref(default_branch) or ""
    if not default_branch:
        return None, "the repository's default branch is unknown"
    base = (base or "").strip()
    if not _FULL_SHA_RE.match(base):
        return None, "the clone base is unknown, so what moved the branch cannot be read"
    common = dict(
        repo=repo, url=url, token=token, private_dir=private_dir, logs_dir=logs_dir,
        timeout_seconds=timeout_seconds, logger=logger, git_binary=git_binary,
    )
    tip = fetch_branch_tip(branch=branch, branch_prefix=branch_prefix, **common)
    if tip is None:
        return None, f"could not fetch {branch} to read what moved it"
    if tip == base:
        return None, f"{branch} has not moved off the clone base {base[:12]}"
    default_tip = fetch_branch_tip(branch=default_branch, branch_prefix="", **common)
    if default_tip is None:
        return None, f"could not fetch {default_branch} to read what moved {branch}"
    g = [git_binary, *_NO_HOOKS]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    head = tip
    steps = 0
    while head != base:
        if steps >= max_steps:
            return None, f"{branch} is more than {max_steps} merges past the clone base"
        code, text = run([*g, "rev-list", "--parents", "--max-count=1", head], "base-merge-parents")
        parents = text.split()[1:] if code == 0 else []
        if len(parents) != 2:
            return None, f"commit {head[:12]} on {branch} is not a merge of {default_branch}"
        code, _ = run(
            [*g, "merge-base", "--is-ancestor", parents[1], default_tip], "base-merge-on-default"
        )
        if code != 0:
            return None, (
                f"merge {head[:12]} on {branch} brings in {parents[1][:12]}, which is not "
                f"on {default_branch}"
            )
        head = parents[0]
        steps += 1
    return tip, ""


def moved_branch_tip(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    base: str | None,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
) -> tuple[str | None, str]:
    """The moved tip of `branch` on `url`, when it still descends from the clone `base`.

    Returns `(sha, "")` when the fetched tip differs from `base` and `base` is
    its ancestor: someone added commits on top of what this attempt cloned.
    Otherwise `(None, why)` -- the branch could not be fetched, has not
    moved, or was rewritten so the clone base is no longer under it -- and
    the caller fails as it always did.

    WHY (#1821, measured 2026-10-21): 14 of 69 ci-fix rounds in 48 h lost
    their fix to a push refused because another fix round, a conflict fix or
    the merge step's update moved the pull request's branch first. The owner
    decided (2026-10-27) that the worker replays the round's commits onto
    such a tip (`replay_onto_tip`, with no tip-side resolution) and pushes
    once as a fast-forward. A rewritten branch is not such a tip: whoever
    rewrote it meant to drop what was there, and a fix stacked onto it was
    never seen against that intent, so it is refused.

    `repo` MUST be the worker-owned publish repository: the fetch carries the
    token (`fetch_branch_tip`). `branch` must carry `branch_prefix`.
    """
    branch = validate_ref(branch) or ""
    if not branch or not branch.startswith(branch_prefix):
        raise GitError(f"refusing to fetch {branch!r}: outside {branch_prefix!r}")
    base = (base or "").strip()
    if not _FULL_SHA_RE.match(base):
        return None, "the clone base is unknown, so what moved the branch cannot be read"
    tip = fetch_branch_tip(
        repo=repo, url=url, branch=branch, token=token, private_dir=private_dir,
        logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
        git_binary=git_binary, branch_prefix=branch_prefix,
    )
    if tip is None:
        return None, f"could not fetch {branch} to read what moved it"
    if tip == base:
        return None, f"{branch} has not moved off the clone base {base[:12]}"
    code, _ = _git_text(
        [git_binary, *_NO_HOOKS, "merge-base", "--is-ancestor", base, tip],
        repo=Path(repo),
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="moved-tip-descends",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if code != 0:
        return None, (
            f"{branch} was rewritten: the clone base {base[:12]} is not under its tip "
            f"{tip[:12]}"
        )
    return tip, ""


def base_merge_changed_paths(
    *,
    repo: Path,
    base: str,
    tip: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> frozenset[str]:
    """The paths whose content differs between the clone `base` and the moved
    `tip` -- what the base merges `base_merge_tip` accepted changed (#2367).

    Empty when the diff cannot be read: `replay_onto_tip` then takes no
    conflict as superseded, and fails as it did before.
    """
    code, text = _git_text(
        [git_binary, *_NO_HOOKS, "diff", "--name-only", "-z", base, tip],
        repo=Path(repo),
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="replay-base-merge-paths",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if code != 0:
        return frozenset()
    return frozenset(path for path in text.split("\0") if path)


def replay_onto_tip(
    *,
    repo: Path,
    base: str,
    tip: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    superseded: list[str] | None = None,
    take_base_paths: bool = True,
) -> str | None:
    """Replay HEAD's first-parent commits since `base` onto `tip`, as the worker's.

    Returns the new HEAD; None when the replay adds nothing over `tip` (it
    already holds every change), with HEAD left on `tip`. Raises GitError on
    a conflict outside the base merge's paths, or on a merge commit in the
    span, after putting HEAD and the working tree back where they were.

    For a continued branch moved only by merges of the default branch
    (`base_merge_tip`, #2179): each commit is cherry-picked onto the tip
    without committing, and the resulting tree is committed with
    `commit-tree` under `_worker_identity`, keeping the commit's message --
    already the worker's (`replay_agent_commits` or the fold ran). A commit
    whose change the tip already holds is dropped. Nothing here touches the
    remote; `repo` is the worker-owned publish repository.

    A two-parent merge in the span (a conflict round's merge of the default
    branch) is dropped when its second parent is an ancestor of the tip and
    applying it against its first parent (`cherry-pick -m 1`) leaves the
    tip's tree as it is, and a real merge of it into the tip
    (`merge-tree --write-tree`) is clean and leaves the tip's tree too -- the
    second check catches a hunk the round resolved by keeping the branch's
    side, which `-m 1` cannot see: the tip already holds it. Any other merge -- its
    second parent not on the tip, a conflict there, a tree that would differ,
    more than two parents -- is refused, so a different resolution on the tip
    is never silently dropped and no merge is ever rewritten as a linear
    commit. WHY (#2366, measured 2026-10-25): conflict round task_fa6637b0
    made one merge of main while the owner merged main into the same branch by
    hand (0626eadf); the round's merge was refused here and the round ended
    publish_failed although the moved tip already held it.

    SUPERSEDED BY THE BASE (#2367). MEASURED 2026-10-25: four CI-fix rounds
    had hand-patched tests/unit/scripts/unit_test_durations.json, main's
    #2312 rewrote it and was merged into their branches, and each replay
    conflicted on it. When EVERY conflicted path of a pick is one the base
    merge changed (`base_merge_changed_paths`, clone base..tip), the tip's
    version of each is taken -- removed where the tip has none -- and the
    pick goes on. That is safe here and nowhere else: `base_merge_tip` has
    already proved everything between the base and the tip is merges of the
    default branch, so the tip's version is main's own change and the
    round's, written against the older base, is stale by construction; the
    merge wake reads CI again at the new head, so a round whose fix was not
    in fact superseded meets its red check again rather than a silent pass.
    Each such path is appended once to `superseded` when given. A conflict
    in any path the base merge did not change, a merge commit the tip does
    not already hold (above), or any other failure still raises, as before.

    `take_base_paths=False` for a tip moved by anything else
    (`moved_branch_tip`, #1821): what lies between the base and the tip is
    then another writer's work, not the default branch's published change,
    so no side of a conflict is known to be stale and every conflict raises.
    """
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    def sha_of(rev: str, slug: str) -> str:
        code, text = run([*g, "rev-parse", "--verify", "--quiet", rev], slug)
        text = text.strip()
        if code != 0 or not _FULL_SHA_RE.match(text):
            raise GitError(f"could not read {rev}")
        return text

    if not _FULL_SHA_RE.match(base or "") or not _FULL_SHA_RE.match(tip or ""):
        raise GitError("refusing to replay without the full shas of the base and the tip")
    original = sha_of("HEAD", "replay-head")
    code, text = run([*g, "rev-list", "--first-parent", "--reverse", f"{base}..HEAD"], "replay-list")
    if code != 0:
        raise GitError("could not list the commits to replay")
    shas = text.split()

    def restore(why: str) -> GitError:
        run([*g, "cherry-pick", "--quit"], "replay-quit")
        code, _ = run([*g, "reset", "--quiet", "--hard", original], "replay-restore")
        if code != 0:
            return GitError(f"{why}, and the publish repository could not be put back")
        return GitError(why)

    merged_paths: list[frozenset[str]] = []

    def take_tip(sha: str) -> None:
        # A failed pick: superseded only when every unmerged path is one the
        # base merge changed; anything else raises as it always did.
        conflicted = f"replaying commit {sha[:12]} onto {tip[:12]} conflicted"
        code, text = run([*g, "diff", "--name-only", "-z", "--diff-filter=U"], "replay-unmerged")
        unmerged = sorted({path for path in text.split("\0") if path}) if code == 0 else []
        if not take_base_paths:
            raise restore(conflicted)
        if not merged_paths:
            merged_paths.append(base_merge_changed_paths(
                repo=repo, base=base, tip=tip, private_dir=private_dir, logs_dir=logs_dir,
                timeout_seconds=timeout_seconds, logger=logger, git_binary=git_binary,
            ))
        if not unmerged or not set(unmerged) <= merged_paths[0]:
            raise restore(conflicted)
        for path in unmerged:
            code, _ = run([*g, "cat-file", "-e", f"HEAD:{path}"], "replay-tip-has")
            if code == 0:
                code, _ = run([*g, "checkout", "HEAD", "--", path], "replay-take-tip")
                if code == 0:
                    code, _ = run([*g, "add", "--", path], "replay-take-add")
            else:
                code, _ = run([*g, "rm", "--quiet", "-f", "--", path], "replay-take-rm")
            if code != 0:
                raise restore(f"{conflicted}, and the tip's version could not be taken")
        # The pick is resolved; its sequencer state is not needed again.
        run([*g, "cherry-pick", "--quit"], "replay-resolved")
        if superseded is not None:
            superseded.extend(path for path in unmerged if path not in superseded)
        logger.info("took the moved tip's version of paths the base merge changed",
                    commit=sha, tip=tip, paths=len(unmerged))

    code, _ = run([*g, "reset", "--quiet", "--hard", tip], "replay-onto")
    if code != 0:
        raise restore(f"could not move onto {tip[:12]}")
    for sha in shas:
        code, parents = run([*g, "rev-list", "--parents", "--max-count=1", sha], "replay-parents")
        listed = parents.split()
        if code != 0 or len(listed) not in (2, 3):
            raise restore(f"commit {sha[:12]} is a merge and cannot be replayed")
        if len(listed) == 3:
            # A merge the round made of the default branch (#2366): dropped
            # only when its second parent is under the tip and replaying it
            # against its first parent changes nothing there. Never written
            # as a linear commit.
            code, _ = run([*g, "merge-base", "--is-ancestor", listed[2], tip], "replay-merged")
            if code != 0:
                raise restore(
                    f"commit {sha[:12]} is a merge and cannot be replayed: its second parent "
                    f"{listed[2][:12]} is not on the moved tip"
                )
            code, _ = run([*g, "cherry-pick", "-m", "1", "--no-commit", sha], "replay-merge-pick")
            if code != 0:
                raise restore(
                    f"commit {sha[:12]} is a merge and cannot be replayed, and conflicts with "
                    "the moved tip"
                )
            code, tree = run([*g, "write-tree"], "replay-merge-tree")
            if code != 0 or tree.strip() != sha_of("HEAD^{tree}", "replay-head-tree"):
                raise restore(
                    f"commit {sha[:12]} is a merge and cannot be replayed: the moved tip "
                    "resolves it differently"
                )
            # `-m 1` sees only what the merge changed against its first
            # parent, so a hunk the round resolved by keeping the branch's
            # side is invisible to it. A real merge into the tip sees it: a
            # held merge merges cleanly and leaves the tip's tree.
            code, merged = run([*g, "merge-tree", "--write-tree", "HEAD", sha], "replay-merge-held")
            merged_tree = merged.split("\n", 1)[0].strip()
            if code != 0 or merged_tree != sha_of("HEAD^{tree}", "replay-head-tree"):
                raise restore(
                    f"commit {sha[:12]} is a merge and cannot be replayed: the moved tip "
                    "resolves it differently"
                )
            continue
        code, _ = run([*g, "cherry-pick", "--no-commit", sha], "replay-pick")
        if code != 0:
            take_tip(sha)
        code, tree = run([*g, "write-tree"], "replay-tree")
        tree = tree.strip()
        if code != 0 or not _FULL_SHA_RE.match(tree):
            raise restore(f"could not write the tree of commit {sha[:12]}")
        if tree == sha_of("HEAD^{tree}", "replay-head-tree"):
            continue
        code, raw = run([*g, "cat-file", "commit", sha], "replay-read")
        if code != 0:
            raise restore(f"could not read commit {sha[:12]}")
        message = raw.partition("\n\n")[2].replace("\x00", "").strip() or "swarm: replayed commit"
        code, made = run(
            [*g, "commit-tree", tree, "-p", sha_of("HEAD", "replay-parent"), "-m", message],
            "replay-write",
        )
        made = made.strip()
        if code != 0 or not _FULL_SHA_RE.match(made):
            raise restore(f"could not commit the replay of {sha[:12]}")
        code, _ = run([*g, "reset", "--quiet", "--soft", made], "replay-advance")
        if code != 0:
            raise restore("could not move HEAD onto the replayed commit")
    head = sha_of("HEAD", "replay-done")
    if head == tip:
        logger.info("the moved tip already holds every replayed change", tip=tip)
        return None
    logger.info("replayed this attempt's commits onto the moved tip", tip=tip, head=head)
    return head


#: The most commits a pushed branch's record lists (`branch_commits`). A
#: step's branch carries a handful; the cap bounds the result summary, and
#: `commits_truncated` says when it was reached.
BRANCH_COMMITS_CAP = 50
#: The most of a commit subject the record keeps.
BRANCH_SUBJECT_MAX_CHARS = 200


def branch_commits(
    *,
    repo: Path,
    base: str | None,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    cap: int = BRANCH_COMMITS_CAP,
) -> tuple[list[dict[str, str]], bool]:
    """The commits HEAD adds over `base`, newest first: `[{sha, subject}]`, and
    whether `cap` cut the list short.

    Read from git in `repo`, which is the worker-owned publish repository the
    push just ran in, so every sha here is one the push sent (#667, observer
    P11). `base` None -- an empty repository's first push -- lists HEAD's own
    history. A git that fails gives `([], False)`: the record then names the
    branch and its head without commits, never commits it did not read.
    """
    revision = f"{base}..HEAD" if base and _FULL_SHA_RE.match(base) else "HEAD"
    code, text = _git_text(
        [
            git_binary, *_NO_HOOKS, "log", "--no-color", f"--max-count={cap + 1}",
            "--format=%H%x1f%s%x1e", revision, "--",
        ],
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-branch-commits",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if code != 0:
        logger.warning("could not list the pushed branch's commits", exit_code=code)
        return [], False
    commits: list[dict[str, str]] = []
    for record in text.split("\x1e"):
        sha, sep, subject = record.strip("\n").partition("\x1f")
        if sep and _FULL_SHA_RE.match(sha):
            # The subject is the agent's text: cut, so the record stays small.
            commits.append({"sha": sha, "subject": subject[:BRANCH_SUBJECT_MAX_CHARS]})
    return commits[:cap], len(commits) > cap


@dataclass(frozen=True)
class MergeOutcome:
    """What an integrator managed to bring together, and what it did not.

    `conflicted` and `missing` are NOT errors that abort the integration, and
    that is a deliberate choice rather than leniency. An integrator is the sink
    of a workflow whose other steps have already run, been billed and pushed
    their work; refusing the whole pull request because one of six contributors
    conflicts would throw away five successful attempts and leave the caller
    with nothing to look at. So the merge takes what merges and NAMES what it
    could not take -- in this object, in the run's result, and in the pull
    request body itself, where a human reviewing it cannot miss it.

    The failure this avoids is the opposite one: a pull request that claims to
    integrate a workflow while silently containing a subset of it.
    """

    merged: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.conflicted and not self.missing


def fetch_branch_tip(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
) -> str | None:
    """Fetch `refs/heads/<branch>` from `url` into `repo`; its sha, or None when absent.

    For `carrier: branches` (D13): every push of a step's work to its branch
    is made on top of what the branch already holds, so it is a fast-forward
    and is never forced (`push_branch`). The tip is fetched with full depth so
    a commit can be made on it.

    `repo` MUST be a worker-owned publish repository, as for `merge_branches`:
    the fetch carries the tenant token. The branch must carry `branch_prefix`,
    the rule every token-bearing ref in this module is held to.
    """
    branch = validate_ref(branch) or ""
    if not branch or not branch.startswith(branch_prefix):
        raise GitError(f"refusing to fetch {branch!r}: outside {branch_prefix!r}")
    url = validate_repository_url(url)
    repo = Path(repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    cred_file = _write_credentials(url, token, private_dir)
    # As in `push_branch`: no helper at all for a host the token may not go to
    # (#307), so the fetch runs without a credential rather than not at all.
    config_args = [*_TOKEN_SAFE]
    if cred_file is not None:
        config_args += ["-c", f"credential.helper=store --file={cred_file}"]
    slug = "carrier-fetch"
    try:
        code, _ = _git_text(
            [
                git_binary, *config_args, "fetch", "--no-tags", "--depth=2147483647",
                "--", url, f"refs/heads/{branch}",
            ],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
    finally:
        try:
            if cred_file is not None:
                cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))
    if code != 0:
        # Absent: no checkpoint of this step has pushed yet. A fetch that failed
        # for another reason surfaces at the push, which is refused unless it
        # fast-forwards.
        return None
    code, text = _git_text(
        [git_binary, *_NO_HOOKS, "rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"],
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="carrier-fetch-head",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    sha = text.strip()
    return sha if code == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def commit_tree_onto(
    *,
    repo: Path,
    parent: str,
    message: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> str | None:
    """Commit HEAD's tree as ONE worker commit on `parent`, and move HEAD there.

    Returns the new commit, or None when `parent` already has HEAD's tree and
    there is nothing to add. The commit is the worker's, made with
    `_worker_identity` (#219's rule that the worker writes every commit it
    pushes), so `verify_worker_authorship` passes it. `repo` is a worker-owned
    publish repository; the index and working tree are not touched.

    For `carrier: branches` (D13): each push of a step's work is this commit
    on the branch's current tip, so the branch only ever fast-forwards, push
    after push, across checkpoints and attempts. A replay of the agent's
    commits (`lifecycle.replay_agent_commits`) writes new shas every time it
    runs, so a second replay of the same work would not descend from the
    first one pushed, and the push, never forced, would be refused.
    """
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    code, tree = run([*g, "rev-parse", "--verify", "HEAD^{tree}"], "carrier-tree")
    tree = tree.strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise GitError("could not read the tree of the work to push")
    code, parent_tree = run([*g, "rev-parse", "--verify", f"{parent}^{{tree}}"], "carrier-parent")
    if code != 0:
        raise GitError(f"could not read the tree of {parent[:12]}")
    if parent_tree.strip() == tree:
        code, _ = run([*g, "reset", "--soft", parent], "carrier-reset")
        if code != 0:
            raise GitError("could not move the branch onto its pushed tip")
        return None
    code, made = run([*g, "commit-tree", tree, "-p", parent, "-m", message], "carrier-commit")
    made = made.strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", made):
        raise GitError("could not commit the work onto the branch's tip")
    code, _ = run([*g, "reset", "--soft", made], "carrier-reset")
    if code != 0:
        raise GitError("could not move the branch onto the worker's commit")
    return made


def merge_branches(
    *,
    repo: Path,
    url: str,
    branches: Sequence[str],
    token: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    author_name: str,
    author_email: str,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
) -> MergeOutcome:
    """Merge each contributor branch into the current HEAD, in the order given.

    `repo` MUST be a worker-owned publish repository (`prepare_publish_repo`):
    the contributor `fetch` below carries the tenant token, so it needs the same
    isolation as the push -- run in the clone, an `insteadOf` the agent wrote
    would redirect the authenticated fetch to another host.

    ORDER IS THE CALLER'S, AND IT MATTERS. swarm-api builds `integrates` from
    the topological prefix of the workflow, so branch N may depend on branch
    N-1 having landed. Merging in any other order turns a clean sequence into
    an artificial conflict.

    `--no-ff` on every merge, so the history shows which contributor each
    change came from even when the merge could have fast-forwarded. An
    integrator's whole purpose is attribution across steps; collapsing that is
    the one thing it must not do.

    A conflicted merge is aborted with `git merge --abort` before the next one
    is attempted, so a failed merge never leaks a half-applied index into the
    branch that follows it.
    """
    repo = Path(repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    url = validate_repository_url(url)

    merged: list[str] = []
    conflicted: list[str] = []
    missing: list[str] = []

    cred_file = _write_credentials(url, token, private_dir)
    # As in `push_branch`: `_TOKEN_SAFE` resets the credential-helper list before
    # the worker's own helper is added, and this runs in a worker-owned publish
    # repository -- the contributor fetch below carries the token. No helper at
    # all for a host the token may not go to (#307).
    credential_args = (
        ["-c", f"credential.helper=store --file={cred_file}"] if cred_file is not None else []
    )
    config_args = [
        *_TOKEN_SAFE,
        *credential_args,
        # The merge commits are the worker's, not the agent's. Without these
        # git refuses to commit at all in a container with no global config,
        # and the merge fails for a reason that reads like a conflict. All six
        # keys, not `user.*` alone: see `_worker_identity`.
        *_worker_identity(author_name, author_email),
    ]

    try:
        for raw in branches:
            branch = validate_ref(raw) or ""
            if not branch:
                missing.append(str(raw))
                continue
            # Re-checked here rather than trusted from the dispatch block: the
            # names arrive through task metadata, and the same prefix rule that
            # governs what this worker may PUSH governs what it may pull into a
            # branch it is about to push.
            if not branch.startswith(branch_prefix):
                logger.warning(
                    "refusing to merge a branch outside the prefix",
                    branch=branch,
                    prefix=branch_prefix,
                )
                missing.append(branch)
                continue

            slug = branch.replace("/", "-")
            code, _ = _git_text(
                [
                    git_binary, *config_args, "fetch", "--no-tags", "--depth=2147483647",
                    "--", url, f"refs/heads/{branch}",
                ],
                repo=repo,
                private_dir=private_dir,
                logs_dir=logs_dir,
                slug=f"integrate-fetch-{slug}",
                timeout_seconds=timeout_seconds,
                logger=logger,
            )
            if code != 0:
                # The contributor never pushed, or pushed under another name.
                # Absent, not conflicting -- the distinction is what tells a
                # reviewer whether to re-run a step or resolve a conflict.
                logger.warning("contributor branch not found on the remote", branch=branch)
                missing.append(branch)
                continue

            code, _ = _git_text(
                [
                    git_binary, *config_args, "merge", "--no-ff", "--no-edit",
                    "-m", f"swarm: integrate {branch}",
                    "FETCH_HEAD",
                ],
                repo=repo,
                private_dir=private_dir,
                logs_dir=logs_dir,
                slug=f"integrate-merge-{slug}",
                timeout_seconds=timeout_seconds,
                logger=logger,
            )
            if code != 0:
                abort_code, _ = _git_text(
                    [git_binary, *_NO_HOOKS, "merge", "--abort"],
                    repo=repo,
                    private_dir=private_dir,
                    logs_dir=logs_dir,
                    slug=f"integrate-abort-{slug}",
                    timeout_seconds=timeout_seconds,
                    logger=logger,
                )
                if abort_code != 0:
                    # An abort that fails leaves the tree in a state the next
                    # merge would silently build on. Stop rather than produce a
                    # pull request nobody can reason about.
                    raise GitError(
                        f"merging {branch} failed and `git merge --abort` then failed "
                        f"with exit {abort_code}; the working tree is mid-merge and "
                        "this integration cannot continue safely"
                    )
                logger.warning("contributor branch conflicts", branch=branch)
                conflicted.append(branch)
                continue

            merged.append(branch)
    finally:
        try:
            if cred_file is not None:
                cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))

    logger.info(
        "integration merge complete",
        merged=len(merged),
        conflicted=len(conflicted),
        missing=len(missing),
    )
    return MergeOutcome(
        merged=tuple(merged), conflicted=tuple(conflicted), missing=tuple(missing)
    )
