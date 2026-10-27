"""codex's `--json` event stream, rendered as a log a person reads (CODEX-JSON, part of #2459).

WHY THE STREAM IS JSON NOW. BGD, 2026-10-26 (BUDGET-CAP question 6): the
codex profile runs `codex exec --json`, so its stdout carries the usage the
budget meter counts (`turn.completed`). Without it the stdout was the final
answer in prose and a codex step's budget was pinned but enforced by nothing
(docs/design/agent-budget.md §6).

WHY IT IS RENDERED. The console's Logs tab opens a codex attempt on its
agent stdout, and swarm-api's transcript reader knows Claude's event types,
not codex's: a JSONL stream it cannot map is a column of empty `other` rows,
and the raw window is a wall of JSON. So when the runner is done with the
stream -- every start of the CLI, the budget's stop and the failures included
-- `finish_capture` turns the capture into:

  * `codex.stdout.log`: the same name the worker uploads and the Logs tab
    reads (`streams.agent_stream_files`), now READABLE TEXT -- the agent's
    messages, its commands with their output and exit codes, file changes,
    tool calls, errors and each turn's usage. A line that is not a codex
    event (a capture's cut notice, prose from a CLI started without
    `--json`) is kept as it was;
  * `codex.events.jsonl`: the raw stream, byte for byte, kept as an artifact;
  * `codex-transcript.json`: the steps as a JSON document, replacing the
    re-indented event list `cliagent` writes, and written only where
    `cliagent` wrote one (never past its cap or over a cut capture).

The live tail the worker publishes WHILE the CLI runs is still the raw
stream: the capture is the one the runner's watchers read as it is written,
and it is rendered only once nothing reads it any more.

The event shapes are codex-cli 0.154.0's `exec --json` (the version the base
image pins): `thread.started {thread_id}`, `turn.started`, `item.started` /
`item.updated` / `item.completed {item}`, `turn.completed {usage}`,
`turn.failed {error}`, and `error {message}`. An item is an `agent_message`,
`reasoning`, `command_execution`, `file_change`, `mcp_tool_call`,
`web_search`, `todo_list` or `error`. A type this module does not know is
named in the log, never dropped silently.

Every string comes from the capture `cliagent` has already redacted, and is
redacted again here AFTER decoding (`scrub_text`): a registered value written
with a JSON escape is only itself once decoded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

#: The raw stream's artifact, beside the readable `codex.stdout.log`.
EVENTS_NAME = "codex.events.jsonl"

#: What a transcript document says it is.
TRANSCRIPT_FORMAT = "codex-exec-json"

#: The longest a command's output may be in the readable log, in characters.
#: The raw stream keeps all of it; a log line is not a file viewer.
OUTPUT_CAP_CHARS = 8_000

#: The final message, as long as a result summary carries it (`_summarise`).
SUMMARY_CHARS = 2_000


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _capped(text: str, cap: int = OUTPUT_CAP_CHARS) -> str:
    if len(text) <= cap:
        return text
    return f"{text[:cap]}\n[... {len(text) - cap:,} more characters in {EVENTS_NAME}]"


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def parse(raw: str) -> list[Any]:
    """The stream's records in order: each event a dict, any other line its text."""
    records: list[Any] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                records.append(event)
                continue
        records.append(line.rstrip())
    return records


def is_event_stream(raw: str) -> bool:
    """Whether a capture holds codex events at all: a CLI started without
    `--json` (an operator's CODEX_ARGS) prints prose, which is left alone."""
    return any(isinstance(record, dict) for record in parse(raw))


def _item_step(item: dict[str, Any]) -> dict[str, Any]:
    kind = item.get("type")
    if kind == "agent_message":
        return {"kind": "message", "text": item.get("text")}
    if kind == "reasoning":
        return {"kind": "reasoning", "text": item.get("text")}
    if kind == "command_execution":
        return {"kind": "command", "command": item.get("command"),
                "output": item.get("aggregated_output"),
                "exit_code": _int(item.get("exit_code")), "status": item.get("status")}
    if kind == "file_change":
        changes = item.get("changes")
        return {"kind": "file_change", "status": item.get("status"),
                "changes": [{"kind": c.get("kind"), "path": c.get("path")}
                            for c in changes if isinstance(c, dict)]
                if isinstance(changes, list) else []}
    if kind == "mcp_tool_call":
        error = item.get("error")
        return {"kind": "tool_call", "server": item.get("server"), "tool": item.get("tool"),
                "status": item.get("status"),
                "error": error.get("message") if isinstance(error, dict) else error}
    if kind == "web_search":
        return {"kind": "web_search", "query": item.get("query")}
    if kind == "todo_list":
        items = item.get("items")
        return {"kind": "todo_list",
                "items": [{"text": t.get("text"), "completed": t.get("completed") is True}
                          for t in items if isinstance(t, dict)]
                if isinstance(items, list) else []}
    if kind == "error":
        return {"kind": "error", "text": item.get("message")}
    return {"kind": "other", "item_type": kind if isinstance(kind, str) else None}


def steps(records: Iterable[Any]) -> list[dict[str, Any]]:
    """The run as steps, in order: one per completed item, per turn's end, per
    error, and per line that was not an event.

    A command is also a step when it STARTED and never completed -- a run
    stopped mid-command (the budget's kill, a timeout) shows what it was
    running. `item.updated` (a todo list ticking) is not a step; the item's
    completion is.
    """
    out: list[dict[str, Any]] = []
    open_commands: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            out.append({"kind": "text", "text": record})
            continue
        kind = record.get("type")
        if kind == "thread.started":
            out.append({"kind": "thread", "thread_id": record.get("thread_id")})
        elif kind == "turn.started":
            out.append({"kind": "turn_started"})
        elif kind == "turn.completed":
            usage = record.get("usage")
            out.append({"kind": "turn_completed",
                        "usage": usage if isinstance(usage, dict) else None})
        elif kind == "turn.failed":
            error = record.get("error")
            out.append({"kind": "turn_failed",
                        "text": error.get("message") if isinstance(error, dict) else None})
        elif kind == "error":
            out.append({"kind": "error", "text": record.get("message")})
        elif kind in ("item.started", "item.completed"):
            item = record.get("item")
            if not isinstance(item, dict):
                continue
            item_id = item.get("id") if isinstance(item.get("id"), str) else None
            if kind == "item.started":
                if item.get("type") == "command_execution":
                    if item_id is not None:
                        open_commands[item_id] = len(out)
                    out.append({**_item_step(item), "status": "in_progress"})
                continue
            step = _item_step(item)
            at = open_commands.pop(item_id, None) if item_id is not None else None
            if at is not None:
                out[at] = step
            else:
                out.append(step)
        elif kind == "item.updated":
            continue
        else:
            out.append({"kind": "other", "event_type": kind})
    return out


def final_message(rendered: list[dict[str, Any]]) -> str | None:
    """The agent's last message: what the run answered."""
    for step in reversed(rendered):
        if step.get("kind") == "message" and _text(step.get("text")):
            return step["text"].strip()
    return None


def usage_total(rendered: list[dict[str, Any]]) -> dict[str, int]:
    """Every turn's usage added up, as codex reports it (cached input inside input)."""
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for step in rendered:
        usage = step.get("usage") if step.get("kind") == "turn_completed" else None
        if isinstance(usage, dict):
            for key in total:
                total[key] += _int(usage.get(key)) or 0
    return total


def _usage_line(usage: dict[str, Any] | None) -> str:
    if not isinstance(usage, dict):
        return "turn completed"
    inp = _int(usage.get("input_tokens")) or 0
    cached = _int(usage.get("cached_input_tokens")) or 0
    out = _int(usage.get("output_tokens")) or 0
    return (f"turn completed: {inp:,} input tokens ({cached:,} cached), "
            f"{out:,} output tokens")


def render_text(rendered: list[dict[str, Any]]) -> str:
    """The steps as the readable log: one block per step, no JSON."""
    blocks: list[str] = []
    for step in rendered:
        kind = step.get("kind")
        if kind == "text":
            blocks.append(str(step.get("text")))
        elif kind == "thread":
            blocks.append(f"== codex session {step.get('thread_id') or '(unnamed)'}")
        elif kind == "turn_started":
            blocks.append("== turn started")
        elif kind == "turn_completed":
            blocks.append(f"== {_usage_line(step.get('usage'))}")
        elif kind == "turn_failed":
            blocks.append(f"!! turn failed: {step.get('text') or '(no message)'}")
        elif kind == "error":
            blocks.append(f"!! error: {step.get('text') or '(no message)'}")
        elif kind == "message":
            blocks.append(f"codex:\n{_indent(str(step.get('text') or ''))}")
        elif kind == "reasoning":
            blocks.append(f"thinking:\n{_indent(str(step.get('text') or ''))}")
        elif kind == "command":
            lines = [f"$ {step.get('command') or ''}"]
            output = _text(step.get("output"))
            if output:
                lines.append(_indent(_capped(output)))
            if step.get("status") == "in_progress":
                lines.append("    (still running when the stream ended)")
            elif step.get("exit_code") is not None:
                lines.append(f"    exit {step['exit_code']}")
            elif step.get("status"):
                lines.append(f"    {step['status']}")
            blocks.append("\n".join(lines))
        elif kind == "file_change":
            changes = ", ".join(f"{c.get('kind') or 'change'} {c.get('path')}"
                                for c in step.get("changes") or [])
            blocks.append(f"files ({step.get('status') or 'unknown'}): {changes or '(none named)'}")
        elif kind == "tool_call":
            line = f"tool {step.get('server')}.{step.get('tool')} ({step.get('status') or 'unknown'})"
            if step.get("error"):
                line += f": {step['error']}"
            blocks.append(line)
        elif kind == "web_search":
            blocks.append(f"web search: {step.get('query')}")
        elif kind == "todo_list":
            blocks.append("plan:\n" + "\n".join(
                f"    [{'x' if t.get('completed') else ' '}] {t.get('text')}"
                for t in step.get("items") or []))
        else:
            what = step.get("item_type") or step.get("event_type")
            blocks.append(f"({what or 'an unnamed'} event, in {EVENTS_NAME})")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def transcript_document(rendered: list[dict[str, Any]]) -> dict[str, Any]:
    """`codex-transcript.json`: the steps, the final message and the usage."""
    thread = next((s.get("thread_id") for s in rendered if s.get("kind") == "thread"), None)
    return {
        "format": TRANSCRIPT_FORMAT,
        "thread_id": thread,
        "final_message": final_message(rendered),
        "usage": usage_total(rendered),
        "events": EVENTS_NAME,
        "steps": rendered,
    }


def _scrubbed(value: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrubbed(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v, scrub) for v in value]
    return value


def finish_capture(
    artifacts_dir: Path, stdout_name: str, transcript_name: str,
    scrub: Callable[[str], str],
) -> str | None:
    """Render the capture in place, keep the raw stream beside it, and
    replace the transcript `cliagent` wrote. The final message, or None.

    Leaves a capture with no codex event in it exactly as it is. Never
    raises: a capture that cannot be rendered stays the raw stream, which is
    what it was before this module.
    """
    stdout_path = artifacts_dir / stdout_name
    try:
        raw = stdout_path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    records = parse(text)
    if not any(isinstance(record, dict) for record in records):
        return None
    try:
        rendered = _scrubbed(steps(records), scrub)
        (artifacts_dir / EVENTS_NAME).write_bytes(raw)
        stdout_path.write_text(render_text(rendered))
        transcript = artifacts_dir / transcript_name
        if transcript.exists():
            transcript.write_text(json.dumps(transcript_document(rendered), indent=2))
    except (OSError, ValueError):
        return None
    message = final_message(rendered)
    return message[:SUMMARY_CHARS] if message else None


__all__ = [
    "EVENTS_NAME",
    "TRANSCRIPT_FORMAT",
    "final_message",
    "finish_capture",
    "is_event_stream",
    "parse",
    "render_text",
    "steps",
    "transcript_document",
    "usage_total",
]
