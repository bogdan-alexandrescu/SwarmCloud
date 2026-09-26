"""The task's input and metadata as the API may serve them: masked at read time, counted.

WHY (#184 follow-up). The owner decided on 2026-09-25 that the Artifacts
pane's Inputs and the drawer's Details show "a read-time-redacted copy of the
task's input, with 'masked N', like every other output". Both drew
`task.input` straight off `GET /v1/tasks/{id}`: the Artifacts pane under an
honest `as submitted · not masked`, Details with no qualifier at all. A
token pasted into a prompt -- the likeliest way a credential reaches this
platform's own documents -- was drawn in clear on the two screens that mask
every other byte they show.

MASKED EVERYWHERE (owner decision, 2026-09-26, on #184). That first change
masked what the SCREENS drew and left `GET /v1/tasks/{id}` serving the input as
submitted, to the tenant that submitted it, for the CLI and MCP clients. The
owner closed that: "the API never serves a credential-shaped string back, even
to the submitter". Every route that serves a task -- the task itself, the task
list, a create or cancel response, a workflow read's tasks -- serves the input
this module masks, with `input_redaction_count` beside it, and a workflow's
steps serve theirs the same way (`codec.workflow_to_api`). Nothing serves the
raw input any more.

AND THE METADATA (the owner's "mask it everywhere", 2026-09-26). Details drew
`task.metadata` raw between two masked blocks. `TaskCreate.metadata` is
caller-supplied, so it is masked by the same masker as the input -- ONE masker
per task, built over the input and the caller's metadata together, so a literal
masked in one is masked in the other -- and served with its own
`metadata_redaction_count`, on the task and as `/input`'s `metadata` block.

THE KEYS THE PLATFORM WRITES STAY READABLE, and they are told apart by the one
fact that proves who wrote them: `validation.RESERVED_METADATA_KEYS`
(`dispatch`, `input_from`, `expected_outputs`) are REFUSED at submission from
every caller (`check_reserved_metadata`), so a value under one of them was
written by `SubmissionService` and nobody else. They are served exactly as
stored and never counted: the worker, the UI's workflow joins and
`codec.dispatch_of` read them, and a masked filename in `input_from` would be a
staged input nobody can find. `unit`, `source` and `origin` -- the label keys
this platform's own CLI, plugin and scripts write -- are NOT reserved, so a
caller can write them too, and nothing distinguishes the platform's value from
a caller's. They go through the masker like every caller key. A label is never
credential-shaped, so it is served as written; a credential put there is
masked. Exempting them by name would be a place to store a secret the API
serves raw.

THE SAME REDACTOR, NOT A SECOND ONE. Every text below comes from
`redaction.redact` and its one set of `RULES`, and the count is its count,
applied by `redaction.JsonMasker` over the document's structure:

  * every string, the prompt included, and every key, as one DECODED string
    (`redact(decoded=True)`), the way `/answer` and `/transcript` treat the
    strings they decode out of JSON: a private key with no END is masked to
    the end of the string;
  * a value under a key that names a credential -- `"api_token": "<a value
    with no recognisable prefix>"`, a list of lines under `private_key`, a PIN
    under `password` -- masked whole and counted once, which no rule over any
    one string could see;
  * a private key stored as a list of lines, masked from BEGIN to END;
  * a literal any of those masked, masked wherever else the document holds it.

ONE MASKER FOR ALL THREE BLOCKS (the PR #210 re-review). The masker is built
over the WHOLE input (and now the caller's metadata), so the blocks drawn side
by side agree about every literal in them. Each string is masked once and the
result reused, so `full`'s count is always `prompt`'s plus `rest`'s, and
`input_redaction_count` on the task is `full`'s.

WHY NOT A RULE OVER THE JSON TEXT, which is what this served first and then
second (the PR #210 review and re-review): see "A JSON document" in
`redaction.py`. The first leaked quoted secrets; the second masked a list's
opening bracket under `password`, served the list, and stopped being JSON.

`prompt_key` says what shape the input has, so a client never goes back to
the raw document to find out: `string` (the prompt is text, possibly empty),
`missing` (no `prompt` key: legitimate, the key is a convention of the CLI
and mock runners), or `other` (a `prompt` that is not text).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from swarm_common.models import Task

from .redaction import RULES, JsonMasker, Redacted
from .validation import RESERVED_METADATA_KEYS

#: The metadata keys served as stored: only the platform can write them,
#: because submission refuses each from every caller. See the module docstring
#: for why `unit`, `source` and `origin` are not among them.
PLATFORM_METADATA_KEYS: tuple[str, ...] = RESERVED_METADATA_KEYS


class TaskMasking:
    """ONE masker over a task's input and the metadata its caller wrote.

    Built once per served task, so a literal the metadata names as a
    credential is masked in the prompt, and one the prompt assigns
    (`DB_PASSWORD=<v>`) is masked in the metadata. The platform's own keys
    (`PLATFORM_METADATA_KEYS`) are outside it: they are neither masked nor a
    source of literals.
    """

    def __init__(self, task_input: dict[str, Any] | None, metadata: dict[str, Any] | None) -> None:
        self.submitted: dict[str, Any] = dict(task_input or {})
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.caller_metadata: dict[str, Any] = {
            k: v for k, v in self.metadata.items() if k not in PLATFORM_METADATA_KEYS
        }
        self.masker = JsonMasker({"input": self.submitted, "metadata": self.caller_metadata})

    @classmethod
    def of(cls, task: Task) -> "TaskMasking":
        return cls(task.input, task.metadata)

    def input_value(self) -> tuple[dict[str, Any], int]:
        """The input as an object, masked, and how many masks that took."""
        value, count = self.masker.value(self.submitted)
        return value, count

    def platform_keys(self) -> list[str]:
        """The platform's keys this task's metadata holds, in its order."""
        return [k for k in self.metadata if k in PLATFORM_METADATA_KEYS]

    def metadata_value(self) -> tuple[dict[str, Any], int]:
        """The metadata as an object: the caller's keys masked, the platform's as stored.

        In the stored order. The count is the caller's keys' masks only; a
        platform key is never counted, because it is never masked.
        """
        masked, count = self.masker.value(self.caller_metadata)
        caller_items = iter(masked.items())
        out: dict[str, Any] = {}
        for key, stored in self.metadata.items():
            if key in PLATFORM_METADATA_KEYS:
                out[key] = stored
            else:
                label, value = next(caller_items)
                out[label] = value
        return out, count


def _served(scrubbed: Redacted) -> dict[str, Any]:
    return {"text": scrubbed.text, "redaction_count": scrubbed.count}


def input_copy(task: Task, *, read_at: datetime) -> dict[str, Any]:
    """`GET /v1/tasks/{id}/input`: the task's input and metadata, masked, with their counts.

    `prompt` is `input.prompt` when it is a string; `rest` is the input
    without it, when anything else was submitted; `full` is the whole input.
    Each carries its own `redaction_count`, and the top-level count is
    `full`'s -- every mask in the input, counted once, which is `prompt`'s
    plus `rest`'s. `metadata` is the task's metadata as `GET /v1/tasks/{id}`
    serves it, from the same masker, with its own count and the platform keys
    it served as stored.
    """
    masking = TaskMasking.of(task)
    submitted = masking.submitted
    masker = masking.masker
    raw_prompt = submitted.get("prompt")
    if isinstance(raw_prompt, str):
        prompt_key = "string"
    elif "prompt" in submitted:
        prompt_key = "other"
    else:
        prompt_key = "missing"

    # One masker over the whole input, shared by every block below: the prompt
    # drawn alone and the prompt inside `full` are one masking, and what the
    # rest of the input names as a credential is masked in the prompt too.
    prompt = masker.text(raw_prompt) if isinstance(raw_prompt, str) else None
    rest = None
    if prompt_key == "string":
        others = {k: v for k, v in submitted.items() if k != "prompt"}
        if others:
            rest = masker.json(others)
    full = masker.json(submitted)
    metadata, metadata_count = masking.metadata_value()

    return {
        "task_id": task.id,
        "tenant_id": task.tenant_id,
        "read_at": read_at,
        "prompt_key": prompt_key,
        "prompt": _served(prompt) if prompt is not None else None,
        "rest": _served(rest) if rest is not None else None,
        "full": _served(full),
        "metadata": {
            "value": metadata,
            "redaction_count": metadata_count,
            "platform_keys": masking.platform_keys(),
        },
        "redacted": full.count > 0,
        "redaction_count": full.count,
        "redaction": {"applied_at_read_time": True, "rules": len(RULES)},
    }
