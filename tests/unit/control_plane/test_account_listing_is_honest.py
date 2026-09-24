"""The account listing must not describe a pool that is not there.

THE COMPLAINT THIS ANSWERS: "account quotas are not even accurate in the UI."

Three separate ways the listing said something that was not so, all of them
silent, and all of them arriving at the same place -- a screen full of
confident percentages computed over the wrong set of accounts.

  1. AN ACCOUNT THE POOL IS ALREADY REFUSING RENDERED AS THE HEALTHIEST ROW.
     `accounts.choose()` skips an account a tenant has reported it cannot read
     the secret of -- for that tenant, and for nobody else. Nothing spends such
     an account, so its windows stay wide open and it sorts to the top of every
     "most headroom" figure on the screen. The broker served `unreadable_by` to
     stop exactly that, but `unreadable_by` carries no ages and the rule is
     time-limited (`Account.is_unreadable_for`, thirty minutes), so a reader
     given only that cannot tell a five-minute onboarding blip from a report
     three days stale. `unreadable_now` is the verdict itself, taken with the
     server's clock, and these tests pin it to `choose()` rather than to a
     second copy of the rule.

  2. A DOCUMENT THE STORE COULD NOT PARSE VANISHED WITHOUT A TRACE.
     `AccountStore.list()` skips it and logs a warning, which is right -- one
     bad document must not hide the fleet -- but the caller got a shorter list
     with nothing to say it was short. Five accounts render as four, "4
     accounts registered", and a pool headroom taken over four.

  3. swarm-api REBUILDS THE PAYLOAD, so a field the broker serves and that
     route does not name is dropped between them. Both fields above travel
     through it.

Nothing here needs a credential, a network or an emulator: the store runs
against the fake Firestore, the broker routes run under TestClient, and the
browser's half is read as text the way `test_blocker_ui_surface.py` and
`test_runtimes_screen.py` read theirs -- a mock of the UI would agree with
whatever the UI happens to do.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import DEFAULT_STALE_AFTER
from quota_broker.accountstore import AccountStore
from quota_broker.main import account_to_api, create_app

ENG = "eng"
RESEARCH = "research"

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"


class _Identity:
    """A verified ID token, as `WorkerIdentity.resolve` returns one."""

    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)

    def as_tenant(self, tenant_id: str) -> None:
        self.tenant, self.platform = tenant_id, False

    def as_platform(self) -> None:
        self.tenant, self.platform = None, True


@pytest.fixture()
def accounts(broker) -> AccountStore:
    return AccountStore(broker.db)


@pytest.fixture()
def client(broker, accounts):
    identity = _Identity()
    app = create_app(broker, identity=identity, account_store=accounts)
    c = TestClient(app, raise_server_exceptions=False)
    c.identity = identity
    return c


def _report_unreadable(db, account_id: str, tenant_id: str, *, minutes_ago: float) -> None:
    """Record that `tenant_id` could not read this account's secret.

    Written straight onto the document rather than driven through assign and
    release, because the AGE of the report is the whole subject here and the
    release path can only ever write "now".
    """
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.document(f"accounts/{account_id}").update({"unreadable_by": {tenant_id: when}})


# --------------------------------------------------------------------------
# 1. The verdict, and the record it is not
# --------------------------------------------------------------------------


def test_a_live_report_is_served_as_a_verdict_and_not_only_as_a_record(
    client, accounts, broker
):
    """`unreadable_now` names the tenants the pool is refusing right now.

    Both fields are present and they say different things. A screen that reads
    only `unreadable_by` cannot tell whether the pool is acting on it.
    """
    accounts.register(ENG, "fresh")
    _report_unreadable(broker.db, f"{ENG}:fresh", ENG, minutes_ago=1)

    client.identity.as_platform()
    row = client.get("/v1/accounts").json()["accounts"][0]

    assert row["unreadable_by"] == [ENG]
    assert row["unreadable_now"] == [ENG]


def test_a_report_that_has_aged_out_leaves_the_verdict_and_stays_on_the_record(
    client, accounts, broker
):
    """THE CASE THE RECORD CANNOT EXPRESS AND THE BROWSER MUST NOT GUESS.

    `is_unreadable_for` forgets a report after DEFAULT_STALE_AFTER, on purpose:
    a freshly onboarded account gets its access token published on the next
    sweep, and a permanent mark would turn a five-minute window into an account
    that never came back. `unreadable_by` still carries the tenant -- it is a
    record and records are kept -- so a reader deriving the verdict from it
    would go on marking a working account as broken forever.
    """
    accounts.register(ENG, "fresh")
    _report_unreadable(
        broker.db,
        f"{ENG}:fresh",
        ENG,
        minutes_ago=DEFAULT_STALE_AFTER.total_seconds() / 60 + 5,
    )

    client.identity.as_platform()
    row = client.get("/v1/accounts").json()["accounts"][0]

    assert row["unreadable_by"] == [ENG], "the record is kept"
    assert row["unreadable_now"] == [], "and the pool is no longer acting on it"


def test_the_verdict_is_the_one_the_assignment_rule_acts_on(client, accounts, broker):
    """`unreadable_now` and `choose()` agree, in both directions.

    They are not two statements of the rule: `account_to_api` asks
    `Account.is_unreadable_for`, which is the predicate `choose()` filters on.
    This pins that they cannot be separated -- the account the listing says is
    being skipped is the account the assign route refuses to hand out, and the
    account whose report has aged out comes straight back.
    """
    accounts.register(ENG, "only")
    _report_unreadable(broker.db, f"{ENG}:only", ENG, minutes_ago=1)

    client.identity.as_tenant(ENG)
    refused = client.post("/v1/accounts/assign", json={"provider": "anthropic"}).json()
    assert refused.get("account_id") is None
    listed = client.get("/v1/accounts").json()["accounts"][0]
    assert listed["unreadable_now"] == [ENG]

    _report_unreadable(
        broker.db,
        f"{ENG}:only",
        ENG,
        minutes_ago=DEFAULT_STALE_AFTER.total_seconds() / 60 + 5,
    )
    assigned = client.post("/v1/accounts/assign", json={"provider": "anthropic"}).json()
    assert assigned["account_id"] == f"{ENG}:only"
    listed = client.get("/v1/accounts").json()["accounts"][0]
    assert listed["unreadable_now"] == []


def test_one_tenants_report_is_not_served_as_everybodys_problem(client, accounts, broker):
    """A borrower that was never granted secretAccessor has learned nothing
    about the owner, and the listing must not say otherwise -- the field would
    otherwise be a griefing tool with a UI attached."""
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    _report_unreadable(broker.db, f"{RESEARCH}:shared", ENG, minutes_ago=1)

    client.identity.as_platform()
    row = client.get("/v1/accounts").json()["accounts"][0]

    assert row["unreadable_now"] == [ENG]
    assert RESEARCH not in row["unreadable_now"]


def test_an_object_without_the_predicate_over_reports_rather_than_reassures():
    """The one direction this field is allowed to fail in.

    `account_to_api` takes `Any` and guards every field it reads. If an object
    carries the record but not `is_unreadable_for`, the answer is "all of them"
    -- a problem drawn that may have cleared -- and never "none of them", which
    would be the silence the field exists to break.
    """

    class _Bare:
        account_id = f"{ENG}:bare"
        owner_tenant = ENG
        label = "bare"
        provider = "anthropic"
        state = type("S", (), {"value": "AVAILABLE"})()
        reason = ""
        lend_to: tuple[str, ...] = ()
        assigned = 0
        windows: dict = {}
        observed_at = None
        unreadable_by = {ENG: datetime.now(timezone.utc)}

    assert account_to_api(_Bare()).get("unreadable_now") == [ENG]


# --------------------------------------------------------------------------
# 2. A document that could not be read is part of the answer
# --------------------------------------------------------------------------


def test_the_store_reports_which_documents_it_could_not_read(accounts, broker):
    """`list()` keeps its old contract; `list_reporting()` adds the shortfall.

    The skip itself is correct and is not what changed: one malformed document
    must not hide the fleet. What changed is that the caller can now say how
    many rows are missing, which is the difference between a short list and a
    short list nobody can see is short.
    """
    accounts.register(ENG, "good")
    # A document with no `label`: `Account.from_firestore` raises KeyError on it,
    # which is precisely the case `list()` swallows.
    broker.db.document(f"accounts/{ENG}:half-written").set(
        {"account_id": f"{ENG}:half-written", "owner_tenant": ENG}
    )

    listing = accounts.list_reporting()

    assert [a.account_id for a in listing.accounts] == [f"{ENG}:good"]
    assert listing.unreadable == [f"{ENG}:half-written"]
    assert [a.account_id for a in accounts.list()] == [f"{ENG}:good"]


def test_the_route_says_how_many_documents_it_could_not_read(client, accounts, broker):
    accounts.register(ENG, "good")
    broker.db.document(f"accounts/{ENG}:half-written").set(
        {"account_id": f"{ENG}:half-written", "owner_tenant": ENG}
    )

    client.identity.as_platform()
    body = client.get("/v1/accounts").json()

    assert len(body["accounts"]) == 1
    assert body["unreadable_document_count"] == 1
    assert body["unreadable_documents"] == [f"{ENG}:half-written"]


def test_a_borrower_learns_the_list_is_short_without_learning_whose(
    client, accounts, broker
):
    """Invariant 9 reaches this field too.

    A document id is `<owner_tenant>:<label>` by construction, so handing the
    whole list to every caller would name another tenant's accounts. The COUNT
    is not optional though: without it a borrower reads a short list as the
    whole pool, which is the bug, merely relocated.
    """
    accounts.register(ENG, "mine")
    broker.db.document(f"accounts/{RESEARCH}:half-written").set(
        {"account_id": f"{RESEARCH}:half-written", "owner_tenant": RESEARCH}
    )

    client.identity.as_tenant(ENG)
    body = client.get("/v1/accounts").json()

    assert body["unreadable_document_count"] == 1
    assert body["unreadable_documents"] == []

    client.identity.as_platform()
    assert client.get("/v1/accounts").json()["unreadable_documents"] == [
        f"{RESEARCH}:half-written"
    ]


def test_a_tenant_scoped_listing_still_narrows_to_what_it_may_run_on(
    client, accounts, broker
):
    """`list_reporting` replaced `for_tenant` in the route; `may_serve` did not
    move. An account another tenant owns and has not lent stays invisible."""
    accounts.register(ENG, "mine")
    accounts.register(RESEARCH, "theirs")
    accounts.register(RESEARCH, "lent", lend_to=[ENG])

    client.identity.as_tenant(ENG)
    ids = [a["account_id"] for a in client.get("/v1/accounts").json()["accounts"]]

    assert sorted(ids) == [f"{ENG}:mine", f"{RESEARCH}:lent"]


def test_a_healthy_pool_reports_no_shortfall(client, accounts):
    """The quiet case has to stay quiet, or the warning is noise within a day."""
    accounts.register(ENG, "one")
    accounts.register(ENG, "two")

    client.identity.as_platform()
    body = client.get("/v1/accounts").json()

    assert body["unreadable_document_count"] == 0
    assert body["unreadable_documents"] == []


# --------------------------------------------------------------------------
# 3. swarm-api rebuilds the payload, so it has to name these
# --------------------------------------------------------------------------


def _api_client(api_context, pool):
    from swarm_api.main import create_app as create_api

    app = create_api(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


class _Pool:
    """The broker's list route, as swarm-api's proxy sees it."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def list_accounts(self, tenant_id: str) -> dict:
        return dict(self._payload)


def test_the_api_forwards_the_shortfall_rather_than_dropping_it(api_context):
    """The proxy rebuilds `{accounts, tenant_id}` by hand, so anything the
    broker adds is lost here by default. This is the test that notices."""
    from .conftest import auth_header

    client = _api_client(
        api_context,
        _Pool(
            {
                "accounts": [],
                "tenant_id": ENG,
                "unreadable_documents": [f"{ENG}:half-written"],
                "unreadable_document_count": 3,
            }
        ),
    )

    body = client.get("/v1/accounts", headers=auth_header("alice")).json()

    assert body["unreadable_documents"] == [f"{ENG}:half-written"]
    assert body["unreadable_document_count"] == 3


def test_an_older_broker_that_serves_only_the_ids_is_not_read_as_healthy(api_context):
    """A 0 beside a non-empty list is the one shape that reads as "nothing
    wrong". The count falls back to the length of the list rather than to zero.
    """
    from .conftest import auth_header

    client = _api_client(
        api_context,
        _Pool(
            {
                "accounts": [],
                "tenant_id": ENG,
                "unreadable_documents": [f"{ENG}:a", f"{ENG}:b"],
            }
        ),
    )

    body = client.get("/v1/accounts", headers=auth_header("alice")).json()

    assert body["unreadable_document_count"] == 2


# --------------------------------------------------------------------------
# 4. The browser reads both, and reads the verdict rather than the record
# --------------------------------------------------------------------------

pytestmark_ui = pytest.mark.skipif(
    not UI.is_dir(), reason="apps/swarm-ui is not checked out"
)


def src(name: str) -> str:
    return (UI / name).read_text()


@pytestmark_ui
def test_the_shared_rule_asks_the_verdict_and_not_the_record():
    """One definition, in `types.ts`, and it reads `unreadable_now`.

    A helper that reached for `unreadable_by` would be the TypeScript
    restatement trap the repository keeps paying for: the browser cannot apply
    a thirty-minute rule to a list with no timestamps in it, so it would mark a
    working account as broken for as long as the record survives.
    """
    text = src("types.ts")
    start = text.index("export function unreadableFor(")
    body = text[start : text.index("\n}\n", start)]

    assert "unreadable_now" in body
    assert "unreadable_by" not in body


@pytestmark_ui
def test_the_accounts_screen_marks_a_row_the_pool_is_skipping():
    text = src("Accounts.tsx")
    assert "unreadableFor(" in text, "the row must ask whether the pool is skipping it"
    assert "neverAssigned(" in text, "never assigned is not recently assigned"
    assert "unreadable_document_count" in text, "the shortfall has to reach the reader"


@pytestmark_ui
def test_the_overview_headroom_excludes_an_account_the_pool_is_skipping():
    """The tile answers "can anything run?". An account this tenant cannot read
    has all the headroom in the world and cannot run anything, and it is
    usually the emptiest one precisely because nothing is spending it."""
    text = src("Overview.tsx")
    start = text.index("function accountHeadroom(")
    body = text[start : text.index("\n}\n", start)]

    assert "unreadableFor(" in body


@pytestmark_ui
def test_the_dev_fixture_carries_what_the_route_serves():
    """A fixture that omits a field is development done against a shape the API
    never sends -- the same demand `test_runtimes_screen.py` makes of its own.
    """
    text = src("api.ts")
    assert "unreadable_now:" in text
    assert "unreadable_document_count:" in text
    assert "last_assigned_at:" in text
