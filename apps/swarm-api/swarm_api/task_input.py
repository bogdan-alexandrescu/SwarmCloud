"""The task's input as the UI may draw it: masked at read time, with a count.

WHY (#184 follow-up). The owner decided on 2026-09-25 that the Artifacts
pane's Inputs and the drawer's Details show "a read-time-redacted copy of the
task's input, with 'masked N', like every other output". Both drew
`task.input` straight off `GET /v1/tasks/{id}`: the Artifacts pane under an
honest `as submitted · not masked`, Details with no qualifier at all. A
token pasted into a prompt -- the likeliest way a credential reaches this
platform's own documents -- was drawn in clear on the two screens that mask
every other byte they show.

THE SAME REDACTOR, NOT A SECOND ONE. Every text below comes from
`redaction.redact` and its one set of `RULES`, and the count is its count:

  * the prompt as one DECODED string (`redact(decoded=True)`), the way
    `/answer` and `/transcript` treat the strings they decode out of JSON: a
    private key with no END is masked to the end of the string;
  * the rest of the input, and the whole of it, through `redact_json`: every
    string masked decoded, as the prompt is, then the JSON text the UI draws
    (`indent=2`, as `JSON.stringify(input, null, 2)`) masked for what only the
    document's shape shows -- `"api_token": "<a value with no recognisable
    prefix>"` is caught by its key, which no string holds.

WHY NOT ONE `redact()` OVER THE JSON TEXT, which is what this served first
(the PR #210 review). JSON text writes a quote inside a string as `\\"`, and
the key/value rule reads a quote as a quote: a prompt holding
`{"api_key": "<bare>"}` or `PASSWORD="<bare>"` came out masked in `prompt`
and in clear in `full`, with `full`'s count saying nothing was found. The
prompt's string is masked once (`cache`), and the same result is used in each
block, so `full`'s count is always `prompt`'s plus `rest`'s.

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

from datetime import datetime
from typing import Any

from swarm_common.models import Task

from .redaction import RULES, Redacted, redact, redact_json


def _served(scrubbed: Redacted) -> dict[str, Any]:
    return {"text": scrubbed.text, "redaction_count": scrubbed.count}


def input_copy(task: Task, *, read_at: datetime) -> dict[str, Any]:
    """`GET /v1/tasks/{id}/input`: the task's input, masked, with its counts.

    `prompt` is `input.prompt` when it is a string; `rest` is the input
    without it, when anything else was submitted; `full` is the whole input.
    Each carries its own `redaction_count`, and the top-level count is
    `full`'s -- every mask in the input, counted once, which is `prompt`'s
    plus `rest`'s.
    """
    submitted: dict[str, Any] = dict(task.input or {})
    raw_prompt = submitted.get("prompt")
    if isinstance(raw_prompt, str):
        prompt_key = "string"
    elif "prompt" in submitted:
        prompt_key = "other"
    else:
        prompt_key = "missing"

    # One pass-1 result per distinct string, shared by every block below, so
    # the prompt drawn alone and the prompt inside `full` are one masking.
    masked: dict[str, Redacted] = {}
    prompt = None
    if isinstance(raw_prompt, str):
        prompt = masked[raw_prompt] = redact(raw_prompt, decoded=True)
    rest = None
    if prompt_key == "string":
        others = {k: v for k, v in submitted.items() if k != "prompt"}
        if others:
            rest = redact_json(others, cache=masked)
    full = redact_json(submitted, cache=masked)

    return {
        "task_id": task.id,
        "tenant_id": task.tenant_id,
        "read_at": read_at,
        "prompt_key": prompt_key,
        "prompt": _served(prompt) if prompt is not None else None,
        "rest": _served(rest) if rest is not None else None,
        "full": _served(full),
        "redacted": full.count > 0,
        "redaction_count": full.count,
        "redaction": {"applied_at_read_time": True, "rules": len(RULES)},
    }
