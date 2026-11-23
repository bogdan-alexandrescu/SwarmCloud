"""The plan lint (`swarm_api.planlint`): checks a parsed plan can be checked.

Improvement plan 2026-11-23, 'Plans that can be checked', item 1. Each rule
is shown passing and failing; the lint is pure, so the same plan always
gives the same findings in the same order.
"""

from __future__ import annotations

import copy

import pytest

from swarm_api import issueruns, planlint


def _step(step_id: str, *, files=("src/a.py",), tests=("tests/test_a.py",), requirements=(1,),
          depends_on=None) -> dict:
    step = {"step_id": step_id, "title": f"Step {step_id}", "prompt": f"Do {step_id}."}
    if files is not None:
        step["files"] = list(files)
    if tests is not None:
        step["tests"] = list(tests)
    if requirements is not None:
        step["requirements"] = list(requirements)
    if depends_on is not None:
        step["depends_on"] = list(depends_on)
    return step


GOOD = {
    "summary": "Sort the widget list by name.",
    "requirements": ["The list sorts by name.", "The header toggles the order."],
    "steps": [
        _step("sort-key", files=["src/widgets/sort.py"], tests=["tests/test_sort.py"],
              requirements=[1]),
        _step("header", files=["src/widgets/header.py"], tests=["tests/test_header.py"],
              requirements=[2]),
    ],
}


def _codes(plan) -> list[str]:
    return [finding["code"] for finding in planlint.lint(plan)]


def test_a_plan_that_meets_every_rule_has_no_findings():
    assert planlint.lint(GOOD) == []
    # And it is a plan the schema accepts, step requirements included.
    assert issueruns.parse_plan(GOOD)["steps"][0]["requirements"] == [1]


def test_every_finding_has_a_code_what_it_names_and_one_sentence():
    plan = {**GOOD, "requirements": []}
    for finding in planlint.lint(plan):
        assert set(finding) == {"code", "step", "requirement", "message"}
        assert finding["code"] in planlint.CODES
        assert finding["message"].endswith(".") and "\n" not in finding["message"]


# -- rule 1: at least one requirement -------------------------------------

@pytest.mark.parametrize("requirements", [None, []])
def test_a_plan_with_no_requirement_is_a_finding(requirements):
    plan = copy.deepcopy(GOOD)
    plan.pop("requirements")
    if requirements is not None:
        plan["requirements"] = requirements
    findings = planlint.lint(plan)
    assert findings[0] == {
        "code": planlint.REQUIREMENTS_MISSING, "step": None, "requirement": None,
        "message": findings[0]["message"],
    }
    # No requirement, nothing to cover: the coverage rule says nothing more.
    assert planlint.REQUIREMENT_UNCOVERED not in _codes(plan)


# -- rule 2: files and tests on every step of a plan of several -----------

def test_a_step_of_several_that_names_no_files_is_a_finding():
    plan = copy.deepcopy(GOOD)
    del plan["steps"][1]["files"]
    findings = planlint.lint(plan)
    assert [(f["code"], f["step"]) for f in findings] == [(planlint.STEP_FILES_MISSING, "header")]


def test_a_step_of_several_that_names_no_tests_is_a_finding():
    plan = copy.deepcopy(GOOD)
    plan["steps"][0]["tests"] = []
    findings = planlint.lint(plan)
    assert [(f["code"], f["step"]) for f in findings] == [(planlint.STEP_TESTS_MISSING, "sort-key")]


def test_a_single_step_plan_needs_neither_files_nor_tests():
    plan = {"summary": "One change.", "requirements": ["It works."],
            "steps": [_step("only", files=None, tests=None, requirements=None)]}
    assert planlint.lint(plan) == []


# -- rule 3: every requirement covered by a step ---------------------------

def test_a_requirement_no_step_names_is_a_finding():
    plan = copy.deepcopy(GOOD)
    plan["steps"][1]["requirements"] = [1]
    findings = planlint.lint(plan)
    assert [(f["code"], f["requirement"]) for f in findings] == [
        (planlint.REQUIREMENT_UNCOVERED, 2)]


def test_a_step_that_declares_no_requirements_covers_none():
    plan = copy.deepcopy(GOOD)
    del plan["steps"][0]["requirements"]
    del plan["steps"][1]["requirements"]
    assert [f["requirement"] for f in planlint.lint(plan)] == [1, 2]


def test_one_step_may_cover_several_requirements():
    plan = copy.deepcopy(GOOD)
    plan["steps"][0]["requirements"] = [1, 2]
    plan["steps"][1]["requirements"] = []
    assert planlint.lint(plan) == []


def test_the_one_step_of_a_single_step_plan_covers_every_requirement():
    plan = {"summary": "One change.", "requirements": ["A.", "B."],
            "steps": [_step("only", requirements=None)]}
    assert planlint.lint(plan) == []


# -- rule 4: no shared file off one dependency line ------------------------

def test_two_parallel_steps_naming_one_file_is_a_finding():
    plan = copy.deepcopy(GOOD)
    plan["steps"][0]["depends_on"] = []
    plan["steps"][1]["depends_on"] = []
    plan["steps"][1]["files"] = ["src/widgets/sort.py"]
    findings = planlint.lint(plan)
    assert [(f["code"], f["step"]) for f in findings] == [(planlint.FILE_SHARED, "sort-key")]
    assert "header" in findings[0]["message"] and "src/widgets/sort.py" in findings[0]["message"]


def test_steps_on_one_dependency_line_may_share_a_file():
    plan = copy.deepcopy(GOOD)
    plan["steps"][1]["files"] = ["src/widgets/sort.py"]
    # The chain: no depends_on anywhere, every step after the one before.
    assert planlint.lint(plan) == []
    plan["steps"][0]["depends_on"] = []
    plan["steps"][1]["depends_on"] = ["sort-key"]
    assert planlint.lint(plan) == []


# -- determinism ------------------------------------------------------------

def test_the_same_plan_gives_the_same_findings_in_the_same_order():
    plan = {"summary": "Messy.", "steps": [
        _step("a", files=None, tests=["t"], requirements=None, depends_on=[]),
        _step("b", files=["x.py"], tests=None, requirements=None, depends_on=[]),
        _step("c", files=["x.py"], tests=None, requirements=None, depends_on=[]),
    ]}
    first = planlint.lint(plan)
    assert first == planlint.lint(copy.deepcopy(plan))
    assert [f["code"] for f in first] == [
        planlint.REQUIREMENTS_MISSING,
        planlint.STEP_FILES_MISSING,
        planlint.STEP_TESTS_MISSING,
        planlint.STEP_TESTS_MISSING,
        planlint.FILE_SHARED,
    ]
    assert [f["step"] for f in first] == [None, "a", "b", "c", "b"]


def test_the_lint_does_not_change_the_plan():
    plan = copy.deepcopy(GOOD)
    plan["requirements"] = []
    before = copy.deepcopy(plan)
    planlint.lint(plan)
    assert plan == before


def test_a_parse_failure_reads_as_a_finding():
    refused = issueruns.InvalidPlan("plan.json is not JSON")
    assert planlint.parse_findings(refused) == [{
        "code": planlint.PLAN_INVALID, "step": None, "requirement": None,
        "message": "plan.json is not JSON.",
    }]
