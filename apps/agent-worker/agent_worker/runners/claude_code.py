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
person; the worker's cache variables (`cachedirs.AGENT_CACHE_ENV`:
`UV_CACHE_DIR`, `npm_config_cache`, `PIP_CACHE_DIR`, `TF_PLUGIN_CACHE_DIR`,
`XDG_CACHE_HOME` and their switches, CACHE-SCRATCH), so the agent's tool
caches stay off the workspace volume; `PLAYWRIGHT_BROWSERS_PATH` when the image sets it (the browser
image only, below); and, when the worker set them, the live hand-off's
variables -- `SWARM_HANDOFF`, `SWARM_CDP_ENDPOINT`, `SWARM_BROWSER_UPLOADS`,
and the workspace's Jev judgment backend `SWARM_JEV_BACKEND` with, for an
internal backend, its in-cluster `JEV_API_URL` (JUDGE-SELECT; the jev MCP
server inherits them from the CLI) (`handoff.HANDOFF_ENV`,
`handoff.AGENT_PASSTHROUGH_ENV`; directories, a loopback address, a name and
a cluster address, never a credential). The CLI's environment
is what its Bash tool inherits, so without them `swarm-handoff` found no spool
and the agent had to export `SWARM_HANDOFF` itself (MEASURED 2026-10-13,
task_1f28d5bfa5a14f618f83; LB-POLISH, part of #1030). On an attempt resumed
after a hand-off park, the worker's summary of the earlier attempts'
hand-offs, `$SWARM_HANDOFF/resumed.md`, follows the caller's prompt
(`resume_summary`; RESUME-PROGRESS), and on an attempt whose proxied browser
has no egress, `$SWARM_HANDOFF/egress.md` says so (`egress_notice`;
EGRESS-GATE), as `$SWARM_HANDOFF/qa-target.md` says why the gate could not
sign the agent in to its QA target (`qa_target_notice`; VQ4-WIRE). Nothing else crosses. The credential is the tenant's secret, or a pool
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

A TEST PROCESS MAY LAUNCH ITS OWN CHROMIUM (PW-TESTRUNNER, BGD 2026-10-24,
"Test-runner exception on browser"; part of #2253). With the stub above, a
repository's own `npx playwright test` could not launch the headless Chromium
it needs, and the image's browsers directory is read-only, so the revision its
`@playwright/test` pins had nowhere to go (task_2791f0996eab4f958252). So on
the browser image, live browser or not, the runner makes a writable directory,
`<XDG_CACHE_HOME>/swarm-test-browsers` (`prepare_test_browsers`), seeded with
links to the image's revisions, and names it to the agent as
`SWARM_TEST_BROWSERS_PATH`. The image's `swarm-test-browsers <command>` runs
that ONE command with `PLAYWRIGHT_BROWSERS_PATH` pointed there; the agent's
own default stays the stub, so its scripts, the browser_* tools and
dev-browser are unchanged, and the note says the command is for test suites
only. In the attempt's cache, not `work/`: a revision is ~170 MB, which a
checkpoint (capped at 2 GiB) would carry and the 5 GiB GKE workspace volume
would count; the cache is never checkpointed, is its own disk on GKE and
counts in the workspace tmpfs on Cloud Run, and is emptied each attempt, so a
resumed attempt downloads the revision again. Named so that Playwright never
finds it by itself (its default is `ms-playwright`): only a process pointed
at it launches from it. No size is refused.

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

THE GRAPH TOOLS (docs/design/context-quality.md §4.4, lane CTXQ-S). When the
worker staged the repository's graph snapshot (`graphstage.prepare`, decided
by the stored setting, never the caller) it sets `SWARM_GRAPH_SNAPSHOT`, and
only then this runner names `swarm-graph` (`agent_worker.graphmcp`) in the
same `--mcp-config`. When the setting asked for context and none could be
staged it sets `SWARM_GRAPH_UNSTAGED` instead, and the prompt gains one line
saying so. The server reads only the staged directory: no network, no key.

SERVED INSTEAD OF STAGED (docs/design/graph-service.md §4.2, lane GS-WORKER).
When the workspace's stored `graph_delivery` is `service` and the worker's
pin, ticket and probe all held, it sets `SWARM_GRAPH_PIN` and
`SWARM_GRAPH_TICKET_FILE` instead of the snapshot, and `swarm-graph` is the
pod's shim (`graphstage.shim_argv`), under the same name and with the same
tools. The service URL is the worker's `SWARM_GRAPH_URL`, written into the
server entry's `env`, never an argument. claude-code-browser runs this same
module, so its `--mcp-config` carries the shim beside the live browser's
servers.

THE JEV GATE (JEV-RUNNER + BSTATUS-API + JUDGE-SELECT, part of #1030). The
`jev` MCP server is registered when BOTH hold. (a) The task's `browser_tool`
allows it: `jev` or `auto` (`browser_tools.choose`; never `playwright` or
`dev-browser`). (b) The workspace's judgment backend `SWARM_JEV_BACKEND` can
run it (`_jev_backend_key`): `internal-jev` / `internal-laya` need NO
TypeSafe key and no key file is ever written (the entry is the wrapper
itself), but they need the route: `JEV_API_URL`, which the dispatcher sends
only while the tailnet egress is on. Without it the wrapper refuses to
start, so the server is NOT registered and the note says why (`no_route`;
JEV-OFF, #1345: measured 2026-10-15, a registered server failed and the
agent fell back to Playwright with nothing said); `typesafe`, or
unset on an old pod, needs the worker's key file (`SWARM_JEV_KEY_FILE`, a
0600 file in `private/`, never the key; `agent_worker.jevkey`). For a key
file the entry's command is `jev_launch.py`, which reads it and execs the
wrapper with TYPESAFE_API_KEY in that process's environment only: the key is
in neither the MCP config nor the CLI's environment. A binary missing from
the image skips the registration (`not_installed`, and `jev_unavailable` is
logged); the file is removed when the agent has finished. jev-browser sends
page text and labels to TypeSafe (api.typesafe.ai) only on the `typesafe`
backend, which the owner accepted.

THE CALLER'S MCP SERVERS (docs/design/registered-mcp-servers.md §4.7,
lane MCP-RUNNERS, part of #1433). When the worker attached the servers the
task's signed input names and serves its relay (`agent_worker.mcp_relay`), it
sets `SWARM_MCP_RELAY`, the path of a 0600 file in `private/`, and only then:
each server is written into the same `--mcp-config` as an `http` entry whose
URL is the relay's loopback path and whose one header is the per-attempt
LOCAL token (`write_mcp_config`), and the prompt gains one line per server
(`mcp_servers_note`). The caller's credential is never in that file, this
process or the CLI's environment: the relay adds it on the way out. The
config is in `work/`, which checkpoints carry, and that is deliberate: a
loopback URL and a token nothing outside this pod can reach and nothing after
this attempt accepts (§4.7 step 5). Measured against the pinned 2.1.283 with
a loopback fake (tests/unit/worker/test_mcp_runner_config.py): the entry is
accepted, the server's tools reach the model as `mcp__<name>__<tool>`, and
the repository's own `.mcp.json` IS loaded under `--print` (no
`--strict-mcp-config` here), with this config's server winning a name both
declare. While SWARM_MCP_SERVERS is off no task names a server, nothing is
attached, and nothing here changes.

WHY BASH STDIN IS /dev/null (#750, owner decision 2026-10-06). A Bash command
that read stdin with nothing attached sat until the Bash timeout killed it:
the CLI adds `< /dev/null` itself only to a command with no heredoc and no `<`
redirect. A second PreToolUse hook in the same settings file
(`stdin_hook.py`, shipped in this package) wraps each Bash command so its
outer stdin is /dev/null, leaving pipes and redirects inside it alone.

WHY A RELATED-TESTS NOTE (RELATED-TESTS, part of #1694, owner decision
2026-10-20). Agents ran whole test suites and lost hours: measured 2026-10-19
to 20, 36 vitest runs with no file filter at about 22 min each (2.05 h), 8
pytest runs over whole tests/unit directories (0.73 h), and both out-of-memory
kills came from four vitest shards started in the background. So when the
checkout carries `scripts/related-tests.sh` the prompt gains the standing rule
(`RELATED_TESTS_NOTE`): run the related tests through that script, never
vitest without file arguments, never pytest over a whole directory or with
`-n auto` across the suite, never parallel shards or background batches
polled with sleep, and `scripts/changed-guards.sh` once at the end. Keyed on
the script being in the checkout, so another tenant's repository is never
told to run a script it does not have.

WHY A SCRATCH NOTE (issue #2436). task_73850f24085447368991 was evicted twice
with `EmptyDir volume "tmp" exceeds 3Gi`: its agent wrote a Postgres data dir,
test logs and Chromium's libraries to a literal /tmp while TMPDIR pointed at
the 20 GiB cache volume. Every repository step's prompt gains
`cachedirs.scratch_note` (`scratch_note_for`): big scratch data under $TMPDIR,
never a literal /tmp or the workspace. Where the worker put
PLAYWRIGHT_BROWSERS_PATH under the cache (an image with no browser), this
runner hands that path to the agent itself, and never reads it as an image's
browser (`cachedirs.image_browsers_path`).

WHY LANE CAPS (TOKEN-A, #2524 items 6 and 8, BGD 2026-10-27). A token
analysis of 2026-10-20..27 (wf_9a256dde8d174201947c) found 97% of the pool's
tokens were cache reads: the same long context re-read on every turn. What
sits in that context is capped here. The repository's own `swarmcloud` MCP
server (`.mcp.json`) is not loaded (`disabledMcpjsonServers`): no lane in the
sample called it and its ~100 tool schemas were in every prefix. A Bash
result is capped at about 12,000 characters (`BASH_MAX_OUTPUT_LENGTH`). A
second `scripts/changed-guards.sh` on a tree nothing has changed since the
last one is warned, never blocked (`guards_repeat_hook.py`). And the prompt
gains one line (`exploration_note`): the graph's `search` and `call_graph`
before a `grep -rn` across the tree, and broad exploration sent to a sonnet
or haiku child, each clause only when the attempt has that tool.

LIGHTPANDA (docs/design/lightpanda-runtime.md §6.2, contract request 127).
On `claude-code-lightpanda` (SWARM_RUNNER_PROFILE, which the worker sets) this
runner starts and supervises `lightpanda serve` on 127.0.0.1:9223 under
`env -i`, sets SWARM_LIGHTPANDA_CDP and never SWARM_CDP_ENDPOINT, registers the
`lightpanda` MCP server and no `playwright` one, denies three of its tools and
adds `lightpanda.LIGHTPANDA_NOTE` to the prompt. There is no live view, so no
`browser.json`. Checkpointing and fencing are this runner's, unchanged: the
child starts inside `body`, after the worker's fencing check, and stops with
it (`agent_worker.lightpanda`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from swarm_common.models import (
    MCP_SERVER_NAME as REGISTERED_MCP_SERVER_NAME,
    RESERVED_MCP_SERVER_NAMES,
    RESERVED_MCP_SERVER_PREFIX,
)

from .. import browser_tools, graphstage, lightpanda
from ..budget import NOTE_ENV as BUDGET_NOTE_ENV
from ..cachedirs import image_browsers_path, scratch_note
from ..children import CHILDREN_ENV, git_shim_argv, search_shim_argv
from ..mcp_attach import MAX_SERVERS as MAX_MCP_SERVERS
from ..mcp_attach import RELAY_ENV as MCP_RELAY_ENV
from ..mcp_relay import LOOPBACK as MCP_RELAY_HOST
from ..mcp_relay import PATH_PREFIX as MCP_RELAY_PATH
from ..gitmcp.server import SERVER_NAME as GIT_MCP_SERVER_NAME
from ..gitmcp.server import SPOOL_ENV as GIT_SPOOL_ENV
from ..searchmcp.server import SERVER_NAME as SEARCH_MCP_SERVER_NAME
from ..searchmcp.server import SPOOL_ENV as SEARCH_SPOOL_ENV
from ..statemcp.server import SERVER_NAME as STATE_MCP_SERVER_NAME
from ..statemcp.server import SPOOL_ENV as STATE_SPOOL_ENV
from ..statemcp.server import shim_argv as state_shim_argv
from ..handoff import AGENT_PASSTHROUGH_ENV, EGRESS_NOTICE_NAME, HANDOFF_ENV, RESUME_NAME
from ..jevkey import BACKEND_ENV as JEV_BACKEND_ENV
from ..jevkey import INTERNAL_BACKENDS as JEV_INTERNAL_BACKENDS
from ..jevkey import KEY_FILE_ENV as JEV_KEY_FILE_ENV
from ..logs import StructuredLogger
from ..qa_identity import NOTICE_NAME as _QA_NOTICE_NAME
from ..qa_identity import requested as qa_identity_requested
from ..qa_target import NOTICE_NAME as QA_TARGET_NOTICE_NAME
from .base import RunnerContext, RunnerFailure, run_runner
from ..livegate.presets import PRESETS as DEVICE_PRESETS
from .cliagent import CliAgentSpec, agent_working_directory, cli_stream_files, run_cli_agent

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
#: The PostToolUse hook that puts the gate's CAPTCHA hand-off into the agent's
#: next tool result (CAPTCHA-HANDOFF-2; the why is in its docstring). Shipped
#: and named by absolute path, as the stdin hook is.
GATE_WAIT_HOOK_NAME = "gate_wait_hook.py"
GATE_WAIT_HOOK = Path(__file__).resolve().parent / GATE_WAIT_HOOK_NAME
#: The budget's 80% note (#2459), handed to the agent once by a PostToolUse
#: hook: the one way text reaches a headless run mid-turn. Registered only
#: when the worker named a note file (`agent_worker.budget.NOTE_ENV`).
BUDGET_NOTE_HOOK = Path(__file__).resolve().parent / "budget_note_hook.py"
#: The PreToolUse hook that warns a second `scripts/changed-guards.sh` on a
#: tree nothing has changed since the last one (module docstring, LANE CAPS).
#: Shipped and named by absolute path, as the stdin hook is; its state is a
#: file beside the settings, reset each time the settings are written.
GUARDS_REPEAT_HOOK = Path(__file__).resolve().parent / "guards_repeat_hook.py"
GUARDS_STATE_NAME = "guards-state.json"
#: The repository `.mcp.json` servers a lane does not load (LANE CAPS).
REPO_MCP_SERVERS_OFF = ("swarmcloud",)
#: The most characters of one Bash result the CLI keeps (LANE CAPS). A
#: string, as the settings' env is.
BASH_MAX_OUTPUT = "12000"

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

#: The helper whose presence in the checkout turns the note below on.
RELATED_TESTS_SCRIPT = "scripts/related-tests.sh"

#: The standing test rule, once, after the prompt (module docstring).
RELATED_TESTS_NOTE = (
    "Run tests narrowly: only the tests related to what you change. "
    "`scripts/related-tests.sh` prints the related test files and the runs of "
    "named files that test them; `scripts/related-tests.sh --run` runs those "
    "one after another in the foreground. You must never run vitest without "
    "file arguments, never run pytest over a whole directory or with `-n auto` "
    "across the suite, and never start parallel shards or background test "
    "batches polled with sleep: whole-suite runs cost lanes hours and parallel "
    "shards are what ran containers out of memory. Run "
    "scripts/changed-guards.sh once at the end and fix what it reports; CI "
    "runs everything else."
)

#: Where to explore first (LANE CAPS, #2524 item 8), once, after the prompt;
#: each clause only when the attempt has the tool it names (`exploration_note`).
EXPLORATION_GRAPH_CLAUSE = (
    "use the swarm-graph `search` and `call_graph` tools before a `grep -rn` "
    "across the tree"
)
EXPLORATION_CHILDREN_CLAUSE = (
    "send broad exploration to a child task through .swarm-children "
    "($SWARM_CHILDREN) with `model_class` \"sonnet\" or \"haiku\" rather "
    "than reading the tree yourself"
)

#: Set by the dispatcher on the worker container only with the live browser's
#: display sidecar (scheduler/dispatch.py); the lifecycle passes it on.
CDP_ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"

#: The agent's PLAYWRIGHT_BROWSERS_PATH when the live browser exists: the
#: image's stub directory, where every Chromium Playwright could launch is
#: `live-browser-only` (images/agent-runtime-browser/install-browser-stub.py).
AGENT_BROWSERS_PATH = "/opt/swarm/live-browser-only"

#: PW-TESTRUNNER (module docstring): the writable browsers directory a test
#: process is pointed at, in the attempt's cache, by the variable the image's
#: `swarm-test-browsers` reads. Never `ms-playwright`, Playwright's default
#: name, so no process finds it unless it is pointed at it.
TEST_BROWSERS_ENV = "SWARM_TEST_BROWSERS_PATH"
TEST_BROWSERS_DIR_NAME = "swarm-test-browsers"
CACHE_HOME_ENV = "XDG_CACHE_HOME"

#: Appended to the browser note when the test directory exists.
TEST_RUNNER_NOTE = (
    "To run a repository's own Playwright suite, prefix each of its commands "
    "with `swarm-test-browsers` (`swarm-test-browsers npx playwright install "
    "chromium`, then `swarm-test-browsers npx playwright test`): that one test "
    "process gets a writable browsers directory for the Chromium revision the "
    "repository pins. That directory is `$SWARM_TEST_BROWSERS_PATH`; a test "
    "runner the prefix cannot wrap is pointed at it the same way, "
    "`PLAYWRIGHT_BROWSERS_PATH=\"$SWARM_TEST_BROWSERS_PATH\" npx playwright test`. "
    "Chromium's libraries and fonts are already installed, so "
    "never pass `--with-deps`. Use it only for test suites, never for your own "
    "browsing."
)

#: The Playwright MCP server the browser image pins (`@playwright/mcp`), by
#: absolute path: the npm global prefix the base image installs CLIs into.
PLAYWRIGHT_MCP_BIN = "/usr/local/share/npm-global/bin/playwright-mcp"
MCP_SERVER_NAME = "playwright"
MCP_CONFIG_NAME = "mcp.json"
#: QA-KIT (owner decision 2026-10-19, part of #1649): where Playwright MCP
#: writes what it names itself, under the task's artifacts, so every
#: `browser_take_screenshot` taken WITHOUT a filename is uploaded and the
#: console draws it. `--output-dir` is @playwright/mcp 0.0.83's flag ("the
#: directory for automatically named output files"); a screenshot given a
#: filename is resolved against the workspace root instead -- the checkout --
#: and, read off 0.0.83's own code, is then NOT returned to the model as an
#: image, which is why the note asks for no filename. Nothing here sets
#: `--image-responses`, whose default ("allow") returns the image; and no
#: `--output-max-size`, so nothing is evicted before the upload. Other
#: auto-named files (a page's download, a console log the server writes) land
#: here too, in the tenant's own artifacts.
SCREENSHOTS_DIR_NAME = "screenshots"
#: The tool whose results `screenshot_images` reads, as the CLI names it.
SCREENSHOT_TOOL = f"mcp__{MCP_SERVER_NAME}__browser_take_screenshot"

#: BROWSER-START (part of #1366): the runner's own refusal to start a
#: live-browser attempt's agent without its browser. The lifecycle sets
#: SWARM_HANDOFF for every live-browser attempt and nothing else, and fails the
#: attempt retryably (`handoff.LIVE_GATE_UNAVAILABLE`) before the runner when
#: the gate or its CDP endpoint is not ready; so a runner that sees the spool
#: with no endpoint, no browser image or no Playwright MCP entry in its
#: `--mcp-config` has met a defect, and says so in `last_error` rather than run
#: the agent headless (devb-1015: 8 of 60 did, silently). The binary itself is
#: the image's to prove: images/agent-runtime-browser/selftest.py fails the
#: build without it.
LIVE_BROWSER_UNAVAILABLE = "live_browser_unavailable"

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
    "person sees it. "
    # CAPTCHA-AVOID (BGD, 2026-10-23; part of #2058): do not provoke the
    # challenges below. The 2026-10-15 benchmark met 11 to 21 Google 429s a
    # batch from searches typed into google.com in this browser. WebSearch is
    # the CLI's built-in tool and this runner restricts none: SPEC passes no
    # --allowedTools and the generated settings deny nothing, so the tool
    # named here is one the agent has (test_captcha_avoid_note.py holds it).
    "For a web search, use the WebSearch tool, never a search engine's page in "
    "the browser. When a site has an official API for what is asked, prefer it "
    "to scraping its pages. And on any one site, keep to about one navigation a "
    "second. "
    # CAPTCHA-HANDOFF (BGD, 2026-10-23; part of #2058): the gate raises the
    # hand-off itself when it sees a challenge on the agent's page.
    "Never try to solve or get around a CAPTCHA yourself: the platform asks a "
    "person to solve it, so when the browser stops answering on one, run "
    "`swarm-handoff wait` and carry on once it returns. "
    "When a site shows a CAPTCHA, a bot check, a consent wall "
    "or a sign-in, do not try to get around it and do not switch to a "
    "different site than the one asked for: run `swarm-handoff request "
    "--reason \"<what the person should do>\" --expect <domain>`, then "
    "`swarm-handoff wait`, and carry on once control comes back -- reading the "
    "page again from the tab the person left (in Python a fresh `live()` and "
    "`swarm_browser.handoff_page(context)`; with the tools, browser_tabs to "
    "select it, then browser_snapshot), never from a handle, snapshot or value "
    "taken before the hand-off. Until then: "
    "wait, do not poll the browser -- run `swarm-handoff wait` "
    "in the foreground and nothing else (no `curl /json/version`, no "
    "snapshots in a loop); on exit 75 run it again, on exit 3 (nobody came) "
    "write where you stopped into $SWARM_HANDOFF/notes.md and stop -- the task "
    "parks until the person is ready. Only if the hand-off is refused (exit "
    "5), say so in your answer and then use an alternative, naming it. Save "
    "screenshots and other captures under $SWARM_ARTIFACTS_DIR so "
    "they are uploaded. "
    # QA-KIT (owner decision 2026-10-19): one line each, built from the
    # gate's own list so a preset added there is named here.
    "Device: `swarm-handoff device <preset>` makes the browser one of "
    + ", ".join(f"{p.name} ({p.width}x{p.height})" for p in DEVICE_PRESETS.values())
    + ". Theme: `swarm-handoff theme dark|light|system` sets the colour scheme "
    "every page is told to prefer (prefers-color-scheme); it holds across "
    "navigation and new tabs, and system lifts it. browser_resize is refused: "
    "the gate owns the browser's size, so the person watching sees the device "
    "the page is laid out for; pick a preset instead. Take screenshots with "
    "browser_take_screenshot without a filename: each is saved under "
    "$SWARM_ARTIFACTS_DIR/screenshots and comes back to you as an image; with a "
    "filename it lands in the checkout and you do not see it. "
    # QA-FLOWS (owner decision 2026-10-19, part of #1649): one line each.
    "Flow GIFs: `swarm-handoff record start <name>`, do the flow, then "
    "`swarm-handoff record stop` saves $SWARM_ARTIFACTS_DIR/flows/<name>.gif (a "
    "frame at each navigation and every second; it stops itself at 200 frames, "
    "8 MiB or 15 minutes). "
    "Mockups: `swarm-handoff serve <dir>` serves that directory at the "
    "http://127.0.0.1:<port>/ it prints, for the live browser to open in a tab "
    "of its own beside the app (file:// is blocked)."
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

#: QI-INSTRUCT (docs/design/qa-identity.md §4.7; owner decisions 2026-10-19,
#: part of #1649). Appended to `LIVE_BROWSER_NOTE` only when the task's input
#: names an identity the frozen catalogue offers (`qa_identity.requested`):
#: the gate then adds the QA account's IAP credential to console requests, so
#: the console is signed in. A task that names none is not told, because its
#: console is not signed in. The hand-off is refused there (`qa_identity_host`,
#: Q7): a person's own sign-in would hand the agent that person's workspaces.
#: The notice is the worker's (`qa_identity.NOTICE_NAME`), in the hand-off spool
#: whenever the browser holds no credential.
QA_IDENTITY_NOTE = (
    "This browser is already signed in to the SwarmCloud console as its QA "
    "identity, a service account that is a member of its own QA workspace: open "
    "console pages directly and you see what a member of that workspace sees, "
    "and nothing of any other workspace or of the admin screens. Do not run "
    "`swarm-handoff request` for a console page; it is refused there "
    "(`qa_identity_host`), because a person's own sign-in would give you their "
    "workspaces. If a console page shows a Google sign-in, never type a password "
    "or an address into it: read $SWARM_HANDOFF/" + _QA_NOTICE_NAME + ", which "
    "exists only while the browser is signed out of the console and says why. "
    "Finish what you can check without the console, and say in your answer that "
    "the console sign-in was unavailable, naming the cause it gives. Hand-offs "
    "for other sites work as above."
)

#: THE BROWSER TOOL (BSTATUS-API, contract request 77, owner decision
#: 2026-10-14 "Picker on Submit, Auto by default"). `input.browser_tool` names
#: one of `browser_tools.SELECTIONS`; `browser_tools.choose` says what that
#: registers, and the note NAMES THE SELECTED TOOL FIRST, then the live
#: browser's rules (`LIVE_BROWSER_NOTE`), then each registered tool's
#: strength, in the choice's order. Playwright MCP is registered on every
#: choice: alone for `playwright`, as the fallback for `dev-browser` and `jev`.
#:
#: jev-browser (JEV-IMAGE): the image's wrapper, off PATH, registered as an
#: MCP server only when it is installed and a TypeSafe key is in this
#: runner's environment (the key's delivery is lane JEV-RUNNER's).
JEV_MCP_BIN = "/opt/swarm/jev-browser/bin/jev-browser-mcp"
JEV_MCP_SERVER_NAME = "jev"
#: The decision API's address for an internal judgment backend, set by the
#: dispatcher only while the tailnet egress is on (scheduler.judgment
#: JEV_API_URL_ENV) and passed on by handoff.AGENT_PASSTHROUGH_ENV.
JEV_URL_ENV = "JEV_API_URL"
JEV_KEY_ENV = "TYPESAFE_API_KEY"
#: Run by absolute path with this runner's interpreter, like the stdin hook,
#: so the agent cannot edit the copy a later attempt runs (key-file path only).
JEV_LAUNCH = Path(__file__).resolve().parent / "jev_launch.py"
#: jev-browser's own switch: never download a Chromium on first use.
JEV_NO_INSTALL_ENV = "JEV_BROWSER_NO_INSTALL"

#: The first sentence of the live-browser note, per selection.
BROWSER_TOOL_LEAD = {
    "auto": (
        "Browser tools for this task (auto, chosen at submission): every tool "
        "available here -- pick the one that suits each step."
    ),
    "playwright": (
        "Browser tool for this task (chosen at submission): Playwright, the "
        "browser_* tools. Use only those."
    ),
    "dev-browser": (
        "Browser tool for this task (chosen at submission): dev-browser. Use it "
        "first; the browser_* tools are the fallback."
    ),
    "jev": (
        "Browser tool for this task (chosen at submission): jev, the jev MCP "
        "tools. Use them first; the browser_* tools are the fallback."
    ),
}

#: Each tool's strength, appended in the choice's order.
BROWSER_TOOL_STRENGTH = {
    "playwright": (
        "The browser_* tools (Playwright MCP) are best for step-by-step work: "
        "read the page with browser_snapshot, then act on the element it names."
    ),
    "jev": (
        "The jev tools (jev-browser MCP) are best for reading and judging a page "
        "as a whole -- finding what matters on it, checking it looks right. They "
        "send the page's text and element labels to TypeSafe (api.typesafe.ai) "
        "to do so; do not use them on a page whose content must not leave "
        "SwarmCloud."
    ),
}

#: The jev strength line for an INTERNAL judgment backend: nothing goes to TypeSafe.
BROWSER_TOOL_STRENGTH_JEV_INTERNAL = (
    "The jev tools (jev-browser MCP) are best for reading and judging a page "
    "as a whole -- finding what matters on it, checking it looks right."
)

#: Said when `jev` was chosen and cannot run on this attempt.
JEV_UNAVAILABLE_NOTE = {
    browser_tools.NO_KEY: (
        "jev was chosen for this task but no TypeSafe key is available to this "
        "attempt, so it is not registered: use the browser_* tools."
    ),
    browser_tools.NOT_INSTALLED: (
        "jev was chosen for this task but is not installed in this image, so it "
        "is not registered: use the browser_* tools."
    ),
    browser_tools.NO_ROUTE: (
        "jev was chosen for this task but is unavailable: this workspace judges "
        "with your internal decision API, and this attempt has no route to it "
        "(the tailnet egress is off), so it is not registered: use the browser_* "
        "tools."
    ),
}

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


def _spool_text(spool: str, name: str) -> str | None:
    if not spool:
        return None
    path = Path(spool) / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESUME_BYTES:
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def related_tests_note(repo_dir: Path | str | None) -> str | None:
    """RELATED_TESTS_NOTE when the checkout carries its script, else None."""
    if repo_dir is None or not (Path(repo_dir) / RELATED_TESTS_SCRIPT).is_file():
        return None
    return RELATED_TESTS_NOTE


def scratch_note_for(environ: Mapping[str, str]) -> str | None:
    """`cachedirs.scratch_note` for this attempt, or None without the worker's TMPDIR.

    On the GKE cache volume exactly when TMPDIR is outside the attempt tree
    (`Workspace.cache_on_own_volume`, read off the two paths the worker set).
    """
    tmpdir = environ.get("TMPDIR", "").strip()
    workspace = environ.get("SWARM_WORKSPACE", "").strip()
    if not tmpdir or not workspace:
        return None
    return scratch_note(own_volume=not Path(tmpdir).is_relative_to(workspace))


def exploration_note(*, graph: bool, children: str) -> str | None:
    """The one exploration line, naming only what this attempt has, or None."""
    clauses = []
    if graph:
        clauses.append(EXPLORATION_GRAPH_CLAUSE)
    if children:
        clauses.append(EXPLORATION_CHILDREN_CLAUSE)
    if not clauses:
        return None
    return f"Explore cheaply: {'; and '.join(clauses)}."


def egress_notice(spool: str) -> str | None:
    """EGRESS-GATE: the worker's notice that this attempt's proxied browser has
    no egress (`handoff.EGRESS_NOTICE_NAME`, written by `HandoffPath.prepare`
    before this runner starts, and absent otherwise), or None."""
    return _spool_text(spool, EGRESS_NOTICE_NAME)


def qa_target_notice(spool: str) -> str | None:
    """VQ4-WIRE: why the gate did not sign the agent in to its QA target (a
    pending host, a challenge; `qa_target.NOTICE_NAME`, written by
    `QaTargetSignIn` before this runner starts, and absent otherwise), or
    None. A cause and what to do, never the credential: the runner reads
    this file and not the secret."""
    return _spool_text(spool, QA_TARGET_NOTICE_NAME)


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
    """Write the CLI's `--settings` file and its hook into `directory`; return the file.

    On a live-browser attempt -- the one kind the lifecycle sets SWARM_HANDOFF
    for, read here from the same environment `body` passes it on from -- the
    file also registers `gate_wait_hook.py` after every tool, so the agent's
    next tool result says when the gate has asked a person to solve a CAPTCHA
    (CAPTCHA-HANDOFF-2, BGD 2026-10-23; part of #2058). Any other attempt has
    no spool for it to read, and gets no hook.
    """
    if directory.is_symlink():
        directory.unlink()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    hook = directory / "refuse-background.py"
    _write_private(hook, _HOOK)
    # A new settings file is a new attempt: an earlier attempt's guard run
    # is not this one's to compare with.
    guards_state = directory / GUARDS_STATE_NAME
    if guards_state.is_symlink() or guards_state.exists():
        guards_state.unlink()
    settings: dict[str, Any] = {
        "env": {
            NO_BACKGROUND_ENV: "1",
            # Observer P21, owner decision 2026-10-06: the CLI's 2-minute
            # default Bash timeout killed a foreground command in 3 of 19
            # chunk-2 steps, while the lane briefs allow any command up to 10
            # minutes. Both at 10 minutes, so the default and the most a call
            # may ask for match the briefs. Strings: the settings' env is.
            "BASH_DEFAULT_TIMEOUT_MS": "600000",
            "BASH_MAX_TIMEOUT_MS": "600000",
            # LANE CAPS (#2524 item 6): a whole test transcript in one Bash
            # result is re-read on every later turn; past this the CLI keeps
            # the head and tail. related-tests.sh and changed-guards.sh print
            # a tail and keep the full output in a log.
            "BASH_MAX_OUTPUT_LENGTH": BASH_MAX_OUTPUT,
        },
        # LANE CAPS (#2524 item 6): the repository's `.mcp.json` server is the
        # operator's console; no lane in the measured week called it, and its
        # ~100 tool schemas sat in every turn's prefix. The platform's own
        # servers come from `--mcp-config`, which this does not touch.
        "disabledMcpjsonServers": list(REPO_MCP_SERVERS_OFF),
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
                {
                    # Warns, never blocks: no permission decision, exit 0,
                    # and fails open (guards_repeat_hook.py).
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{shlex.quote(sys.executable)} -I {shlex.quote(str(GUARDS_REPEAT_HOOK))} "
                                       f"{shlex.quote(str(guards_state))}",
                            "timeout": 30,
                        }
                    ],
                },
            ]
        },
    }
    post_tool_use: list[dict[str, Any]] = []
    spool = os.environ.get(HANDOFF_ENV, "").strip()
    if spool:
        post_tool_use += [
            {
                # Every tool: the call the pause blocks is a browser_* tool,
                # a Bash `swarm_browser` script or anything else. Two small
                # reads, so a short timeout; it fails open on any error.
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{shlex.quote(sys.executable)} -I {shlex.quote(str(GATE_WAIT_HOOK))} "
                                   f"{shlex.quote(spool)}",
                        "timeout": 10,
                    }
                ],
            }
        ]
    budget_note = os.environ.get(BUDGET_NOTE_ENV, "").strip()
    if budget_note:
        # A budgeted attempt only (#2459): after every tool, one rename and
        # at most one small read; fails open, and tells the note once.
        post_tool_use.append({
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{shlex.quote(sys.executable)} -I {shlex.quote(str(BUDGET_NOTE_HOOK))} "
                               f"{shlex.quote(budget_note)}",
                    "timeout": 10,
                }
            ],
        })
    if post_tool_use:
        settings["hooks"]["PostToolUse"] = post_tool_use
    path = directory / "settings.json"
    _write_private(path, json.dumps(settings, indent=2) + "\n")
    return path


#: The relay file is the worker's (`mcp_attach.McpAttachment.start`): a token,
#: at most MAX_MCP_SERVERS servers and their descriptions. Anything larger is
#: not its.
MAX_RELAY_FILE_BYTES = 16 * 1024
#: `secrets.token_urlsafe(32)` (`mcp_relay.Relay`), with room either side.
_RELAY_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_RELAY_URL = re.compile(
    re.escape(f"http://{MCP_RELAY_HOST}:") + r"[1-9][0-9]{0,4}" + re.escape(MCP_RELAY_PATH)
    + r"(?P<name>[a-z][a-z0-9-]{0,30})")
#: The header the relay checks the local token in, bearer scheme (§4.8).
MCP_RELAY_HEADER = "Authorization"


@dataclasses.dataclass(frozen=True)
class RelayServer:
    """One attached server as a runner is handed it: never a credential."""

    name: str
    url: str
    description: str = ""


def relay_servers(environ: Any = None) -> tuple[str, list[RelayServer]]:
    """`(local token, servers)` from the worker's relay file, or `("", [])`
    when the attempt was attached none (`SWARM_MCP_RELAY` unset).

    The file is the worker's, in `private/`, but it is read like anything a
    runner is pointed at: no link followed, bounded, every field checked. A
    URL that is not the relay's loopback path for that name, a name a
    platform server owns, or a file that cannot be read is a RunnerFailure:
    the worker attached these servers because the caller named them, and an
    agent run without one would build on knowledge it was told it had (§4.7
    step 2), so it does not start.
    """
    env = os.environ if environ is None else environ
    raw_path = str(env.get(MCP_RELAY_ENV, "") or "").strip()
    if not raw_path:
        return "", []
    path = Path(raw_path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RELAY_FILE_BYTES:
            raise RunnerFailure("mcp_relay_unreadable: not the worker's relay file")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise RunnerFailure("mcp_relay_unreadable: the relay file did not parse") from None
    token = data.get("token") if isinstance(data, dict) else None
    items = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(token, str) or not _RELAY_TOKEN.fullmatch(token):
        raise RunnerFailure("mcp_relay_unreadable: no local token")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_MCP_SERVERS:
        raise RunnerFailure("mcp_relay_unreadable: no servers")
    servers: list[RelayServer] = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        match = _RELAY_URL.fullmatch(url) if isinstance(url, str) else None
        if (not isinstance(name, str) or not REGISTERED_MCP_SERVER_NAME.fullmatch(name)
                or name in RESERVED_MCP_SERVER_NAMES
                or name.startswith(RESERVED_MCP_SERVER_PREFIX)
                or match is None or match.group("name") != name
                or any(s.name == name for s in servers)):
            raise RunnerFailure("mcp_relay_unreadable: a server is not the relay's")
        description = item.get("description")
        description = " ".join(description.split()) if isinstance(description, str) else ""
        servers.append(RelayServer(name=name, url=url, description=description))
    return token, servers


def mcp_servers_note(servers: list[RelayServer]) -> str:
    """The prompt's line per attached server (§4.7 step 4): its name, its
    registered description, and where its tools are listed."""
    lines = []
    for server in servers:
        about = f" ({server.description.rstrip('.')})" if server.description else ""
        lines.append(
            f"The MCP server `{server.name}`{about} is connected for this task: its "
            f"tools are listed under the MCP server `{server.name}`.")
    return "\n".join(lines)


def write_mcp_config(
    directory: Path,
    endpoint: str | None = None,
    *,
    git_spool: str | None = None,
    search_spool: str | None = None,
    state_spool: str | None = None,
    jev: bool = False,
    jev_key_file: str | None = None,
    graph_snapshot: str | None = None,
    graph_workdir: str | None = None,
    graph_served: tuple[str, str, str] | None = None,
    relay: tuple[str, list[RelayServer]] | None = None,
    screenshots_dir: str | None = None,
    lightpanda_server: dict[str, Any] | None = None,
) -> Path:
    """Write the CLI's `--mcp-config` and return it: Playwright MCP on the live
    browser when `endpoint` is given (writing what it names itself into
    `screenshots_dir` when that is given, `SCREENSHOTS_DIR_NAME`), and jev-browser beside it when `jev` is
    (BSTATUS-API; with `jev_key_file`, the TypeSafe backend's key file PATH, never
    the key: `jev_launch.py` reads it into the server's own environment; without
    it, an internal backend: the wrapper started directly, no key), the `swarm-git` shim when `git_spool` is, the
    `swarm-search` shim when `search_spool` is, the `swarm-state` shim when
    `state_spool` is (docs/design/agent-autonomy.md §3.4), `swarm-graph` when
    `graph_snapshot` (the staged directory) is, or the graph shim when
    `graph_served` (`graphstage.served_env`: pin, ticket file, URL) is, and
    each of the caller's registered servers when `relay` (`relay_servers`) is:
    an `http` entry at the relay's loopback URL whose one header carries the
    LOCAL token, never the caller's credential (module docstring), and
    `lightpanda_server` (`lightpanda.mcp_server`) as the `lightpanda` server
    when it is given: claude-code-lightpanda's, which has no `endpoint` and so
    no `playwright` server (docs/design/lightpanda-runtime.md §6.2).

    Playwright MCP connects with its client tag on the endpoint
    (`browser_tools.tag_endpoint`), so the gate can say which tool drove the
    browser last; jev's wrapper adds its own tag.

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
        playwright_args = ["--cdp-endpoint", browser_tools.tag_endpoint(endpoint, "playwright")]
        if screenshots_dir:
            playwright_args += ["--output-dir", screenshots_dir]
        servers[MCP_SERVER_NAME] = {
            "type": "stdio",
            "command": PLAYWRIGHT_MCP_BIN,
            "args": playwright_args,
        }
        if jev:
            # No `env`: the wrapper reads SWARM_CDP_ENDPOINT and the key from
            # the CLI's environment, so no key is ever written to this file.
            if jev_key_file:
                servers[JEV_MCP_SERVER_NAME] = {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-I", str(JEV_LAUNCH), jev_key_file, JEV_MCP_BIN],
                    "env": {CDP_ENDPOINT_ENV: endpoint, JEV_NO_INSTALL_ENV: "1"},
                }
            else:
                servers[JEV_MCP_SERVER_NAME] = {"type": "stdio", "command": JEV_MCP_BIN, "args": []}
    if lightpanda_server:
        servers[lightpanda.MCP_SERVER_NAME] = lightpanda_server
    if git_spool:
        command, *args = git_shim_argv(git_spool)
        servers[GIT_MCP_SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    if search_spool:
        command, *args = search_shim_argv(search_spool)
        servers[SEARCH_MCP_SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    if state_spool:
        command, *args = state_shim_argv(state_spool)
        servers[STATE_MCP_SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    if graph_served:
        pin, ticket_file, url = graph_served
        command, *args = graphstage.shim_argv(pin, ticket_file, graph_workdir)
        servers[graphstage.SERVER_NAME] = {"type": "stdio", "command": command, "args": args,
                                           "env": {graphstage.URL_ENV: url}}
    elif graph_snapshot:
        command, *args = graphstage.server_argv(graph_snapshot, graph_workdir)
        servers[graphstage.SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    if relay:
        token, attached = relay
        for server in attached:
            servers[server.name] = {"type": "http", "url": server.url,
                                    "headers": {MCP_RELAY_HEADER: f"Bearer {token}"}}
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
    # The context ceiling (TOKEN-CEILING, BGD 2026-10-27, #2524): a session
    # past 200k tokens of context or 150 tool calls is stopped at a turn
    # boundary and the task carried on in a fresh session from a handoff,
    # never `--resume` (`ceiling`).
    context_ceiling=True,
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


def live_browser_note(choice: browser_tools.Choice, *, search: bool,
                      jev_internal: bool = False, qa_identity: str | None = None) -> str:
    """The note an agent with the live browser is given: the selected tool
    first, then the live browser's rules, the QA identity's when the task
    named one, then each tool's strength."""
    parts = [BROWSER_TOOL_LEAD[choice.selected], LIVE_BROWSER_NOTE]
    if qa_identity:
        parts.append(QA_IDENTITY_NOTE)
    for tool in choice.tools:
        if tool == "jev" and jev_internal:
            parts.append(BROWSER_TOOL_STRENGTH_JEV_INTERNAL)
        else:
            parts.append(LIVE_BROWSER_DEV_BROWSER_NOTE if tool == "dev-browser" else BROWSER_TOOL_STRENGTH[tool])
    if choice.selected == "jev" and not choice.jev_enabled and choice.jev_reason in JEV_UNAVAILABLE_NOTE:
        parts.append(JEV_UNAVAILABLE_NOTE[choice.jev_reason])
    if search:
        parts.append(LIVE_BROWSER_SEARCH_NOTE)
    return " ".join(parts)


def _blocks(event: Any) -> list[dict[str, Any]]:
    message = event.get("message") if isinstance(event, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def screenshot_images(events: Any) -> dict[str, int]:
    """How many `browser_take_screenshot` calls the stream carries, and how many
    came back to the model with an image block in their tool_result (QA-KIT).

    `events` is the CLI's stream-json, one decoded event each: a call is a
    `tool_use` block of `SCREENSHOT_TOOL` in an assistant message, its result a
    `tool_result` block with the same id in a later user message, whose
    `content` is a list of blocks. An `image` block there is what the model
    was given. Counts only, never the image.
    """
    calls: set[str] = set()
    seen: set[str] = set()
    for event in events if isinstance(events, list) else []:
        for block in _blocks(event):
            if block.get("type") == "tool_use" and block.get("name") == SCREENSHOT_TOOL:
                calls.add(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in calls:
                content = block.get("content")
                if isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "image" for c in content):
                    seen.add(str(block.get("tool_use_id")))
    return {"taken": len(calls), "seen_as_image": len(seen)}


def _stream_events(path: Path) -> list[Any] | None:
    """The CLI's captured stdout, decoded, a line at a time: only the lines
    that can hold a tool call or its result, since with screenshots in it the
    capture is mostly base64. None when the file cannot be read -- which is
    not a stream with no calls in it (`browser_use`)."""
    events: list[Any] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return None
    return events


def _stream_screenshots(path: Path) -> dict[str, int]:
    """`screenshot_images` over the CLI's captured stdout (`_stream_events`)."""
    return screenshot_images(_stream_events(path) or [])


#: BROWSER-USE-FLAG (part of #2322): what counts as the agent using its
#: browser. The 2026-10-25 audit found 49 browser tasks that never touched the
#: browser, and only by reading agent logs by hand: nothing but screenshots
#: was counted. A call is a `tool_use` of a browser MCP server this runner
#: registers (Playwright, jev), or a Bash command that drives the browser or
#: the gate: `swarm-handoff serve|device|theme|record`, `dev-browser`, or a
#: script through `swarm_browser` (LIVE_BROWSER_NOTE names each).
BROWSER_TOOL_PREFIXES = (f"mcp__{MCP_SERVER_NAME}__", f"mcp__{JEV_MCP_SERVER_NAME}__")
BROWSER_COMMAND = re.compile(
    r"\bswarm-handoff\s+(?:serve|device|theme|record)\b|\bdev-browser\b|\bswarm_browser\b"
)
#: The CLI's shell tool, whose `input.command` is read for `BROWSER_COMMAND`.
BASH_TOOL = "Bash"
#: An image the agent produced, by suffix: some agents render through a
#: script with no tool call at all, and the image is all that shows it.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def browser_calls(events: Any) -> dict[str, int]:
    """How many browser calls the CLI's stream-json carries (`BROWSER_TOOL_PREFIXES`,
    `BROWSER_COMMAND`): `tool` the browser MCP tools' calls, `command` the
    Bash commands that drive the browser. Counts only, never the input."""
    tool = command = 0
    for event in events if isinstance(events, list) else []:
        for block in _blocks(event):
            if block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            if name.startswith(BROWSER_TOOL_PREFIXES):
                tool += 1
            elif name == BASH_TOOL:
                tool_input = block.get("input")
                text = tool_input.get("command") if isinstance(tool_input, dict) else None
                if isinstance(text, str) and BROWSER_COMMAND.search(text):
                    command += 1
    return {"tool": tool, "command": command}


def produced_images(directory: Path) -> int:
    """How many image files (`IMAGE_SUFFIXES`) are under `directory`, the
    task's artifacts: screenshots, flow GIFs and anything a script rendered.
    Links are not followed; a directory that cannot be read counts 0."""
    count = 0
    try:
        for path in directory.rglob("*"):
            if path.suffix.lower() in IMAGE_SUFFIXES and not path.is_symlink() and path.is_file():
                count += 1
    except OSError:
        pass
    return count


def browser_use(stream: Path, artifacts: Path, *, complete: bool = True) -> dict[str, Any]:
    """The counts a live-browser attempt reports as `browser_use` (BROWSER-USE-FLAG).

    `read` is false when the stream could not be read, and the call counts are
    then None, never 0: an unread stream is not one with no calls. `complete`
    is false when the capture was cut, and the counts are then a lower bound.
    The verdict is the worker's (`lifecycle.browser_use_record`), which also
    knows whether the attempt had a browser at all.
    """
    events = _stream_events(stream)
    calls = browser_calls(events) if events is not None else None
    return {
        "read": events is not None,
        "complete": complete,
        "tool_calls": None if calls is None else calls["tool"] + calls["command"],
        "mcp_calls": None if calls is None else calls["tool"],
        "command_calls": None if calls is None else calls["command"],
        "images": produced_images(artifacts),
    }


def prepare_test_browsers(image_browsers: Path, cache_home: Path) -> Path | None:
    """The writable browsers directory for test processes, seeded; None if it cannot be.

    `cache_home/TEST_BROWSERS_DIR_NAME`, with a link to each of the image's
    revisions (`image_browsers`, read-only) so the image's build is found
    without a copy. Dot entries (Playwright's `.links`) are not linked: an
    install writes its own here. An entry already present -- a link from an
    earlier call, a revision a test run installed -- is kept. Never raises:
    an attempt without the directory runs as it did before PW-TESTRUNNER.
    """
    target = cache_home / TEST_BROWSERS_DIR_NAME
    try:
        entries = sorted(image_browsers.iterdir())
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in entries:
            if entry.name.startswith((".", "__")) or not entry.is_dir():
                continue
            link = target / entry.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(entry, target_is_directory=True)
    except OSError as exc:
        StructuredLogger(stream=sys.stderr, component="claude-code-runner").warning(
            "test_browsers_unavailable", error_type=type(exc).__name__)
        return None
    return target


def live_browser_refusal(env: dict[str, str], browsers: str, endpoint: str) -> str | None:
    """Why a live-browser attempt's agent must not start, or None (BROWSER-START).

    Not a live-browser attempt (no SWARM_HANDOFF): None, whatever else is
    missing. Checked before the CLI starts, so the agent never runs with the
    image's own headless Chromium where the live browser was promised.
    """
    if not env.get(HANDOFF_ENV):
        return None
    if not endpoint:
        return f"{LIVE_BROWSER_UNAVAILABLE}: {CDP_ENDPOINT_ENV} is empty on a live-browser attempt"
    if not browsers:
        return f"{LIVE_BROWSER_UNAVAILABLE}: {BROWSERS_PATH_ENV} is unset, so no browser tool is registered"
    return None


def body(ctx: RunnerContext) -> dict[str, Any]:
    extra: list[str] = []
    max_turns = ctx.payload.get("max_turns")
    if max_turns is not None:
        extra += ["--max-turns", str(int(max_turns))]
    # claude-code-lightpanda (docs/design/lightpanda-runtime.md §6.2): the
    # profile the worker named, never input. Its three denied tools go before
    # `--settings`, because the CLI's flag is variadic.
    on_lightpanda = lightpanda.is_lightpanda(os.environ)
    if on_lightpanda:
        extra += lightpanda.disallowed_tools_args()
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
    # The state reads' spool (AU-4), likewise: set only for an attempt offered them.
    state_spool = os.environ.get(STATE_SPOOL_ENV, "").strip()
    if state_spool:
        env[STATE_SPOOL_ENV] = state_spool
    # The staged graph (`graphstage.prepare`): its directory goes to the agent
    # for CONTEXT.md, the snapshot only to the server's argv.
    graph_snapshot = os.environ.get(graphstage.SNAPSHOT_ENV, "").strip()
    # Or the served graph's pin and ticket file (GS-WORKER): to the shim's
    # argv and env only; the agent gets the context directory, as above.
    graph_served = graphstage.served_env(os.environ)
    if (graph_snapshot or graph_served) and os.environ.get(graphstage.CONTEXT_ENV, "").strip():
        env[graphstage.CONTEXT_ENV] = os.environ[graphstage.CONTEXT_ENV].strip()
    live_endpoint = ""
    jev = False
    jev_key_file = ""
    jev_internal = False
    # The image's browser only: the cache's PLAYWRIGHT_BROWSERS_PATH
    # (`cachedirs.cache_env`, #2436) holds none, and is handed on below.
    browsers = image_browsers_path(os.environ)
    cache_browsers = "" if browsers else os.environ.get(BROWSERS_PATH_ENV, "").strip()
    if cache_browsers:
        env[BROWSERS_PATH_ENV] = cache_browsers
    endpoint = os.environ.get(CDP_ENDPOINT_ENV, "").strip()
    lightpanda_env: dict[str, str] = {}
    if on_lightpanda:
        # Lightpanda, never Chromium and never the live browser: no
        # SWARM_CDP_ENDPOINT, no browser note, no `browser.json`
        # (`browser_tools.write_state` is in the endpoint branch below), and
        # the agent's CDP is the `serve` child's, on 9223.
        endpoint = ""
        browsers = ""
        env.pop(CDP_ENDPOINT_ENV, None)
        env[lightpanda.CDP_ENV] = lightpanda.CDP_URL
        cache_home = os.environ.get(CACHE_HOME_ENV, "").strip()
        lightpanda_env = lightpanda.child_env(
            os.environ, Path(cache_home) if cache_home else Path(ctx.work_dir) / ".cache")
    prompt = ctx.payload.get("prompt")
    spec = SPEC
    unstaged = graphstage.unstaged_note(os.environ)
    if unstaged and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{unstaged}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # The standing test rule, where the checkout has its helper (RELATED-TESTS).
    narrow = related_tests_note(ctx.repo_dir)
    if narrow and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{narrow}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # Where big scratch data goes (#2436): every repository step, after the
    # test rule and before the browser note.
    scratch = scratch_note_for(os.environ) if ctx.repo_dir is not None else None
    if scratch and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{scratch}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # Where to explore first, naming only the tools this attempt has (LANE CAPS).
    explore = exploration_note(graph=bool(graph_snapshot or graph_served),
                               children=os.environ.get(CHILDREN_ENV, "").strip())
    if explore and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{explore}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # The caller's registered MCP servers, through the worker's relay
    # (MCP-RUNNERS): the local token and loopback URLs only, and a line each
    # in the prompt. None unless the worker attached one.
    relay = relay_servers()
    if relay[1] and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{mcp_servers_note(relay[1])}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # A resumed live-browser attempt: what the earlier one did, after the
    # caller's prompt (RESUME-PROGRESS; `resume_summary`). A new dict, as below.
    resumed = resume_summary(env.get(HANDOFF_ENV, ""))
    if resumed and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{resumed}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # A proxied browser with no egress (EGRESS-GATE): the agent is told before
    # it tries, rather than reading connection errors.
    stopped = egress_notice(env.get(HANDOFF_ENV, ""))
    if stopped and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{stopped}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    # A QA target the gate could not sign in to (VQ4-WIRE): the agent learns
    # it at start, and checks the public screens rather than the login page.
    signed_out = qa_target_notice(env.get(HANDOFF_ENV, ""))
    if signed_out and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{signed_out}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    refusal = live_browser_refusal(env, browsers, endpoint)
    if refusal:
        raise RunnerFailure(f"{refusal}; the agent was not started")
    if on_lightpanda and isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt.rstrip()}\n\n{lightpanda.LIGHTPANDA_NOTE}"
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
            # The browser tool the task named (contract request 77), and what
            # this attempt registers for it; the gate is told through the spool.
            jev_key_file, jev_internal = _jev_backend_key()
            choice = browser_tools.choose(
                ctx.payload.get("browser_tool"),
                jev_installed=Path(JEV_MCP_BIN).is_file(),
                # ONE gate: an internal backend needs no key but needs its
                # route; typesafe/unset needs the worker's key file (module
                # docstring, THE JEV GATE).
                jev_key=jev_internal or bool(jev_key_file),
                jev_route=not jev_internal or bool(os.environ.get(JEV_URL_ENV, "").strip()),
            )
            jev = choice.jev_enabled
            if choice.jev_reason == browser_tools.NO_ROUTE:
                StructuredLogger(stream=sys.stderr, component="claude-code-runner").warning(
                    "jev_unavailable", reason="no route to the decision API (JEV_API_URL unset)")
            if not jev:
                jev_key_file, jev_internal = "", False
            browser_tools.write_state(env.get(HANDOFF_ENV, ""), choice)
            # The QA identity (QI-INSTRUCT): read as the worker reads it, so
            # the agent is told the console is signed in exactly when the
            # worker asks the gate to sign it in.
            note = live_browser_note(
                choice, search=bool(search_spool), jev_internal=jev_internal,
                qa_identity=qa_identity_requested({"input": ctx.payload}))
        else:
            env[BROWSERS_PATH_ENV] = browsers
        # PW-TESTRUNNER: a test process's own browsers directory, named to the
        # agent but never its default (module docstring).
        cache_home = os.environ.get(CACHE_HOME_ENV, "").strip()
        test_browsers = prepare_test_browsers(
            Path(browsers), Path(cache_home) if cache_home else Path(ctx.work_dir) / ".cache")
        if test_browsers is not None:
            env[TEST_BROWSERS_ENV] = str(test_browsers)
            note = f"{note} {TEST_RUNNER_NOTE}"
        # A new dict on the same context: `run_runner` holds this object.
        if isinstance(prompt, str) and prompt.strip():
            ctx.payload = {**ctx.payload, "prompt": f"{prompt.rstrip()}\n\n{note}"}
    # ONE `--mcp-config` for every server this attempt has (module docstring).
    if (live_endpoint or git_spool or search_spool or state_spool or graph_snapshot
            or graph_served or relay[1] or lightpanda_env):
        mcp = write_mcp_config(
            Path(ctx.work_dir) / SETTINGS_DIR_NAME, live_endpoint or None,
            git_spool=git_spool or None, search_spool=search_spool or None,
            state_spool=state_spool or None,
            jev=jev, jev_key_file=jev_key_file or None,
            graph_snapshot=graph_snapshot or None,
            graph_workdir=(str(ctx.repo_dir) if (graph_snapshot or graph_served) and ctx.repo_dir
                           else None),
            graph_served=graph_served, relay=relay if relay[1] else None,
            screenshots_dir=(str(Path(ctx.artifacts_dir) / SCREENSHOTS_DIR_NAME)
                             if live_endpoint else None),
            lightpanda_server=lightpanda.mcp_server(lightpanda_env) if lightpanda_env else None)
        # BROWSER-START: the live browser's tool is in the config the CLI
        # reads, or the agent does not start.
        if env.get(HANDOFF_ENV) and MCP_SERVER_NAME not in json.loads(mcp.read_text())["mcpServers"]:
            raise RunnerFailure(f"{LIVE_BROWSER_UNAVAILABLE}: Playwright MCP is not registered; "
                                "the agent was not started")
        extra += ["--mcp-config", str(mcp)]
    supervisor: lightpanda.Supervisor | None = None
    try:
        if lightpanda_env:
            # Started here, in the runner the worker starts only after its
            # fencing check, and in the runner's process group, so it ends with
            # the attempt; stopped below whatever the CLI did.
            supervisor = lightpanda.Supervisor(
                lightpanda.serve_argv(lightpanda_env), lightpanda_env,
                log=StructuredLogger(stream=sys.stderr, component="claude-code-runner").warning)
            supervisor.start()
        result = run_cli_agent(ctx, spec, extra_args=extra, extra_env=env)
        if live_endpoint and isinstance(result, dict):
            # QA-KIT: what the model was given of its screenshots, read off the
            # stream, so a report can say what it saw. `complete` is false when
            # the capture was cut, and the counts are then a lower bound.
            stream = Path(ctx.artifacts_dir) / cli_stream_files(spec).stdout
            seen = _stream_screenshots(stream)
            result["browser_screenshots"] = {**seen, "complete": not result.get("stdout_truncated")}
            # BROWSER-USE-FLAG: every browser call and every image, beside
            # the screenshots, for the worker's `browser_used` verdict.
            result["browser_use"] = browser_use(
                stream, Path(ctx.artifacts_dir), complete=not result.get("stdout_truncated"))
        return result
    finally:
        if supervisor is not None:
            supervisor.stop()
        # The worker's key file, whether or not jev was registered: the agent
        # is done, so nothing reads it again (`jev_launch.py`).
        _remove_jev_key_file()


def _jev_backend_key() -> tuple[str, bool]:
    """`(key file, internal)` from the workspace's judgment backend, before the
    task's `browser_tool` is applied (`browser_tools.choose`).

    `internal-jev` / `internal-laya`: ("", True), no key and no file. Otherwise
    (`typesafe`, or unset on an old pod) the worker's key file path, or "" when
    the task has no key. A missing wrapper is logged `jev_unavailable` when
    jev would otherwise be possible; `choose` reports `not_installed`.
    """
    internal = os.environ.get(JEV_BACKEND_ENV, "").strip() in JEV_INTERNAL_BACKENDS
    path = "" if internal else os.environ.get(JEV_KEY_FILE_ENV, "").strip()
    if (internal or path) and not Path(JEV_MCP_BIN).is_file():
        StructuredLogger(stream=sys.stderr, component="claude-code-runner").warning(
            "jev_unavailable", binary=JEV_MCP_BIN, reason="not installed in this image")
    return path, internal


def _remove_jev_key_file() -> None:
    path = os.environ.get(JEV_KEY_FILE_ENV, "").strip()
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> int:
    return run_runner(body, name="claude-code")


if __name__ == "__main__":
    raise SystemExit(main())
