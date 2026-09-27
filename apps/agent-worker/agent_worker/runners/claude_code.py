"""Runner: Claude Code, non-interactive.

The CLI is started in print mode with STREAMED JSON output (one event per line,
`--output-format stream-json --verbose`), in the repository checkout when the
task has one and in the attempt's isolated work directory when it has none,
with HOME the work directory either way, `--model` the Job's `MODEL`, the
tenant's credential in its environment and nothing else from the worker's own
environment. That credential is either `ANTHROPIC_API_KEY`
(metered API access) or `CLAUDE_CODE_OAUTH_TOKEN` (a Claude subscription token
from `claude setup-token`) -- whichever the tenant's secret supplies. Only the
one that is present is passed to the child.

About permissions: an interactive permission prompt in a non-interactive
container is a hang, not a safety feature -- nobody is there to answer it, and
the attempt would burn its whole timeout waiting. The real boundary is the
container itself: non-root, read-only root filesystem, dropped capabilities, a
workspace that is deleted afterwards and a NetworkPolicy that denies egress to
every other tenant's namespace. Within that box the agent is allowed to work
without asking, and the deployment can turn that off by setting
CLAUDE_CODE_ARGS.

WHY stream-json (#184, 2026-09-25). With `--output-format json` the CLI prints
ONE object when it ends and nothing before, so a running claude-code attempt
had nothing for the live tail to show, and the object it did print kept only
the final answer: the reference run (task_73b5f4d9ca3641fbb914) took seven
turns and recorded none of them. `stream-json` prints each event as it happens
-- the init record, every assistant block and tool call, every tool result,
the `rate_limit_event` readings the account pool wants, and the same `result`
event last -- and the CLI requires `--verbose` alongside it in print mode.
`cliagent` already parses a streamed run (`_parse_cli_output` returns the list
of events and `_spend_of` reads the last one that carries spend). The agent's
stdout capture, `claude-code.stdout.log`, is therefore NDJSON, and the worker
publishes its tail every five seconds while the attempt runs.

WHY THE CHECKOUT AND A PINNED MODEL (#226, owner decisions of 2026-09-26). A
step should behave like the same work run in a local Claude Code lane wherever
the difference is a choice. Locally the CLI starts in the repository and loads
its `CLAUDE.md` by itself; here it started in `work/`, with the checkout at
`./repo`, and read `CLAUDE.md` only when a prompt said to. And no Job set
`MODEL`, so the CLI's own default ran (`claude-sonnet-5`, on the QA task of
that day) instead of the model the operator's lanes run. `MODEL` is now set on
this profile's Job in Terraform (`local.runner_models`), and a caller's
`input.model` is refused (invariant 10). See
`cliagent.agent_working_directory` and docs/agent-output.md.
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
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ),
    key_env="ANTHROPIC_API_KEY",
    # A Claude subscription has no API key. `claude setup-token` mints a
    # long-lived OAuth token for exactly this headless case, and the CLI reads it
    # from CLAUDE_CODE_OAUTH_TOKEN. Supporting both means a tenant can bring
    # whichever they actually pay for instead of buying metered API access to
    # run agents they already have a plan for.
    alt_key_envs=("CLAUDE_CODE_OAUTH_TOKEN",),
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
