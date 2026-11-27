"""A fix loop answers "not reviewed rendered" with a rendered re-review (issue 393).

The worker's rendered-review gate (`agent_worker.verdict.gate_rendered_review`)
turns a UI lane's MERGE into NOT_YET with the finding "not reviewed rendered:
..." unless the review ran on the browser profile and left screenshots. No
code change answers that finding, so the loop used to dead-end: under
`integrate` the declared fix rightly committed nothing and the loop parked
`fix_changed_nothing`; a re-review it did start ran on the first review's own
profile. What is held here (docs/design/fix-loop.md §4.3, §4.4):

  1. a first review blocked ONLY by that finding gets `re-review:1`, at the
     judged head, on the browser profile, though the fix changed nothing
     (`integrate`) and instead of a fix (`direct-pr`); it counts as a round;
  2. its MERGE ends the loop, its real findings go on to `review-fix:2`, and
     when it was not rendered either the loop parks for a person, whose added
     round is a re-review again, never a fix that cannot help;
  3. a first review with a real finding beside it goes through the fix as
     before, and a fix that changed nothing for a real finding still parks;
  4. any re-review of a lane that changed UI paths, or whose list cannot be
     built, runs on the browser profile unless the signed override names a
     profile; a code-only lane keeps the review's profile;
  5. the signed list sits where the worker's gate reads it, so a re-review
     that did not render can never end the loop MERGE.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from agent_worker import verdict as worker_verdict
from swarm_api import fixloop, reviewgate
from swarm_common.states import ParkReason

from .conftest import auth_header
from .test_fix_loop_drive import (  # noqa: F401 -- fixtures, used by name
    FIX_HEAD, IMPL_HEAD, MERGE, NOT_YET, NUMBER, SIGNER, Loop, _members_hold_grants, clock, direct_spec,
    finish, github, integrate_spec, loop_client, loop_enabled, park_running, push, pushed,
    round_task,
)

BROWSER = "claude-code-browser"
UI_FILE = "apps/swarm-ui/src/X.tsx"
GATED = ("not reviewed rendered: ran on runner profile 'claude-code', not "
         "'claude-code-browser'; no image under shots/ in its artifacts")
#: What the gate writes over a MERGE, and what a review told the rule writes itself.
REWRITTEN = {"verdict": "NOT_YET", "findings": [GATED]}
WRITTEN = {"verdict": "NOT_YET", "findings": [
    {"summary": GATED, "severity": "major", "fix": "Re-run on claude-code-browser"},
    {"summary": "a docstring typo", "severity": "minor"},
]}
MIXED = {"verdict": "NOT_YET", "findings": [
    GATED, {"summary": "the cache never expires entries", "severity": "blocker"}]}
#: The gate's rewrite of a MERGE that carried the review's own findings
#: (issue 483): plain, not marked minor, as a MERGE's findings are, with the
#: record the worker's gate writes beside them.
REWRITTEN_WITH_FINDINGS = {
    "verdict": "NOT_YET",
    "findings": [GATED, "a docstring typo", {"summary": "an unused import"}],
    "rendered_review": {
        "ui_paths": [UI_FILE], "verdict_given": "MERGE", "verdict": "NOT_YET",
        "missing": ["ran on runner profile 'claude-code', not 'claude-code-browser'",
                    "no image under shots/ in its artifacts"],
        "reason": GATED,
    },
}
GENERATED_FILE = "apps/swarm-ui/src/generated/api-types.ts"


def integrate(client, db, objects, *, review: Any = REWRITTEN, fix_head: str = IMPL_HEAD,
              files: list[str] | None = None, max_rounds: int | None = None,
              **block: Any) -> Loop:
    """implement, review, the declared fix (by default it changed nothing), one wake."""
    loop = Loop(client, db, integrate_spec(max_rounds, **block))
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD, files or [UI_FILE]))
    finish(db, objects, loop.tasks["review"], verdict=review)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], fix_head, []))
    park_running(loop)
    push(client, loop.tasks["fix"])
    return loop


def end_round(client, db, objects, loop: Loop, step: str, verdict: Any) -> str:
    task = round_task(loop, step)
    finish(db, objects, task, verdict=verdict)
    push(client, task)
    return task


def assert_running(loop: Loop, *steps: str) -> None:
    doc = loop.doc()
    assert fixloop.park_phase_of(doc) is None, (fixloop.park_phase_of(doc),
                                                fixloop.park_detail_of(doc))
    assert doc["park_reason"] == ParkReason.FIX_ROUNDS_RUNNING.value
    assert list(loop.round_steps()) == list(steps)


# --------------------------------------------------------------------------
# 1. The first review was not rendered, and nothing else blocks it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("verdict", [REWRITTEN, WRITTEN], ids=["gate", "review"])
def test_a_rendered_only_first_review_gets_a_browser_re_review_of_the_judged_head(
        loop_client, db, objects, verdict):
    loop = integrate(loop_client, db, objects, review=verdict)

    # MUTATION: keep the head == judged park first and the loop dead-ends.
    assert_running(loop, "re-review:1")
    task_id = round_task(loop, "re-review:1")
    task = loop.doc(task_id=task_id)
    assert task["repository_ref"] == IMPL_HEAD
    assert task["runner_profile"] == reviewgate.RENDERED_REVIEW_PROFILE == BROWSER
    assert task["metadata"]["dispatch"]["strategy"] == "collect"
    assert SIGNER.verifies(task, task_id)
    signed = task["metadata"]["dispatch"]["fix_loop_round"]
    assert signed["round"] == 1 and signed["kind"] == "review" and signed["head"] == IMPL_HEAD
    assert signed["rendered_review_paths"] == [UI_FILE]
    prompt = task["input"]["prompt"]
    assert "not reviewed rendered" in prompt and "shots/" in prompt
    assert "no fix was pushed" in prompt


def test_under_direct_pr_it_replaces_a_fix_that_could_change_nothing(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD, [UI_FILE]))
    finish(db, objects, loop.tasks["review"], verdict=REWRITTEN)
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    assert_running(loop, "re-review:1")
    task = loop.doc(task_id=round_task(loop, "re-review:1"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == IMPL_HEAD


# --------------------------------------------------------------------------
# 2. What the rendered re-review says
# --------------------------------------------------------------------------

def test_the_rendered_re_reviews_merge_ends_the_loop_merge(loop_client, db, objects):
    loop = integrate(loop_client, db, objects)
    rereview = end_round(loop_client, db, objects, loop, "re-review:1", MERGE)

    doc = loop.doc()
    assert doc["state"] == "SUCCEEDED", doc.get("blocked_by")
    summary = doc["result_summary"]["fix_loop"]
    assert summary["verdict"] == "MERGE"
    assert summary["final_review"] == rereview and summary["final_head"] == IMPL_HEAD
    assert len(summary["rounds"]) == 1


def test_the_rendered_re_reviews_real_findings_go_on_as_any_round(
        loop_client, db, objects, github):
    github.open_pull(NUMBER, IMPL_HEAD, ref="swarm/x")  # nobody pushed since the build
    loop = integrate(loop_client, db, objects)
    end_round(loop_client, db, objects, loop, "re-review:1", NOT_YET)

    assert_running(loop, "re-review:1", "review-fix:2")
    fix = loop.doc(task_id=round_task(loop, "review-fix:2"))
    assert fix["metadata"]["dispatch"]["fix_loop_round"]["start_head"] == IMPL_HEAD
    assert "the cache never expires entries" in fix["input"]["prompt"]


def test_the_rendered_re_review_counts_against_the_limit(loop_client, db, objects):
    loop = integrate(loop_client, db, objects, max_rounds=1)
    end_round(loop_client, db, objects, loop, "re-review:1", NOT_YET)

    assert fixloop.park_phase_of(loop.doc()) == "fix_loop_exhausted"
    assert list(loop.round_steps()) == ["re-review:1"]


def test_a_re_review_that_could_not_render_either_parks_for_a_person(loop_client, db, objects):
    loop = integrate(loop_client, db, objects)
    end_round(loop_client, db, objects, loop, "re-review:1", REWRITTEN)

    doc = loop.doc()
    assert doc["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert fixloop.park_phase_of(doc) == "fix_changed_nothing"
    detail = fixloop.park_detail_of(doc)
    assert detail["code"] == "not_rendered" and detail["head"] == IMPL_HEAD
    assert detail["round"] == 1 and detail["step"] == "re-review:1"
    assert list(loop.round_steps()) == ["re-review:1"]


@pytest.mark.parametrize("max_rounds", [1, 3])
def test_the_round_a_person_adds_after_it_is_a_re_review_on_the_browser(
        loop_client, db, objects, max_rounds):
    loop = integrate(loop_client, db, objects, max_rounds=max_rounds)
    end_round(loop_client, db, objects, loop, "re-review:1", REWRITTEN)

    response = loop_client.post(f"/v1/workflows/{loop.workflow_id}/fix-loop/rounds",
                                headers=auth_header("alice"), json={"rounds": 1})
    assert response.status_code == 200, response.text

    assert_running(loop, "re-review:1", "re-review:2")
    task = loop.doc(task_id=round_task(loop, "re-review:2"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == IMPL_HEAD


def test_a_later_re_review_that_was_not_rendered_gets_one_more_look_not_a_fix(
        loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD)
    end_round(loop_client, db, objects, loop, "re-review:1", REWRITTEN)

    assert_running(loop, "re-review:1", "re-review:2")
    task = loop.doc(task_id=round_task(loop, "re-review:2"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == FIX_HEAD

    end_round(loop_client, db, objects, loop, "re-review:2", REWRITTEN)
    assert fixloop.park_detail_of(loop.doc())["code"] == "not_rendered"


# --------------------------------------------------------------------------
# 2b. The gate's rewrite of a MERGE that carried its own findings (issue 483)
# --------------------------------------------------------------------------

def test_a_rewritten_merge_that_carries_findings_gets_a_browser_re_review(
        loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=REWRITTEN_WITH_FINDINGS)

    # MUTATION: ignore the record and this parks `fix_changed_nothing`.
    assert_running(loop, "re-review:1")
    task_id = round_task(loop, "re-review:1")
    task = loop.doc(task_id=task_id)
    assert task["repository_ref"] == IMPL_HEAD
    assert task["runner_profile"] == BROWSER
    assert SIGNER.verifies(task, task_id)
    signed = task["metadata"]["dispatch"]["fix_loop_round"]
    assert signed[fixloop.ANSWERS_NOT_RENDERED] is True
    assert signed["head"] == IMPL_HEAD
    # The carried findings stay in the round, so the rendered review sees them.
    prompt = task["input"]["prompt"]
    assert "a docstring typo" in prompt and "an unused import" in prompt
    assert "no fix was pushed" in prompt


def test_a_rewritten_merge_with_findings_under_direct_pr_starts_re_review_1(
        loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD, [UI_FILE]))
    finish(db, objects, loop.tasks["review"], verdict=REWRITTEN_WITH_FINDINGS)
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    # MUTATION: ignore the record and this is review-fix:1.
    assert_running(loop, "re-review:1")
    task = loop.doc(task_id=round_task(loop, "re-review:1"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == IMPL_HEAD


def test_a_later_rewritten_merge_gets_one_more_look_then_parks_not_rendered(
        loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD)
    end_round(loop_client, db, objects, loop, "re-review:1", REWRITTEN_WITH_FINDINGS)

    assert_running(loop, "re-review:1", "re-review:2")
    task = loop.doc(task_id=round_task(loop, "re-review:2"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == FIX_HEAD

    end_round(loop_client, db, objects, loop, "re-review:2", REWRITTEN_WITH_FINDINGS)
    doc = loop.doc()
    assert doc["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert fixloop.park_detail_of(doc)["code"] == "not_rendered"

    response = loop_client.post(f"/v1/workflows/{loop.workflow_id}/fix-loop/rounds",
                                headers=auth_header("alice"), json={"rounds": 1})
    assert response.status_code == 200, response.text
    assert_running(loop, "re-review:1", "re-review:2", "re-review:3")
    task = loop.doc(task_id=round_task(loop, "re-review:3"))
    assert task["runner_profile"] == BROWSER and task["repository_ref"] == FIX_HEAD


def test_a_masked_rendered_review_record_refuses_the_verdict_naming_it(
        loop_client, db, objects):
    record = {**REWRITTEN_WITH_FINDINGS["rendered_review"],
              "reason": f"token: {secrets.token_hex(16)}"}
    loop = integrate(loop_client, db, objects,
                     review={**REWRITTEN_WITH_FINDINGS, "rendered_review": record,
                             "findings": ["a docstring typo"]})

    doc = loop.doc()
    # MUTATION: leave the key out of READ_VERDICT_KEYS and the file is read.
    assert fixloop.park_phase_of(doc) == "review_unreadable"
    detail = fixloop.park_detail_of(doc)
    assert detail["code"] == "credential"
    assert list(detail["keys"]) == ["rendered_review"]
    assert "rendered_review" in fixloop.READ_VERDICT_KEYS


# --------------------------------------------------------------------------
# 3. Every other outcome, as before
# --------------------------------------------------------------------------

def test_a_real_blocker_beside_it_goes_through_the_fix(loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=MIXED, fix_head=FIX_HEAD)

    assert_running(loop, "re-review:1")
    task = loop.doc(task_id=round_task(loop, "re-review:1"))
    assert task["repository_ref"] == FIX_HEAD
    assert "no fix was pushed" not in task["input"]["prompt"]


def test_a_real_blocker_beside_it_under_direct_pr_starts_review_fix_1(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD, [UI_FILE]))
    finish(db, objects, loop.tasks["review"], verdict=MIXED)
    park_running(loop)
    push(loop_client, loop.tasks["review"])

    assert_running(loop, "review-fix:1")


@pytest.mark.parametrize("review", [
    NOT_YET, MIXED,
    # A finding nobody can read is not the gate's.
    {"verdict": "NOT_YET", "findings": [GATED, 7]},
    {"verdict": "NOT_YET", "findings": []},
    # Past the bound on kept findings, still a finding.
    {"verdict": "NOT_YET", "findings": [GATED] * fixloop.MAX_FINDINGS + ["a real defect"]},
    # Issue 483: only a record of the gate's rewrite of a MERGE counts.
    {**REWRITTEN_WITH_FINDINGS, "rendered_review": {
        **REWRITTEN_WITH_FINDINGS["rendered_review"], "verdict_given": "NOT_YET"}},
    {**REWRITTEN_WITH_FINDINGS, "rendered_review": "MERGE"},
    {**REWRITTEN_WITH_FINDINGS, "rendered_review": [{"verdict_given": "MERGE"}]},
    # The record with no finding of the gate's beside it is not the gate's.
    {**REWRITTEN_WITH_FINDINGS, "findings": ["a docstring typo"]},
], ids=["real", "mixed", "unreadable-finding", "none", "past-the-bound",
        "record-given-not-yet", "record-a-string", "record-a-list", "record-no-gate-finding"])
def test_a_fix_that_changed_nothing_for_any_other_verdict_still_parks(
        loop_client, db, objects, review):
    loop = integrate(loop_client, db, objects, review=review)

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) == "fix_changed_nothing"
    assert "code" not in fixloop.park_detail_of(doc)
    assert loop.round_steps() == {}


# --------------------------------------------------------------------------
# 4. The profile of any re-review
# --------------------------------------------------------------------------

def _re_review(loop: Loop) -> dict[str, Any]:
    return loop.doc(task_id=round_task(loop, "re-review:1"))


def test_a_ui_lanes_re_review_runs_on_the_browser(loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD)

    assert _re_review(loop)["runner_profile"] == BROWSER


@pytest.mark.parametrize("files", [
    ["apps/swarm-api/x.py"], ["apps/swarm-ui/src/__tests__/X.test.tsx"]], ids=["api", "test"])
def test_a_code_only_lanes_re_review_keeps_the_reviews_profile(loop_client, db, objects, files):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD, files=files)

    assert _re_review(loop)["runner_profile"] == "mock"


def test_a_generated_only_lanes_re_review_is_not_gated(loop_client, db, objects):
    # Issue 483: the API change's lane, whose only console path is generated.
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD,
                     files=["apps/swarm-api/x.py", GENERATED_FILE])

    task = _re_review(loop)
    # MUTATION: count apps/swarm-ui/src/generated/ as UI and this is the browser.
    assert task["runner_profile"] == "mock"
    assert worker_verdict.ui_paths_in_dispatch(task["metadata"]["dispatch"]) == ([], False)


def test_a_lane_whose_list_cannot_be_built_is_read_as_ui(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["implement"], git={
        **pushed(loop.tasks["implement"], IMPL_HEAD), "own_files_truncated": True})
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    push(loop_client, loop.tasks["fix"])

    task = _re_review(loop)
    assert task["runner_profile"] == BROWSER
    assert worker_verdict.ui_paths_in_dispatch(task["metadata"]["dispatch"]) == ([], True)


def test_a_signed_overrides_profile_wins_on_an_ordinary_round(loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD,
                     re_review={"runner_profile": "mock"})

    assert _re_review(loop)["runner_profile"] == "mock"


def test_an_override_that_cannot_render_does_not_answer_the_gate(loop_client, db, objects):
    loop = integrate(loop_client, db, objects,
                     re_review={"runner_profile": "mock", "prompt": "look at every page"})

    task = _re_review(loop)
    assert task["runner_profile"] == BROWSER
    assert "look at every page" in task["input"]["prompt"]


# --------------------------------------------------------------------------
# 5. The gate binds the re-review, as the worker reads it
# --------------------------------------------------------------------------

def test_the_workers_gate_reads_the_signed_list_of_a_re_review(loop_client, db, objects):
    loop = integrate(loop_client, db, objects)

    # MUTATION: sign the list inside `fix_loop_round` only and the worker
    # gates nothing: an unrendered MERGE would end the loop.
    assert worker_verdict.ui_paths_in_dispatch(
        _re_review(loop)["metadata"]["dispatch"]) == ([UI_FILE], False)


def test_a_code_only_re_review_is_not_gated(loop_client, db, objects):
    loop = integrate(loop_client, db, objects, review=NOT_YET, fix_head=FIX_HEAD,
                     files=["apps/swarm-api/x.py"])

    assert worker_verdict.ui_paths_in_dispatch(
        _re_review(loop)["metadata"]["dispatch"]) == ([], False)


@pytest.mark.parametrize("review,head", [(REWRITTEN, IMPL_HEAD), (MIXED, FIX_HEAD)],
                         ids=["only", "mixed"])
def test_a_finding_is_answered_by_a_gated_review_whatever_the_list_names(
        loop_client, db, objects, review, head):
    loop = integrate(loop_client, db, objects, review=review, fix_head=head,
                     files=["apps/swarm-api/x.py"])

    task = _re_review(loop)
    assert task["runner_profile"] == BROWSER
    # Gated, naming no path: a MERGE that was not rendered reads NOT_YET.
    assert worker_verdict.ui_paths_in_dispatch(task["metadata"]["dispatch"]) == ([], True)


def test_the_restated_rule_is_the_workers():
    assert reviewgate.NOT_REVIEWED_RENDERED == worker_verdict.NOT_REVIEWED_RENDERED
    assert reviewgate.RENDERED_REVIEW_PROFILE == worker_verdict.RENDERED_REVIEW_PROFILE
    for path in (UI_FILE, "/docs/web-ui/a.html", "docs/web-ui/a.test.html",
                 "docs/web-ui/tests/a.html", "apps/swarm-ui/src/App.test.tsx",
                 "apps/swarm-ui/src/a.spec.ts", "apps/swarm-ui/src/__tests__/a.tsx",
                 "apps/swarm-ui/index.html", "apps/swarm-api/x.py", "", None, 7,
                 # Issue 483: generated console files are not UI; a look-alike is.
                 GENERATED_FILE, "/apps/swarm-ui/src/generated/constants/routes.json",
                 "apps/swarm-ui/src/generatedX.tsx", "apps/swarm-ui/src/types.ts",
                 "docs/web-ui/generated/a.html"):
        assert reviewgate.is_ui_path(path) is worker_verdict.is_ui_path(path), path
    assert reviewgate.GENERATED_UI_PREFIXES == worker_verdict.GENERATED_UI_PREFIXES
    assert reviewgate.UI_PATH_PREFIXES == worker_verdict.UI_PATH_PREFIXES
    assert not reviewgate.is_ui_path(GENERATED_FILE)
    assert reviewgate.is_ui_path("apps/swarm-ui/src/types.ts")
    assert fixloop.RENDERED_REVIEW_KEY == worker_verdict.RENDERED_REVIEW_KEY
