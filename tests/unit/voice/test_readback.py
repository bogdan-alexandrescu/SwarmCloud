"""The read-back gate holds a spoken cancel, merge, deploy, delete or approve
until a "go" (docs/design/voice-it-own-speech.md §7; swarm_api/voice/readback.py,
swarm_api/voice/grant.py, swarm_api/routes/voice_readback.py,
swarm_voice/readback.py, swarm_voice/live.py).

swarm-api is built as test_voice_grant builds it: the in-memory Firestore, a
fake swarm-voice, swarm-voice's identity as a static token. The read-back
router is mounted beside the app's own, as routes/voice.py will mount it, and
a gated test route declares the gate the way an act route does. No network,
no cloud credential; every token and grant is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane.conftest import auth_header, db, group_map, objects, tokens  # noqa: E402,F401
from control_plane.fakes import FakeFirestore  # noqa: E402
from control_plane.test_voice_grant import (  # noqa: E402
    _authenticated,
    _build,
    _calls,
    _grant_headers,
    _start,
)
from control_plane.test_voice_sessions import FakeVoice  # noqa: E402

from swarm_api.routes import voice_readback as rvr  # noqa: E402
from swarm_api.voice import cards as vcards  # noqa: E402
from swarm_api.voice import grant as vg  # noqa: E402
from swarm_api.voice import readback as rb  # noqa: E402
from swarm_voice import live  # noqa: E402
from swarm_voice import readback as svr  # noqa: E402
from swarm_voice.grant import SessionGrant  # noqa: E402

GATED = "/v1/test-gated/{thing_id}/cancel"
ANSWER = "/v1/voice/sessions/{session_id}/readbacks/answer"


# ---- §7.1: every act route is classed ---------------------------------------


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:  # noqa: F811
    return _build(db, tokens, group_map, objects, voice)


def test_the_verbs_are_the_five_the_design_names():
    assert rb.READBACK_VERBS == ("cancel", "merge", "deploy", "delete", "approve")
    assert set(rb.GATED_ROUTES.values()) <= set(rb.READBACK_VERBS)
    assert set(rb.GATED_ROUTES) == set(rb.TARGETS)


def test_every_act_route_on_the_grants_lists_is_classed():
    acts = [(m, p) for m, p in sorted(vg.ALLOWED_ROUTES | vg.WRITE_ROUTES) if m != "GET"]
    assert acts, "the grant reaches no act route, so nothing was checked"
    unclassed = [f"{m} {p}" for m, p in acts if rb.classify(m, p) is None]
    assert unclassed == [], "an act route a grant reaches has no read-back class"
    for method, path in acts:
        if rb.looks_gated(method, path):
            assert rb.classify(method, path) in rb.READBACK_VERBS, f"{method} {path} is not gated"


def test_an_unclassed_act_route_on_the_grants_lists_fails_the_check(monkeypatch):
    # Guards the guard: a new act route added to the grant without a class.
    monkeypatch.setattr(vg, "WRITE_ROUTES", vg.WRITE_ROUTES | {("POST", "/v1/tasks")})
    acts = [(m, p) for m, p in vg.ALLOWED_ROUTES | vg.WRITE_ROUTES if m != "GET"]
    assert [p for m, p in acts if rb.classify(m, p) is None] == ["/v1/tasks"]


def test_every_gated_route_is_one_of_swarm_apis_routes(client):
    table = {(m, p) for m, p, _r in _authenticated(client.app)}
    assert len(table) > 100
    missing = sorted(f"{m} {p}" for m, p in rb.GATED_ROUTES if (m, p) not in table)
    assert missing == []


def test_every_gated_route_a_grant_reaches_declares_the_gate(client):
    reached = vg.ALLOWED_ROUTES | vg.WRITE_ROUTES
    for method, path, route in _authenticated(client.app):
        if (method, path) in reached and rb.classify(method, path) in rb.READBACK_VERBS:
            assert any(c is rvr.readback_gate for c in _calls(route.dependant)), (
                f"{method} {path} is gated but does not declare readback_gate")


# ---- §7.3: how "go" is recognised -------------------------------------------


@pytest.mark.parametrize("said", ["go", "Go.", "go ahead", "Yes, go!", "do it", "confirm",
                                  "yes do it", "Um, go ahead.", "okay go", "uh... do it"])
def test_go_phrases_are_go(said):
    assert rb.verdict(said) == rb.GO


@pytest.mark.parametrize("said", ["not that", "No.", "stop", "cancel", "Don't!", "don’t",
                                  "wait", "um, wait"])
def test_refuse_phrases_refuse(said):
    assert rb.verdict(said) == rb.REFUSE


@pytest.mark.parametrize("said", ["yes", "Yes.", "yeah", "yes please", "go to the dashboard",
                                  "cancel the other one", "ok", "", "   "])
def test_a_bare_yes_and_any_other_sentence_are_not_answers(said):
    assert rb.verdict(said) is None


# ---- the store: hold, answer, expire, supersede, consume --------------------


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def _where() -> rb.Where:
    return rb.Where(tenant_id="t-acme", person="alice@example.com",
                    project_id="vp_" + secrets.token_hex(16), session_id="vs_" + secrets.token_hex(16))


def _store(fake: FakeFirestore, clock: Clock) -> tuple[rb.Readbacks, list[dict[str, Any]]]:
    drawn: list[dict[str, Any]] = []

    def draw(where: rb.Where, card: dict[str, Any]) -> dict[str, Any]:
        checked = vcards.validate(card)
        drawn.append(checked)
        return {**checked, "card_id": checked["card_id"] or "vc_" + secrets.token_hex(16)}

    return rb.Readbacks(fake, now=clock, cards=draw), drawn


def _hold(store: rb.Readbacks, where: rb.Where, body: bytes = b'{"why":"duplicate"}') -> dict:
    return store.hold(where, action="cancel", target="agent nightly (t_1)", method="POST",
                      route="/v1/tasks/{task_id}/cancel", body=body)


def test_a_hold_is_pending_with_what_was_heard_and_draws_a_waiting_card():
    fake, clock = FakeFirestore(), Clock()
    store, drawn = _store(fake, clock)
    where = _where()
    store.heard(where, "cancel the nightly agent")
    held = _hold(store, where)
    assert held["state"] == rb.PENDING and held["id"].startswith("vr_")
    assert held["heard"] == "cancel the nightly agent"
    assert held["expires_at"] == clock.at + timedelta(seconds=rb.ASK_SECONDS + rb.EXPIRE_SECONDS)
    assert drawn[-1]["kind"] == "readback" and drawn[-1]["state"] == vcards.WAITING
    assert drawn[-1]["body"]["target_name"] == "agent nightly (t_1)"
    assert drawn[-1]["summary"] == "Will cancel agent nightly (t_1). Go?"
    assert store.pending(where)["id"] == held["id"]


def test_what_was_heard_is_redacted_before_it_is_stored():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    secret = "ghp_" + secrets.token_hex(18)
    store.heard(where, f"cancel it, my token is {secret}")
    assert secret not in json.dumps(_hold(store, where), default=str)


def test_go_confirms_and_the_matching_call_is_used_once():
    fake, clock = FakeFirestore(), Clock()
    store, drawn = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    answered = store.answer(where, "go ahead")
    assert answered["answer"] == rb.GO and answered["to_planner"] is False
    assert answered["readback"]["state"] == rb.CONFIRMED
    used = store.consume(where, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                         body=b'{"why":"duplicate"}')
    assert used["state"] == rb.USED and drawn[-1]["state"] == vcards.DONE
    with pytest.raises(rb.ReadbackRefused):
        store.consume(where, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                      body=b'{"why":"duplicate"}')


def test_a_body_hash_mismatch_is_refused_and_leaves_it_confirmed():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    store.answer(where, "go")
    with pytest.raises(rb.ReadbackRefused):
        store.consume(where, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                      body=b'{"why":"something else"}')
    with pytest.raises(rb.ReadbackRefused):
        store.consume(where, held["id"], method="POST", route="/v1/workflows/{workflow_id}/cancel",
                      body=b'{"why":"duplicate"}')
    assert store.get(where, held["id"])["state"] == rb.CONFIRMED


def test_an_unconfirmed_or_foreign_read_back_runs_nothing():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    with pytest.raises(rb.ReadbackRefused):  # still pending
        store.consume(where, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                      body=b'{"why":"duplicate"}')
    store.answer(where, "go")
    other = rb.Where(tenant_id=where.tenant_id, person=where.person,
                     project_id=where.project_id, session_id="vs_" + secrets.token_hex(16))
    with pytest.raises(rb.ReadbackRefused):  # another session
        store.consume(other, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                      body=b'{"why":"duplicate"}')


def test_not_that_refuses_and_nothing_runs():
    fake, clock = FakeFirestore(), Clock()
    store, drawn = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    answered = store.answer(where, "Not that.")
    assert answered["answer"] == rb.REFUSE and answered["readback"]["state"] == rb.REFUSED
    assert drawn[-1]["state"] == vcards.DISMISSED
    with pytest.raises(rb.ReadbackRefused):
        store.consume(where, held["id"], method="POST", route="/v1/tasks/{task_id}/cancel",
                      body=b'{"why":"duplicate"}')


def test_a_bare_yes_leaves_it_pending_and_goes_to_the_planner():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    answered = store.answer(where, "yes")
    assert answered == {"answer": None, "readback": None, "to_planner": True}
    assert store.pending(where)["id"] == held["id"]


def test_silence_expires_it_and_a_late_go_confirms_nothing():
    fake, clock = FakeFirestore(), Clock()
    store, drawn = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    clock.tick(rb.ASK_SECONDS + rb.EXPIRE_SECONDS - 1)
    assert store.pending(where)["id"] == held["id"]  # still activity: the idle clock waits
    clock.tick(1)
    assert store.pending(where) is None
    assert store.get(where, held["id"])["state"] == rb.EXPIRED
    assert drawn[-1]["state"] == vcards.CANCELLED and drawn[-1]["body"]["status"] == rb.EXPIRED
    late = store.answer(where, "go")
    assert late["answer"] is None and late["to_planner"] is True
    with pytest.raises(rb.ReadbackNotPending):
        store.decide(where, held["id"], go=True)


def test_a_go_after_the_deadline_expires_it_rather_than_confirming():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    clock.tick(rb.ASK_SECONDS + rb.EXPIRE_SECONDS + 5)
    answered = store.answer(where, "go")  # no sweep ran in between
    assert answered["answer"] is None and answered["readback"]["state"] == rb.EXPIRED
    assert store.get(where, held["id"])["state"] == rb.EXPIRED


def test_a_newer_gated_call_supersedes_the_older():
    fake, clock = FakeFirestore(), Clock()
    store, drawn = _store(fake, clock)
    where = _where()
    older = _hold(store, where)
    newer = _hold(store, where, body=b'{"why":"other"}')
    assert store.get(where, older["id"])["state"] == rb.SUPERSEDED
    assert [d["body"]["status"] for d in drawn[-2:]] == [rb.SUPERSEDED, rb.PENDING]
    store.answer(where, "go")
    assert store.get(where, newer["id"])["state"] == rb.CONFIRMED
    assert store.get(where, older["id"])["state"] == rb.SUPERSEDED


def test_a_tap_decides_only_a_pending_read_back():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    assert store.decide(where, held["id"], go=True)["state"] == rb.CONFIRMED
    with pytest.raises(rb.ReadbackNotPending):
        store.decide(where, held["id"], go=False)
    with pytest.raises(Exception):
        store.decide(where, "vr_" + secrets.token_hex(16), go=True)


def test_a_lost_held_call_expires_the_confirmed_read_back():
    fake, clock = FakeFirestore(), Clock()
    store, _ = _store(fake, clock)
    where = _where()
    held = _hold(store, where)
    store.answer(where, "go")
    assert store.lost(where, held["id"])["state"] == rb.EXPIRED


def test_the_target_name_comes_from_the_tenants_document_never_the_body():
    fake = FakeFirestore()
    fake.collection("tasks").document("t_1").set({"tenant_id": "t-acme", "title": "nightly"})
    fake.collection("tasks").document("t_2").set({"tenant_id": "t-other", "title": "theirs"})
    name = rb.target_name(fake, tenant_id="t-acme", method="POST",
                          route="/v1/tasks/{task_id}/cancel", path_params={"task_id": "t_1"})
    assert name == "agent nightly (t_1)"
    foreign = rb.target_name(fake, tenant_id="t-acme", method="POST",
                             route="/v1/tasks/{task_id}/cancel", path_params={"task_id": "t_2"})
    assert foreign == "agent t_2"


def test_the_readback_and_spec_kinds_are_in_the_catalogue():
    assert vcards.BY_KIND["readback"].created_by == ("platform",)
    assert vcards.BY_KIND["readback"].actions == ("go", "not_that")
    assert "draw_readback" not in {t["name"] for t in vcards.tool_schemas()}
    assert "draw_spec" in {t["name"] for t in vcards.tool_schemas()}
    assert set(rb.CARD_STATE.values()) <= set(vcards.BY_KIND["readback"].states)


# ---- the gate through swarm-api ---------------------------------------------


class ReadbackVoice(FakeVoice):
    """The fake swarm-voice, also told when a tap confirms a read-back."""

    def __init__(self) -> None:
        super().__init__()
        self.readbacks: list[tuple[str, str]] = []
        self.holding = True

    def readback(self, session_id: str, readback_id: str) -> bool:
        self.readbacks.append((session_id, readback_id))
        return self.holding


@pytest.fixture
def gated(db, tokens, group_map, objects, monkeypatch):  # noqa: F811
    monkeypatch.setitem(rb.GATED_ROUTES, ("POST", GATED), "cancel")
    monkeypatch.setitem(rb.TARGETS, ("POST", GATED), ("thing", "thing_id", None))
    monkeypatch.setattr(vg, "WRITE_ROUTES", vg.WRITE_ROUTES | {("POST", GATED)})
    voice = ReadbackVoice()
    client = _build(db, tokens, group_map, objects, voice)
    app: FastAPI = client.app
    app.include_router(rvr.router)
    ran: list[dict[str, Any]] = []

    @app.post(GATED, dependencies=[Depends(rvr.readback_gate)])
    async def cancel_thing(thing_id: str, body: dict) -> dict:
        ran.append({"thing_id": thing_id, **body})
        return {"cancelled": thing_id}

    return client, voice, ran


def _readbacks(db) -> list[dict[str, Any]]:  # noqa: F811
    return [row for path, row in db.docs.items() if f"/{rb.READBACKS}/" in path]


def test_a_gated_call_under_a_grant_is_held_and_does_not_run(gated, db):  # noqa: F811
    client, voice, ran = gated
    call = _start(client, voice)
    headers = _grant_headers(call)
    r = client.post(ANSWER.format(session_id=call.session_id),
                    json={"text": "cancel thing seven"}, headers=headers)
    assert r.status_code == 200 and r.json()["to_planner"] is True

    r = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["code"] == "readback_pending"
    rid = r.json()["detail"]["readback_id"]
    assert ran == []
    [doc] = _readbacks(db)
    assert doc["id"] == rid and doc["state"] == rb.PENDING
    assert doc["heard"] == "cancel thing seven" and doc["target_name"] == "thing seven"
    cards = [row for path, row in db.docs.items()
             if "/cards/" in path and row.get("kind") == "readback"]
    assert len(cards) == 1 and cards[0]["body"]["body"]["readback_id"] == rid


def test_a_spoken_go_lets_the_resent_call_run_once(gated, db):  # noqa: F811
    client, voice, ran = gated
    call = _start(client, voice)
    headers = _grant_headers(call)
    rid = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"},
                      headers=headers).json()["detail"]["readback_id"]

    r = client.post(ANSWER.format(session_id=call.session_id), json={"text": "yes"}, headers=headers)
    assert r.json() == {"answer": None, "readback": None, "to_planner": True}
    r = client.post(ANSWER.format(session_id=call.session_id), json={"text": "Go."}, headers=headers)
    assert r.json()["answer"] == "go" and r.json()["readback"]["state"] == rb.CONFIRMED
    assert voice.readbacks == []  # swarm-voice asked, so swarm-voice re-sends

    resend = {**headers, rb.READBACK_HEADER: rid}
    mismatched = client.post("/v1/test-gated/seven/cancel", json={"why": "other"}, headers=resend)
    assert mismatched.status_code == 409 and ran == []
    r = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"}, headers=resend)
    assert r.status_code == 200, r.text
    assert ran == [{"thing_id": "seven", "why": "dup"}]
    again = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"}, headers=resend)
    assert again.status_code == 409 and len(ran) == 1


def test_a_persons_click_on_a_gated_route_is_not_gated(gated):
    client, _voice, ran = gated
    r = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"}, headers=auth_header("alice"))
    assert r.status_code == 200 and len(ran) == 1


def test_a_tap_on_go_tells_swarm_voice_and_not_that_refuses(gated, db):  # noqa: F811
    client, voice, ran = gated
    call = _start(client, voice)
    headers = _grant_headers(call)
    path = "/v1/voice/sessions/{s}/readbacks/{r}/{a}"
    rid = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"},
                      headers=headers).json()["detail"]["readback_id"]
    pending = client.get(f"/v1/voice/sessions/{call.session_id}/readbacks/pending",
                         headers=auth_header("alice"))
    assert pending.json()["readback"]["readback_id"] == rid
    r = client.post(path.format(s=call.session_id, r=rid, a="go"), headers=auth_header("alice"))
    assert r.status_code == 200, r.text
    assert r.json()["ran"] is True and voice.readbacks == [(call.session_id, rid)]
    r = client.post(path.format(s=call.session_id, r=rid, a="not-that"), headers=auth_header("alice"))
    assert r.status_code == 409

    rid2 = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"},
                       headers=headers).json()["detail"]["readback_id"]
    r = client.post(path.format(s=call.session_id, r=rid2, a="not-that"), headers=auth_header("alice"))
    assert r.json()["readback"]["state"] == rb.REFUSED
    resent = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"},
                         headers={**headers, rb.READBACK_HEADER: rid2})
    assert resent.status_code == 409 and ran == []


def test_a_go_swarm_voice_no_longer_holds_expires_and_runs_nothing(gated, db):  # noqa: F811
    client, voice, ran = gated
    voice.holding = False
    call = _start(client, voice)
    rid = client.post("/v1/test-gated/seven/cancel", json={"why": "dup"},
                      headers=_grant_headers(call)).json()["detail"]["readback_id"]
    r = client.post(f"/v1/voice/sessions/{call.session_id}/readbacks/{rid}/go",
                    headers=auth_header("alice"))
    assert r.json()["ran"] is False and r.json()["readback"]["state"] == rb.EXPIRED
    assert ran == []


def test_a_grant_answers_only_for_its_own_session(gated):
    client, voice, _ran = gated
    call = _start(client, voice)
    other = "vs_" + secrets.token_hex(16)
    r = client.post(ANSWER.format(session_id=other), json={"text": "go"},
                    headers=_grant_headers(call))
    assert r.status_code == 403


# ---- swarm-voice: holds and finals ------------------------------------------


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16),
                        session_id="vs_" + secrets.token_hex(16), value=secrets.token_urlsafe(32))


def test_a_202_names_its_read_back_and_anything_else_does_not():
    rid = "vr_" + secrets.token_hex(16)
    assert svr.held_id(202, {"code": "readback_pending", "detail": {"readback_id": rid}}) == rid
    assert svr.held_id(200, {"code": "readback_pending", "detail": {"readback_id": rid}}) is None
    assert svr.held_id(202, {"code": "other", "detail": {"readback_id": rid}}) is None
    assert svr.held_id(202, {"code": "readback_pending", "detail": {"readback_id": "x"}}) is None


def test_a_held_call_is_resent_once_with_its_id_and_a_newer_one_replaces_it():
    sent: list[dict[str, str]] = []

    async def send(headers: dict[str, str]) -> str:
        sent.append(headers)
        return "ran"

    async def scenario() -> None:
        holds = svr.Holds()
        first, second = "vr_" + secrets.token_hex(16), "vr_" + secrets.token_hex(16)
        holds.hold("vs_a", first, send)
        holds.hold("vs_a", second, send)
        with pytest.raises(svr.NotHeld):
            await holds.run("vs_a", first)
        assert await holds.run("vs_a", second) == "ran"
        assert sent == [{svr.READBACK_HEADER: second}]
        with pytest.raises(svr.NotHeld):
            await holds.run("vs_a", second)
        holds.hold("vs_b", first, send)
        holds.forget("vs_b")
        with pytest.raises(svr.NotHeld):
            await holds.run("vs_b", first)

    asyncio.run(scenario())


def test_a_held_call_past_its_time_is_dropped():
    now = [0.0]
    holds = svr.Holds(clock=lambda: now[0])
    rid = "vr_" + secrets.token_hex(16)

    async def send(headers: dict[str, str]) -> None:
        raise AssertionError("an expired hold must not be sent")

    holds.hold("vs_a", rid, send)
    now[0] = svr.HOLD_SECONDS + 1
    assert holds.holds("vs_a", rid) is False


def _finals(handler) -> tuple[svr.Finals, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def token() -> str:
        return "id-" + secrets.token_hex(4)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return svr.Finals(http, api_base="http://api.internal", id_tokens=token, holds=svr.Holds()), seen


def test_a_final_is_posted_under_the_grant_and_a_go_resends_the_held_call():
    grant = _grant()
    rid = "vr_" + secrets.token_hex(16)
    finals, seen = _finals(lambda r: httpx.Response(
        200, json={"answer": "go", "readback": {"readback_id": rid}, "to_planner": False}))
    resent: list[dict[str, str]] = []

    async def send(headers: dict[str, str]) -> None:
        resent.append(headers)

    async def scenario() -> None:
        finals.holds.hold(grant.session_id, rid, send)
        body = await finals.post(grant, "  go ahead  ")
        assert body["answer"] == "go"

    asyncio.run(scenario())
    [request] = seen
    assert request.url.path == ANSWER.format(session_id=grant.session_id)
    assert json.loads(request.content) == {"text": "go ahead"}
    assert request.headers["X-Swarm-Voice-Grant"] == grant.headers()["X-Swarm-Voice-Grant"]
    assert resent == [{svr.READBACK_HEADER: rid}]


def test_a_final_that_is_not_an_answer_resends_nothing():
    grant = _grant()
    finals, _seen = _finals(lambda r: httpx.Response(
        200, json={"answer": None, "readback": None, "to_planner": True}))

    async def send(headers: dict[str, str]) -> None:
        raise AssertionError("nothing was confirmed")

    async def scenario() -> None:
        finals.holds.hold(grant.session_id, "vr_" + secrets.token_hex(16), send)
        assert (await finals.post(grant, "yes"))["to_planner"] is True

    asyncio.run(scenario())


def test_the_sideband_posts_the_persons_finals_and_not_the_voices():
    posted: list[str] = []

    class Recorder:
        holds = svr.Holds()

        async def post(self, grant: SessionGrant, text: str) -> None:
            posted.append(text)

    class Socket:
        def __init__(self, events: list[dict]) -> None:
            self._events = [json.dumps(e) for e in events]

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        session = live.LiveSession("vs_" + secrets.token_hex(16), "live-1", None, None,  # type: ignore[arg-type]
                                   on_event=None, on_closed=lambda s: None, finals=Recorder())  # type: ignore[arg-type]
        session.grant = _grant()
        await session._read(Socket([
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "go"},
            {"type": "response.output_audio_transcript.done", "transcript": "Will cancel it. Go?"},
        ]))
        await asyncio.gather(*list(session._turns))

    asyncio.run(scenario())
    assert posted == ["go"]


def test_sessions_build_finals_from_a_real_planner_and_share_their_holds():
    class PlannerLike:
        def __init__(self) -> None:
            self._http = httpx.AsyncClient()
            self._api_base = "http://api.internal"

        async def _id_tokens(self) -> str:
            return "id"

        def forget(self, session_id: str) -> None:
            return None

    sessions = live.Sessions(None, leases=None, planner=PlannerLike())  # type: ignore[arg-type]
    assert sessions._finals is not None and sessions._finals.holds is sessions.holds
    assert live.Sessions(None, leases=None)._finals is None  # type: ignore[arg-type]
