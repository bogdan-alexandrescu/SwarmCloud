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
  * `ndjson`             -- JSON lines of some other shape;
  * `text`               -- nothing in the window parses; `steps` is null and
                            the reader shows the raw `/logs` window instead.

A line that does not parse inside an otherwise-JSON window is COUNTED in
`skipped_lines`, never silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
    """Redacts and caps every string of one window, and counts what it masked."""

    count: int = 0

    def text(self, value: Any, *, name: str, truncated: list[str]) -> str | None:
        if not isinstance(value, str):
            return None
        # `decoded=True`: masked at least as far as the raw line would be.
        cleaned = redact(value, decoded=True)
        self.count += cleaned.count
        return self._capped(cleaned.text, name=name, truncated=truncated)

    def json(self, value: Any, *, name: str, truncated: list[str], indent: int | None) -> str:
        """A decoded JSON value, masked by its structure (`JsonMasker`), as its text.

        NOT `text(json.dumps(value))`: that is JSON TEXT, where a quote inside
        a string is `\\"` and the key/value rule used to stop at the backslash
        (#221). The masker masks each string decoded, and a value under a
        credential's name whole.
        """
        cleaned = JsonMasker(value).json(value, indent=indent)
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
    data: bytes, *, base_offset: int, whole_object: bool, include_raw: bool = False
) -> ParsedWindow:
    """Steps for one window of WHOLE lines, starting at stream offset `base_offset`.

    `whole_object` is True when the window is the entire object (offset 0 and
    the end of it), which is the only case a single `result` event can be told
    apart as `claude-json` rather than the last page of a stream.
    """
    scrubber = _Scrubber()
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
    else:
        fmt = "ndjson"
    steps: list[dict[str, Any]] = []
    for offset, record in records:
        if isinstance(record, bytes):
            steps.append(_notice_step(scrubber, offset, record))
        else:
            steps.extend(map_event(scrubber, record, line_offset=offset, include_raw=include_raw))
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
    # step that carries it, as the record always was.
    raw = JsonMasker(event).json(event, indent=None) if include_raw else None

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
