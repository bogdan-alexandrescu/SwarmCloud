"""The bell follows the person's notification preferences (PROFILE-BELL).

docs/design/user-profile.md §4.6, Q3 and Q4 of §7.1; docs/notifications.md.
The feed stays DERIVED on read. Preferences are applied after the sources
derive their items, and nothing new is stored:

* every item carries a `topic` from `TOPICS`, a finer cut than its group: a
  failed, cancelled or dead-lettered task or workflow is `failed`, anything
  else that ended is `finished`;
* a muted topic leaves the list and the badge, and is counted in `muted`
  (`needs_you` / `updates`), so a muted approval is quiet, never invisible;
  `admin_pending` (the Admin pill) is counted before muting;
* `mark_all_read` marks only what was shown: a muted item unmuted later is
  unread, which is true;
* a reminder is a read mark that ages out: at `every_4h` / `daily` a
  needs-you item read longer ago than that is unread again; an update never
  re-rings;
* a waiting step's reminder period is the person's when they chose one and
  the author's `remind_after_seconds` at `as_set` (Q4); `never` holds `k` at 0;
* unreadable preferences: with `X-Swarm-Tenant` the bell is served
  unfiltered with `partial: true`; without it, authentication answers 503
  `preferences_unavailable` and no source runs (Q9).

`alice` is in eng; `carol` asks for a workspace, which is an admin's item.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import notifications as notif
from swarm_api import preferences
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forgeapp import CONNECTIONS, connection_id_for
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.preferences import Preferences
from swarm_api.store import Store
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, seed_tenant
from .test_await_input_notifications import _await_step
from .test_default_workspace import (
    GROUPS,
    SVC,
    FlakyPreferencesFirestore,
    _tokens,
    as_,
    authenticator,
    seed_personal,
)
from .test_notifications import _approval, _task, _waiting, _workspace

ALICE = "alice@example.com"
CAROL = "carol@example.com"
HOUR = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**delta: float) -> datetime:
    return _now() - timedelta(**delta)


@pytest.fixture
def db() -> FlakyPreferencesFirestore:
    db = FlakyPreferencesFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_personal(db, "dave")
    return db


@pytest.fixture
def prefs(db) -> Preferences:
    return Preferences(db, [secrets.token_hex(32)], ttl=0.0)


@pytest.fixture
def client(db, prefs) -> TestClient:
    ctx = build_context(
        settings=api_settings(allowed_users=(SVC,)), db=db,
        verifier=StaticTokenVerifier(_tokens()), groups=StaticGroups(GROUPS),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=None, forge_tokens=NoForgeTokens(),
    )
    ctx.authenticator = authenticator(db, prefs)
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def _viewer(*, muted: tuple[str, ...] = (), reminder: str = "as_set",
            admin: bool = False) -> notif.Viewer:
    return notif.Viewer(email=ALICE, tenant_id="eng", is_admin=admin,
                        muted=frozenset(muted), reminder=reminder)


def _service(db, *, now=_now) -> notif.Notifications:
    return notif.Notifications(db, Store(db), now=now)


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for i in body["items"]}


def _workflow(db, workflow_id: str, state: str) -> None:
    db.docs[f"workflows/{workflow_id}"] = {
        "workflow_id": workflow_id, "tenant_id": "eng", "created_at": _ago(hours=1),
        "updated_at": _ago(minutes=3), "state": state, "submitted_by": ALICE, "steps": [],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False}


def _github_failed(db) -> str:
    conn = connection_id_for("eng", ALICE)
    db.docs[f"{CONNECTIONS}/{conn}"] = {
        "connection_id": conn, "tenant_id": "eng", "user": ALICE, "state": "refresh_failed",
        "failed_at": _ago(hours=1), "failure": {"code": "REFRESH_FAILED", "recovery": "Reconnect."}}
    return conn


def _save(prefs: Preferences, *, muted: list[str], reminder: str = "as_set") -> None:
    prefs.save(ALICE, display_name=None, default_workspace=None, muted=muted,
               reminder=reminder, revision=prefs.read(ALICE).revision)


# -- topics ---------------------------------------------------------------------

def test_the_topics_are_the_preferences_topics():
    assert notif.TOPICS is preferences.TOPICS


def test_every_item_carries_a_topic_from_topics(db):
    _workspace(db, CAROL)
    approval_id = _approval(db, "f1")
    _await_step(db, "t_review", by=ALICE, remind=[ALICE])
    _waiting(db, "task_h")
    _task(db, "task_q", result_summary={"questions": 1}, workflow_id="wf_q")
    _task(db, "task_ok")
    _task(db, "task_bad", state="FAILED")
    _task(db, "task_cancel", state="CANCELLED")
    _task(db, "task_dead", state="DEAD_LETTERED")
    _github_failed(db)

    body = _service(db).feed(_viewer(admin=True))
    topics = {i["id"]: i["topic"] for i in body["items"]}
    assert body["items"] and all(t in notif.TOPICS for t in topics.values()), topics
    assert topics["admin:workspace:w-c0ffee:req-w-c0ffee"] == "admin"
    assert topics[f"approval:{approval_id}"] == "approval"
    assert topics["task:input:t_review:0"] == "waiting_step"
    assert topics["task:handoff:task_h:1:signin-1"] == "question"
    assert topics["task:questions:task_q"] == "question"
    assert topics["finished:task:task_ok:SUCCEEDED"] == "finished"
    assert topics["finished:task:task_bad:FAILED"] == "failed"
    assert topics["finished:task:task_cancel:CANCELLED"] == "failed"
    assert topics["finished:task:task_dead:DEAD_LETTERED"] == "failed"
    assert {t for i, t in topics.items() if i.startswith("account:github:")} == {"account"}


def test_a_failed_workflow_is_failed_and_a_succeeded_one_finished(db):
    _workflow(db, "wf_ok", "SUCCEEDED")
    _workflow(db, "wf_bad", "FAILED")
    topics = {i["id"]: i["topic"] for i in _service(db).feed(_viewer())["items"]}
    assert topics == {"finished:workflow:wf_ok:SUCCEEDED": "finished",
                      "finished:workflow:wf_bad:FAILED": "failed"}


# -- muting ---------------------------------------------------------------------

def test_a_muted_topic_leaves_the_list_and_the_badge_and_is_counted(db):
    approval_id = _approval(db, "f1")
    _github_failed(db)
    _task(db, "task_ok")
    _task(db, "task_ok2")
    _task(db, "task_bad", state="FAILED")
    service = _service(db)

    unmuted = service.feed(_viewer())
    assert unmuted["unread_needs_you"] == 2 and unmuted["unread_updates"] == 3
    assert unmuted["muted"] == {"needs_you": 0, "updates": 0}

    body = service.feed(_viewer(muted=("approval", "finished")))
    shown = _by_id(body)
    assert f"approval:{approval_id}" not in shown
    assert not any(i["topic"] in ("approval", "finished") for i in body["items"])
    assert "finished:task:task_bad:FAILED" in shown
    assert body["unread_needs_you"] == 1  # GitHub only: the approval is muted
    assert body["unread_updates"] == 1    # the failure only
    assert body["muted"] == {"needs_you": 1, "updates": 2}


def test_admin_pending_ignores_muting(db):
    _workspace(db, CAROL)
    body = _service(db).feed(_viewer(admin=True, muted=("admin",)))
    assert body["admin_pending"] == 1
    assert not any(i["group"] == "admin" for i in body["items"])
    assert body["muted"]["needs_you"] == 1 and body["unread_needs_you"] == 0


def test_muting_the_finished_updates_leaves_room_for_the_failures(db):
    for n in range(notif.MAX_UPDATES + 5):
        _task(db, f"task_{n:03d}", ended=_ago(minutes=n + 1))
    _task(db, "task_bad", state="FAILED", ended=_ago(hours=5))
    body = _service(db).feed(_viewer(muted=("finished",)))
    assert [i["id"] for i in body["items"]] == ["finished:task:task_bad:FAILED"]
    assert body["muted"]["updates"] == notif.MAX_UPDATES + 5


def test_mark_all_read_marks_only_the_shown_items(db):
    approval_id = _approval(db, "f1")
    _task(db, "task_ok")
    service = _service(db)
    service.mark_all_read(_viewer(muted=("finished",)))

    body = _by_id(service.feed(_viewer()))
    assert body[f"approval:{approval_id}"]["read"] is True
    # Muted when "mark all read" was pressed, so not seen: unread once unmuted.
    assert body["finished:task:task_ok:SUCCEEDED"]["read"] is False


def test_mark_all_read_with_nothing_muted_still_marks_everything_before_now(db):
    _task(db, "task_ok")
    service = _service(db)
    service.mark_all_read(_viewer())
    body = service.feed(_viewer())
    assert body["read_at"] is not None
    assert all(i["read"] for i in body["items"])


# -- reminders ------------------------------------------------------------------

def _marked(db, ids: list[str], hours_ago: float, *, all_read: bool = False) -> None:
    then = _ago(hours=hours_ago)
    service = _service(db, now=lambda: then)
    if all_read:
        service.mark_all_read(_viewer())
    else:
        service.mark_read(_viewer(), ids)


def test_every_4h_rings_an_approval_read_5h_ago_and_not_one_read_3h_ago(db):
    old = _approval(db, "f-old")
    new = _approval(db, "f-new")
    _marked(db, [f"approval:{old}"], 5)
    _marked(db, [f"approval:{new}"], 3)
    service = _service(db)

    ringing = _by_id(service.feed(_viewer(reminder="every_4h")))
    assert ringing[f"approval:{old}"]["read"] is False
    assert ringing[f"approval:{new}"]["read"] is True
    # `as_set` is today's: a read item stays read.
    steady = _by_id(service.feed(_viewer()))
    assert steady[f"approval:{old}"]["read"] is True


def test_daily_ages_a_mark_after_24h(db):
    approval_id = _approval(db, "f1")
    _marked(db, [f"approval:{approval_id}"], 5)
    assert _by_id(_service(db).feed(_viewer(reminder="daily")))[f"approval:{approval_id}"]["read"]
    _marked(db, [f"approval:{approval_id}"], 25)
    assert not _by_id(_service(db).feed(
        _viewer(reminder="daily")))[f"approval:{approval_id}"]["read"]


def test_a_mark_all_read_ages_out_for_needs_you_items_too(db):
    approval_id = _approval(db, "f1")
    _marked(db, [], 5, all_read=True)
    body = _by_id(_service(db).feed(_viewer(reminder="every_4h")))
    assert body[f"approval:{approval_id}"]["read"] is False


def test_an_update_never_re_rings(db):
    _task(db, "task_ok", ended=_ago(hours=6))
    _task(db, "task_bad", state="FAILED", ended=_ago(hours=6))
    _marked(db, ["finished:task:task_ok:SUCCEEDED"], 5)
    _marked(db, [], 5, all_read=True)
    for reminder in preferences.REMINDERS:
        body = _service(db).feed(_viewer(reminder=reminder))
        assert all(i["read"] for i in body["items"]), reminder
        assert body["unread_updates"] == 0


def _step_ids(db, reminder: str) -> list[str]:
    reading = notif.Reading(db=db, store=Store(db),
                            viewer=_viewer(reminder=reminder), now=_now())
    return [i["id"] for i in notif.awaiting_input(reading)]


def test_a_waiting_step_rings_on_the_authors_period_at_as_set(db):
    _await_step(db, "t_review", by=ALICE, remind=[ALICE], remind_after=HOUR,
                opened=_ago(hours=5, minutes=10))
    assert _step_ids(db, "as_set") == ["task:input:t_review:5"]


def test_a_waiting_step_rings_on_the_persons_period_when_they_chose_one(db):
    _await_step(db, "t_review", by=ALICE, remind=[ALICE], remind_after=HOUR,
                opened=_ago(hours=9, minutes=10))
    assert _step_ids(db, "every_4h") == ["task:input:t_review:2"]
    assert _step_ids(db, "daily") == ["task:input:t_review:0"]


def test_the_persons_period_applies_when_the_author_declared_none(db):
    _await_step(db, "t_review", by=ALICE, remind=[ALICE], remind_after=None,
                opened=_ago(hours=9, minutes=10))
    assert _step_ids(db, "as_set") == ["task:input:t_review:0"]
    assert _step_ids(db, "every_4h") == ["task:input:t_review:2"]


def test_never_holds_a_waiting_steps_k_at_0(db):
    opened = _ago(hours=5, minutes=10)
    _await_step(db, "t_review", by=ALICE, remind=[ALICE], remind_after=HOUR, opened=opened)
    assert _step_ids(db, "never") == ["task:input:t_review:0"]
    reading = notif.Reading(db=db, store=Store(db), viewer=_viewer(reminder="never"),
                            now=_now())
    (item,) = notif.awaiting_input(reading)
    assert abs((item["at"] - opened).total_seconds()) < 1


def test_never_keeps_a_read_needs_you_item_read(db):
    approval_id = _approval(db, "f1")
    _marked(db, [f"approval:{approval_id}"], 25)
    assert _by_id(_service(db).feed(_viewer(reminder="never")))[f"approval:{approval_id}"]["read"]


# -- the route: preferences from authentication ----------------------------------

def _get(client: TestClient, tenant: str | None) -> Any:
    return client.get("/v1/notifications", headers=as_("alice", tenant))


def test_the_route_applies_the_stored_mutes_and_reminder(client, db, prefs):
    _approval(db, "f1")
    _task(db, "task_ok")
    _save(prefs, muted=["finished"], reminder="every_4h")

    for tenant in (None, "eng"):
        response = _get(client, tenant)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [i["topic"] for i in body["items"]] == ["approval"]
        assert body["muted"] == {"needs_you": 0, "updates": 1}
        assert body["partial"] is False


def test_unreadable_preferences_with_a_header_serve_everything_unfiltered_as_partial(
        client, db, prefs):
    _approval(db, "f1")
    _task(db, "task_ok")
    _save(prefs, muted=["finished", "approval"])
    db.failing = True

    response = _get(client, "eng")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is True
    assert sorted(i["topic"] for i in body["items"]) == ["approval", "finished"]
    assert body["muted"] == {"needs_you": 0, "updates": 0}


def test_unreadable_preferences_without_a_header_are_503_and_derive_nothing(
        client, db, prefs, monkeypatch):
    _approval(db, "f1")
    derived: list[Any] = []
    monkeypatch.setattr(notif.Notifications, "_derive",
                        lambda self, viewer: derived.append(viewer))
    db.failing = True

    for method, path in (("GET", "/v1/notifications"), ("POST", "/v1/notifications/read-all")):
        response = client.request(method, path, headers=as_("alice"))
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "preferences_unavailable"
    assert derived == []
