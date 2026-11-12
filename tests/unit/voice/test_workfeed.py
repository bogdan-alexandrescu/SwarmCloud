"""Voice it's work feed (docs/design/voice-it.md §3.12.5, §3.12.6, §3.12.8).

Every granted call writes one masked `work.step` on its session's stream: the
route's phrase from a fixed table, the method, the class and the path's ids,
never a query, a body or any text. swarm-voice writes a delegation's
lifecycle through the same grant, in order, for its own session only. The
stream frames the four work events through their whitelists. Firestore is
the in-memory fake, swarm-voice the session tests' fake; no network.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.conftest import auth_header, db, group_map, objects, tokens  # noqa: E402,F401
from control_plane.test_voice_grant import _build, _grant_headers, _start  # noqa: E402
from control_plane.test_voice_sessions import FakeVoice  # noqa: E402

from swarm_api.errors import ValidationFailed  # noqa: E402
from swarm_api.voice import grant as vg  # noqa: E402
from swarm_api.voice import sessions as vs  # noqa: E402
from swarm_api.voice import store as vstore  # noqa: E402
from swarm_api.voice import stream as vstream  # noqa: E402
from swarm_api.voice import workfeed as wf  # noqa: E402

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice)


def _events(db, call: vs.StartCall) -> list[dict[str, Any]]:
    prefix = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}/{vstore.EVENTS}/"
    rows = [row for path, row in db.docs.items() if path.startswith(prefix)]
    return sorted(rows, key=lambda r: r["seq"])


def _frames(db, call: vs.StartCall) -> list[str]:
    framed = [vstream.frame(e, session_id=call.session_id) for e in _events(db, call)]
    return [f for f in framed if f is not None]


def _data(text: str) -> dict[str, Any]:
    return json.loads(text.split("data: ", 1)[1])


def _grant(db, call: vs.StartCall) -> vg.GrantSession:
    return vg.resolve(db, session_header=f"{call.project_id}/{call.session_id}", grant=call.grant)


def _report(db, call: vs.StartCall, body: Any, *, session_id: str | None = None) -> dict:
    return vg.record_work(db, _grant(db, call), session_id=session_id or call.session_id,
                          body=body, at=NOW)


# ---- the phrase table -------------------------------------------------------


def _unphrased(routes) -> list[str]:
    return sorted(f"{m} {p}" for m, p in routes
                  if (m, p) != wf.WORK_ROUTE and (m, p) not in wf.PHRASES)


def test_every_granted_route_has_its_own_phrase():
    granted = vg.ALLOWED_ROUTES | vg.WRITE_ROUTES
    assert len(granted) > 20, "the grant's lists are too short to prove anything"
    assert _unphrased(granted) == []
    for (method, route), phrase in wf.PHRASES.items():
        assert phrase != wf.FALLBACK
        # A placeholder is one of the route's own path parameters, nothing else.
        assert set(re.findall(r"\{(\w+)\}", phrase)) <= set(wf.params_of(route)), route
    # The table names no route the grant does not reach.
    assert set(wf.PHRASES) <= granted


def test_a_route_added_to_the_grant_without_a_phrase_fails_the_check(monkeypatch):
    # Guards the guard: the walk above sees a new route on the grant's lists.
    monkeypatch.setattr(vg, "WRITE_ROUTES", vg.WRITE_ROUTES | {("POST", "/v1/tasks")})
    assert _unphrased(vg.ALLOWED_ROUTES | vg.WRITE_ROUTES) == ["POST /v1/tasks"]
    assert wf.phrase_for("POST", "/v1/tasks") == wf.FALLBACK


# ---- what a step carries ----------------------------------------------------


def test_a_step_carries_the_phrase_method_class_and_ids_only():
    step = wf.step_event(method="get", route="/v1/tasks/{task_id}/events",
                         path_params={"task_id": "task_0123", "extra": "not-in-template"},
                         write=False, delegation_id="call_abc")
    assert step == {"type": wf.WORK_STEP, "delegation_id": "call_abc",
                    "phrase": "Reading agent {task_id}'s events", "method": "GET",
                    "class": "read", "ids": {"task_id": "task_0123"}}


def test_a_path_value_that_is_not_an_id_is_dropped_not_sent():
    step = wf.step_event(method="GET", route="/v1/repositories/{repo_id}",
                         path_params={"repo_id": "a b?c=d"}, write=False, delegation_id=None)
    assert step["ids"] == {}
    assert step["delegation_id"] is None


def test_a_granted_call_writes_one_masked_step_and_the_query_never_reaches_a_frame(
        client, db, voice):
    call = _start(client, voice)
    headers = _grant_headers(call)
    key = secrets.token_hex(24)
    assert client.get(f"/v1/tasks?q={key}&status=running",
                      headers=headers).status_code == 200
    steps = [e for e in _events(db, call) if e["type"] == wf.WORK_STEP]
    assert len(steps) == 1
    assert set(steps[0]) == {"type", "delegation_id", "phrase", "method", "class", "ids",
                             "seq", "tenant_id", "person", "at"}
    framed = _frames(db, call)
    assert len(framed) == 1
    assert _data(framed[0])["phrase"] == "Reading running agents"
    assert _data(framed[0])["class"] == "read"
    assert key not in json.dumps(_events(db, call), default=str)
    assert key not in "".join(framed)
    assert "tenant_id" not in _data(framed[0]) and "person" not in _data(framed[0])


def test_a_granted_write_step_carries_no_body(client, db, voice):
    call = _start(client, voice)
    key = secrets.token_hex(24)
    client.post(f"/v1/voice/sessions/{call.session_id}/cards",
                json={"card": {"kind": "work", "title": key}}, headers=_grant_headers(call))
    steps = [e for e in _events(db, call) if e["type"] == wf.WORK_STEP]
    assert [s["phrase"] for s in steps] == ["Drawing a card"]
    assert steps[0]["class"] == "write"
    assert steps[0]["ids"] == {"session_id": call.session_id}
    assert key not in json.dumps(steps, default=str)


def test_a_refused_call_writes_no_step(client, db, voice):
    call = _start(client, voice)
    assert client.get("/v1/tasks", headers=_grant_headers(
        call, grant=secrets.token_urlsafe(32))).status_code == 401
    assert client.post("/v1/tasks", json={}, headers=_grant_headers(call)).status_code == 403
    assert [e for e in _events(db, call) if e["type"] == wf.WORK_STEP] == []


# ---- the frame whitelists ---------------------------------------------------


def _stored(kind: str, seq: int = 1, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "seq": seq, "at": NOW, "tenant_id": "t", "person": "p", **fields}


def test_a_field_a_writer_added_to_a_step_is_never_framed():
    key = secrets.token_hex(24)
    text = vstream.frame(_stored(wf.WORK_STEP, phrase="Reading running agents", method="GET",
                                 ids={}, delegation_id=None, query=key, body=key,
                                 **{"class": "read"}), session_id="vs_x")
    assert text is not None and key not in text
    assert set(_data(text)) == {"type", "seq", "at", "delegation_id", "phrase", "method",
                                "class", "ids"}


def test_a_step_with_free_text_for_a_phrase_or_an_id_is_skipped():
    key = secrets.token_hex(24)
    assert vstream.frame(_stored(wf.WORK_STEP, phrase=f"Reading {key}", method="GET", ids={},
                                 **{"class": "read"}), session_id="vs_x") is None
    assert vstream.frame(_stored(wf.WORK_STEP, phrase=wf.FALLBACK, method="GET",
                                 ids={"task_id": "two words"}, **{"class": "read"}),
                         session_id="vs_x") is None


def test_the_four_work_events_are_on_the_stream():
    for kind in (wf.WORK_STEP, wf.WORK_DELEGATION, wf.ROSTER_UPDATE, wf.PAUSED):
        assert kind in vstream.EVENT_TYPES


def test_a_roster_update_frames_kind_id_state_and_counts_only():
    rows = [{"kind": "workflow", "id": "wf_1", "state": "running", "done": 2, "total": 5}]
    text = vstream.frame(_stored(wf.ROSTER_UPDATE, rows=rows), session_id="vs_x")
    assert _data(text)["rows"] == rows
    titled = [{**rows[0], "title": "a title"}]
    assert vstream.frame(_stored(wf.ROSTER_UPDATE, rows=titled), session_id="vs_x") is None


def test_a_pause_frames_its_voice_state_and_cause_only():
    text = vstream.frame(_stored(wf.PAUSED, voice_state="waking", cause="result", note="x"),
                         session_id="vs_x")
    assert {k: v for k, v in _data(text).items() if k not in ("seq", "at")} == {
        "type": wf.PAUSED, "voice_state": "waking", "cause": "result"}
    assert vstream.frame(_stored(wf.PAUSED, voice_state="asleep"), session_id="vs_x") is None


# ---- a delegation's lifecycle ----------------------------------------------


def test_a_delegations_lifecycle_frames_in_order(client, db, voice):
    call = _start(client, voice)
    for state in ("started", "progress", "progress", "done"):
        _report(db, call, {"delegation": {"delegation_id": "call_1", "state": state}})
    framed = [_data(f) for f in _frames(db, call)]
    assert [(f["type"], f["state"]) for f in framed] == [
        (wf.WORK_DELEGATION, "started"), (wf.WORK_DELEGATION, "progress"),
        (wf.WORK_DELEGATION, "progress"), (wf.WORK_DELEGATION, "done")]
    assert [f["seq"] for f in framed] == sorted(f["seq"] for f in framed)
    assert all(set(f) == {"type", "seq", "at", "delegation_id", "state"} for f in framed)


def test_a_state_out_of_order_is_refused_and_writes_nothing(client, db, voice):
    call = _start(client, voice)
    with pytest.raises(vg.WorkOutOfOrder):
        _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "progress"}})
    _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "started"}})
    with pytest.raises(vg.WorkOutOfOrder):
        _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "started"}})
    _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "failed"}})
    for state in ("progress", "done", "started"):
        with pytest.raises(vg.WorkOutOfOrder):
            _report(db, call, {"delegation": {"delegation_id": "call_1", "state": state}})
    assert [e["state"] for e in _events(db, call)] == ["started", "failed"]


def test_a_step_names_the_oldest_open_delegation(client, db, voice):
    call = _start(client, voice)
    headers = _grant_headers(call)
    _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "started"}})
    _report(db, call, {"delegation": {"delegation_id": "call_2", "state": "started"}})
    assert client.get("/v1/runs", headers=headers).status_code == 200
    _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "done"}})
    assert client.get("/v1/runs", headers=headers).status_code == 200
    _report(db, call, {"delegation": {"delegation_id": "call_2", "state": "done"}})
    assert client.get("/v1/runs", headers=headers).status_code == 200
    steps = [e for e in _events(db, call) if e["type"] == wf.WORK_STEP]
    assert [s["delegation_id"] for s in steps] == ["call_1", "call_2", None]


def test_a_lifecycle_report_carries_no_text(client, db, voice):
    call = _start(client, voice)
    key = secrets.token_hex(24)
    with pytest.raises(ValidationFailed):
        _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "started",
                                          "summary": key}})
    with pytest.raises(ValidationFailed):
        _report(db, call, {"delegation": {"delegation_id": f"call {key}", "state": "started"}})
    with pytest.raises(ValidationFailed):
        _report(db, call, {"roster": [{"kind": "agent", "id": "task_1", "state": "running",
                                       "title": key}]})
    with pytest.raises(ValidationFailed):
        _report(db, call, {"delegation": {"delegation_id": "call_1", "state": "started"},
                           "note": key})
    assert _events(db, call) == []


def test_a_roster_update_is_written_through_the_grant(client, db, voice):
    call = _start(client, voice)
    rows = [{"kind": "agent", "id": "task_1", "state": "waiting"},
            {"kind": "run", "id": "run_1", "state": "running", "done": 1, "total": 3}]
    _report(db, call, {"roster": rows})
    framed = [_data(f) for f in _frames(db, call)]
    assert [f["type"] for f in framed] == [wf.ROSTER_UPDATE]
    assert framed[0]["rows"] == [{**rows[0], "done": None, "total": None}, rows[1]]


# ---- another session's grant -----------------------------------------------


def test_another_sessions_grant_is_refused(client, db, voice):
    mine = _start(client, voice)
    other = _start(client, voice, user="root")
    with pytest.raises(vg.RouteNotGranted):
        _report(db, mine, {"delegation": {"delegation_id": "call_1", "state": "started"}},
                session_id=other.session_id)
    assert _events(db, other) == [] and _events(db, mine) == []


def test_an_ended_sessions_grant_writes_nothing(client, db, voice):
    call = _start(client, voice)
    grant = _grant(db, call)
    path = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}"
    db.docs[path] = {**db.docs[path], "state": vs.ENDED}
    with pytest.raises(vg.GrantRefused):
        vg.record_work(db, grant, session_id=call.session_id, at=NOW,
                       body={"delegation": {"delegation_id": "call_1", "state": "started"}})
    assert _events(db, call) == []


# ---- the work route ---------------------------------------------------------


def _work_url(session_id: str) -> str:
    return f"/v1/voice/sessions/{session_id}/work"


def test_the_work_route_is_on_the_grant_and_writes_no_step_of_its_own(client, db, voice):
    assert wf.WORK_ROUTE in vg.WRITE_ROUTES
    call = _start(client, voice)
    headers = _grant_headers(call)
    for state in ("started", "done"):
        response = client.post(_work_url(call.session_id), headers=headers,
                               json={"delegation": {"delegation_id": "call_1", "state": state}})
        assert response.status_code == 200, response.text
        assert response.json()["type"] == wf.WORK_DELEGATION
    assert [(e["type"], e.get("state")) for e in _events(db, call)] == [
        (wf.WORK_DELEGATION, "started"), (wf.WORK_DELEGATION, "done")]
    out_of_order = client.post(_work_url(call.session_id), headers=headers,
                               json={"delegation": {"delegation_id": "call_1",
                                                    "state": "progress"}})
    assert out_of_order.status_code == 409
    assert client.post(_work_url(call.session_id), headers=headers,
                       json={"note": "x"}).status_code == 422


def test_the_work_route_refuses_a_person_and_another_sessions_grant(client, db, voice):
    mine = _start(client, voice)
    other = _start(client, voice, user="root")
    body = {"delegation": {"delegation_id": "call_1", "state": "started"}}
    assert client.post(_work_url(mine.session_id), json=body,
                       headers=auth_header("alice")).status_code == 403
    assert client.post(_work_url(other.session_id), json=body,
                       headers=_grant_headers(mine)).status_code == 403
    assert [e for e in _events(db, other) if e["type"] == wf.WORK_DELEGATION] == []
    assert [e for e in _events(db, mine) if e["type"] == wf.WORK_DELEGATION] == []
