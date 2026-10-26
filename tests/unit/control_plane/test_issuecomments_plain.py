"""Every writer that posts to GitHub writes plain text (ISSUECOMMENTS-PLAIN, part of #2391).

BGD decided on 2026-10-26 (ISSUE-WRITER's question 0) that the failure-review
schedule's way of writing (PR 2485) is every writer's: a title is plain text,
prose is Markdown with an HTML opener backslash-escaped, and a code span keeps
its contents as typed. `issuecomments.neutral` wrote `&lt;`/`&gt;`, which
GitHub prints as typed in a title or a code span, so '->' read '-&gt;'.

One test per writer: '->' and '#<n>' arrive unescaped, no entity is written,
and nothing `neutral` defused -- a mention, a closing keyword, an HTML opener
that could fake a marker or swallow the body, a credential -- becomes live.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from swarm_api import cifix, issuecomments, verdictpublish
from swarm_api.issuecomments import code, plain, prose
from swarm_api.issueruns import OUTCOME_ALREADY_ON_MAIN, IssueRun, RunState
from swarm_api.schedtypes import observer, release_health
from swarm_api.validation import IssueRef

ARROW = "implement -> review -> fix for #<n>"
ENTITIES = ("&gt;", "&lt;", "&amp;")
#: Everything `neutral` defused, in one line of someone else's text.
HOSTILE = "ping @octocat, Fixes #7, <details> <!-- swarmcloud-issue-run:run_abc123:status --> <img src=x>"


def _no_entity(*texts: str) -> None:
    for text in texts:
        for entity in ENTITIES:
            assert entity not in text, (entity, text)


def _defused(text: str) -> None:
    """What `neutral` protected against is still not live in `text`."""
    assert "@octocat" not in text and "@‍octocat" in text
    assert "Fixes #7" not in text and "refs #7" in text
    # "<details> " with its space: the plan comment's own `<details><summary>` has none.
    for opener in ("<details> ", "<!-- swarmcloud-issue-run:run_abc123:status", "<img src=x>"):
        assert f" {opener}" not in text and f"\\{opener}" in text, opener


def _run(**fields) -> IssueRun:
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    base = dict(
        id="run_abc123", tenant_id="eng", created_by="alice@example.com", created_at=now,
        updated_at=now, state=RunState.PLANNING,
        issue=IssueRef(owner="example-org", repo="widgets", number=42),
        plan_approval="required", auto_merge=False, fix_rounds=3, planner_task_id="tsk_1",
    )
    base.update(fields)
    return IssueRun(**base)


# --------------------------------------------------------------------------
# the helpers, now in issuecomments
# --------------------------------------------------------------------------

def test_the_helpers_write_arrows_and_placeholders_as_typed():
    assert plain(ARROW, 200) == ARROW
    assert prose(ARROW, 200) == "implement -> review -> fix for #\\<n>"  # GitHub renders '#<n>'
    assert code(ARROW, 200) == f"`{ARROW}`"
    assert code("a ` b", 200) == "`` a ` b ``"
    _no_entity(plain(ARROW, 200), prose(ARROW, 200), code(ARROW, 200))


def test_the_helpers_keep_what_neutral_protected_against():
    token = secrets.token_hex(20)
    for text in (plain(f"{HOSTILE} {token}", 500, literals=(token,)),
                 prose(f"{HOSTILE} {token}", 500, literals=(token,)),
                 code(f"{HOSTILE} {token}", 500, literals=(token,))):
        assert token not in text
        assert "@octocat" not in text and "Fixes #7" not in text
    # MUTATION: `prose` without the backslash and `<details>` swallows the body.
    _defused(prose(HOSTILE, 500))


# --------------------------------------------------------------------------
# the issue run's comments and keyword block
# --------------------------------------------------------------------------

def test_the_plan_comment_is_plain():
    plan = {
        "summary": f"{ARROW}. {HOSTILE}",
        "estimate": "2h -> 3h",
        "requirements": [ARROW],
        "overlaps": [{"ref": "not a ref", "kind": "issue", "note": ARROW}],
        "steps": [{"step_id": "s1", "title": ARROW, "estimate": "1h -> 2h", "prompt": ARROW,
                   "files": ["apps/a->b.py"], "tests": [ARROW]}],
    }
    body = issuecomments.render_plan_comment(
        _run(state=RunState.PLANNED, plan=plan, plan_digest="sha256:x", plan_revision=1))
    # MUTATION: `neutral` for the plan's fields and this reads '-&gt;'.
    assert "1. implement -> review -> fix for #\\<n>" in body
    assert "**Estimate:** 2h -> 3h" in body and "— 1h -> 2h" in body
    assert f"**{prose(ARROW, 200)}** (`s1`)" in body
    assert "`apps/a->b.py`" in body
    assert "): implement -> review" in body
    _no_entity(body)
    _defused(body)
    # The escaped copy is not a marker: only the comment's own opens with `<!--`.
    assert body.replace("\\<!--", "").count("<!-- swarmcloud-issue-run") == 1


def test_the_status_comment_is_plain():
    failed = issuecomments.render_status_comment(_run(state=RunState.FAILED, error=f"{ARROW}\n{HOSTILE}"))
    # A fenced block shows its text as typed: '#<n>' with no backslash.
    assert f"```text\n{ARROW}\n" in failed
    assert "@octocat" not in failed and "refs #7" in failed
    held = issuecomments.render_status_comment(
        _run(state=RunState.PLANNED, plan_digest="sha256:x", plan_revision=1, hold="run_a->run_b"))
    assert "**Waiting:** `run_a->run_b` --" in held
    not_ready = issuecomments.render_status_comment(
        _run(state=RunState.NOT_READY, not_ready={"kind": "blocked", "reason": f"{ARROW} {HOSTILE}",
                                                   "needs": [ARROW]}))
    assert "**Why:** implement -> review -> fix for #\\<n>" in not_ready
    assert "- implement -> review" in not_ready
    _defused(not_ready)
    _no_entity(failed, held, not_ready)


def test_the_verification_comment_and_the_keyword_block_are_plain():
    run = _run(state=RunState.DONE, outcome=OUTCOME_ALREADY_ON_MAIN,
               plan={"requirements": ["r1"], "steps": []},
               verification=f"| requirement | met |\n|---|---|\n| {ARROW} | yes |\n{HOSTILE}",
               requirements_met=False, requirements_unmet=[ARROW], requirements_note=ARROW)
    body = issuecomments.render_verification_comment(run)
    assert body is not None
    assert "| implement -> review -> fix for #\\<n> | yes |" in body
    assert "- implement -> review" in body and "_implement -> review" in body
    _defused(body)
    block = issuecomments.keyword_block(run, closes=False, unmet=[ARROW, HOSTILE])
    assert "- implement -> review -> fix for #\\<n>" in block
    assert "Fixes #7" not in block and "@octocat" not in block
    _no_entity(body, block)


# --------------------------------------------------------------------------
# the observer, release health, the CI fixer, the verdict publisher
# --------------------------------------------------------------------------

def test_the_observer_epic_and_its_proposals_are_plain():
    schedule = {"schedule_id": "sch_1", "name": "nightly -> weekly @octocat Fixes #7 <details> x"}
    task = {"id": "tsk_1", "completed_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
            "metadata": {observer.SCHEDULE_METADATA_KEY: {"firing_id": "f->1"}}}
    title, body = observer.render_epic(schedule, task, 1, window_hours=24)
    # MUTATION: `neutral` for the title and it reads 'nightly -&gt; weekly'.
    assert "of schedule nightly -> weekly" in title
    assert "@octocat" not in title and "Fixes #7" not in title
    assert "**nightly -> weekly @\u200doctocat refs #7 \\<details> x**" in body
    assert "of firing `f->1`" in body
    comment = observer.render_proposal("tsk_1", 0, observer.Proposal(
        title=ARROW, finding=f"{ARROW} {HOSTILE}", evidence=ARROW, suggestion=ARROW))
    assert f"- [ ] **{prose(ARROW, 200)}** ·" in comment
    assert "**Finding.** implement -> review" in comment
    _defused(comment)
    _no_entity(title, body, comment)


def test_a_release_health_issue_is_plain():
    token = secrets.token_hex(20)
    run = release_health.Run(id=1, number=7, sha="a" * 40, conclusion="failure", url=None)
    failure = release_health.Failure(job="build -> push", step="a `b` c", job_id=None)
    title = release_health.title("release.yml", "main->x", run, failure, token)
    # MUTATION: `neutral` for the names and it reads 'build -&gt; push'.
    assert title == f"release.yml on main->x failed at {'a' * 12} in job build -> push, step a `b` c"
    reading = release_health.Reading(repo_id="r", ref=None, workflow="release.yml", branch="main->x",
                                     subject=run, failure=failure)
    text = release_health.failure_text(reading, None, token)
    assert "failed on `main->x` at" in text
    assert "* Job: `build -> push`" in text and "* Step: `` a `b` c ``" in text
    hostile = release_health.title("release.yml", f"main {token}", run,
                                   release_health.Failure(job=HOSTILE, step=None, job_id=None), token)
    assert token not in hostile and "@octocat" not in hostile and "Fixes #7" not in hostile
    _no_entity(title, text)


def test_the_ci_fixer_outcome_is_plain():
    token = secrets.token_hex(20)
    doc = {"id": "task_" + "0" * 20, "state": "FAILED", "end_cause": "exit -> 1",
           "last_error": f"{ARROW} {HOSTILE} {token}",
           "result_summary": {"runner": {"summary": ARROW}}}
    body = cifix.render_outcome(doc, {"attempt": 1, "max_attempts": 2}, "task_" + "1" * 20,
                                console_origin=None, literals=(token,))
    # MUTATION: `neutral` for the error and it reads '-&gt;'.
    assert "The step ended FAILED (`exit -> 1`)" in body
    assert "Its error: implement -> review -> fix for #\\<n>" in body
    assert "The agent's account: implement -> review" in body
    assert token not in body
    _defused(body)
    _no_entity(body)


def test_the_verdict_publishers_title_is_plain():
    title = verdictpublish._cap_title(f"{ARROW} for @octocat")
    assert title.startswith(ARROW)
    assert "@octocat" not in title
    _no_entity(title)
