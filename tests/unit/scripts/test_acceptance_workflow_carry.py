"""accept.yml's `workflow: carry` check: #166's carry, measured live on dev.

WHY THIS EXISTS. #166 (a file written to ./artifacts before a quota park never
reached the attempt that succeeded) was fixed on main by #689 and #712, and
contract request 56 gave the mock `artifact_before_park` so a live run could
show it. The issue stayed open because nothing had proved it live: the dev 403
measurement that reopened it needs an opposite live measurement to close it,
and the owner asked for exactly that on 2026-10-08. docs/workflows.md holds the
recipe; this file holds the acceptance group to running it and to asserting
what it is for -- `carried_from` on the finishing attempt's artifact, and the
child staging the parked attempt's object -- rather than merely that a
two-step workflow succeeded. Only `artifact_before_park` makes the finishing
attempt write nothing, so a recipe without it would pass on a rewrite.

Read as text, offline: the group itself needs a deployment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"
ACCEPTANCE_DOC = ROOT / "docs" / "acceptance.md"


def _function(text: str, name: str) -> str:
    """The body of shell function NAME: from its `name() {` to the `}` that
    closes it at column 0."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}\n", text, re.S | re.M)
    assert match, f"{name}() is not defined in {WORKFLOW.relative_to(ROOT)}"
    return match.group(1)


def _carry_submission(run_workflow: str) -> str:
    """The `acc_workflow carry ...` call, up to the `|| carry=""` that ends it."""
    match = re.search(r'acc_workflow carry "(.*?)\|\| carry=""', run_workflow, re.S)
    assert match, 'run_workflow submits no `acc_workflow carry ... || carry=""`'
    return match.group(1)


def _object(source: str, start: str) -> str:
    """The balanced `{...}` in SOURCE that begins at the first match of START."""
    begin = source.find(start)
    assert begin >= 0, f"{start!r} not found"
    depth = 0
    for index in range(begin, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[begin : index + 1]
    raise AssertionError(f"{start!r} is never closed")


def test_workflow_checks_lists_the_carry_check():
    checks = _function(WORKFLOW.read_text(), "workflow_checks")
    assert any(line.startswith("workflow: carry") for line in checks.splitlines()), checks


def test_run_workflow_submits_the_documented_park_and_carry_recipe():
    text = WORKFLOW.read_text()
    run = _function(text, "run_workflow")
    assert 'ACC_CHECK="workflow: carry"' in run
    submission = _carry_submission(run)
    # The same envelope as every other workflow this group submits.
    assert "priority: 10" in submission and "metadata: $m" in submission
    assert "acc_metadata" in submission and "ACC_RUN_ID" in submission

    step_a = _object(submission, '{step_id: "a"')
    assert 'runner_profile: "mock"' in step_a
    assert re.search(r"\bquota_exhausted: true\b", step_a), step_a
    assert re.search(r"\bartifact_before_park: true\b", step_a), step_a
    # Over the worker's in-place retry ceiling (45 s), so it parks.
    retry = re.search(r"\bretry_after_seconds: (\d+)\b", step_a)
    assert retry and int(retry.group(1)) > 45, step_a
    assert "artifact_text:" in step_a
    name = re.search(r'artifact_name: (\$\w+|"[^"]*")', step_a)
    assert name, step_a

    step_b = _object(submission, '{step_id: "b"')
    assert 'runner_profile: "mock"' in step_b
    assert 'depends_on: ["a"]' in step_b
    staged = re.search(r'input_from: \{a: (\$\w+|"[^"]*")\}', step_b)
    assert staged, step_b
    # Both name the same file, and that file is notes.md, as the recipe has it.
    assert staged.group(1) == name.group(1)
    if name.group(1).startswith("$"):
        bound = re.search(rf'--arg {re.escape(name.group(1)[1:])} "([^"]*)"', submission)
        assert bound, f"{name.group(1)} is not bound by --arg in the carry submission"
        value = bound.group(1)
        variable = re.fullmatch(r"\$\{(\w+)\}", value)
        if variable:
            assigned = re.search(rf'^{variable.group(1)}="([^"]*)"$', text, re.M)
            assert assigned, f"{variable.group(1)} is never assigned"
            value = assigned.group(1)
    else:
        value = json.loads(name.group(1))
    assert value == "notes.md"


def test_carry_check_asserts_carried_from_and_child_staging():
    text = WORKFLOW.read_text()
    check = _function(text, "_wf_check_carry")
    assert re.search(r"^\s*_wf_check_carry ", _function(text, "run_workflow"), re.M)
    assert 'acc_check "workflow: carry' in check
    for needle in (
        "carried_from",
        "staged_inputs",
        "workflow_step_task",
        "acc_run_to_end",
        "attempt_count",
        "acc_events",
        "/attempts/",
        "SUCCEEDED SUCCEEDED",
    ):
        assert needle in check, f"_wf_check_carry does not reference {needle}"
    # The parked attempt is read from the park's own event, not guessed.
    assert re.search(r'select\(\.type == "parked"', check), check


def test_acceptance_doc_has_a_carry_row():
    doc = ACCEPTANCE_DOC.read_text()
    section = doc.split("### workflow\n", 1)[1].split("\n### ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| carry |")]
    assert len(rows) == 1, section
    assert "carried_from" in rows[0]
    assert "#166" in rows[0]
