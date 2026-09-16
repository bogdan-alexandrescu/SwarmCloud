"""Runner: Claude Code, non-interactive.

The CLI is started in print mode with JSON output, in the attempt's isolated
work directory, with the tenant's `ANTHROPIC_API_KEY` in its environment and
nothing else from the worker's own environment.

About permissions: an interactive permission prompt in a non-interactive
container is a hang, not a safety feature -- nobody is there to answer it, and
the attempt would burn its whole timeout waiting. The real boundary is the
container itself: non-root, read-only root filesystem, dropped capabilities, a
workspace that is deleted afterwards and a NetworkPolicy that denies egress to
every other tenant's namespace. Within that box the agent is allowed to work
without asking, and the deployment can turn that off by setting
CLAUDE_CODE_ARGS.
"""

from __future__ import annotations

from typing import Any

from .base import RunnerContext, run_runner
from .cliagent import CliAgentSpec, run_cli_agent

SPEC = CliAgentSpec(
    name="claude-code",
    provider="anthropic",
    binary_env="CLAUDE_CODE_BIN",
    binary_default="claude",
    args_env="CLAUDE_CODE_ARGS",
    args_default=(
        "--print",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ),
    key_env="ANTHROPIC_API_KEY",
    model_flag="--model",
    transcript_name="claude-transcript.json",
)


def body(ctx: RunnerContext) -> dict[str, Any]:
    extra: list[str] = []
    max_turns = ctx.payload.get("max_turns")
    if max_turns is not None:
        extra += ["--max-turns", str(int(max_turns))]
    return run_cli_agent(ctx, SPEC, extra_args=extra)


def main() -> int:
    return run_runner(body, name="claude-code")


if __name__ == "__main__":
    raise SystemExit(main())
