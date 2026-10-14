"""Runner: Claude Code, non-interactive.

The CLI is started in print mode with STREAMED JSON output (one event per line,
`--output-format stream-json --verbose`), in the repository checkout when the
task has one and in the attempt's isolated work directory when it has none,
with HOME the work directory either way and `--model` the Job's `MODEL`.

Its environment is built from scratch by `cliagent.run_cli_agent`, not
inherited. It holds the platform's own values (`PATH`, `HOME`, the locale,
`TERM`, `CI`, `NO_COLOR`, `SWARM_ARTIFACTS_DIR`, `SWARM_WORK_DIR`, and this
runner's `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`), one provider credential,
and exactly these from the worker's own environment: `TMPDIR`; `HTTPS_PROXY`,
`HTTP_PROXY`, `NO_PROXY` and `NODE_EXTRA_CA_CERTS` when set (the two proxy
URLs registered for redaction, since one can embed a password); and, when set,
the dispatcher's git identity, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
`GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` (#764, `agent_worker.gitidentity`),
which the worker sets from the task so an agent's `git commit` names that
person; `PLAYWRIGHT_BROWSERS_PATH` when the image sets it (the browser
image only, below); and, when the worker set them, the live hand-off's
variables -- `SWARM_HANDOFF`, `SWARM_CDP_ENDPOINT`, `SWARM_BROWSER_UPLOADS`
(`handoff.HANDOFF_ENV`, `handoff.AGENT_PASSTHROUGH_ENV`; a directory, a
loopback address and a directory, never a credential). The CLI's environment
is what its Bash tool inherits, so without them `swarm-handoff` found no spool
and the agent had to export `SWARM_HANDOFF` itself (MEASURED 2026-10-13,
task_1f28d5bfa5a14f618f83; LB-POLISH, part of #1030). On an attempt resumed
after a hand-off park, the worker's summary of the earlier attempts'
hand-offs, `$SWARM_HANDOFF/resumed.md`, follows the caller's prompt
(`resume_summary`; RESUME-PROGRESS). Nothing else crosses. The credential is the tenant's secret, or a pool
account's OAuth token when the attempt holds an account, and it is passed in
exactly one variable: `ANTHROPIC_API_KEY` (metered API access) or
`CLAUDE_CODE_OAUTH_TOKEN` (a Claude subscription token from
`claude setup-token`). The tenant's one secret is projected into both, so
`cliagent._credential_env` picks by the value's shape -- the subscription
token's prefix, `_OAUTH_TOKEN_PREFIX`, means the OAuth variable -- and the
other variable is dropped.

About permissions: an interactive permission prompt in a non-interactive
container is a hang, not a safety feature -- nobody is there to answer it, and
the attempt would burn its whole timeout waiting. The real boundary is the
container itself: non-root, read-only root filesystem, dropped capabilities, a
workspace that is deleted afterwards and a NetworkPolicy that denies egress to
every other tenant's namespace. Within that box the agent is allowed to work
without asking, and the deployment can turn that off by setting
CLAUDE_CODE_ARGS.
The checkout is marked trusted in `work/.claude.json` before launch
(`trust_workspace`, WK-TRUST, 2026-10-12), so the repository's own
`.claude/settings.json` applies as it does in a local lane -- its deny rules
included. Nothing beyond what that file grants is added.

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

WHY A PRE-PUBLISH GATE (SPEED-D1, owner decision 2026-10-14). 0.52 red CI
runs per pull request, and 128 of 150 needed an update from main. After the
repair turns the runner rebases the lane onto the current base through the git
tools (one conflict turn) and runs the repository's own checks on what the
diff touches (one fix turn); what still fails is published with its output in
the pull request body (`agent_worker.prepublish`, `prepublish`).

WHY A BROWSER NOTE ON agent-runtime-browser (contract request 67, owner
2026-10-11). `claude-code-browser` runs this runner on the browser image, so
an agent can render and screenshot pages. The image installs Chromium under
`PLAYWRIGHT_BROWSERS_PATH` (the image's ENV), which the lifecycle hands this
runner; the CLI's environment is built from scratch, so without passing it on
the agent's Playwright looks under `$HOME/.cache/ms-playwright` -- HOME is
`work/` -- and finds no browser. And Chromium's own sandbox cannot start in
the pod (no user namespaces, every capability dropped: the pod is the
isolation, images/agent-runtime-browser/Dockerfile), so an agent that asks for
it fails to launch. When the variable is set, both are handled here: it is
passed on, and one paragraph is added to the prompt saying how to launch.
Keyed on the image's variable, not the profile's name, so it holds on any
profile that runs the browser image and never on agent-runtime-base.

WHY THE LIVE BROWSER IS THE ONLY ONE (BROWSER-ENFORCE, owner decision
2026-10-13, part of #1030). On task_7cda0d5335f049b4b442 the agent followed the
note above, launched its own headless Chromium, and the person watching the
live browser saw none of its work. So when the live browser exists -- the
dispatcher set `SWARM_CDP_ENDPOINT`, which it does only with the display
sidecar -- four things change, and only then:
  * the agent's `PLAYWRIGHT_BROWSERS_PATH` is `AGENT_BROWSERS_PATH`, a
    directory the image fills with a stub (images/agent-runtime-browser/
    live-browser-only), so any `chromium.launch()` fails with a message
    naming the CDP endpoint. The worker's own value, which the gate and the
    `browser` runner read, is untouched;
  * the Playwright MCP server is registered with `--cdp-endpoint`
    (`write_mcp_config`), so the agent's browser_* tools drive the live
    browser and the tabs they open are the console's;
  * the note says so, and names `swarm_browser.live()` for Python;
  * dev-browser (DEVB-INSTALL) gets its state directory under `work/`
    (`DEV_BROWSER_HOME`) and the stub as its Chrome, and the note gains one
    paragraph naming it as an alternative to the browser_* tools.
And on the browser image the CLI's PATH puts `VENV_BIN` first, so the agent's
`python` is the one with Playwright and the helper. Without the endpoint the
browser image behaves as before: the real browsers and the headless note.

THE GIT TOOLS (docs/runner-git-tools.md §1.3, lane G6). When the worker
offers the attempt the runner git tools it sets `SWARM_GIT`, the spool, and
only then: this runner names the `swarm-git` shim (`agent_worker.gitmcp`) in
`--mcp-config`. With the live browser too, both servers go in ONE config file
(`write_mcp_config`), because the CLI takes the servers of one `--mcp-config`
and a second flag is a second reading of the same option. The shim holds
nothing: no token, no key, no network. It writes requests the worker answers.

THE SEARCH TOOL (docs/search-tool.md, lane SEARCH-TOOL, part of #1030). When
the worker offers the attempt the search tool it sets `SWARM_SEARCH`, the
spool, and only then -- swarm-api's SEARCH_TOOL_ENABLED on, a live-browser
profile, a registered attempt key: this runner names the `swarm-search` shim
(`agent_worker.searchmcp`) in the same `--mcp-config`, and the live-browser
note gains `LIVE_BROWSER_SEARCH_NOTE`. With the flag off the note says nothing
about a search tool, because an agent told to use a tool it does not have
loses turns looking for it.

WHY BASH STDIN IS /dev/null (#750, owner decision 2026-10-06). A Bash command
that read stdin with nothing attached sat until the Bash timeout killed it:
the CLI adds `< /dev/null` itself only to a command with no heredoc and no `<`
redirect. A second PreToolUse hook in the same settings file
(`stdin_hook.py`, shipped in this package) wraps each Bash command so its
outer stdin is /dev/null, leaving pipes and redirects inside it alone.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import re
import sys
from pathlib import Path
from typing import Any

from ..children import git_shim_argv, search_shim_argv
from ..gitmcp.server import SERVER_NAME as GIT_MCP_SERVER_NAME
from ..gitmcp.server import SPOOL_ENV as GIT_SPOOL_ENV
from ..searchmcp.server import SERVER_NAME as SEARCH_MCP_SERVER_NAME
from ..searchmcp.server import SPOOL_ENV as SEARCH_SPOOL_ENV
from ..handoff import AGENT_PASSTHROUGH_ENV, HANDOFF_ENV, RESUME_NAME
from .base import RunnerContext, run_runner
from .cliagent import CliAgentSpec, agent_working_directory, run_cli_agent

#: The CLI's documented switch for every background-task feature: the Bash and
#: subagent `run_in_background` parameter, auto-backgrounding and Ctrl+B.
NO_BACKGROUND_ENV = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"

#: Where the generated settings and hook live: in the attempt's `work/`, which
#: is HOME, never in the checkout (the harvest's patch) nor in `artifacts/`.
SETTINGS_DIR_NAME = ".swarm-claude-code"

#: The tools whose calls can ask to run in the background.
_BACKGROUND_TOOLS = "Bash|Task|Agent"

#: The PreToolUse hook that runs every Bash command with its outer stdin from
#: /dev/null (#750; the why is in its docstring). Unlike the background
#: refusal it is not generated into `work/`: it ships in this package and is
#: named by absolute path, so an agent cannot edit the copy a later attempt
#: runs, and it is the file the tests run.
STDIN_HOOK_NAME = "stdin_hook.py"
STDIN_HOOK = Path(__file__).resolve().parent / STDIN_HOOK_NAME

#: Where agent-runtime-browser installed Chromium (its Dockerfile's ENV). Set
#: only in that image; the lifecycle passes it to this runner by name.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"

#: What an agent on the browser image is told, once, after its prompt. The
#: launch flags are the ones the image's own build check launches with.
BROWSER_NOTE = (
    "A headless Chromium is installed for Playwright in this environment "
    "(Python: `from playwright.sync_api import sync_playwright`). Launch it "
    "with `p.chromium.launch(headless=True, channel=\"chromium\", "
    "chromium_sandbox=False, args=[\"--no-zygote\"])`: the image has the full "
    "Chromium only, and its own sandbox cannot start in this "
    "container, which is itself the sandbox. Save screenshots and other "
    "captures under $SWARM_ARTIFACTS_DIR so they are uploaded."
)

#: Set by the dispatcher on the worker container only with the live browser's
#: display sidecar (scheduler/dispatch.py); the lifecycle passes it on.
CDP_ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"

#: The agent's PLAYWRIGHT_BROWSERS_PATH when the live browser exists: the
#: image's stub directory, where every Chromium Playwright could launch is
#: `live-browser-only` (images/agent-runtime-browser/install-browser-stub.py).
AGENT_BROWSERS_PATH = "/opt/swarm/live-browser-only"

#: The Playwright MCP server the browser image pins (`@playwright/mcp`), by
#: absolute path: the npm global prefix the base image installs CLIs into.
PLAYWRIGHT_MCP_BIN = "/usr/local/share/npm-global/bin/playwright-mcp"
MCP_SERVER_NAME = "playwright"
MCP_CONFIG_NAME = "mcp.json"

#: The image's venv, first on the agent's PATH on the browser image.
VENV_BIN = "/opt/venv/bin"

#: What an agent is told, once, after its prompt, when the live browser exists.
LIVE_BROWSER_NOTE = (
    "This task has a live browser that a person watches on the console. It is "
    "the only browser here: starting your own Chromium (`chromium.launch()`, "
    "headless or not) fails by design. Drive it with the browser_* tools "
    "(browser_navigate, browser_snapshot, browser_click, browser_tabs, "
    "browser_take_screenshot), which are already connected to it. From Python, "
    "`from swarm_browser import live; playwright, browser, context = live()` "
    "connects over CDP to $SWARM_CDP_ENDPOINT; call `playwright.stop()` when "
    "done, never `browser.close()`. Open each new page in its own tab so the "
    "person sees it. When a site shows a CAPTCHA, a bot check, a consent wall "
    "or a sign-in, do not try to get around it and do not switch to a "
    "different site than the one asked for: run `swarm-handoff request "
    "--reason \"<what the person should do>\" --expect <domain>`, then "
    "`swarm-handoff wait`, and carry on once control comes back. Until then: "
    "wait, do not poll the browser -- run `swarm-handoff wait` "
    "in the foreground and nothing else (no `curl /json/version`, no "
    "snapshots in a loop); on exit 75 run it again, on exit 3 (nobody came) "
    "write where you stopped into $SWARM_HANDOFF/notes.md and stop -- the task "
    "parks until the person is ready. Only if the hand-off is refused (exit "
    "5), say so in your answer and then use an alternative, naming it. Save "
    "screenshots and other captures under $SWARM_ARTIFACTS_DIR so "
    "they are uploaded."
)

#: dev-browser (DEVB-INSTALL, owner decision 2026-10-14, part of #1030): the
#: browser image's opt-in CLI beside the Playwright MCP server, which stays
#: registered and stays the default. Its state directory -- the daemon's
#: socket and the named-page map -- goes under `work/`, so the pages an agent
#: named survive with the workspace across a park; DEV_BROWSER_CHROME is the
#: image's copy of the live-browser-only stub, so even the real binary, run by
#: its path, cannot launch a hidden Chromium (images/agent-runtime-browser/
#: Dockerfile). All three are set only when the live browser exists.
DEV_BROWSER_HOME_ENV = "DEV_BROWSER_HOME"
DEV_BROWSER_HOME_NAME = ".dev-browser"
DEV_BROWSER_CHROME_ENV = "DEV_BROWSER_CHROME"
DEV_BROWSER_CHROME = "/opt/swarm/dev-browser/live-browser-only"
DEV_BROWSER_SKIP_DOWNLOAD_ENV = "DEV_BROWSER_SKIP_DOWNLOAD"

#: Appended to `LIVE_BROWSER_NOTE` whenever the live browser exists.
LIVE_BROWSER_DEV_BROWSER_NOTE = (
    "dev-browser is also available, as an alternative to the browser_* tools "
    "for scripted multi-step browsing (run `dev-browser --help`). It always "
    "drives the same live browser the person watches. After every "
    "`swarm-handoff wait` returns, run `dev-browser stop` before your next "
    "script, so it re-attaches to the pages as the person left them."
)

#: Appended to `LIVE_BROWSER_NOTE` only when the search tool is offered
#: (`SWARM_SEARCH` is set). Owner decision 2026-10-13, "Both: search tool +
#: proxy": a query typed into a search engine's page is what Google answered
#: 429 /sorry to (docs/browser-egress.md §0).
LIVE_BROWSER_SEARCH_NOTE = (
    "To search the web, use the search tool, then open the results in the live "
    "browser: do not type queries into a search engine's page."
)

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


#: The most of the worker's resume summary a prompt carries. The worker
#: bounds what it writes well under this; a larger file is not its.
MAX_RESUME_BYTES = 16 * 1024


def resume_summary(spool: str) -> str | None:
    """The worker's summary for a resumed live-browser attempt, or None.

    RESUME-PROGRESS (QA12-14, part of #1030): the CLI has no conversation to
    continue across a hand-off park, so the next attempt's agent learns which
    hand-offs were done, and reads its own notes, from its prompt.
    `handoff.HandoffPath.prepare` writes the file on every attempt that has an
    earlier one's hand-offs to tell and removes it on every other, before this
    runner starts, so a file here is this attempt's.
    """
    if not spool:
        return None
    path = Path(spool) / RESUME_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESUME_BYTES:
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def write_headless_settings(directory: Path) -> Path:
    """Write the CLI's `--settings` file and its hook into `directory`; return the file."""
    if directory.is_symlink():
        directory.unlink()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = directory / "refuse-background.py"
    _write_private(hook, _HOOK)
    settings = {
        "env": {
            NO_BACKGROUND_ENV: "1",
            # Observer P21, owner decision 2026-10-06: the CLI's 2-minute
            # default Bash timeout killed a foreground command in 3 of 19
            # chunk-2 steps, while the lane briefs allow any command up to 10
            # minutes. Both at 10 minutes, so the default and the most a call
            # may ask for match the briefs. Strings: the settings' env is.
            "BASH_DEFAULT_TIMEOUT_MS": "600000",
            "BASH_MAX_TIMEOUT_MS": "600000",
        },
        # #735, owner decision 2026-10-06: no attribution written at all, so
        # no "Generated with" footer reaches a pr-body.md and no
        # Co-Authored-By trailer a commit. The object form is the pinned
        # CLI's (2.1.283) documented spelling of "hide all attribution";
        # `includeCoAuthoredBy` is its deprecated predecessor, for a CLI that
        # predates `attribution`. The worker still strips what gets through
        # (`lifecycle.strip_attribution`).
        "attribution": {"commit": "", "pr": "", "sessionUrl": False},
        "includeCoAuthoredBy": False,
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
                },
                {
                    # Its own entry, so it runs beside the refusal above. It
                    # sets no permission decision, only `updatedInput`, and
                    # fails open: an error lets the command run unchanged.
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{shlex.quote(sys.executable)} -I {shlex.quote(str(STDIN_HOOK))}",
                            "timeout": 30,
                        }
                    ],
                },
            ]
        },
    }
    path = directory / "settings.json"
    _write_private(path, json.dumps(settings, indent=2) + "\n")
    return path


def write_mcp_config(
    directory: Path,
    endpoint: str | None = None,
    *,
    git_spool: str | None = None,
    search_spool: str | None = None,
) -> Path:
    """Write the CLI's `--mcp-config` and return it: Playwright MCP on the live
    browser when `endpoint` is given, the `swarm-git` shim when `git_spool` is,
    the `swarm-search` shim when `search_spool` is.

    Beside the settings file, in `work/` (never the checkout), mode 0600 and
    replaced rather than followed, for the same reasons (`_write_private`).
    The endpoint is the dispatcher's loopback address and the spool the
    worker's path, never caller input.
    """
    if directory.is_symlink():
        directory.unlink()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    servers: dict[str, Any] = {}
    if endpoint:
        servers[MCP_SERVER_NAME] = {
            "type": "stdio",
            "command": PLAYWRIGHT_MCP_BIN,
            "args": ["--cdp-endpoint", endpoint],
        }
    if git_spool:
        command, *args = git_shim_argv(git_spool)
        servers[GIT_MCP_SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    if search_spool:
        command, *args = search_shim_argv(search_spool)
        servers[SEARCH_MCP_SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    config = {"mcpServers": servers}
    path = directory / MCP_CONFIG_NAME
    _write_private(path, json.dumps(config, indent=2) + "\n")
    return path


def trust_workspace(home: Path, workspace: Path) -> Path:
    """Mark `workspace` trusted in `home/.claude.json`, the config the CLI reads; return it.

    WK-TRUST (part of #1194, owner-approved 2026-10-12). An untrusted
    workspace makes the CLI ignore the repository's `.claude/settings.json`
    allow list, and it said so at the top of every claude-code attempt's
    stderr. The CLI's own remedy is `projects[<path>].hasTrustDialogAccepted`,
    keyed by the exact directory it starts in -- `work/repo` of THIS attempt,
    so a resumed attempt on a new directory writes its own entry.

    MERGED, not overwritten: the CLI keeps its state in this file and `work/`
    travels in checkpoints. Only the flag is added -- trust lets the
    repository's own settings apply, its deny rules with them, and grants
    nothing they do not. A file that is not a JSON object is the CLI's broken
    state, not something to keep, and is replaced. A link at the name is
    replaced, never followed (see `_write_private`).
    """
    path = home / ".claude.json"
    config: Any = None
    if path.is_file() and not path.is_symlink():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            config = None
    if not isinstance(config, dict):
        config = {}
    projects = config.get("projects")
    if not isinstance(projects, dict):
        projects = config["projects"] = {}
    entry = projects.get(str(workspace))
    if not isinstance(entry, dict):
        entry = projects[str(workspace)] = {}
    entry["hasTrustDialogAccepted"] = True
    _write_private(path, json.dumps(config, indent=2) + "\n")
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
    # Then the pre-publish gate (SPEED-D1, owner decision 2026-10-14): a
    # rebase onto the current base, with one conflict turn, and the
    # repository's own checks on what the diff touches, with one fix turn
    # (`agent_worker.prepublish`).
    prepublish=True,
    # The CLI warns on EVERY headless run that it ignores the settings' allow
    # list because the workspace is untrusted ("Ignoring 23 permissions.allow
    # entries from .claude/settings.json: this workspace has not been trusted.
    # Run Claude Code interactively ..."). It is benign -- the platform runs
    # with --dangerously-skip-permissions -- and was recorded as the error of a
    # run SIGTERM killed (owner decision 2026-10-06). Matched by its stable
    # prefix, with the entry count as a number, plus the advice should the CLI
    # wrap it onto a line of its own. Since WK-TRUST (2026-10-12) the checkout
    # IS marked trusted before launch (`trust_workspace`), so the warning
    # should no longer appear; the filter stays for a run with no checkout and
    # for a CLI release that keys trust differently.
    benign_stderr=(
        re.compile(
            r"\s*Ignoring \d+ permissions\.allow entries from \S*: "
            r"this workspace has not been trusted"
        ),
        re.compile(r"\s*Run Claude Code interactively"),
    ),
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
    # HOME is `work/` (cliagent), so `work/.claude.json` is the config the CLI
    # reads. Only a checkout is trusted: with none the agent starts in HOME,
    # where there are no repository settings to apply.
    if ctx.repo_dir is not None:
        trust_workspace(Path(ctx.work_dir), agent_working_directory(ctx))
    env = {NO_BACKGROUND_ENV: "1"}
    # The hand-off's variables, which the lifecycle sets only for an attempt
    # with a spool (`handoff.prepare`): what `swarm-handoff` and the agent's
    # Playwright read in the Bash tool (module docstring).
    for name in (HANDOFF_ENV, *AGENT_PASSTHROUGH_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    # The git tools' spool, which the worker sets only for an attempt offered
    # them (`children.ChildPath.prepare`). Passed on as well as named in the
    # config, so the CLI form of the shim works from the agent's Bash tool.
    git_spool = os.environ.get(GIT_SPOOL_ENV, "").strip()
    if git_spool:
        env[GIT_SPOOL_ENV] = git_spool
    # The search tool's spool, likewise: set only for an attempt offered it.
    search_spool = os.environ.get(SEARCH_SPOOL_ENV, "").strip()
    if search_spool:
        env[SEARCH_SPOOL_ENV] = search_spool
    live_endpoint = ""
    browsers = os.environ.get(BROWSERS_PATH_ENV, "").strip()
    endpoint = os.environ.get(CDP_ENDPOINT_ENV, "").strip()
    prompt = ctx.payload.get("prompt")
    spec = SPEC
    # A resumed live-browser attempt: what the earlier one did, after the
    # caller's prompt (RESUME-PROGRESS; `resume_summary`). A new dict, as below.
    resumed = resume_summary(env.get(HANDOFF_ENV, ""))
    if resumed and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{resumed}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    if browsers:
        spec = dataclasses.replace(SPEC, path_prefix=(VENV_BIN,))
        note = BROWSER_NOTE
        if endpoint:
            # The live browser exists: the agent's own launch finds only the
            # stub, and its browser tools are the live browser's (docstring).
            env[BROWSERS_PATH_ENV] = AGENT_BROWSERS_PATH
            env[DEV_BROWSER_HOME_ENV] = str(Path(ctx.work_dir) / DEV_BROWSER_HOME_NAME)
            env[DEV_BROWSER_CHROME_ENV] = DEV_BROWSER_CHROME
            env[DEV_BROWSER_SKIP_DOWNLOAD_ENV] = "1"
            live_endpoint = endpoint
            note = f"{LIVE_BROWSER_NOTE} {LIVE_BROWSER_DEV_BROWSER_NOTE}"
            if search_spool:
                note = f"{note} {LIVE_BROWSER_SEARCH_NOTE}"
        else:
            env[BROWSERS_PATH_ENV] = browsers
        # A new dict on the same context: `run_runner` holds this object.
        if isinstance(prompt, str) and prompt.strip():
            ctx.payload = {**ctx.payload, "prompt": f"{prompt.rstrip()}\n\n{note}"}
    # ONE `--mcp-config` for every server this attempt has (module docstring).
    if live_endpoint or git_spool or search_spool:
        mcp = write_mcp_config(Path(ctx.work_dir) / SETTINGS_DIR_NAME, live_endpoint or None,
                               git_spool=git_spool or None, search_spool=search_spool or None)
        extra += ["--mcp-config", str(mcp)]
    return run_cli_agent(ctx, spec, extra_args=extra, extra_env=env)


def main() -> int:
    return run_runner(body, name="claude-code")


if __name__ == "__main__":
    raise SystemExit(main())
