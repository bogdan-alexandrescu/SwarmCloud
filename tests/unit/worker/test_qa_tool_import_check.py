"""A visual-check step fails before its agent starts when the venv's Python
cannot import the capture tool (part of #2688).

On 3 November 2026 a production capture leg's agent found "No module named
swarm_qa" mid-run. The owner chose to have the runner find it first: on the
browser image, a step whose brief runs the capture or discovery tool
(`swarm_rollup.agentimage.runs_qa_tool`) asks the venv's interpreter
(`VENV_PYTHON`) to import `swarm_qa`, and when it cannot, the step fails at
once with a message naming the interpreter and the module, and the agent is
never started. No cloud, no emulator: the interpreter's answer is faked,
except in the last two cases, which run this test's own Python.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from swarm_rollup import agentimage

from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext, RunnerFailure

CAPTURE_PROMPT = (
    "Capture, do not review. Then run `/opt/venv/bin/python -m swarm_qa capture --routes "
    "/tmp/swarm-qa/routes-g1.json --group g1 --base https://app.example.com --out "
    "$SWARM_ARTIFACTS_DIR/capture-g1 --bundle $SWARM_ARTIFACTS_DIR/capture-g1.tar`."
)


def _ctx(tmp_path, prompt: str) -> RunnerContext:
    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": prompt},
    )


def _run_body(monkeypatch, tmp_path, *, prompt: str, check, browsers: str | None = "/opt/playwright"):
    """The runner's body with the agent and the interpreter faked: what each saw."""
    seen: dict[str, Any] = {"checks": [], "agent": False}

    def fake_agent(ctx, spec, *, extra_args=(), extra_env=None):
        seen["agent"] = True
        return {}

    def fake_check(argv, **kwargs):
        seen["checks"].append((list(argv), kwargs))
        return check(argv)

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_agent)
    monkeypatch.setattr(claude_code.subprocess, "run", fake_check)
    # As in test_browser_enforce: a SwarmCloud container's own git spool and
    # hand-off spool are not what these cases are about.
    monkeypatch.delenv(claude_code.GIT_SPOOL_ENV, raising=False)
    monkeypatch.delenv(claude_code.HANDOFF_ENV, raising=False)
    monkeypatch.delenv("SWARM_CDP_ENDPOINT", raising=False)
    monkeypatch.setattr(claude_code, "prepare_test_browsers", lambda *_a, **_k: None)
    if browsers is None:
        monkeypatch.delenv(claude_code.BROWSERS_PATH_ENV, raising=False)
    else:
        monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, browsers)
    claude_code.body(_ctx(tmp_path, prompt))
    return seen


def _answer(returncode: int, stderr: str = ""):
    return lambda argv: subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)


def test_a_failed_import_fails_the_step_before_the_agent_starts(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    with pytest.raises(RunnerFailure) as raised:
        seen = _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT,
                         check=_answer(1, "Traceback ...\nModuleNotFoundError: No module named 'swarm_qa'\n"))
    message = str(raised.value)
    # MUTATION: drop the check from `body` and the agent starts instead.
    assert "/opt/venv/bin/python" in message
    assert "swarm_qa" in message
    assert "No module named 'swarm_qa'" in message
    assert "agent was not started" in message
    assert not seen.get("agent")


def test_the_check_asks_the_venvs_python_the_brief_names(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT, check=_answer(0))
    ((argv, kwargs),) = seen["checks"]
    assert argv[0] == agentimage.VENV_PYTHON == "/opt/venv/bin/python"
    assert argv[1:] == ["-I", "-c", "import swarm_qa"]
    assert kwargs["timeout"] == claude_code.QA_TOOL_CHECK_SECONDS
    # Nothing of the worker's environment reaches the child.
    assert set(kwargs["env"]) == {"PATH"}


def test_a_passing_import_starts_the_agent(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT, check=_answer(0))
    assert len(seen["checks"]) == 1
    assert seen["agent"]


def test_a_missing_interpreter_is_named(monkeypatch, tmp_path):
    def gone(argv):
        raise FileNotFoundError(argv[0])

    with pytest.raises(RunnerFailure, match=r"/opt/venv/bin/python does not exist, so it cannot import swarm_qa"):
        _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT, check=gone)


def test_a_hung_import_is_bounded(monkeypatch, tmp_path):
    def hung(argv):
        raise subprocess.TimeoutExpired(argv, claude_code.QA_TOOL_CHECK_SECONDS)

    with pytest.raises(RunnerFailure, match=r"did not import swarm_qa within 30 s"):
        _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT, check=hung)


def test_a_step_that_runs_no_tool_is_not_checked(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, prompt="Review the captures in the tarballs.",
                     check=_answer(1, "ModuleNotFoundError"))
    assert seen["checks"] == []
    assert seen["agent"]


def test_the_base_image_is_not_checked(monkeypatch, tmp_path):
    # A brief on the base image that merely quotes the command (a lane's own
    # brief about this check, say) is not a visual-check step.
    seen = _run_body(monkeypatch, tmp_path, prompt=CAPTURE_PROMPT, browsers=None,
                     check=_answer(1, "ModuleNotFoundError"))
    assert seen["checks"] == []
    assert seen["agent"]


def test_the_real_check_passes_for_an_importable_module(monkeypatch):
    monkeypatch.setattr(claude_code, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(claude_code, "QA_TOOL_MODULE", "json")
    assert claude_code.qa_tool_refusal() is None


def test_the_real_check_names_a_module_that_is_not_there(monkeypatch):
    monkeypatch.setattr(claude_code, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(claude_code, "QA_TOOL_MODULE", "swarm_qa_not_installed_here")
    refusal = claude_code.qa_tool_refusal()
    assert refusal is not None
    assert refusal.startswith(f"{sys.executable} cannot import swarm_qa_not_installed_here (exit 1)")
    assert "No module named 'swarm_qa_not_installed_here'" in refusal
