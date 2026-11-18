"""A delegation's lifecycle reaches the session's stream (swarm_voice/worksink.py;
docs/design/voice-it.md §3.12.5, the `work.delegation` row of the feed table).

With the fast front's fakes (test_claude_front.py) and a fake swarm-api on an
`httpx.MockTransport` that records every post, this holds that:

* a delegation that runs and finishes posts `started` then `done`;
* one whose planner raises posts `started` then `failed`;
* one the planner refuses posts `started` then `refused` (swarm-api takes an
  end only for a delegation it holds open), and one that never ran posts
  nothing;
* a session ended mid-delegation posts the delegation's end, through the
  grant the delegation started with;
* a post swarm-api refuses, or one that never answers, leaves the turn and the
  delegation's result unchanged, and a refusal is one WARNING naming the
  route and the status;
* every body is `{delegation: {delegation_id, state}}` and nothing else: a
  fake key in the person's words never leaves;
* `build_fronts` wires the sink from SWARM_API_URL, and none without it.

Every token and grant is built at runtime; no cloud credential or emulator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import claudefront, planner, worksink  # noqa: E402
from swarm_voice import main as voice_main  # noqa: E402
from swarm_voice.grant import GRANT_HEADER  # noqa: E402

from test_claude_front import PERSON, World, _grant  # noqa: E402

API_URL = "http://swarm-api.test"
WORK_PATH = "/v1/voice/sessions/{session_id}/work"


class SwarmApi:
    """swarm-api's work route: records each post, answers `status`, and holds
    every answer while `gate` is clear."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, dict]] = []
        self.status = 200
        self.gate = asyncio.Event()
        self.gate.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        await self.gate.wait()
        self.posts.append((request.url.path, json.loads(request.content or b"{}"), dict(request.headers)))
        return httpx.Response(self.status, json={"type": "work.delegation", "seq": len(self.posts)})

    def states(self) -> list[str]:
        return [body["delegation"]["state"] for _, body, _ in self.posts]


class Harness:
    def __init__(self, world: World) -> None:
        self.world = world
        self.api = SwarmApi()
        self.api_http = httpx.AsyncClient(transport=httpx.MockTransport(self.api.handler))
        self.id_token = "i" + secrets.token_hex(12)

        async def tokens() -> str:
            return self.id_token

        self.sink = worksink.WorkSink(self.api_http, api_base=API_URL, id_tokens=tokens)
        self.front = claudefront.FrontSessions(accounts=world.accounts, planner=world.planner, claude=world.claude,
                                               clock=lambda: world.now[0], idle_seconds=900.0, sleep=world.sleep,
                                               work=self.sink)


def run(coro_fn):
    async def main():
        world = World()
        h = Harness(world)
        try:
            return await coro_fn(h)
        finally:
            await h.front.end_all()
            await world.front.end_all()
            await h.api_http.aclose()
            await world.http.aclose()
    return asyncio.run(main())


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def _delegate(h: Harness, text: str = "plan the fix for the login bug"):
    grant = _grant()
    await h.front.start("vs-1", person=PERSON, grant=grant)
    answer = await h.front.turn("vs-1", text)
    assert answer.delegation_id
    await _settle()
    return grant, answer


def test_a_delegation_that_runs_and_finishes_posts_started_then_done():
    async def body(h: Harness):
        grant, answer = await _delegate(h)
        await h.sink.drain()
        assert h.api.states() == ["started"]
        h.world.planner.gate.set()
        await _settle()
        await h.sink.drain()
        assert h.api.states() == ["started", "done"]
        for path, sent, headers in h.api.posts:
            assert path == WORK_PATH.format(session_id=grant.session_id)
            assert sent["delegation"]["delegation_id"] == answer.delegation_id
            # Through the session's own grant and this service's ID token.
            assert headers[GRANT_HEADER.lower()] == grant.headers()[GRANT_HEADER]
            assert headers["authorization"] == f"Bearer {h.id_token}"
    run(body)


def test_a_delegation_whose_planner_raises_posts_started_then_failed():
    async def body(h: Harness):
        async def broken(**_kwargs):
            raise RuntimeError("the planner broke")

        h.world.planner.delegate = broken
        _, answer = await _delegate(h)
        await h.sink.drain()
        assert h.front.delegation(answer.delegation_id).state == "failed"
        assert h.api.states() == ["started", "failed"]
    run(body)


@pytest.mark.parametrize("refusal", [planner.SAY_POOL_FULL, planner.SAY_NO_POOL, planner.SAY_NO_LEASE])
def test_a_delegation_the_planner_refuses_posts_started_then_refused(refusal):
    async def body(h: Harness):
        async def refused(**kwargs):
            return SimpleNamespace(spoken=refusal, cards=[], error="the pool leased no account", usage={})

        h.world.planner.delegate = refused
        _, answer = await _delegate(h)
        await h.sink.drain()
        # The voice still says the planner's refusal, as before.
        assert h.front.delegation(answer.delegation_id).spoken == refusal
        assert h.api.states() == ["started", "refused"]
    run(body)


def test_a_planner_turn_that_ends_in_error_posts_failed():
    async def body(h: Harness):
        async def timed_out(**kwargs):
            return SimpleNamespace(spoken=planner.SAY_FAILED, cards=[], error=planner.TIMED_OUT, usage={})

        h.world.planner.delegate = timed_out
        await _delegate(h)
        await h.sink.drain()
        assert h.api.states() == ["started", "failed"]
    run(body)


def test_a_delegation_that_never_ran_posts_nothing():
    async def body(h: Harness):
        grant = _grant()
        await h.front.start("vs-1", person=PERSON, grant=grant)
        first = await h.front.turn("vs-1", "plan one")
        second = await h.front.turn("vs-1", "plan two")
        await _settle()
        await h.front.end("vs-1")
        await h.sink.drain()
        ids = [body["delegation"]["delegation_id"] for _, body, _ in h.api.posts]
        assert second.delegation_id not in ids
        assert ids == [first.delegation_id, first.delegation_id]
    run(body)


def test_a_session_ended_mid_delegation_posts_the_delegations_end_through_its_grant():
    async def body(h: Harness):
        grant, answer = await _delegate(h)
        await h.front.end("vs-1")
        await h.sink.drain()
        assert h.front.get("vs-1") is None
        assert h.api.states() == ["started", "failed"]
        _, _, headers = h.api.posts[-1]
        assert headers[GRANT_HEADER.lower()] == grant.headers()[GRANT_HEADER]
        assert h.api.posts[-1][1]["delegation"]["delegation_id"] == answer.delegation_id
    run(body)


def test_a_refused_post_leaves_the_turn_and_the_result_unchanged_and_warns_once(caplog):
    async def body(h: Harness):
        h.api.status = 503
        with caplog.at_level(logging.WARNING, logger="swarm_voice.worksink"):
            _, answer = await _delegate(h)
            assert answer.reply == "Looking at that now."
            h.world.planner.gate.set()
            await _settle()
            await h.sink.drain()
        delegation = h.front.delegation(answer.delegation_id)
        assert delegation.state == "done" and delegation.spoken == "It's on screen."
        assert h.api.states() == ["started", "done"]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # One line per refused post, naming the route and the status only.
        assert len(warnings) == 2
        for line in warnings:
            assert worksink.WORK_ROUTE in line and "HTTP 503" in line
    run(body)


def test_a_post_that_never_answers_never_holds_the_turn():
    async def body(h: Harness):
        h.api.gate.clear()
        _, answer = await _delegate(h)
        assert answer.reply == "Looking at that now."
        h.world.planner.gate.set()
        await _settle()
        # The delegation ended while swarm-api has not answered its start.
        assert h.front.delegation(answer.delegation_id).state == "done"
        assert h.api.posts == []
        h.api.gate.set()
        await h.sink.drain()
        assert h.api.states() == ["started", "done"]
    run(body)


def test_no_body_carries_anything_but_the_id_and_the_state():
    async def body(h: Harness):
        key = "sk-" + secrets.token_hex(24)
        _, answer = await _delegate(h, f"plan the rotation of {key} please")
        h.world.planner.gate.set()
        await _settle()
        await h.sink.drain()
        assert h.api.states() == ["started", "done"]
        for _, sent, _ in h.api.posts:
            assert set(sent) == {"delegation"}
            assert set(sent["delegation"]) <= {"delegation_id", "state", "at"}
            assert key not in json.dumps(sent)
    run(body)


def test_build_fronts_wires_the_work_sink_from_swarm_apis_address(monkeypatch):
    live = SimpleNamespace(_leases=SimpleNamespace(), _planner=None)

    async def scenario():
        async with httpx.AsyncClient() as http:
            monkeypatch.setenv("SWARM_API_URL", API_URL)
            fronts = voice_main.build_fronts(live, http)
            sink = fronts._work  # noqa: SLF001
            assert isinstance(sink, worksink.WorkSink) and sink._base == API_URL  # noqa: SLF001
            monkeypatch.delenv("SWARM_API_URL")
            assert voice_main.build_fronts(live, http)._work is None  # noqa: SLF001
            assert voice_main.build_fronts(live)._work is None  # noqa: SLF001

    asyncio.run(scenario())
