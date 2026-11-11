"""The acceptance group `workflow`'s fix-loop check (FL-ACCEPT, docs/design/fix-loop.md §8).

The design's row: a workflow whose review says NOT_YET twice and then MERGE
ends SUCCEEDED with `re-review:1`, `review-fix:2` and `re-review:2` appended.
scripts/acceptance/groups/workflow.sh submits it only where the deployment's
catalogue offers `fix-loop` (it ships unavailable until the owner applies
contract request 151) and judges it from the loop's task, the workflow's
steps and each review's verdict.json.

Run here with lib.sh's and testlib.sh's helpers stubbed, so no deployment and
no credential is needed: what is pinned is the workflow the group submits,
the verdicts its reviews return in order, the rounds it expects, and its
judging -- not the platform.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"
CHECK = ("workflow: fix-loop: a review that says NOT_YET twice and then MERGE ends the loop "
         "SUCCEEDED with re-review:1, review-fix:2 and re-review:2 appended")

_HARNESS = r'''
set -euo pipefail
source "$GROUP"
ACC_WORK="$WORK" ACC_RUN_ID=run-1 ACC_REPOSITORY_URL=https://example.invalid/sandbox.git ACC_REF=main
ACC_TENANT=t-acc SWARM_API_TENANT=t-acc API_STATUS=""
acc_metadata() { printf '{"source":"acceptance","acceptance_run":"run-1"}'; }
t_info() { :; }
api_fetch() {
  case "$1" in
    /runtimes) [[ -f "$WORK/runtimes.json" ]] || { API_STATUS=503; return 1; }; cp "$WORK/runtimes.json" "$2" ;;
    /workflows/*) cp "$WORK/workflow.json" "$2" ;;
    *) echo "unexpected GET $1" >&2; return 1 ;;
  esac
}
task_doc() { cat "$WORK/tasks/$1.json"; }
task_field() { task_doc "$1" | jq -r "$2"; }
task_state() { task_doc "$1" | jq -r '.state // "MISSING"'; }
workflow_step_task() { jq -r --arg s "$2" '.workflow.steps[] | select(.step_id == $s) | .task_id // empty' <<<"$1"; }
wait_until() { shift 2; "$@"; }
acc_artifact_text() { cat "$WORK/artifacts/$1-$2" 2>/dev/null; }
task_lease_ids() { cat "$WORK/leases/$1" 2>/dev/null || true; }
acc_record_task() { echo "RECORD $1" >>"$WORK/recorded"; }
acc_close_pr() { echo "CLOSE $*" >>"$WORK/closed"; }
acc_check() { ACC_CHECK="$1"; }
acc_pass() { echo "PASS ${ACC_CHECK}: $1"; }
acc_fail() { echo "FAIL ${ACC_CHECK}: $1"; }
acc_skip() { echo "SKIP ${ACC_CHECK}: $1"; }
acc_assert_eq() { if [[ "$1" == "$2" ]]; then acc_pass "$3"; else acc_fail "$3: want '$1', got '$2'"; fi; }
eval "$ACTION"
'''


def _bash(tmp_path: Path, action: str) -> str:
    done = subprocess.run(
        ["bash", "-c", _HARNESS], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GROUP": str(GROUP), "WORK": str(tmp_path),
             "ACTION": action},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _var(name: str) -> str:
    match = re.search(rf'^{name}="?([^"\n]*)"?$', GROUP.read_text(), re.M)
    assert match, f"{name} is not assigned in {GROUP.relative_to(ROOT)}"
    return match.group(1)


def _function(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}\n", GROUP.read_text(), re.S | re.M)
    assert match, f"{name}() is not defined in {GROUP.relative_to(ROOT)}"
    return match.group(1)


def _body(tmp_path: Path) -> dict[str, Any]:
    return json.loads(_bash(tmp_path, "_wf_fix_loop_body"))


def _steps(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["step_id"]: step for step in body["steps"]}


# --------------------------------------------------------------------------
# The workflow it submits
# --------------------------------------------------------------------------

def test_the_workflow_is_review_then_the_gated_integrator_then_the_loop(tmp_path):
    body = _body(tmp_path)
    assert body["strategy"] == "integrate"
    assert body["repository_url"] == "https://example.invalid/sandbox.git"
    assert body["repository_ref"] == "main"
    assert body["metadata"]["source"] == "acceptance"
    steps = _steps(body)
    assert list(steps) == ["review", "fix", "loop"]

    review = steps["review"]
    # The design's mock-profile review: its verdict is the artifact it is told to write.
    assert review["runner_profile"] == "mock"
    assert review["input"]["artifact_name"] == "verdict.json"

    fix = steps["fix"]
    assert fix["runner_profile"] == "claude-code"
    assert fix["depends_on"] == ["review"]
    assert fix["input_from"] == {"review": "verdict.json"}
    assert fix["when"] == {"step": "review", "verdict_in": ["NOT_YET"]}

    loop = steps["loop"]
    assert loop["runner_profile"] == "fix-loop"
    assert loop["depends_on"] == ["fix"]
    block = loop["fix_loop"]
    assert (block["review"], block["fix"]) == ("review", "fix")
    # Three, not two: the loop must stop on re-review:2's MERGE, not because it
    # ran out of rounds.
    assert block["max_rounds"] == int(_var("WF_FIX_LOOP_MAX_ROUNDS")) == 3
    # Invariant 10: a round is a profile by name and a prompt, nothing else.
    for name in ("fix_round", "re_review"):
        assert set(block[name]) == {"runner_profile", "prompt"}, block[name]
        assert block[name]["runner_profile"] == "claude-code"
    # Nothing a caller may not send: no image, command or resources anywhere.
    text = json.dumps(body)
    for forbidden in ('"image"', '"command"', '"resources"', '"backend"', '"secrets"'):
        assert forbidden not in text


def test_the_workflow_passes_swarm_apis_own_fix_loop_rules(tmp_path, monkeypatch):
    """The body is what swarm-api accepts once the owner makes fix-loop
    available: the real `resolve_fix_loop`, with the profile turned on in this
    process only (the frozen catalogue is not edited)."""
    from swarm_common.profiles import RUNNER_PROFILES

    from swarm_api import validation
    from swarm_api.schemas import WorkflowCreate

    monkeypatch.setitem(RUNNER_PROFILES, "fix-loop",
                        replace(RUNNER_PROFILES["fix-loop"], available=True, disabled_reason=""))
    spec = WorkflowCreate.model_validate(_body(tmp_path))
    plan = validation.resolve_fix_loop(spec.steps, strategy=spec.strategy)
    assert plan is not None
    assert (plan.step_id, plan.review, plan.fix, plan.max_rounds) == ("loop", "review", "fix", 3)
    assert set(plan.overrides) == {"fix_round", "re_review"}


# --------------------------------------------------------------------------
# The verdicts its reviews return, and the rounds that follow from them
# --------------------------------------------------------------------------

def _judge(prompt: str, lines: int) -> str:
    """What a re-review following PROMPT writes for a file of LINES lines."""
    threshold = re.search(r"If it is (\d+) or more, write \{\"verdict\": \"MERGE\"", prompt)
    assert threshold, prompt
    return "MERGE" if lines >= int(threshold.group(1)) else "NOT_YET"


def test_the_mock_review_says_not_yet_with_a_finding_the_fix_can_act_on(tmp_path):
    verdict = json.loads(_steps(_body(tmp_path))["review"]["input"]["artifact_text"])
    assert verdict["verdict"] == "NOT_YET"
    # A NOT_YET with no finding is skipped by the no-op precheck
    # (verdictpublish.noop_verdict_reason), and the fix would never run.
    assert [f["severity"] for f in verdict["findings"]] == ["major"]
    assert _var("WF_FIX_LOOP_FILE") in verdict["findings"][0]["summary"]


def test_the_reviews_say_not_yet_twice_then_merge_and_the_loop_appends_three_rounds(tmp_path):
    """Played through the design's §4.2/§4.4 table under `integrate`: fix 1 is
    the declared fix, then re-review:k; on NOT_YET with rounds left,
    review-fix:k+1; on MERGE, the end."""
    steps = _steps(_body(tmp_path))
    block = steps["loop"]["fix_loop"]
    name = _var("WF_FIX_LOOP_FILE")
    for prompt in (steps["fix"]["input"]["prompt"], block["fix_round"]["prompt"]):
        assert prompt.startswith(f"Append one line reading fix, followed by a newline, to {name}")
    assert f"grep -c . {name}" in block["re_review"]["prompt"]

    verdicts = [json.loads(steps["review"]["input"]["artifact_text"])["verdict"]]
    rounds: list[str] = []
    lines = 1                                    # the declared fix appended one
    k = 1
    while True:
        rounds.append(f"re-review:{k}")
        verdicts.append(_judge(block["re_review"]["prompt"], lines))
        if verdicts[-1] == "MERGE" or k == block["max_rounds"]:
            break
        k += 1
        rounds.append(f"review-fix:{k}")
        lines += 1                               # review-fix:k appends one
    assert verdicts == _var("WF_FIX_LOOP_VERDICTS").split() == ["NOT_YET", "NOT_YET", "MERGE"]
    assert rounds == _var("WF_FIX_LOOP_ROUNDS").split() == ["re-review:1", "review-fix:2", "re-review:2"]
    assert k < block["max_rounds"], "the loop must end on MERGE, with a round to spare"


# --------------------------------------------------------------------------
# Its judging
# --------------------------------------------------------------------------

def _world(tmp_path: Path, *, available: Any = True, loop: dict[str, Any] | None = None,
           rounds: tuple[str, ...] = ("re-review:1", "review-fix:2", "re-review:2"),
           verdicts: tuple[str, ...] = ("NOT_YET", "MERGE"), loop_leases: str = "") -> str:
    """A finished run, written where the stubs read it; returns the workflow JSON."""
    for sub in ("tasks", "artifacts", "leases"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if available is not None:
        entry = {"available": available,
                 "disabled_reason": "" if available is True else "fix-loop is not available yet"}
        (tmp_path / "runtimes.json").write_text(json.dumps({"runtimes": {"fix-loop": entry}}))
    ids = {"review": "task_review", "fix": "task_fix", "loop": "task_loop"}
    ids.update({step: "task_" + step.replace(":", "_").replace("-", "_") for step in rounds})
    steps = [{"step_id": step, "task_id": task} for step, task in ids.items()]
    workflow = {"workflow": {"workflow_id": "wf-1", "state": "SUCCEEDED", "steps": steps}}
    (tmp_path / "workflow.json").write_text(json.dumps(workflow))

    def task(task_id: str, **doc: Any) -> None:
        (tmp_path / "tasks" / f"{task_id}.json").write_text(json.dumps({"id": task_id, **doc}))

    task("task_review", state="SUCCEEDED")
    task("task_fix", state="SUCCEEDED",
         result_summary={"git": {"pull_request": {"number": 7}, "branch": "swarm/task_fix"}})
    task("task_loop", **(loop if loop is not None else {
        "state": "SUCCEEDED", "attempt_count": 0,
        "result_summary": {"fix_loop": {"verdict": "MERGE", "final_review": "task_re_review_2"}}}))
    for step in rounds:
        task(ids[step], state="SUCCEEDED")
    (tmp_path / "artifacts" / "task_review-verdict.json").write_text(
        _steps(_body(tmp_path))["review"]["input"]["artifact_text"])
    reviews = [step for step in rounds if step.startswith("re-review:")]
    for step, verdict in zip(reviews, verdicts):
        (tmp_path / "artifacts" / f"{ids[step]}-verdict.json").write_text(
            json.dumps({"verdict": verdict, "findings": []}))
    if loop_leases:
        (tmp_path / "leases" / "task_loop").write_text(loop_leases)
    return json.dumps({"workflow": {"workflow_id": "wf-1", "steps": steps[:3]}})


def _judged(tmp_path: Path, wf: str) -> list[str]:
    out = _bash(tmp_path, f"_wf_fix_loop_offered || true; _wf_check_fix_loop '{wf}'")
    return [line for line in out.splitlines() if line.startswith(("PASS", "FAIL", "SKIP"))]


def test_a_loop_that_ends_on_the_second_re_reviews_merge_passes_every_assertion(tmp_path):
    verdicts = _judged(tmp_path, _world(tmp_path))
    assert verdicts and all(v.startswith(f"PASS {CHECK}") for v in verdicts), verdicts
    assert len(verdicts) == 8, verdicts
    # The rounds' tasks are cancelled at exit if they still run; the pull request is closed.
    recorded = (tmp_path / "recorded").read_text().split()
    assert [r for r in recorded if r != "RECORD"] == [
        "task_re_review_1", "task_review_fix_2", "task_re_review_2"]
    assert (tmp_path / "closed").read_text().strip() == "CLOSE 7 swarm/task_fix"


def test_a_loop_parked_for_a_person_fails_naming_the_phase(tmp_path):
    loop = {"state": "PARKED", "park_reason": "HUMAN_REQUIRED", "attempt_count": 0,
            "blocked_by": [{"park_phase": "review_unreadable", "fix_loop": {"round": 1}}]}
    verdicts = _judged(tmp_path, _world(tmp_path, loop=loop, rounds=("re-review:1",), verdicts=()))
    fails = [v for v in verdicts if v.startswith("FAIL")]
    assert fails and "review_unreadable" in fails[0], verdicts
    assert (tmp_path / "closed").read_text().strip() == "CLOSE 7 swarm/task_fix"


def test_a_loop_that_ran_past_the_merge_fails_on_its_rounds(tmp_path):
    rounds = ("re-review:1", "review-fix:2", "re-review:2", "review-fix:3", "re-review:3")
    verdicts = _judged(tmp_path, _world(tmp_path, rounds=rounds, verdicts=("NOT_YET", "MERGE", "MERGE")))
    assert any(v.startswith("FAIL") and "round steps appended" in v for v in verdicts), verdicts


def test_a_review_that_said_merge_early_fails_on_the_verdicts(tmp_path):
    verdicts = _judged(tmp_path, _world(tmp_path, verdicts=("MERGE", "MERGE")))
    fail = [v for v in verdicts if v.startswith("FAIL")]
    assert len(fail) == 1 and "want 'NOT_YET NOT_YET MERGE', got 'NOT_YET MERGE MERGE'" in fail[0], verdicts


def test_a_loop_that_held_a_lease_fails(tmp_path):
    verdicts = _judged(tmp_path, _world(tmp_path, loop_leases="lease_1\n"))
    assert any(v.startswith("FAIL") and "lease ids" in v for v in verdicts), verdicts


def test_a_deployment_that_ships_fix_loop_unavailable_is_a_skip_and_submits_nothing(tmp_path):
    _world(tmp_path, available=False)
    out = _bash(tmp_path, '''
      fixloop=""
      if _wf_fix_loop_offered; then fixloop=submitted; fi
      echo "SUBMITTED=${fixloop:-no}"
      _wf_check_fix_loop "${fixloop}"''')
    assert "SUBMITTED=no" in out
    lines = [line for line in out.splitlines() if line.startswith(("PASS", "FAIL", "SKIP"))]
    assert len(lines) == 1 and lines[0].startswith(f"SKIP {CHECK}: not measured"), lines
    assert "fix-loop is not available yet" in lines[0] and "contract request 151" in lines[0]
    assert "once FL-ISSUE, FL-UI and FL-DOCS are on main" in lines[0], lines[0]


def test_an_unreadable_catalogue_is_a_failure_never_a_skip(tmp_path):
    _world(tmp_path, available=None)
    out = _bash(tmp_path, 'fixloop=""; _wf_fix_loop_offered || true; _wf_check_fix_loop "${fixloop}"')
    lines = [line for line in out.splitlines() if line.startswith(("PASS", "FAIL", "SKIP"))]
    assert lines == [f"FAIL {CHECK}: not submitted: GET /runtimes answered HTTP 503, so whether "
                     "this deployment offers fix-loop is not known"], lines


@pytest.mark.parametrize("available", ["true", 1])
def test_only_a_boolean_true_offers_the_profile(tmp_path, available):
    _world(tmp_path, available=available)
    assert "OFFERED=no" in _bash(tmp_path, 'if _wf_fix_loop_offered; then echo OFFERED=yes; else echo OFFERED=no; fi')


# --------------------------------------------------------------------------
# Its wiring
# --------------------------------------------------------------------------

def test_the_group_lists_the_check_and_submits_only_what_the_catalogue_offers():
    assert CHECK in _function("workflow_checks").splitlines()
    run = _function("run_workflow")
    offered = run.index("if _wf_fix_loop_offered; then")
    submitted = run.index('acc_workflow fixloop "$(_wf_fix_loop_body)"')
    judged = run.index('_wf_check_fix_loop "${fixloop}"')
    # Submitted first (the longest wait), only where offered, judged last.
    assert offered < submitted < run.index("acc_workflow stage") < judged
    assert run.rstrip().splitlines()[-1].strip() == '_wf_check_fix_loop "${fixloop}"'
