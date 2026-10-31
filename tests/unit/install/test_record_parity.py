"""The install record's two tables hold the same rows (DAYONE-SETUP-API-2).

`swarm_api.install_record.SWARM_API_DOCUMENTS` restates swarm-api's rows of
`swarm_install.record.WRITERS`, because swarm-api's image does not carry the
engine. A field one side adds and the other does not is a write the other
refuses or a field the engine never reads, so the two are held equal here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _app in ("swarm-install", "swarm-api"):
    _path = str(_ROOT / "apps" / _app)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from swarm_install import record as engine_record  # noqa: E402

from swarm_api import install_record as api_record  # noqa: E402


def _api_rows() -> dict[str, frozenset[str]]:
    return {path: fields for path, (writer, fields) in engine_record.WRITERS.items()
            if writer in (engine_record.SWARM_API, engine_record.SWARM_API_FOR_DOCTOR)}


def test_swarm_api_rows_are_equal_on_both_sides() -> None:
    assert api_record.SWARM_API_DOCUMENTS == _api_rows()


def test_the_engine_document_is_the_engines_alone() -> None:
    assert engine_record.WRITERS[api_record.ENGINE_DOCUMENT][0] == engine_record.ENGINE
    assert api_record.ENGINE_DOCUMENT not in api_record.SWARM_API_DOCUMENTS


def test_the_console_verify_runs_document_is_named_alike() -> None:
    """The INTERFACE SHEET's `verify_runs`: one console document both sides
    name the same, holding the field the engine copies into its outputs."""
    assert api_record.VERIFY_RUNS == engine_record.VERIFY_RUNS == "verify_runs"
    path = f"swarm-install/{{env}}/console/{api_record.VERIFY_RUNS}"
    assert "verify_runs" in api_record.SWARM_API_DOCUMENTS[path]
    assert "verify_runs" in _api_rows()[path]


def test_every_document_both_sides_name_is_a_path_firestore_accepts() -> None:
    """Audit 3, A2: a document path has an even number of segments (the
    real client's `verify_path`); `console/teams/<id>` had five and 500'd
    Admin › Setup on every load. A team document's collection is odd."""
    for path in {*api_record.SWARM_API_DOCUMENTS, *engine_record.WRITERS}:
        assert len(path.split("/")) % 2 == 0, path
    teams = api_record.InstallRecord(None, "dev").team_path("t1").rpartition("/")[0]
    assert len(teams.split("/")) % 2 == 1, teams


def test_the_github_app_document_carries_every_field_pass_3_reads() -> None:
    """E2E-1 (DAYONE-APP-NAME): infra pass 3 copies `github_switches.APP_FIELDS`
    (`name` among them) from the console's `github_app` document into its
    tfvars, so both sides list each of them as a field swarm-api writes."""
    from swarm_install.steps.github_switches import APP_FIELDS

    path = "swarm-install/{env}/console/github_app"
    assert set(APP_FIELDS) <= api_record.SWARM_API_DOCUMENTS[path]
    assert set(APP_FIELDS) <= _api_rows()[path]


def test_the_first_teams_grant_is_a_team_field_on_both_sides() -> None:
    path = "swarm-install/{env}/teams/{id}"
    assert {"grant", "smoke"} <= api_record.SWARM_API_DOCUMENTS[path]
    assert {"grant", "smoke"} <= _api_rows()[path]


# --------------------------------------------------------------------------
# the install's sizing and mode (DAYONE-SETUP-API-3)
# --------------------------------------------------------------------------


SIZING = {
    "mode": "individual", "members": 1,
    "pool_limits": {"global": 4, "default_tenant": 2, "provider_tenant": 2,
                    "resource_classes": {"small": 2}, "runner_profiles": {"claude-code": 2},
                    "backends": {"gke": 2}},
    "service_max_instances": {"swarm-api": 2, "swarm-scheduler": 1,
                              "swarm-quota-broker": 1, "swarm-reconciler": 1},
    "pool_accounts": 1, "budget_usd": 150,
}


def _state(**outputs):
    from swarm_install.state import InstallState

    return InstallState(project="acme-swarm", environment="prod", release="1.0.0",
                        region="us-central1", hostname="swarm.example.com", outputs=outputs)


def test_the_engine_writes_the_installs_sizing_and_installer() -> None:
    """The INTERFACE SHEET's `state.outputs["sizing"]` goes into the engine's
    document as `sizing`, whole, with the installer's email beside it: the two
    things Admin › Setup reads to know a one-person install from a team's."""
    record = engine_record.InstallRecord(env="prod", documents=None)  # type: ignore[arg-type]
    data = record.document_for(_state(sizing=SIZING, installer="ada@example.com"), {})
    assert data["sizing"] == SIZING
    assert data["installer"] == "ada@example.com"
    engine_row = engine_record.WRITERS[api_record.ENGINE_DOCUMENT][1]
    assert set(data) <= engine_row


def test_an_install_with_no_sizing_yet_writes_none() -> None:
    record = engine_record.InstallRecord(env="prod", documents=None)  # type: ignore[arg-type]
    data = record.document_for(_state(), {})
    assert data["sizing"] is None and data["installer"] is None


def test_every_engine_field_swarm_api_reads_is_in_the_engines_row() -> None:
    """swarm-api reads the engine's document; each field it names there is one
    the engine writes, or the page shows a value that never arrives."""
    engine_row = engine_record.WRITERS[api_record.ENGINE_DOCUMENT][1]
    assert {"sizing", "installer"} <= api_record.ENGINE_FIELDS_READ <= engine_row
    assert set(api_record.SIZING_FIELDS) == set(SIZING)


def test_the_personal_workspace_document_is_named_alike() -> None:
    """A one-person install's first workspace, its grant and its smoke live in
    `console/personal`, a swarm-api document on both sides."""
    assert api_record.PERSONAL == engine_record.PERSONAL == "personal"
    path = f"swarm-install/{{env}}/console/{api_record.PERSONAL}"
    assert {"owner", "workspace", "grant", "smoke"} <= api_record.SWARM_API_DOCUMENTS[path]
    assert api_record.SWARM_API_DOCUMENTS[path] == _api_rows()[path]
