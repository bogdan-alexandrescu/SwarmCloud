"""github-verify.sh judges each acceptance pull request by its own check's rules.

On 2026-11-23 the sandbox read-back failed the pull request the workflow
group's review-and-fix loop check opens (it writes the loop's rounds file) on
three assertions that belong to the claude-code direct-pr check: "it changes
files outside tests/acceptance/fixtures/", "its diff does not remove a line
from .../calc.py" and "a direct-pr pull request must change only .../calc.py".
Every pull request that was not git-tools' fell through to the claude-code
rules. These pin, offline, with recorded pull request shapes:

* the loop check's pull request is its own kind (its body's marker, or a
  change to the loop's file alone) and gets only its own assertions: it
  changes the loop's file and nothing else, and its diff only appends lines
  reading `fix`;
* the claude-code direct-pr and integrate pull requests are still judged as
  before, beside it;
* a pull request no check owns fails as UNCLASSIFIED, never with another
  check's message;
* the loop's file lies under tests/acceptance/fixtures/, in the workflow
  group's own directory, so the sweep (github-cleanup.sh, which closes only
  fixture-only pull requests) closes it, and the group and the read-back
  name the same file and marker.

Every fake credential is built at run time (CLAUDE.md).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_acceptance_target import ROOT, _calc_file, _good_pulls, _pr, _verify

VERIFY = ROOT / "scripts" / "acceptance" / "github-verify.sh"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"
FIXTURES = "tests/acceptance/fixtures/"
LOOP_FILE = "tests/acceptance/fixtures/workflow/fix-loop-rounds.txt"
LOOP_MARKER = "acceptance-workflow: fix-loop"
#: The path the loop check wrote on the run of 2026-11-23 (before the move).
RECORDED_ROOT_FILE = "fix-loop-rounds.txt"


def _shell_value(path: Path, name: str) -> str:
    match = re.search(rf'^{name}="([^"\n]*)"$', path.read_text(), re.M)
    assert match, f"{name} is not assigned in {path.relative_to(ROOT)}"
    value = match.group(1)
    for ref in re.findall(r"\$\{([A-Z_]+)\}", value):
        value = value.replace("${" + ref + "}", _shell_value(path, ref))
    return value


def _loop_pr(number: int = 21, *, marker: bool = True) -> dict:
    """The loop's integrator's pull request as the sandbox shows it: the
    agent's title, its pr-body.md and the platform's task line."""
    task = "task_" + "9c" * 8
    body = "acceptance-run:run-1\n" + (f"{LOOP_MARKER}\n" if marker else "") + f"\nOpened by swarm task {task}.\n"
    return _pr(number, task, title="Record the fix loop acceptance run", body=body)


def _loop_file(path: str = LOOP_FILE, patch: str = "@@ -0,0 +1,2 @@\n+fix\n+fix") -> dict:
    return {"filename": path, "patch": patch}


def _lines_for(stderr: str, number: int) -> str:
    return "\n".join(l for l in stderr.splitlines() if f"PR #{number}:" in l)


# ---------------------------------------------------------------------------
# The loop check's pull request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [True, False])
def test_the_loop_check_s_pull_request_passes_by_its_own_rules_beside_claude_code_s(tmp_path, marker):
    pulls, files = _good_pulls()
    pulls.append(_loop_pr(marker=marker))
    files[21] = [_loop_file()]
    result = _verify(tmp_path, pulls, files)
    assert result.returncode == 0, result.stderr
    assert "verified 3 pull request(s): 1 direct-pr, 1 integrate, 0 git-tools, 0 git-tools integrate, 1 fix-loop, 0 unclassified" in result.stderr
    own = _lines_for(result.stderr, 21)
    assert "calc.py" not in own and "direct-pr" not in own, own
    assert f"changes only {LOOP_FILE}" in own, own
    assert "appends 2 line(s) reading fix" in own, own


def test_a_loop_pull_request_from_before_the_first_round_still_passes(tmp_path):
    """The fix step appends one line; a loop that parked before review-fix:2
    leaves a pull request with one. The suite judges the rounds; here only
    what was written is judged."""
    result = _verify(tmp_path, [_loop_pr()], {21: [_loop_file(patch="@@ -0,0 +1 @@\n+fix")]})
    assert result.returncode == 0, result.stderr
    assert "appends 1 line(s) reading fix" in result.stderr


@pytest.mark.parametrize("case", ["extra-fixture-file", "removes-a-line", "writes-another-line", "adds-nothing"])
def test_the_loop_pull_request_fails_on_its_own_defects_with_its_own_messages(tmp_path, case):
    listed = {
        "extra-fixture-file": [_loop_file(), {"filename": f"{FIXTURES}workflow/other.txt", "patch": "+x"}],
        "removes-a-line": [_loop_file(patch="@@ -1,2 +1,2 @@\n-fix\n+fix\n fix")],
        "writes-another-line": [_loop_file(patch="@@ -0,0 +1,2 @@\n+fix\n+MERGE")],
        "adds-nothing": [_loop_file(patch="")],
    }[case]
    result = _verify(tmp_path, [_loop_pr()], {21: listed})
    assert result.returncode != 0, f"{case} passed:\n{result.stderr}"
    own = _lines_for(result.stderr, 21)
    assert "the fix-loop pull request" in own, own
    assert "calc.py" not in own and "direct-pr" not in own, own


def test_the_recorded_2026_11_23_shape_is_never_judged_as_the_direct_pr_check_s(tmp_path):
    """The run that failed: the loop wrote its file at the repository root.
    With its marker it is the loop's pull request and fails the loop's and
    the fixtures rule -- never calc.py's or direct-pr's."""
    result = _verify(tmp_path, [_loop_pr()], {21: [_loop_file(path=RECORDED_ROOT_FILE)]})
    assert result.returncode != 0, result.stderr
    own = _lines_for(result.stderr, 21)
    assert f"it changes files outside {FIXTURES}: {RECORDED_ROOT_FILE}" in own, own
    assert f"the fix-loop pull request must change only {LOOP_FILE}" in own, own
    assert "calc.py" not in own and "direct-pr" not in own, own


# ---------------------------------------------------------------------------
# A pull request no check owns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("listed", [
    [{"filename": f"{FIXTURES}generic/notes.txt", "patch": "+x"}],
    [{"filename": RECORDED_ROOT_FILE, "patch": "+fix"}],
    [],
], ids=["another-fixture", "recorded-loop-shape-without-marker", "no-files"])
def test_an_unknown_pull_request_fails_as_unclassified_never_as_another_check_s(tmp_path, listed):
    task = "task_" + "3d" * 8
    pull = _pr(31, task, title="Tidy the notes", body=f"acceptance-run:run-1\n\nOpened by swarm task {task}.\n")
    result = _verify(tmp_path, [pull], {31: listed})
    assert result.returncode != 0, result.stderr
    own = _lines_for(result.stderr, 31)
    assert "unclassified" in own, own
    for other in ("calc.py", "direct-pr", "integrate", "git-tools", "the fix-loop pull request"):
        assert other not in own, (other, own)
    assert "1 unclassified" in result.stderr


def test_a_claude_code_pull_request_is_still_held_to_calc_py(tmp_path):
    pulls, files = _good_pulls()
    files[7] = [_calc_file("@@ -1 +1 @@\n-x\n+y\n")]
    result = _verify(tmp_path, pulls, files)
    assert result.returncode != 0
    assert "PR #7: its diff does not remove" in result.stderr


# ---------------------------------------------------------------------------
# One file, one marker, under the fixtures
# ---------------------------------------------------------------------------


def test_the_group_and_the_read_back_name_the_same_file_and_marker():
    assert _shell_value(GROUP, "WF_FIX_LOOP_FILE") == LOOP_FILE
    assert _shell_value(VERIFY, "FIX_LOOP_FILE") == LOOP_FILE
    assert _shell_value(GROUP, "WF_FIX_LOOP_MARKER") == LOOP_MARKER
    assert _shell_value(VERIFY, "FIX_LOOP_MARKER") == LOOP_MARKER


def test_the_loop_s_file_lies_in_the_workflow_group_s_own_fixture_directory():
    """github-cleanup.sh closes only pull requests whose every file lies
    under tests/acceptance/fixtures/, and each group writes only its own
    directory there; a file at the root was left open on the sandbox."""
    assert LOOP_FILE.startswith(FIXTURES + "workflow/")
    others = [p.name for p in (ROOT / FIXTURES).iterdir() if p.is_dir() and p.name != "workflow"]
    assert "workflow" not in others and others, others


def test_the_fix_step_writes_the_marker_into_its_pull_request_body():
    text = GROUP.read_text()
    fix = re.search(r'when: \{step: "review", verdict_in: \["NOT_YET"\]\},\n(.*?)\}\},', text, re.S)
    assert fix, "the fix-loop body's fix step is not found"
    assert "$marker" in fix.group(1) and "pr-body.md" in fix.group(1), fix.group(1)
    assert '--arg marker "${WF_FIX_LOOP_MARKER}"' in text
