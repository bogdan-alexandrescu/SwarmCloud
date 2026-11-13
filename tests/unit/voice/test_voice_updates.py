"""The voice speaks up while work runs (the owner's picks of 12 November 2026,
after three talks on the development deployment in which the session looked
frozen while the planner worked): swarm_voice/updates.py, and its feeds in
live.py, follow.py and planner.py.

* 45 seconds of silence pause the voice (was 15); 44 do not.
* One queue per voice session, fed by the result, the roster follower's
  changes that matter, a card drawn and a check-in about every 45 seconds
  while a delegation runs; it speaks one line per update through the Live
  session, only at a gap, wakes a paused voice, merges updates that pile up
  and never says the same update twice.
* No spoken line holds anything beyond a phrase, a card title and ids.

Offline: a fake clock, a fake Live session (test_live_pause's World), fake
follower reads and a fake Claude Code child; no cloud credentials.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import sys
from pathlib import Path
from typing import Any

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from swarm_voice import follow  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
from swarm_voice import updates as vu  # noqa: E402
from swarm_voice.claudeturn import ChildOutcome  # noqa: E402
from test_live_pause import (AUDIO, PERSON, PERSON_DELTA, VOICE_DELTA, VOICE_FINAL, World,  # noqa: E402
                             _grant, _offer, _paused, _said, _settle, _talking_then_delegated, _until,
                             _voice_id)


def _updates(socket) -> list[str]:
    """What the update queue said: its commentary, never the planner's result."""
    return [e["content"] for e in socket.sent
            if e.get("type") == live.COMMENTARY_APPEND and str(e.get("event_id", "")).startswith("update")]


def _commentary(socket) -> list[str]:
    return [e["content"] for e in socket.sent if e.get("type") == live.COMMENTARY_APPEND]


# -- the pause: 45 seconds -------------------------------------------------------


def test_forty_five_seconds_of_silence_pause_the_voice_and_forty_four_do_not():
    assert live.PAUSE_AFTER_SECONDS == 45.0
    world = World()

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        world.clock.now += 44
        socket.push({"type": AUDIO})
        await _settle()
        assert not _said(socket, live.PAUSE_SENTENCE) and not session.paused
        world.clock.now += 1
        socket.push({"type": AUDIO})
        await _until(lambda: _said(socket, live.PAUSE_SENTENCE))
        socket.push({"type": VOICE_FINAL, "transcript": live.PAUSE_SENTENCE, "response_id": "resp_b"})
        await _until(lambda: session.paused)
        world.planner.go.set()
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


# -- at a gap ----------------------------------------------------------------------


def test_an_update_while_the_person_speaks_waits_for_the_gap():
    world = World()

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        queue = session.updates
        assert queue is not None and vu.BOARD.get(voice_id) is queue
        # The person starts speaking (voice detection), then a card is drawn.
        socket.push({"type": "input_audio_buffer.speech_started"})
        await _until(lambda: session._person_speaking)  # noqa: SLF001
        vu.BOARD.drawn(voice_id, "plan", "Login page", "card_1")
        world.clock.now += 2
        assert await queue.pump() == [] and _updates(socket) == []
        socket.push({"type": "input_audio_buffer.speech_stopped"})
        await _until(lambda: _updates(socket) == ["I drew a plan card: Login page."])

        # An utterance of theirs still open holds the next one too, until it ends.
        socket.push({"type": PERSON_DELTA, "delta": "and then the", "item_id": "item_b"})
        await _until(lambda: any(s.speaker == "person" and not s.ended
                                 for s in session._segments))  # noqa: SLF001
        vu.BOARD.roster(voice_id, [vu.Change("agent", "t-1", "running")])
        world.clock.now += 1.2
        assert await queue.pump() == []
        world.clock.now += live.UTTERANCE_IDLE_GAP_SECONDS
        assert await queue.pump() == ["Agent t-1 started."]
        world.planner.go.set()
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_an_update_waits_for_the_voice_to_finish_its_sentence():
    world = World()

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        socket.push({"type": live.OUTPUT_AUDIO_STARTED})
        socket.push({"type": VOICE_DELTA, "delta": "Let me look at", "response_id": "resp_c"})
        await _until(lambda: session._playing)  # noqa: SLF001
        vu.BOARD.roster(voice_id, [vu.Change("workflow", "wf-1", "waiting", "running")])
        world.clock.now += 1.5
        assert await session.updates.pump() == []
        socket.push({"type": live.OUTPUT_AUDIO_STOPPED})
        await _until(lambda: not session._playing)  # noqa: SLF001
        # The audio stopped but the sentence did not end: still not a gap.
        assert await session.updates.pump() == []
        socket.push({"type": VOICE_DELTA, "delta": " the agents.", "response_id": "resp_c"})
        await _until(lambda: _updates(socket) == ["Workflow wf-1 is waiting on you."])
        world.planner.go.set()
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_an_update_while_paused_wakes_the_voice_and_is_its_first_words():
    world = World()

    async def scenario():
        session, voice_id = await _paused(world)
        assert world.states() == [live.PAUSED]
        vu.BOARD.roster(voice_id, [vu.Change("run", "run-1", "running", "running", "CHECKING")])
        await _until(lambda: live.WAKING in world.states())
        waking = [e for e in world.events if e.get("state") == live.WAKING]
        assert waking == [{"type": live.SESSION_STATE, "state": live.WAKING, "cause": "result"}]
        assert session.held_results == ["Run run-1 opened its pull request."]
        reopened = await world.sessions.reopen(voice_id, offer=_offer(), cause="result")
        assert "Run run-1 opened its pull request." in world.api.creates[-1]["session"]["instructions"]
        # Spoken once: the open session does not say it again.
        await reopened.updates.pump()
        assert _updates(world.socket(reopened)) == []
        world.planner.go.set()
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


# -- once, and merged ----------------------------------------------------------------


class Reads:
    """The follower's reads: the session's started items, states set by the test."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.phases: dict[tuple[str, str], str] = {}
        self.writes: list[list[dict]] = []

    async def tagged(self, project_id: str) -> list[dict]:
        return []

    async def started(self, items: list[tuple[str, str]]) -> list[dict]:
        return [{"kind": k, "id": i, "state": self.items[(k, i)], "done": None, "total": None}
                for k, i in items if (k, i) in self.items]

    async def write(self, rows: list[dict]) -> None:
        self.writes.append(rows)


def test_a_milestone_a_needs_the_owner_change_and_a_result_each_speak_once():
    world = World()

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        reads = Reads()
        follower = follow.Follower(voice_id=voice_id, project_id="vp_1", reads=reads, updates=vu.BOARD)
        await follower.tick()  # the baseline: nothing told
        follower.note("workflow", "wf-1")
        reads.items[("workflow", "wf-1")] = "running"
        await follower.tick()
        await follower.tick()
        world.clock.now += 1.5
        await session.updates.pump()
        assert _updates(socket) == ["Workflow wf-1 started."]
        reads.items[("workflow", "wf-1")] = "waiting"
        await follower.tick()
        await follower.tick()
        world.clock.now += 1.5
        await session.updates.pump()
        world.clock.now += 1.5
        await session.updates.pump()
        assert _updates(socket) == ["Workflow wf-1 started.", "Workflow wf-1 is waiting on you."]
        # The same change told again (a later pass, a reopen) is not said again.
        vu.BOARD.roster(voice_id, [vu.Change("workflow", "wf-1", "waiting", "running")])
        world.clock.now += 1.5
        assert await session.updates.pump() == []
        world.planner.go.set()
        await _until(lambda: _said(socket, world.planner.spoken))
        told = [e for e in socket.sent if e.get("content") == world.planner.spoken][0]
        session.updates.note(vu.Update(vu.RESULT, "result:" + told["delegation_id"], world.planner.spoken,
                                       told["delegation_id"]))
        assert _commentary(socket).count(world.planner.spoken) == 1
        world.clock.now += 1.5
        await session.updates.pump()
        await _settle()
        assert _commentary(socket).count(world.planner.spoken) == 1
        assert len(_updates(socket)) == 2
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_updates_past_the_lines_limit_are_counted():
    many = [vu.Update(vu.MILESTONE, f"k{n}", f"Agent t-{n} started.", item=f"agent:t-{n}") for n in range(5)]
    assert vu.merge(many) == "Agent t-0 started. Agent t-1 started. Agent t-2 started. And 2 more updates."
    assert vu.merge(many[:4]).endswith("And 1 more update.")


def test_three_updates_in_one_second_speak_as_one_line():
    world = World()

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        vu.BOARD.roster(voice_id, [vu.Change("agent", "t-1", "running")])
        world.clock.now += 0.4
        vu.BOARD.drawn(voice_id, "plan", "Login page", "card_1")
        world.clock.now += 0.4
        vu.BOARD.roster(voice_id, [vu.Change("agent", "t-2", "failed", "running")])
        await _settle()
        assert _updates(socket) == []
        world.clock.now += 0.3
        await session.updates.pump()
        assert _updates(socket) == [
            "Agent t-2 failed. Agent t-1 started. I drew a plan card: Login page."]
        world.planner.go.set()
        await world.sessions.end(voice_id)

    asyncio.run(asyncio.wait_for(scenario(), 10))


# -- the check-in --------------------------------------------------------------------


def test_a_check_in_comes_about_every_45_seconds_while_a_delegation_runs_and_stops_when_it_ends():
    world = World()
    # The pause off: armed, it falls due on the same silence and wins.
    world.sessions._pause_after = None  # noqa: SLF001

    async def scenario():
        session, voice_id = await _talking_then_delegated(world)
        socket = world.socket(session)
        queue = session.updates
        await queue.pump()
        vu.BOARD.calling(voice_id, "list_agents")
        world.clock.now += 44
        assert await queue.pump() == []
        world.clock.now += 1
        assert await queue.pump() == ["Still working: reading the running agents."]
        vu.BOARD.calling(voice_id, "draw_plan")
        world.clock.now += 30
        assert await queue.pump() == []
        world.clock.now += 15
        assert await queue.pump() == ["Still working: drawing a plan card."]
        world.planner.go.set()
        await _until(lambda: _said(socket, world.planner.spoken) and not session.delegations)
        for _ in range(3):
            world.clock.now += 45
            assert await queue.pump() == []
        assert _updates(socket) == ["Still working: reading the running agents.",
                                    "Still working: drawing a plan card."]
        await world.sessions.end(voice_id)
        assert vu.BOARD.get(voice_id) is None

    asyncio.run(asyncio.wait_for(scenario(), 10))


# -- what it says ----------------------------------------------------------------------


class FakeApi:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cards: list[dict] = []

    def known_secrets(self) -> tuple[str, ...]:
        return ()

    async def write_card(self, card: dict) -> dict:
        self.cards.append(card)
        return {"card": {"card_id": "card_" + secrets.token_hex(3), "title": card.get("title"),
                         "version": 1}}


class FakeClaude:
    """A Claude Code child that draws a card and speaks, with model text and
    tool arguments a line must never carry."""

    model = "claude-sonnet-5"

    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def run(self, *, prompt: str, system: str, token: str, tools: list[dict], handler: Any,
                  known: tuple[str, ...], done: asyncio.Event) -> ChildOutcome:
        await handler("draw_plan", {"title": "Login page", "body": "steps " + self.marker,
                                    "state": "draft", "content": {"steps": [self.marker]}})
        await handler("draw_secret", {"title": "Not a kind " + self.marker})
        await handler("speak", {"summary": "I drew it " + self.marker})
        return ChildOutcome(exit_code=0, stopped=True)


class FakeSpeaker:
    def __init__(self) -> None:
        self.paused = False
        self.ended = False
        self.ending = False
        self.pause_after = None
        self.spoken_at = 0.0
        self.delegations = {"d1": None}
        self.lines: list[str] = []

    def at_gap(self) -> bool:
        return True

    async def say_update(self, line: str, *, wakes: bool, delegation_id: str | None = None) -> bool:
        self.lines.append(line)
        return True


_ID = r"[A-Za-z0-9][A-Za-z0-9_:-]*"
_ALLOWED = [re.compile(p) for p in (
    rf"(Agent|Workflow|Run) {_ID} (started|failed|finished|was cancelled|is waiting on you)\.",
    rf"Run {_ID} (opened its pull request|is fixing its pull request's checks|"
    r"is done: its pull request is through|has a plan waiting for your approval)\.",
    r"I drew a [a-z ]+ card: Login page\.",
    r"Still working: [a-z' -]+\.", r"Still working on it\.", r"And \d+ more updates?\.",
)]


def _phrases(line: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=\.) (?=[A-Z])", line) if p.strip()]


def test_no_spoken_line_holds_anything_beyond_a_phrase_a_card_title_and_ids():
    marker = "m" + secrets.token_hex(8)
    clock = [100.0]
    board = vu.Board()
    voice_id = _voice_id()
    speaker = FakeSpeaker()

    async def scenario():
        queue = board.bind(voice_id, lambda: speaker, clock=lambda: clock[0], tick=None)
        planner = pl.Planner(None, api_base="http://swarm-api", id_tokens=None, accounts=None,
                             claude=FakeClaude(marker), updates=board)
        api = FakeApi(voice_id)
        result = pl.TurnResult("d1", "")
        await planner._run(api, result, "t" + secrets.token_hex(4), [], {"plan"}, [],  # noqa: SLF001
                           claude=FakeClaude(marker))
        assert marker in result.spoken  # the result is the planner's own, as before
        board.roster(voice_id, [vu.Change("agent", "t-1", "running"),
                                vu.Change("agent", "../" + marker, "failed", "running"),
                                vu.Change("run", "run-1", "running", "running", "CHECKING"),
                                vu.Change("workflow", "wf-1", "waiting", "running"),
                                vu.Change("image", "x-1", "done", "running")])
        clock[0] += 1.5
        await queue.pump()
        clock[0] += 45
        await queue.pump()
        return queue

    queue = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert speaker.lines == queue.lines and len(speaker.lines) == 2
    assert speaker.lines[1] == "Still working: putting the answer together."
    for line in speaker.lines:
        assert marker not in line
        for phrase in _phrases(line):
            assert any(p.fullmatch(phrase) for p in _ALLOWED), phrase


def test_every_planner_tool_has_a_check_in_phrase():
    from swarm_voice import prompts, tools_act, tools_read, tools_visual

    names = [t["name"] for t in tools_read.tool_schemas()]
    names += [t["name"] for t in tools_visual.tool_schemas()]
    names += [t["name"] for t in tools_act.tool_schemas()] + [prompts.SPEAK_TOOL]
    assert [n for n in names if vu.tool_phrase(n) is None] == []
    assert vu.tool_phrase("draw_risk_list") == "drawing a risk list card"
    assert vu.tool_phrase("draw_../x") is None and vu.tool_phrase("whatever") is None


def test_nothing_waits_on_the_voice_a_session_without_a_queue_hears_nothing():
    board = vu.Board()
    board.drawn("vs_none", "plan", "Login page", "card_1")
    board.roster("vs_none", [vu.Change("agent", "t-1", "running")])
    board.calling("vs_none", "list_agents")
    assert board.get("vs_none") is None


def test_the_start_binds_one_queue_and_the_end_drops_it():
    world = World()

    async def scenario():
        voice_id = _voice_id()
        session = await world.sessions.start(voice_id, person=PERSON, offer=_offer(), grant=_grant(voice_id))
        assert session.updates is vu.BOARD.get(voice_id) and session.updates is not None
        await world.sessions.end(voice_id)
        assert vu.BOARD.get(voice_id) is None and session.updates.closed

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_the_follower_tells_a_runs_pull_request_from_its_own_phase_and_never_writes_it():
    import httpx

    grant = _grant(_voice_id())
    phase = ["RUNNING"]
    posted: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tasks":
            return httpx.Response(200, json={"tasks": []})
        if request.url.path == "/v1/runs/run-1":
            return httpx.Response(200, json={"run": {"state": phase[0]}})
        if request.url.path == f"/v1/voice/sessions/{grant.session_id}/work":
            import json as _json
            posted.append(_json.loads(request.content))
            return httpx.Response(200, json={"seq": 1})
        return httpx.Response(500)

    async def tokens() -> str:
        return secrets.token_hex(12)

    board = vu.Board()
    speaker = FakeSpeaker()
    clock = [100.0]

    async def scenario():
        queue = board.bind(grant.session_id, lambda: speaker, clock=lambda: clock[0], tick=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            api = follow.RosterApi(http, base_url="http://api", id_tokens=tokens, grant=grant)
            follower = follow.Follower(voice_id=grant.session_id, project_id=grant.project_id,
                                       reads=follow.ApiReads(api), updates=board)
            await follower.tick()  # the baseline: nothing told
            follower.note("run", "run-1")
            await follower.tick()
            phase[0] = "CHECKING"  # still `running` on the roster: nothing written
            await follower.tick()
            phase[0] = "FIXING"
            await follower.tick()
            phase[0] = "CHECKING"
            await follower.tick()
            phase[0] = "DONE"
            await follower.tick()
        clock[0] += 1.5
        await queue.pump()
        queue_said.append(set(queue.said))

    queue_said: list[set] = []
    asyncio.run(asyncio.wait_for(scenario(), 10))
    # Of one run's four updates, the line names where it is now; none is said again.
    assert speaker.lines == ["Run run-1 is done: its pull request is through."]
    assert {"run:run-1:started", "run:run-1:checking", "run:run-1:fixing", "run:run-1:done"} <= queue_said[0]
    # The roster carries its five fields only: the phase is never written.
    assert [list(row) for body in posted for row in body["roster"]] == [
        ["kind", "id", "state", "done", "total"]] * 2
