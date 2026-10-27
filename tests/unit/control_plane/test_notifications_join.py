"""The bell's two join-request sources (docs/design/join-requests.md §4.6;
lane JOIN-BELL, part of #2325).

* `join_requests_to_decide`: an open request to join a group workspace rings
  that workspace's admins and platform admins, `admin:join:<ref>`, and nobody
  else. A workspace admin is rung only for a workspace they administer, and
  only while the request names the group that workspace has now.
* `own_join_requests`: the requester is rung once per decision, approved or
  denied, `join:<ref>:<state>`, for `WINDOW` after it. A withdrawal is their
  own act and rings nothing.
* Both are derived on each read, so a decided request derives no "needs you"
  item, and both read nothing while no join store is wired.

Every signing key is made at runtime.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import joinrequests, notifications as notif
from swarm_api.joinrequests import JoinStore
from swarm_api.metrics import ApiMetrics
from swarm_api.store import Store

from .conftest import ENG_GROUP, RESEARCH_GROUP, api_settings, seed_tenant
from .fakes import FakeFirestore

T0 = datetime(2026, 10, 25, 6, 41, 9, 512000, tzinfo=timezone.utc)
LEE = "lee@example.com"
SAM = "sam@example.com"
ALEX = "alex@example.com"
ROOT = "root@example.com"


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


@pytest.fixture
def db() -> FakeFirestore:
    db = FakeFirestore()
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return db


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def joins(db, clock) -> JoinStore:
    return JoinStore(db, api_settings(settings_key=secrets.token_hex(32)),
                     metrics=ApiMetrics(), now=clock)


def _ask(joins: JoinStore, requester: str = LEE, tenant_id: str = "eng",
         group: str = ENG_GROUP, message: str | None = None) -> str:
    view, _ = joins.ask(tenant_id=tenant_id, group=group, requester=requester, message=message)
    return view.request.ref


def _decide(joins: JoinStore, ref: str, state: str, **kwargs: Any) -> None:
    if state == joinrequests.APPROVED:
        kwargs.setdefault("role", "member")
    if state == joinrequests.DENIED:
        kwargs.setdefault("reason", "the labs rota is full this quarter")
    via = joinrequests.VIA_REQUESTER if state == joinrequests.WITHDRAWN \
        else joinrequests.VIA_WORKSPACE_ADMIN
    by = kwargs.pop("by", LEE if state == joinrequests.WITHDRAWN else ALEX)
    joins.decide(ref, state=state, by=by, via=via, **kwargs)


def _viewer(email: str, *, admin: bool = False,
            administers: tuple[tuple[str, str], ...] | None = ()) -> notif.Viewer:
    return notif.Viewer(email=email, tenant_id="eng", is_admin=admin, administers=administers)


def _reading(db, viewer: notif.Viewer, joins: Any, clock: Clock) -> notif.Reading:
    return notif.Reading(db=db, store=Store(db), viewer=viewer, now=clock(), joins=joins)


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return sorted(i["id"] for i in items)


class Spy:
    """A join store that records every read, to show a reader is not read for."""

    def __init__(self, inner: JoinStore) -> None:
        self.inner = inner
        self.calls: list[tuple[str, Any]] = []

    def open_for(self, tenant_ids: Any) -> Any:
        self.calls.append(("open_for", tenant_ids))
        return self.inner.open_for(tenant_ids)

    def mine(self, requester: str) -> Any:
        self.calls.append(("mine", requester))
        return self.inner.mine(requester)


# --------------------------------------------------------------------------
# join_requests_to_decide
# --------------------------------------------------------------------------

def test_a_workspace_admin_is_rung_for_their_workspaces_request_and_not_anothers(db, joins, clock):
    eng = _ask(joins, LEE, "eng", ENG_GROUP, message="I'm on the labs rota from Monday")
    research = _ask(joins, SAM, "research", RESEARCH_GROUP)
    viewer = _viewer(ALEX, administers=(("eng", ENG_GROUP),))

    items = notif.join_requests_to_decide(_reading(db, viewer, joins, clock))

    assert _ids(items) == [f"admin:join:{eng}"]
    (item,) = items
    assert item["group"] == notif.ADMIN and item["topic"] == notif.T_ADMIN
    assert item["needs_you"] is True and item["href"] == "/admin/people"
    assert item["title"] == f"{LEE} asks to join eng"
    assert item["detail"] == "I'm on the labs rota from Monday"
    assert research not in item["id"]


def test_a_request_naming_a_group_the_workspace_no_longer_has_rings_no_workspace_admin(
        db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    replaced = _viewer(ALEX, administers=(("eng", "eng-new@example.com"),))
    assert notif.join_requests_to_decide(_reading(db, replaced, joins, clock)) == []
    # The platform admin still sees it: they decide any request.
    root = _viewer(ROOT, admin=True)
    assert _ids(notif.join_requests_to_decide(_reading(db, root, joins, clock))) == [
        f"admin:join:{ref}"]


def test_a_non_admin_is_rung_for_nothing_and_no_request_is_read_for_them(db, joins, clock):
    _ask(joins, LEE, "eng", ENG_GROUP)
    spy = Spy(joins)
    assert notif.join_requests_to_decide(_reading(db, _viewer(SAM), spy, clock)) == []
    assert spy.calls == []


def test_a_platform_admin_is_rung_for_every_workspaces_request(db, joins, clock):
    eng = _ask(joins, LEE, "eng", ENG_GROUP)
    research = _ask(joins, SAM, "research", RESEARCH_GROUP)
    items = notif.join_requests_to_decide(_reading(db, _viewer(ROOT, admin=True), joins, clock))
    assert _ids(items) == sorted([f"admin:join:{eng}", f"admin:join:{research}"])


def test_a_decided_request_derives_no_needs_you_item(db, joins, clock):
    approved = _ask(joins, LEE, "eng", ENG_GROUP)
    denied = _ask(joins, SAM, "eng", ENG_GROUP)
    viewer = _viewer(ALEX, administers=(("eng", ENG_GROUP),))
    assert len(notif.join_requests_to_decide(_reading(db, viewer, joins, clock))) == 2

    _decide(joins, approved, joinrequests.APPROVED)
    _decide(joins, denied, joinrequests.DENIED)

    assert notif.join_requests_to_decide(_reading(db, viewer, joins, clock)) == []
    root = _viewer(ROOT, admin=True)
    assert notif.join_requests_to_decide(_reading(db, root, joins, clock)) == []


def test_an_expired_request_rings_nobody(db, joins, clock):
    _ask(joins, LEE, "eng", ENG_GROUP)
    clock.tick(days=joinrequests.OPEN_DAYS, seconds=1)
    viewer = _viewer(ALEX, administers=(("eng", ENG_GROUP),))
    assert notif.join_requests_to_decide(_reading(db, viewer, joins, clock)) == []


# --------------------------------------------------------------------------
# own_join_requests
# --------------------------------------------------------------------------

def test_the_requester_is_rung_once_per_decision(db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    reading = _reading(db, _viewer(LEE), joins, clock)
    assert notif.own_join_requests(reading) == [], "an open request is the admins' item"

    clock.tick(hours=2)
    _decide(joins, ref, joinrequests.APPROVED)
    first = notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock))
    clock.tick(hours=1)
    again = notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock))

    assert _ids(first) == _ids(again) == [f"join:{ref}:approved"]
    (item,) = first
    assert item["title"] == "Your request to join eng was approved"
    assert item["needs_you"] is False and item["group"] == notif.ACCOUNT
    assert item["topic"] == notif.T_ACCOUNT and item["href"] == "/join"
    assert item["at"] == T0 + timedelta(hours=2)


def test_a_denial_rings_the_requester_with_its_reason_and_nobody_else(db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    _decide(joins, ref, joinrequests.DENIED, reason="the labs rota is full this quarter")

    (item,) = notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock))
    assert item["id"] == f"join:{ref}:denied"
    assert item["title"] == "Your request to join eng was denied"
    assert item["detail"] == "the labs rota is full this quarter"
    assert ALEX not in (item["title"] + (item["detail"] or "")), "never who decided"
    assert notif.own_join_requests(_reading(db, _viewer(SAM), joins, clock)) == []
    assert notif.own_join_requests(_reading(db, _viewer(ALEX), joins, clock)) == []


def test_a_withdrawal_rings_nobody(db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    _decide(joins, ref, joinrequests.WITHDRAWN)
    assert notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock)) == []


def test_a_decision_older_than_the_window_rings_no_longer(db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    _decide(joins, ref, joinrequests.APPROVED)
    clock.tick(days=7, seconds=1)
    assert notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock)) == []


def test_a_workspace_without_a_display_name_is_never_named_by_its_id_to_the_requester(
        db, joins, clock):
    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    db.collection("tenants").document("eng").update({"display_name": None})
    _decide(joins, ref, joinrequests.APPROVED)
    (item,) = notif.own_join_requests(_reading(db, _viewer(LEE), joins, clock))
    assert item["title"] == "Your request to join a group workspace was approved"


# --------------------------------------------------------------------------
# through the feed
# --------------------------------------------------------------------------

def test_both_sources_are_in_the_feed(db, joins, clock):
    names = dict(notif.SOURCES)
    assert names["join_requests"] is notif.join_requests_to_decide
    assert names["own_join_requests"] is notif.own_join_requests

    ref = _ask(joins, LEE, "eng", ENG_GROUP)
    service = notif.Notifications(db, Store(db), now=clock, joins=joins)
    alex = service.feed(_viewer(ALEX, administers=(("eng", ENG_GROUP),)))
    assert f"admin:join:{ref}" in [i["id"] for i in alex["items"]]
    assert alex["partial"] is False
    sam = service.feed(_viewer(SAM))
    assert not [i for i in sam["items"] if "join" in i["id"]]


def test_a_platform_admins_join_items_count_in_the_admin_pill_and_not_as_tenants_waiting(
        db, joins, clock):
    _ask(joins, LEE, "eng", ENG_GROUP)
    service = notif.Notifications(db, Store(db), now=clock, joins=joins)
    feed = service.feed(_viewer(ROOT, admin=True))
    assert feed["admin_pending"] == 1
    assert feed["tenants_waiting"] == 0


def test_an_unread_administered_list_is_partial_not_silence(db, joins, clock, caplog):
    _ask(joins, LEE, "eng", ENG_GROUP)
    service = notif.Notifications(db, Store(db), now=clock, joins=joins)
    with caplog.at_level(logging.WARNING, logger="swarm_api.notifications"):
        feed = service.feed(_viewer(ALEX, administers=None))
    assert feed["partial"] is True
    assert "source join_requests unread" in caplog.text


def test_without_a_join_store_the_join_sources_read_nothing(db, joins, clock):
    _ask(joins, LEE, "eng", ENG_GROUP)
    service = notif.Notifications(db, Store(db), now=clock)
    feed = service.feed(_viewer(ROOT, admin=True))
    assert feed["partial"] is False
    assert not [i for i in feed["items"] if "join" in i["id"]]
