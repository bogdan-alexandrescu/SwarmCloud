"""A paused Voice it session stays live, reopens by cause, records what the
owner has seen and bills only open talk (docs/design/voice-it.md §3.12.2,
§3.12.4, §3.12.7, §3.12.8; swarm_api/voice/sessions.py THE PAUSE,
voice/ledger.py `talk_seconds`, routes/voice.py `/reopen` and `/seen`).

swarm-voice reports the pause through the session grant; the session stays
`live`, so its grant still resolves. The browser's reopen goes to swarm-voice
with the project's brief and the session's open cards' one-liners; an ended
session's is 409 and another person's is refused. `seen_seq` only moves
forward. A session paused 60 minutes with nothing delegated running and no
activity from its person ends as `paused_idle` in the abandonment sweep;
paused time while work runs never ends it. swarm-voice is a fake; Firestore
is the in-memory fake; no network, no cloud credential.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.voice import ledger as vledger
from swarm_api.voice import sessions as vs

from .conftest import auth_header, seed_task
from .test_voice_grant import _build, _grant_headers, _start
from .test_voice_sessions import ANSWER, FakeVoice

OFFER_2 = "v=0\r\no=- 7 8 IN IP4 127.0.0.1\r\ns=-\r\n"


class PausingVoice(FakeVoice):
    """FakeVoice plus swarm-voice's internal reopen, recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.reopens: list[dict] = []
        #: An exception the next reopens raise, when set.
        self.reopen_refusal: Exception | None = None

    def reopen_voice(self, session_id: str, *, offer: str, cause: str, brief: dict | None,
                     cards: list[str]) -> vs.StartAnswer:
        self.reopens.append({"session_id": session_id, "offer": offer, "cause": cause,
                             "brief": brief, "cards": cards})
        if self.reopen_refusal is not None:
            raise self.reopen_refusal
        return vs.StartAnswer(answer=ANSWER, live_session_id="live_again_" + session_id[3:9])


@pytest.fixture
def voice() -> PausingVoice:
    return PausingVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice,
                  voice_people=("alice@example.com", "bob@example.com", "root@example.com"))


def _session(db, call: vs.StartCall) -> dict:
    return db.docs[f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}"]


def _report(client: TestClient, call: vs.StartCall, state: str, cause: str | None = None):
    body = {"state": state} if cause is None else {"state": state, "cause": cause}
    return client.post(f"/v1/voice/sessions/{call.session_id}/reopen", json=body,
                       headers=_grant_headers(call))


def _reopen(client: TestClient, call: vs.StartCall, cause: str = "resume", user: str = "alice"):
    return client.post(f"/v1/voice/sessions/{call.session_id}/reopen",
                       json={"offer_sdp": OFFER_2, "cause": cause}, headers=auth_header(user))


def _seen(client: TestClient, call: vs.StartCall, seq: int, user: str = "alice"):
    return client.post(f"/v1/voice/sessions/{call.session_id}/seen", json={"seq": seq},
                       headers=auth_header(user))


def _card(db, call: vs.StartCall, state: str, title: str, *, session_id: str | None = None) -> None:
    card_id = "vc_" + secrets.token_hex(16)
    db.docs[f"{vs.PROJECTS}/{call.project_id}/cards/{card_id}"] = {
        "tenant_id": "eng", "person": "alice@example.com", "project_id": call.project_id,
        "kind": "plan", "session_id": session_id or call.session_id, "version": 1,
        "body": {"state": state, "title": title, "summary": f"{title} summary", "created_by": "planner"}}


# ---- the pause keeps the session live ----------------------------------------------


def test_a_paused_session_stays_live_and_its_grant_still_resolves(client, db, voice):
    seed_task(db, task_id="t-eng", tenant_id="eng")
    call = _start(client, voice)

    reported = _report(client, call, "paused")

    assert reported.status_code == 200, reported.text
    assert reported.json()["voice_state"] == vs.VOICE_PAUSED
    session = _session(db, call)
    assert session["state"] == vs.LIVE and session["voice_state"] == vs.VOICE_PAUSED
    assert session["paused_at"] is not None and session["grant_hash"]
    # The planner and the delegated work keep acting through the grant.
    tasks = client.get("/v1/tasks", headers=_grant_headers(call))
    assert tasks.status_code == 200, tasks.text
    assert voice.ends == []


def test_only_swarm_voices_grant_reports_a_pause(client, db, voice):
    call = _start(client, voice)

    as_person = client.post(f"/v1/voice/sessions/{call.session_id}/reopen", json={"state": "paused"},
                            headers=auth_header("alice"))

    assert as_person.status_code == 403, as_person.text
    assert "voice_state" not in _session(db, call)
    assert _report(client, call, "asleep").status_code == 422


def test_swarm_voices_listening_closes_the_paused_interval(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    assert _report(client, call, "waking", "result").status_code == 200
    assert _session(db, call)["work_running"] is False

    assert _report(client, call, "listening", "result").status_code == 200

    session = _session(db, call)
    assert session["voice_state"] == vs.VOICE_OPEN and session["paused_at"] is None
    (interval,) = session["pauses"]
    assert interval["paused_at"] is not None and interval["woke_at"] >= interval["paused_at"]


# ---- the talk estimate ----------------------------------------------------------------


def test_the_talk_estimate_leaves_paused_time_out():
    t0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    session = {"state": vs.LIVE, "live_at": t0, "voice_state": vs.VOICE_PAUSED,
               "pauses": [{"paused_at": t0 + timedelta(seconds=60), "woke_at": t0 + timedelta(seconds=360)}],
               "paused_at": t0 + timedelta(seconds=600)}

    # Open 0-60 and 360-600; paused since 600.
    assert vledger.talk_seconds(session, t0 + timedelta(seconds=900)) == 300.0
    # Ended while paused: the open pause runs to the end.
    ended = {**session, "state": vs.ENDED, "ended_at": t0 + timedelta(seconds=900)}
    assert vledger.talk_seconds(ended, t0 + timedelta(hours=5)) == 300.0
    woken = {**ended, "paused_at": None}
    assert vledger.talk_seconds(woken, t0 + timedelta(hours=5)) == 600.0
    # swarm-voice's real seconds win once reported.
    assert vledger.talk_seconds({**session, "talk_seconds": 42}, t0 + timedelta(seconds=900)) == 42.0


def test_a_paused_session_reads_no_talk_while_paused(client, db, voice):
    call = _start(client, voice)
    session = _session(db, call)
    session["live_at"] = datetime.now(timezone.utc) - timedelta(seconds=120)
    assert _report(client, call, "paused").status_code == 200

    later = datetime.now(timezone.utc) + timedelta(minutes=30)

    assert vledger.talk_seconds(_session(db, call), later) < 125.0


# ---- the reopen ------------------------------------------------------------------------


def test_a_reopen_forwards_the_offer_cause_brief_and_open_cards(client, db, voice):
    call = _start(client, voice)
    db.docs[f"{vs.PROJECTS}/{call.project_id}"]["brief"] = {"goal": "ship the pause"}
    _card(db, call, "proposed", "Plan the pause")
    _card(db, call, "done", "Finished thing")
    _card(db, call, "waiting", "Another session", session_id="vs_" + secrets.token_hex(16))
    assert _report(client, call, "paused").status_code == 200

    response = _reopen(client, call, cause="result")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_sdp"] == ANSWER and body["voice_state"] == vs.VOICE_OPEN
    assert body["cause"] == "result" and body["session_id"] == call.session_id
    (sent,) = voice.reopens
    assert sent["offer"] == OFFER_2 and sent["cause"] == "result"
    assert sent["brief"] == {"goal": "ship the pause"}
    assert len(sent["cards"]) == 1 and "Plan the pause" in sent["cards"][0]
    session = _session(db, call)
    assert session["voice_state"] == vs.VOICE_OPEN and session["paused_at"] is None
    assert len(session["pauses"]) == 1 and session["work_running"] is False


def test_a_reopen_of_an_ended_session_is_409(client, db, voice):
    call = _start(client, voice)
    assert client.post(f"/v1/voice/sessions/{call.session_id}/end",
                       headers=auth_header("alice")).status_code == 200

    response = _reopen(client, call)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert voice.reopens == []


def test_another_persons_reopen_is_refused(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200

    response = _reopen(client, call, user="bob")

    assert response.status_code == 404, response.text
    assert voice.reopens == []
    assert _session(db, call)["voice_state"] == vs.VOICE_PAUSED


def test_a_reopen_names_a_cause_and_an_offer_and_nothing_else(client, db, voice):
    call = _start(client, voice)
    url = f"/v1/voice/sessions/{call.session_id}/reopen"

    unknown = client.post(url, json={"offer_sdp": OFFER_2, "cause": "tier"}, headers=auth_header("alice"))
    extra = client.post(url, json={"offer_sdp": OFFER_2, "cause": "resume", "image": "x"},
                        headers=auth_header("alice"))
    no_offer = client.post(url, json={"cause": "resume"}, headers=auth_header("alice"))

    assert unknown.status_code == 422 and extra.status_code == 422 and no_offer.status_code == 422
    assert voice.reopens == []


def test_a_failed_reopen_keeps_the_session_paused(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    voice.reopen_refusal = vs.VoiceUnreachable("swarm-voice could not reopen the voice.")

    response = _reopen(client, call)

    assert response.status_code == 502, response.text
    session = _session(db, call)
    assert session["state"] == vs.LIVE and session["voice_state"] == vs.VOICE_PAUSED
    assert voice.ends == []


# ---- seen ------------------------------------------------------------------------------


def test_seen_seq_moves_only_forward(client, db, voice):
    call = _start(client, voice)
    _session(db, call)["event_seq"] = 12

    assert _seen(client, call, 7).json()["seen_seq"] == 7
    lower = _seen(client, call, 3)
    assert lower.status_code == 200 and lower.json() == {"session_id": call.session_id, "seen_seq": 7,
                                                         "changed": False}
    assert _session(db, call)["seen_seq"] == 7
    # Never past what the stream has written.
    assert _seen(client, call, 99).json()["seen_seq"] == 12
    assert _seen(client, call, -1).status_code == 422


def test_another_persons_seen_is_refused(client, db, voice):
    call = _start(client, voice)
    _session(db, call)["event_seq"] = 5

    response = _seen(client, call, 4, user="bob")

    assert response.status_code == 404, response.text
    assert "seen_seq" not in _session(db, call)


# ---- paused_idle -----------------------------------------------------------------------


def _sweep(db, voice, at: datetime) -> list[str]:
    return vs.VoiceSessions(db, voice, now=lambda: at).sweep_abandoned()


def test_sixty_minutes_paused_with_nothing_running_ends_it_as_paused_idle(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    assert _report(client, call, "waking", "result").status_code == 200
    later = datetime.now(timezone.utc) + timedelta(seconds=vs.PAUSED_IDLE_SECONDS + 5)
    # The tab is open (a fresh heartbeat), so the abandonment rule does not apply.
    _session(db, call)["heartbeat_at"] = later - timedelta(seconds=30)

    assert _sweep(db, voice, later - timedelta(seconds=120)) == []
    ended = _sweep(db, voice, later)

    assert ended == [call.session_id]
    session = _session(db, call)
    assert session["state"] == vs.ENDED and session["end_cause"] == vs.END_PAUSED_IDLE
    assert session["grant_hash"] is None
    assert (call.session_id, vs.END_PAUSED_IDLE) in voice.ends
    # The project and its cards are kept.
    assert f"{vs.PROJECTS}/{call.project_id}" in db.docs


def test_paused_time_while_work_runs_never_ends_it(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    later = datetime.now(timezone.utc) + timedelta(hours=4)
    _session(db, call)["heartbeat_at"] = later - timedelta(seconds=30)

    assert _sweep(db, voice, later) == []
    assert _session(db, call)["state"] == vs.LIVE and voice.ends == []


def test_activity_from_the_owner_restarts_the_paused_idle_clock(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    assert _report(client, call, "waking", "result").status_code == 200
    later = datetime.now(timezone.utc) + timedelta(seconds=vs.PAUSED_IDLE_SECONDS + 5)
    session = _session(db, call)
    session["heartbeat_at"] = later - timedelta(seconds=30)
    session["seen_at"] = later - timedelta(minutes=10)

    assert _sweep(db, voice, later) == []
    assert _session(db, call)["state"] == vs.LIVE


def test_the_grant_never_reopens_the_voice(client, db, voice):
    call = _start(client, voice)

    response = client.post(f"/v1/voice/sessions/{call.session_id}/reopen",
                           json={"offer_sdp": OFFER_2, "cause": "resume"}, headers=_grant_headers(call))

    assert response.status_code == 403, response.text
    assert voice.reopens == []


# ---- swarm-voice's internal reopen over HTTP --------------------------------------------


class _Posting(vs.HttpVoiceClient):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__("http://voice.internal:8080", token=lambda _aud, _t: "id-" + secrets.token_hex(4))
        self.answer = (status, body)
        self.sent: list[tuple[str, dict]] = []

    def _post(self, path, body, timeout):
        self.sent.append((path, body))
        return self.answer


def test_the_http_reopen_sends_quoted_data_and_reads_swarm_voices_answers():
    sid = "vs_" + secrets.token_hex(16)
    ok = _Posting(200, ('{"voice_session_id": "%s", "mode": "live", "live_session_id": "l1", '
                        '"answer": "%s", "cause": "speech"}' % (sid, ANSWER.replace("\r\n", "\\r\\n"))).encode())

    answer = ok.reopen_voice(sid, offer=OFFER_2, cause="speech", brief={"goal": "g"}, cards=["plan (proposed): x"])

    assert answer.answer == ANSWER and answer.live_session_id == "l1"
    (path, body), = ok.sent
    assert path == f"/internal/sessions/{sid}/reopen"
    assert body == {"offer": OFFER_2, "cause": "speech", "cards": ["plan (proposed): x"], "brief": {"goal": "g"}}
    with pytest.raises(vs.VoiceSessionNotLive):
        _Posting(404, b"").reopen_voice(sid, offer=OFFER_2, cause="resume", brief=None, cards=[])
    with pytest.raises(vs.VoiceSessionLive):
        _Posting(409, b'{"detail": "nothing to reopen"}').reopen_voice(sid, offer=OFFER_2, cause="resume",
                                                                     brief=None, cards=[])
    with pytest.raises(vs.VoicePoolBusy):
        _Posting(409, b'{"code": "voice_pool_busy", "message": "busy"}').reopen_voice(
            sid, offer=OFFER_2, cause="resume", brief=None, cards=[])
