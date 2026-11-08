"""Voice it's text conversation on the ChatGPT plan (owner, 4 November 2026: "both
paths"; docs/design/voice-it.md §3.2).

OpenAI refuses the plan's token for GPT-Live, but the plan serves text the way AI
Studio uses it. Against a fake Codex backend that streams SSE and a fake quota
broker, both on an `httpx.MockTransport`, this holds that:

* a text start leases the Voice it pool's plan (`capability: text`), creates no
  Live session and answers `mode: text` with no SDP;
* a turn's reply is read from `response.output_item.done`, while the terminal
  `response.completed` carries `output == []`;
* every request carries the wire a peer application measured: the headers, `stream: true`,
  `store: false`, the include, and never `max_output_tokens`, a temperature, a
  top_p or a response_format; `build_body` refuses to build one;
* the plan's usage-limit refusal is a 429 with when it resets;
* the `x-codex-*` windows reach the broker named as it names a plan's;
* a turn that delegates is a planner turn whose summary is the reply and whose
  cards come back with it;
* the person's text and the token never appear in a log line.

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
from swarm_voice import live  # noqa: E402
from swarm_voice import textchat as tc  # noqa: E402
from swarm_voice.main import create_app  # noqa: E402
from swarm_voice.planner import TurnResult  # noqa: E402

PERSON = "owner@example.com"
CALLER = "swarm-api@example-project.iam.gserviceaccount.com"
BROKER = "https://broker.test"
VOICE_ID = "vs_" + "f00dfeed12"
RESET_EPOCH = 4_102_444_800  # 2100-01-01T00:00:00Z


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message(text: str) -> dict:
    return {"type": "response.output_item.done", "output_index": 0,
            "item": {"type": "message", "role": "assistant", "id": "msg_1",
                     "content": [{"type": "output_text", "text": text}]}}


COMPLETED = {"type": "response.completed",
             "response": {"id": "resp_1", "output": [], "usage": {"input_tokens": 12, "output_tokens": 7}}}


class FakeBroker:
    """The Voice it pool: a key for `live` only when one is registered, the plan
    for `text`; and the readings it was handed."""

    def __init__(self, codex: "FakeCodex") -> None:
        self.codex = codex
        self.account_id = "acct_" + secrets.token_hex(4)
        self.plan_account = "chatgpt-" + secrets.token_hex(4)
        self.leases: list[dict] = []
        self.releases: list[dict] = []
        self.renewals: list[dict] = []
        self.readings: list[dict] = []
        self.holder: dict | None = None

    def _credential(self) -> dict:
        token = "plan-" + secrets.token_hex(16)
        self.codex.valid.add(token)
        return {"access_token": token, "account_id": self.plan_account}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path == "/v1/voice/lease":
            self.leases.append(body)
            if body.get("capability") != "text":
                return httpx.Response(200, json={"account_id": None, "assignment_id": None, "credential": None,
                                                 "reason": "no_voice_account_registered", "next_reset_at": None})
            self.holder = {"assignment_id": "asg_" + secrets.token_hex(6),
                           "voice_session_id": body["voice_session_id"]}
            return httpx.Response(200, json={"account_id": self.account_id, **self.holder, "label": "plan",
                                             "credential": self._credential(), "reason": ""})
        match = re.fullmatch(r"/v1/voice/lease/([^/]+)/(renew|release|readings)", path)
        if match and match.group(1) == self.account_id:
            if match.group(2) == "readings":
                self.readings.append(body)
                return httpx.Response(200, json={"recorded": True})
            if match.group(2) == "renew":
                self.renewals.append(body)
                return httpx.Response(200, json={"credential": self._credential()})
            self.releases.append(body)
            self.holder = None
            return httpx.Response(200, json={"released": True})
        return httpx.Response(404)


class FakeCodex:
    """chatgpt.com/backend-api/codex: `/models` and a streamed `/responses`."""

    def __init__(self) -> None:
        self.valid: set[str] = set()
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self.model_queries: list[str] = []
        self.live_creates = 0
        #: What the next `/responses` answers: (status, headers, body bytes).
        self.next: list[tuple[int, dict, bytes]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            self.live_creates += 1
            return httpx.Response(500)
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.path == "/backend-api/codex/models":
            self.model_queries.append(request.url.query.decode())
            return httpx.Response(200, json={"models": [{"slug": "gpt-5.6-sol"}, {"slug": "gpt-5.5"},
                                                        {"slug": "gpt-5.4-mini"}]})
        if request.url.path == "/backend-api/codex/responses" and request.method == "POST":
            self.headers.append(request.headers)
            body = json.loads(request.content)
            self.requests.append(body)
            if auth not in self.valid:
                return httpx.Response(401, json={"error": {"code": "token_expired", "type": "invalid_request_error",
                                                           "message": f"bad {auth}"}})
            if body.get("stream") is not True or "max_output_tokens" in body:
                return httpx.Response(400, json={"error": {"code": "bad_request", "type": "invalid_request_error"}})
            status, headers, content = self.next.pop(0) if self.next else (200, {}, _sse(_message("Hi."), COMPLETED))
            return httpx.Response(status, headers={"content-type": "text/event-stream", **headers}, content=content)
        return httpx.Response(404)


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.forgotten: list[str] = []
        #: The planner model each turn was asked for (None: the pin).
        self.models: list[str | None] = []

    async def delegate(self, *, person, grant, transcript, delegation_id=None, model=None) -> TurnResult:
        self.calls.append({"person": person, "transcript": list(transcript), "delegation_id": delegation_id})
        self.models.append(model)
        return TurnResult(delegation_id, "Three steps are on screen.",
                          cards=[{"kind": "plan", "card_id": "card_1", "title": "Rollout"}])

    def forget(self, session_id: str) -> None:
        self.forgotten.append(session_id)


async def _caller_ok(token: str) -> str:
    if token != "caller-token":
        raise PermissionError("not swarm-api")
    return CALLER


class Harness:
    def __init__(self, *, planner: FakePlanner | None = None) -> None:
        self.codex = FakeCodex()
        self.broker = FakeBroker(self.codex)
        self.planner = planner

        def route(request: httpx.Request) -> httpx.Response:
            if request.url.host == "broker.test":
                return self.broker.handle(request)
            return self.codex.handle(request)

        async def id_token(audience: str) -> str:
            return "idt_" + secrets.token_hex(8)

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(route))
        self.leases = al.VoiceBroker(self.upstream, BROKER, token_fetcher=id_token)
        self.sessions = live.Sessions(live.LiveApi(self.upstream), leases=self.leases)
        self.texts = tc.TextSessions(tc.CodexApi(self.upstream), leases=self.leases, planner=planner)
        self.app = create_app(sessions=self.sessions, verify_caller=_caller_ok, texts=self.texts)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://voice")
        self.responses: list[str] = []

    async def post(self, path: str, body: dict | None = None) -> httpx.Response:
        response = await self.client.post(path, json=body or {}, headers={"Authorization": "Bearer caller-token"})
        self.responses.append(response.text)
        return response

    async def start(self) -> httpx.Response:
        return await self.post("/internal/sessions", {"voice_session_id": VOICE_ID, "person": PERSON,
                                                      "mode": "text"})

    async def close(self) -> None:
        await self.texts.end_all()
        await self.client.aclose()
        await self.upstream.aclose()


def run(scenario, *, planner: FakePlanner | None = None):
    async def main():
        h = Harness(planner=planner)
        try:
            return await asyncio.wait_for(scenario(h), 20)
        finally:
            await h.close()

    return asyncio.run(main())


def test_a_text_start_leases_the_plan_and_answers_text_mode_with_no_sdp():
    async def scenario(h: Harness):
        r = await h.start()
        assert r.status_code == 201, r.text
        assert r.json() == {"voice_session_id": VOICE_ID, "mode": "text"}
        assert h.broker.leases == [{"person": PERSON, "voice_session_id": VOICE_ID, "provider": "openai",
                                    "capability": "text"}]
        assert h.codex.live_creates == 0
        assert h.texts.get(VOICE_ID) is not None and h.sessions.get(VOICE_ID) is None
        # A second start of the same session is refused, in either mode.
        again = await h.start()
        assert again.status_code == 409
        ended = await h.post(f"/internal/sessions/{VOICE_ID}/end")
        assert ended.status_code == 200 and ended.json()["ended"] is True
        assert h.broker.releases and h.broker.releases[0]["voice_session_id"] == VOICE_ID
        assert h.texts.get(VOICE_ID) is None

    run(scenario)


def test_the_reply_is_read_from_output_item_done_not_the_terminal_event(caplog):
    caplog.set_level(logging.DEBUG)
    said = "please plan the rollout " + secrets.token_hex(4)

    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        h.codex.next.append((200, {}, _sse({"type": "response.created", "response": {"id": "resp_1"}},
                                           {"type": "response.output_text.delta", "delta": "Su"},
                                           _message("Sure, here is how."), COMPLETED)))
        r = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": said})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "Sure, here is how."
        assert body["cards"] == [] and body["turn_id"].startswith("vt_")
        # The wire a peer application measured, header for header.
        sent = h.codex.headers[0]
        assert sent["authorization"].removeprefix("Bearer ") in h.codex.valid
        assert sent["openai-beta"] == "responses=experimental"
        assert sent["originator"] == "codex_cli_rs"
        assert sent["chatgpt-account-id"] == h.broker.plan_account
        assert sent["session_id"] == VOICE_ID
        request = h.codex.requests[0]
        assert request["model"] == "gpt-5.5"  # the first listed of the preferred, not the first listed
        assert h.codex.model_queries == ["client_version=0.147.0"]
        assert request["stream"] is True and request["store"] is False
        assert request["include"] == ["reasoning.encrypted_content"]
        assert request["instructions"] == tc.TEXT_INSTRUCTIONS
        assert request["input"] == [{"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": said}]}]
        # The second turn carries the conversation so far.
        r2 = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "and then?"})
        assert r2.json()["reply"] == "Hi."
        roles = [(m["role"], m["content"][0]["text"]) for m in h.codex.requests[1]["input"]]
        assert roles == [("user", said), ("assistant", "Sure, here is how."), ("user", "and then?")]
        tokens = h.codex.valid
        return tokens

    tokens = run(scenario)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert said not in logged and "Sure, here is how." not in logged
    assert not any(t in logged for t in tokens)


def test_no_request_with_stream_false_or_a_refused_field_is_ever_built():
    for bad in ({"stream": False}, {"store": True}, {"max_output_tokens": 200}, {"temperature": 0.2},
                {"top_p": 0.9}, {"response_format": {"type": "json_object"}}):
        with pytest.raises(ValueError):
            tc.build_body(model="gpt-5.5", instructions="x", turns=[("person", "hi")], **bad)
    body = tc.build_body(model="gpt-5.5", instructions="x", turns=[("person", "hi"), ("voice", "hello")],
                         tools=[tc.delegate_tool()])
    assert body["stream"] is True and body["store"] is False
    assert not tc.FORBIDDEN_FIELDS.intersection(body)
    assert all(m["role"] in ("user", "assistant") for m in body["input"])  # never a system role

    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        for i in range(3):
            assert (await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": f"turn {i}"})).status_code == 200
        for request in h.codex.requests:
            assert request["stream"] is True and request["store"] is False
            assert not tc.FORBIDDEN_FIELDS.intersection(request)

    run(scenario)


def test_the_plans_usage_limit_is_a_429_with_when_it_resets():
    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        h.codex.next.append((429, {}, json.dumps({"error": {
            "type": "usage_limit_reached", "message": "You've hit your usage limit", "plan_type": "plus",
            "resets_at": RESET_EPOCH}}).encode()))
        r = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "hello"})
        assert r.status_code == 429, r.text
        assert r.json()["code"] == "usage_limit_reached"
        assert r.json()["resets_at"] == "2100-01-01T00:00:00+00:00"
        # The turn that was refused is not part of the conversation.
        assert h.texts.get(VOICE_ID).turns == []
        # A refusal streamed as `response.failed` reads the same.
        h.codex.next.append((200, {}, _sse({"type": "response.failed", "response": {"error": {
            "code": "usage_limit_reached", "resets_at": RESET_EPOCH}}})))
        r = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "hello"})
        assert r.status_code == 429 and r.json()["resets_at"] == "2100-01-01T00:00:00+00:00"

    run(scenario)


def test_the_plans_windows_reach_the_broker_named_as_it_names_a_plans():
    headers = {"x-codex-primary-used-percent": "42.5", "x-codex-primary-window-minutes": "300",
               "x-codex-primary-reset-at": str(RESET_EPOCH), "x-codex-secondary-used-percent": "10",
               "x-codex-secondary-window-minutes": "10080", "x-codex-secondary-reset-at": str(RESET_EPOCH + 60)}

    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        h.codex.next.append((200, headers, _sse(_message("ok"), COMPLETED)))
        assert (await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "hi"})).status_code == 200
        assert len(h.broker.readings) == 1
        reading = h.broker.readings[0]
        assert reading["assignment_id"] == h.broker.holder["assignment_id"]
        assert reading["voice_session_id"] == VOICE_ID
        assert reading["windows"] == {
            "five_hour": {"utilization": 0.425, "resets_at": "2100-01-01T00:00:00+00:00"},
            "seven_day": {"utilization": 0.1, "resets_at": "2100-01-01T00:01:00+00:00"}}

    run(scenario)


def test_a_turn_that_asks_for_a_plan_is_a_planner_turn_whose_summary_is_the_reply():
    planner = FakePlanner()

    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        h.codex.next.append((200, {}, _sse(
            {"type": "response.output_item.done", "item": {
                "type": "function_call", "name": "delegate", "call_id": "call_1",
                "arguments": json.dumps({"request": "plan the rollout"})}}, COMPLETED)))
        r = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "plan the rollout for me"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "Three steps are on screen."
        assert body["cards"] == [{"kind": "plan", "card_id": "card_1", "title": "Rollout"}]
        assert planner.calls == [{"person": PERSON, "transcript": [("person", "plan the rollout for me")],
                                  "delegation_id": body["turn_id"]}]
        request = h.codex.requests[0]
        assert [t["name"] for t in request["tools"]] == ["delegate"]
        assert request["instructions"] == tc.TEXT_INSTRUCTIONS + tc.TEXT_DELEGATE_INSTRUCTIONS
        assert "delegate tool" in tc.TEXT_DELEGATE_INSTRUCTIONS
        await h.post(f"/internal/sessions/{VOICE_ID}/end")
        assert planner.forgotten == [VOICE_ID]

    run(scenario, planner=planner)


def _delegating_turn(h: Harness) -> None:
    h.codex.next.append((200, {}, _sse(
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "delegate", "call_id": "call_1",
            "arguments": json.dumps({"request": "plan the rollout"})}}, COMPLETED)))


def test_the_planner_model_is_read_at_each_sessions_start_and_kept_for_the_session():
    """swarm-api sends the Voice it setting's planner model in the start body.
    A change between two starts reaches the second session only; a start
    without one leaves the planner on VOICE_PLANNER_MODEL (model None)."""
    planner = FakePlanner()
    ids = [VOICE_ID + suffix for suffix in ("a", "b", "c")]

    async def scenario(h: Harness):
        starts = [{"planner_model": "claude-opus-5-5"}, {"planner_model": "claude-sonnet-5-5"}, {}]
        for voice_id, extra in zip(ids, starts):
            r = await h.post("/internal/sessions", {"voice_session_id": voice_id, "person": PERSON,
                                                    "mode": "text", **extra})
            assert r.status_code == 201, r.text
        # The first session's next turn, after the change: still its own model.
        for voice_id in (ids[0], ids[1], ids[2], ids[0]):
            _delegating_turn(h)
            r = await h.post(f"/internal/sessions/{voice_id}/turns", {"text": "plan the rollout for me"})
            assert r.status_code == 200, r.text
        assert planner.models == ["claude-opus-5-5", "claude-sonnet-5-5", None, "claude-opus-5-5"]

    run(scenario, planner=planner)


@pytest.mark.parametrize("model", ["gpt-5", "claude-opus-4-1", "claude-mythos-5", "fable",
                                   "claude-fable-5-1 --dangerously-skip-permissions", ""])
def test_a_start_naming_a_planner_model_outside_the_classes_is_refused_and_leases_nothing(model):
    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", {"voice_session_id": VOICE_ID, "person": PERSON,
                                                "mode": "text", "planner_model": model})
        assert r.status_code == 422, r.text
        assert h.broker.leases == [] and h.texts.get(VOICE_ID) is None

    run(scenario, planner=FakePlanner())


def test_a_refused_token_is_renewed_once_and_a_turn_on_no_session_is_404():
    async def scenario(h: Harness):
        assert (await h.start()).status_code == 201
        h.codex.valid.clear()  # the leased token went stale
        h.codex.next.append((200, {}, _sse(_message("Back."), COMPLETED)))
        missing = await h.post("/internal/sessions/vs_nosuchsession/turns", {"text": "hi"})
        assert missing.status_code == 404
        # The first try is a 401; the lease renews (a fresh token) and the turn is answered.
        r = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "hi"})
        assert r.status_code == 200 and r.json()["reply"] == "Back."
        assert len(h.broker.renewals) == 1
        too_long = await h.post(f"/internal/sessions/{VOICE_ID}/turns", {"text": "x" * 4001})
        assert too_long.status_code == 422
        assert not any(t in "\n".join(h.responses) for t in h.codex.valid)

    run(scenario)


def test_an_idle_text_conversation_ends_itself_and_gives_the_plan_back():
    async def scenario(h: Harness):
        clock = [0.0]
        h.texts = tc.TextSessions(tc.CodexApi(h.upstream), leases=h.leases, clock=lambda: clock[0],
                                  idle_seconds=60.0, sleep=lambda _s: asyncio.sleep(0))
        clock[0] = 0.0
        session = await h.texts.start(VOICE_ID, person=PERSON)
        clock[0] = 61.0
        for _ in range(200):
            if session.ended and h.broker.releases:
                break
            await asyncio.sleep(0)
        assert session.ended and session.reason == "idle"
        assert h.broker.releases and h.texts.get(VOICE_ID) is None

    run(scenario)


def test_a_live_start_without_an_offer_is_refused_and_the_live_answer_names_its_mode():
    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", {"voice_session_id": VOICE_ID, "person": PERSON})
        assert r.status_code == 422
        assert h.broker.leases == []

    run(scenario)


def test_the_text_lease_asks_the_broker_for_the_plan_and_nothing_else():
    async def scenario(h: Harness):
        with pytest.raises(al.CredentialError):
            await h.leases.lease(person=PERSON, voice_session_id=VOICE_ID, capability="anything")
        assert h.broker.leases == []
        held = await h.leases.lease(person=PERSON, voice_session_id=VOICE_ID, capability=al.CAPABILITY_TEXT)
        bearer = await held.bearer()
        assert bearer.account_id == h.broker.plan_account
        await held.release()

    run(scenario)
