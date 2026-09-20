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

Every tool here is a thin wrapper over `cli`/`patches`. Two implementations of
"what does integrate mean" is how a CLI and a tool quietly start disagreeing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .client import SwarmClient, SwarmError
from .patches import apply_patch, download, explain_absence, integrate, patch_uri

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


def _describe(task: dict[str, Any]) -> dict[str, Any]:
    summary = task.get("result_summary") or {}
    git = summary.get("git") or {}
    out: dict[str, Any] = {
        "task_id": task.get("task_id"),
        "state": task.get("state"),
        "commits": git.get("commit_count", 0),
        "insertions": git.get("insertions", 0),
        "deletions": git.get("deletions", 0),
        "uncommitted_files": git.get("dirty_count", 0),
        "patch": patch_uri(task),
    }
    if not out["patch"]:
        out["no_patch_because"] = explain_absence(task)
    pr = git.get("pull_request")
    out["pull_request"] = pr["url"] if pr else None
    if not pr and git:
        out["no_pull_request_because"] = git.get("publish_reason")
    if task.get("last_error"):
        out["error"] = task["last_error"]
    return out


def _call(client: SwarmClient, name: str, args: dict[str, Any]) -> str:
    if name == "swarm_dispatch":
        task = client.dispatch(
            prompt=args["prompt"],
            runner_profile=args.get("profile") or "claude-code",
            repository_url=args.get("repo"),
            repository_ref=args.get("ref"),
            metadata={"unit": args["label"]} if args.get("label") else None,
        )
        task_id = task.get("task_id", "")
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
        while pending and time.monotonic() < deadline:
            for task_id in list(pending):
                task = client.task(task_id)
                if task.get("state") in TERMINAL:
                    finished[task_id] = _describe(task)
                    pending.remove(task_id)
            if pending:
                time.sleep(5)
        out: dict[str, Any] = {"finished": list(finished.values())}
        if pending:
            # Saying WHICH are still running matters: a caller that read this
            # as "everything finished" would integrate a partial set and call
            # it the whole batch.
            out["still_running"] = pending
            out["note"] = "the wait timed out; these tasks have not finished"
        return json.dumps(out, indent=2)

    if name == "swarm_result":
        return json.dumps(_describe(client.task(args["task_id"])), indent=2)

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
