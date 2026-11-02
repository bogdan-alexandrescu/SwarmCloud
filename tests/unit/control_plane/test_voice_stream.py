"""The Voice it card stream (docs/design/voice-it.md §3.5, §10 phase 2).

With a fake clock: the stream sends a session's events in sequence order,
resumes after a given sequence (`?after=` or `Last-Event-ID`), and ends itself
before 600 seconds, under swarm-api's 660-second request timeout. The tenant
comes from the session document, never from the request: another tenant's
session, another person's, and a missing one read the same 404. Firestore is
the in-memory fake, swarm-voice the session tests' fake; no network.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.errors import NotFound
from swarm_api.voice import cards as vcards
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore
from swarm_api.voice import stream as vstream

from .conftest import ENG_GROUP, RESEARCH_GROUP, auth_header
from .test_voice_sessions import FakeVoice, _client, _end, _start


class FakeClock:
    """Monotonic seconds that move only when the stream sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept += 1
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(db, tokens, group_map, objects, clock) -> TestClient:
    c = _client(db, tokens, group_map, objects, FakeVoice(),
                voice_url="http://voice.internal:8080",
                voice_people=("alice@example.com", "bob@example.com"),
                child_key=secrets.token_hex(32))
    c.app.state.voice_stream_timing = {"clock": clock, "sleep": clock.sleep}
    return c


def _store(db) -> vstore.VoiceStore:
    return vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc))


def _emit(db, started: dict, tenant: str, person: str, *events: dict) -> list[int]:
    return [_store(db).append_event(tenant_id=tenant, person=person,
                                    project_id=started["project_id"],
                                    session_id=started["session_id"], event=e)
            for e in events]


def _mark_ended(db, started: dict) -> None:
    path = f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"
    db.docs[path] = {**db.docs[path], "state": vs.ENDED}


def _states(*names: str) -> list[dict]:
    return [{"type": vstream.SESSION_STATE, "state": n} for n in names]


def _frames(text: str) -> list[dict]:
    """The stream's frames as {id, event, data}; comments and `retry:` skipped."""
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if line and not line.startswith(":") and not line.startswith("retry:"))
        if fields:
            out.append({"id": int(fields["id"]), "event": fields["event"],
                        "data": json.loads(fields["data"])})
    return out


def _collect(stream: vstream.CardStream) -> str:
    async def run() -> str:
        return "".join([part async for part in stream])
    return asyncio.run(run())


def _stream(db, started, *, after: int, clock: FakeClock, tenant: str = "eng",
            person: str = "alice@example.com") -> vstream.CardStream:
    found = vstream.find_session(db, person=person, session_id=started["session_id"],
                                 tenants={tenant})
    return vstream.CardStream(_store(db), db, found=found, person=person, after=after,
                              clock=clock, sleep=clock.sleep)


# ---- resume and the clock -----------------------------------------------------


def test_the_stream_resumes_after_a_given_sequence_in_order(client, db, clock):
    started = _start(client, "alice").json()
    _emit(db, started, "eng", "alice@example.com", *_states("listening", "thinking", "speaking"))
    assert _end(client, "alice", started["session_id"]).status_code == 200

    frames = _frames(_collect(_stream(db, started, after=1, clock=clock)))

    assert [f["id"] for f in frames] == [2, 3]
    assert [f["data"]["state"] for f in frames] == ["thinking", "speaking"]
    # A frame never carries the tenant or the person.
    assert all("tenant_id" not in f["data"] and "person" not in f["data"] for f in frames)


def test_a_live_stream_closes_itself_before_600_seconds(client, db, clock):
    started = _start(client, "alice").json()  # live, and never ended here
    _emit(db, started, "eng", "alice@example.com", *_states("listening"))
    began = clock()

    stream = _stream(db, started, after=0, clock=clock)
    text = _collect(stream)

    assert stream.ended_by == "time"
    assert clock() - began < 600
    assert clock() - began >= vstream.MAX_SECONDS - vstream.POLL_SECONDS
    assert vstream.MAX_SECONDS + vstream.POLL_SECONDS < 600
    assert [f["id"] for f in _frames(text)] == [1]
    assert text.startswith(f"retry: {vstream.RETRY_MS}\n\n")
    assert ": keep-alive" in text
    assert stream.after == 1


def test_events_written_while_the_stream_is_open_arrive_after_what_was_there(client, db, clock):
    started = _start(client, "alice").json()
    _emit(db, started, "eng", "alice@example.com", *_states("listening"))
    stream = _stream(db, started, after=0, clock=clock)
    original = clock.sleep

    async def sleep_and_write(seconds: float) -> None:
        await original(seconds)
        if clock.slept == 3:
            _emit(db, started, "eng", "alice@example.com", *_states("thinking"))
        if clock.slept == 5:
            _mark_ended(db, started)

    stream._sleep = sleep_and_write
    frames = _frames(_collect(stream))

    assert [f["id"] for f in frames] == [1, 2]
    assert stream.ended_by == "session_ended"


def test_a_card_upsert_carries_the_card_and_an_unknown_kind_is_not_sent(client, db, clock):
    started = _start(client, "alice").json()
    kw = {"tenant_id": "eng", "person": "alice@example.com", "project_id": started["project_id"]}
    card = vcards.write_card(_store(db), **kw, session_id=started["session_id"], card={
        "kind": "stop", "state": "waiting", "title": "Held", "created_by": "platform",
        "references": {name: None for name in vcards.REFERENCES},
        "summary": "An IAM change waits for you.",
        "body": {"what": "a grant", "rule": "iam", "where": "https://console.example/iam"}})
    # A row no writer could produce through `write_card`: skipped, not sent.
    _emit(db, started, "eng", "alice@example.com",
          {"type": "card.upsert", "card": {"kind": "banner"}}, {"type": "debug"})
    assert _end(client, "alice", started["session_id"]).status_code == 200

    frames = _frames(_collect(_stream(db, started, after=0, clock=clock)))

    assert [(f["id"], f["event"]) for f in frames] == [(1, "card.upsert")]
    assert frames[0]["data"]["card"] == card


# ---- the route ----------------------------------------------------------------


def test_the_route_streams_then_resumes_from_last_event_id_then_says_204(client, db):
    started = _start(client, "alice").json()
    _emit(db, started, "eng", "alice@example.com", *_states("listening", "thinking"))
    assert _end(client, "alice", started["session_id"]).status_code == 200
    url = f"/v1/voice/sessions/{started['session_id']}/events"

    first = client.get(url, headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    assert first.headers["content-type"].startswith("text/event-stream")
    assert [f["id"] for f in _frames(first.text)] == [1, 2]

    after = client.get(url + "?after=1", headers=auth_header("alice"))
    assert [f["id"] for f in _frames(after.text)] == [2]

    resumed = client.get(url, headers={**auth_header("alice"), "Last-Event-ID": "1"})
    assert [f["id"] for f in _frames(resumed.text)] == [2]

    done = client.get(url, headers={**auth_header("alice"), "Last-Event-ID": "2"})
    assert done.status_code == 204


def test_a_live_session_streams_through_the_route_and_ends_before_600_seconds(client, db, clock):
    started = _start(client, "alice").json()
    _emit(db, started, "eng", "alice@example.com", *_states("listening"))
    began = clock()

    response = client.get(f"/v1/voice/sessions/{started['session_id']}/events",
                          headers=auth_header("alice"))

    assert response.status_code == 200
    assert [f["id"] for f in _frames(response.text)] == [1]
    assert clock() - began < 600


def test_another_tenants_session_reads_404_like_a_missing_one(client, db):
    theirs = _start(client, "bob").json()  # research
    missing = "vs_" + secrets.token_hex(16)

    response = client.get(f"/v1/voice/sessions/{theirs['session_id']}/events",
                          headers=auth_header("alice"))
    absent = client.get(f"/v1/voice/sessions/{missing}/events", headers=auth_header("alice"))

    assert response.status_code == absent.status_code == 404
    assert response.json()["code"] == absent.json()["code"]
    assert response.json()["message"].replace(theirs["session_id"], missing) == \
        absent.json()["message"]


def test_a_session_whose_tenant_the_caller_is_not_in_reads_404(client, db):
    # alice's own session, but in a tenant she does not belong to on this
    # request: the session's tenant is checked, not trusted.
    started = _start(client, "alice").json()
    for path in (f"{vs.PROJECTS}/{started['project_id']}",
                 f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"):
        db.docs[path] = {**db.docs[path], "tenant_id": "research"}

    response = client.get(f"/v1/voice/sessions/{started['session_id']}/events",
                          headers=auth_header("alice"))

    assert response.status_code == 404
    with pytest.raises(NotFound):
        vstream.find_session(db, person="alice@example.com",
                             session_id=started["session_id"], tenants={"eng"})


def test_the_tenant_comes_from_the_session_not_the_request(db, tokens, objects, clock):
    # alice belongs to both eng and research; her request selects eng (the
    # default), her session is in research, and the stream reads research's.
    groups = {"alice@example.com": (ENG_GROUP, RESEARCH_GROUP), "bob@example.com": ()}
    c = _client(db, tokens, groups, objects, FakeVoice(),
                voice_url="http://voice.internal:8080",
                voice_people=("alice@example.com",), child_key=secrets.token_hex(32))
    c.app.state.voice_stream_timing = {"clock": clock, "sleep": clock.sleep}
    started = _start(c, "alice").json()
    tenant = db.docs[f"{vs.PROJECTS}/{started['project_id']}"]["tenant_id"]
    other = "research" if tenant == "eng" else "eng"
    for path in (f"{vs.PROJECTS}/{started['project_id']}",
                 f"{vs.PROJECTS}/{started['project_id']}/{vs.SESSIONS}/{started['session_id']}"):
        db.docs[path] = {**db.docs[path], "tenant_id": other}
    _emit(db, started, other, "alice@example.com", *_states("listening"))
    _mark_ended(db, started)

    response = c.get(f"/v1/voice/sessions/{started['session_id']}/events?tenant={tenant}",
                     headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    assert [f["id"] for f in _frames(response.text)] == [1]
    found = vstream.find_session(db, person="alice@example.com",
                                 session_id=started["session_id"], tenants={"eng", "research"})
    assert found.tenant_id == other


def test_the_stream_is_403_for_anyone_not_on_the_list(client):
    started = _start(client, "alice").json()
    response = client.get(f"/v1/voice/sessions/{started['session_id']}/events",
                          headers=auth_header("carol"))
    assert response.status_code == 403


def test_a_malformed_last_event_id_or_session_id_is_422(client):
    started = _start(client, "alice").json()
    url = f"/v1/voice/sessions/{started['session_id']}/events"
    assert client.get(url, headers={**auth_header("alice"), "Last-Event-ID": "x1"}).status_code == 422
    assert client.get("/v1/voice/sessions/not-a-session/events",
                      headers=auth_header("alice")).status_code == 422
