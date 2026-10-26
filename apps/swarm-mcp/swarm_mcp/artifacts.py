"""`swarm_artifact`'s save mode: a task's image artifact, written to a local file (#2182).

WHY. `/artifacts/content` answers `binary` with no bytes for a PNG -- bytes
nothing can scan for a credential are bytes that route does not serve -- so a
Claude Code session could list a lane's screenshots and never look at one. On
2026-10-23 a mock-up review re-rendered every page locally and forked 15
review agents instead. `/artifacts/raw` already serves an image byte for byte,
behind the same tenant check; this module writes what it serves to
`<save_to>/<task_id>/<name>`, where the session reads it with its own Read tool.

IMAGES ONLY. A name with an image extension is read through the raw route, and
saved only when the route ITSELF says the bytes are an image (`X-Artifact-Kind:
image`, which it decides from the extension AND the magic bytes). A text
artifact keeps the redacted, windowed read -- with or without `save_to` -- so
nothing unredacted reaches a file, and a binary that is not an image stays
`binary` with no bytes, as before.

THE TENANT BOUNDARY IS THE ROUTE'S. Nothing here decides whose task this is:
another tenant's task is the route's 404, raised exactly as the text read
raises it.

WHERE A FILE MAY GO. The arguments arrive from a model, so a `save_to` that
resolves -- symlinks included -- outside the bridge's working directory or the
user's Claude Code temp directory (where the session's scratchpad lives) is
refused, and so is a name or a task id that could climb out of `save_to`. Every
refusal happens BEFORE any request: a refused call reads nothing and writes
nothing.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .client import SwarmClient, SwarmError

#: The extensions the raw route serves as an image -- swarm-api's
#: `checkpoint_content.IMAGE_EXTENSIONS`, which this bridge cannot import (it
#: ships alone, from a git tag, with no swarm-api beside it). SVG is not here
#: because the route serves it as text: it can carry script.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: The most one saved artifact may be. A screenshot is a few MiB; the body is
#: held in memory before it is written, so a bound is what keeps an
#: unexpectedly huge object from taking the bridge -- and the session -- down.
MAX_SAVED_BYTES = 64 * 1024 * 1024

#: The most one image shown INLINE may be (ARTIFACT-IMAGES). Its bytes travel
#: base64 in one JSON-RPC line and land in the session's context: a 1440p
#: screenshot is well under 1 MiB, and a larger image is the console's to show,
#: which the over-cap answer links to.
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024

#: A task id is one path segment of this shape. It becomes a directory name.
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: `C:` and friends: absolute on Windows whatever PurePosixPath thinks.
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class SavePlan:
    """Where an image artifact would be written, checked before any request."""

    directory: Path
    parts: tuple[str, ...]

    @property
    def is_image_name(self) -> bool:
        return is_image_name(self.parts[-1])


def claude_temp_roots() -> list[Path]:
    """The directories Claude Code keeps a session's scratchpad under.

    Claude Code puts it at `<tmp>/claude-<uid>/<project>/<session>/scratchpad`,
    with `<tmp>` `/tmp` (macOS resolves it to `/private/tmp`) or the
    platform's temp directory; older builds used `<tmp>/claude`. Only this
    user's directory is accepted, resolved, so a symlink cannot widen it.
    """
    bases = {Path("/tmp"), Path(tempfile.gettempdir())}
    names = ["claude"]
    if hasattr(os, "getuid"):
        names.insert(0, f"claude-{os.getuid()}")
    return sorted({(base / name).resolve() for base in bases for name in names})


def _refuse(why: str) -> SwarmError:
    return SwarmError(f"swarm_artifact: {why}. Nothing was read and nothing was written")


def plan_save(task_id: str, name: str, save_to: str, *, base: Path) -> SavePlan:
    """Check `task_id`, `name` and `save_to`, and say where the file would go.

    Raises SwarmError -- before anything is requested or written -- for a
    `save_to` outside `base` (the bridge's working directory) and the Claude
    temp roots, a name that is absolute or has a `..` segment, and a task id
    that is not one plain path segment.
    """
    if not _TASK_ID.match(task_id):
        raise _refuse(f"task id {task_id!r} is not a task id; it would become a directory name")
    if not name or "\x00" in name:
        raise _refuse("an empty name, or one with a NUL, is not an artifact name")
    if name.startswith(("/", "\\")) or _DRIVE.match(name):
        raise _refuse(f"{name!r} is an absolute path; an artifact name is relative to `save_to`")
    parts = tuple(part for part in re.split(r"[/\\]", name) if part not in ("", "."))
    if ".." in parts:
        raise _refuse(f"{name!r} has a `..` segment, which could write outside `save_to`")
    if not parts:
        raise _refuse(f"{name!r} names no file")

    root = base.expanduser().resolve()
    target = Path(save_to).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    allowed = [root, *claude_temp_roots()]
    if not any(target.is_relative_to(place) for place in allowed):
        raise _refuse(
            f"save_to {save_to!r} resolves to {target}, outside this bridge's working "
            f"directory {root} and the Claude Code temp directories "
            f"{[str(place) for place in allowed[1:]]} (symlinks resolved). Save under "
            "the project or the session's scratchpad"
        )
    return SavePlan(directory=target / task_id, parts=parts)


def save_image(client: SwarmClient, task_id: str, name: str, plan: SavePlan) -> dict[str, Any] | None:
    """Read `name` through `/artifacts/raw` and write it, if the route says it is an image.

    Returns the `saved` answer, or None when the route classified the bytes as
    text or a non-image binary -- the caller then gives today's redacted read,
    which redacts text and reports a binary as `binary`. Nothing is written in
    that case: the raw route's text is redacted, but a file is not where a
    windowed, redacted read belongs.
    """
    body = client.artifact_raw(task_id, name, max_bytes=MAX_SAVED_BYTES)
    content_type = body.headers.get("content-type", "").split(";")[0].strip()
    if body.headers.get("x-artifact-kind") != "image" or not content_type.startswith("image/"):
        return None
    if sniff_image(body.data) != content_type:
        # The bridge's own magic-byte check, as `show_image` makes it: no
        # unredacted byte is written on the route's word alone.
        return None
    stored = body.headers.get("x-artifact-bytes")
    if stored is not None and stored.isdigit() and int(stored) != len(body.data):
        # The route streams chunked, and a failure after the first byte can
        # only end the body early; the stored size is how that is seen.
        raise SwarmError(
            f"swarm_artifact: {name!r} is {stored} bytes stored and {len(body.data)} arrived; "
            "the read came up short, so nothing was written. Call again"
        )

    plan.directory.mkdir(parents=True, exist_ok=True)
    parent = plan.directory.joinpath(*plan.parts[:-1])
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve()
    if not parent.is_relative_to(plan.directory.resolve()):
        # A symlink already inside save_to that points out of it.
        raise SwarmError(
            f"swarm_artifact: {parent} is outside {plan.directory} after symlinks are "
            "resolved; nothing was written"
        )
    target = parent / plan.parts[-1]
    partial = parent / f".{plan.parts[-1]}.partial"
    partial.write_bytes(body.data)
    os.replace(partial, target)
    return {
        "status": "saved",
        "path": str(target),
        "bytes": len(body.data),
        "content_type": content_type,
        "task_id": task_id,
        "name": name,
    }


def is_image_name(name: str) -> bool:
    """Whether the raw route could ever serve `name` as an image: its extension."""
    return PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES


def sniff_image(data: bytes) -> str | None:
    """The image type the bytes' own MAGIC BYTES say they are, or None.

    swarm-api's `agent_output.sniff_image`, restated because this bridge ships
    alone, from a git tag. It is the bridge's own check that what it shows is
    an image, whatever a header said.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class ImageAnswer:
    """A tool answer that carries an image: the metadata as text, then the bytes.

    `serve` sends `content()` as the call's content blocks; everything else
    that reads a tool's answer (the build board) reads `text`.
    """

    text: str
    data: bytes
    mime_type: str

    def content(self) -> list[dict[str, Any]]:
        return [
            {"type": "text", "text": self.text},
            {"type": "image", "data": base64.b64encode(self.data).decode("ascii"), "mimeType": self.mime_type},
        ]


#: What every answer about an image says, because it is true of every one.
_NOT_REDACTED = (
    "image bytes are NOT redacted: no scanner reads pixels, so a screenshot of a page that "
    "showed a value carries it, exactly as the console's image viewer does"
)


def _too_large(task_id: str, name: str, stored: int | None, console: str | None) -> str:
    answer: dict[str, Any] = {
        "status": "image_too_large",
        "task_id": task_id,
        "name": name,
        "bytes": stored,
        "max_bytes": MAX_INLINE_IMAGE_BYTES,
        "redaction": "not-applied",
        "detail": (
            f"this image is over the {MAX_INLINE_IMAGE_BYTES} bytes this bridge shows inline, "
            "so no bytes were returned. Open it in the console, or call again with `save_to` "
            "to write it to a file you read with the Read tool"
        ),
    }
    if console is not None:
        answer["console"] = console
    return json.dumps(answer, indent=2)


def show_image(
    client: SwarmClient,
    task_id: str,
    name: str,
    *,
    stored_bytes: Any = None,
    console: Any = None,
) -> ImageAnswer | str | None:
    """Read `name` through `/artifacts/raw` and answer it as image content.

    Returns the ImageAnswer; a JSON string answering the size and the console
    link (`console()`, called only then) for an image over
    `MAX_INLINE_IMAGE_BYTES`; or None when the bytes are not an image -- by
    the route's `x-artifact-kind`, its content type, or the magic bytes read
    here -- and the caller gives today's read, which redacts text and reports
    a binary as `binary` with no bytes. `stored_bytes` is the size the content
    read already reported, which refuses an oversized image before its bytes
    are requested at all.
    """

    def _link() -> str | None:
        return console() if callable(console) else None

    stored = stored_bytes if isinstance(stored_bytes, int) and not isinstance(stored_bytes, bool) else None
    if stored is not None and stored > MAX_INLINE_IMAGE_BYTES:
        return _too_large(task_id, name, stored, _link())
    try:
        body = client.artifact_raw(task_id, name, max_bytes=MAX_INLINE_IMAGE_BYTES, disposition="inline")
    except SwarmError as exc:
        # A 2xx with more bytes than the cap: the client kept none of them.
        if exc.status is not None and 200 <= exc.status < 300:
            return _too_large(task_id, name, stored, _link())
        raise
    content_type = body.headers.get("content-type", "").split(";")[0].strip()
    if body.headers.get("x-artifact-kind") != "image" or not content_type.startswith("image/"):
        return None
    if sniff_image(body.data) != content_type:
        return None
    header = body.headers.get("x-artifact-bytes")
    if header is not None and header.isdigit() and int(header) != len(body.data):
        raise SwarmError(
            f"swarm_artifact: {name!r} is {header} bytes stored and {len(body.data)} arrived; "
            "the read came up short, so nothing is shown. Call again"
        )
    metadata = {
        "status": "image",
        "task_id": task_id,
        "name": name,
        "bytes": len(body.data),
        "content_type": content_type,
        "redaction": "not-applied",
        "detail": f"the image follows as image content; {_NOT_REDACTED}",
    }
    return ImageAnswer(text=json.dumps(metadata, indent=2), data=body.data, mime_type=content_type)
