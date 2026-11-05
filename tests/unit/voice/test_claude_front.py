"""Own speech's fast Claude front (swarm_voice.claudefront;
docs/design/voice-it-own-speech.md §3.3 step 4, §14 L12, §14.1).

With a fake session-long child, a fake planner and a fake quota broker on one
`httpx.MockTransport` (which refuses chatgpt.com and api.openai.com), this
holds that:

* the front's reply comes back before a slow planner ends, carrying the
  delegation's id, and `GET /delegations/{id}` serves the planner's answer;
* `delegate` runs `Planner.delegate` with the session's person and grant;
* delegations run one at a time per session, at most two waiting;
* no request reaches an OpenAI host;
* one lease for the session (`holder: voice-front`), renewed, released on
  end, idle and `end_all`; a renewal the broker refuses stops the child;
* a full pool is a refusal with nothing held and no child started;
* a dead child re-leases once excluding its account, then is `FrontDown`;
* usage is `by_model` with both pins;
* no token in a repr or a log.

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

from swarm_voice import accountlease, claudefront, claudeturn, plannerlease  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SessionGrant  # noqa: E402

PERSON = "person@example.com"
BROKER = "http://broker.test"
OPENAI_HOSTS = {"chatgpt.com", "api.openai.com"}
FRONT_PIN = "claude-haiku-5-5"
PLANNER_PIN = "claude-fable-5-1"


class Sleeper:
    """`asyncio.sleep` that waits for the test to `tick`."""

    def __init__(self) -> None:
        self.waiting: list[asyncio.Future] = []

    async def __call__(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waiting.append(future)
        await future

    async def tick(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        waiting, self.waiting = self.waiting, []
        for future in waiting:
            if not future.done():
                future.set_result(None)
        for _ in range(200):
            await asyncio.sleep(0)


class Broker:
    """The quota broker's assign, renew and release, on a MockTransport that
    refuses every OpenAI host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.hosts: list[str] = []
        self.tokens: dict[str, str] = {}
        self.accounts = ["acct-1", "acct-2"]
        self.full = False
        self.renew_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        if request.url.host in OPENAI_HOSTS:
            return httpx.Response(599, json={"refused": "an OpenAI host"})
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path, body))
        if request.url.path == "/v1/accounts/assign":
            free = [a for a in self.accounts if a not in body.get("exclude", [])]
            if self.full or not free:
                return httpx.Response(200, json={"account_id": None, "assignment_id": None,
                                                 "credential": None, "reason": "no_account_available"})
            token = "t" + secrets.token_hex(20)
            self.tokens[free[0]] = token
            return httpx.Response(200, json={"account_id": free[0], "assignment_id": "as-" + secrets.token_hex(4),
                                             "credential": {"access_token": token}, "reason": ""})
        if request.url.path.endswith("/renew"):
            return httpx.Response(self.renew_status, json={})
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json={"released": True})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] if p.startswith("/v1/voice/lease/") else p for p, _ in self.calls]


class FakeConversation:
    def __init__(self, handler, token: str, script: "FakeClaude") -> None:  # noqa: ANN001
        self.handler, self.token, self.script = handler, token, script
        self.closed = False
        self.dead = False
        self.said: list[str] = []
        self.home = "/nonexistent"

    @property
    def alive(self) -> bool:
        return not self.closed and not self.dead

    async def say(self, text: str) -> claudeturn.ChildOutcome:
        if not self.alive:
            raise claudeturn.ChildGone("gone")
        if self.script.die_next:
            self.script.die_next -= 1
            self.dead = True
            raise claudeturn.ChildGone("gone")
        self.said.append(text)
        reply = "Hi there."
        if "plan" in text:
            answer, is_error = await self.handler("delegate", {"request": text})
            reply = "Looking at that now." if not is_error else "Too much is waiting; give me a moment."
        return claudeturn.ChildOutcome(result_text=reply, usage={"input_tokens": 5, "output_tokens": 1})

    async def close(self) -> None:
        self.closed = True


class FakeClaude:
    model = FRONT_PIN

    def __init__(self) -> None:
        self.started: list[FakeConversation] = []
        self.systems: list[str] = []
        self.tools: list[list[dict]] = []
        self.die_next = 0
        self.fail_start = False

    async def converse(self, *, system, token, tools, handler, known):  # noqa: ANN001
        if self.fail_start:
            raise FileNotFoundError("claude")
        self.systems.append(system)
        self.tools.append(tools)
        conversation = FakeConversation(handler, token, self)
        self.started.append(conversation)
        return conversation


class FakePlanner:
    """`Planner.delegate`, held until the test lets it finish."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.gate = asyncio.Event()
        self.forgotten: list[str] = []
        self.running = 0
        self.most = 0

    async def delegate(self, *, person, grant, transcript, delegation_id=None):  # noqa: ANN001
        self.calls.append({"person": person, "grant": grant, "transcript": list(transcript),
                           "delegation_id": delegation_id})
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            await self.gate.wait()
        finally:
            self.running -= 1
        return SimpleNamespace(spoken="It's on screen.", cards=[{"kind": "plan", "card_id": "vc_1"}], error=None,
                               usage={"input_tokens": 100, "output_tokens": 20, "model": PLANNER_PIN,
                                      "billing": "subscription"})

    def forget(self, session_id: str) -> None:
        self.forgotten.append(session_id)


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id="vs_" + secrets.token_hex(16),
                        value=secrets.token_urlsafe(32))


class World:
    def __init__(self) -> None:
        self.broker = Broker()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.broker.handler))

        async def id_token(audience: str) -> str:
            return "i" + secrets.token_hex(12)

        voice_broker = accountlease.VoiceBroker(self.http, BROKER, token_fetcher=id_token)
        self.accounts = plannerlease.PlannerAccounts(voice_broker, holder=plannerlease.FRONT_HOLDER)
        self.claude = FakeClaude()
        self.planner = FakePlanner()
        self.sleep = Sleeper()
        self.now = [1000.0]
        self.front = claudefront.FrontSessions(accounts=self.accounts, planner=self.planner, claude=self.claude,
                                               clock=lambda: self.now[0], idle_seconds=900.0, sleep=self.sleep)


def run(coro_fn):
    async def main():
        world = World()
        try:
            return await coro_fn(world)
        finally:
            await world.front.end_all()
            await world.http.aclose()
    return asyncio.run(main())


def test_the_reply_comes_back_before_a_slow_planner_ends():
    async def body(w: World):
        grant = _grant()
        await w.front.start("vs-1", person=PERSON, grant=grant)
        answer = await w.front.turn("vs-1", "plan the fix for the login bug")
        assert answer.reply == "Looking at that now." and answer.delegation_id
        for _ in range(5):
            await asyncio.sleep(0)
        delegation = w.front.delegation(answer.delegation_id)
        assert delegation.state == "running" and not w.planner.gate.is_set()
        # A newer sentence never waits for it.
        assert (await w.front.turn("vs-1", "thanks")).reply == "Hi there."

        call = w.planner.calls[0]
        assert call["person"] == PERSON and call["grant"] is grant
        assert call["delegation_id"] == answer.delegation_id
        assert call["transcript"][-1] == ("person", "plan the fix for the login bug")

        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(claudefront.delegations_router(w.front, lambda: "swarm-api"))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice.test")
        pending = await client.get(f"/delegations/{answer.delegation_id}")
        assert pending.json()["state"] == "running"
        waiter = asyncio.create_task(client.get(f"/delegations/{answer.delegation_id}", params={"wait": 30}))
        await asyncio.sleep(0)
        w.planner.gate.set()
        done = (await waiter).json()
        assert done["state"] == "done" and done["spoken"] == "It's on screen."
        assert done["cards"] == [{"kind": "plan", "card_id": "vc_1"}]
        assert (await client.get("/delegations/vd_unknown")).status_code == 404
        await client.aclose()
    run(body)


def test_delegations_run_one_at_a_time_and_at_most_two_wait():
    async def body(w: World):
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        ids = [(await w.front.turn("vs-1", f"plan step {n}")).delegation_id for n in range(4)]
        assert all(ids[:3]) and ids[3] is None
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(w.planner.calls) == 1 and w.front.counts()["front_delegations"] == 3
        w.planner.gate.set()
        for delegation_id in ids[:3]:
            await asyncio.wait_for(w.front.delegation(delegation_id).done.wait(), 5)
        assert w.planner.most == 1 and len(w.planner.calls) == 3
    run(body)


def test_one_lease_for_the_session_renewed_and_released_on_end():
    async def body(w: World):
        session = await w.front.start("vs-1", person=PERSON, grant=_grant())
        for text in ("hello", "how are you", "and now"):
            await w.front.turn("vs-1", text)
        assert len(w.claude.started) == 1 and w.claude.started[0].said == ["hello", "how are you", "and now"]
        await w.sleep.tick()
        await w.sleep.tick()
        assert w.broker.paths() == ["/v1/accounts/assign", "renew", "renew"]
        assign = w.broker.calls[0][1]
        assert assign == {"provider": "anthropic", "holder": "voice-front", "person": PERSON,
                          "voice_session_id": "vs-1"}
        assert all(body["voice_session_id"] == "vs-1" for _, body in w.broker.calls[1:])
        await w.front.end("vs-1")
        assert w.broker.paths()[-1] == "release" and w.claude.started[0].closed
        assert session.grant is None and w.planner.forgotten == ["vs-1"]
        assert w.front.counts()["front_sessions"] == 0
        assert not (set(w.broker.hosts) & OPENAI_HOSTS) and set(w.broker.hosts) == {"broker.test"}
    run(body)


def test_idle_and_end_all_release_the_lease():
    async def body(w: World):
        await w.front.start("vs-1", person=PERSON)
        await w.front.start("vs-2", person=PERSON)
        w.now[0] += 901
        await w.sleep.tick()
        assert w.front.get("vs-1") is None and w.front.get("vs-2") is None
        assert w.broker.paths().count("release") == 2

    run(body)

    async def shutdown(w: World):
        await w.front.start("vs-3", person=PERSON)
        await w.front.end_all()
        assert w.broker.paths() == ["/v1/accounts/assign", "release"] and w.claude.started[0].closed

    run(shutdown)


def test_a_refused_renewal_stops_the_child_and_the_next_sentence_re_leases():
    async def body(w: World):
        await w.front.start("vs-1", person=PERSON)
        w.broker.renew_status = 409
        await w.sleep.tick()
        assert w.claude.started[0].closed
        answer = await w.front.turn("vs-1", "hello again")
        assert answer.reply == "Hi there." and len(w.claude.started) == 2
        assigns = [b for p, b in w.broker.calls if p == "/v1/accounts/assign"]
        assert assigns[1]["exclude"] == ["acct-1"]
        assert "hello again" in w.claude.started[1].said[0]
    run(body)


def test_a_full_pool_is_a_refusal_with_nothing_started():
    async def body(w: World):
        w.broker.full = True
        with pytest.raises(claudefront.FrontRefused) as refused:
            await w.front.start("vs-1", person=PERSON)
        assert refused.value.reason == "no_account_available"
        assert w.claude.started == [] and w.front.get("vs-1") is None

        w.broker.full = False
        w.claude.fail_start = True
        with pytest.raises(claudefront.FrontRefused):
            await w.front.start("vs-1", person=PERSON)
        assert w.broker.paths()[-1] == "release" and w.front.get("vs-1") is None
    run(body)


def test_a_dead_child_re_leases_once_then_is_engine_down():
    async def body(w: World):
        await w.front.start("vs-1", person=PERSON)
        await w.front.turn("vs-1", "first")
        w.claude.die_next = 1
        answer = await w.front.turn("vs-1", "second")
        assert answer.reply == "Hi there." and len(w.claude.started) == 2
        resumed = w.claude.started[1].said[0]
        assert "first" in resumed and resumed.endswith("second")
        assigns = [b for p, b in w.broker.calls if p == "/v1/accounts/assign"]
        assert assigns[1]["exclude"] == ["acct-1"]
        w.claude.die_next = 1
        with pytest.raises(claudefront.FrontDown):
            await w.front.turn("vs-1", "third")
        # Re-leased once already: the account is given back, no third lease.
        assert w.broker.paths().count("/v1/accounts/assign") == 2
        assert w.broker.paths().count("release") == 2
    run(body)


def test_usage_is_by_model_with_both_pins():
    async def body(w: World):
        session = await w.front.start("vs-1", person=PERSON, grant=_grant())
        answer = await w.front.turn("vs-1", "plan it")
        w.planner.gate.set()
        await asyncio.wait_for(w.front.delegation(answer.delegation_id).done.wait(), 5)
        report = session.usage_report()
        assert report["billing"] == "subscription"
        assert report["by_model"] == {FRONT_PIN: {"input_tokens": 5, "output_tokens": 1},
                                      PLANNER_PIN: {"input_tokens": 100, "output_tokens": 20}}
    run(body)


def test_the_front_runs_the_haiku_pin_and_offers_only_delegate(monkeypatch):
    monkeypatch.delenv("VOICE_FRONT_MODEL", raising=False)
    world = World()
    front = claudefront.FrontSessions(accounts=world.accounts)
    assert front.model == FRONT_PIN
    with pytest.raises(ValueError):
        claudefront.FrontSessions(accounts=plannerlease.PlannerAccounts(world.accounts._broker))  # noqa: SLF001

    async def body(w: World):
        await w.front.start("vs-1", person=PERSON)
        assert [t["name"] for t in w.claude.tools[0]] == ["delegate"]
        assert "delegate" in w.claude.systems[0]
    run(body)
    asyncio.run(world.http.aclose())


def test_no_token_in_a_repr_or_a_log(caplog):
    caplog.set_level(logging.DEBUG)

    async def body(w: World):
        grant = _grant()
        session = await w.front.start("vs-1", person=PERSON, grant=grant)
        answer = await w.front.turn("vs-1", "plan it")
        w.planner.gate.set()
        delegation = w.front.delegation(answer.delegation_id)
        await asyncio.wait_for(delegation.done.wait(), 5)
        token = w.broker.tokens["acct-1"]
        for value in (session, session.lease, delegation, w.claude.started[0], answer):
            assert token not in repr(value) and grant.headers()[GRANT_HEADER] not in repr(value)
        await w.front.end("vs-1")
        assert token not in caplog.text and grant.headers()[GRANT_HEADER] not in caplog.text
        assert "plan it" not in caplog.text
    run(body)


def test_the_holder_is_voice_front_or_voice_planner_and_nothing_else():
    world = World()
    broker = world.accounts._broker  # noqa: SLF001
    assert plannerlease.PlannerAccounts(broker).holder == "voice-planner"
    assert plannerlease.PlannerAccounts(broker, holder="voice-front").holder == "voice-front"
    with pytest.raises(ValueError):
        plannerlease.PlannerAccounts(broker, holder="worker")
    asyncio.run(world.http.aclose())
