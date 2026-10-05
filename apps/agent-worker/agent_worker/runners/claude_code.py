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

WHY NO BACKGROUND COMMANDS (owner decision 2026-10-05, lane review W1+G5).
In 3 of 17 implement steps the agent started its test run with
`run_in_background`, ended its turn with "the suite is still running, I'll
report", and the print-mode session ended there: the worker committed a tree
whose tests never finished. Headless, there is no later turn for a background
command to report into. So the CLI is started with background tasks switched
off twice over -- the CLI's own `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, and a
PreToolUse hook in a generated `--settings` file (`write_headless_settings`)
that refuses any call asking for `run_in_background` and tells the agent to run
it in the foreground. The hook is the layer that holds when a CLI release
renames or ignores the variable. And when an answer still announces pending
work, `cliagent` resumes the session once to finish (`finish_on_pending`).

WHY REPAIR TURNS (#624, owner decision 2026-10-05, history I1). About $201 --
14.9% of all spend -- went to agents that finished and then failed a check
they were never shown: an expected output not written, or a credential-shaped
line in the final tree, both checked by the worker only after the agent had
exited. So after the turn ends (and after any finish pass) `cliagent` runs the
same expected-outputs check and the same publish credential scan
(`agent_worker.publish_scan`) against the tree, and when either fails resumes
the session for up to two repair turns, naming each missing file's path or
each flagged `path:line rule` -- never the matched text (`repair_checks`).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from .base import RunnerContext, run_runner
from .cliagent import CliAgentSpec, run_cli_agent

#: The CLI's documented switch for every background-task feature: the Bash and
#: subagent `run_in_background` parameter, auto-backgrounding and Ctrl+B.
NO_BACKGROUND_ENV = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"

#: Where the generated settings and hook live: in the attempt's `work/`, which
#: is HOME, never in the checkout (the harvest's patch) nor in `artifacts/`.
SETTINGS_DIR_NAME = ".swarm-claude-code"

#: The tools whose calls can ask to run in the background.
_BACKGROUND_TOOLS = "Bash|Task|Agent"

#: The PreToolUse hook. Standard library only, run by this runner's own
#: interpreter. Exit 2 is the CLI's "block this tool call", and what it writes
#: to stderr is what the model reads. Input it cannot read is let through: a
#: hook that wedged every tool call would cost the step, and the variable above
#: is still in force.
_HOOK = """\
import json, sys
try:
    call = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tool_input = call.get("tool_input") if isinstance(call, dict) else None
asked = tool_input.get("run_in_background") if isinstance(tool_input, dict) else None
if asked is True or (isinstance(asked, str) and asked.strip().lower() == "true"):
    sys.stderr.write(
        "Background commands are switched off in this headless run: the session "
        "ends with your answer, so nothing started in the background is ever "
        "reported. Run the command in the foreground and wait for it (raise the "
        "Bash timeout parameter if it is long), then report its result.\\n"
    )
    sys.exit(2)
sys.exit(0)
"""


def _write_private(path: Path, text: str) -> None:
    """Write `path` afresh, mode 0600, never through a link left in its place.

    `work/` travels in checkpoints, so whatever is at this name may have been
    put there by an earlier attempt's agent; it is replaced, not followed.
    """
    if path.is_symlink() or path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_headless_settings(directory: Path) -> Path:
    """Write the CLI's `--settings` file and its hook into `directory`; return the file."""
    if directory.is_symlink():
        directory.unlink()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = directory / "refuse-background.py"
    _write_private(hook, _HOOK)
    settings = {
        "env": {NO_BACKGROUND_ENV: "1"},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": _BACKGROUND_TOOLS,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{shlex.quote(sys.executable)} -I {shlex.quote(str(hook))}",
                            "timeout": 30,
                        }
                    ],
                }
            ]
        },
    }
    path = directory / "settings.json"
    _write_private(path, json.dumps(settings, indent=2) + "\n")
    return path

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
    # A pool-account attempt that must change account mid-run is stopped at a
    # turn boundary and continued under the next account with
    # `--resume <session_id>` (S13/S14, `cliagent.AccountStreamWatcher`).
    resume_flag="--resume",
    # An answer that announces pending work is resumed once to finish it
    # (owner decision 2026-10-05; `cliagent.pending_work`).
    finish_on_pending=True,
    # After the turn ends, the expected-outputs check and the publish
    # credential scan run against the tree, and a failure is resumed for up
    # to two repair turns naming what failed (#624; `cliagent.REPAIR_MAX_TURNS`).
    repair_checks=True,
)


def body(ctx: RunnerContext) -> dict[str, Any]:
    extra: list[str] = []
    max_turns = ctx.payload.get("max_turns")
    if max_turns is not None:
        extra += ["--max-turns", str(int(max_turns))]
    # After the platform's flags, so a deployment's CLAUDE_CODE_ARGS cannot
    # leave background commands switched on by replacing the flag set.
    settings = write_headless_settings(Path(ctx.work_dir) / SETTINGS_DIR_NAME)
    extra += ["--settings", str(settings)]
    return run_cli_agent(ctx, SPEC, extra_args=extra, extra_env={NO_BACKGROUND_ENV: "1"})


def main() -> int:
    return run_runner(body, name="claude-code")


if __name__ == "__main__":
    raise SystemExit(main())
