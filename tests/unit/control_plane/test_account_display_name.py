"""An account can be renamed: an editable Name shown in place of its label (#1913).

Owner decision 2026-10-22 (BGD, "Editable display name"): each account gets a
Name -- any text, trimmed, 1 to 60 characters, capitals and spaces allowed, no
control characters. The id (`<owner>:<label>`), the credential secret, the
holds, the readings, the history and the lending do not change. So:

  * THE NAME IS ONE OPTIONAL FIELD on the account document, `display_name`,
    read as absent from every document written before it.
  * ONE ROUTE SETS OR CLEARS IT, owner only, and it writes that field and
    nothing else: a rename never interrupts work on the account.
  * A BAD NAME IS A 422 in the broker's words, never a silent trim to nothing.

Unit tests: the fake Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import Account
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app

ENG = "eng"
RESEARCH = "research"


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


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


def _rename(client, account_id: str, name):
    return client.put(f"/v1/accounts/{account_id}/name", json={"display_name": name})


# --------------------------------------------------------------------------
# The account record
# --------------------------------------------------------------------------


def test_a_name_round_trips_through_firestore():
    account = Account(
        account_id=f"{ENG}:ai-eng-2", owner_tenant=ENG, label="ai-eng-2", display_name="AI Eng 2"
    )
    stored = account.to_firestore()
    # MUTATION: drop `display_name` from `Account.to_firestore` -- the Name is
    # never written and this key is missing.
    assert stored["display_name"] == "AI Eng 2"
    # MUTATION: drop `display_name=` from `Account.from_firestore` -- it reads
    # back as None.
    assert Account.from_firestore(stored).display_name == "AI Eng 2"


def test_a_document_without_a_name_reads_as_none():
    stored = Account(account_id=f"{ENG}:one", owner_tenant=ENG, label="one").to_firestore()
    old = dict(stored)
    old.pop("display_name", None)
    # MUTATION: default `display_name` to the label in `from_firestore` -- an
    # unnamed account would claim a Name nobody gave it.
    assert Account.from_firestore(old).display_name is None
    # A malformed stored value is display text, so it reads as absent rather
    # than taking the account out of the pool.
    # MUTATION: return the raw value from `_display_name_from_firestore` -- a
    # number is served as a Name.
    assert Account.from_firestore({**old, "display_name": 42}).display_name is None


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def test_the_owner_names_an_account_and_reads_it_back(client, accounts):
    accounts.register(ENG, "ai-eng-2")

    r = _rename(client, f"{ENG}:ai-eng-2", "  AI Eng 2  ")

    # MUTATION: drop the `/v1/accounts/{account_id}/name` route -- 404/405.
    assert r.status_code == 200, r.text
    # Trimmed, capitals and the space kept.
    # MUTATION: store `body.display_name` untrimmed -- "  AI Eng 2  ".
    assert r.json()["account"]["display_name"] == "AI Eng 2"
    assert accounts.get(f"{ENG}:ai-eng-2").display_name == "AI Eng 2"
    # The id and the label are untouched: the Name is display text.
    assert r.json()["account"]["account_id"] == f"{ENG}:ai-eng-2"
    assert r.json()["account"]["label"] == "ai-eng-2"

    listed = client.get("/v1/accounts", params={"tenant_id": ENG}).json()["accounts"]
    # MUTATION: drop `display_name` from `account_to_api` -- the listing the
    # console and the plugin read never carries it.
    assert listed[0]["display_name"] == "AI Eng 2"


def test_an_unnamed_account_is_served_with_no_name(client, accounts):
    accounts.register(ENG, "plain")
    listed = client.get("/v1/accounts", params={"tenant_id": ENG}).json()["accounts"]
    # MUTATION: serve the label as `display_name` when none is set -- the
    # console could no longer tell a named account from an unnamed one.
    assert listed[0]["display_name"] is None


def test_null_clears_the_name(client, accounts):
    accounts.register(ENG, "ai-eng-2")
    assert _rename(client, f"{ENG}:ai-eng-2", "AI Eng 2").status_code == 200

    cleared = _rename(client, f"{ENG}:ai-eng-2", None)

    # MUTATION: refuse None in `validate_display_name`'s caller -- a Name could
    # never be removed.
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["account"]["display_name"] is None
    assert accounts.get(f"{ENG}:ai-eng-2").display_name is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "x" * 61,
        "AI\nEng",
        "AI\x00Eng",
        "AI Eng",
    ],
    ids=["empty", "blank", "61-chars", "newline", "nul", "line-separator"],
)
def test_a_bad_name_is_refused_with_422_and_nothing_is_written(client, accounts, name):
    accounts.register(ENG, "ai-eng-2")
    assert _rename(client, f"{ENG}:ai-eng-2", "Kept").status_code == 200

    r = _rename(client, f"{ENG}:ai-eng-2", name)

    # MUTATION: skip `validate_display_name` in the route -- each of these is
    # stored, and the 61-character one overflows every heading it is printed in.
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"
    # The refusal says what a Name may be, in words a person can act on.
    assert "Name" in r.json()["message"]
    assert accounts.get(f"{ENG}:ai-eng-2").display_name == "Kept"


def test_sixty_characters_is_the_longest_name_accepted(client, accounts):
    accounts.register(ENG, "ai-eng-2")
    # MUTATION: make the limit `>= 60` -- the longest legal Name is refused.
    assert _rename(client, f"{ENG}:ai-eng-2", "y" * 60).status_code == 200


def test_another_owners_caller_is_refused(client, accounts):
    accounts.register(RESEARCH, "shared", lend_to=[ENG])
    client.identity.tenant = ENG  # the borrower

    r = _rename(client, f"{RESEARCH}:shared", "Mine now")

    # MUTATION: drop `_authorize(...)` from the route -- a borrower renames
    # the lender's account.
    assert r.status_code in (401, 403), r.text
    assert accounts.get(f"{RESEARCH}:shared").display_name is None


def test_naming_changes_no_other_field(client, accounts, broker):
    accounts.register(ENG, "busy", lend_to=[RESEARCH])
    assert client.put(
        f"/v1/accounts/{ENG}:busy/model-classes", json={"model_classes": ["opus"]}
    ).status_code == 200
    before = dict(broker.db.docs[f"accounts/{ENG}:busy"])

    assert _rename(client, f"{ENG}:busy", "Busy one").status_code == 200

    after = dict(broker.db.docs[f"accounts/{ENG}:busy"])
    # MUTATION: write the Name with `register` or a whole-document `set` --
    # any key below that moved is a rename interrupting the account.
    assert after.pop("display_name") == "Busy one"
    before.pop("display_name", None)
    assert after == before


def test_changing_who_an_account_is_lent_to_keeps_its_name(client, accounts):
    """Lending is changed by re-registering, which writes the whole document."""
    accounts.register(ENG, "team")
    assert _rename(client, f"{ENG}:team", "The team's").status_code == 200

    r = client.put(f"/v1/accounts/{ENG}:team/lending", json={"lend_to": [RESEARCH]})

    assert r.status_code == 200, r.text
    # MUTATION: drop `display_name=current.display_name` from
    # `AccountStore.register` -- re-lending forgets the Name.
    assert accounts.get(f"{ENG}:team").display_name == "The team's"
    assert r.json()["account"]["display_name"] == "The team's"


def test_an_unknown_account_is_a_422(client):
    r = _rename(client, f"{ENG}:nobody", "Ghost")
    assert r.status_code == 422, r.text
