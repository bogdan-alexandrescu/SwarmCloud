"""The agent's own process is started with SWARM_TASK_ID (#1641).

PR #1642 capped vitest's workers in an agent container: `agentMaxWorkers` in
`apps/swarm-ui/src/vitestWorkers.ts` returns a memory-sized `maxWorkers` only
when `SWARM_TASK_ID` is set, so CI and laptops keep vitest's default. But the
worker set that variable for the RUNNER process only
(`lifecycle._build_child_env`), and both runners build the agent's environment
from scratch without it. So the gate never opened where it was meant to: on
2026-10-23 task_45e9d3b7c0264ef78091 ran `scripts/related-tests.sh` vitest
batches with one jsdom worker per node CPU, sat at ~4.6 cores with its RSS
pinned at the 8 GiB limit, and was OOM-killed.

What holds now, one test per property:

1. the claude-code CLI is started with the runner's SWARM_TASK_ID (and the
   pre-publish gate is handed the same `env`, so its vitest check is capped
   too);
2. it is a plain passthrough, not registered for redaction: a task id is not
   a secret;
3. the generic runner's command carries it when it is set, and not when it is
   not -- CI never sets it;
4. the name the worker passes is the name `vitestWorkers.ts` gates on.

MUTATIONS: drop "SWARM_TASK_ID" from `cliagent._PLAIN_PASSTHROUGH` (tests 1,
2 and 4); move it to `_SENSITIVE_PASSTHROUGH` (test 2); drop it from
`generic._build_env` (test 3); rename `env.SWARM_TASK_ID` in vitestWorkers.ts
(test 4).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from agent_worker.runners import claude_code, cliagent, generic

from test_runners import make_ctx

REPO = Path(__file__).resolve().parents[3]
VITEST_WORKERS = REPO / "apps" / "swarm-ui" / "src" / "vitestWorkers.ts"

#: A stand-in for `claude --print`: records the SWARM_TASK_ID it was started
#: with, then reports a successful turn.
FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

sys.stdin.read()
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
(artifacts / "task-id-env.json").write_text(json.dumps({"SWARM_TASK_ID": os.environ.get("SWARM_TASK_ID")}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
"""


def _task_id() -> str:
    return "task_" + "a" * 20


def test_the_claude_code_cli_is_started_with_swarm_task_id(tmp_path, monkeypatch):
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # A metered key's shape (`_credential_env` picks the variable by it),
    # built in pieces so no line of this file looks like a credential.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "-".join(("sk", "ant", "api03", "x" * 40)))
    monkeypatch.setenv("SWARM_TASK_ID", _task_id())
    # The real claude-code spec, without the turns after the first: those run
    # git against a checkout this test does not have.
    spec = dataclasses.replace(
        claude_code.SPEC, finish_on_pending=False, repair_checks=False, prepublish=False,
    )
    ctx = make_ctx(tmp_path, {"prompt": "go"})

    cliagent.run_cli_agent(ctx, spec)

    seen = json.loads((ctx.artifacts_dir / "task-id-env.json").read_text())
    assert seen["SWARM_TASK_ID"] == _task_id(), (
        "the agent CLI was started without SWARM_TASK_ID, so vitest's "
        "agent-container worker cap never applies"
    )


def test_swarm_task_id_is_a_plain_passthrough_not_a_secret():
    assert "SWARM_TASK_ID" in cliagent._PLAIN_PASSTHROUGH
    assert "SWARM_TASK_ID" not in cliagent._SENSITIVE_PASSTHROUGH


def test_the_generic_runner_carries_swarm_task_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_TASK_ID", _task_id())
    assert generic._build_env(make_ctx(tmp_path, {})).get("SWARM_TASK_ID") == _task_id()

    # CI and a laptop have no task id, and vitest must keep its default there.
    monkeypatch.delenv("SWARM_TASK_ID")
    assert "SWARM_TASK_ID" not in generic._build_env(make_ctx(tmp_path, {}))


def test_the_name_the_worker_passes_is_the_one_vitest_workers_reads():
    source = VITEST_WORKERS.read_text()
    gated = re.findall(r"if \(!env\.([A-Z_][A-Z0-9_]*)\) return undefined", source)
    assert gated, f"{VITEST_WORKERS} no longer gates agentMaxWorkers on an env variable"
    for name in gated:
        assert name in cliagent._PLAIN_PASSTHROUGH, (
            f"vitestWorkers.ts gates on {name}, which the CLI runner does not pass to the agent"
        )
