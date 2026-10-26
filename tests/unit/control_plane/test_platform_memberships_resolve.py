"""Tenant resolution offers a group workspace a person joined by an approved
request, beside its Google group (docs/design/join-requests.md §4.5; lane
JOIN-RESOLVE, part of #2325; owner's Q1 (A), 2026-10-25).

Tenant isolation (invariant 9) is the risk, so every case runs the real
`Authenticator` over the real `CloudIdentityGroups` (a fake HTTP session that
records every call) and the real `MembershipStore` (a fake Firestore that
counts every membership query). With `platform_memberships` off nothing may
read a membership; with it on, a membership may only ADD a choice, after the
confirmed groups, for the registered group it names -- and a forged, replayed
or removed grant, a replaced group, or a read that failed must never admit.

`alice` is in research only; `lee` is in no group; `svc` is a service
account. The registered groups are eng and research.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_api import joinrequests, memberships
from swarm_api.auth import Authenticator, StaticTokenVerifier, TenantNotMember
from swarm_api.errors import JoinDeniedRecently, JoinRequestsDailyLimit, JoinRequestsOpenLimit, \
    JoinWorkspaceQueueFull, MembershipUnavailable
from swarm_api.groups import CloudIdentityGroups
from swarm_api.joinrequests import JoinStore
from swarm_api.memberships import MembershipStore
from swarm_api.routes import tenants as tenant_routes
from swarm_api.settings import ApiSettings

from .conftest import ENG_GROUP, PROJECT, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore
from .test_group_resolution import FakeSession

T0 = datetime(2026, 10, 25, 6, 41, tzinfo=timezone.utc)
ALICE = "alice@example.com"
LEE = "lee@example.com"
SVC = "builder@proj.iam.gserviceaccount.com"

GOOGLE = {
    ENG_GROUP: set(),
    RESEARCH_GROUP: {ALICE},
}


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Recording(FakeFirestore):
    """Counts the membership queries, and breaks them when told to."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0
        self.broken = False

    def collection(self, path: str) -> Any:
        if path == memberships.COLLECTION:
            self.asked += 1
            if self.broken:
                return Broken()
        return super().collection(path)


class Broken:
    def where(self, *args: Any, **kwargs: Any) -> "Broken":
        return self

    order_by = limit = where

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("firestore unavailable")


class Ready:
    """`PersonalTenants`: whose own personal workspace is ready."""

    def __init__(self, *emails: str) -> None:
        self.emails = set(emails)

    def ready(self, email: str) -> bool:
        return email in self.emails

    def owns_service_tenant(self, email: str) -> bool:
        return False


def claims() -> dict[str, dict[str, Any]]:
    people = {"alice": ALICE, "lee": LEE, "svc": SVC}
    return {f"t-{name}": {"email": email, "email_verified": True, "sub": f"s-{name}"}
            for name, email in people.items()}


@pytest.fixture
def key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def db() -> Recording:
    return Recording()


@pytest.fixture
def store(db, key) -> MembershipStore:
    return MembershipStore(db, api_settings(settings_key=key, role_cache_seconds=15.0),
                           now=Clock())


def build(store: MembershipStore | None, key: str, *, on: bool = True,
          session: FakeSession | None = None, tenant_groups: tuple[str, ...] = (
              ENG_GROUP, RESEARCH_GROUP), personal: Any = None,
          **settings: Any) -> tuple[Authenticator, FakeSession]:
    session = session if session is not None else FakeSession(
        {g: set(m) for g, m in GOOGLE.items()})
    auth = Authenticator(
        api_settings(tenant_groups=tenant_groups, platform_memberships=on, settings_key=key,
                     **settings),
        StaticTokenVerifier(claims()),
        CloudIdentityGroups(PROJECT, session=session),
        personal=personal,
        memberships=store,
    )
    return auth, session


def join(store: MembershipStore, subject: str = LEE, tenant_id: str = "eng",
         group: str = ENG_GROUP, state: str = memberships.MEMBER) -> None:
    store.write(tenant_id=tenant_id, group=group, subject=subject, state=state,
                by="alex@example.com", via=memberships.VIA_JOIN_REQUEST,
                detail={"ref": "j-" + secrets.token_hex(6)})


def group_tenant(tenant_id: str = "eng", principal: str = ENG_GROUP) -> SimpleNamespace:
    return SimpleNamespace(kind="group", tenant_id=tenant_id, principal=principal)


# -- the switch off: nothing reads a membership --------------------------------

def test_switch_off_reads_no_membership_and_admits_nobody(store, db, key):
    join(store)
    asked = db.asked
    auth, _ = build(store, key, on=False)

    ctx = auth.authenticate("Bearer t-lee")
    assert ctx.tenant_choices == ()
    assert ctx.platform_memberships == ()
    assert ctx.tenant_id == "u-lee"
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-lee", None, tenant="eng")
    assert auth.is_tenant_member(LEE, group_tenant(), fresh=True) is False
    assert auth.may_act_in(LEE, group_tenant(), fresh=True) == ""
    assert db.asked == asked


def test_switch_off_is_the_default_and_answers_as_before_join_requests(key):
    assert ApiSettings.__dataclass_fields__["platform_memberships"].default is False
    store_db = Recording()
    store = MembershipStore(store_db, api_settings(settings_key=key), now=Clock())
    join(store)
    asked = store_db.asked
    before, _ = build(None, key, on=False)
    after, _ = build(store, key, on=False)
    for token in ("Bearer t-alice", "Bearer t-lee"):
        assert after.authenticate(token) == before.authenticate(token)
    assert store_db.asked == asked


def test_the_switch_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert ApiSettings.from_env().platform_memberships is False
    monkeypatch.setenv("PLATFORM_MEMBERSHIPS", "true")
    assert ApiSettings.from_env().platform_memberships is True


# -- the switch on: a valid membership is a choice -----------------------------

def test_a_valid_membership_is_a_choice_the_header_selects(store, key):
    join(store)
    auth, _ = build(store, key)

    ctx = auth.authenticate("Bearer t-lee")
    assert ctx.tenant_choices == (("eng", ENG_GROUP),)
    assert ctx.platform_memberships == (("eng", ENG_GROUP),)

    picked = auth.authenticate("Bearer t-lee", None, tenant="eng")
    assert picked.tenant_id == "eng"
    assert picked.tenant_principal == ENG_GROUP
    assert picked.tenant_via == ""
    # The frozen resolver's input is untouched: the group is not the person's.
    assert ENG_GROUP not in picked.principal.groups


def test_a_membership_comes_after_the_confirmed_groups(store, key):
    join(store, subject=ALICE)
    auth, _ = build(store, key)

    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.tenant_choices == (("research", RESEARCH_GROUP), ("eng", ENG_GROUP))
    assert ctx.tenant_id == "research"
    assert ctx.tenant_principal == RESEARCH_GROUP
    assert auth.authenticate("Bearer t-alice", None, tenant="eng").tenant_principal == ENG_GROUP


def test_the_own_personal_workspace_stays_first(store, key):
    join(store)
    auth, _ = build(store, key, personal=Ready(LEE))

    ctx = auth.authenticate("Bearer t-lee")
    assert ctx.tenant_choices == (("u-lee", LEE), ("eng", ENG_GROUP))
    assert ctx.tenant_id == "u-lee"


def test_with_no_other_choice_the_membership_is_the_default(store, key):
    join(store)
    auth, _ = build(store, key)
    ctx = auth.authenticate("Bearer t-lee")
    assert (ctx.tenant_id, ctx.tenant_principal) == ("eng", ENG_GROUP)


def test_a_group_members_choice_is_not_duplicated(store, key):
    join(store, subject=ALICE, tenant_id="research", group=RESEARCH_GROUP)
    auth, _ = build(store, key)
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.tenant_choices == (("research", RESEARCH_GROUP),)
    assert ctx.platform_memberships == ()


def test_the_mine_route_says_platform_member(store, key):
    join(store, subject=ALICE)
    auth, _ = build(store, key)
    ctx = auth.authenticate("Bearer t-alice")
    entries = tenant_routes.get_mine(auth=ctx, ctx=SimpleNamespace(roles=None))
    assert [(e["tenant_id"], e["via"]) for e in entries] == [
        ("research", "member"), ("eng", "platform_member")]
    assert entries[1]["kind"] == "group" and entries[1]["display_name"] == ENG_GROUP


# -- what never admits -----------------------------------------------------------

def test_a_membership_for_a_replaced_group_admits_nobody(store, key):
    join(store)
    # `eng@partner.com` also slugs to `eng` and is now registered first, so the
    # id selects it; the grant names `eng@example.com`, the group it replaced.
    replaced = ("eng@partner.com", ENG_GROUP, RESEARCH_GROUP)
    auth, _ = build(store, key, tenant_groups=replaced)
    ctx = auth.authenticate("Bearer t-lee")
    assert ctx.tenant_choices == ()
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-lee", None, tenant="eng")
    assert auth.is_tenant_member(LEE, group_tenant("eng", "eng@partner.com"), fresh=True) is False


def test_a_membership_for_a_group_no_longer_registered_admits_nobody(store, key):
    join(store)
    auth, _ = build(store, key, tenant_groups=(RESEARCH_GROUP,))
    assert auth.authenticate("Bearer t-lee").tenant_choices == ()
    assert auth.is_tenant_member(LEE, group_tenant(), fresh=True) is False


def test_a_forged_unsigned_membership_admits_nobody(store, db, key):
    forged = {"tenant_id": "eng", "group": ENG_GROUP, "subject": LEE, "state": "member",
              "by": "alex@example.com", "at": "2026-10-25T06:41:00.000000Z",
              "via": "join_request", "reason": "",
              "mac": {"v": 1, "key": "k", "sig": secrets.token_urlsafe(32)}}
    FakeFirestore.collection(db, memberships.COLLECTION).document().set(forged)
    auth, _ = build(store, key)
    assert auth.authenticate("Bearer t-lee").tenant_choices == ()
    assert auth.is_tenant_member(LEE, group_tenant(), fresh=True) is False


def test_a_grant_signed_under_another_key_admits_nobody(db, key):
    other = MembershipStore(db, api_settings(settings_key=secrets.token_hex(32)), now=Clock())
    join(other)
    auth, _ = build(MembershipStore(db, api_settings(settings_key=key), now=Clock()), key)
    assert auth.authenticate("Bearer t-lee").tenant_choices == ()


def test_a_removed_membership_admits_nobody_and_a_replayed_member_loses(store, db, key):
    join(store)
    (granted,) = [d for d in db.dump(f"{memberships.COLLECTION}/").values()]
    join(store, state=memberships.REMOVED)
    # The worker's replay: a copy of the old signed `member`, under a new id.
    FakeFirestore.collection(db, memberships.COLLECTION).document().set(dict(granted))
    auth, _ = build(store, key)
    assert auth.authenticate("Bearer t-lee").tenant_choices == ()
    assert auth.is_tenant_member(LEE, group_tenant(), fresh=True) is False


def test_a_service_account_reads_no_membership(store, db, key):
    asked = db.asked
    auth, _ = build(store, key, allowed_users=(SVC,))
    ctx = auth.authenticate("Bearer t-svc")
    assert ctx.platform_memberships == ()
    assert auth.is_tenant_member(SVC, group_tenant(), fresh=True) is False
    assert db.asked == asked


def test_a_membership_never_widens_to_another_tenant(store, key):
    join(store)
    auth, _ = build(store, key)
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-lee", None, tenant="research")
    assert auth.is_tenant_member(LEE, group_tenant("research", RESEARCH_GROUP),
                                 fresh=True) is False


# -- a failed read ----------------------------------------------------------------

def test_a_failed_read_503s_a_header_naming_that_tenant(store, db, key):
    join(store)
    db.broken = True
    auth, _ = build(store, key)
    with pytest.raises(MembershipUnavailable) as caught:
        auth.authenticate("Bearer t-lee", None, tenant="eng")
    assert caught.value.status_code == 503
    assert caught.value.code == "membership_unavailable"
    # Any non-`u-` id answers the same, so the outage names no tenant id.
    with pytest.raises(MembershipUnavailable):
        auth.authenticate("Bearer t-lee", None, tenant="no-such-workspace")
    # A personal id is never a membership: still 403.
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-lee", None, tenant="u-someone")


def test_a_failed_read_leaves_a_group_member_untouched(store, db, key):
    join(store, subject=ALICE)
    db.broken = True
    auth, _ = build(store, key)
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.tenant_choices == (("research", RESEARCH_GROUP),)
    assert ctx.tenant_id == "research"
    assert ctx.memberships_unread is True
    picked = auth.authenticate("Bearer t-alice", None, tenant="research")
    assert picked.tenant_id == "research"
    entries = tenant_routes.get_mine(auth=ctx, ctx=SimpleNamespace(roles=None))
    assert [e["tenant_id"] for e in entries] == ["research"]


def test_a_failed_fresh_read_is_a_503_never_a_no(store, db, key):
    join(store)
    db.broken = True
    auth, _ = build(store, key)
    with pytest.raises(MembershipUnavailable):
        auth.is_tenant_member(LEE, group_tenant(), fresh=True)
    # A group member never needs the read.
    assert auth.is_tenant_member(ALICE, group_tenant("research", RESEARCH_GROUP),
                                 fresh=True) is True


# -- is_tenant_member and may_act_in ------------------------------------------------

def test_is_tenant_member_fresh_reads_past_the_cache(db, key):
    settings = api_settings(settings_key=key, role_cache_seconds=15.0)
    ours = MembershipStore(db, settings, now=Clock())
    theirs = MembershipStore(db, settings, now=Clock())  # another instance
    join(theirs)
    auth, _ = build(ours, key)
    assert auth.is_tenant_member(LEE, group_tenant()) is True
    assert auth.may_act_in(LEE, group_tenant()) == "member"

    join(theirs, state=memberships.REMOVED)
    # Within the cache window the ordinary path may still say yes ...
    assert auth.is_tenant_member(LEE, group_tenant()) is True
    # ... and a decision path, reading fresh, already says no.
    assert auth.is_tenant_member(LEE, group_tenant(), fresh=True) is False
    assert auth.may_act_in(LEE, group_tenant(), fresh=True) == ""


# -- no Cloud Identity call is added --------------------------------------------------

def test_no_cloud_identity_call_is_added(store, key):
    join(store)
    join(store, subject=ALICE)
    # (token, header with the switch on) against the same token's request with
    # it off, headerless: a membership costs no directory call.
    for token, header in (("Bearer t-lee", None), ("Bearer t-lee", "eng"),
                          ("Bearer t-alice", None), ("Bearer t-alice", "eng"),
                          ("Bearer t-alice", "research")):
        off, off_session = build(store, key, on=False)
        on, on_session = build(store, key)
        off.authenticate(token)
        on.authenticate(token, None, tenant=header)
        assert on_session.calls == off_session.calls, (token, header)
    # And is_tenant_member asks the one group, once, as before.
    on, on_session = build(store, key)
    assert on.is_tenant_member(LEE, group_tenant(), fresh=True) is True
    assert len([c for c in on_session.calls
                if c["url"].endswith(":checkTransitiveMembership")]) == 1


# -- the join rate limits are settings (BGD, 2026-10-25) -------------------------------

def test_the_join_limits_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    defaults = ApiSettings.from_env()
    assert (defaults.join_open_per_person, defaults.join_daily_per_person,
            defaults.join_denied_hold_days, defaults.join_open_per_workspace) == (
        joinrequests.OPEN_PER_PERSON, joinrequests.DAILY_PER_PERSON,
        joinrequests.DENIED_HOLD_DAYS, joinrequests.OPEN_PER_WORKSPACE)
    monkeypatch.setenv("JOIN_OPEN_PER_PERSON", "2")
    monkeypatch.setenv("JOIN_DAILY_PER_PERSON", "4")
    monkeypatch.setenv("JOIN_DENIED_HOLD_DAYS", "3")
    monkeypatch.setenv("JOIN_OPEN_PER_WORKSPACE", "5")
    read = ApiSettings.from_env()
    assert (read.join_open_per_person, read.join_daily_per_person,
            read.join_denied_hold_days, read.join_open_per_workspace) == (2, 4, 3, 5)
    monkeypatch.setenv("JOIN_OPEN_PER_PERSON", "0")
    with pytest.raises(ValueError, match="JOIN_OPEN_PER_PERSON must be 1 or more"):
        ApiSettings.from_env()


def _join_store(key: str, clock: Clock, **limits: Any) -> JoinStore:
    return JoinStore(FakeFirestore(), api_settings(settings_key=key, **limits), now=clock)


def _ask(store: JoinStore, requester: str = LEE, tenant_id: str = "eng",
         group: str = ENG_GROUP) -> joinrequests.RequestView:
    view, _ = store.ask(tenant_id=tenant_id, group=group, requester=requester)
    return view


def test_join_open_per_person_is_read_by_the_store(key):
    store = _join_store(key, Clock(), join_open_per_person=1)
    _ask(store)
    with pytest.raises(JoinRequestsOpenLimit):
        _ask(store, tenant_id="research", group=RESEARCH_GROUP)


def test_join_daily_per_person_is_read_by_the_store(key):
    clock = Clock()
    store = _join_store(key, clock, join_daily_per_person=2)
    for i in range(2):
        ref = _ask(store, tenant_id=f"ws{i}", group=f"ws{i}@example.com").request.ref
        store.decide(ref, state=joinrequests.WITHDRAWN, by=LEE, via=joinrequests.VIA_REQUESTER)
        clock.at += timedelta(minutes=1)
    with pytest.raises(JoinRequestsDailyLimit):
        _ask(store, tenant_id="ws9", group="ws9@example.com")


def test_join_denied_hold_days_is_read_by_the_store(key):
    clock = Clock()
    store = _join_store(key, clock, join_denied_hold_days=2)
    ref = _ask(store).request.ref
    store.decide(ref, state=joinrequests.DENIED, by="alex@example.com",
                 via=joinrequests.VIA_WORKSPACE_ADMIN, reason="not on this team")
    denied = clock.at
    clock.at = denied + timedelta(days=2, microseconds=-1)
    with pytest.raises(JoinDeniedRecently):
        _ask(store)
    clock.at = denied + timedelta(days=2)
    assert _ask(store).state == joinrequests.REQUESTED


def test_join_open_per_workspace_is_read_by_the_store(key):
    store = _join_store(key, Clock(), join_open_per_workspace=2)
    _ask(store, requester="p0@example.com")
    _ask(store, requester="p1@example.com")
    with pytest.raises(JoinWorkspaceQueueFull):
        _ask(store, requester="p2@example.com")


def test_a_store_over_settings_without_the_limits_keeps_the_constants(key):
    settings = SimpleNamespace(settings_key=key, settings_key_previous="")
    store = JoinStore(FakeFirestore(), settings, now=Clock())
    for i in range(joinrequests.OPEN_PER_PERSON):
        _ask(store, tenant_id=f"ws{i}", group=f"ws{i}@example.com")
    with pytest.raises(JoinRequestsOpenLimit):
        _ask(store, tenant_id="ws9", group="ws9@example.com")


# -- an unread membership never decides a headerless request's workspace ------------

class Prefs:
    """`Preferences`: the stored default workspace, per email."""

    def __init__(self, **defaults: str) -> None:
        self.defaults = defaults

    def cached(self, email: str) -> SimpleNamespace:
        name = email.split("@", 1)[0]
        return SimpleNamespace(default_workspace=self.defaults.get(name, ""), display_name="")


def _build_with_prefs(store: MembershipStore, key: str, prefs: Prefs,
                      ) -> tuple[Authenticator, FakeSession]:
    session = FakeSession({g: set(m) for g, m in GOOGLE.items()})
    auth = Authenticator(
        api_settings(platform_memberships=True, settings_key=key),
        StaticTokenVerifier(claims()),
        CloudIdentityGroups(PROJECT, session=session),
        preferences=prefs,  # type: ignore[arg-type]
        memberships=store,
    )
    return auth, session


def test_a_stored_default_the_unread_memberships_may_hold_is_never_guessed(store, db, key):
    from swarm_api.auth import PREFERENCE_FREE_ROUTES, require_known_default

    join(store, subject=ALICE)
    auth, _ = _build_with_prefs(store, key, Prefs(alice="eng"))
    # The control: read, the stored default applies.
    assert auth.authenticate("Bearer t-alice").tenant_id == "eng"

    db.broken = True
    store._cache.clear()
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.default_unknown is True
    assert ctx.default_ignored == ""
    with pytest.raises(MembershipUnavailable):
        require_known_default(ctx, ("POST", "/v1/tasks"))
    # The switcher's own routes are still served, and a named workspace is.
    for route in PREFERENCE_FREE_ROUTES:
        require_known_default(ctx, route)
    named = auth.authenticate("Bearer t-alice", None, tenant="research")
    assert named.tenant_id == "research" and named.default_unknown is False


def test_with_no_other_choice_an_unread_membership_leaves_the_default_unknown(store, db, key):
    from swarm_api.auth import require_known_default

    join(store)
    db.broken = True
    auth, _ = build(store, key)
    ctx = auth.authenticate("Bearer t-lee")
    assert ctx.default_unknown is True
    with pytest.raises(MembershipUnavailable):
        require_known_default(ctx, ("POST", "/v1/tasks"))


def test_an_unread_membership_leaves_a_group_members_default_known(store, db, key):
    from swarm_api.auth import require_known_default

    db.broken = True
    auth, _ = _build_with_prefs(store, key, Prefs())
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.default_unknown is False and ctx.tenant_id == "research"
    require_known_default(ctx, ("POST", "/v1/tasks"))


def test_a_read_cut_short_by_a_flood_is_unknown_not_a_no(store, db, key):
    join(store)
    # A worker floods lee's subject with unsigned grants dated after the
    # valid one, pushing it past the bounded read.
    for i in range(memberships.SCAN_LIMIT):
        FakeFirestore.collection(db, memberships.COLLECTION).document().set({
            "tenant_id": f"junk{i}", "group": ENG_GROUP, "subject": LEE, "state": "member",
            "by": "x@example.com", "at": f"2027-01-01T00:00:{i % 60:02d}.{i:06d}Z",
            "via": "join_request", "reason": "",
            "mac": {"v": 1, "key": "k", "sig": secrets.token_urlsafe(32)}})
    auth, _ = build(store, key)
    with pytest.raises(MembershipUnavailable):
        auth.authenticate("Bearer t-lee", None, tenant="eng")
    # Denial of service of the membership alone: a group member is untouched.
    assert auth.authenticate("Bearer t-alice").tenant_id == "research"
