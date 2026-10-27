"""An agent's stdout, parsed into readable steps -- server-side, redacted after decoding.

WHAT IS PARSED (#184). claude-code runs `--output-format stream-json
--verbose`: one JSON event per line -- `system`/`init`, `assistant` messages
whose content is a list of blocks (text, thinking, tool_use), `user` messages
carrying tool results, `rate_limit_event` readings, and a final `result`. An
attempt made before that ran `--output-format json`, which prints ONE `result`
object when the run ends and records none of the turns. Other runners print
plain text (codex `exec`) or their own lines. This module turns one window of
any of those into `Step` records the drawer draws one row each.

WHY ON THE SERVER. Every string is redacted AFTER JSON decoding. A regex over
the raw line would miss a credential written with a JSON escape -- `\\u0067hp_`
is `ghp_` once decoded -- and the browser must never receive the bytes to
decode itself. So each string field of each step goes through
`swarm_api.redaction.redact` and is then capped (`FIELD_CAP_CHARS`, named in
`truncated_fields` when it bit), and the event's own record (`raw`) is only
served on request, re-serialised from the decoded event after it was masked by
its structure (below). Image bytes inside a tool result are COUNTED, never
served: base64 in a JSON string would be a way around the raw route's image
path.

AT LEAST AS FAR AS THE RAW LINE (#188 review). Decoding turns a private key's
`\\n` escapes into real newlines, and the line-scoped rule then masked the
BEGIN line and served the body that `/logs` masks over the same bytes. Each
string is redacted with `decoded=True`: a key is masked as a block, and one
with no END to the end of the string, which is what the line rule masked of
the raw line holding it.

A TOOL'S INPUT AND AN EVENT'S RECORD ARE MASKED BY THEIR STRUCTURE (#221,
owner decision 2026-09-26). Both used to be `json.dumps` of a decoded object,
redacted as ONE string: JSON TEXT, where a quote inside a string is `\\"`. An
agent's Bash call `export DB_PASSWORD="<v>"` was served with its backslash
masked and `<v>` in clear, under a count of 1, and `"api_key": "<v>"` inside a
curl body was not matched at all. Both now go through `redaction.JsonMasker`
-- every string and key masked as a decoded string, a value under a
credential's name masked whole -- which is what `/input` does with a task's
input. The text is the same `json.dumps` the step always carried (indented for
a tool's input, one line for `raw`), so a clean record reads as it did.

A CAPTURE CUT AT ITS CAP. Where the worker's output capture dropped the
middle of a stream, it wrote a line starting `TRUNCATION_MARK`. That line is a
`system` step with `meta.subtype: "capture_truncated"`, not an unparseable
line, and the window says `capture_truncated`.

THE FORMAT IS SNIFFED, NOT TRUSTED. Nothing the worker wrote says what the
stream is:

  * `claude-stream-json` -- JSON lines, at least one of a Claude event type;
  * `claude-json`        -- the whole object is ONE `result` event (one line,
                            or pretty-printed across several);
  * `codex-exec-json`    -- JSON lines, none of a Claude event type and at
                            least one of a codex `exec --json` event type
                            (CODEX-TRANSCRIPT, below);
  * `ndjson`             -- JSON lines of some other shape;
  * `text`               -- nothing in the window parses; `steps` is null and
                            the reader shows the raw `/logs` window instead.

A line that does not parse inside an otherwise-JSON window is COUNTED in
`skipped_lines`, never silently dropped.

THE TASK'S OWN LITERALS TOO (the PR #229 fix-up, owner decision 2026-09-26). A
value named only in the task's input or metadata -- never a rule any string
here would match on its own -- is masked wherever the agent echoes it, the same
way `/input` masks it: `agent_output.read_transcript` passes `masking_for(task)
.literals` down as `parse_window`'s `literals`, and every `_Scrubber` call in
this module applies them after the rules, exactly as `JsonMasker.text()` does.
No second masker is built; these are the SAME literals the task's one masker
already learned.

CODEX'S EVENTS (CODEX-TRANSCRIPT, part of #2459; BGD, 2026-10-27, PR 2515
question 2). The codex profile runs `codex exec --json`. The worker renders its
capture readable once the run ends (`agent_worker.runners.codex_events`), but
the live tail a RUNNING attempt publishes is the raw stream, and read as
`ndjson` every event was an empty `other` row. Those events now map onto the
step kinds the drawer already draws, with the item kinds and the turn
summary's wording of `codex_events` -- kept in step by hand, since swarm-api
does not depend on the worker:

  * `thread.started` is `init` (`meta.thread_id`); `turn.started`,
    `turn.completed` (the usage row: input, cached and output tokens),
    `turn.failed` and `error` are `system` rows named by `meta.subtype`;
  * an `agent_message` item is assistant `text`, `reasoning` is `thinking`;
  * `command_execution` is a `tool_call` named `command` whose input is the
    command, and once it has output or has ended a `tool_result` carrying the
    output and its `exit N`; `mcp_tool_call` is a `tool_call` named
    `server.tool` whose input is its arguments, masked by their structure;
  * `file_change`, `web_search`, `todo_list` and an `error` item are short
    `system` rows; an item or event of any other type is `other`, named.

An item's `item.started`, `item.updated` and `item.completed` share ONE place
in the window: the latest of them is mapped where the first one was, so a
running command becomes its result and a todo list ticking stays one row. Only
the served event is mapped, so nothing replaced is counted in
`redaction_count`. Every string still goes through `_Scrubber`, exactly as a
Claude event's does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .agent_streams import TRUNCATION_MARK
from .redaction import JsonMasker, Redacted, redact

#: The longest any one string field of a step may be, in characters (16 KiB).
#: A tool result that printed a whole file is the common case that reaches it,
#: and a step row is not a file viewer -- the artifact routes are.
FIELD_CAP_CHARS = 16 * 1024

#: Event types Claude Code's stream-json emits. Any one of them in a window
#: makes it `claude-stream-json`.
CLAUDE_EVENT_TYPES = frozenset(
    {"system", "assistant", "user", "result", "rate_limit_event", "stream_event"}
)

#: Event types codex-cli's `exec --json` emits (0.154.0, the version the base
#: image pins). Any one of them in a window with no Claude event makes it
#: `codex-exec-json`, the format the worker's finished transcript names.
CODEX_EVENT_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed", "error",
})

#: The item events that carry one `item` and share its place in the window.
CODEX_ITEM_EVENTS = frozenset({"item.started", "item.updated", "item.completed"})

STEP_KINDS = (
    "init", "text", "thinking", "tool_call", "tool_result", "result",
    "rate_limit", "system", "other",
)

#: The `result` event's fields a reader wants beside the answer text.
RESULT_META = (
    "subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd",
    "stop_reason", "terminal_reason",
)

_SCALARS = (str, int, float, bool, type(None))


@dataclass
class ParsedWindow:
    format: str | None
    steps: list[dict[str, Any]] | None
    skipped_lines: int = 0
    redaction_count: int = 0
    answer_in_window: bool = False
    #: A line of this window is the capture's notice that it cut the stream.
    capture_truncated: bool = False


@dataclass
class _Scrubber:
    """Redacts and caps every string of one window, and counts what it masked.

    `literals`: the task's OWN masker's learned literals (`task_input.
    TaskMasking.literals`, the same tuple `/input` and `redaction.redact_lines`
    apply) -- the PR #229 fix-up. A value named only in the task's input or
    metadata, echoed by the agent in its transcript, is looked for here after
    the rules run, exactly as `JsonMasker.text()` applies them, so a literal a
    rule already caught is never counted twice.
    """

    count: int = 0
    literals: tuple[str, ...] = field(default_factory=tuple)

    def text(self, value: Any, *, name: str, truncated: list[str]) -> str | None:
        if not isinstance(value, str):
            return None
        # `decoded=True`: masked at least as far as the raw line would be.
        cleaned = redact(value, decoded=True, extra=self.literals)
        self.count += cleaned.count
        return self._capped(cleaned.text, name=name, truncated=truncated)

    def json(self, value: Any, *, name: str, truncated: list[str], indent: int | None) -> str:
        """A decoded JSON value, masked by its structure (`JsonMasker`), as its text.

        NOT `text(json.dumps(value))`: that is JSON TEXT, where a quote inside
        a string is `\\"` and the key/value rule used to stop at the backslash
        (#221). The masker masks each string decoded, and a value under a
        credential's name whole.
        """
        cleaned = JsonMasker(value, literals=self.literals).json(value, indent=indent)
        self.count += cleaned.count
        return self._capped(cleaned.text, name=name, truncated=truncated)

    @staticmethod
    def _capped(out: str, *, name: str, truncated: list[str]) -> str:
        if len(out) > FIELD_CAP_CHARS:
            out = out[:FIELD_CAP_CHARS]
            truncated.append(name)
        return out


def split_lines(data: bytes, *, base_offset: int) -> list[tuple[int, bytes]]:
    """`(stream offset, line)` for every line of `data`, terminator dropped."""
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(data):
        newline = data.find(b"\n", pos)
        end = len(data) if newline < 0 else newline
        out.append((base_offset + pos, data[pos:end]))
        pos = end + 1
    return out


def _decode(line: bytes) -> tuple[bool, Any]:
    """`(parsed, value)` for one line. Invalid UTF-8 and deep nesting are just unparseable."""
    try:
        return True, json.loads(line)
    except (ValueError, RecursionError):
        return False, None


def parse_window(
    data: bytes,
    *,
    base_offset: int,
    whole_object: bool,
    include_raw: bool = False,
    literals: Iterable[str] = (),
) -> ParsedWindow:
    """Steps for one window of WHOLE lines, starting at stream offset `base_offset`.

    `whole_object` is True when the window is the entire object (offset 0 and
    the end of it), which is the only case a single `result` event can be told
    apart as `claude-json` rather than the last page of a stream.

    `literals`: the task's masker's learned literals (the PR #229 fix-up),
    passed down to every step's `_Scrubber` so a value the task's input or
    metadata named as secret is masked wherever the agent echoes it too.
    """
    scrubber = _Scrubber(literals=tuple(literals))
    # In order: `(offset, event)` for a JSON object, `(offset, line)` for the
    # capture's notice that it cut the stream here.
    records: list[tuple[int, dict[str, Any] | bytes]] = []
    events: list[dict[str, Any]] = []
    skipped = 0
    cut = False
    for offset, line in split_lines(data, base_offset=base_offset):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(TRUNCATION_MARK):
            cut = True
            records.append((offset, stripped))
            continue
        parsed, value = _decode(stripped)
        if parsed and isinstance(value, dict):
            records.append((offset, value))
            events.append(value)
        else:
            skipped += 1

    if not events:
        if skipped == 0:
            # A measured-empty window: the object is empty, or this page held
            # only blank lines (and the capture's notices). Not "text".
            steps = [_notice_step(scrubber, offset, line) for offset, line in records]
            return ParsedWindow(
                format=None, steps=steps, redaction_count=scrubber.count, capture_truncated=cut
            )
        if whole_object and not cut:
            # One JSON document written across several lines.
            parsed, document = _decode(data.strip())
            if parsed and isinstance(document, dict) and document.get("type") == "result":
                steps = map_event(scrubber, document, line_offset=base_offset, include_raw=include_raw)
                return ParsedWindow(
                    format="claude-json",
                    steps=steps,
                    redaction_count=scrubber.count,
                    answer_in_window=_has_answer(steps),
                )
        # Nothing here is JSON: a plain-text stream. Nothing is dropped -- the
        # reader shows the raw window -- so nothing is counted as skipped.
        return ParsedWindow(format="text", steps=None, capture_truncated=cut)

    if any(event.get("type") in CLAUDE_EVENT_TYPES for event in events):
        single_result = (
            whole_object and skipped == 0 and not cut and len(events) == 1
            and events[0].get("type") == "result"
        )
        fmt = "claude-json" if single_result else "claude-stream-json"
    elif any(event.get("type") in CODEX_EVENT_TYPES for event in events):
        fmt = "codex-exec-json"
        records = _codex_records(records)
    else:
        fmt = "ndjson"
    mapper = map_codex_event if fmt == "codex-exec-json" else map_event
    steps: list[dict[str, Any]] = []
    for offset, record in records:
        if isinstance(record, bytes):
            steps.append(_notice_step(scrubber, offset, record))
        else:
            steps.extend(mapper(scrubber, record, line_offset=offset, include_raw=include_raw))
    return ParsedWindow(
        format=fmt,
        steps=steps,
        skipped_lines=skipped,
        redaction_count=scrubber.count,
        answer_in_window=_has_answer(steps),
        capture_truncated=cut,
    )


def _notice_step(scrubber: _Scrubber, offset: int, line: bytes) -> dict[str, Any]:
    """The capture's notice that it dropped bytes here, as a `system` step."""
    return _step(
        scrubber, uuid=None, line_offset=offset, block=0, kind="system", role="system",
        parent=None, raw=None, text=line.decode("utf-8", errors="replace"),
        meta={"subtype": "capture_truncated"},
    )


def _has_answer(steps: list[dict[str, Any]]) -> bool:
    return any(step["kind"] == "result" and step["text"] is not None for step in steps)


def map_event(
    scrubber: _Scrubber, event: dict[str, Any], *, line_offset: int, include_raw: bool
) -> list[dict[str, Any]]:
    """One decoded event to one step per content block (at least one step)."""
    kind = event.get("type")
    uuid = event.get("uuid") if isinstance(event.get("uuid"), str) and event.get("uuid") else None
    parent = event.get("parent_tool_use_id")
    # Masked ONCE per event, by its structure (#221), and counted on every
    # step that carries it, as the record always was. `scrubber.literals`:
    # the task's own masker's literals (the PR #229 fix-up), same as every
    # other string this event contributes.
    raw = JsonMasker(event, literals=scrubber.literals).json(event, indent=None) if include_raw else None

    def step(block: int, **fields: Any) -> dict[str, Any]:
        return _step(
            scrubber, uuid=uuid, line_offset=line_offset, block=block,
            parent=parent, raw=raw, **fields,
        )

    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            tools = event.get("tools")
            return [step(
                0, kind="init", role="system",
                meta={
                    "model": event.get("model"),
                    "permission_mode": event.get("permissionMode"),
                    "tool_count": len(tools) if isinstance(tools, list) else None,
                },
            )]
        return [step(0, kind="system", role="system", meta={"subtype": subtype})]

    if kind in ("assistant", "user"):
        role = kind
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return [step(0, kind="text", role=role, text=content)]
        if not isinstance(content, list) or not content:
            return [step(0, kind="other", role=role, meta={"raw_type": kind})]
        return [
            _block_step(step, index, block, role=role)
            for index, block in enumerate(content)
        ]

    if kind == "result":
        return [step(
            0, kind="result", role=None, text=event.get("result"),
            meta={name: event.get(name) for name in RESULT_META},
        )]

    if kind == "rate_limit_event":
        return [step(0, kind="rate_limit", role=None, meta=_scalars(event))]

    return [step(
        0, kind="other", role=None,
        meta={"raw_type": kind if isinstance(kind, str) else None},
    )]


def _codex_records(
    records: list[tuple[int, dict[str, Any] | bytes]],
) -> list[tuple[int, dict[str, Any] | bytes]]:
    """One record per codex item: its LATEST event, at its FIRST event's offset.

    `item.updated` and `item.completed` replace what `item.started` drew,
    never add a row. A `thread.started` begins a new CLI start whose item ids
    count from `item_0` again, so it forgets the ids seen before it.
    """
    out: list[tuple[int, dict[str, Any] | bytes]] = []
    places: dict[str, int] = {}
    for offset, record in records:
        if isinstance(record, dict):
            kind = record.get("type")
            if kind == "thread.started":
                places = {}
            item = record.get("item") if kind in CODEX_ITEM_EVENTS else None
            item_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(item_id, str) and item_id:
                at = places.get(item_id)
                if at is not None:
                    out[at] = (out[at][0], record)
                    continue
                places[item_id] = len(out)
        out.append((offset, record))
    return out


def _codex_usage_line(usage: Any) -> str:
    """A turn's usage in `codex_events._usage_line`'s words, so the live row
    and the finished log say the same thing."""
    if not isinstance(usage, dict):
        return "turn completed"
    inp = _int(usage.get("input_tokens")) or 0
    cached = _int(usage.get("cached_input_tokens")) or 0
    out = _int(usage.get("output_tokens")) or 0
    return (f"turn completed: {inp:,} input tokens ({cached:,} cached), "
            f"{out:,} output tokens")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _message(value: Any) -> str | None:
    """An error's message, from `{message}` or as the string itself."""
    if isinstance(value, dict):
        value = value.get("message")
    return value if isinstance(value, str) and value else None


def map_codex_event(
    scrubber: _Scrubber, event: dict[str, Any], *, line_offset: int, include_raw: bool
) -> list[dict[str, Any]]:
    """One decoded codex `exec --json` event to its steps (at least one)."""
    kind = event.get("type")
    raw = JsonMasker(event, literals=scrubber.literals).json(event, indent=None) if include_raw else None

    def step(block: int, **fields: Any) -> dict[str, Any]:
        return _step(
            scrubber, uuid=None, line_offset=line_offset, block=block,
            parent=None, raw=raw, **fields,
        )

    def row(subtype: str, text: str, **meta: Any) -> list[dict[str, Any]]:
        return [step(0, kind="system", role="system", text=text, meta={"subtype": subtype, **meta})]

    if kind == "thread.started":
        return [step(0, kind="init", role="system", meta={"thread_id": event.get("thread_id")})]
    if kind == "turn.started":
        return row("turn_started", "turn started")
    if kind == "turn.completed":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        return row(
            "turn_completed", _codex_usage_line(event.get("usage")),
            **{name: _int(usage.get(name))
               for name in ("input_tokens", "cached_input_tokens", "output_tokens")},
        )
    if kind == "turn.failed":
        return row("turn_failed", f"turn failed: {_message(event.get('error')) or '(no message)'}")
    if kind == "error":
        return row("error", f"error: {_message(event) or '(no message)'}")
    if kind in CODEX_ITEM_EVENTS and isinstance(event.get("item"), dict):
        return _codex_item_steps(step, row, event["item"])
    return [step(0, kind="other", role=None, meta={"raw_type": kind if isinstance(kind, str) else None})]


def _codex_item_steps(step: Any, row: Any, item: dict[str, Any]) -> list[dict[str, Any]]:
    """An item's steps, by `codex_events._item_step`'s item kinds."""
    kind = item.get("type")
    item_id = item.get("id") if isinstance(item.get("id"), str) else None
    status = item.get("status") if isinstance(item.get("status"), str) else None
    if kind == "agent_message":
        return [step(0, kind="text", role="assistant", text=item.get("text"))]
    if kind == "reasoning":
        return [step(0, kind="thinking", role="assistant", text=item.get("text"))]
    if kind == "command_execution":
        call = step(0, kind="tool_call", role="assistant",
                    tool={"id": item_id, "name": "command", "input": item.get("command")})
        output = item.get("aggregated_output") if isinstance(item.get("aggregated_output"), str) else ""
        exit_code = _int(item.get("exit_code"))
        if status == "in_progress" and not output:
            return [call]
        lines = [output.rstrip("\n")] if output.strip() else []
        if exit_code is not None:
            lines.append(f"exit {exit_code}")
        elif status and status != "in_progress":
            lines.append(status)
        is_error = (exit_code != 0) if exit_code is not None else (
            True if status in ("failed", "declined") else None)
        return [call, step(
            1, kind="tool_result", role="user",
            tool_result={"tool_use_id": item_id, "is_error": is_error, "content": "\n".join(lines)},
            meta={"exit_code": exit_code, "status": status},
        )]
    if kind == "mcp_tool_call":
        server, tool = item.get("server"), item.get("tool")
        call = step(0, kind="tool_call", role="assistant", tool={
            "id": item_id,
            "name": ".".join(str(part) for part in (server, tool) if isinstance(part, str)) or None,
            "input": item.get("arguments"),
        })
        if status == "in_progress":
            return [call]
        error = _message(item.get("error"))
        result = item.get("result")
        content = error if error is not None else (
            result.get("content") if isinstance(result, dict) else result)
        return [call, step(
            1, kind="tool_result", role="user",
            tool_result={"tool_use_id": item_id,
                         "is_error": error is not None or status == "failed",
                         "content": content},
            meta={"status": status},
        )]
    if kind == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        named = ", ".join(f"{c.get('kind') or 'change'} {c.get('path')}"
                          for c in changes if isinstance(c, dict))
        return row("file_change", f"files ({status or 'unknown'}): {named or '(none named)'}",
                   status=status)
    if kind == "web_search":
        return row("web_search", f"web search: {item.get('query')}")
    if kind == "todo_list":
        items = item.get("items") if isinstance(item.get("items"), list) else []
        return row("todo_list", "plan:\n" + "\n".join(
            f"    [{'x' if t.get('completed') is True else ' '}] {t.get('text')}"
            for t in items if isinstance(t, dict)))
    if kind == "error":
        return row("error", f"error: {_message(item) or '(no message)'}")
    return [step(0, kind="other", role=None, meta={"raw_type": kind if isinstance(kind, str) else None})]


def _block_step(step: Any, index: int, block: Any, *, role: str) -> dict[str, Any]:
    if not isinstance(block, dict):
        return step(index, kind="other", role=role, meta={"raw_type": type(block).__name__})
    block_type = block.get("type")
    if block_type == "text":
        return step(index, kind="text", role=role, text=block.get("text"))
    if block_type == "thinking":
        return step(index, kind="thinking", role=role, text=block.get("thinking"))
    if block_type == "redacted_thinking":
        return step(index, kind="thinking", role=role, meta={"redacted": True})
    if block_type == "tool_use":
        return step(
            index, kind="tool_call", role=role,
            tool={"id": block.get("id"), "name": block.get("name"), "input": block.get("input")},
        )
    if block_type == "tool_result":
        return step(index, kind="tool_result", role=role, tool_result=block)
    return step(
        index, kind="other", role=role,
        meta={"raw_type": block_type if isinstance(block_type, str) else None},
    )


def _scalars(event: dict[str, Any]) -> dict[str, Any]:
    """An event's top-level scalars, plus one level of nested ones as `outer.inner`.

    `rate_limit_event` keeps its readings in a nested object in some CLI
    releases (`rate_limit_info`), and a reading reduced to its top level would
    be a reading with nothing in it.
    """
    out: dict[str, Any] = {}
    for key, value in event.items():
        if key in ("type", "uuid", "session_id") or not isinstance(key, str):
            continue
        if isinstance(value, _SCALARS):
            out[key] = value
        elif isinstance(value, dict):
            for inner, nested in value.items():
                if isinstance(inner, str) and isinstance(nested, _SCALARS):
                    out[f"{key}.{inner}"] = nested
    return out


def _step(
    scrubber: _Scrubber,
    *,
    uuid: str | None,
    line_offset: int,
    block: int,
    kind: str,
    role: str | None,
    parent: Any,
    raw: Redacted | None,
    text: Any = None,
    tool: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    truncated: list[str] = []
    ident = f"{uuid}:{block}" if uuid else f"L{line_offset}:{block}"
    out: dict[str, Any] = {
        "id": scrubber.text(ident, name="id", truncated=truncated),
        "line_offset": line_offset,
        "block": block,
        "kind": kind,
        "role": role,
        "parent_tool_use_id": scrubber.text(parent, name="parent_tool_use_id", truncated=truncated),
        "text": scrubber.text(text, name="text", truncated=truncated),
        "tool": None,
        "tool_result": None,
        "meta": None,
        "truncated_fields": truncated,
        "raw": None,
    }
    if tool is not None:
        served_id = scrubber.text(tool.get("id"), name="tool.id", truncated=truncated)
        served_name = scrubber.text(tool.get("name"), name="tool.name", truncated=truncated)
        tool_input = tool.get("input")
        if tool_input is None or isinstance(tool_input, str):
            # A string input is one decoded string already.
            served_input = scrubber.text(tool_input, name="tool.input", truncated=truncated)
        else:
            # A decoded object: masked by its structure, never as its JSON
            # text (#221), and served as the same indented text as before.
            served_input = scrubber.json(tool_input, name="tool.input", truncated=truncated, indent=2)
        out["tool"] = {"id": served_id, "name": served_name, "input": served_input}
    if tool_result is not None:
        content_text, images = _tool_result_content(tool_result.get("content"))
        is_error = tool_result.get("is_error")
        out["tool_result"] = {
            "tool_use_id": scrubber.text(
                tool_result.get("tool_use_id"), name="tool_result.tool_use_id", truncated=truncated
            ),
            "is_error": is_error if isinstance(is_error, bool) else None,
            "content": scrubber.text(content_text, name="tool_result.content", truncated=truncated),
            "images": images,
        }
    if meta is not None:
        cleaned: dict[str, Any] = {}
        for key, value in meta.items():
            if isinstance(value, str):
                cleaned[key] = scrubber.text(value, name=f"meta.{key}", truncated=truncated)
            elif isinstance(value, _SCALARS):
                cleaned[key] = value
        out["meta"] = cleaned
    if raw is not None:
        scrubber.count += raw.count
        out["raw"] = scrubber._capped(raw.text, name="raw", truncated=truncated)
    return out


def _tool_result_content(content: Any) -> tuple[str | None, int]:
    """A tool result's text parts joined, and how many images it carried."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return None, 0
    parts: list[str] = []
    images = 0
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            if part.get("type") == "image":
                images += 1
            elif isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts), images


def last_result_event(data: bytes, *, starts_mid_line: bool) -> dict[str, Any] | None:
    """The LAST `type: "result"` event in a window of an agent's stdout, or None.

    Scanned from the end, one line at a time, so a 4 MiB stream costs one
    decode in the common case (the result is the last line). A window that
    starts mid-line drops its first fragment. When no line parses as a result
    and the window is the whole object, the object is tried as ONE document --
    a `result` written across several lines.
    """
    lines = data.split(b"\n")
    if starts_mid_line and lines:
        lines = lines[1:]
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith(b"{"):
            continue
        parsed, value = _decode(stripped)
        if parsed and isinstance(value, dict) and value.get("type") == "result":
            return value
    if not starts_mid_line:
        parsed, value = _decode(data.strip())
        if parsed and isinstance(value, dict) and value.get("type") == "result":
            return value
    return None
