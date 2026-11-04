"""swarm-voice opens a GPT-Live session from a browser's offer on an account leased
from the quota broker's Voice it pool, and holds its sideband (docs/design/voice-it.md
section 10, phase one; owner, 2026-11-02: the Voice it pool, "pool only, wait for it").

Against a fake OpenAI -- the Live REST surface on an `httpx.MockTransport`, the
sideband on a real local `websockets` server -- and a fake quota broker on the
same transport, this holds that:

* each session leases a Voice it pool account for its person before anything is
  created, asking for a Live-capable account, and every Live call presents that
  lease's OpenAI API key as `Authorization` alone, with no `ChatGPT-Account-Id`
  (OpenAI refused the ChatGPT plan for GPT-Live on 4 November 2026);
* a person the broker refuses, or a pool whose account is busy or missing, gets a
  clear refusal and nothing starts;
* the lease is released when the session ends, when its start fails and when
  OpenAI closes the session; it is renewed (with a fresh token) before it goes stale;
* an end sends `session.close` on the sideband and closes the socket; a sideband
  that dropped is attached again to close it;
* no code path reads a person's own ChatGPT sign-in: there is no Secret Manager
  path for the OpenAI credential;
* a token never appears in a log line, a response or an error.

Every token here is built at runtime with `secrets.token_hex`; nothing needs a
cloud credential or an emulator.
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
from websockets.asyncio.server import serve

# swarm-voice is not a member of the root uv workspace (the root pyproject and
# uv.lock are outside this lane), so its package is put on the path the way
# tests/unit/live/test_relay.py reaches swarm-live.
_APP = Path(__file__).resolve().parents[3] / "apps" / "swarm-voice"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from swarm_voice import accountlease as al  # noqa: E402
from swarm_voice import credential as cred  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice.main import create_app  # noqa: E402

PERSON = "bogdan@saga.xyz"
OTHER = "someone-else@example.com"
FAKE_LOG = "fake_openai"
CALLER = "swarm-api@voice-test-project.iam.gserviceaccount.com"
BROKER = "https://broker.test"
BROKER_AUDIENCE = "https://swarm-quota-broker.test"


def _offer() -> str:
    return "v=0\r\no=- " + secrets.token_hex(4) + " 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


# ---------------------------------------------------------------- doubles


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeBroker:
    """The quota broker's three Voice it pool routes, in memory: one account, held
    by at most one voice session at a time."""

    def __init__(self, openai: "FakeOpenAI") -> None:
        self.openai = openai
        self.people = {PERSON}
        self.registered = True
        self.account_id = "acct_" + secrets.token_hex(4)
        #: A plan account id the broker must NOT send for a key; kept so a
        #: header carrying it would be caught.
        self.plan_account = "chatgpt-" + secrets.token_hex(4)
        self.holder: dict | None = None
        self.leases: list[dict] = []
        self.renewals: list[dict] = []
        self.releases: list[dict] = []
        self.id_tokens: list[str] = []
        self.audiences: list[str] = []

    def _token(self) -> dict:
        access = "sk-" + secrets.token_hex(16)
        self.openai.valid.add(access)
        return {"access_token": access, "kind": "api_key"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.id_tokens.append(request.headers.get("authorization", ""))
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path == "/v1/voice/lease":
            self.leases.append(body)
            refusal = {"account_id": None, "assignment_id": None, "credential": None, "next_reset_at": None}
            if body.get("person") not in self.people:
                return httpx.Response(200, json={**refusal, "reason": "not_in_voice_pool"})
            if not self.registered:
                return httpx.Response(200, json={**refusal, "reason": "no_voice_account_registered"})
            if self.holder is not None:
                return httpx.Response(200, json={**refusal, "reason": "no_voice_account_available"})
            self.holder = {"assignment_id": "asg_" + secrets.token_hex(6),
                           "voice_session_id": body["voice_session_id"], "person": body["person"]}
            return httpx.Response(200, json={
                "account_id": self.account_id, "assignment_id": self.holder["assignment_id"],
                "label": "voice", "display_name": "Voice it", "expires_at": "2026-11-02T10:10:00Z",
                "credential": self._token(), "reason": ""})
        match = re.fullmatch(r"/v1/voice/lease/([^/]+)/(renew|release)", path)
        if match and match.group(1) == self.account_id:
            held = (self.holder is not None and self.holder["assignment_id"] == body.get("assignment_id")
                    and self.holder["voice_session_id"] == body.get("voice_session_id"))
            if match.group(2) == "renew":
                self.renewals.append(body)
                if not held:
                    return httpx.Response(404, json={"detail": "no such hold"})
                return httpx.Response(200, json={"expires_at": "2026-11-02T10:20:00Z", "credential": self._token()})
            self.releases.append(body)
            if held:
                self.holder = None
            return httpx.Response(200, json={"released": held})
        return httpx.Response(404)


class FakeOpenAI:
    """The Live REST create, plus a sideband server. A bearer is valid when the
    fake broker minted it and it has not been refused."""

    def __init__(self) -> None:
        self.valid: set[str] = set()
        self.creates: list[dict] = []
        self.create_headers: list[dict] = []
        self.live_ids: list[str] = []
        self.answers: dict[str, str] = {}
        self.attaches: list[str] = []
        self.received: list[dict] = []
        self.closed_by_client: list[str] = []
        self.drop_after_attach = False
        self.close_after_attach = False
        self.create_status = 201
        #: The refused create's body, when set; else an error whose message echoes the bearer.
        self.create_body: bytes | None = None
        #: An event the sideband sends in place of `session.updated`, when set.
        self.attach_event: dict | None = None
        self.port = 0

    # -- REST
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/live/sessions" and request.method == "POST":
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[len("Bearer "):] not in self.valid:
                return httpx.Response(401, json={"error": {"message": f"bad token {auth}"}})
            if self.create_status != 201:
                if self.create_body is not None:
                    return httpx.Response(self.create_status, content=self.create_body)
                return httpx.Response(self.create_status, json={"error": {"message": f"echo {auth}"}})
            body = json.loads(request.content)
            self.creates.append(body)
            self.create_headers.append({"authorization": auth,
                                        "chatgpt-account-id": request.headers.get("chatgpt-account-id")})
            live_id = "live_" + secrets.token_hex(6)
            self.live_ids.append(live_id)
            answer = "v=0\r\no=- answer-" + secrets.token_hex(4) + " IN IP4 127.0.0.1\r\n"
            self.answers[live_id] = answer
            return httpx.Response(201, json={
                "session": {"id": live_id, "object": "live.session", "model": body["session"]["model"]},
                "transport": {"type": "webrtc", "sdp": answer},
            })
        return httpx.Response(404)

    # -- sideband
    async def sideband(self, conn) -> None:
        path = conn.request.path
        parts = path.strip("/").split("/")
        if parts[:3] != ["v1", "live", "sessions"] or len(parts) != 5 or parts[4] != "attach":
            await conn.close(4404)
            return
        live_id = parts[3]
        auth = conn.request.headers.get("Authorization") or ""
        if auth[len("Bearer "):] not in self.valid:
            await conn.close(4401)
            return
        self.attaches.append(live_id)
        if self.attach_event is not None:
            await conn.send(json.dumps(self.attach_event))
            await conn.close()
            return
        await conn.send(json.dumps({"type": "session.updated",
                                    "session": {"id": live_id, "status": "active", "model": "gpt-live-1"}}))
        if self.drop_after_attach:
            self.drop_after_attach = False
            await conn.close(1011)
            return
        if self.close_after_attach:
            # OpenAI ends the session on its own (its duration limit).
            await conn.send(json.dumps({"type": "session.closed", "reason": "max_duration",
                                        "usage": {"seconds": 7}, "session": {"id": live_id}}))
            await conn.close()
            return
        await conn.send(json.dumps({"type": "session.input_transcript.delta", "delta": "hello"}))
        async for raw in conn:
            event = json.loads(raw)
            self.received.append(event)
            if event.get("type") == "session.close":
                self.closed_by_client.append(live_id)
                await conn.send(json.dumps({"type": "session.closed", "reason": "client_close",
                                            "usage": {"seconds": 42}, "session": {"id": live_id}}))
                await conn.close()
                return


async def _caller_ok(token: str) -> str:
    if token != "caller-token":
        raise PermissionError("not swarm-api")
    return CALLER


class Harness:
    def __init__(self, openai: FakeOpenAI, broker: FakeBroker, clock: Clock, server) -> None:
        self.openai = openai
        self.broker = broker
        self.clock = clock
        port = server.sockets[0].getsockname()[1] if hasattr(server, "sockets") else None

        def route(request: httpx.Request) -> httpx.Response:
            if request.url.host == "broker.test":
                return broker.handle(request)
            return openai.handle(request)

        async def id_token(audience: str) -> str:
            broker.audiences.append(audience)
            return "idt_" + secrets.token_hex(8)

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(route))
        self.api = live.LiveApi(self.upstream, ws_base=f"ws://127.0.0.1:{port}")
        self.leases = al.VoiceBroker(self.upstream, BROKER, audience=BROKER_AUDIENCE,
                                     token_fetcher=id_token, clock=clock)
        self.sessions = live.Sessions(self.api, leases=self.leases, close_grace=2.0)
        self.app = create_app(sessions=self.sessions, verify_caller=_caller_ok)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://voice")
        self.responses: list[str] = []

    async def post(self, path: str, body: dict | None = None, token: str = "caller-token") -> httpx.Response:
        response = await self.client.post(path, json=body or {}, headers={"Authorization": f"Bearer {token}"})
        self.responses.append(response.text)
        return response

    async def settle(self) -> None:
        """Let the lease releases in flight reach the broker."""
        await self.sessions.drain_releases()

    async def close(self) -> None:
        await self.client.aclose()
        await self.upstream.aclose()


def run(scenario):
    """Run `scenario(harness)` against a fresh fake OpenAI and broker."""

    async def main():
        openai = FakeOpenAI()
        broker = FakeBroker(openai)
        clock = Clock()
        # The fake's own server logs the headers it RECEIVES, as any server would; that
        # is OpenAI's side of the wire, so its records are left out of the log check.
        async with serve(openai.sideband, "127.0.0.1", 0, logger=logging.getLogger(FAKE_LOG)) as server:
            h = Harness(openai, broker, clock, server)
            try:
                return await asyncio.wait_for(scenario(h), 20)
            finally:
                await h.sessions.end_all()
                await h.close()

    return asyncio.run(main())


def _start_body(voice_id: str = "vs_" + "a1b2c3d4e5", person: str = PERSON) -> dict:
    return {"voice_session_id": voice_id, "person": person, "offer": _offer()}


async def _detached(held) -> None:
    for _ in range(100):
        if not held.attached:
            break
        await asyncio.sleep(0.01)
    assert not held.attached


# ---------------------------------------------------------------- tests


def test_the_offer_is_answered_on_a_leased_voice_pool_account():
    async def scenario(h: Harness):
        body = _start_body()
        r = await h.post("/internal/sessions", body)
        assert r.status_code == 201, r.text
        live_id = h.openai.live_ids[0]
        assert r.json() == {"voice_session_id": body["voice_session_id"], "live_session_id": live_id,
                            "answer": h.openai.answers[live_id]}
        # The lease came first, for this person and this session, from the openai pool,
        # with an ID token minted for the broker's audience.
        assert h.broker.leases == [{"person": PERSON, "voice_session_id": body["voice_session_id"],
                                    "provider": "openai", "capability": "live"}]
        assert h.broker.audiences and set(h.broker.audiences) == {BROKER_AUDIENCE}
        assert all(t.startswith("Bearer idt_") for t in h.broker.id_tokens)
        # The Live create presented the leased API key, and no plan account id.
        assert h.openai.create_headers[0]["authorization"].removeprefix("Bearer ") in h.openai.valid
        assert h.openai.create_headers[0]["chatgpt-account-id"] is None
        create = h.openai.creates[0]
        assert create["transport"] == {"type": "webrtc", "sdp": body["offer"]}
        assert create["session"]["model"] == "gpt-live-1"
        # The caller picks nothing about the session: the instructions are swarm-voice's own.
        assert create["session"]["instructions"] == live.INSTRUCTIONS
        # Attached before the answer went back, and held; the lease is held too.
        assert h.openai.attaches == [live_id]
        held = h.sessions.get(body["voice_session_id"])
        assert held is not None and held.attached
        assert h.broker.holder["voice_session_id"] == body["voice_session_id"]
        assert h.broker.releases == []
        health = await h.client.get("/healthz")
        assert health.json() == {"sessions": 1, "attached": 1}

    run(scenario)


def test_a_person_the_broker_refuses_is_refused_and_nothing_starts():
    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", _start_body(person=OTHER))
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "voice_pool_refused"
        assert isinstance(r.json()["message"], str) and r.json()["message"].endswith(".")
        assert h.openai.creates == [] and h.openai.attaches == []
        assert h.sessions.get("vs_a1b2c3d4e5") is None
        assert h.broker.holder is None and h.broker.releases == []

    run(scenario)


def test_a_busy_or_empty_pool_is_a_spoken_409_and_nothing_starts():
    async def scenario(h: Harness):
        first = _start_body(voice_id="vs_first00001")
        assert (await h.post("/internal/sessions", first)).status_code == 201
        busy = await h.post("/internal/sessions", _start_body(voice_id="vs_second0002"))
        assert busy.status_code == 409
        assert busy.json() == {"code": "voice_pool_busy", "message":
                               "Voice it's ChatGPT plan is in use by another session; try again when it ends."}
        assert len(h.openai.creates) == 1 and h.sessions.get("vs_second0002") is None
        # Once the first ends, its account is free again.
        assert (await h.post(f"/internal/sessions/{first['voice_session_id']}/end")).status_code == 200
        await h.settle()
        assert h.broker.holder is None
        h.broker.registered = False
        empty = await h.post("/internal/sessions", _start_body(voice_id="vs_third00003"))
        assert empty.status_code == 409 and empty.json()["code"] == "voice_pool_refused"
        assert len(h.openai.creates) == 1

    run(scenario)


def test_an_end_closes_the_session_and_releases_the_lease():
    async def scenario(h: Harness):
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        assignment = h.broker.holder["assignment_id"]
        held = h.sessions.get(body["voice_session_id"])
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200, r.text
        assert r.json() == {"voice_session_id": body["voice_session_id"], "ended": True,
                            "usage_seconds": 42, "reason": "client_close"}
        assert h.openai.closed_by_client == [live_id]
        assert held.socket_closed
        assert h.sessions.get(body["voice_session_id"]) is None
        await h.settle()
        assert h.broker.holder is None
        assert {"assignment_id": assignment, "voice_session_id": body["voice_session_id"]} in h.broker.releases
        # Ending again is the same 404 as a session never started.
        assert (await h.post(f"/internal/sessions/{body['voice_session_id']}/end")).status_code == 404

    run(scenario)


def test_a_session_openai_closes_on_its_own_releases_the_lease():
    async def scenario(h: Harness):
        h.openai.close_after_attach = True
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        for _ in range(200):
            if h.sessions.get(body["voice_session_id"]) is None:
                break
            await asyncio.sleep(0.01)
        assert h.sessions.get(body["voice_session_id"]) is None
        await h.settle()
        assert h.broker.holder is None and len(h.broker.releases) == 1

    run(scenario)


def test_a_failed_create_releases_the_lease():
    async def scenario(h: Harness):
        h.openai.create_status = 500
        r = await h.post("/internal/sessions", _start_body())
        assert r.status_code == 502
        assert r.json() == {"code": "openai_refused",
                            "message": "OpenAI refused the Live session (HTTP 500, code unreadable, "
                                       "type unreadable)",
                            "openai_status": 500, "openai_code": "unreadable", "openai_type": "unreadable"}
        assert len(h.broker.leases) == 1
        assert h.broker.holder is None and len(h.broker.releases) == 1
        assert h.sessions.get("vs_a1b2c3d4e5") is None

    run(scenario)


def _log_text(caplog) -> str:
    records = [r for r in caplog.records if not r.name.startswith(FAKE_LOG)]
    return "\n".join(r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "") for r in records)


def test_a_refused_create_says_openais_code_and_type_and_never_its_message(caplog):
    """Measured 2026-11-04: OpenAI answered a Voice it pool account's create 403 and
    nobody could see why. The code and type are kept; the message, which may quote
    the request, is not."""
    caplog.set_level(logging.DEBUG)
    quoted = "sk-" + secrets.token_hex(24)

    async def scenario(h: Harness):
        h.openai.create_status = 403
        h.openai.create_body = json.dumps({"error": {
            "message": f"The plan behind {quoted} may not open a Live session.",
            "code": "unsupported_plan", "type": "invalid_request_error",
            "param": quoted}}).encode()
        r = await h.post("/internal/sessions", _start_body())
        assert r.status_code == 502
        assert r.json() == {"code": "openai_refused",
                            "message": "OpenAI refused the Live session (HTTP 403, code unsupported_plan, "
                                       "type invalid_request_error)",
                            "openai_status": 403, "openai_code": "unsupported_plan",
                            "openai_type": "invalid_request_error"}
        await h.settle()
        assert h.broker.holder is None and len(h.broker.releases) == 1
        return list(h.responses)

    responses = run(scenario)
    text = _log_text(caplog)
    assert "403" in text and "unsupported_plan" in text and "invalid_request_error" in text
    assert quoted not in text
    assert all(quoted not in response for response in responses)


@pytest.mark.parametrize("body", [
    b"<html>403 Forbidden</html>",
    b"",
    b"\xff\xfe",
    json.dumps({"error": "a string"}).encode(),
    json.dumps({"error": {"code": None, "type": 7}}).encode(),
    json.dumps({"error": {"code": "Bearer SK-SECRET", "type": "x" * 65}}).encode(),
    json.dumps({"error": {"code": "a" + " " * 3, "type": "line\nbreak"}}).encode(),
], ids=["html", "empty", "not-utf8", "not-an-object", "not-strings", "off-pattern", "whitespace"])
def test_an_unreadable_refusal_reads_unreadable(body):
    async def scenario(h: Harness):
        h.openai.create_status = 403
        h.openai.create_body = body
        r = await h.post("/internal/sessions", _start_body())
        assert r.status_code == 502
        assert r.json()["openai_status"] == 403
        assert r.json()["openai_code"] == "unreadable" and r.json()["openai_type"] == "unreadable"

    run(scenario)


def test_the_error_reader_keeps_only_the_pattern():
    assert live.openai_error(json.dumps({"error": {"code": "rate_limit.v2-x", "type": "requests"}})) == (
        "rate_limit.v2-x", "requests")
    assert live.openai_error(b"{" + b" " * live.MAX_ERROR_BYTES + b"}") == ("unreadable", "unreadable")
    assert live.openai_error(None) == ("unreadable", "unreadable")


class _OneBearer:
    """A credential with one bearer, built at runtime."""

    async def bearer(self) -> cred.Bearer:
        return cred.Bearer("at_" + secrets.token_hex(8))

    async def refused(self) -> None:
        return None


def test_a_sideband_handshake_refusal_says_openais_code_and_type(caplog):
    caplog.set_level(logging.DEBUG)
    quoted = "sk-" + secrets.token_hex(24)
    body = json.dumps({"error": {"message": f"echo {quoted}", "code": "session_not_found",
                                 "type": "invalid_request_error"}})

    def refuse(connection, request):
        return connection.respond(403, body)

    async def main():
        async with serve(lambda conn: conn.close(), "127.0.0.1", 0, process_request=refuse,
                         logger=logging.getLogger(FAKE_LOG)) as server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient() as http:
                api = live.LiveApi(http, ws_base=f"ws://127.0.0.1:{port}")
                credential = _OneBearer()
                with pytest.raises(live.LiveRefused) as raised:
                    await api.attach(credential, "live_" + secrets.token_hex(6))
        return raised.value

    refused = asyncio.run(main())
    assert (refused.status, refused.code, refused.type) == (403, "session_not_found", "invalid_request_error")
    assert quoted not in str(refused) and quoted not in _log_text(caplog)
    assert "session_not_found" in _log_text(caplog)


def test_a_sideband_that_answers_with_an_error_event_is_a_refusal_with_its_code():
    async def scenario(h: Harness):
        quoted = "sk-" + secrets.token_hex(24)
        h.openai.attach_event = {"type": "error", "error": {
            "message": f"echo {quoted}", "code": "plan_not_allowed", "type": "invalid_request_error"}}
        r = await h.post("/internal/sessions", _start_body())
        assert r.status_code == 502
        assert r.json()["code"] == "openai_refused"
        assert (r.json()["openai_status"], r.json()["openai_code"], r.json()["openai_type"]) == (
            None, "plan_not_allowed", "invalid_request_error")
        assert quoted not in r.text
        await h.settle()
        assert h.broker.holder is None and len(h.broker.releases) == 1

    run(scenario)


def test_no_answer_is_returned_and_the_lease_released_when_the_sideband_cannot_attach():
    async def scenario(h: Harness):
        h.api.ws_base = "ws://127.0.0.1:1"
        r = await h.post("/internal/sessions", _start_body())
        assert r.status_code == 502
        assert "answer" not in r.json()
        assert h.sessions.get("vs_a1b2c3d4e5") is None
        assert h.broker.holder is None and len(h.broker.releases) == 1

    run(scenario)


def test_a_dropped_sideband_is_attached_again_to_close_the_session():
    async def scenario(h: Harness):
        h.openai.drop_after_attach = True
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        held = h.sessions.get(body["voice_session_id"])
        await _detached(held)
        # Still leased: the re-attach needs the account's token.
        assert h.broker.holder is not None
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200 and r.json()["ended"] is True
        assert h.openai.attaches == [live_id, live_id]
        assert h.openai.closed_by_client == [live_id]
        await h.settle()
        assert h.broker.holder is None

    run(scenario)


def test_an_end_whose_reattach_fails_keeps_the_session_and_its_lease_so_a_retry_closes_it():
    async def scenario(h: Harness):
        h.openai.drop_after_attach = True
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        held = h.sessions.get(body["voice_session_id"])
        await _detached(held)
        reachable = h.api.ws_base
        h.api.ws_base = "ws://127.0.0.1:1"  # the re-attach fails once
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 502
        # Still held, lease and all: forgetting it here would leave the GPT-Live session billing.
        assert h.sessions.get(body["voice_session_id"]) is held and not held.ended
        assert h.broker.holder is not None and h.broker.releases == []
        h.api.ws_base = reachable
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200 and r.json()["ended"] is True
        assert h.openai.closed_by_client == [live_id]
        assert h.sessions.get(body["voice_session_id"]) is None
        await h.settle()
        assert h.broker.holder is None

    run(scenario)


def test_a_second_live_session_for_one_voice_session_is_refused_without_a_second_lease():
    async def scenario(h: Harness):
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        again = await h.post("/internal/sessions", {**body, "offer": _offer()})
        assert again.status_code == 409
        assert len(h.openai.creates) == 1 and len(h.broker.leases) == 1

    run(scenario)


def test_only_the_callers_identity_is_admitted():
    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", _start_body(), token="not-the-caller")
        assert r.status_code == 401
        r = await h.client.post("/internal/sessions", json=_start_body())
        assert r.status_code == 401
        assert h.openai.creates == [] and h.broker.leases == []

    run(scenario)


def test_the_start_takes_no_credential_from_the_caller():
    async def scenario(h: Harness):
        ref = "projects/p/secrets/swarm-tenant-u-x-openai/versions/1"
        r = await h.post("/internal/sessions", {**_start_body(), "credential": ref})
        # An extra field is ignored: the session runs on the lease, never on what the caller names.
        assert r.status_code == 201
        assert len(h.broker.leases) == 1

    run(scenario)


def test_the_lease_is_renewed_with_a_fresh_token_before_it_goes_stale():
    async def scenario(h: Harness):
        lease = await h.leases.lease(person=PERSON, voice_session_id="vs_renew00001")
        first = await lease.bearer()
        assert (await lease.bearer()).reveal() == first.reveal() and h.broker.renewals == []
        h.clock.now += al.RENEW_SECONDS
        fresh = await lease.bearer()
        assert fresh.reveal() != first.reveal() and len(h.broker.renewals) == 1
        assert fresh.account_id is None and fresh.headers().keys() == {"Authorization"}
        # OpenAI refused a bearer: the next one is renewed, never reused.
        await lease.refused()
        assert (await lease.bearer()).reveal() != fresh.reveal() and len(h.broker.renewals) == 2
        await lease.release()
        assert h.broker.holder is None
        with pytest.raises(cred.CredentialError):
            await lease.bearer()
        await lease.release()  # idempotent: no second call
        assert len(h.broker.releases) == 1

    run(scenario)


def test_a_hold_the_broker_no_longer_has_ends_the_lease():
    async def scenario(h: Harness):
        lease = await h.leases.lease(person=PERSON, voice_session_id="vs_gone000001")
        h.broker.holder = None  # expired on the broker's side
        with pytest.raises(al.HoldGone):
            await lease.renew()
        assert lease.lost
        with pytest.raises(cred.CredentialError):
            await lease.bearer()

    run(scenario)


def test_a_token_never_appears_in_a_log_line_a_response_or_an_error(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario(h: Harness):
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        await h.settle()
        # A refused create whose error body quotes the request.
        h.openai.create_status = 500
        failed = await h.post("/internal/sessions", _start_body(voice_id="vs_refused001"))
        assert failed.status_code == 502
        return sorted(h.openai.valid), list(h.responses)

    tokens, responses = run(scenario)
    assert len(tokens) >= 2
    records = [r for r in caplog.records if not r.name.startswith(FAKE_LOG)]
    text = "\n".join(r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "") for r in records)
    assert records, "nothing was logged, so the check below would prove nothing"
    for value in tokens:
        assert value not in text
        assert all(value not in response for response in responses)


def test_credential_reprs_hold_no_secret():
    access = "at_" + secrets.token_hex(16)
    token = cred.Bearer(access, "chatgpt-" + secrets.token_hex(4))
    assert access not in repr(token) and access not in str(token)
    assert token.reveal() == access
    assert token.headers()["Authorization"] == f"Bearer {access}"
    assert cred.Bearer(access).headers() == {"Authorization": f"Bearer {access}"}


def test_no_code_reads_a_persons_own_chatgpt_sign_in():
    # The personal-secret path is gone from credential.py ...
    for name in ("personal_secret_id", "check_scope", "VersionRef", "parse_ref", "VERSION_REF",
                 "SecretManagerStore", "SubscriptionCredential", "CredentialFactory", "TOKEN_URL"):
        assert not hasattr(cred, name), name
    package = _APP / "swarm_voice"
    sources = {p.name: p.read_text() for p in package.glob("*.py")}
    assert {"accountlease.py", "credential.py", "live.py", "main.py"} <= set(sources)
    for name, text in sources.items():
        # ... no module names an OpenAI secret, refreshes a ChatGPT token or adds a secret version ...
        assert not re.search(r"swarm-tenant-[^\"'\s]*openai|-openai\b", text), name
        assert "auth.openai.com" not in text and "refresh_token" not in text, name
        assert ":addVersion" not in text, name
        # ... and the session path touches no Secret Manager at all. The planner's own
        # Anthropic key (planner.py) is the one Secret Manager read left in swarm-voice.
        if name != "planner.py":
            assert "secretmanager" not in text.lower() and "swarm-tenant-" not in text, name


def test_the_caller_check_refuses_to_run_without_an_audience_or_callers():
    from swarm_voice.main import google_caller_check

    with pytest.raises(RuntimeError, match="SWARM_VOICE_AUDIENCE"):
        google_caller_check("", frozenset({CALLER}))
    with pytest.raises(RuntimeError, match="SWARM_VOICE_CALLERS"):
        google_caller_check("https://voice.internal", frozenset())


def test_nothing_is_built_at_import_and_a_start_without_the_broker_refuses(monkeypatch):
    from swarm_voice.main import build_sessions

    create_app()  # builds no client: everything waits for the app's start
    monkeypatch.delenv("QUOTA_BROKER_URL", raising=False)
    with pytest.raises(RuntimeError, match="QUOTA_BROKER_URL"):
        build_sessions(http=None)


def test_an_api_key_bearer_sends_authorization_only_even_when_given_an_account_id():
    """A key is the whole credential: `ChatGPT-Account-Id` names a ChatGPT
    plan's account, and a broker that sent one beside a key is not obeyed."""
    from swarm_voice.credential import Bearer

    key = "sk-" + secrets.token_hex(16)
    bearer = al._credential({"credential": {"access_token": key, "kind": "api_key",
                                            "account_id": "chatgpt-" + secrets.token_hex(4)}})
    assert bearer.headers() == {"Authorization": f"Bearer {key}"}
    assert bearer.account_id is None and bearer.kind == "api_key"
    assert key not in repr(bearer)
    assert Bearer(key, kind="api_key", account_id="x").headers() == {"Authorization": f"Bearer {key}"}
