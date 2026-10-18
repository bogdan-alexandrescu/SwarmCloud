"""An await-input step's answer, its expiry and its view (docs/design/human-steps.md §4.2-§4.6).

Lane HW-API-ANSWER. A workflow step whose runner profile is `await-input`
(`control_plane`, contract request 93) runs no container. The scheduler parks
it PARKED(AWAITING_INPUT) with `metadata.await_input.opened_at` and
`expires_at` (lane HW-SCHED) and never leases it. Everything that ENDS it is
here, in swarm-api, because swarm-api is the only service that sees the
answer arrive and the only one that may write it to the bucket
(`inputwriter`):

  * `answer`: a tenant member's answer, from the console or the MCP bridge.
    PARKED -> SUCCEEDED, with the form and its files as the step's artifacts;
  * `expire_due`: the schedule tick's sweep. PARKED -> DEAD_LETTERED under
    `on_expire: "fail"`, PARKED -> SUCCEEDED with the signed default under
    `on_expire: "default"`;
  * a cancel is `Store.request_cancel_with_target`, unchanged: it ends a task
    that holds no capacity directly, in a transaction on its current state.

WHO MAY ANSWER (§4.2, the owner's Q2). Any member of the step's tenant, and
only a person: `auth.person_refusal` refuses every service account and the
platform's own jobs, so a workflow's own agent -- whose MCP bridge speaks as
its worker -- can answer nothing. Membership is asked of the directory AGAIN
for this request, past the cache (`Authenticator.is_tenant_member(fresh=True)`),
because a wait of up to a day must not outlive a removal from the group by
more than one Cloud Identity round trip. A failed lookup is a 503 and
nothing is written. There is no admin bypass: `is_admin` is never read.

ONE WINNER (§4.6). The answer, the expiry and a cancel each end the step in a
transaction that commits only on PARKED + AWAITING_INPUT + no recorded answer
+ no cancel requested. Firestore serialises them, so exactly one commits. The
objects are created BEFORE that transaction, under a fresh `input-<nonce>`
attempt, so a loser leaves objects that no manifest names -- never staged,
deleted with the task's prefix by the purge and the retention rule.

THE CODES. Every refusal carries the design's name in `detail.reason`
(`person_required`, `not_a_member`, `not_open`, `already_answered`,
`file_contains_credential`, ...) under an ESTABLISHED `code` -- `forbidden`,
`conflict`, `validation_failed`, `rate_limited` -- with the design's HTTP
status (403, 404, 409, 411, 413, 415, 422, 429). A new 4xx `code` ships behind
a report-only switch (`swarm_api.refusals`), and a switched-off
`person_required` would let an agent answer its own review; the established
code is the precedent HW-API-SUBMIT set for the same feature
(`validation.AwaitInputRefused`). The two 503s are new codes, which the
switch rule does not reach: `membership_unavailable`, `store_unavailable`.

NOTHING OF AN ANSWER IS WRITTEN INTO FIRESTORE BUT ITS RECORD: who, when, how,
sizes and digests. A task document holds 1 MiB, and an answer up to 16.

DARK UNTIL REQUEST D. While `await-input` is unavailable no such step can be
submitted, so the routes find nothing to answer, and `expire_due` does not
even query: the schedule tick behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import TaskEvent, new_id
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import EventType, ParkReason, TaskState
from swarm_redaction import KEY_VALUE, RULES, mask_code_literals, redact

from .agent_output import AgentOutputService
from .agent_questions import read_questions
from .auth import AuthContext, person_refusal
from .codec import event_to_firestore
from .errors import (
    ApiError,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from .inputwriter import (
    InputWriteFailed,
    InputWriter,
    NAME_PATTERN,
    attachments_dir,
    input_key,
    new_input_attempt,
)
from .validation import (
    AWAIT_ANSWER_MAX_TEXT_CHARS,
    AWAIT_INPUT_FIELD,
    AWAIT_INPUT_METADATA_KEY,
    AWAIT_INPUT_PROFILE,
    AWAIT_VERDICT_MAX_FINDING_CHARS,
    AWAIT_VERDICT_MAX_FINDINGS,
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    REVIEW_VERDICTS,
)
from .verdictpublish import Decline, parse_verdict, spec_refusal
from .waker import TASK_FINISHED, ring

log = logging.getLogger(__name__)

TASKS = "tasks"
WORKFLOWS = "workflows"
EVENTS = "events"

#: The whole answer, form plus every file: the owner's number (Q6). It is
#: what swarm-api holds in memory for one answer, and well inside the
#: worker's 512 MiB staging total.
MAX_ANSWER_BYTES = 16 * 1024 * 1024
#: The form, serialised: `max_input_bytes`, the bound on a task's own input.
MAX_FORM_BYTES = 256 * 1024
#: A `text` form's text (§4.6).
MAX_TEXT_BYTES = 64 * 1024
#: What multipart framing may add to a 16 MiB answer (§4.3 step 1).
MULTIPART_FRAMING_BYTES = 64 * 1024
#: A JSON-only answer's body: the form and room for its envelope.
MAX_JSON_BODY_BYTES = MAX_FORM_BYTES + 4 * 1024
#: Multipart answers one instance holds at once (§4.3 step 3). swarm-api runs
#: 1 GiB at concurrency 80; four 16 MiB bodies with the one copy
#: `routes.await_input.parse_multipart` makes of their parts are at most
#: 128 MiB, whatever the bytes are (a test holds the parser to that copy).
MAX_CONCURRENT_MULTIPART = 4
#: Steps the expiry sweep ends per tick; the next tick takes the rest.
EXPIRE_LIMIT = 200
#: Waiting steps one list read returns at most.
LIST_MAX = 200

#: How an answer arrived. GitHub is a later lane (HW-GITHUB, Q3).
VIA = ("console", "mcp")

ANSWER_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_FILE_NAME_RE = re.compile(rf"^{NAME_PATTERN}$")

#: Text files: a closed list of extensions, the bytes UTF-8 with no NUL
#: (§4.3). `.json` is stored as JSON; every other one as plain text.
TEXT_EXTENSIONS = (
    ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".diff", ".patch", ".py",
    ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".sh", ".html", ".css", ".sql",
    ".toml",
)
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
JSON_CONTENT_TYPE = "application/json"
#: Binary types, by magic number, each with the extensions its name may end
#: in. A PNG named `.md` is refused: the name is what the next agent reads.
BINARY_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("application/pdf", (".pdf",)),
    ("image/png", (".png",)),
    ("image/jpeg", (".jpg", ".jpeg")),
    ("image/webp", (".webp",)),
)

#: The answer's record names this as who wrote the artifacts.
PUBLISHED_BY = "control_plane"

#: Statuses of a step, as the GET route and the 409s name them (§4.6).
WAITING = "waiting"
NOT_OPEN = "not_open"
ANSWERED = "answered"
EXPIRED = "expired"
CANCELLED = "cancelled"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------

class AnswerTooLarge(ValidationFailed):
    """413: the answer, or its form, is over its bound. `detail.reason` says which."""

    status_code = 413


class LengthRequired(ValidationFailed):
    """411: a multipart answer without `Content-Length`."""

    status_code = 411


class FileTypeRefused(ValidationFailed):
    """415: a file whose sniffed bytes are not on the closed list, or whose name lies."""

    status_code = 415


class MembershipUnavailable(ApiError):
    """503: the directory did not answer, so nobody is let in and nothing is written."""

    status_code = 503
    code = "membership_unavailable"


class StoreUnavailable(ApiError):
    """503: the bucket did not create an object. Firestore is untouched; retry."""

    status_code = 503
    code = "store_unavailable"


def _detail(reason: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"reason": reason, **dict(extra or {})}


def forbidden(reason: str, message: str) -> Forbidden:
    return Forbidden(message, detail=_detail(reason))


def conflict(reason: str, message: str, **extra: Any) -> Conflict:
    return Conflict(message, detail=_detail(reason, extra))


def invalid(reason: str, message: str, **extra: Any) -> ValidationFailed:
    return ValidationFailed(message, detail=_detail(reason, extra))


def too_large(message: str) -> AnswerTooLarge:
    return AnswerTooLarge(message, detail=_detail("answer_too_large",
                                                  {"max_answer_bytes": MAX_ANSWER_BYTES}))


def busy(retry_after_seconds: float = 2.0) -> RateLimited:
    refused = RateLimited(
        f"{MAX_CONCURRENT_MULTIPART} answers with files are being received here already; "
        "retry shortly",
        retry_after_seconds,
    )
    refused.detail = _detail("answer_busy", {"retry_after_seconds": retry_after_seconds})
    return refused


# --------------------------------------------------------------------------
# the step
# --------------------------------------------------------------------------

def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def is_await_profile(runner_profile: Any) -> bool:
    profile = RUNNER_PROFILES.get(str(runner_profile or ""))
    return profile is not None and bool(getattr(profile, "control_plane", False))


def declaration(doc: Mapping[str, Any]) -> dict[str, Any]:
    """`metadata.dispatch.await_input`: SIGNED. Read only after `spec_refusal` passes."""
    dispatch = _mapping(_mapping(doc.get("metadata")).get(DISPATCH_METADATA_KEY))
    return _mapping(dispatch.get(AWAIT_INPUT_FIELD))


def window(doc: Mapping[str, Any]) -> dict[str, Any]:
    """`metadata.await_input`: the park's window and, once answered, the record."""
    return _mapping(_mapping(doc.get("metadata")).get(AWAIT_INPUT_METADATA_KEY))


@dataclass(frozen=True)
class Step:
    workflow_id: str
    step_id: str
    task_id: str
    tenant_id: str
    doc: dict[str, Any]
    #: {task id: step id} of the workflow, for naming what the step shows.
    step_ids: dict[str, str]


def load_step(db: Any, tenant_id: str, workflow_id: str, step_id: str) -> Step:
    """The await-input step, read inside `tenant_id`. Anything else is one 404.

    A workflow or step that does not exist, belongs to another tenant, or is
    not an await-input step is the same answer, so a caller learns nothing
    about another tenant's workflows (invariant 9).
    """
    missing = NotFound(f"workflow {workflow_id!r} has no step {step_id!r} waiting for input")
    snap = db.collection(WORKFLOWS).document(workflow_id).get()
    workflow = (snap.to_dict() or {}) if snap.exists else None
    if workflow is None or workflow.get("tenant_id") != tenant_id:
        raise missing
    step_ids: dict[str, str] = {}
    task_id = ""
    for step in workflow.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        if step.get("task_id"):
            step_ids[str(step["task_id"])] = str(step.get("step_id") or "")
        if step.get("step_id") == step_id and step.get("task_id"):
            task_id = str(step["task_id"])
    if not task_id:
        raise missing
    task_snap = db.collection(TASKS).document(task_id).get()
    doc = (task_snap.to_dict() or {}) if task_snap.exists else None
    if (
        doc is None
        or doc.get("tenant_id") != tenant_id
        or doc.get("workflow_id") != workflow_id
        or not is_await_profile(doc.get("runner_profile"))
    ):
        raise missing
    return Step(workflow_id, step_id, task_id, tenant_id, {**doc, "id": task_id}, step_ids)


def status_of(doc: Mapping[str, Any]) -> str:
    """Where the step is, in the five words of §4.6."""
    state = doc.get("state")
    held = window(doc)
    if held.get("answer"):
        return ANSWERED
    if state == TaskState.PARKED.value and doc.get("park_reason") == ParkReason.AWAITING_INPUT.value:
        return CANCELLED if doc.get("cancel_requested") else WAITING
    if state == TaskState.CANCELLED.value:
        return CANCELLED
    if held.get("expired") or state in (TaskState.DEAD_LETTERED.value, TaskState.SUCCEEDED.value):
        return EXPIRED
    return NOT_OPEN


def _closed(step: Step, status: str) -> Conflict:
    """The 409 for a step that is not waiting, saying what it is instead."""
    if status == ANSWERED:
        record = _mapping(window(step.doc).get("answer"))
        who = record.get("answered_by")
        return conflict(
            "already_answered",
            f"step {step.step_id!r} was already answered by {who} at "
            f"{_iso(record.get('answered_at'))}; the first answer is final, and a "
            "correction is a new round",
            answered_by=who, answered_at=_iso(record.get("answered_at")),
            via=record.get("via"),
        )
    if status == NOT_OPEN:
        return conflict(
            "not_open",
            f"step {step.step_id!r} is not waiting for an answer yet: the steps it "
            "depends on have not all finished",
            state=step.doc.get("state"),
        )
    if status == EXPIRED:
        return conflict(
            "expired", f"step {step.step_id!r} expired before it was answered",
            state=step.doc.get("state"),
        )
    return conflict(
        "cancelled", f"step {step.step_id!r} was cancelled", state=step.doc.get("state"),
    )


# --------------------------------------------------------------------------
# who may answer
# --------------------------------------------------------------------------

def member_refusal(ctx: Any, auth: AuthContext, tenant_id: str) -> None:
    """Raise unless the caller is a person and, asked freshly, a member of `tenant_id`.

    A group tenant asks Cloud Identity about its ONE registered group, past
    the cache; a personal tenant (`u-<slug>`) is its owner's address and
    nobody else's. 403 `person_required` / `not_a_member`, 503
    `membership_unavailable`. Nothing is written by any of them.
    """
    why = person_refusal(auth)
    if why is not None:
        raise forbidden("person_required", why)
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise forbidden("not_a_member", f"you are not a member of tenant {tenant_id!r}")
    try:
        member = ctx.authenticator.is_tenant_member(auth.email, tenant, fresh=True)
    except UpstreamUnavailable:
        raise MembershipUnavailable(
            "your membership of this tenant could not be confirmed just now, so the "
            "answer was not taken; retry shortly",
            detail=_detail("membership_unavailable"),
        ) from None
    if not member:
        raise forbidden(
            "not_a_member",
            f"you are not a member of tenant {tenant_id!r}, the step's tenant, so you "
            "may not answer it",
        )


def may_answer(ctx: Any, auth: AuthContext, tenant_id: str) -> bool:
    """The display's answer to "may I answer?", from the CACHED membership.

    The POST decides freshly (`member_refusal`); this only decides whether
    the console offers the form.
    """
    if person_refusal(auth) is not None:
        return False
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        return False
    try:
        return bool(ctx.authenticator.is_tenant_member(auth.email, tenant))
    except UpstreamUnavailable:
        return False


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------

def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def _form_bytes(value: Any) -> bytes:
    """The artifact's bytes: the form as a dependant reads it, one JSON object."""
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise invalid("invalid_payload", f"{where} must be a list of strings", position=where)
    return list(value)


def _questions_of(ctx: Any, step: Step) -> list[dict[str, Any]]:
    """The upstream agent's questions, as the worker validated them (Q11)."""
    staged = _mapping(_mapping(step.doc.get("metadata")).get(INPUT_FROM_METADATA_KEY))
    upstream = [task_id for task_id, name in staged.items() if name == "questions.json"]
    if len(upstream) != 1:
        raise conflict("questions_unavailable",
                       "the step stages no questions.json, so it has no questions to answer")
    read = read_questions(ctx.store, AgentOutputService(ctx.inspection), step.tenant_id,
                          upstream[0], submitted_by=None)
    if read.get("status") != "ok":
        raise conflict(
            "questions_unavailable",
            "the questions this step asks could not be read back "
            f"({read.get('status')}); nothing was taken",
        )
    return list(read.get("questions") or [])


def validate_form(form: str, payload: Any, *, questions: Sequence[Mapping[str, Any]] = ()
                  ) -> dict[str, Any]:
    """The payload, checked against `form` and normalised (§4.6). Not yet redacted.

    A refusal names the position and the key, never quotes the payload.
    """
    if not isinstance(payload, dict):
        raise invalid("invalid_payload", "payload must be a JSON object", position="payload")
    try:
        size = len(_canonical(payload))
    except (TypeError, ValueError):
        raise invalid("invalid_payload", "payload is not plain JSON", position="payload") from None
    if size > MAX_FORM_BYTES:
        raise AnswerTooLarge(
            f"the form is {size} bytes; at most {MAX_FORM_BYTES} are taken",
            detail=_detail("answer_too_large", {"max_form_bytes": MAX_FORM_BYTES}),
        )
    allowed = {"verdict": {"verdict", "findings"}, "answers": {"answers"}, "text": {"text"}}[form]
    extra = sorted(set(payload) - allowed)
    if extra:
        raise invalid("invalid_payload",
                      f"a {form!r} answer takes only {sorted(allowed)}, not {extra}",
                      position="payload", keys=extra)
    if form == "verdict":
        verdict = payload.get("verdict")
        if verdict not in REVIEW_VERDICTS:
            raise invalid("invalid_payload",
                          f"payload.verdict must be one of {list(REVIEW_VERDICTS)}",
                          position="payload.verdict")
        findings = _string_list(payload.get("findings", []), "payload.findings")
        if len(findings) > AWAIT_VERDICT_MAX_FINDINGS:
            raise invalid("invalid_payload",
                          f"payload.findings has {len(findings)} entries; at most "
                          f"{AWAIT_VERDICT_MAX_FINDINGS}", position="payload.findings")
        for index, finding in enumerate(findings):
            if len(finding) > AWAIT_VERDICT_MAX_FINDING_CHARS:
                raise invalid("invalid_payload",
                              f"payload.findings[{index}] is over "
                              f"{AWAIT_VERDICT_MAX_FINDING_CHARS} characters",
                              position=f"payload.findings[{index}]")
        return {"verdict": verdict, "findings": findings}
    if form == "text":
        text = payload.get("text")
        if not isinstance(text, str):
            raise invalid("invalid_payload", "payload.text must be a string", position="payload.text")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise invalid("invalid_payload", f"payload.text is over {MAX_TEXT_BYTES} bytes",
                          position="payload.text")
        return {"text": text}
    answers = payload.get("answers")
    if not isinstance(answers, list):
        raise invalid("invalid_payload", "payload.answers must be a list", position="payload.answers")
    by_index: dict[int, dict[str, Any]] = {}
    for position, answer in enumerate(answers):
        where = f"payload.answers[{position}]"
        if not isinstance(answer, dict):
            raise invalid("invalid_payload", f"{where} must be an object", position=where)
        unknown = sorted(set(answer) - {"question_index", "choice", "text"})
        if unknown:
            raise invalid("invalid_payload",
                          f"{where} takes question_index, choice and text, not {unknown}",
                          position=where, keys=unknown)
        index, choice, text = answer.get("question_index"), answer.get("choice"), answer.get("text")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(questions):
            raise invalid("invalid_payload",
                          f"{where}.question_index must name one of the {len(questions)} questions",
                          position=f"{where}.question_index")
        if index in by_index:
            raise invalid("invalid_payload", f"{where} answers question {index} a second time",
                          position=f"{where}.question_index")
        labels = [o.get("label") for o in questions[index].get("options") or []
                  if isinstance(o, Mapping) and isinstance(o.get("label"), str)]
        if choice is not None and choice not in labels:
            raise invalid("invalid_payload",
                          f"{where}.choice must be one of question {index}'s option labels, or null",
                          position=f"{where}.choice")
        if text is not None and not isinstance(text, str):
            raise invalid("invalid_payload", f"{where}.text must be a string or null",
                          position=f"{where}.text")
        if choice is None and not (text or "").strip():
            raise invalid("invalid_payload", f"{where} needs a choice or a text", position=where)
        if text is not None and len(text) > AWAIT_ANSWER_MAX_TEXT_CHARS:
            raise invalid("invalid_payload",
                          f"{where}.text is over {AWAIT_ANSWER_MAX_TEXT_CHARS} characters",
                          position=f"{where}.text")
        by_index[index] = {"question_index": index, "choice": choice, "text": text}
    unanswered = [i for i in range(len(questions)) if i not in by_index]
    if unanswered:
        raise invalid("invalid_payload",
                      f"questions {unanswered} are not answered; every question must be",
                      position="payload.answers", unanswered=unanswered)
    return {"answers": [by_index[i] for i in sorted(by_index)]}


def redact_form(value: Any) -> tuple[Any, int]:
    """Every string in the form through the shared rules, decoded (§4.3)."""
    if isinstance(value, str):
        masked = redact(value, decoded=True)
        return masked.text, masked.count
    if isinstance(value, list):
        out, count = [], 0
        for item in value:
            masked, found = redact_form(item)
            out.append(masked)
            count += found
        return out, count
    if isinstance(value, dict):
        out_map, count = {}, 0
        for key, item in value.items():
            masked, found = redact_form(item)
            out_map[key] = masked
            count += found
        return out_map, count
    return value, 0


def _gate_reads(form: str, data: bytes) -> None:
    """A verdict form must read as a verdict with the parser a `when` gate uses."""
    if form != "verdict":
        return
    try:
        parse_verdict(data.decode("utf-8"))
    except Decline:
        raise invalid("invalid_payload", "the verdict does not read as one a gate accepts",
                      position="payload.verdict") from None


# --------------------------------------------------------------------------
# the files
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AnswerFile:
    #: As the manifest names it: `<artifact stem>.files/<file>`.
    name: str
    data: bytes
    content_type: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def _extension(name: str) -> str:
    return "." + name.rsplit(".", 1)[1].lower() if "." in name[1:] else ""


def sniff(name: str, data: bytes) -> str:
    """The stored content type, from the BYTES, never the client's header (§4.3).

    Raises 415 for everything off the closed list: archives and executables
    on purpose (the scan cannot see inside one), and a name whose extension
    disagrees with its bytes.
    """
    extension = _extension(name)
    kind: str | None = None
    if data.startswith(b"%PDF-"):
        kind = "application/pdf"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "image/webp"
    if kind is not None:
        extensions = dict(BINARY_TYPES)[kind]
        if extension in extensions:
            return kind
        raise FileTypeRefused(
            f"file {name!r} is {kind} by its bytes, which a name ending {extension or '(none)'} "
            f"does not describe; name it {' or '.join(extensions)}",
            detail=_detail("file_type_refused", {"file": name}),
        )
    if extension in TEXT_EXTENSIONS and b"\x00" not in data:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return JSON_CONTENT_TYPE if extension == ".json" else TEXT_CONTENT_TYPE
    raise FileTypeRefused(
        f"file {name!r} is not a file this step takes: UTF-8 text with one of "
        f"{', '.join(TEXT_EXTENSIONS)}, a PDF, or a PNG, JPEG or WebP image. "
        "Archives and executables are refused.",
        detail=_detail("file_type_refused", {"file": name}),
    )


def _rule_masks(rule: Any, text: str) -> str:
    if rule is KEY_VALUE:
        text, _ = mask_code_literals(text)
    if rule.apply is not None:
        masked, _ = rule.apply(text, False, False)
        return masked
    return rule.pattern.sub(r"\1********", text)


def credential_hit(text: str) -> tuple[int, str] | None:
    """(line, rule) of the first credential the shared rules find, or None.

    The rules `swarm_redaction.redact` masks with, one family at a time, so
    the refusal can say which family and where without ever quoting what
    matched (§4.3, "refused on a match, never rewritten").
    """
    for rule in RULES:
        masked = _rule_masks(rule, text)
        if masked == text:
            continue
        at = next((i for i, (a, b) in enumerate(zip(text, masked)) if a != b),
                  min(len(text), len(masked)))
        return text.count("\n", 0, at) + 1, rule.name
    return None


def check_files(
    raw: Sequence[tuple[str, bytes]], *, artifact: str, max_files: int
) -> list[AnswerFile]:
    """Every file's name, count, type and content checked; any refusal writes nothing."""
    if len(raw) > max_files:
        raise invalid(
            "too_many_files",
            f"the answer carries {len(raw)} files; this step takes at most {max_files}",
            max_files=max_files,
        )
    directory = attachments_dir(artifact)
    seen: set[str] = set()
    checked: list[AnswerFile] = []
    for filename, data in raw:
        if not isinstance(filename, str) or not _FILE_NAME_RE.match(filename):
            raise invalid(
                "invalid_file_name",
                "a file's name must be letters, digits, '_', '.' and '-', at most 128 "
                "characters, starting with a letter or a digit, with no directory",
            )
        if filename.lower() in seen:
            raise invalid("invalid_file_name", f"two files are named {filename!r}", file=filename)
        seen.add(filename.lower())
        content_type = sniff(filename, data)
        if content_type in (TEXT_CONTENT_TYPE, JSON_CONTENT_TYPE):
            hit = credential_hit(data.decode("utf-8"))
            if hit is not None:
                line, rule = hit
                raise invalid(
                    "file_contains_credential",
                    f"file {filename!r} holds what looks like a credential on line {line} "
                    f"({rule}). Files go to the next agent byte for byte, so it is refused, "
                    "not masked: remove it and send the answer again.",
                    file=filename, line=line, rule=rule,
                )
        checked.append(AnswerFile(
            name=f"{directory}/{filename}", data=data, content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
        ))
    return checked


# --------------------------------------------------------------------------
# the one transaction
# --------------------------------------------------------------------------

def _open(data: Mapping[str, Any] | None, tenant_id: str) -> bool:
    """The step-8 guard: still waiting, unanswered, uncancelled, this tenant's."""
    return (
        data is not None
        and data.get("tenant_id") == tenant_id
        and data.get("state") == TaskState.PARKED.value
        and data.get("park_reason") == ParkReason.AWAITING_INPUT.value
        and not window(data).get("answer")
        and not data.get("cancel_requested")
    )


def _end(db: Any, task_id: str, tenant_id: str, *, now: datetime, state: TaskState,
         window_update: Mapping[str, Any], fields: Mapping[str, Any],
         event_type: EventType, event_detail: Mapping[str, Any]) -> bool:
    """End the waiting step in one guarded transaction, with its event. True if it committed."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if not _open(data, tenant_id):
            return False
        metadata = _mapping(data.get("metadata"))
        metadata[AWAIT_INPUT_METADATA_KEY] = {**window(data), **dict(window_update)}
        txn.update(ref, {
            "state": state.value,
            "park_reason": None,
            "next_eligible_at": None,
            "blocked_by": [],
            "current_lease_id": None,
            "completed_at": now,
            "updated_at": now,
            "metadata": metadata,
            **dict(fields),
        })
        event = TaskEvent(
            event_id=new_id("ev"), task_id=task_id, tenant_id=tenant_id, type=event_type,
            at=now, detail=dict(event_detail),
        )
        txn.set(ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
        return True

    return bool(_apply(transaction))


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _artifact_entry(name: str, data: bytes, uri: str, content_type: str,
                    files: Sequence[tuple[AnswerFile, str]]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "uri": uri, "content_type": content_type,
    }
    if files:
        entry["attachments"] = [
            {"name": f.name, "bytes": f.size, "sha256": f.sha256,
             "content_type": f.content_type, "uri": file_uri}
            for f, file_uri in files
        ]
    return entry


def _write_objects(writer: InputWriter, step_tenant: str, task_id: str, artifact: str,
                   form: bytes, files: Sequence[AnswerFile]) -> tuple[str, list[tuple[AnswerFile, str]]]:
    """The files first, then the form: an answer whose form exists has its files."""
    attempt = new_input_attempt()
    written: list[tuple[AnswerFile, str]] = []
    try:
        for f in files:
            written.append((f, writer.create(input_key(step_tenant, task_id, attempt, f.name),
                                             f.data, f.content_type)))
        uri = writer.create(input_key(step_tenant, task_id, attempt, artifact), form,
                            JSON_CONTENT_TYPE)
    except InputWriteFailed as exc:
        log.warning("await-input %s: the answer's objects were not created: %s", task_id, exc)
        raise StoreUnavailable(
            "the answer could not be stored just now; nothing was recorded, so sending it "
            "again is safe",
            detail=_detail("store_unavailable"),
        ) from None
    return uri, written


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------

def _record_files(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(str(f.get("name")), str(f.get("sha256"))) for f in record.get("files") or []
            if isinstance(f, Mapping)]


def _response(step: Step, doc: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
    record = _mapping(window(doc).get("answer"))
    artifacts = (_mapping(doc.get("result_summary")).get("artifacts") or [{}])
    entry = _mapping(artifacts[0]) if artifacts else {}
    return {
        "workflow_id": step.workflow_id,
        "step_id": step.step_id,
        "task_id": step.task_id,
        "state": doc.get("state"),
        "artifact": {"name": entry.get("name"), "bytes": entry.get("bytes"),
                     "sha256": entry.get("sha256")},
        "files": [{"name": a.get("name"), "bytes": a.get("bytes"), "sha256": a.get("sha256")}
                  for a in entry.get("attachments") or [] if isinstance(a, Mapping)],
        "answered_by": record.get("answered_by"),
        "answered_at": _iso(record.get("answered_at")),
        "via": record.get("via"),
        "redaction_count": record.get("redaction_count", 0),
        "replayed": replayed,
    }


def _reread(db: Any, step: Step) -> Step:
    snap = db.collection(TASKS).document(step.task_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    return Step(step.workflow_id, step.step_id, step.task_id, step.tenant_id,
                {**doc, "id": step.task_id}, step.step_ids)


def _replay_or_conflict(step: Step, answer_id: str, sha256: str,
                        files: Sequence[AnswerFile]) -> dict[str, Any]:
    """The original 200 for the same answer sent again; the 409 for any other."""
    status = status_of(step.doc)
    record = _mapping(window(step.doc).get("answer"))
    if (
        status == ANSWERED
        and record.get("answer_id") == answer_id
        and record.get("sha256") == sha256
        and _record_files(record) == [(f.name, f.sha256) for f in files]
    ):
        return _response(step, step.doc, replayed=True)
    raise _closed(step, status)


def answer(
    ctx: Any,
    writer: InputWriter | None,
    auth: AuthContext,
    *,
    tenant_id: str,
    workflow_id: str,
    step_id: str,
    body: Any,
    files: Sequence[tuple[str, bytes]] = (),
) -> dict[str, Any]:
    """§4.6 steps 2-9: read, check, validate, write, commit, wake.

    `body` is the parsed `{answer_id, payload, via?}`; `files` are the
    multipart `file` parts as (filename, bytes). The size gate (steps 1 and
    3) is the route's, before the body is read.
    """
    db = ctx.store.db
    step = load_step(db, tenant_id, workflow_id, step_id)            # 2: 404
    member_refusal(ctx, auth, tenant_id)                             # 3: 403 / 503

    if not isinstance(body, dict):
        raise invalid("invalid_payload", "the answer must be a JSON object", position="answer")
    unknown = sorted(set(body) - {"answer_id", "payload", "via"})
    if unknown:
        raise invalid("invalid_payload", f"the answer takes answer_id, payload and via, not {unknown}",
                      position="answer", keys=unknown)
    answer_id = body.get("answer_id")
    if not isinstance(answer_id, str) or not ANSWER_ID_RE.match(answer_id):
        raise invalid("invalid_payload", "answer_id must be 1-64 of a-z, 0-9 and '-'",
                      position="answer_id")
    via = body.get("via", "console")
    if via not in VIA:
        raise invalid("invalid_payload", f"via must be one of {list(VIA)}", position="via")

    status = status_of(step.doc)                                     # 4: 409
    replaying = status == ANSWERED and _mapping(
        window(step.doc).get("answer")).get("answer_id") == answer_id
    if status != WAITING and not replaying:
        raise _closed(step, status)

    refusal = spec_refusal(ctx.settings, step.doc, step.task_id)     # 5: the signature
    if refusal is not None:
        log.warning("await-input %s: spec refused: %s", step.task_id, refusal)
        raise conflict("spec_refused", f"the step cannot be answered: {refusal}")
    block = declaration(step.doc)
    form = block.get("form")
    artifact = block.get("artifact")
    max_files = block.get("max_files")
    if form not in ("verdict", "answers", "text") or not isinstance(artifact, str) or \
            isinstance(max_files, bool) or not isinstance(max_files, int):
        raise conflict("spec_refused", "the step's await_input declaration is not one this "
                                       "platform wrote")

    questions = _questions_of(ctx, step) if form == "answers" else ()
    normalised = validate_form(form, body.get("payload"), questions=questions)  # 6
    masked, redaction_count = redact_form(normalised)
    form_data = _form_bytes(masked)
    _gate_reads(form, form_data)
    checked = check_files(files, artifact=artifact, max_files=max_files)
    total = len(form_data) + sum(f.size for f in checked)
    if total > MAX_ANSWER_BYTES:
        raise too_large(f"the answer is {total} bytes; at most {MAX_ANSWER_BYTES} are taken")
    sha256 = hashlib.sha256(form_data).hexdigest()

    if replaying:
        return _replay_or_conflict(step, answer_id, sha256, checked)

    uri, written = _write_objects(writer_or_refuse(writer), tenant_id, step.task_id,
                                  artifact, form_data, checked)          # 7

    now = ctx.now()
    opened_at = window(step.doc).get("opened_at")
    wait_seconds = (
        max(0, int((now - opened_at).total_seconds())) if isinstance(opened_at, datetime) else None
    )
    entry = _artifact_entry(artifact, form_data, uri, JSON_CONTENT_TYPE, written)
    record = {
        "answer_id": answer_id, "answered_by": auth.email, "answered_at": now, "via": via,
        "sha256": sha256, "bytes": total, "redaction_count": redaction_count,
        "files": [{"name": f.name, "bytes": f.size, "sha256": f.sha256,
                   "content_type": f.content_type} for f in checked],
    }
    summary = {
        "artifacts": [entry],
        "artifact_bytes": total,
        "published_by": PUBLISHED_BY,
        "input": {"source": "person", "answered_by": auth.email, "answered_at": now,
                  "via": via, "wait_seconds": wait_seconds, "files": len(checked)},
    }
    committed = _end(                                                # 8: one winner
        db, step.task_id, tenant_id, now=now, state=TaskState.SUCCEEDED,
        window_update={"answer": record},
        fields={"result_summary": summary, "last_error": None, "end_cause": None},
        event_type=EventType.SUCCEEDED,
        event_detail={"published_by": PUBLISHED_BY, "source": "person",
                      "answered_by": auth.email, "via": via, "files": len(checked)},
    )
    after = _reread(db, step)
    if not committed:
        return _replay_or_conflict(after, answer_id, sha256, checked)
    log.info("await-input %s tenant=%s answered by %s via %s (%d files)",
             step.task_id, tenant_id, auth.email, via, len(checked))
    ring(ctx.waker, ctx.metrics, TASK_FINISHED, task_id=step.task_id,   # 9
         tenant_id=tenant_id, state=TaskState.SUCCEEDED.value)
    return _response(after, after.doc, replayed=False)


def writer_or_refuse(writer: InputWriter | None) -> InputWriter:
    if writer is None:
        raise StoreUnavailable(
            "no artifact store is configured for this deployment, so no answer can be stored",
            detail=_detail("store_unavailable"),
        )
    return writer


# --------------------------------------------------------------------------
# what the card, the bridge and the inbox read
# --------------------------------------------------------------------------

def _public_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "answer_id": record.get("answer_id"),
        "answered_by": record.get("answered_by"),
        "answered_at": _iso(record.get("answered_at")),
        "via": record.get("via"),
        "sha256": record.get("sha256"),
        "bytes": record.get("bytes"),
        "files": [{"name": f.get("name"), "bytes": f.get("bytes"), "sha256": f.get("sha256")}
                  for f in record.get("files") or [] if isinstance(f, Mapping)],
    }


def step_view(ctx: Any, auth: AuthContext, *, tenant_id: str, workflow_id: str,
              step_id: str) -> dict[str, Any]:
    """`GET .../steps/{step}/input` (§4.6). The declaration is read only once verified."""
    step = load_step(ctx.store.db, tenant_id, workflow_id, step_id)
    status = status_of(step.doc)
    verified = spec_refusal(ctx.settings, step.doc, step.task_id) is None
    block = declaration(step.doc) if verified else {}
    held = window(step.doc)
    prompt = _mapping(step.doc.get("input")).get("prompt")
    staged = _mapping(_mapping(step.doc.get("metadata")).get(INPUT_FROM_METADATA_KEY))
    questions = None
    if verified and block.get("form") == "answers" and status == WAITING:
        try:
            questions = _questions_of(ctx, step)
        except ApiError:
            questions = None
    return {
        "workflow_id": workflow_id,
        "step_id": step_id,
        "task_id": step.task_id,
        "status": status,
        "spec_verified": verified,
        "prompt": redact(prompt, decoded=True).text if isinstance(prompt, str) else None,
        "form": block.get("form"),
        "artifact": block.get("artifact"),
        "max_files": block.get("max_files"),
        "max_answer_bytes": MAX_ANSWER_BYTES,
        "tenant": tenant_id,
        "may_answer": status == WAITING and verified and may_answer(ctx, auth, tenant_id),
        "remind": list(block.get("remind") or []),
        "opened_at": _iso(held.get("opened_at")),
        "expires_at": _iso(held.get("expires_at")),
        "on_expire": block.get("on_expire"),
        "shows": [{"step_id": step.step_ids.get(task_id), "task_id": task_id, "name": name}
                  for task_id, name in staged.items()],
        "questions": questions,
        "answer": _public_record(_mapping(held.get("answer"))),
    }


def list_waiting(db: Any, tenant_id: str, *, limit: int) -> dict[str, Any]:
    """`GET /v1/inputs?status=waiting`: this tenant's waiting steps, newest first.

    Every member may answer every one (Q2), so every one is listed. One
    equality query within the tenant; the newest-first order is applied to
    what it returns, which `LIST_MAX` bounds.
    """
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
        .where(filter=FieldFilter("park_reason", "==", ParkReason.AWAITING_INPUT.value))
        .limit(LIST_MAX + 1)
    )
    rows = []
    for snap in query.stream():
        doc = {**(snap.to_dict() or {}), "id": snap.id}
        if doc.get("tenant_id") != tenant_id or not is_await_profile(doc.get("runner_profile")):
            continue
        if status_of(doc) != WAITING:
            continue
        held = window(doc)
        rows.append({
            "workflow_id": doc.get("workflow_id"),
            "step_id": doc.get("step_id"),
            "task_id": doc["id"],
            "opened_at": held.get("opened_at"),
            "expires_at": held.get("expires_at"),
        })
    truncated = len(rows) > LIST_MAX
    rows.sort(key=lambda r: (r["opened_at"] is not None, r["opened_at"] or 0, r["task_id"]),
              reverse=True)
    items = [{**r, "opened_at": _iso(r["opened_at"]), "expires_at": _iso(r["expires_at"])}
             for r in rows[:limit]]
    return {"items": items, "truncated": truncated or len(rows) > limit}


# --------------------------------------------------------------------------
# expiry: the schedule tick
# --------------------------------------------------------------------------

@dataclass
class ExpiryReport:
    due: int = 0
    failed: int = 0
    defaulted: int = 0
    refused: int = 0
    lost: int = 0
    errors: int = 0

    def to_api(self) -> dict[str, int]:
        return dict(self.__dict__)


def dark() -> bool:
    """True while `await-input` is unavailable (request D is the owner's)."""
    profile = RUNNER_PROFILES.get(AWAIT_INPUT_PROFILE)
    return profile is None or not profile.available


def _due(db: Any, now: datetime, limit: int) -> Iterable[dict[str, Any]]:
    """The index `await_input_expiry` serves: state, park_reason, expires_at (§4.7)."""
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("state", "==", TaskState.PARKED.value))
        .where(filter=FieldFilter("park_reason", "==", ParkReason.AWAITING_INPUT.value))
        .where(filter=FieldFilter(f"metadata.{AWAIT_INPUT_METADATA_KEY}.expires_at", "<=", now))
        .order_by(f"metadata.{AWAIT_INPUT_METADATA_KEY}.expires_at")
        .limit(limit)
    )
    for snap in query.stream():
        yield {**(snap.to_dict() or {}), "id": snap.id}


def _dead_letter(ctx: Any, doc: Mapping[str, Any], now: datetime, text: str) -> bool:
    return _end(
        ctx.store.db, doc["id"], str(doc.get("tenant_id") or ""), now=now,
        state=TaskState.DEAD_LETTERED,
        window_update={"expired": {"at": now, "on_expire": "fail"}},
        fields={"last_error": text[:1000], "end_cause": None},
        event_type=EventType.DEAD_LETTERED,
        event_detail={"reason": text[:1000], "park_reason": ParkReason.AWAITING_INPUT.value},
    )


def expire_due(ctx: Any, writer: InputWriter | None, *, limit: int = EXPIRE_LIMIT
               ) -> ExpiryReport | None:
    """End every waiting step past `expires_at` as its signed declaration says (§4.6).

    None, having read nothing, while the profile is dark. Each step is
    checked again inside its own transaction (`_open`), so a member's answer
    or a cancel that lands first wins and this does nothing to it.

    A step whose signature does not verify is dead-lettered whatever it
    declares: its `on_expire` and `default` are exactly what a forged
    document would choose, and ending it is what keeps its workflow from
    waiting forever.
    """
    if dark():
        return None
    report = ExpiryReport()
    now = ctx.now()
    for doc in _due(ctx.store.db, now, limit):
        report.due += 1
        task_id, tenant_id = doc["id"], str(doc.get("tenant_id") or "")
        if not is_await_profile(doc.get("runner_profile")) or not _open(doc, tenant_id):
            continue
        refusal = spec_refusal(ctx.settings, doc, task_id)
        block = declaration(doc) if refusal is None else {}
        seconds = block.get("expires_after_seconds")
        try:
            if refusal is not None:
                ended = _dead_letter(ctx, doc, now, f"await_input_spec_refused: {refusal}")
                report.refused += int(ended)
            elif block.get("on_expire") == "default" and isinstance(block.get("default"), dict):
                ended = _default(ctx, writer, doc, block, now)
                report.defaulted += int(ended)
            else:
                ended = _dead_letter(
                    ctx, doc, now,
                    f"await_input_expired: nobody answered within {seconds} seconds",
                )
                report.failed += int(ended)
        except StoreUnavailable:
            # The default's object was not created: nothing was committed,
            # and the next tick tries again.
            report.errors += 1
            continue
        if not ended:
            report.lost += 1
            continue
        ring(ctx.waker, ctx.metrics, TASK_FINISHED, task_id=task_id, tenant_id=tenant_id,
             state=(TaskState.SUCCEEDED if block.get("on_expire") == "default" and refusal is None
                    else TaskState.DEAD_LETTERED).value)
    if report.due:
        log.info("await-input expiry: %s", report.to_api())
    return report


def _default(ctx: Any, writer: InputWriter | None, doc: Mapping[str, Any],
             block: Mapping[str, Any], now: datetime) -> bool:
    """`on_expire: "default"`: the signed default, written as the artifact, then committed."""
    task_id, tenant_id = doc["id"], str(doc.get("tenant_id") or "")
    form_data = _form_bytes(block["default"])
    artifact = str(block.get("artifact") or "")
    uri, _ = _write_objects(writer_or_refuse(writer), tenant_id, task_id, artifact, form_data, ())
    opened_at = window(doc).get("opened_at")
    wait_seconds = (
        max(0, int((now - opened_at).total_seconds())) if isinstance(opened_at, datetime) else None
    )
    summary = {
        "artifacts": [_artifact_entry(artifact, form_data, uri, JSON_CONTENT_TYPE, ())],
        "artifact_bytes": len(form_data),
        "published_by": PUBLISHED_BY,
        "input": {"source": "default", "answered_by": None, "answered_at": now,
                  "via": None, "wait_seconds": wait_seconds, "files": 0},
    }
    return _end(
        ctx.store.db, task_id, tenant_id, now=now, state=TaskState.SUCCEEDED,
        window_update={"expired": {"at": now, "on_expire": "default"}},
        fields={"result_summary": summary, "last_error": None, "end_cause": None},
        event_type=EventType.SUCCEEDED,
        event_detail={"published_by": PUBLISHED_BY, "source": "default"},
    )

