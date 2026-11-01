"""Admin › Setup's Sign-in client step (issue #2964): `GET /v1/admin/setup`
serves `sign_in_client` and `POST /v1/admin/setup/sign-in-client` takes the
Desktop client's id and secret.

The secret goes to Secret Manager (`swarm-iap-desktop-client-secret`) and
nowhere else: never in the answer, the record, the audit line or any log line.
"""

from __future__ import annotations

import logging
import secrets

from swarm_api import install_record as ir

from .conftest import PROJECT, auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENGINE_DOC,
    ENV,
    ROOT,
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
SECRET = "swarm-iap-desktop-client-secret"
DOC = f"swarm-install/{ENV}/console/sign_in_client"


def client_id() -> str:
    return f"{secrets.randbelow(10**12)}-{secrets.token_hex(8)}.apps.googleusercontent.com"


def client_secret() -> str:
    # Built at run time: nothing in the repository looks like a credential.
    return "-".join(("GOCSPX", secrets.token_urlsafe(21)))


def test_the_page_serves_a_waiting_step_with_the_projects_links(setup_client, db) -> None:
    seed_engine_document(db)
    body = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()
    step = body["sign_in_client"]
    assert step["state"] == "waiting"
    assert step["client_id"] is None and step["saved_at"] is None and step["saved_by"] is None
    assert step["links"] == {
        "branding": f"https://console.cloud.google.com/auth/branding?project={PROJECT}",
        "audience": f"https://console.cloud.google.com/auth/audience?project={PROJECT}",
        "create_client": f"https://console.cloud.google.com/auth/clients/create?project={PROJECT}",
    }


def test_a_non_admin_is_refused_and_nothing_is_stored(setup_client, db, fake_secrets) -> None:
    for user in ("wadmin", "alice", "carol", "deployer", "doctor"):
        answer = setup_client.post(ROUTE, json={"client_id": client_id(), "client_secret": client_secret()},
                                   headers=auth_header(user))
        assert answer.status_code == 403, (user, answer.text)
    assert SECRET not in fake_secrets.versions
    assert DOC not in db.docs


def test_a_client_id_that_is_not_a_google_client_is_refused(setup_client, db, fake_secrets) -> None:
    for bad in ("not-a-client", "123-abc.apps.example.com", f"{client_id()} x", ""):
        answer = setup_client.post(ROUTE, json={"client_id": bad, "client_secret": client_secret()},
                                   headers=auth_header("root"))
        assert answer.status_code == 422, (bad, answer.text)
        assert answer.json()["code"] in ("client_id_invalid", "validation_failed"), answer.text
    answer = setup_client.post(ROUTE, json={"client_id": "x.apps.example.com", "client_secret": client_secret()},
                               headers=auth_header("root"))
    assert answer.json()["code"] == "client_id_invalid"
    assert SECRET not in fake_secrets.versions
    assert DOC not in db.docs


def test_a_saved_secret_never_appears_in_the_answer_the_record_or_a_log(setup_client, db, fake_secrets,
                                                                         caplog) -> None:
    seed_engine_document(db)
    first_id, first_secret = client_id(), client_secret()
    with caplog.at_level(logging.DEBUG):
        answer = setup_client.post(ROUTE, json={"client_id": first_id, "client_secret": first_secret},
                                   headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["state"] == "saved"
    assert body["client_id"] == first_id
    assert body["saved_by"] == ROOT and body["saved_at"]
    assert first_secret not in answer.text
    assert fake_secrets.versions[SECRET] == [first_secret]
    assert set(db.docs[DOC]) == {"client_id", "at", "by"}
    assert first_secret not in repr(db.docs)
    assert first_secret not in caplog.text
    # The audit line names the client and the person, never the secret.
    assert any(first_id in record.getMessage() and ROOT in record.getMessage() for record in caplog.records)

    page = setup_client.get("/v1/admin/setup", headers=auth_header("root"))
    assert page.json()["sign_in_client"]["state"] == "saved"
    assert first_secret not in page.text

    # A second save replaces the id and adds a new secret version.
    second_id, second_secret = client_id(), client_secret()
    answer = setup_client.post(ROUTE, json={"client_id": second_id, "client_secret": second_secret},
                               headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["client_id"] == second_id
    assert fake_secrets.versions[SECRET] == [first_secret, second_secret]
    assert db.docs[DOC]["client_id"] == second_id


def test_applied_once_the_engine_reports_the_client_in_iaps_list(setup_client, db) -> None:
    seed_engine_document(db)
    saved = client_id()
    setup_client.post(ROUTE, json={"client_id": saved, "client_secret": client_secret()},
                      headers=auth_header("root"))
    db.docs[ENGINE_DOC]["sign_in_client"] = {"client_id": saved}
    step = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()["sign_in_client"]
    assert step["state"] == "applied" and step["client_id"] == saved

    # A newer save waits for the next pass.
    newer = client_id()
    setup_client.post(ROUTE, json={"client_id": newer, "client_secret": client_secret()},
                      headers=auth_header("root"))
    step = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()["sign_in_client"]
    assert step["state"] == "saved" and step["client_id"] == newer


def test_saving_the_applied_client_again_answers_saved(setup_client, db, fake_secrets) -> None:
    """The contract: a save answers `saved`. A second save of the id the
    engine already applied (a new secret for the same client) is no
    exception: its new version is not taken in until the hand-off sees it."""
    seed_engine_document(db)
    saved = client_id()
    db.docs[ENGINE_DOC]["sign_in_client"] = {"client_id": saved}
    answer = setup_client.post(ROUTE, json={"client_id": saved, "client_secret": client_secret()},
                               headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "saved" and answer.json()["client_id"] == saved


def test_a_client_the_terminal_recorded_before_reads_applied(setup_client, db) -> None:
    """An install whose Desktop client came from the old terminal prompt has
    no console document; the engine's record still says it is applied."""
    seed_engine_document(db)
    recorded = client_id()
    db.docs[ENGINE_DOC]["sign_in_client"] = {"client_id": recorded}
    step = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()["sign_in_client"]
    assert step["state"] == "applied" and step["client_id"] == recorded


def test_the_record_row_names_the_three_fields() -> None:
    assert ir.SWARM_API_DOCUMENTS["swarm-install/{env}/console/sign_in_client"] == frozenset(
        {"client_id", "at", "by"})
    assert "sign_in_client" in ir.ENGINE_FIELDS_READ
