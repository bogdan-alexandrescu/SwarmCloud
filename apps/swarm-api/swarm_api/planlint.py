"""The plan lint: whether a parsed plan can be checked (improvement plan
2026-11-23, 'Plans that can be checked', item 1).

    lint(plan) -> [{"code", "step", "requirement", "message"}, ...]

`parse_plan` decides whether a plan is well formed; this decides whether a
person, the review and the requirements check have something to hold it to.
Each finding has a stable `code` (the console and the plugin key on it), the
step id or the 1-based requirement number it names (None when it names the
plan), and one plain sentence.

    requirements_missing   the plan states no requirement
    step_files_missing     a step of a plan of several names no files it edits
    step_tests_missing     a step of a plan of several names no tests that prove it
    requirement_uncovered  no step names the requirement in its `requirements`
    file_shared            two steps not on one dependency line name one file
    plan_invalid           the planner's output did not parse (`parse_findings`)

A step links to the requirements it delivers by `PlanStep.requirements`, their
numbers 1 up, as `validation.tests[].requirements` numbers them. The one step
of a single-step plan covers every requirement: there is no other step that
could. `file_shared` is `issueruns.parallel_file_conflicts`, the rule
`parse_plan` already refuses a new plan for, so a plan stored before that rule
or edited round it is still named.

PURE: no model call, no network, no clock and no randomness, and the plan is
not changed. The same plan always gives the same findings in the same order:
the plan's own findings, then each step's in plan order, then each
requirement's in number order, then each shared file in plan order.
"""

from __future__ import annotations

from typing import Any, Mapping

from .issueruns import InvalidPlan, parallel_file_conflicts
from .redaction import redact_detail

REQUIREMENTS_MISSING = "requirements_missing"
STEP_FILES_MISSING = "step_files_missing"
STEP_TESTS_MISSING = "step_tests_missing"
REQUIREMENT_UNCOVERED = "requirement_uncovered"
FILE_SHARED = "file_shared"
PLAN_INVALID = "plan_invalid"

CODES: tuple[str, ...] = (
    REQUIREMENTS_MISSING, STEP_FILES_MISSING, STEP_TESTS_MISSING, REQUIREMENT_UNCOVERED,
    FILE_SHARED, PLAN_INVALID,
)

#: The most findings one parse failure is read as, and the longest sentence:
#: the schema's errors quote the planner's text, which a run stores and serves.
MAX_PARSE_FINDINGS = 20
MAX_FINDING_CHARS = 500


def _finding(code: str, message: str, *, step: str | None = None,
             requirement: int | None = None) -> dict[str, Any]:
    return {"code": code, "step": step, "requirement": requirement, "message": message}


def _listed(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def lint(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The findings on `plan`, a plan as `parse_plan` returns it; [] when it passes."""
    steps = [step for step in _listed(plan.get("steps")) if isinstance(step, Mapping)]
    requirements = _listed(plan.get("requirements"))
    findings: list[dict[str, Any]] = []
    if not requirements:
        findings.append(_finding(
            REQUIREMENTS_MISSING,
            "The plan states no requirement, so nothing says when its work is done."))
    several = len(steps) > 1
    covered: set[int] = set()
    for step in steps:
        sid = str(step.get("step_id") or "")
        if several and not _listed(step.get("files")):
            findings.append(_finding(
                STEP_FILES_MISSING, f"Step {sid!r} names no files it will edit.", step=sid))
        if several and not _listed(step.get("tests")):
            findings.append(_finding(
                STEP_TESTS_MISSING, f"Step {sid!r} names no tests that prove it.", step=sid))
        covered.update(n for n in _listed(step.get("requirements"))
                       if isinstance(n, int) and not isinstance(n, bool))
    if not several:
        covered.update(range(1, len(requirements) + 1))
    for number in range(1, len(requirements) + 1):
        if number not in covered:
            findings.append(_finding(
                REQUIREMENT_UNCOVERED,
                f"Requirement {number} is covered by no step's requirements.",
                requirement=number))
    for one, other, path in parallel_file_conflicts(steps):
        findings.append(_finding(
            FILE_SHARED,
            f"Steps {one!r} and {other!r} both name {path!r} and are not on one dependency line.",
            step=one))
    return findings


def _sentence(text: str) -> str:
    text = " ".join(redact_detail(str(text), limit=MAX_FINDING_CHARS).split())
    if len(text) > MAX_FINDING_CHARS - 1:
        text = text[:MAX_FINDING_CHARS - 2].rstrip() + "…"
    return text if text.endswith(".") else text + "."


def parse_findings(refused: InvalidPlan) -> list[dict[str, Any]]:
    """A planner output that did not parse, as findings: one per schema error
    the refusal lists, else one for its message. Masked and bounded, because
    an error may quote what the planner wrote."""
    errors = (refused.detail or {}).get("errors") if isinstance(refused.detail, Mapping) else None
    texts = [e for e in _listed(errors) if isinstance(e, str) and e.strip()] or [refused.message]
    return [_finding(PLAN_INVALID, _sentence(text)) for text in texts[:MAX_PARSE_FINDINGS]]
