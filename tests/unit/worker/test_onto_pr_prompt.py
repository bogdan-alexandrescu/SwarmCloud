"""A step that publishes onto an existing pull request is told to leave the
workflow paths alone (issue 472, task_3162ecbd, 2026-11-25).

THE MEASURED FAILURE. A step whose signed dispatch block carries
`publish_to_pr` pushes onto an EXISTING pull request's branch, and the
worker refuses the WHOLE publish (`publish_to_pr_workflow_files`) when the
step's change touches `.github/workflows` or `.github/actions`: the tenant
token has no `workflow` scope. Nothing told the agent. task_3162ecbd worked
40 minutes on pull request 356, deleted one line of
`.github/workflows/terraform.yml`, and the publish refused the branch.

The worker now exports `SWARM_PUBLISH_ONTO_PR=1` for such a step
(`publishpr.ONTO_PR_ENV`), and the runner adds one paragraph after the
publishing paragraph. Held here against the real claude-code runner (the
paragraph rides its system prompt) and the real codex runner (no such flag:
the user prompt), each with a fake CLI that records its argv and stdin:

* with the variable, the paragraph is there exactly once, naming both
  pathspecs and saying to leave them as they are;
* without it, or on a step that publishes nothing (`SWARM_STEP_PUBLISHES=0`),
  it is not: those prompts are byte-identical to before;
* the runner's spelling of the variable is the worker's.

MUTATIONS: drop the append in `run_cli_agent` (the first test is red);
append it without `publishes_onto_pr` (the second is red); without
`step_publishes` (the third is red).
"""

from __future__ import annotations

import json
import secrets as pysecrets
from pathlib import Path

import pytest

from agent_worker import publishpr
from agent_worker.runners import claude_code, cliagent, codex
from agent_worker.runners.base import RunnerContext

#: Records each start's stdin and argv in $HOME; answers as stream-json.
RECORDING_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ["HOME"], "stdin.txt").write_text(sys.stdin.read())
pathlib.Path(os.environ["HOME"], "argv.json").write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "num_turns": 1}), flush=True)
'''

ONTO_PR = "SWARM_PUBLISH_ONTO_PR"

RUNNERS = [
    # Built at runtime: no credential-shaped literal in this file.
    pytest.param(claude_code, "CLAUDE_CODE_BIN", "ANTHROPIC_API_KEY",
                 lambda: "sk-ant-" + "api03-" + pysecrets.token_hex(16), id="claude-code"),
    pytest.param(codex, "CODEX_BIN", "OPENAI_API_KEY",
                 lambda: "sk-" + "proj-" + pysecrets.token_hex(16), id="codex"),
]


def _ctx(tmp_path: Path) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    checkout = work / "repo"
    checkout.mkdir(parents=True)
    artifacts.mkdir()
    ctx = RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Make the failing check green."},
    )
    ctx.repo_dir = checkout
    return ctx


def _run(tmp_path: Path, monkeypatch, runner, bin_env: str, key_env: str, key,
         **env: str) -> tuple[str, str, bool]:
    """(system prompt, user prompt, whether the runner has a system-prompt flag)."""
    ctx = _ctx(tmp_path)
    cli = tmp_path / "fake-cli"
    cli.write_text(RECORDING_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv(bin_env, str(cli))
    monkeypatch.setenv(key_env, key())
    for name in (
        "CLAUDE_CODE_ARGS", "CODEX_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
        "SWARM_CHILDREN", "SWARM_GIT", cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
        cliagent.RESUME_REASON_ENV, cliagent.STEP_PUBLISHES_ENV, ONTO_PR,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    runner.body(ctx)
    prompt = (ctx.work_dir / "stdin.txt").read_text()
    argv = json.loads((ctx.work_dir / "argv.json").read_text())
    flag = runner.SPEC.system_prompt_flag
    system = argv[argv.index(flag) + 1] if flag and flag in argv[:-1] else ""
    return system, prompt, bool(flag)


def _workflow_paragraphs(text: str) -> list[str]:
    return [p for p in text.split("\n\n")
            if ".github/workflows" in p and ".github/actions" in p]


@pytest.mark.parametrize("runner, bin_env, key_env, key", RUNNERS)
def test_an_onto_pr_step_is_told_to_leave_the_workflow_paths_alone(
    tmp_path, monkeypatch, runner, bin_env, key_env, key,
):
    system, prompt, has_flag = _run(tmp_path, monkeypatch, runner, bin_env, key_env, key,
                                    **{ONTO_PR: "1"})
    # claude-code is told in its system prompt; codex, which has no such
    # flag, in its user prompt -- where PUBLISH_PARAGRAPH is.
    where, other = (system, prompt) if has_flag else (prompt, system)
    hits = _workflow_paragraphs(where)
    assert len(hits) == 1, (system[-1500:], prompt[-1500:])
    assert _workflow_paragraphs(other) == []
    paragraph = hits[0]
    assert "exactly as it is" in paragraph, paragraph
    assert "existing pull request" in paragraph, paragraph
    assert "refuses the WHOLE publish" in paragraph, paragraph
    assert "$SWARM_ARTIFACTS_DIR/pr-body.md" in paragraph, paragraph
    assert paragraph == cliagent.ONTO_PR_WORKFLOW_PARAGRAPH
    # Right after the publishing paragraph, which is still said once.
    assert f"{cliagent.PUBLISH_PARAGRAPH}\n\n{paragraph}" in where
    assert (system + prompt).count(cliagent.PUBLISH_PARAGRAPH) == 1


@pytest.mark.parametrize("value", [None, "0", ""])
@pytest.mark.parametrize("runner, bin_env, key_env, key", RUNNERS)
def test_a_step_without_the_variable_is_not_told(
    tmp_path, monkeypatch, runner, bin_env, key_env, key, value,
):
    env = {} if value is None else {ONTO_PR: value}
    system, prompt, _ = _run(tmp_path, monkeypatch, runner, bin_env, key_env, key, **env)
    assert _workflow_paragraphs(system + "\n\n" + prompt) == []
    assert ".github/workflows" not in system + prompt
    # Still a publishing repository step: the publishing paragraph is there.
    assert (system + prompt).count(cliagent.PUBLISH_PARAGRAPH) == 1


@pytest.mark.parametrize("runner, bin_env, key_env, key", RUNNERS)
def test_a_step_that_publishes_nothing_is_not_told(
    tmp_path, monkeypatch, runner, bin_env, key_env, key,
):
    system, prompt, _ = _run(tmp_path, monkeypatch, runner, bin_env, key_env, key,
                             **{ONTO_PR: "1", "SWARM_STEP_PUBLISHES": "0"})
    assert ".github/workflows" not in system + prompt
    assert cliagent.PUBLISH_PARAGRAPH not in system + prompt


def test_publishes_onto_pr_is_true_only_for_one():
    assert cliagent.publishes_onto_pr({ONTO_PR: "1"}) is True
    for value in ("", "0", "true", "yes"):
        assert cliagent.publishes_onto_pr({ONTO_PR: value}) is False, value
    assert cliagent.publishes_onto_pr({}) is False


def test_the_runner_spells_the_variable_as_the_worker_does():
    assert cliagent.ONTO_PR_ENV == publishpr.ONTO_PR_ENV == ONTO_PR
