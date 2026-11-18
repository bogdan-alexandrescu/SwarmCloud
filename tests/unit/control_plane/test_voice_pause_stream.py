"""A Voice it session's stream says when its voice pauses and wakes
(docs/design/voice-it.md §3.12.7; swarm_api/voice/sessions.py
`report_voice_state`, voice/stream.py's `paused` frame).

swarm-voice's report through the grant appends one `paused` event per change
of the reported state, carrying the frame's whitelisted fields (the voice
state and a known cause) and nothing else; a repeated report appends none,
and another tenant's session is untouched. swarm-voice is a fake; Firestore
is the in-memory fake; no network, no cloud credential.
"""

from __future__ import annotations

from swarm_api.voice import sessions as vs
from swarm_api.voice import stream as vstream
from swarm_api.voice import workfeed as vwork

from .test_voice_grant import _start
from .test_voice_pause import _report, _session, client, voice  # noqa: F401  (fixtures)

_STORED = ("type", "seq", "tenant_id", "person", "at")


def _events(db, call: vs.StartCall) -> list[dict]:
    prefix = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}/events/"
    return [row for path, row in sorted(db.docs.items()) if path.startswith(prefix)]


def _paused(db, call: vs.StartCall) -> list[dict]:
    return [e for e in _events(db, call) if e.get("type") == vwork.PAUSED]


def test_a_pause_appends_one_paused_event(client, db, voice):
    call = _start(client, voice)

    assert _report(client, call, "paused").status_code == 200

    (event,) = _paused(db, call)
    assert event["voice_state"] == "paused" and "cause" not in event
    assert event["seq"] <= _session(db, call)["event_seq"]
    assert event["tenant_id"] == call.tenant_id
    assert vstream.work_payload(event) == {"voice_state": "paused", "cause": None}


def test_a_wake_appends_one_with_waking_and_its_cause(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200

    assert _report(client, call, "waking", "result").status_code == 200

    pause, wake = _paused(db, call)
    assert (wake["voice_state"], wake["cause"]) == ("waking", "result")
    assert wake["seq"] > pause["seq"]
    assert vstream.work_payload(wake) == {"voice_state": "waking", "cause": "result"}
    assert _report(client, call, "listening").status_code == 200
    assert [e["voice_state"] for e in _paused(db, call)] == ["paused", "waking", "listening"]


def test_a_repeated_report_appends_none(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200

    assert _report(client, call, "paused").status_code == 200
    assert _report(client, call, "waking", "result").status_code == 200
    assert _report(client, call, "waking", "result").status_code == 200

    assert [e["voice_state"] for e in _paused(db, call)] == ["paused", "waking"]
    # Listening while the voice is already open is no change either.
    other = _start(client, voice, user="bob")
    assert _report(client, other, "listening").status_code == 200
    assert _paused(db, other) == []


def test_the_event_carries_nothing_outside_the_whitelist(client, db, voice):
    call = _start(client, voice)
    assert _report(client, call, "paused").status_code == 200
    # A cause swarm-voice names that is not one the frame knows is left out,
    # so the frame never skips the row.
    assert _report(client, call, "waking", "timer").status_code == 200
    assert _report(client, call, "listening", "speech").status_code == 200

    for event in _paused(db, call):
        assert set(event) - set(_STORED) <= {"voice_state", "cause"}, event
        assert vstream.work_payload(event) is not None
    assert [e.get("cause") for e in _paused(db, call)] == [None, None, "speech"]


def test_another_tenants_session_is_untouched(client, db, voice):
    mine = _start(client, voice)
    theirs = _start(client, voice, user="bob")
    assert theirs.tenant_id != mine.tenant_id
    before = dict(_session(db, theirs))

    assert _report(client, mine, "paused").status_code == 200
    assert _report(client, mine, "waking", "result").status_code == 200

    assert _paused(db, theirs) == [] and _session(db, theirs) == before
    assert len(_paused(db, mine)) == 2
