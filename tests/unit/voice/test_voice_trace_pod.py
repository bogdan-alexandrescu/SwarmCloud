"""Debug mode's trace, swarm-voice's half (swarm_voice/trace.py; the owner,
10 November 2026: "Trace on Telemetry").

THE DEFECT behind it (live use, 10 November 2026): the GPT voice engine told the
person "there is a card on screen" while the planner had drawn nothing, and
finding out why took reading the voice pod's logs by hand. This holds:

* with debug off a planner turn sends no trace write at all;
* with debug on each GPT-Live and Own speech planner turn sends one, with the
  transcript it was given, the handoff, the sideband's counts and what the
  voice was told;
* a turn with an empty transcript records `empty_transcript`; a refused card
  records its reason (`refused`); a read is its name and target;
* a failed trace write never changes or holds up the turn's reply, and is
  logged once with its fixed code.

Offline: a LiveSession reads a scripted socket; the planner is a fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import live  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
from swarm_voice import tools_read  # noqa: E402
from swarm_voice import trace as vtrace  # noqa: E402
from swarm_voice.grant import SessionGrant  # noqa: E402


class _State:
    name = "OPEN"


class ScriptedSocket:
    def __init__(self, events: list[dict]) -> None:
        self._events = [json.dumps(e) for e in events]
        self.sent: list[dict] = []
        self.state = _State()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for raw in self._events:
            yield raw

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        pass


class FakePlanner:
    def __init__(self, result: pl.TurnResult | None = None, *, trace_fails: bool = False) -> None:
        self.calls: list[dict] = []
        self.traces: list[dict] = []
        self.result = result
        self.trace_fails = trace_fails

    async def delegate(self, **kwargs):
        self.calls.append(kwargs)
        if self.result is not None:
            self.result.delegation_id = kwargs["delegation_id"]
            return self.result
        return pl.TurnResult(kwargs["delegation_id"], "It's on screen.",
                             drawn=[{"kind": "plan", "title": "Bakery website"}])

    async def write_trace(self, grant: Any, body: dict) -> bool:
        if self.trace_fails:
            # As `trace.send` does for a refusal: logged once, dropped.
            return await vtrace.send(_failing_http(), base_url="https://swarm-api.test",
                                     id_tokens=_tokens, grant=_grant(), body=body)
        self.traces.append(body)
        return True

    def forget(self, session_id: str) -> None:
        pass


async def _tokens() -> str:
    return "id-" + secrets.token_hex(8)


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16),
                                   session_id="vs_" + secrets.token_hex(16),
                                   value=secrets.token_urlsafe(32))


def _failing_http() -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _delegation() -> dict:
    return {"type": live.DELEGATION_CREATED,
            "delegation": {"id": "item_" + secrets.token_hex(6), "type": "delegation"}}


def _run(events: list[dict], planner: FakePlanner, *, debug: bool) -> tuple[live.LiveSession, ScriptedSocket]:
    socket = ScriptedSocket(events)

    async def main() -> live.LiveSession:
        session = live.LiveSession("vs_" + secrets.token_hex(4), "live_" + secrets.token_hex(4),
                                   credential=None, api=None, on_event=None,
                                   on_closed=lambda s: None, person="owner@example.com",
                                   planner=planner)
        if debug:
            session.trace = vtrace.SessionTrace(vtrace.ENGINE_LIVE)
        session._socket = socket
        await session._read(socket)
        for _ in range(300):
            if not session._turns and not session._trace_writes:
                break
            await asyncio.sleep(0.01)
        return session

    return asyncio.run(asyncio.wait_for(main(), 10)), socket


def _said(socket: ScriptedSocket) -> list[str]:
    return [e["content"] for e in socket.sent if e.get("type") == live.COMMENTARY_APPEND]


TALK = [
    {"type": "session.input_audio_transcript.delta", "item_id": "i1", "delta": "Help me build "},
    {"type": "session.input_audio_transcript.delta", "item_id": "i1", "delta": "a website."},
    {"type": "session.input_audio_transcription.completed", "item_id": "i1",
     "transcript": "Help me build a website."},
    {"type": "session.input_audio_transcription.completed", "item_id": "i2", "transcript": ""},
]


def test_with_debug_off_a_planner_turn_sends_no_trace():
    planner = FakePlanner()
    # MUTATION: trace every session regardless of `debug` and a write is sent.
    session, socket = _run([*TALK, _delegation()], planner, debug=False)
    assert session.trace is None
    assert planner.traces == []
    assert _said(socket) == ["It's on screen."]


def test_with_debug_on_a_live_turn_is_traced_with_its_counts():
    planner = FakePlanner()
    _, socket = _run([*TALK, _delegation()], planner, debug=True)
    assert len(planner.traces) == 1
    body = planner.traces[0]
    assert body["engine"] == vtrace.ENGINE_LIVE
    turn = body["turn"]
    assert turn["n"] == 1
    assert turn["lines"] == [{"who": "person", "text": "Help me build a website."}]
    assert turn["drawn"] == [{"kind": "plan", "title": "Bakery website"}]
    assert turn["none"] is None
    assert turn["told"] == "It's on screen."
    assert turn["events"] == {"pieces": 2, "finals": 2, "empty_finals": 1, "handoffs": 1,
                              "barge_ins": 0}
    assert isinstance(turn["secs"], float)
    assert body["session_events"]["replies"] == 1
    assert _said(socket) == ["It's on screen."]


def test_an_empty_transcript_records_empty_transcript():
    planner = FakePlanner()
    # MUTATION: skip `_traced` on the ask-again path and no turn is recorded.
    _run([{"type": "session.updated"}, _delegation()], planner, debug=True)
    assert planner.calls == []
    assert len(planner.traces) == 1
    turn = planner.traces[0]["turn"]
    assert turn["none"] == vtrace.NONE_EMPTY == "empty_transcript"
    assert turn["told"] == live.SAY_ASK_AGAIN
    assert turn["drawn"] == [] and turn["lines"] == []


def test_a_refused_card_records_its_reason():
    result = pl.TurnResult(None, "I couldn't draw that.",
                           drawn=[{"kind": "work", "title": "Publish the order form",
                                   "refused": "swarm-api /v1/voice/sessions/{session_id}/cards: HTTP 409"}])
    planner = FakePlanner(result)
    _run([*TALK, _delegation()], planner, debug=True)
    turn = planner.traces[0]["turn"]
    assert turn["none"] == "refused"
    assert turn["drawn"][0]["refused"].endswith("HTTP 409")


def test_the_no_cards_code_for_errors_timeouts_and_words():
    assert vtrace.none_code(pl.TurnResult(None, "x", error=pl.TIMED_OUT)) == "timeout"
    assert vtrace.none_code(pl.TurnResult(None, "x", error="Claude Code exited 1")) == "planner_error"
    assert vtrace.none_code(pl.TurnResult(None, pl.SAY_NO_GRANT, error="no grant")) == "no_grant"
    assert vtrace.none_code(pl.TurnResult(None, "Here it is in words.")) == "answered_in_words"
    assert vtrace.none_code(None) == "empty_transcript"


def test_a_failed_trace_write_leaves_the_reply_unchanged_and_is_logged_once(caplog):
    planner = FakePlanner(trace_fails=True)
    with caplog.at_level(logging.WARNING, logger="swarm_voice.trace"):
        _, socket = _run([*TALK, _delegation()], planner, debug=True)
    # MUTATION: await the write inside the turn and let it raise: no reply.
    assert _said(socket) == ["It's on screen."]
    failed = [r for r in caplog.records if vtrace.WRITE_FAILED in r.getMessage()]
    assert len(failed) == 1
    assert "ApiRefused" in failed[0].getMessage()


def test_the_planner_records_each_card_refusal_and_read_target():
    class Api:
        session_id = "vs_" + "0" * 32

        async def write_card(self, card: dict) -> dict:
            raise tools_read.ApiRefused(tools_read.CARD_WRITE_ROUTE, 409)

        async def get(self, route: str, path: str, params: dict | None = None) -> Any:
            return {"task": {"task_id": "t_1"}}

    planner = pl.Planner.__new__(pl.Planner)
    result = pl.TurnResult(None, "")

    async def go() -> None:
        await planner._tool(Api(), {"name": "draw_plan", "input": {"title": "Bakery website"}},
                            {"plan"}, (), result)
        await planner._tool(Api(), {"name": "draw_nothing", "input": {"title": "Odd"}},
                            {"plan"}, (), result)
        await planner._tool(Api(), {"name": "get_agent", "input": {"task_id": "t_1"}},
                            {"plan"}, (), result)

    asyncio.run(go())
    assert [d["kind"] for d in result.drawn] == ["plan", "nothing"]
    assert result.drawn[0]["title"] == "Bakery website"
    assert result.drawn[0]["refused"].endswith("HTTP 409")
    assert "not a card kind" in result.drawn[1]["refused"]
    assert result.read_targets == [{"tool": "get_agent", "target": "task_id=t_1"}]
    assert vtrace.none_code(result) == "refused"


def _front_turn(debug: bool, planner: FakePlanner) -> list[dict]:
    from swarm_voice import claudefront

    async def go() -> list[dict]:
        fronts = claudefront.FrontSessions.__new__(claudefront.FrontSessions)
        fronts._planner = planner
        fronts._clock = lambda: 10.0
        session = claudefront.FrontSession("vs-1", person="owner@example.com", grant=None,
                                           clock=lambda: 10.0)
        if debug:
            session.trace = vtrace.SessionTrace(vtrace.ENGINE_OWN, clock=lambda: 10.0)
            session.trace.note("finals")
        session.turns = [("person", "A website for the bakery."), ("voice", "On it.")]
        delegation = claudefront.Delegation("vd_1", "vs-1", "Plan the bakery website.")
        delegation.heard_at = 7.5 if debug else None
        session.queue.append(delegation)
        await fronts._run_delegations(session)
        for _ in range(100):
            if not session.trace_writes:
                break
            await asyncio.sleep(0.01)
        assert delegation.spoken == "It's on screen."
        return planner.traces

    return asyncio.run(asyncio.wait_for(go(), 10))


def test_an_own_speech_delegation_is_traced_only_with_debug_on():
    # MUTATION: trace every front session and the debug-off turn sends a write.
    assert _front_turn(False, FakePlanner()) == []
    traces = _front_turn(True, FakePlanner())
    assert len(traces) == 1
    body = traces[0]
    assert body["engine"] == vtrace.ENGINE_OWN == "own"
    turn = body["turn"]
    assert turn["handoff"] == "Plan the bakery website."
    assert turn["lines"][-1] == {"who": "person", "text": "Plan the bakery website."}
    assert turn["told"] == "It's on screen."
    assert turn["secs"] == 2.5
    assert turn["events"]["finals"] == 1
    assert turn["none"] is None
