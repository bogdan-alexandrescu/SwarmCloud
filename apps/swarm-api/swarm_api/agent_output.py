"""What an agent task produced, read through the API: outputs, raw bytes, transcript, answer.

The task drawer's Artifacts tab (#184, owner decisions 2026-09-25) shows a
task's inputs, its outputs and its logs, live while it runs. Four reads here
serve the parts that did not exist:

  * the artifact LISTING gains, per entry, the attempt it came from, a `kind`
    and `content_type` from the one name table (`checkpoint_content.
    artifact_kind`), and a `role` naming the agent's own stdout, stderr and
    transcript -- so the UI stops guessing a viewer from a file name;
  * `GET /v1/tasks/{id}/artifacts/raw` serves an artifact's BYTES, for an
    `<img>` and for a download;
  * `GET /v1/tasks/{id}/transcript` parses the agent's stdout into steps
    (`swarm_api.transcript`);
  * `GET /v1/tasks/{id}/answer` finds the agent's final answer.

EVERY READ GOES THROUGH THE API, NEVER A SIGNED URL. The owner's decision: a
signed GCS URL would skip the two things every byte served here must pass --
the tenant check (`InspectionService._scoped`, the same 404 for another
tenant's task as for a missing one) and read-time redaction
(`swarm_api.redaction`). So text is redacted on the way out whatever happened
at write time; images pass through as stored, as the owner decided, and the
response says `X-Swarm-Redaction: not-applied` rather than letting a caller
assume otherwise.

Built AROUND the one `InspectionService` the context holds, as
`checkpoint_content.CheckpointContent` is, so the tenant scoping, the segment
validation, the artifact resolution and the stream chooser are the ones the
existing routes use rather than a second copy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterator

from swarm_common.states import TERMINAL_STATES

from . import agent_streams as agent_streams_mod
from .attempt_state import attempt_is_over
from .checkpoint_content import (
    CHUNK_BYTES,
    IMAGE_EXTENSIONS,
    DownloadAborted,
    artifact_kind,
)
from .errors import Gone, UpstreamUnavailable, ValidationFailed
from .inspect import (
    ARTIFACT_SNIFF_BYTES,
    InspectionService,
    OpenedStream,
    _ages,
    _attempt_of_artifact_uri,
    _iso,
    _last_boundary,
    attempt_prefix,
    manifest_attempt,
    parse_tail_header,
)
from .objects import ObjectAbsent, ObjectReader, ObjectSlice, ObjectUnreadable
from .redaction import RULES as REDACTION_RULES, open_key_start, redact, redact_detail
from .transcript import last_result_event, parse_window

log = logging.getLogger(__name__)

#: How much of an agent's stdout `/answer` reads to find the last `result`
#: event: the end of the object, never more than this. The result is the last
#: line of a stream-json run and the whole of a json-format one.
ANSWER_TAIL_BYTES = 4 * 1024 * 1024

#: `cliagent._summarise` cuts `result_summary.runner.summary` here. A summary
#: of exactly this length is marked `complete: false` -- it may have been cut.
RUNNER_SUMMARY_CAP = 2000

#: A text body is redacted window by window, each cut at the last whitespace
#: so no token is split between two windows. A run with no whitespace at all
#: is carried into the next window until it reaches this length, and only then
#: cut (at a UTF-8 character boundary) -- the one case a credential could be
#: split, in a whitespace-free run longer than the largest content window.
MAX_TEXT_CARRY = 4 * 1024 * 1024

#: What `/transcript` and `/answer` say when the agent's stdout capture was cut
#: at its size cap (`agent_worker.procman.StreamCapture`, `keep_tail`).
CAPTURE_CUT_DETAIL = (
    "the agent's output passed its capture size cap: the capture kept the start "
    "and the end of the stream and dropped the middle, with a notice where, so "
    "this is not the whole run"
)

#: The headers every successful raw response carries, whatever it holds.
#: `nosniff` and a text/plain type are what stop an agent-written `.html` or
#: `.svg` rendering in the API's origin; the CSP and `sandbox` are the second
#: line if anything ever did render.
_RAW_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
    "Referrer-Policy": "no-referrer",
}

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def download_filename(name: str) -> str:
    """The `filename=` a download is saved as: the basename, every unusual byte `_`.

    Every segment of a served name has already passed `safe_segment`, so this
    changes nothing today. It is here so the quoted header value cannot be
    closed or extended by whatever a future layout lets through.
    """
    base = PurePosixPath(name).name or "artifact"
    return _FILENAME_UNSAFE.sub("_", base)


def sniff_image(head: bytes) -> str | None:
    """The image type the object's own MAGIC BYTES say it is, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class RawArtifact:
    """One artifact's bytes, ready to stream, and the headers that go with them."""

    chunks: Iterator[bytes]
    media_type: str
    headers: dict[str, str]
    kind: str


class AgentOutputService:
    """The Artifacts tab's reads. One per request, around the context's inspection."""

    def __init__(self, inspection: InspectionService, *, chunk_bytes: int = CHUNK_BYTES) -> None:
        self._ins = inspection
        self._chunk = max(1, chunk_bytes)

    # -- 1. the listing --------------------------------------------------------
    def list_artifacts(self, tenant_id: str, task_id: str, *, limit: int) -> dict[str, Any]:
        """The manifest, each entry with its attempt, kind, content type and role.

        No object is read: everything here is the manifest the worker wrote
        into `result_summary` plus the name table, so the listing costs what
        it always has. `complete: false` with `artifacts: []` is a task that
        has not finished -- artifacts are uploaded when the attempt ends, never
        live -- which the UI draws as "uploaded when the attempt ends", never
        as "none".

        Only the FINAL attempt's artifacts are listed: the manifest is written
        once, at terminal state, for the attempt that ended the task.
        """
        task, _prefix = self._ins._scoped(tenant_id, task_id)
        manifest = self._ins._store.artifact_manifest(task)
        declared = agent_streams_mod.declared_streams(task.result_summary)
        roles = {
            name: role
            for role, name in agent_streams_mod.stream_artifacts(
                task.runner_profile, declared
            ).items()
        }
        rows: list[dict[str, Any]] = []
        for entry in manifest.artifacts[:limit]:
            name = entry.get("name")
            usable = isinstance(name, str) and bool(name)
            kind, content_type = artifact_kind(name) if usable else ("binary", None)
            rows.append(
                {
                    **entry,
                    "attempt_id": _attempt_of_artifact_uri(
                        entry.get("uri"), tenant_id=task.tenant_id, task_id=task.id
                    ),
                    "kind": kind,
                    "content_type": content_type,
                    "role": roles.get(name) if usable else None,
                }
            )
        return {
            "task_id": task.id,
            "artifacts": rows,
            "artifacts_skipped": manifest.skipped,
            "artifact_bytes": manifest.artifact_bytes,
            "complete": manifest.complete,
            "attempt_id": manifest_attempt(task),
        }

    # -- 2. one artifact's text --------------------------------------------------
    def read_artifact(
        self,
        tenant_id: str,
        task_id: str,
        *,
        name: str,
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """`InspectionService.read_artifact`, plus the listing's `kind` and `content_type`."""
        row = self._ins.read_artifact(
            tenant_id, task_id, name=name, offset=offset, limit_bytes=limit_bytes
        )
        kind, content_type = artifact_kind(name)
        row["kind"] = kind
        row["content_type"] = content_type
        return row

    # -- 3. one artifact's bytes -------------------------------------------------
    def raw_artifact(
        self,
        tenant_id: str,
        task_id: str,
        *,
        name: str,
        disposition: str | None = None,
    ) -> RawArtifact:
        """An artifact's bytes, classified from its first 4096, streamed in bounded windows.

        RESOLVED EXACTLY AS THE CONTENT ROUTE RESOLVES A NAME
        (`InspectionService._resolve_artifact`): exact manifest match, key
        rebuilt from the task document, stored uri compared.

        THREE KINDS, decided from the head of the object:

          * IMAGE -- the name is in `IMAGE_EXTENSIONS` AND the magic bytes are
            a PNG, JPEG, GIF or WEBP. Served as that image type, byte for byte.
          * TEXT -- no NUL in the head. Served as `text/plain; charset=utf-8`
            WHATEVER the name says, so an `.html` or an `.svg` an agent wrote
            is shown as its source and never rendered; redacted window by
            window on the way out. Bytes that are not UTF-8 pass through
            exactly as stored -- only a credential-shaped run changes -- so
            a Latin-1 file downloads as itself, not as U+FFFD.
          * BINARY -- anything else. `application/octet-stream`, always an
            attachment (an `inline` request is ignored), not redacted -- no
            rule runs over bytes like these, and the header says so.

        EVERY ERROR BEFORE THE FIRST BYTE, as the JSON envelope: 404 when the
        manifest does not list the name, 410 when it does and the object is
        gone, 503 when the store cannot be read, 422 for a disposition that
        is neither `inline` nor `attachment`. A failure after the first byte
        cannot change the status already sent; the chunked body then ends
        without its final chunk, and `X-Artifact-Bytes` (the STORED size; a
        redacted text body may differ) lets a client see it came up short.

        NO Content-Length, for the reason `CheckpointContent.download` gives:
        Cloud Run refuses an unchunked HTTP/1 response over 32 MiB, and
        uvicorn chunks exactly when no length is declared.
        """
        if disposition not in (None, "inline", "attachment"):
            raise ValidationFailed(
                "disposition must be inline or attachment",
                detail={"disposition": disposition},
            )
        task, _prefix = self._ins._scoped(tenant_id, task_id)
        entry, attempt_id, key, reader = self._ins._resolve_artifact(task, name)
        try:
            first = reader.read_range(key, offset=0, length=self._chunk)
        except ObjectAbsent:
            raise Gone(
                f"task {task.id!r} lists {name!r}, and its object is no longer in the "
                "bucket: bucket retention reclaimed it, or the upload the manifest "
                "records never completed",
                detail={
                    "task_id": task.id,
                    "name": name,
                    "attempt_id": attempt_id,
                    "uri": entry.get("uri"),
                },
            ) from None
        except ObjectUnreadable as exc:
            raise UpstreamUnavailable(
                "the artifact store could not be read, so nothing may be concluded "
                "about this artifact: " + redact_detail(exc.reason)
            ) from None

        head = first.data[:ARTIFACT_SNIFF_BYTES]
        suffix = PurePosixPath(name).suffix.lower()
        image_type = sniff_image(head) if suffix in IMAGE_EXTENSIONS else None
        if image_type is not None:
            kind, media_type, redaction = "image", image_type, "not-applied"
        elif b"\x00" not in head:
            kind, media_type, redaction = "text", "text/plain; charset=utf-8", "applied"
        else:
            kind, media_type, redaction = "binary", "application/octet-stream", "not-applied"

        chosen = disposition or "attachment"
        if kind == "binary":
            chosen = "attachment"
        headers = {
            **_RAW_HEADERS,
            "Content-Disposition": f'{chosen}; filename="{download_filename(name)}"',
            "X-Artifact-Bytes": str(first.total_bytes),
            "X-Artifact-Kind": kind,
            "X-Swarm-Redaction": redaction,
        }
        if kind == "text":
            chunks = self._redacted_text(reader, key, first)
        else:
            chunks = self._verbatim(reader, key, first)
        return RawArtifact(chunks=chunks, media_type=media_type, headers=headers, kind=kind)

    def _next_window(self, reader: ObjectReader, key: str, at: int, total: int) -> ObjectSlice:
        """The window at `at`, or `DownloadAborted` -- the status is already sent."""
        try:
            piece = reader.read_range(key, offset=at, length=self._chunk)
        except (ObjectAbsent, ObjectUnreadable) as exc:
            reason = redact_detail(
                exc.reason if isinstance(exc, ObjectUnreadable) else "the object is gone"
            )
            log.error("artifact download of %s ended at byte %d of %d: %s", key, at, total, reason)
            raise DownloadAborted(
                f"artifact download ended at byte {at} of {total}: {reason}"
            ) from None
        if piece.total_bytes != total or not piece.data:
            log.error(
                "artifact download of %s ended at byte %d of %d: the object changed size "
                "or returned nothing", key, at, total,
            )
            raise DownloadAborted(
                f"artifact download ended at byte {at} of {total}: the artifact changed "
                "while it was being sent"
            )
        return piece

    def _verbatim(self, reader: ObjectReader, key: str, first: ObjectSlice) -> Iterator[bytes]:
        total = first.total_bytes
        if first.data:
            yield first.data
        at = first.end
        while at < total:
            piece = self._next_window(reader, key, at, total)
            yield piece.data
            at = piece.end

    def _redacted_text(
        self, reader: ObjectReader, key: str, first: ObjectSlice
    ) -> Iterator[bytes]:
        """The whole object, redacted one whitespace-bounded window at a time.

        The same boundary rule the paged routes apply (`inspect._last_boundary`:
        the last newline, else the last whitespace): each window is cut after
        it and the remainder carried into the next, so every token is redacted
        whole. A private key is a TOKEN OF LINES: a window that would end
        inside one is cut before its BEGIN marker instead, and the key carried
        whole into the next (up to `redaction.PEM_BLOCK_MAX_CHARS`), so it is
        masked as one block. Concatenated, the windows are the redaction of the
        whole object.
        """
        total = first.total_bytes
        carry = b""
        piece = first
        at = first.end
        while True:
            buffer = carry + piece.data
            if at >= total:
                if buffer:
                    yield _scrub(buffer)
                return
            cut = _last_boundary(buffer)
            if cut >= 0:
                begin = open_key_start(buffer, cut + 1)
                if begin is not None:
                    # -1 when the key is all this window holds: carry it.
                    cut = _last_boundary(buffer[:begin])
            if cut >= 0:
                yield _scrub(buffer[: cut + 1])
                carry = buffer[cut + 1 :]
            elif len(buffer) >= MAX_TEXT_CARRY:
                split = _char_boundary(buffer) or len(buffer)
                yield _scrub(buffer[:split])
                carry = buffer[split:]
            else:
                carry = buffer
            piece = self._next_window(reader, key, at, total)
            at = piece.end

    # -- 4. the transcript -------------------------------------------------------
    def read_transcript(
        self,
        tenant_id: str,
        task_id: str,
        *,
        attempt_id: str | None = None,
        source: str = "auto",
        offset: int = 0,
        limit_bytes: int | None = None,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        """One window of the agent's stdout, as steps.

        THE SAME OBJECT `/logs?stream=agent_stdout` WOULD SERVE, chosen by the
        same `_open_stream` -- the final copy, the live tail, or, for an
        attempt made before #184, the manifest entry the runner wrote. Windows
        are cut on NEWLINES only, so an NDJSON line is never split: a window
        that begins mid-line drops the fragment, and one line longer than the
        window becomes a single `other` step with no text and a `detail`,
        which the next page skips past. Default 512 KiB, clamped to 4 KiB..4
        MiB like an artifact window.

        HONEST ABOUT WHAT IT IS NOT. `complete` is true only for the final
        record read from its start to its end. `window_starts_mid_stream` is
        true for any offset past 0 and for a live tail whose window starts
        after the stream's first byte -- the UI says "earlier steps are not in
        this window". Absent and unreadable are 200s with `stream.status`,
        three answers, not errors.

        A CAPTURE CUT AT ITS CAP IS NEVER COMPLETE (#188 review). The worker's
        capture of a stream past `max_stdout_bytes` keeps its start and its
        end and drops the middle, with a notice line where. `capture_truncated`
        is true when this window holds that notice or the worker reported the
        cut for this attempt (`result_summary.agent_streams.stdout_truncated`);
        false when the worker reported none, or this read is the whole final
        object and holds no notice; null when neither can be said. True makes
        `complete` false and says why in `stream.detail`.
        """
        ins = self._ins
        task, _prefix = ins._scoped(tenant_id, task_id)
        if offset < 0:
            raise ValidationFailed("offset must not be negative")
        if source not in ("auto", "final", "live"):
            raise ValidationFailed("source must be one of: auto, final, live")
        window = ins._default_artifact_bytes if limit_bytes is None else limit_bytes
        if window < 1:
            raise ValidationFailed("limit_bytes must be at least 1")
        window = min(max(window, ins._min_artifact_bytes), ins._max_artifact_bytes)

        attempts = ins._attempt_order(tenant_id, task_id)
        chosen, attempt_status = ins._choose_attempt(attempts, attempt_id)
        read_at = ins._now()
        body: dict[str, Any] = {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "attempt_id": chosen,
            "attempt": ins._attempt_block(attempts, chosen, attempt_status),
            "read_at": _iso(read_at),
            "stream": _stream_block("agent_stdout", None, "absent"),
            "format": None,
            "steps": None,
            "complete": False,
            "window_starts_mid_stream": offset > 0,
            "skipped_lines": 0,
            "answer_in_window": False,
            "capture_truncated": None,
            "redaction": {"applied_at_read_time": True, "rules": len(REDACTION_RULES)},
            "redaction_count": 0,
        }
        if chosen is None:
            body["stream"]["detail"] = "this task has no attempt yet, so it has no transcript"
            return body
        # What the worker reported about this attempt's capture. The summary
        # describes the manifest's attempt only.
        declared_cut = (
            agent_streams_mod.declared_truncation(task.result_summary, "stdout")
            if chosen == manifest_attempt(task)
            else None
        )

        reader = ins._reader()
        base = attempt_prefix(tenant_id=task.tenant_id, task_id=task.id, attempt_id=chosen)
        probe = 1 if offset > 0 else 0
        opened = ins._open_stream(
            task=task, base=base, attempt_id=chosen, stream="agent_stdout",
            source=source, offset=offset - probe, length=window + probe, reader=reader,
        )
        stream = _stream_block("agent_stdout", opened.source, opened.status)
        stream.update(uri=opened.uri, detail=opened.detail)
        body["stream"] = stream
        if opened.status != "ok" or opened.chunk is None:
            return body

        chunk = opened.chunk
        stream.update(_ages(chunk, read_at))
        stream["total_bytes"] = chunk.total_bytes
        if not chunk.data:
            # A zero-byte object, or a page past the end: an object that
            # EXISTS and holds nothing here -- no steps, not "no transcript".
            whole = opened.source in ("final", "artifact") and offset == 0
            cut = _capture_cut(seen=False, declared=declared_cut, whole=whole)
            stream.update(offset=min(offset, chunk.total_bytes), next_offset=None)
            body.update(
                format=None,
                steps=[],
                complete=whole and cut is not True,
                capture_truncated=cut,
            )
            return body
        data = chunk.data
        previous = data[:probe]
        raw = data[probe:]
        start = chunk.offset + probe
        # STREAM offset minus OBJECT offset. Zero for the final copy and the
        # artifact, whose bytes are the stream's; for a live tail, the header's
        # `offset` (where its window starts in the stream) minus the header's
        # own length. Step ids and `line_offset` are stream offsets, so the
        # same line keeps the same id from one live poll to the next.
        delta = 0
        tail_window = None
        if opened.source == "live" and start == 0:
            tail_window, header_end = parse_tail_header(raw)
            raw = raw[header_end:]
            start += header_end
            if tail_window is not None:
                delta = tail_window["object_offset"] - header_end
        stream["tail_window"] = tail_window

        at_eof = chunk.end >= chunk.total_bytes
        lines, start, end, oversize, detail = _align_lines(
            raw, start=start, previous=previous, at_eof=at_eof, read_end=chunk.end
        )
        base_offset = start + delta
        complete_object = end >= chunk.total_bytes
        stream.update(
            offset=start,
            returned_bytes=len(lines),
            next_offset=None if complete_object else end,
            truncated=not complete_object,
        )
        if detail:
            stream["detail"] = detail

        seen_cut = False
        if oversize:
            body.update(format=None, steps=[_oversize_step(base_offset)])
        else:
            parsed = parse_window(
                lines,
                base_offset=base_offset,
                whole_object=(
                    opened.source in ("final", "artifact") and start == 0 and complete_object
                ),
                include_raw=include_raw,
            )
            seen_cut = parsed.capture_truncated
            body.update(
                format=parsed.format,
                steps=parsed.steps,
                skipped_lines=parsed.skipped_lines,
                answer_in_window=parsed.answer_in_window,
                redaction_count=parsed.redaction_count,
            )
        whole = opened.source in ("final", "artifact") and offset == 0 and complete_object
        cut = _capture_cut(seen=seen_cut, declared=declared_cut, whole=whole)
        body["capture_truncated"] = cut
        body["complete"] = whole and cut is not True
        if cut is True and stream.get("detail") is None:
            stream["detail"] = CAPTURE_CUT_DETAIL
        body["window_starts_mid_stream"] = offset > 0 or bool(
            tail_window and tail_window["object_offset"] > 0
        )
        return body

    # -- 5. the answer -----------------------------------------------------------
    def read_answer(
        self, tenant_id: str, task_id: str, *, attempt_id: str | None = None
    ) -> dict[str, Any]:
        """The agent's final answer: the LAST `result` event's `result`, redacted.

        RESOLUTION. The agent's stdout is tried final copy, then the pre-#184
        artifact, then the live tail, and the FIRST object that exists is read
        -- its last 4 MiB, which holds the result of a stream-json run (its
        last line) and the whole of a json-format one. When that object holds
        no result event, and this is the attempt the manifest describes,
        `result_summary.runner.summary` is the fallback (`source:
        runner_summary`, `format: text`), marked `complete: false` when it is
        at the runner's 2,000-character cap -- it may have been cut -- and
        null otherwise.

        FOUR ANSWERS. `ok` -- here it is (`is_error` true is still ok: the
        agent reported an error, and that report is the answer); `not_yet` --
        the attempt is running and has no result yet; `absent` -- the attempt
        ended and neither source exists; `unreadable` -- a read FAILED, and
        nothing further down the chain is tried, exactly as `/logs` never
        falls back past a failure.

        `capture_truncated` (#188 review): whether the agent's stdout capture
        was cut at its size cap, as `/transcript` decides it. The capture
        keeps the END of the stream, where the result event is, so a cut
        capture still answers whole; the flag and `detail` say the rest of the
        run was not all kept, and why a result event may be missing.

        Decoded strings are redacted with `decoded=True`: at least as far as
        their raw line would be (see `swarm_api.transcript`).
        """
        ins = self._ins
        task, _prefix = ins._scoped(tenant_id, task_id)
        attempts = ins._attempt_order(tenant_id, task_id)
        chosen, attempt_status = ins._choose_attempt(attempts, attempt_id)
        read_at = ins._now()
        body: dict[str, Any] = {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "attempt_id": chosen,
            "attempt": ins._attempt_block(attempts, chosen, attempt_status),
            "read_at": _iso(read_at),
            "status": "absent",
            "source": None,
            "object": None,
            "format": None,
            "content": None,
            "complete": None,
            "is_error": None,
            "subtype": None,
            "stop_reason": None,
            "terminal_reason": None,
            "num_turns": None,
            "bytes": None,
            "redacted": False,
            "redaction_count": 0,
            "capture_truncated": None,
            "detail": None,
        }
        if chosen is None:
            terminal = task.state in TERMINAL_STATES
            body["status"] = "absent" if terminal else "not_yet"
            body["detail"] = (
                "the task ended without an attempt, so there is no answer"
                if terminal
                else "the task has not started an attempt yet"
            )
            return body

        record = attempts.get(chosen)
        latest = (
            max(attempts.values(), key=lambda a: (a.created_at, a.attempt_id)).attempt_id
            if attempts
            else None
        )
        # An attempt with no document cannot be running: nothing admitted it.
        over = True if record is None else attempt_is_over(record, task, is_latest=chosen == latest)
        declared_cut = (
            agent_streams_mod.declared_truncation(task.result_summary, "stdout")
            if chosen == manifest_attempt(task)
            else None
        )
        body["capture_truncated"] = declared_cut

        reader = ins._reader()
        base = attempt_prefix(tenant_id=task.tenant_id, task_id=task.id, attempt_id=chosen)
        opened = ins._open_stream(
            task=task, base=base, attempt_id=chosen, stream="agent_stdout",
            source="auto", offset=0, length=ANSWER_TAIL_BYTES, reader=reader,
            order=("final", "artifact", "live"),
        )
        if opened.status == "unreadable":
            body.update(
                status="unreadable",
                detail="the agent's output could not be read, so nothing may be "
                f"concluded about its answer: {opened.detail}",
                object=_answer_object(opened, None),
            )
            return body
        if opened.status == "ok" and opened.chunk is not None:
            try:
                chunk, starts_mid_line = self._answer_window(reader, opened)
            except ObjectUnreadable as exc:
                body.update(
                    status="unreadable",
                    detail="the end of the agent's output could not be read: "
                    + redact_detail(exc.reason),
                    object=_answer_object(opened, None),
                )
                return body
            except ObjectAbsent:
                chunk, starts_mid_line = None, False
            if chunk is not None:
                data = chunk.data
                if opened.source == "live" and chunk.offset == 0:
                    _window, header_end = parse_tail_header(data)
                    data = data[header_end:]
                whole = (
                    opened.source in ("final", "artifact")
                    and chunk.offset == 0
                    and chunk.end >= chunk.total_bytes
                )
                body["capture_truncated"] = _capture_cut(
                    seen=agent_streams_mod.has_truncation_notice(data),
                    declared=declared_cut,
                    whole=whole,
                )
                event = last_result_event(data, starts_mid_line=starts_mid_line)
                if event is not None:
                    self._fill_from_event(body, event, opened, chunk)
                    if body["capture_truncated"] is True:
                        body["detail"] = (
                            CAPTURE_CUT_DETAIL + "; the result event, its last line, was "
                            "kept whole"
                        )
                    return body
        # Why a result event may be missing, when it may be: said first.
        cut_note = (
            CAPTURE_CUT_DETAIL + ", and no result event is in what was kept; "
            if body["capture_truncated"] is True
            else ""
        )

        summary = task.result_summary if isinstance(task.result_summary, dict) else {}
        runner = summary.get("runner") if isinstance(summary.get("runner"), dict) else {}
        text = runner.get("summary")
        if chosen == manifest_attempt(task) and isinstance(text, str) and text.strip():
            scrubbed = redact(text, decoded=True)
            body.update(
                status="ok",
                source="runner_summary",
                format="text",
                content=scrubbed.text,
                complete=False if len(text) >= RUNNER_SUMMARY_CAP else None,
                bytes=len(text.encode("utf-8")),
                redacted=scrubbed.any,
                redaction_count=scrubbed.count,
                detail=cut_note + (
                    "the agent's output holds no result event, so this is the runner's "
                    "summary, which the runner cuts at 2,000 characters"
                ),
            )
            return body

        if over:
            body.update(
                status="absent",
                detail=cut_note
                + "the attempt ended and left neither a result event nor a runner summary",
            )
        else:
            body.update(
                status="not_yet",
                detail=cut_note
                + "the attempt is still running and has not reported a result yet",
            )
        return body

    def _answer_window(
        self, reader: ObjectReader, opened: OpenedStream
    ) -> tuple[ObjectSlice, bool]:
        """The last `ANSWER_TAIL_BYTES` of the chosen object, and whether it starts mid-line."""
        chunk = opened.chunk
        assert chunk is not None and opened.key is not None
        if chunk.total_bytes <= ANSWER_TAIL_BYTES:
            return chunk, False
        start = chunk.total_bytes - ANSWER_TAIL_BYTES
        tail = reader.read_range(opened.key, offset=start - 1, length=ANSWER_TAIL_BYTES + 1)
        previous, data = tail.data[:1], tail.data[1:]
        window = ObjectSlice(
            key=tail.key, offset=tail.offset + 1, data=data, total_bytes=tail.total_bytes,
            updated=tail.updated, generation=tail.generation,
        )
        return window, previous != b"\n"

    @staticmethod
    def _fill_from_event(
        body: dict[str, Any], event: dict[str, Any], opened: OpenedStream, chunk: ObjectSlice
    ) -> None:
        result = event.get("result")
        text = result if isinstance(result, str) else None
        scrubbed = redact(text, decoded=True) if text is not None else None
        is_error = event.get("is_error")
        num_turns = event.get("num_turns")
        body.update(
            status="ok",
            source="agent_result_event",
            object=_answer_object(opened, chunk),
            format="markdown" if text is not None else None,
            content=scrubbed.text if scrubbed is not None else None,
            complete=True if text is not None else None,
            is_error=is_error if isinstance(is_error, bool) else None,
            subtype=_text_or_none(event.get("subtype")),
            stop_reason=_text_or_none(event.get("stop_reason")),
            terminal_reason=_text_or_none(event.get("terminal_reason")),
            num_turns=num_turns
            if isinstance(num_turns, int) and not isinstance(num_turns, bool)
            else None,
            bytes=len(text.encode("utf-8")) if text is not None else None,
            redacted=bool(scrubbed and scrubbed.any),
            redaction_count=scrubbed.count if scrubbed is not None else 0,
            detail=None if text is not None else "the result event carries no result text",
        )


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact(value, decoded=True).text


def _capture_cut(*, seen: bool, declared: bool | None, whole: bool) -> bool | None:
    """Whether the agent's stdout capture was cut at its cap: yes, no, or not known.

    Yes when the read holds the capture's notice or the worker reported the
    cut; no when the worker reported none, or the read is the whole final
    object and holds no notice; otherwise nothing can be said.
    """
    if seen or declared is True:
        return True
    if declared is False or whole:
        return False
    return None


def _answer_object(opened: OpenedStream, chunk: ObjectSlice | None) -> dict[str, Any] | None:
    if opened.source is None:
        return None
    updated = _iso(chunk.updated) if chunk is not None and chunk.updated is not None else None
    return {
        "stream": "agent_stdout",
        "source": opened.source,
        "uri": opened.uri,
        "object_updated_at": updated,
    }


def _stream_block(stream: str, source: str | None, status: str) -> dict[str, Any]:
    """The `stream` block of a transcript read, in its constant shape."""
    return {
        "stream": stream,
        "source": source,
        "status": status,
        "detail": None,
        "uri": None,
        "object_updated_at": None,
        "age_seconds": None,
        "total_bytes": None,
        "offset": 0,
        "returned_bytes": 0,
        "next_offset": None,
        "truncated": False,
        "tail_window": None,
    }


def _oversize_step(line_offset: int) -> dict[str, Any]:
    return {
        "id": f"L{line_offset}:0",
        "line_offset": line_offset,
        "block": 0,
        "kind": "other",
        "role": None,
        "parent_tool_use_id": None,
        "text": None,
        "tool": None,
        "tool_result": None,
        "meta": {"oversize": True},
        "truncated_fields": [],
        "raw": None,
    }


def _align_lines(
    raw: bytes, *, start: int, previous: bytes, at_eof: bool, read_end: int
) -> tuple[bytes, int, int, bool, str | None]:
    """Move both ends of a transcript window onto line boundaries.

    Returns `(lines, start, end, oversize, detail)`; `start` and `end` are
    absolute object offsets. A window that began mid-line drops the fragment;
    one that ends mid-line leaves the partial line for the next page. When the
    window starts on a line and holds no newline at all, that line is longer
    than the window: `oversize` is True, nothing is decoded, and paging moves
    past what was read. When it began inside such a line, the page is empty
    and moves on the same way.
    """
    if previous and previous != b"\n":
        newline = raw.find(b"\n")
        if newline < 0:
            return b"", start, read_end, False, (
                "this window began inside a line longer than the window; it was "
                "skipped, and the next page continues after it"
            )
        raw = raw[newline + 1 :]
        start += newline + 1
    if at_eof:
        return raw, start, start + len(raw), False, None
    cut = raw.rfind(b"\n")
    if cut < 0:
        return b"", start, read_end, True, (
            "one line of the agent's output is longer than this window, so it is "
            "shown as a single step with no text; raise limit_bytes (up to 4 MiB) "
            "to read it"
        )
    raw = raw[: cut + 1]
    return raw, start, start + len(raw), False, None


def _scrub(data: bytes) -> bytes:
    """One window of a text download, redacted, every other byte exactly as stored.

    `surrogateescape` both ways (#188 review): a byte that is not UTF-8 becomes
    a lone surrogate, which no rule matches and the encode turns back into the
    same byte. It used to be `errors="replace"`, which turned every such byte
    into U+FFFD -- a Latin-1 CSV downloaded corrupted, and nothing said so.
    """
    text = data.decode("utf-8", errors="surrogateescape")
    return redact(text).text.encode("utf-8", errors="surrogateescape")


def _char_boundary(data: bytes) -> int:
    """`len(data)`, less the bytes of a UTF-8 character left unfinished at its end.

    A window boundary can fall inside a multi-byte character; decoding the
    fragment would turn it into U+FFFD on both sides of the cut. Only the
    last four bytes can belong to an unfinished character.
    """
    size = len(data)
    for back in range(1, min(4, size) + 1):
        byte = data[size - back]
        if byte & 0xC0 == 0x80:
            continue  # a continuation byte: keep looking for its lead byte
        if byte & 0x80 == 0:
            return size  # ASCII: nothing is unfinished
        if byte & 0xE0 == 0xC0:
            need = 2
        elif byte & 0xF0 == 0xE0:
            need = 3
        elif byte & 0xF8 == 0xF0:
            need = 4
        else:
            return size  # not a lead byte UTF-8 knows; leave it to the decoder
        return size if back >= need else size - back
    return size
