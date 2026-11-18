"""A Voice it session left `live` without activity stops holding its person
(swarm_api/voice/sessions.py, LIVE_STALE_SECONDS).

Its tab went away without the end call: swarm-voice stopped its side on its
own idle stop, but the session stayed `live` and every later Start was 409.
Now a `live` session whose newest recorded activity is older than the bound
no longer holds: the next start ends it in its own transaction (cause
`abandoned`) and tells swarm-voice, and the speech sweep ends such sessions
of every mode. A session live in another workspace is refused in words
alone. Firestore is the in-memory fake, swarm-voice a fake client, the clock
a fake: no network, no cloud credential.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_sessions_engine import ALICE, CHOSEN, TENANT, Rig  # noqa: E402
from test_speech_machine import CONFIG  # noqa: E402
from test_speech_routes import SWEEP  # noqa: E402
from test_speech_routes import Rig as RouteRig  # noqa: E402

from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import speech_catalogue as cat  # noqa: E402
from swarm_api.voice import speech_machine as sm  # noqa: E402
from swarm_api.voice import stream as vstream  # noqa: E402

OTHER_TENANT = "ops"


def _start(rig: Rig, *, tenant: str = TENANT, person: str = ALICE) -> dict:
    return rig.sessions(machine=None).start(tenant_id=tenant, person=person, offer="", project_id=None,
                                            mode=vs.MODE_TEXT, chosen=CHOSEN)


def _marker(rig: Rig, person: str = ALICE) -> dict:
    return rig.db.docs[f"{vs.LIVE_MARKERS}/{vs.marker_id('', person)}"]


# ---- the bound -------------------------------------------------------------------


def test_the_bound_is_never_below_the_longest_idle_stop_a_person_can_pick():
    longest = max(idle.seconds for idle in cat.IDLE)
    assert vs.LIVE_STALE_SECONDS > longest
    assert vs.LIVE_STALE_SECONDS >= longest + vs.LIVE_STALE_MARGIN_SECONDS
    # An open Talk tab beats on each stream connect: the bound outlasts the gap.
    assert vs.LIVE_STALE_SECONDS > vstream.MAX_SECONDS


# ---- the start -------------------------------------------------------------------


def test_a_stale_live_session_no_longer_holds_and_is_ended_with_its_cause():
    rig = Rig()
    first = _start(rig)
    rig.clock.advance(vs.LIVE_STALE_SECONDS + 1)

    second = _start(rig)

    old = rig.session(first)
    assert old["state"] == vs.ENDED and old["end_cause"] == vs.END_ABANDONED
    assert old["ended_at"] == rig.clock.now() and old["grant_hash"] is None
    assert rig.session(second)["state"] == vs.LIVE
    assert _marker(rig)["session_id"] == second["session_id"]
    assert (first["session_id"], vs.END_ABANDONED) in rig.voice.ends
    ended = [row for key, row in rig.db.docs.items()
             if key.startswith(vs.AUDIT + "/") and row["action"] == vs.AUDIT_ENDED]
    assert [row["session_id"] for row in ended] == [first["session_id"]]
    assert ended[0]["detail"] == {"cause": vs.END_ABANDONED}


def test_a_stale_live_session_in_another_workspace_is_ended_too():
    rig = Rig()
    first = _start(rig, tenant=OTHER_TENANT)
    rig.clock.advance(vs.LIVE_STALE_SECONDS + 1)

    _start(rig)

    assert rig.session(first)["end_cause"] == vs.END_ABANDONED


def test_a_recent_live_session_still_holds():
    rig = Rig()
    first = _start(rig)
    rig.clock.advance(vs.LIVE_STALE_SECONDS - 60)

    with pytest.raises(vs.VoiceSessionLive) as refused:
        _start(rig)

    assert refused.value.message == vs.LIVE_HERE
    assert refused.value.detail == {"project_id": first["project_id"], "session_id": first["session_id"]}
    assert rig.session(first)["state"] == vs.LIVE
    assert rig.voice.ends == []


def test_a_turn_is_activity_so_a_long_conversation_keeps_holding():
    rig = Rig()
    first = _start(rig)
    rig.clock.advance(vs.LIVE_STALE_SECONDS - 60)
    rig.sessions(machine=None).turn(tenant_id=TENANT, person=ALICE, session_id=first["session_id"],
                                    text="next step")
    rig.clock.advance(120)

    with pytest.raises(vs.VoiceSessionLive):
        _start(rig)
    assert rig.session(first)["state"] == vs.LIVE


def test_an_open_tabs_heartbeat_keeps_the_session_holding():
    rig = Rig()
    first = _start(rig)
    rig.clock.advance(vs.LIVE_STALE_SECONDS - 60)
    vs.record_heartbeat(rig.db, project_id=first["project_id"], session_id=first["session_id"],
                        now=rig.clock.now())
    rig.clock.advance(120)

    with pytest.raises(vs.VoiceSessionLive):
        _start(rig)


def test_a_session_live_in_another_workspace_is_refused_in_words_with_no_id():
    rig = Rig()
    first = _start(rig, tenant=OTHER_TENANT)

    with pytest.raises(vs.VoiceSessionLive) as refused:
        _start(rig)

    payload = refused.value.to_payload()
    text = repr(payload)
    assert "another workspace" in refused.value.message
    assert f"after {vs.LIVE_STALE_SECONDS // 60} minutes without activity" in refused.value.message
    assert refused.value.detail == {}
    for named in (first["session_id"], first["project_id"], OTHER_TENANT):
        assert named not in text


# ---- the sweep -------------------------------------------------------------------


def _seed(rig: Any, *, person: str, mode: str, session_id: str, at: Any) -> str:
    project_id = "vp_" + session_id[3:]
    rig.db.docs[f"{vs.PROJECTS}/{project_id}"] = {"tenant_id": TENANT, "person": person}
    rig.db.docs[f"{vs.PROJECTS}/{project_id}/{vs.SESSIONS}/{session_id}"] = {
        "tenant_id": TENANT, "person": person, "project_id": project_id, "state": vs.LIVE,
        "mode": mode, "created_at": at, "live_at": at, "grant_hash": "h"}
    rig.db.docs[f"{vs.LIVE_MARKERS}/{vs.marker_id('', person)}"] = {
        "tenant_id": TENANT, "project_id": project_id, "session_id": session_id, "at": at}
    return f"{vs.PROJECTS}/{project_id}/{vs.SESSIONS}/{session_id}"


@pytest.mark.parametrize("engine_on", [False, True], ids=["engine-off", "engine-on"])
def test_the_sweep_ends_stale_live_sessions_of_every_mode(engine_on):
    rig = RouteRig(config=sm.SpeechConfig(**{**(CONFIG.__dict__ if engine_on else {}), **SWEEP}))
    long_ago = rig.clock.now()
    stale = {mode: _seed(rig, person=f"p{i}@example.com", mode=mode,
                         session_id=f"vs_{i:032x}", at=long_ago)
             for i, mode in enumerate(vs.MODES, start=1)}
    rig.clock.advance(vs.LIVE_STALE_SECONDS + 1)
    fresh = _seed(rig, person="fresh@example.com", mode=vs.MODE_TEXT,
                  session_id="vs_" + "f" * 32, at=rig.clock.now())

    reply = rig.sweep(rig.verifier.token)

    assert reply.status_code == 200, reply.text
    assert reply.json()["abandoned"] == len(vs.MODES)
    for path in stale.values():
        assert rig.db.docs[path]["state"] == vs.ENDED
        assert rig.db.docs[path]["end_cause"] == vs.END_ABANDONED
    assert rig.db.docs[fresh]["state"] == vs.LIVE
    markers = [key for key in rig.db.docs if key.startswith(vs.LIVE_MARKERS + "/")]
    assert markers == [f"{vs.LIVE_MARKERS}/{vs.marker_id('', 'fresh@example.com')}"]
    assert sorted(rig.voice.ended) == sorted(
        (path.rsplit("/", 1)[1], vs.END_ABANDONED) for path in stale.values())
