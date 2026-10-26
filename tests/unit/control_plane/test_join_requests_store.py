"""Join requests, their decisions and a workspace's join link: signed,
append-only records that admit nobody by themselves
(docs/design/join-requests.md §4.1, §4.2, §4.3, §4.7; lane JOIN-STORE, part
of #2325).

The attacks are a tenant worker's: create a document under a new id, or
update one whose id it holds. None may make an admin approve someone they did
not see, show a decision nobody made, or turn on a link nobody turned on.
Every code and hash is made at runtime.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import joinrequests, memberships
from swarm_api.errors import (
    JoinDeniedRecently,
    JoinLinkUnknown,
    JoinNotOffered,
    JoinRequestDecided,
    JoinRequestInvalid,
    JoinRequestsDailyLimit,
    JoinRequestsOpenLimit,
    JoinRequestUnknown,
    JoinRoleNotAllowed,
    JoinWorkspaceQueueFull,
    ValidationFailed,
)
from swarm_api.joinrequests import JoinStore
from swarm_api.metrics import ApiMetrics

from .conftest import ENG_GROUP, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore

T0 = datetime(2026, 10, 25, 6, 41, 9, 512000, tzinfo=timezone.utc)
LEE = "lee@example.com"
ALEX = "alex@example.com"
REGISTERED = {"eng": ENG_GROUP, "research": RESEARCH_GROUP}


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def metrics() -> ApiMetrics:
    return ApiMetrics()


@pytest.fixture
def signing_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def store(db, clock, metrics, signing_key) -> JoinStore:
    return JoinStore(db, api_settings(settings_key=signing_key), metrics=metrics, now=clock)


def ask(store: JoinStore, requester: str = LEE, tenant_id: str = "eng",
        group: str = ENG_GROUP, message: str | None = None) -> joinrequests.RequestView:
    view, _created = store.ask(tenant_id=tenant_id, group=group, requester=requester,
                               message=message)
    return view


def approve(store: JoinStore, ref: str, role: str = "member", **kwargs: Any):
    return store.decide(ref, state=joinrequests.APPROVED, by=ALEX,
                        via=joinrequests.VIA_WORKSPACE_ADMIN, role=role, **kwargs)


def request_docs(db: FakeFirestore) -> dict[str, dict]:
    return {p: d for p, d in db.dump(f"{joinrequests.REQUESTS}/").items()
            if p.count("/") == 1}


def decision_docs(db: FakeFirestore) -> dict[str, dict]:
    return {p: d for p, d in db.dump(f"{joinrequests.REQUESTS}/").items()
            if f"/{joinrequests.DECISIONS}/" in p}


def audit(db: FakeFirestore, action: str) -> list[dict]:
    return [r for r in db.collection_docs("admin_audit") if r["action"] == action]


def doc_path_for(db: FakeFirestore, ref: str) -> str:
    (path,) = [p for p, d in request_docs(db).items() if d.get("ref") == ref]
    return path


def worker_create(db: FakeFirestore, collection: str, doc: dict) -> str:
    ref = FakeFirestore.collection(db, collection).document()
    ref.set(doc)
    return ref.path


# -- a request -----------------------------------------------------------------

def test_a_request_is_signed_open_for_14_days_and_audited_without_its_message(store, db):
    view = ask(store, message="I'm on the labs rota from Monday")
    assert view.state == joinrequests.REQUESTED
    assert view.request.ref.startswith("j-") and len(view.request.ref) == 14
    (path, doc) = next(iter(request_docs(db).items()))
    # The handle is not the document id: knowing it overwrites nothing.
    assert path.rsplit("/", 1)[1] != view.request.ref
    assert doc["requester"] == LEE and doc["group"] == ENG_GROUP
    assert doc["requested_at"] == "2026-10-25T06:41:09.512000Z"
    assert doc["expires_at"] == "2026-11-08T06:41:09.512000Z"
    assert joinrequests.request_verifies((store_key(store),), doc)
    (row,) = audit(db, joinrequests.AUDIT_REQUESTED)
    assert row["detail"] == {"ref": view.request.ref, "via": "console"}
    assert "rota" not in repr(row)


def store_key(store: JoinStore) -> str:
    return store._keys()[0]


def test_a_second_identical_ask_returns_the_open_one_and_writes_nothing(store, db):
    first = ask(store)
    before = db.dump()
    again, created = store.ask(tenant_id="eng", group=ENG_GROUP, requester=LEE)
    assert created is False
    assert again.request.ref == first.request.ref
    assert db.dump() == before


def test_a_message_is_bounded_and_redacted(store, db):
    with pytest.raises(ValidationFailed):
        ask(store, message="x" * (joinrequests.MESSAGE_MAX + 1))
    token = "ghp_" + secrets.token_hex(18)
    view = ask(store, message=f"my token is {token}")
    assert token not in view.request.message
    assert token not in repr(db.dump())


def test_expiry_is_derived_at_14_days(store, clock):
    ref = ask(store).request.ref
    clock.tick(days=14, microseconds=-1)
    assert store.view(ref).state == joinrequests.REQUESTED
    clock.tick(microseconds=1)
    assert store.view(ref).state == joinrequests.EXPIRED
    with pytest.raises(JoinRequestDecided):
        approve(store, ref)
    # Expired, the person may ask again, and that is a new request.
    assert ask(store).request.ref != ref


def test_state_of_is_the_newest_valid_decision():
    request = joinrequests.JoinRequest(
        doc_id="d", ref="j-000000000001", tenant_id="eng", group=ENG_GROUP, requester=LEE,
        requested_at="2026-10-25T06:41:09.512000Z", expires_at="2026-11-08T06:41:09.512000Z")
    older = joinrequests.Decision(ref=request.ref, tenant_id="eng", requester=LEE,
                                  state="denied", by=ALEX, at="2026-10-26T00:00:00.000000Z",
                                  via="workspace_admin", reason="no")
    newer = joinrequests.Decision(ref=request.ref, tenant_id="eng", requester=LEE,
                                  state="approved", by=ALEX, at="2026-10-27T00:00:00.000000Z",
                                  via="workspace_admin", role="member")
    late = T0 + timedelta(days=30)
    assert joinrequests.state_of(request, [newer, older], late) == ("approved", newer)
    assert joinrequests.state_of(request, [], late) == ("expired", None)
    assert joinrequests.state_of(request, [], T0) == ("requested", None)


# -- what a worker can write ---------------------------------------------------

def test_a_request_whose_requester_was_edited_after_signing_is_invalid_on_approval(
        store, db, metrics):
    ref = ask(store).request.ref
    db.document(doc_path_for(db, ref)).update({"requester": "mallory@example.com"})
    before = db.dump()
    with pytest.raises(JoinRequestInvalid) as caught:
        approve(store, ref)
    assert caught.value.status_code == 409
    assert db.dump() == before
    assert metrics.registry.get_sample_value(
        "swarm_api_join_request_invalid_total", {"reason": "mac"}) >= 1.0
    # The admin's list does not show it either.
    assert store.open_for(["eng"]) == []


def test_a_forged_approval_is_skipped(store, db):
    ref = ask(store).request.ref
    path = doc_path_for(db, ref)
    worker_create(db, f"{path}/{joinrequests.DECISIONS}", {
        "ref": ref, "tenant_id": "eng", "requester": LEE, "state": "approved",
        "by": ALEX, "at": "2026-10-25T07:00:00.000000Z", "via": "workspace_admin",
        "role": "member", "mac": {"v": 1, "key": "k", "sig": secrets.token_hex(16)}})
    assert store.view(ref).state == joinrequests.REQUESTED


def test_a_forged_unsigned_request_is_invisible_and_cannot_be_decided(store, db):
    worker_create(db, joinrequests.REQUESTS, {
        "ref": "j-" + secrets.token_hex(6), "tenant_id": "eng", "group": ENG_GROUP,
        "requester": LEE, "requested_at": "2026-10-25T06:41:09.512000Z",
        "expires_at": "2026-11-08T06:41:09.512000Z"})
    assert store.open_for(["eng"]) == []
    assert store.mine(LEE) == []
    forged_ref = next(iter(request_docs(db).values()))["ref"]
    with pytest.raises(JoinRequestInvalid):
        approve(store, forged_ref)


# -- refused at write, skipped at read -----------------------------------------

def test_a_personal_workspace_is_refused_at_write(store, db):
    with pytest.raises(JoinNotOffered):
        ask(store, tenant_id="u-kim", group="kim@example.com")
    assert request_docs(db) == {}


@pytest.mark.parametrize("requester", [
    "builder@my-project.iam.gserviceaccount.com", "not-an-email",
])
def test_a_service_account_requester_is_refused_at_write(store, db, requester):
    with pytest.raises(ValidationFailed):
        ask(store, requester=requester)
    assert request_docs(db) == {}


def test_a_signed_personal_or_service_account_request_is_skipped_at_read(store, db):
    keys = store._keys()
    service = "builder@my-project.iam.gserviceaccount.com"
    for tenant_id, requester in (("u-kim", LEE), ("eng", service)):
        fields = {"ref": "j-" + secrets.token_hex(6), "tenant_id": tenant_id,
                  "group": ENG_GROUP, "requester": requester,
                  "requested_at": "2026-10-25T06:41:09.512000Z",
                  "expires_at": "2026-11-08T06:41:09.512000Z"}
        worker_create(db, joinrequests.REQUESTS,
                      dict(fields, mac=joinrequests.sign_request(keys, fields)))
    assert store.open_for(None) == []
    assert store.mine(LEE) == []
    assert store.mine(service) == []


# -- the rate limits (§4.1), each at its edge and not one below ----------------

def test_open_requests_per_person_refuse_the_fourth_not_the_third(store):
    limit = joinrequests.OPEN_PER_PERSON
    assert limit == 3
    for i in range(limit):
        ask(store, tenant_id=f"ws{i}", group=f"ws{i}@example.com")
    with pytest.raises(JoinRequestsOpenLimit) as caught:
        ask(store, tenant_id="ws9", group="ws9@example.com")
    assert caught.value.status_code == 429
    assert caught.value.code == "join_requests_open_limit"


def test_requests_per_day_refuse_the_eleventh_not_the_tenth(store, clock):
    limit = joinrequests.DAILY_PER_PERSON
    assert limit == 10
    for i in range(limit):
        ref = ask(store, tenant_id=f"ws{i}", group=f"ws{i}@example.com").request.ref
        # Withdrawn, so the open limit never decides this test.
        store.decide(ref, state=joinrequests.WITHDRAWN, by=LEE, via=joinrequests.VIA_REQUESTER)
        clock.tick(minutes=1)
    with pytest.raises(JoinRequestsDailyLimit) as caught:
        ask(store, tenant_id="ws99", group="ws99@example.com")
    assert caught.value.code == "join_requests_daily_limit"
    # A day after the first, the window has moved on.
    clock.at = T0 + timedelta(days=1)
    assert ask(store, tenant_id="ws99", group="ws99@example.com").state == "requested"


def test_a_denial_holds_a_new_request_for_7_days_to_the_microsecond(store, clock):
    ref = ask(store).request.ref
    clock.tick(hours=1)
    store.decide(ref, state=joinrequests.DENIED, by=ALEX, via=joinrequests.VIA_WORKSPACE_ADMIN,
                 reason="not on this team")
    denied_at = clock.at
    clock.at = denied_at + timedelta(days=7, microseconds=-1)
    with pytest.raises(JoinDeniedRecently) as caught:
        ask(store)
    assert caught.value.code == "join_denied_recently"
    assert caught.value.detail["opens_on"] == "2026-11-01"
    # Another workspace is not held.
    ask(store, tenant_id="research", group=RESEARCH_GROUP)
    clock.at = denied_at + timedelta(days=7)
    assert ask(store).state == joinrequests.REQUESTED


def test_open_requests_per_workspace_refuse_the_51st_not_the_50th(store):
    limit = joinrequests.OPEN_PER_WORKSPACE
    assert limit == 50
    for i in range(limit):
        ask(store, requester=f"p{i}@example.com")
    with pytest.raises(JoinWorkspaceQueueFull) as caught:
        ask(store, requester="late@example.com")
    assert caught.value.code == "join_workspace_queue_full"
    assert len(store.open_for(["eng"])) == limit


# -- deciding ------------------------------------------------------------------

def test_approval_writes_a_signed_decision_and_its_audit_row(store, db):
    ref = ask(store).request.ref
    view, decision = approve(store, ref, role="viewer", admitted=True)
    assert view.state == joinrequests.APPROVED and decision.role == "viewer"
    (doc,) = decision_docs(db).values()
    assert joinrequests.decision_verifies(store._keys(), doc)
    (row,) = audit(db, joinrequests.AUDIT_APPROVED)
    assert row["detail"] == {"ref": ref, "role": "viewer", "admitted": True,
                             "via": "workspace_admin"}
    assert row["target_email"] == LEE and row["target_tenant_id"] == "eng"
    with pytest.raises(JoinRequestDecided):
        approve(store, ref)


def test_admin_is_never_offered_on_approval(store, db):
    ref = ask(store).request.ref
    with pytest.raises(JoinRoleNotAllowed) as caught:
        approve(store, ref, role="admin")
    assert caught.value.status_code == 409
    assert decision_docs(db) == {}


def test_a_denial_needs_a_reason_which_is_redacted(store, db):
    ref = ask(store).request.ref
    with pytest.raises(ValidationFailed):
        store.decide(ref, state=joinrequests.DENIED, by=ALEX,
                     via=joinrequests.VIA_WORKSPACE_ADMIN, reason="  ")
    token = "ghp_" + secrets.token_hex(18)
    _view, decision = store.decide(ref, state=joinrequests.DENIED, by=ALEX,
                                   via=joinrequests.VIA_PLATFORM_ADMIN,
                                   reason=f"pasted {token}")
    assert token not in decision.reason
    (row,) = audit(db, joinrequests.AUDIT_DENIED)
    assert row["detail"] == {"ref": ref, "via": "platform_admin"}


def test_only_the_requester_withdraws_and_anyone_else_reads_unknown(store, db):
    ref = ask(store).request.ref
    with pytest.raises(JoinRequestUnknown) as caught:
        store.decide(ref, state=joinrequests.WITHDRAWN, by="kim@example.com",
                     via=joinrequests.VIA_REQUESTER)
    assert caught.value.status_code == 404
    store.decide(ref, state=joinrequests.WITHDRAWN, by=LEE, via=joinrequests.VIA_REQUESTER)
    assert store.view(ref).state == joinrequests.WITHDRAWN
    assert len(audit(db, joinrequests.AUDIT_WITHDRAWN)) == 1


def test_an_unknown_ref_is_join_request_unknown(store):
    with pytest.raises(JoinRequestUnknown):
        approve(store, "j-" + secrets.token_hex(6))


def test_the_membership_lands_in_the_decisions_transaction_or_nothing_does(
        store, db, clock, signing_key):
    members = memberships.MembershipStore(db, api_settings(settings_key=signing_key), now=clock)
    ref = ask(store).request.ref

    def admit(writer, request, decision):
        members.write(tenant_id=request.tenant_id, group=request.group,
                      subject=request.requester, state=memberships.MEMBER, by=decision.by,
                      via=memberships.VIA_JOIN_REQUEST, detail={"ref": request.ref},
                      writer=writer)
        raise RuntimeError("injected after the membership")

    before = db.dump()
    with pytest.raises(RuntimeError):
        approve(store, ref, also=admit)
    assert db.dump() == before

    def admit_ok(writer, request, decision):
        members.write(tenant_id=request.tenant_id, group=request.group,
                      subject=request.requester, state=memberships.MEMBER, by=decision.by,
                      via=memberships.VIA_JOIN_REQUEST, detail={"ref": request.ref},
                      writer=writer)

    approve(store, ref, also=admit_ok)
    assert members.is_member(LEE, "eng", REGISTERED, fresh=True) is True
    assert len(audit(db, memberships.AUDIT_ADDED)) == 1


# -- reads for the routes ------------------------------------------------------

def test_mine_lists_each_state_newest_first(store, clock):
    a = ask(store).request.ref
    clock.tick(seconds=1)
    b = ask(store, tenant_id="research", group=RESEARCH_GROUP).request.ref
    store.decide(b, state=joinrequests.DENIED, by=ALEX, via="workspace_admin", reason="no")
    views = store.mine(LEE)
    assert [(v.request.ref, v.state) for v in views] == [(b, "denied"), (a, "requested")]
    api = views[0].to_api()
    assert api["reason"] == "no" and "doc_id" not in api and "decided_at" in api
    assert all(views[0].request.doc_id not in str(value) for value in api.values())


def test_open_for_scopes_to_the_workspaces_named(store):
    ask(store)
    ask(store, requester="kim@example.com", tenant_id="research", group=RESEARCH_GROUP)
    assert {v.request.tenant_id for v in store.open_for(["eng"])} == {"eng"}
    assert {v.request.tenant_id for v in store.open_for(None)} == {"eng", "research"}
    assert store.open_for([]) == []


def test_a_decided_request_is_kept_90_days_then_pruned(store, db, clock):
    ref = ask(store).request.ref
    clock.tick(days=2)
    store.decide(ref, state=joinrequests.DENIED, by=ALEX, via="workspace_admin", reason="no")
    decided = clock.at
    open_ref = ask(store, requester="kim@example.com").request.ref
    clock.at = decided + timedelta(days=90, microseconds=-1)
    assert store.prune() == 0
    clock.at = decided + timedelta(days=90)
    assert store.prune() == 1
    assert [d["ref"] for d in request_docs(db).values()] == [open_ref]
    assert decision_docs(db) == {}
    # The audit rows are the permanent record.
    assert len(audit(db, joinrequests.AUDIT_DENIED)) == 1


# -- the join link (§4.2) ------------------------------------------------------

def test_a_link_turned_on_shows_its_code_once_and_stores_only_its_hash(store, db):
    assert store.link("eng").on is False
    state, code = store.set_link(tenant_id="eng", group=ENG_GROUP, on=True, rotate=False, by=ALEX)
    assert state.on is True and isinstance(code, str) and len(code) >= 22
    assert code not in repr(db.dump())
    doc = db.dump(f"{joinrequests.LINKS}/")[f"{joinrequests.LINKS}/eng"]
    assert doc["code_sha256"] == hashlib.sha256(code.encode()).hexdigest()
    assert store.lookup(code, REGISTERED) == ("eng", ENG_GROUP)
    # Saving again without rotating shows nothing.
    _state, again = store.set_link(tenant_id="eng", group=ENG_GROUP, on=True, rotate=False,
                                   by=ALEX)
    assert again is None
    rows = audit(db, joinrequests.AUDIT_LINK_CHANGED)
    assert sorted((r["detail"]["on"], r["detail"]["rotated"]) for r in rows) == [
        (True, False), (True, True)]
    assert doc["code_sha256"] not in repr(audit(db, joinrequests.AUDIT_LINK_CHANGED))


def _same_refusal(store: JoinStore, code: Any, registered: dict) -> dict:
    with pytest.raises(JoinLinkUnknown) as caught:
        store.lookup(code, registered)
    assert caught.value.status_code == 404
    return caught.value.to_payload()


def test_unknown_rotated_off_and_regrouped_codes_answer_identically(store, db):
    _state, old = store.set_link(tenant_id="eng", group=ENG_GROUP, on=True, rotate=False, by=ALEX)
    _state, new = store.set_link(tenant_id="eng", group=ENG_GROUP, on=True, rotate=True, by=ALEX)
    assert new and new != old
    unknown = _same_refusal(store, secrets.token_urlsafe(16), REGISTERED)
    assert _same_refusal(store, old, REGISTERED) == unknown
    assert _same_refusal(store, new, {"eng": "eng-new@example.com"}) == unknown
    assert _same_refusal(store, None, REGISTERED) == unknown
    store.set_link(tenant_id="eng", group=ENG_GROUP, on=False, rotate=False, by=ALEX)
    assert _same_refusal(store, new, REGISTERED) == unknown


def test_an_overwritten_link_reads_off_and_admits_no_request(store, db):
    _state, code = store.set_link(tenant_id="eng", group=ENG_GROUP, on=True, rotate=False,
                                  by=ALEX)
    forged = secrets.token_urlsafe(16)
    db.document(f"{joinrequests.LINKS}/eng").update(
        {"code_sha256": hashlib.sha256(forged.encode()).hexdigest()})
    assert store.link("eng").on is False
    _same_refusal(store, forged, REGISTERED)
    _same_refusal(store, code, REGISTERED)


def test_a_personal_workspace_has_no_link(store, db):
    with pytest.raises(JoinNotOffered):
        store.set_link(tenant_id="u-lee", group=LEE, on=True, rotate=False, by=LEE)
    assert db.dump(f"{joinrequests.LINKS}/") == {}


# -- one domain per record -----------------------------------------------------

SIGNERS = {
    "request": (joinrequests.sign_request, joinrequests.request_verifies),
    "decision": (joinrequests.sign_decision, joinrequests.decision_verifies),
    "link": (joinrequests.sign_link, joinrequests.link_verifies),
    "membership": (memberships.sign_membership, memberships.membership_verifies),
}


def _union_doc() -> dict[str, Any]:
    """One document carrying every record's signed fields, so a MAC made for
    one record could only fail as another because of its domain or fields."""
    return {
        "ref": "j-" + secrets.token_hex(6), "tenant_id": "eng", "group": ENG_GROUP,
        "requester": LEE, "subject": LEE, "requested_at": "2026-10-25T06:41:09.512000Z",
        "expires_at": "2026-11-08T06:41:09.512000Z", "state": "member", "by": ALEX,
        "at": "2026-10-25T07:00:00.000000Z", "via": "workspace_admin", "role": None,
        "reason": "", "code_sha256": hashlib.sha256(secrets.token_bytes(16)).hexdigest(),
        "on": True, "set_by": ALEX, "set_at": "2026-10-25T07:00:00.000000Z",
    }


@pytest.mark.parametrize("made", sorted(SIGNERS))
@pytest.mark.parametrize("read_as", sorted(SIGNERS))
def test_a_record_signed_under_one_domain_verifies_under_no_other(made, read_as):
    keys = (secrets.token_hex(32),)
    doc = _union_doc()
    signed = dict(doc, mac=SIGNERS[made][0](keys, doc))
    assert SIGNERS[read_as][1](keys, signed) is (made == read_as)


def test_the_four_domain_strings_are_distinct_and_versioned():
    domains = [joinrequests.REQUEST_DOMAIN, joinrequests.DECISION_DOMAIN,
               joinrequests.LINK_DOMAIN, memberships.DOMAIN]
    assert len(set(domains)) == 4
    assert all(d.endswith(b"/v1\n") for d in domains)
    # The same fields under another domain do not verify: the domain, not
    # only the field list, separates them.
    keys = (secrets.token_hex(32),)
    doc = _union_doc()
    mac = memberships.sign_record(keys, joinrequests.REQUEST_DOMAIN, doc,
                                  memberships.SIGNED_FIELDS)
    assert memberships.membership_verifies(keys, dict(doc, mac=mac)) is False


def test_a_rotated_settings_key_still_verifies_old_records(db, clock):
    old, new = secrets.token_hex(32), secrets.token_hex(32)
    before = JoinStore(db, api_settings(settings_key=old), now=clock)
    ref = ask(before).request.ref
    after = JoinStore(db, api_settings(settings_key=new, settings_key_previous=old), now=clock)
    assert after.view(ref).state == joinrequests.REQUESTED
