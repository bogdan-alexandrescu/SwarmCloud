"""The read-back gate: a spoken command that cancels, merges, deploys, deletes
or approves is read back first, and runs only on a "go"
(docs/design/voice-it-own-speech.md §7).

WHICH ROUTES (§7.1). Every act route a session grant could reach is classed
here by its (method, route template): `GATED_ROUTES` maps a route to the verb
in `READBACK_VERBS` it performs, `AS_HEARD_ROUTES` names the acts that run as
heard (filing, starting, answering, scheduling, drawing). A route on the
grant's lists (`voice.grant`) that is not a GET and is in neither is unclassed,
and tests/unit/voice/test_readback.py fails on it, so a new act route cannot
land without a class.

HOW IT HOLDS (§7.2). swarm-api enforces it, not the prompt: a gated route
reached under a grant with no confirmed read-back does not run
(`routes.voice_readback.readback_gate`). `Readbacks.hold` stores
`{id, session_id, heard, action, target_name, method, route, body_hash, state:
"pending", expires_at}` and the route answers 202 with the id. `heard` is the
last final utterance the session posted (`heard`), redacted; `target_name` is
resolved here from the route's path parameters (`target_name`), never taken
from the model. A `readback` card (voice/cards.py) shows "Heard / Will
<action>: <target_name>" with Go and Not that.

  * Go (spoken, `answer`, or a tap, `decide`) marks it `confirmed`; swarm-voice
    re-sends the held request with `X-Swarm-Voice-Readback: <id>`, and
    `consume` lets it run only when the id is confirmed, unused, of this
    session, and its method, route and body hash match. It is then `used`.
  * Not that marks it `refused`; nothing runs.
  * One read-back is pending a session at a time: a newer gated call marks the
    older `superseded`.

HOW "GO" IS RECOGNISED (§7.3). Deterministically, before the planner, for both
engines (`verdict`): the utterance is lower-cased and stripped of punctuation
and fillers, and only an exact phrase answers. A bare "yes" is NOT Go: it is
too often an answer to something else, so it, like any other sentence, leaves
the read-back pending and goes to the planner as a turn.

SILENCE (§7.4). A pending read-back expires `EXPIRE_SECONDS` after the voice
finishes asking: `expired`, nothing runs, the card greys and stays. swarm-api
does not hear the voice finish, so the clock starts at the hold plus
`ASK_SECONDS`, the time "Will <action> <target>. Go?" takes to say. Expiry is
judged on every read and write of the session's read-backs
(`expire_due`), so a late "go" never confirms one. While one is pending it is
activity (`pending`): the idle clock does not run.

AN OPEN NAVIGATION OFFER IS HELD LIKE A READ-BACK (voice-it.md §3.12.3). The
planner offers a console page as a `navigate` card, always new and always
`proposed` (cards.writer_rules). The owner's next whole utterance through this
gate answers it after any pending read-back has had its turn (a "go" confirms
a pending read-back first): a yes (`OFFER_YES`: "yes", "open it", "go", "show
me") accepts the newest open offer, a no (`OFFER_NO`) dismisses it, and
anything else leaves it open and goes to the planner. `Offers` moves the card
in one transaction, from `proposed` only, with `accepted_by: person`, and the
person's click (routes/voice_cards.py) takes the same path. One offer is open
a session at a time: a newer one dismisses the older on write
(cards.write_card); one unanswered for `OFFER_SECONDS`, or outlived by the
next turn's output (a planner card written after the utterance that followed
it), is dismissed when the session's offers are next read (`Offers.settle`).
Nothing here navigates: the holder does, on an accepted card of its own
session (§3.12.3).

NOTHING HERE IS A CREDENTIAL, and nothing logs what was heard: `heard` is put
through the shared redaction rules before it is stored, and log lines name ids
and states only.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_redaction import redact

from ..errors import Conflict, NotFound, ValidationFailed
from . import cards as vcards
from . import sessions as vs
from . import store as vstore

#: The verbs a spoken command is read back for (§7.1, pick 3).
READBACK_VERBS: tuple[str, ...] = ("cancel", "merge", "deploy", "delete", "approve")

#: The class of an act that runs as heard.
AS_HEARD = "as_heard"

#: The header swarm-voice re-sends a confirmed call with.
READBACK_HEADER = "X-Swarm-Voice-Readback"

#: Where a session's read-backs live, under its session document.
READBACKS = "readbacks"
#: The session's pointer to its one pending read-back and the last final heard.
READBACK_STATE = "readback_state"
CURRENT = "current"

PENDING = "pending"
CONFIRMED = "confirmed"
REFUSED = "refused"
EXPIRED = "expired"
SUPERSEDED = "superseded"
USED = "used"
STATES: tuple[str, ...] = (PENDING, CONFIRMED, REFUSED, EXPIRED, SUPERSEDED, USED)

#: §7.4: 45 seconds after the voice finishes asking.
EXPIRE_SECONDS = 45
#: What saying "Will <action> <target>. Go?" takes; swarm-api does not hear it end.
ASK_SECONDS = 10

#: How long a final utterance is kept as what was heard, and its length.
HEARD_MAX = vcards.LINE_MAX
TARGET_MAX = vcards.TITLE_MAX

_READBACK_ID_RE = re.compile(r"^vr_[0-9a-f]{32}$")

log = logging.getLogger("swarm_api.voice.readback")

# ---- which routes (§7.1) ----------------------------------------------------

#: Every gated act route, by (method, route template), and its verb. These
#: are swarm-api's routes today; each joins the grant's lists only in the act
#: lane that opens it to Voice it, and then runs only through the gate.
GATED_ROUTES: Mapping[tuple[str, str], str] = {
    ("POST", "/v1/tasks/{task_id}/cancel"): "cancel",
    ("POST", "/v1/workflows/{workflow_id}/cancel"): "cancel",
    ("DELETE", "/v1/schedules/{schedule_id}"): "delete",
    ("DELETE", "/v1/voice/projects/{project_id}"): "delete",
    ("POST", "/v1/approvals/{item_id}:approve"): "approve",
    ("POST", "/v1/runs/{run_id}/plan:approve"): "approve",
}

#: The act routes a grant reaches that run as heard: drawing a card,
#: answering a read-back, drawing a visual, pinning a note (§8.3) and
#: answering an agent's question.
AS_HEARD_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/voice/sessions/{session_id}/cards"),
    ("POST", "/v1/voice/sessions/{session_id}/readbacks/answer"),
    ("POST", "/v1/voice/sessions/{session_id}/visuals"),
    ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"),
    # The planner's answer to an agent's question (voice-it.md §3.9), read
    # back by swarm-voice before it is sent (swarm_voice/tools_act.py).
    ("POST", "/v1/tasks/{task_id}/answers"),
    # Debug mode's trace of a planner turn (voice/trace.py): a record of what
    # the turn did, never an act, so nothing is read back before it.
    ("POST", "/v1/voice/sessions/{session_id}/trace"),
    # A spent Live key's next tier (llm-providers.md §5.3): swarm-voice says
    # which tier is held for the reopen, never an act of the person's.
    ("POST", "/v1/voice/sessions/{session_id}/reopen"),
    # The work feed (voice-it.md §3.12.5): a delegation's lifecycle and the
    # roster's changed rows, a record of the session's work, never an act.
    ("POST", "/v1/voice/sessions/{session_id}/work"),
})

#: A route template whose last step names a gated verb, or a DELETE: what the
#: test holds to `GATED_ROUTES` when such a route is on the grant's lists.
_VERB_PATH = re.compile(r"(?:/|:)(cancel|merge|deploy|release|approve)(?:$|/)")


def classify(method: str, route: str) -> str | None:
    """The verb a gated act route performs, `AS_HEARD`, or None when the
    route is unclassed."""
    key = (method.upper(), route)
    if key in GATED_ROUTES:
        return GATED_ROUTES[key]
    if key in AS_HEARD_ROUTES:
        return AS_HEARD
    return None


def looks_gated(method: str, route: str) -> bool:
    """Whether a route's method or template names a gated verb."""
    return method.upper() == "DELETE" or _VERB_PATH.search(route) is not None


#: What a target is called on the card, and where its name is read:
#: (the noun, the path parameter, the collection holding its document).
TARGETS: Mapping[tuple[str, str], tuple[str, str, str | None]] = {
    ("POST", "/v1/tasks/{task_id}/cancel"): ("agent", "task_id", "tasks"),
    ("POST", "/v1/workflows/{workflow_id}/cancel"): ("workflow", "workflow_id", "workflows"),
    ("DELETE", "/v1/schedules/{schedule_id}"): ("schedule", "schedule_id", "schedules"),
    ("DELETE", "/v1/voice/projects/{project_id}"): ("project", "project_id", vs.PROJECTS),
    ("POST", "/v1/approvals/{item_id}:approve"): ("inbox item", "item_id", "approvals"),
    ("POST", "/v1/runs/{run_id}/plan:approve"): ("run plan", "run_id", None),
}

_NAME_FIELDS = ("title", "name", "display_name")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def target_name(db: Any, *, tenant_id: str, method: str, route: str,
                path_params: Mapping[str, Any]) -> str:
    """What the card names: the target's own name from its document in the
    caller's tenant, else its noun and id. Never from the model."""
    noun, param, collection = TARGETS.get((method.upper(), route), ("item", "", None))
    ident = str(path_params.get(param, "") or "").strip()
    name = ""
    if collection and ident:
        try:
            snap = _snapshot(db.collection(collection).document(ident).get())
            data = (snap.to_dict() or {}) if snap.exists else {}
        except Exception:  # noqa: BLE001 - a name is a courtesy; the id still names it
            data = {}
        if data.get("tenant_id") == tenant_id:
            name = next((str(data[f]) for f in _NAME_FIELDS
                         if isinstance(data.get(f), str) and data[f].strip()), "")
    shown = f"{noun} {name} ({ident})" if name else f"{noun} {ident}".strip()
    return _clip(redact(shown).text, TARGET_MAX)


# ---- how "go" is recognised (§7.3) ------------------------------------------

GO = "go"
REFUSE = "refuse"

GO_PHRASES: frozenset[str] = frozenset({"go", "go ahead", "yes go", "do it", "confirm",
                                        "yes do it"})
REFUSE_PHRASES: frozenset[str] = frozenset({"not that", "no", "stop", "cancel", "dont",
                                            "wait"})
FILLERS: frozenset[str] = frozenset({"uh", "um", "okay"})


def normalise(text: str) -> str:
    """Lower case, no punctuation (an apostrophe joins its word: "don't" is
    "dont"), no fillers, single spaces."""
    lowered = str(text or "").lower().replace("'", "").replace("’", "")
    words = re.sub(r"[^\w\s]", " ", lowered).split()
    return " ".join(w for w in words if w not in FILLERS)


def verdict(text: str) -> str | None:
    """GO, REFUSE, or None when the utterance is not an answer. A bare "yes"
    is None."""
    said = normalise(text)
    if said in GO_PHRASES:
        return GO
    if said in REFUSE_PHRASES:
        return REFUSE
    return None


#: A spoken answer to a navigation offer (§3.12.3). A bare "yes" is a yes
#: here, unlike a read-back's Go: an offer only opens a page.
ACCEPT = "accept"
DISMISS = "dismiss"
OFFER_YES: frozenset[str] = frozenset({"yes", "open it", "go", "show me", "yes please",
                                       "yes open it", "yes show me", "go ahead"})
OFFER_NO: frozenset[str] = frozenset({"no", "not that", "no thanks", "not now", "dont",
                                      "nope"})
#: An offer not answered within two minutes is dismissed.
OFFER_SECONDS = 120
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def offer_verdict(text: str) -> str | None:
    """ACCEPT, DISMISS, or None when the utterance is not an answer to an offer."""
    said = normalise(text)
    if said in OFFER_YES:
        return ACCEPT
    if said in OFFER_NO:
        return DISMISS
    return None


def body_hash(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


# ---- the card ---------------------------------------------------------------

#: A read-back's state as its card's common state (voice/cards.py).
CARD_STATE: Mapping[str, str] = {
    PENDING: vcards.WAITING, CONFIRMED: vcards.ACTING, USED: vcards.DONE,
    REFUSED: vcards.DISMISSED, EXPIRED: vcards.CANCELLED, SUPERSEDED: vcards.CANCELLED,
}


def card_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The `readback` card a read-back is shown as."""
    expires = doc.get("expires_at")
    return {
        "card_id": doc.get("card_id"), "kind": "readback",
        "state": CARD_STATE[str(doc.get("state"))],
        "title": _clip(f"Will {doc.get('action')}: {doc.get('target_name')}", vcards.TITLE_MAX),
        "created_by": "platform",
        "references": {name: None for name in vcards.REFERENCES},
        "summary": _clip(f"Will {doc.get('action')} {doc.get('target_name')}. Go?",
                         vcards.SUMMARY_MAX),
        "body": {
            "readback_id": doc.get("id"), "heard": doc.get("heard") or "",
            "action": doc.get("action"), "target_name": doc.get("target_name"),
            "status": doc.get("state"),
            "expires_at": expires.isoformat() if isinstance(expires, datetime) else None,
        },
    }


def view(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A read-back as a route answers it: no body hash, no tenant."""
    expires = doc.get("expires_at")
    return {
        "readback_id": doc.get("id"), "session_id": doc.get("session_id"),
        "heard": doc.get("heard") or "", "action": doc.get("action"),
        "target_name": doc.get("target_name"), "method": doc.get("method"),
        "route": doc.get("route"), "state": doc.get("state"), "card_id": doc.get("card_id"),
        "expires_at": expires.isoformat() if isinstance(expires, datetime) else expires,
    }


# ---- refusals ---------------------------------------------------------------


class ReadbackRefused(Conflict):
    """409: the presented read-back is not one this call may run on."""

    code = "readback_refused"


class ReadbackNotPending(Conflict):
    """409: the read-back was already answered, superseded or expired."""

    code = "readback_not_pending"


# ---- the store --------------------------------------------------------------


@dataclass(frozen=True)
class Where:
    """The session a read-back belongs to, as the grant or the session
    document resolved it."""

    tenant_id: str
    person: str
    project_id: str
    session_id: str


def check_readback_id(value: Any) -> str:
    if not isinstance(value, str) or not _READBACK_ID_RE.match(value):
        raise ValidationFailed("readback_id is a Voice it read-back id",
                               detail={"field": "readback_id"})
    return value


def _aware(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


class Readbacks:
    """A session's read-backs in Firestore, and their cards.

    `cards` writes a card for (where, card) -- `cards.write_card` through the
    session's `VoiceStore` in swarm-api -- after each change of state."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 cards: Callable[[Where, dict[str, Any]], dict[str, Any]] | None = None,
                 offers: "Offers | None" = None) -> None:
        self._db = db
        self._now = now
        self._cards = cards
        self._offers = offers if offers is not None else Offers(db, now=now)

    # -- paths

    def _session_ref(self, where: Where) -> Any:
        return self._db.collection(vs.PROJECTS).document(where.project_id) \
            .collection(vs.SESSIONS).document(where.session_id)

    def _ref(self, where: Where, readback_id: str) -> Any:
        return self._session_ref(where).collection(READBACKS).document(readback_id)

    def _state_ref(self, where: Where) -> Any:
        return self._session_ref(where).collection(READBACK_STATE).document(CURRENT)

    def _read(self, txn: Any, ref: Any) -> dict[str, Any]:
        snap = _snapshot(txn.get(ref) if txn is not None else ref.get())
        return (snap.to_dict() or {}) if snap.exists else {}

    def _mine(self, doc: Mapping[str, Any], where: Where) -> bool:
        return (doc.get("tenant_id") == where.tenant_id and doc.get("person") == where.person
                and doc.get("session_id") == where.session_id)

    def _draw(self, where: Where, docs: list[dict[str, Any]]) -> None:
        """Draw each changed read-back's card. The read-back's state is what
        the gate judges; a card that cannot be drawn is logged by id and never
        undoes or blocks a state already committed (a used read-back's call
        still runs)."""
        if self._cards is None:
            return
        for doc in docs:
            try:
                drawn = self._cards(where, card_of(doc))
                if not doc.get("card_id") and isinstance(drawn, dict) and drawn.get("card_id"):
                    doc["card_id"] = drawn["card_id"]
                    self._ref(where, str(doc["id"])).update({"card_id": doc["card_id"]})
            except Exception as exc:  # noqa: BLE001 - the card is the record, not the gate
                log.warning("voice read-back %s: its card was not drawn (%s)",
                            doc.get("id"), type(exc).__name__)

    def _expired(self, doc: Mapping[str, Any], now: datetime) -> bool:
        expires = _aware(doc.get("expires_at"))
        return doc.get("state") == PENDING and expires is not None and now >= expires

    # -- what was heard

    def heard(self, where: Where, text: str) -> None:
        """Record the session's last final utterance (redacted): what a read-back
        created next says it heard."""
        self._state_ref(where).set({
            "heard": _clip(redact(str(text or "")).text, HEARD_MAX), "heard_at": self._now(),
            "tenant_id": where.tenant_id, "person": where.person,
            "session_id": where.session_id}, merge=True)

    # -- hold (§7.2 step 1, and step 5's supersede)

    def hold(self, where: Where, *, action: str, target: str, method: str, route: str,
             body: bytes | None) -> dict[str, Any]:
        """Store a pending read-back for a gated call, superseding the
        session's older pending one, and draw its card."""
        if action not in READBACK_VERBS:
            raise ValidationFailed(f"{action!r} is not a read-back verb", detail={"field": "action"})
        readback_id = "vr_" + secrets.token_hex(16)
        state_ref = self._state_ref(where)
        ref = self._ref(where, readback_id)

        @firestore.transactional
        def _apply(txn: Any) -> list[dict[str, Any]]:
            current = self._read(txn, state_ref)
            changed: list[dict[str, Any]] = []
            older_id = current.get("pending")
            if isinstance(older_id, str) and older_id:
                older_ref = self._ref(where, older_id)
                older = self._read(txn, older_ref)
                if older.get("state") == PENDING:
                    older = {**older, "state": SUPERSEDED, "decided_at": self._now()}
                    txn.set(older_ref, older)
                    changed.append(older)
            now = self._now()
            doc = {
                "id": readback_id, "tenant_id": where.tenant_id, "person": where.person,
                "project_id": where.project_id, "session_id": where.session_id,
                "heard": str(current.get("heard") or ""), "action": action,
                "target_name": target, "method": method.upper(), "route": route,
                "body_hash": body_hash(body), "state": PENDING, "card_id": None,
                "created_at": now,
                "expires_at": now + timedelta(seconds=ASK_SECONDS + EXPIRE_SECONDS),
            }
            txn.set(ref, doc)
            txn.set(state_ref, {**current, "pending": readback_id, "tenant_id": where.tenant_id,
                                "person": where.person, "session_id": where.session_id})
            return [*changed, doc]

        changed = _apply(self._db.transaction())
        self._draw(where, changed)
        return changed[-1]

    # -- answers (§7.2 steps 3 and 4, §7.3)

    def _decide(self, where: Where, readback_id: str | None, to: str) -> dict[str, Any] | None:
        """Move the pending read-back (or `readback_id`, which must be the
        pending one) to `to`; None when nothing is pending."""
        state_ref = self._state_ref(where)

        @firestore.transactional
        def _apply(txn: Any) -> tuple[dict[str, Any] | None, bool]:
            current = self._read(txn, state_ref)
            target_id = readback_id or current.get("pending")
            if not isinstance(target_id, str) or not target_id:
                return None, False
            ref = self._ref(where, target_id)
            doc = self._read(txn, ref)
            if not doc or not self._mine(doc, where):
                if readback_id is not None:
                    raise NotFound(f"voice read-back {readback_id!r} not found")
                return None, False
            now = self._now()
            if self._expired(doc, now):
                doc = {**doc, "state": EXPIRED, "decided_at": now}
                txn.set(ref, doc)
                txn.set(state_ref, {**current, "pending": None})
                return doc, True
            if doc.get("state") != PENDING:
                if readback_id is not None:
                    return doc, False
                return None, False
            doc = {**doc, "state": to, "decided_at": now}
            txn.set(ref, doc)
            txn.set(state_ref, {**current, "pending": None})
            return doc, True

        doc, changed = _apply(self._db.transaction())
        if doc is not None and changed:
            self._draw(where, [doc])
        return doc

    def decide(self, where: Where, readback_id: str, *, go: bool) -> dict[str, Any]:
        """A tap on Go or Not that. 409 unless the read-back is still pending."""
        check_readback_id(readback_id)
        doc = self._decide(where, readback_id, CONFIRMED if go else REFUSED)
        if doc is None:
            raise NotFound(f"voice read-back {readback_id!r} not found")
        if doc.get("state") != (CONFIRMED if go else REFUSED):
            raise ReadbackNotPending(
                f"voice read-back {readback_id!r} is {doc.get('state')}; nothing was changed",
                detail={"state": doc.get("state")})
        return doc

    def answer(self, where: Where, text: str) -> dict[str, Any]:
        """A final utterance, before the planner sees it: GO or REFUSE answers
        the pending read-back; else a yes or a no answers the open navigation
        offer (`offer`); anything else (a bare "yes" with no offer open
        included) is recorded as heard and goes to the planner."""
        said = verdict(text)
        doc = None
        if said is not None:
            doc = self._decide(where, None, CONFIRMED if said == GO else REFUSED)
            if doc is not None and doc.get("state") != EXPIRED:
                return {"answer": said, "readback": view(doc), "to_planner": False}
        offered = offer_verdict(text)
        if offered is not None:
            card = self._offers.answer(where, accept=offered == ACCEPT)
            if card is not None:
                return {"answer": None, "readback": view(doc) if doc else None,
                        "offer": {"answer": offered, "card": card}, "to_planner": False}
        # No read-back or offer was waiting: the words are an ordinary turn.
        self.heard(where, text)
        if said is None:
            self.expire_due(where)
        return {"answer": None, "readback": view(doc) if doc else None, "to_planner": True}

    # -- the re-sent call (§7.2 step 3)

    def consume(self, where: Where, readback_id: str, *, method: str, route: str,
                body: bytes | None) -> dict[str, Any]:
        """Mark a confirmed read-back `used` when this call is the one it
        held; `ReadbackRefused` otherwise, and nothing runs."""
        if not isinstance(readback_id, str) or not _READBACK_ID_RE.match(readback_id):
            raise ReadbackRefused("the read-back id is not valid; nothing was run")
        ref = self._ref(where, readback_id)
        presented = body_hash(body)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            doc = self._read(txn, ref)
            if not doc or not self._mine(doc, where):
                raise ReadbackRefused("no read-back of this session has that id; nothing was run")
            if doc.get("state") != CONFIRMED:
                raise ReadbackRefused(
                    f"the read-back is {doc.get('state')}, not confirmed; nothing was run",
                    detail={"state": doc.get("state")})
            if (doc.get("method") != method.upper() or doc.get("route") != route
                    or doc.get("body_hash") != presented):
                raise ReadbackRefused("this call is not the one that was read back; nothing was run")
            doc = {**doc, "state": USED, "used_at": self._now()}
            txn.set(ref, doc)
            return doc

        doc = _apply(self._db.transaction())
        self._draw(where, [doc])
        return doc

    def lost(self, where: Where, readback_id: str) -> dict[str, Any] | None:
        """swarm-voice no longer holds the confirmed call (a restart): the
        read-back is `expired` and nothing runs."""
        ref = self._ref(where, readback_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            doc = self._read(txn, ref)
            if not doc or not self._mine(doc, where) or doc.get("state") != CONFIRMED:
                return None
            doc = {**doc, "state": EXPIRED, "decided_at": self._now()}
            txn.set(ref, doc)
            return doc

        doc = _apply(self._db.transaction())
        if doc is not None:
            self._draw(where, [doc])
        return doc

    # -- silence (§7.4)

    def expire_due(self, where: Where) -> dict[str, Any] | None:
        """Expire the session's pending read-back once its time is up."""
        state_ref = self._state_ref(where)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            current = self._read(txn, state_ref)
            pending_id = current.get("pending")
            if not isinstance(pending_id, str) or not pending_id:
                return None
            ref = self._ref(where, pending_id)
            doc = self._read(txn, ref)
            if not self._expired(doc, self._now()):
                return None
            doc = {**doc, "state": EXPIRED, "decided_at": self._now()}
            txn.set(ref, doc)
            txn.set(state_ref, {**current, "pending": None})
            return doc

        doc = _apply(self._db.transaction())
        if doc is not None:
            self._draw(where, [doc])
        return doc

    def pending(self, where: Where) -> dict[str, Any] | None:
        """The session's pending read-back, after expiry is judged: while one
        is pending the session is active and its idle clock does not run."""
        self.expire_due(where)
        current = self._read(None, self._state_ref(where))
        pending_id = current.get("pending")
        if not isinstance(pending_id, str) or not pending_id:
            return None
        doc = self._read(None, self._ref(where, pending_id))
        return doc if doc.get("state") == PENDING and self._mine(doc, where) else None

    def get(self, where: Where, readback_id: str) -> dict[str, Any]:
        check_readback_id(readback_id)
        self.expire_due(where)
        doc = self._read(None, self._ref(where, readback_id))
        if not doc or not self._mine(doc, where):
            raise NotFound(f"voice read-back {readback_id!r} not found")
        return doc


# ---- the navigation offer (voice-it.md §3.12.3) -----------------------------


class Offers:
    """A session's `navigate` cards: which one is open, and the person's
    answer to it. Reads the cards and the gate's `heard_at` from Firestore
    and moves a card through its stored document, so its `card.upsert`
    reaches the page."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _cards_ref(self, where: Where) -> Any:
        return self._db.collection(vs.PROJECTS).document(where.project_id) \
            .collection(vstore.CARDS)

    def _session_cards(self, where: Where) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for snap in self._cards_ref(where).stream():
            doc = snap.to_dict() or {}
            if (doc.get("tenant_id") == where.tenant_id and doc.get("person") == where.person
                    and doc.get("session_id") == where.session_id):
                out.append((snap.id, doc))
        return out

    def _heard_at(self, where: Where) -> datetime | None:
        ref = self._db.collection(vs.PROJECTS).document(where.project_id) \
            .collection(vs.SESSIONS).document(where.session_id) \
            .collection(READBACK_STATE).document(CURRENT)
        snap = ref.get()
        return _aware((snap.to_dict() or {}).get("heard_at")) if snap.exists else None

    @staticmethod
    def _state(doc: Mapping[str, Any]) -> Any:
        return (doc.get("body") or {}).get("state")

    def _closed(self, card_id: str, doc: Mapping[str, Any], newest: str | None,
                cards: list[tuple[str, dict[str, Any]]], heard_at: datetime | None,
                now: datetime) -> bool:
        """Whether a proposed offer is no longer open: replaced, timed out, or
        outlived by the next turn's output."""
        if card_id != newest:
            return True
        made = _aware(doc.get("created_at"))
        if made is None or now >= made + timedelta(seconds=OFFER_SECONDS):
            return True
        if heard_at is None or heard_at <= made:
            return False
        # A newer offer replaces this one on write; any other planner card is output.
        return any(other.get("kind") != "navigate"
                   and (other.get("body") or {}).get("created_by") == "planner"
                   and (_aware(other.get("updated_at")) or made) > heard_at
                   for other_id, other in cards)

    def settle(self, where: Where) -> str | None:
        """Dismiss every offer of the session that is no longer open; the id
        of the one that is, or None."""
        cards = self._session_cards(where)
        offers = sorted(((cid, doc) for cid, doc in cards
                         if doc.get("kind") == "navigate" and self._state(doc) == vcards.PROPOSED),
                        key=lambda item: (_aware(item[1].get("created_at")) or _EPOCH, item[0]))
        if not offers:
            return None
        newest = offers[-1][0]
        heard_at = self._heard_at(where)
        now = self._now()
        open_id = None
        for cid, doc in offers:
            if self._closed(cid, doc, newest, cards, heard_at, now):
                self._move(where, cid, vcards.DISMISSED)
            else:
                open_id = cid
        return open_id

    def _move(self, where: Where, card_id: str, to: str) -> dict[str, Any] | None:
        """Move one of the session's proposed offers to `to` in one
        transaction, then append its event; None when it is not proposed."""
        ref = self._cards_ref(where).document(card_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if (doc.get("kind") != "navigate" or self._state(doc) != vcards.PROPOSED
                    or doc.get("tenant_id") != where.tenant_id
                    or doc.get("person") != where.person
                    or doc.get("session_id") != where.session_id):
                return None
            stored = dict(doc.get("body") or {})
            body = dict(stored.get("body") or {})
            if to == vcards.ACCEPTED:
                body["accepted_by"] = "person"
            stored = {**stored, "state": to, "body": body}
            # The catalogue still judges the card; only the writer's rules
            # (cards.writer_rules) are not a person's.
            vcards.validate({**stored, "card_id": card_id, "kind": "navigate"})
            doc = {**doc, "body": stored, "version": int(doc.get("version") or 0) + 1,
                   "updated_at": self._now()}
            txn.set(ref, doc)
            return doc

        doc = _apply(self._db.transaction())
        if doc is None:
            return None
        view_ = vcards.card_view(card_id, doc)
        vstore.VoiceStore(self._db, now=self._now).append_event(
            tenant_id=where.tenant_id, person=where.person, project_id=where.project_id,
            session_id=where.session_id, event={"type": vcards.CARD_UPSERT, "card": view_})
        log.info("voice session %s: navigation offer %s is %s", where.session_id, card_id, to)
        return view_

    def answer(self, where: Where, *, accept: bool) -> dict[str, Any] | None:
        """A spoken yes or no: the newest open offer accepted or dismissed, as
        its card; None when no offer is open."""
        open_id = self.settle(where)
        if open_id is None:
            return None
        return self._move(where, open_id, vcards.ACCEPTED if accept else vcards.DISMISSED)

    def accept(self, where: Where, card_id: str) -> dict[str, Any]:
        """The person's click on Open: `card_id` must be the session's open
        offer. NotFound when no navigate card of this session has that id;
        a 409 (the established `conflict`, never a new refusal code) when it
        was answered, replaced, outlived or timed out."""
        snap = self._cards_ref(where).document(card_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
        if (doc.get("kind") != "navigate" or doc.get("tenant_id") != where.tenant_id
                or doc.get("person") != where.person
                or doc.get("session_id") != where.session_id):
            raise NotFound(f"voice card {card_id!r} not found")
        open_id = self.settle(where)
        moved = self._move(where, card_id, vcards.ACCEPTED) if open_id == card_id else None
        if moved is None:
            raise Conflict(f"voice card {card_id!r} is not an open offer; nothing was opened")
        return moved
