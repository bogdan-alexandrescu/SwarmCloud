"""The bridge's half of a human step: a person answers an await-input step from the host.

docs/design/human-steps.md §4.2, §4.5 and §4.8; lane HW-MCP. swarm-api owns
the decision (`swarm_api.awaitinput.answer`): who may answer, the form's
shape, redaction, the file scan, the one winner. This module owns what only
the bridge can own: that the answer it sends is the PERSON's.

THE ANSWER IS TYPED INTO THE HOST'S ELICITATION, NEVER TAKEN FROM THE TOOL
CALL. On a laptop an agent drives the person's own plugin session with the
person's own ID token, which swarm-api cannot tell from the person, so a
`swarm_workflow_input` that took `verdict` as an argument would let a
workflow's agent approve its own draft. The tool takes the step and,
optionally, local file paths -- nothing that could carry an answer -- and
`_refuse_unknown_arguments` refuses anything else before a byte is read. The
verdict, findings, answers or text are the elicitation's content, typed by
the person at the host's own prompt, which the model neither writes nor sees
(`server._elicit`). A host that cannot elicit is refused with the console
link, never offered a fallback the agent could fill in. That is how
`swarm_account_remove` keeps an agent from confirming its own removal.

FILES: THE AGENT PROPOSES, THE PERSON DECIDES. An elicitation carries flat
fields, not files, so `files` are paths in the call's arguments. The bridge
reads each one, shows the person the exact list (name, bytes, sha256) and
§4.3's warning, and sends them only when the person ticks `send_files`. An
unticked box sends the answer without any file. Every bound the bridge can
check without the API -- the count, the bytes, that each path is a readable
file with a name of its own -- is checked before the API is called at all,
and the step's own `max_files` before the person is asked.

The bridge restates two numbers of swarm-api's, which it cannot import:
`MAX_FILES` and `MAX_ANSWER_BYTES`. tests/unit/mcp/test_await_input_bridge.py
reads both packages and fails if they differ. swarm-api repeats every check;
the bridge's are for a refusal before anything travels, not for safety.

DARK UNTIL REQUEST D. While `await-input` is unavailable no such step can be
submitted, so `swarm_inputs` lists nothing and `swarm_workflow_input` finds
no step to answer (swarm-api's 404).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import client as client_module
from .client import SwarmError

#: swarm-api's `validation.AWAIT_INPUT_MAX_FILES`: the most files one answer carries.
MAX_FILES = 10
#: swarm-api's `awaitinput.MAX_ANSWER_BYTES`: the form and every file, the owner's 16 MiB (Q6).
MAX_ANSWER_BYTES = 16 * 1024 * 1024
#: swarm-api's `awaitinput.ANSWER_ID_RE`. A fresh id per call: a retry of the
#: same POST is replayed by the API as its original 200, a new call is a new answer.
ANSWER_ID = re.compile(r"^[a-z0-9-]{1,64}$")
#: How the answer arrived, as the API records it (`awaitinput.VIA`).
VIA = "mcp"

#: How long the bridge waits for the person to fill the form in. Longer than
#: `swarm_account_remove`'s five minutes: a person types findings here, not a
#: label. The tool call is a hold (`server._holds`), kept alive by progress
#: notifications, so it occupies no quick worker while it waits.
ELICIT_TIMEOUT_SECONDS = 1800.0

#: The verdicts a `verdict` form takes (`validation.REVIEW_VERDICTS`), and
#: the words the console's buttons use for them (§4.5).
VERDICTS = ("MERGE", "NOT_YET")
VERDICT_WORDS = ("Approve", "Request changes")

#: §4.3's sentence, shown above the upload in the console and in the
#: elicitation here, word for word.
FILE_WARNING = (
    "Files go to the next agent byte for byte. Text files are checked for credentials "
    "and refused if one is found. PDFs and images cannot be checked, so never attach a "
    "screenshot or a document that shows a key, a token or a password."
)

#: The elicitation field that sends the listed files. Unticked, none is sent.
SEND_FILES = "send_files"

#: Waits one `swarm_inputs` read lists at most: `swarm_api.awaitinput.LIST_MAX`.
LIST_LIMIT = 200


def input_path(workflow_id: str, step_id: str) -> str:
    return (f"/v1/workflows/{urllib.parse.quote(workflow_id, safe='')}"
            f"/steps/{urllib.parse.quote(step_id, safe='')}/input")


# --------------------------------------------------------------------------
# the files, before anything travels
# --------------------------------------------------------------------------


def read_files(paths: Any) -> list[tuple[str, bytes]]:
    """[(name, bytes)] of the proposed files, or a refusal before anything is sent.

    The count is checked before any file is opened and the bytes before any is
    read, so a list over the bound costs nothing but a stat per file.
    """
    if paths is None:
        return []
    if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
        raise SwarmError("swarm_workflow_input: `files` is a list of local file paths; nothing was sent")
    if len(paths) > MAX_FILES:
        raise SwarmError(
            f"swarm_workflow_input: {len(paths)} files proposed, and an answer carries at most "
            f"{MAX_FILES}; nothing was sent"
        )
    resolved: list[Path] = []
    total = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise SwarmError(f"swarm_workflow_input: {raw} is not a readable file; nothing was sent")
        total += path.stat().st_size
        resolved.append(path)
    if total > MAX_ANSWER_BYTES:
        raise SwarmError(
            f"swarm_workflow_input: the files are {total} bytes, and an answer is at most "
            f"{MAX_ANSWER_BYTES} with its form; nothing was sent"
        )
    names = [p.name for p in resolved]
    doubled = sorted({n for n in names if names.count(n) > 1})
    if doubled:
        raise SwarmError(
            f"swarm_workflow_input: two files are both named {doubled}, and the next agent reads "
            "them by name; nothing was sent"
        )
    out: list[tuple[str, bytes]] = []
    for path in resolved:
        try:
            out.append((path.name, path.read_bytes()))
        except OSError as exc:
            raise SwarmError(f"swarm_workflow_input: {path} could not be read ({exc}); nothing was sent") from exc
    if sum(len(data) for _, data in out) > MAX_ANSWER_BYTES:
        raise SwarmError(
            f"swarm_workflow_input: the files grew past {MAX_ANSWER_BYTES} bytes while they were "
            "read; nothing was sent"
        )
    return out


def file_lines(files: list[tuple[str, bytes]]) -> list[str]:
    return [f"  {name} · {len(data)} bytes · sha256 {hashlib.sha256(data).hexdigest()}"
            for name, data in files]


# --------------------------------------------------------------------------
# the elicitation: one schema per form, and its content read back
# --------------------------------------------------------------------------


def _question_text(question: Any) -> str:
    text = question.get("question") if isinstance(question, dict) else None
    return text if isinstance(text, str) else "(no question text)"


def _labels(question: Any) -> list[str]:
    options = question.get("options") if isinstance(question, dict) else None
    return [o["label"] for o in options or []
            if isinstance(o, dict) and isinstance(o.get("label"), str)]


def schema(form: str, questions: list[Any] | None, *, with_files: bool) -> dict[str, Any]:
    """The elicitation's `requestedSchema`: flat fields only, as MCP allows."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    if form == "verdict":
        properties["verdict"] = {
            "type": "string", "title": "Verdict", "enum": list(VERDICTS),
            "enumNames": list(VERDICT_WORDS),
        }
        properties["findings"] = {
            "type": "string", "title": "Findings",
            "description": "What must change, one finding per line. Empty for none.",
        }
        required.append("verdict")
    elif form == "text":
        properties["text"] = {"type": "string", "title": "Your answer"}
        required.append("text")
    elif form == "answers":
        for index, question in enumerate(questions or [], start=1):
            labels = _labels(question)
            if labels:
                properties[f"q{index}"] = {"type": "string", "title": f"Q{index}: {_question_text(question)}",
                                           "enum": labels}
            properties[f"q{index}_text"] = {
                "type": "string", "title": f"Q{index}, in your words",
                "description": "Optional when you chose an option above." if labels else "",
            }
    else:
        raise SwarmError(f"the step declares form {form!r}, which this bridge cannot ask; "
                         "answer it in the console. Nothing was sent")
    if with_files:
        properties[SEND_FILES] = {
            "type": "boolean", "title": "Send the files listed above", "default": False,
        }
        required.append(SEND_FILES)
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def payload_from(form: str, content: dict[str, Any], questions: list[Any] | None) -> dict[str, Any]:
    """The form's payload, from what the person typed and nothing else."""
    if form == "verdict":
        verdict = content.get("verdict")
        if verdict not in VERDICTS:
            raise SwarmError("no verdict was chosen; nothing was sent")
        findings = content.get("findings")
        lines = findings.splitlines() if isinstance(findings, str) else []
        return {"verdict": verdict, "findings": [line.strip() for line in lines if line.strip()]}
    if form == "text":
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SwarmError("the answer was empty; nothing was sent")
        return {"text": text}
    answers: list[dict[str, Any]] = []
    blank: list[int] = []
    for index, question in enumerate(questions or [], start=1):
        choice = content.get(f"q{index}")
        choice = choice if isinstance(choice, str) and choice in _labels(question) else None
        text = content.get(f"q{index}_text")
        text = text if isinstance(text, str) and text.strip() else None
        if choice is None and text is None:
            blank.append(index)
        answers.append({"question_index": index - 1, "choice": choice, "text": text})
    if blank:
        raise SwarmError(f"question(s) {blank} were left blank, and every question needs an answer; "
                         "nothing was sent")
    return {"answers": answers}


def message(view: dict[str, Any], files: list[tuple[str, bytes]]) -> str:
    """What the person reads above the form."""
    lines = [
        f"A SwarmCloud workflow step is waiting for a member of tenant {view.get('tenant')} to answer: "
        f"step {view.get('step_id')} of {view.get('workflow_id')}, open until {view.get('expires_at')}.",
        "Your answer is what you type here. Nothing the agent that asked passed is sent.",
    ]
    prompt = view.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        lines += ["", prompt.strip()]
    shows = [s for s in view.get("shows") or [] if isinstance(s, dict)]
    if shows:
        lines += ["", "It shows (read them with swarm_artifact, or in the console):"]
        lines += [f"  {s.get('name')} from step {s.get('step_id')}" for s in shows]
    questions = view.get("questions") or []
    if view.get("form") == "answers" and questions:
        lines += ["", "The questions:"]
        for index, question in enumerate(questions, start=1):
            lines.append(f"  Q{index}. {_question_text(question)}")
            for option in question.get("options") or [] if isinstance(question, dict) else []:
                if isinstance(option, dict) and option.get("label"):
                    detail = f" -- {option['description']}" if option.get("description") else ""
                    lines.append(f"      {option['label']}{detail}")
    if files:
        lines += ["", f"The agent proposes sending these {len(files)} file(s) with your answer:"]
        lines += file_lines(files)
        lines += [FILE_WARNING, "Leave \"Send the files\" unticked and none of them is sent."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def multipart(answer: dict[str, Any], files: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    """(content type, body): one `answer` part and one `file` part per file (§4.3)."""
    boundary = f"swarm-{secrets.token_hex(16)}"
    head = boundary.encode("ascii")
    parts = [
        b"--" + head + b"\r\n"
        b'Content-Disposition: form-data; name="answer"\r\n'
        b"Content-Type: application/json\r\n\r\n"
        + json.dumps(answer).encode("utf-8") + b"\r\n"
    ]
    for name, data in files:
        quoted = name.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(
            b"--" + head + b"\r\n"
            + f'Content-Disposition: form-data; name="file"; filename="{quoted}"\r\n'.encode("utf-8")
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + data + b"\r\n"
        )
    parts.append(b"--" + head + b"--\r\n")
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def post_multipart(client: Any, path: str, content_type: str, body: bytes) -> Any:
    """POST a multipart body as the client's own caller.

    `SwarmClient.request` sends JSON only, and `client.py` is outside this
    lane, so the one non-JSON request the bridge makes is built here from the
    client's own parts: its base URL, its credential, its tenant header and
    its pacing gate, with its error explanations. The answer route is never
    retried: a second POST of the same `answer_id` is replayed by the API as
    the first one's 200, so the person can simply call again.
    """
    gate = getattr(client, "_gate", None)
    if gate is not None:
        gate.take(hold=client_module.in_hold_lane())
    req = urllib.request.Request(f"{client.base_url}{path}", data=body, method="POST")
    if client.sends_own_token:
        req.add_header("Authorization", f"Bearer {client.credential()}")
    if client.tenant:
        req.add_header(client_module.TENANT_HEADER, client.tenant)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    shared = getattr(client, "_shared", None)
    try:
        with client_module._open(req, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError as exc:
                raise SwarmError(
                    f"POST {path} -> {response.status}, but the body is not JSON: "
                    f"{client_module._explain(response.status, raw)}",
                    status=response.status, edge=client_module._is_edge(response.status, raw),
                ) from exc
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        edge = client_module._is_edge(exc.code, raw)
        code, detail = (None, None) if edge else client_module._error_fields(raw)
        raise SwarmError(f"POST {path} -> {exc.code}: {client_module._explain(exc.code, raw)}",
                         status=exc.code, edge=edge, code=code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise SwarmError(f"could not reach {client.base_url}: {exc.reason}") from exc
    finally:
        if shared is not None:
            shared.invalidate()


def console_of(client: Any, workflow_id: str, step_id: str) -> str | None:
    """The step's console link as the API serves it, else the workflow's, else None."""
    from .follow import console_link
    from .workflows import fetch

    try:
        envelope = fetch(client, workflow_id)
    except SwarmError:
        return None
    workflow = envelope.get("workflow") or {}
    for step in workflow.get("steps") or []:
        if isinstance(step, dict) and step.get("step_id") == step_id and console_link(step):
            return console_link(step)
    return console_link(workflow)


def answer(
    client: Any,
    workflow_id: str,
    step_id: str,
    files: Any,
    *,
    can_elicit: bool,
    elicit: Callable[..., dict[str, Any] | None],
) -> dict[str, Any]:
    """The whole of `swarm_workflow_input`: check, ask the person, send what they gave."""
    proposed = read_files(files)                                   # bounds: nothing read yet
    if not can_elicit:
        link = console_of(client, workflow_id, step_id)
        where = link or "the console's Work › Workflows screen"
        raise SwarmError(
            "answering a workflow step needs the answer typed by a person, and this host did not "
            f"say it can ask one (MCP elicitation); nothing was sent. Answer it at {where}"
        )
    view = client.request("GET", input_path(workflow_id, step_id))
    if not isinstance(view, dict):
        raise SwarmError(f"GET {input_path(workflow_id, step_id)} answered no step; nothing was sent")
    status = view.get("status")
    if status != "waiting":
        record = view.get("answer") or {}
        who = f" by {record.get('answered_by')} at {record.get('answered_at')}" if record else ""
        raise SwarmError(f"step {step_id} of {workflow_id} is {status}{who}, not waiting for an "
                         "answer; nothing was sent")
    if not view.get("spec_verified", True):
        raise SwarmError(f"step {step_id}'s signed spec did not verify, so it cannot be answered; "
                         "nothing was sent")
    max_files = view.get("max_files")
    if isinstance(max_files, int) and len(proposed) > max_files:
        raise SwarmError(f"step {step_id} takes at most {max_files} files with its answer, and "
                         f"{len(proposed)} were proposed; nothing was sent")
    form = str(view.get("form") or "")
    questions = view.get("questions") if isinstance(view.get("questions"), list) else None
    if form == "answers" and not questions:
        raise SwarmError(f"step {step_id}'s questions could not be read, so they cannot be asked "
                         "here; answer it in the console. Nothing was sent")
    requested = schema(form, questions, with_files=bool(proposed))

    reply = elicit(message(view, proposed), requested, timeout=ELICIT_TIMEOUT_SECONDS)
    if reply is None:
        raise SwarmError(f"nobody answered the form within {int(ELICIT_TIMEOUT_SECONDS)}s; "
                         "nothing was sent")
    result = reply.get("result")
    if not isinstance(result, dict) or result.get("action") != "accept":
        raise SwarmError("the form was declined, cancelled, or the host could not show it; "
                         "nothing was sent")
    content = result.get("content") if isinstance(result.get("content"), dict) else {}
    payload = payload_from(form, content, questions)
    sending = proposed if proposed and content.get(SEND_FILES) is True else []

    body = {"answer_id": f"mcp-{secrets.token_hex(8)}", "payload": payload, "via": VIA}
    path = input_path(workflow_id, step_id)
    if sending:
        content_type, data = multipart(body, sending)
        sent = post_multipart(client, path, content_type, data)
    else:
        sent = client.request("POST", path, payload=body)
    out = dict(sent) if isinstance(sent, dict) else {"answer": sent}
    out["files_sent"] = [name for name, _ in sending]
    if proposed and not sending:
        out["files_not_sent"] = [name for name, _ in proposed]
    return out


def waiting(client: Any, limit: Any = None) -> dict[str, Any]:
    """`GET /v1/inputs?status=waiting`: every step of the tenant waiting for an answer."""
    count = LIST_LIMIT if limit is None else limit
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= LIST_LIMIT:
        raise SwarmError(f"swarm_inputs: `limit` is a whole number from 1 to {LIST_LIMIT}. Nothing was read")
    query = urllib.parse.urlencode({"status": "waiting", "limit": count})
    listing = client.request("GET", f"/v1/inputs?{query}")
    items = listing.get("items") if isinstance(listing, dict) else None
    rows = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append({
            **item,
            "answer_with": (f"swarm_workflow_input {{workflow_id: {item.get('workflow_id')!r}, "
                            f"step_id: {item.get('step_id')!r}}}"),
        })
    return {"tenant": getattr(client, "tenant", None), "count": len(rows), "items": rows,
            "truncated": bool(listing.get("truncated")) if isinstance(listing, dict) else False}


# --------------------------------------------------------------------------
# what a step row and /sc check read of a task
# --------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def window(task: dict[str, Any]) -> dict[str, Any]:
    """`metadata.await_input`: the park's window and, once answered, the record."""
    return _mapping(_mapping(task.get("metadata")).get("await_input"))


def declaration(task: dict[str, Any]) -> dict[str, Any]:
    """`metadata.dispatch.await_input`: what the author declared."""
    return _mapping(_mapping(_mapping(task.get("metadata")).get("dispatch")).get("await_input"))


def row_fields(task: dict[str, Any], tenant: Any) -> dict[str, Any]:
    """`awaiting` on a waiting step's row, `answered` on an answered one's (§4.5)."""
    held = window(task)
    record = _mapping(held.get("answer"))
    if record:
        return {"answered": {
            "answered_by": record.get("answered_by"),
            "answered_at": _text(record.get("answered_at")),
            "via": record.get("via"),
            "files": [f.get("name") for f in record.get("files") or [] if isinstance(f, dict)],
        }}
    if task.get("state") == "PARKED" and task.get("park_reason") == "AWAITING_INPUT":
        block = declaration(task)
        return {"awaiting": {
            "tenant": task.get("tenant_id") or tenant,
            "opened_at": _text(held.get("opened_at")),
            "expires_at": _text(held.get("expires_at")),
            "form": block.get("form"),
            "artifact": block.get("artifact"),
            "max_files": block.get("max_files"),
            "remind": list(block.get("remind") or []),
        }}
    return {}
