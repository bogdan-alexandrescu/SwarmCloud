"""The attempt graph ticket, the worker's half (docs/design/graph-service.md
§3.1, §4.1 steps 2-3, §10; lane GS-WORKER, part of #1384).

swarm-api mints the ticket (`POST /v1/attempts/{attempt_id}/graph-ticket`,
`swarm_api.graph_ticket`) for the worker, proven by the attempt key in this
heap under the route's own purpose, GRAPH_TICKET_PURPOSE. The body names only
the attempt and `index_sha`, the commit the worker's ancestry proof pinned;
the tenant's registration and the manifest digest come from swarm-api's own
records, never from here. The pod's shim (`graphmcp.shim`) and the context
pack (`contextpack.build.HttpService`) present the ticket to the `swarm-graph`
service on every call.

CUSTODY (owner decision Q1, 2026-10-15). The ticket is written to
`private/graph-ticket`, mode 0600, by write-to-a-temporary-then-rename, so the
shim's stat sees either the old file or the new one, never half of one.
`private/` is never checkpointed and never uploaded (`workspace.py`); the
agent runs as the same uid and could read it, which `jevkey.py` records for
the TypeSafe key too. A leaked ticket grants only the read of this
workspace's graph the agent already has, for at most MAX_TICKET_SECONDS, and
only from inside the cluster.

RENEWAL. When a third of the ticket's life is left (`egress_ticket.py`'s
RENEW_FRACTION), on the lifecycle's control poll. A transient failure (no
answer, a 5xx, a 429) is asked again after 5 s, doubling to a 60 s cap, and
the ticket in hand is left where it is: it still works until its `exp`.

THE FENCE (invariant 5). swarm-api answers 409 when the task is not RUNNING
under this attempt's live lease at this generation. That is
`GraphTicketFenced`, at the first mint and at every renewal, and the
lifecycle exits fenced without touching the lease, as it does for
`SecretReleaseFenced` and `ChildSubmitFenced`. A stale worker therefore
cannot renew: its last ticket lives for at most MAX_TICKET_SECONDS (§3.2).

THE STORED SETTING (invariant 10). A 403 at the first mint is
`GraphTicketForbidden`: swarm-api's code-intelligence setting says no graph
for this repository (or the proof did not verify, which is no ticket
either). `graphstage` then registers no server and stages nothing, rather
than handing the agent through a snapshot what the setting refused. Any
other failure at the first mint is a reason `graphstage` falls back to
staging with (§8.2).

ADDITIONAL REPOSITORIES (docs/design/multi-repository-steps.md §4.9, lane
MR-GRAPH, part of #1434). A step that reads additional repositories gets one
more ticket per indexed one, each its own upkeep (`GraphTicket.for_repository`):
the body adds that entry's `repo_id`, which swarm-api accepts only from the
task's signed list, and the file is `private/graph-ticket-<name>`. The
primary's body and file are unchanged. The lifecycle's poll asks the
primary's `due` and `keep`, and those cover every additional upkeep too, so
a 409 at any renewal is the fence for the whole attempt.

NOTHING HERE LOGS a ticket, a signature or a key. Log lines carry codes.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from swarm_common.models import ADDITIONAL_REPOSITORY_NAME

#: `swarm_api.graph_ticket.TICKET_PURPOSE`, restated (this image does not carry
#: swarm-api); tests/unit/worker/test_graph_ticket_worker.py holds them equal.
GRAPH_TICKET_PURPOSE = "swarm-graph-ticket-request/v1"
#: `swarm_api.graph_ticket.GraphTicketRequest`'s fields, exactly: a repository,
#: a tenant registration or a digest in the body is swarm-api's 422.
REQUEST_FIELDS = frozenset({"tenant_id", "task_id", "attempt_id", "lease_id", "generation",
                            "index_sha"})
#: `swarm_api.graph_ticket.AdditionalGraphTicketRequest`'s fields: the
#: primary's plus the `repo_id` of one of the task's signed additional
#: repositories (§4.9). Sent only for one of those.
ADDITIONAL_REQUEST_FIELDS = REQUEST_FIELDS | {"repo_id"}
_REPO_ID = re.compile(r"repo_[0-9a-f]{16}")
#: `swarm_api.graph_ticket.MAX_TICKET_SECONDS`: no answer claiming more life is
#: believed, so a misconfigured setting cannot push renewal out of reach.
MAX_TICKET_SECONDS = 900

#: The file in `private/` (§4.1 step 2, owner Q1).
TICKET_FILE_NAME = "graph-ticket"
#: `graphmcp.shim.MAX_TICKET_BYTES`: the shim refuses to send anything longer,
#: so nothing longer is written.
MAX_TICKET_BYTES = 4096
_TICKET = re.compile(r"[A-Za-z0-9_\-.]+")
_SHA = re.compile(r"[0-9a-f]{40}")

#: Renew once this fraction of the ticket's life is left (`egress_ticket`'s):
#: a 600 s ticket is renewed at 200 s left, several control polls and a few
#: backoffs before the shim would see a refusal.
RENEW_FRACTION = 1 / 3
#: A transient failure is tried again after this, doubling to the max (§4.1).
RETRY_SECONDS = 5.0
RETRY_MAX_SECONDS = 60.0

#: §4.1 step 3: one `initialize` with the ticket, answered within this.
PROBE_TIMEOUT_SECONDS = 2.0

#: Why there is no ticket to ask for: the attempt registered no key.
NO_ATTEMPT_KEY = "no_attempt_key"


class GraphTicketFenced(Exception):
    """409: this attempt is superseded (invariant 5). The message is a code."""


class GraphTicketForbidden(Exception):
    """403 at the first mint: no graph for this attempt, staged or served."""


class TicketUnavailable(Exception):
    """No ticket this time: `cause` is a word, `retryable` whether asking again may help."""

    def __init__(self, cause: str, *, retryable: bool) -> None:
        super().__init__(cause)
        self.cause = cause
        self.retryable = retryable


def ticket_file_name(name: str | None) -> str:
    """`graph-ticket` for the task's own repository, `graph-ticket-<name>` for
    the additional one checked out at `work/repos/<name>` (§4.9). Raises
    ValueError on a name the contract's ADDITIONAL_REPOSITORY_NAME refuses, so
    no name can step out of `private/`."""
    if name is None:
        return TICKET_FILE_NAME
    if not isinstance(name, str) or not ADDITIONAL_REPOSITORY_NAME.fullmatch(name):
        raise ValueError("not an additional repository's name")
    return f"{TICKET_FILE_NAME}-{name}"


def mint(cfg: Any, key: Any, api: Any, *, index_sha: str, now: float,
         repo_id: str | None = None) -> tuple[str, int]:
    """Ask swarm-api for this attempt's graph ticket of `index_sha`: `(ticket, expires_in)`.

    `repo_id` names one of the task's signed additional repositories (§4.9);
    None, the task's own, sends exactly the body a worker sent before it.

    Raises GraphTicketFenced on a 409, GraphTicketForbidden on a 403, and
    TicketUnavailable for everything else, the ticket's shape included.
    """
    from .handoff import sign_headers

    fields: dict[str, Any] = {
        "tenant_id": cfg.tenant_id,
        "task_id": cfg.task_id,
        "attempt_id": cfg.attempt_id,
        "lease_id": cfg.lease_id,
        "generation": cfg.generation,
        "index_sha": index_sha,
    }
    if repo_id is not None:
        if not isinstance(repo_id, str) or not _REPO_ID.fullmatch(repo_id):
            raise TicketUnavailable("not_a_repo_id", retryable=False)
        fields["repo_id"] = repo_id
    body = json.dumps(fields).encode("utf-8")
    path = f"/v1/attempts/{quote(cfg.attempt_id, safe='')}/graph-ticket"
    try:
        headers = sign_headers(key, GRAPH_TICKET_PURPOSE, "POST", path, body, now=now)
        _, payload = api.call("POST", path, body=body, headers=headers)
    except Exception as exc:
        status = getattr(exc, "status", None)
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        if status == 409:
            raise GraphTicketFenced(f"graph ticket: 409 {code}") from None
        if status == 403:
            raise GraphTicketForbidden(f"the graph ticket was refused ({code})") from None
        retryable = getattr(exc, "retryable", None)
        raise TicketUnavailable(code, retryable=retryable is not False) from None
    payload = payload if isinstance(payload, Mapping) else {}
    ticket = payload.get("ticket")
    seconds = payload.get("expires_in")
    if not isinstance(ticket, str) or not _TICKET.fullmatch(ticket) \
            or len(ticket) > MAX_TICKET_BYTES:
        raise TicketUnavailable("not_a_ticket", retryable=True)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise TicketUnavailable("not_a_ticket", retryable=True)
    if payload.get("index_sha", index_sha) != index_sha:
        # A ticket for another commit would read another graph than the pin.
        raise TicketUnavailable("another_commit", retryable=False)
    return ticket, min(seconds, MAX_TICKET_SECONDS)


def write_ticket(path: Path, ticket: str) -> None:
    """`ticket` at `path`, mode 0600, by a new temporary renamed into place.

    The temporary is created O_EXCL beside it; `os.replace` swaps a link
    planted at `path` for the file rather than writing through it.
    """
    fd, scratch = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(ticket)
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def _read_ticket(path: Path) -> str | None:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_TICKET_BYTES + 1)
    except OSError:
        return None
    text = raw.decode("ascii", "replace").strip()
    if not text or len(raw) > MAX_TICKET_BYTES or not _TICKET.fullmatch(text):
        return None
    return text


def probe(url: str, ticket_file: Path, *, transport: Callable[..., tuple[int, bytes]] | None = None,
          timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """One `initialize` to the service with the ticket (§4.1 step 3). None when
    it answered; otherwise why not, in a word or two. Never raises.

    `initialize` makes the service verify the ticket and open the pinned
    manifest, so a pass means this attempt's graph is readable there. The
    transport is the shim's: no proxy, no redirect followed.
    """
    from .graphmcp import shim

    ticket = _read_ticket(ticket_file)
    if ticket is None:
        return "no readable ticket"
    send = transport or shim.http_transport
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"capabilities": {},
                                  "clientInfo": {"name": "swarm-worker-probe", "version": "1"}}})
    try:
        status, raw = send(url, body.encode("ascii"), shim.request_headers(ticket), timeout)
    except shim.ServiceUnreachable as exc:
        return f"unreachable: {exc.cause}"
    except Exception as exc:  # noqa: BLE001 -- a probe never fails the attempt
        return f"unreachable: {type(exc).__name__}"
    finally:
        ticket = ""
    if status != 200:
        return f"HTTP {status}"
    try:
        answer = json.loads(raw)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("result"), dict) \
            or "error" in answer:
        return "its answer could not be read"
    return None


class GraphTicket:
    """One attempt's graph ticket upkeep: the first mint and the renewals.

    `start` and `discard` are `graphstage`'s; `due` and `keep` are the
    lifecycle's control poll's. Only `start` and `keep` raise, and only
    GraphTicketFenced (and, from `start`, GraphTicketForbidden).

    `name` and `repo_id` are set on an additional repository's upkeep, made by
    `for_repository`; the primary's `due` and `keep` cover each of those.
    """

    def __init__(self, cfg: Any, *, children: Any, logger: Any,
                 wall: Callable[[], float] = time.time,
                 name: str | None = None, repo_id: str | None = None) -> None:
        self.cfg = cfg
        self.children = children
        self.log = logger
        self._wall = wall
        self.file_name = ticket_file_name(name)
        if (name is None) != (repo_id is None) or \
                (repo_id is not None and not _REPO_ID.fullmatch(repo_id)):
            raise ValueError("an additional repository's upkeep has both a name and a repo_id")
        self.name = name
        self.repo_id = repo_id
        #: Which repository a log line is about: a name from the signed list,
        #: never a URL; nothing for the task's own, whose lines are unchanged.
        self._where: dict[str, str] = {"repository": name} if name else {}
        #: An additional repository's upkeep by its name (§4.9); the primary's only.
        self.additional: dict[str, GraphTicket] = {}
        #: The ticket file once one is written; None before and after `discard`.
        self.path: Path | None = None
        self.index_sha: str | None = None
        self.renewals = 0
        self.retry_at = 0.0
        self._renew_at = 0.0
        self._backoff = RETRY_SECONDS
        #: A permanent refusal at a renewal: nothing more is asked this attempt.
        self._ended = False

    def _credentials(self) -> tuple[Any, Any] | None:
        key = getattr(self.children, "_key", None)
        api = getattr(self.children, "_api", None)
        if key is None or api is None or not getattr(self.children, "keyed", False):
            return None
        return key, api

    def for_repository(self, name: str, repo_id: str) -> "GraphTicket":
        """The upkeep of the additional repository `name` (§4.9), made once.

        Renewed whenever this one's `keep` runs. Raises ValueError for a name
        or repo_id that is not the contract's shape, or on an upkeep that is
        itself an additional repository's.
        """
        if self.name is not None:
            raise ValueError("only the task's own upkeep holds additional ones")
        found = self.additional.get(name)
        if found is not None:
            if found.repo_id != repo_id:
                raise ValueError("that name is another repository's")
            return found
        made = GraphTicket(self.cfg, children=self.children, logger=self.log, wall=self._wall,
                           name=name, repo_id=repo_id)
        self.additional[name] = made
        return made

    def start(self, private_dir: Path, index_sha: str) -> str | None:
        """Mint the first ticket into `private_dir`. None when written, else why not."""
        if not _SHA.fullmatch(index_sha or ""):
            return "no pinned commit"
        found = self._credentials()
        if found is None:
            return NO_ATTEMPT_KEY
        key, api = found
        now = self._wall()
        try:
            ticket, seconds = mint(self.cfg, key, api, index_sha=index_sha, now=now,
                                   repo_id=self.repo_id)
        except TicketUnavailable as failed:
            self.log.warning("graph ticket not minted; the attempt stages instead",
                             code=failed.cause, **self._where)
            return f"the graph ticket was not minted ({failed.cause})"
        self._redact(ticket)
        path = Path(private_dir) / self.file_name
        try:
            write_ticket(path, ticket)
        except OSError as exc:
            self.log.warning("graph ticket not written", error=type(exc).__name__)
            return f"the graph ticket could not be written ({type(exc).__name__})"
        finally:
            ticket = ""
        self.path, self.index_sha = path, index_sha
        self._renewed(now, seconds)
        self.log.info("graph ticket minted", expires_in=seconds, **self._where)
        return None

    def discard(self) -> None:
        """The attempt is not on the service after all: remove the file, renew
        nothing. The task's own upkeep discards every additional one with it."""
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                self.log.warning("graph ticket not removed", error=type(exc).__name__)
        self.path, self.index_sha, self._ended = None, None, True
        for extra in self.additional.values():
            extra.discard()

    def due(self) -> bool:
        """Whether `keep` would ask swarm-api now: a cheap check for every poll.
        True when this ticket or any additional repository's is due."""
        return self._due() or any(extra._due() for extra in self.additional.values())

    def _due(self) -> bool:
        if self.path is None or self.index_sha is None or self._ended:
            return False
        now = self._wall()
        return now >= self._renew_at and now >= self.retry_at

    def keep(self) -> None:
        """Renew the ticket, and each additional repository's, when due.
        Raises only GraphTicketFenced, from any of them."""
        self._keep()
        for extra in self.additional.values():
            extra._keep()

    def _keep(self) -> None:
        if not self._due():
            return
        found = self._credentials()
        if found is None:
            self._ended = True
            return
        key, api = found
        now = self._wall()
        try:
            ticket, seconds = mint(self.cfg, key, api, index_sha=self.index_sha or "", now=now,
                                   repo_id=self.repo_id)
        except GraphTicketForbidden:
            # The setting was turned off mid-attempt, or the proof stopped
            # verifying: the shim says the ticket was refused once it expires.
            self._ended = True
            self.log.warning("graph ticket renewal refused; it is not asked for again",
                             code="forbidden")
            return
        except TicketUnavailable as failed:
            if not failed.retryable:
                self._ended = True
                self.log.warning("graph ticket renewal refused; it is not asked for again",
                                 code=failed.cause)
                return
            self._retry_later(failed.cause)
            return
        self._redact(ticket)
        try:
            write_ticket(self.path, ticket)  # type: ignore[arg-type]
        except OSError as exc:
            self._retry_later(type(exc).__name__)
            return
        finally:
            ticket = ""
        self.renewals += 1
        self._renewed(now, seconds)
        self.log.info("graph ticket renewed", expires_in=seconds, renewals=self.renewals,
                      **self._where)

    def _redact(self, ticket: str) -> None:
        """Mark the ticket unloggable, as `jevkey` marks its key: whatever logs
        it by accident -- an exception's text, a child's output -- is scrubbed."""
        register = getattr(self.log, "register_secret", None)
        if callable(register):
            register(ticket)

    def _renewed(self, now: float, seconds: int) -> None:
        self._renew_at = now + seconds * (1 - RENEW_FRACTION)
        self.retry_at = 0.0
        self._backoff = RETRY_SECONDS

    def _retry_later(self, cause: str) -> None:
        self.retry_at = self._wall() + self._backoff
        self.log.warning("graph ticket not renewed; trying again", code=cause,
                         retry_seconds=int(self._backoff))
        self._backoff = min(self._backoff * 2, RETRY_MAX_SECONDS)


__all__ = [
    "ADDITIONAL_REQUEST_FIELDS", "GRAPH_TICKET_PURPOSE", "MAX_TICKET_SECONDS", "NO_ATTEMPT_KEY",
    "PROBE_TIMEOUT_SECONDS",
    "REQUEST_FIELDS", "TICKET_FILE_NAME", "GraphTicket", "GraphTicketFenced",
    "GraphTicketForbidden", "TicketUnavailable", "mint", "probe", "ticket_file_name",
    "write_ticket",
]
