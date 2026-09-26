"""An MCP server, so SwarmCloud tasks sit beside local subagents in a session.

WHY THE PROTOCOL IS SPOKEN DIRECTLY RATHER THAN THROUGH AN SDK. This runs on an
operator's laptop and its whole job is to remove friction; requiring a `pip
install` before anyone can look at their own platform would add some. MCP over
stdio is newline-delimited JSON-RPC, which is about eighty lines, and those
eighty lines mean `uv run swarm-mcp` works in a fresh checkout.

WHAT THIS SERVER DELIBERATELY DOES NOT DO: stream. An MCP tool call returns
exactly once, so there is no way for a tool to print a log line while an agent
is still writing it. `swarm_wait` therefore blocks and returns the outcome.
Pretending otherwise -- a `swarm_watch` tool that returned a 900-line transcript
and called it live -- would be the kind of almost-true that wastes an afternoon.

WHAT IT DOES INSTEAD, and why that is not the same thing. `swarm_follow` is a
RESUMABLE CURSOR, not a stream: it returns what is new since a cursor and hands
back the next one, so a session polls it between other work and narrates
progress. That shape was forced twice over -- an MCP call returns once, and
`client.py:25` records that a long-lived tail inside a subprocess dies with a
401 that looks like a permission problem -- so nothing here holds a connection
open. Before it existed, every description in this file pointed at `swarm
tail`, a terminal command the model cannot run, which meant the one question a
session asks of a remote agent ("what is it doing?") had no answer it could
reach.

Every tool here is a thin wrapper over `cli`/`patches`/`workflows`. Two
implementations of "what does integrate mean" is how a CLI and a tool quietly
start disagreeing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from swarm_common.profiles import RESOURCE_CLASSES

from . import profiles as catalogue
from . import workflows
from .client import TERMINAL, SwarmClient, SwarmError, task_id_of
from .follow import DEFAULT_EVENT_PAGE, DEFAULT_LOG_BUDGET, follow, follow_command
from .invocation import terminal_command
from .patches import (
    apply_patch,
    describe_task,
    download,
    explain_absence,
    explain_failure,
    integrate,
    masked_counts,
    patch_uri,
)

PROTOCOL_VERSION = "2024-11-05"

#: A runner's DECLARED inputs, for `swarm_dispatch` and a `swarm_workflow` step
#: (#142). An open object in the schema because what it may hold depends on the
#: profile named beside it; `profiles.check_inputs` refuses anything the
#: profile does not declare, before anything travels. The profiles that declare
#: any are read from the frozen catalogue, which swarm-api enforces too.
_INPUTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Data for the runner beside the prompt -- ONLY the inputs the named "
        "profile declares, which swarm_profiles lists under `inputs`; any other "
        "key, or any input at all for a profile that declares none, is refused. "
        f"Declared today by: {', '.join(catalogue.declaring()) or 'no profile'}"
        " -- e.g. {\"sleep_seconds\": 120} keeps a mock step RUNNING long enough "
        "to cancel. Never an image, a command, a resource spec, a backend or a model."
    ),
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_dispatch",
        "description": (
            "Run an agent in SwarmCloud instead of locally. Returns a task id "
            "immediately; the agent runs remotely on its own tenant identity. "
            "Use this the way you would spawn a local subagent, then call "
            "swarm_follow with the returned id to watch it work, and "
            "swarm_wait or swarm_result for the outcome."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The agent's instructions."},
                "profile": {
                    "type": "string",
                    "default": "claude-code",
                    "description": (
                        "A runner profile BY NAME from the frozen catalogue. The "
                        "image, command and resource class come from the name; "
                        "they cannot be supplied here."
                    ),
                },
                "repo": {"type": "string", "description": "Repository to clone (https or ssh)."},
                "ref": {"type": "string", "description": "Branch, tag or commit."},
                "label": {"type": "string", "description": "A short name, for the UI."},
                "inputs": _INPUTS_SCHEMA,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "swarm_profiles",
        "description": (
            "The runner profiles this platform will accept BY NAME, with what "
            "each one is: whether it can be dispatched at all and, when it "
            "cannot, why; which backend it lands on; its cpu, memory and "
            "workspace size; its timeout; and whether it needs a provider "
            "credential. Read this before dispatching rather than guessing a "
            "name -- a name the catalogue does not hold is refused, and a "
            "profile that is `available: false` is refused WITH a reason that "
            "names the remedy, so it is never a typo to go hunting for.\n"
            "\n"
            "There is no image and no command here, and that is deliberate "
            "rather than missing. A caller names a profile and the image, the "
            "command and the resource spec follow from the name; they are not "
            "part of the vocabulary a caller has, so this tool does not put "
            "them in front of one. Nothing in this plugin can supply them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_wait",
        "description": (
            "Block until the given tasks reach a terminal state, then return what "
            "each produced: commits, files changed, patch uri and pull request. "
            "This does NOT stream -- an MCP tool returns once, so it is silent "
            "for as long as the work takes. To show progress while it runs, "
            "poll swarm_follow instead and call this at the end."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "integer", "default": 3600},
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "swarm_follow",
        "description": (
            "What these agents have produced SINCE a cursor: new events and new "
            "log lines, plus the cursor to pass back next time. This is how you "
            "watch remote work the way you watch a local subagent -- call it, "
            "narrate what came back, do something else, call it again. It "
            "returns immediately and holds nothing open, and nothing is ever "
            "returned twice, so polling it is cheap and safe. Pass every task "
            "you are running in ONE call; the fan-out case is five or six "
            "agents at once. READ THREE FIELDS BEFORE TRUSTING THE OUTPUT: "
            "`truncated` -- when true this is NOT all the output and "
            "`truncation` names what was withheld and how to get it; each "
            "task's `read` -- `failed` means the task could not be read at all, "
            "which is not the same as an agent that printed nothing; and each "
            "stream's `status` -- `absent`, `unreadable` and `up_to_date` are "
            "three different facts and none of them mean 'the agent is idle'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every task to follow, in one call.",
                },
                "cursor": {
                    "type": "object",
                    "description": (
                        "The `cursor` object a previous swarm_follow returned, "
                        "passed back VERBATIM. Omit it on the first call, or for "
                        "a task id it does not mention, and that task is read "
                        "from the beginning."
                    ),
                },
                "max_log_bytes": {
                    "type": "integer",
                    "default": DEFAULT_LOG_BUDGET,
                    "description": (
                        "How much log text this call may return in total, across "
                        "every task. Raise it when `truncation` says a stream was "
                        "not reached; lower it when following many agents at once."
                    ),
                },
                "max_new_events": {
                    "type": "integer",
                    "default": DEFAULT_EVENT_PAGE,
                    "description": "How many new events to report per task.",
                },
                "include_heartbeats": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Heartbeat events are hidden by default -- at one every "
                        "few seconds they bury the agent's own output. The count "
                        "hidden is always reported."
                    ),
                },
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "swarm_status",
        "description": (
            "Current state of one or more tasks. Returns immediately. `masked` "
            "says how many credential-shaped strings the API masked in each "
            "task's input and metadata, which it never serves raw; null means "
            "the deployment sent no count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["task_ids"],
        },
    },
    {
        "name": "swarm_result",
        "description": (
            "What a finished task produced: its commits, its patch, its pull "
            "request, and -- when there is no pull request -- WHY, which has six "
            "different causes needing six different responses. Every result also "
            "names the `runner_profile` that ran and the `backend` it resolves "
            "to.\n"
            "\n"
            "WHEN THE TASK FAILED it additionally carries `failure`, read from "
            "the per-attempt record: the last attempt's backend, execution name, "
            "exit code, error and whether it came near an OOM, plus every "
            "EARLIER attempt's exit code and error -- which the task document "
            "cannot give you, because `result_summary` is written once at "
            "terminal state and a task that failed twice and succeeded on the "
            "third try carries only the third attempt's numbers.\n"
            "\n"
            "`exit_code: null` means NOT RECORDED. It is not 0. Reporting it as "
            "0 says the agent exited cleanly, which is the one thing it did not "
            "do. `failure.note` means the task failed before any agent ran, so "
            "the investigation is admission and dispatch rather than the agent.\n"
            "\n"
            "`commits`, `insertions`, `deletions` and `uncommitted_files` are "
            "null when nothing was counted -- the task cloned no repository, "
            "never started, or its harvest failed -- and `no_patch_because` says "
            "which. Null is not zero; 0 is a count the harvest made.\n"
            "\n"
            "`masked` counts the credential-shaped strings the API masked in the "
            "task's `input` and `metadata`. The API serves both masked, to the "
            "submitter too, so a prompt read back is the masked copy; null means "
            "the deployment sent no count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "swarm_apply",
        "description": (
            "Apply one task's code changes into a local working tree, with a "
            "three-way fallback so a conflict leaves ordinary markers rather "
            "than failing. This is how a remote agent's work reaches your repo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "repo": {"type": "string", "description": "Working tree. Defaults to cwd."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "swarm_integrate",
        "description": (
            "Put several agents' work on ONE branch, applying in the order given "
            "so each patch lands on the accumulated result. Conflicts come back "
            "as markers in files for you to resolve, not as a failure. Requires "
            "a clean working tree."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "branch": {"type": "string"},
                "base": {"type": "string", "description": "Commit or branch to start from."},
                "repo": {"type": "string"},
            },
            "required": ["task_ids", "branch"],
        },
    },
    # -- workflows ---------------------------------------------------------
    #
    # The four below are the difference between "a convenient remote API" and
    # the thing this platform was built to be. Without them a session can start
    # N agents and must then join them by hand: poll each one, notice which
    # finished, copy one's output into the next one's prompt, and keep the DAG
    # in its own head. That IS the bookkeeping the platform removes, so leaving
    # it out left the platform's actual feature unreachable from the place the
    # work happens.
    {
        "name": "swarm_workflow",
        "description": (
            "Submit a DAG of agents and let SwarmCloud schedule it: fan-out, "
            "dependencies, and one step's output staged into another's "
            "workspace. Use this instead of several swarm_dispatch calls "
            "whenever the units are not independent.\n"
            "\n"
            "Each step names a runner profile BY NAME and carries its own "
            "prompt. `depends_on` lists step ids that must SUCCEED first; empty "
            "means the step is eligible immediately, NOT that it runs first. "
            "`input_from` maps an upstream step id to ONE artifact filename, "
            "copied into this step's working directory before its agent starts "
            "-- the upstream agent must have written that exact filename into "
            "$SWARM_ARTIFACTS_DIR, and a declared input that cannot be staged "
            "FAILS the attempt rather than starting the agent without it. "
            "A claude-code or codex upstream agent is told which filenames its "
            "dependants stage and the absolute path of $SWARM_ARTIFACTS_DIR, "
            "which is outside the repository; any other runner is told nothing, "
            "so its prompt must say it. `./artifacts` in an agent's working "
            "directory is a link to $SWARM_ARTIFACTS_DIR, unless a staged input "
            "or a restored checkpoint already has that name. A file written "
            "anywhere else never reaches a dependant. An upstream attempt that "
            "finishes without writing a file a dependant stages FAILS, "
            "retryably, naming the missing files: it runs again until "
            "max_attempts is spent, then fails and its dependants are "
            "cancelled. Every "
            "`input_from` source must also appear in `depends_on`, or the API "
            "refuses the whole submission. The same refusal applies when two "
            "parents of one step stage the same filename, since the name is also "
            "where the file lands, or when a filename is absolute or contains "
            "an empty, '.' or '..' segment. So have each parent write its own "
            "distinct relative filename.\n"
            "\n"
            "Returns as soon as the workflow is accepted, with the task id of "
            "every step. It does not wait: poll swarm_workflow_status for "
            "per-step state, or swarm_follow with the returned task ids to "
            "narrate events and log lines as they arrive. swarm_follow takes a "
            "cursor and returns the next one, so a session can report progress "
            "without holding anything open."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "description": "The DAG. Order does not matter; dependencies do.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "description": (
                                    "Unique within the workflow; how other steps "
                                    "name this one."
                                ),
                            },
                            "prompt": {
                                "type": "string",
                                "description": (
                                    "This step's instructions. Required: a step "
                                    "with no prompt is accepted by the API and "
                                    "then fails at the agent, after it has been "
                                    "admitted and dispatched."
                                ),
                            },
                            "runner_profile": {
                                "type": "string",
                                "default": "claude-code",
                                "description": (
                                    "A profile BY NAME from the frozen "
                                    "catalogue. The image, command and resource "
                                    "spec come from the name and cannot be "
                                    "supplied here."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Step ids that must succeed before this one "
                                    "is eligible."
                                ),
                            },
                            "input_from": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": (
                                    "{upstream_step_id: artifact_filename} to "
                                    "stage into this step's workspace."
                                ),
                            },
                            "resource_class": {
                                "type": "string",
                                "description": (
                                    "A NAMED class from the catalogue, no larger "
                                    "than the profile's own. Not a resource spec."
                                ),
                            },
                            "timeout_seconds": {"type": "integer"},
                            "inputs": _INPUTS_SCHEMA,
                        },
                        "required": ["step_id", "prompt"],
                    },
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "The repository EVERY step clones. Workflow-level, not "
                        "per-step: the steps of one workflow integrate into one "
                        "branch. Omit it and no step clones anything, which "
                        "leaves `collect` as the only usable strategy."
                    ),
                },
                "ref": {"type": "string", "description": "Branch, tag or commit."},
                "strategy": {
                    "type": "string",
                    "description": (
                        "How the work comes back. `integrate` produces ONE pull "
                        "request from one nominated step; the response says "
                        "which."
                    ),
                },
                "carrier": {"type": "string", "description": "How changes are carried back."},
                "on_step_failure": {
                    "type": "string",
                    "enum": ["fail_workflow", "continue"],
                    "default": "fail_workflow",
                    "description": (
                        "What one step's failure does to the rest of the "
                        "workflow. Applied by the scheduler on its next drain, "
                        "not at the instant of the failure, so a step it admits "
                        "before it has seen the failure counts as started.\n"
                        "`fail_workflow` (default): as soon as any step is FAILED "
                        "or DEAD_LETTERED, every step that has not started yet "
                        "(queued, ready or parked) is cancelled, including "
                        "independent branches that do not depend on the failed "
                        "step. Each cancelled step's event names the step that "
                        "failed. Steps already running are never killed: they "
                        "run to completion, and the workflow reads FAILED once "
                        "they finish.\n"
                        "`continue`: only the dependents of the failed step "
                        "(and their dependents) are cancelled. Independent "
                        "branches keep starting and run to completion.\n"
                        "Under both settings a step that is CANCELLED, for "
                        "example stopped by hand, is not a failure. Only its "
                        "own dependents are cancelled."
                    ),
                },
                "priority": {"type": "integer"},
                "label": {"type": "string", "description": "A short name, for the UI."},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "swarm_workflow_status",
        "description": (
            "Where a workflow is: one row per step with its task's state, and "
            "the workflow's own state.\n"
            "\n"
            "THE WORKFLOW STATE REPORTED HERE IS DERIVED FROM THE STEPS. A "
            "workflow document also carries a stored `state` field that is a "
            "cache written once at submission and advanced by nothing, so "
            "workflows read QUEUED with every step already finished. That value "
            "is reported separately as `stored_state` and is never served as "
            "the state.\n"
            "\n"
            "So read the absences. `state: null` with "
            "`state_unavailable_because` means this API did NOT derive on that "
            "read -- the per-step states below are then the only trustworthy "
            "answer and the server needs fixing; say so rather than quoting the "
            "cache. `state: \"UNKNOWN\"` is a different thing: the server "
            "derived and could not finish reading the steps, and "
            "`state_incomplete_because` names which ones. A STEP whose `state` "
            "is null was likewise not read -- it is neither queued nor gone. "
            "`park_reason: DEPENDENCY_INCOMPLETE` means the step is waiting for "
            "a parent and is holding no capacity, which costs nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "swarm_workflow_result",
        "description": (
            "What each step of a workflow PRODUCED: commits, insertions and "
            "deletions, the patch uri and the pull request -- and, when there "
            "is no patch or no pull request, WHY, with the same six causes "
            "swarm_result distinguishes. A step that produced no patch is not a "
            "step that did nothing; `no_patch_because` is what tells those "
            "apart.\n"
            "\n"
            "Carries the same state discipline as swarm_workflow_status: the "
            "workflow state is DERIVED from the steps, the Firestore cache is "
            "reported beside it as `stored_state` and is never served as the "
            "state, `state_unavailable_because` means this API did not derive "
            "on that read, and `state_incomplete_because` means it derived over "
            "a partial read. Bring a step's work into a local tree with "
            "swarm_apply, or several steps' work onto one branch with "
            "swarm_integrate, using the task ids below.\n"
            "\n"
            "A step that FAILED also carries `failure`, the same per-attempt "
            "block swarm_result gives: the last attempt's backend, exit code, "
            "error and near-OOM flag, and every earlier attempt's exit code. In "
            "a fan-out this is the difference between 'four of twenty failed' "
            "and four investigations that each have a starting point. "
            "`exit_code: null` means NOT RECORDED and never 0."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "swarm_workflow_cancel",
        "description": (
            "Cancel a workflow: it is marked cancel_requested and cancellation "
            "is requested for every step task that has one. Work already done "
            "stays checkpointed and harvestable. Steps that had already reached "
            "a terminal state come back under `tasks_already_terminal` -- that "
            "is the cancel arriving after they finished, not a failure of the "
            "cancel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    # -- cluster state -----------------------------------------------------
    #
    # These return the SAME text `sc` prints, at a fixed 80 columns with no
    # colour. Not JSON: the marks are the contract here -- `~12%` is a
    # projection and an em dash is "not measured" -- and a JSON dump of
    # `{"utilization": 0.12, "stale": true}` invites a reader to quote the
    # number and drop the flag, which is precisely the mistake the marks exist
    # to prevent.
    {
        "name": "swarm_overview",
        "description": (
            "The whole cluster on one screen: the account pool and its quota "
            "windows, which pool actually binds each runner profile, what is "
            "running and queued, and what is wrong. Read the marks: a bare "
            "percentage is measured, '~' means projected from an older reading "
            "and must not be quoted as current, and an em dash means NOT "
            "MEASURED -- never report it as zero."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "default": 80, "description": "Column budget."}
            },
        },
    },
    {
        "name": "swarm_accounts",
        "description": (
            "The subscription account pool, in the columns `cs status` uses: "
            "account, 5-hour and 7-day utilisation with bars, when the binding "
            "window clears, and state (available / paused / draining / reauth "
            "needed). Carries no credential material of any kind. A stale "
            "reading is marked '~' and an unread one is an em dash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer", "default": 80}},
        },
    },
    {
        "name": "swarm_capacity",
        "description": (
            "Pool ceilings, and for each runner profile WHICH pool actually "
            "binds it. Admission is all-or-nothing across every required pool, "
            "so the ceiling a profile feels is the tightest of them -- not the "
            "global one, which is the one people quote. ROOM is AGENTS: how many "
            "more tasks of that profile fit before the binding pool refuses. "
            "UNITS is capacity UNITS in use out of the pool's limit, not agents -- "
            "an agent takes its resource class's units ("
            + ", ".join(
                f"{name} {resource.units}"
                for name, resource in sorted(RESOURCE_CLASSES.items(), key=lambda kv: kv[1].units)
            )
            + ") -- and on a shared pool it counts every tenant's."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer", "default": 80}},
        },
    },
    {
        "name": "swarm_agents",
        "description": (
            "Agents running and queued, with why each queued one is waiting. "
            "Returns immediately; it does not follow anything. For the events "
            "and log lines an agent has produced since you last looked, call "
            "swarm_follow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer", "default": 80}},
        },
    },
    {
        "name": "swarm_trouble",
        "description": (
            "Everything wrong right now, worst first: paused dispatch, accounts "
            "needing re-auth, stale quota readings, exhausted windows, full or "
            "paused pools, parked and dead-lettered tasks. A subsystem that "
            "could not be READ is itself reported -- silence about one is how "
            "an operator concludes it is fine."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_cancel",
        "description": "Cancel running tasks. Their work is still checkpointed and harvestable.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["task_ids"],
        },
    },
]


def _int_arg(args: dict[str, Any], name: str, default: int) -> int:
    """An integer argument, with zero surviving as zero.

    A model sends `"20000"` as often as `20000`, and `args.get(name) or default`
    turns a deliberate 0 into the default -- the same `false // true` shape this
    repository documents for jq and that `swarm_wait` was bitten by.
    """
    value = args.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# `_describe` USED TO LIVE HERE, byte-for-byte identical to
# `patches.describe_task`. The function was moved into `patches` when the
# workflow rollup became its third caller -- and that docstring says so -- but
# the original was left behind and `swarm_follow` kept calling it. Two copies of
# "what did this task produce" is precisely what the move was meant to prevent,
# and the second copy is the one that would have missed every field added to
# the first. Deleted; `swarm_follow` now reads the same function as
# `swarm_result`, `swarm_wait` and the workflow rollup.


def _overview_view(snap, style):
    from . import render

    return render.render_overview(snap, style)


def _accounts_view(snap, style):
    from . import render

    return [render.section("accounts", render.accounts_subtitle(snap.accounts, style), style)] + \
        render.render_accounts(snap.accounts, style, snap.now, error=snap.accounts_error)


def _capacity_view(snap, style):
    from . import render

    return [render.section("capacity", render.capacity_subtitle(snap.capacity, style), style)] + \
        render.render_capacity(snap.capacity, style, error=snap.capacity_error)


def _agents_view(snap, style):
    from . import render

    return [render.section("agents", render.agents_subtitle(snap.tasks, style), style)] + \
        render.render_agents(snap.tasks, style, snap.now, error=snap.tasks_error)


def _trouble_view(snap, style):
    from . import render

    findings = render.find_trouble(snap, style)
    return [render.section("trouble", f"{len(findings)} finding(s)" if findings else "", style)] + \
        render.render_trouble(findings, style)


#: tool name -> (renderer, which fetches it needs). Every one of these goes
#: through `sc.collect`, so the tool and the terminal command cannot drift.
_SC_VIEWS = {
    "swarm_overview": (_overview_view, "overview"),
    "swarm_accounts": (_accounts_view, "accounts"),
    "swarm_capacity": (_capacity_view, "capacity"),
    "swarm_agents": (_agents_view, "agents"),
    "swarm_trouble": (_trouble_view, "trouble"),
}


def _call(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    if name == "swarm_dispatch":
        # CHECKED BEFORE THE ROUND TRIP. The API refuses an unknown or disabled
        # profile too and stays the authority; this only refuses sooner, with
        # the catalogue's own reason, so a session that typed `claude` for
        # `claude-code` is told which names exist instead of reading a 4xx. See
        # `profiles.check` for why a local check here is not a second opinion.
        profile = catalogue.check(args.get("profile") or "claude-code", where="swarm_dispatch")
        # The inputs this profile DECLARES, and nothing else, refused by name
        # before anything travels (#142).
        inputs = catalogue.check_inputs(profile, args.get("inputs"), where="swarm_dispatch")
        task = client.dispatch(
            prompt=args["prompt"],
            runner_profile=profile,
            repository_url=args.get("repo"),
            repository_ref=args.get("ref"),
            metadata={"unit": args["label"]} if args.get("label") else None,
            inputs=inputs or None,
        )
        task_id = task_id_of(task)
        if not task_id:
            # Raised rather than returned, because a task id is what every tool
            # downstream of this one takes: without it the reply is a success
            # the caller can do nothing with. (`follow_command` also refuses to
            # build a command out of an empty id -- a tail command with the id
            # missing still looks runnable -- but that is the second line of
            # defence, not this one.)
            raise SwarmError(
                f"the API accepted the task but its response named no id: {sorted(task)}"
            )
        return json.dumps(
            {
                "task_id": task_id,
                "state": task.get("state"),
                # THE TOOL FIRST, the terminal second. `swarm_follow` is the one
                # a model can actually call; the tail below is for a human's
                # background shell. Handing back only the shell command was how
                # a session holding a perfectly good cursor tool went and ran a
                # binary that is not on PATH.
                "follow_with": "swarm_follow",
                "follow_live_with": follow_command([task_id]),
            },
            indent=2,
        )

    if name == "swarm_follow":
        # `cursor` crosses the model boundary, so it arrives as whatever the
        # model sent -- including a string it decided to quote. `follow`
        # normalises every field it reads, and a cursor it cannot use re-reads
        # from the beginning rather than skipping ahead: a position that
        # quietly became zero costs a repeat, a position that quietly became
        # large loses output silently, and only one of those is recoverable.
        report = follow(
            client,
            list(args["task_ids"]),
            cursor=args.get("cursor"),
            # NOT `args.get(...) or DEFAULT`, for the reason swarm_wait's
            # timeout spells out: zero is a legitimate value here too -- "tell
            # me the events and the states, spend nothing on logs" -- and it is
            # falsy, so the `or` form would turn the smallest request into the
            # largest one.
            max_log_bytes=_int_arg(args, "max_log_bytes", DEFAULT_LOG_BUDGET),
            max_new_events=_int_arg(args, "max_new_events", DEFAULT_EVENT_PAGE),
            include_heartbeats=bool(args.get("include_heartbeats")),
        )
        # The finished ones carry their outcome, so a session that was polling
        # does not have to notice `terminal` and make a second call to find out
        # what the agent actually produced.
        for task in report["tasks"]:
            if task["read"] == "ok" and task["terminal"]:
                task["result"] = describe_task(client.task(task["task_id"]))
        return json.dumps(report, indent=2, default=str)

    if name == "swarm_status":
        # `masked` (owner decision, 2026-09-26): how many credential-shaped
        # strings the API masked in each task's input and metadata, which it
        # serves masked to everyone, the submitter included. Null for a count
        # an older API did not send, never 0.
        rows = []
        for t in args["task_ids"]:
            task = client.task(t)
            rows.append({"task_id": t, "state": task.get("state"), "masked": masked_counts(task)})
        return json.dumps(rows, indent=2)

    if name == "swarm_wait":
        # NOT `args.get(...) or 3600`. Zero is a legitimate value -- "tell me
        # what has finished, do not wait" -- and it is falsy, so the `or` form
        # silently turns the shortest possible wait into the longest one. The
        # same shape as the `false // true` trap this repository already
        # documents for jq.
        requested = args.get("timeout_seconds")
        deadline = time.monotonic() + int(3600 if requested is None else requested)
        pending = list(args["task_ids"])
        finished: dict[str, dict[str, Any]] = {}
        # ALWAYS ONE PASS, then wait. `while ... < deadline` skipped the body
        # entirely at timeout 0, so the one value the comment above calls
        # legitimate -- "tell me what has finished, do not wait" -- returned
        # `finished: []` and a `still_running` list of tasks it had not looked
        # at, under a note asserting they had not finished. Reporting a
        # finished task as running is the same defect as reporting a failed
        # read as an empty result: an answer given without asking.
        while True:
            for task_id in list(pending):
                task = client.task(task_id)
                if task.get("state") in TERMINAL:
                    finished[task_id] = describe_task(task)
                    pending.remove(task_id)
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            # Clamped, so a short wait is not rounded up to the poll interval.
            time.sleep(min(5.0, remaining))
        out: dict[str, Any] = {"finished": list(finished.values())}
        if pending:
            # Saying WHICH are still running matters: a caller that read this
            # as "everything finished" would integrate a partial set and call
            # it the whole batch.
            out["still_running"] = pending
            out["note"] = "the wait timed out; these tasks have not finished"
        return json.dumps(out, indent=2)

    if name == "swarm_profiles":
        # NO CLIENT CALL. The catalogue is the frozen contract, which this
        # process already holds, so this is the one tool that answers without a
        # round trip -- and, usefully, the one tool that still answers when the
        # cluster cannot be reached at all. A session can at least tell the
        # developer which names exist while `swarm doctor` works out why
        # nothing else does.
        return json.dumps({"profiles": catalogue.catalogue()}, indent=2)

    if name == "swarm_result":
        task = client.task(args["task_id"])
        described = describe_task(task)
        # ONLY ON A FAILURE, and only here. `explain_failure` costs one extra
        # round trip to the attempts route, which is where the exit code, the
        # backend that actually ran and the earlier attempts' errors live --
        # none of them are on the task document. A successful read pays nothing
        # because the function returns None without asking.
        failure = explain_failure(client, task)
        if failure is not None:
            described["failure"] = failure
        return json.dumps(described, indent=2)

    if name == "swarm_apply":
        repo = Path(args.get("repo") or ".").resolve()
        task = client.task(args["task_id"])
        uri = patch_uri(task)
        if uri is None:
            return json.dumps(
                {"applied": False, "reason": explain_absence(task)}, indent=2
            )
        result = apply_patch(download(client, uri), repo, task_id=args["task_id"])
        return json.dumps(
            {
                "applied": result.applied,
                "conflicts": result.conflicted,
                "detail": result.detail,
            },
            indent=2,
        )

    if name == "swarm_integrate":
        repo = Path(args.get("repo") or ".").resolve()
        result = integrate(
            client,
            list(args["task_ids"]),
            repo,
            branch=args["branch"],
            base=args.get("base"),
        )
        return result.render()

    if name == "swarm_workflow":
        envelope = workflows.submit(
            client,
            steps=workflows.build_steps(args.get("steps")),
            strategy=args.get("strategy"),
            carrier=args.get("carrier"),
            repository_url=args.get("repo"),
            repository_ref=args.get("ref"),
            on_step_failure=args.get("on_step_failure"),
            priority=args.get("priority"),
            label=args.get("label"),
        )
        workflow = envelope["workflow"]
        workflow_id = workflow.get("workflow_id")
        if not workflow_id:
            # Same refusal `swarm_dispatch` makes about a missing task id, for
            # the same reason: every tool below takes this string, and handing
            # back an empty one produces a session that polls "" forever.
            raise SwarmError(
                "the API accepted the workflow but its response named no id: "
                f"{sorted(workflow)}"
            )
        steps = [
            {
                "step_id": step.get("step_id"),
                "task_id": step.get("task_id"),
                "runner_profile": step.get("runner_profile"),
                "depends_on": step.get("depends_on") or [],
                "input_from": step.get("input_from") or {},
            }
            for step in workflow.get("steps") or []
        ]
        created: dict[str, Any] = {
            "workflow_id": workflow_id,
            "steps": steps,
            "dispatch": envelope.get("dispatch"),
            # NO STATE HERE, deliberately. The create response is the one read
            # that honestly says `state_source: "stored"`: the step tasks were
            # written microseconds ago and deriving over them would spend a read
            # per step to be told what this very request just decided. Echoing
            # the stored QUEUED would look like an answer.
            "state_available_from": (
                "swarm_workflow_status -- a create response does not derive a "
                "workflow state and this tool will not quote the stored one"
            ),
        }
        task_ids = [str(s["task_id"]) for s in steps if s["task_id"]]
        if task_ids:
            created["follow_with"] = "swarm_follow"
            created["follow_live_with"] = follow_command(task_ids)
        return json.dumps(created, indent=2)

    if name in ("swarm_workflow_status", "swarm_workflow_result"):
        # ONE read for both. The difference is only how much of each step's task
        # is unpacked, and a second route call to answer "and what did it
        # produce" would be a second chance for the two answers to disagree
        # about the same workflow.
        envelope = workflows.fetch(client, args["workflow_id"])

        def _describe_step(task: dict[str, Any]) -> dict[str, Any]:
            """A step's result, plus WHY it died when it did.

            "Which step failed" is answerable from the rollup already; "and why"
            was not, and a fan-out is where that gap hurts most -- four failed
            steps of twenty, each reported as a state word, is four
            investigations with no starting point. `explain_failure` costs one
            extra read per FAILED step and nothing at all for the others,
            because it returns None on the state check before touching the
            client.
            """
            out = describe_task(task)
            failure = explain_failure(client, task)
            if failure is not None:
                out["failure"] = failure
            return out

        return json.dumps(
            workflows.report(
                envelope,
                describe=_describe_step if name == "swarm_workflow_result" else None,
            ),
            indent=2,
        )

    if name == "swarm_workflow_cancel":
        return json.dumps(workflows.cancel(client, args["workflow_id"]), indent=2)

    if name in _SC_VIEWS:
        from . import render
        from .sc import NEEDS, collect

        style = render.Style(width=int(args.get("width") or 80), color=False, unicode=True)
        view, needs = _SC_VIEWS[name]
        snap = collect(client, NEEDS[needs])
        return render.join(view(snap, style))

    if name == "swarm_cancel":
        for task_id in args["task_ids"]:
            client.cancel(task_id)
        return json.dumps({"cancelled": args["task_ids"]}, indent=2)

    raise SwarmError(f"unknown tool: {name}")


def _tool_error_text(exc: SwarmError) -> str:
    """What a failed tool call answers: the error, and, where the request never
    reached swarm-api, the command that says why -- spelled for this install.

    WHY THE BRIDGE SAYS IT AND THE SKILL DOES NOT (review of PR #201). The
    delegate skill answered "IAP refused this before the API saw it" with "Run
    `uv run swarm doctor`", which on a plugin-only install answers `Failed to
    spawn: swarm` -- #189's own output, met while diagnosing an unreachable
    API. A skill is text and cannot know the install; `terminal_command` does.
    So the refusal carries the command and the skill says to run the one the
    error names, as it says of `follow_live_with`.

    ONLY ON `edge`: Google's edge answered, not the API. An answer from the
    API itself -- a 409, a validation error -- means the request arrived, and
    doctor, which explains how a request fails to arrive, is no answer to it.
    """
    text = str(exc)
    if getattr(exc, "edge", False):
        text = text.rstrip().rstrip(".") + (
            f". This never reached swarm-api: `{terminal_command('swarm doctor')}` prints "
            "the address this bridge used and the credential that address takes"
        )
    return text


def _respond(message_id: Any, result: Any) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(message_id: Any, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})
        + "\n"
    )
    sys.stdout.flush()


def serve(stdin=None, stdout=None) -> int:
    """The stdio loop. One JSON-RPC message per line, both directions."""
    stream = stdin or sys.stdin
    # Built lazily: constructing a client shells out to gcloud, and a host that
    # merely LISTS this server's tools should not trigger an auth prompt.
    client: SwarmClient | None = None

    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        message_id = message.get("id")

        if method == "initialize":
            _respond(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "swarmcloud", "version": "0.1.0"},
                },
            )
        elif method == "notifications/initialized":
            continue  # a notification has no id and takes no response
        elif method == "tools/list":
            _respond(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                if client is None:
                    client = SwarmClient()
                text = _call(client, params.get("name", ""), params.get("arguments") or {})
                _respond(message_id, {"content": [{"type": "text", "text": text}]})
            except SwarmError as exc:
                # isError, not a JSON-RPC error: the CALL failed, the protocol
                # did not, and a host that saw a transport error would drop the
                # session rather than show the operator what went wrong.
                _respond(
                    message_id,
                    {"content": [{"type": "text", "text": _tool_error_text(exc)}], "isError": True},
                )
            except Exception as exc:  # pragma: no cover - defensive
                _respond(
                    message_id,
                    {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                     "isError": True},
                )
        elif message_id is not None:
            _error(message_id, -32601, f"method not found: {method}")
    return 0


def seed_plugin_config() -> None:
    """Write the plugin's deployment where `sc login` in a terminal can find it.

    Claude Code hands this process the plugin's `userConfig` in its
    environment; a terminal never sees it. So at start-up the non-secret half
    goes into the user's config file and the client secret into the credential
    store (config.seed_from_plugin). Best effort: this process reads the
    plugin's values from its own environment whether or not that write
    succeeds, so a read-only home directory costs the terminal its shortcut,
    not the session its tools. The one line it prints goes to STDERR -- stdout
    is the JSON-RPC channel, and a stray line there breaks the protocol.
    """
    from . import config

    try:
        config.seed_from_plugin()
    except Exception as exc:  # noqa: BLE001 - never fatal; see above
        print(f"swarm-mcp: could not save the plugin's deployment for the terminal: {exc}", file=sys.stderr)


def main() -> int:
    seed_plugin_config()
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
