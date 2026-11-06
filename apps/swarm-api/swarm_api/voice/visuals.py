"""Voice it's visuals and notes: stored, served, annotated and turned into work
(docs/design/voice-it-own-speech.md §8; build plan §14, L9).

A VISUAL is what the Talk page shows large in the centre of the Presence
layout (§8.1), one of `KINDS`:

  mockup   HTML frames the planner drew, shown only in a sandboxed frame
  diagram  Mermaid text or architecture JSON the planner drew
  spec     Markdown the planner wrote; each heading is an addressable part
  code     a file slice (`show_file`) or a pull request's diff (`show_diff`),
           read HERE from the tenant's repository, never sent by the planner
  log      a failed step's log tail (`show_log`), read HERE from the task's
           own events

THE SHAPES (§8.2), documents under the project, so a project's own documents
are where its visuals are:

  voice_projects/{p}/visuals/{v}
      {visual_id, kind, title, version, source, parts, lines, body_ref,
       created_by, created_at, updated_at, session_id, tenant_id, person}
  voice_projects/{p}/visuals/{v}/notes/{n}
      {note_id, visual_id, visual_version, anchor, text, by, via, state, work,
       created_at, updated_at, session_id, tenant_id, person}
  voice_projects/{p}/pointers/{s}
      the page's latest pointer on a visual for one session (§8.3, spoken notes)

  anchor: {type: "point", x, y} | {type: "region", x, y, w, h}  (0-1 of the frame)
        | {type: "part", part_id} | {type: "lines", from, to} | null (the whole visual)

EVERY BODY LIVES UNDER THE TENANT'S OWN PREFIX (invariant 9):
`tenants/<t>/voice/<p>/visuals/<v>/<version>.txt`, made only by
`store.blob_key`, which refuses anything outside `tenants/<t>/voice/<p>/`, and
created with `if_generation_match=0`, so a version is never rewritten. The
tenant and the person are the ones the route resolved (the session document's,
or the console's selected workspace), never a field of the body; a project of
another tenant or another person is the same 404 as a missing one
(`VoiceStore._owned`). A body is served as text inside JSON, never as HTML.

A REPOSITORY READ GOES THROUGH THE SESSION'S GRANT SCOPE (§8.1's `code` row).
`repository_scope` answers for the person the request runs as -- the session's
person under a session grant, the caller otherwise -- in the session's tenant:
the repository must be registered in that tenant (`repositories/<repo_id>`,
`repo_id_for(tenant, owner, repo)`, its `tenant_id` checked again), and the
person must hold a `read` or `write` grant on it (`access.grant_id_for`, else
the workspace's own grant, `access.person_grant_from_workspace`, exactly as a
task submission is judged). Without both it is 403 and GitHub is never asked.
The read itself uses the GitHub App INSTALLATION token for that one repository
with read permissions only (`forge_installation.read_token`), never a person's
token (lesson 43); an owner without an installation is a 409 naming the
repository. The token is used for one request and is never logged, stored or
returned. What the read returns is passed through the shared redaction rules
before it is stored, so a credential in a file is never shown.

A LOG is read from the task's own events through the same store the task
routes read (`Store.get_task` with `submitted_by`, then `list_events`), in the
session's tenant: a task of another tenant is the same 404 as a missing one.

A NOTE ATTACHES TO A PLACE (§8.3): a typed or tapped note carries its anchor; a
spoken note takes the anchor the page last sent for this session (`point`,
under `POINTER_MAX_AGE_SECONDS` old, on the same visual), or a part the
planner names from the visual's `parts`; with neither it is pinned to the whole
visual. A part anchor must name one of the visual's parts and a lines anchor
must fall inside its lines, or the note is 422.

NOTES BECOME WORK (§8.4), one of `WORK_KINDS`, each note then `state: "work"`
with `work: {kind, ref}`:

  follow_up   an item on the project's open list in its brief (`brief.open`)
  issue       a proposed Work card (target_kind "issue") naming the notes, which
              the act path files as heard; the card is the reference
  lane_brief  a Plan card whose lane's territory is the visual's source files

THE STREAM. Each write appends its event to the session's stream through
`VoiceStore.append_event`: `visual.upsert` (the visual's view, no body),
`note.upsert` (the note) and `focus` (`speak`'s `point_at`, §8.5: the visual,
the anchor and the caption the page rings while the speech plays).
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote

from google.cloud import firestore
from swarm_common.admission import _snapshot
from swarm_common.states import EventType
from swarm_redaction import redact

from ..access import GRANTS, MODES, grant_id_for, person_grant_from_workspace
from ..errors import Conflict, Forbidden, NotFound, UpstreamUnavailable, ValidationFailed
from ..forge_installation import InstallationSource, read_token
from ..repositories import repo_id_for
from . import cards as vcards
from . import ledger as vledger
from . import sessions as vs
from . import store as vstore

log = logging.getLogger("swarm_api.voice.visuals")

VISUALS = "visuals"
NOTES = "notes"
POINTERS = "pointers"
REPOSITORIES = "repositories"

KINDS: tuple[str, ...] = ("mockup", "diagram", "spec", "code", "log")
#: The kinds the planner sends a body for; the others swarm-api reads itself.
DRAWN_KINDS: tuple[str, ...] = ("mockup", "diagram", "spec")
READ_KINDS: tuple[str, ...] = ("code", "log")
CREATORS: tuple[str, ...] = ("planner", "person")

ANCHOR_TYPES: tuple[str, ...] = ("point", "region", "part", "lines")
VIAS: tuple[str, ...] = ("voice", "typed", "tap")
OPEN, RESOLVED, WORK = "open", "resolved", "work"
NOTE_STATES: tuple[str, ...] = (OPEN, RESOLVED, WORK)
FOLLOW_UP, ISSUE, LANE_BRIEF = "follow_up", "issue", "lane_brief"
WORK_KINDS: tuple[str, ...] = (FOLLOW_UP, ISSUE, LANE_BRIEF)

VISUAL_UPSERT = "visual.upsert"
NOTE_UPSERT = "note.upsert"
FOCUS = "focus"

#: §8.3: a spoken note takes the page's pointer only while it is this fresh.
POINTER_MAX_AGE_SECONDS = 5.0
#: A body is text; a document holds 1 MiB, and the body goes to the bucket.
BODY_MAX_BYTES = 256 * 1024
#: A file slice shown at once; a longer range is cut to this many lines.
FILE_LINES_MAX = 400
#: A log tail: the newest failure events of the task, and their lines.
LOG_EVENTS_SCAN = 200
LOG_LINES_MAX = 400
PARTS_MAX = 200
NOTE_MAX = vcards.LINE_MAX * 4
NOTES_TO_WORK_MAX = 50
#: The brief's open list keeps this many items.
OPEN_LIST_MAX = 100
GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_SECONDS = 10.0

_VISUAL_ID_RE = re.compile(r"^vv_[0-9a-f]{32}$")
_NOTE_ID_RE = re.compile(r"^vn_[0-9a-f]{32}$")
_PART_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
#: `owner/name` as GitHub spells them.
_REPO_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_PATH_SEGMENT_RE = re.compile(r"^[^\x00-\x1f\\]{1,255}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class RepositoryNotGranted(Forbidden):
    """403: the person holds no grant on the repository in this workspace."""


class RepositoryNotCovered(Conflict):
    """409: the GitHub App installation does not cover the repository."""


class RepositoryReadFailed(UpstreamUnavailable):
    """503: GitHub did not answer the read. Constant text, never its body."""


def _refuse(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(f"{field} {message}", detail={"field": field})


def check_visual_id(value: Any) -> str:
    if not isinstance(value, str) or not _VISUAL_ID_RE.match(value):
        raise _refuse("visual_id", "is not a Voice it visual id")
    return value


def check_note_id(value: Any) -> str:
    if not isinstance(value, str) or not _NOTE_ID_RE.match(value):
        raise _refuse("note_id", "is not a Voice it note id")
    return value


def _line(value: Any, field: str, *, max: int = vcards.TITLE_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise _refuse(field, "is one line of text")
    if len(value) > max:
        raise _refuse(field, f"is at most {max} characters")
    return value.strip()


def _clean(text: str, field: str) -> str:
    """`text`, refused when a credential rule matches (as a card is), naming
    the field and never the text."""
    if redact(text).count:
        raise _refuse(field, "looks like it holds a credential; nothing was stored")
    return text


# --------------------------------------------------------------------------
# Parts and anchors
# --------------------------------------------------------------------------


def spec_parts(markdown: str) -> list[dict[str, str]]:
    """A spec's headings as parts: `{id, label}`, the id a slug of the label,
    numbered when two headings share one."""
    parts: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    fenced = False
    for raw in markdown.splitlines():
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING_RE.match(raw.strip())
        if match is None:
            continue
        label = match.group(2).strip()[:vcards.TITLE_MAX]
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:100] or "heading"
        seen[slug] = seen.get(slug, 0) + 1
        parts.append({"id": slug if seen[slug] == 1 else f"{slug}-{seen[slug]}", "label": label})
        if len(parts) >= PARTS_MAX:
            break
    return parts


def check_parts(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > PARTS_MAX:
        raise _refuse("parts", f"is a list of at most {PARTS_MAX} parts")
    out: list[dict[str, str]] = []
    ids: set[str] = set()
    for i, part in enumerate(value):
        if not isinstance(part, dict) or set(part) != {"id", "label"}:
            raise _refuse(f"parts[{i}]", "is {id, label}")
        pid = part["id"]
        if not isinstance(pid, str) or not _PART_ID_RE.match(pid) or pid in ids:
            raise _refuse(f"parts[{i}].id", "is a unique plain id")
        ids.add(pid)
        out.append({"id": pid, "label": _line(part["label"], f"parts[{i}].label")})
    return out


def _unit(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise _refuse(field, "is a number from 0 to 1 of the frame")
    return float(value)


def check_anchor(anchor: Any, visual: Mapping[str, Any]) -> dict[str, Any] | None:
    """`anchor` checked against `visual` (§8.2): a point or a region inside the
    frame, one of the visual's parts, or lines inside its lines; None pins the
    note to the whole visual."""
    if anchor is None:
        return None
    if not isinstance(anchor, dict) or anchor.get("type") not in ANCHOR_TYPES:
        raise _refuse("anchor", f"is null or an object whose type is one of {', '.join(ANCHOR_TYPES)}")
    kind = anchor["type"]
    keys = {"point": {"type", "x", "y"}, "region": {"type", "x", "y", "w", "h"},
            "part": {"type", "part_id"}, "lines": {"type", "from", "to"}}[kind]
    if set(anchor) != keys:
        raise _refuse("anchor", f"of type {kind} has exactly {', '.join(sorted(keys))}")
    if kind == "point":
        return {"type": kind, "x": _unit(anchor["x"], "anchor.x"), "y": _unit(anchor["y"], "anchor.y")}
    if kind == "region":
        x, y = _unit(anchor["x"], "anchor.x"), _unit(anchor["y"], "anchor.y")
        w, h = _unit(anchor["w"], "anchor.w"), _unit(anchor["h"], "anchor.h")
        if w <= 0 or h <= 0 or x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
            raise _refuse("anchor", "is a region inside the frame")
        return {"type": kind, "x": x, "y": y, "w": w, "h": h}
    if kind == "part":
        ids = {p.get("id") for p in visual.get("parts") or []}
        if anchor["part_id"] not in ids:
            raise _refuse("anchor.part_id", "is not one of this visual's parts")
        return {"type": kind, "part_id": anchor["part_id"]}
    lines = visual.get("lines") or {}
    first, last = lines.get("from"), lines.get("to")
    start, end = anchor["from"], anchor["to"]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)) \
            or not isinstance(first, int) or not isinstance(last, int) \
            or not first <= start <= end <= last:
        raise _refuse("anchor", "is lines inside this visual's lines")
    return {"type": kind, "from": start, "to": end}


def _count_lines(text: str, first: int = 1) -> dict[str, int] | None:
    count = len(text.splitlines()) or 1
    return {"from": first, "to": first + count - 1}


# --------------------------------------------------------------------------
# Sources: what swarm-api reads itself
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSource:
    repo: str
    ref: str
    path: str
    start: int
    end: int | None

    def view(self) -> dict[str, Any]:
        return {"repo": self.repo, "ref": self.ref, "path": self.path, "from": self.start,
                "to": self.end, "pull": None}


@dataclass(frozen=True)
class DiffSource:
    repo: str
    pull: int

    def view(self) -> dict[str, Any]:
        return {"repo": self.repo, "ref": None, "path": None, "from": None, "to": None,
                "pull": self.pull}


@dataclass(frozen=True)
class LogSource:
    task_id: str
    step: str | None

    def view(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "step": self.step}


def _repo(value: Any) -> str:
    if not isinstance(value, str) or not _REPO_RE.match(value) or ".." in value:
        raise _refuse("source.repo", "is a repository as owner/name")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or len(value) > 1024:
        raise _refuse("source.path", "is a path inside the repository")
    for segment in value.split("/"):
        if segment in ("", ".", "..") or not _PATH_SEGMENT_RE.match(segment):
            raise _refuse("source.path", "is a path inside the repository")
    return value


def _positive(value: Any, field: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _refuse(field, "is a whole number from 1")
    return value


def check_source(kind: str, source: Any) -> FileSource | DiffSource | LogSource | None:
    """The source of a `code` or `log` visual; None for a drawn kind."""
    if kind in DRAWN_KINDS:
        if source is not None:
            raise _refuse("source", f"is null for a {kind}: its body is drawn")
        return None
    if not isinstance(source, dict):
        raise _refuse("source", f"is required for a {kind}")
    if kind == "log":
        if set(source) - {"task_id", "step"} or "task_id" not in source:
            raise _refuse("source", "of a log is {task_id, step}")
        task_id = source["task_id"]
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            raise _refuse("source.task_id", "is a task id")
        step = source.get("step")
        if step is not None and (not isinstance(step, str) or not _TASK_ID_RE.match(step)):
            raise _refuse("source.step", "is null or an attempt id")
        return LogSource(task_id=task_id, step=step)
    if source.get("pull") is not None:
        if set(source) - {"repo", "pull", "ref", "path", "from", "to"} \
                or any(source.get(k) is not None for k in ("ref", "path", "from", "to")):
            raise _refuse("source", "of a diff is {repo, pull}")
        return DiffSource(repo=_repo(source.get("repo")),
                          pull=int(_positive(source["pull"], "source.pull") or 0))
    if set(source) - {"repo", "ref", "path", "from", "to", "pull"}:
        raise _refuse("source", "of a file is {repo, ref, path, from, to}")
    ref = source.get("ref")
    if not isinstance(ref, str) or not _REF_RE.match(ref) or ".." in ref:
        raise _refuse("source.ref", "is a branch, tag or commit")
    start = _positive(source.get("from", 1) if source.get("from") is not None else 1,
                      "source.from")
    end = _positive(source.get("to"), "source.to", nullable=True)
    if end is not None and start is not None and end < start:
        raise _refuse("source.to", "is at or after source.from")
    return FileSource(repo=_repo(source.get("repo")), ref=ref, path=_path(source.get("path")),
                      start=int(start or 1), end=end)


def repository_scope(db: Any, *, tenant_id: str, person: str, repo: str,
                     at: datetime) -> str:
    """The repository's id when `person` may read `repo` in `tenant_id`;
    `RepositoryNotGranted` (403) otherwise. See the module docstring."""
    owner, name = _repo(repo).split("/", 1)
    repo_id = repo_id_for(tenant_id, owner, name)
    snap = db.collection(REPOSITORIES).document(repo_id).get()
    registered = snap.to_dict() if snap.exists else None
    if not registered or registered.get("tenant_id") != tenant_id:
        raise RepositoryNotGranted(f"{repo} is not a repository of this workspace")
    email = person.strip().lower()
    snap = db.collection(GRANTS).document(grant_id_for(tenant_id, email, repo_id)).get()
    grant = snap.to_dict() if snap.exists else None
    if grant:
        ok = (grant.get("tenant_id") == tenant_id and grant.get("user") == email
              and grant.get("repo_id") == repo_id and grant.get("mode") in MODES)
    else:
        ok = person_grant_from_workspace(db, tenant_id, email, repo_id, at=at) in MODES
    if not ok:
        raise RepositoryNotGranted(f"you hold no grant on {repo} in this workspace")
    return repo_id


class RepoReader(Protocol):
    def read_file(self, tenant_id: str, owner: str, repo: str, ref: str, path: str) -> str: ...

    def read_diff(self, tenant_id: str, owner: str, repo: str, pull: int) -> str: ...


Send = Callable[[str, str, dict[str, str], bytes | None, float], Any]


class GitHubRepoReader:
    """Reads a file or a pull request's diff with the App installation token
    for that one repository (`forge_installation.read_token`, read
    permissions only), never a person's."""

    def __init__(self, installations: InstallationSource | None, *, send: Send | None = None) -> None:
        self._installations = installations
        if send is None:
            from ..forgeapp import urllib_send
            send = urllib_send
        self._send = send

    def _get(self, tenant_id: str, owner: str, repo: str, path: str, accept: str) -> str:
        token = read_token(self._installations, tenant_id, owner, repo)
        if not token:
            raise RepositoryNotCovered(
                f"the GitHub App installation does not cover {owner}/{repo}, so it cannot be shown")
        url = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": accept,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "swarm-api-voice"}
        try:
            answer = self._send("GET", url, headers, None, GITHUB_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - the transport's text may quote the request
            log.warning("voice visual read did not reach GitHub repo=%s/%s (%s)",
                        owner.lower(), repo.lower(), type(exc).__name__)
            raise RepositoryReadFailed("GitHub did not answer the read") from None
        status = int(getattr(answer, "status", 0) or 0)
        if status == 404:
            raise NotFound(f"{owner}/{repo} has no such file, ref or pull request")
        if status in (401, 403):
            raise RepositoryNotCovered(
                f"the GitHub App installation may not read {owner}/{repo}")
        if status != 200:
            raise RepositoryReadFailed(f"GitHub answered the read with {status}")
        body = bytes(getattr(answer, "body", b"") or b"")
        if len(body) > BODY_MAX_BYTES:
            body = body[:BODY_MAX_BYTES]
        return body.decode("utf-8", "replace")

    def read_file(self, tenant_id: str, owner: str, repo: str, ref: str, path: str) -> str:
        return self._get(tenant_id, owner, repo,
                         f"contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}",
                         "application/vnd.github.raw+json")

    def read_diff(self, tenant_id: str, owner: str, repo: str, pull: int) -> str:
        return self._get(tenant_id, owner, repo, f"pulls/{int(pull)}",
                         "application/vnd.github.diff")


def file_slice(text: str, start: int, end: int | None) -> tuple[str, dict[str, int]]:
    """Lines `start`..`end` of `text` (1-based, inclusive), at most
    `FILE_LINES_MAX` of them, and the lines they are."""
    lines = text.splitlines()
    if not lines:
        return "", {"from": 1, "to": 1}
    if start > len(lines):
        raise _refuse("source.from", f"is past the file's {len(lines)} lines")
    last = min(len(lines), end or len(lines), start + FILE_LINES_MAX - 1)
    return "\n".join(lines[start - 1:last]), {"from": start, "to": last}


_FAILURE_TYPES = (EventType.FAILED, EventType.RETRYING, EventType.DEAD_LETTERED,
                  EventType.QUOTA_EXHAUSTED)


def log_tail(store: Any, *, tenant_id: str, submitted_by: str | None, source: LogSource) -> str:
    """The task's failure events, oldest first, each its stderr tail
    (`detail.error`), redacted. `store` is swarm-api's `Store`; the task is
    read in `tenant_id` only, so another tenant's is the same 404."""
    task = store.get_task(tenant_id, source.task_id, submitted_by=submitted_by)
    page = store.list_events(tenant_id, source.task_id, limit=LOG_EVENTS_SCAN, descending=True,
                             task=task)
    blocks: list[str] = []
    for event in reversed(list(page.items)):
        if event.type not in _FAILURE_TYPES:
            continue
        if source.step is not None and event.attempt_id != source.step:
            continue
        error = (event.detail or {}).get("error")
        if not isinstance(error, str) or not error.strip():
            continue
        at = event.at.isoformat() if isinstance(event.at, datetime) else str(event.at)
        blocks.append(f"== {event.type.value} {event.attempt_id or ''} {at}".rstrip()
                      + "\n" + error.rstrip())
    if not blocks:
        raise NotFound(f"task {source.task_id!r} has no failed step with a log to show")
    lines = "\n".join(blocks).splitlines()
    return "\n".join(lines[-LOG_LINES_MAX:])


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


_PRIVATE = ("tenant_id", "person")


def visual_view(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A visual as the page reads it: no tenant, no person, no body."""
    return {
        "visual_id": doc.get("visual_id"), "kind": doc.get("kind"), "title": doc.get("title"),
        "version": doc.get("version"), "source": doc.get("source"),
        "parts": doc.get("parts") or [], "lines": doc.get("lines"),
        "body_ref": doc.get("body_ref"), "created_by": doc.get("created_by"),
        "session_id": doc.get("session_id"), "project_id": doc.get("project_id"),
        "created_at": _iso(doc.get("created_at")), "updated_at": _iso(doc.get("updated_at")),
    }


def note_view(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in doc.items() if k not in _PRIVATE}


@dataclass(frozen=True)
class Where:
    """The session or project a write belongs to, resolved by the route."""

    tenant_id: str
    person: str
    project_id: str
    session_id: str | None


class Visuals:
    """One person's visuals and notes in one tenant's project."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 blobs: vstore.VoiceBlobs | None) -> None:
        self._db = db
        self._now = now
        self._blobs = blobs
        self._store = vstore.VoiceStore(db, now=now, blobs=blobs)

    # ---- references ------------------------------------------------------

    def _visual_ref(self, project_id: str, visual_id: str) -> Any:
        return self._db.collection(vs.PROJECTS).document(project_id) \
            .collection(VISUALS).document(visual_id)

    def _note_ref(self, project_id: str, visual_id: str, note_id: str) -> Any:
        return self._visual_ref(project_id, visual_id).collection(NOTES).document(note_id)

    def _pointer_ref(self, project_id: str, session_id: str) -> Any:
        return self._db.collection(vs.PROJECTS).document(project_id) \
            .collection(POINTERS).document(session_id)

    @staticmethod
    def _mine(data: Mapping[str, Any] | None, where: Where) -> bool:
        return bool(data) and data.get("tenant_id") == where.tenant_id \
            and data.get("person") == where.person

    def _visual(self, where: Where, visual_id: str) -> dict[str, Any]:
        self._store._owned(where.tenant_id, where.person, where.project_id)
        check_visual_id(visual_id)
        snap = self._visual_ref(where.project_id, visual_id).get()
        data = snap.to_dict() if snap.exists else None
        if not self._mine(data, where):
            raise NotFound(f"visual {visual_id!r} not found")
        return data or {}

    def _event(self, where: Where, event: dict[str, Any]) -> None:
        if where.session_id is not None:
            self._store.append_event(tenant_id=where.tenant_id, person=where.person,
                                     project_id=where.project_id, session_id=where.session_id,
                                     event=event)

    # ---- visuals ---------------------------------------------------------

    def put(self, where: Where, *, kind: str, title: str, body: str, parts: list[dict[str, str]],
            source: dict[str, Any] | None, lines: dict[str, int] | None, created_by: str,
            visual_id: str | None = None) -> dict[str, Any]:
        """Store the visual's next version: its body under the tenant's prefix,
        then the document, then `visual.upsert` on the session's stream."""
        self._store._owned(where.tenant_id, where.person, where.project_id)
        if kind not in KINDS:
            raise _refuse("kind", f"is one of {', '.join(KINDS)}")
        if created_by not in CREATORS:
            raise _refuse("created_by", f"is one of {', '.join(CREATORS)}")
        title = _clean(_line(title, "title"), "title")
        data = body.encode("utf-8")
        if len(data) > BODY_MAX_BYTES:
            raise _refuse("body", f"is at most {BODY_MAX_BYTES // 1024} KiB")
        if visual_id is None:
            visual_id, previous = "vv_" + secrets.token_hex(16), None
        else:
            previous = self._visual(where, visual_id)
            if previous.get("kind") != kind:
                raise _refuse("kind", "stays the kind the visual was drawn as")
        version = int((previous or {}).get("version") or 0) + 1
        if self._blobs is None:
            raise vstore.VoiceBlobFailed("Voice it's storage is not configured (no artifact bucket)")
        key = vstore.blob_key(where.tenant_id, where.project_id, VISUALS, visual_id,
                              f"{version:06d}.txt")
        self._blobs.create(where.tenant_id, key, data, "text/plain; charset=utf-8")
        at = self._now()
        doc = {
            "visual_id": visual_id, "kind": kind, "title": title, "version": version,
            "source": source, "parts": parts, "lines": lines, "body_ref": key,
            "created_by": created_by, "session_id": where.session_id,
            "project_id": where.project_id, "tenant_id": where.tenant_id,
            "person": where.person, "created_at": (previous or {}).get("created_at") or at,
            "updated_at": at,
        }
        ref = self._visual_ref(where.project_id, visual_id)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            found = (snap.to_dict() or {}) if snap.exists else None
            if int((found or {}).get("version") or 0) != version - 1:
                raise Conflict(f"visual {visual_id!r} changed while it was written; draw it again")
            txn.set(ref, doc)

        _apply(self._db.transaction())
        view = visual_view(doc)
        self._event(where, {"type": VISUAL_UPSERT, "visual": view})
        log.info("voice visual stored tenant=%s project=%s visual=%s kind=%s version=%d",
                 where.tenant_id, where.project_id, visual_id, kind, version)
        return view

    def list(self, where: Where) -> list[dict[str, Any]]:
        self._store._owned(where.tenant_id, where.person, where.project_id)
        rows = self._db.collection(vs.PROJECTS).document(where.project_id) \
            .collection(VISUALS).stream()
        found = [snap.to_dict() or {} for snap in rows]
        views = [visual_view(d) for d in found if self._mine(d, where)]
        return sorted(views, key=lambda v: str(v.get("updated_at") or ""), reverse=True)

    def get(self, where: Where, visual_id: str) -> dict[str, Any]:
        """The visual, its body as text, and its notes."""
        doc = self._visual(where, visual_id)
        key = str(doc.get("body_ref") or "")
        body = self._store.read_blob(tenant_id=where.tenant_id, person=where.person,
                                     project_id=where.project_id, key=key)
        if body is None:
            raise vstore.VoiceBlobFailed("the visual's body could not be read")
        return {"visual": visual_view(doc), "body": body.decode("utf-8", "replace"),
                "notes": self.notes(where, visual_id, doc=doc)}

    def focus(self, where: Where, *, visual_id: str, anchor: Any,
              caption: str | None) -> dict[str, Any]:
        """`speak`'s `point_at` (§8.5): the `focus` event the page rings."""
        doc = self._visual(where, visual_id)
        event = {"type": FOCUS, "visual_id": visual_id, "anchor": check_anchor(anchor, doc),
                 "caption": None if caption is None else _clean(
                     _line(caption, "caption", max=vcards.LINE_MAX), "caption")}
        self._event(where, event)
        return {k: v for k, v in event.items() if k != "type"}

    # ---- the pointer -----------------------------------------------------

    def point(self, where: Where, *, visual_id: str, anchor: Any) -> dict[str, Any]:
        """The page's pointer resting on a visual (§8.3), for a spoken note."""
        if where.session_id is None:
            raise _refuse("session_id", "is required for a pointer")
        doc = self._visual(where, visual_id)
        row = {"visual_id": visual_id, "anchor": check_anchor(anchor, doc), "at": self._now(),
               "tenant_id": where.tenant_id, "person": where.person,
               "session_id": where.session_id}
        self._pointer_ref(where.project_id, where.session_id).set(row)
        return {"visual_id": visual_id, "anchor": row["anchor"], "at": _iso(row["at"])}

    def _pointer(self, where: Where, visual_id: str) -> dict[str, Any] | None:
        if where.session_id is None:
            return None
        snap = self._pointer_ref(where.project_id, where.session_id).get()
        row = snap.to_dict() if snap.exists else None
        if not self._mine(row, where) or (row or {}).get("visual_id") != visual_id:
            return None
        at = (row or {}).get("at")
        if not isinstance(at, datetime) \
                or self._now() - at > timedelta(seconds=POINTER_MAX_AGE_SECONDS):
            return None
        anchor = (row or {}).get("anchor")
        return anchor if isinstance(anchor, dict) else None

    # ---- notes -----------------------------------------------------------

    def notes(self, where: Where, visual_id: str, *,
              doc: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if doc is None:
            self._visual(where, visual_id)
        rows = self._visual_ref(where.project_id, visual_id).collection(NOTES).stream()
        found = [snap.to_dict() or {} for snap in rows]
        views = [note_view(d) for d in found if self._mine(d, where)]
        return sorted(views, key=lambda n: str(n.get("created_at") or ""))

    def add_note(self, where: Where, *, visual_id: str, text: str, via: str, anchor: Any = None,
                 part: str | None = None) -> dict[str, Any]:
        """Pin one note (§8.3). A spoken note with no anchor takes the page's
        fresh pointer, else the part the planner named, else the whole visual."""
        doc = self._visual(where, visual_id)
        if via not in VIAS:
            raise _refuse("via", f"is one of {', '.join(VIAS)}")
        if not isinstance(text, str) or not text.strip() or len(text) > NOTE_MAX:
            raise _refuse("text", f"is 1 to {NOTE_MAX} characters")
        _clean(text, "text")
        if anchor is None and part is not None:
            anchor = {"type": "part", "part_id": self._part(doc, part)}
        if anchor is None and via == "voice":
            anchor = self._pointer(where, visual_id)
        checked = check_anchor(anchor, doc)
        at = self._now()
        note_id = "vn_" + secrets.token_hex(16)
        row = {"note_id": note_id, "visual_id": visual_id, "visual_version": doc.get("version"),
               "anchor": checked, "text": text.strip(), "by": where.person, "via": via,
               "state": OPEN, "work": None, "created_at": at, "updated_at": at,
               "session_id": where.session_id, "tenant_id": where.tenant_id,
               "person": where.person}
        self._note_ref(where.project_id, visual_id, note_id).set(row)
        view = note_view(row)
        self._event(where, {"type": NOTE_UPSERT, "note": view})
        return view

    @staticmethod
    def _part(doc: Mapping[str, Any], part: str) -> str:
        """A part named by its id or its label (any case)."""
        wanted = str(part).strip().lower()
        for p in doc.get("parts") or []:
            if str(p.get("id", "")).lower() == wanted or str(p.get("label", "")).lower() == wanted:
                return str(p["id"])
        raise _refuse("part", "is not one of this visual's parts")

    def _note(self, where: Where, visual_id: str, note_id: str) -> tuple[Any, dict[str, Any]]:
        check_note_id(note_id)
        ref = self._note_ref(where.project_id, visual_id, note_id)
        snap = ref.get()
        data = snap.to_dict() if snap.exists else None
        if not self._mine(data, where) or (data or {}).get("visual_id") != visual_id:
            raise NotFound(f"note {note_id!r} not found")
        return ref, data or {}

    def edit_note(self, where: Where, *, visual_id: str, note_id: str, text: str | None = None,
                  state: str | None = None) -> dict[str, Any]:
        """Change a note's text, or open or resolve it. `work` is set only by
        `notes_to_work`, so a note that became work stays linked."""
        self._visual(where, visual_id)
        ref, data = self._note(where, visual_id, note_id)
        changes: dict[str, Any] = {}
        if text is not None:
            if not isinstance(text, str) or not text.strip() or len(text) > NOTE_MAX:
                raise _refuse("text", f"is 1 to {NOTE_MAX} characters")
            changes["text"] = _clean(text, "text").strip()
        if state is not None:
            if state not in (OPEN, RESOLVED):
                raise _refuse("state", "is open or resolved; work is made by notes_to_work")
            if data.get("state") == WORK:
                raise Conflict(f"note {note_id!r} became work; it is not reopened")
            changes["state"] = state
        if not changes:
            raise _refuse("note", "changes its text or its state")
        changes["updated_at"] = self._now()
        ref.update(changes)
        view = note_view({**data, **changes})
        self._event(where, {"type": NOTE_UPSERT, "note": view})
        return view

    def notes_to_work(self, where: Where, *, visual_id: str, note_ids: Iterable[str], kind: str,
                      title: str | None = None) -> dict[str, Any]:
        """Turn open notes into one piece of work (§8.4); each becomes
        `state: "work"` with `{kind, ref}`."""
        doc = self._visual(where, visual_id)
        if kind not in WORK_KINDS:
            raise _refuse("kind", f"is one of {', '.join(WORK_KINDS)}")
        ids = list(dict.fromkeys(note_ids))
        if not ids or len(ids) > NOTES_TO_WORK_MAX:
            raise _refuse("note_ids", f"names 1 to {NOTES_TO_WORK_MAX} notes")
        found = [self._note(where, visual_id, n) for n in ids]
        for (_ref, data) in found:
            if data.get("state") != OPEN:
                raise Conflict(f"note {data.get('note_id')!r} is not open")
        name = _clean(_line(title, "title"), "title") if title is not None \
            else str(doc.get("title") or "Notes")[:vcards.TITLE_MAX]
        texts = [str(data.get("text") or "") for _ref, data in found]
        if kind == FOLLOW_UP:
            ref = self._follow_up(where, doc, name, texts)
        else:
            ref = self._card(where, doc, kind, name, texts)
        work = {"kind": kind, "ref": ref}
        at = self._now()
        batch = self._db.batch()
        views = []
        for note_ref, data in found:
            batch.update(note_ref, {"state": WORK, "work": work, "updated_at": at})
            views.append(note_view({**data, "state": WORK, "work": work, "updated_at": at}))
        batch.commit()
        for view in views:
            self._event(where, {"type": NOTE_UPSERT, "note": view})
        return {"work": work, "notes": views}

    def _follow_up(self, where: Where, doc: Mapping[str, Any], name: str,
                   texts: list[str]) -> str:
        project = self._store._owned(where.tenant_id, where.person, where.project_id)
        brief = dict(project.get("brief") or {})
        items = list(brief.get("open") or [])
        item_id = "vf_" + secrets.token_hex(8)
        items.append({"id": item_id, "title": name, "notes": texts,
                      "visual_id": doc.get("visual_id"), "at": _iso(self._now())})
        brief["open"] = items[-OPEN_LIST_MAX:]
        self._store.update_project(tenant_id=where.tenant_id, person=where.person,
                                   project_id=where.project_id, brief=brief)
        return f"brief.open/{item_id}"

    def _card(self, where: Where, doc: Mapping[str, Any], kind: str, name: str,
              texts: list[str]) -> str:
        source = doc.get("source") or {}
        repository = source.get("repo") if isinstance(source, dict) else None
        summary = " ".join("; ".join(texts).split())[:vcards.SUMMARY_MAX]
        references = {name: None for name in vcards.REFERENCES}
        if kind == ISSUE:
            card = {"card_id": None, "kind": "work", "state": vcards.PROPOSED, "title": name,
                    "created_by": "planner", "references": references, "summary": summary,
                    "body": {"target_kind": "issue", "target_id": None,
                             "repository": repository, "lanes": [], "live_state": None}}
        else:
            territory = [str(source["path"])] if isinstance(source, dict) and source.get("path") \
                else []
            card = {"card_id": None, "kind": "plan", "state": vcards.PROPOSED, "title": name,
                    "created_by": "planner", "references": references, "summary": summary,
                    "body": {"goal": "\n".join(f"- {t}" for t in texts)[:2_000],
                             "size": "one_fix",
                             "waves": [{"name": "Notes", "lanes": [
                                 {"name": name, "territory": territory,
                                  "reservation_usd": None, "state": None}]}],
                             "reserved_usd": 0, "session_cap_usd": vledger.SESSION_CAP_USD,
                             "day_cap_usd": vledger.DAY_CAP_USD}}
        written = vcards.write_card(self._store, tenant_id=where.tenant_id, person=where.person,
                                    project_id=where.project_id, session_id=where.session_id,
                                    card=card)
        return f"card/{written['card_id']}"
