"""Runner: OpenAI Codex CLI, non-interactive.

Same shape as the Claude Code runner -- deliberately, because the two profiles
must behave identically from the platform's point of view: the same workspace,
the same artifact layout, the same rate-limit handling, the same park decision.
Only the binary, the flags and the credential differ, and all three are data.
"""

from __future__ import annotations

from typing import Any

from .base import RunnerContext, run_runner
from .cliagent import CliAgentSpec, run_cli_agent

SPEC = CliAgentSpec(
    name="codex",
    provider="openai",
    binary_env="CODEX_BIN",
    binary_default="codex",
    args_env="CODEX_ARGS",
    args_default=("exec", "--skip-git-repo-check"),
    key_env="OPENAI_API_KEY",
    model_flag="--model",
    transcript_name="codex-transcript.json",
)


def body(ctx: RunnerContext) -> dict[str, Any]:
    return run_cli_agent(ctx, SPEC)


def main() -> int:
    return run_runner(body, name="codex")


if __name__ == "__main__":
    raise SystemExit(main())
