"""BROWSER-USE-FLAG (part of #2322), the API half.

The worker records `result_summary.browser_used` (true, false or null) and
`browser_use` (the counts and seconds) on every attempt it ran a runner for
(`lifecycle.browser_use_record`). swarm-api serves both on the task read and
on every list row -- the summary view too, which drops `result_summary` --
and `GET /v1/outcomes?usage=...` sums, for the span, the browser-runtime run
seconds whose task did not use its browser (`usage.*.browser.unused_s`), so
unused browser hours are a figure rather than a hand audit of agent logs.

Offline: the fake Firestore, no credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.codec import browser_use_to_api
from swarm_api.outcomes import arrival_from_doc, tuple_from_docs

from . import test_outcomes_counts_rollup as rollup
from .conftest import auth_header, seed_task, seed_tenant

USED = {
    "browser_used": True,
    "browser_use": {"tool_calls": 12, "mcp_calls": 9, "command_calls": 3, "images": 4,
                    "complete": True, "seconds": 1500.0},
}
UNUSED = {
    "browser_used": False,
    "browser_use": {"tool_calls": 0, "mcp_calls": 0, "command_calls": 0, "images": 0,
                    "complete": True, "seconds": 2400.0},
}


def _seed(db) -> None:
    seed_tenant(db, "eng")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, (profile, summary) in enumerate((
        ("claude-code-browser", USED),
        ("claude-code-browser", UNUSED),
        ("claude-code", {"browser_used": None}),
    )):
        doc = seed_task(db, task_id=f"task_{i}", tenant_id="eng", state="SUCCEEDED",
                        runner_profile=profile, created_at=base + timedelta(minutes=i))
        doc["result_summary"] = {"summary": "done", **summary}


def test_the_task_read_carries_the_verdict_and_its_counts(client, db):
    _seed(db)
    used = client.get("/v1/tasks/task_0", headers=auth_header("alice")).json()["task"]
    # MUTATION: leave the verdict inside `result_summary` only and this is a KeyError.
    assert used["browser_used"] is True
    assert used["browser_use"] == {**USED["browser_use"], "unknown_because": None}
    unused = client.get("/v1/tasks/task_1", headers=auth_header("alice")).json()["task"]
    assert unused["browser_used"] is False
    plain = client.get("/v1/tasks/task_2", headers=auth_header("alice")).json()["task"]
    assert plain["browser_used"] is None and plain["browser_use"] is None


def test_every_list_row_carries_it_in_both_views(client, db):
    _seed(db)
    for params in ({}, {"view": "summary"}):
        response = client.get("/v1/tasks", headers=auth_header("alice"), params=params)
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["tasks"]}
        # MUTATION: drop it with `result_summary` in the summary view and this fails there.
        assert [rows[f"task_{i}"]["browser_used"] for i in range(3)] == [True, False, None], params
        assert rows["task_1"]["browser_use"]["seconds"] == 2400.0


def test_only_whitelisted_values_of_the_expected_types_are_served():
    served = browser_use_to_api({
        "browser_used": "yes",
        "browser_use": {"tool_calls": -1, "images": True, "seconds": "long",
                        "unknown_because": "<script>", "extra": "x"},
    })
    assert served["browser_used"] is None
    assert served["browser_use"] == {
        "tool_calls": None, "mcp_calls": None, "command_calls": None, "images": None,
        "seconds": None, "complete": False, "unknown_because": None,
    }
    assert browser_use_to_api(None) == {"browser_used": None, "browser_use": None}


# --------------------------------------------------------------------------
# The span's unused browser seconds
# --------------------------------------------------------------------------

def _ended(task_id: str, profile: str, summary: dict, run_s: float):
    doc = rollup._task(task_id, runner_profile=profile, result_summary=summary)
    return doc, tuple_from_docs(doc, [rollup._attempt(1, run_s=run_s)], {})


def test_the_usage_block_sums_unused_browser_seconds_over_the_span():
    used_doc, used = _ended("a", "claude-code-browser", USED, 1500.0)
    unused_doc, unused = _ended("b", "claude-code-browser", UNUSED, 2400.0)
    unknown_doc, unknown = _ended("c", "claude-code-browser", {"browser_used": None}, 300.0)
    plain_doc, plain = _ended("d", "claude-code", {"browser_used": None}, 900.0)
    assert unused["browser_s"] == 2400.0 and unused["browser_used"] is False
    assert plain["browser_s"] is None and plain["browser_used"] is None
    folded = rollup._fold(
        {"usage": "submitted"},
        ended=[used, unused, unknown, plain],
        arrived=[arrival_from_doc(d) for d in (used_doc, unused_doc, unknown_doc, plain_doc)],
    )
    browser = folded["usage"]["totals"]["browser"]
    # MUTATION: sum every browser agent's seconds as unused and this is 4200.
    assert browser == {
        "agents": 3, "run_s": 4200.0, "measured": 3,
        "unused_agents": 1, "unused_s": 2400.0, "unknown_agents": 1,
    }
    # Per bucket too: every agent was submitted on the one day.
    day = rollup._bucket(folded, rollup.SUBMITTED_DAY)
    assert day["browser"]["unused_s"] == 2400.0
    # And per group row, so the profile breakdown says where the hours went.
    rows = {r["key"]: r for r in folded["usage"]["breakdowns"]["runner_profile"]["rows"]}
    assert rows["claude-code-browser"]["browser"]["unused_s"] == 2400.0
    assert rows["claude-code"]["browser"]["agents"] == 0
    assert rows["claude-code"]["browser"]["unused_s"] is None


def test_a_span_where_every_browser_agent_used_it_sums_zero_not_null():
    doc, used = _ended("a", "claude-code-browser", USED, 1500.0)
    folded = rollup._fold({"usage": "ended"}, ended=[used])
    # MUTATION: serve null for "none unused" and the console cannot tell it from unmeasured.
    assert folded["usage"]["totals"]["browser"]["unused_s"] == 0.0
