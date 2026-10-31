"""The GitHub App manifest flow, run by swarm-api (docs/design/day-one-plan.md
§0 R3, §5.3; design §9.1; lesson 46; review finding 10).

`state` is checked before GitHub is called; GitHub's `pem`, `client_secret`
and `webhook_secret` go straight into Secret Manager and appear in no answer,
log record or Firestore field; the manifest asks for `statuses: read` and
calls back to the deployment's own host; the webhook stays off until its
secret has a version, and swarm-api signs the App JWT that turns it on.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from swarm_api.setup_github import PERMISSIONS

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    HOOKS_URL,
    HOSTNAME,
    ORG,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    setup_app,
    setup_client,
    tokens,
)

GITHUB_APP_DOC = f"swarm-install/{ENV}/console/github_app"
MANIFEST = "/v1/admin/setup/github-app/manifest"
CONVERSION = "/v1/admin/setup/github-app/conversion"
HOOK = "/v1/admin/setup/github-app/hook"


def _start(client) -> tuple[dict, str]:
    response = client.post(MANIFEST, json={}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    state = parse_qs(urlsplit(body["action"]).query)["state"][0]
    return body, state


def _conversion_answer() -> dict:
    return {
        "id": 4242, "slug": "swarmcloud-test", "client_id": "Iv1.example",
        "name": "SwarmCloud test", "pem": secrets.token_hex(64), "client_secret": secrets.token_hex(20),
        "webhook_secret": secrets.token_hex(20), "owner": {"login": ORG},
    }


def _firestore_text(db) -> str:
    return json.dumps({k: v for k, v in db.docs.items()}, default=str)


def test_state_checked_before_exchange(setup_client, github, fake_secrets, db) -> None:
    """A callback whose state is not the one issued never reaches GitHub, and
    writes nothing; one with no manifest started is refused the same way."""
    github.conversion = _conversion_answer()
    refused = setup_client.post(CONVERSION, json={"code": "c0de", "state": "never-issued"},
                                headers=auth_header("root"))
    assert refused.status_code == 403, refused.text
    _start(setup_client)
    github.requests.clear()  # the start's account lookup; the exchange is what is checked
    wrong = setup_client.post(CONVERSION, json={"code": "c0de", "state": secrets.token_hex(8)},
                              headers=auth_header("root"))
    assert wrong.status_code == 403
    assert refused.json()["code"] == "forbidden"
    assert "state is not the one Admin › Setup issued" in refused.json()["message"]
    assert github.requests == []
    assert fake_secrets.versions == {}
    assert "id" not in db.docs[GITHUB_APP_DOC]


def test_exchange_writes_three_secrets_and_returns_no_value(setup_client, github, fake_secrets,
                                                           db, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    answer = _conversion_answer()
    github.conversion = answer
    _, state = _start(setup_client)
    github.requests.clear()  # the start's account lookup; the exchange is what is checked
    response = setup_client.post(CONVERSION, json={"code": "c0de", "state": state},
                                 headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 4242, "slug": "swarmcloud-test"}
    assert [r["url"] for r in github.requests] == [
        "https://api.github.com/app-manifests/c0de/conversions"]
    assert fake_secrets.versions == {
        "swarm-github-app-private-key": [answer["pem"]],
        "swarm-github-app-client-secret": [answer["client_secret"]],
        "swarm-github-app-webhook-secret": [answer["webhook_secret"]],
    }
    doc = db.docs[GITHUB_APP_DOC]
    assert doc["id"] == 4242 and doc["slug"] == "swarmcloud-test"
    assert doc["client_id"] == "Iv1.example"
    # E2E-1 (DAYONE-APP-NAME): infra pass 3 renders github_app_name from it.
    assert doc["name"] == "SwarmCloud test"
    assert doc["secrets_written_at"] is not None
    assert doc["state_hash"] is None
    stored = _firestore_text(db)
    for field in ("pem", "client_secret", "webhook_secret"):
        value = answer[field]
        assert value not in response.text, field
        assert value not in caplog.text, field
        assert value not in stored, field
    # The state is single use: the same callback again is refused.
    again = setup_client.post(CONVERSION, json={"code": "c0de", "state": state},
                              headers=auth_header("root"))
    assert again.status_code == 403


def test_manifest_requests_statuses_read(setup_client, db) -> None:
    """Lesson 46: the live App lacked Commit statuses. The callback is derived
    from the deployment's hostname; the webhook is registered off."""
    body, state = _start(setup_client)
    manifest = body["manifest"]
    assert manifest["default_permissions"]["statuses"] == "read"
    assert manifest["default_permissions"] == PERMISSIONS
    assert manifest["redirect_url"] == f"https://{HOSTNAME}/admin/setup/github-app/callback"
    assert manifest["url"] == f"https://{HOSTNAME}"
    assert all(url.startswith(f"https://{HOSTNAME}/") for url in manifest["callback_urls"])
    assert manifest["hook_attributes"] == {"url": HOOKS_URL, "active": False}
    assert manifest["public"] is False
    assert body["action"].startswith(f"https://github.com/organizations/{ORG}/settings/apps/new?")
    # Only the state's hash is kept.
    assert state not in _firestore_text(db)
    assert db.docs[GITHUB_APP_DOC]["state_hash"]


def _created(setup_client, github) -> None:
    github.conversion = _conversion_answer()
    _, state = _start(setup_client)
    assert setup_client.post(CONVERSION, json={"code": "c0de", "state": state},
                             headers=auth_header("root")).status_code == 200
    github.requests.clear()


def test_hook_inactive_until_secret_has_version(setup_client, github, fake_secrets) -> None:
    """Key before switch: with no webhook secret version, the hook route is a
    409 and GitHub is not called."""
    assert fake_secrets.versions == {}
    response = setup_client.post(HOOK, headers=auth_header("deployer"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert "has no enabled version" in response.json()["message"]
    assert github.requests == []


def test_hook_activation_signed_server_side(setup_client, github, fake_secrets) -> None:
    """The engine's request carries no key material and the answer holds none;
    swarm-api signs the App JWT with the key only it reads (review finding 10)."""
    _created(setup_client, github)
    response = setup_client.post(HOOK, headers=auth_header("deployer"))
    assert response.status_code == 200, response.text
    assert response.json() == {"active": True}
    [call] = github.requests
    assert call["method"] == "PATCH"
    assert call["url"] == "https://api.github.com/app/hook/config"
    sent = json.loads(call["body"])
    assert sent["url"] == HOOKS_URL and sent["active"] is True
    assert "secret" not in sent
    jwt = call["headers"]["Authorization"].removeprefix("Bearer ")
    header, claims, signature = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(claims)))["iss"] == "4242"
    fake_secrets.key.public_key().verify(
        base64.urlsafe_b64decode(pad(signature)), f"{header}.{claims}".encode(),
        padding.PKCS1v15(), hashes.SHA256())
    pem = fake_secrets.private_key()
    assert pem not in response.text
    assert "PRIVATE KEY" not in response.text


def test_installation_polled_with_app_jwt(setup_client, github, db) -> None:
    _created(setup_client, github)
    github.installations = [{"account": {"login": ORG}}]
    response = setup_client.get("/v1/admin/setup/github-app/installation",
                                headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["installed"] is True
    assert db.docs[GITHUB_APP_DOC]["installed_on"] == ORG
