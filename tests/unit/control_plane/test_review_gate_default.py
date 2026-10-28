"""The default review gate, its workspace setting, risk paths, and the requirements check (#2524).

docs/workflows.md "The review gate". BGD 2026-10-28:

  * "every review step": a review step that names no gate carries the
    default block (test_review_gate_default_submit.py), resolved here
    against `workspace_settings/{tenant}.review_gate`, else the platform's
    300 lines and risk paths; a step's own block wins.
  * a risk path always runs the review, whatever `min_lines` a step or a
    workspace names, and whatever list it names in place of the platform's.
  * "Gate + cheap requirements check": when the gate skips an issue run's
    review, its `requirements-check` runs, and its requirements.json list --
    only that -- is folded into the skipped review's verdict, so the keyword
    is `Closes #N` only when every requirement is met. It never writes a
    verdict.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import issueruns, reviewgate
from swarm_api.issueruns import compile_plan, plan_digest
from swarm_api.validation import parse_issue_ref

from .conftest import seed_task
from .test_review_gate import (  # noqa: F401 -- fixtures are used by name
    IMPL,
    REVIEW,
    SMALL_FILE,
    _base,
    _decide,
    _git,
    _parent,
    _review,
    _review_doc,
    client,
    contract,
    github,
    store,
    waker,
)
from .test_verdict_publish import _finished

CHECK = "task_check00001"
PLAN = {
    "summary": "Make the widget list sortable by name.",
    "requirements": ["Widgets sort by name", "The header toggles the order"],
    "steps": [{"step_id": "sort", "title": "Sort", "prompt": "Sort the widgets by name."}],
}


def _workspace(db, setting: dict[str, Any]) -> None:
    db.docs["workspace_settings/eng"] = {"review_gate": setting}


def _inline(doc: dict[str, Any]) -> dict[str, Any]:
    (entry,) = doc["result_summary"]["artifacts"]
    return json.loads(entry["inline"])


# ---------------------------------------------------------------------------
# the default, the workspace, a step's own
# ---------------------------------------------------------------------------


def test_the_default_gate_skips_a_small_change(db, client):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12))
    _review(db, gate={"default": True})

    report = _decide(client)

    # MUTATION: make `policy_of` return None for the default block and the
    # review is declined as unreadable.
    assert report["reviews_skipped"] == [REVIEW], report
    assert _inline(_review_doc(db))["verdict"] == "MERGE"


def test_the_default_runs_over_the_platforms_300_lines(db, client):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=301))
    _review(db, gate={"default": True})
    assert _decide(client)["reviews_skipped"] == []
    assert _review_doc(db)["metadata"]["control_publish"]["state"] == "declined"


def test_the_workspace_setting_moves_the_default(db, client):
    _base(db)
    _workspace(db, {"min_lines": 500})
    _parent(db, _git(SMALL_FILE, insertions=301))
    _review(db, gate={"default": True})
    # MUTATION: ignore the workspace in `_consider_review` and 301 lines run.
    assert _decide(client)["reviews_skipped"] == [REVIEW]


def test_a_workspace_that_switches_the_default_off_runs_every_review(db, client):
    _base(db)
    _workspace(db, {"enabled": False})
    _parent(db, _git(SMALL_FILE, insertions=1))
    _review(db, gate={"default": True})
    report = _decide(client)
    assert report["reviews_skipped"] == []
    assert report["declined"] == [{"task_id": REVIEW, "code": "review_gate_off"}]


def test_a_steps_own_gate_wins_over_the_workspace(db, client):
    _base(db)
    _workspace(db, {"min_lines": 5, "enabled": False})
    _parent(db, _git(SMALL_FILE, insertions=40))
    _review(db, gate={"min_lines": 50})
    assert _decide(client)["reviews_skipped"] == [REVIEW]


# ---------------------------------------------------------------------------
# risk paths always run the review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml",
    "apps/common/swarm_common/states.py",
    "apps/swarm-api/swarm_api/auth.py",
    "terraform/infra/iam.tf",
    "apps/redaction/swarm_redaction/core.py",
])
@pytest.mark.parametrize("gate", [
    {"default": True},
    {"min_lines": 1_000_000},
    # A step naming its own list does not drop the platform's.
    {"min_lines": 1_000_000, "risk_paths": ["docs/"]},
])
def test_a_risk_path_always_runs_the_review(db, client, path, gate):
    _base(db)
    _workspace(db, {"min_lines": 1_000_000, "risk_paths": ["docs/"]})
    _parent(db, _git(path, insertions=1))
    _review(db, gate=gate)
    report = _decide(client)
    # MUTATION: use a step's or workspace's list in place of the platform's.
    assert report["reviews_skipped"] == [], (path, gate)
    assert report["declined"] == [{"task_id": REVIEW, "code": "review_runs"}]


# ---------------------------------------------------------------------------
# the requirements check
# ---------------------------------------------------------------------------


def _check(db, state: str = "PARKED") -> dict[str, Any]:
    doc = seed_task(db, task_id=CHECK, tenant_id="eng", state=state,
                    park_reason="DEPENDENCY_INCOMPLETE" if state == "PARKED" else None,
                    runner_profile="claude-code", depends_on=(IMPL,))
    doc["metadata"] = {"review_gate": {"default": True, "requirements_check": True}}
    return doc


def test_the_check_runs_only_when_the_review_would_be_skipped(db, client):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12))
    _check(db)
    report = _decide(client)
    # MUTATION: decide the check as a review and it ends SUCCEEDED with a MERGE.
    assert report["declined"] == [{"task_id": CHECK, "code": "requirements_check_runs"}]
    assert db.docs[f"tasks/{CHECK}"]["state"] == "PARKED"


def test_the_check_is_not_run_when_the_review_runs_and_writes_no_verdict(db, client):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=400))
    _check(db)
    report = _decide(client)
    doc = db.docs[f"tasks/{CHECK}"]
    assert report["reviews_skipped"] == [CHECK]
    assert doc["state"] == "SUCCEEDED"
    assert doc["result_summary"]["requirements_check"]["needed"] is False
    # Never a verdict: no artifact, no `review_skipped`.
    assert "artifacts" not in doc["result_summary"]
    assert "review_skipped" not in doc["result_summary"]


def _skipped_with_check(db, objects, client, requirements: Any) -> str:
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12))
    check = _finished(db, objects, CHECK, files={"requirements.json": json.dumps(requirements)})
    check["metadata"] = {"review_gate": {"default": True, "requirements_check": True}}
    review = _review(db, gate={"default": True})
    review["depends_on"] = [IMPL, CHECK]
    assert _decide(client)["reviews_skipped"] == [REVIEW]
    (entry,) = _review_doc(db)["result_summary"]["artifacts"]
    return reviewgate.inline_verdict(_review_doc(db)["result_summary"], entry)


def test_an_all_met_check_lets_the_skipped_review_close_the_issue(db, objects, client):
    text = _skipped_with_check(db, objects, client, {"requirements": [
        {"index": 1, "met": True, "note": "widgets.py:12 sorts by name"},
        {"index": 2, "met": True, "note": "header.tsx:40 toggles"},
    ]})
    # The check's own change is not counted: 12 lines, skipped.
    assert json.loads(text)["verdict"] == "MERGE"
    # MUTATION: drop the requirements from `skip_summary` and this is part of #N.
    assert issueruns.requirements_finding(PLAN, text) == (True, [], None)


def test_a_not_met_check_leaves_part_of(db, objects, client):
    text = _skipped_with_check(db, objects, client, {"requirements": [
        {"index": 1, "met": True, "note": "widgets.py:12"},
        {"index": 2, "met": False, "note": "no toggle in header.tsx"},
    ]})
    met, unmet, _ = issueruns.requirements_finding(PLAN, text)
    assert met is False
    assert unmet == ["The header toggles the order (no toggle in header.tsx)"]


def test_the_check_never_writes_a_verdict(db, objects, client):
    text = _skipped_with_check(db, objects, client, {
        "verdict": "NOT_YET", "findings": ["x"],
        "requirements": [{"index": 1, "met": True, "note": "a"},
                         {"index": 2, "met": True, "note": "b"}]})
    verdict = json.loads(text)
    # Only the list is read from it: the verdict stays the gate's MERGE.
    assert verdict["verdict"] == "MERGE" and verdict["findings"] == []
    assert reviewgate.requirements_from(json.dumps({"verdict": "MERGE"})) is None


def test_a_long_list_still_fits_the_inline_bound():
    entries = [{"index": n, "met": True, "note": "n" * 400} for n in range(1, 11)]
    decision = reviewgate.Decision(False, reviewgate.REASON_SMALL, changed_lines=1)
    (entry,) = reviewgate.skip_summary(decision, entries)["artifacts"]
    assert entry["bytes"] <= reviewgate.MAX_INLINE_BYTES
    assert len(json.loads(entry["inline"])["requirements"]) == 10


# ---------------------------------------------------------------------------
# the compiled issue run
# ---------------------------------------------------------------------------


def _run(plan: dict[str, Any]) -> issueruns.IssueRun:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return issueruns.IssueRun(
        id="run_x", tenant_id="eng", created_by="alice@example.com", created_at=now,
        updated_at=now, state=issueruns.RunState.APPROVED,
        issue=parse_issue_ref("example-org/widgets#42"), plan_approval="required",
        auto_merge=False, fix_rounds=3, planner_task_id="task_p", plan=plan,
        plan_digest=plan_digest(plan))


def test_the_compiled_issue_run_carries_a_cheap_read_only_check():
    spec = compile_plan(_run(PLAN), class_models={"sonnet": "a-sonnet-model"})
    by_id = {s.step_id: s for s in spec.steps}
    check = by_id["requirements-check"]
    assert check.runner_profile == "claude-code"  # no browser
    assert check.model_class == "sonnet"
    assert check.timeout_seconds == issueruns.REQUIREMENTS_CHECK_TIMEOUT_SECONDS
    assert check.metadata == {"review_gate": {"requirements_check": True}}
    assert check.depends_on == ["impl-sort"] and check.builds_on == "impl-sort"
    prompt = check.input["prompt"]
    assert "requirements.json" in prompt and "verdict.json" not in prompt
    assert "1. Widgets sort by name" in prompt
    # The review waits for it, names no gate of its own (the default applies),
    # and nothing reads a verdict from the check.
    assert by_id["review"].depends_on == ["impl-sort", "requirements-check"]
    assert "review_gate" not in (by_id["review"].metadata or {})
    assert all((s.when is None or s.when.step != "requirements-check")
               and "requirements-check" not in (s.input_from or {}) for s in spec.steps)


def test_the_check_names_the_cheap_class_only_where_it_is_pinned():
    """A class with no pin is refused at submission, which would fail the run."""
    check = {s.step_id: s for s in compile_plan(_run(PLAN)).steps}["requirements-check"]
    assert check.model_class is None
    pinned = compile_plan(_run(PLAN), class_models={"opus": "an-opus-model"})
    assert {s.step_id: s for s in pinned.steps}["requirements-check"].model_class is None


def test_a_plan_with_no_requirements_compiles_no_check():
    plan = {k: v for k, v in PLAN.items() if k != "requirements"}
    assert "requirements-check" not in {s.step_id for s in compile_plan(_run(plan)).steps}
