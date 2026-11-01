"""Voice it sessions: the project and session documents, and swarm-voice's
internal start and end calls (docs/design/voice-it.md §3.6, §3.7, §10 phase 1).

THE DOCUMENTS (§3.7). Everything is under the workspace the console has
selected, and every read also requires the caller to be the document's person:

  voice_projects/{project}                      tenant_id, person, title, status
  voice_projects/{project}/sessions/{session}   tenant_id, person, state, grant_hash
  voice_live/{marker}                           the person's one live session
  voice_audit/{row}                             one row per start and end

A talk with no project names gets a new untitled project. Another tenant's
project or session, or another person's, is the same 404 as a missing one
(invariant 9): nothing in a refusal says the document exists.

ENDING BY SESSION ID. The Talk pane ends a session by its id alone, so `end`
finds the project: it lists the projects whose `tenant_id` and `person` are
the caller's (two equality filters, served by the single-field indexes) and
reads that session id under each. A session under anyone else's project is
never read, so it is the same 404 as a missing one.

ONE LIVE SESSION PER PERSON (§3.7). `voice_live/{marker}` names the person's
live session and is read and written in the same transaction as the session
it names, so two starts racing each other cannot both pass: the second is 409
`conflict`. The marker's id is an HMAC of the email under the
deployment's settings key when one is configured, so no worker can compute
it; a session left `starting` by a crash longer than STARTING_STALE_SECONDS
ago no longer holds it.

THE GRANT (§3.6). Each session gets a random 256-bit grant. Only its SHA-256
is stored, on the session document; the grant itself goes to swarm-voice in
the start call and nowhere else, and is never logged or returned to the
browser. The grant is not accepted by any route yet: the grant lane
(§10 phase 2, `swarm_api/voice/grant.py`) adds that check.

SWARM-VOICE'S INTERNAL CALLS. swarm-api POSTs, with a Google ID token for
`voice_audience` (else the URL) as its bearer:

  {SWARM_VOICE_URL}/internal/sessions
      {"session_id", "project_id", "tenant_id", "person", "offer", "grant"}
      -> 200 {"answer": "<SDP>"}
  {SWARM_VOICE_URL}/internal/sessions/{session_id}/end
      {"session_id", "cause"} -> 200

A start swarm-voice refuses or does not answer ends the session at once with
the cause `voice_unreachable` and frees the person's marker, so the next try
is not a 409. An end swarm-voice does not hear is logged and the session
still ends: what stops OpenAI's billing then is swarm-voice's own idle stop
(§3.10).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import ApiError, Conflict, Forbidden, NotFound, UpstreamUnavailable

log = logging.getLogger(__name__)

PROJECTS = "voice_projects"
SESSIONS = "sessions"
LIVE_MARKERS = "voice_live"
AUDIT = "voice_audit"

#: A new talk's project title until the planner names it (§3.11).
UNTITLED = "Untitled"

STARTING = "starting"
LIVE = "live"
ENDED = "ended"
#: The states that hold the person's one live session.
HOLDING = frozenset({STARTING, LIVE})

#: A session still `starting` this long after it was created was left by a
#: crash between the start transaction and swarm-voice's answer, and no
#: longer blocks the person's next start. swarm-voice's start call times out
#: well inside it.
STARTING_STALE_SECONDS = 120

END_BY_PERSON = "ended_by_person"
END_VOICE_UNREACHABLE = "voice_unreachable"

AUDIT_STARTED = "voice_session_started"
AUDIT_ENDED = "voice_session_ended"

#: An SDP offer or answer larger than this is not one a browser made.
MAX_SDP_BYTES = 64 * 1024

START_TIMEOUT_SECONDS = 20.0
END_TIMEOUT_SECONDS = 5.0

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

_MARKER_PURPOSE = "voice-live:"


#: The two refusals keep the established codes, `forbidden` and `conflict`
#: (tests/unit/control_plane/test_new_refusals_ship_off.py): a new code would
#: have to ship behind a switch, and a switch that lets a stranger start a
#: session, or a person hold two, is the breach itself.
class VoiceNotAllowed(Forbidden):
    pass


class VoiceUnavailable(UpstreamUnavailable):
    code = "voice_unavailable"


class VoiceSessionLive(Conflict):
    pass


class VoiceUnreachable(ApiError):
    status_code = 502
    code = "voice_unreachable"


def not_allowed() -> VoiceNotAllowed:
    return VoiceNotAllowed(
        "Voice it is open only to the people this deployment names "
        "(VOICE_IT_PEOPLE, else the platform owner). Nothing was started.")


def unavailable() -> VoiceUnavailable:
    return VoiceUnavailable(
        "Voice it is unavailable: swarm-voice is not deployed here "
        "(SWARM_VOICE_URL is unset). Nothing was started.")


def is_allowed(people: tuple[str, ...], email: str) -> bool:
    who = (email or "").strip().lower()
    return bool(who) and who in {p.strip().lower() for p in people if p.strip()}


def grant_hash(grant: str) -> str:
    return hashlib.sha256(grant.encode("ascii")).hexdigest()


def marker_id(key: str, email: str) -> str:
    """`vl_` + 32 hex: an HMAC of the email under the settings key, or its
    SHA-256 where none is configured (a test, a local run)."""
    message = (_MARKER_PURPOSE + email.strip().lower()).encode("utf-8")
    if key:
        digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(message).hexdigest()
    return "vl_" + digest[:32]


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# swarm-voice
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StartCall:
    session_id: str
    project_id: str
    tenant_id: str
    person: str
    offer: str
    #: Excluded from the repr: it is the session's credential.
    grant: str

    def __repr__(self) -> str:
        return (f"StartCall(session_id={self.session_id!r}, project_id={self.project_id!r}, "
                f"tenant_id={self.tenant_id!r})")


class VoiceClient(Protocol):
    def start(self, call: StartCall) -> str:
        """swarm-voice's SDP answer, or `VoiceUnreachable`."""
        ...

    def end(self, session_id: str, *, cause: str) -> bool:
        """True when swarm-voice took the end. Never raises."""
        ...


def _metadata_id_token(audience: str, timeout: float) -> str:
    url = f"{METADATA_IDENTITY_URL}?{urllib.parse.urlencode({'audience': audience, 'format': 'full'})}"
    request = urllib.request.Request(url, method="GET", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the metadata server
        token = response.read().decode("ascii").strip()
    if not token:
        raise OSError("the metadata server returned no identity token")
    return token


class HttpVoiceClient:
    """swarm-voice's internal routes over HTTP. What is logged is the session
    id and a status, never a body: the start call carries the grant and the
    browser's offer."""

    def __init__(self, url: str, *, audience: str = "",
                 token: Callable[[str, float], str] = _metadata_id_token) -> None:
        self._base = url.strip().rstrip("/")
        self._audience = audience.strip() or self._base
        self._token = token

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[int, bytes]:
        bearer = self._token(self._audience, timeout)
        request = urllib.request.Request(
            self._base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - our own internal URL
                return response.status, response.read(MAX_SDP_BYTES + 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    def start(self, call: StartCall) -> str:
        body = {"session_id": call.session_id, "project_id": call.project_id,
                "tenant_id": call.tenant_id, "person": call.person,
                "offer": call.offer, "grant": call.grant}
        try:
            status, raw = self._post("/internal/sessions", body, START_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice start for session=%s did not reach swarm-voice (%s)",
                        call.session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached; the session was ended.") from None
        if status != 200:
            log.warning("swarm-voice refused the start for session=%s (HTTP %s)", call.session_id, status)
            raise VoiceUnreachable(f"swarm-voice refused the session (HTTP {status}); it was ended.")
        try:
            answer = json.loads(raw.decode("utf-8")).get("answer")
        except (ValueError, AttributeError):
            answer = None
        if not isinstance(answer, str):
            raise VoiceUnreachable("swarm-voice answered without an SDP answer; the session was ended.")
        return answer

    def end(self, session_id: str, *, cause: str) -> bool:
        path = "/internal/sessions/" + urllib.parse.quote(session_id, safe="") + "/end"
        try:
            status, _ = self._post(path, {"session_id": session_id, "cause": cause},
                                   END_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice end for session=%s did not reach swarm-voice (%s)",
                        session_id, type(exc).__name__)
            return False
        if status != 200:
            log.warning("swarm-voice refused the end for session=%s (HTTP %s)", session_id, status)
        return status == 200


def client_for(settings: Any) -> VoiceClient | None:
    """The deployment's client: none without SWARM_VOICE_URL."""
    url = str(getattr(settings, "voice_url", "") or "").strip()
    if not url:
        return None
    return HttpVoiceClient(url, audience=str(getattr(settings, "voice_audience", "") or ""))


# --------------------------------------------------------------------------
# The documents
# --------------------------------------------------------------------------


def _live_detail(marker: dict[str, Any] | None, tenant_id: str) -> dict[str, Any]:
    """Which session holds the person, so the console can end it; named only
    when it is in the tenant asking (invariant 9)."""
    if not marker or marker.get("tenant_id") != tenant_id:
        return {}
    return {"project_id": marker.get("project_id"), "session_id": marker.get("session_id")}


def _valid_answer(answer: str) -> bool:
    return answer.startswith("v=0") and len(answer.encode("utf-8")) <= MAX_SDP_BYTES


class VoiceSessions:
    """Starts and ends one person's sessions in one tenant."""

    def __init__(self, db: Any, client: VoiceClient, *, marker_key: str = "",
                 now: Callable[[], datetime]) -> None:
        self._db = db
        self._client = client
        self._marker_key = marker_key
        self._now = now

    def _project_ref(self, project_id: str) -> Any:
        return self._db.collection(PROJECTS).document(project_id)

    def _session_ref(self, project_id: str, session_id: str) -> Any:
        return self._project_ref(project_id).collection(SESSIONS).document(session_id)

    def _audit(self, txn: Any, *, action: str, tenant_id: str, person: str,
               project_id: str, session_id: str, detail: dict[str, Any]) -> None:
        txn.set(self._db.collection(AUDIT).document(), {
            "action": action, "tenant_id": tenant_id, "by": person, "at": self._now(),
            "project_id": project_id, "session_id": session_id, "detail": detail,
        })

    @staticmethod
    def _mine(data: dict[str, Any] | None, tenant_id: str, person: str) -> bool:
        return bool(data) and data.get("tenant_id") == tenant_id and data.get("person") == person

    def _holds(self, txn: Any, marker: dict[str, Any] | None) -> bool:
        """Whether the marker still names a live (or freshly starting) session."""
        if not marker or not marker.get("project_id") or not marker.get("session_id"):
            return False
        snap = _snapshot(txn.get(self._session_ref(marker["project_id"], marker["session_id"])))
        if not snap.exists:
            return False
        session = snap.to_dict() or {}
        state = session.get("state")
        if state == LIVE:
            return True
        if state == STARTING:
            created = _aware(session.get("created_at"))
            return created is None or \
                self._now() - created < timedelta(seconds=STARTING_STALE_SECONDS)
        return False

    def start(self, *, tenant_id: str, person: str, offer: str,
              project_id: str | None) -> dict[str, Any]:
        person = person.strip().lower()
        session_id = "vs_" + secrets.token_hex(16)
        new_project = project_id is None
        project_id = project_id or "vp_" + secrets.token_hex(16)
        grant = secrets.token_urlsafe(32)
        marker_ref = self._db.collection(LIVE_MARKERS).document(marker_id(self._marker_key, person))
        project_ref = self._project_ref(project_id)
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _open(txn: Any) -> None:
            marker_snap = _snapshot(txn.get(marker_ref))
            project_snap = None if new_project else _snapshot(txn.get(project_ref))
            if project_snap is not None and not (
                    project_snap.exists and self._mine(project_snap.to_dict(), tenant_id, person)):
                raise NotFound(f"voice project {project_id!r} not found")
            marker = marker_snap.to_dict() if marker_snap.exists else None
            if self._holds(txn, marker):
                raise VoiceSessionLive(
                    "You already have a live Voice it session; end it before starting another.",
                    detail=_live_detail(marker, tenant_id))
            now = self._now()
            if new_project:
                txn.set(project_ref, {
                    "tenant_id": tenant_id, "person": person, "title": UNTITLED,
                    "status": "active", "created_at": now, "updated_at": now})
            else:
                txn.update(project_ref, {"updated_at": now})
            txn.set(session_ref, {
                "tenant_id": tenant_id, "person": person, "project_id": project_id,
                "state": STARTING, "created_at": now, "grant_hash": grant_hash(grant)})
            txn.set(marker_ref, {
                "tenant_id": tenant_id, "project_id": project_id,
                "session_id": session_id, "at": now})
            self._audit(txn, action=AUDIT_STARTED, tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id,
                        detail={"new_project": new_project})

        _open(self._db.transaction())

        call = StartCall(session_id=session_id, project_id=project_id, tenant_id=tenant_id,
                         person=person, offer=offer, grant=grant)
        try:
            answer = self._client.start(call)
            if not isinstance(answer, str) or not _valid_answer(answer):
                raise VoiceUnreachable("swarm-voice's answer is not an SDP answer; the session was ended.")
        except VoiceUnreachable:
            self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                        session_id=session_id, cause=END_VOICE_UNREACHABLE)
            raise
        self._session_ref(project_id, session_id).update({"state": LIVE, "live_at": self._now()})
        log.info("voice session started tenant=%s project=%s session=%s new_project=%s",
                 tenant_id, project_id, session_id, new_project)
        return {"project_id": project_id, "session_id": session_id, "state": LIVE,
                "new_project": new_project, "answer_sdp": answer}

    def _close(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
               cause: str) -> dict[str, Any] | None:
        """Mark the session ended and free the person's marker when it names
        it; the session's data before, or None when it was already ended.
        `NotFound` when it is not this person's in this tenant."""
        session_ref = self._session_ref(project_id, session_id)
        marker_ref = self._db.collection(LIVE_MARKERS).document(marker_id(self._marker_key, person))

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(session_ref))
            marker_snap = _snapshot(txn.get(marker_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person):
                raise NotFound(f"voice session {session_id!r} not found")
            if data.get("state") == ENDED:
                return None
            now = self._now()
            txn.update(session_ref, {"state": ENDED, "ended_at": now, "end_cause": cause})
            marker = marker_snap.to_dict() if marker_snap.exists else None
            if marker and marker.get("session_id") == session_id \
                    and marker.get("project_id") == project_id:
                txn.delete(marker_ref)
            self._audit(txn, action=AUDIT_ENDED, tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id, detail={"cause": cause})
            return data

        return _apply(self._db.transaction())

    def _project_of(self, *, tenant_id: str, person: str, session_id: str) -> str:
        """The project of this person's session in this tenant; `NotFound`
        when no project of theirs holds it."""
        projects = (self._db.collection(PROJECTS)
                    .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("person", "==", person))
                    .stream())
        # get_all does not keep the order it was given: match by path.
        refs = {project.id: self._session_ref(project.id, session_id) for project in projects}
        owner = {ref.path: project_id for project_id, ref in refs.items()}
        for snap in (self._db.get_all(list(refs.values())) if refs else ()):
            if snap.exists and self._mine(snap.to_dict(), tenant_id, person):
                return owner[snap.reference.path]
        raise NotFound(f"voice session {session_id!r} not found")

    def end(self, *, tenant_id: str, person: str, session_id: str) -> dict[str, Any]:
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        before = self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                             session_id=session_id, cause=END_BY_PERSON)
        told = None
        if before is not None:
            told = self._client.end(session_id, cause=END_BY_PERSON)
            log.info("voice session ended tenant=%s project=%s session=%s voice_told=%s",
                     tenant_id, project_id, session_id, told)
        return {"project_id": project_id, "session_id": session_id, "state": ENDED,
                "changed": before is not None, "voice_told": told}
