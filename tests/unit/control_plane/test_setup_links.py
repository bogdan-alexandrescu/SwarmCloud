"""One-time links from Admin › Setup (docs/design/day-one-plan.md §5.2, §5.3;
review finding 21; review-fix finding 1).

One route mints three kinds. A link is for one named person, opens once and
expires 24 hours after it is minted. A pool-account link exists only once the
first team does, only for a member of its group, and its sign-in lands in that
team through the existing `/v1/accounts/authorize` -> `/exchange` pair.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta

import pytest

from google.api_core import exceptions as gexc

from swarm_api.errors import UpstreamUnavailable
from swarm_api.setup_github import SecretManagerSetupSecrets
from swarm_api.setup_links import COLLECTION, LINK_TTL, digest

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ALICE,
    CAROL,
    ENV,
    TEAM_GROUP,
    create_team,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

LINKS = "/v1/admin/setup/links"
REDEEM = "/v1/setup/links/redeem"
ACCOUNTS_DOC = f"swarm-install/{ENV}/console/accounts"


def _mint(client, kind: str, label: str, owner: str = ALICE):
    return client.post(LINKS, json={"kind": kind, "label": label, "owner": owner},
                       headers=auth_header("root"))


def _token(response) -> str:
    assert response.status_code == 201, response.text
    return response.json()["path"].rsplit("/", 1)[1]


def _team(client, db) -> str:
    response = create_team(client)
    assert response.status_code in (200, 202), response.text
    return response.json()["team"]


def test_link_single_use_and_expires_24h(setup_client, db, fake_secrets) -> None:
    minted = _mint(setup_client, "addon-secret", "tailscale")
    token = _token(minted)
    record = db.docs[f"{COLLECTION}/{digest(token)}"]
    assert record["expires_at"] - record["created_at"] == timedelta(hours=24) == LINK_TTL
    assert token not in json.dumps(db.docs, default=str)
    first = setup_client.post(REDEEM, json={"token": token, "value": secrets.token_hex(16)},
                              headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    second = setup_client.post(REDEEM, json={"token": token, "value": secrets.token_hex(16)},
                               headers=auth_header("alice"))
    assert second.status_code == 404
    assert len(fake_secrets.versions["swarm-tailscale-oauth"]) == 1

    stale = _token(_mint(setup_client, "addon-secret", "typesafe"))
    ref = f"{COLLECTION}/{digest(stale)}"
    db.docs[ref]["expires_at"] = datetime.now(db.docs[ref]["created_at"].tzinfo) \
        - timedelta(seconds=1)
    expired = setup_client.post(REDEEM, json={"token": stale, "value": secrets.token_hex(16)},
                                headers=auth_header("alice"))
    assert expired.status_code == 404
    assert "swarm-typesafe-fwd-key" not in fake_secrets.versions


def test_link_bound_to_named_account_owner(setup_client, db, fake_secrets) -> None:
    """Someone else's verified email is refused and does not burn the link."""
    token = _token(_mint(setup_client, "addon-secret", "brave-search"))
    value = secrets.token_hex(16)
    other = setup_client.post(REDEEM, json={"token": token, "value": value},
                              headers=auth_header("carol"))
    assert other.status_code == 403, other.text
    assert fake_secrets.versions == {}
    assert db.docs[f"{COLLECTION}/{digest(token)}"]["used_at"] is None
    own = setup_client.post(REDEEM, json={"token": token, "value": value},
                            headers=auth_header("alice"))
    assert own.status_code == 200, own.text


def test_link_kinds_pool_account_team_git_addon_secret(setup_client, db, fake_secrets,
                                                       caplog) -> None:
    """One route serves all three kinds; a value never comes back, nor is it
    logged or stored outside its one container."""
    caplog.set_level(logging.DEBUG)
    team = _team(setup_client, db)
    pool_link = _mint(setup_client, "pool-account", "pool-1")
    git_link = _mint(setup_client, "team-git", team)
    addon_link = _mint(setup_client, "addon-secret", "bright-data-egress")
    assert {r.json()["kind"] for r in (pool_link, git_link, addon_link)} == {
        "pool-account", "team-git", "addon-secret"}
    assert _mint(setup_client, "printer", "x").status_code == 422
    assert _mint(setup_client, "addon-secret", "not-an-addon").status_code == 422

    git_value, addon_value = secrets.token_hex(20), secrets.token_hex(20)
    git = setup_client.post(REDEEM, json={"token": _token(git_link), "value": git_value},
                            headers=auth_header("alice"))
    addon = setup_client.post(REDEEM, json={"token": _token(addon_link), "value": addon_value},
                              headers=auth_header("alice"))
    assert git.status_code == 200 and addon.status_code == 200, (git.text, addon.text)
    assert fake_secrets.versions[f"swarm-tenant-{team}-git"] == [git_value]
    assert fake_secrets.versions["swarm-egress-credential"] == [addon_value]
    stored = json.dumps(db.docs, default=str)
    for value in (git_value, addon_value):
        assert value not in git.text + addon.text
        assert value not in caplog.text
        assert value not in stored
    accounts = db.docs[ACCOUNTS_DOC]
    assert accounts["pool-1"]["owner"] == ALICE
    assert accounts["pool-1"]["link_hash"] == digest(_token(pool_link))


class _SecretManagerClient:
    """Secret Manager's create/add as a fresh install answers them: a version
    added to a container nobody created is NOT_FOUND. Any other call (an IAM
    grant) is an AttributeError, so the test sees it."""

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.versions: dict[str, list[bytes]] = {}

    def create_secret(self, request: dict) -> None:
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.containers:
            raise gexc.AlreadyExists("exists")
        self.containers[name] = request["secret"]

    def add_secret_version(self, request: dict) -> None:
        if request["parent"] not in self.containers:
            raise gexc.NotFound("no such secret")
        self.versions.setdefault(request["parent"], []).append(request["payload"]["data"])


def test_team_git_container_created_on_a_fresh_install(setup_client, db, fake_secrets) -> None:
    """Nothing on an install makes `swarm-tenant-<tenant>-git`: the redemption
    creates it, labelled, keeps an existing one, and grants no accessor."""
    team = _team(setup_client, db)
    value = secrets.token_hex(20)
    git = setup_client.post(REDEEM, json={"token": _token(_mint(setup_client, "team-git", team)),
                                          "value": value}, headers=auth_header("alice"))
    assert git.status_code == 200, git.text
    secret_id = git.json()["secret"]
    tenant = secret_id.removeprefix("swarm-tenant-").removesuffix("-git")
    assert fake_secrets.created[secret_id] == {"managed-by": "swarm-api", "tenant": tenant}

    client = _SecretManagerClient()
    writer = SecretManagerSetupSecrets("p", client=client)
    with pytest.raises(UpstreamUnavailable):
        writer.add_version(secret_id, value)  # no container, no create: refused
    labels = {"managed-by": "swarm-api", "tenant": tenant}
    writer.add_version(secret_id, value, create_labels=labels)
    writer.add_version(secret_id, value, create_labels=labels)  # AlreadyExists is kept
    name = f"projects/p/secrets/{secret_id}"
    assert client.containers[name]["labels"] == labels
    assert client.versions[name] == [value.encode()] * 2


def test_form_link_survives_a_missing_value_or_a_failed_write(setup_client, db,
                                                              fake_secrets) -> None:
    """A form link is consumed only after its value is written."""
    token = _token(_mint(setup_client, "addon-secret", "tailscale"))
    ref = f"{COLLECTION}/{digest(token)}"
    empty = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert empty.status_code == 422, empty.text
    assert db.docs[ref]["used_at"] is None
    fake_secrets.fail = True
    failed = setup_client.post(REDEEM, json={"token": token, "value": secrets.token_hex(16)},
                               headers=auth_header("alice"))
    assert failed.status_code == 503, failed.text
    assert db.docs[ref]["used_at"] is None
    fake_secrets.fail = False
    done = setup_client.post(REDEEM, json={"token": token, "value": secrets.token_hex(16)},
                             headers=auth_header("alice"))
    assert done.status_code == 200, done.text
    assert db.docs[ref]["used_at"] is not None


def test_pool_link_refused_before_first_team(setup_client, db) -> None:
    response = _mint(setup_client, "pool-account", "pool-1")
    assert response.status_code == 409, response.text
    assert "create the first team first" in response.json()["message"]
    assert not any(k.startswith(COLLECTION + "/") for k in db.docs)


def test_pool_link_owner_must_be_team_member(setup_client, db, directory) -> None:
    _team(setup_client, db)
    refused = _mint(setup_client, "pool-account", "pool-1", owner=CAROL)
    assert refused.status_code == 403, refused.text
    assert (CAROL, (TEAM_GROUP,)) in directory.asked
    assert ACCOUNTS_DOC not in db.docs


def test_pool_link_sign_in_lands_in_first_team(setup_client, db, pool) -> None:
    """The owner redeems the link, then signs in through the EXISTING routes
    with the first team selected: the broker files the account there."""
    team = _team(setup_client, db)
    # swarm-workspace-apply finishes the workspace (its one writer, design §5.7).
    db.docs[f"workspaces/{team}"]["state"] = "ready"
    db.docs[f"workspaces/{team}"]["ready_at"] = datetime.now(
        db.docs[f"workspaces/{team}"]["requested_at"].tzinfo)
    token = _token(_mint(setup_client, "pool-account", "pool-1"))
    redeemed = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert redeemed.status_code == 200, redeemed.text
    body = redeemed.json()
    assert body["tenant"] == team
    selected = {**auth_header("alice"), body["sign_in"]["header"]: body["tenant"]}
    begun = setup_client.post(body["sign_in"]["authorize"], json={"label": body["label"]},
                              headers=selected)
    assert begun.status_code == 200, begun.text
    finished = setup_client.post(body["sign_in"]["exchange"],
                                 json={"state": begun.json()["state"], "code": "a-code#x"},
                                 headers=selected)
    assert finished.status_code == 201, finished.text
    assert [a["owner_tenant"] for a in pool.finished] == [team]
