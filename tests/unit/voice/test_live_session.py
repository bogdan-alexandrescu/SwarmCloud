"""swarm-voice opens a GPT-Live session from a browser's offer and holds its sideband
(docs/design/voice-it.md section 10, phase one, the swarm-voice lane).

Against a fake OpenAI -- the Live REST surface and the OAuth token endpoint on an
`httpx.MockTransport`, the sideband on a real local `websockets` server -- this
holds that:

* the browser's offer is answered with OpenAI's SDP answer, and the sideband is
  attached before the answer goes back;
* an end sends `session.close` on the sideband and closes the socket, so nothing
  is left billing; a sideband that dropped is attached again to close it;
* the credential is read from Secret Manager by version reference, only from the
  person's own scope, refreshed when it expires (a rotated refresh token becomes
  a new secret version), and never appears in a log line, a response or an
  error.

Every token here is built at runtime with `secrets.token_hex`; nothing needs a
cloud credential or an emulator: Secret Manager and the caller check are
in-memory doubles defined in this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from websockets.asyncio.server import serve

# swarm-voice is not a member of the root uv workspace (the root pyproject and
# uv.lock are outside this lane), so its package is put on the path the way
# tests/unit/live/test_relay.py reaches swarm-live.
_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_common.identity import tenant_id_for_user  # noqa: E402
from swarm_voice import credential as cred  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice.main import create_app  # noqa: E402

PROJECT = "voice-test-project"
PERSON = "person@example.com"
OTHER = "someone-else@example.com"
FAKE_LOG = "fake_openai"
CALLER = "swarm-api@voice-test-project.iam.gserviceaccount.com"


def _secret(person: str) -> str:
    return f"projects/{PROJECT}/secrets/swarm-tenant-{tenant_id_for_user(person)}-openai"


def _offer() -> str:
    return "v=0\r\no=- " + secrets.token_hex(4) + " 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


# ---------------------------------------------------------------- doubles


class FakeSecrets:
    """Secret Manager in memory: version reference -> payload."""

    def __init__(self) -> None:
        self.versions: dict[str, bytes] = {}
        self.reads: list[str] = []

    def put(self, secret: str, payload: dict) -> str:
        ref = f"{secret}/versions/{len([v for v in self.versions if v.startswith(secret + '/')]) + 1}"
        self.versions[ref] = json.dumps(payload).encode()
        return ref

    async def access(self, version_ref: str) -> bytes:
        self.reads.append(version_ref)
        if version_ref not in self.versions:
            raise cred.CredentialError("the credential's secret version could not be read (HTTP 404)")
        return self.versions[version_ref]

    async def add_version(self, secret: str, payload: bytes) -> str:
        return self.put(secret, json.loads(payload))


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeOpenAI:
    """The Live REST create and the OAuth token endpoint, plus a sideband server."""

    def __init__(self) -> None:
        self.client_id = "app_" + secrets.token_hex(6)
        self.refresh_token = "rt_" + secrets.token_hex(16)
        self.access_tokens: list[str] = []
        self.token_requests: list[dict] = []
        self.creates: list[dict] = []
        self.live_ids: list[str] = []
        self.answers: dict[str, str] = {}
        self.attaches: list[str] = []
        self.received: list[dict] = []
        self.closed_by_client: list[str] = []
        self.refuse_refresh = False
        self.drop_after_attach = False
        self.port = 0

    # -- REST
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if self.refuse_refresh or form.get("refresh_token") != self.refresh_token:
                # An error body that quotes the request, as some servers do.
                return httpx.Response(400, json={"error": "invalid_grant", "echo": form})
            access = "at_" + secrets.token_hex(16)
            self.access_tokens.append(access)
            self.refresh_token = "rt_" + secrets.token_hex(16)  # rotated on every refresh
            return httpx.Response(200, json={"access_token": access, "token_type": "Bearer",
                                             "expires_in": 3600, "refresh_token": self.refresh_token})
        if request.url.path == "/v1/live/sessions" and request.method == "POST":
            auth = request.headers.get("authorization", "")
            if not self.access_tokens or auth != f"Bearer {self.access_tokens[-1]}":
                return httpx.Response(401, json={"error": {"message": f"bad token {auth}"}})
            body = json.loads(request.content)
            self.creates.append(body)
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
        if conn.request.headers.get("Authorization") != f"Bearer {self.access_tokens[-1]}":
            await conn.close(4401)
            return
        self.attaches.append(live_id)
        await conn.send(json.dumps({"type": "session.updated",
                                    "session": {"id": live_id, "status": "active", "model": "gpt-live-1"}}))
        if self.drop_after_attach:
            self.drop_after_attach = False
            await conn.close(1011)
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
    def __init__(self, openai: FakeOpenAI, store: FakeSecrets, clock: Clock, server) -> None:
        self.openai = openai
        self.store = store
        self.clock = clock
        port = server.sockets[0].getsockname()[1] if hasattr(server, "sockets") else None
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(openai.handle))
        self.api = live.LiveApi(self.upstream, ws_base=f"ws://127.0.0.1:{port}")
        self.sessions = live.Sessions(
            self.api,
            credentials=cred.CredentialFactory(store=store, http=self.upstream, project=PROJECT, clock=clock),
            close_grace=2.0,
        )
        self.app = create_app(sessions=self.sessions, verify_caller=_caller_ok)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://voice")
        self.responses: list[str] = []

    async def post(self, path: str, body: dict | None = None, token: str = "caller-token") -> httpx.Response:
        response = await self.client.post(path, json=body or {}, headers={"Authorization": f"Bearer {token}"})
        self.responses.append(response.text)
        return response

    async def close(self) -> None:
        await self.client.aclose()
        await self.upstream.aclose()


def run(scenario):
    """Run `scenario(harness)` against a fresh fake OpenAI."""

    async def main():
        openai = FakeOpenAI()
        store = FakeSecrets()
        clock = Clock()
        # The fake's own server logs the headers it RECEIVES, as any server would; that
        # is OpenAI's side of the wire, so its records are left out of the log check.
        async with serve(openai.sideband, "127.0.0.1", 0, logger=logging.getLogger(FAKE_LOG)) as server:
            h = Harness(openai, store, clock, server)
            try:
                return await asyncio.wait_for(scenario(h), 20)
            finally:
                await h.sessions.end_all()
                await h.close()

    return asyncio.run(main())


def _seed(h: Harness, person: str = PERSON) -> str:
    return h.store.put(_secret(person), {"kind": "chatgpt_subscription", "client_id": h.openai.client_id,
                                         "refresh_token": h.openai.refresh_token})


def _start_body(ref: str, voice_id: str = "vs_" + "a1b2c3d4e5", person: str = PERSON) -> dict:
    return {"voice_session_id": voice_id, "person": person, "credential": ref, "offer": _offer()}


# ---------------------------------------------------------------- tests


def test_the_offer_is_answered_and_the_sideband_attached():
    async def scenario(h: Harness):
        ref = _seed(h)
        body = _start_body(ref)
        r = await h.post("/internal/sessions", body)
        assert r.status_code == 201, r.text
        live_id = h.openai.live_ids[0]
        assert r.json() == {"voice_session_id": body["voice_session_id"], "live_session_id": live_id,
                            "answer": h.openai.answers[live_id]}
        create = h.openai.creates[0]
        assert create["transport"] == {"type": "webrtc", "sdp": body["offer"]}
        assert create["session"]["model"] == "gpt-live-1"
        # The caller picks nothing about the session: the instructions are swarm-voice's own.
        assert create["session"]["instructions"] == live.INSTRUCTIONS
        # Attached before the answer went back, and held.
        assert h.openai.attaches == [live_id]
        held = h.sessions.get(body["voice_session_id"])
        assert held is not None and held.attached
        assert h.store.reads == [ref]
        health = await h.client.get("/healthz")
        assert health.json() == {"sessions": 1, "attached": 1}

    run(scenario)


def test_an_end_closes_the_session_and_the_sideband():
    async def scenario(h: Harness):
        body = _start_body(_seed(h))
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        held = h.sessions.get(body["voice_session_id"])
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200, r.text
        assert r.json() == {"voice_session_id": body["voice_session_id"], "ended": True,
                            "usage_seconds": 42, "reason": "client_close"}
        assert h.openai.closed_by_client == [live_id]
        assert held.socket_closed
        assert h.sessions.get(body["voice_session_id"]) is None
        # Ending again is the same 404 as a session never started.
        assert (await h.post(f"/internal/sessions/{body['voice_session_id']}/end")).status_code == 404

    run(scenario)


def test_a_dropped_sideband_is_attached_again_to_close_the_session():
    async def scenario(h: Harness):
        h.openai.drop_after_attach = True
        body = _start_body(_seed(h))
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        held = h.sessions.get(body["voice_session_id"])
        for _ in range(100):
            if not held.attached:
                break
            await asyncio.sleep(0.01)
        assert not held.attached
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200 and r.json()["ended"] is True
        assert h.openai.attaches == [live_id, live_id]
        assert h.openai.closed_by_client == [live_id]

    run(scenario)


def test_an_end_whose_reattach_fails_keeps_the_session_held_so_a_retry_closes_it():
    async def scenario(h: Harness):
        h.openai.drop_after_attach = True
        body = _start_body(_seed(h))
        assert (await h.post("/internal/sessions", body)).status_code == 201
        live_id = h.openai.live_ids[0]
        held = h.sessions.get(body["voice_session_id"])
        for _ in range(100):
            if not held.attached:
                break
            await asyncio.sleep(0.01)
        assert not held.attached
        reachable = h.api.ws_base
        h.api.ws_base = "ws://127.0.0.1:1"  # the re-attach fails once
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 502
        # Still held: forgetting it here would leave the GPT-Live session billing.
        assert h.sessions.get(body["voice_session_id"]) is held and not held.ended
        h.api.ws_base = reachable
        r = await h.post(f"/internal/sessions/{body['voice_session_id']}/end")
        assert r.status_code == 200 and r.json()["ended"] is True
        assert h.openai.closed_by_client == [live_id]
        assert h.sessions.get(body["voice_session_id"]) is None

    run(scenario)


def test_a_second_live_session_for_one_voice_session_is_refused():
    async def scenario(h: Harness):
        body = _start_body(_seed(h))
        assert (await h.post("/internal/sessions", body)).status_code == 201
        again = await h.post("/internal/sessions", {**body, "offer": _offer()})
        assert again.status_code == 409
        assert len(h.openai.creates) == 1

    run(scenario)


def test_only_the_callers_identity_is_admitted():
    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", _start_body(_seed(h)), token="not-the-caller")
        assert r.status_code == 401
        r = await h.client.post("/internal/sessions", json=_start_body(_seed(h)))
        assert r.status_code == 401
        assert h.openai.creates == [] and h.store.reads == []

    run(scenario)


def test_a_credential_outside_the_persons_own_scope_is_never_read():
    async def scenario(h: Harness):
        others = _seed(h, OTHER)
        r = await h.post("/internal/sessions", _start_body(others, person=PERSON))
        assert r.status_code == 403
        workspace = h.store.put(f"projects/{PROJECT}/secrets/swarm-tenant-eng-openai", {"kind": "x"})
        assert (await h.post("/internal/sessions", _start_body(workspace))).status_code == 403
        elsewhere = h.store.put(f"projects/another-project/secrets/swarm-tenant-{tenant_id_for_user(PERSON)}-openai",
                                {"kind": "x"})
        assert (await h.post("/internal/sessions", _start_body(elsewhere))).status_code == 403
        not_a_version = _secret(PERSON)
        assert (await h.post("/internal/sessions", _start_body(not_a_version))).status_code == 422
        assert h.store.reads == [] and h.openai.creates == []

    run(scenario)


def test_an_expired_credential_is_refreshed_and_the_rotated_token_stored_as_a_new_version():
    async def scenario(h: Harness):
        ref = _seed(h)
        first = _start_body(ref, voice_id="vs_first00001")
        assert (await h.post("/internal/sessions", first)).status_code == 201
        assert len(h.openai.token_requests) == 1
        request = h.openai.token_requests[0]
        assert request["grant_type"] == "refresh_token"
        assert request["client_id"] == h.openai.client_id
        assert request["resource"] == "https://api.openai.com/v1"
        assert "scope" not in request
        # The rotated refresh token went back to the person's own secret as a new version.
        newest = f"{_secret(PERSON)}/versions/2"
        assert json.loads(h.store.versions[newest])["refresh_token"] == h.openai.refresh_token
        await h.post(f"/internal/sessions/{first['voice_session_id']}/end")

        # The same reference again, an hour later: the credential is refreshed from the
        # newest version swarm-voice wrote, not the spent one.
        h.clock.now += 3600
        second = _start_body(ref, voice_id="vs_second0002")
        assert (await h.post("/internal/sessions", second)).status_code == 201, h.responses[-1]
        assert len(h.openai.token_requests) == 2
        assert h.openai.token_requests[1]["refresh_token"] == json.loads(h.store.versions[newest])["refresh_token"]

    run(scenario)


def test_a_credential_never_appears_in_a_log_line_a_response_or_an_error(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario(h: Harness):
        spent = [h.openai.refresh_token]
        body = _start_body(_seed(h))
        assert (await h.post("/internal/sessions", body)).status_code == 201
        spent.append(h.openai.refresh_token)
        await h.post(f"/internal/sessions/{body['voice_session_id']}/end")

        # A refused refresh whose error body quotes the request.
        h.openai.refuse_refresh = True
        h.clock.now += 7200
        failed = await h.post("/internal/sessions", _start_body(_seed(h), voice_id="vs_refused001"))
        assert failed.status_code == 502
        assert failed.json() == {"detail": "the OpenAI credential could not be refreshed (HTTP 400)"}
        spent.append(h.openai.refresh_token)
        return spent + list(h.openai.access_tokens)

    values = run(scenario)
    assert len(values) >= 4
    records = [r for r in caplog.records if not r.name.startswith(FAKE_LOG)]
    text = "\n".join(r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "") for r in records)
    assert records, "nothing was logged, so the check below would prove nothing"
    for value in values:
        assert value not in text


def test_no_answer_is_returned_when_the_sideband_cannot_attach():
    async def scenario(h: Harness):
        h.api.ws_base = "ws://127.0.0.1:1"
        r = await h.post("/internal/sessions", _start_body(_seed(h)))
        assert r.status_code == 502
        assert "answer" not in r.json()
        assert h.sessions.get("vs_a1b2c3d4e5") is None

    run(scenario)


def test_credential_reprs_hold_no_secret():
    access = "at_" + secrets.token_hex(16)
    token = cred.Bearer(access)
    assert access not in repr(token) and access not in str(token)
    assert token.reveal() == access


@pytest.mark.parametrize("ref", [
    "projects/p/secrets/s",
    "projects/voice-test-project/secrets/swarm-tenant-u-person-openai/versions/latest/../1",
    "https://evil.example/versions/1",
])
def test_only_a_secret_version_reference_is_accepted(ref):
    with pytest.raises(cred.CredentialRefused):
        cred.check_scope(ref, person=PERSON, project=PROJECT)


def test_the_caller_check_refuses_to_run_without_an_audience_or_callers():
    from swarm_voice.main import google_caller_check

    with pytest.raises(RuntimeError, match="SWARM_VOICE_AUDIENCE"):
        google_caller_check("", frozenset({CALLER}))
    with pytest.raises(RuntimeError, match="SWARM_VOICE_CALLERS"):
        google_caller_check("https://voice.internal", frozenset())


def test_nothing_is_built_at_import_and_a_start_without_a_project_refuses(monkeypatch):
    from swarm_voice.main import build_sessions

    create_app()  # builds no client: everything waits for the app's start
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError, match="PROJECT_ID"):
        build_sessions(http=None)
