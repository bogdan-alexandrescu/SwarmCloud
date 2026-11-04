"""A step whose type is already known asks Jev nothing (owner's choice, 4
November 2026; part of #2688).

SwarmCloud's own usage ledger, 3 to 4 November: 172 of 377 Jev calls were the
step-type classification (J0, `tasktype.classify_with_jev`), and 122 of those
typed AI Studio's visual-check capture steps, whose type the QA renderer
already names. Held here:

  * every platform template, rendered by its REAL renderer in swarm-api, is
    recognised (`tasktype.known_template`), so the leads cannot drift from
    what the templates write;
  * a capture step and a review step make no J0 call and record a J0 row with
    outcome `skipped:known_type` in the attempt's ledger;
  * a step the rules really leave as implement (the issue run's own implement
    step, an ad-hoc brief) still asks;
  * the skip shows in the usage tally as a skip, not a call: the worker's
    `budget.split_by_kind` and swarm-api's `jevusage.row_kinds`.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker.contextpack import tasktype
from agent_worker.judge import budget
from swarm_api import issueci, issueruns, jevusage, mergewake, qaspec, validation
from swarm_api.issueruns import IssueRef, IssueRun, RunState
from swarm_api.schedtypes import pr_shepherd

_PLAN = {
    "summary": "Make the widget list sortable by name.",
    "mode": "workflow",
    "requirements": ["The Name header sorts the list"],
    "steps": [{"step_id": "sort-key", "title": "Add a sort key",
               "prompt": "Add a name sort key to WidgetList."}],
}
_PR_URL = "https://github.com/example-org/widgets/pull/7"
_REPO_URL = "https://github.com/example-org/widgets"


def _run(**over: Any) -> IssueRun:
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    fields: dict[str, Any] = dict(
        id="run_abc123", tenant_id="eng", created_by="a@example.com", created_at=now,
        updated_at=now, state=RunState.PLANNING,
        issue=IssueRef(owner="example-org", repo="widgets", number=42),
        plan_approval="required", auto_merge=False, fix_rounds=3, planner_task_id="tsk_1",
        plan=_PLAN, plan_digest=issueruns.plan_digest(_PLAN))
    fields.update(over)
    return IssueRun(**fields)


def _qa_target() -> dict[str, Any]:
    return {
        "id": "qat_3f9a", "revision": 4, "name": "Pricing site", "tenant_id": "tenant-a",
        "base_url": "https://app.example.com",
        "repository": "https://github.com/example-org/pricing-site",
        "sign_in": {"kind": "none"},
        "screens": [{"id": "home", "path": "/"}, {"id": "pricing", "path": "/pricing"}],
        "flows": [{"id": "find-docs", "goal": "find the API reference", "start_path": "/"},
                  {"id": "buy", "goal": "buy a plan", "start_path": "/pricing",
                   "mutating": True}],
        # non-production: only such a target may run a mutating flow (`flow-<id>`)
        "environment": "non_production",
    }


def _prompts_of(workflow: Any) -> dict[str, str]:
    return {step.step_id: step.input["prompt"] for step in workflow.steps}


def _issue_run_steps() -> dict[str, str]:
    return _prompts_of(issueruns.compile_plan(_run()))


def _template_prompts() -> list[tuple[str, str, str]]:
    """`(template, step id, prompt)` for every platform template, rendered."""
    out: list[tuple[str, str, str]] = []
    spec = qaspec.render(_qa_target())
    for step in spec["steps"]:
        sid = step["step_id"]
        name = ("capture" if sid.startswith("capture-") else "qa-review" if sid.startswith("review-")
                else "flow" if sid.startswith("flow") and sid != "flow-report"
                else "qa-report" if sid in ("report", "flow-report") else None)
        if name is not None:
            out.append((name, sid, step["prompt"]))
    discovery = {**_qa_target(), "screens": None, "discover": {"max_pages": 30, "max_depth": 2}}
    for step in qaspec.render_discovery(discovery)["steps"]:
        out.append(("discover", step["step_id"], step["prompt"]))
    issue = _issue_run_steps()
    out += [("requirements-check", "requirements-check", issue["requirements-check"]),
            ("review", "review", issue["review"]), ("fix", "fix", issue["fix"]),
            ("re-review", "re-review", issue["re-review"]),
            ("plan", "planner", issueruns.planner_prompt(_run().issue))]
    out += [("re-review", "re-review", validation.REREVIEW_PREAMBLE)]
    excerpt = "FAILED tests/test_x.py::test_y"
    for lead in ("CI is red on the pull request for GitHub issue example-org/widgets#42.",
                 f"CI is red on pull request #7 ({_PR_URL}), which this workflow's merge step "
                 "merges once its checks are green."):
        ci = issueci.ci_fix_continuation(
            continues_task="tsk_9", repository_url=_REPO_URL, lead=lead, round_no=1, rounds=3,
            excerpt=excerpt, step_input={}, metadata={})
        out += [("ci-fix", sid, p) for sid, p in _prompts_of(ci).items()]
    conflict = mergewake.conflict_fix_continuation(
        continues_task="tsk_9", repository_url=_REPO_URL, number=7, url=_PR_URL, base="main",
        round_no=1, rounds=3, metadata={})
    out += [("conflict-fix", sid, p) for sid, p in _prompts_of(conflict).items()]
    shepherd = pr_shepherd.conflict_fix_workflow(
        _run(pr_task_id="tsk_9"),
        SimpleNamespace(name="example-org/widgets#7", repo="example-org/widgets", number=7,
                        head_sha="a" * 40, conflict_files=["src/a.py"],
                        listed=SimpleNamespace(pull=SimpleNamespace(url=_PR_URL, base_ref="main"))))
    out += [("conflict-fix", sid, p) for sid, p in _prompts_of(shepherd).items()]
    pick = mergewake.pick_fix_continuation(
        continues_task="tsk_9", repository_url=_REPO_URL, number=7, url=_PR_URL, round_no=1,
        rounds=3, picks=["question 0: the owner picked 'b'"], metadata={})
    out += [("pick-fix", sid, p) for sid, p in _prompts_of(pick).items()]
    advisory = mergewake.advisory_pick_fix_round(
        repository_url=_REPO_URL,
        merge={"id": "tsk_m", "result_summary": {"pull_request": 7}},
        claim={"task_id": "tsk_9", "question_index": 0, "step_id": "impl", "question": "Which?",
               "picked": "b", "built": "a"})
    names = {mergewake.ADVISORY_PICK_FIX_STEP: "pick-fix", mergewake.REVIEW_STEP: "review",
             mergewake.FIX_STEP: "fix"}
    out += [(names[sid], sid, p) for sid, p in _prompts_of(advisory).items()]
    return out


class _Judge:
    """A Jev client whose `decide` must not run, over a real ledger."""

    def __init__(self, ledger_path: Path) -> None:
        self.backend = SimpleNamespace(name="typesafe", model="jev-1.14.0")
        self.ledger = budget.Ledger.create(ledger_path)
        self.asked: list[str] = []

    def decide(self, kind: str, state: Any, questions: Any) -> Any:
        self.asked.append(kind)
        return SimpleNamespace(ok=True, answers={
            "role": {"choice": "test", "probabilities": {"test": 0.9}, "confidence": 0.9}})


def _audit(judge: _Judge) -> list[dict[str, Any]]:
    _calls, _tokens, rows = judge.ledger.take_unsettled_attempt()
    return rows


def test_every_platform_template_is_recognised_as_rendered() -> None:
    rendered = _template_prompts()
    seen = {name for name, _sid, _p in rendered}
    # MUTATION: drop a template from the renders above and this names it.
    assert seen >= {"capture", "discover", "qa-review", "flow", "qa-report", "plan", "review",
                    "re-review", "fix", "requirements-check", "ci-fix", "conflict-fix",
                    "pick-fix"}
    for name, sid, prompt in rendered:
        # MUTATION: reword a lead in TEMPLATE_LEADS and its template is not recognised.
        assert tasktype.known_template({"prompt": prompt}) == name, sid


def test_the_issue_runs_implement_step_and_an_ad_hoc_brief_are_not_templates() -> None:
    impl = _issue_run_steps()["impl-sort-key"]
    assert tasktype.known_template({"prompt": impl}) is None
    assert tasktype.known_template({"prompt": "Make fetch return a copy."}) is None
    assert tasktype.known_template({}) is None


def test_a_capture_step_makes_no_call_and_records_the_skip(tmp_path: Path) -> None:
    (capture,) = [p for n, _s, p in _template_prompts() if n == "capture"][:1]
    judge = _Judge(tmp_path / "ledger.json")
    kind = tasktype.classify_with_jev({"prompt": capture}, tmp_path, {}, judge)
    # MUTATION: drop `known_template` from `classify_with_jev` and J0 is asked.
    assert judge.asked == []
    assert kind == "implement"
    (row,) = _audit(judge)
    assert row["kind"] == "J0" and row["outcome"] == "skipped:known_type"
    assert row["input_tokens"] == 0 and row["backend"] == "typesafe"


def test_a_review_step_makes_no_call_and_records_the_skip(tmp_path: Path) -> None:
    review = _issue_run_steps()["review"]
    payload = {"prompt": review, "staged_inputs": [{"path": "work/swarm-work.patch"}],
               "expected_outputs": ["verdict.json"]}
    judge = _Judge(tmp_path / "ledger.json")
    assert tasktype.classify_with_jev(payload, tmp_path, {}, judge) == "review"
    assert judge.asked == []
    # MUTATION: record nothing when the rules type the step and no row is here.
    (row,) = _audit(judge)
    assert row["outcome"] == f"skipped:{tasktype.KNOWN_TYPE_SKIP}"


def test_a_step_the_rules_leave_as_implement_still_asks(tmp_path: Path) -> None:
    impl = _issue_run_steps()["impl-sort-key"]
    judge = _Judge(tmp_path / "ledger.json")
    # MUTATION: skip J0 for every step and this answer is implement, unasked.
    assert tasktype.classify_with_jev({"prompt": impl}, tmp_path, {}, judge) == "test"
    assert judge.asked == ["J0"]
    assert _audit(judge) == []


def test_without_a_jev_client_nothing_is_recorded_and_the_type_is_unchanged(tmp_path: Path) -> None:
    capture = next(p for n, _s, p in _template_prompts() if n == "capture")
    assert tasktype.classify_with_jev({"prompt": capture}, tmp_path, {}, None) == "implement"
    assert tasktype.classify({"prompt": capture}, tmp_path, {}) == "implement"


def test_a_ledger_that_refuses_the_row_costs_the_pack_nothing(tmp_path: Path) -> None:
    class _Broken:
        backend = SimpleNamespace(name="typesafe", model="m")

        class ledger:  # noqa: N801 -- the attribute the client names
            @staticmethod
            def record(row: Any, *, succeeded: Any) -> bool:
                raise OSError("read-only ledger")

        def decide(self, *a: Any) -> Any:
            raise AssertionError("J0 asked for a known type")

    capture = next(p for n, _s, p in _template_prompts() if n == "capture")
    assert tasktype.classify_with_jev({"prompt": capture}, tmp_path, {}, _Broken()) == "implement"


def test_the_skip_shows_in_the_usage_tally_as_a_skip_not_a_call(tmp_path: Path) -> None:
    judge = _Judge(tmp_path / "ledger.json")
    capture = next(p for n, _s, p in _template_prompts() if n == "capture")
    tasktype.classify_with_jev({"prompt": capture}, tmp_path, {}, judge)
    rows = _audit(judge)
    split = budget.split_by_kind(rows, calls=0, tokens=0)
    # The worker's split: J0 has no call and one known_type skip.
    assert split["J0"]["calls"] == 0
    assert split["J0"]["outcomes"] == {"skipped:known_type": 1}
    # swarm-api's tally of that attempt row: the usage page's skipped-by-reason.
    tally = jevusage.row_kinds({"by_kind": split})["J0"]
    assert tally.calls == 0
    assert tally.outcomes() == {"ok": 0, "failed": 0, "skipped": 1,
                                "skipped_by_reason": {"known_type": 1}, "unrecorded": 0}


@pytest.mark.parametrize("lead", ["Capture, do not review.", "Visual QA review of"])
def test_a_lead_only_counts_at_the_start(lead: str) -> None:
    # A brief that quotes a template's lead mid-text is not that template.
    prompt = f"Make fetch return a copy; the docs say '{lead} x'. {secrets.token_hex(4)}"
    assert tasktype.known_template({"prompt": prompt}) is None
