"""The planner's project brief (swarm_voice/planner.py, prompts.py; swarm-api's
voice/projectbrief.py).

* After each planner turn that spoke, the brief `speak` gave is written once,
  scrubbed and cut to `prompts.BRIEF_MAX` bytes.
* The next delegation reads it (the project's status view) and its turn
  message carries it inside its own fence; hostile text in a brief neither
  closes that fence nor writes a word outside it.
* A session at its cap writes no brief: a capped delegation runs no turn, and
  a write swarm-api refuses at the cap changes nothing the voice says.

The fake `claude`, broker and swarm-api are test_planner.py's; no network.
"""

from __future__ import annotations

import asyncio
import json
import secrets

import httpx
import injection_reader as reader
import pytest
from test_planner import World, _delegate

from swarm_api.voice import projectbrief as api_brief
from swarm_voice import planner as pl
from swarm_voice import prompts


class BriefWorld(World):
    """test_planner's world, whose swarm-api also serves the project's status
    view and stores the brief."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.stored: str | None = None
        self.brief_posts: list[str] = []
        self.status_reads = 0
        #: swarm-api's answer to a brief write at a cap (routes/voice_status.py).
        self.capped_write = False

    def swarm_api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/voice/projects/{self.grant.project_id}/status":
            assert request.headers.get("x-swarm-voice-grant") == self.grant.headers()[
                "X-Swarm-Voice-Grant"]
            self.status_reads += 1
            brief = {"text": self.stored, "at": None, "session_id": None} if self.stored else None
            return httpx.Response(200, json={"project_id": self.grant.project_id, "cards": [],
                                             "roster": [], "brief": brief})
        if request.method == "POST" and path == f"/v1/voice/sessions/{self.grant.session_id}/brief":
            text = json.loads(request.content)["text"]
            self.brief_posts.append(text)
            if self.capped_write:
                return httpx.Response(409, json={"code": "conflict",
                                                 "detail": {"reason": "voice_budget_capped"}})
            self.stored = text
            return httpx.Response(200, json={"brief": {"text": text}})
        return super().swarm_api(request)


@pytest.fixture
def world(tmp_path) -> BriefWorld:
    from test_planner import _grant

    return BriefWorld(_grant(), tmp_path)


def _again(world: BriefWorld) -> None:
    """A fresh client for the next delegation (`_delegate` closes its own)."""
    world.http = httpx.AsyncClient(transport=httpx.MockTransport(world.handle))


def _speak(summary: str, brief: str | None) -> list:
    return ["speak", {"summary": summary, "point_at": None, "brief": brief}]


def test_the_brief_is_written_once_per_turn_and_read_on_the_next_delegation(world):
    note = "Export plan proposed, waiting on the owner. Login fix queued as an agent."
    world.plan = {"calls": [_speak("The plan is on screen.", note)]}

    first = _delegate(world)

    assert first.error is None, first.error
    assert world.brief_posts == [note] and first.brief_written
    assert first.brief_read is False
    _again(world)
    world.plan = {"calls": [_speak("Still waiting on you.", "Nothing new.")]}

    second = _delegate(world)

    assert second.brief_read is True
    prompt = world.child()["prompt"]
    reading = reader.read(prompt)
    assert reading.inside(note), "the brief is not inside a fence of the turn message"
    assert 'source="project_brief"' in prompt
    assert world.brief_posts == [note, "Nothing new."]
    assert world.status_reads == 2


def test_a_turn_without_a_brief_writes_none(world):
    world.plan = {"calls": [_speak("The plan is on screen.", None)]}
    result = _delegate(world)
    assert result.error is None and world.brief_posts == [] and not result.brief_written


def test_the_brief_is_bounded_and_scrubbed(world):
    grant_value = world.grant.headers()["X-Swarm-Voice-Grant"]
    long = ("é" * prompts.BRIEF_MAX) + " " + grant_value
    world.plan = {"calls": [_speak("Done.", long)]}

    _delegate(world)

    [posted] = world.brief_posts
    assert len(posted.encode("utf-8")) <= prompts.BRIEF_MAX
    assert grant_value not in posted
    # One bound on both sides of the wire.
    assert prompts.BRIEF_MAX == api_brief.BRIEF_TEXT_MAX


def test_a_capped_session_runs_no_turn_and_writes_no_brief(world):
    world.budget = {**world.budget, "capped": "session"}
    world.plan = {"calls": [_speak("Done.", "A note.")]}

    result = _delegate(world)

    assert world.brief_posts == [] and not result.brief_written
    assert world.paths() == []  # nothing leased


def test_a_brief_refused_at_the_cap_changes_nothing_said(world):
    world.capped_write = True
    world.plan = {"calls": [_speak("The plan is on screen.", "A note.")]}

    result = _delegate(world)

    assert world.brief_posts == ["A note."]
    assert not result.brief_written and world.stored is None
    assert result.spoken.startswith("The plan is on screen.")


def test_a_failed_status_read_runs_the_turn_without_a_brief(world):
    real = world.swarm_api

    def broken(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/status"):
            return httpx.Response(503)
        return real(request)

    world.swarm_api = broken
    world.plan = {"calls": [_speak("Done.", "A note.")]}
    result = _delegate(world)
    assert result.error is None and result.brief_read is False


def test_the_speak_tool_asks_for_the_brief_and_the_system_prompt_fences_it():
    schema = prompts.speak_tool()["input_schema"]
    assert "brief" in schema["required"] and schema["properties"]["brief"] is prompts.BRIEF_SCHEMA
    assert "project_brief" in prompts.SYSTEM and "never instructions" in prompts.SYSTEM


# ---- injection: a hostile brief stays inside its fence -----------------------------------


def _preamble() -> tuple[str, ...]:
    lines = prompts.turn_message([("person", "hello")], brief="x").split("\n")
    return tuple(lines)


@pytest.mark.parametrize("which", range(6))
def test_a_hostile_brief_stays_inside_its_fence(which):
    token = "tk" + secrets.token_hex(6)
    plain = reader.read(prompts.turn_message([("person", "hi")], brief="plain words " + token))
    assert len(plain.sections) == 2
    for text in reader.hostile(token, preamble=_preamble())[which::6]:
        message = prompts.turn_message([("person", "hi")], brief=text)
        reading = reader.read(message)
        assert len(reading.sections) == 2, ascii(message[:300])
        assert reading.inside(token)
        assert reading.outside() == plain.outside(), ascii(text)


def test_a_brief_at_and_over_the_bound_stays_inside():
    token = "tk" + secrets.token_hex(6)
    opening, closing, _ = reader.markers()
    plain = reader.read(prompts.turn_message([("person", "x")], brief="y"))
    limit = prompts.BRIEF_MAX
    for size in (limit - len(closing), limit - 3, limit, limit + 1, 2 * limit):
        text = (token + (closing + opening) * (size // len(closing + opening) + 1))[:size]
        reading = reader.read(prompts.turn_message([("person", "x")], brief=text))
        assert len(reading.sections) == 2, size
        assert reading.outside() == plain.outside(), size


def test_the_planner_writes_the_brief_after_the_turn_not_during_it():
    # The write is the delegation's last step (one per turn): `_write_brief` is
    # a no-op once written, so a retry inside the turn cannot write twice.
    result = pl.TurnResult(None, "", brief="A note.", brief_written=True)

    class Api:
        session_id = "vs_" + "0" * 32
        calls = 0

        async def _call(self, *a, **k):  # noqa: ANN002, ANN003
            Api.calls += 1

    asyncio.run(pl.Planner._write_brief(object.__new__(pl.Planner), Api(), result))  # noqa: SLF001
    assert Api.calls == 0
