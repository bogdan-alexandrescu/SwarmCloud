"""An MCP server, so SwarmCloud tasks sit beside local subagents in a session.

WHY THE PROTOCOL IS SPOKEN DIRECTLY RATHER THAN THROUGH AN SDK. This runs on an
operator's laptop and its whole job is to remove friction; requiring a `pip
install` before anyone can look at their own platform would add some. MCP over
stdio is newline-delimited JSON-RPC, which is about eighty lines, and those
eighty lines mean `uv run swarm-mcp` works in a fresh checkout.

WHAT THIS SERVER DELIBERATELY DOES NOT DO: stream. An MCP tool call returns
exactly once, so there is no way for a tool to print a log line while an agent
is still writing it. `swarm_wait` therefore blocks and returns the outcome, and
the tool descriptions point at `swarm tail`, which is a terminal command that
CAN stream and can be run in the background. Pretending otherwise -- a
`swarm_watch` tool that returned a 900-line transcript and called it live --
would be the kind of almost-true that wastes an afternoon.

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

from . import workflows
from .client import SwarmClient, SwarmError, task_id_of
from .patches import (
    apply_patch,
    describe_task,
    download,
    explain_absence,
    integrate,
    patch_uri,
)

PROTOCOL_VERSION = "2024-11-05"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER", "DEAD_LETTERED"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_dispatch",
        "description": (
            "Run an agent in SwarmCloud instead of locally. Returns a task id "
            "immediately; the agent runs remotely on its own tenant identity. "
            "Use this the way you would spawn a local subagent, then call "
            "swarm_wait, or run `swarm tail <id>` in a background shell to "
            "follow its output live as it works."
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
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "swarm_wait",
        "description": (
            "Block until the given tasks reach a terminal state, then return what "
            "each produced: commits, files changed, patch uri and pull request. "
            "This does NOT stream -- an MCP tool returns once. For live output "
            "while an agent works, run `swarm tail <id>` in a background shell."
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
            "different causes needing six different responses."
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
            "FAILS the attempt rather than starting the agent without it. Every "
            "`input_from` source must also appear in `depends_on`, or the API "
            "refuses the whole submission.\n"
            "\n"
            "Returns as soon as the workflow is accepted, with the task id of "
            "every step. It does not wait and cannot stream: poll "
            "swarm_workflow_status, or run the `swarm tail` command it hands "
            "back in a background shell."
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
                        "`fail_workflow` cancels the dependents of a failed "
                        "step; `continue` lets independent branches finish."
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
            "swarm_integrate, using the task ids below."
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
            "global one, which is the one people quote. ROOM is how many more "
            "tasks of that profile fit before the binding pool refuses."
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
            "Returns immediately; it does not follow anything. For live output "
            "run `swarm tail <id>` in a background shell."
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
        task = client.dispatch(
            prompt=args["prompt"],
            runner_profile=args.get("profile") or "claude-code",
            repository_url=args.get("repo"),
            repository_ref=args.get("ref"),
            metadata={"unit": args["label"]} if args.get("label") else None,
        )
        task_id = task_id_of(task)
        if not task_id:
            # `follow_live_with: "swarm tail "` is worse than an error: it
            # still looks like a command, so a session runs it.
            raise SwarmError(
                f"the API accepted the task but its response named no id: {sorted(task)}"
            )
        return json.dumps(
            {
                "task_id": task_id,
                "state": task.get("state"),
                "follow_live_with": f"swarm tail {task_id}",
            },
            indent=2,
        )

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

    if name == "swarm_result":
        return json.dumps(describe_task(client.task(args["task_id"])), indent=2)

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
            created["follow_live_with"] = "swarm tail " + " ".join(task_ids)
        return json.dumps(created, indent=2)

    if name in ("swarm_workflow_status", "swarm_workflow_result"):
        # ONE read for both. The difference is only how much of each step's task
        # is unpacked, and a second route call to answer "and what did it
        # produce" would be a second chance for the two answers to disagree
        # about the same workflow.
        envelope = workflows.fetch(client, args["workflow_id"])
        return json.dumps(
            workflows.report(
                envelope,
                describe=describe_task if name == "swarm_workflow_result" else None,
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
                    {"content": [{"type": "text", "text": str(exc)}], "isError": True},
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


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
