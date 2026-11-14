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
from swarm_api.setup_links import COLLECTION, LINK_TTL, VOICE_KEY_TTL, digest

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ALICE,
    CAROL,
    ROOT,
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


def _served_expiry(response) -> datetime:
    """The `expires_at` a refusal's detail carries, in universal time."""
    served = datetime.fromisoformat(response.json()["detail"]["expires_at"])
    assert served.utcoffset() == timedelta(0), served
    return served


def test_a_value_links_422_tells_its_owner_when_it_expires(setup_client, db,
                                                           fake_secrets) -> None:
    """The owner's pick (3 November 2026): the 422 that keeps a value link
    unused carries the link's `expires_at`, so the teammate's page draws the
    hours left on it."""
    token = _token(_mint(setup_client, "addon-secret", "tailscale"))
    record = db.docs[f"{COLLECTION}/{digest(token)}"]
    empty = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert empty.status_code == 422, empty.text
    assert _served_expiry(empty) == record["expires_at"]
    assert record["used_at"] is None


def test_a_pool_links_409_tells_its_owner_when_it_expires(setup_client, db) -> None:
    """The 409 for a workspace not ready yet keeps its workspace, state and
    retry, and adds the link's `expires_at`."""
    team = _team(setup_client, db)
    assert db.docs[f"workspaces/{team}"]["state"] != "ready"
    token = _token(_mint(setup_client, "pool-account", "pool-1"))
    record = db.docs[f"{COLLECTION}/{digest(token)}"]
    early = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("alice"))
    assert early.status_code == 409, early.text
    detail = early.json()["detail"]
    assert detail["workspace"] == team and detail["retry"] is True
    assert "workspace_state" in detail
    assert _served_expiry(early) == record["expires_at"]
    assert record["used_at"] is None


def test_no_expiry_reaches_a_probe_or_a_dead_link(setup_client, db, fake_secrets) -> None:
    """The 404 for a used, an expired or an unknown link stays one answer
    with no expiry, and someone else holding the token learns none either."""
    token = _token(_mint(setup_client, "addon-secret", "tailscale"))
    other = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("carol"))
    assert other.status_code == 403, other.text
    assert "expires_at" not in json.dumps(other.json())
    used = setup_client.post(REDEEM, json={"token": token, "value": secrets.token_hex(16)},
                             headers=auth_header("alice"))
    assert used.status_code == 200, used.text
    stale = _token(_mint(setup_client, "addon-secret", "typesafe"))
    ref = f"{COLLECTION}/{digest(stale)}"
    db.docs[ref]["expires_at"] = datetime.now(db.docs[ref]["created_at"].tzinfo) \
        - timedelta(seconds=1)
    answers = [setup_client.post(REDEEM, json={"token": t}, headers=auth_header("alice"))
               for t in (token, stale, secrets.token_hex(24))]
    assert [a.status_code for a in answers] == [404, 404, 404]
    assert len({json.dumps(a.json(), sort_keys=True) for a in answers}) == 1
    assert "expires_at" not in json.dumps(answers[0].json())


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


# -- Voice it's OpenAI API key (the owner, 4 November 2026) --------------------


def _voice_key() -> str:
    return "sk-proj-" + secrets.token_hex(24)


def _voice_link(client) -> str:
    return _token(_mint(client, "voice-api-key", "voice-key", owner=ROOT))


def test_voice_key_link_is_the_minting_admins_alone_for_ten_minutes(setup_client, db) -> None:
    minted = _mint(setup_client, "voice-api-key", "voice-key", owner=ROOT)
    token = _token(minted)
    record = db.docs[f"{COLLECTION}/{digest(token)}"]
    assert record["owner"] == ROOT and record["kind"] == "voice-api-key"
    assert record["expires_at"] - record["created_at"] == timedelta(minutes=10) == VOICE_KEY_TTL
    assert minted.json()["tenant"] == record["tenant_id"] and record["tenant_id"]
    # Minted for someone else: refused, and nothing is written.
    before = dict(db.docs)
    for_alice = _mint(setup_client, "voice-api-key", "voice-key", owner=ALICE)
    assert for_alice.status_code == 403, for_alice.text
    assert db.docs == before
    # Minted by a non-admin, even for themselves: refused by the admin gate.
    by_alice = setup_client.post(LINKS, json={"kind": "voice-api-key", "label": "voice-key",
                                              "owner": ALICE}, headers=auth_header("alice"))
    assert by_alice.status_code == 403, by_alice.text


@pytest.mark.parametrize("user", ["alice", "carol"])
def test_voice_key_link_refuses_anyone_but_the_minting_admin(setup_client, db, pool,
                                                            user) -> None:
    token = _voice_link(setup_client)
    other = setup_client.post(REDEEM, json={"token": token, "value": _voice_key()},
                              headers=auth_header(user))
    assert other.status_code == 403, other.text
    assert pool.registered == []
    assert db.docs[f"{COLLECTION}/{digest(token)}"]["used_at"] is None


def test_voice_key_link_refuses_a_second_use_and_an_expired_link(setup_client, db,
                                                                pool) -> None:
    token = _voice_link(setup_client)
    first = setup_client.post(REDEEM, json={"token": token, "value": _voice_key()},
                              headers=auth_header("root"))
    assert first.status_code == 200, first.text
    second = setup_client.post(REDEEM, json={"token": token, "value": _voice_key()},
                               headers=auth_header("root"))
    assert second.status_code == 404, second.text
    assert len(pool.registered) == 1

    stale = _voice_link(setup_client)
    ref = f"{COLLECTION}/{digest(stale)}"
    # Ten minutes have passed: the stored expiry is behind the clock.
    db.docs[ref]["expires_at"] = datetime.now(db.docs[ref]["created_at"].tzinfo) \
        - timedelta(seconds=1)
    expired = setup_client.post(REDEEM, json={"token": stale, "value": _voice_key()},
                                headers=auth_header("root"))
    assert expired.status_code == 404, expired.text
    assert len(pool.registered) == 1


def test_voice_key_reaches_the_broker_once_and_is_kept_nowhere(setup_client, db, pool,
                                                              caplog) -> None:
    caplog.set_level(logging.DEBUG)
    token = _voice_link(setup_client)
    tenant = db.docs[f"{COLLECTION}/{digest(token)}"]["tenant_id"]
    # Without its value the link is refused and stays unused.
    bare = setup_client.post(REDEEM, json={"token": token}, headers=auth_header("root"))
    assert bare.status_code == 422, bare.text
    assert db.docs[f"{COLLECTION}/{digest(token)}"]["used_at"] is None
    key = _voice_key()
    opened = setup_client.post(REDEEM, json={"token": token, "value": key},
                               headers=auth_header("root"))
    assert opened.status_code == 200, opened.text
    assert pool.registered == [{"owner_tenant": tenant, "label": "voice-key",
                                "provider": "openai-api", "lend_to": [], "credential": key,
                                "added_by": "root@example.com"}]
    body = opened.json()
    assert body["kind"] == "voice-api-key" and body["account_id"] == f"{tenant}:voice-key"
    assert body["pool"] == "voice-it" and body["written"] is True
    assert key not in opened.text
    assert key not in caplog.text
    assert key not in json.dumps(db.docs, default=str)
    again = setup_client.post(REDEEM, json={"token": token, "value": key},
                              headers=auth_header("root"))
    assert again.status_code == 404 and key not in again.text
    assert len(pool.registered) == 1


def test_voice_key_the_broker_refuses_is_not_echoed_and_spends_the_link(setup_client, db,
                                                                       pool) -> None:
    """The broker's refusal reaches the admin in its own words, which never
    quote the key; the link is spent, so the value is never forwarded twice."""
    from swarm_api.errors import ValidationFailed

    def refuse(**_: object) -> dict:
        raise ValidationFailed("an OpenAI API key begins with 'sk-'; this is not one")

    pool.register = refuse
    token = _voice_link(setup_client)
    pasted = secrets.token_hex(24)
    refused = setup_client.post(REDEEM, json={"token": token, "value": pasted},
                                headers=auth_header("root"))
    assert refused.status_code == 422, refused.text
    assert pasted not in refused.text
    assert db.docs[f"{COLLECTION}/{digest(token)}"]["used_at"] is not None


# ---- replacing the dedicated key keeps its owner workspace ------------------------------


def _dedicated(tenant: str, label: str = "voice-key") -> dict:
    """The broker's row for Voice it's dedicated key, owned by `tenant`."""
    return {"account_id": f"{tenant}:{label}", "owner_tenant": tenant, "label": label,
            "provider": "openai-api", "voice_dedicated": True, "pools": ["voice-it"]}


def test_a_replace_by_an_admin_of_another_workspace_files_the_key_under_its_owner(
        setup_client, db, pool) -> None:
    first = _mint(setup_client, "voice-api-key", "probe", owner=ROOT)
    own = first.json()["tenant"]
    owner = "voice-home" if own != "voice-home" else "voice-home-2"
    pool.accounts.append(_dedicated(owner))

    minted = _mint(setup_client, "voice-api-key", "voice-key", owner=ROOT)
    token = _token(minted)
    assert db.docs[f"{COLLECTION}/{digest(token)}"]["tenant_id"] == owner != own
    assert minted.json()["tenant"] == owner
    key = _voice_key()
    opened = setup_client.post(REDEEM, json={"token": token, "value": key},
                               headers=auth_header("root"))
    assert opened.status_code == 200, opened.text
    assert [r["owner_tenant"] for r in pool.registered] == [owner]
    assert opened.json()["account_id"] == f"{owner}:voice-key"


def test_a_first_key_keeps_the_minting_admins_workspace(setup_client, db, pool) -> None:
    # A shared key in the Voice it pool is no dedicated key: still a first key.
    pool.accounts.append({**_dedicated("voice-home", "shared"), "voice_dedicated": False})
    minted = _mint(setup_client, "voice-api-key", "voice-key", owner=ROOT)
    token = _token(minted)
    tenant = db.docs[f"{COLLECTION}/{digest(token)}"]["tenant_id"]
    assert tenant and tenant != "voice-home"
    assert minted.json()["tenant"] == tenant


def test_a_voice_key_link_is_not_minted_while_the_broker_cannot_say_who_owns_the_key(
        setup_client, db, pool) -> None:
    pool.listing_fails = True
    before = dict(db.docs)
    refused = _mint(setup_client, "voice-api-key", "voice-key", owner=ROOT)
    assert refused.status_code == 503, refused.text
    assert {k: v for k, v in db.docs.items() if k.startswith(COLLECTION)} == \
        {k: v for k, v in before.items() if k.startswith(COLLECTION)}


@pytest.mark.parametrize("label", ["pool-", "a" * 41, "-pool"])
def test_a_label_the_quota_broker_refuses_is_refused_at_mint(setup_client, label) -> None:
    """The broker's label rule (`quota_broker.accounts._LABEL`): at most 40
    characters, ending alphanumeric; a link minted past it could never land."""
    assert _mint(setup_client, "pool-account", label).status_code == 422
    assert _mint(setup_client, "pool-account", "a" * 40).status_code != 422


def test_a_redeemed_team_git_link_marks_the_slot_written(setup_client, db, fake_secrets) -> None:
    """Once the team's git secret is written, the team's `progress.git_link`
    says so (never the value), and the console's Team git slot reads done."""
    team = _team(setup_client, db)
    value = secrets.token_hex(20)
    git = setup_client.post(REDEEM, json={"token": _token(_mint(setup_client, "team-git", team)),
                                          "value": value}, headers=auth_header("alice"))
    assert git.status_code == 200, git.text
    doc = next(d for path, d in db.docs.items() if path.endswith(f"/teams/{team}"))
    link = doc["progress"]["git_link"]
    assert link["written_at"] is not None and link["written_by"] == ALICE
    assert value not in json.dumps(db.docs, default=str)
