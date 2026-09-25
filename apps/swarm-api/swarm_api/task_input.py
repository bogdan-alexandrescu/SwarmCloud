"""The task's input as the UI may draw it: masked at read time, with a count.

WHY (#184 follow-up). The owner decided on 2026-09-25 that the Artifacts
pane's Inputs and the drawer's Details show "a read-time-redacted copy of the
task's input, with 'masked N', like every other output". Both drew
`task.input` straight off `GET /v1/tasks/{id}`: the Artifacts pane under an
honest `as submitted · not masked`, Details with no qualifier at all. A
token pasted into a prompt -- the likeliest way a credential reaches this
platform's own documents -- was drawn in clear on the two screens that mask
every other byte they show.

THE SAME REDACTOR, NOT A SECOND ONE. Every text below is what
`redaction.redact` returns for it, and the count is its count:

  * the prompt as one DECODED string (`decoded=True`), the way `/answer` and
    `/transcript` treat the strings they decode out of JSON: a private key
    with no END is masked to the end of the string;
  * the rest of the input, and the whole of it, as the pretty-printed JSON
    text the UI draws (`indent=2`, as `JSON.stringify(input, null, 2)`). As
    TEXT, not value by value, because the key/value rule is what catches
    `"api_token": "<a value with no recognisable prefix>"`, and it needs the
    key beside its value to do it.

THE TASK DOCUMENT IS UNCHANGED, and `GET /v1/tasks/{id}` still serves the
input as submitted -- to the tenant that submitted it, for the CLI and MCP
clients that resubmit or inspect it. This route is what a SCREEN draws. That
the task route stays raw is a decision for the owner, recorded in the #184
follow-up PR, not one this module makes.

`prompt_key` says what shape the input has, so a client never goes back to
the raw document to find out: `string` (the prompt is text, possibly empty),
`missing` (no `prompt` key: legitimate, the key is a convention of the CLI
and mock runners), or `other` (a `prompt` that is not text).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from swarm_common.models import Task

from .redaction import RULES, redact


def _json_text(value: Any) -> str:
    """The JSON text the UI draws for a value: two-space indent, unicode kept.

    `default=str` so a Firestore timestamp that reached an input (a document
    written by hand, an import) is shown as its text rather than failing the
    read.
    """
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _masked(text: str, *, decoded: bool) -> dict[str, Any]:
    scrubbed = redact(text, decoded=decoded)
    return {"text": scrubbed.text, "redaction_count": scrubbed.count}


def input_copy(task: Task, *, read_at: datetime) -> dict[str, Any]:
    """`GET /v1/tasks/{id}/input`: the task's input, masked, with its counts.

    `prompt` is `input.prompt` when it is a string; `rest` is the input
    without it, when anything else was submitted; `full` is the whole input.
    Each carries its own `redaction_count`, and the top-level count is
    `full`'s -- every mask in the input, counted once.
    """
    submitted: dict[str, Any] = dict(task.input or {})
    raw_prompt = submitted.get("prompt")
    if isinstance(raw_prompt, str):
        prompt_key = "string"
    elif "prompt" in submitted:
        prompt_key = "other"
    else:
        prompt_key = "missing"

    prompt = _masked(raw_prompt, decoded=True) if isinstance(raw_prompt, str) else None
    rest = None
    if prompt_key == "string":
        others = {k: v for k, v in submitted.items() if k != "prompt"}
        if others:
            rest = _masked(_json_text(others), decoded=False)
    full = _masked(_json_text(submitted), decoded=False)

    return {
        "task_id": task.id,
        "tenant_id": task.tenant_id,
        "read_at": read_at,
        "prompt_key": prompt_key,
        "prompt": prompt,
        "rest": rest,
        "full": full,
        "redacted": full["redaction_count"] > 0,
        "redaction_count": full["redaction_count"],
        "redaction": {"applied_at_read_time": True, "rules": len(RULES)},
    }
