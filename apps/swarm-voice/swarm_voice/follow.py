"""Voice it's roster follower: one per live session, keeping the list of the
session's agents current (docs/design/voice-it.md §3.12.6, lane V6).

ONE FOLLOWER PER LIVE SESSION, started and stopped through main.py's
follower hook (`Followers`): `RosterFollowers.start` at the session's start,
`RosterFollowers.stop` at its end. A session that holds no grant is not
followed: the follower reads nothing without one.

EVERY 15 SECONDS (`TICK_SECONDS`) IT MAKES TWO READS, whatever the count:

  1. one tag query: `GET /v1/tasks?claude_session=voice:<project>`, the
     agents carrying the project's tag (the platform's follow-ups: a run's
     planner, its lanes, fix rounds, merge steps), the tag the ledger reads;
  2. one batched read of the started ids: every item the session started
     (`RosterFollowers.started`, the act lane's call once it starts
     something) read by id through the existing reads, `GET /v1/tasks/{id}`,
     `/v1/workflows/{id}` and `/v1/runs/{id}`, sent together.

Each read carries `X-Swarm-Voice-Quiet: roster` (`QUIET_HEADER`), so
swarm-api writes its audit row and no `work.step` for it: the tick's reads
never reach the working card (the owner's pick, 12 November 2026;
swarm_api/voice/grant.py A QUIET READ).

It compares each row with what it last wrote and writes ONE `roster.update`
event carrying the changed rows only, through the grant's write route
(`POST /v1/voice/sessions/{session_id}/work {roster: [rows]}`;
swarm_api/voice/grant.py `record_work`). Nothing changed, nothing is
written. A tick whose reads fail writes nothing and marks nothing seen, so
the next tick sends what this one missed; a write that fails is sent again
on the next tick. A first tick finding more than `ROSTER_MAX` rows writes
them `ROSTER_MAX` to an event.

A row is `{kind, id, state, done, total}`: `state` one of queued, running,
waiting (on the owner), done, failed, cancelled (`STATES`, the same table as
swarm_api/voice/roster.py; a test holds them equal); `done`/`total` a
workflow's steps succeeded of its steps. No title: the page reads titles from
`GET /v1/voice/projects/{id}/roster` and by id.

ITS CHANGES ARE TOLD TO THE SESSION'S UPDATE QUEUE (updates.py, the owner's
pick of 12 November 2026): after each write, the rows it wrote, each with the
state it had before, and a run's own phase (CHECKING: it opened its pull
request; DONE; PLANNED: a plan waits for the owner), so the voice says what
started, what finished or failed and what waits on the owner. The first pass
is the baseline: it tells nothing. Telling never waits on the voice.

IT RUNS WHILE THE VOICE IS PAUSED. The pause closes the Live session and
keeps the voice session, its grant and the follower (§3.12.7); nothing here
reads the voice's state. It stops when the session ends: the end route runs
the hook, and a grant swarm-api no longer accepts (401: the session ended or
was abandoned elsewhere) stops it too.

PER-TENANT ISOLATION (invariant 9). Every read and write goes through the
session's grant, so swarm-api runs it as the session's person in the
session's workspace; the follower holds no other credential and no store.
It only reads and writes stream events: nothing here admits, reserves or runs
work. Logs name the session id and an exception's class, never a body.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx

from . import tools_read
from . import updates as vupdates
from .grant import SessionGrant
from .tools_read import ApiRefused, SwarmApi

log = logging.getLogger("swarm_voice.follow")

#: How often a follower reads (§3.12.6: "every 15 seconds").
TICK_SECONDS = 15.0
#: The most tagged agents one tag query reads.
TAG_LIMIT = 200
#: The most rows one `roster.update` carries (swarm_api/voice/workfeed.py ROSTER_MAX).
ROSTER_MAX = 50
#: The project's tag on what the platform starts for it (swarm_api/voice/ledger.py).
TAG_PREFIX = "voice:"
#: A started item's id, as a path names one (swarm_api/voice/workfeed.py ID_RE).
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")

TASKS_ROUTE = "/v1/tasks"
READ_ROUTES: Mapping[str, str] = {
    "agent": "/v1/tasks/{task_id}",
    "workflow": "/v1/workflows/{workflow_id}",
    "run": "/v1/runs/{run_id}",
}
WORK_ROUTE = "/v1/voice/sessions/{session_id}/work"
#: The header that keeps a grant read off the working card (swarm_api/voice/grant.py
#: QUIET_HEADER and QUIET_ROSTER; a test holds them equal).
QUIET_HEADER = "X-Swarm-Voice-Quiet"
QUIET_ROSTER = "roster"

QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED = (
    "queued", "running", "waiting", "done", "failed", "cancelled")
#: The platform's states as the roster names them: swarm_api/voice/roster.py STATES.
STATES: Mapping[str, str] = {
    "SUBMITTED": QUEUED, "QUEUED": QUEUED, "PARKED": QUEUED, "READY": QUEUED,
    "LEASED": RUNNING, "DISPATCHED": RUNNING, "STARTING": RUNNING, "RUNNING": RUNNING,
    "SUCCEEDED": DONE, "FAILED": FAILED, "DEAD_LETTERED": FAILED, "CANCELLED": CANCELLED,
    "UNKNOWN": RUNNING,
    "PLANNING": RUNNING, "APPROVED": RUNNING, "CHECKING": RUNNING, "FIXING": RUNNING,
    "PLANNED": WAITING, "NOT_READY": WAITING,
    "DONE": DONE, "REJECTED": CANCELLED,
}

Row = dict[str, Any]
Sleep = Callable[[float], Awaitable[None]]


class SessionGone(RuntimeError):
    """swarm-api no longer accepts the session's grant: the session ended."""


def row_state(state: Any) -> str | None:
    return STATES.get(str(state or "").upper())


def _row(kind: str, item_id: Any, state: Any, done: int | None = None,
         total: int | None = None) -> Row | None:
    mapped = row_state(state)
    if not isinstance(item_id, str) or not item_id or mapped is None:
        return None
    return {"kind": kind, "id": item_id, "state": mapped, "done": done, "total": total}


class RosterReads(Protocol):
    """The tick's two reads and its one write, as the session's person."""

    async def tagged(self, project_id: str) -> list[Row]: ...

    async def started(self, items: list[tuple[str, str]]) -> list[Row]: ...

    async def write(self, rows: list[Row]) -> None: ...


class RosterApi(SwarmApi):
    """swarm-api through the session grant (tools_read.SwarmApi), plus the
    roster's write through the grant's work route. Every call is marked
    quiet; swarm-api honours it on the reads only."""

    async def _headers(self) -> dict[str, str]:
        return {**await super()._headers(), QUIET_HEADER: QUIET_ROSTER}

    async def write_roster(self, rows: list[Row]) -> None:
        path = WORK_ROUTE.format(session_id=self.session_id)
        await self._call("POST", WORK_ROUTE, path, json_body={"roster": rows})


def _ended(exc: ApiRefused) -> bool:
    return exc.status == 401


class ApiReads:
    """`RosterReads` over swarm-api's existing reads."""

    def __init__(self, api: RosterApi) -> None:
        self._api = api
        #: Each started run's own state as last read (issueruns' states): what
        #: the update queue says of its pull request. Never written to the roster.
        self.phases: dict[tuple[str, str], str] = {}

    async def tagged(self, project_id: str) -> list[Row]:
        try:
            body = await self._api.get(TASKS_ROUTE, TASKS_ROUTE, {
                "claude_session": TAG_PREFIX + project_id, "view": "summary", "limit": TAG_LIMIT})
        except ApiRefused as exc:
            if _ended(exc):
                raise SessionGone(str(exc)) from None
            raise
        tasks = body.get("tasks") if isinstance(body, dict) else None
        rows = [_row("agent", t.get("id"), t.get("state")) for t in tasks or [] if isinstance(t, dict)]
        return [r for r in rows if r is not None]

    async def _one(self, kind: str, item_id: str) -> Row | None:
        route = READ_ROUTES[kind]
        path = route.split("{", 1)[0] + item_id
        try:
            body = await self._api.get(route, path)
        except ApiRefused as exc:
            if _ended(exc):
                raise SessionGone(str(exc)) from None
            if exc.status == 404:
                return None
            raise
        if not isinstance(body, dict):
            return None
        if kind == "agent":
            task = body.get("task") or {}
            return _row(kind, item_id, task.get("state"))
        if kind == "workflow":
            workflow = body.get("workflow") or {}
            steps = workflow.get("steps") or []
            tasks = [t for t in body.get("tasks") or [] if isinstance(t, dict)]
            done = sum(1 for t in tasks if t.get("state") == "SUCCEEDED")
            return _row(kind, item_id, workflow.get("state"), min(done, len(steps)), len(steps))
        state = (body.get("run") or {}).get("state")
        if isinstance(state, str) and state:
            self.phases[(kind, item_id)] = state.upper()
        return _row(kind, item_id, state)

    async def started(self, items: list[tuple[str, str]]) -> list[Row]:
        if not items:
            return []
        found = await asyncio.gather(*(self._one(k, i) for k, i in items))
        return [r for r in found if r is not None]

    async def write(self, rows: list[Row]) -> None:
        try:
            await self._api.write_roster(rows)
        except ApiRefused as exc:
            if _ended(exc):
                raise SessionGone(str(exc)) from None
            raise


@dataclass
class Follower:
    """One live session's follower."""

    voice_id: str
    project_id: str
    reads: RosterReads
    #: Every item the session started, (kind, id), in the order it started them.
    items: list[tuple[str, str]] = field(default_factory=list)
    #: The rows as last written, by (kind, id).
    seen: dict[tuple[str, str], Row] = field(default_factory=dict)
    #: The update board the changes are told to (updates.py); None tells nothing.
    updates: vupdates.Board | None = None
    #: Each run's phase as last told.
    phases_told: dict[tuple[str, str], str] = field(default_factory=dict)
    #: Whether a pass has completed: the first is the baseline, told nothing.
    baseline: bool = False

    def note(self, kind: str, item_id: str) -> None:
        if kind in READ_ROUTES and isinstance(item_id, str) and ID_RE.match(item_id) \
                and (kind, item_id) not in self.items:
            self.items.append((kind, item_id))

    async def tick(self) -> list[Row]:
        """One pass: the two reads, then one event of the changed rows (none
        when nothing changed). The rows written. `SessionGone` ends it."""
        tagged = await self.reads.tagged(self.project_id)
        started = await self.reads.started(list(self.items))
        now: dict[tuple[str, str], Row] = {}
        for row in [*tagged, *started]:
            now[(row["kind"], row["id"])] = row
        changed = [row for key, row in now.items() if self.seen.get(key) != row]
        before = {key: (self.seen[key] or {}).get("state") for key in now if key in self.seen}
        written: list[Row] = []
        for at in range(0, len(changed), ROSTER_MAX):
            part = changed[at:at + ROSTER_MAX]
            await self.reads.write(part)
            for row in part:
                self.seen[(row["kind"], row["id"])] = row
            written += part
        self._tell(now, written, before)
        return written

    def _tell(self, now: dict[tuple[str, str], Row], written: list[Row],
              before: dict[tuple[str, str], Any]) -> None:
        """Tell the update queue what this pass wrote, each row with its state
        before, and each run whose phase moved; the first pass tells nothing."""
        phases = dict(getattr(self.reads, "phases", None) or {})
        told = self.baseline
        self.baseline = True
        changes: list[vupdates.Change] = []
        for row in written:
            key = (row["kind"], row["id"])
            changes.append(vupdates.Change(row["kind"], row["id"], row["state"], before.get(key),
                                           phases.get(key)))
        named = {(c.kind, c.id) for c in changes}
        for key, phase in phases.items():
            if key in now and key not in named and self.phases_told.get(key) != phase:
                state = now[key]["state"]
                changes.append(vupdates.Change(key[0], key[1], state, state, phase))
        self.phases_told.update(phases)
        if told and changes and self.updates is not None:
            self.updates.roster(self.voice_id, changes)

    async def run(self, *, every: float, sleep: Sleep) -> None:
        while True:
            try:
                await self.tick()
            except SessionGone:
                log.info("voice session %s: the roster follower stops: the session ended", self.voice_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed pass is retried on the next
                log.warning("voice session %s: a roster pass failed: %s", self.voice_id, type(exc).__name__)
            await sleep(every)


class RosterFollowers:
    """Every live session's follower, registered on main.py's follower hook."""

    def __init__(self, reads_for: Callable[[SessionGrant], RosterReads], *,
                 every: float = TICK_SECONDS, sleep: Sleep = asyncio.sleep,
                 updates: vupdates.Board | None = vupdates.BOARD) -> None:
        self._reads_for = reads_for
        self._updates = updates
        self._every = every
        self._sleep = sleep
        self._followers: dict[str, Follower] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, voice_id: str) -> Follower | None:
        return self._followers.get(voice_id)

    def running(self, voice_id: str) -> bool:
        task = self._tasks.get(voice_id)
        return task is not None and not task.done()

    async def start(self, voice_id: str, grant: SessionGrant | None) -> None:
        if grant is None or self.running(voice_id):
            return
        follower = Follower(voice_id=voice_id, project_id=grant.project_id,
                            reads=self._reads_for(grant), updates=self._updates)
        self._followers[voice_id] = follower
        self._tasks[voice_id] = asyncio.create_task(
            follower.run(every=self._every, sleep=self._sleep), name=f"roster-{voice_id}")

    def started(self, voice_id: str, kind: str, item_id: str) -> None:
        """The session started `item_id` (an agent, a workflow or a run): read
        by id on every pass from now on."""
        follower = self._followers.get(voice_id)
        if follower is not None:
            follower.note(kind, item_id)

    async def stop(self, voice_id: str) -> None:
        self._followers.pop(voice_id, None)
        task = self._tasks.pop(voice_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_all(self) -> None:
        for voice_id in list(self._tasks):
            await self.stop(voice_id)


def build_roster(http: httpx.AsyncClient) -> RosterFollowers | None:
    """The followers from SWARM_API_URL (and SWARM_API_AUDIENCE, else the
    URL), as the planner reaches swarm-api; None without that address."""
    base = os.environ.get("SWARM_API_URL", "").strip()
    if not base:
        return None
    audience = os.environ.get("SWARM_API_AUDIENCE", "").strip() or base
    tokens = tools_read.google_id_tokens(audience)
    return RosterFollowers(lambda grant: ApiReads(RosterApi(http, base_url=base, id_tokens=tokens,
                                                            grant=grant)))
