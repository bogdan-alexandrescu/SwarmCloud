"""Checkpoint CONTENT routes: what is inside one checkpoint's archive.

Owner decision 2026-09-24 (redesign-v2 S3, item A3) -- all three of:

    GET /v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files
        every member of the archive, `{path, size, mode, type}`, bounded, with
        a truncation signal;
    GET /v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path}
        one member as text: content-type allowlist, redacted, windowed under
        the artifact-content route's own size cap;
    GET /v1/tasks/{task_id}/checkpoints/{checkpoint_id}/content
        the whole archive, streamed, `Content-Disposition: attachment`.

`{checkpoint_id}` is the id the checkpoint LISTING returns (`ckpt-00001`),
not a bare sequence number: ids restart per attempt, and a number that meant
"the first checkpoint" would name one per attempt. `attempt_id` narrows to one
attempt and is what a client that already has a listing row always sends; left
out, the server resolves the id across the task's attempts and refuses (422,
naming them) when more than one attempt wrote it.

A separate router rather than more lines in `tasks.py`, so the three routes
and their tests land without touching the file the task and checkpoint-listing
routes live in. The tenant argument on every call is `tenant_scope`'s, never
`auth.tenant_id` and never anything from the request -- see `tasks.py` for why
the dependency and not the raw id.

All logic is in `swarm_api.checkpoint_content`; the routes only translate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ..checkpoint_content import CheckpointContent
from ..deps import AppContext, get_context, tenant_scope

router = APIRouter(prefix="/v1/tasks", tags=["checkpoints"])


def checkpoint_content(ctx: AppContext = Depends(get_context)) -> CheckpointContent:
    """The service, built around the one `InspectionService` the app holds.

    A dependency rather than a field on `AppContext` so that the context --
    constructed in `deps.build_context`, which several lanes edit -- does not
    change shape for this. A test swaps budgets through
    `app.dependency_overrides[checkpoint_content]`, which is FastAPI's own
    injection point rather than a patched import.
    """
    return CheckpointContent(ctx.inspection)


@router.get("/{task_id}/checkpoints/{checkpoint_id}/files")
def list_checkpoint_files(
    task_id: str,
    checkpoint_id: str,
    attempt_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    service: CheckpointContent = Depends(checkpoint_content),
) -> dict:
    """Every member of one checkpoint's archive, in archive order.

    Read out of the tarball in GCS, streamed, never held whole. `status` is
    `ok`, `absent` (no archive object: `files` is null, NEVER `[]`) or
    `corrupt` (the rows before the point it stopped being readable); a failed
    read is a 503 with no `files` at all. `truncated` + `truncated_reason`
    (`entry_cap`, `scan_budget`, `inflate_budget`, `corrupt`) say when the list
    is not the whole archive. `manifest` is the commit marker's own account,
    and `file_count_agrees` compares the two.
    """
    return service.list_files(
        tenant_id,
        task_id,
        checkpoint_id=checkpoint_id,
        attempt_id=attempt_id,
        limit=limit,
    )


@router.get("/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}")
def read_checkpoint_file(
    task_id: str,
    checkpoint_id: str,
    path: str,
    attempt_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    service: CheckpointContent = Depends(checkpoint_content),
) -> dict:
    """One member's content, as the artifact-content route serves an artifact.

    `path` is matched EXACTLY against the member names in this checkpoint's
    archive and never becomes an object key; it is refused with a 422 before
    any object is read if it is absolute or carries `..`, `.`, an empty
    segment, a backslash or a control character. Text only, by allowlist and
    by NUL sniff; redacted at read time; `truncated`/`next_offset` on every
    window.
    """
    return service.read_file(
        tenant_id,
        task_id,
        checkpoint_id=checkpoint_id,
        path=path,
        attempt_id=attempt_id,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get(
    "/{task_id}/checkpoints/{checkpoint_id}/content",
    response_class=StreamingResponse,
)
def download_checkpoint(
    task_id: str,
    checkpoint_id: str,
    attempt_id: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    service: CheckpointContent = Depends(checkpoint_content),
) -> StreamingResponse:
    """The whole archive as a download, streamed in bounded windows.

    NOT REDACTED -- it is gzip, and no rule runs over compressed bytes; the
    owner chose to serve it knowing that, and `X-Swarm-Redaction: not-applied`
    says so on every response. An absent archive is a 404 and an unreadable
    one a 503 BEFORE any byte is sent; a failure after that ends the stream
    short of its `Content-Length`.
    """
    archive = service.download(
        tenant_id, task_id, checkpoint_id=checkpoint_id, attempt_id=attempt_id
    )
    return StreamingResponse(
        archive.chunks, media_type="application/gzip", headers=archive.headers
    )
