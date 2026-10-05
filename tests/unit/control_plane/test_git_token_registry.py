"""The git token registry (docs/git-tokens.md §1, §2, §4.1, §7; lane GT1).

A record describes one Secret Manager slot and never holds a value. What is
held here:

  * a tenant whose document lists the `git` credential has its tenant-default
    record, named `swarm-tenant-<tenant>-git`, without anyone registering it;
  * registering a slot answers the exact `scripts/create-secrets.sh --stdin`
    command for it (Git tokens B: a command line, no paste box), and no route
    accepts a token value in any field;
  * every read and write is scoped to the caller's tenant: another tenant's
    token id is a 404 worded exactly like a missing one;
  * no response and no log record carries anything token-shaped.

Every token-shaped value is built at runtime.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

import pytest

from swarm_api.gittokens import (
    COLLECTION,
    provider_suffix,
    repo_id_for,
    token_id_for,
)
from swarm_api.routes import gittokens as gittokens_routes

from .conftest import auth_header, seed_tenant

#: Built at runtime from pieces: nothing token-shaped is a literal here.
FAKE_VALUE = "".join(("ghp", "_", secrets.token_hex(18)))

#: The shapes a GitHub credential takes, matched against every response and log.
TOKEN_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN)"
)

REPO = repo_id_for("eng", "saga-xyz/widgets")
REPO_HEX = REPO.removeprefix("repo_")


def assert_no_token_shape(text: str) -> None:
    assert FAKE_VALUE not in text
    assert not TOKEN_SHAPES.search(text), text


@pytest.fixture
def eng_with_git(db):
    seed_tenant(db, "eng", credentials=("git", "anthropic"))
    seed_tenant(db, "research", credentials=("anthropic",))


# -- naming (§1, §2) --------------------------------------------------------


def test_repo_id_is_repo_index_section_1() -> None:
    digest = hashlib.sha256(b"eng" + b"github.com/" + b"saga-xyz/widgets").hexdigest()
    assert REPO == "repo_" + digest[:16]
    assert repo_id_for("eng", "Saga-XYZ/Widgets") == REPO


def test_provider_suffixes_follow_section_2() -> None:
    assert provider_suffix("tenant") == "git"
    assert provider_suffix("repository", repo_id=REPO) == "git-r-" + REPO_HEX
    email_hex = hashlib.sha256(b"alice@saga.xyz").hexdigest()[:16]
    assert provider_suffix("user", user="Alice@Saga.xyz") == "git-u-" + email_hex


def test_token_id_is_deterministic_per_slot() -> None:
    assert token_id_for("eng", "tenant", "") == token_id_for("eng", "tenant", "")
    assert token_id_for("eng", "tenant", "") != token_id_for("research", "tenant", "")
    assert re.fullmatch(r"tok_[0-9a-f]{16}", token_id_for("eng", "repository", REPO))


# -- the tenant default ------------------------------------------------------


def test_tenant_default_appears_for_a_tenant_with_a_git_slot(client, eng_with_git, db) -> None:
    response = client.get("/v1/git-tokens", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    tokens = response.json()["tokens"]
    assert len(tokens) == 1
    record = tokens[0]
    assert record["scope"] == "tenant"
    assert record["tenant_id"] == "eng"
    assert record["provider_suffix"] == "git"
    assert record["secret_name"] == "swarm-tenant-eng-git"
    assert record["forge"] == "github"
    assert record["state"] == "unverified"
    assert record["last4"] is None
    assert record["store_command"] == (
        "scripts/create-secrets.sh --tenant eng --provider git --stdin"
    )
    # Created, not only drawn: the record is a document from now on.
    assert f"{COLLECTION}/{record['token_id']}" in db.docs


def test_no_tenant_default_without_a_git_slot(client, eng_with_git) -> None:
    response = client.get("/v1/git-tokens", headers=auth_header("bob"))
    assert response.status_code == 200, response.text
    assert response.json()["tokens"] == []


def test_listing_twice_creates_one_default(client, eng_with_git, db) -> None:
    client.get("/v1/git-tokens", headers=auth_header("alice"))
    client.get("/v1/git-tokens", headers=auth_header("alice"))
    assert len([k for k in db.docs if k.startswith(f"{COLLECTION}/")]) == 1


# -- registering a slot ------------------------------------------------------


def test_registering_a_repository_slot_answers_its_command(client, eng_with_git) -> None:
    response = client.post(
        "/v1/git-tokens", json={"scope": "repository", "repo_id": REPO},
        headers=auth_header("root"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    record = body["token"]
    assert record["scope"] == "repository"
    assert record["repo_ids"] == [REPO]
    assert record["secret_name"] == f"swarm-tenant-eng-git-r-{REPO_HEX}"
    assert record["registered_by"] == "root@saga.xyz"
    assert record["state"] == "unverified"
    assert body["store_command"] == (
        f"scripts/create-secrets.sh --tenant eng --provider git-r-{REPO_HEX} --stdin"
    )
    # Idempotent on the slot.
    again = client.post(
        "/v1/git-tokens", json={"scope": "repository", "repository": "saga-xyz/widgets"},
        headers=auth_header("root"),
    )
    assert again.status_code == 200, again.text
    assert again.json()["token"]["token_id"] == record["token_id"]


def test_a_member_may_not_register_a_repository_slot(client, eng_with_git) -> None:
    response = client.post(
        "/v1/git-tokens", json={"scope": "repository", "repo_id": REPO},
        headers=auth_header("alice"),
    )
    assert response.status_code == 403, response.text


def test_a_user_slot_is_always_the_callers_own(client, eng_with_git) -> None:
    response = client.post(
        "/v1/git-tokens", json={"scope": "user"}, headers=auth_header("alice"),
    )
    assert response.status_code == 201, response.text
    record = response.json()["token"]
    assert record["user"] == "alice@saga.xyz"
    email_hex = hashlib.sha256(b"alice@saga.xyz").hexdigest()[:16]
    assert record["secret_name"] == f"swarm-tenant-eng-git-u-{email_hex}"
    # The email is hashed in the secret's name, never spelled.
    assert "alice" not in record["secret_name"]
    refused = client.post(
        "/v1/git-tokens", json={"scope": "user", "user": "bob@saga.xyz"},
        headers=auth_header("alice"),
    )
    assert refused.status_code == 422, refused.text


def test_a_repository_slot_needs_a_well_formed_repository(client, eng_with_git) -> None:
    for body in (
        {"scope": "repository"},
        {"scope": "repository", "repo_id": "repo_xyz"},
        {"scope": "repository", "repository": "not a repo"},
        {"scope": "repository", "repo_id": REPO, "repository": "saga-xyz/widgets"},
        {"scope": "tenant", "repo_id": REPO},
        {"scope": "everything"},
    ):
        response = client.post("/v1/git-tokens", json=body, headers=auth_header("root"))
        assert response.status_code == 422, (body, response.text)


# -- no route accepts a value ------------------------------------------------


def test_no_route_accepts_a_token_value(client, eng_with_git) -> None:
    # The router's own routes: the pinned FastAPI mounts an included router
    # as one opaque entry in `app.routes`. That it IS mounted is every other
    # test in this file, which reaches it over HTTP.
    routes = list(gittokens_routes.router.routes)
    assert len(routes) == 4
    for route in routes:
        assert not route.path.endswith("/value"), route.path
        assert route.methods <= {"GET", "POST", "DELETE"}, (route.path, route.methods)
    for field in ("value", "token", "secret", "api_key", "password"):
        response = client.post(
            "/v1/git-tokens", json={"scope": "user", field: FAKE_VALUE},
            headers=auth_header("alice"),
        )
        assert response.status_code == 422, (field, response.text)
        assert_no_token_shape(response.text)
    token_id = token_id_for("eng", "tenant", "")
    for method in ("put", "patch"):
        response = client.request(
            method, f"/v1/git-tokens/{token_id}/value", json={"value": FAKE_VALUE},
            headers=auth_header("root"),
        )
        assert response.status_code in (404, 405), response.text
        assert_no_token_shape(response.text)


# -- tenant isolation --------------------------------------------------------


def test_another_tenants_token_is_a_404_like_a_missing_one(client, eng_with_git) -> None:
    listed = client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"]
    eng_default = listed[0]["token_id"]
    theirs = client.get(f"/v1/git-tokens/{eng_default}", headers=auth_header("bob"))
    missing_id = token_id_for("research", "repository", REPO)
    missing = client.get(f"/v1/git-tokens/{missing_id}", headers=auth_header("bob"))
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["message"].replace(eng_default, "X") == (
        missing.json()["message"].replace(missing_id, "X")
    )
    # Nor can they revoke it, and it stays active for its own tenant.
    gone = client.delete(f"/v1/git-tokens/{eng_default}", headers=auth_header("bob"))
    assert gone.status_code == 404
    mine = client.get(f"/v1/git-tokens/{eng_default}", headers=auth_header("alice"))
    assert mine.status_code == 200 and mine.json()["token"]["state"] == "unverified"
    # And their list never shows it.
    assert client.get("/v1/git-tokens", headers=auth_header("bob")).json()["tokens"] == []


def test_registering_names_the_callers_own_tenant(client, eng_with_git) -> None:
    response = client.post(
        "/v1/git-tokens", json={"scope": "user"}, headers=auth_header("bob"),
    )
    assert response.status_code == 201, response.text
    record = response.json()["token"]
    assert record["tenant_id"] == "research"
    assert record["secret_name"].startswith("swarm-tenant-research-git-u-")
    eng = client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"]
    assert record["token_id"] not in {t["token_id"] for t in eng}


# -- revoking ----------------------------------------------------------------


def test_revoking(client, eng_with_git) -> None:
    mine = client.post("/v1/git-tokens", json={"scope": "user"}, headers=auth_header("alice"))
    token_id = mine.json()["token"]["token_id"]
    default_id = client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"][0][
        "token_id"
    ]
    # A member may not revoke the tenant default...
    assert client.delete(f"/v1/git-tokens/{default_id}", headers=auth_header("alice")).status_code == 403
    # ...nor another member's own token; an admin may.
    root_token = client.post("/v1/git-tokens", json={"scope": "user"}, headers=auth_header("root"))
    root_id = root_token.json()["token"]["token_id"]
    assert client.delete(f"/v1/git-tokens/{root_id}", headers=auth_header("alice")).status_code == 403
    revoked = client.delete(f"/v1/git-tokens/{token_id}", headers=auth_header("alice"))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["token"]["state"] == "revoked"
    assert client.delete(f"/v1/git-tokens/{default_id}", headers=auth_header("root")).status_code == 200
    # A revoked default is not re-created by the next read.
    listed = {t["token_id"]: t["state"]
              for t in client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"]}
    assert listed[default_id] == "revoked"
    # Registering the slot again makes it usable again.
    again = client.post("/v1/git-tokens", json={"scope": "tenant"}, headers=auth_header("root"))
    assert again.status_code == 200 and again.json()["token"]["state"] == "unverified"


# -- nothing token-shaped anywhere -------------------------------------------


def test_no_response_or_log_carries_a_token(client, eng_with_git, db, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    # A last4 known from an earlier rotation is served; nothing more of a value is.
    texts = [client.get("/v1/git-tokens", headers=auth_header("alice")).text]
    default_id = token_id_for("eng", "tenant", "")
    db.docs[f"{COLLECTION}/{default_id}"]["last4"] = FAKE_VALUE[-4:]
    for method, path, body, user in (
        ("post", "/v1/git-tokens", {"scope": "repository", "repo_id": REPO}, "root"),
        ("post", "/v1/git-tokens", {"scope": "user"}, "alice"),
        ("post", "/v1/git-tokens", {"scope": "user", "value": FAKE_VALUE}, "alice"),
        ("get", "/v1/git-tokens", None, "alice"),
        ("get", f"/v1/git-tokens/{default_id}", None, "alice"),
        ("delete", f"/v1/git-tokens/{default_id}", None, "root"),
    ):
        response = client.request(method, path, json=body, headers=auth_header(user))
        texts.append(response.text)
    listed = client.get("/v1/git-tokens", headers=auth_header("alice")).json()["tokens"]
    assert {t["token_id"]: t["last4"] for t in listed}[default_id] == FAKE_VALUE[-4:]
    for text in texts:
        assert_no_token_shape(text)
    for record in caplog.records:
        assert_no_token_shape(record.getMessage())
