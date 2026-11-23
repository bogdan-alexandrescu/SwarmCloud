"""One own-speech Voice it session, start to end, through the real service code
of both services (docs/design/voice-it.md §3.6, §3.8, §3.12.5;
docs/design/voice-it-own-speech.md §3.3).

swarm-api's `VoiceSessions`, `VoiceLedger`, `BudgetGuard`, the speech relay's
`ClientTurns` and the grant's `resolve` and `record_work` run as they are;
so do swarm-voice's `FrontSessions`, `Planner`, `WorkSink` and
`PlannerAccounts`. Only the edges are fakes, all built at runtime:

* storage: the in-memory Firestore; Compute: the speech machine's fake;
* the clock: one fake wall clock for swarm-api, the speech machine and the
  ledger, and a sleep that never fires for swarm-voice's renewals;
* speech: the recognised text of an utterance, handed to the relay's turn;
* the model calls: the fast front's warm child and the planner's child;
* the quota broker, and the two HTTP hops between the services (each a thin
  dispatcher onto the real code, as swarm-voice's and swarm-api's routes are).

The walk holds that a person opens a session; a spoken turn's recognised text
is answered by the fast front at once; its one request reaches the planner,
whose line comes back and is relayed; the ledger records what the session
costs while it runs and stops when it ends; the cap warns once at 80 percent
and refuses work at the cap; and the session ends and is marked ended.

No network, no cloud credential, no emulator, no real audio.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane.fakes import FakeFirestore  # noqa: E402
from test_claude_front import BROKER, Broker, FakeClaude, Sleeper  # noqa: E402
from test_speech_machine import CONFIG, Clock, FakeCompute, _machine  # noqa: E402

from swarm_api import modelprices  # noqa: E402
from swarm_api.errors import ApiError  # noqa: E402
from swarm_api.voice import budget as vbudget  # noqa: E402
from swarm_api.voice import cards as vcards  # noqa: E402
from swarm_api.voice import grant as vgrant  # noqa: E402
from swarm_api.voice import ledger as vledger  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402
from swarm_api.voice import speech_relay as sr  # noqa: E402
from swarm_api.voice import store as vstore  # noqa: E402
from swarm_api.voice import workfeed as vwork  # noqa: E402
from swarm_voice import accountlease, claudefront, claudeturn, plannerlease, prompts, worksink  # noqa: E402
from swarm_voice.grant import SessionGrant  # noqa: E402
from swarm_voice.planner import Planner  # noqa: E402

TENANT = "eng"
PERSON = "person@example.com"
API = "http://swarm-api.test"
CHOSEN = dict(cat.defaults((CONFIG.zone,)))
#: What the person says; the fake front delegates any turn that asks to plan.
FIRST_WORDS = "plan the release checklist for the billing service"
LATER_WORDS = "plan one more review of the release"
#: The planner's spoken summary.
PLANNER_LINE = "The release checklist has four steps."
#: The ledger's cap check runs once per simulated hour of talk.
HOUR = 3600.0


class PlannerModel:
    """The planner's Claude Code child: it reads the turn's prompt and calls
    `speak` once, as the real CLI's model would, and reports token counts."""

    def __init__(self, model: str = "claude-opus-5-5") -> None:
        self.model = model
        self.prompts: list[str] = []

    def with_model(self, model: str) -> "PlannerModel":
        if model == self.model:
            return self
        other = PlannerModel(model)
        other.prompts = self.prompts
        return other

    async def run(self, *, prompt: str, system: str, token: str, tools: list[dict], handler: Any,
                  known: tuple[str, ...], done: asyncio.Event) -> claudeturn.ChildOutcome:
        self.prompts.append(prompt)
        await handler(prompts.SPEAK_TOOL, {"summary": PLANNER_LINE, "point_at": None})
        return claudeturn.ChildOutcome(exit_code=0, usage={"input_tokens": 1200, "output_tokens": 300},
                                       stopped=True)


class SwarmApiEdge:
    """swarm-api as swarm-voice reaches it, on a MockTransport: every request
    is resolved through the real session grant (`vgrant.resolve`), and the
    two routes the scenario uses do what routes/voice.py does with them."""

    def __init__(self, db: FakeFirestore, guard: vbudget.BudgetGuard, clock: Clock) -> None:
        self.db, self.guard, self.clock = db, guard, clock
        self.seen: list[tuple[str, str, int]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        self.seen.append((request.method, request.url.path, response.status_code))
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        try:
            session = vgrant.resolve(self.db, session_header=request.headers.get(vgrant.SESSION_HEADER),
                                     grant=request.headers.get(vgrant.GRANT_HEADER))
            path = request.url.path
            if request.method == "GET" and path == "/v1/voice/cards/schema":
                return httpx.Response(200, json=vcards.catalogue())
            if request.method == "POST" and path == f"/v1/voice/sessions/{session.session_id}/work":
                return httpx.Response(200, json=self._work(session, _json(request)))
            return httpx.Response(404, json={"code": "not_found", "message": "not in this scenario"})
        except ApiError as exc:
            return httpx.Response(exc.status_code, json=exc.to_payload())

    def _work(self, session: vgrant.GrantSession, body: dict[str, Any]) -> dict[str, Any]:
        where = {"tenant_id": session.tenant_id, "person": session.person,
                 "project_id": session.project_id, "session_id": session.session_id}
        if body.get("budget") is True:
            return {"session_id": session.session_id, "budget": self.guard.enforce(**where)}
        delegation = body.get("delegation")
        state = None
        if isinstance(delegation, dict) and delegation.get("state") == vwork.STARTED:
            state = self.guard.admit_work(**where)
        answer = vgrant.record_work(self.db, session, session_id=session.session_id,
                                    body={"delegation": delegation}, at=self.clock.now())
        if state is None:
            state = self.guard.enforce(**where)
        return {**answer, "budget": state}


def _json(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content or b"{}")
    return body if isinstance(body, dict) else {}


class VoiceBridge:
    """swarm-api's session client (`VoiceSessions`' and the relay's), handed
    to swarm-voice's real fast front in process, as swarm-voice's
    `/internal/sessions`, `/turns`, `/end` and `/delegations/{id}` hand it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, front: claudefront.FrontSessions) -> None:
        self.loop, self.front = loop, front
        self.ends: list[tuple[str, str]] = []
        #: Each session grant swarm-api handed over at a start.
        self.grants: list[SessionGrant] = []

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def start(self, call: vs.StartCall) -> vs.StartAnswer:
        assert call.mode == vs.MODE_SPEECH
        grant = SessionGrant(project_id=call.project_id, session_id=call.session_id, value=call.grant)
        self.grants.append(grant)
        self.run(self.front.start(call.session_id, person=call.person, grant=grant,
                                  planner_model=call.planner_model,
                                  conversation_model=call.conversation_model, debug=call.debug))
        return vs.StartAnswer(answer="", live_session_id="", mode=vs.MODE_SPEECH)

    def turn(self, session_id: str, text: str) -> vs.TurnAnswer:
        answered = self.run(self.front.turn(session_id, text))
        return vs.TurnAnswer(turn_id=answered.turn_id, reply=answered.reply, cards=list(answered.cards),
                             delegation_id=answered.delegation_id)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        found = self.front.delegation(delegation_id)
        if found is None:
            return None

        async def wait() -> None:
            await asyncio.wait_for(found.done.wait(), timeout)

        self.run(wait())
        body = found.to_api()
        if body["voice_session_id"] != session_id or body["state"] != "done":
            return None
        return body["spoken"] or None

    def end(self, session_id: str, *, cause: str) -> bool:
        self.ends.append((session_id, cause))
        return self.run(self.front.end(session_id)) is not None


class World:
    """Both services, wired to each other and to nothing else."""

    def __init__(self) -> None:
        self.db = FakeFirestore()
        self.clock = Clock()
        self.compute = FakeCompute(running_after=1)
        self.machine = _machine(self.db, self.compute, self.clock)
        self.ledger = vledger.VoiceLedger(self.db, now=self.clock.now, prices=lambda: modelprices.SEED,
                                          time_zone=lambda _email: None)
        self.loop = asyncio.new_event_loop()
        self.broker = Broker()
        self.guard = vbudget.BudgetGuard(self.db, self.ledger, now=self.clock.now,
                                         end=lambda **where: self.sessions.end_for_budget(**where))
        self.api = SwarmApiEdge(self.db, self.guard, self.clock)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._route))

        async def id_token(*_audience: str) -> str:
            return "i" + secrets.token_hex(16)

        voice_broker = accountlease.VoiceBroker(self.http, BROKER, token_fetcher=id_token)
        self.planner_model = PlannerModel()
        self.planner = Planner(self.http, api_base=API, id_tokens=id_token,
                               accounts=plannerlease.PlannerAccounts(voice_broker), claude=self.planner_model,
                               updates=None)
        self.sink = worksink.WorkSink(self.http, api_base=API, id_tokens=id_token)
        self.front_model = FakeClaude()
        self.front = claudefront.FrontSessions(
            accounts=plannerlease.PlannerAccounts(voice_broker, holder=plannerlease.FRONT_HOLDER),
            planner=self.planner, claude=self.front_model, clock=self.clock.clock, sleep=Sleeper(),
            work=self.sink)
        self.voice = VoiceBridge(self.loop, self.front)
        self.sessions = vs.VoiceSessions(self.db, self.voice, now=self.clock.now, class_models={},
                                         machine=self.machine)
        self.relay = sr.ClientTurns(self.voice)

    def _route(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "swarm-api.test":
            return self.api.handler(request)
        return self.broker.handler(request)

    def close(self) -> None:
        async def shut() -> None:
            await self.front.end_all()
            await self.sink.drain()
            await self.http.aclose()

        self.loop.run_until_complete(shut())
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    # ---- reads -----------------------------------------------------------

    def session_doc(self, answer: dict[str, Any]) -> dict[str, Any]:
        return self.db.docs[f"{vs.PROJECTS}/{answer['project_id']}/{vs.SESSIONS}/{answer['session_id']}"]

    def events(self, answer: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = f"{vs.PROJECTS}/{answer['project_id']}/{vs.SESSIONS}/{answer['session_id']}/{vstore.EVENTS}/"
        return [doc for path, doc in sorted(self.db.docs.items()) if path.startswith(prefix)]

    def cost(self, answer: dict[str, Any]) -> dict[str, Any]:
        return self.ledger.compute(tenant_id=TENANT, person=PERSON, session_id=answer["session_id"])

    def enforce(self, answer: dict[str, Any]) -> dict[str, Any]:
        return self.guard.enforce(tenant_id=TENANT, person=PERSON, project_id=answer["project_id"],
                                  session_id=answer["session_id"])

    def broker_holders(self, path: str) -> list[str]:
        return [str(body.get("holder")) for p, body in self.broker.calls if p.endswith(path)]


def _line(view: dict[str, Any], name: str) -> dict[str, float]:
    (row,) = [r for r in view[vledger.SESSION]["lines"] if r["line"] == name]
    return row


def _delegation_states(world: World, answer: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["delegation_id"], e["state"]) for e in world.events(answer)
            if e.get("type") == vwork.WORK_DELEGATION]


@pytest.fixture
def world():
    built = World()
    try:
        yield built
    finally:
        built.close()


def test_one_speech_session_is_answered_delegated_costed_capped_and_ended(world: World):
    # ---- a person opens a session --------------------------------------------
    # The start's own check (routes/voice.py `start_session`): an empty day is under its cap.
    world.guard.refuse_start(tenant_id=TENANT, person=PERSON)
    answer = world.sessions.start(tenant_id=TENANT, person=PERSON, offer="", project_id=None,
                                  mode=vs.MODE_SPEECH, fallback=None, chosen=CHOSEN)
    session_id = answer["session_id"]

    assert answer["mode"] == vs.MODE_SPEECH and answer["state"] == vs.LIVE
    doc = world.session_doc(answer)
    assert doc["state"] == vs.LIVE and doc["person"] == PERSON and doc["tenant_id"] == TENANT
    # The front holds the session, on one Voice it pool account leased for it.
    held = world.front.get(session_id)
    assert held is not None and held.grant is not None and held.person == PERSON
    assert world.broker_holders("/assign") == [plannerlease.FRONT_HOLDER]
    # The speech machine runs for this session.
    assert world.machine.holder_of(session_id) is not None
    assert world.compute.status == sm.INSTANCE_RUNNING
    # Nothing has cost anything that is not time yet: no planner, no work.
    opened = world.cost(answer)
    assert _line(opened, "planner") == {"line": "planner", "spent_usd": 0.0, "reserved_usd": 0.0}
    assert opened[vledger.SESSION]["total_usd"] < opened[vledger.SESSION]["cap_usd"] * vledger.WARN_SHARE

    # ---- a spoken turn arrives as recognised text; the fast front answers -----
    world.clock.advance(60)
    front_line = world.relay.turn(session_id, FIRST_WORDS, None)

    assert front_line.reply.strip()
    assert front_line.delegation_id is not None and front_line.delegation_id.startswith("vd_")
    # The front heard the person's own words, and nothing past the turn's text.
    (conversation,) = world.front_model.started
    assert any(FIRST_WORDS in said for said in conversation.said)

    # ---- one request is handed to the planner; its line comes back -----------
    planner_line = world.relay.delegation(session_id, front_line.delegation_id,
                                          timeout=sr.DELEGATION_SECONDS)
    world.voice.run(world.sink.drain())

    assert planner_line is not None and PLANNER_LINE in planner_line
    assert planner_line != front_line.reply
    # The planner ran once, on the person's request, on a lease of its own,
    # which went back to the pool when the turn ended.
    assert len(world.planner_model.prompts) == 1 and FIRST_WORDS in world.planner_model.prompts[0]
    assert world.broker_holders("/assign") == [plannerlease.FRONT_HOLDER, plannerlease.HOLDER]
    assert sum(1 for p, _ in world.broker.calls if p.endswith("/release")) == 1
    # swarm-api admitted it through the session's grant, and the session's
    # stream says it started and then finished, in that order.
    assert _delegation_states(world, answer) == [(front_line.delegation_id, vwork.STARTED),
                                                 (front_line.delegation_id, vwork.DONE)]
    assert all(status < 300 for _m, _p, status in world.api.seen)

    # ---- the ledger records what the session costs while it runs --------------
    # The talk and the speech machine are both charged, and both grow with the
    # session's time. The planner turn's cost is not asserted: nothing in
    # either service writes the turn the ledger's planner line reads
    # (`VoiceStore.record_turn` has no caller), so that line stays at $0.
    before = world.cost(answer)
    world.clock.advance(HOUR)
    after = world.cost(answer)
    for line in ("talk", "speech_machine"):
        assert _line(after, line)["spent_usd"] > _line(before, line)["spent_usd"] > 0.0, line
    talk_hour = _line(after, "talk")["spent_usd"] - _line(before, "talk")["spent_usd"]
    assert talk_hour == pytest.approx(modelprices.per_minute_cost(vledger.TALK_MODEL, HOUR), abs=0.01)
    assert after[vledger.SESSION]["total_usd"] == pytest.approx(
        after[vledger.SESSION]["spent_usd"] + after[vledger.SESSION]["reserved_usd"], abs=0.01)

    # ---- the cap warns once at 80 percent ------------------------------------
    cap = after[vledger.SESSION]["cap_usd"]
    warned_at: list[float] = []
    while True:
        total = world.cost(answer)[vledger.SESSION]["total_usd"]
        notices = world.enforce(answer)["notices"]
        session_warnings = [n for n in notices if n == {"notice": vbudget.WARNING, "line": vledger.SESSION}]
        if total < cap * vledger.WARN_SHARE:
            assert session_warnings == [], f"warned at ${total:.2f}, under 80% of ${cap:.0f}"
        else:
            warned_at.append(total)
            assert len(session_warnings) == 1
            break
        world.clock.advance(HOUR)
    assert cap * vledger.WARN_SHARE <= warned_at[0] < cap
    # Said once: asking again, and again an hour later still under the cap, says nothing new.
    assert world.enforce(answer)["notices"] == []
    world.clock.advance(HOUR)
    if world.cost(answer)[vledger.SESSION]["total_usd"] < cap:
        assert world.enforce(answer)["notices"] == []
    state = world.enforce(answer)
    assert vledger.SESSION in state["warned"]
    card = _budget_card(world, answer)
    assert card is not None and card["kind"] == "budget"
    # Under the cap the session still takes turns and stays live.
    assert world.session_doc(answer)["state"] == vs.LIVE

    # ---- at the cap: refused, said once, and the grace starts -----------------
    capped: list[dict[str, str]] = []
    while not capped:
        capped = [n for n in world.enforce(answer)["notices"] if n["notice"] == vbudget.CAPPED]
        if not capped:
            assert world.cost(answer)[vledger.SESSION]["total_usd"] < cap
            world.clock.advance(HOUR)
    assert capped == [{"notice": vbudget.CAPPED, "line": vledger.SESSION}]
    assert world.cost(answer)[vledger.SESSION]["total_usd"] >= cap
    state = world.enforce(answer)
    assert state["notices"] == [] and state["capped"] is not None and state["ends_at"] is not None
    assert state["ended"] is False and world.session_doc(answer)["state"] == vs.LIVE

    # A new request at the cap: the front still answers, the delegation is
    # refused by swarm-api before it runs, and the voice says why.
    second = world.relay.turn(session_id, LATER_WORDS, None)
    assert second.reply.strip() and second.delegation_id is not None
    refused_line = world.relay.delegation(session_id, second.delegation_id, timeout=sr.DELEGATION_SECONDS)
    world.voice.run(world.sink.drain())
    assert refused_line == worksink.CAP_SENTENCES[vledger.SESSION_CAPPED]
    assert len(world.planner_model.prompts) == 1, "the planner ran past the cap"
    assert world.broker_holders("/assign").count(plannerlease.HOLDER) == 1
    # Refused before anything was written: no `started` for it on the stream.
    assert [d for d, _s in _delegation_states(world, answer)].count(second.delegation_id) == 0

    # ---- the grace runs out: the session ends and is marked ended -------------
    world.clock.advance(vledger.CAP_GRACE_SECONDS + 1)
    ended = world.enforce(answer)
    assert ended["ended"] is True

    doc = world.session_doc(answer)
    assert doc["state"] == vs.ENDED and doc["end_cause"] == vs.END_BUDGET_CAP
    assert doc["ended_at"] == world.clock.now() and not doc.get("grant_hash")
    # swarm-voice was told (its billing stops), the front let its account go,
    # and the speech machine stopped with the session.
    assert world.voice.ends == [(session_id, vs.END_BUDGET_CAP)]
    assert world.front.get(session_id) is None
    assert world.broker_holders("/assign").count(plannerlease.FRONT_HOLDER) == 1
    assert sum(1 for p, _ in world.broker.calls if p.endswith("/release")) == 2
    assert world.machine.holder_of(session_id) is None
    # The person's marker is free: their next session may start.
    assert not [p for p in world.db.docs if p.startswith(vs.LIVE_MARKERS + "/")]
    # An ended session's grant opens nothing in swarm-api any more.
    with pytest.raises(vgrant.GrantRefused):
        vgrant.resolve(world.db, session_header=f"{answer['project_id']}/{session_id}",
                       grant=_grant_of(world, session_id))
    # An ended session costs nothing more, and is not enforced or ended twice.
    final = world.cost(answer)[vledger.SESSION]
    world.clock.advance(HOUR)
    assert world.cost(answer)[vledger.SESSION]["spent_usd"] == pytest.approx(final["spent_usd"], abs=0.01)
    again = world.enforce(answer)
    assert again["ended"] is False and again["notices"] == []
    assert world.voice.ends == [(session_id, vs.END_BUDGET_CAP)]


def _budget_card(world: World, answer: dict[str, Any]) -> dict[str, Any] | None:
    prefix = f"{vs.PROJECTS}/{answer['project_id']}/{vstore.CARDS}/"
    found = [doc for path, doc in world.db.docs.items()
             if path.startswith(prefix) and doc.get("kind") == "budget"]
    return found[-1] if found else None


def _grant_of(world: World, session_id: str) -> str:
    """The grant swarm-voice was started with, as the bridge handed it over."""
    (grant,) = [g for g in world.voice.grants if g.session_id == session_id]
    return grant.headers()[vgrant.GRANT_HEADER]
