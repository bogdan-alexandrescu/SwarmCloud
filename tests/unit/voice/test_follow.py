"""Voice it's roster follower: one per live session keeps the list of the
session's agents current from one tag query and one batched read a tick
(docs/design/voice-it.md §3.12.6, lane V6).

* One tag query and one batched read per tick, whatever the count.
* A changed state is one `roster.update` carrying only the changed rows;
  nothing is written when nothing changed; a failed write is sent again.
* The follower keeps running while the voice is paused and stops when the
  session ends (the end route, or a grant swarm-api no longer accepts).
* Through swarm-api: the tag query is `GET /v1/tasks?claude_session=voice:
  <project>`, each started id is read by its own existing route, and the
  write is the grant's work route, every call carrying the session grant.
* Every call carries the quiet header, so through swarm-api's grant path a
  tick writes its `roster.update` and no `work.step`, and a tick with nothing
  changed adds nothing to the session's stream.
* Its state table is swarm-api's roster table.

Offline: fake reads, a fake swarm-api over httpx.MockTransport, the pause
test's fake Live world; every grant and token built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import follow  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant  # noqa: E402
from swarm_voice.main import Followers, create_app  # noqa: E402
from test_live_pause import (AUDIO, PERSON, PERSON_DELTA, VOICE_FINAL, World, _caller_ok,  # noqa: E402
                             _offer, _said, _until)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.conftest import db, group_map, objects, seed_task, tokens  # noqa: E402,F401
from control_plane.test_voice_grant import VOICE_TOKEN, _build, _start  # noqa: E402
from control_plane.test_voice_sessions import FakeVoice  # noqa: E402

from swarm_api.voice import grant as api_grant  # noqa: E402
from swarm_api.voice import roster as api_roster  # noqa: E402
from swarm_api.voice import sessions as api_sessions  # noqa: E402
from swarm_api.voice import store as api_store  # noqa: E402
from swarm_api.voice import workfeed as vwork  # noqa: E402


def _voice_id() -> str:
    return "vs_" + secrets.token_hex(16)


def _grant(voice_id: str | None = None) -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id=voice_id or _voice_id(),
                        value=secrets.token_urlsafe(32))


class FakeReads:
    """The platform's reads, as the session's person: states set by the test."""

    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.items: dict[tuple[str, str], str] = {}
        self.tag_calls: list[str] = []
        self.batch_calls: list[list[tuple[str, str]]] = []
        self.writes: list[list[dict]] = []
        self.fail_write = False
        self.gone = False

    async def tagged(self, project_id: str) -> list[dict]:
        self.tag_calls.append(project_id)
        if self.gone:
            raise follow.SessionGone("401")
        return [{"kind": "agent", "id": i, "state": s, "done": None, "total": None}
                for i, s in self.tags.items()]

    async def started(self, items: list[tuple[str, str]]) -> list[dict]:
        self.batch_calls.append(list(items))
        return [{"kind": k, "id": i, "state": self.items[(k, i)], "done": None, "total": None}
                for k, i in items if (k, i) in self.items]

    async def write(self, rows: list[dict]) -> None:
        if self.fail_write:
            raise follow.ApiRefused(follow.WORK_ROUTE, 503)
        self.writes.append(rows)


def _follower(reads: FakeReads) -> follow.Follower:
    return follow.Follower(voice_id=_voice_id(), project_id="vp_" + secrets.token_hex(16), reads=reads)


# ---- one tick ---------------------------------------------------------------


def test_one_tag_query_and_one_batched_read_per_tick_whatever_the_count():
    async def scenario():
        for count in (0, 1, 7, 120):
            reads = FakeReads()
            follower = _follower(reads)
            for n in range(count):
                kind = ("agent", "workflow", "run")[n % 3]
                follower.note(kind, f"item-{n}")
                reads.items[(kind, f"item-{n}")] = "running"
                reads.tags[f"tagged-{n}"] = "queued"
            await follower.tick()
            await follower.tick()
            assert len(reads.tag_calls) == 2, count
            assert len(reads.batch_calls) == 2, count
            assert all(len(batch) == count for batch in reads.batch_calls)
            assert reads.tag_calls == [follower.project_id] * 2

    asyncio.run(scenario())


def test_a_changed_state_is_one_event_carrying_only_the_changed_rows():
    async def scenario():
        reads = FakeReads()
        follower = _follower(reads)
        follower.note("workflow", "wf-1")
        reads.items[("workflow", "wf-1")] = "running"
        reads.tags.update({"t-1": "queued", "t-2": "running", "t-3": "running"})
        first = await follower.tick()
        assert len(reads.writes) == 1 and len(first) == 4

        reads.tags["t-2"] = "done"
        written = await follower.tick()

        assert len(reads.writes) == 2
        assert reads.writes[-1] == written == [
            {"kind": "agent", "id": "t-2", "state": "done", "done": None, "total": None}]

    asyncio.run(scenario())


def test_nothing_is_written_when_nothing_changed():
    async def scenario():
        reads = FakeReads()
        follower = _follower(reads)
        reads.tags["t-1"] = "running"
        await follower.tick()
        writes = len(reads.writes)
        assert await follower.tick() == []
        assert await follower.tick() == []
        assert len(reads.writes) == writes == 1

        empty = FakeReads()
        quiet = _follower(empty)
        assert await quiet.tick() == []
        assert empty.writes == []

    asyncio.run(scenario())


def test_a_failed_write_marks_nothing_seen_and_is_sent_again():
    async def scenario():
        reads = FakeReads()
        follower = _follower(reads)
        reads.tags["t-1"] = "running"
        reads.fail_write = True
        try:
            await follower.tick()
        except follow.ApiRefused:
            pass
        assert reads.writes == []
        reads.fail_write = False
        assert await follower.tick() == [
            {"kind": "agent", "id": "t-1", "state": "running", "done": None, "total": None}]

    asyncio.run(scenario())


def test_a_first_pass_with_more_rows_than_one_event_holds_splits_them():
    async def scenario():
        reads = FakeReads()
        follower = _follower(reads)
        for n in range(follow.ROSTER_MAX + 3):
            reads.tags[f"t-{n}"] = "queued"
        await follower.tick()
        assert [len(w) for w in reads.writes] == [follow.ROSTER_MAX, 3]

    asyncio.run(scenario())


# ---- the follower's life ----------------------------------------------------


class Ticker:
    """`sleep` that returns only when the test releases a tick."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self._gate: asyncio.Queue[None] | None = None

    @property
    def gate(self) -> asyncio.Queue[None]:
        if self._gate is None:
            self._gate = asyncio.Queue()
        return self._gate

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        await self.gate.get()

    def release(self) -> None:
        self.gate.put_nowait(None)


def test_the_follower_runs_while_the_voice_is_paused_and_stops_when_the_session_ends():
    world = World()
    reads = FakeReads()
    reads.tags["t-1"] = "running"
    ticker = Ticker()
    roster = follow.RosterFollowers(lambda grant: reads, sleep=ticker.sleep)
    followers = Followers()
    followers.register(roster.start, roster.stop)

    async def scenario():
        app = create_app(sessions=world.sessions, verify_caller=_caller_ok, followers=followers)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice")
        auth = {"Authorization": "Bearer " + secrets.token_hex(8)}
        voice_id = _voice_id()
        project_id = "vp_" + secrets.token_hex(16)
        started = await client.post("/internal/sessions", headers=auth, json={
            "voice_session_id": voice_id, "person": PERSON, "offer": _offer(),
            "project_id": project_id, "grant": secrets.token_urlsafe(32)})
        assert started.status_code == 201, started.text
        await _until(lambda: len(reads.tag_calls) == 1)
        assert roster.running(voice_id) and reads.tag_calls == [project_id]
        assert ticker.waits == [follow.TICK_SECONDS]

        session = world.sessions.get(voice_id)
        socket = world.socket(session)
        socket.push({"type": PERSON_DELTA, "delta": "Plan it.", "item_id": "item_a"})
        socket.push({"type": live.DELEGATION_CREATED, "delegation": {"id": "item_d"}})
        await _until(lambda: session.delegations)
        world.clock.now += live.PAUSE_AFTER_SECONDS
        socket.push({"type": AUDIO})
        await _until(lambda: _said(socket, live.PAUSE_SENTENCE))
        socket.push({"type": VOICE_FINAL, "transcript": live.PAUSE_SENTENCE})
        await _until(lambda: session.paused)

        reads.tags["t-1"] = "done"
        ticker.release()
        await _until(lambda: len(reads.tag_calls) == 2)
        assert roster.running(voice_id)
        assert reads.writes[-1] == [{"kind": "agent", "id": "t-1", "state": "done",
                                     "done": None, "total": None}]

        world.planner.go.set()
        ended = await client.post(f"/internal/sessions/{voice_id}/end", headers=auth)
        assert ended.status_code == 200
        assert not roster.running(voice_id) and roster.get(voice_id) is None
        ticker.release()
        await asyncio.sleep(0.05)
        assert len(reads.tag_calls) == 2
        await client.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_a_grant_swarm_api_no_longer_accepts_stops_the_follower():
    reads = FakeReads()
    ticker = Ticker()
    roster = follow.RosterFollowers(lambda grant: reads, sleep=ticker.sleep)

    async def scenario():
        grant = _grant()
        await roster.start(grant.session_id, grant)
        await _until(lambda: len(reads.tag_calls) == 1)
        reads.gone = True
        ticker.release()
        await _until(lambda: not roster.running(grant.session_id))
        await roster.stop(grant.session_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_a_session_without_a_grant_is_not_followed():
    reads = FakeReads()
    roster = follow.RosterFollowers(lambda grant: reads)

    async def scenario():
        voice_id = _voice_id()
        await roster.start(voice_id, None)
        assert not roster.running(voice_id) and reads.tag_calls == []

    asyncio.run(scenario())


# ---- through swarm-api ------------------------------------------------------


def test_the_reads_and_the_write_go_through_the_existing_routes_with_the_grant():
    grant = _grant()
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/tasks":
            return httpx.Response(200, json={"tasks": [{"id": "t-tag", "state": "PARKED"}]})
        if path == "/v1/tasks/t-1":
            return httpx.Response(200, json={"task": {"id": "t-1", "state": "RUNNING"}})
        if path == "/v1/workflows/wf-1":
            return httpx.Response(200, json={
                "workflow": {"state": "RUNNING", "steps": [{}, {}, {}]},
                "tasks": [{"state": "SUCCEEDED"}, {"state": "RUNNING"}, {"state": "SUCCEEDED"}]})
        if path == "/v1/runs/run-1":
            return httpx.Response(200, json={"run": {"state": "PLANNED"}})
        if path == "/v1/runs/run-gone":
            return httpx.Response(404, json={})
        if path == f"/v1/voice/sessions/{grant.session_id}/work":
            return httpx.Response(200, json={"seq": 1})
        return httpx.Response(500)

    async def tokens() -> str:
        return secrets.token_hex(12)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            api = follow.RosterApi(http, base_url="http://api", id_tokens=tokens, grant=grant)
            follower = follow.Follower(voice_id=grant.session_id, project_id=grant.project_id,
                                       reads=follow.ApiReads(api))
            for kind, item in (("agent", "t-1"), ("workflow", "wf-1"), ("run", "run-1"),
                               ("run", "run-gone"), ("agent", "../t"), ("image", "x-1")):
                follower.note(kind, item)
            written = await follower.tick()
            return written

    written = asyncio.run(scenario())
    tag = seen[0]
    assert tag.method == "GET" and tag.url.path == "/v1/tasks"
    assert tag.url.params["claude_session"] == "voice:" + grant.project_id
    assert sorted(r.url.path for r in seen[1:5]) == [
        "/v1/runs/run-1", "/v1/runs/run-gone", "/v1/tasks/t-1", "/v1/workflows/wf-1"]
    post = seen[-1]
    assert post.method == "POST" and len(seen) == 6
    for request in seen:
        assert request.headers[follow.QUIET_HEADER] == follow.QUIET_ROSTER
        assert request.headers[GRANT_HEADER] == grant.headers()[GRANT_HEADER]
        assert request.headers[SESSION_HEADER] == f"{grant.project_id}/{grant.session_id}"
    rows = json.loads(post.content)["roster"]
    assert rows == written == [
        {"kind": "agent", "id": "t-tag", "state": "queued", "done": None, "total": None},
        {"kind": "agent", "id": "t-1", "state": "running", "done": None, "total": None},
        {"kind": "workflow", "id": "wf-1", "state": "running", "done": 2, "total": 3},
        {"kind": "run", "id": "run-1", "state": "waiting", "done": None, "total": None},
    ]
    for row in rows:
        assert vwork.roster_row(row) == row


def _stream(db, call) -> list[dict]:
    prefix = (f"{api_sessions.PROJECTS}/{call.project_id}/{api_sessions.SESSIONS}/"
              f"{call.session_id}/{api_store.EVENTS}/")
    rows = [row for path, row in db.docs.items() if path.startswith(prefix)]
    return sorted(rows, key=lambda r: r["seq"])


def test_through_swarm_apis_grant_a_tick_writes_its_roster_event_and_no_steps(
        db, tokens, group_map, objects):
    voice = FakeVoice()
    client = _build(db, tokens, group_map, objects, voice)
    call = _start(client, voice)
    seed_task(db, task_id="t-tag", tenant_id="eng", state="PARKED")
    db.docs["tasks/t-tag"]["metadata"] = {"claude_session": "voice:" + call.project_id}
    seed_task(db, task_id="t-1", tenant_id="eng", state="RUNNING")
    grant = SessionGrant(project_id=call.project_id, session_id=call.session_id, value=call.grant)
    audits = lambda: [row for path, row in db.docs.items()  # noqa: E731
                      if path.startswith(api_sessions.AUDIT + "/")
                      and row.get("action") == api_grant.AUDIT_GRANT_USED]

    def answer(request: httpx.Request) -> httpx.Response:
        sent = client.request(request.method, request.url.raw_path.decode(),
                              headers=dict(request.headers), content=request.content)
        return httpx.Response(sent.status_code, content=sent.content,
                              headers={"content-type": "application/json"})

    async def tokens_() -> str:
        return VOICE_TOKEN

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            api = follow.RosterApi(http, base_url="http://api", id_tokens=tokens_, grant=grant)
            follower = follow.Follower(voice_id=grant.session_id, project_id=grant.project_id,
                                       reads=follow.ApiReads(api))
            follower.note("agent", "t-1")
            first = await follower.tick()
            after_first = list(_stream(db, call))
            second = await follower.tick()
            return first, after_first, second

    first, after_first, second = asyncio.run(scenario())
    assert [r["id"] for r in first] == ["t-tag", "t-1"]
    assert [e["type"] for e in after_first] == [vwork.ROSTER_UPDATE]
    assert second == []
    assert _stream(db, call) == after_first
    # Every read is still audited: two a tick, the write once.
    assert len(audits()) == 5
    assert sum(1 for row in audits() if row["quiet"]) == 4


def test_the_quiet_header_matches_swarm_apis():
    assert follow.QUIET_HEADER == api_grant.QUIET_HEADER
    assert follow.QUIET_ROSTER == api_grant.QUIET_ROSTER


def test_the_follower_maps_states_as_swarm_apis_roster_does():
    assert dict(follow.STATES) == dict(api_roster.STATES)
    assert set(follow.STATES.values()) == set(vwork.ROSTER_STATES)
    assert follow.ROSTER_MAX == vwork.ROSTER_MAX
    assert follow.WORK_ROUTE == vwork.WORK_ROUTE[1]
