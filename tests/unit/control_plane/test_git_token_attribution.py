"""Whose user token is whose (owner decision 2026-10-05, U1 follow-up).

Repository Settings draws "your user token (…last4) is used only for
attribution" from the record `GET /v1/git-tokens` marks `yours`. What is held
here:

  * `yours` is computed per request from the VERIFIED caller, never from
    anything the caller sends: it is true only on the caller's own user token;
  * another member's user token reaches a non-admin caller with `yours:
    false` and no email anywhere in it -- `owner`, `user`, `registered_by`
    and `revoked_by` are null -- and an admin still sees whose it is;
  * no response carries anything token-shaped.

Every token-shaped value is built at runtime.
"""

from __future__ import annotations

import re
import secrets

import pytest

from swarm_api.gittokens import COLLECTION, GitTokenRecord, Viewer, token_id_for

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, auth_header, seed_tenant

#: Built at runtime from pieces: nothing token-shaped is a literal here.
FAKE_VALUE = "".join(("ghp", "_", secrets.token_hex(18)))

TOKEN_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN)"
)

@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """conftest's map with a second, non-admin member of eng: `dave`."""
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "dave@saga.xyz": (ENG_GROUP,),
        "bob@saga.xyz": (RESEARCH_GROUP,),
        "carol@saga.xyz": (),
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def eng_with_git(db):
    seed_tenant(db, "eng", credentials=("git", "anthropic"))


def _register_user(client, who: str) -> str:
    response = client.post("/v1/git-tokens", json={"scope": "user"}, headers=auth_header(who))
    assert response.status_code == 201, response.text
    return response.json()["token"]["token_id"]


def _listed(client, who: str) -> dict[str, dict]:
    response = client.get("/v1/git-tokens", headers=auth_header(who))
    assert response.status_code == 200, response.text
    return {t["token_id"]: t for t in response.json()["tokens"]}


def test_yours_is_true_only_on_the_callers_own_user_token(client, eng_with_git, db) -> None:
    alice_id = _register_user(client, "alice")
    dave_id = _register_user(client, "dave")
    default_id = token_id_for("eng", "tenant", "")
    db.docs[f"{COLLECTION}/{alice_id}"]["last4"] = FAKE_VALUE[-4:]

    as_alice = _listed(client, "alice")
    assert as_alice[alice_id]["yours"] is True
    assert as_alice[alice_id]["owner"] == "alice@saga.xyz"
    assert as_alice[alice_id]["last4"] == FAKE_VALUE[-4:]
    assert as_alice[dave_id]["yours"] is False
    # A tenant token is nobody's own, admin or not.
    assert as_alice[default_id]["yours"] is False

    as_dave = _listed(client, "dave")
    assert as_dave[dave_id]["yours"] is True
    assert as_dave[alice_id]["yours"] is False

    # An admin is told whose each is, and that neither is theirs.
    as_root = _listed(client, "root")
    assert as_root[alice_id]["yours"] is False
    assert as_root[alice_id]["owner"] == "alice@saga.xyz"
    assert as_root[dave_id]["owner"] == "dave@saga.xyz"


def test_another_members_record_carries_no_email(client, eng_with_git) -> None:
    alice_id = _register_user(client, "alice")
    _register_user(client, "dave")
    response = client.get("/v1/git-tokens", headers=auth_header("dave"))
    record = {t["token_id"]: t for t in response.json()["tokens"]}[alice_id]
    assert record["yours"] is False
    for field in ("owner", "user", "registered_by", "revoked_by"):
        assert record[field] is None, field
    # Not in any other field either: the whole served record, as text.
    single = client.get(f"/v1/git-tokens/{alice_id}", headers=auth_header("dave"))
    assert single.status_code == 200, single.text
    assert single.json()["token"]["yours"] is False
    for text in (str(record), single.text):
        assert "alice" not in text


def test_an_admins_email_reaches_admins_only(client, eng_with_git) -> None:
    registered = client.post("/v1/git-tokens", json={"scope": "repository", "repository": "saga-xyz/widgets"},
                             headers=auth_header("root"))
    assert registered.status_code == 201, registered.text
    repo_slot = registered.json()["token"]["token_id"]
    assert registered.json()["token"]["registered_by"] == "root@saga.xyz"
    assert _listed(client, "root")[repo_slot]["registered_by"] == "root@saga.xyz"
    as_alice = _listed(client, "alice")[repo_slot]
    assert as_alice["registered_by"] is None and as_alice["yours"] is False
    assert "root@" not in str(as_alice)
    # The tenant default's registrar is the service, not a person, and is served as it is.
    assert _listed(client, "alice")[token_id_for("eng", "tenant", "")]["registered_by"] == "swarm-api"


def test_yours_cannot_be_claimed_by_the_request(client, eng_with_git) -> None:
    alice_id = _register_user(client, "alice")
    # No query, header or body field names the caller: only the verified identity does.
    for headers in (
        {**auth_header("dave"), "X-Swarm-User": "alice@saga.xyz"},
        {**auth_header("dave"), "X-Goog-Authenticated-User-Email": "accounts.google.com:alice@saga.xyz"},
    ):
        response = client.get("/v1/git-tokens?user=alice@saga.xyz&yours=true", headers=headers)
        assert response.status_code == 200, response.text
        record = {t["token_id"]: t for t in response.json()["tokens"]}[alice_id]
        assert record["yours"] is False and record["owner"] is None


def test_the_revoke_answer_follows_the_same_rule(client, eng_with_git) -> None:
    alice_id = _register_user(client, "alice")
    revoked = client.delete(f"/v1/git-tokens/{alice_id}", headers=auth_header("root"))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["token"]["owner"] == "alice@saga.xyz"
    as_dave = _listed(client, "dave")[alice_id]
    assert as_dave["state"] == "revoked"
    assert as_dave["revoked_by"] is None and as_dave["owner"] is None
    as_alice = _listed(client, "alice")[alice_id]
    assert as_alice["yours"] is True and as_alice["revoked_by"] is None


def test_without_a_viewer_nothing_is_yours_and_no_email_is_served() -> None:
    from datetime import datetime, timezone

    from swarm_api.gittokens import record_for_slot

    record: GitTokenRecord = record_for_slot(
        "eng", "user", user="Alice@Saga.xyz", registered_by="alice@saga.xyz",
        now=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )
    bare = record.to_api()
    assert bare["yours"] is False and bare["owner"] is None and bare["registered_by"] is None
    mine = record.to_api(viewer=Viewer(email=" ALICE@saga.xyz"))
    assert mine["yours"] is True and mine["owner"] == "alice@saga.xyz"
    theirs = record.to_api(viewer=Viewer(email="dave@saga.xyz"))
    assert theirs["yours"] is False and theirs["user"] is None
    admin = record.to_api(viewer=Viewer(email="root@saga.xyz", is_admin=True))
    assert admin["yours"] is False and admin["owner"] == "alice@saga.xyz"


def test_no_attribution_response_carries_a_token(client, eng_with_git, db) -> None:
    alice_id = _register_user(client, "alice")
    db.docs[f"{COLLECTION}/{alice_id}"]["last4"] = FAKE_VALUE[-4:]
    texts = [client.get("/v1/git-tokens", headers=auth_header(who)).text
             for who in ("alice", "dave", "root")]
    texts.append(client.get(f"/v1/git-tokens/{alice_id}", headers=auth_header("alice")).text)
    for text in texts:
        assert FAKE_VALUE not in text
        assert not TOKEN_SHAPES.search(text), text
