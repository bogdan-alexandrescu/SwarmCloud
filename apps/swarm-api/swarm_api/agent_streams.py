"""Which artifact is an agent's own stdout, stderr and transcript -- restated, and pinned.

THE TWO KINDS OF STREAM (#184). The worker captures the RUNNER process's
stdout and stderr (`logs/stdout.log`, `logs/stderr.log`): the runner's own
JSON log lines -- "child started", its argv, its cwd. A CLI runner starts the
agent binary as ITS child and captures that child's streams into `artifacts/`
-- for claude-code, `claude-code.stdout.log` is the agent's stream-json
transcript. The task drawer's "Output, as the agent wrote it" panel served the
first and labelled it the second.

Since #184 the worker also publishes the agent's two streams live
(`logs/live/agent_stdout.tail.log`, `.../agent_stderr.tail.log`), uploads a
final copy beside the runner's (`logs/agent_stdout.log`,
`logs/agent_stderr.log`), and records in `result_summary.agent_streams` which
manifest entry holds which stream -- or `null` when the runner has no agent
child at all. An attempt that ran BEFORE that change has none of those: its
agent streams exist only as manifest entries named by the runner's own
convention. That is the reference task, `task_73b5f4d9ca3641fbb914`.

RESTATED, NOT IMPORTED. The swarm-api image installs swarm-api and
swarm-common and nothing else, so `agent_worker.runners.streams` is not
importable in the one environment that matters -- the precedent `inspect.py`
records for the key layout. `tests/unit/control_plane/
test_agent_stream_parity.py` compares this table with
`agent_worker.runners.streams.agent_stream_files` for every profile in the
frozen catalogue, so the two cannot drift silently.
"""

from __future__ import annotations

from typing import Any

#: Profile name -> (stdout, stderr, transcript) under `artifacts/`, or None for
#: a runner with no agent child. A profile missing from this table is UNKNOWN,
#: which is a different answer from None: nothing is concluded about it.
AGENT_STREAM_FILES: dict[str, tuple[str, str, str | None] | None] = {
    "claude-code": ("claude-code.stdout.log", "claude-code.stderr.log", "claude-transcript.json"),
    "codex": ("codex.stdout.log", "codex.stderr.log", "codex-transcript.json"),
    "generic": ("command.stdout.log", "command.stderr.log", None),
    "mock": None,
    "browser": None,
}

#: The agent's streams, as the log routes name them. `stdout` and `stderr` keep
#: meaning the RUNNER's, as they always have.
AGENT_STREAMS = ("agent_stdout", "agent_stderr")

#: `result_summary.agent_streams` key -> the role an artifact listing gives it.
ROLE_OF_KEY = {
    "stdout": "agent_stdout",
    "stderr": "agent_stderr",
    "transcript": "agent_transcript",
}

#: Sentinel: `result_summary` carries no `agent_streams` key at all.
UNDECLARED = object()


def declared_streams(summary: Any) -> Any:
    """`result_summary.agent_streams` as written, or `UNDECLARED` when there is none.

    Three different answers, kept apart:

      * a dict -- the worker named each stream's artifact (or `None` for one
        it did not upload);
      * `None` -- the worker said this runner has NO agent child;
      * `UNDECLARED` -- no summary yet, or one written before #184.
    """
    if not isinstance(summary, dict) or "agent_streams" not in summary:
        return UNDECLARED
    value = summary.get("agent_streams")
    return value if value is None or isinstance(value, dict) else UNDECLARED


def has_no_agent(runner_profile: str, declared: Any) -> bool:
    """True when this task's runner is KNOWN to have no agent child.

    Either the worker said so (`agent_streams: null`), or nothing was declared
    yet and the profile is one this table knows has none -- which is what lets
    a RUNNING mock task answer "not applicable" rather than "absent".
    """
    if declared is None:
        return True
    return declared is UNDECLARED and AGENT_STREAM_FILES.get(runner_profile, ()) is None


def stream_artifacts(runner_profile: str, declared: Any) -> dict[str, str]:
    """Role -> artifact name, for every agent stream this task's manifest should hold.

    From `agent_streams` when the worker declared it; from the runner's own
    naming convention (the table above) when nothing was declared -- the
    pre-#184 attempts. A declared-but-null entry stays absent: the worker said
    that stream was not uploaded, and the convention does not overrule it.
    """
    out: dict[str, str] = {}
    if isinstance(declared, dict):
        for key, role in ROLE_OF_KEY.items():
            name = declared.get(key)
            if isinstance(name, str) and name:
                out[role] = name
        return out
    if declared is UNDECLARED:
        files = AGENT_STREAM_FILES.get(runner_profile)
        if files:
            stdout, stderr, transcript = files
            out["agent_stdout"] = stdout
            out["agent_stderr"] = stderr
            if transcript:
                out["agent_transcript"] = transcript
    return out
