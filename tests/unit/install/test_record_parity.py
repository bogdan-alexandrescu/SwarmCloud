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


def test_the_first_teams_grant_is_a_team_field_on_both_sides() -> None:
    path = "swarm-install/{env}/console/teams/{id}"
    assert {"grant", "smoke"} <= api_record.SWARM_API_DOCUMENTS[path]
    assert {"grant", "smoke"} <= _api_rows()[path]
