"""The planner's visual tools (docs/design/voice-it-own-speech.md §8.1, §8.3, §8.4,
§8.5; swarm_api/routes/voice_visuals.py).

  show_file      a file slice of a workspace repository, as a `code` visual
  show_diff      a pull request's diff, as a `code` visual
  show_log       a failed step's log tail, as a `log` visual
  add_note       pin a spoken note on a visual: at the place the person's pointer
                 rests, at a part the planner names, at lines, or on the whole visual
  notes_to_work  turn open notes into a follow-up, an issue or a lane brief

THE PLANNER NAMES A SOURCE, NEVER A BODY. `show_*` send swarm-api the source
alone (`{repo, ref, path, from, to}`, `{repo, pull}`, `{task_id, step}`);
swarm-api reads it as the session's person, through their repository grant and
with the GitHub App installation token, or from the task's own events in the
session's workspace, stores the body under the tenant's prefix and answers the
visual's id, its version, its parts and its lines, which is all the planner
gets back. So nothing the planner writes can put another tenant's file, or a
repository the person holds no grant on, on the page.

EVERY CALL GOES THROUGH THE SESSION GRANT, on the two writes the grant reaches
(swarm_api/voice/grant.py `WRITE_ROUTES`): the visual write
(`POST /v1/voice/sessions/{session_id}/visuals`, `{visual}` or `{focus}`) and
the note write (`POST /v1/voice/projects/{project_id}/visuals/{visual_id}/notes`,
`{note}` or `{to_work}`). `focus` is how `speak`'s `point_at` reaches the page
(§8.5). An error names the route and the HTTP status, never a body.
"""

from __future__ import annotations

import re
from typing import Any

from .tools_read import ApiRefused, BadInput, SwarmApi

VISUAL_WRITE_ROUTE = "/v1/voice/sessions/{session_id}/visuals"
NOTE_WRITE_ROUTE = "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"

SHOW_FILE = "show_file"
SHOW_DIFF = "show_diff"
SHOW_LOG = "show_log"
ADD_NOTE = "add_note"
NOTES_TO_WORK = "notes_to_work"
NAMES: tuple[str, ...] = (SHOW_FILE, SHOW_DIFF, SHOW_LOG, ADD_NOTE, NOTES_TO_WORK)

ANCHOR_TYPES: tuple[str, ...] = ("point", "region", "part", "lines")
WORK_KINDS: tuple[str, ...] = ("follow_up", "issue", "lane_brief")

_VISUAL_ID_RE = re.compile(r"^vv_[0-9a-f]{32}$")
_NOTE_ID_RE = re.compile(r"^vn_[0-9a-f]{32}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TITLE_MAX = 120
NOTE_MAX = 2_000


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _nullable(kind: str, desc: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": desc}


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "additionalProperties": False,
                             "properties": props, "required": list(props)}}


#: An anchor in strict form: every key present, the ones its type does not use null.
ANCHOR_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": ("Where on the visual: a point (x, y) or region (x, y, w, h) as 0-1 of the "
                    "frame, a part (part_id from the visual's parts), or lines (from, to). "
                    "Null: the place the person is pointing at, else the whole visual."),
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": list(ANCHOR_TYPES)},
        "x": {"type": ["number", "null"]}, "y": {"type": ["number", "null"]},
        "w": {"type": ["number", "null"]}, "h": {"type": ["number", "null"]},
        "part_id": {"type": ["string", "null"]},
        "from": {"type": ["integer", "null"]}, "to": {"type": ["integer", "null"]},
    },
    "required": ["type", "x", "y", "w", "h", "part_id", "from", "to"],
}

_TITLE = _str("A short title for the visual (at most 120 characters).")


def tool_schemas() -> list[dict[str, Any]]:
    return [
        _tool(SHOW_FILE, "Show a file of a workspace repository on screen, as a code visual. "
              "swarm-api reads it; you get back its visual_id and lines.",
              {"repo": _str("owner/name"), "ref": _str("Branch, tag or commit."),
               "path": _str("Path inside the repository."),
               "from": _nullable("integer", "First line, from 1; null for the top."),
               "to": _nullable("integer", "Last line; null for up to 400 lines."),
               "title": _TITLE}),
        _tool(SHOW_DIFF, "Show a pull request's diff on screen, as a code visual.",
              {"repo": _str("owner/name"), "pull": {"type": "integer"}, "title": _TITLE}),
        _tool(SHOW_LOG, "Show an agent's failed step's log tail on screen, as a log visual.",
              {"task_id": _str("The agent (task) id."),
               "step": _nullable("string", "The attempt id of one step; null for every failure."),
               "title": _TITLE}),
        _tool(ADD_NOTE, "Pin the person's spoken note on a visual.",
              {"visual_id": _str("The visual's id."), "text": _str("The note, as said."),
               "anchor": ANCHOR_SCHEMA,
               "part": _nullable("string", "A part named by its label or id, e.g. 'the header'; "
                                 "used when anchor is null.")}),
        _tool(NOTES_TO_WORK, "Turn open notes on a visual into work: a follow-up on the "
              "project's open list, an issue, or a lane brief over the visual's files.",
              {"visual_id": _str("The visual's id."),
               "note_ids": {"type": "array", "items": {"type": "string"}},
               "kind": {"type": "string", "enum": list(WORK_KINDS)},
               "title": _nullable("string", "A title for the work; null for the visual's.")}),
    ]


def is_visual_tool(name: str) -> bool:
    return name in NAMES


def _text(args: dict[str, Any], key: str, *, max: int = TITLE_MAX) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max:
        raise BadInput(f"{key} is 1 to {max} characters")
    return value.strip()


def _match(args: dict[str, Any], key: str, pattern: re.Pattern[str], what: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not pattern.match(value):
        raise BadInput(f"{key} is {what}")
    return value


def _int(args: dict[str, Any], key: str, *, nullable: bool) -> int | None:
    value = args.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BadInput(f"{key} is a whole number from 1")
    return value


def anchor_of(value: Any) -> dict[str, Any] | None:
    """The strict tool form of an anchor as swarm-api takes it: only its type's keys."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") not in ANCHOR_TYPES:
        raise BadInput("anchor is null or has a type of point, region, part or lines")
    keys = {"point": ("x", "y"), "region": ("x", "y", "w", "h"), "part": ("part_id",),
            "lines": ("from", "to")}[value["type"]]
    missing = [k for k in keys if value.get(k) is None]
    if missing:
        raise BadInput(f"a {value['type']} anchor needs {', '.join(missing)}")
    return {"type": value["type"], **{k: value[k] for k in keys}}


async def _visual(api: SwarmApi, visual: dict[str, Any]) -> dict[str, Any]:
    path = VISUAL_WRITE_ROUTE.format(session_id=api.session_id)
    body = await api._call("POST", VISUAL_WRITE_ROUTE, path, json_body={"visual": visual})
    view = body.get("visual") if isinstance(body, dict) else None
    if not isinstance(view, dict):
        raise ApiRefused(VISUAL_WRITE_ROUTE, 200, "no visual in the answer")
    return {"visual_id": view.get("visual_id"), "kind": view.get("kind"),
            "version": view.get("version"), "parts": view.get("parts") or [],
            "lines": view.get("lines")}


async def _note(api: SwarmApi, visual_id: str, payload: dict[str, Any]) -> Any:
    project_id = api._grant.project_id  # the grant's own project: the only one it writes to
    path = NOTE_WRITE_ROUTE.format(project_id=project_id, visual_id=visual_id)
    return await api._call("POST", NOTE_WRITE_ROUTE, path, json_body=payload)


async def focus(api: SwarmApi, point_at: Any, *, caption: str | None) -> dict[str, Any]:
    """`speak`'s `point_at` (§8.5): bring the visual forward and ring the anchor."""
    if not isinstance(point_at, dict):
        raise BadInput("point_at is {visual_id, anchor}")
    visual_id = _match(point_at, "visual_id", _VISUAL_ID_RE, "a visual id")
    body = {"visual_id": visual_id, "anchor": anchor_of(point_at.get("anchor")),
            "caption": caption[:500] if caption else None}
    path = VISUAL_WRITE_ROUTE.format(session_id=api.session_id)
    answer = await api._call("POST", VISUAL_WRITE_ROUTE, path, json_body={"focus": body})
    return answer.get("focus") if isinstance(answer, dict) else {}


async def run_visual_tool(api: SwarmApi, name: str, args: Any) -> Any:
    """Run one visual tool through swarm-api; what the planner gets back."""
    args = args if isinstance(args, dict) else {}
    if name == SHOW_FILE:
        return await _visual(api, {
            "kind": "code", "title": _text(args, "title"), "body": None, "parts": None,
            "source": {"repo": _match(args, "repo", _REPO_RE, "owner/name"),
                       "ref": _text(args, "ref", max=200), "path": _text(args, "path", max=1024),
                       "from": _int(args, "from", nullable=True),
                       "to": _int(args, "to", nullable=True)}})
    if name == SHOW_DIFF:
        return await _visual(api, {
            "kind": "code", "title": _text(args, "title"), "body": None, "parts": None,
            "source": {"repo": _match(args, "repo", _REPO_RE, "owner/name"),
                       "pull": _int(args, "pull", nullable=False)}})
    if name == SHOW_LOG:
        step = args.get("step")
        if step is not None and (not isinstance(step, str) or not _ID_RE.match(step)):
            raise BadInput("step is null or an attempt id")
        return await _visual(api, {
            "kind": "log", "title": _text(args, "title"), "body": None, "parts": None,
            "source": {"task_id": _match(args, "task_id", _ID_RE, "an agent id"), "step": step}})
    if name == ADD_NOTE:
        visual_id = _match(args, "visual_id", _VISUAL_ID_RE, "a visual id")
        part = args.get("part")
        if part is not None and (not isinstance(part, str) or len(part) > TITLE_MAX):
            raise BadInput("part is null or a part's label or id")
        answer = await _note(api, visual_id, {"note": {
            "text": _text(args, "text", max=NOTE_MAX), "via": "voice",
            "anchor": anchor_of(args.get("anchor")), "part": part}})
        note = answer.get("note") if isinstance(answer, dict) else None
        note = note if isinstance(note, dict) else {}
        return {"note_id": note.get("note_id"), "anchor": note.get("anchor")}
    if name == NOTES_TO_WORK:
        visual_id = _match(args, "visual_id", _VISUAL_ID_RE, "a visual id")
        ids = args.get("note_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 50 \
                or not all(isinstance(i, str) and _NOTE_ID_RE.match(i) for i in ids):
            raise BadInput("note_ids is 1 to 50 note ids")
        kind = args.get("kind")
        if kind not in WORK_KINDS:
            raise BadInput(f"kind is one of {', '.join(WORK_KINDS)}")
        title = args.get("title")
        answer = await _note(api, visual_id, {"to_work": {
            "note_ids": ids, "kind": kind,
            "title": None if title is None else _text(args, "title")}})
        work = answer.get("work") if isinstance(answer, dict) else None
        return {"work": work, "notes": len((answer or {}).get("notes") or [])}
    raise BadInput(f"{name[:64]!r} is not a visual tool")
