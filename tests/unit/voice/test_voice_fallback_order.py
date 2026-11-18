"""Voice it's half of the fallback order (docs/design/llm-providers.md §5.3).

The broker walks each Voice it lease down its chain and answers the `tier`
it granted (`plan`, `api_key`, `voice_dedicated`). Against a fake broker that
walks a chain the way quota_broker's `voice_chain` and `walk_chain` do, a fake
ChatGPT plan backend, a fake OpenAI developer API and a fake Live API, this
holds that:

* Live, text and planner leases take the tier the broker answers, and each
  moves to the next tier when its account is spent (a Live or text lease
  while it still holds the spent one, so the broker's walk passes over it; a
  planner lease with the spent account excluded);
* text on a key talks to the developer Responses wire (`/v1/responses` with
  the key as `Authorization` alone) and on a plan today's Codex wire; a plan
  at its usage limit, or a key whose credit is gone, moves the turn to the
  next tier and the turn is answered;
* a planner turn on a key hands Claude Code ANTHROPIC_API_KEY and no plan
  token; on a plan the plan token and no ANTHROPIC_API_KEY;
* a Live session whose key's credit is gone mid-session leases the next tier,
  tells swarm-api to reopen, and the next start of the same voice session
  reopens Live on that tier, the session records its tier and the trace names
  it;
* no key value appears in a log line, a trace body, an event or a response.

Every token here is built at runtime with `secrets.token_hex`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sys
from pathlib import Path

import httpx
import pytest

_APP = Path(__file__).resolve().parents[3] / "apps" / "swarm-voice"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from swarm_voice import accountlease as al  # noqa: E402
from swarm_voice import claudeturn as ct  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice import plannerlease as pl  # noqa: E402
from swarm_voice import textchat as tc  # noqa: E402
from swarm_voice import trace as vtrace  # noqa: E402

PERSON = "owner@example.com"
BROKER = "https://broker.test"
VOICE_ID = "vs_" + "c0ffee0123"


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _reply(text: str) -> bytes:
    return _sse({"type": "response.output_item.done", "output_index": 0,
                 "item": {"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": text}]}},
                {"type": "response.completed", "response": {"output": [], "usage": {"output_tokens": 3}}})


class Account:
    def __init__(self, tier: str, *, plan: bool = False) -> None:
        self.id = ("plan-" if plan else "key-") + secrets.token_hex(4)
        self.tier = tier
        self.plan = plan
        self.holds: list[str] = []
        self.limited = False

    def credential(self, minted: set[str]) -> dict:
        value = secrets.token_hex(16)
        minted.add(value)
        if self.plan:
            return {"access_token": value, "account_id": "chatgpt-" + self.id}
        return {"access_token": value, "kind": "api_key"}


class ChainBroker:
    """The broker's tiered walk, in memory: each tier's first account (fewest
    live holds) that is not limited, tier after tier; `exclude` passed over."""

    def __init__(self, chains: dict[str, list[list[Account]]]) -> None:
        self.chains = chains
        self.minted: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        self.accounts = {a.id: a for tiers in chains.values() for tier in tiers for a in tier}
        #: A broker from before the voice lease's `exclude`: it refuses the field.
        self.refuses_exclude = False

    def _walk(self, tiers: list[list[Account]], exclude: set[str]) -> Account | None:
        for tier in tiers:
            free = [a for a in tier if not a.limited and a.id not in exclude]
            if free:
                return sorted(free, key=lambda a: len(a.holds))[0]
        return None

    def _grant(self, chosen: Account | None) -> httpx.Response:
        if chosen is None:
            return httpx.Response(200, json={"account_id": None, "assignment_id": None, "credential": None,
                                             "tier": None, "reason": "no_voice_account_available",
                                             "next_reset_at": None})
        assignment = "asg_" + secrets.token_hex(6)
        chosen.holds.append(assignment)
        return httpx.Response(200, json={"account_id": chosen.id, "assignment_id": assignment,
                                         "tier": chosen.tier, "label": chosen.tier,
                                         "credential": chosen.credential(self.minted), "reason": ""})

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        self.calls.append((path, body))
        if path == "/v1/voice/lease":
            if self.refuses_exclude and "exclude" in body:
                return httpx.Response(422, json={"detail": "extra fields not permitted"})
            chain = self.chains[body.get("capability", "live")]
            if body.get("capability") == "text" and not body.get("key_tier"):
                chain = chain[:1]
            return self._grant(self._walk(chain, set(body.get("exclude") or ())))
        if path == "/v1/accounts/assign":
            chain = self.chains["planner"]
            if not body.get("key_tier"):
                chain = chain[:1]
            return self._grant(self._walk(chain, set(body.get("exclude") or ())))
        match = re.fullmatch(r"/v1/voice/lease/([^/]+)/(renew|release|readings)", path)
        account = self.accounts.get(match.group(1)) if match else None
        if account is None or (match.group(2) != "readings" and body.get("assignment_id") not in account.holds):
            return httpx.Response(409, json={"code": "not_held"})
        if match.group(2) == "renew":
            return httpx.Response(200, json={"credential": account.credential(self.minted)})
        if match.group(2) == "release":
            account.holds.remove(body["assignment_id"])
            return httpx.Response(200, json={"released": True})
        return httpx.Response(200, json={"recorded": True})

    def leases(self, path: str) -> list[dict]:
        return [b for p, b in self.calls if p == path]


class Upstreams:
    """The plan's Codex backend and the developer API, on one transport."""

    def __init__(self, broker: ChainBroker) -> None:
        self.broker = broker
        self.codex: list[tuple[httpx.Headers, dict]] = []
        self.developer: list[tuple[httpx.Headers, dict]] = []
        #: What the next answer of each wire is, popped first in, else a reply.
        self.codex_next: list[tuple[int, bytes]] = []
        self.developer_next: list[tuple[int, bytes]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "broker.test":
            return self.broker.handle(request)
        if request.url.host == "chatgpt.com":
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"models": [{"slug": "gpt-5.5"}]})
            self.codex.append((request.headers, json.loads(request.content)))
            status, body = self.codex_next.pop(0) if self.codex_next else (200, _reply("On the plan."))
            return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})
        if request.url.host == "api.openai.com":
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "gpt-5.4-mini"}, {"id": "gpt-5.5"}]})
            if request.url.path == "/v1/responses":
                self.developer.append((request.headers, json.loads(request.content)))
                status, body = self.developer_next.pop(0) if self.developer_next else (200, _reply("On a key."))
                return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


async def _id_token(audience: str) -> str:
    return "idt_" + secrets.token_hex(8)


def _broker(chains: dict[str, list[list[Account]]]) -> tuple[ChainBroker, Upstreams, httpx.AsyncClient,
                                                              al.VoiceBroker]:
    broker = ChainBroker(chains)
    upstreams = Upstreams(broker)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstreams.handle))
    return broker, upstreams, http, al.VoiceBroker(http, BROKER, token_fetcher=_id_token)


def _no_key_in(text: str, minted: set[str]) -> None:
    for value in minted:
        assert value not in text


# ------------------------------------------------------------ the Voice it lease


def test_each_voice_lease_takes_the_tier_the_broker_answers_and_text_asks_for_the_key_tier():
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    plan = Account(al.TIER_PLAN, plan=True)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[plan], [shared], [dedicated]]})

    async def scenario():
        held = await voice.lease(person=PERSON, voice_session_id=VOICE_ID)
        assert held.tier == al.TIER_KEY and held.account_id == shared.id
        text = await voice.lease(person=PERSON, voice_session_id=VOICE_ID, capability=al.CAPABILITY_TEXT)
        assert text.tier == al.TIER_PLAN
        # A text lease says it can run on a key, so the broker walks on past the plans.
        assert broker.leases("/v1/voice/lease")[-1]["key_tier"] is True
        # A Live lease needs no flag: a key is the only kind it runs on.
        assert "key_tier" not in broker.leases("/v1/voice/lease")[0]
        await held.release()
        await text.release()
        await http.aclose()

    asyncio.run(scenario())


def test_a_spent_voice_lease_moves_to_the_next_tier_and_gives_the_spent_one_back_after():
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})

    async def scenario():
        held = await voice.lease(person=PERSON, voice_session_id=VOICE_ID)
        shared.limited = True  # what the broker reads once the key's credit is gone
        moved = await voice.move(held, person=PERSON)
        assert moved.tier == al.TIER_VOICE_KEY and moved.account_id == dedicated.id
        # The spent hold is the caller's to give back: never two moves, never none.
        assert shared.holds and dedicated.holds
        await held.release()
        await moved.release()
        assert not shared.holds and not dedicated.holds
        await http.aclose()

    asyncio.run(scenario())


def test_a_move_names_the_spent_accounts_in_exclude_so_the_walk_passes_over_them():
    """The spent key need not read as limited yet: `exclude` passes over it."""
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})

    async def scenario():
        held = await voice.lease(person=PERSON, voice_session_id=VOICE_ID)
        assert "exclude" not in broker.leases("/v1/voice/lease")[0]
        moved = await voice.move(held, person=PERSON)
        assert broker.leases("/v1/voice/lease")[-1]["exclude"] == [shared.id]
        assert moved.account_id == dedicated.id and moved.excluded == (shared.id,)
        await held.release()
        # No tier 3 for Live: the next move passes over both and is refused.
        with pytest.raises(al.VoicePoolRefused):
            await voice.move(moved, person=PERSON)
        assert broker.leases("/v1/voice/lease")[-1]["exclude"] == [shared.id, dedicated.id]
        await moved.release()
        assert not shared.holds and not dedicated.holds
        await http.aclose()

    asyncio.run(scenario())


def test_a_move_the_broker_answers_with_the_same_account_is_refused_and_holds_nothing_more():
    """A broker from before `exclude` (422) is asked again without it."""
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})
    broker.refuses_exclude = True

    async def scenario():
        held = await voice.lease(person=PERSON, voice_session_id=VOICE_ID)
        with pytest.raises(al.VoicePoolRefused) as refused:
            await voice.move(held, person=PERSON)
        assert refused.value.reason == al.NONE_AVAILABLE
        assert shared.holds == [held.assignment_id]
        await held.release()
        await http.aclose()

    asyncio.run(scenario())


def test_a_hold_the_broker_refuses_to_renew_tells_the_session():
    shared = Account(al.TIER_KEY)
    broker, _, http, voice = _broker({"live": [[shared]], "text": [[]]})

    async def scenario():
        held = await voice.lease(person=PERSON, voice_session_id=VOICE_ID)
        told: list[str] = []
        held.on_lost = lambda: told.append(held.account_id)
        shared.holds.clear()
        held._broker.renew_seconds = 0.0  # noqa: SLF001
        held.start_renewing()
        for _ in range(100):
            if told:
                break
            await asyncio.sleep(0.01)
        assert told == [shared.id] and held.lost
        await held.release()
        await http.aclose()

    asyncio.run(scenario())


# ------------------------------------------------------------ the planner


def test_the_planner_lease_asks_for_the_key_tier_takes_the_tier_and_moves_with_the_spent_one_excluded():
    plan, key = Account(al.TIER_PLAN, plan=True), Account(al.TIER_KEY)
    broker, _, http, voice = _broker({"planner": [[plan], [key]], "live": [[]], "text": [[]]})
    accounts = pl.PlannerAccounts(voice)

    async def scenario():
        held = await accounts.assign(person=PERSON, voice_session_id=VOICE_ID)
        assert held.tier == al.TIER_PLAN
        assert broker.leases("/v1/accounts/assign")[0]["key_tier"] is True
        assert not isinstance(held.bearer.reveal(), ct.ApiKey)
        moved = await accounts.move(held, person=PERSON)
        assert moved.tier == al.TIER_KEY and moved.account_id == key.id
        assert broker.leases("/v1/accounts/assign")[-1]["exclude"] == [plan.id]
        assert held.released and not plan.holds
        assert isinstance(moved.bearer.reveal(), ct.ApiKey)
        # No tier 3 for the planner (Q2): the next move is a refusal.
        with pytest.raises(pl.PoolFull):
            await accounts.move(moved, person=PERSON)
        await http.aclose()

    asyncio.run(scenario())


def test_a_planner_turn_on_a_key_sets_anthropic_api_key_and_on_a_plan_does_not(tmp_path):
    claude = ct.ClaudeCode(binary="claude", model=ct.MODEL, tmp_root=str(tmp_path))
    value = secrets.token_hex(16)
    on_key = claude.env(home=str(tmp_path), token=ct.ApiKey(value))
    assert on_key[ct.KEY_ENV] == value and ct.TOKEN_ENV not in on_key
    on_plan = claude.env(home=str(tmp_path), token=value)
    assert on_plan[ct.TOKEN_ENV] == value and ct.KEY_ENV not in on_plan
    # The MCP server the child starts gets neither.
    server_env = claude.mcp_config(socket_path="s", nonce="n")["mcpServers"][ct.SERVER]["env"]
    assert server_env.get(ct.TOKEN_ENV) == "" and server_env.get(ct.KEY_ENV) == ""
    assert value not in repr(ct.ApiKey(value)) and value not in str([ct.ApiKey(value)])


@pytest.mark.parametrize(("result", "spent"), [
    ("Credit balance is too low", ct.SPENT_CREDIT),
    ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":'
     '"Your credit balance is too low to access the Anthropic API."}}', ct.SPENT_CREDIT),
    ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', ct.SPENT_RATE),
    ("Something else went wrong", None),
])
def test_a_child_that_ends_on_a_spent_account_says_which(result, spent):
    outcome = ct.ChildOutcome()
    ct.note_result(outcome, {"type": "result", "is_error": True, "result": result})
    assert outcome.spent == spent
    fine = ct.ChildOutcome()
    ct.note_result(fine, {"type": "result", "is_error": False, "result": result})
    assert fine.spent is None


# ------------------------------------------------------------ text


def _texts(chains):
    broker, upstreams, http, voice = _broker(chains)
    texts = tc.TextSessions(tc.CodexApi(http), leases=voice)
    return broker, upstreams, http, texts


def test_text_on_a_plan_uses_todays_wire_and_on_a_key_the_developer_responses_wire(caplog):
    caplog.set_level(logging.DEBUG)
    key = Account(al.TIER_KEY)
    broker, upstreams, http, texts = _texts({"text": [[], [key], []], "live": [[]]})

    async def scenario():
        session = await texts.start(VOICE_ID, person=PERSON)
        assert session.tier == al.TIER_KEY
        reply = await texts.turn(VOICE_ID, "hello there")
        assert reply.reply == "On a key."
        assert not upstreams.codex
        headers, body = upstreams.developer[0]
        assert headers["authorization"].removeprefix("Bearer ") in broker.minted
        assert "chatgpt-account-id" not in headers and "originator" not in headers
        assert body["model"] == "gpt-5.5" and body["stream"] is True and body["store"] is False
        assert body["input"][-1]["content"][0]["text"] == "hello there"
        await texts.end_all()
        await http.aclose()

    asyncio.run(scenario())
    _no_key_in(caplog.text, broker.minted)

    plan = Account(al.TIER_PLAN, plan=True)
    broker, upstreams, http, texts = _texts({"text": [[plan], [], []], "live": [[]]})

    async def on_plan():
        session = await texts.start(VOICE_ID, person=PERSON)
        assert session.tier == al.TIER_PLAN
        assert (await texts.turn(VOICE_ID, "hello")).reply == "On the plan."
        assert upstreams.codex and not upstreams.developer
        assert upstreams.codex[0][0]["chatgpt-account-id"] == "chatgpt-" + plan.id
        await texts.end_all()
        await http.aclose()

    asyncio.run(on_plan())


def test_a_plan_at_its_usage_limit_moves_the_turn_to_the_key_tier_and_answers_it(caplog):
    caplog.set_level(logging.DEBUG)
    plan, key = Account(al.TIER_PLAN, plan=True), Account(al.TIER_KEY)
    broker, upstreams, http, texts = _texts({"text": [[plan], [key], []], "live": [[]]})

    async def scenario():
        await texts.start(VOICE_ID, person=PERSON)
        plan.limited = True  # the reading the refusal reports
        upstreams.codex_next.append((429, json.dumps({"error": {"type": "usage_limit_reached",
                                                                "resets_at": 4_102_444_800}}).encode()))
        reply = await texts.turn(VOICE_ID, "plan this")
        assert reply.reply == "On a key."
        session = texts.get(VOICE_ID)
        assert session.tier == al.TIER_KEY
        assert session.turns[-1] == ("voice", "On a key.")
        # The plan's hold went back once the key held the session.
        assert not plan.holds and key.holds
        await texts.end_all()
        await http.aclose()

    asyncio.run(scenario())
    _no_key_in(caplog.text, broker.minted)


def test_a_key_whose_credit_is_gone_moves_to_the_dedicated_key_and_with_no_tier_left_the_refusal_stands():
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, upstreams, http, texts = _texts({"text": [[], [shared], [dedicated]], "live": [[]]})
    gone = json.dumps({"error": {"code": "insufficient_quota", "type": "insufficient_quota"}}).encode()

    async def scenario():
        await texts.start(VOICE_ID, person=PERSON)
        shared.limited = True
        upstreams.developer_next.append((429, gone))
        assert (await texts.turn(VOICE_ID, "one")).reply == "On a key."
        assert texts.get(VOICE_ID).tier == al.TIER_VOICE_KEY
        dedicated.limited = True
        upstreams.developer_next.append((429, gone))
        with pytest.raises(tc.CodexRefused) as refused:
            await texts.turn(VOICE_ID, "two")
        assert refused.value.code == "insufficient_quota" and refused.value.status == 429
        assert texts.get(VOICE_ID).tier == al.TIER_VOICE_KEY
        await texts.end_all()
        await http.aclose()

    asyncio.run(scenario())


# ------------------------------------------------------------ Live


class FakeSocket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

        class _State:
            name = "OPEN"

        self.state = _State()

    def push(self, event: dict) -> None:
        self.queue.put_nowait(json.dumps(event))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, raw: str) -> None:
        event = json.loads(raw)
        self.sent.append(event)
        if event.get("type") == "session.close":
            self.push({"type": "session.closed", "reason": "client_close", "usage": {"seconds": 3}})

    async def close(self) -> None:
        self.closed = True
        self.state.name = "CLOSED"
        self.queue.put_nowait(None)


class FakeLiveApi:
    """The Live create and attach, recording which key each used."""

    def __init__(self) -> None:
        self.creates: list[tuple[str, str]] = []
        self.sockets: dict[str, FakeSocket] = {}

    async def create(self, credential, offer: str, session: dict) -> tuple[str, str]:
        bearer = await credential.bearer()
        live_id = "live_" + secrets.token_hex(4)
        self.creates.append((live_id, bearer.reveal()))
        return live_id, "answer-" + live_id

    async def attach(self, credential, live_id: str, held=None, *, seconds=None):
        await credential.bearer()
        socket = FakeSocket()
        self.sockets[live_id] = socket
        if held is not None:
            held.append({"type": "session.updated"})
        return socket


def _offer() -> str:
    return "v=0\r\no=- " + secrets.token_hex(4) + " 2 IN IP4 127.0.0.1\r\n"


async def _until(check, tries: int = 200) -> None:
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


class FakePlannerForTrace:
    def __init__(self) -> None:
        self.traces: list[dict] = []

    async def write_trace(self, grant, body) -> bool:
        self.traces.append(body)
        return True

    def forget(self, voice_id: str) -> None:
        return None


def test_live_reopens_on_the_next_tier_when_its_keys_credit_is_gone_and_the_session_records_it(caplog):
    caplog.set_level(logging.DEBUG)
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})
    api = FakeLiveApi()
    events: list[tuple[str, dict]] = []

    async def sink(voice_id: str, event: dict) -> None:
        events.append((voice_id, event))

    sessions = live.Sessions(api, leases=voice, on_event=sink, close_grace=1.0)
    planner = FakePlannerForTrace()

    async def scenario():
        first = await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        assert first.tier == al.TIER_KEY
        first.trace = vtrace.SessionTrace(vtrace.ENGINE_LIVE)
        first._planner = planner  # noqa: SLF001
        first._traced([("person", "hi")], "plan it", None, "Done.")  # noqa: SLF001
        await _until(lambda: planner.traces)
        assert planner.traces[0]["tier"] == al.TIER_KEY and planner.traces[0]["engine"] == vtrace.ENGINE_LIVE
        # A second start while nothing asked for a reopen is still refused.
        with pytest.raises(live.SessionExists):
            await sessions.start(VOICE_ID, person=PERSON, offer=_offer())

        shared.limited = True
        api.sockets[first.live_id].push({"type": "error", "error": {"code": "insufficient_quota",
                                                                    "type": "insufficient_quota"}})
        await _until(lambda: any(e.get("type") == live.REOPEN_EVENT for _, e in events))
        told = [e for _, e in events if e.get("type") == live.REOPEN_EVENT]
        assert told == [{"type": live.REOPEN_EVENT, "tier": al.TIER_VOICE_KEY}]
        assert first.reopen_pending

        second = await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        assert second is not first and second.tier == al.TIER_VOICE_KEY
        assert sessions.get(VOICE_ID) is second and second.live_id != first.live_id
        # The new Live session runs on the dedicated key; the spent one is closed and given back.
        assert api.creates[-1][1] != api.creates[0][1]
        assert first.ended and any(e.get("type") == "session.close" for e in api.sockets[first.live_id].sent)
        await sessions.drain_releases()
        assert not shared.holds and dedicated.holds
        # What the person said carries over, and so do the trace and its turn count.
        assert second.transcript == first.transcript
        assert second.trace is first.trace
        second._planner = planner  # noqa: SLF001
        second._traced([("person", "again")], "and this", None, "Done.")  # noqa: SLF001
        await _until(lambda: len(planner.traces) == 2)
        assert planner.traces[1]["tier"] == al.TIER_VOICE_KEY and planner.traces[1]["turn"]["n"] == 2

        await sessions.end(VOICE_ID)
        await sessions.drain_releases()
        assert not dedicated.holds
        await http.aclose()

    asyncio.run(scenario())
    text = caplog.text + json.dumps([e for _, e in events]) + json.dumps(planner.traces)
    _no_key_in(text, broker.minted)


def test_a_live_session_whose_spent_key_closes_before_the_reopen_keeps_the_next_tier_for_it():
    shared, dedicated = Account(al.TIER_KEY), Account(al.TIER_VOICE_KEY)
    broker, _, http, voice = _broker({"live": [[shared], [dedicated]], "text": [[]]})
    api = FakeLiveApi()
    sessions = live.Sessions(api, leases=voice, close_grace=1.0)

    async def scenario():
        first = await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        shared.limited = True
        socket = api.sockets[first.live_id]
        socket.push({"type": "error", "error": {"code": "insufficient_quota"}})
        await _until(lambda: first.reopen_pending)
        # OpenAI ends the spent session on its own before the browser offers again.
        socket.push({"type": "session.closed", "reason": "insufficient_quota", "usage": {"seconds": 9}})
        await _until(lambda: first.ended)
        await sessions.drain_releases()
        assert not shared.holds and dedicated.holds
        second = await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        assert second.tier == al.TIER_VOICE_KEY and sessions.get(VOICE_ID) is second
        await sessions.end_all()
        assert not dedicated.holds
        await http.aclose()

    asyncio.run(scenario())


def test_a_live_session_with_no_next_tier_is_not_told_to_reopen_and_ends_as_today():
    shared = Account(al.TIER_KEY)
    broker, _, http, voice = _broker({"live": [[shared], []], "text": [[]]})
    api = FakeLiveApi()
    events: list[dict] = []

    async def sink(voice_id: str, event: dict) -> None:
        events.append(event)

    sessions = live.Sessions(api, leases=voice, on_event=sink, close_grace=1.0)

    async def scenario():
        first = await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        shared.limited = True
        api.sockets[first.live_id].push({"type": "error", "error": {"code": "insufficient_quota"}})
        await _until(lambda: any(e.get("type") == "error" for e in events))
        await asyncio.sleep(0.05)
        assert not first.reopen_pending
        assert not any(e.get("type") == live.REOPEN_EVENT for e in events)
        with pytest.raises(live.SessionExists):
            await sessions.start(VOICE_ID, person=PERSON, offer=_offer())
        await sessions.end_all()
        assert not shared.holds
        await http.aclose()

    asyncio.run(scenario())


def test_an_unknown_tier_in_the_brokers_answer_is_read_as_none():
    assert al.tier_of({"tier": "plan"}) == al.TIER_PLAN
    assert al.tier_of({"tier": "something"}) is None
    assert al.tier_of({}) is None and al.tier_of(None) is None
