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
steps serve theirs from the SAME masker as their task (`codec.workflow_to_api`).

WHAT ELSE CARRIES THE INPUT, AND HOW EACH IS MASKED (the PR #229 review, which
found the claim "nothing serves the raw input" false as first written):

  * the text the task collected about itself -- `last_error` (the agent's
    stderr tail), `result_summary` (its summary text), each attempt's `error`
    and each event's `detail` -- by this masker's rules and literals, string by
    string, each with its count (`TaskMasking.text`, `.leaves`);
  * `repository_url`, whose userinfo is masked whole
    (`TaskMasking.repository_url`), and which submission now refuses to carry
    one at all (`validation.check_repository_url`);
  * `input.json` inside a checkpoint: no longer archived (`agent_worker.
    checkpoint`, which the worker rewrites at every attempt's prepare anyway),
    and one in an archive written before that is served by the file view
    through this masker (`checkpoint_content`);
  * the runner's `child started` line, which logged the prompt inside argv:
    it logs the prompt's length now (`runners/cliagent.py`), and `/logs`
    masks this task's literals in every window (`redaction.redact_lines`).

ONE PATH STILL SERVES IT AS STORED, and it is the owner's to rule on: the whole
checkpoint archive (`/checkpoints/{id}/content`), which the owner decided on
2026-09-24 to serve byte for byte, unredacted, and which holds `input.json` in
every archive written before this change. It is named in docs/agent-output.md.

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
serves raw. `workflow_step`, which `SubmissionService` writes on every step
task of a workflow, is the same case (the PR #229 review asked): not reserved,
so a plain task's caller can write it too, and it goes through the masker; a
step id is never credential-shaped, so it is served as written.

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

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any

from swarm_common.models import Task

from .redaction import MASK, RULES, JsonMasker, Redacted
from .validation import RESERVED_METADATA_KEYS, repository_userinfo

#: The metadata keys served as stored: only the platform can write them,
#: because submission refuses each from every caller. See the module docstring
#: for why `unit`, `source` and `origin` are not among them.
PLATFORM_METADATA_KEYS: tuple[str, ...] = RESERVED_METADATA_KEYS

#: Inside what the task collected about itself (`result_summary`, an event's
#: `detail`), the keys whose string values are identifiers a client looks
#: things up by, served as stored (`TaskMasking.leaves`): an artifact's `name`
#: and `uri`, a staged input's `filename` and `path` (the workflow graph matches
#: them against what was declared), and ids. Every other string is masked.
LOOKUP_KEYS = frozenset(
    {"name", "uri", "key", "filename", "path", "attempt_id", "task_id", "checkpoint_id"}
)


class TaskMasking:
    """ONE masker over a task's input and the metadata its caller wrote.

    Built once per served task, so a literal the metadata names as a
    credential is masked in the prompt, and one the prompt assigns
    (`DB_PASSWORD=<v>`) is masked in the metadata. The platform's own keys
    (`PLATFORM_METADATA_KEYS`) are outside it: they are neither masked nor a
    source of literals.

    The masked input and metadata are computed once and kept, so a masker that
    `masking_for` holds across requests pays for them once; every read hands
    out its own copy, so no caller can change what the next one is served.
    """

    def __init__(self, task_input: dict[str, Any] | None, metadata: dict[str, Any] | None) -> None:
        self.submitted: dict[str, Any] = dict(task_input or {})
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.caller_metadata: dict[str, Any] = {
            k: v for k, v in self.metadata.items() if k not in PLATFORM_METADATA_KEYS
        }
        self.masker = JsonMasker({"input": self.submitted, "metadata": self.caller_metadata})
        self._input: tuple[dict[str, Any], int] | None = None
        self._metadata: tuple[dict[str, Any], int] | None = None

    @classmethod
    def of(cls, task: Task) -> "TaskMasking":
        return cls(task.input, task.metadata)

    @property
    def literals(self) -> tuple[str, ...]:
        """What the input and the caller's metadata named as secret, longest first."""
        return self.masker.literals

    def input_value(self) -> tuple[dict[str, Any], int]:
        """The input as an object, masked, and how many masks that took."""
        if self._input is None:
            self._input = self.masker.value(self.submitted)
        value, count = self._input
        return copy.deepcopy(value), count

    def step_input_value(self, step_input: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
        """A workflow step's input, masked by its TASK's masker (the PR #229 review).

        The step's input is the input its task was created with, so this is the
        task's own masking of the same object: a literal the workflow's metadata
        names is masked in both copies of it that one response serves.
        """
        step = dict(step_input or {})
        if step == self.submitted:
            return self.input_value()
        return self.masker.value(step)

    def platform_keys(self) -> list[str]:
        """The platform's keys this task's metadata holds, in its order."""
        return [k for k in self.metadata if k in PLATFORM_METADATA_KEYS]

    def metadata_value(self) -> tuple[dict[str, Any], int]:
        """The metadata as an object: the caller's keys masked, the platform's as stored.

        In the stored order. The count is the caller's keys' masks only; a
        platform key is never counted, because it is never masked.
        """
        if self._metadata is None:
            masked, count = self.masker.value(self.caller_metadata)
            caller_items = iter(masked.items())
            out: dict[str, Any] = {}
            for key, stored in self.metadata.items():
                if key in PLATFORM_METADATA_KEYS:
                    out[key] = stored
                else:
                    label, value = next(caller_items)
                    out[label] = value
            self._metadata = (out, count)
        value, count = self._metadata
        return copy.deepcopy(value), count

    # -- what the task collected about itself (the PR #229 review) ----------
    #
    # `last_error` is the agent's stderr tail, an attempt's `error` and a
    # FAILED event's `detail.error` are the same string, and `result_summary`
    # carries the agent's own summary text. The worker scrubs them for the
    # secrets IT registered and nothing else, so a credential the prompt used
    # and the agent echoed was served in clear on every task route -- beside
    # the input that masked it. Each is masked here by the input's masker: the
    # rules, and every literal the input and metadata named.
    #
    # STRING BY STRING, NEVER BY KEY. These are documents the PLATFORM wrote:
    # a key named `credential` in an event's detail is the platform saying
    # which kind of credential a park waited for (`scheduler.credentials`), and
    # masking the value under it whole, as `JsonMasker` does for a caller's
    # document, would hide the answer the event exists to give.

    def text(self, value: str | None) -> tuple[str | None, int]:
        """One string the task collected, masked; None stays None."""
        if value is None:
            return None, 0
        masked = self.masker.text(str(value), remember=False)
        return masked.text, masked.count

    def leaves(self, value: Any) -> tuple[Any, int]:
        """A platform-written value with every string in it masked; keys and shape as stored.

        Except a string directly under one of `LOOKUP_KEYS`: those are the
        names and locations a client matches EXACTLY (an artifact's `name`
        against a staged file, a `uri`), which the artifact listing serves as
        stored anyway. The rules would take `eye-tracking-summary.md` for a
        JWT, and a masked name is an artifact nobody can find.
        """
        total = 0

        def walk(node: Any, key: str | None = None) -> Any:
            nonlocal total
            if isinstance(node, str):
                if key in LOOKUP_KEYS:
                    return node
                masked = self.masker.text(node, remember=False)
                total += masked.count
                return masked.text
            if isinstance(node, dict):
                return {name: walk(item, str(name)) for name, item in node.items()}
            if isinstance(node, (list, tuple)):
                return [walk(item) for item in node]
            return node

        return walk(value), total

    def repository_url(self, url: str | None) -> tuple[str | None, int]:
        """The repository URL with any credential-bearing userinfo masked whole.

        `validation.repository_userinfo` is the one definition, the one that
        refuses such a URL at submission; this masks the ones stored before
        that refusal existed. Then the rules and the task's literals, like any
        string the task carries.
        """
        if url is None:
            return None, 0
        count = 0
        span = repository_userinfo(url)
        if span is not None:
            start, end = span
            url = url[:start] + MASK + url[end:]
            count += 1
        masked, found = self.text(url)
        return masked, count + found


# --------------------------------------------------------------------------
# One masker per task, kept across requests (the PR #229 review)
# --------------------------------------------------------------------------
#
# WHY A CACHE. `GET /v1/tasks` masks every row's input, and the UI reads it
# with limit=200. `JsonMasker` over a 256 KiB input -- the largest submission
# accepts -- was measured at 57 ms with no credential in it and 69 ms with 256
# learned literals, so one page of large prompts cost about 11 s of CPU, under
# the GIL of an instance every tenant shares, on every refresh. Before masking
# the same page only serialised.
#
# EXACT, NOT APPROXIMATE. The masking is a function of the input and the
# metadata alone (the rules are code), and no writer changes either after
# `SubmissionService` creates the task. The key is not the task id alone,
# though: it carries a digest of both, so a document that DID change -- a
# fixture rewriting one, or a writer added tomorrow -- is a miss, never a stale
# answer. Hashing the input costs a serialisation, a small fraction of masking
# it.
#
# BOUNDED BY SIZE, not only by count: an entry holds the task's input and its
# masked copy, so it is charged a multiple of the input's serialised length,
# and the least recently used entries go first.

#: The cache's budget, in estimated bytes. An entry is charged three times its
#: input and metadata's serialised length (the input, its masked copy, the
#: masker's memo of each string) plus a fixed kilobyte.
MASKING_CACHE_MAX_BYTES = 32 * 1024 * 1024
#: And in entries, so a flood of tiny tasks cannot grow the dict without end.
MASKING_CACHE_MAX_ENTRIES = 4096


class MaskingCache:
    """An LRU of `TaskMasking` by (tenant, task, digest of input and metadata)."""

    def __init__(self, *, max_bytes: int, max_entries: int) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str, str], tuple[TaskMasking, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self.hits = self.misses = 0

    def masking_for(self, task: Task) -> TaskMasking:
        text = json.dumps([task.input, task.metadata], ensure_ascii=False, default=str)
        digest = hashlib.blake2b(
            text.encode("utf-8", errors="surrogatepass"), digest_size=16
        ).hexdigest()
        key = (task.tenant_id, task.id, digest)
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return found[0]
            self.misses += 1
        # Built outside the lock: two requests for the same task may both build
        # one, and the second put replaces the first. Either is correct.
        masking = TaskMasking.of(task)
        # The first read of each pays for both here, not on a later request.
        masking.input_value()
        masking.metadata_value()
        size = 3 * len(text) + 1024
        if size > self._max_bytes:
            return masking
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._entries[key] = (masking, size)
            self._bytes += size
            while self._entries and (
                self._bytes > self._max_bytes or len(self._entries) > self._max_entries
            ):
                _, (_, dropped) = self._entries.popitem(last=False)
                self._bytes -= dropped
        return masking


#: The process's one cache. Per instance, which is all it needs to be: a miss
#: costs one masking, never a wrong answer.
MASKING_CACHE = MaskingCache(
    max_bytes=MASKING_CACHE_MAX_BYTES, max_entries=MASKING_CACHE_MAX_ENTRIES
)


def masking_for(task: Task) -> TaskMasking:
    """The task's one masker, from `MASKING_CACHE` when the same document was masked before."""
    return MASKING_CACHE.masking_for(task)


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
    masking = masking_for(task)
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
