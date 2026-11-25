"""`GET /v1/outcomes?rows=...`: the tasks behind one Timeline figure (archived issue 116).

WHY THIS EXISTS. "Failed 21" on the Timeline used to open the Agents list's
Recent tab filtered to FAILED -- the newest 200 agents, whatever they ended
as and whenever -- because no task read lists by `completed_at`. The ledger's
own fold already holds every ended task of the span, placed in its bucket by
`completed_at`, so the figure's rows come from there: the same tasks the
figure counted, from the same read, with nothing re-derived.

What is held here, over archived pull request 196's fixture week (`test_outcomes_route.seed_week`):

  * a bucket's rows are exactly the bucket's figure -- the count and the ids;
  * failed is FAILED + DEAD_LETTERED, the one definition the drawing uses;
  * a group row's cell (a person, a profile) narrows to that row's tasks;
  * another tenant's work never appears, in tenant scope;
  * the rows are served from the fold the page already read (no new read);
  * the parameters are validated with the one error envelope;
  * the block is the shape `wire_types.EndedRows` documents, every field, both
    directions (the console's `EndedRows` is the generated one; part of #72).

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups, a fixed clock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import outcomes as module
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app

from . import test_outcomes_route as route_tests
from .openapi_reads import operation

WEEK = route_tests.WEEK
SEP22 = "2026-09-22T00:00:00+00:00"
SEP23 = "2026-09-23T00:00:00+00:00"


@pytest.fixture
def api(db, tokens, group_map) -> TestClient:
    route_tests.seed_week(db)
    ctx = route_tests._context(db, tokens, StaticGroups(group_map), route_tests.Clock())
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def rows(api: TestClient, user: str = "alice", **params: Any) -> dict[str, Any]:
    body = route_tests.ok(api, user, **{**WEEK, **params})
    assert "ended_rows" in body, f"no ended_rows block in {sorted(body)}"
    return body["ended_rows"]


def ids(block: dict[str, Any]) -> list[str]:
    return [r["id"] for r in block["rows"]]


# --------------------------------------------------------------------------
# The rows are the figure
# --------------------------------------------------------------------------

def test_a_buckets_failed_rows_are_the_tasks_its_failed_figure_counted(api):
    whole = route_tests.ok(api, "alice", **WEEK)
    sep22 = whole["buckets"][3]
    block = rows(api, rows="failed", rows_at=SEP22)
    assert block["total"] == sep22["failed"] + sep22["dead_lettered"] == 1
    assert ids(block) == ["f1"]
    assert block["outcome"] == "failed"
    assert block["at"] == SEP22 and block["end"] == "2026-09-23T00:00:00+00:00"
    assert block["unread_reason"] is None
    row = block["rows"][0]
    assert row["state"] == "FAILED" and row["runner_profile"] == "claude-code"
    assert row["completed_at"] == "2026-09-22T13:00:00+00:00"
    assert row["failure_class"] == "timeout" and row["cancel_cause"] is None
    assert row["workflow_id"] == "wf1" and row["step_id"] == "b"


def test_a_buckets_cancelled_rows_are_its_cancelled_total_newest_first(api):
    sep22 = route_tests.ok(api, "alice", **WEEK)["buckets"][3]
    block = rows(api, rows="cancelled", rows_at=SEP22)
    assert block["total"] == sep22["cancelled"]["total"] == 4
    assert ids(block) == ["c4", "c1", "c2", "c3"], "newest end first, then by id"
    assert block["rows"][0]["cancel_cause"] == "after_failure"
    assert {r["cancel_cause"] for r in block["rows"][1:]} == {"requested"}


def test_failed_counts_dead_lettered_the_way_the_drawing_does(api):
    block = rows(api, rows="failed", rows_at=SEP23)
    assert ids(block) == ["d1"]
    assert block["rows"][0]["state"] == "DEAD_LETTERED"


def test_without_a_bucket_the_rows_are_the_spans_figure(api):
    totals = route_tests.ok(api, "alice", **WEEK)["totals"]
    failed = rows(api, rows="failed")
    assert failed["total"] == totals["failed"] + totals["dead_lettered"]
    assert sorted(ids(failed)) == ["d1", "f1"]
    assert failed["at"] is None and failed["end"] is None
    succeeded = rows(api, rows="succeeded")
    assert succeeded["total"] == totals["succeeded"]


def test_a_group_rows_cell_narrows_to_that_rows_tasks(api):
    body = route_tests.ok(api, "alice", **WEEK, group="submitted_by")
    bob = next(r for r in body["groups"]["rows"] if r["key"] == "bob@example.com")
    block = rows(api, rows="failed", group="submitted_by", rows_key="bob@example.com")
    assert block["total"] == bob["failed"] + bob["dead_lettered"] == 1
    assert ids(block) == ["d1"]
    assert block["key"] == "bob@example.com" and block["group"] == "submitted_by"
    by_profile = rows(api, rows="failed", rows_key="claude-code")
    assert ids(by_profile) == ["f1"] and by_profile["group"] == "runner_profile"


def test_the_page_filters_narrow_the_rows_too(api):
    block = rows(api, rows="failed", profile=["generic"])
    assert ids(block) == ["d1"]


def test_another_tenants_work_is_never_listed(api):
    every = rows(api, rows="failed", rows_at=SEP22)
    assert "r2" not in ids(every)
    assert {r["tenant_id"] for r in every["rows"]} == {"eng"}
    theirs = rows(api, "bob", rows="failed", rows_at=SEP22)
    assert ids(theirs) == ["r2"]


def test_no_rows_parameter_is_the_response_unchanged(api):
    body = route_tests.ok(api, "alice", **WEEK)
    assert "ended_rows" not in body


def test_the_rows_are_served_from_the_fold_the_page_already_read(api):
    route_tests.ok(api, "alice", **WEEK)
    body = route_tests.ok(api, "alice", **WEEK, rows="failed", rows_at=SEP22, section="totals")
    assert body["cached"] is True and body["reads"] == 0
    assert ids(body["ended_rows"]) == ["f1"]


def test_a_long_list_is_capped_and_says_how_many_it_left_out(api, monkeypatch):
    monkeypatch.setattr(module, "ENDED_ROWS_MAX", 2)
    block = rows(api, rows="cancelled", rows_at=SEP22)
    assert block["total"] == 4 and block["rows_max"] == 2
    assert ids(block) == ["c4", "c1"]


# --------------------------------------------------------------------------
# Validation, with the one envelope
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("params", "parameter", "reason"),
    [
        ({"rows": "dead"}, "rows", None),
        ({"rows": "failed", "rows_at": "2026-09-22T05:00:00+00:00"}, "rows_at", "not_a_bucket"),
        ({"rows": "failed", "rows_at": "2026-08-01"}, "rows_at", "not_a_bucket"),
        ({"rows_at": SEP22}, "rows_at", "needs_rows"),
        ({"rows_key": "mock"}, "rows_key", "needs_rows"),
        ({"rows": "failed", "rows_at": "yesterday"}, "rows_at", "not_iso8601"),
    ],
)
def test_a_bad_rows_parameter_is_the_one_validation_envelope(api, params, parameter, reason):
    response = route_tests.get(api, "alice", **WEEK, **params)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["parameter"] == parameter
    if reason is not None:
        assert body["detail"]["reason"] == reason
    else:
        assert body["detail"]["allowed"] == list(module.ROW_OUTCOMES)


def test_a_non_admin_cannot_list_another_tenants_rows_by_naming_rows(api):
    response = route_tests.get(api, "alice", **WEEK, scope="platform", rows="failed")
    assert response.status_code == 403


# --------------------------------------------------------------------------
# The shape the page reads
# --------------------------------------------------------------------------

def test_the_block_is_exactly_the_model_both_directions(api):
    """The block against `wire_types.EndedRows` at every depth, which the
    console's generated `EndedRows` is (part of #72: this once parsed outcomes.ts)."""
    from swarm_api import wire_types

    from .test_wire_types import walk

    seen: set[str] = set()
    problems: list[str] = []
    for params in (
        {"rows": "failed", "rows_at": SEP22},
        {"rows": "cancelled", "rows_at": SEP22},
        {"rows": "succeeded", "group": "submitted_by", "rows_key": "alice@example.com"},
    ):
        block = rows(api, **params)
        problems += walk(wire_types.EndedRows, block, "EndedRows", seen)
        wire_types.EndedRows.model_validate(block)
    assert not problems, "wire_types.EndedRows and the rows block disagree:\n  " + "\n  ".join(problems[:40])
    assert "EndedRow" in seen, "no row was checked; the element type is unproven"


def test_the_row_outcomes_are_the_routes():
    """The generated union (and the generated constants the page's
    ROW_OUTCOMES is held to in contract.shapes.test.ts) are the route's tuple."""
    from swarm_api import wire_types

    assert [m.value for m in wire_types.RowOutcome] == list(module.ROW_OUTCOMES)


def test_every_parameter_the_rows_read_sends_is_one_the_route_declares(client):
    """`rowsQuery` sends `rows`, `rows_at` and `rows_key` beside the ledger's
    query (contract.shapes.test.ts); the route declares all three."""
    declared = {p["name"] for p in operation(client, "/v1/outcomes")["parameters"]
                if p["in"] == "query"}
    assert {"rows", "rows_at", "rows_key"} <= declared, sorted(declared)
