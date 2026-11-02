"""swarm-api's side of the install record's one-writer table
(docs/design/day-one-plan.md §5.4; DAYONE-ENGINE-1's `swarm_install.record`).

`swarm_api.install_record.SWARM_API_DOCUMENTS` restates swarm-api's rows of
`swarm_install.record.WRITERS` (swarm-api's image does not carry the engine);
this test holds the two equal, and holds that the console's whole flow writes
under `swarm-install/` only the documents those rows name.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

_ENGINE = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-install")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from swarm_install.record import SWARM_API, SWARM_API_FOR_DOCTOR, WRITERS  # noqa: E402

from swarm_api import install_record as ir  # noqa: E402

from .conftest import auth_header  # noqa: E402
from .test_setup_routes import (  # noqa: E402,F401 - fixtures
    ALICE,
    ENV,
    create_team,
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


def test_table_matches_the_engines() -> None:
    engine_side = {path: fields for path, (writer, fields) in WRITERS.items()
                   if writer in (SWARM_API, SWARM_API_FOR_DOCTOR)}
    assert ir.SWARM_API_DOCUMENTS == engine_side
    assert ir.ENGINE_DOCUMENT in WRITERS
    assert ir.ENGINE_DOCUMENT not in ir.SWARM_API_DOCUMENTS


def test_a_field_outside_its_row_is_refused(db) -> None:
    record = ir.InstallRecord(db, ENV)
    with pytest.raises(ir.RecordFieldRefused):
        record.write(record.console_path(ir.GITHUB_APP), {"pem": "x"})
    with pytest.raises(ir.RecordFieldRefused):
        record.write(record.engine_path(), {"stages": []})
    with pytest.raises(ir.RecordFieldRefused):
        record.write(record.console_path(ir.ACCOUNTS), {"pool-1": {"token": "x"}})
    assert db.docs == {}


def _concrete(template: str, team_ids: set[str]) -> set[str]:
    path = template.replace("{env}", ENV)
    if "{id}" in path:
        return {path.replace("{id}", team) for team in team_ids}
    return {path}


def test_api_writes_only_console_documents(setup_client, db, github, fake_secrets) -> None:
    """The console's whole flow -- App, team, links, self-check, health, finish
    -- writes under `swarm-install/` only swarm-api's rows, each with only its
    row's fields; the engine's document is untouched."""
    engine = seed_engine_document(db)
    root = auth_header("root")
    github.conversion = {"id": 7, "slug": "s", "client_id": "c", "name": "n", "pem": secrets.token_hex(8),
                         "client_secret": secrets.token_hex(8),
                         "webhook_secret": secrets.token_hex(8)}
    start = setup_client.post("/v1/admin/setup/github-app/manifest", json={}, headers=root)
    state = start.json()["action"].rsplit("state=", 1)[1]
    assert setup_client.post("/v1/admin/setup/github-app/conversion",
                             json={"code": "c", "state": state}, headers=root).status_code == 200
    github.installations = [{"account": {"login": "example-org"}}]
    setup_client.get("/v1/admin/setup/github-app/installation", headers=root)
    team = create_team(setup_client).json()["team"]
    setup_client.post("/v1/admin/setup/links", headers=root,
                      json={"kind": "pool-account", "label": "pool-1", "owner": ALICE})
    setup_client.post("/v1/admin/setup/links", headers=root,
                      json={"kind": "team-git", "label": team, "owner": ALICE})
    setup_client.put("/v1/admin/setup/health", headers=auth_header("doctor"),
                     json={"run_at": "2026-10-29T12:00:00Z", "checks": [], "findings": []})
    setup_client.post("/v1/admin/setup/finish", headers=root)

    allowed: dict[str, frozenset[str]] = {}
    for template, fields in ir.SWARM_API_DOCUMENTS.items():
        for path in _concrete(template, {team}):
            allowed[path] = fields
    written = {k: v for k, v in db.docs.items() if k.startswith("swarm-install/")}
    assert written[f"swarm-install/{ENV}"] == engine
    del written[f"swarm-install/{ENV}"]
    assert written, "the flow wrote no console document"
    for path, doc in written.items():
        assert path in allowed, path
        if path.endswith("/console/accounts"):
            for value in doc.values():
                assert set(value) <= ir.ACCOUNT_FIELDS
        else:
            assert set(doc) <= allowed[path], (path, set(doc) - allowed[path])
