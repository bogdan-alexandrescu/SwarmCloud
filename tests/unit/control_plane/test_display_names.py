"""The display name's rules, its claim, its audit and its lookup.

docs/design/user-profile.md §4.4 (Q5, Q12); lane PROFILE-STORE. Each rule is
held by the case that breaks it, with the code the refusal carries. Every key
here is built at runtime.
"""

from __future__ import annotations

import secrets

import pytest

from swarm_api.display_names import (
    AUDIT_ACTION,
    CLAIMS,
    DisplayNames,
    check,
    claim_id,
    fold,
)
from swarm_api.errors import (
    DisplayNameInvalid,
    DisplayNameReserved,
    DisplayNameTaken,
    PreferencesChanged,
)
from swarm_api.preferences import COLLECTION, Preferences, preference_id

from .fakes import FakeFirestore

ADA = "ada@example.com"
OTHER = "other@example.com"


def _key() -> str:
    return secrets.token_hex(32)


def _save(prefs: Preferences, email: str, name: str | None, revision: int = 0):
    return prefs.save(
        email, display_name=name, default_workspace=None, muted=[], reminder="as_set",
        revision=revision,
    )


def _audit(db: FakeFirestore) -> list[dict]:
    return [d for d in db.collection_docs("admin_audit") if d.get("action") == AUDIT_ACTION]


@pytest.fixture
def store():
    db = FakeFirestore()
    prefs = Preferences(db, [_key()])
    return db, prefs


# -- rules 1-5: the name alone ------------------------------------------------------


def test_a_name_is_normalised_before_it_is_stored() -> None:
    assert check("  Ada   Lovelace ") == "Ada Lovelace"
    assert check("Adá") == "Adá"  # NFC


@pytest.mark.parametrize(
    "name",
    [
        "A" * 65,  # rule 2: longer than 64
        "",  # rule 2: nothing left
        "   ",
        "Ada Lovelace 2",  # rule 3: a digit
        "ada@example.com",  # rule 3: `@`, so no name embeds an email
        "Ada ‮ecalevoL",  # rule 3: a bidi override
        "Ada‍Lovelace",  # rule 3: a zero-width joiner
        "Ada <ada>",
        "[admin] Ada",
        "Ada\tLovelace",
        "Аda Lovelace",  # rule 4: a Cyrillic A among Latin letters
        "...",  # no letter at all
        42,
    ],
)
def test_each_format_rule_refuses_its_case_as_display_name_invalid(name) -> None:
    with pytest.raises(DisplayNameInvalid) as caught:
        check(name)
    assert caught.value.code == "display_name_invalid"
    assert caught.value.status_code == 422


def test_64_characters_is_accepted() -> None:
    assert check("A" * 64) == "A" * 64


@pytest.mark.parametrize("name", ["Ada Admin", "admin", "The Owner", "SwarmCloud", "Bot-Smith",
                                  "ADMINISTRATOR Jones"])
def test_a_reserved_word_is_display_name_reserved(name: str) -> None:
    with pytest.raises(DisplayNameReserved) as caught:
        check(name)
    assert caught.value.code == "display_name_reserved"


def test_a_reserved_word_only_as_a_whole_word() -> None:
    assert check("Botticelli Adminson") == "Botticelli Adminson"


@pytest.mark.parametrize("name", ["Ada Lovelace", "Jean-Luc O'Neil", "Анна Каренина",
                                  "Ζωή", "Adá Lovelace", "J. R. R. Tolkien"])
def test_real_names_in_one_script_pass(name: str) -> None:
    assert check(name) == name


def test_fold_drops_case_accents_punctuation_and_extra_spaces() -> None:
    assert fold("ADA  lovelace") == fold("Adá Lovelace") == fold("Ada Lovelace") == "ada lovelace"
    assert fold("Jean-Luc O'Neil") == "jean luc o neil"


# -- rule 6: another person's name -----------------------------------------------------


@pytest.mark.parametrize("name", ["ADA  lovelace", "Adá Lovelace"])
def test_an_exact_folded_match_is_taken(store, name: str) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Ada Lovelace")
    with pytest.raises(DisplayNameTaken) as caught:
        _save(prefs, ADA, name)
    assert caught.value.code == "display_name_taken"
    assert OTHER not in caught.value.message and "Lovelace" not in caught.value.message
    assert prefs.read(ADA).display_name is None


def test_a_containing_name_is_refused_for_containment_and_for_the_reserved_word(store) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Ada Lovelace")
    with pytest.raises(DisplayNameReserved):
        check("Ada Lovelace Admin")
    with pytest.raises(DisplayNameTaken):
        prefs.names.ensure_free(fold("Ada Lovelace Admin"), prefs.ids(ADA))


def test_a_containing_name_is_refused_for_containment(store) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Ada Lovelace")
    with pytest.raises(DisplayNameTaken):
        _save(prefs, ADA, "Ada Lovelace Jr")
    assert prefs.read(ADA).revision == 0


def test_a_one_word_name_does_not_block_a_longer_name_that_contains_it(store) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Bob")
    assert _save(prefs, ADA, "Bob Smith").display_name == "Bob Smith"


@pytest.mark.parametrize("name", ["BOB", "Bób"])
def test_a_one_word_name_blocks_its_exact_match(store, name: str) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Bob")
    with pytest.raises(DisplayNameTaken):
        _save(prefs, ADA, name)


@pytest.mark.parametrize("name", ["Bob Smith", "Bobby"])
def test_a_one_word_name_lets_other_names_through(store, name: str) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Bob")
    assert _save(prefs, ADA, name).display_name == name


def test_ones_own_name_never_collides_with_itself(store) -> None:
    db, prefs = store
    _save(prefs, ADA, "Ada Lovelace")
    saved = _save(prefs, ADA, "ADA Lovelace", revision=1)
    assert saved.display_name == "ADA Lovelace"
    assert len(db.paths(CLAIMS + "/")) == 1


def test_a_claim_planted_at_an_id_that_is_not_its_hmac_is_ignored_by_both_checks(store) -> None:
    db, prefs = store
    # The owner's document exists and holds the name, so only the id betrays it.
    _save(prefs, OTHER, None)
    owner = prefs.ids(OTHER)[0]
    db.document(f"{COLLECTION}/{owner}").set(
        {"display_name": "Ada Lovelace", "display_name_folded": "ada lovelace", "revision": 1}
    )
    db.document(f"{CLAIMS}/dn_{secrets.token_hex(16)}").set(
        {"owner": owner, "folded": "ada lovelace"}
    )
    claim_key = _key()  # an HMAC under a key the platform does not hold
    db.document(f"{CLAIMS}/{claim_id(claim_key, 'ada lovelace jr')}").set(
        {"owner": owner, "folded": "ada lovelace jr"}
    )
    # Exact (the foreign-key claim) and containing (the random-id claim).
    assert _save(prefs, "third@example.com", "Ada Lovelace Jr").display_name
    assert _save(prefs, ADA, "Ada Lovelace").display_name == "Ada Lovelace"  # exact
    assert db.docs[f"{CLAIMS}/{claim_id(prefs.keys[0], 'ada lovelace')}"]["owner"] == \
        prefs.ids(ADA)[0]


def test_a_claim_whose_owner_no_longer_holds_the_name_is_taken_over(store) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Ada Lovelace")
    # The owner's document no longer holds it: lost to a rotation, or edited.
    db.document(f"{COLLECTION}/{prefs.ids(OTHER)[0]}").set({"revision": 1})
    ref = f"{CLAIMS}/{claim_id(prefs.keys[0], 'ada lovelace')}"
    assert db.docs[ref]["owner"] == prefs.ids(OTHER)[0]
    assert _save(prefs, ADA, "Ada Lovelace Jr").display_name == "Ada Lovelace Jr"
    assert _save(prefs, ADA, "Ada Lovelace", revision=1).display_name == "Ada Lovelace"
    assert db.docs[ref]["owner"] == prefs.ids(ADA)[0]


def test_a_name_held_through_its_claim_is_refused_inside_the_transaction(store) -> None:
    db, prefs = store
    _save(prefs, OTHER, "Ada Lovelace")
    # Skip the streamed check: the transaction alone must still refuse.
    prefs.names.ensure_free = lambda *_a, **_k: None  # type: ignore[method-assign]
    with pytest.raises(DisplayNameTaken):
        _save(prefs, ADA, "Ada Lovelace")
    assert prefs.read(ADA).revision == 0


def test_changing_a_name_frees_the_old_claim(store) -> None:
    db, prefs = store
    _save(prefs, ADA, "Ada Lovelace")
    _save(prefs, ADA, "Augusta King", revision=1)
    assert db.paths(CLAIMS + "/") == [f"{CLAIMS}/{claim_id(prefs.keys[0], 'augusta king')}"]
    assert _save(prefs, OTHER, "Ada Lovelace").display_name == "Ada Lovelace"


# -- the audit ---------------------------------------------------------------------------


def test_every_change_including_clearing_writes_one_audit_entry_with_from_and_to(store) -> None:
    db, prefs = store
    _save(prefs, ADA, "Ada Lovelace")
    _save(prefs, ADA, "Augusta King", revision=1)
    _save(prefs, ADA, None, revision=2)
    entries = sorted(_audit(db), key=lambda d: d["at"])
    assert [e["detail"] for e in entries] == [
        {"from": None, "to": "Ada Lovelace"},
        {"from": "Ada Lovelace", "to": "Augusta King"},
        {"from": "Augusta King", "to": None},
    ]
    assert all(e["by"] == ADA and e["target_workspace_id"] is None for e in entries)
    assert db.paths(CLAIMS + "/") == []


def test_a_save_that_leaves_the_name_alone_writes_no_audit_entry(store) -> None:
    db, prefs = store
    _save(prefs, ADA, "Ada Lovelace")
    prefs.save(ADA, display_name="Ada Lovelace", default_workspace="team-x", muted=["failed"],
               reminder="daily", revision=1)
    assert len(_audit(db)) == 1


def test_the_audit_entry_is_written_in_the_documents_transaction(store) -> None:
    db, prefs = store
    committed = db.transactions_committed
    _save(prefs, ADA, "Ada Lovelace")
    assert db.transactions_committed == committed + 1
    assert len(_audit(db)) == 1
    # A refused save (stale revision) writes neither the document nor an entry.
    with pytest.raises(PreferencesChanged):
        _save(prefs, ADA, "Augusta King", revision=0)
    assert len(_audit(db)) == 1
    assert prefs.read(ADA).display_name == "Ada Lovelace"


# -- the lookup --------------------------------------------------------------------------


def test_names_for_returns_only_the_display_name(store) -> None:
    db, prefs = store
    prefs.save(ADA, display_name="Ada Lovelace", default_workspace="team-x",
               muted=["failed"], reminder="daily", revision=0)
    names = prefs.names.names_for([ADA, OTHER])
    assert names == {ADA: "Ada Lovelace", OTHER: None}


def test_names_for_is_null_for_every_email_when_the_read_fails(store) -> None:
    db, prefs = store
    _save(prefs, ADA, "Ada Lovelace")

    def broken(*_a, **_k):
        raise RuntimeError("firestore down")

    fresh = Preferences(db, prefs.keys)
    db.get_all = broken  # type: ignore[method-assign]
    assert fresh.names.names_for([ADA, OTHER]) == {ADA: None, OTHER: None}


def test_names_for_caches_60_seconds_and_a_save_drops_the_entry() -> None:
    db = FakeFirestore()
    key = _key()
    now = [0.0]
    prefs = Preferences(db, [key], clock=lambda: now[0])
    assert isinstance(prefs.names, DisplayNames)
    _save(prefs, ADA, "Ada Lovelace")
    assert prefs.names.names_for([ADA]) == {ADA: "Ada Lovelace"}
    db.document(f"{COLLECTION}/{preference_id(key, ADA)}").set(
        {"display_name": "Changed Elsewhere", "revision": 2}
    )
    now[0] += 59.0
    assert prefs.names.names_for([ADA]) == {ADA: "Ada Lovelace"}
    now[0] += 1.0
    assert prefs.names.names_for([ADA]) == {ADA: "Changed Elsewhere"}
    _save(prefs, ADA, "Augusta King", revision=2)
    assert prefs.names.names_for([ADA]) == {ADA: "Augusta King"}
