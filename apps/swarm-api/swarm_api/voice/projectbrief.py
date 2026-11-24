"""A Voice it project's status view and its planner brief (voice-it.md §3.7,
§3.12.6; voice-it-own-speech.md §3.3 step 4).

THE STATUS VIEW (`status`). What the console's project page already shows
this person for this project, in one read: the project's title and state,
its open cards and the cards that wait on the owner (`VoiceStore.
project_counts`), its newest cards as one-liners (kind, title, state,
summary), and its roster (`roster.Roster.rows`: the delegations, agents,
workflows and runs it started, each with its state). It is how the fast
front answers "what is running" or "did that finish" in its own turn,
instead of delegating a status question to the planner. Every read is the
caller's own project in the caller's tenant (`VoiceStore._owned`), exactly
the roster route's check; nothing here settles, moves or writes anything.

THE PLANNER BRIEF (`write_brief`, `read_brief`). After each planner turn
swarm-voice writes the turn's short brief of the project's state and open
threads, at most `BRIEF_TEXT_MAX` bytes of UTF-8, as the project document's
`planner_brief` field: `{text, at, session_id}`. It is kept apart from the
`brief` object (whose `open` list notes_to_work and the state rules use), so
neither write can drop the other's. A later delegation reads it back through
the status view, so the planner does not start cold.

THE BRIEF IS DATA. A model wrote it from tool results and a person's words,
so wherever it is read back into a prompt it is fenced as quoted data
(swarm_voice/prompts.py `fence`), as transcripts are. Here it is redacted
with the shared rules before it is stored, and a credential-shaped brief is
refused whole, as a card is.

THE CAPS. The brief rides in the planner turn's own output (`speak`'s
`brief`), so it costs no model call of its own and its tokens are in the
turn's recorded spend. A session at a spend cap writes no brief: the write
asks the caps first (`budget.BudgetGuard.enforce`) and is refused, nothing
stored, when the session is capped.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..errors import ValidationFailed
from ..redaction import redact
from . import roster as vroster
from . import store as vstore

#: The longest planner brief, in bytes of UTF-8: a short note of the
#: project's state and open threads, read into every later planner turn.
BRIEF_TEXT_MAX = 2048
#: The project document's field that holds it.
BRIEF_FIELD = "planner_brief"
#: The newest cards the status view names (the page's own list is longer).
STATUS_CARDS_MAX = 30
#: A card's summary in the status view, in characters.
SUMMARY_MAX = 280


def _cut(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def read_brief(data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The stored planner brief, `{text, at, session_id}`, or None."""
    held = (data or {}).get(BRIEF_FIELD)
    if not isinstance(held, Mapping) or not isinstance(held.get("text"), str):
        return None
    text = held["text"].strip()
    if not text:
        return None
    at = held.get("at")
    return {"text": _cut(text, BRIEF_TEXT_MAX),
            "at": at.isoformat() if hasattr(at, "isoformat") else at,
            "session_id": held.get("session_id")}


def _card_line(card: Mapping[str, Any]) -> dict[str, Any]:
    body = card.get("body") if isinstance(card.get("body"), Mapping) else {}
    summary = body.get("summary")
    at = card.get("updated_at")
    return {"card_id": card.get("card_id"), "kind": card.get("kind"),
            "title": body.get("title"), "state": body.get("state"),
            "summary": summary[:SUMMARY_MAX] if isinstance(summary, str) else None,
            "updated_at": at.isoformat() if hasattr(at, "isoformat") else at}


def _newest(card: Mapping[str, Any]) -> str:
    at = card.get("updated_at")
    return at.isoformat() if hasattr(at, "isoformat") else str(at or "")


def status(db: Any, store: vstore.VoiceStore, *, tenant_id: str, person: str,
           project_id: str) -> dict[str, Any]:
    """The project's status view (module docstring); `NotFound` unless the
    project is this person's in this tenant."""
    data = store._owned(tenant_id, person, project_id)  # noqa: SLF001 - the store's own check
    counts = store.project_counts(tenant_id=tenant_id, person=person, project_id=project_id)
    cards = store.list_cards(tenant_id=tenant_id, person=person, project_id=project_id)
    cards.sort(key=_newest, reverse=True)
    rows = vroster.Roster(db, store).rows(tenant_id=tenant_id, person=person,
                                          project_id=project_id)
    return {
        "project_id": project_id,
        "title": data.get("title") or None,
        "state": vstore.read_state(data.get("status")),
        "open_cards": counts.get("open_cards"),
        "waiting_on_owner": list(counts.get("waiting_on_owner") or []),
        "cards": [_card_line(c) for c in cards[:STATUS_CARDS_MAX]],
        "cards_total": len(cards),
        "roster": rows,
        "brief": read_brief(data),
    }


def check_text(text: Any) -> str:
    """A brief's text, stripped; 422 when it is not text, is over
    `BRIEF_TEXT_MAX` bytes, or holds anything credential-shaped."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationFailed("a brief is non-empty text", detail={"field": "text"})
    text = text.strip()
    if len(text.encode("utf-8")) > BRIEF_TEXT_MAX:
        raise ValidationFailed(f"a brief is at most {BRIEF_TEXT_MAX} bytes",
                               detail={"field": "text"})
    if redact(text, decoded=True).count:
        # Never echoed: the detail names the field, not the text.
        raise ValidationFailed("a brief holds no credential", detail={"field": "text"})
    return text


def write_brief(store: vstore.VoiceStore, *, tenant_id: str, person: str, project_id: str,
                session_id: str, text: Any, now: Callable[[], Any]) -> dict[str, Any]:
    """Store the planner's brief on this person's project; the stored brief.
    The caller has already asked the caps."""
    clean = check_text(text)
    store._owned(tenant_id, person, project_id)  # noqa: SLF001 - the store's own check
    held = {"text": clean, "at": now(), "session_id": session_id}
    store._project_ref(project_id).update({BRIEF_FIELD: held})  # noqa: SLF001
    return read_brief({BRIEF_FIELD: held}) or {}
