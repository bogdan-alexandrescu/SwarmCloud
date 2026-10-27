"""The agent is told its memory budget, in its env and in its prompt (#2504).

Five claude-code attempts (f-013, f-014, f-039, f-040, f-041) ran `pytest -n
8`, `tsc` with `NODE_OPTIONS=--max-old-space-size=5000` and resident dev
servers inside 5 and 8 GiB containers. The kernel OOM-killed the whole
container, the worker with it, and each attempt ended `lost_worker`. Nothing
had told the agent how much memory it had: `run_cli_agent` set no resource
variable and its prompt had no budget line, and `nproc` said 6.

What holds now, one test per property:

1. the agent CLI's env carries SWARM_MEMORY_LIMIT_BYTES, the cgroup's
   `memory.max`, and PYTEST_XDIST_AUTO_NUM_WORKERS, max(1, min(floor(cpu),
   (limit - 2 GiB) // 1 GiB)) -- 6 for 8 GiB/6 cpu, 2 for 5 GiB/2 cpu, 3 for
   5 GiB/6 cpu;
2. its stdin prompt carries one line naming the budget in GiB and the worker
   count, telling it to stop dev servers before tsc or the tests and never to
   set --max-old-space-size above the budget;
3. an unknown `memory.max` sets neither variable and adds no line;
4. a resumed session is not told again (the session already holds the line),
   though its env still carries the variables;
5. neither a caller's payload, the runner's `extra_env` nor the runner's own
   environment replaces the platform's values.

MUTATIONS: drop the budget from the `env` dict in `run_cli_agent` (1, 5);
drop the line from the prompt (2); add it on resume too (4); set the budget
before `extra_env` with `setdefault` semantics (5); drop the cpu cap in
`agent_resource_budget` (the 5 GiB/2 cpu case of 1).
"""

from __future__ import annotations

import dataclasses
import json
import math
import secrets as pysecrets
from pathlib import Path

import pytest

from agent_worker import metrics
from agent_worker.runners import claude_code, cliagent

from test_runners import make_ctx

GIB = 1 << 30
SESSION = "sess-" + "b2c3d4e5f6071829"

#: A stand-in for `claude --print`: records the budget variables and its whole
#: stdin in `$SWARM_ARTIFACTS_DIR/budget-seen.json`, then reports success.
FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

stdin = sys.stdin.read()
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
names = ("SWARM_MEMORY_LIMIT_BYTES", "PYTEST_XDIST_AUTO_NUM_WORKERS")
(artifacts / "budget-seen.json").write_text(json.dumps(
    {"env": {name: os.environ.get(name) for name in names}, "stdin": stdin}
))
print(json.dumps({"type": "system", "subtype": "init", "session_id": "SESSION"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "SESSION", "result": "ok", "num_turns": 1}), flush=True)
""".replace("SESSION", SESSION)


@pytest.fixture(autouse=True)
def _no_container_budget_from_the_environment():
    """Overrides the worker conftest's fixture of this name: here the cgroup
    readers are the ones each test patches, so nothing is neutralised."""
    yield


def _limits(monkeypatch, memory: int | None, cpu: float | None) -> None:
    monkeypatch.setattr(metrics, "cgroup_memory_max", lambda: memory)
    monkeypatch.setattr(metrics, "cgroup_cpu_limit_cores", lambda: cpu)


def _run(
    tmp_path: Path, monkeypatch, payload: dict | None = None, *, resume: bool = False, **kwargs
) -> dict:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    for name in (
        "CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", "SWARM_CHILDREN",
        cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    if resume:
        monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, SESSION)
        monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_MOVED)
    # A metered key's shape, built at runtime so no line here looks like one.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "-".join(("sk", "ant", "api03", pysecrets.token_hex(20))))
    spec = dataclasses.replace(
        claude_code.SPEC, finish_on_pending=False, repair_checks=False, prepublish=False,
    )
    ctx = make_ctx(tmp_path, payload or {"prompt": "go"})
    cliagent.run_cli_agent(ctx, spec, **kwargs)
    return json.loads((ctx.artifacts_dir / "budget-seen.json").read_text())


def _expected_workers(memory: int, cpu: float) -> str:
    return str(max(1, min(math.floor(cpu), (memory - 2 * GIB) // GIB)))


def _budget_lines(stdin: str) -> list[str]:
    return [line for line in stdin.splitlines() if "--max-old-space-size" in line]


@pytest.mark.parametrize(
    ("memory", "cpu", "workers"),
    [(8 * GIB, 6.0, "6"), (5 * GIB, 2.0, "2"), (5 * GIB, 6.0, "3")],
)
def test_the_env_carries_the_memory_limit_and_the_xdist_worker_count(
    tmp_path, monkeypatch, memory, cpu, workers
):
    _limits(monkeypatch, memory, cpu)

    seen = _run(tmp_path, monkeypatch)

    assert _expected_workers(memory, cpu) == workers
    assert seen["env"] == {
        "SWARM_MEMORY_LIMIT_BYTES": str(memory),
        "PYTEST_XDIST_AUTO_NUM_WORKERS": workers,
    }, seen["env"]


@pytest.mark.parametrize(
    ("memory", "cpu", "gib", "workers"),
    [(8 * GIB, 6.0, "8 GiB", "6"), (5 * GIB, 2.0, "5 GiB", "2")],
)
def test_the_prompt_names_the_budget_in_one_line(tmp_path, monkeypatch, memory, cpu, gib, workers):
    _limits(monkeypatch, memory, cpu)

    seen = _run(tmp_path, monkeypatch)

    assert seen["stdin"].startswith("go"), seen["stdin"][:200]
    lines = _budget_lines(seen["stdin"])
    assert len(lines) == 1, seen["stdin"]
    line = lines[0]
    assert gib in line, line
    assert f"-n {workers}" in line or f"-n above {workers}" in line, line
    for phrase in ("dev server", "tsc", "test", "next dev", "uvicorn", "postgres"):
        assert phrase in line, (phrase, line)
    assert line == cliagent.budget_line(memory, int(workers))


def test_an_unknown_memory_limit_sets_nothing_and_says_nothing(tmp_path, monkeypatch):
    _limits(monkeypatch, None, 6.0)

    seen = _run(tmp_path, monkeypatch)

    assert seen["env"] == {"SWARM_MEMORY_LIMIT_BYTES": None, "PYTEST_XDIST_AUTO_NUM_WORKERS": None}
    assert _budget_lines(seen["stdin"]) == [], seen["stdin"]
    assert cliagent.agent_resource_budget(None, 6.0) == {}


def test_an_unknown_cpu_limit_caps_the_workers_at_the_cpu_count(monkeypatch):
    monkeypatch.setattr(cliagent.os, "cpu_count", lambda: 2)

    budget = cliagent.agent_resource_budget(8 * GIB, None)

    assert budget == {"SWARM_MEMORY_LIMIT_BYTES": str(8 * GIB), "PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}


def test_a_tiny_container_still_gets_one_worker():
    budget = cliagent.agent_resource_budget(2 * GIB, 0.5)

    assert budget["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "1"


def test_a_resumed_session_is_not_told_again(tmp_path, monkeypatch):
    _limits(monkeypatch, 8 * GIB, 6.0)

    seen = _run(tmp_path, monkeypatch, resume=True)

    assert seen["stdin"] == cliagent.RESUME_PROMPT
    assert _budget_lines(seen["stdin"]) == []
    # The process is new, so its env is told again.
    assert seen["env"]["SWARM_MEMORY_LIMIT_BYTES"] == str(8 * GIB)
    assert seen["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "6"


def test_no_caller_or_runner_value_replaces_the_budget(tmp_path, monkeypatch):
    _limits(monkeypatch, 5 * GIB, 2.0)
    monkeypatch.setenv("SWARM_MEMORY_LIMIT_BYTES", str(64 * GIB))
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "32")
    payload = {
        "prompt": "go",
        "env": {"SWARM_MEMORY_LIMIT_BYTES": str(64 * GIB), "PYTEST_XDIST_AUTO_NUM_WORKERS": "32"},
    }

    seen = _run(
        tmp_path,
        monkeypatch,
        payload,
        extra_env={"SWARM_MEMORY_LIMIT_BYTES": str(64 * GIB), "PYTEST_XDIST_AUTO_NUM_WORKERS": "32"},
    )

    assert seen["env"] == {
        "SWARM_MEMORY_LIMIT_BYTES": str(5 * GIB),
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
    }, seen["env"]


def test_the_budget_names_are_platform_values_not_passthroughs():
    for name in ("SWARM_MEMORY_LIMIT_BYTES", "PYTEST_XDIST_AUTO_NUM_WORKERS"):
        assert name not in cliagent._PLAIN_PASSTHROUGH
        assert name not in cliagent._SENSITIVE_PASSTHROUGH
