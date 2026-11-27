"""An issue run a person's push superseded says so, and closes nothing (archived issue 474).

`issueci._merge` ends an `auto_merge` run DONE with `outcome:
superseded_by_person_push` when a person on its pull request -- or a plain
lane a person dispatched onto its branch -- pushed the green head: no merge
is submitted and the pull request is that person's to merge. The status
comment and the planner's history say so in plain words; the issue stays
open, because nothing merged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_api import issuecomments, issueruns, planhistory
from swarm_api.issueruns import IssueRun, RunState, plan_digest
from swarm_api.validation import parse_issue_ref

from .test_issue_runs import PLAN, REF

AT = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
SUPERSEDED = "superseded_by_person_push"


def _run(**overrides) -> IssueRun:
    base = dict(
        id="ir_" + "5" * 20, tenant_id="eng", created_by="alice@example.com", created_at=AT,
        updated_at=AT, state=RunState.DONE, issue=parse_issue_ref(REF),
        plan_approval="required", auto_merge=True, fix_rounds=3, planner_task_id="task_p",
        plan=PLAN, plan_digest=plan_digest(PLAN), outcome=SUPERSEDED, green_sha="b" * 40,
        pull_request={"number": 57, "url": "https://github.com/example-org/widgets/pull/57",
                      "checks": "green", "merged": False},
        requirements_met=True,
    )
    base.update(overrides)
    return IssueRun(**base)


def test_the_outcome_is_the_runs_constant():
    # planhistory restates it, as it does already_on_main.
    assert (issueruns.OUTCOME_SUPERSEDED_BY_PERSON_PUSH == SUPERSEDED
            == planhistory._SUPERSEDED_BY_PERSON_PUSH)


def test_the_status_comment_says_a_person_pushed_and_closes_nothing():
    run = _run()

    text = issuecomments.render_status_comment(run)

    # MUTATION: drop the outcome's branch of `status_phase` -- it reads
    # "checks green, ready to merge", as if the run would merge it.
    assert "left to the person who pushed" in issuecomments.status_phase(run)
    assert f"- **Outcome:** `{SUPERSEDED}`" in text
    assert "submitted no merge" in text
    assert "ready to merge" not in text
    assert not issuecomments.closes_issue(run)
    assert issuecomments.render_verification_comment(run) is None


def test_plan_history_words_it_as_superseded_by_a_persons_push():
    how, why = planhistory.ending(_run())

    assert how == "superseded by a person's push: its pull request green and left to them"
    assert why == "pull request #57"


def test_a_plain_done_run_reads_as_before():
    run = _run(outcome=None)

    assert planhistory.ending(run)[0] == "done, its pull request green but not merged"
    assert issuecomments.status_phase(run) == "checks green, ready to merge"
    assert "**Outcome:**" not in issuecomments.render_status_comment(run)
