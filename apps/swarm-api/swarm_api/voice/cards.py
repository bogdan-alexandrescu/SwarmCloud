"""The Voice it card catalogue: one schema for every card (docs/design/voice-it.md
§4, §3.5, §10 phase 2).

ONE SCHEMA, THREE READERS.

  * swarm-api validates every card write against it (`validate`, and
    `write_card`, which stores the card and its stream event): an unknown kind,
    a field the kind does not carry, or a state the kind does not use is a 422
    before anything is written.
  * The planner reads it as strict tool schemas, one `draw_<kind>` tool a kind
    (`tool_schemas`), from `GET /v1/voice/cards/schema` (routes/voice.py).
  * The console names the same kinds, fields, states and actions in
    apps/swarm-ui/src/voice/cards.ts, and
    tests/unit/control_plane/test_voice_cards.py holds the two equal, the way
    test_issue_forms.py holds the forms to `SECTIONS`. The console renders only
    kinds it knows; an unknown kind is not drawn.

THE SHAPE follows Google's A2UI (§4): declarative data drawn only from a
catalogue of trusted components, never executable code. A mock-up's HTML is
data the console puts in a sandboxed frame; nothing here serves it as HTML.

EVERY CARD CARRIES (`ENVELOPE`): id, kind, project, session, version, state,
title, who created it, references, a one-line summary the voice may read out,
and the time. The project, the session, the version and the time are set by
swarm-api from the session the write belongs to, never by the writer.

NO CREDENTIAL VALUE IN ANY CARD (§3.9, §7 invariant 9). A credential card
names a secret, its provider, why it is needed and whether it is set: it has
no field that can hold a value, and every field it has is either an enum or a
name-shaped string. Every string of every card is also put through the shared
redaction rules on write (`swarm_redaction.redact`), and a card where one
matches is refused, naming the field and never the text.

STRICT TOOL SCHEMAS. Every object closes (`additionalProperties: false`) and
lists every property as required; an optional field is nullable instead of
absent, which strict tool use on both Anthropic and OpenAI accepts. Lengths,
counts and patterns are not in the tool schema (strict mode does not take all
of them) and are enforced here on write, which is the check that matters.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from swarm_redaction import redact

from ..errors import ValidationFailed
from .workfeed import ID_RE

#: Bumped when a kind, field, state or action changes, so the planner can
#: tell a cached catalogue from the served one.
CATALOGUE_VERSION = 3

# ---- the common states (§4) -------------------------------------------------

PROPOSED = "proposed"
ACTING = "acting"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
DISMISSED = "dismissed"
#: A navigation offer a person said yes to (§3.12.3); only swarm-api sets it.
ACCEPTED = "accepted"
STATES = (PROPOSED, ACTING, WAITING, DONE, FAILED, CANCELLED, DISMISSED, ACCEPTED)

#: Who created a card: the planner, the platform (swarm-voice following a tag,
#: swarm-api's ledger), or the person by a click.
CREATORS = ("planner", "platform", "person")

#: What a card may point at (§4's references).
REFERENCES = ("agent", "workflow", "run", "schedule", "pull_request", "issue")

TITLE_MAX = 120
SUMMARY_MAX = 240
LINE_MAX = 500
TEXT_MAX = 8_000
SOURCE_MAX = 64 * 1024
#: A mock-up frame's HTML; the store refuses a whole body over 256 KiB and the
#: caller then puts the frame in the bucket and names its key.
HTML_MAX = 200 * 1024
LIST_MAX = 50

_CARD_ID_RE = re.compile(r"^vc_[0-9a-f]{32}$")
#: A secret's name as Config › Secrets takes it: lowercase, digits, dashes.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_BLOB_RE = re.compile(r"^tenants/[^\s]+$")
#: A read-back's id (voice/readback.py mints it).
_READBACK_ID_RE = re.compile(r"^vr_[0-9a-f]{32}$")

# ---- the navigation offer (docs/design/voice-it.md §3.12.3) -----------------

#: The pages a Navigate card may offer: the `FIXED` keys of
#: apps/swarm-ui/src/paths.ts that a person reads, in its order, never an
#: admin page, a submit form or a callback. The console maps the key through
#: `FIXED` itself, so no string of the card becomes an address.
#: tests/unit/control_plane/test_voice_navigate.py holds this to paths.ts.
NAVIGATE_DESTINATIONS: tuple[str, ...] = (
    "overview/now", "work/running", "work/workflows", "work/timeline",
    "work/timeline/outcomes", "work/runs", "automate/schedules", "automate/approvals",
    "capacity/pools", "capacity/profiles", "capacity/catalogue", "capacity/holders",
    "capacity/accounts", "capacity/llm-providers", "capacity/llm-providers/plans",
    "capacity/llm-providers/api-keys", "capacity/account-health", "capacity/quota",
    "capacity/jev-usage", "capacity/api-reads",
    "config/github", "config/repositories", "config/secrets", "config/mcp-servers",
    "config/plugins", "config/people", "config/workspace",
    "settings/profile",
    "voice/talk", "voice/projects", "voice/preferences", "voice/settings", "voice/telemetry",
    "help",
)
#: What an offer's `object` may name; the console builds that object's own
#: page from its kind and id (`/agents/<id>`, `/runs/<id>`, ...).
NAVIGATE_OBJECTS: tuple[str, ...] = ("agent", "workflow", "run", "schedule", "approval")
#: Who accepted an offer: a person, by a click or a spoken yes. Never a writer.
ACCEPTED_BY: tuple[str, ...] = ("person",)


# ---- the field language -----------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One field: its type and what a value may be.

    `type` is one of string (one line), text (lines), integer, number,
    boolean, enum (`values`), list (of `item`) and object (`fields`)."""

    name: str
    type: str
    nullable: bool = False
    max: int | None = None
    min: int | None = None
    values: tuple[str, ...] = ()
    item: "Field | None" = None
    fields: tuple["Field", ...] = ()
    pattern: re.Pattern[str] | None = None


def _s(name: str, *, nullable: bool = False, max: int = LINE_MAX,
       pattern: re.Pattern[str] | None = None) -> Field:
    return Field(name, "string", nullable=nullable, max=max, pattern=pattern)


def _t(name: str, *, nullable: bool = False, max: int = TEXT_MAX) -> Field:
    return Field(name, "text", nullable=nullable, max=max)


def _e(name: str, values: tuple[str, ...], *, nullable: bool = False) -> Field:
    return Field(name, "enum", nullable=nullable, values=values)


def _n(name: str, *, nullable: bool = False) -> Field:
    return Field(name, "number", nullable=nullable, min=0)


def _b(name: str) -> Field:
    return Field(name, "boolean")


def _l(name: str, item: Field, *, min: int = 0, max: int = LIST_MAX) -> Field:
    return Field(name, "list", item=item, min=min, max=max)


def _o(name: str, *fields: Field, nullable: bool = False) -> Field:
    return Field(name, "object", nullable=nullable, fields=fields)


# ---- the envelope (§4: every card carries) ----------------------------------


@dataclass(frozen=True)
class EnvelopeField:
    name: str
    #: "server" when swarm-api sets it from the session and the store; "writer"
    #: when the planner, swarm-voice or a click supplies it.
    set_by: str


#: In the order the console's `CARD_FIELDS` lists them.
ENVELOPE: tuple[EnvelopeField, ...] = (
    EnvelopeField("card_id", "writer"),
    EnvelopeField("kind", "writer"),
    EnvelopeField("project_id", "server"),
    EnvelopeField("session_id", "server"),
    EnvelopeField("version", "server"),
    EnvelopeField("state", "writer"),
    EnvelopeField("title", "writer"),
    EnvelopeField("created_by", "writer"),
    EnvelopeField("references", "writer"),
    EnvelopeField("summary", "writer"),
    EnvelopeField("at", "server"),
    EnvelopeField("body", "writer"),
)

_REFERENCES = _o("references", *(_s(name, nullable=True, max=LINE_MAX) for name in REFERENCES))


# ---- the kinds (§4's table) -------------------------------------------------


@dataclass(frozen=True)
class Kind:
    kind: str
    label: str
    #: What the console shows and the planner fills: the card's body.
    fields: tuple[Field, ...]
    states: tuple[str, ...]
    actions: tuple[str, ...]
    created_by: tuple[str, ...]
    description: str = field(default="")


_LANE = _o("lane",
           _s("name", max=TITLE_MAX),
           _l("territory", _s("path")),
           _n("reservation_usd", nullable=True),
           _s("state", nullable=True, max=TITLE_MAX))

KINDS: tuple[Kind, ...] = (
    Kind("work", "Work",
         (_e("target_kind", ("issue", "run", "agent", "workflow", "schedule")),
          _s("target_id", nullable=True),
          _s("repository", nullable=True),
          _l("lanes", _LANE),
          _s("live_state", nullable=True, max=TITLE_MAX)),
         (PROPOSED, ACTING, WAITING, DONE, FAILED, CANCELLED),
         ("open_in_work", "cancel", "pause", "resume", "hold"),
         ("planner", "platform"),
         "What was filed or started: an issue, run, agent, workflow or schedule, "
         "its repository, lanes, each step's reservation and live state."),
    Kind("review", "Review",
         (_t("change_summary"),
          _e("checks", ("pending", "passing", "failing", "none")),
          _e("verdict", ("merge", "fix", "hold"), nullable=True),
          _e("merge_state", ("open", "merged", "closed")),
          _s("release_state", nullable=True, max=TITLE_MAX)),
         (WAITING, DONE, FAILED),
         ("open_diff", "ask_for_change", "merge"),
         ("platform",),
         "Finished work: the pull request, a change summary, checks, the review "
         "verdict, and its merge and release state."),
    Kind("proposal", "Proposal",
         (_t("question", max=LINE_MAX),
          _l("options", _o("option",
                           _s("label", max=TITLE_MAX),
                           _t("gets_right", max=2_000),
                           _t("costs", max=2_000),
                           _b("recommended")), min=2, max=4),
          _t("recommendation", nullable=True, max=2_000)),
         (PROPOSED, DONE, DISMISSED),
         ("pick", "mix", "ask_again"),
         ("planner",),
         "Two to four options, each with what it gets right and what it costs, "
         "and a recommendation."),
    Kind("diagram", "Diagram",
         (_e("diagram_type", ("architecture", "flow", "user_flow", "sequence", "er", "state")),
          _t("source", nullable=True, max=SOURCE_MAX),
          _o("graph",
             _l("nodes", _o("node", _s("id", max=TITLE_MAX), _s("label", max=TITLE_MAX),
                            _s("group", nullable=True, max=TITLE_MAX)), max=200),
             _l("edges", _o("edge", _s("from", max=TITLE_MAX), _s("to", max=TITLE_MAX),
                            _s("label", nullable=True, max=TITLE_MAX)), max=400),
             nullable=True),
          _s("blob_key", nullable=True, max=1024, pattern=_BLOB_RE),
          _b("repaired")),
         (PROPOSED, DONE, FAILED),
         ("edit_by_voice", "open_full_size", "file_issue_from_node"),
         ("planner",),
         "An architecture map (node and edge JSON) or a Mermaid flow, user flow, "
         "sequence, ER or state diagram."),
    Kind("mockup", "Mock-up",
         (_e("drawn_by", ("planner", "browser_lane")),
          _l("frames", _o("frame",
                          _s("name", max=TITLE_MAX),
                          _t("html", nullable=True, max=HTML_MAX),
                          _s("blob_key", nullable=True, max=1024, pattern=_BLOB_RE)),
             max=12)),
         (PROPOSED, WAITING, DONE),
         ("pick_frame", "comment", "open_full_size"),
         ("planner", "platform"),
         "One or more frames of HTML, drawn only inside a sandboxed frame."),
    Kind("details_input", "Details",
         (_t("question", max=2_000),
          _l("options", _s("option", max=TITLE_MAX), max=12),
          _b("free_text"),
          _t("answer", nullable=True, max=2_000),
          _s("task_id", nullable=True, max=TITLE_MAX)),
         (WAITING, DONE),
         ("answer",),
         ("planner", "platform"),
         "A question with options or free text; a lane's waiting question reuses "
         "the await-input card."),
    Kind("credential_input", "Credential",
         (_s("name", max=63, pattern=_NAME_RE),
          _s("provider", max=63, pattern=_NAME_RE),
          _t("why", max=LINE_MAX),
          _e("status", ("set", "missing"))),
         (WAITING, DONE),
         ("set_it",),
         ("planner",),
         "A secret's name, provider, why it is needed, and whether it is set. "
         "Never a value: Set it opens the one-time form."),
    Kind("plan", "Plan",
         (_t("goal", max=2_000),
          _e("size", ("answer", "one_fix", "feature", "rescue", "approvals_walk", "product")),
          _l("waves", _o("wave", _s("name", max=TITLE_MAX), _l("lanes", _LANE)), max=20),
          _n("reserved_usd"),
          _n("session_cap_usd"),
          _n("day_cap_usd")),
         (PROPOSED, ACTING, DONE),
         ("start", "change", "split"),
         ("planner",),
         "The goal, the size, waves of lanes with their territory and "
         "reservations, and the total against both caps."),
    Kind("budget", "Budget",
         (_l("lines", _o("line", _s("label", max=TITLE_MAX), _n("spent_usd"),
                         _n("reserved_usd"))),
          _n("spent_usd"),
          _n("reserved_usd"),
          _n("session_cap_usd"),
          _n("day_cap_usd"),
          _l("not_counted", _s("item", max=TITLE_MAX))),
         (WAITING, DONE),
         ("raise",),
         ("platform",),
         "Spent and reserved against the session and day caps, by line, and "
         "what is not counted."),
    Kind("stop", "Stop",
         (_t("what", max=2_000),
          _e("rule", ("iam", "secrets", "tenancy", "shared_project", "workflow_file")),
          _s("where", max=1024)),
         (WAITING, DONE),
         ("open_where",),
         ("platform",),
         "What stopped, which rule held it, and where the person acts."),
    Kind("rescue", "Rescue",
         (_o("subject", _e("kind", ("agent", "workflow", "inbox_item")),
             _s("id", max=TITLE_MAX)),
          _t("failure", max=2_000),
          _t("cause", max=2_000),
          _l("evidence", _t("item", max=2_000), max=12),
          _t("fix", max=2_000)),
         (PROPOSED, ACTING, DONE),
         ("fix_it", "explain_more"),
         ("planner",),
         "What failed, the likely cause and its evidence, and the proposed fix."),
    # voice-it-own-speech.md §7.2: swarm-api draws it when it holds a spoken
    # cancel, merge, deploy, delete or approve (voice/readback.py), never the
    # planner. Its state follows the read-back's (`readback.CARD_STATE`).
    Kind("readback", "Read-back",
         (_s("readback_id", max=TITLE_MAX, pattern=_READBACK_ID_RE),
          _t("heard", max=LINE_MAX),
          _e("action", ("cancel", "merge", "deploy", "delete", "approve")),
          _s("target_name", max=TITLE_MAX),
          _e("status", ("pending", "confirmed", "refused", "expired", "superseded", "used")),
          _s("expires_at", nullable=True, max=TITLE_MAX)),
         (WAITING, ACTING, DONE, DISMISSED, CANCELLED),
         ("go", "not_that"),
         ("platform",),
         "What was heard and what will be done, held until Go or Not that."),
    # §8.1: a spec the planner writes, in Markdown the console renders with its
    # safe renderer; each heading is a part a note can attach to (§8.3).
    Kind("spec", "Spec",
         (_t("markdown", max=SOURCE_MAX),
          _l("headings", _o("heading", _s("id", max=TITLE_MAX), _s("label", max=TITLE_MAX)),
             max=200)),
         (PROPOSED, DONE),
         ("comment", "open_full_size"),
         ("planner",),
         "A written spec in Markdown, its headings addressable by notes."),
    # §3.12.3: a console page offered after an answer. The planner writes it
    # only as `proposed` and only new (`writer_rules`); swarm-api moves it to
    # `accepted` on the person's click or spoken yes, or `dismissed`
    # (readback.Offers); the holder marks an accepted one `done` once it
    # has navigated. No field is an address.
    Kind("navigate", "Navigate",
         (_e("destination", NAVIGATE_DESTINATIONS),
          _o("object", _e("kind", NAVIGATE_OBJECTS), _s("id", max=TITLE_MAX, pattern=ID_RE),
             nullable=True),
          _s("why", max=SUMMARY_MAX),
          _e("accepted_by", ACCEPTED_BY, nullable=True)),
         (PROPOSED, ACCEPTED, DONE, DISMISSED),
         ("open",),
         ("planner",),
         "Offers one console page from a fixed list, after the answer: its destination, "
         "optionally one object's id, and why in one line. Always new, always proposed; "
         "only the person's yes opens it."),
)

BY_KIND: dict[str, Kind] = {k.kind: k for k in KINDS}


def kind_of(kind: Any) -> Kind:
    """The kind, or a 422 naming the known ones."""
    found = BY_KIND.get(kind) if isinstance(kind, str) else None
    if found is None:
        raise ValidationFailed(f"kind is one of {', '.join(BY_KIND)}",
                               detail={"field": "kind"})
    return found


def allows_action(kind: str, action: str) -> bool:
    found = BY_KIND.get(kind)
    return found is not None and action in found.actions


# ---- validation -------------------------------------------------------------


def _refuse(path: str, message: str) -> ValidationFailed:
    return ValidationFailed(f"{path}: {message}", detail={"field": path})


def _value(spec: Field, value: Any, path: str) -> Any:
    if value is None:
        if spec.nullable:
            return None
        raise _refuse(path, "is required")
    if spec.type in ("string", "text"):
        if not isinstance(value, str):
            raise _refuse(path, "is a string")
        if spec.type == "string" and ("\n" in value or "\r" in value):
            raise _refuse(path, "is one line")
        if spec.max is not None and len(value) > spec.max:
            raise _refuse(path, f"is at most {spec.max} characters")
        if spec.pattern is not None and not spec.pattern.match(value):
            raise _refuse(path, "is not in the accepted form")
        return value
    if spec.type == "enum":
        if value not in spec.values:
            raise _refuse(path, f"is one of {', '.join(spec.values)}")
        return value
    if spec.type == "boolean":
        if not isinstance(value, bool):
            raise _refuse(path, "is true or false")
        return value
    if spec.type in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (spec.type == "integer" and not isinstance(value, int)):
            raise _refuse(path, f"is a {spec.type}")
        if spec.min is not None and value < spec.min:
            raise _refuse(path, f"is at least {spec.min}")
        return value
    if spec.type == "list":
        if not isinstance(value, list):
            raise _refuse(path, "is a list")
        if spec.min is not None and len(value) < spec.min:
            raise _refuse(path, f"has at least {spec.min} items")
        if spec.max is not None and len(value) > spec.max:
            raise _refuse(path, f"has at most {spec.max} items")
        assert spec.item is not None
        return [_value(spec.item, item, f"{path}[{i}]") for i, item in enumerate(value)]
    if spec.type == "object":
        return _object(spec.fields, value, path)
    raise _refuse(path, f"has an unknown field type {spec.type!r}")


def _object(fields: tuple[Field, ...], value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse(path, "is an object")
    names = {f.name for f in fields}
    extra = sorted(str(k) for k in value if k not in names)
    if extra:
        # The name only: an unknown field is how a value would ride along.
        raise _refuse(f"{path}.{extra[0]}" if path else extra[0],
                      "is not a field of this card")
    return {f.name: _value(f, value.get(f.name), f"{path}.{f.name}" if path else f.name)
            for f in fields}


def _strings(value: Any, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))


def credential_refusal(card: dict[str, Any]) -> str | None:
    """The path of the first string in the card that the shared redaction
    rules would mask, or None. Never the string."""
    for path, text in _strings(card, ""):
        if redact(text, decoded=True).count:
            return path
    return None


#: The fields a writer sends: the envelope's writer fields.
WRITER_FIELDS = tuple(f.name for f in ENVELOPE if f.set_by == "writer")


def validate(card: Any) -> dict[str, Any]:
    """The card as it is stored, or a 422: an unknown kind, a field the
    envelope or the kind does not carry, a state the kind does not use, or a
    string the redaction rules recognise as a credential."""
    if not isinstance(card, dict):
        raise _refuse("card", "is an object")
    kind = kind_of(card.get("kind"))
    extra = sorted(str(k) for k in card if k not in WRITER_FIELDS)
    if extra:
        raise _refuse(extra[0], "is not a field of a card")
    card_id = card.get("card_id")
    if card_id is not None and (not isinstance(card_id, str) or not _CARD_ID_RE.match(card_id)):
        raise _refuse("card_id", "is a Voice it card id")
    state = card.get("state")
    if state not in kind.states:
        raise _refuse("state", f"a {kind.kind} card is one of {', '.join(kind.states)}")
    created_by = card.get("created_by")
    if created_by not in kind.created_by:
        raise _refuse("created_by",
                      f"a {kind.kind} card is created by {' or '.join(kind.created_by)}")
    out = {
        "card_id": card_id,
        "kind": kind.kind,
        "state": state,
        "title": _value(_s("title", max=TITLE_MAX), card.get("title"), "title"),
        "created_by": created_by,
        "references": _value(_REFERENCES, card.get("references"), "references"),
        "summary": _value(_s("summary", max=SUMMARY_MAX), card.get("summary"), "summary"),
        "body": _object(kind.fields, card.get("body"), "body"),
    }
    if not out["title"].strip():
        raise _refuse("title", "is required")
    found = credential_refusal(out)
    if found is not None:
        raise _refuse(found, "looks like a credential; a card never carries a credential value")
    return out


# ---- the stored card and its stream event -----------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def card_view(card_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """The card as the stream and the console carry it: the envelope in
    `ENVELOPE` order. The tenant and the person stay on the document."""
    stored = doc.get("body") or {}
    return {
        "card_id": card_id,
        "kind": doc.get("kind"),
        "project_id": doc.get("project_id"),
        "session_id": doc.get("session_id"),
        "version": doc.get("version"),
        "state": stored.get("state"),
        "title": stored.get("title"),
        "created_by": stored.get("created_by"),
        "references": stored.get("references"),
        "summary": stored.get("summary"),
        "at": _iso(doc.get("updated_at")),
        "body": stored.get("body"),
    }


CARD_UPSERT = "card.upsert"
CARD_REMOVE = "card.remove"


def writer_rules(checked: dict[str, Any], *, session_id: str | None) -> None:
    """What a writer may not do that the schema alone allows. A `navigate`
    offer is written only new, only `proposed`, with no `accepted_by`, and
    only on a session: `accepted` and `dismissed` are swarm-api's to set from
    the person's answer (§3.12.3), so the planner can neither accept an offer
    nor reopen one."""
    if checked["kind"] != "navigate":
        return
    if checked["state"] != PROPOSED:
        raise _refuse("state", "a navigate card is written only as proposed; "
                               "only the person's yes accepts it")
    if checked["body"].get("accepted_by") is not None:
        raise _refuse("body.accepted_by", "is set by swarm-api when the person accepts")
    if checked["card_id"] is not None:
        raise _refuse("card_id", "a navigate offer is new each time; draw another offer")
    if session_id is None:
        raise _refuse("kind", "a navigate offer belongs to a live session")


def _dismiss_older_offers(store: Any, *, tenant_id: str, person: str, project_id: str,
                          session_id: str, keep: str) -> None:
    """One offer is open a session at a time: a newer one replaces the
    session's older open offers, each written `dismissed` with its event."""
    for doc in store.list_cards(tenant_id=tenant_id, person=person, project_id=project_id):
        stored = doc.get("body") or {}
        if (doc.get("kind") != "navigate" or doc.get("session_id") != session_id
                or doc.get("card_id") == keep or stored.get("state") != PROPOSED):
            continue
        moved = store.put_card(
            tenant_id=tenant_id, person=person, project_id=project_id, card_id=doc["card_id"],
            kind="navigate", session_id=session_id, turn_id=doc.get("turn_id"),
            body={**stored, "state": DISMISSED})
        store.append_event(tenant_id=tenant_id, person=person, project_id=project_id,
                           session_id=session_id,
                           event={"type": CARD_UPSERT, "card": card_view(doc["card_id"], moved)})


def write_card(store: Any, *, tenant_id: str, person: str, project_id: str,
               session_id: str | None, card: Any, turn_id: str | None = None) -> dict[str, Any]:
    """Validate, store the card's next version, and append its `card.upsert`
    to the session's stream when it belongs to one. `store` is a
    `voice.store.VoiceStore`; the tenant and the person are the caller's,
    resolved by the route, never a field of the card. A new navigate offer
    replaces the session's open one."""
    checked = validate(card)
    writer_rules(checked, session_id=session_id)
    card_id = checked["card_id"] or "vc_" + secrets.token_hex(16)
    doc = store.put_card(
        tenant_id=tenant_id, person=person, project_id=project_id, card_id=card_id,
        kind=checked["kind"], session_id=session_id, turn_id=turn_id,
        body={k: checked[k] for k in ("state", "title", "created_by", "references",
                                      "summary", "body")})
    view = card_view(card_id, doc)
    if session_id is not None:
        store.append_event(tenant_id=tenant_id, person=person, project_id=project_id,
                           session_id=session_id, event={"type": CARD_UPSERT, "card": view})
        if checked["kind"] == "navigate":
            _dismiss_older_offers(store, tenant_id=tenant_id, person=person,
                                  project_id=project_id, session_id=session_id, keep=card_id)
    return view


# ---- the planner's strict tool schemas --------------------------------------


def json_schema(spec: Field) -> dict[str, Any]:
    """`spec` as strict-mode JSON Schema: closed objects, every property
    required, optional values nullable."""
    if spec.type in ("string", "text"):
        out: dict[str, Any] = {"type": "string"}
    elif spec.type == "enum":
        out = {"type": "string", "enum": list(spec.values)}
    elif spec.type == "boolean":
        out = {"type": "boolean"}
    elif spec.type in ("integer", "number"):
        out = {"type": spec.type}
    elif spec.type == "list":
        assert spec.item is not None
        out = {"type": "array", "items": json_schema(spec.item)}
    else:
        out = {"type": "object", "additionalProperties": False,
               "properties": {f.name: json_schema(f) for f in spec.fields},
               "required": [f.name for f in spec.fields]}
    if spec.nullable:
        if spec.type == "object":
            return {"anyOf": [out, {"type": "null"}]}
        out["type"] = [out["type"], "null"]
        if "enum" in out:
            out["enum"] = [*out["enum"], None]
    return out


def tool_schema(kind: Kind) -> dict[str, Any]:
    """The planner's `draw_<kind>` tool. A null `card_id` draws a new card; a
    card's id draws its next version."""
    inputs = (
        _s("card_id", nullable=True),
        _e("state", kind.states),
        _s("title", max=TITLE_MAX),
        _s("summary", max=SUMMARY_MAX),
        _REFERENCES,
        _o("body", *kind.fields),
    )
    return {
        "name": f"draw_{kind.kind}",
        "description": f"Draw or update a {kind.label} card. {kind.description}",
        "strict": True,
        "input_schema": json_schema(_o("input", *inputs)),
    }


def tool_schemas() -> list[dict[str, Any]]:
    return [tool_schema(k) for k in KINDS if "planner" in k.created_by]


def _field_view(spec: Field) -> dict[str, Any]:
    out: dict[str, Any] = {"name": spec.name, "type": spec.type, "nullable": spec.nullable}
    if spec.values:
        out["values"] = list(spec.values)
    if spec.item is not None:
        out["item"] = _field_view(spec.item)
    if spec.fields:
        out["fields"] = [_field_view(f) for f in spec.fields]
    return out


def catalogue() -> dict[str, Any]:
    """What `GET /v1/voice/cards/schema` serves."""
    return {
        "version": CATALOGUE_VERSION,
        "fields": [{"name": f.name, "set_by": f.set_by} for f in ENVELOPE],
        "states": list(STATES),
        "creators": list(CREATORS),
        "references": list(REFERENCES),
        "kinds": [{"kind": k.kind, "label": k.label, "states": list(k.states),
                   "actions": list(k.actions), "created_by": list(k.created_by),
                   "fields": [_field_view(f) for f in k.fields]} for k in KINDS],
        "tools": tool_schemas(),
    }
