"""An issue run's pull request says `Closes #N` only when the review confirmed it (#454).

Owner decision on #454: "`Closes #N` only when the review confirms every
requirement in the planner's list; otherwise `part of #N` naming what is
left." What these tests hold:

  1. THE REVIEW IS ASKED, PER REQUIREMENT. Its prompt numbers the plan's
     requirements and asks for `requirements: [{index, met, note}]` in the
     same verdict.json the fix step's gate reads.
  2. ONLY A COMPLETE, WELL-FORMED "ALL MET" CLOSES. One unmet requirement, a
     missing or malformed list, a plan with no requirements, or no verdict
     at all is `part of #N`, with what is left named.
  3. THE BLOCK IS ONE BLOCK. Written again after a fix round it replaces
     itself; the text the agent wrote around it survives; no other closing
     keyword survives anywhere in the body or the title.
  4. NO AGENT IS TOLD IT MAY CLOSE THE ISSUE. Every compiled prompt says not
     to write `Closes/Fixes/Resolves #N`.

No credentials, no network, no emulator: GitHub is the fake transport the
CI-loop tests use.
"""

from __future__ import annotations

import json
import re

import pytest

from swarm_api import issueci, issuecomments, issueruns
from swarm_api.issueruns import compile_plan, requirements_finding

from .test_issue_run_ci import (  # noqa: F401  (fixtures, imported to be used)
    PR,
    SHA_A,
    SHA_B,
    _read,
    _round_ends,
    _rounds,
    _step_task,
    _stored,
    _workflow_ends,
    api_context,
    clock,
    forge_tokens,
    writes,
)
from .test_issue_runs import BUCKET, FULL_PLAN, _approve, _create, _finish_planner, _run
from .test_issue_writeback import _bare_run

REQUIREMENTS = FULL_PLAN["requirements"]
CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+#42\b")


def _verdict(*met: bool, verdict: str = "MERGE", notes: tuple[str, ...] = ()) -> dict:
    return {
        "verdict": verdict,
        "findings": [],
        "requirements": [
            {"index": i, "met": ok, "note": notes[i - 1] if i <= len(notes) else ""}
            for i, ok in enumerate(met, start=1)
        ],
    }


# --------------------------------------------------------------------------
# 1. the review is asked, per requirement
# --------------------------------------------------------------------------

def _full_run(**fields):
    return _bare_run(plan=FULL_PLAN, plan_digest=issueruns.plan_digest(FULL_PLAN), **fields)


def test_the_review_prompt_numbers_the_requirements_and_asks_for_each():
    spec = compile_plan(_full_run()).model_dump()
    (review,) = [s for s in spec["steps"] if s["step_id"] == issueruns.REVIEW_STEP]
    prompt = review["input"]["prompt"]
    assert "1. The Name header sorts the list" in prompt
    assert "2. The sort survives a reload" in prompt
    assert '"requirements"' in prompt and '"index"' in prompt and '"met"' in prompt
    assert issueruns.VERDICT_FILE in prompt


def test_no_compiled_prompt_lets_an_agent_close_the_issue():
    run = _full_run(pr_task_id="tsk_int", pull_request={"number": PR, "url": "u"})
    spec = compile_plan(run).model_dump()
    prompts = [s["input"]["prompt"] for s in spec["steps"]]
    prompts.append(issueci.ci_fix_workflow(run, 1, "red", SHA_A).model_dump()["steps"][0]["input"]["prompt"])
    for prompt in prompts:
        assert issueruns.NO_CLOSING_KEYWORD in prompt
    assert "Closes" in issueruns.NO_CLOSING_KEYWORD and "#42" not in issueruns.NO_CLOSING_KEYWORD


# --------------------------------------------------------------------------
# 2. only a complete, well-formed "all met" closes (the pure check)
# --------------------------------------------------------------------------

def test_every_requirement_met_is_all_met():
    assert requirements_finding(FULL_PLAN, json.dumps(_verdict(True, True))) == (True, [], None)


def test_one_unmet_requirement_is_named_with_the_reviews_note():
    met, unmet, note = requirements_finding(
        FULL_PLAN, json.dumps(_verdict(True, False, notes=("", "reload drops it")))
    )
    assert met is False and note is None
    assert unmet == ["The sort survives a reload (reload drops it)"]


@pytest.mark.parametrize(
    "content,why",
    [
        ("not json", "not JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"verdict": "MERGE"}), "no `requirements` list"),
        (json.dumps(_verdict(True)), "requirement 2"),
        (json.dumps({"requirements": [{"index": 1, "met": True}, {"index": 1, "met": True}]}),
         "more than once"),
        (json.dumps({"requirements": [{"index": 1, "met": "yes"}, {"index": 2, "met": True}]}),
         "true or false"),
        (json.dumps({"requirements": [{"index": 0, "met": True}, {"index": 2, "met": True}]}),
         "index"),
        (json.dumps({"requirements": [{"index": True, "met": True}, {"index": 2, "met": True}]}),
         "index"),
        (json.dumps({"requirements": [{"index": 1, "met": True, "extra": 1},
                                      {"index": 2, "met": True}]}), "extra"),
        (json.dumps({"requirements": [{"index": 1, "met": True, "note": 5},
                                      {"index": 2, "met": True}]}), "note"),
        (json.dumps({"requirements": [{"index": 1, "met": True}, {"index": 2, "met": True},
                                      {"index": 3, "met": True}]}), "index"),
    ],
)
def test_a_malformed_or_incomplete_list_is_not_all_met(content, why):
    met, unmet, note = requirements_finding(FULL_PLAN, content)
    assert met is False
    # Nothing was confirmed, so every requirement is still owed.
    assert unmet == REQUIREMENTS
    assert note is not None and why in note


def test_no_verdict_at_all_is_not_all_met():
    met, unmet, note = requirements_finding(FULL_PLAN, None, problem="the review wrote no verdict.json")
    assert (met, unmet) == (False, REQUIREMENTS)
    assert "no verdict.json" in note


def test_a_plan_with_no_requirements_never_closes():
    plan = {k: v for k, v in FULL_PLAN.items() if k != "requirements"}
    met, unmet, note = requirements_finding(plan, json.dumps({"verdict": "MERGE", "requirements": []}))
    assert (met, unmet) == (False, [])
    assert "no requirements" in note


# --------------------------------------------------------------------------
# 2 and 3, through the run: the PR body
# --------------------------------------------------------------------------

def _review_writes(db, objects, workflow_id: str, content: str | None) -> None:
    """The review task's verdict.json, exactly as a worker uploads it."""
    review = _step_task(db, workflow_id, issueruns.REVIEW_STEP)
    if content is None:
        review["result_summary"] = {"artifacts": []}
        return
    key = (
        f"tenants/{review['tenant_id']}/tasks/{review['id']}/attempts/att_1/artifacts/"
        f"{issueruns.VERDICT_FILE}"
    )
    objects.put(key, content)
    review["result_summary"] = {"artifacts": [{
        "name": issueruns.VERDICT_FILE, "bytes": len(content.encode()), "uri": f"gs://{BUCKET}/{key}",
    }]}


def _checking(
    client, db, objects, writes, verdict, *, body: str = "", title: str = "", plan=FULL_PLAN
) -> dict:
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    planned = _run(client, run["id"]).json()["run"]
    running = _approve(client, run["id"], planned["plan_digest"]).json()["run"]
    writes.open_pull(PR, SHA_A, body=body)
    if title:
        writes.pulls[PR]["title"] = title
    _workflow_ends(db, running["workflow_id"])
    _review_writes(
        db, objects, running["workflow_id"],
        verdict if verdict is None or isinstance(verdict, str) else json.dumps(verdict),
    )
    return running


def _block(body: str, run_id: str) -> str:
    start = issuecomments.marker(run_id, issuecomments.KEYWORD_KIND)
    end = issuecomments.keyword_end(run_id)
    assert body.count(start) == 1 and body.count(end) == 1, body
    return body[body.index(start): body.index(end)]


def test_all_met_closes_the_issue(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, _verdict(True, True))

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING"
    assert run["requirements_met"] is True and run["requirements_unmet"] == []
    body = writes.pulls[PR]["body"]
    assert "Closes #42" in _block(body, run["id"])
    assert CLOSING.findall(body) == ["Closes #42"]
    status = [c["body"] for c in writes.on_issue(42) if ":status -->" in c["body"]]
    assert status and "`Closes #42`" in status[-1]


def test_one_unmet_requirement_is_part_of_naming_it(client, db, objects, writes, clock):
    running = _checking(
        client, db, objects, writes, _verdict(True, False, notes=("", "reload drops it"))
    )

    run = _read(client, clock, running["id"])

    assert run["requirements_met"] is False
    block = _block(writes.pulls[PR]["body"], run["id"])
    assert "part of #42" in block and "Closes" not in block
    assert "The sort survives a reload (reload drops it)" in block
    assert "The Name header sorts the list" not in block
    status = [c["body"] for c in writes.on_issue(42) if ":status -->" in c["body"]]
    assert status and "`part of #42`" in status[-1]


@pytest.mark.parametrize("verdict", ["{not json", None, {"verdict": "MERGE", "findings": []}])
def test_a_malformed_or_missing_review_is_part_of(client, db, objects, writes, clock, verdict):
    running = _checking(client, db, objects, writes, verdict)

    run = _read(client, clock, running["id"])

    assert run["requirements_met"] is False and run["requirements_note"]
    body = writes.pulls[PR]["body"]
    assert "part of #42" in _block(body, run["id"]) and not CLOSING.findall(body)


def test_the_block_is_rewritten_after_a_fix_round_once_and_the_agents_text_survives(
    client, db, objects, writes, clock
):
    running = _checking(
        client, db, objects, writes, _verdict(True, True),
        body="The agent's summary of the change.\n\nFixes #42",
    )
    first = _read(client, clock, running["id"])
    assert "Closes #42" in _block(writes.pulls[PR]["body"], first["id"])

    # CI red: one round; its agent rewrites the body above the block.
    writes.check(SHA_A, "unit", "failure")
    _read(client, clock, running["id"])
    (round_one,) = _rounds(db, running["id"])
    writes.pulls[PR]["body"] = (
        "Rewritten by the fix round.\n\nResolves #42\n\n" + writes.pulls[PR]["body"]
    )
    writes.pulls[PR]["head"]["sha"] = SHA_B
    _round_ends(db, round_one)

    after = _read(client, clock, running["id"])

    body = writes.pulls[PR]["body"]
    assert after["state"] == "CHECKING"
    assert "Rewritten by the fix round." in body and "The agent's summary of the change." in body
    assert CLOSING.findall(body) == ["Closes #42"]
    _block(body, after["id"])  # exactly one block


def test_writing_the_block_twice_changes_nothing_the_second_time(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, _verdict(True, False))
    run = _read(client, clock, running["id"])
    once = writes.pulls[PR]["body"]
    stored = issueruns.IssueRun.from_firestore(_stored(db, run["id"]))

    block = issuecomments.keyword_block(
        stored, closes=stored.requirements_met is True, unmet=stored.requirements_unmet
    )
    assert issuecomments.apply_keyword_block(once, stored, block) == once


def test_a_platform_title_that_would_close_the_issue_is_retitled(client, db, objects, writes, clock):
    # The worker's fallback title for a step with an `issue` input is
    # "Fixes #N"; a squash merge puts the title in a commit on the default
    # branch, which closes the issue whatever the body says.
    running = _checking(client, db, objects, writes, _verdict(True, False), title="Fixes #42")

    _read(client, clock, running["id"])

    assert not CLOSING.findall(writes.pulls[PR]["title"])
    assert "#42" in writes.pulls[PR]["title"]


def test_a_title_without_a_closing_keyword_is_left_alone(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, _verdict(True, True), title="Sortable widgets (#42)")

    _read(client, clock, running["id"])

    assert writes.pulls[PR]["title"] == "Sortable widgets (#42)"
    assert not [c for c in writes.calls if c[0] == "PATCH" and b'"title"' in (c[3] or b"")]


def test_a_failed_keyword_write_is_written_again_and_a_green_run_waits_for_it(
    client, db, objects, writes, clock
):
    # One failed write must not leave the worker's "Fixes #42" title on a pull
    # request whose review left a requirement open: a squash merge would close
    # the issue. The run stays CHECKING, green or not, until the block is on it.
    running = _checking(client, db, objects, writes, _verdict(True, False), title="Fixes #42")
    writes.check(SHA_A, "unit", "success")
    writes.status = {"PATCH": 503}

    held = _read(client, clock, running["id"])

    assert held["state"] == "CHECKING" and held["pull_request"]["checks"] == "green"
    assert held["green_sha"] is None
    assert held["writeback_error"]
    assert CLOSING.findall(writes.pulls[PR]["title"]), "the failed write changed the title"
    assert _stored(db, running["id"])["pull_request"].get("keyword_written") is None

    writes.status = {}
    done = _read(client, clock, running["id"])

    assert done["state"] == "DONE" and done["green_sha"] == SHA_A
    assert not CLOSING.findall(writes.pulls[PR]["title"])
    assert not CLOSING.findall(writes.pulls[PR]["body"])
    assert "part of #42" in _block(writes.pulls[PR]["body"], running["id"])
    assert _stored(db, running["id"])["pull_request"]["keyword_written"] == "part_of"


def test_a_written_keyword_is_not_written_again_on_every_read(client, db, objects, writes, clock):
    running = _checking(client, db, objects, writes, _verdict(True, True))

    first = _read(client, clock, running["id"])
    assert first["state"] == "CHECKING"
    assert _stored(db, running["id"])["pull_request"]["keyword_written"] == "closes"
    patches = [c for c in writes.calls if c[0] == "PATCH" and "/pulls/" in c[1]]

    _read(client, clock, running["id"])

    assert [c for c in writes.calls if c[0] == "PATCH" and "/pulls/" in c[1]] == patches


# --------------------------------------------------------------------------
# a staged plan: the CI loop still finds the integrator and the review
# --------------------------------------------------------------------------

#: FULL_PLAN's requirements over stages: two independent roots, then a step
#: joining both -- `depends_on` compiles it to {sort-key, ui} then {wire}.
STAGED_FULL_PLAN = {
    **FULL_PLAN,
    "steps": [
        {"step_id": "sort-key", "title": "Add a sort key",
         "prompt": "Add a name sort key to WidgetList.", "depends_on": []},
        {"step_id": "ui", "title": "Draw the header",
         "prompt": "Add a sortable Name header.", "depends_on": []},
        {"step_id": "wire", "title": "Wire the header",
         "prompt": "Make the Name header toggle the sort.", "depends_on": ["sort-key", "ui"]},
    ],
}


def test_a_staged_plan_compiles_to_one_review_after_every_step_and_one_integrator():
    # `issueci` finds the review and the integrator by step id in the
    # compiled workflow; stages (#540) must keep exactly one of each, with the
    # review waiting for every implementer and the fix gated on it.
    workflow = compile_plan(_bare_run(
        plan=STAGED_FULL_PLAN, plan_digest=issueruns.plan_digest(STAGED_FULL_PLAN),
    ))
    by_id = {step.step_id: step for step in workflow.steps}
    assert [s.step_id for s in workflow.steps].count(issueruns.REVIEW_STEP) == 1
    assert [s.step_id for s in workflow.steps].count(issueruns.FIX_STEP) == 1
    impl = ["impl-sort-key", "impl-ui", "impl-wire"]
    assert sorted(by_id[issueruns.REVIEW_STEP].depends_on) == sorted(impl)
    assert by_id[issueruns.FIX_STEP].depends_on == [issueruns.REVIEW_STEP]
    assert issueruns.plan_stages(issueruns.parse_plan(STAGED_FULL_PLAN)) == [
        ["sort-key", "ui"], ["wire"],
    ]


def test_a_staged_run_reads_its_integrators_pull_request_and_its_reviews_verdict(
    client, db, objects, writes, clock
):
    running = _checking(client, db, objects, writes, _verdict(True, True), plan=STAGED_FULL_PLAN)

    run = _read(client, clock, running["id"])

    assert run["state"] == "CHECKING", run.get("error")
    integrator = _step_task(db, running["workflow_id"], issueruns.FIX_STEP)
    assert run["pr_task_id"] == integrator["id"]
    assert run["pull_request"]["number"] == PR
    # The verdict was read from the review task the stages compiled: all met.
    assert run["requirements_met"] is True, run.get("requirements_note")
    assert run["requirements_unmet"] == []
    assert "Closes #42" in _block(writes.pulls[PR]["body"], run["id"])



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
