"""`user_preferences/up_<32 hex>`: the id, the key versions, the revision.

docs/design/user-profile.md §4.3 (Q1), §4.2's revision rule; lane
PROFILE-STORE. The id is an HMAC under `swarm-child-key`, so a tenant identity
can neither compute a person's document nor plant one that is read. Every key
here is built at runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import pytest

from swarm_api.display_names import claim_id
from swarm_api.errors import PreferencesChanged, PreferencesUnavailable, ValidationFailed
from swarm_api.forgeapp import user_hash
from swarm_api.preferences import (
    COLLECTION,
    REMINDERS,
    TOPICS,
    Preferences,
    StoredPreferences,
    preference_id,
)

from .fakes import FakeFirestore

EMAIL = "Ada@Example.com"


def _key() -> str:
    return secrets.token_hex(32)


def _save(prefs: Preferences, email: str = EMAIL, **over):
    body = {
        "display_name": None,
        "default_workspace": None,
        "muted": [],
        "reminder": "as_set",
        "revision": 0,
    }
    body.update(over)
    return prefs.save(email, **body)


# -- the id -------------------------------------------------------------------


def test_the_id_is_up_and_32_hex_of_the_hmac_of_user_prefs_and_the_lowered_email() -> None:
    key = _key()
    expected = hmac.new(
        key.encode(), ("user-prefs:" + EMAIL.lower()).encode(), hashlib.sha256
    ).hexdigest()[:32]
    assert preference_id(key, EMAIL) == "up_" + expected
    assert preference_id(key, EMAIL.lower()) == preference_id(key, EMAIL)


def test_the_id_is_not_the_computable_user_hash() -> None:
    key = _key()
    doc_id = preference_id(key, EMAIL)
    assert user_hash(EMAIL) not in doc_id
    assert doc_id != "up_" + hashlib.sha256(EMAIL.lower().encode()).hexdigest()[:32]


def test_the_id_differs_under_another_key() -> None:
    assert preference_id(_key(), EMAIL) != preference_id(_key(), EMAIL)


def test_the_id_differs_from_the_claim_id_of_the_same_string() -> None:
    key = _key()
    same = EMAIL.lower()
    assert preference_id(key, same)[3:] != claim_id(key, same)[3:]


def test_the_id_is_stable_across_reads_and_saves() -> None:
    db = FakeFirestore()
    key = _key()
    prefs = Preferences(db, [key])
    _save(prefs, reminder="daily")
    first = prefs.ids(EMAIL)
    assert prefs.read(EMAIL).reminder == "daily"
    assert Preferences(db, [key]).read(EMAIL).reminder == "daily"
    assert prefs.ids(EMAIL) == first == [preference_id(key, EMAIL)]
    assert db.paths(COLLECTION + "/") == [f"{COLLECTION}/{first[0]}"]


# -- reads ----------------------------------------------------------------------


def test_an_absent_document_is_the_defaults_at_revision_zero() -> None:
    stored = Preferences(FakeFirestore(), [_key()]).read(EMAIL)
    assert stored == StoredPreferences()
    assert stored.revision == 0 and stored.reminder == "as_set" and stored.muted == ()


def test_a_failed_read_is_preferences_unavailable_never_the_defaults() -> None:
    db = FakeFirestore()

    def broken(*_a, **_k):
        raise RuntimeError("firestore down")

    db.get_all = broken  # type: ignore[method-assign]
    with pytest.raises(PreferencesUnavailable) as caught:
        Preferences(db, [_key()]).read(EMAIL)
    assert caught.value.status_code == 503
    assert caught.value.code == "preferences_unavailable"
    assert "nothing was submitted" in caught.value.message


def test_no_key_configured_is_unavailable_rather_than_a_computable_id() -> None:
    with pytest.raises(PreferencesUnavailable):
        Preferences(FakeFirestore(), []).read(EMAIL)


def test_a_planted_document_under_an_invented_id_is_read_by_nobody() -> None:
    db = FakeFirestore()
    db.document(f"{COLLECTION}/up_{user_hash(EMAIL)}").set(
        {"default_workspace": "elsewhere", "revision": 9}
    )
    assert Preferences(db, [_key()]).read(EMAIL) == StoredPreferences()


def test_a_malformed_document_reads_as_its_defaults() -> None:
    db = FakeFirestore()
    key = _key()
    db.document(f"{COLLECTION}/{preference_id(key, EMAIL)}").set(
        {"display_name": 7, "notifications": {"muted": ["nope", "failed"], "reminder": "hourly"},
         "revision": True}
    )
    stored = Preferences(db, [key]).read(EMAIL)
    assert stored.display_name is None
    assert stored.muted == ("failed",)
    assert stored.reminder == "as_set"
    assert stored.revision == 0


def test_the_cached_read_serves_15_seconds_then_reads_again_and_a_save_drops_it() -> None:
    db = FakeFirestore()
    key = _key()
    now = [100.0]
    prefs = Preferences(db, [key], clock=lambda: now[0])
    assert prefs.cached(EMAIL).reminder == "as_set"
    db.document(f"{COLLECTION}/{preference_id(key, EMAIL)}").set(
        {"notifications": {"muted": [], "reminder": "daily"}, "revision": 1}
    )
    assert prefs.cached(EMAIL).reminder == "as_set"
    now[0] += 15.0
    assert prefs.cached(EMAIL).reminder == "daily"
    _save(prefs, reminder="never", revision=1)
    assert prefs.cached(EMAIL).reminder == "never"


# -- key rotation -----------------------------------------------------------------


def test_a_document_under_the_previous_key_is_read_and_a_write_lands_under_the_newest() -> None:
    db = FakeFirestore()
    old, new = _key(), _key()
    Preferences(db, [old]).save(
        EMAIL, display_name="Ada Lovelace", default_workspace="team-x",
        muted=["finished"], reminder="daily", revision=0,
    )
    rotated = Preferences(db, [new, old])
    stored = rotated.read(EMAIL)
    assert stored.default_workspace == "team-x"
    assert stored.display_name == "Ada Lovelace"
    assert stored.revision == 1

    saved = rotated.save(
        EMAIL, display_name="Ada Lovelace", default_workspace="team-x",
        muted=[], reminder="daily", revision=1,
    )
    assert saved.revision == 2
    assert db.paths(COLLECTION + "/") == [f"{COLLECTION}/{preference_id(new, EMAIL)}"]
    assert db.docs[f"{COLLECTION}/{preference_id(new, EMAIL)}"]["revision"] == 2
    # The claim moved to the newest key, owned by the newest id.
    claims = db.dump("display_name_claims/")
    assert list(claims) == ["display_name_claims/" + claim_id(new, "ada lovelace")]
    assert claims["display_name_claims/" + claim_id(new, "ada lovelace")]["owner"] == \
        preference_id(new, EMAIL)
    # Once the old key is gone, the newest document is still read.
    assert Preferences(db, [new]).read(EMAIL).revision == 2


def test_the_newest_existing_version_wins_when_both_exist() -> None:
    db = FakeFirestore()
    old, new = _key(), _key()
    db.document(f"{COLLECTION}/{preference_id(old, EMAIL)}").set({"revision": 3})
    db.document(f"{COLLECTION}/{preference_id(new, EMAIL)}").set({"revision": 5})
    assert Preferences(db, [new, old]).read(EMAIL).revision == 5


# -- the save ----------------------------------------------------------------------


def test_a_save_replaces_the_whole_set_and_bumps_the_revision() -> None:
    db = FakeFirestore()
    key = _key()
    prefs = Preferences(db, [key])
    saved = _save(prefs, default_workspace="team-x", muted=["finished", "approval"],
                  reminder="every_4h")
    assert saved.revision == 1
    assert saved.muted == ("approval", "finished")
    doc = db.docs[f"{COLLECTION}/{preference_id(key, EMAIL)}"]
    assert doc["v"] == 1
    assert doc["notifications"] == {"muted": ["approval", "finished"], "reminder": "every_4h"}
    assert "email" not in doc and EMAIL.lower() not in str(doc).lower()
    saved = _save(prefs, revision=1)
    assert saved.default_workspace is None and saved.muted == () and saved.revision == 2


def test_a_stale_revision_is_409_and_stores_nothing() -> None:
    db = FakeFirestore()
    prefs = Preferences(db, [_key()])
    _save(prefs, reminder="daily")
    before = db.dump()
    with pytest.raises(PreferencesChanged) as caught:
        _save(prefs, display_name="Ada Lovelace", reminder="never", revision=0)
    assert caught.value.status_code == 409
    assert caught.value.code == "preferences_changed"
    assert db.dump() == before


def test_a_first_save_with_a_nonzero_revision_is_409() -> None:
    db = FakeFirestore()
    with pytest.raises(PreferencesChanged):
        _save(Preferences(db, [_key()]), revision=1)
    assert db.dump() == {}


@pytest.mark.parametrize(
    "over",
    [
        {"muted": ["nope"]},
        {"muted": ["failed", "failed"]},
        {"muted": "failed"},
        {"reminder": "hourly"},
        {"revision": -1},
        {"revision": True},
        {"default_workspace": ""},
    ],
)
def test_a_malformed_save_is_422_and_stores_nothing(over: dict) -> None:
    db = FakeFirestore()
    with pytest.raises(ValidationFailed):
        _save(Preferences(db, [_key()]), **over)
    assert db.dump() == {}


def test_every_topic_may_be_muted_and_every_reminder_saved() -> None:
    prefs = Preferences(FakeFirestore(), [_key()])
    revision = 0
    for reminder in REMINDERS:
        saved = _save(prefs, muted=list(TOPICS), reminder=reminder, revision=revision)
        revision = saved.revision
        assert saved.muted == TOPICS and saved.reminder == reminder


def test_the_save_log_names_the_user_hash_and_fields_never_values(caplog) -> None:
    prefs = Preferences(FakeFirestore(), [_key()])
    with caplog.at_level("INFO", logger="swarm_api.preferences"):
        _save(prefs, display_name="Ada Lovelace", default_workspace="team-secret")
    text = caplog.text
    assert user_hash(EMAIL) in text
    assert "display_name" in text and "default_workspace" in text
    assert "Ada" not in text and "team-secret" not in text and EMAIL.lower() not in text.lower()
