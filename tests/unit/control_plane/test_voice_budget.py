"""Voice it's spend caps, enforced (docs/design/voice-it.md §3.8; the owner's
decision of 23 November 2026; swarm_api/voice/budget.py).

Crossing 80% of a cap warns once, with a Budget card; reaching a cap refuses
new work at once and starts a two-minute grace; when the grace runs out the
session ends with the cause `budget_cap` through the same end as End, whether
swarm-voice asks again or only the sweep comes by. A start on a day already
at its cap is refused before anything is written. The clock is injected,
Firestore is the in-memory fake, swarm-voice a fake client: no network, no
cloud credential.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import modelprices
from swarm_api.voice import budget as vb
from swarm_api.voice import ledger as vl
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_grant import _build, _grant_headers, _start
from .test_voice_ledger import FakeWork
from .test_voice_sessions import OFFER, FakeVoice

ALICE = "alice@example.com"
TENANT = "eng"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
#: gpt-live-1 is $0.05 a minute: seconds for a talk line of `usd`.
PER_DOLLAR = 1200


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Rig:
    """One live session of Alice's with a talk line we set, a ledger and a
    guard on the same injected clock, and the person's End behind the guard."""

    def __init__(self, db) -> None:
        self.db = db
        self.clock = Clock()
        self.voice = FakeVoice()
        self.project = "vp_" + secrets.token_hex(16)
        self.session = "vs_" + secrets.token_hex(16)
        db.collection(vs.PROJECTS).document(self.project).set({
            "tenant_id": TENANT, "person": ALICE, "title": "t", "status": "active",
            "created_at": NOW, "updated_at": NOW})
        self.ref.set({"tenant_id": TENANT, "person": ALICE, "project_id": self.project,
                      "state": vs.LIVE, "mode": vs.MODE_TEXT, "created_at": NOW,
                      "live_at": NOW, "talk_seconds": 0})
        db.collection(vs.LIVE_MARKERS).document(vs.marker_id("", ALICE)).set({
            "tenant_id": TENANT, "project_id": self.project, "session_id": self.session,
            "at": NOW})
        self.sessions = vs.VoiceSessions(db, self.voice, now=self.clock.now)
        self.ledger = vl.VoiceLedger(db, now=self.clock.now, prices=lambda: modelprices.SEED,
                                     time_zone=lambda email: None, work=FakeWork())
        self.guard = vb.BudgetGuard(db, self.ledger, now=self.clock.now,
                                    end=self.sessions.end_for_budget)

    @property
    def ref(self):
        return (self.db.collection(vs.PROJECTS).document(self.project)
                .collection(vs.SESSIONS).document(self.session))

    def spend(self, usd: float) -> None:
        self.ref.update({"talk_seconds": usd * PER_DOLLAR})

    def doc(self) -> dict:
        return self.ref.get().to_dict()

    def enforce(self) -> dict:
        return self.guard.enforce(tenant_id=TENANT, person=ALICE, project_id=self.project,
                                  session_id=self.session)

    def admit(self) -> dict:
        return self.guard.admit_work(tenant_id=TENANT, person=ALICE, project_id=self.project,
                                     session_id=self.session)

    def cards(self) -> list[dict]:
        prefix = f"{vs.PROJECTS}/{self.project}/{vstore.CARDS}/"
        return [row for path, row in self.db.docs.items() if path.startswith(prefix)]


@pytest.fixture
def rig(db) -> Rig:
    return Rig(db)


# ---- the constants --------------------------------------------------------------


def test_the_caps_the_warning_and_the_grace_are_as_designed():
    assert (vl.SESSION_CAP_USD, vl.DAY_CAP_USD) == (100.0, 300.0)
    assert vl.WARN_SHARE == 0.8
    assert vl.CAP_GRACE_SECONDS == 120


# ---- 80% --------------------------------------------------------------------------


def test_below_80_percent_nothing_is_said_or_drawn(rig):
    rig.spend(79.0)
    state = rig.enforce()
    assert state["notices"] == [] and state["warned"] == [] and state["capped"] is None
    assert rig.cards() == []


def test_crossing_80_percent_of_the_session_cap_warns_once_with_a_budget_card(rig):
    rig.spend(81.0)

    first = rig.enforce()

    assert first["notices"] == [{"notice": vb.WARNING, "line": vl.SESSION}]
    assert first["warned"] == [vl.SESSION] and first["capped"] is None
    [card] = rig.cards()
    assert card["kind"] == "budget" and card["body"]["created_by"] == "platform"
    body = card["body"]["body"]
    assert body["status"] == "warning" and body["cap"] == "session" and body["ends_at"] is None
    assert body["spent_usd"] == pytest.approx(81.0) and body["session_cap_usd"] == 100.0

    # Once per session per cap: the next checks say nothing and draw nothing new.
    rig.clock.advance(30)
    rig.spend(85.0)
    again = rig.enforce()
    assert again["notices"] == [] and again["warned"] == [vl.SESSION]
    assert len(rig.cards()) == 1 and rig.cards()[0]["version"] == 1


def test_crossing_80_percent_of_the_day_cap_warns_once_for_the_day(rig, db):
    # Another session of today's, ended, carries $230: the day is at $240.
    other = "vs_" + secrets.token_hex(16)
    db.collection(vs.PROJECTS).document(rig.project).collection(vs.SESSIONS).document(other).set({
        "tenant_id": TENANT, "person": ALICE, "project_id": rig.project, "state": vs.ENDED,
        "created_at": NOW, "live_at": NOW, "talk_seconds": 230 * PER_DOLLAR})
    rig.spend(10.0)

    state = rig.enforce()

    assert state["notices"] == [{"notice": vb.WARNING, "line": vl.DAY}]
    assert rig.cards()[0]["body"]["body"]["cap"] == "day"
    assert rig.enforce()["notices"] == []


# ---- the cap, the grace and the end --------------------------------------------------


def test_reaching_the_cap_refuses_new_work_at_once_and_starts_a_two_minute_grace(rig):
    rig.spend(100.0)

    state = rig.enforce()

    assert {"notice": vb.CAPPED, "line": vl.SESSION} in state["notices"]
    assert state["capped"] == vl.SESSION
    assert state["ends_at"] == (NOW + timedelta(seconds=vl.CAP_GRACE_SECONDS)).isoformat()
    assert state["ended"] is False and rig.doc()["state"] == vs.LIVE
    body = rig.cards()[-1]["body"]["body"]
    assert body["status"] == "capped" and body["ends_at"] == state["ends_at"]

    with pytest.raises(vl.VoiceCapReached) as refused:
        rig.admit()
    assert refused.value.detail["reason"] == vl.SESSION_CAPPED
    assert refused.value.status_code == 409


def test_a_cap_is_entered_once_even_when_both_are_reached(rig, db):
    other = "vs_" + secrets.token_hex(16)
    db.collection(vs.PROJECTS).document(rig.project).collection(vs.SESSIONS).document(other).set({
        "tenant_id": TENANT, "person": ALICE, "project_id": rig.project, "state": vs.ENDED,
        "created_at": NOW, "live_at": NOW, "talk_seconds": 200 * PER_DOLLAR})
    rig.spend(100.0)

    state = rig.enforce()

    assert [n for n in state["notices"] if n["notice"] == vb.CAPPED] == [
        {"notice": vb.CAPPED, "line": vl.SESSION}]
    rig.clock.advance(60)
    assert rig.enforce()["notices"] == []
    assert rig.enforce()["ends_at"] == state["ends_at"]


def test_the_grace_expiring_ends_the_session_with_the_cause_budget_cap(rig):
    rig.spend(100.0)
    rig.enforce()
    rig.clock.advance(vl.CAP_GRACE_SECONDS - 1)
    assert rig.enforce()["ended"] is False

    rig.clock.advance(1)
    state = rig.enforce()

    assert state["ended"] is True
    doc = rig.doc()
    assert doc["state"] == vs.ENDED and doc["end_cause"] == vs.END_BUDGET_CAP == "budget_cap"
    assert doc["grant_hash"] is None
    # The person's End path: swarm-voice told (billing stops), the marker freed.
    assert rig.voice.ends == [(rig.session, vs.END_BUDGET_CAP)]
    assert not [p for p in rig.db.docs if p.startswith(vs.LIVE_MARKERS + "/")]


def test_the_sweep_ends_the_session_when_swarm_voice_never_calls_back(rig):
    rig.spend(100.0)
    # The sweep alone notices the cap, starts the grace and, two minutes on, ends it.
    assert rig.guard.sweep() == []
    assert rig.doc()["budget"]["capped"]["line"] == vl.SESSION
    rig.clock.advance(vl.CAP_GRACE_SECONDS)

    assert rig.guard.sweep() == [rig.session]

    assert rig.doc()["end_cause"] == vs.END_BUDGET_CAP
    assert rig.voice.ends == [(rig.session, vs.END_BUDGET_CAP)]
    assert rig.guard.sweep() == []


def test_the_speech_machine_lease_ends_with_end_budget_cap(rig):
    from swarm_api.voice import speech_machine as sm

    stopped: list[str] = []

    class Machine:
        def holder_of(self, session_id):
            return sm.Holder(session_id=session_id, generation=1)

        def stop(self, holder, *, cause):
            stopped.append(cause)
            return True

    rig.ref.update({"mode": vs.MODE_SPEECH})
    rig.sessions = vs.VoiceSessions(rig.db, rig.voice, now=rig.clock.now, machine=Machine())
    rig.guard = vb.BudgetGuard(rig.db, rig.ledger, now=rig.clock.now,
                               end=rig.sessions.end_for_budget)
    rig.spend(100.0)
    rig.enforce()
    rig.clock.advance(vl.CAP_GRACE_SECONDS)
    rig.enforce()

    assert stopped == [sm.END_BUDGET_CAP]


# ---- delegated work ---------------------------------------------------------------


def test_work_below_the_cap_is_admitted_and_reserves_nothing(rig):
    rig.spend(10.0)
    rig.admit()
    assert not [p for p in rig.db.docs if p.startswith(vstore.STARTS + "/")]


def test_work_that_would_pass_the_session_cap_is_refused_naming_it(rig):
    rig.spend(99.5)  # one more planner turn, held at $1.20, would pass $100
    with pytest.raises(vl.VoiceCapReached) as refused:
        rig.admit()
    assert refused.value.detail["reason"] == vl.SESSION_CAP_REACHED


def test_a_reservation_counts_toward_the_cap(rig, db):
    # A subscription planner turn is reserved, never spent: it still reaches the cap.
    db.collection(vs.PROJECTS).document(rig.project).collection(vstore.TURNS).document(
        "vt_" + "3" * 32).set({
            "tenant_id": TENANT, "person": ALICE, "session_id": rig.session, "created_at": NOW,
            "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000,
                      "billing": "subscription"}})  # $6 reserved
    rig.spend(94.0)

    state = rig.enforce()

    assert state["capped"] == vl.SESSION
    assert rig.cards()[-1]["body"]["body"]["reserved_usd"] == pytest.approx(6.0)


# ---- another tenant ------------------------------------------------------------------


def test_another_tenants_session_is_not_found(rig):
    with pytest.raises(vl.NotFound):
        rig.guard.enforce(tenant_id="research", person=ALICE, project_id=rig.project,
                          session_id=rig.session)


# ---- the routes ------------------------------------------------------------------------


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice):
    return _build(db, tokens, group_map, objects, voice)


def _spent_today(db, usd: float, *, tenant: str = TENANT) -> None:
    project = "vp_" + secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    db.collection(vs.PROJECTS).document(project).set({
        "tenant_id": tenant, "person": ALICE, "title": "t", "status": "active",
        "created_at": now, "updated_at": now})
    db.collection(vs.PROJECTS).document(project).collection(vs.SESSIONS).document(
        "vs_" + secrets.token_hex(16)).set({
            "tenant_id": tenant, "person": ALICE, "project_id": project, "state": vs.ENDED,
            "created_at": now, "live_at": now, "talk_seconds": usd * PER_DOLLAR})


def test_a_start_at_the_day_cap_is_refused_before_any_write(client, db, voice):
    _spent_today(db, 300.0)
    before = dict(db.docs)

    response = client.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                           headers=auth_header("alice"))

    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["code"] == "conflict"
    detail = payload["detail"]
    assert detail["reason"] == vl.DAY_CAP_REACHED
    assert detail["cap_usd"] == 300.0 and detail["spent_usd"] == pytest.approx(300.0)
    assert detail["reserved_usd"] == 0.0
    assert "$300" in payload["message"]
    # No session, no marker, no lease, no call to swarm-voice: nothing written.
    assert voice.starts == []
    assert db.docs == before


def test_a_start_below_the_day_cap_goes_ahead(client, db, voice):
    _spent_today(db, 299.0)
    response = client.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text


def test_another_tenants_spend_does_not_count_toward_the_day(client, db, voice):
    _spent_today(db, 300.0, tenant="research")
    response = client.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text


def _session_ref(db, call):
    return (db.collection(vs.PROJECTS).document(call.project_id)
            .collection(vs.SESSIONS).document(call.session_id))


def _delegation_events(db, call) -> list[dict]:
    prefix = f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}/{vstore.EVENTS}/"
    return [row for path, row in db.docs.items()
            if path.startswith(prefix) and row.get("type") == "work.delegation"]


def test_work_starting_at_the_cap_is_refused_and_the_voice_is_told_why(client, db, voice):
    call = _start(client, voice)
    _session_ref(db, call).update({"talk_seconds": 100 * PER_DOLLAR})

    response = client.post(f"/v1/voice/sessions/{call.session_id}/work",
                           json={"delegation": {"delegation_id": "vd_" + "a" * 24,
                                                "state": "started"}},
                           headers=_grant_headers(call))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == vl.SESSION_CAPPED
    assert _delegation_events(db, call) == []


def test_work_below_the_cap_is_recorded_with_the_budget_state(client, db, voice):
    call = _start(client, voice)
    _session_ref(db, call).update({"talk_seconds": 85 * PER_DOLLAR})

    response = client.post(f"/v1/voice/sessions/{call.session_id}/work",
                           json={"delegation": {"delegation_id": "vd_" + "b" * 24,
                                                "state": "started"}},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    budget = response.json()["budget"]
    assert budget["notices"] == [{"notice": vb.WARNING, "line": vl.SESSION}]
    assert len(_delegation_events(db, call)) == 1


def test_the_followers_budget_question_answers_the_state_and_writes_no_event(client, db, voice):
    call = _start(client, voice)
    _session_ref(db, call).update({"talk_seconds": 100 * PER_DOLLAR})

    response = client.post(f"/v1/voice/sessions/{call.session_id}/work", json={"budget": True},
                           headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    budget = response.json()["budget"]
    assert budget["capped"] == vl.SESSION and budget["ends_at"] is not None
    assert _delegation_events(db, call) == []
    # The person may not ask it through the grant's route.
    assert client.post(f"/v1/voice/sessions/{call.session_id}/work", json={"budget": True},
                       headers=auth_header("alice")).status_code == 403


def test_the_ledger_read_shows_the_enforcement_state(db, tokens, group_map, objects, voice):
    # The ledger's GET reads the saved time zone: the preferences need their key.
    client = _build(db, tokens, group_map, objects, voice, child_key=secrets.token_hex(32))
    call = _start(client, voice)
    _session_ref(db, call).update({"talk_seconds": 100 * PER_DOLLAR})
    client.post(f"/v1/voice/sessions/{call.session_id}/work", json={"budget": True},
                headers=_grant_headers(call))

    view = client.get(f"/v1/voice/sessions/{call.session_id}/ledger",
                      headers=auth_header("alice")).json()

    assert view["budget"]["capped"] == "session"
    assert view["budget"]["warned"] == ["session"]
    assert view["budget"]["ends_at"] is not None
