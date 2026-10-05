"""An issue run whose build finds the work already on main ends DONE, not FAILED (#646).

Owner decision 2026-10-05: the compiled build steps set `allow_empty_diff`
(#644), so an implementer that finds nothing to change ends SUCCEEDED with
`result_summary.no_change`, and the review and the integrator it fed end
SKIPPED. What these tests hold:

  1. THE COMPILER ASKS FOR IT. Every implementer step -- chain and stages --
     carries `allow_empty_diff`; the review and the integrator do not. Every
     implementer's prompt asks, when it changes nothing, for
     `$SWARM_ARTIFACTS_DIR/verification.md`: one table row per planned
     requirement, met on main or not, where, and the proving test.
  2. THE RUN ENDS DONE, `outcome: already_on_main`, never FAILED, when the
     workflow SUCCEEDED, nothing changed and the integrator was skipped. A
     build that genuinely failed, or an integrator that ran and opened no
     pull request, is still FAILED.
  3. THE TABLE IS POSTED, AND CLOSES ONLY WHEN EVERY ROW IS MET. The table is
     a comment on the issue, written with the tenant's forge token through
     the write-back (`issuesync`), never the agent's. Every planned
     requirement met: the issue is closed. One unmet, a missing table or a
     malformed one: the issue stays open, with the table.
  4. THE OUTCOME IS SERVED: `GET /v1/runs/{id}` (what `swarm_run` answers
     and the console's run page reads) carries `outcome`.
  5. A RUN THAT OPENED A PULL REQUEST IS UNCHANGED: CHECKING, no outcome, no
     verification comment, the issue never closed by the run.

GitHub is the fake transport the CI-loop tests use. Every token is built at
runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from swarm_api import issuecomments, issueruns, issuesync
from swarm_api.issueruns import RunState, compile_plan, verification_finding

from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    PR,
    SHA_A,
    _read,
    _step_task,
    _stored,
    _workflow_ends,
    api_context,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_run_keyword import STAGED_FULL_PLAN
from .test_issue_runs import BUCKET, FULL_PLAN, _approve, _create, _docs, _finish_planner, _run
from .test_issue_writeback import _bare_run

ISSUE = 42
REQUIREMENTS = FULL_PLAN["requirements"]


def _table(*met: bool) -> str:
    rows = "".join(
        f"| {n} | {'yes' if ok else 'no'} | src/widgets/list.py, sort_by_name "
        f"| tests/test_list.py::test_sorts_{n} |\n"
        for n, ok in enumerate(met, start=1)
    )
    return (
        "Verified on main at the clone's head.\n\n"
        "| # | Met on main | Where (file, function) | Proving test |\n"
        "|---|---|---|---|\n" + rows
    )


def _full_run(**fields):
    return _bare_run(plan=FULL_PLAN, plan_digest=issueruns.plan_digest(FULL_PLAN), **fields)


# --------------------------------------------------------------------------
# 1. the compiler
# --------------------------------------------------------------------------

@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_every_build_step_allows_an_empty_diff_and_nothing_else_does(plan):
    run = _bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan))
    spec = compile_plan(run)
    for step in spec.steps:
        building = step.step_id.startswith(issueruns.IMPLEMENT_PREFIX)
        assert step.allow_empty_diff is building, step.step_id


@pytest.mark.parametrize("plan", [FULL_PLAN, STAGED_FULL_PLAN], ids=["chain", "stages"])
def test_every_build_prompt_asks_for_the_verification_table_per_requirement(plan):
    spec = compile_plan(_bare_run(plan=plan, plan_digest=issueruns.plan_digest(plan)))
    builds = [s for s in spec.steps if s.step_id.startswith(issueruns.IMPLEMENT_PREFIX)]
    assert builds
    for step in builds:
        prompt = step.input["prompt"]
        assert f"$SWARM_ARTIFACTS_DIR/{issueruns.VERIFICATION_FILE}" in prompt
        assert "| # | Met on main | Where (file, function) | Proving test |" in prompt
        for n, requirement in enumerate(REQUIREMENTS, start=1):
            assert f"{n}. {requirement}" in prompt
        # Still forbidden from closing the issue itself.
        assert issueruns.NO_CLOSING_KEYWORD in prompt
    for step in spec.steps:
        if not step.step_id.startswith(issueruns.IMPLEMENT_PREFIX):
            assert issueruns.VERIFICATION_FILE not in step.input["prompt"]


# --------------------------------------------------------------------------
# the table, read strictly (pure)
# --------------------------------------------------------------------------

def test_a_table_with_every_row_met_is_all_met():
    assert verification_finding(FULL_PLAN, [("impl-sort-key", _table(True, True), None)]) == (
        True, [], None,
    )


def test_one_unmet_row_names_that_requirement():
    met, unmet, note = verification_finding(
        FULL_PLAN, [("impl-sort-key", _table(True, False), None)]
    )
    assert met is False and note is None
    assert unmet == [REQUIREMENTS[1]]


def test_two_tables_close_only_when_neither_says_unmet():
    met, unmet, _ = verification_finding(FULL_PLAN, [
        ("impl-sort-key", _table(True, True), None),
        ("impl-ui", _table(True, False), None),
    ])
    assert met is False and unmet == [REQUIREMENTS[1]]


@pytest.mark.parametrize(
    "content,why",
    [
        (None, "wrote no verification.md"),
        ("All good, nothing to change.", "no table row"),
        (_table(True), "requirement 2"),
        (_table(True, True) + "| 2 | yes | x | y |\n", "more than once"),
        (_table(True, True) + "| 3 | yes | x | y |\n", "1 to 2"),
        (_table(True, True).replace("| 1 | yes", "| 1 | probably"), "yes or no"),
        (_table(True, True).replace("| 1 | yes", "| one | yes"), "requirement number"),
    ],
)
def test_a_missing_or_malformed_table_is_not_all_met(content, why):
    problem = None if content is not None else "impl-sort-key wrote no verification.md"
    met, unmet, note = verification_finding(FULL_PLAN, [("impl-sort-key", content, problem)])
    assert met is False
    assert unmet == REQUIREMENTS
    assert note is not None and why in note


def test_a_plan_with_no_requirements_never_closes_the_issue():
    plan = {k: v for k, v in FULL_PLAN.items() if k != "requirements"}
    met, unmet, note = verification_finding(plan, [("impl-sort-key", _table(True), None)])
    assert (met, unmet) == (False, [])
    assert "no requirements" in note


# --------------------------------------------------------------------------
# 2-4. through the run
# --------------------------------------------------------------------------

def _running(client, db, objects, plan=FULL_PLAN) -> dict:
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    planned = _run(client, run["id"]).json()["run"]
    response = _approve(client, run["id"], planned["plan_digest"])
    assert response.status_code == 200, response.text
    return response.json()["run"]


def _upload(objects, task: dict, name: str, content: str) -> dict:
    key = f"tenants/{task['tenant_id']}/tasks/{task['id']}/attempts/att_1/artifacts/{name}"
    objects.put(key, content)
    return {"name": name, "bytes": len(content.encode()), "uri": f"gs://{BUCKET}/{key}"}


def _workflow_changed_nothing(db, objects, workflow_id: str, tables: dict[str, str | None]) -> None:
    """Every step SUCCEEDED; the implementers changed nothing, the rest were skipped.

    Exactly what the worker records since #644: an implementer with an empty
    diff has `no_change: true` (and uploads what it verified); a step that
    needed its change has `skipped`, and ran no agent.
    """
    steps = db.docs[f"workflows/{workflow_id}"]["steps"]
    for step in steps:
        task = db.docs[f"tasks/{step['task_id']}"]
        task["state"] = "SUCCEEDED"
        if step["step_id"].startswith(issueruns.IMPLEMENT_PREFIX):
            summary: dict = {
                "no_change": True,
                "git": {"patch_cause": "empty_diff", "commit_count": 0, "dirty_count": 0},
                "artifacts": [],
            }
            content = tables.get(step["step_id"])
            if content is not None:
                summary["artifacts"].append(
                    _upload(objects, task, issueruns.VERIFICATION_FILE, content)
                )
            task["result_summary"] = summary
        else:
            task["result_summary"] = {"skipped": {"reason": "nothing to change", "needed": []}}


def _comments(writes, kind: str, run_id: str) -> list[dict]:
    mark = issuecomments.marker(run_id, kind)
    return [c for c in writes.on_issue(ISSUE) if c["body"].startswith(mark)]


def _closed(writes) -> bool:
    return writes.issues.get(ISSUE, {}).get("state") == "closed"


def test_an_empty_diff_with_every_row_met_is_done_comments_and_closes_the_issue(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE"
    assert run["outcome"] == issueruns.OUTCOME_ALREADY_ON_MAIN == "already_on_main"
    assert run["requirements_met"] is True
    assert run["error"] is None and run["pull_request"] is None
    (comment,) = _comments(writes, issuecomments.VERIFICATION_KIND, running["id"])
    assert "| 1 | yes | src/widgets/list.py, sort_by_name" in comment["body"]
    assert "tests/test_list.py::test_sorts_2" in comment["body"]
    assert _closed(writes)
    assert writes.issues[ISSUE]["state_reason"] == "completed"
    assert run["issue_closed"] is True
    # The status comment says how the run ended.
    (status,) = _comments(writes, issuecomments.STATUS_KIND, running["id"])
    assert "already_on_main" in status["body"]
    # Nothing was submitted after the compiled workflow: no fix round, no merge.
    assert len(_docs(db, "workflows")) == 1


def test_an_empty_diff_with_one_row_unmet_is_done_comments_and_leaves_the_issue_open(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, False), "impl-ui": _table(True, False),
    })

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE" and run["outcome"] == "already_on_main"
    assert run["requirements_met"] is False
    assert run["requirements_unmet"] == [REQUIREMENTS[1]]
    (comment,) = _comments(writes, issuecomments.VERIFICATION_KIND, running["id"])
    assert "| 2 | no |" in comment["body"]
    assert "stays open" in comment["body"]
    assert not _closed(writes)
    assert ("PATCH", "/repos/saga-xyz/widgets/issues/42") not in writes.writes()
    assert not run["issue_closed"]


def test_no_verification_table_is_done_and_leaves_the_issue_open_saying_so(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {})

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE" and run["outcome"] == "already_on_main"
    assert run["requirements_met"] is False
    (comment,) = _comments(writes, issuecomments.VERIFICATION_KIND, running["id"])
    assert issueruns.VERIFICATION_FILE in comment["body"] and "stays open" in comment["body"]
    assert not _closed(writes)


def test_a_staged_run_that_changed_nothing_reads_every_builds_table(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects, plan=STAGED_FULL_PLAN)
    builds = [
        s["step_id"] for s in db.docs[f"workflows/{running['workflow_id']}"]["steps"]
        if s["step_id"].startswith(issueruns.IMPLEMENT_PREFIX)
    ]
    _workflow_changed_nothing(
        db, objects, running["workflow_id"], {sid: _table(True, True) for sid in builds}
    )

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE" and run["outcome"] == "already_on_main"
    assert _closed(writes)


def test_reading_a_closed_run_again_posts_and_closes_nothing_more(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })
    _read(client, clock, running["id"])
    before = list(writes.writes())

    _read(client, clock, running["id"])

    assert writes.writes() == before


def test_a_failed_close_is_recorded_and_tried_again(client, db, objects, writes, clock):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })
    writes.refuse[("PATCH", "/repos/saga-xyz/widgets/issues/42")] = 403

    run = _read(client, clock, running["id"])

    assert run["state"] == "DONE" and not _closed(writes)
    assert run["writeback_error"] and "issues: write" in run["writeback_error"]
    assert not run["issue_closed"]

    writes.refuse.clear()
    clock.at += timedelta(seconds=issuesync.RETRY_SECONDS)
    run = _read(client, clock, running["id"])

    assert _closed(writes) and run["issue_closed"] is True
    assert run["writeback_error"] is None
    # The table was posted once, not again with the retry.
    assert len(_comments(writes, issuecomments.VERIFICATION_KIND, running["id"])) == 1


def test_the_table_is_redacted_before_it_is_stored_or_posted(
    client, db, objects, writes, clock, forge_tokens
):
    fake = "ghp_" + "x" * 36
    running = _running(client, db, objects)
    tenant_token = forge_tokens.issued["swarm-tenant-eng-git"]
    leaky = _table(True, True) + f"\nchecked with {fake} and {tenant_token}\n"
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": leaky, "impl-ui": _table(True, True),
    })

    _read(client, clock, running["id"])

    (comment,) = _comments(writes, issuecomments.VERIFICATION_KIND, running["id"])
    assert "checked with" in comment["body"]
    assert fake not in comment["body"] and tenant_token not in comment["body"]
    assert fake not in str(db.docs) and tenant_token not in str(db.docs)


def test_the_comment_is_written_with_the_tenants_token_not_the_agents(
    client, db, objects, writes, clock, forge_tokens
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })

    _read(client, clock, running["id"])

    tenant_token = forge_tokens.issued["swarm-tenant-eng-git"]
    writes_to_issue = [
        headers for method, url, headers, _ in writes.calls
        if method in ("POST", "PATCH") and "/issues/42" in url
    ]
    assert writes_to_issue
    for headers in writes_to_issue:
        assert headers["Authorization"].endswith(tenant_token)


# --------------------------------------------------------------------------
# what is unchanged
# --------------------------------------------------------------------------

def test_a_run_that_opened_a_pull_request_is_unchanged(client, db, objects, writes, clock):
    running = _running(client, db, objects)
    writes.open_pull(PR, SHA_A)
    _workflow_ends(db, running["workflow_id"])

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    assert run["outcome"] is None
    assert _comments(writes, issuecomments.VERIFICATION_KIND, running["id"]) == []
    assert not _closed(writes)


def test_a_genuinely_failed_build_still_fails(client, db, objects, writes, clock):
    running = _running(client, db, objects)
    _workflow_ends(db, running["workflow_id"], pull=None, state="FAILED")

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and run["outcome"] is None
    assert _comments(writes, issuecomments.VERIFICATION_KIND, running["id"]) == []
    assert not _closed(writes)


def test_an_integrator_that_ran_and_opened_no_pull_request_still_fails(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })
    # One build DID change something, and the integrator ran -- and opened nothing.
    build = _step_task(db, running["workflow_id"], "impl-ui")
    build["result_summary"] = {"git": {"commit_count": 1}}
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    integrator["result_summary"] = {"git": {"published": False, "publish_reason": "push refused"}}

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and "no pull request" in run["error"]
    assert run["outcome"] is None
    assert not _closed(writes)


def test_a_skipped_integrator_over_a_build_that_changed_something_still_fails(
    client, db, objects, writes, clock
):
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {})
    build = _step_task(db, running["workflow_id"], "impl-sort-key")
    build["result_summary"] = {"git": {"commit_count": 2}}

    run = _read(client, clock, running["id"])

    assert run["state"] == "FAILED" and run["outcome"] is None


def test_running_may_end_done_only_through_already_on_main():
    assert RunState.DONE in issueruns.RUN_TRANSITIONS[RunState.RUNNING]


def test_the_stored_document_round_trips_the_outcome_and_the_table():
    run = _full_run(
        state=RunState.DONE, outcome="already_on_main", verification=_table(True, True),
        verification_comment_id=9001, last_verification_posted="sha256:x", issue_closed=True,
    )
    again = issueruns.IssueRun.from_firestore(run.to_firestore())
    assert again.outcome == "already_on_main"
    assert again.verification == _table(True, True)
    assert (again.verification_comment_id, again.issue_closed) == (9001, True)
    served = again.to_api()
    assert served["outcome"] == "already_on_main" and served["issue_closed"] is True
    assert served["verification"] == _table(True, True)
    # A run stored before #646 reads with no outcome.
    old = run.to_firestore()
    for key in ("outcome", "verification", "verification_comment_id",
                "last_verification_posted", "issue_closed"):
        old.pop(key)
    assert issueruns.IssueRun.from_firestore(old).to_api()["outcome"] is None


def test_the_status_comment_shows_the_outcome():
    met = _full_run(state=RunState.DONE, outcome="already_on_main", requirements_met=True)
    unmet = _full_run(
        state=RunState.DONE, outcome="already_on_main", requirements_met=False,
        requirements_unmet=[REQUIREMENTS[1]],
    )
    assert issuecomments.status_phase(met) == "already done on main"
    assert "closed" in issuecomments.render_status_comment(met)
    assert "stays open" in issuecomments.render_status_comment(unmet)


def test_the_stored_table_is_not_the_raw_artifact_bytes(client, db, objects, writes, clock):
    running = _running(client, db, objects)
    big = _table(True, True) + ("filler line\n" * 5000)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": big, "impl-ui": _table(True, True),
    })

    _read(client, clock, running["id"])

    stored = _stored(db, running["id"])["verification"]
    assert len(stored) <= issueruns.MAX_VERIFICATION_CHARS


def test_json_of_the_served_run_is_what_swarm_run_prints(client, db, objects, writes, clock):
    # swarm_run answers `GET /v1/runs/{id}` as served (swarm_mcp.runs.summary).
    running = _running(client, db, objects)
    _workflow_changed_nothing(db, objects, running["workflow_id"], {
        "impl-sort-key": _table(True, True), "impl-ui": _table(True, True),
    })
    _read(client, clock, running["id"])

    served = _run(client, running["id"]).json()["run"]

    assert json.loads(json.dumps(served))["outcome"] == "already_on_main"
