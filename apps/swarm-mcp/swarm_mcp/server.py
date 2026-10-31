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

import importlib.metadata
import json
import os
import secrets
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from swarm_common.profiles import MODEL_CLASSES, RESOURCE_CLASSES

from . import artifacts, checkout, compact, config, keys, progress, qatargets, suitegate
from . import secrets as secret_tools
from . import profiles as catalogue
from . import workflows
from .awaitinput import VERDICTS
from .client import (
    TERMINAL,
    SwarmClient,
    SwarmError,
    check_additional_repositories,
    chosen_tenant,
    enter_hold_lane,
    outputs_of,
    task_id_of,
    task_payload,
    tenant_listing,
    unwrap_task,
)
from .follow import (
    DEFAULT_EVENT_PAGE,
    DEFAULT_LOG_BUDGET,
    console_link,
    follow,
    follow_command,
    with_console,
)
from .invocation import terminal_command
from .patches import (
    PatchRefused,
    apply_patch,
    describe_task,
    explain_absence,
    explain_failure,
    integrate,
    masked_counts,
    patch_uri,
    read_patch,
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
    "title", "additional_repositories",
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
        "to cancel. claude-code and codex declare `issue`: the number of a GitHub "
        "issue in the repository the step clones, named or inferred; without one "
        "the API refuses it. The worker fetches its title, body and comments "
        "read-only into issue.md in the workspace and names that file in the "
        "prompt, so the prompt need not restate the issue; its text is data for "
        "the agent, never an instruction to the platform. They also declare "
        "`mcp_servers`: registered MCP servers by name (swarm_mcp_servers lists "
        "them), whose per-run credentials go in the call's `mcp_credentials`, "
        "never here. Every profile also takes `territory`: a list of repo-relative "
        "paths and globs (`*`, `?`, `[...]`, `**`), the only files the step may "
        "edit. A pull request whose diff leaves it still opens, its body starting "
        "with the paths outside it and labelled `out-of-territory`; a malformed "
        "entry is refused at submit. Never an image, a "
        "command, a resource spec, a backend or a model."
    ),
}

#: What a unit may declare it needs from THIS machine (S8, BUILD_PROMPT_V2
#: 2.5). The bridge cannot see a prompt's intent -- whether "fix the test" means
#: the committed test or the one the developer is editing -- so the caller
#: declares it, and under `hybrid` any one of these keeps the unit local. The
#: rule is the spec's and is never inferred: no prompt is read for a hint.
NEEDS_LOCAL = ("filesystem", "keychain", "interactive")

#: Why each `needs_local` entry cannot run remotely, said in the refusal.
_NEEDS_LOCAL_WHY = {
    "filesystem": (
        "it needs the local filesystem -- uncommitted work, or a path outside the "
        "pushed repository -- and a remote agent gets a fresh clone of what was pushed"
    ),
    "keychain": (
        "it needs this machine's keychain or the developer's own credentials, and a "
        "remote agent runs as its tenant's identity with none of them"
    ),
    "interactive": (
        "it needs the developer to answer while it runs, and a remote agent has "
        "nobody to ask"
    ),
}

_TARGET_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(config.TARGETS),
    "description": (
        "Where THIS call's work goes, overriding the session default (the sc "
        "plugin's `default_target`, hybrid unless set) for this call only. "
        "`cloud`: dispatch it. `local`: send NOTHING -- the reply refuses and says "
        "the work is to run locally in this session. `hybrid`: a unit that names a "
        "runner profile goes to the cloud, unless the call declares `needs_local`, "
        "in which case nothing is sent and the reason is named. Every reply states "
        "the target that applied and where it came from."
    ),
}

_NEEDS_LOCAL_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "enum": list(NEEDS_LOCAL)},
    "description": (
        "What this work needs from THIS machine, declared by the caller because "
        "the bridge cannot see it: `filesystem` (uncommitted work, or a path "
        "outside the pushed repository), `keychain` (the keychain or the "
        "developer's own credentials), `interactive` (the developer answering as "
        "it runs). Under target `hybrid` any entry keeps the work local and "
        "nothing is sent. Under `cloud` it is not consulted, and the reply says so."
    ),
}

#: The model class a task, a step or a run's planner runs (#1435,
#: docs/design/model-per-step.md §4.8). A NAME from the frozen `MODEL_CLASSES`,
#: checked against the profile by `profiles.check_model_class` before anything
#: is sent; the platform runs the class's pinned model. Never a model ID.
_MODEL_CLASS_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(MODEL_CLASSES),
    "description": (
        "The model class to run, BY NAME; omit it for the profile's default (`opus`: "
        "coding and implementation). `fable` for planning, `sonnet` for summarising "
        "and reformatting, `haiku` for classifying and extracting. The platform runs "
        "the class's pinned model (swarm_profiles lists them); a model ID is not "
        "accepted here. Only profiles that run a Claude model take one."
    ),
}

#: The registered repositories a task or step reads beside its own (#1434,
#: docs/design/multi-repository-steps.md). NAMES, checked for shape by
#: `client.check_additional_repositories` before anything is sent; swarm-api
#: resolves each from the workspace's registration and pins its ref. `infer`
#: never touches this list: it only ever picks the primary repository.
_ADDITIONAL_REPOSITORIES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "repository": {"type": "string", "description": "owner/repo, registered by your workspace. Never a URL."},
            "ref": {"type": "string", "description": "Branch, tag or full sha; default the registration's branch."},
            "name": {"type": "string", "description": "The directory under ../repos/; default the repo part, lower-cased."},
        },
        "required": ["repository"],
        "additionalProperties": False,
    },
    "description": (
        "Repositories your workspace registered that the agent READS beside its own, "
        "each `{repository: \"owner/repo\", ref?, name?}`. "
        "SwarmCloud pins each ref to a commit at submission and checks it out read-only "
        "at ../repos/<name>, beside the agent's repository; a change made there is never "
        "collected or pushed. Names only: a URL is refused before anything is sent. "
        "SwarmCloud refuses a repository not registered or not readable by tasks, a "
        "list over the platform's current limit, and a profile that does not take them "
        "(GKE agent profiles do). Off until the deployment enables it."
    ),
}

_FILES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "The repository paths (or `dir/` prefixes) this unit will EDIT -- its "
        "territory. Read by this bridge and never sent: the repository's knowledge "
        "graph expands them to the callers and tests a change reaches, and the reply's "
        "`territory_warning` names every open pull request, live issue-run step, other "
        "unit of this call, or still-running task this session dispatched that already "
        "holds part of it. A warning, never a refusal. Silent when the repository has "
        "no version-3 index."
    ),
}

#: The most entries a per-run credential map may hold: the `mcp_servers` input
#: names one to four servers (contract request 88), as swarm-api's
#: `MAX_MCP_CREDENTIALS` says, so a fifth entry could only be refused there.
MAX_MCP_CREDENTIALS = 4

#: The per-run MCP credentials of a dispatch, a batch task or a workflow
#: (docs/design/registered-mcp-servers.md §4.3 and §4.11). Passed to swarm-api
#: as given, top-level beside `input`, and to NOTHING else: never into the
#: dispatch signature, a reply, a run copy, `launched.jsonl` or a refusal.
#: swarm-api owns every rule past the shape (the 8 B-8 KiB printable line,
#: `expires_in` 60-86400, which servers take a credential at all), because a
#: second statement of those here would be a second thing to keep in step.
_MCP_CREDENTIALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "maxProperties": MAX_MCP_CREDENTIALS,
    "additionalProperties": {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "description": (
                    "The credential the caller's own server checks, minted for this run. "
                    "One printable line, 8 B to 8 KiB."
                ),
            },
            "expires_in": {
                "type": "integer",
                "description": "Seconds it stays stored, 60 to 86400; it is also destroyed when the task ends.",
            },
        },
        "required": ["value", "expires_in"],
        "additionalProperties": False,
    },
    "description": (
        "Per-run credentials for the registered MCP servers this work names in "
        "`inputs.mcp_servers`, by server name: {\"kb\": {\"value\": ..., \"expires_in\": 3600}}. "
        "Only for a server registered with auth mode `dispatch` (swarm_mcp_servers says "
        "which); a `none` or `workspace_secret` server takes none. Sent to swarm-api, "
        "which stores it in Secret Manager and keeps it from the agent, and nowhere "
        "else: no reply, log or run file of this plugin holds it. Never a long-lived "
        "secret for the workspace store, which no tool accepts, and never a "
        "registration: a server is registered in the console."
    ),
}

#: A batch entry's fields: what one `swarm_dispatch` takes for one task, with
#: the profile under the API's own name. Read by `_dispatch_batch`, so the
#: schema and the check cannot disagree.
_BATCH_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "This task's instructions."},
        "runner_profile": {
            "type": "string",
            "default": "claude-code",
            "description": "A runner profile BY NAME, as `profile` is for one task.",
        },
        "repo": {"type": "string", "description": "As `repo` for one task."},
        "ref": {"type": "string", "description": "As `ref` for one task."},
        "infer": {"type": "boolean", "default": False, "description": "As `infer` for one task."},
        "strategy": {"type": "string", "enum": list(_DISPATCH_STRATEGIES), "default": "collect"},
        "label": {"type": "string", "description": "A short name, for the UI."},
        "inputs": _INPUTS_SCHEMA,
        "files": _FILES_SCHEMA,
        "mcp_credentials": _MCP_CREDENTIALS_SCHEMA,
        "additional_repositories": _ADDITIONAL_REPOSITORIES_SCHEMA,
        "model_class": _MODEL_CLASS_SCHEMA,
    },
    "required": ["prompt"],
}

#: The `swarm_dispatch` arguments that describe ONE task, refused beside `tasks`.
_SINGLE_TASK_ARGS = ("prompt", "profile", "repo", "ref", "infer", "strategy", "label", "inputs", "files",
                     "mcp_credentials", "additional_repositories", "model_class")

#: Why `tasks` takes no `publish_to_pr` (#2183, PTP-DISPATCH): a pull request's
#: branch takes one lane's fast-forward, so several tasks publishing onto one
#: would race, and only one of them could land.
_BATCH_PUBLISH_REFUSAL = (
    "publish_to_pr pushes one task's commits onto one existing pull request: one "
    "pull request, one publishing task. Dispatch that task alone, with `prompt` and "
    "`publish_to_pr`. Nothing was sent"
)

#: `swarm_account_pause`/`resume`/`drain` share one shape.
_ACCOUNT_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account": {
            "type": "string",
            "description": (
                "The account's label or id, as swarm_accounts lists it. A label "
                "resolves to YOUR OWN account only; a label two of your accounts "
                "share is refused with both ids."
            ),
        },
        "reason": {"type": "string", "description": "Recorded with the state change."},
    },
    "required": ["account"],
}

#: `swarm_workflow_spec`'s window over ONE step's prompt (LAUNCH-LARGE-SPEC,
#: #2875): its reply carries an outline, never every prompt -- a 24-step spec
#: with ~90 KB of prompts answered 86,506 characters, over Claude Code's
#: tool-output limit, and its run ended NOT_SUBMITTED. A prompt is read only
#: on request, a step at a time, at most this many characters per call.
_PROMPT_WINDOW_DEFAULT = 8000
_PROMPT_WINDOW_MAX = 16000
#: How much of a prompt's first line the outline shows.
_PROMPT_FIRST_LINE_CHARS = 160

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_dispatch",
        "description": (
            "Run an agent in SwarmCloud instead of locally. Returns a task id "
            "immediately; the agent runs remotely on its own tenant identity. "
            "Use this the way you would spawn a local subagent, then call "
            "swarm_follow with the returned id to watch it work, and "
            "swarm_wait or swarm_result for the outcome.\n"
            "\n"
            "Every task is tagged with this Claude Code session (`metadata.claude_session`, "
            "echoed as `claude_session`), so the sc plugin's check, attach-all and watch "
            "count it as this session's own; the reply carries `arm_watch`, the CronCreate "
            "call that arms the `/sc check` watch (CronList first, never a second job).\n"
            "\n"
            "SEVERAL INDEPENDENT TASKS ARE ONE CALL: pass `tasks` -- a list of "
            "{prompt, runner_profile, repo, ref, infer, strategy, label, inputs, files, "
            "mcp_credentials, additional_repositories} "
            "-- instead of `prompt` and the fields beside it. They are sent as ONE "
            "request, every task id comes back in the order given, and a list "
            "longer than the API's max_batch_size is refused before anything is "
            "sent. Collect them with swarm_collect. Units that depend on each "
            "other are a workflow: use swarm_workflow.\n"
            "\n"
            "WHERE IT GOES is `target`: this call's, else the session default. "
            "`local` sends nothing; `hybrid` sends nothing when `needs_local` "
            "names anything. The reply's `target` says which applied and why.\n"
            "\n"
            "THE REPLY STARTS ITS OWN ROWS: `rows` is a titled /sc:swarmcloud "
            "script that shows each task just sent as one live [SwarmCloud] row. "
            "Launch it at once with the Workflow tool, {scriptPath: "
            "<rows.script_path>, args: <rows.args>}, as `rows.start_now` says; "
            "`rows.error` means it could not be written, and `/sc attach --all` "
            "gives the task its row instead.\n"
            "\n"
            "TERRITORY: a task that names the files it will edit (`files`) is "
            "checked against the repository's knowledge graph before it is sent; "
            "the reply's `territory_warning` names every open pull request, live "
            "issue-run step, other task of the call or still-running task of this "
            "session it overlaps. The task is sent either way.\n"
            "\n"
            "REGISTERED MCP SERVERS: claude-code and codex take `inputs.mcp_servers`, "
            "the names of servers your workspace registered (swarm_mcp_servers lists "
            "them), and the agent may call those servers during its run. A server "
            "registered with auth `dispatch` needs this call's `mcp_credentials` entry "
            "for it, a credential your application minted for this run; it goes to "
            "SwarmCloud and nowhere else, and the agent never sees it. Until the "
            "deployment turns the feature on, SwarmCloud refuses any `mcp_servers` "
            "with `mcp_unavailable`.\n"
            "\n"
            "MORE THAN ONE REPOSITORY: `additional_repositories` names registered "
            "repositories the agent reads, read-only, at ../repos/<name> beside its own; "
            "only its own repository's changes come back.\n"
            "\n"
            "A FOLLOW-UP TASK ON AN EXISTING PULL REQUEST (#2183): `publish_to_pr` names a "
            "pull request of the task's repository (41, \"#41\", owner/repo#41 or its URL), "
            "and the task's commits are pushed onto that pull request's branch as a "
            "fast-forward instead of opening a new one. One task only: beside `tasks`, or "
            "in one of them, it is refused before anything is sent, because one pull "
            "request takes one publishing task. It is sent top-level, only when given. "
            "The API checks the rest: `strategy: direct-pr`, a repository, no `ref`, and a "
            "pull request that is open, not from a fork, on a `swarm/task_*` branch a lane "
            "of your tenant published. The reply's `publish_to_pr` says what was sent."
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
                        "Repository to clone (https or ssh). Omit it, and omit "
                        "`infer`, to dispatch with no repository at all -- the task "
                        "can then produce no patch and no pull request. Pass `infer: "
                        "true` instead of this to clone the repository and PUSHED "
                        "branch of the git checkout this bridge runs in, pinned at "
                        "its current commit."
                    ),
                },
                "ref": {"type": "string", "description": "Branch, tag or commit."},
                "infer": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "With no `repo`: clone this checkout's repository and PUSHED "
                        "branch, pinned at its current commit -- never the branch "
                        "name, which can move after this dispatch is sent. The "
                        "branch must be pushed and have no unpushed commits, or the "
                        "dispatch is refused with the command that fixes it; "
                        "uncommitted changes are reported as invisible to the agent. "
                        "Outside a checkout nothing is cloned and the reply says why. "
                        "Ignored when `repo` is given."
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
                "files": _FILES_SCHEMA,
                "mcp_credentials": _MCP_CREDENTIALS_SCHEMA,
                "additional_repositories": _ADDITIONAL_REPOSITORIES_SCHEMA,
                "model_class": _MODEL_CLASS_SCHEMA,
                "publish_to_pr": {
                    "type": ["integer", "string"],
                    "description": (
                        "An existing pull request of this task's repository to push its "
                        "commits onto, instead of opening one (#2183): its number (41), "
                        "\"#41\", owner/repo#41 or its https://github.com/<owner>/<repo>/"
                        "pull/<n> URL. Needs `strategy: direct-pr`. One task only: refused "
                        "beside `tasks`."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": _BATCH_TASK_SCHEMA,
                    "description": (
                        "Several independent tasks in ONE request, instead of "
                        "`prompt` and the one-task fields beside it. At most the "
                        "API's max_batch_size (its /v1/stats `limits`). Each is "
                        "checked as a single dispatch is, and a task identical to "
                        "another in the list, or to one already dispatched in this "
                        "session, refuses the whole list before anything is sent."
                    ),
                },
                "target": _TARGET_SCHEMA,
                "needs_local": _NEEDS_LOCAL_SCHEMA,
            },
            # `prompt` OR `tasks`, checked in `_call`, for the reason
            # swarm_workflow gives about `steps` OR `spec`.
            "required": [],
        },
    },
    {
        "name": "swarm_profiles",
        "description": (
            "The runner profiles this platform will accept BY NAME, with what "
            "each one is: whether it can be dispatched at all and, when it "
            "cannot, why; which backend it lands on; its cpu, memory and "
            "workspace size; its timeout; whether it needs a provider "
            "credential; and the model classes a step on it may name "
            "(`model_classes`, its `default_model_class`, and `models`, each "
            "class's pinned model ID as the platform serves it). Read this "
            "before dispatching rather than guessing a "
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
        "name": "swarm_tenants",
        "description": (
            "The tenants you may act as (GET /v1/tenants/mine: tenant_id and "
            "display_name, the group's email), each marked `current` when it is "
            "the one this bridge acts as. `current` is the API's own answer; "
            "`chosen` is the bridge's `tenant` setting, null when none is set and "
            "the API picks your first matching tenant. The setting is "
            "SWARM_TENANT in the bridge's environment, read once at start, and "
            "applies to every call this session makes; there is no per-call "
            "tenant. An empty list means your tenant is personal and there is "
            "nothing to choose. Each entry carries your `role` there "
            "(`{name, source}`: viewer, member or admin, and whether it came from "
            "an override, the workspace's default, your personal workspace or "
            "being a platform admin) and `via` (`member`, `personal`, or "
            "`platform_admin` for a group workspace you reach as a platform admin "
            "without being in it, where every action is audited); both are null "
            "when the API did not send them. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ROLES-MCP (docs/design/workspace-roles.md §4.8, owner answer Q11): the
    # roster is READ here and nowhere written. No tool grants or revokes a
    # role -- an agent driving a person's own session must not be able to
    # raise anyone -- so the schema takes nothing, and a change is the
    # person's own, typed at their terminal (sc roles set).
    # tests/unit/mcp/test_roles_bridge.py.
    {
        "name": "swarm_roles",
        "description": (
            "The roles of the workspace this bridge acts in (GET /v1/workspace/roles): "
            "its default role, the people with an override and their role, your own "
            "role with its source and `via`, what each role permits (viewer sees and "
            "changes nothing; member submits, approves, answers and cancels; admin "
            "also manages secrets, MCP servers, provider keys, settings and roles), "
            "whether you may change roles here (`can_manage`), and the recent role "
            "and platform-admin events. Read-only: no tool changes a role. A "
            "workspace admin does that in the console (Settings > People) or in "
            "their own terminal with the sc roles set, clear or default command, "
            "which asks them to type the change back; never run it for them. `text` is what to show."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
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
        "name": "swarm_collect",
        "description": (
            "Wait for these tasks as swarm_wait does, then return, in ONE reply "
            "and in the order given, each task's state and -- for a task that "
            "FINISHED -- what swarm_result says it produced. A task that has not "
            "finished when the wait ends is reported `finished: false` with its "
            "state and NO result: it is never presented as one. A task that could "
            "not be read is `finished: null` with `read_error`. `still_running` "
            "lists the unfinished ones. This does not stream; for progress while "
            "they run, poll swarm_follow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}},
                "wait_seconds": {
                    "type": "integer",
                    "default": 3600,
                    "description": (
                        "How long to wait for every task to finish before "
                        "answering. 0 reads once and answers at once."
                    ),
                },
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
            "FOR A WORKFLOW STEP'S ROW (`sc:step`), pass `format: \"progress\"`: "
            "no log lines at all, one short line per task only when it changed "
            "(state; elapsed; attempt n/m; last checkpoint age; tokens and cost "
            "so far; what a waiting task waits for), the state `transitions` "
            "since `since`, `changed`, and a finished task's `outcome`; the "
            "reply that says `stop: true` also carries `result`, the step's "
            "answer with no null in it, to return as given -- a few "
            "hundred bytes a call. With `step_id`, a task PARKED on "
            "`CI_PENDING` (a merge waiting on its pull request's checks) also "
            "says `stop: true`, with a `PARKED` `result` naming the park and "
            "how to resume, rather than holding hour after hour. A task PARKED "
            "on `HUMAN_REQUIRED` -- the merge's owner-question hold or a "
            "hand-off -- says `stop: true` with `stop_reason: "
            "\"waiting_on_person\"` and a `PARKED` `result` whose "
            "`waiting_on_person` names the questions (asking step, index, text), "
            "the console link and, for a hand-off, the human-ready route. A wait "
            "line says when the wait began (`parked since 03:05Z (4h 30m)`), and "
            "an unchanged line comes back at most every 30 minutes "
            "(`refreshed`). `held_s` is how long the call held; holds that keep "
            "returning in under 60 s with nothing changed answer `stop_reason: "
            "\"hold_wait\"` (wait, then follow again) and, at five, "
            "`\"hold_not_holding\"`. A call holds up to `wait_seconds` (max "
            f"{compact.MAX_WAIT_SECONDS}) and returns early only when a task's "
            "STATE changes -- waiting, parked (and why), holding capacity, or "
            "finished; progress inside one state does not end it. With "
            "`parents` (the task ids of the step's unfinished parents) even the "
            "first call holds, until every parent has finished or the task "
            "leaves its dependency wait. Measured 2026-10-01: rows following "
            "`lines` cost 5-16 KB a reply and 4.0M tokens for one row.\n"
            "\n"
            "FOR A WHOLE WORKFLOW'S ONE ROW (`sc:summary`), pass `format: "
            "\"summary\"` and `summary_of` from the row's prompt, unchanged, with "
            "EVERY task id it was handed on every call: the reply's `line` is "
            "the workflow's count line (`18 steps: 6 running · 9 queued · 3 done "
            "· 0 failed · PR #N`), counted by the bridge in the buckets "
            "/sc:swarmcloud uses -- write it verbatim when `line_changed` is "
            "true. A task already seen finished is not read again; the rest "
            "hold as `progress` does.\n"
            "\n"
            "FOR AN AGENT THAT RELAYS A REMOTE AGENT'S WORK (`sc:remote`), "
            "pass `format: \"lines\"`: the answer is then short narrated lines -- "
            "what the remote agent said, which tools it called, where a waiting "
            "task is waiting and why -- plus `since`, a short handle (such as "
            "`r7f3a2`) to pass back unchanged, instead of the full report. With `wait_seconds` a call "
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
                        "The `since` a previous call returned, passed back "
                        "UNCHANGED. With `lines` and `progress` it is a short "
                        "handle, `r` and five hex digits (such as `r7f3a2`): "
                        "the bridge keeps the position behind it, and a handle "
                        "changed on the way is refused -- copy it again from the "
                        "previous reply. A full token from an older bridge is "
                        "still read. The first call has none; with `lines` or "
                        "`progress`, a later call that has none, for tasks this "
                        "bridge already answered, resumes from the last position "
                        "it returned and holds like any follow. Pass `since` or "
                        "`cursor`, not both."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["json", "lines", "progress", "summary", "stage"],
                    "default": "json",
                    "description": (
                        "`json` (default): the full report. `lines`: short "
                        "narrated lines of the agent's log, a `since` token and, "
                        "for a finished task, its `outcome`. `progress`: no log "
                        "at all -- one progress line per task when it changed, "
                        "the state transitions, `since` and `outcome`; the "
                        "shape a workflow step's row reads. `summary`: the "
                        "sc:summary row's -- `progress`'s hold and rows, no "
                        "per-task line, and `line`, the whole workflow's count "
                        "line, with `line_changed`; needs `summary_of`. `stage`: "
                        "the sc:finish row's (the pane layout's one row per "
                        "workflow) -- the call holds through every step change "
                        "that leaves the workflow's stage where it was, and "
                        "answers `key`, `line` and `stage_changed`; needs `stage_of`."
                    ),
                },
                "stage_of": {
                    "type": "object",
                    "description": (
                        "With `format: \"stage\"` only, and required there: the "
                        "object the sc:finish row's prompt gives on its `stage_of:` "
                        "line, passed UNCHANGED on every call -- `stages` (every "
                        "task id of the workflow to its console stage), `states` "
                        "(the tasks not followed, at their states) and `key`, the "
                        "stage key last written."
                    ),
                },
                "summary_of": {
                    "type": "object",
                    "description": (
                        "With `format: \"summary\"` only, and required there: the "
                        "object the sc:summary row's prompt gives on its "
                        "`summary_of:` line, passed UNCHANGED on every call -- "
                        "`noun`, `total`, the `counted` running/queued/done/failed "
                        "of the items not followed, their `prs`, `console` and "
                        "the `line` last written."
                    ),
                },
                "wait_seconds": {
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "`lines` and `progress` only: gather for up to this many "
                        "seconds (max 300 for `lines`, "
                        f"{compact.MAX_WAIT_SECONDS} for `progress`) before "
                        "returning. `lines` returns early when every task has "
                        "finished or a task starts, or the read budget is spent; "
                        "`progress` returns early only when a task's state "
                        "changes or every task has finished. The first call, "
                        "for tasks this bridge has not answered yet, returns "
                        "at once -- unless it names `parents`."
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
                "parents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "`progress` only, with exactly ONE task id: the task ids of "
                        "that step's parents. While the task is SUBMITTED, QUEUED "
                        "or PARKED on DEPENDENCY_INCOMPLETE and any parent has not "
                        "finished, the call holds -- the first call too -- so a "
                        "step that has not started makes one call while its "
                        "parents run. The reply's `parents` gives each parent's "
                        "state."
                    ),
                },
                "step_id": {
                    "type": "string",
                    "description": (
                        "`lines` or `progress` only, with exactly ONE task id: the workflow step "
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
            "the deployment sent no count.\n"
            "\n"
            "`outputs` says what the task made beyond its code: each artifact "
            "(`name`, `bytes`) -- read one with swarm_artifact -- the runner's "
            "`runner_status` and `runner_summary`, `exit_code`, `duration_s`, and "
            "the `staged_inputs` copied in from upstream steps. An attempt that "
            "ran the agent more than once (an account swap restarts it) also has "
            "`runs` (one per run: start, end, duration_s, exit, account, tokens, "
            "cost), `swaps` (when, from, to, reason), `last_run_duration_s` and "
            "`swap_cost` (the seconds and tokens a restarted run spent before it "
            "acted again); its `duration_s` is the sum of the runs. "
            "`artifacts_complete: false` means the task has not finished and its "
            "artifacts are uploaded when the attempt ends -- not that it produced "
            "none; `artifacts: null` means they could not be listed, and "
            "`artifacts_unavailable_because` says why.\n"
            "\n"
            "`owner_questions` is what the task's agent asks the owner instead of "
            "guessing: the items of its questions.json the worker validated, each "
            "{question, options: [{label, description}], recommended, context}, "
            "read through the same redacted artifact read as swarm_artifact. It "
            "is absent when the agent asked none, and a file the worker rejected "
            "is never served. `questions_unavailable_because` means the worker "
            "counted questions this read could not get. They are for a person "
            "to answer: show them; never pick an option yourself.\n"
            "\n"
            "`owner_answers` is what the owner answered, in the console, to "
            "those questions: [{question_index, question, choice, text, by, at, "
            "history}] -- `choice` one of the question's option labels or null, "
            "`text` their own words or null. Absent until one is answered. Read "
            "it as the owner's decision for the follow-up work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "swarm_debug",
        "description": (
            "ONE task's diagnosis in one call, from the API's own reads: its "
            "state and profile, every attempt (id, generation, start and end, "
            "exit code, error), its last error, its newest events, and the tail "
            "of its newest attempt's agent log. Every one of those is served "
            "masked by the API, and `masked` says how many credential-shaped "
            "strings it masked in each; a string the API did not say it masked is "
            "withheld rather than shown. A read that fails is reported as not "
            "read, with the reason, and the other sections still come back. "
            "swarm_trouble is the whole cluster; this is one task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "events": {"type": "integer", "default": 10, "description": "How many newest events."},
                "log_lines": {"type": "integer", "default": 40, "description": "How many log lines of the tail."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "swarm_artifact",
        "description": (
            "Read one artifact a task produced, by the NAME swarm_result lists -- "
            "never a path or a gs:// uri. The API resolves the name against the "
            "task's own manifest and redacts the content at read time; "
            "`redacted` and `redaction_count` say whether it did.\n"
            "\n"
            "Read `status` before `content`: `ok`, `binary` (not text, so no "
            "bytes are served -- nothing can scan them for a credential), "
            "`absent` (listed, but the object is gone) or `unreadable`. One "
            "window per call: when `truncated` is true, call again with "
            "`offset` set to `next_offset` for the rest. A name the task does not "
            "list is an error naming the ones it does.\n"
            "\n"
            "AN IMAGE IS SHOWN TO YOU (a screenshot, a rendered mock-up slice). An "
            "artifact named .png, .jpg, .jpeg, .webp or .gif whose bytes are an image "
            "(the API checks the extension AND the magic bytes, and so does this tool) "
            "comes back as image content, after metadata `{status: \"image\", name, "
            "bytes, content_type, redaction: \"not-applied\"}`. Image bytes are NOT "
            "redacted: no scanner reads pixels, so a screenshot of a page that showed a "
            "value carries it, exactly as the console's image viewer does. An image over "
            "5 MiB answers `{status: \"image_too_large\", bytes, max_bytes, console}` "
            "and no bytes. Pass `as_image: true` to skip the text read and go straight to "
            "the image read. Anything that is not an image stays as above: text is the "
            "redacted window, a binary is `binary` with no bytes.\n"
            "\n"
            "TO KEEP AN IMAGE AS A FILE: pass "
            "`save_to`, a directory under your working directory or your "
            "scratchpad. An artifact named .png, .jpg, .jpeg, .webp or .gif that "
            "the API serves as an image is written to "
            "`<save_to>/<task_id>/<name>` and the answer is `{status: \"saved\", "
            "path, bytes, content_type}`: open `path` with your own Read tool to "
            "see it. A text artifact is still the redacted window above (nothing "
            "is saved), and a binary that is not an image stays `binary` and is "
            "not saved. A `save_to` outside those directories, or a name with "
            "`..` or an absolute path, is refused before anything is read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string", "description": "The artifact's name, as swarm_result lists it."},
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Byte to start at: 0, or the `next_offset` a previous call returned.",
                },
                "limit_bytes": {
                    "type": "integer",
                    "description": "Window size. The API clamps it into its own range and says so.",
                },
                "as_image": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Read an image-named artifact straight through the image read, "
                        "without the text read first. Bytes that are not an image still "
                        "answer the redacted text read or `binary`, never raw."
                    ),
                },
                "save_to": {
                    "type": "string",
                    "description": (
                        "A directory, under your working directory or your scratchpad, to "
                        "save an IMAGE artifact into as <save_to>/<task_id>/<name>; read the "
                        "returned `path` with the Read tool. Ignored for text, which is "
                        "returned redacted as usual."
                    ),
                },
            },
            "required": ["task_id", "name"],
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
            "`repository_ref`, `on_step_failure`, `priority`, `label`, "
            "`additional_repositories`, `after`, `merge`, `title`, `publish_to_pr`), nothing "
            "else beside it but `infer`, `mcp_credentials` and `publish_to_pr` -- or, so no model has to retype a long "
            "spec, as `spec_path` (a JSON spec file this bridge reads itself), "
            "`spec_ref` (the id `swarm_workflow_spec` handed back for a file it "
            "read) or `spec_handle` (the file swarm_workflow_launch wrote the spec "
            "it embedded to). `spec_digest` checks all four. With no repository named and no `infer`, "
            "no step clones a repository. `infer: true` clones the repository and "
            "pushed branch of the checkout this bridge runs in for EVERY step, "
            "pinned at its current commit, under the same rules as swarm_dispatch.\n"
            "\n"
            "TERRITORY: a step may name the files it will edit (`files`, never sent). "
            "Steps that declare them are checked against the repository's knowledge "
            "graph before submission, and the reply's `territory_warning` names every "
            "open pull request, live issue-run step, still-running task of this session, "
            "or step of this workflow NOT on its dependency line that overlaps one. The "
            "workflow is submitted either way.\n"
            "\n"
            "REGISTERED MCP SERVERS: a claude-code or codex step names them in "
            "`inputs.mcp_servers`, as swarm_dispatch does. The workflow takes ONE "
            "`mcp_credentials` map, top-level, which serves every step naming that "
            "server; it is never part of a spec, because a spec is written, held and "
            "copied into a run script. So a workflow that needs a credential is "
            "submitted with this tool, `mcp_credentials` beside `steps` or the spec, "
            "and not through /sc:swarmcloud's launch.\n"
            "\n"
            "MORE THAN ONE REPOSITORY: top-level `additional_repositories` names the "
            "registered repositories every step reads at ../repos/<name>; a step's own "
            "`additional_repositories` REPLACES that list, and `[]` means none. Every "
            "step of one submission reads each at the same pinned commit. `infer` never "
            "changes them.\n"
            "\n"
            "A FOLLOW-UP LANE ON AN EXISTING PULL REQUEST (#2183): `publish_to_pr` -- the "
            "spec's top-level key, or this tool's argument beside `steps` or a spec -- "
            "names a pull request of the workflow's repository (41, \"#41\", "
            "owner/repo#41 or its URL), and the lane's commits are pushed onto that pull "
            "request's branch as a fast-forward instead of opening a new one. It is sent "
            "top-level, and NO `merge` is sent with it: such a lane opens no pull request, "
            "so a spec that names it beside a merge step or a `merge` other than \"off\" "
            "is refused before anything is sent, with the API's reason. The API checks the "
            "rest: `direct-pr` of one agent step or `integrate`, a repository, no "
            "`repository_ref`, no `carrier: branches`, and a pull request that is open, "
            "not from a fork, on a `swarm/task_*` branch a lane of your tenant published. "
            "The reply's `publish_to_pr` says what was sent and that no merge was."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": (
                        "A whole workflow spec -- the file the terminal's workflow "
                        "command reads -- instead of `steps` and the "
                        "parameters beside it. A step may carry `stage`, a note for "
                        "the spec's reader that is never sent: /sc:swarmcloud's rows "
                        "show the console's stage, the step's level in the DAG. The "
                        "spec's optional `title` is its short name, sent as "
                        "`metadata.title`."
                    ),
                },
                "spec_path": {
                    "type": "string",
                    "description": (
                        "In place of `spec`: the path of a JSON workflow spec file, "
                        "relative to this checkout or absolute, and under the checkout "
                        "either way (a path outside it is refused). The bridge reads the "
                        "bytes itself -- checked as swarm_workflow_spec checks them -- "
                        "so a long spec reaches the API without being retyped."
                    ),
                },
                "spec_ref": {
                    "type": "string",
                    "description": (
                        "In place of `spec`: the `spec_ref` swarm_workflow_spec "
                        "returned. The bridge submits the spec it read then, held in "
                        "this process; a ref from another session or a restarted "
                        "bridge is refused, and nothing is sent."
                    ),
                },
                "spec_handle": {
                    "type": "string",
                    "description": (
                        "In place of `spec`: the path of the spec handle "
                        "swarm_workflow_launch wrote beside its titled copy "
                        "(`spec-<digest>-<hex>.json` in a sc-swarmcloud-runs folder; any "
                        "other path is refused). The bridge reads the bytes the launch "
                        "embedded and checks them against `spec_digest`, which this "
                        "takes as required, so /sc:swarmcloud's submit never retypes a spec."
                    ),
                },
                "spec_digest": {
                    "type": "string",
                    "description": (
                        "With `spec`, `spec_path`, `spec_ref` or `spec_handle`: the digest the caller computed of the spec "
                        "it meant to send (`fnv1a32:` and eight hex digits, over the "
                        "spec as sorted-key compact JSON). When it differs from the "
                        "digest of the spec received, NOTHING is submitted: the spec "
                        "was changed on the way here, by whoever retyped it. The reply "
                        "carries the digest of what was received either way."
                    ),
                },
                "mcp_credentials": _MCP_CREDENTIALS_SCHEMA,
                "publish_to_pr": {
                    "type": ["integer", "string"],
                    "description": (
                        "An existing pull request of the workflow's repository to push the "
                        "lane's commits onto, instead of opening one (#2183): its number "
                        "(41), \"#41\", owner/repo#41 or its https://github.com/<owner>/"
                        "<repo>/pull/<n> URL. Beside `steps` or a spec; a spec may carry it "
                        "as its own top-level key, and the two must name the same pull "
                        "request. No merge is sent with it, and a merge step or a `merge` "
                        "other than \"off\" beside it is refused before anything is sent."
                    ),
                },
                "merge_pr": {
                    "type": "object",
                    "description": (
                        "Merge a pull request NO workflow opened, at the head sha you "
                        "name (#352): submits one `merge` step and nothing else, so it "
                        "takes no `steps`/`spec` and only `repo` and `title` beside it. "
                        "With no `title` the run is named \"Merge PR <number>\". "
                        "The repository is `repo` when given, which must be one your "
                        "tenant registered, else your tenant's only registered one. The "
                        "API refuses a pull request that is closed, merged, from a fork, "
                        "or whose head is not `head_sha` now; the step merges only once "
                        "every required check is green at that head."
                    ),
                    "properties": {
                        "number": {"type": "integer", "minimum": 1},
                        "head_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    },
                    "required": ["number", "head_sha"],
                    "additionalProperties": False,
                },
                "infer": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "With no repository named in `steps`/`spec`: clone this "
                        "checkout's pushed branch, pinned at its current commit, for "
                        "every step. Omitted, no step clones a repository."
                    ),
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
                            "model_class": _MODEL_CLASS_SCHEMA,
                            "timeout_seconds": {"type": "integer"},
                            "when": {
                                "type": "object",
                                "properties": {
                                    "step": {"type": "string"},
                                    "verdict_in": {
                                        "type": "array",
                                        "items": {"type": "string", "enum": ["MERGE", "NOT_YET"]},
                                    },
                                },
                                "required": ["step", "verdict_in"],
                                "description": (
                                    "Run this step's agent only when the verdict "
                                    "file it stages from `step` (through "
                                    "input_from) says one of `verdict_in`. "
                                    "Otherwise the step still runs and "
                                    "publishes, without an agent. The review "
                                    "writes {\"verdict\": \"MERGE\"|\"NOT_YET\", "
                                    "\"findings\": [...]}."
                                ),
                            },
                            "builds_on": {
                                "type": "string",
                                "description": (
                                    "An upstream step id whose pushed branch "
                                    "this step's checkout starts from, so a fix "
                                    "sees the code it fixes. Needs strategy "
                                    "`integrate` or `direct-pr`."
                                ),
                            },
                            "allow_empty_diff": {
                                "type": "boolean",
                                "description": (
                                    "The step may end SUCCEEDED with no change "
                                    "(result_summary.no_change) instead of "
                                    "failing on an empty diff; dependants that "
                                    "need its patch are SKIPPED. Under "
                                    "direct-pr the API sets it itself for a "
                                    "reader step (one a dependant depends on "
                                    "and no step builds_on), so a review "
                                    "feeding a fix need not."
                                ),
                            },
                            "runtime": {
                                "type": "string",
                                "enum": ["browser_if_ui"],
                                "description": (
                                    "For a review: run on claude-code-browser only when "
                                    "an upstream step's change touches UI (test files "
                                    "excluded), on claude-code otherwise; the platform "
                                    "decides as the step is readied. Name no "
                                    "runner_profile, or claude-code-browser."
                                ),
                            },
                            "inputs": _INPUTS_SCHEMA,
                            "files": _FILES_SCHEMA,
                            "additional_repositories": {
                                **_ADDITIONAL_REPOSITORIES_SCHEMA,
                                "description": (
                                    "This step's own list, REPLACING the workflow's "
                                    "`additional_repositories`; `[]` reads none. "
                                    + _ADDITIONAL_REPOSITORIES_SCHEMA["description"]
                                ),
                            },
                            "stage": {
                                "type": "string",
                                "description": (
                                    "Display only: the group /sc:swarmcloud shows this "
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
                "title": {
                    "type": "string",
                    "description": (
                        "The workflow's short name, sent as `metadata.title`: what "
                        "Claude Code titles its /sc:swarmcloud run with."
                    ),
                },
                "additional_repositories": {
                    **_ADDITIONAL_REPOSITORIES_SCHEMA,
                    "description": (
                        "Every step's default list; a step's own replaces it. "
                        + _ADDITIONAL_REPOSITORIES_SCHEMA["description"]
                    ),
                },
                "target": _TARGET_SCHEMA,
                "needs_local": _NEEDS_LOCAL_SCHEMA,
            },
            # `steps` OR `spec`, checked in `_call` -- JSON Schema's oneOf is
            # not something every MCP host renders, and a host that cannot show
            # it shows no required field at all.
            "required": [],
        },
    },
    {
        "name": "swarm_workflow_spec",
        "description": (
            "Read a workflow spec FILE from the checkout this bridge runs in -- or the "
            "`spec_handle` swarm_workflow_launch wrote -- check it as swarm_workflow "
            "would, and return its OUTLINE with its `spec_digest` and a `spec_ref`, "
            "NEVER the whole spec. Submits NOTHING and makes no request.\n"
            "\n"
            "For /sc:swarmcloud given a path or a launch's handle: a workflow script "
            "has no filesystem, so the bridge reads the file, and the script checks "
            "the reply relayed back against this digest before it submits. `path` is "
            "relative to the checkout, or absolute; either way the file must be UNDER "
            "the checkout once symlinks are resolved -- anything outside it is "
            "refused and not read. `spec_handle` (with the `spec_digest` the launch "
            "wrote beside it, which is checked here: a changed file is refused) is "
            "read from the launch's run folder only. The reply's `path` is the file "
            "actually read. A file that is not JSON, or not a workflow spec, is "
            "refused without its content being repeated.\n"
            "\n"
            "The reply carries `spec_ref`, which swarm_workflow takes in place of the "
            "spec, and `outline` -- the title, the label, the `strategy` and `merge`, "
            "and each step's id, `depends_on`, `stage`, `runner_profile`, `when_step`, "
            "`input_from`, `prompt_chars` and `prompt_first_line` -- which is all a "
            "relay needs to copy back. Submit with `spec_ref` (or the handle); never "
            "retype the spec. ONE step's prompt, and only on request: pass `step_id`, "
            "and `offset`/`limit` (characters; at most "
            f"{_PROMPT_WINDOW_MAX}) for a window of it; `next_offset` is null at its end."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The spec file, under this checkout: relative to it, or absolute.",
                },
                "spec_handle": {
                    "type": "string",
                    "description": (
                        "In place of `path`: the `spec_handle.path` swarm_workflow_launch "
                        "returned, exactly as given; read only with `spec_digest`."
                    ),
                },
                "spec_digest": {
                    "type": "string",
                    "description": (
                        "The digest the file must have: required with `spec_handle` "
                        "(its `spec_handle.digest`), checked with `path` when given."
                    ),
                },
                "step_id": {
                    "type": "string",
                    "description": "Return this one step's prompt, windowed, beside the outline.",
                },
                "offset": {
                    "type": "integer",
                    "description": "With `step_id`: the first character of the window (default 0).",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"With `step_id`: the window's length in characters (default "
                        f"{_PROMPT_WINDOW_DEFAULT}, at most {_PROMPT_WINDOW_MAX})."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "swarm_workflow_launch",
        "description": (
            "Write the script a /sc:swarmcloud run is launched from, titled after "
            "its SwarmCloud workflow: `SC · <name> · N steps` (single tasks: `SC · "
            "<label> · task`), at most 150 characters, where <name> is the spec's "
            "`title`, else its `label` cut at a word, else (attach) the title or label "
            "SwarmCloud stored, else the workflow id; the step count is never cut. "
            "No run or row name carries a workspace. The run's description STARTS with "
            "its DETAILS (SC-DESC-DETAILS), in this order: `Workspace: <workspace> "
            "(<tenant>) · Started by: <submitted_by> · Profiles: <runner profiles> · "
            "LLM: <pool account> (<provider>), model <model> · Browser tool: "
            "<browser_tool> · Session: <claude_session>`, each `not reported` when the "
            "API serves none (`Workspace: not reported` included), so clipping the "
            "description never drops the workspace; the reply's `details` is that line. "
            "Each step or task row shows its own details line when expanded: an attach's "
            "`args` carry them, and a spec's reply carries its whole `args`: its "
            "`spec_handle` -- the file this writes the spec to beside the copy, "
            "`{path, digest}`, which the run reads its outline from and submits "
            "from, so no agent retypes the spec (SC-LAUNCH-SPEC) --, `details` and "
            "`rows`, and NEVER the spec itself, so a spec of any size launches "
            "(#2875); launch with those `args` as given. Nothing credential-shaped "
            "is ever shown. A workflow "
            "script's name is fixed when it is launched, so "
            "this writes a copy of the plugin's run.js with that name and returns "
            "its `script_path`: launch it with the Workflow tool, "
            "{scriptPath: <script_path>, args: ...}. Submits NOTHING.\n"
            "\n"
            "Pass ONE of `spec` (the spec object), `spec_path` (its file) or "
            "`attach` (a workflow id, read once for its stored name and step "
            "count -- ANY workflow your tenant may read; `not_this_session` says when "
            "it is not this Claude Code session's; or \"all\": every running workflow "
            "THIS Claude Code session started (SC-SESSION-SCOPE: every submission is "
            "tagged `metadata.claude_session`; other sessions' and untagged work is "
            "counted in `session.left_out`, never attached), closest to finish "
            "first and grouped by workspace, at most the sc plugin's "
            "`max_live_rows` (100 unless set; 1-200) -- one Claude Code run per "
            "SwarmCloud workflow, each launched with the `args` its entry carries; "
            "the rest in `not_followed`).\n"
            "\n"
            "ROWS (PLUGIN-ROW-SPLIT): `rows` picks each attached workflow's shape, "
            "else the sc plugin's `rows` setting does. `summary` (the default): ONE "
            "copy per workflow, whose run follows the whole workflow with one "
            "summary row -- `18 steps: 6 running · 9 queued · 3 done · 0 failed` -- "
            "and the cap counts workflows. `per-step`: a row per unfinished step, "
            "and the cap counts step rows; a workflow with more than Claude Code "
            "runs per run (min(16, CPU cores - 2)) comes back as SPLIT copies, "
            "`SC · <name> · N steps · rows 1-8`, `rows 9-16`..., each a run of its "
            "own: launch EVERY entry of `launches`. A batch of single tasks follows "
            "`rows` too.\n"
            "\n"
            "`arm_watch` (SC-AUTO-WATCH): when this launches a spec, an attach, or at "
            "least one attach-all run, the CronCreate call that arms the sc plugin's "
            "`/sc check` watch for this project -- make it after CronList shows no "
            "`/sc check` job, never a second. Null when nothing was launched or the "
            "project's watch is off (`arm_watch_off`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": "The workflow spec about to be run."},
                "spec_path": {"type": "string", "description": "Its file, under this checkout: relative to it, or absolute."},
                "attach": {"type": "string", "description": "A workflow id, or \"all\"."},
                "rows": {
                    "type": "string",
                    "enum": ["summary", "per-step", "pane"],
                    "description": (
                        "With `attach` only: `summary`, one row per workflow (and per batch of "
                        "single tasks), `per-step`, a row per step split over runs, or `pane`, "
                        "one quiet sc:finish row per workflow that notices its finish (the "
                        "pane layout's). Unset, this project's layout (`/sc layout`) and then "
                        "the sc plugin's `rows` setting decide."
                    ),
                },
                "repository": {
                    "type": "string",
                    "description": (
                        "With attach \"all\" only (SC-WATCH): keep the workflows and single "
                        "tasks that clone this repository -- a URL, `owner/repo`, \"origin\" "
                        "(this checkout's remote) or \"all\". `repository_unread` lists those "
                        "whose repository could not be read, with the command for each."
                    ),
                },
                "project": {
                    "type": "boolean",
                    "description": (
                        "With attach \"all\" only, AT SESSION START (`/sc attach --all --project`, "
                        "what the sc plugin's SessionStart hook asks for in the rows layout; "
                        "SC-BROWSER-FOLLOWUP): this session's work AND every workflow and single task "
                        "still running that clones this checkout's origin repository (`repository` "
                        "names another), whichever Claude Code session started it; `project` in the "
                        "reply says how many came from other sessions. `/sc check`, the watch and the "
                        "plain attach-all stay this session's."
                    ),
                },
                "new_only": {
                    "type": "boolean",
                    "description": (
                        "With attach \"all\" only: leave out every workflow and task this "
                        "session was already handed a row for (listed in `already_attached`), "
                        "so nothing is attached twice."
                    ),
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "With attach \"all\" only: workflow or task ids with a live row you can see; left out.",
                },
            },
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
            "a parent and is holding no capacity, which costs nothing.\n"
            "\n"
            "Each step whose task was read carries `questions`: how many "
            "questions the worker validated in that step's questions.json, 0 "
            "when it asked none. Read a step with questions with swarm_result, "
            "whose `owner_questions` holds them; they are for the owner to answer.\n"
            "\n"
            "A step parked AWAITING_INPUT waits for a member of its tenant to "
            "answer and holds nothing while it does; its row carries `awaiting: "
            "{tenant, opened_at, expires_at, form, artifact, max_files, remind}`. "
            "Once answered it carries `answered: {answered_by, answered_at, via, "
            "files}`. A person answers with swarm_workflow_input, or in the console."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    {
        "name": "swarm_inputs",
        "description": (
            "The workflow steps of YOUR tenant waiting for a person's answer (an "
            "`await-input` step, parked AWAITING_INPUT): each with its workflow_id, "
            "step_id, task_id, `opened_at` and `expires_at`, newest first, and "
            "`answer_with`, the swarm_workflow_input call that answers it. Any member "
            "of the tenant may answer any of them. Read-only. A waiting step holds no "
            "lease and no capacity (invariant 1): it is waiting for a person, not stuck."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "At most this many, 1-200 (default 200)."},
            },
        },
    },
    {
        "name": "swarm_workflow_input",
        "description": (
            "Answer a workflow step that waits for a person (an `await-input` step). "
            "THE PERSON TYPES THE ANSWER, not the caller: the bridge shows the step's "
            "prompt and what it stages in this host's own prompt (MCP elicitation), "
            "and sends only what the person typed there -- a verdict and findings, "
            "answers to the upstream agent's questions, or a text. This tool takes no "
            "verdict, payload or text argument, and refuses a call that passes one. "
            "`files` are LOCAL PATHS you may propose to attach; the person is shown "
            "each one's name, size and sha256 and sends them only by ticking the box, "
            "so a file they did not see is never sent. At most 10 files and 16 MiB in "
            "all, refused before anything is sent. A host that cannot ask its human "
            "sends nothing, and the reply names the step's console link instead. The "
            "first answer is final; the API records who answered, when, and `via: "
            "mcp`. List the waiting steps with swarm_inputs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "step_id": {"type": "string", "description": "The await-input step to answer."},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths to propose attaching. The person decides.",
                },
            },
            "required": ["workflow_id", "step_id"],
        },
    },
    {
        "name": "swarm_workflows",
        "description": (
            "The workflows of YOUR tenant that are still running in SwarmCloud: "
            "every one whose derived state is not SUCCEEDED, FAILED, CANCELLED or "
            "DEAD_LETTERED, newest first and grouped by workspace, each with its "
            "workflow_id, the `workspace` it runs in, its spec's "
            "label, its state, its current (unfinished) steps and their states, "
            "its age and its console link. Read-only; it submits and attaches "
            "nothing. The API filters on the workflows' stored state in its "
            "query, so SUCCEEDED and CANCELLED history costs nothing and the "
            "running set is usually one page; newer FAILED or DEAD_LETTERED "
            "workflows are still read (a retry can revive them) and can push it "
            "to a few. `count` is how many are listed; `complete: false` with "
            "`incomplete_because` means the list stopped short and older ones "
            "may be missing. A workflow with `steps_unread_because` was listed "
            "but its steps were not read. To show them all as live rows, run "
            "`/sc attach --all` (the /sc:swarmcloud workflow with "
            "{attach: \"all\"}), which follows at most the sc plugin's "
            "`max_live_rows` (30 unless set).\n"
            "\n"
            "`single_tasks` lists YOUR running single tasks beside them: not "
            "finished, in no workflow, submitted by you (`swarm_dispatch`), each "
            "with its task_id, workspace, label, state, age and console link. "
            "`/sc attach --all` gives each one row too, at most `max_live_rows`. "
            "`single_tasks_error` means they could not be read."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_check",
        "description": (
            "Which SwarmCloud work of THIS PROJECT is stuck or stale -- read-only, it "
            "cancels, retries and attaches nothing. Only work THIS Claude Code session "
            "started is checked (SC-SESSION-SCOPE: tagged `metadata.claude_session`); "
            "other sessions' and untagged work is counted in `session.left_out` and "
            "never flagged. Scoped to one repository (default: "
            "this checkout's `origin` remote; a workflow belongs to the repository its "
            "steps clone) in your tenant (`tenants: \"all\"` for every tenant you may "
            "choose). `flagged` lists each item with `kind` STUCK or STALE, its `rule`, a "
            "`reason`, the `evidence` the API served, its console link and `next`, the "
            "command for a person to run:\n"
            "  no_heartbeat (STALE): RUNNING/STARTING with no heartbeat and no event for "
            "more than `stale_minutes` (default 20);\n"
            "  waiting_for_capacity (STUCK): READY/QUEUED longer than that, with the pool "
            "that binds it (`evidence.binding`);\n"
            "  parked (STUCK): HUMAN_REQUIRED, CI_PENDING over 90 min, or "
            "DEPENDENCY_INCOMPLETE whose dependency failed;\n"
            "  merge_refused (STUCK): a merge step FAILED with head_moved, merge_conflict "
            "or checks_failed while its PR is still open (read from GitHub's public API, "
            "unauthenticated; a PR since MERGED or CLOSED is not flagged, and a failed read "
            "keeps the flag saying \"PR state not read\"), with its PR link;\n"
            "  state_disagrees (STALE): a workflow whose stored and derived state disagree "
            "on whether it is finished (the stale RUNNING cache).\n"
            "`owner_questions` (information, NEVER a flag) lists this session's tasks in "
            "this project, finished in the last 7 days, whose agent asked the owner questions "
            "with no answer yet: workspace, workflow and step_id (or the task's title), "
            "task_id, console, `open` and the open `questions` whole, each {index, question, "
            "options, recommended, context}. Put EACH to the person with AskUserQuestion, "
            "the recommended option marked, then record the pick with swarm_task_answer "
            "{task_id, question_index: index, choice}; never answer one yourself.\n"
            "`running` lists the project's running workflows and your single tasks; "
            "`unchecked` names what was not checked and why; `repository: null` means "
            "the check was not scoped (`repository_note` says why). Then attach rows "
            "with swarm_workflow_launch {attach: \"all\", repository, new_only: true}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "description": "A URL or `owner/repo`; \"origin\" (the default) or \"all\".",
                },
                "stale_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The window for no_heartbeat and waiting_for_capacity. Default 20.",
                },
                "tenants": {
                    "type": "string",
                    "enum": ["current", "all"],
                    "description": "\"current\" (the default): the tenant this session acts in.",
                },
            },
        },
    },
    {
        "name": "swarm_check_schedule",
        "description": (
            "The sc plugin's watch for THIS PROJECT: a `/sc check` every interval. It "
            "schedules nothing and reads nothing from SwarmCloud: `{interval}` (default "
            "30m, at least 5m, e.g. `30m`, `1h`) saves the preference for this project "
            "and answers `arm_with`, the CronCreate call that schedules it in this "
            "session (`replace_first` says how to drop an older one), and `without_cron`, "
            "the `/loop` command that does the same; `{off: true}` saves \"off\" for the "
            "project -- no reply's `arm_watch` arms it until `{interval}` again -- and says "
            "how to cancel the job; neither reports it. An interval a cron expression "
            "cannot say is rounded (`rounded`), one below the floor raised (`clamped`). "
            "Scheduled prompts end with the session; the plugin's SessionStart hook asks "
            "the next session to run `/sc watch` again while the preference is saved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "interval": {"type": "string", "description": "`30m`, `45`, `1h`, `1h30m`. Default 30m."},
                "off": {"type": "boolean", "description": "true: turn the watch off for this project."},
            },
        },
    },
    {
        "name": "swarm_browse",
        "description": (
            "The sc plugin's SwarmCloud pane's reads (SC-BROWSER-BUILD), one per click, "
            "read-only. `level`: `workflows` -- THIS Claude Code session's workflows, "
            "finished ones too, each with its derived state, its steps (id, depends_on, "
            "state), duration_s, tokens (finished steps' result usage, every kind) and "
            "cost_usd, and this session's running single tasks; `stages` with "
            "`workflow_id` -- that workflow, read fresh, in the same shape; `agents` with "
            "`task_ids` -- each task's step_id, state and a one-line note; `agent` with "
            "`task_id` -- its state, attempts, duration, cost, tokens, last error, pull "
            "request, artifact names and console link. A figure nobody recorded is null "
            "with a `*_because`, never 0. The pane calls it directly, so browsing costs no "
            "model tokens; a session reads SwarmCloud with the other tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["workflows", "stages", "agents", "agent"]},
                "project": {
                    "type": "boolean",
                    "description": (
                        "With `workflows`: also this project's running work -- every workflow and "
                        "single task still running that clones this checkout's origin repository, "
                        "whichever session started it, marked `this_session: false`. The pane asks "
                        "for it after session start, which tracks the project's running work."
                    ),
                },
                "workflow_id": {"type": "string", "description": "With `stages`."},
                "task_ids": {"type": "array", "items": {"type": "string"}, "description": "With `agents`."},
                "task_id": {"type": "string", "description": "With `agent`."},
            },
            "required": ["level"],
        },
    },
    {
        "name": "swarm_layout",
        "description": (
            "How THIS PROJECT follows SwarmCloud work in Claude Code (`/sc layout`): "
            "`rows` -- today's /workflows rows, as the plugin's `rows` setting says --, "
            "`pane` -- the SwarmCloud pane beside the chat plus ONE quiet row per "
            "workflow that notices its finish -- or `band` -- a strip above the prompt "
            "and a summary line below it, and no rows. No argument reads it: `{layout, source, "
            "project}`. `{layout: \"rows\"|\"pane\"|\"band\"}` saves it for this project (it "
            "outranks the plugin's `layout` option) and takes effect at the next launch "
            "or `/sc attach --all`, without restarting the session; rows already running "
            "keep going. Reads and writes nothing in SwarmCloud."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"layout": {"type": "string", "enum": ["rows", "pane", "band"]}},
        },
    },
    {
        "name": "swarm_build_board",
        "description": (
            "Refresh THIS PROJECT's build board (#1773): one self-contained HTML page at "
            "`<project>/.claude/build-board/index.html`, git-ignored by a `.gitignore` of "
            "`*` beside it. Read-only towards SwarmCloud -- GETs only, and nothing from "
            "GitHub. It shows every workflow and single task YOU submitted against this "
            "checkout's `origin` repository, from any session: in flight always, finished "
            "for 30 days; grouped by the issue each names; and \"Waiting on someone\": owner "
            "questions, steps awaiting input or parked HUMAN_REQUIRED (with their "
            "`/sc answer WF STEP`), refused merges and CI parks over 90 min. Answers "
            "`{path, written_at, counts, waiting}` (`waiting` the number of items), or "
            "`{error, path}` when it could not -- the previous page then stays, and the "
            "next page written says why in its banner. The sc CLI's build-board command "
            "refreshes and opens it."
        ),
        "inputSchema": {"type": "object", "properties": {}},
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
    # -- issue runs (#454) -------------------------------------------------
    #
    # A run plans a GitHub issue with one planner task; the plan waits, holding
    # no capacity, until a person approves the digest they were SHOWN. The
    # same calls `sc run` / `sc plan` make, through `SwarmClient` and `runs`.
    {
        "name": "swarm_run_issue",
        "description": (
            "Plan a GitHub issue and, once the plan is approved, run it: one "
            "planner task reads the issue and the repository's open issues and "
            "pull requests and writes a plan; the plan compiles into one workflow "
            "that opens a pull request, whose CI the platform reads and fixes for "
            "up to `fix_rounds` rounds. SUBMITS WORK (the planner) -- say so "
            "before calling it. With plan_approval `required` (the default) the "
            "run stops PLANNED, holding no capacity, until someone approves: read "
            "it with swarm_run, show the developer the plan, then "
            "swarm_plan_approve with the plan_digest that read returned. "
            "`auto_merge` is visible but disabled: the API refuses it until #295, "
            "and the refusal comes back in words with nothing created. The "
            "tenant's own forge credential reads the issue; this tool takes none."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "owner/repo#N, or the issue's URL."},
                "plan_approval": {"type": "string", "enum": ["required", "auto"], "default": "required"},
                "auto_merge": {"type": "boolean", "default": False},
                "fix_rounds": {"type": "integer", "minimum": 1, "maximum": 5},
                "model_class": {
                    **_MODEL_CLASS_SCHEMA,
                    "description": (
                        "The PLANNER's model class, by name; omit it for the default. "
                        "Each plan step's class is the planner's to write."
                    ),
                },
            },
            "required": ["issue"],
        },
    },
    {
        "name": "swarm_runs",
        "description": (
            "Your tenant's issue runs, newest first, each as served (state, issue, "
            "plan digest, workflow, pull request, CI round). Each live run is "
            "advanced by the read. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1},
                "page_token": {"type": "string"},
            },
        },
    },
    {
        "name": "swarm_run",
        "description": (
            "One issue run: its state (PLANNING, PLANNED, APPROVED, RUNNING, "
            "CHECKING, FIXING, DONE, FAILED, REJECTED, CANCELLED), its plan and "
            "plan_digest, its pull request, CI fix rounds, green sha or the "
            "redacted failing-CI excerpt. The read advances the run. While a "
            "workflow runs (RUNNING, or FIXING for a CI round) the answer carries "
            "its steps and `attach_with`: `/sc attach <workflow_id>` shows them "
            "as live rows. Read-only. Another tenant's run answers as missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "swarm_plan_approve",
        "description": (
            "Approve a PLANNED run's plan, which submits its workflow. "
            "`plan_digest` MUST be the digest of the plan the developer was SHOWN "
            "(from swarm_run) and agreed to -- never read the run and approve in "
            "one go. A plan that changed since is refused (`plan_changed`) and "
            "nothing is done: read it again and show it again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "plan_digest": {"type": "string", "description": "The digest of the plan shown."},
            },
            "required": ["run_id", "plan_digest"],
        },
    },
    {
        "name": "swarm_plan_edit",
        "description": (
            "Replace a PLANNED run's plan with `plan` (the whole plan object: "
            "summary, steps, and optionally mode, requirements, overlaps, "
            "estimate). `plan_digest` is the digest of the plan that was edited; "
            "a plan that changed since is refused (`plan_changed`). An invalid "
            "plan is refused (`invalid_plan`) and the run keeps its plan. The run "
            "stays PLANNED, with a new digest to approve."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "plan_digest": {"type": "string"},
                "plan": {"type": "object"},
            },
            "required": ["run_id", "plan_digest", "plan"],
        },
    },
    {
        "name": "swarm_plan_reject",
        "description": (
            "Reject a PLANNED run's plan; the run ends REJECTED and nothing is "
            "submitted. `reason` is recorded on the run and the issue. With "
            "`plan_digest`, refused (`plan_changed`) if the plan is no longer "
            "the one shown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "reason": {"type": "string"},
                "plan_digest": {"type": "string"},
            },
            "required": ["run_id", "reason"],
        },
    },
    # -- schedules and approvals (docs/schedules.md §7.2, lane S8) -----------
    #
    # The calls `sc schedules` / `sc approvals` make, through `schedules`. A
    # schedule names a TYPE, never a backend (invariant 10); an approval sends
    # the digest of what the developer was SHOWN, as `swarm_plan_approve` does.
    {
        "name": "swarm_schedule_types",
        "description": (
            "The schedule-type catalogue (docs/schedules.md §3), as your tenant sees "
            "it: each type's name, description, params_schema, default and floor "
            "gate, minimum interval, budget caps, the scopes you may create it in, "
            "and `available` with `disabled_reason`. Read it before "
            "swarm_schedule_create: it is the authority on which types this "
            "deployment can run. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "swarm_schedules",
        "description": (
            "Your tenant's schedules, each with its state, cron in words, "
            "next_run_at, last_firing, spend today (with `coverage: partial` when "
            "an attempt's cost was never reported -- the figure is then a floor) "
            "and pending approval count. With `id` (a schedule id or its name), "
            "that one schedule with its last 50 firings, each with its work, cost "
            "and dry-run output. Read-only. Another tenant's schedule answers as "
            "missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "A schedule id (sch_...) or name."}},
        },
    },
    {
        "name": "swarm_schedule_create",
        "description": (
            "Create a recurring schedule on your tenant's repositories. `type` is "
            "one of the catalogue's seventeen: `issue-sweep`, `issue-plan-only`, "
            "`repo-index-refresh`, `observer`, `epic-triage`, `pr-shepherd`, "
            "`ci-flake-hunter`, `dependency-cve-refresh`, `image-tool-rescan`, `release-health`, "
            "`docs-drift`, `cost-report`, `custom-prompt`, `visual-qa` (a workspace "
            "admin's, scope `all`, naming one of the workspace's QA targets), and the admin-only "
            "platform types `model-prices-refresh` and `model-prices-check`, and the owner-only "
            "platform type `failure-review` -- which of them this "
            "deployment can run now is swarm_schedule_types' `available`, and a "
            "type that is not is refused with the available ones named. The type "
            "chooses the work: NO IMAGE OR COMMAND IS ACCEPTED, nor a runner "
            "profile, resources or a credential, at the top level or inside "
            "`params`, and offering one sends nothing. `params` are the type's own "
            "(its params_schema). `repos` are owner/repo names registered in your "
            "tenant (scope `repos`, the default). The gate, budget and policy "
            "default to the type's; `dry_run: true` makes every firing list what "
            "it would do and create nothing. `gate.merge: auto` is refused here: "
            "it is set only through the audited merge switch. WRITES: the "
            "schedule fires on its cron as you -- say so before calling it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique in your tenant, at most 80 characters."},
                "type": {"type": "string", "description": "A catalogue type's name."},
                "cron": {"type": "string", "description": "Five fields, e.g. `0 9 * * 1-5`."},
                "timezone": {"type": "string", "default": "UTC", "description": "An IANA zone."},
                "scope": {"type": "string", "enum": ["repos", "all", "platform"], "default": "repos"},
                "repos": {"type": "array", "items": {"type": "string"}, "description": "owner/repo names."},
                "params": {"type": "object"},
                "gate": {"type": "object"},
                "budget": {"type": "object"},
                "policy": {"type": "object"},
                "dry_run": {"type": "boolean", "default": False},
                "state": {"type": "string", "enum": ["enabled", "paused"], "default": "enabled"},
            },
            "required": ["name", "type", "cron"],
        },
    },
    {
        "name": "swarm_schedule_update",
        "description": (
            "Edit a schedule, carrying the `revision` you read (from "
            "swarm_schedules): only the fields given change. A schedule edited "
            "since is refused (`schedule_changed`) and nothing changes. A gate "
            "below the type's default needs an admin; `gate.merge: auto` is "
            "refused (`use_merge_switch`). `type` is not editable. No image or "
            "command is accepted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "A schedule id (sch_...) or name."},
                "revision": {"type": "integer", "minimum": 1},
                "name": {"type": "string"},
                "cron": {"type": "string"},
                "timezone": {"type": "string"},
                "scope": {"type": "string", "enum": ["repos", "all", "platform"]},
                "repos": {"type": "array", "items": {"type": "string"}},
                "params": {"type": "object"},
                "gate": {"type": "object"},
                "budget": {"type": "object"},
                "policy": {"type": "object"},
            },
            "required": ["id", "revision"],
        },
    },
    {
        "name": "swarm_schedule_pause",
        "description": (
            "Pause a schedule: it makes no firing until resumed. Work it already "
            "made runs on. `reason` is recorded in its audit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "swarm_schedule_resume",
        "description": "Resume a paused schedule from its next slot; missed slots are not replayed.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "swarm_schedule_run_now",
        "description": (
            "Fire a schedule now (a `run_now` firing), under its gate's `run` "
            "point, its overlap rule and its budget, as the schedule's owner. "
            "With `dry_run: true` the firing lists what it would do and creates "
            "nothing. Without it, it may SUBMIT WORK -- say so before calling it. "
            "Answers the firing; a gate that holds it answers it "
            "`awaiting_approval`, and it is then in swarm_approvals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "dry_run": {"type": "boolean", "default": False}},
            "required": ["id"],
        },
    },
    {
        "name": "swarm_approvals",
        "description": (
            "Your tenant's approval inbox: firings waiting to run, issue-run plans "
            "(id `run:<run_id>`), merges, proposals, holds and specs, each with "
            "its `digest` -- the evidence an approval must send back. `kind` "
            "filters. Read-only; nothing waiting holds capacity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["run", "plan", "merge", "proposal", "spec", "hold"]},
            },
        },
    },
    {
        "name": "swarm_schedule_approve",
        "description": (
            "Approve an inbox item. `digest` MUST be the digest of the item the "
            "developer was SHOWN (from swarm_approvals) and agreed to -- a string, "
            "or a merge's {head_sha, verdict} -- never read and approve in one go. "
            "An item that changed since is refused (`approval_changed`, "
            "`merge_changed`, `plan_changed`) and nothing is done. For an "
            "issue-run plan (`run:<run_id>`) this is the same call as "
            "swarm_plan_approve. `confirm` is a one-person tenant's typed "
            "confirmation of its own hold."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "digest": {"type": ["string", "object"], "description": "The digest of the item shown."},
                "confirm": {"type": "string"},
            },
            "required": ["approval_id", "digest"],
        },
    },
    {
        "name": "swarm_schedule_reject",
        "description": (
            "Reject an inbox item with a reason: a firing creates nothing, a plan "
            "ends REJECTED, a merge does not happen. Anyone in the tenant may "
            "reject."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"approval_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["approval_id", "reason"],
        },
    },
    # -- onboarding (#780, OB9) ---------------------------------------------
    #
    # The calls `sc setup` and `sc access` make, for /sc:setup. None takes or
    # returns a token, a code or a state: swarm_setup_connect returns the
    # authorize URL, whose `state` GitHub requires, and nothing else carries it.
    {
        "name": "swarm_setup_status",
        "description": (
            "The caller's SwarmCloud onboarding checklist, the one the console "
            "draws: the steps in order (signed_in, workspace, claude_account, "
            "github_connected, app_installed, orgs_enabled, repos_chosen, "
            "access_verified, ready, as the API serves them), each todo, "
            "in_progress, done, failed or stale, with every failure's code and "
            "its recovery copy word for word, and `next_step` (null when ready). "
            "`checklist` is the text to show. `connected_as_you` is the GitHub "
            "login SwarmCloud acts as YOU through, or null -- a step that is done "
            "through the tenant's token is not that. With "
            "`wait_for_github_seconds`, holds until GitHub is connected as you or "
            "that many seconds pass (at most 600): call it that way right after "
            "swarm_setup_connect, while the person approves in the browser."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_for_github_seconds": {
                    "type": "integer", "minimum": 0, "maximum": 600, "default": 0,
                    "description": "Hold until GitHub is connected as you, up to this long.",
                },
            },
        },
    },
    {
        "name": "swarm_setup_workspace",
        "description": (
            "The caller's OWN personal workspace (docs/workspaces.md): the "
            "isolated space their tasks and workflows run in, and the checklist's "
            "`workspace` and `claude_account` steps. `action` `status` (the "
            "default) reads it; `request` asks for it -- an admin approves it in "
            "the console and a job sets it up a few minutes later; asking again "
            "changes nothing -- and `loan` asks an admin to lend a Claude account "
            "(refused until the workspace was requested). Returns `workspace` "
            "(state, workspace_id, the job's steps, a failure's copy word for "
            "word) and `text`. It never waits for the approval or the job: read "
            "it again later, or call swarm_setup_status. Takes no other argument."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "request", "loan"],
                           "default": "status",
                           "description": "status reads; request and loan post once, then read."},
            },
        },
    },
    {
        "name": "swarm_setup_connect",
        "description": (
            "Start connecting GitHub as the caller, through SwarmCloud's GitHub "
            "App: returns `authorize_url` and `expires_in_seconds` (the link "
            "works once, for 10 minutes), and opens the browser on this machine "
            "when it can (`opened`). Show the person the URL in case it did not "
            "open; repeat it nowhere else. The console's callback page finishes "
            "the connection in their browser -- nothing comes back here -- so "
            "follow with swarm_setup_status and `wait_for_github_seconds`. If "
            "GitHub is already connected as them, starts nothing and says as whom."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "open_browser": {"type": "boolean", "default": True,
                                 "description": "Open the URL in this machine's browser."},
            },
        },
    },
    {
        "name": "swarm_setup_orgs",
        "description": (
            "The GitHub owners the caller reaches -- their account and each "
            "organisation -- with `install_state` (installed / not_installed), "
            "`sso` and `enabled`, plus the App's `install_url` for an owner it is "
            "not installed on (an org the person does not own sends its owners a "
            "request). With `enable`, enables that installed owner first; an "
            "owner without the App is refused with REPO_NOT_INSTALLED and its "
            "recovery copy. `approval_pending` lists each owner whose owners the "
            "person asked to install the App and has not yet approved it, with "
            "ORG_APPROVAL_PENDING and its recovery copy word for word; it re-checks "
            "on its own every 15 minutes. With `request_install`, records first "
            "that the person asked that org's owners -- only when they say they "
            "did, or will. An org that will not install the App takes a "
            "fine-grained token instead, which the person stores THEMSELVES in a "
            "terminal, with sc's setup token verb for that org (/sc:setup names it): no tool takes a "
            "token, and none may be asked for in chat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enable": {"type": "string", "description": "An installed owner's login to enable."},
                "request_install": {
                    "type": "string",
                    "description": "An org whose owners the person asked to install the App.",
                },
            },
        },
    },
    {
        "name": "swarm_setup_repos",
        "description": (
            "One page (up to 100) of an ENABLED owner's repositories that the "
            "App's installation covers, each with `visibility`, `can_push`, "
            "`archived`, and whether the caller already `granted` it and in which "
            "`mode`. `q` filters by name server-side; `next_page` is the page to "
            "ask for next, null on the last; `capped` says the listing stops "
            "there -- search, or grant a typed owner/repo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "q": {"type": "string", "description": "Only names containing this."},
            },
            "required": ["owner"],
        },
    },
    {
        "name": "swarm_setup_grant",
        "description": (
            "Let SwarmCloud use one repository AS THE CALLER: `read` (clone) or "
            "`write` (clone, push, pull request). The API reads it once as them "
            "and refuses write on an archived repository or one they cannot push "
            "to, with the code and recovery copy; the first grant registers it "
            "for the tenant. Only what the person chose: never grant a repository "
            "or a mode they did not name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "owner/repo"},
                "mode": {"type": "string", "enum": ["read", "write"]},
            },
            "required": ["repository", "mode"],
        },
    },
    {
        "name": "swarm_setup_revoke",
        "description": (
            "Revoke the caller's grant on one repository: their tasks for it are "
            "refused from then on, and a running worker is refused at its next "
            "push. The tenant's registration goes with the last grant on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repository": {"type": "string", "description": "owner/repo"}},
            "required": ["repository"],
        },
    },
    {
        "name": "swarm_setup_verify",
        "description": (
            "Check what SwarmCloud can do as the caller in each granted "
            "repository (or only those named): clone, and for a write grant push "
            "and pull request -- reads only by default, nothing is pushed. Per "
            "repository: `passed`, each check's state (ok / missing / unknown / "
            "not_required) and every failure's code and recovery copy. `unknown` "
            "with FORGE_UNREACHABLE means GitHub did not answer, not that it "
            "failed. `push_test: true` is the one write (D6): in each NAMED write "
            "grant, SwarmCloud creates and deletes the branch "
            "swarmcloud/onboarding-check-<nonce> as the person. Send it only after "
            "asking them and hearing yes for that repository; it needs "
            "`repositories`, and a read grant is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repositories": {"type": "array", "items": {"type": "string"},
                                 "description": "owner/repo each; default every grant."},
                "push_test": {
                    "type": "boolean", "default": False,
                    "description": "Also create and delete a test branch in each named "
                                   "write grant. Only when the person said yes.",
                },
            },
        },
    },
    {
        "name": "swarm_access",
        "description": (
            "The caller's Access page: their GitHub connection (state, login -- "
            "never a token), the owners they enabled, and every repository grant "
            "with its mode and last checks. `text` is what to show."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # -- provider keys (KEYS-PLUGIN, docs/provider-keys.md) -----------------
    #
    # THE KEY NEVER PASSES THROUGH CLAUDE (owner, 2026-10-14): not as a tool
    # argument, not in a reply. So there is no tool that SETS a key -- only
    # status and a one-time link to the console form, where the person pastes
    # it. Both schemas are enums and `additionalProperties: false`: no field a
    # key fits in, and an argument not in the schema is refused unsent
    # (`_refuse_unknown_arguments`). tests/unit/mcp/test_provider_keys_bridge.py.
    {
        "name": "swarm_mcp_servers",
        "description": (
            "The MCP servers your workspace registered, by name: the names a "
            "claude-code or codex step may put in `inputs.mcp_servers`, each with its "
            "URL, auth mode (`dispatch` needs a per-run `mcp_credentials` entry from "
            "you; `none` and `workspace_secret` take none), the profiles allowed to "
            "use it, the tools an agent may call and whether it is enabled; and the "
            "addresses a server's operator allowlists. Read-only: no tool registers a "
            "server or stores its credential -- an admin does that in the console's "
            "Settings > Workspace > MCP servers. `text` is what to show."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "swarm_keys",
        "description": (
            "Whether a TypeSafe key is set for YOU and for your selected tenant, "
            "and which one a task you submit there would use (your own key wins "
            "over the tenant's). Status only -- set, version, who set it and when; "
            "never a key, its length or any part of it. `text` is what to show. "
            "Read-only. To set a key, call swarm_key_set_link; never ask the person "
            "for the key itself."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "provider": {"type": "string", "enum": list(keys.PROVIDERS),
                             "default": "typesafe"},
            },
        },
    },
    {
        "name": "swarm_key_set_link",
        "description": (
            "A one-time console link where the person pastes their TypeSafe key "
            "themselves: answers ONLY `url` and `expires_at` (ten minutes, one use, "
            "valid only in their own console session). There is deliberately no "
            "argument for the key: never ask for it, never accept it in chat. Give "
            "the person the url and tell them to open it. scope `user` (default) is "
            "their own key, which wins over the tenant's; `tenant` is the selected "
            "tenant's key and needs an admin. In their own terminal, the sc CLI's "
            "`key set` does the same at a hidden prompt; tell them, never run it."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "provider": {"type": "string", "enum": list(keys.PROVIDERS)},
                "scope": {"type": "string", "enum": list(keys.SCOPES), "default": "user"},
            },
            "required": ["provider"],
        },
    },
    # -- QA targets (VQ-6, docs/design/visual-qa-any-app.md §2.1, §6) --------
    #
    # A target's test credential never passes through Claude: no schema below
    # has a field a username or a password fits in, and `qatargets` copies
    # every answer field by field.
    {
        "name": "swarm_qa_targets",
        "description": (
            "The QA targets your selected workspace registered: the web apps its "
            "visual-QA runs test, each with the repository its findings are filed in "
            "and its `environments` (one app's legs, e.g. staging and production), each "
            "entry with its name, its mark (`production` or `non_production`), its base "
            "URL, its host's approval (`pending` until a platform admin approves it), "
            "whether it signs in with its own login, `credential_set` (yes or no: never "
            "a username, a password or a bypass value), and `can_run` with "
            "`cannot_run_because`. Optional `repository` (owner/repo or a repo_ id) "
            "narrows it to one. Read-only; `text` is what to show."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"repository": {"type": "string", "maxLength": 200}},
        },
    },
    {
        "name": "swarm_qa_run",
        "description": (
            "Start a visual-QA run of one QA target, by its id (qat_...) or name, as "
            "your submission in the selected workspace (a member may run; only a "
            "workspace admin registers or changes a target, in the console). The "
            "platform renders the workflow from the registration: you choose only "
            "`file` (`issues`, the default, files findings in the target's "
            "repository; `none` reports only) and optionally a subset of its "
            "`screens` ids, and optionally `browser_tool` (`playwright` or `jev`), "
            "the browser tool this one run's flows use. Left out, the target's own "
            "`flows_browser_tool` decides, and that defaults to `playwright`. "
            "`environments` names the legs to run, by the entry names swarm_qa_targets "
            "lists (default: every entry); never a URL, a sign-in or a credential. Each "
            "leg is its own workflow; if any named leg cannot run (a pending host is "
            "`qa_target_host_pending`) nothing starts. Answers the run's `group_id` and "
            "`legs`, each with its `environment` and `workflow_id`; follow each with "
            "swarm_workflow_status. `flows_per_step` (1-30) is how many of the target's "
            "flows one live-browser step runs, in batches, for this run only; left out, "
            "the workspace's `qa_flows_per_step` decides, and that defaults to 15."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string", "maxLength": 100},
                "file": {"type": "string", "enum": list(qatargets.FILES), "default": "issues"},
                "screens": {"type": "array", "maxItems": qatargets.MAX_SCREENS,
                            "items": {"type": "string", "maxLength": 100}},
                "browser_tool": {"type": "string", "enum": list(qatargets.BROWSER_TOOLS)},
                "environments": {"type": "array", "minItems": 1,
                                 "maxItems": qatargets.MAX_ENVIRONMENTS, "uniqueItems": True,
                                 "items": {"type": "string",
                                           "pattern": qatargets.ENVIRONMENT_NAME.pattern}},
                "flows_per_step": {"type": "integer", "minimum": qatargets.MIN_FLOWS_PER_STEP,
                                   "maximum": qatargets.MAX_FLOWS_PER_STEP},
            },
            "required": ["target"],
        },
    },
    {
        "name": "swarm_suite_gate",
        "description": (
            "Run a registered repository's whole Playwright suite as a sharded gate "
            "(docs/suite-gate.md). The bridge renders the workflow, you name only data: "
            "`shards` steps on `claude-code` each run, from `working_dir`, `npm ci`, "
            "`npx playwright install chromium` and `npx playwright test --shard k/N "
            "--retries=1 --reporter=blob,line` (with `--project` when named, after loading "
            "`env_file` when named), and put their blob report in their artifacts; one "
            "`report` step depending on all of them merges the blobs "
            "(`npx playwright merge-reports`) into playwright-report.zip and writes "
            "summary.md and summary.json: passed, failed, flaky, and each failure's spec and "
            "first error line. Every step runs tests only: none fixes code or commits "
            "(strategy collect, no merge). Never an image, a command or resources from the "
            "caller (invariant 10). Returns as soon as the workflow is accepted; follow it "
            "with swarm_workflow_status, then read the report step with swarm_result and "
            "swarm_artifact. A shard that produced no blob fails, and the gate with it."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repository": {"type": "string", "maxLength": 220,
                               "description": "owner/repo, registered by your workspace."},
                "ref": {"type": "string", "maxLength": 200,
                        "description": "The branch, tag or commit SHA to test."},
                "suite": {"type": "string", "enum": list(suitegate.SUITES)},
                "working_dir": {"type": "string", "maxLength": 200,
                                "description": "The repo-relative directory holding package.json "
                                               "and the Playwright config, e.g. `web`; default the root."},
                "shards": {"type": "integer", "minimum": suitegate.MIN_SHARDS,
                           "maximum": suitegate.MAX_SHARDS, "default": suitegate.DEFAULT_SHARDS},
                "project": {"type": "string", "maxLength": 100,
                            "description": "A Playwright project name, passed as --project."},
                "env_file": {"type": "string", "maxLength": 200,
                             "description": "A repo-relative file of NON-SECRET env the suite "
                                            "needs (a mock server's URL, say), loaded before the tests."},
                "target": _TARGET_SCHEMA,
            },
            "required": ["repository", "ref", "suite"],
        },
    },
    {
        "name": "swarm_qa_target_credential_link",
        "description": (
            "A one-time console link where a workspace admin types a QA target's "
            "test-account username and password themselves: answers ONLY `url` and "
            "`expires_at` (fifteen minutes, one use, bound to the admin's own "
            "console session and this target). There is deliberately no argument for "
            "the credential: never ask for it, never accept it in chat. Give the "
            "person the url and tell them to open it. Each environment has its own "
            "test account, so `environment` names the entry the link is for (required "
            "when the target has several). `field` picks the form: `credential` (the "
            "default, the test account) or `challenge_bypass` (the value the app's "
            "challenge bypass sends; it too is typed only into that form)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {"type": "string", "maxLength": 100},
                "environment": {"type": "string", "pattern": qatargets.ENVIRONMENT_NAME.pattern},
                "field": {"type": "string", "enum": list(qatargets.LINK_FIELDS)},
            },
            "required": ["target"],
        },
    },
    # -- Config › Secrets (SSC-SC, docs/design/secrets-scopes.md §6.4, §11 Q8) --
    #
    # Links and reads only: NO TOOL TAKES A VALUE. A value arrives through the
    # console form a one-time link opens, or a terminal's --stdin. Every
    # string below is an enum or a pattern a value does not fit, checked again
    # in `secrets` before anything is sent and refused without being repeated;
    # every answer is copied field by field. tests/unit/mcp/test_secret_links.py.
    {
        "name": "swarm_secret_set_link",
        "description": (
            "A one-time console link to Config > Secrets' add or rotate form, where the "
            "person types a secret's value themselves: answers ONLY `url` and "
            "`expires_at` (ten minutes, one use, bound to their own console session). "
            "There is deliberately no argument for the value: never ask for it, never "
            "accept it in chat. `scope`: `workspace` (the selected workspace's store, "
            "an admin's), `personal` (your own personal workspace's; the link is minted "
            "there, so open it with that workspace selected) or `repository` (released "
            "only to agents that clone it; name it in `repository` as owner/repo or its "
            "repo_ id). `action`: `add` (the form asks for the name) or `rotate` (name "
            "the secret in `name`, as swarm_secrets lists it). A null url comes with a "
            "`note`: show it."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": list(secret_tools.SCOPES)},
                "action": {"type": "string", "enum": list(secret_tools.ACTIONS)},
                "name": {"type": "string", "pattern": secret_tools.STORE_NAME.pattern},
                "repository": {"type": "string", "pattern": secret_tools.REPOSITORY_PATTERN},
            },
            "required": ["scope", "action"],
        },
    },
    {
        "name": "swarm_actions_secret_link",
        "description": (
            "A one-time console link to the form that sets a GitHub Actions secret of a "
            "repository registered in the selected workspace, at repository level or in "
            "one `environment`: answers ONLY `url` and `expires_at` (ten minutes, one "
            "use). The person types the value into the form; SwarmCloud seals it to "
            "GitHub's key, sends it to GitHub and keeps only the name, who and when. "
            "There is deliberately no argument for the value: never ask for it. "
            "`repository` is owner/repo or its repo_ id; `name` is GitHub's secret name "
            "(letters, digits, underscores; not GITHUB_...). Writing needs a workspace "
            "admin whom GitHub calls admin of the repository; the API checks that when "
            "the link is minted and again when the form is sent."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repository": {"type": "string", "pattern": secret_tools.REPOSITORY_PATTERN},
                "name": {"type": "string", "pattern": secret_tools.ACTIONS_NAME.pattern},
                "environment": {"type": "string", "pattern": secret_tools.ENVIRONMENT.pattern},
            },
            "required": ["repository", "name"],
        },
    },
    {
        "name": "swarm_secrets",
        "description": (
            "Config > Secrets' one read: the secrets of one scope of the selected "
            "workspace -- `workspace` (its store, TypeSafe and runner keys, MCP servers' "
            "stored credentials, GitHub token status), `personal` (your own) or "
            "`repository` (name it in `repository`: its secrets for agents, its QA "
            "targets' sign-in status, the Actions secrets SwarmCloud wrote). Names, "
            "status, who set each and when, what it is granted to, and which scope's "
            "value wins a shared name (`shadowed_by`, `shadows`). Never a value, a "
            "length or any part of one. Read-only; `text` is what to show."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": list(secret_tools.SCOPES)},
                "repository": {"type": "string", "pattern": secret_tools.REPOSITORY_PATTERN},
            },
            "required": ["scope"],
        },
    },
    {
        "name": "swarm_platform_secrets",
        "description": (
            "Admin > Platform secrets: each platform secret's status from Secret Manager "
            "metadata (set, enabled versions, the newest one's time, who declares and "
            "who writes it), then the quota broker's account secrets. Platform admins "
            "only: anyone else is refused 403 by the API, and that refusal is the answer. "
            "Never a value. Read-only; `text` is what to show."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
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
        "name": "swarm_account_pause",
        "description": (
            "PAUSE one of YOUR subscription accounts: no new agent is assigned to "
            "it; agents already on it keep running. Resolved by label or id "
            "against your own accounts in swarm_accounts. The reply carries the "
            "broker's answer verbatim."
        ),
        "inputSchema": _ACCOUNT_STATE_SCHEMA,
    },
    {
        "name": "swarm_account_resume",
        "description": (
            "Make one of YOUR paused or draining accounts AVAILABLE again for new "
            "assignments. Resolved as swarm_account_pause resolves. The reply "
            "carries the broker's answer verbatim."
        ),
        "inputSchema": _ACCOUNT_STATE_SCHEMA,
    },
    {
        "name": "swarm_account_drain",
        "description": (
            "DRAIN one of YOUR accounts -- what that does is in the reply's "
            "`what_draining_does`, beside the broker's answer verbatim. Use it "
            "before removing an account."
        ),
        "inputSchema": _ACCOUNT_STATE_SCHEMA,
    },
    {
        "name": "swarm_account_remove",
        "description": (
            "REMOVE one of YOUR accounts from the pool. Destructive, so the HUMAN "
            "confirms it: the bridge asks them, through this host's own prompt, "
            "to type the account's label, and nothing the caller passes counts "
            "as that answer. A host that cannot ask its human sends nothing, and "
            "the reply names the terminal command that asks there instead. The broker removes the pool entry and KEEPS the "
            "account's secret (its answer, returned verbatim, says so). Drain it "
            "first if agents are running on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "The account's label or id."},
            },
            "required": ["account"],
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
            "paused pools, leases held past their TTL, parked and dead-lettered "
            "tasks. A task PARKED on DEPENDENCY_INCOMPLETE is left out: it is "
            "waiting for an earlier step, holds no capacity, and is not "
            "trouble; every other park reason is listed. A subsystem that "
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
        "name": "swarm_territory",
        "description": (
            "Before dispatching parallel lanes: which files would each one reach, "
            "and who already holds them. Each lane's `files` (paths or `dir/` "
            "prefixes it will edit) are expanded by the repository's knowledge graph "
            "to the callers a change forces and the tests that cover them, with the "
            "seams among them; then intersected with the repository's open pull "
            "requests, the tenant's live issue-run steps, the other lanes given here "
            "and the tasks this session dispatched that are still running. Every "
            "overlap names both sides. Read-only; sends nothing.\n"
            "\n"
            "`status` says what the answer rests on: `checked`; `no_index` (the "
            "repository has no version-3 graph, so only the named paths were "
            "compared); `unregistered`; `unchecked` with the `reason`. "
            "`not_read` lists what could not be read, which is never the same as "
            "no overlap. swarm_dispatch and swarm_workflow make the same check "
            "when a unit declares `files`, and warn without refusing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One unit's files: repository paths or `dir/` prefixes.",
                },
                "lanes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "How the answer names this lane."},
                            "files": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["files"],
                    },
                    "description": (
                        "Several units at once, instead of `files`: each one's "
                        "territory, and every pair that overlaps."
                    ),
                },
                "repo": {"type": "string", "description": "The repository: a URL or owner/repo."},
                "infer": {
                    "type": "boolean",
                    "default": False,
                    "description": "With no `repo`: this checkout's repository, as swarm_dispatch infers it.",
                },
                "depth": {
                    "type": "integer",
                    "description": "How many caller hops to expand (the API takes 1, the default, or 2).",
                },
                "exclude_runs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Issue-run ids left out of the overlap: the caller's own.",
                },
                "exclude_pull_requests": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Pull request numbers left out of the overlap: the caller's own.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "swarm_task_answer",
        "description": (
            "Record on a SwarmCloud task the answer a person gave, in this Claude Code "
            "session, to one of the questions the task's agent asked (its questions.json: "
            "`owner_questions` in swarm_result, `questions` on a row's result). It carries "
            "only the person's own pick, verbatim, from a question this session asked them; "
            "never an answer the agent chose itself, a recommended option the person did "
            "not pick, or a summary of what they might want. `choice` is the question's own "
            "option label, exactly as served, when the person picked one; `text` is the "
            "person's words, or what they chose when it was not one of the options. "
            "`question_index` is the question's 0-based place in the task's questions. "
            "The bridge sends `via: claude-code` and this session's id, so the console "
            "shows it as answered in Claude Code; neither changes who may answer: the "
            "task's submitter or an admin, a viewer never. Re-answering replaces the "
            "answer and keeps the earlier one in its history. Answers with the questions "
            "as the API now serves them, or the API's refusal word for word."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task whose agent asked."},
                "question_index": {
                    "type": "integer",
                    "description": "The question's 0-based place in the task's questions.",
                },
                "choice": {
                    "type": "string",
                    "description": "The option label the person picked, exactly as the task serves it.",
                },
                "text": {
                    "type": "string",
                    "description": "The person's own words, or what they chose (at most 2,000 characters).",
                },
            },
            "required": ["task_id", "question_index"],
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
    often as `false` -- as true, and for `infer` that is a dispatch that
    silently clones a repository the caller asked NOT to infer.
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


#: Signatures of dispatches already sent, per CLIENT (one proxy session):
#: `id(client)` isn't safe (a garbage-collected client can hand its address to
#: a new object), so this is keyed on the client itself, and an entry
#: disappears the moment its client does.
_DISPATCHED: "weakref.WeakKeyDictionary[Any, set[tuple]]" = weakref.WeakKeyDictionary()


def check_mcp_credentials(raw: Any, named: Any, *, where: str) -> dict[str, Any] | None:
    """A per-run credential map, its SHAPE checked before anything travels
    (docs/design/registered-mcp-servers.md §4.3): an object of at most
    MAX_MCP_CREDENTIALS entries, each `{value: string, expires_in: integer}`
    and nothing else, each keyed by a server the work names in
    `inputs.mcp_servers` (`named`). None or `{}` is no map.

    Shape only. Which servers take a credential, the value's length and
    alphabet and the `expires_in` range are swarm-api's rules
    (`mcp_credentials.check_credential`), refused there with their own codes.
    EVERY REFUSAL NAMES THE FIELD AND THE RULE, NEVER A VALUE: a refusal is a
    tool reply, and a tool reply is the session's transcript.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SwarmError(
            f"{where}: `mcp_credentials` is an object of server name to "
            "{value, expires_in}. Nothing was sent"
        )
    if not raw:
        return None
    if len(raw) > MAX_MCP_CREDENTIALS:
        raise SwarmError(
            f"{where}: `mcp_credentials` holds {len(raw)} entries, and a step names at most "
            f"{MAX_MCP_CREDENTIALS} servers, so it takes at most 4. Nothing was sent"
        )
    names = {str(n) for n in named} if isinstance(named, (list, set, tuple)) else set()
    for server_name, entry in raw.items():
        if not (isinstance(entry, dict) and set(entry) == {"value", "expires_in"}
                and isinstance(entry["value"], str)
                and isinstance(entry["expires_in"], int) and not isinstance(entry["expires_in"], bool)):
            raise SwarmError(
                f"{where}: `mcp_credentials` entry {str(server_name)[:40]!r} is not "
                "{value, expires_in}: `value` a string, `expires_in` whole seconds, "
                "nothing else. Nothing was sent"
            )
        if server_name not in names:
            raise SwarmError(
                f"{where}: `mcp_credentials` has an entry for {str(server_name)[:40]!r}, and the "
                f"work's `inputs.mcp_servers` does not name it (it names {sorted(names)}). "
                "Nothing was sent"
            )
    return raw


def without_credentials(value: Any) -> Any:
    """`value` with every `mcp_credentials` it holds cut down to
    `{server, expires_at}` per entry -- what swarm-api answers for a stored
    credential (§4.3) -- at any depth.

    Applied to every answer of a dispatch or a workflow submission BEFORE it
    reaches a reply, a run copy or `launched.jsonl`. swarm-api never echoes a
    value; this is what keeps "the plugin's log of a dispatch holds no
    credential" true of an API that one day did, rather than true only
    because of what another service chose to answer.
    """
    if isinstance(value, dict):
        return {
            key: (_credential_echo(item) if key == "mcp_credentials" else without_credentials(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [without_credentials(item) for item in value]
    return value


def _credential_echo(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        entries = [{"server": key, **(item if isinstance(item, dict) else {})} for key, item in value.items()]
    elif isinstance(value, list):
        entries = [item for item in value if isinstance(item, dict)]
    else:
        entries = []
    return [{"server": entry.get("server"), "expires_at": entry.get("expires_at")} for entry in entries]


def _send_task(client: Any, credentials: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
    """`POST /v1/tasks` for one task, with its per-run credential map beside
    `input` when it has one.

    Without a map this is `client.dispatch`, the call every dispatch made
    before MCP servers existed, so a dispatch naming none is sent exactly as
    it was. With one, the body is the same `task_payload` plus
    `mcp_credentials`, sent through the client's own transport: the map is a
    request field, never a part of `input`, which the spec signature covers
    and the runner reads.
    """
    if not credentials:
        return client.dispatch(**fields)
    payload = task_payload(**fields)
    payload["mcp_credentials"] = credentials
    return unwrap_task(client.request("POST", "/v1/tasks", payload=payload))


def _step_servers(steps: list[dict[str, Any]]) -> set[str]:
    """Every server any step of a workflow names, which the workflow's one
    credential map may hold entries for."""
    named: set[str] = set()
    for step in steps:
        servers = (step.get("input") or {}).get("mcp_servers") if isinstance(step, dict) else None
        if isinstance(servers, list):
            named.update(str(n) for n in servers)
    return named


def _dispatch_signature(profile: str, prompt: str, strategy: str | None, repository, inputs: Any,
                        additional: list[dict[str, str]] | None = None,
                        model_class: str | None = None,
                        publish_to_pr: int | str | None = None) -> tuple:
    # The repositories it reads are part of what the task IS: the same prompt
    # over another set of repositories is a different question, not a repeat.
    # So is the model class it runs (#1435): one prompt on `haiku` and on
    # `opus` is a comparison, not a double dispatch. So is the pull request it
    # publishes onto (#2183): the same prompt as a new pull request and onto
    # an existing one are two deliveries.
    return (
        profile, prompt, strategy or "collect", repository.url, repository.ref,
        json.dumps(inputs or {}, sort_keys=True, default=str),
        json.dumps(additional, sort_keys=True),
        model_class,
        publish_to_pr,
    )


def _refuse_a_repeat_dispatch(client: Any, signature: tuple) -> None:
    """`sc:remote` keeps `swarm_dispatch` in its tool list for its whole row --
    Claude Code has no way to narrow a plugin agent's tools mid-turn -- so
    nothing stops a haiku row calling it a second time with its own
    instructions unchanged. This is the fallback: an IDENTICAL second dispatch
    from the same client (the same proxy session) is refused rather than paid
    for. A genuinely different prompt -- a different row's own step -- is a
    different signature and is never refused. Checked BEFORE the round trip;
    the signature is recorded separately, only once the dispatch actually
    reaches the API, so a retry after a FAILED dispatch is never blocked by
    its own earlier attempt.
    """
    if signature in _DISPATCHED.get(client, ()):
        raise SwarmError(
            "this exact prompt (profile, repository and strategy unchanged) was already "
            "dispatched once in this session; swarm_dispatch is refused a second time for "
            "it. `sc:remote` dispatches its instructions exactly once -- follow the task "
            "already running, or change the prompt if this is genuinely different work"
        )


def _remember_dispatch(client: Any, signature: tuple) -> None:
    _DISPATCHED.setdefault(client, set()).add(signature)


#: How a per-call `target` is named in a reply, beside a session default's source.
_CALL_TARGET = "this call's `target`"


def _needs_local(args: dict[str, Any]) -> list[str]:
    """The call's `needs_local`, checked: each entry one of NEEDS_LOCAL.

    An entry it does not know is refused, not dropped -- a dropped
    `"filesytem"` is a unit that needed this machine dispatched to one that
    does not have it.
    """
    value = args.get("needs_local")
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise SwarmError(f"`needs_local` is a list drawn from {list(NEEDS_LOCAL)}. Nothing was sent")
    needs: list[str] = []
    for item in value:
        text = item.strip().lower() if isinstance(item, str) else ""
        if text not in NEEDS_LOCAL:
            raise SwarmError(
                f"`needs_local` takes {', '.join(NEEDS_LOCAL)}; {item!r} is none of them. "
                "Nothing was sent"
            )
        if text not in needs:
            needs.append(text)
    return needs


def _placement(name: str, args: dict[str, Any], *, profiles: list[str]) -> dict[str, Any]:
    """Where this call's work goes (S8), decided BEFORE anything is read or sent.

    The per-call `target` first, then the session default
    (`config.session_target`). `local` refuses outright; `hybrid` applies the
    spec's explicit rule -- a unit naming a runner profile goes to the cloud,
    a unit declaring `needs_local` stays here -- and never a heuristic over the
    prompt. A refusal is a SwarmError, so the host shows it as the tool's
    answer and nothing has travelled: no stats read, no checkout probe, no
    dispatch.

    `profiles` is what the call's units name, with the tool's documented
    default (`claude-code`) where a unit omits one: the schema declares that
    default, so a unit that omits it names it.
    """
    if args.get("target") is not None:
        target = config.Target(config.check_target(args["target"], where=f"{name}'s `target`"), _CALL_TARGET)
    else:
        target = config.session_target()
    needs = _needs_local(args)
    applied = f"target `{target.value}` (from {target.source})"
    if target.value == "local":
        raise SwarmError(
            f"{applied} applies to this call, so NOTHING was sent to SwarmCloud: this "
            "work is to run locally in this session -- do it here, directly or as a "
            "local subagent. Only if the developer wants it dispatched, call again with "
            "`target: \"cloud\"`"
        )
    if target.value == "hybrid" and needs:
        reasons = "; ".join(f"`{need}`: {_NEEDS_LOCAL_WHY[need]}" for need in needs)
        raise SwarmError(
            f"{applied} keeps work local when the call declares `needs_local`, and this "
            f"call declares {needs}, so NOTHING was sent to SwarmCloud: {reasons}. Run it "
            "locally in this session"
        )
    placed: dict[str, Any] = {"applied": target.value, "from": target.source, "sent_to": "cloud"}
    if target.value == "hybrid":
        named = ", ".join(sorted(set(profiles)))
        placed["because"] = (
            f"every unit names a runner profile ({named}) and the call declares no `needs_local`"
            if named
            # A workflow whose steps could not be listed here: swarm-api
            # validates the spec, and the rule's other half still held.
            else "the call declares no `needs_local`"
        )
    elif needs:
        placed["needs_local_not_consulted"] = (
            f"target `cloud` dispatches whatever the call declares, so {needs} was not "
            "consulted; pass `target: \"hybrid\"` for it to keep this work local"
        )
    return placed


def _batch_limit(client: Any) -> tuple[int | None, str | None]:
    """The API's `max_batch_size`, read from `/v1/stats` and never restated.

    `(limit, None)`, or `(None, why)` when it could not be read -- and then the
    batch is still sent: swarm-api enforces the same limit on the request
    (`validate_batch_size`) and is the authority. This check only refuses
    sooner, with the number, when it can.
    """
    from .sc import fetch_stats

    try:
        stats = fetch_stats(client)
    except SwarmError as exc:
        return None, f"the API's max_batch_size could not be read ({exc}); the API checks it itself"
    limits = stats.get("limits") if isinstance(stats, dict) else None
    limit = limits.get("max_batch_size") if isinstance(limits, dict) else None
    if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1:
        return limit, None
    return None, "the API's /v1/stats carried no limits.max_batch_size; the API checks it itself"


#: What a dispatch reply's `rows` tells the session to do with it (#830).
ROWS_START_NOW = (
    "Start the live [SwarmCloud] row of what you just dispatched NOW, without being "
    "asked: call the Workflow tool once with {scriptPath: <rows.script_path>, args: "
    "<rows.args>}. It starts one sc:task row per task, titled from its label, follows "
    "it until it finishes, and submits nothing. Skip it only when you are already that "
    "task's row (sc:remote follows the task it dispatched itself)"
)


def _submitted_session_fields() -> dict[str, Any]:
    """What every reply to a submission carries (SC-SESSION-SCOPE, owner
    2026-10-14): `claude_session`, the Claude Code session the work was
    tagged with (null when this bridge knows none, and then nothing it sent
    counts as this session's), and `arm_watch` -- a task sent with
    swarm_dispatch arms the watch exactly as a workflow does."""
    from . import session, watch

    sid = session.current()
    out: dict[str, Any] = {"claude_session": sid}
    if not sid:
        out["claude_session_note"] = session.UNKNOWN_NOTE
    out.update(watch.arm_fields(True))
    return out


def _dispatch_rows(sent: list[tuple[str, str | None, dict[str, Any]]]) -> dict[str, Any]:
    """The row launch for tasks just dispatched (#830): `swarm_dispatch`'s
    reply hands it back so a dispatched task gets a row without anyone asking
    for one. `sent` is `(task_id, label, task)` per task.

    Never a reason for the dispatch to fail: the tasks are already in
    SwarmCloud, so a copy that cannot be written -- a bridge started outside
    the plugin has no run.js to copy -- comes back as `error` with
    `/sc attach --all`, which lists the caller's running single tasks and
    gives each a row."""
    from . import launch

    entries = []
    for task_id, label, task in sent:
        entry: dict[str, Any] = {"task_id": task_id, "label": label}
        # The workspace the task runs in, from the API's own task (SC-ROWS).
        workspace = workflows.workspace_of(task)
        if workspace:
            entry["workspace"] = workspace
        # The task as the API created it: its row's DETAILS (who started it,
        # its profile, its account once assigned, its session) are read off
        # it by `details`, never copied whole into the row (SC-DESC-DETAILS).
        entry["task"] = task
        entries.append(with_console(entry, task))
    try:
        rows = launch.for_tasks(entries)
    except (SwarmError, OSError) as exc:
        return {"error": str(exc), "attach_with": "/sc attach --all"}
    if rows.get("followed_by") != "band":
        # In the band layout there is no row to start: the strip follows the tasks.
        rows["start_now"] = ROWS_START_NOW
    return rows


# --------------------------------------------------------------------------
# territory: `swarm_territory` and the dispatch-time warning (lane KG7,
# docs/design/knowledge-graph.md §4.8)
# --------------------------------------------------------------------------
#
# WHY A WARNING AND NOT A REFUSAL. "Two issues that edit the same file are one
# lane" (CLAUDE.md, Issues) was checked by reading, and parallel lanes handed
# one file kept arriving at merge as conflicts. A unit that names the files it
# will edit (`files`) is now expanded by swarm-api's territory route (KG3) to
# the callers and tests a change to them reaches, and every overlap with an
# open pull request, a live issue-run step, another unit of the same
# submission or a task this session dispatched that is still in flight is
# NAMED in the reply, both sides. The dispatch still goes: whether two lanes
# should be chained, merged or run anyway is the orchestrator's judgement,
# and the territory is built from graph facts that a stale index can miss
# (§7.8, judgement in gates). The row's target is "unannounced → 0", not
# "overlapping → 0".
#
# WHY SILENT WITHOUT A V3 INDEX. Below format 3 the graph has no fact-edge
# communities, and with no promoted graph at all the route can only compare
# the named paths; a warning built from that is the hand-reading this
# replaces, with a confidence it has not earned. So a repository that is not
# registered, has no promoted graph, or has one older than
# TERRITORY_INDEX_FORMAT adds nothing to the reply. A route that FAILED is a
# different case -- "not read is never no overlap" -- and says so in
# `territory_unchecked`. `swarm_territory`, asked directly, always says which.
#
# `files` IS READ HERE AND NEVER SENT. The platform has no such field on a
# task or a workflow step (`WorkflowStepCreate` forbids extras), so it is
# stripped before `workflows.build_steps`/`read_spec` see a step, the way
# `stage` is a display-only key there.

#: The first shard format whose graph carries KG2's communities and fact edges
#: (`swarm_api.territory.INDEX_FORMAT`). Below it the warning is silent.
TERRITORY_INDEX_FORMAT = 3

#: How many units one territory check reads. One route call per unit; a batch
#: is bounded by max_batch_size before this matters, a workflow by its step
#: ceiling. Units past this are not checked, and the reply says so.
MAX_TERRITORY_LANES = 20

#: How many units dispatched with declared files this session remembers, per
#: client, to compare later dispatches with while they are in flight.
MAX_REMEMBERED_LANES = 50

#: Shared paths shown per overlap; `shared_total` carries the full count.
MAX_SHARED_SHOWN = 10

#: Concurrent territory reads for one check.
TERRITORY_READ_WORKERS = 8



def _declared_files(value: Any, where: str) -> list[str] | None:
    """A unit's `files`: None when it declares none; refused when it is not a list of paths."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(f, str) and f.strip() for f in value):
        raise SwarmError(
            f"{where}'s `files` is a list of repository paths or `dir/` prefixes, each a "
            "non-empty string. Nothing was sent"
        )
    files = list(dict.fromkeys(f.strip() for f in value))
    return files or None


#: Step keys `workflows.build_steps` never sees: `files` (above), read here
#: and never sent, and `model_class` (#1435), checked and put back on the
#: built step by `profiles.with_model_classes`.
_STEP_KEYS_HELD_HERE = frozenset({"files", "model_class"})


def _without_files(steps: Any) -> Any:
    """Workflow steps with the keys this module holds removed (see above)."""
    if not isinstance(steps, list):
        return steps
    return [
        {k: v for k, v in step.items() if k not in _STEP_KEYS_HELD_HERE}
        if isinstance(step, dict) else step
        for step in steps
    ]


def _spec_without_files(spec: Any) -> Any:
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
        return spec
    return {**spec, "steps": _without_files(spec["steps"])}


def _repo_slug(url: Any) -> tuple[str, str] | None:
    """`(owner, repo)` of a GitHub repository URL (https, ssh, git@) or `owner/repo`."""
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text and "@" not in text and ":" not in text:
        host, path = "github.com", text
    else:
        try:
            parts = urlsplit(checkout.https_url(text))
        except SwarmError:
            return None
        host, path = (parts.hostname or "").lower(), parts.path
    segments = [s for s in path.strip("/").split("/") if s]
    if host != "github.com" or len(segments) != 2:
        return None
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    return (owner, repo) if owner and repo else None


def _conflicts(a: str, b: str) -> bool:
    """`swarm_api.territory.conflicts`: equal, or one a `dir/` prefix of the other."""
    if a == b:
        return True
    return (a.endswith("/") and b.startswith(a)) or (b.endswith("/") and a.startswith(b))


def _territory_paths(answer: dict[str, Any]) -> list[tuple[str, str]]:
    """`swarm_api.territory.territory_paths` over a route answer: (entry, why)."""
    out = [(str(e), "named") for e in answer.get("named") or []]
    out += [(str(r.get("path")), "caller") for r in answer.get("callers") or [] if isinstance(r, dict)]
    out += [(str(r.get("path")), "test") for r in answer.get("tests") or [] if isinstance(r, dict)]
    return out


def _seams(answer: dict[str, Any]) -> set[str]:
    return {str(s.get("path")) for s in answer.get("seams") or [] if isinstance(s, dict)}


def _shared(named: list[str], territory: list[tuple[str, str]], seams: set[str]) -> list[dict[str, Any]]:
    """Each of `named` that falls inside `territory`, as the route's `shared` rows."""
    rows = []
    for path in sorted(set(named)):
        for entry, why in territory:
            if _conflicts(entry, path):
                rows.append({"path": path, "entry": entry, "why": why,
                             "seam": path in seams or entry in seams})
                break
    return rows


def _between(a: str, ta: dict[str, Any], b: str, tb: dict[str, Any]) -> list[dict[str, Any]]:
    """Where lane `a` and lane `b` overlap: either's named files inside the other's territory."""
    seams = _seams(ta) | _seams(tb)
    shared = _shared(list(tb.get("named") or []), _territory_paths(ta), seams)
    known = {row["path"] for row in shared}
    shared += [row for row in _shared(list(ta.get("named") or []), _territory_paths(tb), seams)
               if row["path"] not in known]
    return shared


def _has_index(answer: dict[str, Any]) -> bool:
    version = answer.get("format_version")
    return (answer.get("graph_digest") is not None and isinstance(version, int)
            and not isinstance(version, bool) and version >= TERRITORY_INDEX_FORMAT)


#: Units this session dispatched with declared files, per client, while they
#: may still be in flight: {task_id, lane, slug, answer}. Weakly keyed, as
#: `_DISPATCHED` is, for the same reason.
_IN_FLIGHT: "weakref.WeakKeyDictionary[Any, list[dict[str, Any]]]" = weakref.WeakKeyDictionary()
_IN_FLIGHT_LOCK = threading.Lock()


def _overlap_row(lane: str, other: dict[str, Any], shared: list[dict[str, Any]],
                 total: int | None = None) -> dict[str, Any]:
    if other["kind"] == "pull_request":
        name = f"open pull request #{other.get('number')}"
    elif other["kind"] == "issue_run_step":
        name = (f"issue run {other.get('run_id')} (issue #{other.get('issue')}, step "
                f"{other.get('step_id')!r}, {other.get('state')})")
    elif other["kind"] == "dispatched_task":
        name = f"in-flight task {other.get('task_id')} ({other.get('lane')})"
    else:
        name = f"{other.get('lane')} of this same call"
    paths = ", ".join(row["path"] for row in shared[:3])
    more = (total or len(shared)) - min(3, len(shared))
    return {
        "lane": lane,
        "with": other,
        "shared": shared[:MAX_SHARED_SHOWN],
        "shared_total": total if total is not None else len(shared),
        "says": f"{lane} overlaps {name} on {paths}" + (f" and {more} more" if more > 0 else ""),
    }


def _territory_check(
    client: Any,
    repository_url: Any,
    lanes: list[dict[str, Any]],
    *,
    related: Any = None,
    depth: int | None = None,
    exclude_runs: list[str] | None = None,
    exclude_pull_requests: list[int] | None = None,
) -> dict[str, Any]:
    """Every lane's territory and every overlap it has, or why there are none.

    `lanes` is `[{"lane": name, "files": [...]}]`. `related(a, b)` says two
    lanes of THIS call are on one dependency line, so their overlap is the
    plan and not a collision; without it every pair is compared.

    `status` is `checked`, `no_index` (silent at dispatch), `no_repository`,
    `unregistered` or `unchecked` (with `reason`).
    """
    lanes = [lane for lane in lanes if lane.get("files")]
    if not lanes:
        return {"status": "no_files", "lanes": {}, "overlaps": []}
    slug = _repo_slug(repository_url)
    if slug is None:
        return {"status": "no_repository", "lanes": {}, "overlaps": [],
                "reason": "the unit names no GitHub repository, so it has no registered graph"}
    try:
        record = client.registration(*slug)
    except SwarmError as exc:
        if exc.status == 404:
            return {"status": "unregistered", "lanes": {}, "overlaps": [],
                    "reason": "this deployment serves no repository registrations"}
        return {"status": "unchecked", "lanes": {}, "overlaps": [],
                "reason": f"the repository's registration could not be read: {exc}"[:400]}
    if not record or not record.get("repo_id"):
        return {"status": "unregistered", "lanes": {}, "overlaps": [],
                "reason": f"{slug[0]}/{slug[1]} is not registered to this tenant, so it has no graph"}
    repo_id = str(record["repo_id"])
    checked, skipped = lanes[:MAX_TERRITORY_LANES], [lane["lane"] for lane in lanes[MAX_TERRITORY_LANES:]]

    def read(lane: dict[str, Any]) -> dict[str, Any]:
        return client.territory(repo_id, lane["files"], depth=depth,
                                exclude_runs=exclude_runs or (),
                                exclude_pull_requests=exclude_pull_requests or ())

    answers: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(TERRITORY_READ_WORKERS, len(checked)),
                            thread_name_prefix="swarm-mcp-territory") as pool:
        futures = [(lane["lane"], pool.submit(read, lane)) for lane in checked]
        for name, future in futures:
            try:
                answers[name] = future.result()
            except SwarmError as exc:
                if exc.status == 404:
                    return {"status": "no_index", "lanes": {}, "overlaps": [], "repo_id": repo_id,
                            "reason": "this deployment has no territory route for the repository"}
                return {"status": "unchecked", "lanes": {}, "overlaps": [], "repo_id": repo_id,
                        "reason": f"the territory of {name} could not be read: {exc}"[:400]}
    first = next(iter(answers.values()))
    index = {k: first.get(k) for k in ("index_sha", "head_sha", "behind_by", "stale",
                                         "graph_digest", "format_version")}
    overlaps: list[dict[str, Any]] = []
    not_read: list[str] = []
    for name, answer in answers.items():
        overlap = answer.get("overlap") or {}
        read_state = overlap.get("pull_requests_read") or {}
        if read_state and read_state.get("ok") is False:
            not_read.append(f"{name}: the open pull requests could not be read "
                            f"({read_state.get('code')})")
        unread = overlap.get("pull_requests_unread") or []
        if unread:
            not_read.append(f"{name}: the files of pull request(s) "
                            f"{', '.join('#' + str(n) for n in unread)} were not read")
        undeclared = overlap.get("lanes_undeclared") or []
        if undeclared:
            runs = ", ".join(sorted({str(u.get("run_id")) for u in undeclared if isinstance(u, dict)}))
            not_read.append(f"{name}: issue run(s) {runs} have declared no files yet")
        for pull in overlap.get("pull_requests") or []:
            overlaps.append(_overlap_row(
                name, {"kind": "pull_request", "number": pull.get("number"), "title": pull.get("title")},
                list(pull.get("shared") or []), pull.get("shared_total")))
        for run in overlap.get("lanes") or []:
            overlaps.append(_overlap_row(
                name, {"kind": "issue_run_step", **{k: run.get(k) for k in
                       ("run_id", "issue", "step_id", "state", "pull_request")}},
                list(run.get("shared") or []), run.get("shared_total")))
    names = list(answers)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if related is not None and related(a, b):
                continue
            shared = _between(a, answers[a], b, answers[b])
            if shared:
                overlaps.append(_overlap_row(a, {"kind": "this_call", "lane": b}, shared))
    with _IN_FLIGHT_LOCK:
        remembered = [r for r in _IN_FLIGHT.get(client, []) if r["slug"] == slug]
    for row in remembered:
        try:
            state = str((client.task(row["task_id"]) or {}).get("state") or "")
        except SwarmError:
            not_read.append(f"in-flight task {row['task_id']} ({row['lane']}) could not be read")
            continue
        if state in TERMINAL:
            with _IN_FLIGHT_LOCK:
                held = _IN_FLIGHT.get(client, [])
                if row in held:
                    held.remove(row)
            continue
        for name, answer in answers.items():
            shared = _between(name, answer, row["lane"], row["answer"])
            if shared:
                overlaps.append(_overlap_row(
                    name, {"kind": "dispatched_task", "task_id": row["task_id"],
                           "lane": row["lane"], "state": state}, shared))
    result: dict[str, Any] = {"status": "checked", "lanes": answers, "overlaps": overlaps,
                              "not_read": not_read, "repo_id": repo_id, "index": index,
                              "slug": slug}
    if not all(_has_index(a) for a in answers.values()):
        # The route still compared the NAMED paths; `swarm_territory` shows
        # that, and a dispatch says nothing (see the section's note).
        result.update(status="no_index", reason=(
            "the repository has no promoted graph of index format "
            f"{TERRITORY_INDEX_FORMAT} or later, so the named files were not expanded "
            "to their callers and tests"))
    if skipped:
        result["not_checked"] = skipped
    return result


def _territory_reply(check: dict[str, Any]) -> dict[str, Any]:
    """What a dispatch reply gains from a check: a warning, a note, or nothing."""
    if check["status"] == "unchecked":
        return {"territory_unchecked": check["reason"]}
    if check["status"] != "checked":
        return {}  # no repository, not registered, no v3 index: silent.
    not_read = list(check.get("not_read") or [])
    if check.get("not_checked"):
        not_read.append(f"not checked (past {MAX_TERRITORY_LANES} units): "
                        + ", ".join(check["not_checked"]))
    if not check["overlaps"] and not not_read:
        return {}
    warning: dict[str, Any] = {
        "overlaps": check["overlaps"],
        "index": check.get("index"),
    }
    if check["overlaps"]:
        warning["warning"] = (
            f"DISPATCHED ANYWAY, with {len(check['overlaps'])} territory overlap(s): "
            + "; ".join(row["says"] for row in check["overlaps"][:5])
            + ". Lanes that edit the same files conflict at merge -- two issues that edit "
            "the same file are one lane (CLAUDE.md). Chain them with depends_on, fold "
            "them into one unit, or cancel one; this bridge does not decide which"
        )
    if not_read:
        warning["not_read"] = not_read
    return {"territory_warning": warning}


def _territory_of_units(
    client: Any, units: list[tuple[Any, dict[str, Any]]], *, related: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check `units` -- `(repository_url, {lane, files})` -- one check per repository.

    Returns the reply's additions (`_territory_reply`, merged) and the checks,
    for `_remember_territory` once the units are sent.
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for url, lane in units:
        if lane.get("files") and url:
            groups.setdefault(url, []).append(lane)
    checks = [_territory_check(client, url, lanes, related=related) for url, lanes in groups.items()]
    merged: dict[str, Any] = {}
    for check in checks:
        reply = _territory_reply(check)
        if "territory_unchecked" in reply:
            merged.setdefault("territory_unchecked", []).append(reply["territory_unchecked"])
        if "territory_warning" in reply:
            warning = merged.setdefault("territory_warning", {"overlaps": []})
            add = reply["territory_warning"]
            warning["overlaps"] += add["overlaps"]
            warning.setdefault("index", add.get("index"))
            if add.get("not_read"):
                warning.setdefault("not_read", []).extend(add["not_read"])
    if "territory_unchecked" in merged:
        merged["territory_unchecked"] = "; ".join(merged["territory_unchecked"])
    warning = merged.get("territory_warning")
    if warning is not None and warning["overlaps"]:
        # One sentence over every repository's overlaps, worded as one check's.
        warning["warning"] = _territory_reply(
            {"status": "checked", "overlaps": warning["overlaps"]})["territory_warning"]["warning"]
    return merged, checks


def _remember_territory(client: Any, check: dict[str, Any], sent: list[tuple[str, str]]) -> None:
    """Hold each sent unit's territory against later dispatches: `(lane, task_id)`."""
    if check.get("status") != "checked":
        return
    with _IN_FLIGHT_LOCK:
        held = _IN_FLIGHT.setdefault(client, [])
        for lane, task_id in sent:
            answer = check["lanes"].get(lane)
            if answer is not None and task_id:
                held.append({"task_id": task_id, "lane": lane, "slug": check["slug"],
                             "answer": answer})
        del held[:max(0, len(held) - MAX_REMEMBERED_LANES)]


def _dependency_lines(steps: list[Any]) -> Any:
    """`related(a, b)` for workflow steps: one is upstream of the other.

    Upstream through `depends_on`, `input_from` and `builds_on`. A cycle or a
    dangling id is the API's to refuse; here it only stops the walk.
    """
    parents: dict[str, set[str]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("step_id") or "").strip()
        ups = {str(d) for d in step.get("depends_on") or [] if isinstance(d, str)}
        if isinstance(step.get("input_from"), dict):
            ups |= {str(k) for k in step["input_from"]}
        if isinstance(step.get("builds_on"), str):
            ups.add(step["builds_on"])
        parents[sid] = ups

    def ancestors(sid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(parents.get(sid, ()))
        while stack:
            up = stack.pop()
            if up not in seen:
                seen.add(up)
                stack.extend(parents.get(up, ()))
        return seen

    lines = {sid: ancestors(sid) for sid in parents}
    return lambda a, b: a in lines.get(b, set()) or b in lines.get(a, set())


def _step_lanes(steps: Any) -> list[dict[str, Any]]:
    """`[{lane: step_id, files}]` for the steps of a workflow that declare files."""
    lanes = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict):
            continue
        sid = str(step.get("step_id") or "").strip() or f"step[{index}]"
        files = _declared_files(step.get("files"), f"step {sid!r}")
        if files:
            lanes.append({"lane": sid, "files": files})
    return lanes


def _territory_tool(client: Any, args: dict[str, Any]) -> str:
    """`swarm_territory`: the knowledge graph's answer to "who else holds these files"."""
    if (args.get("files") is None) == (args.get("lanes") is None):
        raise SwarmError("swarm_territory takes `files` (one unit) or `lanes` (several), exactly one")
    if args.get("lanes") is not None:
        raw = args["lanes"]
        if not isinstance(raw, list) or not raw:
            raise SwarmError("`lanes` is a non-empty list of {name, files}")
        lanes = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise SwarmError(f"lanes[{index}] is not an object {{name, files}}")
            name = str(item.get("name") or "").strip() or f"lanes[{index}]"
            files = _declared_files(item.get("files"), f"lanes[{index}]")
            if not files:
                raise SwarmError(f"lanes[{index}] names no files")
            lanes.append({"lane": name, "files": files})
        if len({lane["lane"] for lane in lanes}) != len(lanes):
            raise SwarmError("two of `lanes` share a name; name each lane once")
    else:
        files = _declared_files(args.get("files"), "swarm_territory")
        if not files:
            raise SwarmError("`files` names no path")
        lanes = [{"lane": "files", "files": files}]
    if len(lanes) > MAX_TERRITORY_LANES:
        raise SwarmError(f"swarm_territory reads at most {MAX_TERRITORY_LANES} lanes at once")
    depth = args.get("depth")
    if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool) or depth < 1):
        raise SwarmError("`depth` is a positive integer (the API takes 1 or 2)")
    repository = checkout.resolve(repo=args.get("repo"), ref=None, infer=_flag(args, "infer"))
    if not repository.url:
        raise SwarmError("swarm_territory needs `repo`, or `infer: true` inside a checkout: "
                         + "; ".join(repository.notes))
    runs = args.get("exclude_runs") or []
    pulls = args.get("exclude_pull_requests") or []
    if not isinstance(runs, list) or not all(isinstance(r, str) and r.strip() for r in runs):
        raise SwarmError("`exclude_runs` is a list of issue-run ids")
    if not isinstance(pulls, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in pulls):
        raise SwarmError("`exclude_pull_requests` is a list of pull request numbers")
    check = _territory_check(client, repository.url, lanes, depth=depth,
                             exclude_runs=runs, exclude_pull_requests=pulls)
    answer: dict[str, Any] = {"repository": repository.as_dict(), "status": check["status"]}
    if check.get("reason"):
        answer["reason"] = check["reason"]
    if check.get("index"):
        answer["index"] = check["index"]
    answer["lanes"] = [
        {
            "lane": name,
            "named": t.get("named"),
            "unknown": t.get("unknown") or [],
            "callers": [r.get("path") for r in t.get("callers") or [] if isinstance(r, dict)],
            "tests": [r.get("path") for r in t.get("tests") or [] if isinstance(r, dict)],
            "seams": t.get("seams") or [],
            "communities": [{"id": c.get("id"), "label": c.get("label")}
                            for c in t.get("communities") or [] if isinstance(c, dict)],
            "truncated": t.get("truncated") or [],
        }
        for name, t in check["lanes"].items()
    ]
    if check["status"] in ("checked", "no_index"):
        answer["overlaps"] = check["overlaps"]
        answer["overlap_count"] = len(check["overlaps"])
        if check.get("not_read"):
            answer["not_read"] = check["not_read"]
        if check.get("not_checked"):
            answer["not_checked"] = check["not_checked"]
    return json.dumps(answer, indent=2)


def _dispatch_batch(client: Any, args: dict[str, Any], placed: dict[str, Any]) -> str:
    """`swarm_dispatch` with `tasks`: every task checked, then ONE request (S7).

    EVERY CHECK BEFORE THE ONE SEND, for every task: the profile and its
    inputs, the strategy, the repository, and the repeat guard -- against this
    session's earlier dispatches AND against the other tasks of this list. A
    refusal names the task by its index and sends nothing, so a list is never
    half-dispatched by this bridge. The API takes the batch all or nothing
    too (`submit_tasks`).
    """
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SwarmError("`tasks` is a non-empty list of tasks, each {prompt, runner_profile, ...}")
    limit, limit_note = _batch_limit(client)
    if limit is not None and len(tasks) > limit:
        raise SwarmError(
            f"`tasks` holds {len(tasks)} tasks and this API's max_batch_size is {limit} "
            "(its /v1/stats `limits`); nothing was sent. Split the list"
        )
    accepted = set(_BATCH_TASK_SCHEMA["properties"])
    prepared: list[tuple[dict[str, Any], tuple, Any, str | None]] = []
    units: list[tuple[Any, dict[str, Any]]] = []
    seen: dict[tuple, int] = {}
    for index, item in enumerate(tasks):
        where = f"swarm_dispatch tasks[{index}]"
        if not isinstance(item, dict):
            raise SwarmError(f"{where} is not an object. Nothing was sent")
        unknown = sorted(str(key) for key in item if key not in accepted)
        if unknown:
            raise SwarmError(
                f"{where} does not take {unknown}; a task takes {sorted(accepted)}. "
                "Nothing was sent"
            )
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise SwarmError(f"{where} has no prompt. Nothing was sent")
        profile = catalogue.check(item.get("runner_profile") or "claude-code", where=where)
        inputs = catalogue.check_inputs(profile, item.get("inputs"), where=where)
        credentials = check_mcp_credentials(
            item.get("mcp_credentials"), (inputs or {}).get("mcp_servers"), where=where
        )
        strategy = _dispatch_strategy(item.get("strategy"))
        additional = check_additional_repositories(item.get("additional_repositories"), where=where)
        model_class = catalogue.check_model_class(profile, item.get("model_class"), where=where)
        repository = checkout.resolve(repo=item.get("repo"), ref=item.get("ref"), infer=_flag(item, "infer"))
        if strategy == "direct-pr" and not repository.url:
            raise SwarmError(
                f"{where}: strategy 'direct-pr' needs a repository, and this task has "
                "none: " + "; ".join(repository.notes) + ". Nothing was sent"
            )
        signature = _dispatch_signature(profile, prompt, strategy, repository, inputs, additional,
                                        model_class)
        try:
            _refuse_a_repeat_dispatch(client, signature)
        except SwarmError as exc:
            raise SwarmError(f"{where}: {exc}. Nothing in this list was sent") from None
        if signature in seen:
            raise SwarmError(
                f"{where} is the same task as tasks[{seen[signature]}] (profile, prompt, "
                "repository and strategy unchanged): the list would dispatch it twice. "
                "Nothing was sent"
            )
        seen[signature] = index
        label = item.get("label")
        units.append((repository.url, {
            "lane": f"tasks[{index}]" + (f" ({label})" if isinstance(label, str) and label.strip() else ""),
            "files": _declared_files(item.get("files"), where),
        }))
        payload = task_payload(
            prompt=prompt,
            runner_profile=profile,
            repository_url=repository.url,
            repository_ref=repository.ref,
            metadata={"unit": item["label"]} if item.get("label") else None,
            inputs=inputs or None,
            strategy=strategy,
            additional_repositories=additional,
            model_class=model_class,
        )
        if credentials:
            payload["mcp_credentials"] = credentials
        prepared.append((payload, signature, repository, strategy))

    # THE TERRITORY, read before the one send and reported after it: a
    # warning, never a refusal (see "territory" above).
    territory, checks = _territory_of_units(client, units)
    created = without_credentials(client.dispatch_batch([payload for payload, *_ in prepared]))
    # The request reached the API, so every signature is remembered BEFORE the
    # answer is judged: a reply this bridge cannot read is not proof nothing
    # was created, and a retry must not create the list twice.
    for _, signature, _, _ in prepared:
        _remember_dispatch(client, signature)
    ids = [task_id_of(task) for task in created]
    if len(created) != len(prepared) or not all(ids):
        raise SwarmError(
            f"the API accepted a batch of {len(prepared)} and answered {len(created)} task(s) "
            f"with ids {ids}; do not resend it -- read the named tasks with swarm_status"
        )
    rows = [
        with_console(
            {
                "task_id": task_id,
                "state": task.get("state"),
                "strategy": _accepted_strategy(task, strategy),
                "repository": repository.as_dict(),
            },
            task,
        )
        for task_id, task, (_, _, repository, strategy) in zip(ids, created, prepared)
    ]
    answer: dict[str, Any] = {
        "task_ids": ids,
        "count": len(ids),
        "tasks": rows,
        "max_batch_size": limit,
        "target": placed,
        "collect_with": "swarm_collect",
        "follow_with": "swarm_follow",
        "follow_live_with": follow_command(ids),
        # A row per task, started by the session from this reply (#830).
        "rows": _dispatch_rows([
            (task_id, (item.get("label") if isinstance(item, dict) else None) or None, task)
            for task_id, task, item in zip(ids, created, tasks)
        ]),
    }
    if limit_note:
        answer["max_batch_size_unread_because"] = limit_note
    answer.update(_submitted_session_fields())
    answer.update(territory)
    for check in checks:
        _remember_territory(client, check, [(lane["lane"], task_id)
                                            for (_, lane), task_id in zip(units, ids)])
    return json.dumps(answer, indent=2)


def _result_of(client: Any, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    """What `swarm_result` says of one read task: `describe_task`, `failure`
    on a failure, and `outputs`. One function for `swarm_result` and
    `swarm_collect`, so a collected result is a result."""
    described = describe_task(task, client)
    # ONLY ON A FAILURE, and only here. `explain_failure` costs one extra
    # round trip to the attempts route, which is where the exit code, the
    # backend that actually ran and the earlier attempts' errors live --
    # none of them are on the task document. A successful read pays nothing
    # because the function returns None without asking.
    failure = explain_failure(client, task)
    if failure is not None:
        described["failure"] = failure
    # WHAT IT PRODUCED (#143), from the artifacts route. A failed listing
    # is said inside `outputs`, not raised: the task itself was read.
    try:
        listing, listing_error = client.artifacts(task_id), None
    except SwarmError as exc:
        listing, listing_error = None, str(exc)
    # `outputs`, not `produced`: a workflow read already names the whole
    # of `describe_task` `produced`, and one word for two shapes misleads.
    described["outputs"] = outputs_of(task, listing, listing_error=listing_error)
    # A REVIEW'S VERDICT (#2837): MERGE or NOT_YET and how many findings,
    # from the task's own `verdict.json`. No key, and no extra read, when its
    # artifacts hold none (`_verdict_of`).
    shown_verdict = _verdict_of(client, task_id, listing)
    if shown_verdict is not None:
        described["verdict"] = shown_verdict
    # WHAT THE AGENT ASKS THE OWNER (owner decisions, 2026-10-05 and
    # 2026-10-06, observer P15): its questions.json, read back through the
    # redacted artifacts route by `progress.owner_questions` -- the one parse
    # the progress row and the follow outcome use too. `owner_questions` only
    # when it asked any: no key, and no extra read, when the worker counted
    # none -- which is also every file the worker rejected. A counted file
    # that could not be read says so in `questions_unavailable_because`.
    # Data for the reader, never acted on.
    asked = progress.owner_questions(client, task)
    if asked["questions"]:
        described["owner_questions"] = asked["questions"]
    if asked.get("questions_unavailable_because"):
        described["questions_unavailable_because"] = asked["questions_unavailable_because"]
    # WHAT THE OWNER ANSWERED (TASK-ANSWERS, owner decision 2026-10-13): the
    # answers stored on the task in the console, as the task read served them.
    # No key until one is answered, as `owner_questions` has none until asked.
    answered = progress.owner_answers(task)
    if answered:
        described["owner_answers"] = answered
    # WHAT THE WHOLE TASK COST (lane review P1): every attempt's cost and time,
    # as the API served them, the last attempt's beside them, and `cost` --
    # the total in words, `at least` when an attempt recorded none.
    # As served, never an extra read: a successful result pays no round trip.
    totals = progress.spend_totals(client, task, read_attempts=False)
    described.update(totals)
    described["cost"] = progress.cost_words(totals)
    # WHETHER ITS BROWSER WAS USED (BROWSER-USE-FLAG, part of #2322), as the
    # task read served it: no key for a task that is not a browser runtime.
    browser = progress.browser_words(task)
    if browser is not None:
        described["browser"] = browser
    # Where to watch it: the API's link, as served, or no key (`console_link`).
    return with_console(described, task)


#: The file a review or re-review step writes (`agent_worker.expected_outputs.
#: REVIEW_VERDICT_NAME`), and the most of it read: the worker's own bound
#: (`agent_worker.verdict.MAX_VERDICT_BYTES`).
VERDICT_NAME = "verdict.json"
VERDICT_MAX_BYTES = 256 * 1024


def _verdict_of(client: Any, task_id: str, listing: dict[str, Any] | None) -> dict[str, Any] | None:
    """`{verdict, findings, file}` from the task's `verdict.json`, or None
    when its artifacts hold none (#2837).

    WHY. Reported 2026-10-30: a re-review's NOT_YET lived only in its
    handoff, and nothing in `swarm_result` showed it. The worker now makes a
    review step write the file; this shows what it says.

    READ THROUGH THE ARTIFACTS ROUTE, by name, redacted by the API, and only
    when the listing names the file: a task with none pays no read. A file
    that does not read is `{"verdict": None, "unavailable_because": ...}`,
    never a guessed verdict. `findings` counts the file's `findings` as the
    worker does (`verdict.verdict_of_document`): a list's non-blank entries,
    one for a lone non-empty value.
    """
    names = {
        entry.get("name")
        for entry in (listing or {}).get("artifacts") or []
        if isinstance(entry, dict)
    }
    if VERDICT_NAME not in names:
        return None

    def unavailable(why: str) -> dict[str, Any]:
        return {"verdict": None, "file": VERDICT_NAME, "unavailable_because": why}

    try:
        window = client.artifact_content(task_id, VERDICT_NAME, limit_bytes=VERDICT_MAX_BYTES + 1)
    except SwarmError as exc:
        return unavailable(f"the artifact route could not be read: {exc}")
    if window.get("status") != "ok" or not isinstance(window.get("content"), str):
        return unavailable(f"the artifact route says {window.get('status')}")
    if window.get("truncated"):
        return unavailable(f"the file is larger than the {VERDICT_MAX_BYTES} bytes the worker reads")
    try:
        document = json.loads(window["content"])
    except ValueError:
        return unavailable("the file is not JSON")
    if not isinstance(document, dict):
        return unavailable("the file is not a JSON object")
    raw = document.get("verdict")
    verdict = raw.strip().upper() if isinstance(raw, str) else None
    if verdict not in VERDICTS:
        return unavailable(f"its `verdict` is {raw!r}, not {' or '.join(VERDICTS)}")
    findings = document.get("findings")
    if isinstance(findings, list):
        count = sum(1 for item in findings if item not in (None, "", {}, []))
    else:
        count = 0 if findings in (None, "") else 1
    return {"verdict": verdict, "findings": count, "file": VERDICT_NAME}


def _collect(client: Any, task_ids: list[str], wait_seconds: int) -> dict[str, Any]:
    """`swarm_collect`: `swarm_wait`'s loop, then each task's state and result.

    ALWAYS ONE PASS, then wait, for the reason `swarm_wait` gives. A task that
    answers 404 or 403 is not waited for -- it will not appear by waiting --
    and any other read failure is retried until the deadline, then reported
    as not read. An unfinished task is reported with its state and NO
    `result`, so nothing downstream can mistake a running task's partial
    record for what it produced.
    """
    deadline = time.monotonic() + max(0, wait_seconds)
    latest: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    pending = list(dict.fromkeys(task_ids))
    while True:
        for task_id in list(pending):
            try:
                task = client.task(task_id)
            except SwarmError as exc:
                errors[task_id] = str(exc)
                latest.pop(task_id, None)
                if exc.status in (403, 404):
                    pending.remove(task_id)
                continue
            errors.pop(task_id, None)
            latest[task_id] = task
            if task.get("state") in TERMINAL:
                pending.remove(task_id)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        time.sleep(min(5.0, remaining))

    rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = latest.get(task_id)
        if task is None:
            rows.append({
                "task_id": task_id, "state": None, "finished": None,
                "read_error": errors.get(task_id, "not read"),
            })
        elif task.get("state") in TERMINAL:
            rows.append({
                "task_id": task_id, "state": task.get("state"), "finished": True,
                "result": _result_of(client, task_id, task),
            })
        else:
            rows.append({
                "task_id": task_id, "state": task.get("state"), "finished": False,
                "note": "not finished: this is its state, not a result",
            })
    out: dict[str, Any] = {"tasks": rows}
    running = [row["task_id"] for row in rows if row["finished"] is False]
    unread = [row["task_id"] for row in rows if row["finished"] is None]
    if running:
        out["still_running"] = running
        out["note"] = (
            "the wait ended before these tasks finished; nothing here is their result. "
            "Collect them again, or follow them with swarm_follow"
        )
    if unread:
        out["not_read"] = unread
    return out


def _with_console_links(envelope: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """A workflow read's report, with the console links the API served.

    The workflow's own link at the top, and each step's on its row: the
    step's `links.console` from the workflow document, else the one on the
    step task the same read returned. Every one is copied as served and none is
    built here (`follow.console_link`); a step the API served no link for --
    one with no task yet, or a deployment with no console -- gets no key.
    Rows are matched to steps by position, the order `workflows.step_rows`
    keeps, and checked by `step_id` so a mismatch adds nothing rather than
    another step's link.
    """
    workflow = envelope.get("workflow") or {}
    link = console_link(workflow)
    out: dict[str, Any] = {"workflow_id": report.get("workflow_id")}
    if link is not None:
        out["console"] = link
    out.update(report)
    tasks = {
        str(task.get("id")): task
        for task in envelope.get("tasks") or []
        if isinstance(task, dict) and task.get("id")
    }
    for row, step in zip(out.get("steps") or [], workflow.get("steps") or []):
        if not isinstance(step, dict) or row.get("step_id") != step.get("step_id"):
            continue
        served = console_link(step)
        if served is None and step.get("task_id"):
            served = console_link(tasks.get(str(step["task_id"])))
        if served is not None:
            row["console"] = served
    return out


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


#: The account state tools, by the `sc account` verb each one is.
_ACCOUNT_STATE_TOOLS = {
    "swarm_account_pause": "pause",
    "swarm_account_resume": "resume",
    "swarm_account_drain": "drain",
}


#: The arguments each tool takes, read from its own schema so the two cannot
#: disagree.
_ACCEPTED = {
    tool["name"]: frozenset((tool.get("inputSchema") or {}).get("properties") or {})
    for tool in TOOLS
}


#: The largest spec file `swarm_workflow_spec` reads. A workflow spec is a few
#: kilobytes of prompts; a path that names anything much larger is not one, and
#: reading it whole into a tool reply would only fill the relay's context.
MAX_SPEC_FILE_BYTES = 1024 * 1024


def _read_spec_file(path: str, *, base: Path) -> dict[str, Any]:
    """A spec file, checked by `workflows.read_spec`, with its digest (epic #227).

    `/sc:swarmcloud` took only a spec object or JSON text, unlike `swarm workflow`,
    which takes a file; a path passed as its argument failed in `readSpec`. A
    workflow script has no filesystem, so the bridge reads the file here, and
    the script holds the spec relayed back to this digest before it submits.

    NOTHING FROM THE FILE IS REPEATED IN AN ERROR. The path arrives through a
    relay and may name any file this user can read; only a file that parses
    and passes `read_spec` is returned, and every refusal names the path and
    what is wrong, never what the file holds.

    ONLY A FILE UNDER THE CHECKOUT IS READ (epic #227, finding 25). The path
    arrives through a relay, so a reader that took any absolute path, or a
    `../` or a symlink out of the checkout, would read any file this user can
    -- an SSH key, a cloud credential -- and only a refusal that happened to
    hide the content stood between that file and a tool reply. Both the
    checkout and the target are resolved, symlinks included, BEFORE the check,
    and the check comes before anything about the target (whether it exists,
    its size) is looked at, so a refusal says nothing about a file outside.
    """
    root = base.expanduser().resolve()
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    if not target.is_relative_to(root):
        raise SwarmError(
            f"{path} is outside this bridge's checkout, {root}; a workflow spec is read "
            f"only from a file under the checkout ({checkout.CHECKOUT_DIR_ENV}, else the "
            "directory the bridge runs in), after symlinks are resolved. Nothing was read: "
            "move the spec into the checkout, or pass the spec object as `spec`"
        )
    if not target.is_file():
        raise SwarmError(f"{path} is not a file in {root} (looked for {target})")
    size = target.stat().st_size
    if size > MAX_SPEC_FILE_BYTES:
        raise SwarmError(
            f"{target} is {size} bytes; a workflow spec is at most {MAX_SPEC_FILE_BYTES}, "
            "so this is not one"
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        where = f" (line {exc.lineno}, column {exc.colno})" if isinstance(exc, json.JSONDecodeError) else ""
        raise SwarmError(f"{target} is not a JSON workflow spec{where}") from None
    if isinstance(document, dict):
        unknown = sorted(set(document) - workflows.SPEC_KEYS)
        if unknown:
            # COUNTED, NOT NAMED. `read_spec` would name the keys, and a key is
            # the file's content too; this file may be anything the path named.
            raise SwarmError(
                f"{target} carries {len(unknown)} key(s) a workflow spec does not have; "
                f"accepted: {sorted(workflows.SPEC_KEYS)}"
            )
    try:
        catalogue.with_model_classes(
            workflows.read_spec(_spec_without_files(document), where=str(target))["steps"],
            document.get("steps") if isinstance(document, dict) else None,
        )
    except SwarmError as exc:
        raise SwarmError(f"{target} is not a workflow spec: {exc}") from None
    except (TypeError, ValueError):
        # `int("…")` and the like carry the offending value in their message.
        raise SwarmError(f"{target} is not a workflow spec: a field holds a value of the wrong type") from None
    return {"path": str(target), "spec": document, "spec_digest": workflows.spec_digest(document)}


#: Specs `swarm_workflow_spec` read, by `spec_ref`, for `swarm_workflow` to
#: submit without a relay retyping them. In this process only: a bridge that
#: restarted holds none, and says so. Bounded, oldest dropped first -- a
#: session reads a handful of specs, and a ref that aged out is refused, never
#: guessed at.
_HELD_SPECS: dict[str, dict[str, Any]] = {}
MAX_HELD_SPECS = 32


def _hold_spec(spec: dict[str, Any], path: str) -> str:
    ref = "spec_" + secrets.token_hex(8)
    _HELD_SPECS[ref] = {"spec": spec, "path": path}
    while len(_HELD_SPECS) > MAX_HELD_SPECS:
        del _HELD_SPECS[next(iter(_HELD_SPECS))]
    return ref


def _held_spec(ref: str) -> dict[str, Any]:
    held = _HELD_SPECS.get(ref.strip())
    if held is None:
        raise SwarmError(
            f"no spec is held under `spec_ref` {ref!r}: a ref comes from swarm_workflow_spec "
            "in this same bridge process, and a restarted bridge holds none. Read the file "
            "again with swarm_workflow_spec, or pass `spec_path`. Nothing was sent"
        )
    return held["spec"]


def _prompt_first_line(prompt: str) -> str:
    """A prompt's first non-blank line, cut at `_PROMPT_FIRST_LINE_CHARS`."""
    for line in prompt.splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= _PROMPT_FIRST_LINE_CHARS else line[: _PROMPT_FIRST_LINE_CHARS - 1] + "…"
    return ""


def _workflow_spec_tool(args: dict[str, Any]) -> dict[str, Any]:
    """`swarm_workflow_spec`: a spec file or a launch's handle, read and checked,
    answered as its OUTLINE, digest and ref -- never every prompt
    (LAUNCH-LARGE-SPEC, #2875).

    Measured 2026-10-31 (product-oracle-2f): a 24-step spec with ~90 KB of
    prompts answered 86,506 characters here, over Claude Code's tool-output
    limit, and the /sc:swarmcloud run that asked ended NOT_SUBMITTED. The
    bridge holds the spec under `spec_ref`; the reply names each step's prompt
    by its size and first line, and one step's prompt is read only when
    `step_id` asks for it, a window of at most `_PROMPT_WINDOW_MAX`
    characters at a time.

    A HANDLE IS READ WITH ITS DIGEST, AND CHECKED HERE. `spec_handle` is the
    file swarm_workflow_launch wrote (`launch.handle_target`: its run folders
    only), and the digest it wrote beside it must match the bytes read now, so
    a file changed since the launch is refused before any ref is handed out.
    `spec_digest` beside a `path` is checked the same way."""
    sources = sorted(k for k in ("path", "spec_handle") if args.get(k) not in (None, ""))
    if len(sources) != 1:
        raise SwarmError(
            f"swarm_workflow_spec takes exactly one of `path` or `spec_handle`; it was given "
            f"{sources or 'none'}. Nothing was read"
        )
    expected = args.get("spec_digest")
    expected = expected.strip() if isinstance(expected, str) else None
    if args.get("spec_handle") not in (None, ""):
        if not expected:
            raise SwarmError(
                "`spec_handle` is read only with the `spec_digest` the launch wrote beside it "
                "(`spec_handle.digest`): pass both, exactly as given. Nothing was read"
            )
        from . import launch

        target = launch.handle_target(str(args["spec_handle"]))
        read = _read_spec_file(str(target), base=target.parent)
    else:
        read = _read_spec_file(str(args["path"]), base=checkout.directory())
    if expected and read["spec_digest"] != expected:
        raise SwarmError(
            f"{read['path']} has digest {read['spec_digest']}, and the digest given is {expected}: "
            "the file changed after its digest was taken, so no ref was handed out. Launch the "
            "spec again. Nothing was submitted"
        )
    spec = read["spec"]
    outline = _outline(spec)
    prompts: dict[str, str] = {}
    for step in spec.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("step_id"), str):
            prompt = step.get("prompt")
            prompts[step["step_id"]] = prompt if isinstance(prompt, str) else ""
    for entry in outline["steps"]:
        prompt = prompts.get(entry["step_id"], "")
        entry["prompt_chars"] = len(prompt)
        entry["prompt_first_line"] = _prompt_first_line(prompt)
    out: dict[str, Any] = {
        "path": read["path"],
        "spec_digest": read["spec_digest"],
        "spec_ref": _hold_spec(spec, read["path"]),
        "outline": outline,
        "prompts": (
            "not in this reply: pass `step_id` (with `offset` and `limit`) for one step's "
            "prompt, a window at a time"
        ),
    }
    if args.get("step_id") not in (None, ""):
        step_id = str(args["step_id"]).strip()
        if step_id not in prompts:
            raise SwarmError(
                f"swarm_workflow_spec: the spec has no step {step_id!r}; its steps are listed in "
                "`outline.steps`"
            )
        prompt = prompts[step_id]
        offset = max(0, _int_arg(args, "offset", 0))
        limit = min(_PROMPT_WINDOW_MAX, max(1, _int_arg(args, "limit", _PROMPT_WINDOW_DEFAULT)))
        end = min(len(prompt), offset + limit)
        out["prompt"] = {
            "step_id": step_id,
            "offset": offset,
            "text": prompt[offset:end],
            "chars": len(prompt),
            "next_offset": end if end < len(prompt) else None,
        }
    return out


def _outline(spec: dict[str, Any]) -> dict[str, Any]:
    """What a relay copies back instead of a spec: its title, its label and its DAG.

    Plus what decides the merge steps swarm-api appends to it (2026-10-11):
    the spec's `strategy` and `merge`, and each step's `runner_profile`, its
    verdict gate (`when_step`) and its `input_from`. `/sc:swarmcloud`'s
    run.js (`appendable`) derives from them the `re-review` and `merge` steps
    the submit reply may name beyond the spec, and refuses any other.
    """
    steps = []
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        stage = step.get("stage")
        when = step.get("when")
        when_step = when.get("step") if isinstance(when, dict) else None
        input_from = step.get("input_from")
        profile = step.get("runner_profile")
        steps.append({
            "step_id": step.get("step_id"),
            "depends_on": list(step.get("depends_on") or []),
            "stage": stage.strip() if isinstance(stage, str) and stage.strip() else None,
            "runner_profile": profile if isinstance(profile, str) and profile else None,
            "when_step": when_step if isinstance(when_step, str) and when_step else None,
            "input_from": (
                {str(k): str(v) for k, v in input_from.items()}
                if isinstance(input_from, dict) else {}
            ),
        })
    label = spec.get("label")
    title = spec.get("title")
    strategy = spec.get("strategy")
    merge = spec.get("merge")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else None,
        "label": label if isinstance(label, str) and label.strip() else None,
        "steps": steps,
        "strategy": strategy if isinstance(strategy, str) and strategy else None,
        "merge": merge if isinstance(merge, str) and merge else None,
    }


def bridge_version() -> str | None:
    """This bridge's own package version, or None when it is not installed as one."""
    try:
        return importlib.metadata.version("swarm-mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


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


_RUN_TOOLS = frozenset(
    {"swarm_run_issue", "swarm_runs", "swarm_run", "swarm_plan_approve", "swarm_plan_edit",
     "swarm_plan_reject"}
)


def _text_arg(args: dict[str, Any], name: str, tool: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SwarmError(f"{tool} needs `{name}` as a non-empty string. Nothing was sent")
    return value.strip()


def _run_tool(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    """The issue-run tools (#454): the calls `sc run` and `sc plan` make.

    Every refusal is a `SwarmError` worded by `client.run_refusal` and answered
    through `_tool_error_text`, like every other tool's. Nothing here reads the
    digest it approves with: `swarm_plan_approve` takes it from the caller,
    who was shown it.
    """
    if name == "swarm_run_issue":
        approval = args.get("plan_approval") or "required"
        if approval not in ("required", "auto"):
            raise SwarmError("plan_approval is `required` or `auto`. Nothing was sent")
        fix_rounds = args.get("fix_rounds")
        if fix_rounds is not None and (isinstance(fix_rounds, bool) or not isinstance(fix_rounds, int)):
            raise SwarmError("fix_rounds is an integer. Nothing was sent")
        # The planner runs `claude-code` (`issueruns.PLANNER_PROFILE`), so its
        # class is checked against that profile before the run is created.
        model_class = catalogue.check_model_class("claude-code", args.get("model_class"), where=name)
        run = client.create_run(
            issue=_text_arg(args, "issue", name),
            plan_approval=approval,
            auto_merge=_flag(args, "auto_merge"),
            fix_rounds=fix_rounds,
            **({"model_class": model_class} if model_class is not None else {}),
        )
        return json.dumps(_run_summary(client, run), indent=2, default=str)
    if name == "swarm_runs":
        limit = args.get("limit")
        listing = client.runs(
            limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            page_token=args.get("page_token") or None,
        )
        return json.dumps(listing, indent=2, default=str)
    run_id = _text_arg(args, "run_id", name)
    if name == "swarm_run":
        run = client.run(run_id)
    elif name == "swarm_plan_approve":
        run = client.approve_plan(run_id, plan_digest=_text_arg(args, "plan_digest", name))
    elif name == "swarm_plan_edit":
        plan = args.get("plan")
        if not isinstance(plan, dict):
            raise SwarmError("swarm_plan_edit needs `plan` as an object. Nothing was sent")
        run = client.edit_plan(run_id, plan_digest=_text_arg(args, "plan_digest", name), plan=plan)
    else:
        run = client.reject_plan(
            run_id,
            plan_digest=args.get("plan_digest") or None,
            reason=_text_arg(args, "reason", name),
        )
    return json.dumps(_run_summary(client, run), indent=2, default=str)


def _run_summary(client: SwarmClient, run: dict[str, Any]) -> dict[str, Any]:
    """`runs.summary`, and `arm_watch` (SC-AUTO-WATCH) once the run's workflow
    exists -- the same moment it carries `attach_with`."""
    from . import runs, watch

    out = runs.summary(client, run)
    # The session the run was tagged with (SC-SESSION-SCOPE), as swarm-api
    # stored it, and why not when a deployment refused the tag.
    out["claude_session"] = run.get("claude_session")
    if run.get("session_tag"):
        out["session_tag"] = run["session_tag"]
    out.update(watch.arm_fields(bool(out.get("attach_with"))))
    return out


_SCHEDULE_TOOLS = frozenset(
    {"swarm_schedule_types", "swarm_schedules", "swarm_schedule_create", "swarm_schedule_update",
     "swarm_schedule_pause", "swarm_schedule_resume", "swarm_schedule_run_now", "swarm_approvals",
     "swarm_schedule_approve", "swarm_schedule_reject"}
)

#: The edit fields `swarm_schedule_update` passes through as given.
_SCHEDULE_EDIT_FIELDS = ("name", "cron", "timezone", "params", "gate", "budget", "policy")


def _schedule_tool(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    """The schedule and approval tools (docs/schedules.md §7.2).

    Refusals are `SwarmError`s worded by `schedules.refusal` and answered
    through `_tool_error_text`. Nothing here reads the digest it approves
    with: `swarm_schedule_approve` takes it from the caller, who was shown it.
    """
    from . import schedules

    def dumps(value: Any) -> str:
        return json.dumps(value, indent=2, default=str)

    if name == "swarm_schedule_types":
        return dumps(schedules.types(client))
    if name == "swarm_approvals":
        listing = schedules.inbox(client, args.get("kind") or None)
        step = schedules.next_step(listing)
        return dumps({**listing, "next": step} if step else listing)
    if name == "swarm_schedule_approve":
        return dumps(schedules.approve(client, _text_arg(args, "approval_id", name),
                                       digest=args.get("digest"), confirm=args.get("confirm") or None))
    if name == "swarm_schedule_reject":
        return dumps(schedules.reject(client, _text_arg(args, "approval_id", name),
                                      reason=_text_arg(args, "reason", name)))
    if name == "swarm_schedule_create":
        body: dict[str, Any] = {
            "name": _text_arg(args, "name", name),
            "type": _text_arg(args, "type", name),
            "cron": _text_arg(args, "cron", name),
            "timezone": args.get("timezone") or "UTC",
            "state": args.get("state") or "enabled",
        }
        for key in ("params", "gate", "budget", "policy"):
            if args.get(key) is not None:
                if not isinstance(args[key], dict):
                    raise SwarmError(f"{name} takes `{key}` as an object. Nothing was sent")
                body[key] = args[key]
        if _flag(args, "dry_run"):
            body["policy"] = {**(body.get("policy") or {}), "dry_run": True}
        # Refused before `repos` is resolved, so a refusal reads nothing either.
        schedules.refuse_backend_fields(body, name)
        body["scope"] = schedules.scope(client, args.get("scope"), args.get("repos"))
        return dumps(schedules.create(client, body))
    if name == "swarm_schedules" and not args.get("id"):
        return dumps(schedules.listing(client))
    sid = schedules.resolve(client, args.get("id"))
    if name == "swarm_schedules":
        return dumps(schedules.schedule(client, sid))
    if name == "swarm_schedule_update":
        revision = args.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise SwarmError(f"{name} needs the `revision` it read, as an integer. Nothing was sent")
        edit: dict[str, Any] = {"revision": revision}
        edit.update({k: args[k] for k in _SCHEDULE_EDIT_FIELDS if args.get(k) is not None})
        if args.get("scope") is not None or args.get("repos") is not None:
            edit["scope"] = schedules.scope(client, args.get("scope"), args.get("repos"))
        return dumps(schedules.update(client, sid, edit))
    if name == "swarm_schedule_pause":
        return dumps(schedules.pause(client, sid, args.get("reason") or None))
    if name == "swarm_schedule_resume":
        return dumps(schedules.resume(client, sid))
    return dumps(schedules.run_now(client, sid, dry_run=_flag(args, "dry_run")))


_SETUP_TOOLS = frozenset(
    {"swarm_setup_status", "swarm_setup_workspace", "swarm_setup_connect", "swarm_setup_orgs",
     "swarm_setup_repos", "swarm_setup_grant", "swarm_setup_revoke", "swarm_setup_verify",
     "swarm_access"}
)

#: The longest `swarm_setup_status` holds for GitHub: the authorize link's life.
MAX_SETUP_WAIT_SECONDS = 600


def _setup_view(view: dict[str, Any]) -> dict[str, Any]:
    """The checklist for a tool reply: the text, and each step without its
    evidence -- which names secrets and records the model has no use for --
    bar who GitHub is connected as."""
    from .sc import connected_as_you, setup_checklist

    steps = [
        {key: step.get(key) for key in ("step", "state", "code", "copy", "issues")}
        for step in view.get("steps") or [] if isinstance(step, dict)
    ]
    return {
        "checklist": "\n".join(setup_checklist(view)),
        "next_step": view.get("next_step"),
        "complete": view.get("next_step") is None,
        "connected_as_you": connected_as_you(view),
        "steps": steps,
    }


def _setup_tool(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    """The onboarding tools (#780, OB9): the calls `sc setup` and `sc access`
    make, through the same functions, so the terminal and /sc:setup cannot
    disagree about a step. A §2.3 refusal answers with its code and recovery
    copy, word for word, as `sc` prints it."""
    from . import sc

    def reply(value: Any) -> str:
        return json.dumps(value, indent=2, default=str)

    try:
        if name == "swarm_setup_status":
            wait = _int_arg(args, "wait_for_github_seconds", 0)
            if wait < 0 or wait > MAX_SETUP_WAIT_SECONDS:
                raise SwarmError(
                    f"wait_for_github_seconds is 0-{MAX_SETUP_WAIT_SECONDS}. Nothing was read")
            if wait:
                login, view = sc.wait_for_github(client, timeout=wait)
                answer = _setup_view(view)
                if login is None:
                    answer["not_connected"] = sc.not_connected_text(wait)
                return reply(answer)
            return reply(_setup_view(sc.setup_status(client)))
        if name == "swarm_setup_workspace":
            # ONE POST AT MOST, THEN ONE READ, AND NO WAIT (invariant 4's
            # spirit): an approval is a person's, minutes or days away, and a
            # tool call held open for it holds the session. The record says
            # where it stands; /sc:setup reads it again.
            action = args.get("action") or "status"
            if action not in ("status", "request", "loan"):
                raise SwarmError(
                    f"action is status, request or loan, not {action!r}. Nothing was sent")
            answer: dict[str, Any] = {"action": action}
            if action == "request":
                sc.request_workspace(client)
            elif action == "loan":
                answer["loan_request"] = sc.request_loan(client)
            record = sc.workspace_status(client)
            return reply({**answer, "workspace": record, "text": sc.workspace_line(record),
                          "setup_command": sc.PLUGIN_SETUP_COMMAND})
        if name == "swarm_setup_connect":
            already = sc.connected_as_you(sc.setup_status(client))
            if already is not None:
                return reply({"already_connected_as": already,
                              "next": "swarm_setup_orgs: enable the owners to choose from"})
            started = sc.start_github_connect(client)
            opened = bool(args.get("open_browser", True)) and sc._open_browser(  # noqa: SLF001
                started["authorize_url"])
            return reply({**started, "opened": opened,
                          "next": "swarm_setup_status with wait_for_github_seconds, while "
                                  "the person approves in the browser"})
        if name == "swarm_setup_orgs":
            owner = args.get("enable")
            asked = args.get("request_install")
            enabled = requested = None
            if asked is not None:
                done = sc.request_install(client, _text_arg(args, "request_install", name))
                requested = done.get("org") if isinstance(done, dict) else None
            if owner is not None:
                done = sc.enable_owner(client, _text_arg(args, "enable", name))
                enabled = done.get("org") if isinstance(done, dict) else None
            listing = sc.access_owners(client)
            answer = {"owners": listing.get("owners"), "install_url": listing.get("install_url"),
                      "orgs_listed": listing.get("orgs_listed"),
                      "approval_pending": sc.approval_pending(listing)}
            if owner is not None:
                answer["enabled"] = enabled
            if asked is not None:
                answer["install_requested"] = requested
            return reply(answer)
        if name == "swarm_setup_repos":
            page = _int_arg(args, "page", 1)
            listing = sc.repositories_page(client, _text_arg(args, "owner", name), page=page,
                                           query=(args.get("q") or None))
            return reply({key: value for key, value in listing.items() if key != "tenant_id"})
        if name == "swarm_setup_grant":
            mode = _text_arg(args, "mode", name)
            return reply(sc.grant_repository(client, _text_arg(args, "repository", name), mode))
        if name == "swarm_setup_revoke":
            return reply(sc.revoke_repository(client, _text_arg(args, "repository", name)))
        if name == "swarm_setup_verify":
            wanted = args.get("repositories") or []
            if not isinstance(wanted, list) or not all(isinstance(r, str) for r in wanted):
                raise SwarmError("repositories is a list of owner/repo. Nothing was verified")
            push_test = args.get("push_test", False)
            if not isinstance(push_test, bool):
                raise SwarmError("push_test is true or false. Nothing was verified")
            if push_test and not wanted:
                raise SwarmError("push_test needs `repositories`, each named by the person who "
                                 "said yes to it. Nothing was verified")
            results = sc.verify_repositories(client, wanted, push_test=push_test)
            return reply({"results": results, "text": "\n".join(sc.verify_lines(results)),
                          "passed": bool(results) and all(r.get("passed") for r in results)})
        overview = sc.access_overview(client)
        return reply({**overview, "text": "\n".join(sc.access_lines(overview))})
    except SwarmError as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("failure_code"):
            raise sc._refused(exc) from None  # noqa: SLF001
        if name == "swarm_setup_workspace" and exc.code:
            # The workspace routes' refusals carry an upper-case code and a
            # sentence for the person (`WORKSPACE_REQUEST_TOO_SOON`,
            # `WORKSPACE_NOT_REQUESTED`, ...): both, word for word.
            raise SwarmError(sc.api_refusal(exc), status=exc.status, edge=exc.edge,
                             code=exc.code, detail=exc.detail) from None
        raise


def _remember_person_waits(reply: dict[str, Any]) -> None:
    """Each task this reply ended a row for because a person must act on it
    (#2441), remembered so `/sc check` attaches a new row once it resumes
    (`launch.resumed_person_waits`)."""
    from . import launch

    for row in reply.get("tasks") or []:
        wait = row.get("waiting_on_person") if isinstance(row, dict) else None
        if isinstance(wait, dict) and row.get("task_id"):
            launch.remember_person_wait(str(row["task_id"]), wait.get("workflow_id") or None,
                                        wait.get("step_id") or None)


def _follow_rows(
    client: SwarmClient,
    args: dict[str, Any],
    expected_step: Any,
    parents: list[str] | None,
    *,
    keepalive: bool,
) -> dict[str, Any]:
    """A `lines`, `progress` or `summary` follow's reply, read from
    `args["since"]` -- a full token by now: `_call` has already resolved any
    handle."""
    if args.get("format") == "stage":
        # The sc:finish row's view (SC-BROWSER-BUILD, #2318): held until the
        # workflow's stage changes, so the pane layout's one row per workflow
        # spends a turn per stage, not per step.
        return compact.watch_stage(
            client,
            list(args["task_ids"]),
            args.get("stage_of"),
            since=args.get("since"),
            wait_seconds=_int_arg(args, "wait_seconds", 0),
            max_wait=compact.MAX_WAIT_SECONDS if keepalive else compact.SILENT_MAX_WAIT_SECONDS,
        )
    if args.get("format") == "summary":
        # The sc:summary row's view (PLUGIN-ROW-SPLIT-2, owner 2026-10-21):
        # `progress`'s hold over the unfinished tasks, and the count line
        # the bridge builds in run.js's buckets, which the row copies.
        return compact.watch_summary(
            client,
            list(args["task_ids"]),
            args.get("summary_of"),
            since=args.get("since"),
            wait_seconds=_int_arg(args, "wait_seconds", 0),
            max_wait=compact.MAX_WAIT_SECONDS if keepalive else compact.SILENT_MAX_WAIT_SECONDS,
        )
    if args.get("format") == "progress":
        # The slim row view (`compact.watch_progress`, owner decision
        # 2026-10-01): no log, one line per task when it changed, compact
        # JSON -- every byte here is re-read on each of the row's turns.
        return compact.watch_progress(
            client,
            list(args["task_ids"]),
            since=args.get("since"),
            wait_seconds=_int_arg(args, "wait_seconds", 0),
            step_id=expected_step.strip() if isinstance(expected_step, str) else None,
            parents=[p.strip() for p in parents] if parents else None,
            max_wait=compact.MAX_WAIT_SECONDS if keepalive else compact.SILENT_MAX_WAIT_SECONDS,
        )
    # The row view (`progress.watch`): narrated lines, one opaque token, a
    # call that may gather for a window, and a finished task's outcome.
    return progress.watch(
        client,
        list(args["task_ids"]),
        since=args.get("since"),
        wait_seconds=_int_arg(args, "wait_seconds", 0),
        max_log_bytes=_int_arg(args, "max_log_bytes", progress.DEFAULT_LINES_LOG_BUDGET),
        max_new_events=_int_arg(args, "max_new_events", DEFAULT_EVENT_PAGE),
        max_lines=_int_arg(args, "max_lines", progress.DEFAULT_MAX_LINES),
        include_heartbeats=_flag(args, "include_heartbeats"),
        step_id=expected_step.strip() if isinstance(expected_step, str) else None,
    )


def _unquoted(value: str) -> str:
    """`value` without surrounding whitespace and ONE layer of MATCHING
    quotes -- what a row agent that JSON-encoded an argument twice added
    (#1865, #2379). A lone or unmatched quote stays, so the value is refused
    or compared as received."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


#: What `merge_pr` takes beside it: the repository and the run's name. Every
#: other `swarm_workflow` argument would add work to, or move, a pull request
#: that is already written, and swarm-api would refuse it anyway.
_MERGE_PR_BESIDE = ("repo", "title", "target", "merge_pr")


def _workflow_merge_pr(client: SwarmClient, args: dict[str, Any]) -> str:
    """`swarm_workflow` with `merge_pr` (#352): one merge step for an existing PR."""
    beside = sorted(
        k for k, v in args.items()
        if k not in _MERGE_PR_BESIDE and v is not None and v is not False
    )
    if beside:
        raise SwarmError(
            f"swarm_workflow was given `merge_pr` AND {beside}; `merge_pr` submits the one "
            "merge step for an existing pull request and takes only `repo` and `title` "
            "beside it. Nothing was sent"
        )
    merge_pr = args["merge_pr"]
    if not isinstance(merge_pr, dict) or set(merge_pr) != {"number", "head_sha"}:
        raise SwarmError(
            "`merge_pr` is {\"number\": <the pull request's number>, \"head_sha\": <its "
            "full head sha>} and nothing else. Nothing was sent"
        )
    number = merge_pr["number"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise SwarmError("`merge_pr.number` is the pull request's number, a positive integer. "
                         "Nothing was sent")
    placed = _placement("swarm_workflow", args, profiles=["merge"])
    repo = args.get("repo") or None
    envelope = workflows.submit_merge_pr(
        client, number=number, head_sha=str(merge_pr["head_sha"]),
        repository_url=repo,
        title=workflows.check_title(args.get("title"), where="swarm_workflow"),
    )
    return _workflow_created(
        envelope, placed=placed,
        repository={"url": repo, "ref": None,
                    "source": "named" if repo else "the tenant's only registered repository"},
        name=args.get("title"),
    )


def _workflow_created(
    envelope: dict[str, Any], *, placed: dict[str, Any], repository: dict[str, Any],
    digest: str | None = None, expected_digest: Any = None,
    given: list[str] | None = None, name: str | None = None,
    publish_to_pr: int | str | None = None,
) -> str:
    """`swarm_workflow`'s answer to an accepted submission, spec or `merge_pr`.

    With `publish_to_pr` (#2183) the answer says what was sent and that no
    merge was: the caller who wrote `merge: "off"` reads why it is not in the
    request, and nobody waits for a merge step that will never be appended.

    `given` is the step ids this bridge submitted. Each reply step then says
    whether SwarmCloud ADDED it (`added`): swarm-api appends a re-review and a
    merge the spec never named (`swarm_api.service._with_merge_step`), and
    /sc:swarmcloud starts a row for every step the server marks so, instead of
    predicting the server's merge rules and refusing the reply when the
    prediction missed (SPEED-D5, measured 2026-10-14 on plugin sc 0.5.27: a
    3-step spec came back with 5 and no row started). The comparison is made
    here because this is the one place that holds both the spec submitted and
    the reply; no relay retyped either.

    THE PROJECT RECORD (SC-TITLES-B, part of #1511): the submission is
    appended to the project's `launched.jsonl` HERE, by the id SwarmCloud
    just named, so `session.recorded` matches it by id and two of this
    session's workflows with one `name` are told apart. `name` is the spec's
    title, else its label; without one, the run is named by its id
    (`workflows.title_name`).
    """
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
        with_console(
            {
                "step_id": step.get("step_id"),
                "task_id": step.get("task_id"),
                "runner_profile": step.get("runner_profile"),
                "depends_on": step.get("depends_on") or [],
                "input_from": step.get("input_from") or {},
                **({} if given is None else {"added": step.get("step_id") not in given}),
            },
            step,
        )
        for step in workflow.get("steps") or []
    ]
    named = workflows.title_name(title=name, workflow_id=workflow_id)
    try:
        from . import launch

        launch.record(
            "submit", title=workflows.workflow_title(named, len(given) if given is not None else len(steps)),
            workflow_id=str(workflow_id), repository=repository.get("url"), name=named,
        )
    except Exception:  # noqa: BLE001 - the workflow is in SwarmCloud; a lost record line never fails its answer
        pass
    created: dict[str, Any] = with_console({"workflow_id": workflow_id}, workflow)
    created.update({
        # The workspace it runs in, from the API's document (SC-ROWS): the
        # label of every row /sc:swarmcloud starts for it.
        "workspace": workflows.workspace_of(workflow, fallback=envelope.get("tenant_id")),
        "steps": steps,
        "dispatch": envelope.get("dispatch"),
        "repository": repository,
        # NO STATE HERE, deliberately. The create response is the one read
        # that honestly says `state_source: "stored"`: the step tasks were
        # written microseconds ago and deriving over them would spend a read
        # per step to be told what this very request just decided. Echoing
        # the stored QUEUED would look like an answer.
        "state_available_from": (
            "swarm_workflow_status -- a create response does not derive a "
            "workflow state and this tool will not quote the stored one"
        ),
        "target": placed,
        # Which bridge answered: /sc:swarmcloud names it when this
        # session's bridge refuses the follow its rows make.
        "bridge_version": bridge_version(),
    })
    if publish_to_pr is not None:
        created["publish_to_pr"] = {
            "pull_request": publish_to_pr,
            "merge": (
                "no merge sent: the lane pushes onto this existing pull request and "
                "opens none, so no merge step is appended and nothing merges it"
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
    # A workflow now exists for this session to follow (SC-AUTO-WATCH).
    created.update(_submitted_session_fields())
    return json.dumps(created, indent=2)


def _answered(
    client: SwarmClient, name: str, args: dict[str, Any], *, keepalive: bool = False
) -> str | artifacts.ImageAnswer:
    """`_call`'s answer, after which the build board is told what happened
    (BOARD-WIRE, part of #1773): a submission, a launch or a check refreshes
    it, a follow that saw a state change refreshes it at most every two
    minutes (`buildboard.AutoRefresh`). The refresh runs in the background,
    so the answer is never delayed by it, and nothing it raises reaches the
    caller: the answer returned is `_call`'s, byte for byte. A call that
    raised refreshes nothing -- a refused submission sent nothing.

    The trigger sits here, around `_call`, rather than inside it: every tool
    test drives `_call` against a fake control plane, and a refresh there
    would write a board into whatever checkout the suite runs in."""
    text = _call(client, name, args, keepalive=keepalive)
    try:
        from . import buildboard

        buildboard.after_tool(client, name, text if isinstance(text, str) else text.text)
    except Exception as exc:  # noqa: BLE001 - the board never fails the tool behind it
        print(f"swarm-mcp: build board: the refresh was not started ({type(exc).__name__}: {exc})",
              file=sys.stderr)
    return text


def _call(
    client: SwarmClient, name: str, args: dict[str, Any], *, keepalive: bool = False
) -> str | artifacts.ImageAnswer:
    """One tool call's answer. `keepalive` says the caller is sending the host
    progress notifications for as long as this runs (`_Keepalive`), so a
    progress hold may take its full `compact.MAX_WAIT_SECONDS`; without it the
    hold stops at `compact.SILENT_MAX_WAIT_SECONDS`, inside the host's idle
    timeout. The answer is text, except `swarm_artifact`'s image, which is an
    `artifacts.ImageAnswer` that `serve` sends as text and image blocks."""
    _refuse_unknown_arguments(name, args)
    if name == "swarm_dispatch" and args.get("tasks") is not None:
        # Before any request, `/v1/stats` included: neither the call nor a
        # task of it may name a pull request to publish onto (#2183).
        if args.get("publish_to_pr") is not None:
            raise SwarmError(f"swarm_dispatch was given `tasks` AND `publish_to_pr`. "
                             f"{_BATCH_PUBLISH_REFUSAL}")
        for index, item in enumerate(args["tasks"] if isinstance(args["tasks"], list) else []):
            if isinstance(item, dict) and "publish_to_pr" in item:
                raise SwarmError(f"swarm_dispatch tasks[{index}] names `publish_to_pr`. "
                                 f"{_BATCH_PUBLISH_REFUSAL}")
        beside = sorted(k for k in _SINGLE_TASK_ARGS if args.get(k) is not None)
        if beside:
            raise SwarmError(
                f"swarm_dispatch was given `tasks` AND {beside}; each task in `tasks` "
                "carries those itself. Pass one task's fields, or `tasks` -- not both. "
                "Nothing was sent"
            )
        units = args["tasks"] if isinstance(args["tasks"], list) else []
        placed = _placement(
            name, args,
            profiles=[
                str((u.get("runner_profile") if isinstance(u, dict) else None) or "claude-code")
                for u in units
            ],
        )
        return _dispatch_batch(client, args, placed)

    if name == "swarm_dispatch":
        if not isinstance(args.get("prompt"), str):
            raise SwarmError("swarm_dispatch needs `prompt`, or `tasks` for several. Nothing was sent")
        # WHERE IT GOES, before anything else is read: `local`, or `hybrid`
        # with a declared local need, sends nothing at all (S8).
        placed = _placement(name, args, profiles=[str(args.get("profile") or "claude-code")])
        # CHECKED BEFORE THE ROUND TRIP. The API refuses an unknown or disabled
        # profile too and stays the authority; this only refuses sooner, with
        # the catalogue's own reason, so a session that typed `claude` for
        # `claude-code` is told which names exist instead of reading a 4xx. See
        # `profiles.check` for why a local check here is not a second opinion.
        profile = catalogue.check(args.get("profile") or "claude-code", where="swarm_dispatch")
        # The inputs this profile DECLARES, and nothing else, refused by name
        # before anything travels (#142).
        inputs = catalogue.check_inputs(profile, args.get("inputs"), where="swarm_dispatch")
        credentials = check_mcp_credentials(
            args.get("mcp_credentials"), (inputs or {}).get("mcp_servers"), where="swarm_dispatch"
        )
        strategy = _dispatch_strategy(args.get("strategy"))
        # WHAT IS READ BESIDE IT: names, checked for shape and sent as given. A
        # URL is refused here, before any request (#1434).
        additional = check_additional_repositories(
            args.get("additional_repositories"), where="swarm_dispatch")
        # THE MODEL CLASS, by name, refused here when the profile cannot run
        # it (#1435). The API stays the authority, and its 422 -- a gate not
        # open yet, say -- comes back as it was answered.
        model_class = catalogue.check_model_class(
            profile, args.get("model_class"), where="swarm_dispatch")
        # THE PULL REQUEST it publishes onto (#2183), in every form
        # `parse_pull_request` reads and sent in the form swarm_workflow sends.
        # Refused here, before any request, when it names none; whether this
        # tenant may push onto it is the API's to say.
        publish_to_pr = workflows.check_publish_to_pr(args.get("publish_to_pr"), where="swarm_dispatch")
        # WHAT IS CLONED, decided before anything travels: the caller's repo,
        # or -- only with `infer: true` -- the checkout this bridge runs in,
        # refused when its branch is not pushed, because a remote agent on an
        # older tip succeeds on the wrong base (see `checkout`). Neither given
        # and `infer` not set: nothing is cloned, exactly as before this PR.
        repository = checkout.resolve(
            repo=args.get("repo"),
            ref=args.get("ref"),
            infer=_flag(args, "infer"),
        )
        signature = _dispatch_signature(profile, args["prompt"], strategy, repository, inputs, additional,
                                        model_class, publish_to_pr)
        _refuse_a_repeat_dispatch(client, signature)
        if strategy == "direct-pr" and not repository.url:
            raise SwarmError(
                "strategy 'direct-pr' pushes the agent's branch and opens a pull "
                "request, so it needs a repository, and this dispatch has none: "
                + "; ".join(repository.notes)
            )
        # THE TERRITORY of the files the task declares: read before the send,
        # reported after it, and never a reason not to send.
        lane = {"lane": args.get("label") or "this task",
                "files": _declared_files(args.get("files"), "swarm_dispatch")}
        territory, checks = _territory_of_units(client, [(repository.url, lane)])
        task = without_credentials(_send_task(
            client,
            credentials,
            prompt=args["prompt"],
            runner_profile=profile,
            repository_url=repository.url,
            repository_ref=repository.ref,
            metadata={"unit": args["label"]} if args.get("label") else None,
            inputs=inputs or None,
            strategy=strategy,
            # Only when named, so a dispatch naming none calls exactly as before.
            **({"additional_repositories": additional} if additional is not None else {}),
            **({"model_class": model_class} if model_class is not None else {}),
            **({"publish_to_pr": publish_to_pr} if publish_to_pr is not None else {}),
        ))
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
        # Recorded only now that the dispatch actually reached the API: a
        # dispatch that raised above (a bad profile, a refused repository, a
        # transport failure) never marks its signature, so retrying it after
        # fixing the problem is never refused as a repeat.
        _remember_dispatch(client, signature)
        reply = with_console({
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
                # Which target applied, and which layer chose it (S8).
                "target": placed,
            }, task)
        if publish_to_pr is not None:
            # What was sent, so the caller reads which pull request the task
            # publishes onto in the form the API received.
            reply["publish_to_pr"] = {"pull_request": publish_to_pr}
        # Its live row, started by the session from this reply (#830).
        reply["rows"] = _dispatch_rows([(task_id, args.get("label") or None, task)])
        reply.update(_submitted_session_fields())
        reply.update(territory)
        for check in checks:
            _remember_territory(client, check, [(lane["lane"], task_id)])
        return json.dumps(reply, indent=2)

    format_received = args.get("format") if name == "swarm_follow" else None
    if name == "swarm_follow" and isinstance(format_received, str):
        # Normalised ONCE, here, and written back, so the guard below, the
        # `step_id`/`parents` checks, the follower and the reply's shape all
        # read one value (#1865): a row agent that JSON-encoded the argument
        # twice sent '"progress"', the guard refused it, and the row -- told
        # to stop on a format error -- ended UNKNOWN while its task finished.
        # Surrounding whitespace and ONE layer of MATCHING quotes go; a lone
        # quote or a still-unknown word is refused below, as received.
        fmt = _unquoted(format_received)
        args = {**args, "format": fmt or None}
    if name == "swarm_follow":
        # `step_id` and each `parents` entry the same way, and for the same
        # reason (#2379 comment 20): a row agent sent its step id as
        # '"review"', the step check compared 'review' to it, and the row
        # ended UNKNOWN on a step that succeeded. Written back before the
        # checks below, so an id that is only quotes is refused as empty.
        if isinstance(args.get("step_id"), str):
            args = {**args, "step_id": _unquoted(args["step_id"])}
        if isinstance(args.get("parents"), list):
            args = {**args, "parents": [_unquoted(p) if isinstance(p, str) else p for p in args["parents"]]}
    if name == "swarm_follow" and (args.get("format") or "json") not in ("json", "lines", "progress", "summary", "stage"):
        raise SwarmError(
            f"unknown format {format_received!r}; "
            "swarm_follow takes json, lines, progress, summary or stage"
        )

    if name == "swarm_follow" and args.get("since") not in (None, "") and args.get("cursor"):
        raise SwarmError(
            "pass `since` or `cursor`, not both: they are the same position in two "
            "shapes, and two positions for one task is a request to read from two places"
        )

    expected_step = args.get("step_id") if name == "swarm_follow" else None
    if expected_step is not None and (not isinstance(expected_step, str) or not expected_step.strip()):
        raise SwarmError("`step_id` must be the step's id, a non-empty string")
    if expected_step is not None and args.get("format") not in ("lines", "progress"):
        # Refused, not ignored: a caller that asked for the check and did not
        # get it would believe the task it followed was its step.
        raise SwarmError(
            "`step_id` is checked only with `format: \"lines\"` or `format: \"progress\"`; "
            "pass one, or no `step_id`"
        )

    parents = args.get("parents") if name == "swarm_follow" else None
    if parents is not None:
        if args.get("format") != "progress":
            raise SwarmError(
                "`parents` is read only with `format: \"progress\"`; pass that, or no `parents`"
            )
        if not isinstance(parents, list) or not all(isinstance(p, str) and p.strip() for p in parents):
            raise SwarmError("`parents` must be a list of task ids, each a non-empty string")
        if len(args.get("task_ids") or []) != 1:
            raise SwarmError("`parents` names the parents of ONE task; follow one task with it")

    if name == "swarm_follow" and args.get("format") in ("lines", "progress", "summary", "stage"):
        # THE HANDLE (owner decision, 2026-10-05, `progress.Handles`): the row
        # passes back a short handle, an old full token, or -- having dropped
        # it -- nothing, which for tasks this process already answered resumes
        # from the last position it returned, so the call HOLDS rather than
        # answering at once. The reply's `since` is a new handle.
        follower = progress.HANDLES.follower(args["format"], list(args["task_ids"]))
        since_token, since_note = progress.HANDLES.resolve(args.get("since"), follower)
        args = {**args, "since": since_token}
        reply = _follow_rows(client, args, expected_step, parents, keepalive=keepalive)
        _remember_person_waits(reply)
        reply["since"] = progress.HANDLES.issue(follower, reply["since"])
        if since_note:
            reply["since_note"] = since_note
        if args["format"] in ("progress", "summary", "stage"):
            return json.dumps(reply, separators=(",", ":"), ensure_ascii=False, default=str)
        return json.dumps(reply, indent=1, default=str)

    if name == "swarm_follow":
        # `cursor` crosses the model boundary, so it arrives as whatever the
        # model sent -- including a string it decided to quote. `follow`
        # normalises every field it reads, and a cursor it cannot use re-reads
        # from the beginning rather than skipping ahead: a position that
        # quietly became zero costs a repeat, a position that quietly became
        # large loses output silently, and only one of those is recoverable.
        # A handle (`progress.Handles`) is read as the token it stands for;
        # no `since` here is still a read from the beginning.
        given_since, handle_note = progress.HANDLES.resolve(
            args.get("since"), progress.HANDLES.follower("json", list(args["task_ids"])), resume=False,
        )
        since_cursor, since_states, since_said, _groups, since_note = progress.decode_since(given_since)
        since_note = since_note or handle_note
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
                task["result"] = describe_task(client.task(task["task_id"]), client)
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
        # `masked` (owner decision, 2026-09-26): how many credential-shaped
        # strings the API masked in each task's input and metadata, which it
        # serves masked to everyone, the submitter included. Null for a count
        # an older API did not send, never 0.
        rows = []
        for t in args["task_ids"]:
            task = client.task(t)
            # `console`: the API's link to this task, as served, in every
            # state -- a QUEUED task is the one a person opens it to watch.
            # `workspace`: the tenant the task runs in, from its own document
            # (SC-ROWS), which `/sc status` prints first on the task's line.
            # `waiting`/`waiting_line` (#1782): why a waiting task waits, in
            # the API's words, printed after the state; absent otherwise.
            rows.append(with_console(
                {"task_id": t, "workspace": workflows.workspace_of(task), "state": task.get("state"),
                 "masked": masked_counts(task), **progress.waiting_fields(task)}, task
            ))
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
                    finished[task_id] = describe_task(task, client)
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
        # The PLATFORM's catalogue (`GET /v1/runtimes`), because this
        # bridge's copy is as old as the installed plugin. Still answers when
        # the cluster cannot be reached, from the copy, labelled
        # `catalogue_source` -- see `catalogue.served_catalogue`.
        return json.dumps(catalogue.served_catalogue(client), indent=2)

    if name == "swarm_mcp_servers":
        from . import render
        from .sc import mcp_servers, mcp_servers_lines

        view = mcp_servers(client)
        return json.dumps({**view, "text": render.join(mcp_servers_lines(view))}, indent=2)

    if name == "swarm_keys":
        view = keys.key_status(client, args.get("provider", "typesafe"))
        return json.dumps({**view, "text": keys.status_text(view)}, indent=2)

    if name == "swarm_key_set_link":
        # The url and its expiry, nothing else: `keys.key_set_link` copies
        # those two fields, so not even the API's `path` reaches a transcript.
        return json.dumps(
            keys.key_set_link(client, args.get("provider"), args.get("scope", "user")), indent=2
        )

    if name == "swarm_qa_targets":
        view = qatargets.qa_targets(client, args.get("repository"))
        return json.dumps({**view, "text": qatargets.targets_text(view)}, indent=2)

    if name == "swarm_qa_run":
        return json.dumps(
            qatargets.qa_run(client, args.get("target"), screens=args.get("screens"),
                             file=args.get("file", "issues"),
                             browser_tool=args.get("browser_tool"),
                             environments=args.get("environments"),
                             flows_per_step=args.get("flows_per_step")),
            indent=2,
        )

    if name == "swarm_suite_gate":
        # WHERE IT GOES first (S8), as swarm_workflow: under `local` nothing is
        # read or sent. Then every argument is checked and the registration
        # read before the one POST.
        placed = _placement(name, {k: v for k, v in args.items() if k == "target"},
                            profiles=[suitegate.PROFILE])
        envelope, rendered = suitegate.gate(client, args)
        reply = json.loads(_workflow_created(
            without_credentials(envelope), placed=placed,
            repository={"url": rendered["repository_url"], "ref": rendered["repository_ref"],
                        "source": "given"},
            given=[str(step.get("step_id")) for step in rendered["steps"]],
            name=rendered["title"]))
        reply["suite_gate"] = suitegate.reply_fields(rendered)
        return json.dumps(reply, indent=2)

    if name == "swarm_qa_target_credential_link":
        # The url and its expiry, nothing else: `qatargets.credential_link`
        # copies those two fields, so not even the API's `path` (which holds
        # the link's token) reaches a transcript.
        return json.dumps(qatargets.credential_link(client, args.get("target"),
                                                    args.get("environment"),
                                                    args.get("field")), indent=2)

    if name == "swarm_secret_set_link":
        # The url and its expiry, nothing else (secrets-scopes.md §6.4).
        return json.dumps(secret_tools.secret_set_link(
            client, args.get("scope"), args.get("action"), name=args.get("name"),
            repository=args.get("repository")), indent=2)

    if name == "swarm_actions_secret_link":
        return json.dumps(secret_tools.actions_secret_link(
            client, args.get("repository"), args.get("name"),
            environment=args.get("environment")), indent=2)

    if name == "swarm_secrets":
        view = secret_tools.overview(client, args.get("scope"), args.get("repository"))
        return json.dumps({**view, "text": secret_tools.overview_text(view)}, indent=2)

    if name == "swarm_platform_secrets":
        # A 403 for anyone but a platform admin is the route's, raised unchanged.
        view = secret_tools.platform_secrets(client)
        return json.dumps({**view, "text": secret_tools.platform_text(view)}, indent=2)

    if name == "swarm_roles":
        # One GET. A refusal (a deployment predating roles, a 403 for a tenant
        # the caller is not in) is the API's, through `_tool_error_text`.
        from . import render
        from .sc import workspace_roles, workspace_roles_lines

        view = workspace_roles(client)
        return json.dumps({**view, "text": render.join(workspace_roles_lines(view))}, indent=2)

    if name == "swarm_tenants":
        # Two GETs. A `tenant` setting the caller is not a member of comes back
        # as the API's 403, unchanged, through `_tool_error_text`.
        return json.dumps(tenant_listing(client), indent=2)

    if name == "swarm_result":
        task = client.task(args["task_id"])
        return json.dumps(_result_of(client, args["task_id"], task), indent=2)

    if name == "swarm_collect":
        return json.dumps(
            _collect(client, list(args["task_ids"]), _int_arg(args, "wait_seconds", 3600)),
            indent=2,
        )

    if name == "swarm_artifact":
        if args.get("save_to") is not None:
            # Every check before any request (#2182): a refused call reads nothing.
            plan = artifacts.plan_save(
                str(args["task_id"]), str(args["name"]), str(args["save_to"]), base=checkout.directory()
            )
            if plan.is_image_name:
                saved = artifacts.save_image(client, str(args["task_id"]), str(args["name"]), plan)
                if saved is not None:
                    return json.dumps(saved, indent=2)

        def _window() -> dict[str, Any]:
            return client.artifact_content(
                args["task_id"],
                str(args["name"]),
                offset=_int_arg(args, "offset", 0),
                # Absent is the route's own default window, not a size of ours.
                limit_bytes=None if args.get("limit_bytes") is None else _int_arg(args, "limit_bytes", 0),
            )

        # SHOWN AS AN IMAGE (ARTIFACT-IMAGES, part of #2136): only a name with
        # an image extension, only once the text read said `binary` (or the
        # caller asked for the image), and only bytes `show_image` itself
        # sniffs as an image. Text never takes this path, so it stays redacted.
        art = str(args["name"])
        window = None if args.get("as_image") is True and artifacts.is_image_name(art) else _window()
        if artifacts.is_image_name(art) and (window is None or window.get("status") == "binary"):
            shown = artifacts.show_image(
                client, str(args["task_id"]), art,
                stored_bytes=(window or {}).get("total_bytes"),
                console=lambda: console_link(client.task(args["task_id"])),
            )
            if shown is not None:
                return shown
        if window is None:
            window = _window()
        return json.dumps(window, indent=2, default=str)

    if name == "swarm_workflow_spec":
        return json.dumps(_workflow_spec_tool(args), indent=2)

    if name == "swarm_workflow_launch":
        from . import launch, watch

        given = sorted(k for k in ("spec", "spec_path", "attach") if args.get(k) is not None)
        if len(given) != 1:
            raise SwarmError(
                f"swarm_workflow_launch takes exactly one of `spec`, `spec_path` or `attach`; "
                f"it was given {given or 'none'}. Nothing was written"
            )
        filters = sorted(k for k in ("repository", "new_only", "exclude", "project") if args.get(k) is not None)
        target = _text_arg(args, "attach", name) if args.get("attach") is not None else None
        if filters and target not in ("all", "--all"):
            raise SwarmError(
                f"swarm_workflow_launch takes {filters} only with attach \"all\". Nothing was written"
            )
        # THE ROW SHAPE, ASKED FOR (PLUGIN-ROW-SPLIT-2, owner 2026-10-21):
        # `rows` outranks the plugin's setting for this launch, an attach
        # or an attach-all alike. A spec's run reads the setting.
        rows = args.get("rows")
        if rows is not None:
            if rows not in (*config.ROWS_MODES, config.ROWS_PANE):
                raise SwarmError(
                    f"swarm_workflow_launch: `rows` is one of {[*config.ROWS_MODES, config.ROWS_PANE]}; it was "
                    f"{rows!r}. Nothing was written"
                )
            if target is None:
                raise SwarmError(
                    "swarm_workflow_launch takes `rows` only with `attach`: a spec's run follows "
                    "the plugin's `rows` setting. Nothing was written"
                )
        # EVERY LAUNCH ARMS THE WATCH (SC-AUTO-WATCH, owner 2026-10-14): a spec
        # about to run, a workflow attached, or an attach-all that launched at
        # least one run puts work in front of this session, so the reply
        # carries `arm_watch` -- null when nothing was launched or the
        # project's watch is explicitly off.
        if target is not None:
            if target in ("all", "--all"):
                if not filters:
                    attached = launch.for_all(client, rows=rows)
                else:
                    exclude = args.get("exclude")
                    if exclude is not None and not (
                        isinstance(exclude, list) and all(isinstance(i, str) for i in exclude)
                    ):
                        raise SwarmError("swarm_workflow_launch: `exclude` is a list of ids. Nothing was written")
                    attached = launch.for_all(
                        client,
                        repository=args.get("repository"),
                        new_only=_flag(args, "new_only"),
                        exclude=exclude,
                        rows=rows,
                        project=_flag(args, "project"),
                    )
                # The band launches no row but still puts this session's work in
                # front of it: arming is unchanged there (SC-BAND).
                attached.update(watch.arm_fields(bool(attached.get("launches")) or attached.get("followed_by") == "band"))
                return json.dumps(attached, indent=2, default=str)
            return json.dumps({**launch.for_attach(client, target, rows=rows), **watch.arm_fields(True)}, indent=2)
        document = (
            _read_spec_file(str(args["spec_path"]), base=checkout.directory())["spec"]
            if args.get("spec_path") is not None
            else args["spec"]
        )
        # The steps' model classes are checked here and the launch reads the
        # rest (#1435): `workflows.read_spec`, which it calls, holds no
        # `model_class` key, and the titled copy needs none.
        catalogue.check_spec_model_classes(document)
        return json.dumps({
            **launch.for_spec(catalogue.spec_without_model_classes(document), client, embedded=document),
            **watch.arm_fields(True),
        }, indent=2)

    if name == "swarm_apply":
        repo = Path(args.get("repo") or ".").resolve()
        task = client.task(args["task_id"])
        uri = patch_uri(task)
        if uri is None:
            return json.dumps(
                {"applied": False, "reason": explain_absence(task)}, indent=2
            )
        # Through swarm-api's artifact route with the bridge's own credential,
        # never Cloud Storage with the developer's gcloud login (#2834). A
        # patch the API masked or could not serve byte for byte is refused
        # with the reason, as an outcome, and nothing is applied.
        try:
            patch = read_patch(client, task)
        except PatchRefused as exc:
            return json.dumps({"applied": False, "reason": str(exc)}, indent=2)
        result = apply_patch(patch, repo, task_id=args["task_id"])
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

    if name == "swarm_workflow" and args.get("merge_pr") is not None:
        return _workflow_merge_pr(client, args)

    if name == "swarm_territory":
        return _territory_tool(client, args)

    if name == "swarm_workflow":
        # BY REFERENCE (measured 2026-10-01): a spec a relay retyped came back
        # altered three times in one evening. `spec_path` and `spec_ref` put
        # the bytes the bridge read in `spec`'s place, so from here on there is
        # one spec and no model ever carried it; `spec_digest` still checks it.
        # `spec_handle` (SC-LAUNCH-SPEC, #2034) is the file swarm_workflow_launch
        # wrote the embedded spec to: read back from the run folder only, and
        # never without the digest it was written with.
        sources = sorted(k for k in ("spec", "spec_path", "spec_ref", "spec_handle", "steps") if args.get(k) is not None)
        if len(sources) > 1:
            raise SwarmError(
                f"swarm_workflow was given {sources}; pass exactly one of `spec`, "
                "`spec_path`, `spec_ref`, `spec_handle` or `steps`. Nothing was sent"
            )
        if args.get("spec_path") is not None:
            args = {**args, "spec": _read_spec_file(str(args["spec_path"]), base=checkout.directory())["spec"]}
        elif args.get("spec_handle") is not None:
            if args.get("spec_digest") in (None, ""):
                raise SwarmError(
                    "`spec_handle` is submitted only with the `spec_digest` the launch wrote beside "
                    "it: pass both, exactly as the prompt gives them. Nothing was sent"
                )
            from . import launch

            target = launch.handle_target(str(args["spec_handle"]))
            args = {**args, "spec": _read_spec_file(str(target), base=target.parent)["spec"]}
        elif args.get("spec_ref") is not None:
            args = {**args, "spec": _held_spec(str(args["spec_ref"]))}
        # WHERE IT GOES first (S8): a workflow under `local`, or under `hybrid`
        # with a declared local need, submits nothing.
        given_steps = (args.get("spec") or {}).get("steps") if isinstance(args.get("spec"), dict) else args.get("steps")
        placed = _placement(
            name, args,
            profiles=[
                str((step.get("runner_profile") if isinstance(step, dict) else None) or "claude-code")
                for step in (given_steps if isinstance(given_steps, list) else [])
            ],
        )
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
            # THE DIGEST OF WHAT ARRIVED, before anything else reads it. /sc:swarmcloud
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
            fields = workflows.read_spec(_spec_without_files(args["spec"]), where="swarm_workflow's `spec`")
            catalogue.with_model_classes(fields["steps"], args["spec"].get("steps"))
            repo, ref = fields["repository_url"], fields["repository_ref"]
        else:
            if expected_digest not in (None, ""):
                raise SwarmError(
                    "`spec_digest` checks a whole `spec`, and this call passed `steps`; "
                    "pass the spec it was computed over, or no `spec_digest`"
                )
            if args.get("steps") is None:
                raise SwarmError("swarm_workflow needs `steps`, or a whole `spec` (inline, `spec_path`, `spec_ref` or `spec_handle`)")
            fields = {
                "steps": catalogue.with_model_classes(
                    workflows.build_steps(_without_files(args.get("steps"))), args.get("steps")),
                "strategy": args.get("strategy"),
                "carrier": args.get("carrier"),
                "on_step_failure": args.get("on_step_failure"),
                "priority": args.get("priority"),
                "label": args.get("label"),
                "title": workflows.check_title(args.get("title"), where="swarm_workflow"),
                "additional_repositories": check_additional_repositories(
                    args.get("additional_repositories"), where="swarm_workflow"),
            }
            repo, ref = args.get("repo"), args.get("ref")
        # `publish_to_pr` (#2183): the spec's own key, or this tool's argument
        # beside `steps` or a spec. Both naming one pull request is one
        # answer; two different ones are refused, never one picked.
        publish_to_pr = workflows.check_publish_to_pr(args.get("publish_to_pr"), where="swarm_workflow")
        spec_publish = fields.get("publish_to_pr")
        if publish_to_pr is not None and spec_publish is not None and publish_to_pr != spec_publish:
            raise SwarmError(
                f"swarm_workflow was given `publish_to_pr` {publish_to_pr!r} and a spec whose "
                f"`publish_to_pr` is {spec_publish!r}: two pull requests. Pass one. Nothing was sent"
            )
        publish_to_pr = publish_to_pr if publish_to_pr is not None else spec_publish
        # Checked here too, before the territory reads, so a refused spec
        # costs no request at all; `submit` checks it again.
        workflows.check_publish_merge(publish_to_pr, merge=fields.get("merge"),
                                      steps=fields["steps"], where="swarm_workflow")
        credentials = check_mcp_credentials(
            args.get("mcp_credentials"), _step_servers(fields["steps"]), where="swarm_workflow"
        )
        repository = checkout.resolve(
            repo=repo, ref=ref, infer=_flag(args, "infer")
        )
        # THE TERRITORY of every step that declares files; two steps on one
        # dependency line are the plan, not a collision.
        step_lanes = _step_lanes(given_steps)
        territory, checks = _territory_of_units(
            client, [(repository.url, lane) for lane in step_lanes],
            related=_dependency_lines(given_steps if isinstance(given_steps, list) else []),
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
            title=fields["title"],
            # Only a whole spec carries `merge`; the `steps` form never did.
            merge=fields.get("merge"),
            # Nor `after` (SPEED-D3).
            after=fields.get("after"),
            mcp_credentials=credentials,
            # As named: `infer` above chose only the primary repository.
            additional_repositories=fields["additional_repositories"],
            publish_to_pr=publish_to_pr,
        )
        reply = _workflow_created(without_credentials(envelope), placed=placed, repository=repository.as_dict(),
                                  digest=digest, expected_digest=expected_digest,
                                  given=[str(step.get("step_id")) for step in fields["steps"]],
                                  name=fields["title"] or fields["label"],
                                  publish_to_pr=publish_to_pr)
        if not territory and not checks:
            return reply
        created = json.loads(reply)
        created.update(territory)
        tasks = {str(step.get("step_id")): step.get("task_id") for step in created.get("steps") or []}
        for check in checks:
            _remember_territory(client, check, [(lane["lane"], tasks.get(lane["lane"]))
                                                for lane in step_lanes])
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
            out = describe_task(task, client)
            failure = explain_failure(client, task)
            if failure is not None:
                out["failure"] = failure
            return out

        reported = _with_console_links(
            envelope,
            workflows.report(
                envelope,
                describe=_describe_step if name == "swarm_workflow_result" else None,
            ),
        )
        reported["bridge_version"] = bridge_version()
        return json.dumps(reported, indent=2)

    if name == "swarm_inputs":
        from . import awaitinput

        return json.dumps(awaitinput.waiting(client, args.get("limit")), indent=2, default=str)

    if name == "swarm_workflow_input":
        # THE ANSWER IS THE PERSON'S (docs/design/human-steps.md §4.2): typed
        # into the host's elicitation, never read from `args`, which
        # `_refuse_unknown_arguments` has already held to the step and files.
        from . import awaitinput

        return json.dumps(
            awaitinput.answer(
                client, _text_arg(args, "workflow_id", name), _text_arg(args, "step_id", name),
                args.get("files"), can_elicit=_HOST_CAN_ELICIT.is_set(), elicit=_elicit,
            ),
            indent=2,
            default=str,
        )

    if name == "swarm_check":
        from . import check

        return json.dumps(
            check.run(
                client,
                repository=args.get("repository"),
                stale_minutes=args.get("stale_minutes"),
                tenants=args.get("tenants"),
            ),
            indent=2,
            default=str,
        )

    if name == "swarm_build_board":
        from . import buildboard

        return json.dumps(buildboard.refresh(client), indent=2, default=str)

    if name == "swarm_check_schedule":
        from . import watch

        return json.dumps(watch.tool(args), indent=2)

    if name == "swarm_browse":
        from . import browse

        return json.dumps(browse.read(client, args), separators=(",", ":"), ensure_ascii=False, default=str)

    if name == "swarm_layout":
        from . import layout

        if args.get("layout") is None:
            return json.dumps(layout.current(), indent=2)
        return json.dumps({**layout.save(args["layout"]), "note": layout.SWITCH_NOTE}, indent=2)

    if name == "swarm_workflows":
        from . import session
        from .sc import mark_session, running_tasks, running_workflows

        from .launch import named_workspaces

        listing = running_workflows(client)
        names = workflows.workspace_names(client)
        named_workspaces(listing["workflows"], names)
        # THE WHOLE TENANT, MARKED (SC-SESSION-SCOPE): `this_session` on each
        # entry is what /sc:swarmcloud's attach-all keeps, and what
        # `/sc workflows` says is this Claude Code session's.
        sid = session.current()
        mark_session(listing["workflows"], sid)
        listing["session"] = session.describe(sid)
        # The caller's running SINGLE tasks beside them (#830), so the LIST
        # row of `/sc attach --all` can give each a row. A failed read is said,
        # never an empty list.
        try:
            singles = running_tasks(client)
        except SwarmError as exc:
            listing["single_tasks"] = []
            listing["single_tasks_error"] = str(exc)
        else:
            listing["single_tasks"] = mark_session(named_workspaces(singles["tasks"], names), sid)
            listing["single_tasks_count"] = singles["count"]
            if not singles["complete"]:
                listing["single_tasks_incomplete_because"] = singles.get("incomplete_because")
        listing["attach_all_with"] = (
            "/sc attach --all -- or the /sc:swarmcloud workflow with {attach: \"all\"}"
        )
        listing["bridge_version"] = bridge_version()
        return json.dumps(listing, indent=2, default=str)

    if name == "swarm_workflow_cancel":
        return json.dumps(workflows.cancel(client, args["workflow_id"]), indent=2)

    if name in _RUN_TOOLS:
        return _run_tool(client, name, args)

    if name in _SCHEDULE_TOOLS:
        return _schedule_tool(client, name, args)

    if name in _SETUP_TOOLS:
        return _setup_tool(client, name, args)

    if name in _SC_VIEWS:
        from . import render
        from .sc import NEEDS, collect

        style = render.Style(width=int(args.get("width") or 80), color=False, unicode=True)
        view, needs = _SC_VIEWS[name]
        snap = collect(client, NEEDS[needs])
        return render.join(view(snap, style))

    if name in _ACCOUNT_STATE_TOOLS:
        # The same resolve-and-PUT `sc account <verb>` runs (S10): a label is
        # the caller's OWN account or it is refused, an ambiguous one names
        # every id, and the broker's answer comes back verbatim.
        from .sc import change_account_state

        return json.dumps(
            change_account_state(
                client, str(args["account"]), _ACCOUNT_STATE_TOOLS[name], str(args.get("reason") or "")
            ),
            indent=2,
            default=str,
        )

    if name == "swarm_account_remove":
        # DESTRUCTIVE, so a HUMAN types the label (#453, finding 83). This
        # took `confirm_label` as an argument, and its refusal named the label
        # it wanted: a typo guard, since the agent that made the call could
        # repeat it with that label -- which `swarm_accounts` lists anyway --
        # and confirm its own removal. So no argument is the answer any more.
        # The bridge asks the host's human (MCP elicitation), the way `sc
        # account remove` asks at a terminal, and a refusal never says what
        # the answer should have been.
        from .sc import remove_account, resolve_account

        account = resolve_account(client, str(args["account"]))
        label = str(account.get("label") or "")
        if not label:
            raise SwarmError(
                f"account {account.get('account_id')} has no label to type back, so it cannot "
                "be confirmed here; nothing was removed"
            )
        _human_typed_label(account, label)
        return json.dumps(
            {
                "account_id": account.get("account_id"),
                "label": label,
                "broker_answer": remove_account(client, account),
            },
            indent=2,
            default=str,
        )

    if name == "swarm_debug":
        from .sc import DEBUG_EVENTS, DEBUG_LOG_LINES, debug_report

        return json.dumps(
            debug_report(
                client,
                str(args["task_id"]),
                events=_int_arg(args, "events", DEBUG_EVENTS),
                log_lines=_int_arg(args, "log_lines", DEBUG_LOG_LINES),
            ),
            indent=2,
            default=str,
        )

    if name == "swarm_task_answer":
        # One POST. A refusal -- a 403 for someone who may not answer, a 422
        # for a choice that is not one of the options -- is the API's,
        # through `_tool_error_text`, word for word.
        from . import answers

        return json.dumps(answers.record(client, args), indent=2)

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
    from .sc import PLUGIN_SETUP_COMMAND, workspace_refusal_text

    # THE SUBMISSION GATE'S REFUSAL (#847, docs/workspaces.md §6.2), from
    # every submitting tool at once -- swarm_dispatch, swarm_workflow and
    # swarm_run_issue -- because they all fail through here: the
    # API's message word for word, and the command that fixes it.
    refused = workspace_refusal_text(exc, PLUGIN_SETUP_COMMAND)
    if refused is not None:
        return refused
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

#: How many QUICK tool calls may be in flight at once: every call that answers
#: from a read or two -- a submit, a check, a status, a listing. Holds never
#: run here (`HOLD_CALLS`), so this many is a ceiling on the session's own
#: concurrent work, not on how many rows it can watch. 32 was the ceiling for
#: everything before #1437, and with ~17 workflows' rows holding 60-85
#: `swarm_follow`s every submit and check queued behind them for up to 30 min.
MAX_CONCURRENT_CALLS = 32

#: How many HOLDS may be in flight at once (`_holds`): one per live row, and a
#: 5-step workflow keeps 5. 256 is 50 five-step workflows -- three times the 17
#: measured on #1437 -- and a parked hold costs one mostly idle thread, its
#: stack and, since the shared reads, nothing per poll beyond its tasks' one
#: read. `SWARM_MCP_MAX_HOLDS` moves it, held to 32-1024: under 32 a single big
#: workflow's rows would queue again, over 1024 threads is a host problem.
HOLD_CALLS_DEFAULT = 256
HOLD_CALLS_BOUNDS = (32, 1024)

#: The client class `serve` paces, held apart from the `SwarmClient` name a
#: test replaces with a stand-in.
_PACED_CLIENT = SwarmClient


def _hold_calls(environ: Any = None) -> int:
    """`SWARM_MCP_MAX_HOLDS`, read once by `serve`, clamped to `HOLD_CALLS_BOUNDS`."""
    raw = (os.environ if environ is None else environ).get("SWARM_MCP_MAX_HOLDS", "")
    try:
        value = int(str(raw).strip())
    except ValueError:
        return HOLD_CALLS_DEFAULT
    low, high = HOLD_CALLS_BOUNDS
    return max(low, min(high, value))


def _waits(args: dict[str, Any], name: str, default: int) -> bool:
    """Does this argument ask the call to wait? Absent means `default`; a value
    that is not a number is taken as waiting, because the classifier must never
    put a call that might block on the quick lane -- the tool refuses or
    defaults it either way."""
    value = args.get(name)
    if value is None:
        return default > 0
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return True


def _holds(name: str, args: Any) -> bool:
    """Whether this tool call may block for longer than a few reads -- a HOLD,
    which runs on the hold lane so it can never occupy a quick worker.

    Every tool that blocks is named here, with the argument that makes it:

    * `swarm_follow` with `wait_seconds` > 0, or with `parents` (a step whose
      parents still run holds even on its first call, up to 30 minutes);
    * `swarm_wait` unless `timeout_seconds` is 0 (default 3600);
    * `swarm_collect` unless `wait_seconds` is 0 (default 3600);
    * `swarm_setup_status` with `wait_for_github_seconds` > 0;
    * `swarm_account_remove`, which waits up to `ELICIT_TIMEOUT_SECONDS` for
      the host's human to type a label;
    * `swarm_workflow_input`, which waits up to
      `awaitinput.ELICIT_TIMEOUT_SECONDS` for the person to fill the form in.

    Everything else answers from a bounded number of reads and is quick.
    """
    if not isinstance(args, dict):
        args = {}
    if name == "swarm_follow":
        return _waits(args, "wait_seconds", 0) or bool(args.get("parents"))
    if name == "swarm_wait":
        return _waits(args, "timeout_seconds", 3600)
    if name == "swarm_collect":
        return _waits(args, "wait_seconds", 3600)
    if name == "swarm_setup_status":
        return _waits(args, "wait_for_github_seconds", 0)
    return name in ("swarm_account_remove", "swarm_workflow_input")


#: How often a running tool call tells the host it is alive. Claude Code
#: (2.1.283) aborts a call that sent no response and no progress notification
#: for its idle timeout -- `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, else 1,800,000
#: ms for a stdio server, 300,000 ms otherwise -- checked every 30 s, and its
#: `onprogress` handler resets that clock. A row's parent hold is one silent
#: call of up to thirty minutes, and lane U12's rows were cut there on
#: 2026-10-04. Once a minute is a fifth of the shortest default and a few
#: dozen bytes; it does not extend `MCP_TOOL_TIMEOUT`, the hard limit, which
#: Claude Code says progress never does.
KEEPALIVE_SECONDS = 60.0


class _Keepalive:
    """`notifications/progress` for one call, every `KEEPALIVE_SECONDS`, until
    `stop()` -- which returns only once the last one is written, so none can
    follow the call's answer.

    Only for a call whose request carried `_meta.progressToken`: Claude Code
    sets it (the request id) on every call it makes with an `onprogress`, and
    drops a notification whose token it does not know, so the token is echoed
    exactly as it came. `progress` rises by one each time, as the protocol
    requires; there is no `total`, because a hold has no known length.
    """

    def __init__(self, token: Any) -> None:
        self._token = token
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="swarm-mcp-keepalive", daemon=True)

    def start(self) -> "_Keepalive":
        self._thread.start()
        return self

    def _run(self) -> None:
        sent = 0
        began = time.monotonic()
        while not self._done.wait(KEEPALIVE_SECONDS):
            sent += 1
            elapsed = int(time.monotonic() - began)
            _notify("notifications/progress", {
                "progressToken": self._token,
                "progress": sent,
                "message": f"still holding after {elapsed}s",
            })

    def stop(self) -> None:
        self._done.set()
        self._thread.join()


def _progress_token(params: dict[str, Any]) -> Any:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    token = meta.get("progressToken")
    # The protocol's token is a string or an integer; anything else is not
    # one a host could match, and a bool is not an integer here.
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


def _notify(method: str, params: dict[str, Any]) -> None:
    line = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
    with _WRITE_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


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


#: WHETHER THE HOST CAN ASK ITS HUMAN A QUESTION (MCP elicitation), as its
#: `initialize` declared. Read by `swarm_account_remove` and
#: `swarm_workflow_input`: a host that cannot ask gets a refusal naming the
#: terminal command or the console link, never a confirmation or an answer
#: the calling agent could supply itself.
_HOST_CAN_ELICIT = threading.Event()

#: How long `swarm_account_remove` waits for the human's answer. A person reads
#: the prompt and types a label; five minutes is ample for that, and a prompt
#: left unanswered removes nothing.
ELICIT_TIMEOUT_SECONDS = 300.0

#: Elicitations in flight, by the id of the request this bridge sent: the host
#: answers on stdin, which `serve` reads, while the asking call waits in its
#: worker thread.
_ELICITING: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
_ELICIT_LOCK = threading.Lock()


def _elicit(
    message: str, schema: dict[str, Any], *, timeout: float = ELICIT_TIMEOUT_SECONDS
) -> dict[str, Any] | None:
    """Ask the host's human through the host (`elicitation/create`).

    The host's whole JSON-RPC response, or None when none came within
    `timeout` seconds (`ELICIT_TIMEOUT_SECONDS` unless the caller says). The answer is typed by the person at the host,
    into the host's own prompt: the model that made the tool call neither sees
    the request nor writes its answer.
    """
    request_id = f"swarm-elicit-{secrets.token_hex(8)}"
    answered = threading.Event()
    box: list[dict[str, Any]] = []
    with _ELICIT_LOCK:
        _ELICITING[request_id] = (answered, box)
    try:
        line = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "elicitation/create",
            "params": {"message": message, "requestedSchema": schema},
        }) + "\n"
        with _WRITE_LOCK:
            sys.stdout.write(line)
            sys.stdout.flush()
        if not answered.wait(timeout):
            return None
        return box[0]
    finally:
        with _ELICIT_LOCK:
            _ELICITING.pop(request_id, None)


def _deliver_elicitation(message: dict[str, Any]) -> None:
    """Hand the host's response to the call waiting on it; any other is dropped."""
    message_id = message.get("id")
    if not isinstance(message_id, str):
        return
    with _ELICIT_LOCK:
        waiting = _ELICITING.get(message_id)
    if waiting is None:
        return
    answered, box = waiting
    box.append(message)
    answered.set()


def _human_typed_label(account: dict[str, Any], label: str) -> None:
    """Return only when the host's human typed `label`; otherwise refuse.

    EVERY REFUSAL LEAVES THE LABEL OUT. The one this replaced named it, which
    told the caller exactly what to send next.
    """
    command = terminal_command(f"sc account remove {account.get('account_id')}")
    if not _HOST_CAN_ELICIT.is_set():
        raise SwarmError(
            "removing an account needs its label typed by a person, and this host did not "
            "say it can ask one (MCP elicitation); nothing was removed. Run "
            f"`{command}` in a terminal: it asks for the label there"
        )
    reply = _elicit(
        f"An agent asked to REMOVE the SwarmCloud account {label} "
        f"({account.get('account_id')}) from the pool. The broker keeps its secret. "
        "Type the account's label to confirm; anything else removes nothing.",
        {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "title": "Account label",
                    "description": "The label of the account to remove, exactly.",
                },
            },
            "required": ["label"],
        },
    )
    if reply is None:
        raise SwarmError(
            f"nobody answered the confirmation within {int(ELICIT_TIMEOUT_SECONDS)}s; "
            "nothing was removed"
        )
    result = reply.get("result")
    if not isinstance(result, dict) or result.get("action") != "accept":
        raise SwarmError(
            "the removal was not confirmed (declined, cancelled, or the host could not "
            f"ask); nothing was removed. `{command}` asks at a terminal instead"
        )
    typed = (result.get("content") or {}).get("label") if isinstance(result.get("content"), dict) else None
    if not isinstance(typed, str) or typed.strip() != label:
        raise SwarmError("the label typed was not this account's; nothing was removed")


def serve(stdin=None, stdout=None) -> int:
    """The stdio loop. One JSON-RPC message per line, both directions.

    TOOL CALLS RUN CONCURRENTLY, and answer in the order they finish, on TWO
    LANES (#1437): a hold (`_holds`) runs on its own pool of `HOLD_CALLS`, and
    every other call on a quick pool of `MAX_CONCURRENT_CALLS` that no hold can
    occupy -- so a submit or a check answers at once however many rows hold.
    The hold pool's threads are marked (`enter_hold_lane`), so the client's
    rate gate serves a quick call's request before a hold's. This loop
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
    # THE `tenant` SETTING, READ ONCE, HERE (#447). Every call this session
    # makes acts as the same tenant: read per call, an edit to the variable
    # mid-session would move half a workflow's reads to another tenant. None
    # sends no X-Swarm-Tenant at all, and the API picks as it always did.
    tenant = chosen_tenant(None)

    def _client() -> SwarmClient:
        with building:
            if "client" not in held:
                client = SwarmClient(tenant=tenant)
                # SHARED READS AND A RATE GATE, for the bridge only (#1437):
                # every hold of this process is one principal at swarm-api's
                # 20/s. Only the real client is paced; a test's stand-in for
                # it is served as it is.
                if isinstance(client, _PACED_CLIENT):
                    client.pace_for_bridge()
                held["client"] = client
            return held["client"]

    def _answer(message_id: Any, params: dict[str, Any]) -> None:
        token = _progress_token(params)
        alive = _Keepalive(token).start() if token is not None else None
        try:
            try:
                text = _answered(
                    _client(), params.get("name", ""), params.get("arguments") or {},
                    keepalive=alive is not None,
                )
            finally:
                if alive is not None:
                    alive.stop()
            if isinstance(text, artifacts.ImageAnswer):
                _respond(message_id, {"content": text.content()})
            else:
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
    ) as calls, ThreadPoolExecutor(
        max_workers=_hold_calls(), thread_name_prefix="swarm-mcp-hold",
        initializer=enter_hold_lane,
    ) as holds:
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
                capabilities = (message.get("params") or {}).get("capabilities") or {}
                if isinstance(capabilities, dict) and isinstance(capabilities.get("elicitation"), dict):
                    _HOST_CAN_ELICIT.set()
                else:
                    _HOST_CAN_ELICIT.clear()
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
                lane = holds if _holds(params.get("name", ""), params.get("arguments")) else calls
                lane.submit(_answer, message_id, params)
            elif method is None and message_id is not None:
                # A RESPONSE, to a request this bridge sent (an elicitation).
                # A response is never answered, whatever it matches.
                _deliver_elicitation(message)
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


def check_session_target() -> str | None:
    """The session's default target, checked at start-up: None, or why not.

    REFUSED HERE rather than at the first dispatch (S8): a setting that names
    no target would otherwise surface as an error on whichever call happened
    to dispatch first, long after the person who set it has moved on, and the
    read tools would work in the meantime as if nothing were wrong.
    """
    try:
        config.session_target()
    except SwarmError as exc:
        return str(exc)
    return None


def main() -> int:
    problem = check_session_target()
    if problem is not None:
        # STDERR: stdout is the JSON-RPC channel. Claude Code shows a server
        # that exits at start-up as failed, with this line in its log.
        print(f"swarm-mcp: refusing to start: {problem}", file=sys.stderr)
        return 2
    seed_plugin_config()
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
