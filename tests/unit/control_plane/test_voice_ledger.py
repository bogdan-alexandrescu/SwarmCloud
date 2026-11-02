"""Voice it's ledger (docs/design/voice-it.md §3.8, §10 phase 3).

A start that would pass $100 for the session or $300 for the person's day is
refused with the line that binds; a finished step's measured cost replaces
its reservation; the day follows the saved time zone across midnight;
another tenant's session reads 404. Nothing here writes a total: every figure
is recomputed from the session, its turns and the start rows. Firestore is
the in-memory fake; the platform's work is a fake `WorkReader` except where
`FirestoreWork` itself is under test.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import modelprices
from swarm_api.errors import NotFound, ValidationFailed
from swarm_api.store import ATTEMPTS
from swarm_api.voice import ledger as vl
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_sessions import FakeVoice, _client, _start

ALICE = "alice@example.com"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class FakeWork:
    """Each started item's state by target id; tagged tasks by tag."""

    def __init__(self) -> None:
        self.items: dict[str, vl.WorkState] = {}
        self.tags: dict[str, list[vl.TaggedTask]] = {}

    def state(self, tenant_id, kind, target_id):
        return self.items.get(target_id, vl.WorkState(ended=False, cost_usd=None))

    def tagged(self, tenant_id, tag):
        return list(self.tags.get(tag, []))


def _pid() -> str:
    return "vp_" + secrets.token_hex(16)


def _sid() -> str:
    return "vs_" + secrets.token_hex(16)


def _session(db, *, tenant="eng", person=ALICE, project=None, session=None,
             created=NOW, seconds=0.0, state=vs.ENDED):
    project = project or _pid()
    session = session or _sid()
    ref = db.collection(vs.PROJECTS).document(project)
    if not ref.get().exists:
        ref.set({"tenant_id": tenant, "person": person, "title": "t", "status": "active",
                 "created_at": created, "updated_at": created})
    ref.collection(vs.SESSIONS).document(session).set({
        "tenant_id": tenant, "person": person, "project_id": project, "state": state,
        "created_at": created, "live_at": created, "talk_seconds": seconds})
    return project, session


def _start_row(db, project, session, target, *, steps, at=NOW, tenant="eng", person=ALICE,
               kind="agent"):
    db.collection(vstore.STARTS).document("vst_" + secrets.token_hex(16)).set({
        "tenant_id": tenant, "person": person, "project_id": project, "session_id": session,
        "kind": kind, "target_id": target, "day": "ignored",
        "reservation": {"steps": [{"usd": s} for s in steps]}, "at": at})


def _ledger(db, *, now=NOW, tz=None, work=None) -> vl.VoiceLedger:
    return vl.VoiceLedger(db, now=lambda: now, prices=lambda: modelprices.SEED,
                          time_zone=lambda email: tz, work=work or FakeWork())


def _line(view, name):
    return next(row for row in view["lines"] if row["line"] == name)


# ---- the per-minute price ---------------------------------------------------


def test_talk_is_priced_per_second_at_five_cents_a_minute():
    assert modelprices.per_minute_cost("gpt-live-1", 60) == pytest.approx(0.05)
    assert modelprices.per_minute_cost("gpt-live-1", 90) == pytest.approx(0.075)
    assert modelprices.per_minute_cost("gpt-live-1", -5) == 0.0
    assert modelprices.per_minute_cost("not-a-voice-model", 60) is None


# ---- the lines ----------------------------------------------------------------


def test_the_lines_are_computed_from_talk_turns_and_starts(db):
    project, session = _session(db, seconds=600)  # ten minutes: $0.50
    db.collection(vs.PROJECTS).document(project).collection(vstore.TURNS).document(
        "vt_" + "1" * 32).set({
            "tenant_id": "eng", "person": ALICE, "session_id": session, "created_at": NOW,
            # Opus 5.5: $4 in and $20 out per million.
            "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}})
    _start_row(db, project, session, "task-a", steps=[4.0, 1.5])

    view = _ledger(db).compute(tenant_id="eng", person=ALICE, session_id=session)

    s = view["session"]
    assert _line(s, "talk")["spent_usd"] == pytest.approx(0.5)
    assert _line(s, "planner")["spent_usd"] == pytest.approx(6.0)
    assert _line(s, "work_running")["reserved_usd"] == pytest.approx(5.5 * 1.2)
    assert s["total_usd"] == pytest.approx(0.5 + 6.0 + 6.6)
    assert s["cap_usd"] == 100.0 and view["day"]["cap_usd"] == 300.0
    assert view["day"]["total_usd"] == pytest.approx(s["total_usd"])
    assert "deploys" in view["not_counted"]
    # Nothing was written: the ledger keeps no total of its own.
    assert not [p for p in db.docs if "ledger" in p]


def test_a_live_sessions_talk_grows_with_the_clock(db):
    project, session = _session(db, state=vs.LIVE, seconds=None,
                                created=NOW - timedelta(minutes=20))

    view = _ledger(db).compute(tenant_id="eng", person=ALICE, session_id=session)

    assert _line(view["session"], "talk")["spent_usd"] == pytest.approx(1.0)


def test_a_finished_steps_measured_cost_replaces_its_reservation(db):
    project, session = _session(db)
    _start_row(db, project, session, "task-a", steps=[10.0])
    work = FakeWork()
    before = _ledger(db, work=work).compute(tenant_id="eng", person=ALICE, session_id=session)
    assert before["session"]["reserved_usd"] == pytest.approx(12.0)

    work.items["task-a"] = vl.WorkState(ended=True, cost_usd=3.25, task_ids=("task-a",))
    after = _ledger(db, work=work).compute(tenant_id="eng", person=ALICE, session_id=session)

    assert after["session"]["reserved_usd"] == 0.0
    assert _line(after["session"], "work_finished")["spent_usd"] == pytest.approx(3.25)
    assert after["session"]["total_usd"] == pytest.approx(3.25)


def test_an_ended_item_with_an_unreported_cost_keeps_its_reservation(db):
    project, session = _session(db)
    _start_row(db, project, session, "task-a", steps=[10.0])
    work = FakeWork()
    work.items["task-a"] = vl.WorkState(ended=True, cost_usd=None, task_ids=("task-a",))

    view = _ledger(db, work=work).compute(tenant_id="eng", person=ALICE, session_id=session)

    assert _line(view["session"], "work_finished")["reserved_usd"] == pytest.approx(12.0)
    assert view["session"]["unmeasured_items"] == 1


def test_follow_ups_the_platform_started_count_by_the_projects_tag(db):
    project, session = _session(db)
    _start_row(db, project, session, "task-a", steps=[1.0], at=NOW - timedelta(hours=1))
    work = FakeWork()
    work.items["task-a"] = vl.WorkState(ended=True, cost_usd=0.5, task_ids=("task-a",))
    work.tags[f"voice:{project}"] = [
        vl.TaggedTask("task-a", NOW - timedelta(hours=1), 0.5),  # the start's own: not twice
        vl.TaggedTask("fix-1", NOW - timedelta(minutes=30), 2.0),
    ]

    view = _ledger(db, work=work).compute(tenant_id="eng", person=ALICE, session_id=session)

    assert _line(view["session"], "followups")["spent_usd"] == pytest.approx(2.0)
    assert view["session"]["total_usd"] == pytest.approx(2.5)


# ---- the caps -------------------------------------------------------------------


def test_a_start_that_would_pass_the_session_cap_is_refused_naming_it(db):
    project, session = _session(db)
    _start_row(db, project, session, "task-a", steps=[75.0])  # $90 reserved
    ledger = _ledger(db)

    with pytest.raises(vl.VoiceCapReached) as refused:
        ledger.reserve_or_refuse(tenant_id="eng", person=ALICE, session_id=session,
                                 steps=[{"usd": 10.0}])  # +$12 -> $102

    assert refused.value.status_code == 409
    assert refused.value.detail["reason"] == vl.SESSION_CAP_REACHED
    assert refused.value.detail["line"] == "session"
    assert refused.value.detail["would_be_usd"] == pytest.approx(102.0)
    assert "$100 cap" in refused.value.message

    # Exactly at the cap is not past it.
    ok = ledger.reserve_or_refuse(tenant_id="eng", person=ALICE, session_id=session,
                                  steps=[{"budget": {"usd": 5.0}}, {"usd": 3.0}],
                                  allowance_usd=0.4)  # $9.60 + $0.40 -> exactly $100
    assert ok["reserved_usd"] == pytest.approx(10.0)
    assert ok["steps"] == [{"usd": 5.0}, {"usd": 3.0}] and ok["day"] == "2026-11-02"


def test_a_start_that_would_pass_the_day_cap_is_refused_naming_the_day(db):
    # Earlier sessions today, in other projects, hold $291.60 between them.
    for _ in range(3):
        p, s = _session(db, created=NOW - timedelta(hours=3))
        _start_row(db, p, s, "t-" + secrets.token_hex(4), steps=[81.0],
                   at=NOW - timedelta(hours=3))
    _project, session = _session(db)
    ledger = _ledger(db)

    with pytest.raises(vl.VoiceCapReached) as refused:
        ledger.reserve_or_refuse(tenant_id="eng", person=ALICE, session_id=session,
                                 steps=[{"usd": 10.0}])  # session $12; day $303.60

    assert refused.value.detail["reason"] == vl.DAY_CAP_REACHED
    assert refused.value.detail["line"] == "day"
    assert "$300 cap" in refused.value.message


def test_a_step_without_a_usd_budget_is_refused():
    with pytest.raises(ValidationFailed):
        vl.step_budgets([{"tokens": 1000}])
    with pytest.raises(ValidationFailed):
        vl.step_budgets([])


# ---- the day ------------------------------------------------------------------


def test_the_day_follows_the_saved_time_zone_across_midnight(db):
    # 23:30 UTC on 2 November is 08:30 on 3 November in Tokyo.
    now = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    project, session = _session(db, created=now)
    # Started at 14:00 UTC: 2 November in UTC, 23:00 on 2 November in Tokyo.
    p2, s2 = _session(db, created=datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
    _start_row(db, p2, s2, "earlier", steps=[50.0],
               at=datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
    # Started at 15:30 UTC: 00:30 on 3 November in Tokyo.
    _start_row(db, p2, s2, "after-tokyo-midnight", steps=[25.0],
               at=datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc))

    utc = _ledger(db, now=now, tz=None).compute(tenant_id="eng", person=ALICE, session_id=session)
    tokyo = _ledger(db, now=now, tz="Asia/Tokyo").compute(
        tenant_id="eng", person=ALICE, session_id=session)

    assert utc["date"] == "2026-11-02" and utc["time_zone"] == "UTC"
    # Both starts are on 2 November in UTC; in Tokyo only the second is today.
    assert utc[vl.DAY]["reserved_usd"] == pytest.approx(90.0)
    assert tokyo["date"] == "2026-11-03" and tokyo["time_zone"] == "Asia/Tokyo"
    assert tokyo[vl.DAY]["reserved_usd"] == pytest.approx(30.0)


# ---- the tenant boundary ------------------------------------------------------


def test_another_tenants_or_persons_session_is_the_same_404(db):
    _p, theirs = _session(db, tenant="research", person="bob@example.com")
    _p2, hers = _session(db, tenant="eng", person="dana@example.com")
    ledger = _ledger(db)

    for session in (theirs, hers, _sid()):
        with pytest.raises(NotFound):
            ledger.compute(tenant_id="eng", person=ALICE, session_id=session)
        with pytest.raises(NotFound):
            ledger.reserve_or_refuse(tenant_id="eng", person=ALICE, session_id=session,
                                     steps=[{"usd": 1.0}])


def test_another_tenants_starts_never_enter_the_ledger(db):
    project, session = _session(db)
    # A row naming alice's project and session, in another tenant.
    _start_row(db, project, session, "planted", steps=[80.0], tenant="research")

    view = _ledger(db).compute(tenant_id="eng", person=ALICE, session_id=session)

    assert view["session"]["total_usd"] == 0.0


@pytest.fixture
def client(db, tokens, group_map, objects):
    c = _client(db, tokens, group_map, objects, FakeVoice(),
                voice_url="http://voice.internal:8080",
                voice_people=("alice@example.com", "bob@example.com"),
                child_key=secrets.token_hex(32))
    c.app.state.voice_work = FakeWork()
    return c


def test_the_read_route_serves_the_lines_and_404s_another_tenants_session(client):
    mine = _start(client, "alice").json()
    theirs = _start(client, "bob").json()  # research

    ok = client.get(f"/v1/voice/sessions/{mine['session_id']}/ledger",
                    headers=auth_header("alice"))
    other = client.get(f"/v1/voice/sessions/{theirs['session_id']}/ledger",
                       headers=auth_header("alice"))
    missing = client.get(f"/v1/voice/sessions/{_sid()}/ledger", headers=auth_header("alice"))
    stranger = client.get(f"/v1/voice/sessions/{mine['session_id']}/ledger",
                          headers=auth_header("carol"))

    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["session"]["cap_usd"] == 100.0 and body["day"]["cap_usd"] == 300.0
    assert [row["line"] for row in body["session"]["lines"]] == list(vl.LINES)
    assert body["time_zone"] == "UTC"
    assert other.status_code == missing.status_code == 404
    assert other.json()["code"] == missing.json()["code"]
    assert stranger.status_code == 403


# ---- FirestoreWork --------------------------------------------------------------


def _attempt(db, task_id, cost, *, tenant="eng"):
    attempt_id = "at-" + secrets.token_hex(4)
    db.collection(ATTEMPTS).document(attempt_id).set({
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant, "generation": 1,
        "created_at": NOW, "cost_usd": cost})


def test_firestore_work_measures_an_ended_task_in_the_callers_tenant_only(db):
    db.collection(vl.TASKS).document("t-done").set({"tenant_id": "eng", "state": "SUCCEEDED"})
    db.collection(vl.TASKS).document("t-run").set({"tenant_id": "eng", "state": "RUNNING"})
    db.collection(vl.TASKS).document("t-theirs").set({"tenant_id": "research",
                                                       "state": "SUCCEEDED"})
    _attempt(db, "t-done", 1.25)
    _attempt(db, "t-done", 0.75)
    work = vl.FirestoreWork(db)

    assert work.state("eng", "agent", "t-done") == vl.WorkState(True, 2.0, ("t-done",))
    assert work.state("eng", "agent", "t-run").ended is False
    theirs = work.state("eng", "agent", "t-theirs")
    assert theirs.ended is False and theirs.task_ids == ()


def test_firestore_work_finds_tagged_tasks_in_the_tenant(db):
    tag = "voice:" + _pid()
    db.collection(vl.TASKS).document("fix-1").set({
        "tenant_id": "eng", "state": "SUCCEEDED", "created_at": NOW,
        "metadata": {"claude_session": tag}})
    db.collection(vl.TASKS).document("fix-2").set({
        "tenant_id": "research", "state": "SUCCEEDED", "created_at": NOW,
        "metadata": {"claude_session": tag}})
    _attempt(db, "fix-1", 0.4)

    rows = vl.FirestoreWork(db).tagged("eng", tag)

    assert [(r.task_id, r.cost_usd) for r in rows] == [("fix-1", pytest.approx(0.4))]
