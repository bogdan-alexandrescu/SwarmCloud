"""The await-input routes: a person answers a workflow step (docs/design/human-steps.md §4.6).

    POST /v1/workflows/{workflow_id}/steps/{step_id}/input   the answer
    GET  /v1/workflows/{workflow_id}/steps/{step_id}/input   what the card shows
    GET  /v1/inputs?status=waiting                           the tenant's waits

Lane HW-API-ANSWER. The decisions are `swarm_api.awaitinput`'s; this module
is the HTTP half, and above all THE SIZE GATE (§4.3), which runs before the
body is read and long before anything is written:

  1. a multipart answer must say its `Content-Length` (411) and that length
     must be at most 16 MiB plus 64 KiB of framing (413), refused before one
     byte of the body is read;
  2. at most `MAX_CONCURRENT_MULTIPART` multipart answers are held by one
     instance at once; another is 429 with `Retry-After`. Taken BEFORE the
     body is read, so a refused one costs no memory;
  3. the body is read through a counting reader that aborts at the same
     bound, because `Content-Length` is a claim, not a measurement.

Then the step, the caller and the membership are checked (`awaitinput.
answer`), so an outsider's 16 MiB is refused without a single GCS call.

THE MULTIPART PARSER IS THIS MODULE'S OWN, fed the bytes the counting reader
already holds, and it runs off the event loop. The design names
`python-multipart`; a dependency of swarm-api is also a line in the
repository's root `uv.lock`, outside this lane, and `uv lock --check` fails
CI on a pyproject that disagrees with it, so whether to add it is a question
for the owner (the PR's questions.json). The standard library's `email`
parser, used before, is not bounded: one 16 MiB file of short lines held
~570 MiB and 4 s of CPU on the event loop (review, 2026-10-18). This one
finds each delimiter with `bytes.find` and copies each part once, so one
answer holds at most twice its body whatever its bytes are, which is the
bound `awaitinput.MAX_CONCURRENT_MULTIPART` is sized from. It takes only
what RFC 7578 lets a sender send: a part's headers are at most
`MAX_PART_HEADER_BYTES`, a transfer encoding is refused rather than
decoded, and a part's declared type changes nothing about its bytes.
Parsing counted bytes also keeps the bound in one place: nothing reads the
stream but `read_counted`.

The routes' request bodies and responses are never captured
(`main.UNCAPTURED_ROUTES`): an answer may carry a person's files.
"""

from __future__ import annotations

import email.policy
import json
from email.message import Message
from email.parser import BytesHeaderParser
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from .. import awaitinput
from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, tenant_scope
from ..errors import ValidationFailed
from ..validation import AWAIT_INPUT_MAX_FILES

router = APIRouter(tags=["workflows"])

INPUT_ROUTE = "/v1/workflows/{workflow_id}/steps/{step_id}/input"
INPUTS_ROUTE = "/v1/inputs"

#: The most a multipart body may be: the answer and its framing (§4.3 step 1).
MAX_MULTIPART_BYTES = awaitinput.MAX_ANSWER_BYTES + awaitinput.MULTIPART_FRAMING_BYTES
#: Parts one multipart answer may have: the `answer` and every file the
#: largest `max_files` allows. One more `file` is still parsed, so that the
#: refusal says too many files rather than a malformed body.
MAX_PARTS = 1 + AWAIT_INPUT_MAX_FILES + 1
#: One part's header block. A form field says its name, its filename and its
#: type; 8 KiB is many times that, and it bounds what one part's headers cost.
MAX_PART_HEADER_BYTES = 8 * 1024
#: The longest boundary RFC 2046 allows.
MAX_BOUNDARY_LENGTH = 70
#: Transfer encodings that leave the bytes as they are. Any other is refused:
#: RFC 7578 4.7 says a sender does not use one, so none is decoded here.
_IDENTITY_ENCODINGS = ("7bit", "8bit", "binary")


class UnsupportedMediaType(ValidationFailed):
    """415: an answer is `application/json` or `multipart/form-data`, nothing else."""

    status_code = 415


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise awaitinput.invalid("invalid_payload", "Content-Length is not a number") from None
    if value < 0:
        raise awaitinput.invalid("invalid_payload", "Content-Length is negative")
    return value


async def read_counted(request: Any, bound: int) -> bytes:
    """The body, counted as it arrives; 413 the moment it passes `bound`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > bound:
            raise awaitinput.too_large(
                f"the body is more than the {bound} bytes an answer may be; nothing was read further"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _malformed(message: str) -> ValidationFailed:
    return awaitinput.invalid("invalid_payload", message, position="multipart")


def _boundary(content_type: str) -> bytes:
    header = Message()
    header["content-type"] = content_type
    boundary = header.get_param("boundary")
    if not isinstance(boundary, str) or not 1 <= len(boundary) <= MAX_BOUNDARY_LENGTH:
        raise _malformed("the multipart body has no usable boundary")
    try:
        return boundary.encode("ascii")
    except UnicodeEncodeError:
        raise _malformed("the multipart boundary is not ASCII") from None


def _parts(content_type: str, body: bytes):
    """Each part as (its headers, its bytes), found with `bytes.find`.

    Every part's bytes are one slice of `body`, so the parts together are at
    most one more copy of it; nothing here splits the body into lines.
    """
    delimiter = b"--" + _boundary(content_type)
    separator = b"\r\n" + delimiter
    if body.startswith(delimiter):
        pos = len(delimiter)
    else:
        found = body.find(separator)
        if found < 0:
            raise _malformed("the multipart body has no delimiter")
        pos = found + len(separator)
    while True:
        if body.startswith(b"--", pos):
            return
        while body[pos:pos + 1] in (b" ", b"\t"):
            pos += 1
        if not body.startswith(b"\r\n", pos):
            raise _malformed("a multipart delimiter is not followed by a line break")
        pos += 2
        if body.startswith(b"\r\n", pos):
            head, data_start = b"", pos + 2
        else:
            end = body.find(b"\r\n\r\n", pos, pos + MAX_PART_HEADER_BYTES + 4)
            if end < 0:
                raise _malformed(
                    f"a part's headers are more than {MAX_PART_HEADER_BYTES} bytes or never end"
                )
            head, data_start = body[pos:end + 2], end + 4
        following = body.find(separator, data_start)
        if following < 0:
            raise _malformed("the multipart body has no closing delimiter")
        headers = BytesHeaderParser(policy=email.policy.HTTP).parsebytes(head + b"\r\n")
        yield headers, body[data_start:following]
        pos = following + len(separator)


def parse_multipart(content_type: str, body: bytes) -> tuple[Any, list[tuple[str, bytes]]]:
    """(the `answer` part as JSON, [(filename, bytes)] of the `file` parts)."""
    answer: Any = None
    seen_answer = False
    files: list[tuple[str, bytes]] = []
    for count, (part, data) in enumerate(_parts(content_type, body), start=1):
        if count > MAX_PARTS:
            raise awaitinput.invalid(
                "too_many_files",
                f"the answer carries more than {AWAIT_INPUT_MAX_FILES} files",
                max_files=AWAIT_INPUT_MAX_FILES,
            )
        encoding = str(part.get("content-transfer-encoding", "binary")).strip().lower()
        if encoding not in _IDENTITY_ENCODINGS:
            raise _malformed(f"a part is sent with transfer encoding {encoding!r}; send its bytes")
        name = part.get_param("name", header="content-disposition")
        if name == "answer" and not seen_answer:
            seen_answer = True
            try:
                answer = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise awaitinput.invalid("invalid_payload", "the answer part is not JSON",
                                         position="answer") from None
        elif name == "file":
            filename = part.get_filename()
            if not filename:
                raise awaitinput.invalid("invalid_file_name", "a file part has no filename")
            files.append((filename, data))
        else:
            raise awaitinput.invalid(
                "invalid_payload",
                "a multipart answer has one part named 'answer' and parts named 'file'",
                position="multipart",
            )
    if not seen_answer:
        raise awaitinput.invalid("invalid_payload", "the multipart body has no 'answer' part",
                                 position="answer")
    return answer, files


@router.post(INPUT_ROUTE)
async def post_input(
    workflow_id: str,
    step_id: str,
    request: Request,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Answer an await-input step. 200 with what was recorded, or the refusal."""
    media = _media_type(request)
    length = _content_length(request)
    writer = getattr(request.app.state, "input_writer", None)
    if media == "multipart/form-data":
        if length is None:
            raise awaitinput.LengthRequired(
                "an answer with files must say its Content-Length",
                detail={"reason": "length_required"},
            )
        if length > MAX_MULTIPART_BYTES:
            raise awaitinput.too_large(
                f"the answer is {length} bytes; at most {awaitinput.MAX_ANSWER_BYTES} are taken"
            )
        slots = request.app.state.answer_slots
        if not slots.acquire(blocking=False):
            raise awaitinput.busy()
        try:
            body = await read_counted(request, MAX_MULTIPART_BYTES)
            answer, files = await run_in_threadpool(
                parse_multipart, request.headers.get("content-type", ""), body
            )
            del body
            return await run_in_threadpool(
                awaitinput.answer, ctx, writer, auth, tenant_id=tenant_id,
                workflow_id=workflow_id, step_id=step_id, body=answer, files=files,
            )
        finally:
            slots.release()
    if media in ("application/json", ""):
        bound = awaitinput.MAX_JSON_BODY_BYTES
        if length is not None and length > bound:
            raise awaitinput.too_large(
                f"a JSON answer is at most {bound} bytes; send files as multipart/form-data"
            )
        raw = await read_counted(request, bound)
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise awaitinput.invalid("invalid_payload", "the body is not JSON",
                                     position="answer") from None
        return await run_in_threadpool(
            awaitinput.answer, ctx, writer, auth, tenant_id=tenant_id,
            workflow_id=workflow_id, step_id=step_id, body=answer,
        )
    raise UnsupportedMediaType(
        "an answer is application/json, or multipart/form-data with its files",
        detail={"reason": "content_type_refused"},
    )


@router.get(INPUT_ROUTE)
def get_input(
    workflow_id: str,
    step_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """What the step shows and asks, its window, and its answer once given."""
    return awaitinput.step_view(ctx, auth, tenant_id=tenant_id, workflow_id=workflow_id,
                                step_id=step_id)


@router.get(INPUTS_ROUTE)
def list_inputs(
    status: str = Query(default=awaitinput.WAITING),
    limit: int = Query(default=50, ge=1, le=awaitinput.LIST_MAX),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every step of the caller's tenant waiting for an answer, newest first."""
    if status != awaitinput.WAITING:
        raise awaitinput.invalid("invalid_payload", "status takes only 'waiting'",
                                 position="status")
    return awaitinput.list_waiting(ctx.store.db, tenant_id, limit=limit)
