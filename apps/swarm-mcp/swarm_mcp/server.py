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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from swarm_common.profiles import RESOURCE_CLASSES

from . import checkout, progress
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
    patch_uri,
)

PROTOCOL_VERSION = "2024-11-05"

#: What `swarm_dispatch` takes as `strategy`: the API's strategies
#: (`workflows.STRATEGIES`) less `integrate`, which names the final step of a
#: WORKFLOW and which the API refuses for a single task. A subset of that
#: tuple, not a restatement: a strategy the API drops is dropped here too.
_DISPATCH_STRATEGIES = tuple(s for s in workflows.STRATEGIES if s != "integrate")

#: The `swarm_workflow` parameters a `spec` carries itself, so passing one of
#: them beside a spec is two answers to one question.
_SPEC_EXCLUSIVE = (
    "steps", "repo", "ref", "strategy", "carrier", "on_step_failure", "priority", "label",
)

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
                "repo": {
                    "type": "string",
                    "description": (
                        "Repository to clone (https or ssh). OMIT IT to clone the "
                        "repository and branch of the git checkout this bridge runs "
                        "in: the branch must be pushed and have no unpushed commits, "
                        "or the dispatch is refused with the command that fixes it; "
                        "uncommitted changes are reported as invisible to the agent. "
                        "Outside a checkout nothing is cloned and the reply says so."
                    ),
                },
                "ref": {"type": "string", "description": "Branch, tag or commit."},
                "no_repository": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Clone nothing, even inside a checkout. The task can then "
                        "produce no patch and no pull request -- its work exists only "
                        "as its answer."
                    ),
                },
                "strategy": {
                    "type": "string",
                    "enum": list(_DISPATCH_STRATEGIES),
                    "default": "collect",
                    "description": (
                        "How the work comes back. `collect` (default): the agent's "
                        "changes are harvested as a patch and nothing is pushed -- "
                        "bring them in with swarm_apply. `direct-pr`: the agent's "
                        "branch is pushed as swarm/<task> and a pull request opened, "
                        "which needs a repository and a forge token that can push. "
                        "`integrate` is a workflow strategy; use swarm_workflow."
                    ),
                },
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
            "three different facts and none of them mean 'the agent is idle'.\n"
            "\n"
            "FOR AN AGENT THAT IS THE ROW OF A WORKFLOW (`sc:remote`, `sc:step`), "
            "pass `format: \"lines\"`: the answer is then short narrated lines -- "
            "what the remote agent said, which tools it called, where a waiting "
            "task is waiting and why -- plus an opaque `since` token to pass back "
            "unchanged, instead of the full report. With `wait_seconds` a call "
            "gathers for up to that long (max 300) and returns early when every "
            "task has finished or a task starts; the first call, without "
            "`since`, returns at once. A finished task carries `outcome`: its "
            "answer, `answer_json` (the JSON object the answer ends with, if any), "
            "`cost_usd` (null when not recorded, never 0), `duration_s`, "
            "`pr_url`, `artifacts`, `last_error` and, on a failure, `failure`. "
            "Stop calling when the reply says `stop: true`: every task has "
            "finished, or the row gave up on one -- it cannot be read (a 404 or "
            f"403 at once, other failures after {progress.READ_FAILURE_LIMIT} "
            "calls in a row), or it is "
            "not the `step_id` passed -- and that task says why in "
            "`abandoned_because`."
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
                "since": {
                    "type": "string",
                    "description": (
                        "The `since` string a previous call returned, passed back "
                        "UNCHANGED -- the same position as `cursor`, as one opaque "
                        "token. Omit it on the first call. Pass `since` or "
                        "`cursor`, not both."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["json", "lines"],
                    "default": "json",
                    "description": (
                        "`json` (default): the full report. `lines`: short "
                        "narrated lines, a `since` token and, for a finished task, "
                        "its `outcome` -- the shape a workflow row reads."
                    ),
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "`lines` only: gather for up to this many seconds (max "
                        "300) before returning; returns early when every task has "
                        "finished, a task starts, or the read budget is spent. A "
                        "call without `since` always returns at once."
                    ),
                },
                "max_lines": {
                    "type": "integer",
                    "default": progress.DEFAULT_MAX_LINES,
                    "description": (
                        "`lines` only: the most lines one call returns. Past it the "
                        "window's EARLIEST lines are left out and a line says how "
                        "many; every byte stays in the task's log."
                    ),
                },
                "step_id": {
                    "type": "string",
                    "description": (
                        "`lines` only, with exactly ONE task id: the workflow step "
                        "that task must be. A task that is a different step is not "
                        "followed -- the reply says `stop: true` and why -- so a "
                        "mis-copied task id cannot report another step's work under "
                        "this step's name."
                    ),
                },
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "swarm_status",
        "description": "Current state of one or more tasks. Returns immediately.",
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
            "which. Null is not zero; 0 is a count the harvest made."
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
            "without holding anything open.\n"
            "\n"
            "Pass the DAG either as `steps` with the parameters beside it, or as "
            "`spec`: a whole workflow spec object exactly as the terminal's "
            "workflow command reads it (`steps`, `strategy`, `carrier`, `repository_url`, "
            "`repository_ref`, `on_step_failure`, `priority`, `label`), nothing "
            "else beside it but `no_repository`. With no repository named, EVERY "
            "step clones the repository and pushed branch of the checkout this "
            "bridge runs in, under the same rules as swarm_dispatch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": (
                        "A whole workflow spec -- the file the terminal's workflow "
                        "command reads -- instead of `steps` and the "
                        "parameters beside it. A step may carry `stage`, the group "
                        "/sc:run shows it under; it is never sent."
                    ),
                },
                "spec_digest": {
                    "type": "string",
                    "description": (
                        "With `spec` only: the digest the caller computed of the spec "
                        "it meant to send (`fnv1a32:` and eight hex digits, over the "
                        "spec as sorted-key compact JSON). When it differs from the "
                        "digest of the spec received, NOTHING is submitted: the spec "
                        "was changed on the way here, by whoever retyped it. The reply "
                        "carries the digest of what was received either way."
                    ),
                },
                "no_repository": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clone nothing for any step, even inside a checkout.",
                },
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
                            "stage": {
                                "type": "string",
                                "description": (
                                    "Display only: the group /sc:run shows this "
                                    "step under. Never sent to the platform."
                                ),
                            },
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
            # `steps` OR `spec`, checked in `_call` -- JSON Schema's oneOf is
            # not something every MCP host renders, and a host that cannot show
            # it shows no required field at all.
            "required": [],
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
        # `width` is read by the view like the other four's; it was missing
        # here, and now that an undeclared argument is refused it must be
        # declared (`_refuse_unknown_arguments`).
        "inputSchema": {
            "type": "object",
            "properties": {"width": {"type": "integer", "default": 80}},
        },
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


def _flag(args: dict[str, Any], name: str) -> bool:
    """A boolean argument, read strictly.

    `bool(args.get(name))` reads the STRING "false" -- which a model sends as
    often as `false` -- as true, and for `no_repository` that is a dispatch
    that silently clones nothing.
    """
    value = args.get(name)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return value is True


def _dispatch_strategy(value: Any) -> str | None:
    """`collect`, `direct-pr`, or None for the API's default (collect).

    Refused BEFORE the round trip, with the reason, for the two ways to get it
    wrong: `integrate` names a final step of a workflow and a single task has
    none (the API refuses it too, as `invalid_dispatch`), and anything else is
    a typo.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if text == "integrate":
        raise SwarmError(
            "strategy 'integrate' makes one workflow step apply every other step's "
            "work and open ONE pull request; a single task has nothing to integrate. "
            "Use `collect` (the default) or `direct-pr` here, or submit the units as "
            "a workflow with swarm_workflow"
        )
    if text not in _DISPATCH_STRATEGIES:
        raise SwarmError(
            f"unknown strategy {value!r}; swarm_dispatch accepts "
            + ", ".join(_DISPATCH_STRATEGIES)
        )
    return text


def _accepted_strategy(task: dict[str, Any], sent: str | None) -> str:
    """The strategy the API RECORDED for this task, else what was sent, else its default."""
    block = (task.get("metadata") or {}).get("dispatch")
    if isinstance(block, dict) and isinstance(block.get("strategy"), str):
        return block["strategy"]
    return sent or "collect"


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


#: The arguments each tool takes, read from its own schema so the two cannot
#: disagree.
_ACCEPTED = {
    tool["name"]: frozenset((tool.get("inputSchema") or {}).get("properties") or {})
    for tool in TOOLS
}


def _refuse_unknown_arguments(name: str, args: dict[str, Any]) -> None:
    """An argument this bridge does not know is REFUSED, never dropped.

    Dropping was the old behaviour, and it fails silently in exactly the case
    that matters: a caller newer than the bridge. A plugin agent written for a
    bridge that takes `strategy` and `step_id`, talking to one that does not,
    had its `strategy: direct-pr` ignored -- the task ran as `collect` and
    returned as if nothing were wrong. Refused, the caller reads which argument
    this version lacks, and nothing is sent.
    """
    accepted = _ACCEPTED.get(name)
    if accepted is None:
        return  # an unknown TOOL is refused by name at the end of `_call`
    unknown = sorted(str(key) for key in args if key not in accepted)
    if unknown:
        raise SwarmError(
            f"{name} does not take {unknown}; it takes {sorted(accepted)}. Nothing was "
            "done. An argument this bridge does not know is refused rather than ignored: "
            "if a newer caller relies on it, this bridge is older than that caller"
        )


def _call(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    _refuse_unknown_arguments(name, args)
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
        strategy = _dispatch_strategy(args.get("strategy"))
        # WHAT IS CLONED, decided before anything travels: the caller's repo,
        # or the checkout this bridge runs in -- refused when its branch is not
        # pushed, because a remote agent on an older tip succeeds on the wrong
        # base (see `checkout`).
        repository = checkout.resolve(
            repo=args.get("repo"),
            ref=args.get("ref"),
            no_repository=_flag(args, "no_repository"),
        )
        if strategy == "direct-pr" and not repository.url:
            raise SwarmError(
                "strategy 'direct-pr' pushes the agent's branch and opens a pull "
                "request, so it needs a repository, and this dispatch has none: "
                + "; ".join(repository.notes)
            )
        task = client.dispatch(
            prompt=args["prompt"],
            runner_profile=profile,
            repository_url=repository.url,
            repository_ref=repository.ref,
            metadata={"unit": args["label"]} if args.get("label") else None,
            inputs=inputs or None,
            strategy=strategy,
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
                "strategy": _accepted_strategy(task, strategy),
                # What the agent will see, and how that was decided -- a null
                # url with the reason, never a silent no-clone.
                "repository": repository.as_dict(),
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

    if name == "swarm_follow" and (args.get("format") or "json") not in ("json", "lines"):
        raise SwarmError(f"unknown format {args.get('format')!r}; swarm_follow takes json or lines")

    if name == "swarm_follow" and args.get("since") not in (None, "") and args.get("cursor"):
        raise SwarmError(
            "pass `since` or `cursor`, not both: they are the same position in two "
            "shapes, and two positions for one task is a request to read from two places"
        )

    expected_step = args.get("step_id") if name == "swarm_follow" else None
    if expected_step is not None and (not isinstance(expected_step, str) or not expected_step.strip()):
        raise SwarmError("`step_id` must be the step's id, a non-empty string")
    if expected_step is not None and args.get("format") != "lines":
        # Refused, not ignored: a caller that asked for the check and did not
        # get it would believe the task it followed was its step.
        raise SwarmError(
            "`step_id` is checked only with `format: \"lines\"`; pass that, or no `step_id`"
        )

    if name == "swarm_follow" and args.get("format") == "lines":
        # The row view (`progress.watch`): narrated lines, one opaque token, a
        # call that may gather for a window, and a finished task's outcome.
        return json.dumps(
            progress.watch(
                client,
                list(args["task_ids"]),
                since=args.get("since"),
                wait_seconds=_int_arg(args, "wait_seconds", 0),
                max_log_bytes=_int_arg(args, "max_log_bytes", progress.DEFAULT_LINES_LOG_BUDGET),
                max_new_events=_int_arg(args, "max_new_events", DEFAULT_EVENT_PAGE),
                max_lines=_int_arg(args, "max_lines", progress.DEFAULT_MAX_LINES),
                include_heartbeats=_flag(args, "include_heartbeats"),
                step_id=expected_step.strip() if isinstance(expected_step, str) else None,
            ),
            indent=1,
            default=str,
        )

    if name == "swarm_follow":
        # `cursor` crosses the model boundary, so it arrives as whatever the
        # model sent -- including a string it decided to quote. `follow`
        # normalises every field it reads, and a cursor it cannot use re-reads
        # from the beginning rather than skipping ahead: a position that
        # quietly became zero costs a repeat, a position that quietly became
        # large loses output silently, and only one of those is recoverable.
        since_cursor, since_states, since_said, _groups, since_note = progress.decode_since(args.get("since"))
        report = follow(
            client,
            list(args["task_ids"]),
            cursor=since_cursor if args.get("since") not in (None, "") else args.get("cursor"),
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
        # The same position as `cursor`, as one token, for a caller that would
        # rather not copy a nested object back by hand.
        report["since"] = progress.encode_since(
            report["cursor"],
            {**since_states, **{t["task_id"]: t.get("state") for t in report["tasks"]}},
            since_said,
        )
        if since_note:
            report["since_note"] = since_note
        return json.dumps(report, indent=2, default=str)

    if name == "swarm_status":
        return json.dumps(
            [
                {"task_id": t, "state": client.task(t).get("state")}
                for t in args["task_ids"]
            ],
            indent=2,
        )

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
        digest: str | None = None
        expected_digest = args.get("spec_digest")
        if args.get("spec") is not None:
            beside = sorted(k for k in _SPEC_EXCLUSIVE if args.get(k) is not None)
            if beside:
                raise SwarmError(
                    f"swarm_workflow was given `spec` AND {beside}; a spec carries all of "
                    "those itself. Pass the whole spec, or `steps` with the parameters "
                    "beside it -- not both"
                )
            # THE DIGEST OF WHAT ARRIVED, before anything else reads it. /sc:run
            # hands the spec to a relay that retypes it into this call; the
            # digest the script computed is the only thing that can show the
            # relay dropped a step or "tidied" a prompt, and it is checked
            # before anything is sent, so a changed spec submits nothing.
            digest = workflows.spec_digest(args["spec"])
            if expected_digest not in (None, "") and str(expected_digest).strip() != digest:
                raise SwarmError(
                    f"the spec this tool received has digest {digest}, and `spec_digest` "
                    f"says the spec meant was {str(expected_digest).strip()}: the spec was "
                    "changed on its way here -- by whoever retyped it -- so NOTHING was "
                    "submitted. Pass the spec exactly as given, character for character"
                )
            fields = workflows.read_spec(args["spec"], where="swarm_workflow's `spec`")
            repo, ref = fields["repository_url"], fields["repository_ref"]
        else:
            if expected_digest not in (None, ""):
                raise SwarmError(
                    "`spec_digest` checks a whole `spec`, and this call passed `steps`; "
                    "pass the spec it was computed over, or no `spec_digest`"
                )
            if args.get("steps") is None:
                raise SwarmError("swarm_workflow needs `steps`, or a whole `spec`")
            fields = {
                "steps": workflows.build_steps(args.get("steps")),
                "strategy": args.get("strategy"),
                "carrier": args.get("carrier"),
                "on_step_failure": args.get("on_step_failure"),
                "priority": args.get("priority"),
                "label": args.get("label"),
            }
            repo, ref = args.get("repo"), args.get("ref")
        repository = checkout.resolve(
            repo=repo, ref=ref, no_repository=_flag(args, "no_repository")
        )
        envelope = workflows.submit(
            client,
            steps=fields["steps"],
            strategy=fields["strategy"],
            carrier=fields["carrier"],
            repository_url=repository.url,
            repository_ref=repository.ref,
            on_step_failure=fields["on_step_failure"],
            priority=fields["priority"],
            label=fields["label"],
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
            "repository": repository.as_dict(),
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
        if digest is not None:
            # What was RECEIVED, so a caller that did not pass `spec_digest` can
            # still compare it with the spec it meant.
            created["spec_digest"] = digest
            created["spec_digest_checked"] = expected_digest not in (None, "")
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


#: ONE LINE AT A TIME ON STDOUT. Tool calls answer from worker threads (see
#: `serve`), and two responses interleaved mid-line are two unparseable lines
#: to the host.
_WRITE_LOCK = threading.Lock()

#: How many tool calls may be in flight at once. A workflow of a dozen steps is
#: a dozen `sc:step` agents, each holding a `swarm_follow` open for its window,
#: plus the session's own calls; this leaves room for all of them.
MAX_CONCURRENT_CALLS = 32


def _respond(message_id: Any, result: Any) -> None:
    line = json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n"
    with _WRITE_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _error(message_id: Any, code: int, message: str) -> None:
    line = json.dumps(
        {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
    ) + "\n"
    with _WRITE_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def serve(stdin=None, stdout=None) -> int:
    """The stdio loop. One JSON-RPC message per line, both directions.

    TOOL CALLS RUN CONCURRENTLY, and answer in the order they finish. This loop
    answered one call at a time, so one `swarm_wait` -- which blocks for up to
    an hour by design -- held every other tool call in the session behind it,
    and a workflow's step agents, each following its own task, would each have
    waited for all the others' windows in turn. JSON-RPC matches a response to
    its request by `id`, not by order, so nothing a host relies on changes.
    `initialize` and `tools/list` still answer inline, before anything else is
    read. The client is shared: `SwarmClient` already serialises token mints
    behind its own lock, because `sc` fetches concurrently.
    """
    stream = stdin or sys.stdin
    # Built lazily: constructing a client shells out to gcloud, and a host that
    # merely LISTS this server's tools should not trigger an auth prompt. Built
    # once, under a lock, by whichever call needs it first.
    held: dict[str, SwarmClient] = {}
    building = threading.Lock()

    def _client() -> SwarmClient:
        with building:
            if "client" not in held:
                held["client"] = SwarmClient()
            return held["client"]

    def _answer(message_id: Any, params: dict[str, Any]) -> None:
        try:
            text = _call(_client(), params.get("name", ""), params.get("arguments") or {})
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

    with ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix="swarm-mcp-call"
    ) as calls:
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
                calls.submit(_answer, message_id, message.get("params") or {})
            elif message_id is not None:
                _error(message_id, -32601, f"method not found: {method}")
        # Leaving the `with` waits for every call still running, so a host
        # that closes stdin after its last request still gets every answer.
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
