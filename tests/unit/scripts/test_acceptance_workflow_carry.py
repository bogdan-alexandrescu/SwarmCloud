"""accept.yml's `workflow: carry` check: archived issue 166's carry, measured live on dev.

WHY THIS EXISTS. archived issue 166 (a file written to ./artifacts before a quota park never
reached the attempt that succeeded) was fixed on main by archived pull request 689 and archived pull request 712, and
contract request 56 gave the mock `artifact_before_park` so a live run could
show it. The issue stayed open because nothing had proved it live: the dev 403
measurement that reopened it needs an opposite live measurement to close it,
and the owner asked for exactly that on 2026-10-08. docs/workflows.md holds the
recipe; this file holds the acceptance group to running it and to asserting
what it is for -- `carried_from` on the finishing attempt's artifact, and the
child staging the parked attempt's object -- rather than merely that a
two-step workflow succeeded. Only `artifact_before_park` makes the finishing
attempt write nothing, so a recipe without it would pass on a rewrite.

Most of this reads the group as text, offline: the group itself needs a
deployment. The tests at the end run the real `_wf_check_carry` against fakes
(no platform, no network, no credentials) for how it counts the parent's
attempts.

THE FALSE FAILURE (dev acceptance, 2026-11-20). The check read the parent's
`attempt_count` and reported "the parent ran 1 attempt(s); the recipe parks its
first, so it needs at least 2" for a parent that parked and resumed exactly as
designed. A PROVIDER_QUOTA_EXHAUSTED park REFUNDS its attempt
(`agent_worker.control.ControlPlane.park`, the owner 2026-10-27, "Refund,
bounded"), so `attempt_count` went back down and the second lease took it to 1.
The check now counts the parent's attempt documents, which every attempt
writes, refunded or not, and fails when they cannot be read.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
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
        "task_attempts",
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
    assert "the park-and-carry 403 finding" in rows[0]


PARENT = "task_carrya00000000000001"
CHILD = "task_carryb00000000000001"
PARKED = "att_parked"
FINAL = "att_final"
OBJECT = f"/tasks/{PARENT}/attempts/{PARKED}/artifacts/notes.md"
#: The parent the 2026-11-20 run read: refunded back to one after its park.
PARENT_DOC = {
    "state": "SUCCEEDED",
    "attempt_count": 1,
    "result_summary": {
        "artifacts": [{"name": "notes.md", "carried_from": PARKED, "uri": f"gs://bucket{OBJECT}"}],
    },
}
CHILD_DOC = {
    "state": "SUCCEEDED",
    "result_summary": {
        "staged_inputs": [{"filename": "notes.md", "task_id": PARENT, "uri": f"gs://bucket{OBJECT}"}],
    },
}
EVENTS = [
    {"type": "parked", "attempt_id": PARKED, "at": "2026-11-20T10:01:00Z"},
    {"type": "succeeded", "attempt_id": FINAL, "at": "2026-11-20T10:03:00Z"},
]
ATTEMPTS_LINE = "attempt document"


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("API_", "SWARM_", "GITHUB_", "GH_"))
    }
    env["NO_COLOR"] = "1"
    # The placeholder deployment's sandbox: config.sh has no default for it.
    env["SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"] = os.environ["SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"]
    return env


def _carry_check(tmp_path: Path, *, attempts: list[dict] | None,
                 parent: dict | None = None, events: list[dict] | None = None) -> subprocess.CompletedProcess:
    (tmp_path / f"{PARENT}.json").write_text(json.dumps(PARENT_DOC if parent is None else parent))
    (tmp_path / f"{CHILD}.json").write_text(json.dumps(CHILD_DOC))
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (EVENTS if events is None else events)))
    (tmp_path / "work").mkdir()
    if attempts is None:
        fake_attempts = "task_attempts() { return 1; }"
    else:
        (tmp_path / "attempts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in attempts))
        fake_attempts = f"task_attempts() {{ cat {shlex.quote(str(tmp_path / 'attempts.jsonl'))}; }}"
    docs = shlex.quote(str(tmp_path))
    script = f"""set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
die() {{ printf 'DIE %s\\n' "$*" >&2; exit 9; }}
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ :; }}; t_info() {{ :; }}; info() {{ :; }}; step() {{ :; }}
redact() {{ cat; }}
source {shlex.quote(str(LIB))}
source {shlex.quote(str(WORKFLOW))}
ACC_WORK={shlex.quote(str(tmp_path / "work"))}
workflow_step_task() {{ case "$2" in a) echo {PARENT} ;; b) echo {CHILD} ;; esac; }}
acc_run_to_end() {{ printf -v "$1" '%s' SUCCEEDED; }}
task_doc() {{ cat {docs}/"$1".json; }}
task_field() {{ task_doc "$1" | jq -r "$2"; }}
task_state() {{ task_field "$1" '.state'; }}
acc_events() {{ cat {docs}/events.jsonl; }}
_wf_check_child_copy() {{ :; }}
{fake_attempts}
_wf_check_carry '{{"workflow":{{}}}}' 'carried notes'
"""
    # stdin is an empty pipe, so a fake that drains it never waits on a tty.
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=_clean_env(),
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _attempt(attempt_id: str) -> dict:
    return {"attempt_id": attempt_id, "task_id": PARENT}


def _verdicts(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith(("PASS", "FAIL", "SKIP"))]


def test_a_refunded_park_then_a_second_attempt_passes(tmp_path):
    # The 2026-11-20 run: attempt_count 1 after the refund, two attempt documents.
    result = _carry_check(tmp_path, attempts=[_attempt(PARKED), _attempt(FINAL)])
    lines = _verdicts(result)
    assert any(line.startswith("PASS") and "2 attempt documents" in line for line in lines), (
        result.stdout + result.stderr
    )
    # Everything else the check asserts still holds against this shape.
    assert lines and not any(line.startswith("FAIL") for line in lines), result.stdout + result.stderr


def test_one_attempt_document_fails(tmp_path):
    result = _carry_check(tmp_path, attempts=[_attempt(FINAL)])
    lines = [line for line in _verdicts(result) if ATTEMPTS_LINE in line]
    assert any(line.startswith("FAIL") and "1 attempt document(s)" in line for line in lines), (
        result.stdout + result.stderr
    )
    assert not any(line.startswith("PASS") for line in lines), result.stdout


def test_an_unreadable_attempts_query_fails_rather_than_counting_nothing(tmp_path):
    result = _carry_check(tmp_path, attempts=None)
    lines = [line for line in _verdicts(result) if ATTEMPTS_LINE in line]
    assert any(line.startswith("FAIL") and "could not read" in line for line in lines), (
        result.stdout + result.stderr
    )
    assert not any(line.startswith("PASS") for line in lines), result.stdout


def test_a_parent_that_failed_names_its_last_error(tmp_path):
    """Issue 202: the 2026-11-23 22:30 run read FAILED CANCELLED with one
    attempt document and no park event, and its log could not say why the
    first attempt failed. The check now prints the parent's `last_error`."""
    error = "quota_exhausted needs the attempt_count the worker writes into the runner's input"
    parent = {"state": "FAILED", "attempt_count": 1, "last_error": error, "result_summary": {}}
    result = _carry_check(tmp_path, attempts=[_attempt(FINAL)], parent=parent, events=[])
    lines = _verdicts(result)
    assert any(line.startswith("FAIL") and "the parent ended FAILED: " + error in line
               for line in lines), result.stdout + result.stderr


def test_a_parent_that_succeeded_prints_no_last_error_line(tmp_path):
    result = _carry_check(tmp_path, attempts=[_attempt(PARKED), _attempt(FINAL)])
    assert not any("the parent ended" in line for line in _verdicts(result)), result.stdout
