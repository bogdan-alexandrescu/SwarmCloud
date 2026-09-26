"""The runner's `child started` line says the prompt's length, never the prompt.

THE PR #229 REVIEW. `run_child` logged the argv it started, and a CLI runner's
last argument is the task's prompt, so the runner's stderr -- which `/logs`
serves as the runner's `stderr` stream -- carried every prompt in the clear.
The task routes serve the prompt masked, and a value the task's metadata named
as a secret was masked there and printed here.

The prompt still reaches the agent: the fake CLI below records the argv it was
really given, and the prompt is its last element.

MUTATIONS: log `self.argv` again in `ChildProcess.start`; stop passing
`log_argv` from `run_cli_agent`; replace the prompt in the argv the child is
started with rather than in the logged copy (the control goes red).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_worker.runners.base import RunnerContext

PROMPT = "deploy with zork-grue-lantern-brass-4471 and report back"


def _ctx(tmp_path: Path) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": PROMPT},
    )


def test_the_start_line_logs_the_prompts_length_and_the_agent_still_gets_the_prompt(
    tmp_path, monkeypatch, capsys
):
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    seen = tmp_path / "argv.json"
    cli = tmp_path / "fake-cli"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'result': 'ok'}))\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(cli))
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag=None,
    )

    run_cli_agent(_ctx(tmp_path), spec)
    err = capsys.readouterr().err

    started = [
        json.loads(line) for line in err.splitlines()
        if line.startswith("{") and "child started" in line
    ]
    assert len(started) == 1, err
    logged = started[0]["argv"]
    assert "zork-grue-lantern-brass-4471" not in err, "the runner's stderr printed the prompt"
    assert logged[-1] == f"<prompt: {len(PROMPT)} characters>", logged
    # The control: the agent was started with the prompt, unchanged.
    received = json.loads(seen.read_text())
    assert received[-1] == PROMPT, received
    # Everything else is logged as it was passed: the binary, then its flags.
    assert logged[1:-1] == received[:-1], (logged, received)
