"""One own-speech session through the real service code of both services, as
the end-to-end scenario beside this file walks it, with the planner kept warm
as the deployment builds it (swarm_voice/plannerwarm.py).

Two requests are delegated and both lines come back, under their own ids; the
planner holds one account and one child for both; each turn writes its brief;
at the cap the next request is refused and the planner lets both go; a session
ended by the person lets both go too.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_one_session_end_to_end import (CHOSEN, FIRST_WORDS, HOUR, LATER_WORDS, PERSON,  # noqa: E402
                                         PLANNER_BRIEF, PLANNER_LINE, TENANT, PlannerModel, World,
                                         _delegation_states)

from swarm_api.voice import budget as vbudget  # noqa: E402
from swarm_api.voice import ledger as vledger  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_relay as sr  # noqa: E402
from swarm_api.voice import workfeed as vwork  # noqa: E402
from swarm_voice import claudeturn, plannerlease, plannerwarm, prompts, worksink  # noqa: E402


class WarmChild:
    def __init__(self, handler: Any) -> None:
        self.handler, self.said, self.closed = handler, [], False

    @property
    def alive(self) -> bool:
        return not self.closed

    async def say(self, text: str, **_kw: Any) -> claudeturn.ChildOutcome:
        self.said.append(text)
        await self.handler(prompts.SPEAK_TOOL, {"summary": PLANNER_LINE, "point_at": None, "brief": PLANNER_BRIEF})
        return claudeturn.ChildOutcome(usage={"input_tokens": 1200, "output_tokens": 300}, stopped=True)

    async def close(self) -> None:
        self.closed = True


class WarmModel(PlannerModel):
    def __init__(self, model: str = "claude-opus-5-5") -> None:
        super().__init__(model)
        self.children: list[WarmChild] = []

    def with_model(self, model: str) -> "WarmModel":
        self.model = model
        return self

    async def converse(self, *, system: str, token: str, tools: list[dict], handler: Any,
                       known: tuple[str, ...]) -> WarmChild:
        self.children.append(WarmChild(handler))
        return self.children[-1]


@pytest.fixture
def world():
    built = World()
    built.planner_model = WarmModel()
    built.planner._claude = built.planner_model  # noqa: SLF001
    built.planner._warm = plannerwarm.WarmPlanners(built.planner._accounts)  # noqa: SLF001
    try:
        yield built
    finally:
        built.voice.run(built.planner.aclose())
        built.close()


def _start(world: World) -> dict[str, Any]:
    return world.sessions.start(tenant_id=TENANT, person=PERSON, offer="", project_id=None,
                                mode=vs.MODE_SPEECH, fallback=None, chosen=CHOSEN)


def _ask(world: World, session_id: str, words: str) -> tuple[str, str | None]:
    said = world.relay.turn(session_id, words, None)
    assert said.delegation_id is not None
    line = world.relay.delegation(session_id, said.delegation_id, timeout=sr.DELEGATION_SECONDS)
    world.voice.run(world.sink.drain())
    return said.delegation_id, line


def _drain(world: World) -> None:
    world.voice.run(world.planner._warm.drain())  # noqa: SLF001


def _releases(world: World) -> int:
    return sum(1 for p, _ in world.broker.calls if p.endswith("/release"))


def test_two_delegations_share_one_account_and_one_child_until_the_cap(world: World):
    answer = _start(world)
    session_id = answer["session_id"]
    first, first_line = _ask(world, session_id, FIRST_WORDS)
    second, second_line = _ask(world, session_id, LATER_WORDS)

    assert first != second
    assert all(line is not None and PLANNER_LINE in line for line in (first_line, second_line))
    (child,) = world.planner_model.children
    assert len(child.said) == 2 and FIRST_WORDS in child.said[0] and LATER_WORDS in child.said[1]
    assert world.broker_holders("/assign") == [plannerlease.FRONT_HOLDER, plannerlease.HOLDER]
    assert _releases(world) == 0 and child.alive and world.planner_model.prompts == []
    assert _delegation_states(world, answer) == [(first, vwork.STARTED), (first, vwork.DONE),
                                                 (second, vwork.STARTED), (second, vwork.DONE)]
    # Each turn wrote its brief, and the second read the first's, fenced.
    assert len([p for m, p, _s in world.api.seen if m == "POST" and p.endswith("/brief")]) == 2
    assert PLANNER_BRIEF not in child.said[0] and '<data source="project_brief">' in child.said[1]
    assert all(status < 300 for _m, _p, status in world.api.seen)

    while not [n for n in world.enforce(answer)["notices"] if n["notice"] == vbudget.CAPPED]:
        world.clock.advance(HOUR)
    _refused, line = _ask(world, session_id, LATER_WORDS)
    assert line == worksink.CAP_SENTENCES[vledger.SESSION_CAPPED]
    _drain(world)
    # The planner's account went back at the cap; the front still holds its own.
    assert child.closed and _releases(world) == 1 and world.front.get(session_id) is not None
    assert len(child.said) == 2


def test_the_persons_end_lets_the_planners_account_and_child_go(world: World):
    answer = _start(world)
    _ask(world, answer["session_id"], FIRST_WORDS)
    (child,) = world.planner_model.children
    assert child.alive and _releases(world) == 0
    world.voice.run(world.front.end(answer["session_id"]))
    _drain(world)
    # The front's account and the planner's.
    assert child.closed and _releases(world) == 2
