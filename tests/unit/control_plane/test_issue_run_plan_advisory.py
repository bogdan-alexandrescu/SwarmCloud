"""The planner's Jev advisory, served on the run read (CLD's Jev report: plan coverage).

The planner's worker writes `plan-advisory.json` (J2's uncovered requirements,
J5's likely duplicate). Until now only the hold read it, so nobody saw a flag.
`GET /v1/runs/{id}` serves it as `plan_advisory`, read from the artifact when
the run is read; the list serves it on the rows that wait for an approval.
Read-only: serving it moves nothing and writes nothing to the run document.
"""

from __future__ import annotations

from swarm_api.routes import runs as runs_routes

from .conftest import auth_header
from .test_issue_run_jev_hold import _advise
from .test_issue_run_tick import (  # noqa: F401 -- fixtures are used by name
    _stored,
    api_context,
    client,
    github,
)
from .test_issue_runs import PLAN, _create, _finish_planner, _run

ADVISED = {
    "schema": "swarm.plan-advisory/v1", "advisory": True, "hold_for_human": True,
    "coverage": {"requirements": 3, "flagged": [
        {"index": 1, "requirement": "The sort survives a reload"},
        {"index": 2, "requirement": "Empty names sort last", "coverage": "weak"},
    ]},
    "duplicate": {"title": "Sort widgets by name", "ref": "acme/widgets#41", "p": 0.91},
}


def _planned_with(client, db, objects, advisory, **body) -> dict:
    run = _create(client, **body).json()["run"]
    _finish_planner(db, objects, run, PLAN)
    if advisory is not None:
        _advise(db, objects, run, advisory)
    return run


def test_a_run_read_carries_the_planners_advisory(client, db, objects):
    run = _planned_with(client, db, objects, ADVISED)

    served = _run(client, run["id"]).json()["run"]

    assert served["state"] == "PLANNED"
    assert served["plan_advisory"] == {
        "coverage": {"requirements": 3, "flagged": [
            {"index": 1, "requirement": "The sort survives a reload", "coverage": "none"},
            {"index": 2, "requirement": "Empty names sort last", "coverage": "weak"},
        ]},
        "duplicate": {"title": "Sort widgets by name", "ref": "acme/widgets#41",
                      "url": "https://github.com/acme/widgets/issues/41", "p": 0.91},
        "hold_for_human": True,
        "judged_revision": 1,
    }
    # Read-only: the advisory is served, never stored on the run.
    assert "plan_advisory" not in _stored(db, run["id"])


def test_a_run_without_an_advisory_carries_none(client, db, objects):
    run = _planned_with(client, db, objects, None)

    served = _run(client, run["id"]).json()["run"]

    assert served["state"] == "PLANNED"
    assert served["plan_advisory"] is None


def test_a_run_with_no_plan_yet_reads_no_artifact(client, db, objects):
    run = _create(client).json()["run"]

    served = _run(client, run["id"]).json()["run"]

    assert served["plan"] is None
    assert served["plan_advisory"] is None


def test_a_malformed_or_non_advisory_file_is_none(client, db, objects):
    for advisory in ("{not json", {**ADVISED, "advisory": False}, ["advisory"]):
        run = _planned_with(client, db, objects, advisory)
        assert _run(client, run["id"]).json()["run"]["plan_advisory"] is None


def test_what_is_served_is_only_the_shaped_fields(client, db, objects):
    noisy = {
        **ADVISED,
        "secret_looking": "x" * 10,
        "coverage": {"requirements": "three", "flagged": [
            "not a row", {"index": "1", "requirement": "kept, index dropped"},
            {"index": 0}, {"index": 0, "requirement": "r" * 2000, "coverage": "bogus"},
        ]},
        "duplicate": {"title": "Same work", "ref": "javascript:alert(1)", "p": "high"},
    }
    run = _planned_with(client, db, objects, noisy)

    advisory = _run(client, run["id"]).json()["run"]["plan_advisory"]

    assert set(advisory) == {"coverage", "duplicate", "hold_for_human", "judged_revision"}
    assert advisory["coverage"]["requirements"] is None
    assert advisory["coverage"]["flagged"] == [
        {"index": None, "requirement": "kept, index dropped", "coverage": "none"},
        {"index": 0, "requirement": "r" * runs_routes.MAX_ADVISORY_TEXT, "coverage": "none"},
    ]
    assert advisory["duplicate"] == {"title": "Same work", "ref": "javascript:alert(1)",
                                     "url": None, "p": None}


def test_the_list_serves_the_advisory_on_rows_waiting_for_approval(client, db, objects):
    advised = _planned_with(client, db, objects, ADVISED)
    plain = _planned_with(client, db, objects, None)
    waiting = _create(client).json()["run"]

    rows = {r["id"]: r for r in client.get("/v1/runs", headers=auth_header("alice")).json()["runs"]}

    assert rows[advised["id"]]["plan_advisory"]["duplicate"]["ref"] == "acme/widgets#41"
    assert rows[plain["id"]]["plan_advisory"] is None
    # Not PLANNED: not read, so the key is absent rather than a "none" nobody found.
    assert "plan_advisory" not in rows[waiting["id"]]

    summary = client.get("/v1/runs?view=summary", headers=auth_header("alice")).json()["runs"]
    by_id = {r["id"]: r for r in summary}
    assert by_id[advised["id"]]["plan_advisory"]["coverage"]["flagged"][0]["index"] == 1
