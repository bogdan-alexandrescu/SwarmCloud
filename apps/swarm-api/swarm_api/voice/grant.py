"""The Voice it session grant: how swarm-voice acts as the session's person
(docs/design/voice-it.md §3.6, §10 phase 2).

A session's grant is created at start (`sessions.VoiceSessions.start`): a
random 256-bit value whose SHA-256 alone is stored on the session document,
handed to swarm-voice in the internal start call and nowhere else. swarm-voice
presents it on every call it makes to swarm-api:

    the bearer:             swarm-voice's Google ID token, audience swarm-api
    X-Swarm-Voice-Session:  <project_id>/<session_id>
    X-Swarm-Voice-Grant:    <the grant>

A request carrying `X-Swarm-Voice-Grant` is judged by this module and never
by the ordinary person path (`deps.current_auth`). It is accepted only when,
in this order:

  1. the bearer verifies, through the same verifier and audience as every
     caller, as swarm-voice's own service account (`voice_account`), with a
     verified email: anyone else, a person included, is 401;
  2. the route is on `ALLOWED_ROUTES` or `WRITE_ROUTES`: anything else is
     403, before any document is read;
  3. the session document named by `X-Swarm-Voice-Session` exists, is `live`,
     still holds a grant hash, and that hash equals the presented grant's
     (`hmac.compare_digest`): anything else -- an ended session, a wrong or
     revoked grant, a session that does not exist -- is the same 401.

The request then runs as the session's person, in the session's workspace,
through the same authenticator a click goes through
(`Authenticator.authenticate_voice_grant`): their memberships, roles, holds,
refusals and tenant scope. It acts with none of the person's platform-admin
powers, and a workspace the person reaches only as a platform admin is
refused (403): Voice it never reaches admin routes, and every row the request
writes then carries `via` (below), which the platform-admin audit row does
not. Before the route runs, one `voice_audit` row records the request with
`via: voice-it` and the session id (`audit_row`); a row that cannot be written
is 503 and the route never runs.

THE ALLOWLIST is the planner's read routes (§3.3, §4): reads only. A test
walks swarm-api's whole route table and fails when a route off this list is
reachable with a grant, when this list names a route that is not a GET, or
when it names a route that takes a typed confirmation or a secret value. Never
on it (§3.6): typed confirmations, secret values, IAM, admin and platform
routes, the merge switch, budget raises, approving a hold, destroy and purge.
Phase three's acting routes join it in their own lane, with their audit rows
stamped from `AuthContext.via`.

THE WRITE LIST began as one route, apart from the reads so the read list stays reads
only: `POST /v1/voice/sessions/{session_id}/cards`, the card write path the
planner draws through (§3.5, §4; routes/voice.py). The route writes only to
the grant's own session (any other session id is 403), takes the tenant from
that session, and validates the card against the catalogue, which has no
field that can hold a credential value. A test walks the route table and
fails when any other write is reachable with a grant.

THE WRITE LIST ALSO HOLDS what voice-it-own-speech.md adds (§7.3, §8.3):
`POST /v1/voice/sessions/{session_id}/readbacks/answer`, where swarm-voice
posts each final utterance GPT-Live heard so the read-back gate can tell a
"go" before the planner does (routes/voice_readback.py), and the visual and
note writes the planner draws through (§8.3). Each is classed as heard in
`voice/readback.py`, and each writes only to the grant's own session.

THE WRITE LIST ALSO HOLDS the planner's one act route (voice-it.md §3.9; the owner's
pick, 6 November 2026): `POST /v1/tasks/{task_id}/answers`, the answer to an
agent's question, sent by swarm-voice's `answer_question` act tool with
`via: voice-it` after a read-back the person confirmed. Who may answer is the
route's rule, unchanged: the task's submitter or an admin, never a viewer
(`role_member_required`), and a grant carries none of the person's
platform-admin powers. It runs as heard (`readback.AS_HEARD_ROUTES`).

THE READ-BACK GATE (voice-it-own-speech.md §7.2). An act route that cancels,
merges, deploys, deletes or approves (`readback.GATED_ROUTES`) does not run on
a grant alone: `gate` holds it as a pending read-back and answers 202
`readback_pending` with its id, and runs it only when it is re-sent with
`X-Swarm-Voice-Readback: <id>` naming a confirmed, unused read-back of this
session whose method, route and body hash match. The route declares
`routes.voice_readback.readback_gate`, which reads the body and calls `gate`;
tests/unit/voice/test_readback.py fails when a gated route on these lists
does not, and when a non-GET route on them is unclassed.

THE WORK FEED (voice-it.md §3.12.5). Every granted call writes one masked
`work.step` event on its session's stream, right after its audit row
(`write_audit`): the route's phrase, the method, the call's class and the ids
its path names (`workfeed.step_event`), never a query, a body or any text. A
step that cannot be written is logged by class name and the route still runs:
the audit row is the gate, the step is what the working card shows.

A QUIET READ writes its audit row and no step: a GET swarm-voice's roster
follower sends with `X-Swarm-Voice-Quiet: roster` (`QUIET_HEADER`), its tick's
tag query and started-id reads every 15 seconds (voice-it.md §3.12.6), which
would otherwise fill the working card with 1 + N steps a tick while nothing
changed (the owner's pick, 12 November 2026). The roster's own event is
`roster.update`, written through the work route. Only a GET is quiet: a write
always writes its step, and the header changes nothing else.

`record_work` is swarm-voice's write for a delegation's lifecycle (`started`,
`progress`, then `done`, `failed` or `refused`) and the roster's changed rows,
under the same grant: it writes only to the grant's own session (anything
else is 403), keeps the delegations still open on the session document
(`work_open`, oldest first, and the last `WORK_CLOSED_KEPT` ended ones in
`work_closed`) and refuses a state out of order (409), so the frames a
delegation writes are always in lifecycle order. `resolve` hands the oldest
open delegation to the step, from the document it already reads.

THE GRANT IS A CREDENTIAL. Nothing here logs, raises, returns or stores it:
messages name the session id at most, and the stored value is its hash.
`deps.current_auth` adds it to the values the error pages scrub. It dies with
the session: `sessions.VoiceSessions._close` removes the hash, and a session
that is not `live` is refused whatever it holds.
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from google.cloud import firestore
from swarm_common.admission import _snapshot

from ..errors import (ApiError, Conflict, Forbidden, Unauthenticated, UpstreamUnavailable,
                      ValidationFailed)
from . import readback as vreadback
from . import sessions as vs
from . import store as vstore
from . import workfeed as vwork

log = logging.getLogger(__name__)

GRANT_HEADER = "X-Swarm-Voice-Grant"
SESSION_HEADER = "X-Swarm-Voice-Session"
#: A grant read that writes no work step (A QUIET READ above), and its one value.
QUIET_HEADER = "X-Swarm-Voice-Quiet"
QUIET_ROSTER = "roster"

#: What `AuthContext.via` and every audit row written under a grant carry.
VIA = "voice-it"

#: swarm-voice's service account id: terraform/modules/service_account_ids'
#: `voice_id`, created by terraform/bootstrap in the deployment's project.
VOICE_ACCOUNT_ID = "swarm-voice"

AUDIT_GRANT_USED = "voice_grant_used"

#: `token_urlsafe(32)` is 43 characters; anything else was not minted here.
_GRANT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SESSION_RE = re.compile(r"^(vp_[0-9a-f]{32})/(vs_[0-9a-f]{32})$")

#: The routes a grant reaches, as (method, route template): the planner's
#: reads (§3.3, §4) and nothing else. GETs only; see the module docstring.
ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/v1/tenants/me"),
    ("GET", "/v1/tasks"),
    ("GET", "/v1/tasks/open-questions"),
    ("GET", "/v1/tasks/{task_id}"),
    ("GET", "/v1/tasks/{task_id}/attempts"),
    ("GET", "/v1/tasks/{task_id}/events"),
    ("GET", "/v1/tasks/{task_id}/questions"),
    ("GET", "/v1/workflows"),
    ("GET", "/v1/workflows/{workflow_id}"),
    ("GET", "/v1/runs"),
    ("GET", "/v1/runs/{run_id}"),
    ("GET", "/v1/schedules"),
    ("GET", "/v1/schedules/{schedule_id}"),
    ("GET", "/v1/schedules/{schedule_id}/firings"),
    ("GET", "/v1/repositories"),
    ("GET", "/v1/repositories/{repo_id}"),
    ("GET", "/v1/repositories/{repo_id}/languages"),
    ("GET", "/v1/approvals"),
    ("GET", "/v1/approvals/{item_id}"),
    ("GET", "/v1/failure-findings"),
    ("GET", "/v1/failure-findings/{finding_id}"),
    ("GET", "/v1/runtimes"),
    ("GET", "/v1/voice/cards/schema"),
})


#: The one write a grant reaches: the planner's card write. Kept apart from
#: `ALLOWED_ROUTES` so that list stays GETs only; see the module docstring.
WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/voice/sessions/{session_id}/cards"),
    # voice-it-own-speech.md §7.3: GPT-Live's finals, for the read-back gate.
    ("POST", "/v1/voice/sessions/{session_id}/readbacks/answer"),
    # §8.3: the planner's visuals and spoken notes (routes/voice_visuals.py).
    ("POST", "/v1/voice/sessions/{session_id}/visuals"),
    ("POST", "/v1/voice/projects/{project_id}/visuals/{visual_id}/notes"),
    # voice-it.md §3.9: the planner's answer act tool (swarm_voice/tools_act.py),
    # the owner's pick of 6 November 2026. The route's own rule decides who answers.
    ("POST", "/v1/tasks/{task_id}/answers"),
    # Debug mode's trace (voice/trace.py, the owner, 10 November 2026): each
    # planner turn of a session started with debug on; the route refuses any
    # caller that did not come through a grant.
    ("POST", "/v1/voice/sessions/{session_id}/trace"),
    # llm-providers.md §5.3: a spent Live key's next tier, held for the
    # reopen (routes/voice.py `tier_reopen`); the route refuses any caller
    # that did not come through a grant.
    ("POST", "/v1/voice/sessions/{session_id}/reopen"),
    # voice-it.md §3.12.5: a delegation's lifecycle and the roster's changed
    # rows (`record_work`, routes/voice.py `work`); the route refuses any
    # caller that did not come through a grant, and any other session.
    vwork.WORK_ROUTE,
})


class GrantRefused(Unauthenticated):
    """401: not swarm-voice, or no live session holds this grant. One
    message per cause, none of which says whether the session exists."""


class RouteNotGranted(Forbidden):
    """403: the route is on neither `ALLOWED_ROUTES` nor `WRITE_ROUTES`, or the grant would act
    somewhere its session does not."""


class ReadbackHeld(ApiError):
    """202: a gated call was held as a read-back and did not run; the
    detail names the read-back, what it will do and to what."""

    status_code = 202
    code = "readback_pending"


class GrantAuditUnavailable(UpstreamUnavailable):
    """503: the audit row could not be written, so the route does not run."""


@dataclass(frozen=True)
class GrantSession:
    """The live session a grant resolved to. Holds no grant."""

    tenant_id: str
    person: str
    project_id: str
    session_id: str
    #: The oldest delegation the session holds open (`work_open`), or None.
    delegation_id: str | None = None


#: The ended delegation ids a session keeps, so a late report of one is 409.
WORK_CLOSED_KEPT = 50
#: The delegations a session may hold open at once: the fast front runs one
#: and holds two waiting; GPT-Live's are as many as it asks for in a turn.
WORK_OPEN_MAX = 8


class WorkOutOfOrder(Conflict):
    """409: a delegation state that does not follow the one before it."""


def voice_account(settings: Any) -> str:
    """swarm-voice's service account email in this deployment's project."""
    project = str(getattr(settings, "project_id", "") or "").strip()
    return f"{VOICE_ACCOUNT_ID}@{project}.iam.gserviceaccount.com" if project else ""


def check_caller(claims: Mapping[str, Any], settings: Any) -> None:
    """`GrantRefused` unless the verified claims are swarm-voice's own, for
    swarm-api's audience. The verifier already pinned the audience outside
    local development; it is checked again here so that a grant is never
    accepted on a token minted for anything else, wherever it verified."""
    expected = voice_account(settings)
    email = str(claims.get("email", ""))
    if not expected or email.lower() != expected or email != email.strip():
        raise GrantRefused("a Voice it grant is accepted only from swarm-voice's identity")
    if claims.get("email_verified") is not True:
        raise GrantRefused("a Voice it grant needs a verified identity")
    audience = str(getattr(getattr(settings, "core", None), "api_audience", "") or "")
    if audience and claims.get("aud") != audience:
        raise GrantRefused("a Voice it grant needs an identity token for swarm-api")


def check_route(route: tuple[str, str] | None) -> None:
    if route is None or (route not in ALLOWED_ROUTES and route not in WRITE_ROUTES):
        raise RouteNotGranted("this route is not open to a Voice it session grant")


def resolve(db: Any, *, session_header: str | None, grant: str | None) -> GrantSession:
    """The live session `grant` belongs to; `GrantRefused` otherwise."""
    match = _SESSION_RE.match((session_header or "").strip())
    if match is None or not grant or not _GRANT_RE.match(grant):
        raise GrantRefused("the Voice it grant is not valid")
    project_id, session_id = match.group(1), match.group(2)
    ref = db.collection(vs.PROJECTS).document(project_id) \
        .collection(vs.SESSIONS).document(session_id)
    snap = _snapshot(ref.get())
    data = (snap.to_dict() or {}) if snap.exists else {}
    stored = data.get("grant_hash")
    # Hash first and compare in constant time whatever was found, so a
    # missing session and a wrong grant take the same path.
    presented = vs.grant_hash(grant)
    matches = hmac.compare_digest(
        str(stored).encode("ascii", "replace") if isinstance(stored, str) else b"",
        presented.encode("ascii"))
    if not matches or not isinstance(stored, str) or data.get("state") != vs.LIVE:
        raise GrantRefused("the Voice it grant is not valid")
    tenant_id = str(data.get("tenant_id") or "")
    person = str(data.get("person") or "").strip().lower()
    if not tenant_id or not person or data.get("project_id", project_id) != project_id:
        raise GrantRefused("the Voice it grant is not valid")
    open_ids = data.get("work_open")
    oldest = open_ids[0] if isinstance(open_ids, list) and open_ids else None
    return GrantSession(tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id,
                        delegation_id=vwork.clean_id(oldest))


def audit_row(session: GrantSession, *, method: str, route: str,
              path_params: Mapping[str, Any], at: datetime,
              quiet: str | None = None) -> dict[str, Any]:
    """The `voice_audit` row a grant request writes before its route runs;
    `quiet` is the request's `QUIET_HEADER`, honoured on a GET only."""
    return {
        "action": AUDIT_GRANT_USED, "via": VIA, "session_id": session.session_id,
        "project_id": session.project_id, "tenant_id": session.tenant_id,
        "by": session.person, "at": at, "method": method, "route": route,
        "path_params": {str(k): str(v) for k, v in path_params.items()},
        "delegation_id": session.delegation_id,
        "quiet": method.upper() == "GET" and quiet == QUIET_ROSTER,
    }


def write_audit(db: Any, row: dict[str, Any]) -> None:
    """The audit row, then the call's masked work step (THE WORK FEED),
    none for a quiet read (A QUIET READ)."""
    try:
        db.collection(vs.AUDIT).document().set(row)
    except Exception as exc:  # noqa: BLE001 - any failure means no audit, so no route
        raise GrantAuditUnavailable(
            f"the Voice it audit row could not be written ({type(exc).__name__}); "
            "nothing was done") from None
    if not row.get("quiet"):
        write_step(db, row)


def write_step(db: Any, row: Mapping[str, Any]) -> int | None:
    """One `work.step` for the granted call `row` audits; its sequence, or
    None when there is none (the work route, whose event is the lifecycle)
    or it could not be written (logged by class name; the route still runs)."""
    method, route = str(row.get("method") or ""), str(row.get("route") or "")
    if (method, route) == vwork.WORK_ROUTE:
        return None
    event = vwork.step_event(
        method=method, route=route, path_params=row.get("path_params") or {},
        write=(method, route) in WRITE_ROUTES, delegation_id=row.get("delegation_id"))
    at = row.get("at")
    try:
        return vstore.VoiceStore(db, now=lambda: at).append_event(
            tenant_id=str(row.get("tenant_id") or ""), person=str(row.get("by") or ""),
            project_id=str(row.get("project_id") or ""),
            session_id=str(row.get("session_id") or ""), event=event)
    except Exception as exc:  # noqa: BLE001 - the step is what is shown, never the gate
        log.warning("voice work step not written (%s) session=%s on %s %s",
                    type(exc).__name__, row.get("session_id"), method, route)
        return None


def _next_work(data: Mapping[str, Any], delegation_id: str, state: str) -> dict[str, Any]:
    """The session fields after `delegation_id` moves to `state`, or
    `WorkOutOfOrder`: `started` once, `progress` and an end only while open."""
    open_ids = [i for i in (data.get("work_open") or []) if isinstance(i, str)]
    closed = [i for i in (data.get("work_closed") or []) if isinstance(i, str)]
    if state == vwork.STARTED:
        if delegation_id in open_ids or delegation_id in closed:
            raise WorkOutOfOrder("this delegation has already started",
                                 detail={"field": "state"})
        if len(open_ids) >= WORK_OPEN_MAX:
            raise WorkOutOfOrder(f"a session holds at most {WORK_OPEN_MAX} open delegations",
                                 detail={"field": "delegation_id"})
        return {"work_open": [*open_ids, delegation_id]}
    if delegation_id not in open_ids:
        raise WorkOutOfOrder(f"a delegation that is not open cannot be {state}",
                             detail={"field": "state"})
    if state == vwork.PROGRESS:
        return {}
    return {"work_open": [i for i in open_ids if i != delegation_id],
            "work_closed": [*closed, delegation_id][-WORK_CLOSED_KEPT:]}


def record_work(db: Any, session: GrantSession, *, session_id: str, body: Any,
                at: datetime) -> dict[str, Any]:
    """swarm-voice's report through the grant (THE WORK FEED): `{delegation:
    {delegation_id, state}}` writes one `work.delegation` event, `{roster:
    [rows]}` one `roster.update`. Only for the grant's own session (403), on
    a session still live and still this person's in this tenant (the same
    401 as `resolve`), in lifecycle order (409). The event's sequence."""
    if session_id != session.session_id:
        raise RouteNotGranted("a Voice it grant writes only to its own session")
    if not isinstance(body, dict) or len(body) != 1 or not set(body) <= {"delegation", "roster"}:
        raise ValidationFailed("work is {delegation} or {roster}", detail={"field": "work"})
    if "delegation" in body:
        event = vwork.delegation_event(body["delegation"])
    else:
        event = vwork.roster_event(body["roster"])
    ref = db.collection(vs.PROJECTS).document(session.project_id) \
        .collection(vs.SESSIONS).document(session.session_id)

    @firestore.transactional
    def _apply(txn: Any) -> int:
        snap = _snapshot(txn.get(ref))
        data = (snap.to_dict() or {}) if snap.exists else {}
        if data.get("state") != vs.LIVE or data.get("tenant_id") != session.tenant_id \
                or str(data.get("person") or "").strip().lower() != session.person:
            raise GrantRefused("the Voice it grant is not valid")
        fields: dict[str, Any] = {}
        if event["type"] == vwork.WORK_DELEGATION:
            fields = _next_work(data, event["delegation_id"], event["state"])
        seq = int(data.get("event_seq") or 0) + 1
        txn.update(ref, {**fields, "event_seq": seq})
        txn.set(ref.collection(vstore.EVENTS).document(f"{seq:012d}"), {
            **event, "seq": seq, "tenant_id": session.tenant_id,
            "person": data.get("person"), "at": at})
        return seq

    seq = _apply(db.transaction())
    return {"session_id": session.session_id, "seq": seq, "type": event["type"]}


def where_of(session: GrantSession) -> vreadback.Where:
    return vreadback.Where(tenant_id=session.tenant_id, person=session.person,
                           project_id=session.project_id, session_id=session.session_id)


def session_of(session_header: str | None, *, tenant_id: str, person: str) -> GrantSession:
    """The session a grant request named, after `resolve` accepted it on the
    same request (`deps.current_auth`)."""
    match = _SESSION_RE.match((session_header or "").strip())
    if match is None:
        raise GrantRefused("the Voice it grant is not valid")
    return GrantSession(tenant_id=tenant_id, person=person.strip().lower(),
                        project_id=match.group(1), session_id=match.group(2))


def gate(db: Any, readbacks: vreadback.Readbacks, session: GrantSession, *, method: str,
         route: str, path_params: Mapping[str, Any], body: bytes | None,
         presented: str | None) -> dict[str, Any] | None:
    """The read-back gate (voice-it-own-speech.md §7.2) for a grant request.

    An act that runs as heard, or a read, passes (None). A gated act passes
    only with `presented` naming its confirmed read-back, which is then used
    (returned); without one it is held and `ReadbackHeld` (202) is raised,
    so the route never runs. A wrong, used or mismatched id is 409 and
    nothing runs."""
    verb = vreadback.classify(method, route)
    if verb is None or verb == vreadback.AS_HEARD:
        return None
    where = where_of(session)
    if presented is not None:
        return readbacks.consume(where, presented, method=method, route=route, body=body)
    target = vreadback.target_name(db, tenant_id=session.tenant_id, method=method,
                                   route=route, path_params=path_params)
    held = readbacks.hold(where, action=verb, target=target, method=method, route=route,
                          body=body)
    raise ReadbackHeld(
        f"Will {verb} {target}: held for a spoken go",
        detail={"readback_id": held["id"], "action": verb, "target_name": target,
                "expires_at": held["expires_at"].isoformat()})
