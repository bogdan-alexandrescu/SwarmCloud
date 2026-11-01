"""Admin › Setup's Sign-in client step, swarm-api's side (issue #2964).

`POST /v1/admin/setup/sign-in-client` takes the Desktop OAuth client's id and
secret: the secret becomes a new version of `swarm-iap-desktop-client-secret`
and is never recorded or served; the id lands in `console/sign_in_client`,
which `GET /v1/admin/setup` serves as `sign_in_client` with the deployment's
own Google Cloud console links.
"""

from __future__ import annotations

import secrets

from swarm_api.routes import setup as setup_routes

from .conftest import PROJECT, auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    db,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    seed_engine_document,
    setup_app,
    setup_client,
    tokens,
)

ROUTE = "/v1/admin/setup/sign-in-client"
DOC = f"swarm-install/{ENV}/console/sign_in_client"


def _client_id() -> str:
    return f"{secrets.randbelow(10**12)}-{secrets.token_hex(8)}.apps.googleusercontent.com"


def test_the_page_waits_for_the_client_and_links_this_projects_console(setup_client, db) -> None:
    seed_engine_document(db)
    page = setup_client.get("/v1/admin/setup", headers=auth_header("root"))
    assert page.status_code == 200, page.text
    step = page.json()["sign_in_client"]
    assert step["state"] == "waiting"
    assert step["client_id"] is None
    assert step["links"] == {
        "branding": f"https://console.cloud.google.com/auth/branding?project={PROJECT}",
        "audience": f"https://console.cloud.google.com/auth/audience?project={PROJECT}",
        "create_client": f"https://console.cloud.google.com/auth/clients/create?project={PROJECT}",
    }


def test_saving_writes_the_secret_and_records_only_the_id(setup_client, db, fake_secrets) -> None:
    seed_engine_document(db)
    client_id, client_secret = _client_id(), secrets.token_hex(12)
    answer = setup_client.post(ROUTE, json={"client_id": client_id, "client_secret": client_secret},
                               headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["state"] == "saved"
    assert body["client_id"] == client_id
    assert body["saved_by"]
    assert client_secret not in answer.text
    assert fake_secrets.versions[setup_routes.SIGN_IN_CLIENT_SECRET] == [client_secret]
    assert set(db.docs[DOC]) == {"client_id", "saved_at", "saved_by"}
    assert client_secret not in repr(db.docs)

    page = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()
    assert page["sign_in_client"]["state"] == "saved"
    assert page["sign_in_client"]["client_id"] == client_id
    assert client_secret not in repr(page)


def test_an_id_that_is_not_a_desktop_client_is_refused_and_nothing_is_written(
        setup_client, db, fake_secrets) -> None:
    answer = setup_client.post(ROUTE, json={"client_id": "not-a-client",
                                            "client_secret": secrets.token_hex(12)},
                               headers=auth_header("root"))
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "client_id_invalid"
    assert fake_secrets.versions == {}
    assert DOC not in db.docs


def test_a_failed_secret_write_records_no_id(setup_client, db, fake_secrets) -> None:
    fake_secrets.fail = True
    answer = setup_client.post(ROUTE, json={"client_id": _client_id(),
                                            "client_secret": secrets.token_hex(12)},
                               headers=auth_header("root"))
    assert answer.status_code == 503, answer.text
    assert DOC not in db.docs


def test_only_a_platform_admin_saves_the_client(setup_client, db, fake_secrets) -> None:
    for user in ("wadmin", "alice", "carol"):
        answer = setup_client.post(ROUTE, json={"client_id": _client_id(),
                                                "client_secret": secrets.token_hex(12)},
                                   headers=auth_header(user))
        assert answer.status_code == 403, (user, answer.text)
    assert fake_secrets.versions == {}
    assert DOC not in db.docs
