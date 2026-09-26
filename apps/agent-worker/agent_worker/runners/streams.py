"""Where each runner's AGENT writes its own streams, named ONCE.

A CLI runner starts the agent binary as its own child and captures that
child's stdout and stderr into `artifacts/` (`cliagent.run_cli_agent`); the
generic runner does the same for its catalogue command. Those two files are
the agent's own words -- for claude-code, the `stream-json` transcript -- and
they are a different thing from the RUNNER's stdout and stderr, which the
worker captures into `logs/stdout.log` and `logs/stderr.log` and which hold
the runner's own JSON log lines ("child started", argv, cwd). The task
drawer's "Output, as the agent wrote it" panel showed the second and called it
the first (#184).

Three readers need the names, and every rule restated twice in this repository
has since drifted, so they come from here:

  * the runner that WRITES them (`cliagent.cli_stream_files`, `generic`);
  * the worker's live-tail publisher, which republishes both every
    `live_log_interval_seconds` as `logs/live/agent_stdout.tail.log` and
    `logs/live/agent_stderr.tail.log`;
  * the worker's final upload, which copies both to `logs/agent_stdout.log`
    and `logs/agent_stderr.log` and records the names in
    `result_summary.agent_streams`.

swarm-api cannot import this package (its image installs swarm-api and
swarm-common only), so `swarm_api.agent_streams.AGENT_STREAM_FILES` restates
the table, and `tests/unit/control_plane/test_agent_stream_parity.py` compares
the two for every profile in the frozen catalogue.

THE NAMES NEVER COME FROM A CALLER. They are the runner module's own
constants, selected by the profile NAME the frozen catalogue already chose
(invariant 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentStreamFiles:
    """The agent's own capture files, as names under `artifacts/`."""

    stdout: str
    stderr: str
    #: The runner's re-indented copy of the parsed CLI output, when the runner
    #: writes one. Omitted, not truncated, past the size cap -- see
    #: `cliagent.TRANSCRIPT_MAX_CHARS`.
    transcript: str | None = None

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in (self.stdout, self.stderr, self.transcript) if name)


def cli_agent_spec(profile: str) -> Any | None:
    """The `CliAgentSpec` of a profile whose child is a provider's coding-agent CLI.

    claude-code and codex, the two runners that go through
    `cliagent.run_cli_agent`, and None for every other name. The ONE place
    the worker learns which profiles those are: `agent_stream_files` below
    builds their file names from it, and the lifecycle uses it to decide
    which standalone tasks have their working folder uploaded
    (`agent_worker.standalone_outputs`, #184).

    The runner modules are imported inside the function so that importing this
    module -- which the lifecycle does -- does not import every runner.
    """
    if profile == "claude-code":
        from .claude_code import SPEC as claude_spec

        return claude_spec
    if profile == "codex":
        from .codex import SPEC as codex_spec

        return codex_spec
    return None


def agent_stream_files(profile: str) -> AgentStreamFiles | None:
    """The agent stream files of a runner profile, or None when it has no agent child.

    None for `mock` (it writes progress files and nothing to a child) and for
    `browser` (it drives Chromium in-process). None for a name this function
    does not know, rather than a guess: a guessed name is a tail of a file
    nothing writes.
    """
    spec = cli_agent_spec(profile)
    if spec is not None:
        from .cliagent import cli_stream_files

        return cli_stream_files(spec)
    if profile == "generic":
        from .generic import STREAM_FILES

        return STREAM_FILES
    return None
