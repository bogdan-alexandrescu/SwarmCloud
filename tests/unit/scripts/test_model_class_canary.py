"""The model-class canary in the claude-code acceptance group, offline (MODEL-CANARY, part of #1435).

docs/design/model-per-step.md §4.9: acceptance runs one claude-code step per
model class, and for each asserts that the attempt's `model` is the class's
pin from /v1/runtimes and that the runner summary's `usage.models` holds that
pin and no other class's. scripts/acceptance/groups/claude-code.sh does it
against a deployment; these pin, with no deployment:

* the group's classes are the frozen vocabulary, in its order, and it lists
  one check per class;
* `run_claude_code` submits one step per class, naming the class and nothing
  else that chooses a model, and judges each step under its own class;
* the verdict PASSES on the evidence a working platform leaves, and FAILS on
  each defect it exists to catch: an attempt that recorded another pin, a
  stream that ran none of the pin, a stream that ran another class's pin, a
  catalogue that serves no pin, a step that did not succeed.

lib.sh's own code runs; testlib's platform reads are stubbed, as
test_acceptance_helpers.py's _bash_lib does.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from swarm_common.profiles import MODEL_CLASSES

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "claude-code.sh"

PINS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
TASK = "task_" + "5e" * 10


def _runtimes(models: dict[str, str] | None = None) -> str:
    served = PINS if models is None else models
    return json.dumps({"runtimes": {"claude-code": {
        "model_classes": list(MODEL_CLASSES), "default_model_class": "opus", "models": served}}})


def _bash(body: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
ACC_RUN_ID=run1
ACC_CHECK=""
ACC_REPOSITORY_URL=https://github.com/example/sandbox
ACC_REF=main
ACC_ISSUE=1
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ printf 'CASE %s\\n' "$*"; }}
t_info() {{ printf 'INFO %s\\n' "$*"; }}
step() {{ :; }}
redact() {{ cat; }}
source {shlex.quote(str(LIB))}
source {shlex.quote(str(GROUP))}
# lib.sh resets ACC_WORK at source time (acc_init makes the real one).
ACC_WORK={shlex.quote(str(tmp_path))}
{body}
"""
    path = tmp_path / "run.sh"
    path.write_text(script)
    full_env = dict(os.environ, NO_COLOR="1", **env)
    return subprocess.run(["bash", str(path)], capture_output=True, text=True, timeout=60,
                          check=False, env=full_env)


def _judge(tmp_path: Path, cls: str, attempt_model: str, usage_models, runtimes: str | None = None):
    rt = tmp_path / "runtimes.json"
    rt.write_text(_runtimes() if runtimes is None else runtimes)
    usage = usage_models if isinstance(usage_models, str) else json.dumps(usage_models)
    body = (f"_cc_judge_model_class {shlex.quote(cls)} {shlex.quote(str(rt))} "
            f"{shlex.quote(attempt_model)} {shlex.quote(usage)} {TASK}")
    result = _bash(body, tmp_path)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _verdicts(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(("PASS", "FAIL", "SKIP"))]


# ---------------------------------------------------------------------------
# The classes and the checks
# ---------------------------------------------------------------------------


def test_the_canary_runs_every_class_of_the_frozen_vocabulary_in_its_order():
    text = GROUP.read_text()
    m = re.search(r"^CC_MODEL_CLASSES=\(([^)]*)\)", text, re.M)
    assert m, "claude-code.sh declares no CC_MODEL_CLASSES"
    assert tuple(m.group(1).split()) == tuple(MODEL_CLASSES)


def test_the_group_lists_one_check_per_class(tmp_path):
    result = _bash("claude_code_checks", tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for cls in MODEL_CLASSES:
        named = [line for line in lines if f"a {cls} step runs its pin" in line]
        assert len(named) == 1, (cls, lines)
    # The three checks that were there stay, and the test-services pair follows
    # (TS-ACCEPT; tests/unit/scripts/test_acceptance_test_services.py).
    assert len(lines) == 3 + len(MODEL_CLASSES) + 2


# ---------------------------------------------------------------------------
# What run_claude_code submits and judges
# ---------------------------------------------------------------------------

_RUN_STUBS = r"""
N=0
acc_submit() {
  local var="$1" profile="$2" input="$3" extra="$4"
  N=$(( N + 1 ))
  printf 'SUBMIT %s %s %s\n' "${profile}" "$(jq -c '{model_class, max_attempts, model: (.model // null)}' <<<"${extra}")" "$(jq -c '{model: (.model // null)}' <<<"${input}")"
  printf -v "${var}" 'task_%02d' "${N}"
}
acc_door() { printf '422 invalid_dispatch'; }
api_fetch() { printf '%s' "${RUNTIMES}" >"$2"; }
_cc_check_collect() { :; }
_cc_check_direct_pr() { :; }
_cc_check_issue() { :; }
_cc_check_test_services() { :; }
_cc_check_model_class() { printf 'JUDGE %s %s %s\n' "$1" "$2" "$(jq -c '.runtimes["claude-code"].models' "$3")"; }
run_claude_code
"""


def test_run_submits_one_claude_code_step_per_class_and_judges_each_under_its_own_class(tmp_path):
    result = _bash(_RUN_STUBS, tmp_path, RUNTIMES=_runtimes())
    assert result.returncode == 0, result.stderr
    submits = [line.split(" ", 3) for line in result.stdout.splitlines() if line.startswith("SUBMIT")]
    classed = [s for s in submits if json.loads(s[2])["model_class"] is not None]
    assert [json.loads(s[2])["model_class"] for s in classed] == list(MODEL_CLASSES)
    for s in classed:
        assert s[1] == "claude-code"
        extra = json.loads(s[2])
        # The class chooses the model; no ID is sent (invariant 10).
        assert extra["model"] is None and json.loads(s[3])["model"] is None
        assert extra["max_attempts"] == 1
    judged = [line.split(" ", 3) for line in result.stdout.splitlines() if line.startswith("JUDGE")]
    assert [j[1] for j in judged] == list(MODEL_CLASSES)
    # Each class is judged on the task submitted for it, against the catalogue read.
    ids = {json.loads(s[2])["model_class"]: f"task_{i + 1:02d}" for i, s in enumerate(submits)
           if json.loads(s[2])["model_class"] is not None}
    assert {j[1]: j[2] for j in judged} == ids
    assert all(json.loads(j[3]) == PINS for j in judged)


def test_a_class_whose_submission_was_refused_is_judged_not_submitted(tmp_path):
    body = _RUN_STUBS.replace(
        'printf -v "${var}" \'task_%02d\' "${N}"',
        'if [[ "${extra}" == *\'"haiku"\'* ]]; then return 1; fi; printf -v "${var}" \'task_%02d\' "${N}"',
    )
    result = _bash(body, tmp_path, RUNTIMES=_runtimes())
    assert result.returncode == 0, result.stderr
    judged = {line.split(" ")[1]: line.split(" ")[2] for line in result.stdout.splitlines()
              if line.startswith("JUDGE")}
    assert judged["haiku"] == ""
    assert all(judged[c].startswith("task_") for c in ("opus", "fable", "sonnet"))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", list(PINS))
def test_a_working_platform_passes_every_class(tmp_path, cls):
    out = _judge(tmp_path, cls, PINS[cls], [PINS[cls]])
    verdicts = _verdicts(out)
    assert verdicts and all(v.startswith("PASS") for v in verdicts), out
    assert any(PINS[cls] in v for v in verdicts)


def test_a_context_suffix_names_the_same_model(tmp_path):
    out = _judge(tmp_path, "opus", PINS["opus"], [PINS["opus"] + "[1m]"])
    assert all(v.startswith("PASS") for v in _verdicts(out)), out


def test_an_attempt_that_recorded_another_classes_pin_fails(tmp_path):
    out = _judge(tmp_path, "sonnet", PINS["opus"], [PINS["sonnet"]])
    assert any(v.startswith("FAIL") and "the attempt's model is the sonnet pin" in v for v in _verdicts(out)), out


def test_an_attempt_that_recorded_no_model_fails(tmp_path):
    out = _judge(tmp_path, "fable", "", [PINS["fable"]])
    assert any(v.startswith("FAIL") and "attempt's model" in v for v in _verdicts(out)), out


def test_a_stream_that_ran_none_of_the_pin_fails(tmp_path):
    out = _judge(tmp_path, "fable", PINS["fable"], ["claude-opus-4-1"])
    assert any(v.startswith("FAIL") and "does not hold the fable pin" in v for v in _verdicts(out)), out


def test_a_stream_with_no_models_fails(tmp_path):
    out = _judge(tmp_path, "haiku", PINS["haiku"], [])
    assert any(v.startswith("FAIL") for v in _verdicts(out)), out


def test_a_stream_that_also_ran_another_classes_pin_fails(tmp_path):
    out = _judge(tmp_path, "opus", PINS["opus"], [PINS["opus"], PINS["fable"]])
    assert any(v.startswith("FAIL") and f"fable={PINS['fable']}" in v for v in _verdicts(out)), out


def test_the_housekeeping_haiku_pin_beside_the_pin_is_noted_not_failed(tmp_path):
    out = _judge(tmp_path, "opus", PINS["opus"], [PINS["haiku"], PINS["opus"]])
    assert all(v.startswith("PASS") for v in _verdicts(out)), out
    assert any(line.startswith("INFO") and PINS["haiku"] in line for line in out.splitlines()), out


def test_a_haiku_step_whose_stream_ran_opus_fails(tmp_path):
    out = _judge(tmp_path, "haiku", PINS["haiku"], [PINS["haiku"], PINS["opus"]])
    assert any(v.startswith("FAIL") and f"opus={PINS['opus']}" in v for v in _verdicts(out)), out


def test_a_catalogue_that_serves_no_pin_for_the_class_fails(tmp_path):
    served = {k: v for k, v in PINS.items() if k != "sonnet"}
    out = _judge(tmp_path, "sonnet", PINS["sonnet"], [PINS["sonnet"]], runtimes=_runtimes(served))
    verdicts = _verdicts(out)
    assert verdicts and verdicts[0].startswith("FAIL") and "no pin for class sonnet" in verdicts[0], out


def test_an_unreadable_catalogue_fails(tmp_path):
    out = _judge(tmp_path, "opus", PINS["opus"], [PINS["opus"]], runtimes="")
    assert any(v.startswith("FAIL") for v in _verdicts(out)), out


def test_a_usage_models_that_is_not_a_list_fails(tmp_path):
    out = _judge(tmp_path, "opus", PINS["opus"], '"claude-opus-5-5"')
    assert any(v.startswith("FAIL") and "not a list" in v for v in _verdicts(out)), out


# ---------------------------------------------------------------------------
# One step end to end, on stubbed reads
# ---------------------------------------------------------------------------


def _step(tmp_path: Path, *, state: str, attempts: list[dict], doc: dict, task: str = TASK) -> str:
    rt = tmp_path / "runtimes.json"
    rt.write_text(_runtimes())
    body = f"""
acc_run_to_end() {{ printf -v "$1" '%s' "${{STATE}}"; }}
task_attempts() {{ printf '%s\\n' "${{ATTEMPTS}}" | jq -c '.[]'; }}
task_field() {{ printf '%s' "${{TASK_DOC}}" | jq -r "$2"; }}
_cc_check_model_class sonnet {shlex.quote(task)} {shlex.quote(str(rt))}
"""
    result = _bash(body, tmp_path, STATE=state, ATTEMPTS=json.dumps(attempts), TASK_DOC=json.dumps(doc))
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_step_reads_the_newest_attempts_model_and_the_runner_usage(tmp_path):
    attempts = [{"created_at": "2026-10-19T10:00:00Z", "model": PINS["opus"]},
                {"created_at": "2026-10-19T10:05:00Z", "model": PINS["sonnet"]}]
    doc = {"state": "SUCCEEDED", "result_summary": {"runner": {"usage": {"models": [PINS["sonnet"]]}}}}
    out = _step(tmp_path, state="SUCCEEDED", attempts=attempts, doc=doc)
    assert "CASE claude-code: a sonnet step runs its pin" in out
    verdicts = _verdicts(out)
    assert verdicts and all(v.startswith("PASS") for v in verdicts), out


def test_a_step_the_cli_refused_fails_with_its_error(tmp_path):
    doc = {"state": "FAILED", "last_error": "runner_error: model not found"}
    out = _step(tmp_path, state="FAILED", attempts=[], doc=doc)
    verdicts = _verdicts(out)
    assert len(verdicts) == 1 and verdicts[0].startswith("FAIL") and "ended FAILED" in verdicts[0], out
    assert "model not found" in verdicts[0]


def test_a_step_never_submitted_fails(tmp_path):
    out = _step(tmp_path, state="SUCCEEDED", attempts=[], doc={}, task="")
    verdicts = _verdicts(out)
    assert verdicts == ["FAIL claude-code: a sonnet step runs its pin: the attempt records it and usage.models agrees: not submitted"], out
