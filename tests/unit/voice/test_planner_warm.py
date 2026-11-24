"""The planner keeps one lease and one child warm per voice session
(swarm_voice/plannerwarm.py; docs/design/voice-it.md §3.6).

Against the planner tests' fake quota broker and fake swarm-api, with a fake
launcher (and, for the wire, a fake `claude` executable that reads stream-json
user messages), this holds that:

* two delegated turns of one session use one lease and one child;
* the session's end, a pause, a spend cap and the idle period each release
  both, the child before the lease; a running turn is never cut for it;
* a dead or silent child, a refused renewal, a spent account and a child that
  cannot start each fall back to a lease and a child of that turn's own;
* a turn that had already put something on screen is never run again;
* two sessions, two people, two projects or two grants never share a child,
  and a call the child makes between turns is refused.

Every token and grant is built at runtime; no cloud credential or emulator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

import httpx
import pytest

_HERE = Path(__file__).resolve().parent
_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
for _path in (str(_HERE), _APP):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_planner import API, BROKER, PERSON, World, _details_card, _grant  # noqa: E402

from swarm_voice import accountlease, claudeturn, follow, live, plannerlease, plannerwarm  # noqa: E402
from swarm_voice import main as voice_main  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
from swarm_voice import prompts  # noqa: E402
from swarm_voice.grant import SESSION_HEADER, SessionGrant  # noqa: E402

RELEASE = "/release"
ASSIGN = "/v1/accounts/assign"


class FakeChild:
    """A warm child in this process: each `say` plays the next script entry."""

    def __init__(self, launcher: "Launcher", handler, token, tools, known) -> None:  # noqa: ANN001
        self.launcher, self.handler, self.token = launcher, handler, token
        self.tools, self.known = tools, known
        self.said: list[str] = []
        self.signs: list[float | None] = []
        self.dead = False
        self.closed = False
        self.entered = asyncio.Event()
        self.go = asyncio.Event()

    @property
    def alive(self) -> bool:
        return not self.dead and not self.closed

    async def say(self, text, *, on_text=None, done=None, closes=None, sign_seconds=None):  # noqa: ANN001
        if not self.alive:
            raise claudeturn.ChildGone("gone")
        self.said.append(text)
        self.signs.append(sign_seconds)
        self.launcher.order.append("say")
        step = self.launcher.script.pop(0) if self.launcher.script else "speak"
        self.entered.set()
        if step == "wait":
            await self.go.wait()
        elif step == "hang":
            await asyncio.Event().wait()
        elif step == "die":
            self.dead = True
            raise claudeturn.ChildGone("exited")
        elif step == "draw-die":
            await self.handler("draw_details_input", _details_card())
            self.dead = True
            raise claudeturn.ChildGone("exited")
        elif step == "draw-late-die":
            # The card's write is still on its way when the child is gone.
            self.late = asyncio.create_task(self.handler("draw_details_input", _details_card()))
            await asyncio.sleep(0)
            self.dead = True
            raise claudeturn.ChildGone("exited", {"input_tokens": 5})
        elif step == "used-die":
            self.dead = True
            raise claudeturn.ChildGone("exited", {"input_tokens": 5, "output_tokens": 1})
        elif step == "spent":
            return claudeturn.ChildOutcome(is_error=True, spent=claudeturn.SPENT_RATE, usage={})
        elif step == "refused":
            return claudeturn.ChildOutcome(is_error=True, result_text="authentication_error", usage={})
        await self.handler(prompts.SPEAK_TOOL, {"summary": "Warm answer.", "brief": None, "point_at": None})
        return claudeturn.ChildOutcome(stopped=True, usage={"input_tokens": 100, "output_tokens": 7})

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.launcher.order.append("child closed")


class Launcher:
    def __init__(self, model: str = "claude-opus-5-5", shared: "Launcher | None" = None) -> None:
        self.model = model
        self.script = shared.script if shared else []
        self.order = shared.order if shared else []
        self.children = shared.children if shared else []
        self.cold = shared.cold if shared else []
        self.no_start = False

    def with_model(self, model: str) -> "Launcher":
        return self if model == self.model else Launcher(model, self)

    async def converse(self, *, system, token, tools, handler, known):  # noqa: ANN001
        if self.no_start:
            raise FileNotFoundError("claude")
        child = FakeChild(self, handler, token, tools, known)
        self.children.append(child)
        return child

    async def run(self, *, prompt, system, token, tools, handler, known, done):  # noqa: ANN001
        self.cold.append(prompt)
        self.order.append("cold")
        await handler(prompts.SPEAK_TOOL, {"summary": "Cold answer.", "brief": None, "point_at": None})
        return claudeturn.ChildOutcome(exit_code=0, stopped=True, usage={"input_tokens": 100, "output_tokens": 7})


class Warm(World):
    """The planner tests' world with several sessions, a broker whose renewals
    can be refused, and a warm planner."""

    def __init__(self, tmp: Path, **warm) -> None:  # noqa: ANN003
        super().__init__(_grant(), tmp)
        self.grants = [self.grant]
        self.renew_status = 200
        self.launcher = Launcher()
        self.renew_seconds = warm.pop("renew_seconds", 240.0)
        self.warm_kw = warm
        self.brief_reply: httpx.Response | None = None
        self._n = 0

    def another(self) -> SessionGrant:
        self.grants.append(_grant())
        return self.grants[-1]

    def swarm_api(self, request: httpx.Request) -> httpx.Response:
        if self.brief_reply is not None and request.url.path.endswith("/brief"):
            return self.brief_reply
        session = (request.headers.get(SESSION_HEADER) or "/").split("/")[-1]
        self.grant = next(g for g in self.grants if g.session_id == session)
        return super().swarm_api(request)

    def broker(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/renew"):
            self.broker_calls.append((path, json.loads(request.content)))
            return httpx.Response(self.renew_status, json={})
        if path.endswith(RELEASE):
            self.launcher.order.append("lease released")
        if path == ASSIGN and not self.refuse:
            self._n += 1
            self.launcher.order.append("leased")
            self.broker_calls.append((path, json.loads(request.content)))
            return httpx.Response(200, json={"account_id": f"acct-{self._n}", "tier": "plan",
                                             "assignment_id": "as-" + secrets.token_hex(4),
                                             "credential": {"access_token": self.token}, "reason": "",
                                             "next_reset_at": None})
        return super().broker(request)

    def build(self, *, claude=None, turn_seconds: float = 20.0) -> pl.Planner:  # noqa: ANN001
        broker = accountlease.VoiceBroker(self.http, BROKER, token_fetcher=self.broker_id_token)
        accounts = plannerlease.PlannerAccounts(broker, renew_seconds=self.renew_seconds)
        return pl.Planner(self.http, api_base=API, id_tokens=self.id_tokens, accounts=accounts,
                          claude=claude or self.launcher, turn_seconds=turn_seconds, updates=None,
                          warm=plannerwarm.WarmPlanners(accounts, **self.warm_kw))

    def count(self, what: str) -> int:
        return sum(1 for p in self.paths() if p == what or p.endswith(what))


@pytest.fixture
def world(tmp_path) -> Warm:
    return Warm(tmp_path)


def _run(world: Warm, scenario, **build):  # noqa: ANN001, ANN003
    async def main():  # noqa: ANN202
        planner = world.build(**build)
        try:
            return await scenario(planner)
        finally:
            await planner.aclose()
            await world.http.aclose()

    return asyncio.run(asyncio.wait_for(main(), 30))


async def _ask(planner: pl.Planner, world: Warm, *, grant: SessionGrant | None = None, person: str = PERSON,
               words: str = "Plan it.", model: str | None = None) -> pl.TurnResult:
    return await planner.delegate(person=person, grant=grant or world.grants[0], transcript=[("person", words)],
                                  delegation_id="item_" + secrets.token_hex(6), model=model)


async def _until(what) -> None:  # noqa: ANN001
    for _ in range(400):
        if what():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("it never happened")


# ---------------------------------------------------------------- one lease, one child


def test_two_delegated_turns_of_one_session_use_one_lease_and_one_child(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        first = await _ask(planner, world, words="Plan the first.")
        second = await _ask(planner, world, words="Plan the second.")
        assert world.paths() == [ASSIGN]
        return first, second

    first, second = _run(world, scenario)
    for result in (first, second):
        assert result.error is None and result.spoken.startswith("Warm answer.")
        assert result.usage == {"input_tokens": 100, "output_tokens": 7, "model": "claude-opus-5-5",
                                "tier": "plan", "billing": "subscription"}
    assert first.delegation_id != second.delegation_id
    (child,) = world.launcher.children
    assert len(child.said) == 2 and "Plan the first." in child.said[0] and "Plan the second." in child.said[1]
    # The data stays fenced on every turn, the second's window included.
    assert all(prompts.fence("transcript", f"person: Plan the {n}.") .splitlines()[0] in said
               for n, said in zip(("first", "second"), child.said))
    assert world.launcher.cold == []
    # A reused child owes a sign of life; a child just started does not.
    assert child.signs == [None, plannerwarm.SIGN_SECONDS]


def test_both_caps_are_asked_before_every_turn(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        await _ask(planner, world)

    _run(world, scenario)
    assert world.budget_asks == 2


def test_the_default_planner_and_its_warm_registry_are_built_together(monkeypatch):
    monkeypatch.setenv("SWARM_API_URL", API)
    monkeypatch.setenv("QUOTA_BROKER_URL", BROKER)

    async def main():  # noqa: ANN202
        async with httpx.AsyncClient() as http:
            return pl.build_planner(http)

    planner = asyncio.run(main())
    assert planner is not None and isinstance(planner._warm, plannerwarm.WarmPlanners)  # noqa: SLF001
    # The idle period lets go before an idle hold is renewed twice.
    assert 0 < plannerwarm.WARM_IDLE_SECONDS < plannerlease.RENEW_SECONDS


# ---------------------------------------------------------------- what releases both


def _released(world: Warm, *, turns: int = 1) -> None:
    assert world.launcher.order[-2:] == ["child closed", "lease released"]
    assert world.count(ASSIGN) == 1 and world.count(RELEASE) == 1
    assert all(c.closed for c in world.launcher.children)


@pytest.mark.parametrize("how", ["end", "pause", "cap", "brief refused at a cap"])
def test_an_end_a_pause_and_a_cap_each_release_the_child_then_the_lease(world, how):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        session = world.grants[0].session_id
        if how == "end":
            planner.forget(session)
        elif how == "pause":
            planner.cool(session, "paused")
        elif how == "cap":
            world.budget = {**world.budget, "capped": "session"}
            assert (await _ask(planner, world)).spoken in pl.CAP_SENTENCES.values()
        else:
            world.brief_reply = httpx.Response(409, json={"code": "conflict",
                                                          "detail": {"reason": pl.CAPPED_REASON}})
            api = pl.tools_read.SwarmApi.for_session(world.http, base_url=API, id_tokens=world.id_tokens,
                                                     grant=world.grants[0])
            await planner._write_brief(api, pl.TurnResult("d", "", brief="four steps"))  # noqa: SLF001
        await _until(lambda: world.count(RELEASE) == 1)

    _run(world, scenario)
    _released(world)


def test_the_idle_period_releases_both_and_the_next_turn_leases_again(tmp_path):
    world = Warm(tmp_path, idle_seconds=0.05)

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        assert world.count(RELEASE) == 0
        await _until(lambda: world.count(RELEASE) == 1)
        _released(world)
        await _ask(planner, world)

    _run(world, scenario)
    assert world.count(ASSIGN) == 2 and len(world.launcher.children) == 2 and world.launcher.cold == []


def test_a_running_turn_is_not_idle_and_its_end_starts_the_period_again(tmp_path):
    world = Warm(tmp_path, idle_seconds=0.05)
    world.launcher.script = ["speak", "wait"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        (child,) = world.launcher.children
        child.entered.clear()
        turn = asyncio.create_task(_ask(planner, world))
        await child.entered.wait()
        await asyncio.sleep(0.15)
        # MUTATION: leave the first turn's timer running and the child dies mid-turn.
        assert world.count(RELEASE) == 0 and child.alive
        child.go.set()
        assert (await turn).spoken.startswith("Warm answer.")
        await _until(lambda: world.count(RELEASE) == 1)

    _run(world, scenario)
    _released(world)


@pytest.mark.parametrize("how", ["end", "pause"])
def test_a_turn_running_when_the_session_ends_or_pauses_finishes_then_releases(world, how):
    world.launcher.script = ["wait"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        turn = asyncio.create_task(_ask(planner, world))
        await _until(lambda: world.launcher.children)
        (child,) = world.launcher.children
        await child.entered.wait()
        session = world.grants[0].session_id
        planner.forget(session) if how == "end" else planner.cool(session, "paused")
        await asyncio.sleep(0.02)
        assert child.alive and world.count(RELEASE) == 0
        child.go.set()
        assert (await turn).spoken.startswith("Warm answer.")
        await _until(lambda: world.count(RELEASE) == 1)

    _run(world, scenario)
    _released(world)


def test_a_turn_out_of_time_says_so_as_today_and_holds_nothing(world):
    world.launcher.script = ["speak", "hang"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        planner._turn_seconds = 0.1  # noqa: SLF001
        late = await _ask(planner, world)
        assert late.error == pl.TIMED_OUT and late.spoken == pl.SAY_FAILED
        _released(world)
        planner._turn_seconds = 20.0  # noqa: SLF001
        return await _ask(planner, world)

    after = _run(world, scenario)
    assert after.spoken.startswith("Warm answer.") and len(world.launcher.children) == 2


def test_a_cancelled_turn_releases_both(world):
    world.launcher.script = ["wait"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        turn = asyncio.create_task(_ask(planner, world))
        await _until(lambda: world.launcher.children)
        await world.launcher.children[0].entered.wait()
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        _released(world)

    _run(world, scenario)


def test_the_shutdown_releases_what_is_warm(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)

    _run(world, scenario)  # its close is the planner's `aclose`, as the app's shutdown calls it
    _released(world)


# ---------------------------------------------------------------- the fallbacks


def test_a_child_found_dead_is_replaced_by_the_turns_own_lease_and_child(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        world.launcher.children[0].dead = True
        second = await _ask(planner, world)
        assert second.error is None and second.spoken.startswith("Cold answer.")
        # The dead child's lease went back before the turn's own was taken, and that one after the turn.
        assert world.launcher.order == ["leased", "say", "child closed", "lease released",
                                        "leased", "cold", "lease released"]
        third = await _ask(planner, world)
        assert third.spoken.startswith("Warm answer.")

    _run(world, scenario)
    assert len(world.launcher.children) == 2


@pytest.mark.parametrize("step", ["die", "refused"])
def test_a_child_that_fails_mid_turn_before_doing_anything_falls_back(world, step):
    world.launcher.script = ["speak", step]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        second = await _ask(planner, world, words="Plan the second.")
        assert second.error is None and second.spoken.startswith("Cold answer.")
        assert world.launcher.order[2:] == ["say", "child closed", "lease released", "leased", "cold",
                                            "lease released"]

    _run(world, scenario)
    assert "Plan the second." in world.launcher.cold[0]


def test_a_child_that_dies_after_drawing_is_not_run_again(world):
    world.launcher.script = ["speak", "draw-die"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        second = await _ask(planner, world)
        # MUTATION: run it again and the person gets the card twice.
        assert second.error is not None and len(second.cards) == 1 and "I drew 1 card" in second.spoken
        await _until(lambda: world.count(RELEASE) == 1)

    _run(world, scenario)
    assert world.launcher.cold == [] and len(world.cards) == 1
    _released(world)


def test_a_write_still_running_when_the_child_dies_is_the_turns_and_is_not_run_again(world):
    world.launcher.script = ["speak", "draw-late-die"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        second = await _ask(planner, world)
        # MUTATION: decide before the write lands and the person gets the card twice.
        assert second.error is not None and len(second.cards) == 1

    _run(world, scenario)
    assert world.launcher.cold == [] and len(world.cards) == 1


def test_the_failed_attempts_tokens_are_charged_with_the_turns(world):
    world.launcher.script = ["speak", "used-die"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        return await _ask(planner, world)

    second = _run(world, scenario)
    assert second.spoken.startswith("Cold answer.")
    assert (second.usage["input_tokens"], second.usage["output_tokens"]) == (105, 8)


def test_an_error_from_a_child_just_started_is_todays_failure_and_is_not_run_again(world):
    world.launcher.script = ["refused"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        result = await _ask(planner, world)
        assert result.error is not None and result.spoken == pl.SAY_FAILED

    _run(world, scenario)
    assert world.launcher.cold == []
    _released(world)


def test_a_reused_child_is_told_the_interrupt_before_its_message_was_not_the_persons(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        await _ask(planner, world)

    _run(world, scenario)
    (child,) = world.launcher.children
    assert plannerwarm.REUSED_NOTE not in child.said[0] and child.said[1].startswith(plannerwarm.REUSED_NOTE)


def test_a_third_idle_session_has_the_longest_idle_one_let_go(tmp_path):
    world = Warm(tmp_path)
    grants = [world.grants[0], world.another(), world.another()]

    async def scenario(planner):  # noqa: ANN001, ANN202
        for grant in grants:
            assert (await _ask(planner, world, grant=grant)).spoken.startswith("Warm answer.")
        one, two, three = world.launcher.children
        await _until(lambda: one.closed and world.count(RELEASE) == 1)
        assert plannerwarm.MAX_IDLE == 2 and two.alive and three.alive

    _run(world, scenario)
    assert world.count(RELEASE) == 3


def test_a_child_that_cannot_start_falls_back_and_leaves_no_lease(world):
    world.launcher.no_start = True

    async def scenario(planner):  # noqa: ANN001, ANN202
        return await _ask(planner, world)

    result = _run(world, scenario)
    assert result.error is None and result.spoken.startswith("Cold answer.")
    assert world.count(ASSIGN) == world.count(RELEASE) == 2


@pytest.mark.parametrize("status", [404, 409])
def test_a_refused_renewal_stops_the_child_and_the_next_turn_leases_afresh(tmp_path, status):
    world = Warm(tmp_path, renew_seconds=0.05)
    world.renew_status = status

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        # Not held any more: no child may run on an account the pool counts free.
        await _until(lambda: world.launcher.children[0].closed and world.count(RELEASE) == 1)
        second = await _ask(planner, world)
        assert second.error is None and second.spoken.startswith("Cold answer.")

    _run(world, scenario)
    assert world.count(ASSIGN) == 2


def test_a_renewal_that_fails_past_the_holds_life_stops_the_child(tmp_path):
    world = Warm(tmp_path, renew_seconds=0.02)
    world.renew_status = 503

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        await _until(lambda: world.launcher.children[0].closed)

    _run(world, scenario)
    # One failed renewal is retried: the hold outlives it.
    assert world.count("/renew") >= 2


def test_a_hold_lost_while_a_turn_runs_lets_that_turn_finish(tmp_path):
    world = Warm(tmp_path, renew_seconds=0.02)
    world.renew_status = 409
    world.launcher.script = ["wait"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        turn = asyncio.create_task(_ask(planner, world))
        await _until(lambda: world.count("/renew") >= 1)
        await asyncio.sleep(0.02)
        (child,) = world.launcher.children
        assert child.alive
        child.go.set()
        assert (await turn).spoken.startswith("Warm answer.")
        await _until(lambda: child.closed)

    _run(world, scenario)


@pytest.mark.parametrize("reason,say", [(plannerlease.NO_ACCOUNT_AVAILABLE, pl.SAY_POOL_FULL),
                                        (plannerlease.NO_ACCOUNTS_REGISTERED, pl.SAY_NO_POOL)])
def test_a_refused_lease_is_todays_spoken_refusal_asked_once(world, reason, say):
    world.refuse = reason

    async def scenario(planner):  # noqa: ANN001, ANN202
        result = await _ask(planner, world)
        assert result.spoken == say and reason in result.error
        assert world.paths() == [ASSIGN] and world.launcher.children == []
        # The refusal is that turn's alone.
        world.refuse = None
        assert (await _ask(planner, world)).spoken.startswith("Warm answer.")

    _run(world, scenario)


def test_a_spent_account_is_left_for_the_next_one_as_today(world):
    world.launcher.script = ["speak", "spent"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        second = await _ask(planner, world)
        assert second.error is None and second.spent is None and second.spoken.startswith("Cold answer.")

    _run(world, scenario)
    assert [p.split("/")[-1] for p in world.paths()] == ["assign", "release", "assign", "release"]
    assert "exclude" not in world.broker_calls[0][1] and world.broker_calls[2][1]["exclude"] == ["acct-1"]


def test_a_spent_account_with_no_tier_left_says_the_pool_is_full(world):
    world.launcher.script = ["speak", "spent"]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        world.refuse = plannerlease.NO_ACCOUNT_AVAILABLE
        second = await _ask(planner, world)
        assert second.spoken == pl.SAY_POOL_FULL and "no tier is left" in second.error

    _run(world, scenario)


# ---------------------------------------------------------------- nothing crosses a session


def test_two_sessions_never_share_a_child_or_a_lease(world):
    other = world.another()

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world, words="alpha words")
        await _ask(planner, world, grant=other, words="beta words")
        await _ask(planner, world, words="alpha again")
        one, two = world.launcher.children
        assert len(one.said) == 2 and len(two.said) == 1
        assert not any("beta" in said for said in one.said) and not any("alpha" in said for said in two.said)
        # One session's end takes only its own.
        planner.forget(other.session_id)
        await _until(lambda: two.closed)
        assert one.alive and world.count(RELEASE) == 1

    _run(world, scenario)
    sessions = [b["voice_session_id"] for p, b in world.broker_calls if p == ASSIGN]
    assert sessions == [world.grants[0].session_id, other.session_id]


@pytest.mark.parametrize("what", ["person", "project", "grant", "model"])
def test_another_person_project_grant_or_model_gets_a_child_of_its_own(world, what):
    first = world.grants[0]
    project = "vp_" + secrets.token_hex(16) if what == "project" else first.project_id
    value = secrets.token_urlsafe(32) if what in ("grant", "project") else None

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        grant = first
        if value is not None:
            grant = SessionGrant(project_id=project, session_id=first.session_id, value=value)
            world.grants[0] = grant
        await _ask(planner, world, grant=grant, person="other@example.com" if what == "person" else PERSON,
                   model="claude-sonnet-5-5" if what == "model" else None)
        one, two = world.launcher.children
        assert one.closed and len(one.said) == 1 and two.alive and len(two.said) == 1
        # The old one let go before the new one was leased: never two at once.
        assert world.launcher.order[2:5] == ["child closed", "lease released", "leased"]

    _run(world, scenario)


def test_a_call_between_turns_or_after_speak_is_refused_and_writes_nothing(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        text, is_error = await world.launcher.children[0].handler("draw_details_input", _details_card())
        assert is_error and text.startswith("refused")

    _run(world, scenario)
    # MUTATION: leave the last turn's handler in place and a late call draws on the next person's screen.
    assert world.cards == []


def test_no_repr_names_a_token_or_a_grant(world):
    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        warm = planner._warm  # noqa: SLF001
        shown = repr(warm) + repr(list(warm._slots.values())) + repr(warm._held)  # noqa: SLF001
        assert not any(secret in shown for secret in world.secret_values())

    _run(world, scenario)


# ---------------------------------------------------------------- who tells the planner


def test_the_roster_pass_that_reads_a_cap_tells_it_once_per_pass():
    told: list[str] = []

    class Reads:
        capped: str | None = None

        async def tagged(self, project_id: str) -> list:
            return []

        async def started(self, items: list) -> list:
            return []

        async def budget(self) -> dict:
            return {"warned": [], "capped": self.capped}

    reads = Reads()
    follower = follow.Follower(voice_id="vs_" + secrets.token_hex(8), project_id="vp_" + secrets.token_hex(8),
                               reads=reads, on_capped=told.append)
    asyncio.run(follower.tick())
    assert told == []
    reads.capped = "session"
    asyncio.run(follower.tick())
    assert told == [follower.voice_id]


def test_the_deployed_roster_tells_the_planner_of_a_cap(monkeypatch):
    monkeypatch.setenv("SWARM_API_URL", API)
    told: list[str] = []
    roster = follow.build_roster(None, on_capped=told.append)
    assert roster is not None and roster._on_capped == told.append  # noqa: SLF001


def test_the_apps_shutdown_closes_the_planner_before_its_client(monkeypatch):
    closed: list[str] = []

    class Planner:
        async def aclose(self) -> None:
            closed.append("planner")

    class Sessions:
        _planner = Planner()

        async def end_all(self) -> None:
            closed.append("sessions")

    app = voice_main.create_app(sessions=Sessions(), verify_caller=lambda *_a: None)

    async def main() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(main())
    assert closed == ["sessions", "planner"]


# ---------------------------------------------------------------- the wire: one process, real pipes

#: A `claude -p --input-format stream-json` that plans one turn per user
#: message: it calls the real MCP server's `speak`, echoes the tool's result,
#: then writes on until it is interrupted, as the CLI does.
FAKE = r'''#!{python}
import json, os, subprocess, sys
mode = open({mode!r}).read()
argv = sys.argv[1:]
server = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]["planner"]
env = dict(os.environ); env.update(server.get("env") or {{}})
proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        env=env, text=True)
seq = [0]
def rpc(method, params=None):
    seq[0] += 1
    proc.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params or {{}}}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())
def out(event):
    print(json.dumps(event), flush=True)
rpc("initialize")
turn = 0
for line in sys.stdin:
    msg = json.loads(line)
    if msg["type"] == "control_request":
        out({{"type": "control_response", "response": {{"subtype": "success", "request_id": msg["request_id"]}}}})
        with open({log!r}, "a") as fh:
            fh.write(msg["request"]["subtype"] + " " + str(os.getpid()) + "\n")
        out({{"type": "result", "is_error": True, "subtype": "error_during_execution", "result": "",
             "usage": {{"input_tokens": 10 * turn, "output_tokens": turn}}}})
        continue
    turn += 1
    if turn > 1 and mode == "silent":
        continue
    out({{"type": "system", "subtype": "init"}})
    out({{"type": "assistant", "message": {{"id": "m%d" % turn, "usage": {{"input_tokens": 1, "output_tokens": 1}},
         "content": [{{"type": "tool_use", "id": "t%d" % turn, "name": "mcp__planner__speak", "input": {{}}}}]}}}})
    rpc("tools/call", {{"name": "speak", "arguments": {{"summary": "Turn %d." % turn}}}})
    if turn == 1 and mode == "dies after speak":
        sys.exit(1)
    if mode == "never settles":
        continue
    out({{"type": "user", "message": {{"role": "user", "content": [
        {{"type": "tool_result", "tool_use_id": "t%d" % turn, "content": "said"}}]}}}})
'''


def _launcher(tmp: Path, mode: str) -> claudeturn.ClaudeCode:
    (tmp / "mode").write_text(mode)
    script = tmp / "claude"
    script.write_text(FAKE.format(python=sys.executable, mode=str(tmp / "mode"), log=str(tmp / "log")))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return claudeturn.ClaudeCode(binary=str(script), model="claude-opus-5-5")


def test_one_process_answers_two_turns_each_ended_by_an_interrupt_with_its_own_usage(world, tmp_path):
    claude = _launcher(tmp_path, "")
    homes: list[str] = []

    async def scenario(planner):  # noqa: ANN001, ANN202
        first = await _ask(planner, world)
        homes.append(next(iter(planner._warm._held)).conversation.home)  # noqa: SLF001
        second = await _ask(planner, world)
        return first, second

    first, second = _run(world, scenario, claude=claude)
    assert first.spoken.startswith("Turn 1.") and second.spoken.startswith("Turn 2.")
    # Each turn's usage is its own: the CLI's result for that message.
    assert (first.usage["input_tokens"], second.usage["input_tokens"]) == (10, 20)
    lines = (tmp_path / "log").read_text().splitlines()
    assert len(lines) == 2 and len(set(lines)) == 1 and lines[0].startswith("interrupt ")
    assert world.count(ASSIGN) == world.count(RELEASE) == 1
    assert not os.path.exists(homes[0])
    with pytest.raises(ProcessLookupError):
        os.kill(int(lines[0].split()[1]), 0)


def test_a_child_that_gives_no_sign_of_life_is_replaced_in_that_turn(tmp_path):
    world = Warm(tmp_path, sign_seconds=0.2)
    claude = _launcher(tmp_path, "silent")
    cold: list[str] = []

    async def run(**kw):  # noqa: ANN003, ANN202
        cold.append(kw["prompt"])
        await kw["handler"](prompts.SPEAK_TOOL, {"summary": "Cold answer."})
        return claudeturn.ChildOutcome(exit_code=0, stopped=True)

    claude.run = run  # type: ignore[method-assign]

    async def scenario(planner):  # noqa: ANN001, ANN202
        await _ask(planner, world)
        return await _ask(planner, world)

    second = _run(world, scenario, claude=claude)
    assert second.error is None and second.spoken.startswith("Cold answer.") and len(cold) == 1
    assert world.count(ASSIGN) == world.count(RELEASE) == 2


@pytest.mark.parametrize("mode", ["dies after speak", "never settles"])
def test_a_child_that_does_not_settle_after_speak_still_answers_and_is_not_reused(world, tmp_path, monkeypatch,
                                                                                  mode):
    monkeypatch.setattr(claudeturn, "STOP_GRACE_SECONDS", 0.3)
    claude = _launcher(tmp_path, mode)

    async def scenario(planner):  # noqa: ANN001, ANN202
        first = await _ask(planner, world)
        assert first.error is None and first.spoken.startswith("Turn 1.")
        await _until(lambda: world.count(RELEASE) == 1)

    _run(world, scenario, claude=claude)
    assert world.count(ASSIGN) == 1


# ---------------------------------------------------------------- the latency record


def test_first_audio_is_still_attributed_to_its_own_turn_with_a_warm_planner(world):
    """Two delegated turns on one warm child: each reply's first audio closes
    its own turn's record, taken from the sideband and not the child."""
    clock = [0.0]
    written: list[dict] = []

    async def scenario(planner):  # noqa: ANN001, ANN202
        async def keep(grant, record) -> bool:  # noqa: ANN001
            written.append(record)
            return True

        planner.write_latency = keep
        session = live.LiveSession(world.grants[0].session_id, "live_" + secrets.token_hex(4), credential=None,
                                   api=None, on_event=None, on_closed=lambda s: None, person=PERSON,
                                   planner=planner, clock=lambda: clock[0])
        session.grant = world.grants[0]
        for span in (0.7, 1.2):
            session._note_latency("input_audio_buffer.speech_stopped", {})  # noqa: SLF001
            assert (await _ask(planner, world)).spoken.startswith("Warm answer.")
            clock[0] += span
            session._note_latency("session.output_audio.started", {})  # noqa: SLF001
        await asyncio.gather(*list(session._trace_writes), return_exceptions=True)  # noqa: SLF001

    _run(world, scenario)
    assert [r["span_ms"] for r in written] == [700.0, 1200.0]
    assert len({r["turn_id"] for r in written}) == 2 and len(world.launcher.children) == 1
