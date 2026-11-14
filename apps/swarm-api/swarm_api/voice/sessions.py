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
ago no longer holds it. Nor does a `live` one with no activity (the newest of
`activity_at`, written on each turn, `heartbeat_at`, written when the Talk
tab's event stream connects, `live_at`, `created_at` and `fallback.at`) for
LIVE_STALE_SECONDS: its tab went away without the end call. The start ends
it in its own transaction (cause `abandoned`), and the speech sweep ends any
others (`sweep_abandoned`); swarm-voice is told the way an end tells it. A
session still live in another workspace is refused in words alone.

THE GRANT (§3.6). Each session gets a random 256-bit grant. Only its SHA-256
is stored, on the session document; the grant itself goes to swarm-voice in
the start call and nowhere else, and is never logged or returned to the
browser. `swarm_api/voice/grant.py` accepts it from swarm-voice on the
allowlisted routes while the session is live; ending a session removes the
hash, so the grant dies with it.

SWARM-VOICE'S INTERNAL CALLS. swarm-api POSTs, with a Google ID token for
`voice_audience` (else the URL) as its bearer:

  {SWARM_VOICE_URL}/internal/sessions
      {"voice_session_id", "project_id", "person", "offer", "grant"}
      -> 201 {"voice_session_id", "live_session_id", "answer": "<SDP>"}
  {SWARM_VOICE_URL}/internal/sessions/{session_id}/end
      {"session_id", "cause"} -> 200

The start body is swarm-voice's `StartRequest` (apps/swarm-voice/swarm_voice/
main.py) field for field (owner, 2026-11-02: "swarm-api follows swarm-voice").
An answer for another session id is read as a refusal; the live session's id
is kept on the session document.

THE ACCOUNT (owner, 2026-11-02: the ChatGPT plan account is reserved for
Voice it through the quota broker's "Voice it pool"). swarm-api sends no
credential: swarm-voice leases the pool account from the broker itself for
each session (`POST /v1/voice/lease`), so the start body is
{voice_session_id, person, offer, project_id, grant} and nothing here reads a
person's secret. swarm-voice's refusals of the pool reach the browser with
swarm-voice's own sentence, under the established codes:

  403 (the person is not in the Voice it pool)        -> 403 `forbidden`,
      detail.reason `voice_pool_refused`
  409 `voice_pool_refused` (no OpenAI API key)        -> 409 `conflict`,
      detail.reason `voice_pool_refused`
  409 `voice_pool_busy` on a Live start               -> 409 `conflict`,
      detail.reason `openai_key_in_use`: the key is another session's
  409 `voice_pool_busy` on a text start               -> 409 `conflict`,
      detail.reason `no_voice_account_available`: no plan account is free

The cause is read from the mode swarm-api asked for, never from swarm-voice's
sentence: a text start leases the Voice it pool's ChatGPT plan, never the
OpenAI key, so its refusal never says the key is in use (live QA, 9 November
2026: an own-speech fallback was told so).

Either way the session is ended at once (cause `voice_pool_refused`) and the
person's marker freed, so the next try is not a 409 of ours.

THE MODELS (the owner, 6 November 2026: "wire-in-follow-up", "front-reads-it").
Voice it › Settings is read once, before the start transaction, and both
start bodies (live and text) carry what it names:

  planner_model       the planner class's pin
                      (`VoiceSettings.session_planner_model` over swarm-api's
                      `worker_class_models`)
  conversation_model  the fast front's model: the curated entry's id
                      (`speech_catalogue.CONVERSATION_MODELS`)

Each is omitted when the setting is unset, or names an entry the catalogue no
longer lists or a class with no pin of its own, so swarm-voice keeps its pin
(VOICE_PLANNER_MODEL, VOICE_FRONT_MODEL). swarm-voice keeps both for the
session, so a change reaches the next session's start, never a running one.

DEBUG MODE (voice/settings.py, the owner, 10 November 2026) is read at the
same moment (`VoiceSettings.session_debug`); both start bodies carry
`debug: true` only while it is on, so with it off the body is unchanged and
swarm-voice traces nothing.

THE ENGINE (docs/design/voice-it-own-speech.md §4.1, §10; pick 7: "Start
uses exactly the engine in Settings"). The browser's Start names no engine and
no mode. `pick_engine` reads Voice it › Settings' `engine`:

  own        `speech`: the session opens, swarm-voice starts the fast Claude
             front (no offer, `mode: "speech"`), then the speech machine's
             lease is taken and the instance brought to RUNNING
             (speech_machine.SpeechMachine.start, swarm-api's four-minute
             wait). The OpenAI pool is never asked.
  gpt-live   `live`, today's GPT-Live path, when the broker's Voice it pool
             holds an OpenAI API key. With none (`GET /v1/voice/pool` answers
             `no_voice_account_registered`) the session is `text`, with
             `fallback: {from: "gpt-live", reason: "no_openai_key"}` (owner's
             pick, PR #3647: "text fallback"): no other account or person's
             key is tried for Live. A broker that cannot be asked keeps the
             live path, and swarm-voice's own lease then says why.

TEXT MODE IS THE FALLBACK, never an engine Settings choose (§10.1). A session
becomes text when its engine cannot start, with the engine and the reason as
a fixed code in `fallback: {from, reason}` on the session document and in the
start's answer. GPT-Live's one reason, `no_openai_key`, is known before
anything is written; own speech's are:

  speech_not_deployed   the deployment runs no speech machine (SWARM_SPEECH_*
                        empty), so no session is opened as speech at all
  front_refused         swarm-voice's 409 `voice_front_refused`: no Claude
                        account free for the front (a refusal, never a wait)
  no_l4                 no zone of SWARM_SPEECH_ZONES had an L4 free, each tried
                        once in order (owner, 2026-11-09)
  not_ready             the machine was not RUNNING inside the four minutes

The session is not re-opened: its document turns `mode: "text"`. After a
machine fallback (`no_l4`, `not_ready`) the front that started is KEPT and
takes the text turns, on the Claude account it already holds: nothing is
released and leased again, and no OpenAI key or ChatGPT plan is asked for
(live QA, 9 November 2026: ending the front and leasing the Voice it pool's
plan for a text start was refused, `no_voice_account_available`). A turn the
front delegates waits for the planner's line and adds it to the reply, as a
text page reads one reply per turn. Only `front_refused` and
`speech_not_deployed`, where no front holds anything, start swarm-voice's text
conversation on the same session id. A machine another
session holds is a refusal (409, `speech_machine_in_use`), not a fallback:
the session is ended and the person freed. While its machine starts, a
`speech` session holds the person for SPEECH_STARTING_STALE_SECONDS.

A text or speech start sends `{voice_session_id, project_id, person, grant,
mode}` with no offer; swarm-voice answers `201 {voice_session_id, mode}`. The
browser then posts each turn as text (a speech session's come from the audio
relay too):

  {SWARM_VOICE_URL}/internal/sessions/{session_id}/turns
      {"text"} -> 200 {"turn_id", "reply", "cards", "delegation_id"?}

`delegation_id` is set when the fast front handed the turn to the planner;
`VoiceSessions.delegation` reads the planner's spoken line from
`{SWARM_VOICE_URL}/delegations/{id}?wait=<seconds>`, only for that session.
Ending a speech session also stops its machine, under the lease's generation.

swarm-voice's 404 (it holds no text conversation for the session: ended
there, or lost to a restart) reads as 409 `conflict`, reason
`voice_session_not_live`; its 429 `usage_limit_reached` as 429
`rate_limited` with `detail.resets_at`. Neither the text nor the reply is
logged or stored here.

A start swarm-voice refuses or does not answer ends the session at once with
the cause `voice_unreachable` and frees the person's marker, so the next try
is not a 409. An end swarm-voice does not hear is logged and the session
still ends: what stops OpenAI's billing then is swarm-voice's own idle stop
(§3.10).

THE REOPEN (docs/design/llm-providers.md §5.3, the owner, 11 November 2026).
When a Live session's key is spent, swarm-voice holds the next tier and says
so on `POST /v1/voice/sessions/{id}/reopen` through the session grant
(routes/voice.py `tier_reopen`): `hold_reopen` records `reopen: {tier, at}` on
the session document and the route puts `voice.tier.reopen` ({tier}, a name of
REOPEN_TIERS and never a key) on the session's stream. A GPT-Live session is
bound to the browser's offer, so the console makes a fresh one and starts the
SAME session again (`{offer_sdp, session_id}`): `reopen` accepts it only for
the session's own person in this tenant, while it is live and a reopen is held
(at most REOPEN_HOLD_SECONDS old), and claims the hold in one transaction so
two starts cannot both pass. Anything else is 409 `conflict`, as a second
start always was. The offer goes to swarm-voice with no grant and no models:
swarm-voice's reopen keeps the spent session's grant, planner model and trace.
A reopen swarm-voice refuses ends the session (cause `reopen_failed`) and tells
swarm-voice, so the held tier and any spent Live session are closed.

THE PAUSE (docs/design/voice-it.md §3.12.2, §3.12.4, §3.12.7, §3.12.8). swarm-voice
closes a GPT-Live session's voice while delegated work runs and says so through
the session grant (routes/voice.py `/reopen`, `{state, cause}`, its
`session.state` event's names): `paused` writes `voice_state: paused`,
`paused_at` and `work_running: true` (it pauses only with a delegation in
flight); `waking` with cause `result` writes `work_running: false` and
`work_done_at`; `listening` closes the interval. A paused session stays
`live`: its grant still resolves, the person's marker still names it, and
swarm-voice keeps its Voice it pool lease. It holds no lease or pool slot of
the platform's (invariant 1): it is a voice session, not an agent.

The session document keeps

  voice_state     open | paused
  paused_at       the open pause's start; None while the voice is open
  pauses          the closed intervals, [{paused_at, woke_at}], the newest
                  PAUSES_KEPT; older ones fold into `paused_seconds_before`
  work_running    whether a delegation swarm-voice paused for is in flight
  seen_seq        the highest stream sequence the owner's main view has
                  shown (`seen`): only ever moved forward, never past
                  `event_seq`; `seen_at` is when it was last posted

THE BROWSER'S REOPEN (`reopen_paused`). The console posts a fresh offer and
the cause (REOPEN_CAUSES: `result`, `speech`, `resume`) for the session's own
person in this tenant; the offer goes to swarm-voice's
`POST /internal/sessions/{id}/reopen` with the project's brief and the
session's open cards' one-liners, read here for that person, as quoted data
(the owner's answer, 11 November 2026). An ended session is 409 and nothing is
forwarded. swarm-voice's answer closes the pause (`woke_at`); a refusal keeps
the session as it was, paused, so Resume can try again (§3.12.7: a failed
reopen keeps the session paused). The tier move keeps its own path (THE
REOPEN above).

PAUSED_IDLE. A paused session with no delegated work running and no activity
from its person (`paused_at`, `work_done_at`, `seen_at` and `activity_at`, the
newest of them) for PAUSED_IDLE_SECONDS ends as `paused_idle`, by the same
sweep and start that end an abandoned one, so swarm-voice is told the same
way. The heartbeat is not the person's activity: an open tab is not him. The
project, its cards, its starts and its brief are kept, as every end keeps them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import ApiError, Conflict, Forbidden, NotFound, RateLimited, UpstreamUnavailable
from . import speech_catalogue as cat
from . import workfeed as vwork

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
#: The same for a `speech` session, which stays `starting` while swarm-api
#: brings the speech machine to RUNNING: speech_machine.READY_SECONDS (240),
#: plus swarm-voice's start call (START_TIMEOUT_SECONDS) and a margin.
SPEECH_STARTING_STALE_SECONDS = 330
#: A `live` session with no activity this long was abandoned (its tab went
#: away without the end call) and no longer holds its person: the longest
#: idle stop Settings offer (swarm-voice has stopped its side by then), plus
#: a margin, so no idle stop a person can pick ever cuts a real conversation.
#: An open Talk tab beats on each event-stream connect, at most
#: stream.MAX_SECONDS (540) apart, well inside it.
LIVE_STALE_MARGIN_SECONDS = 900
LIVE_STALE_SECONDS = max(idle.seconds for idle in cat.IDLE) + LIVE_STALE_MARGIN_SECONDS
#: The session-document times that show a session is still in use; the
#: newest of them (and of `fallback.at`) is its last activity.
ACTIVITY_FIELDS = ("activity_at", "heartbeat_at", "live_at", "created_at")

END_BY_PERSON = "ended_by_person"
#: The holder's pagehide end (voice-it.md §3.12.2, "Closing the tab").
END_TAB_CLOSED = "tab_closed"
#: The causes the person's own end route may name; every other is swarm-api's own.
PERSON_END_CAUSES = (END_BY_PERSON, END_TAB_CLOSED)
#: Live with no activity for LIVE_STALE_SECONDS: ended by the next start or the sweep.
END_ABANDONED = "abandoned"
END_VOICE_UNREACHABLE = "voice_unreachable"
END_VOICE_POOL_REFUSED = "voice_pool_refused"
#: Another session holds the speech machine (speech_machine.MachineInUse).
END_MACHINE_IN_USE = "speech_machine_in_use"
#: A paused session idle this long with nothing delegated running (PAUSED_IDLE).
END_PAUSED_IDLE = "paused_idle"
#: The owner's answer (§3.12.2, question 10): 60 minutes paused with nothing
#: running and no activity from him ends the session.
PAUSED_IDLE_SECONDS = 60 * 60
#: The times on a paused session that count from (PAUSED_IDLE): the pause,
#: the delegated work's end, the owner's last seen post and his last turn.
PAUSED_IDLE_FIELDS = ("paused_at", "work_done_at", "seen_at", "activity_at")
#: The voice's states on a live session (THE PAUSE).
VOICE_OPEN = "open"
VOICE_PAUSED = "paused"
#: What swarm-voice's grant reports (its live.py `session.state` names).
VOICE_REPORTS = ("paused", "waking", "listening")
#: What wakes a paused voice from the browser (swarm_voice.live.WAKE_CAUSES).
REOPEN_CAUSES = ("result", "speech", "resume")
#: A session's stream events (store.EVENTS; store imports this module).
_EVENTS = "events"
#: The closed pause intervals kept on the document; a session that pauses
#: more often folds the oldest into `paused_seconds_before`, so the document
#: stays small whatever its length.
PAUSES_KEPT = 100
#: Card states that are still open (cards.PROPOSED, ACTING, WAITING), and the
#: longest one-liner a reopen carries (swarm_voice.main.REOPEN_CARD_MAX_CHARS).
OPEN_CARD_STATES = frozenset({"proposed", "acting", "waiting"})
REOPEN_CARD_MAX_CHARS = 600
REOPEN_CARDS_MAX = 200

#: A session's modes: GPT-Live over WebRTC on an OpenAI API key, or the ChatGPT
#: plan's text conversation (owner, 4 November 2026: "both paths").
MODE_LIVE = "live"
MODE_TEXT = "text"
#: Our own speech: the speech machine's ears and mouth, the fast Claude front's
#: conversation (docs/design/voice-it-own-speech.md §10.1).
MODE_SPEECH = "speech"
MODES = (MODE_LIVE, MODE_TEXT, MODE_SPEECH)
#: The modes whose turns are posted as text to swarm-voice's `/turns`.
TURN_MODES = frozenset({MODE_TEXT, MODE_SPEECH})
#: Voice it › Settings' engines (speech_catalogue.ENGINE_OWN / ENGINE_GPT_LIVE).
ENGINE_MODES = {cat.ENGINE_OWN: MODE_SPEECH, cat.ENGINE_GPT_LIVE: MODE_LIVE}
#: Why an own-speech session became text: fixed codes, written to the session's
#: `fallback.reason` and the start's answer (§4.1). `no_l4` and `not_ready` are
#: speech_machine's FALLBACK_NO_L4 and FALLBACK_NOT_READY.
FALLBACK_NOT_DEPLOYED = "speech_not_deployed"
FALLBACK_FRONT_REFUSED = "front_refused"
FALLBACK_NO_L4 = "no_l4"
FALLBACK_NOT_READY = "not_ready"
#: GPT-Live's: the Voice it pool holds no OpenAI API key, so the session is text.
FALLBACK_NO_OPENAI_KEY = "no_openai_key"
FALLBACK_REASONS = frozenset({FALLBACK_NOT_DEPLOYED, FALLBACK_FRONT_REFUSED, FALLBACK_NO_L4,
                              FALLBACK_NOT_READY, FALLBACK_NO_OPENAI_KEY})
#: The engine each fallback reason falls back from (`fallback.from`).
FALLBACK_FROM = {reason: cat.ENGINE_OWN for reason in FALLBACK_REASONS} | {
    FALLBACK_NO_OPENAI_KEY: cat.ENGINE_GPT_LIVE}
#: swarm-voice's 409 code for a speech start whose fast front could not lease
#: a Claude account (swarm_voice.main.FRONT_REFUSED).
FRONT_REFUSED_CODE = "voice_front_refused"
#: The broker's reason for a Voice it pool with no account that can open a Live
#: session, i.e. no OpenAI API key: the one answer that makes a session text.
NO_LIVE_ACCOUNT = "no_voice_account_registered"
#: A text turn, as the console sends it (swarm_voice.textchat.MAX_TURN_CHARS).
MAX_TURN_CHARS = 4000
TURN_TIMEOUT_SECONDS = 300.0
#: `detail.reason` of the 409 for a turn on a session that takes none.
NOT_LIVE = "voice_session_not_live"
USAGE_LIMIT = "usage_limit_reached"

#: swarm-voice's 409 codes for the Voice it pool, carried to the browser as
#: `detail.reason` (a 403 from swarm-voice is always `voice_pool_refused`).
POOL_BUSY = "voice_pool_busy"
POOL_REFUSED = "voice_pool_refused"
POOL_REASONS = frozenset({POOL_BUSY, POOL_REFUSED})
#: `detail.reason` of a busy pool, by what the start leased: a Live start's
#: OpenAI API key held by another session, or no ChatGPT plan account free for
#: a text start (the broker's own word for it).
OPENAI_KEY_IN_USE = "openai_key_in_use"
NO_PLAN_FREE = "no_voice_account_available"
KEY_IN_USE_SENTENCE = "Voice it's OpenAI API key is in use by another session; try again when it ends."
NO_PLAN_SENTENCE = "No Voice it plan account is free for a text session; try again in a few minutes."
#: What a start is told when the Voice it pool holds no OpenAI API key. Every
#: Voice it session is a GPT-Live session, which only a developer API key can
#: open (OpenAI refused the ChatGPT plan's token, 403 on /v1/live/sessions, 4
#: November 2026), so the broker refuses the lease BEFORE taking any hold and
#: swarm-voice answers 409 `voice_pool_refused`. That code means exactly this,
#: so the person reads where to add one, whatever older sentence swarm-voice
#: sent, and never a 502.
NO_API_KEY_SENTENCE = (
    "Voice it needs an OpenAI API key in the Voice it pool: add one on Admin \u203a Accounts")
#: A refusal sentence longer than this is cut: it is shown, not parsed.
MAX_REFUSAL_MESSAGE = 500
#: swarm-voice's 502 when OpenAI refused the Live session: it carries OpenAI's
#: status and its error's code and type, never the error's message.
OPENAI_REFUSED = "openai_refused"
#: The pattern swarm-voice holds OpenAI's code and type to (swarm_voice.live), held
#: again here: anything else reads as "unreadable".
OPENAI_ERROR_TOKEN = re.compile(r"[a-z0-9_.-]{1,64}")

AUDIT_STARTED = "voice_session_started"
AUDIT_ENDED = "voice_session_ended"

#: An SDP offer or answer larger than this is not one a browser made.
MAX_SDP_BYTES = 64 * 1024

#: How long a start waits for swarm-voice's answer; past it the person reads
#: "swarm-voice could not be reached". swarm-voice bounds its whole Live start
#: (lease, create, every sideband attach try, the lease's release on a failure)
#: by swarm_voice.live.START_WORST_SECONDS (17.0), held below this with a margin
#: for the hop by tests/unit/voice/test_live_sideband_events.py, so a refusal
#: swarm-voice has reaches the person as itself (10 November 2026: two 10 s attach
#: tries after the create outran this and the console said "could not be reached").
#: Raise both together, never this alone below swarm-voice's bound.
START_TIMEOUT_SECONDS = 20.0

#: The stream event swarm-voice's held next tier puts on the session's stream
#: (swarm_voice.live.REOPEN_EVENT).
REOPEN_EVENT = "voice.tier.reopen"
#: The tiers a reopen may name (swarm_voice.accountlease.TIERS): a plan, a key
#: shared with Voice it, the dedicated Voice it key. A name, never a key.
REOPEN_TIERS = ("plan", "api_key", "voice_dedicated")
#: How long a held reopen is accepted. swarm-voice keeps the next tier while
#: the spent Live session lasts and REOPEN_SECONDS (60 s) after it ends; twice
#: that bounds a hold whose session swarm-voice has long given back.
REOPEN_HOLD_SECONDS = 120
END_REOPEN_FAILED = "reopen_failed"
#: The longest a delegation is waited on: swarm-voice's
#: claudefront.MAX_WAIT_SECONDS, the planner's turn.
DELEGATION_WAIT_SECONDS = 240.0
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


class VoicePoolRefused(Forbidden):
    """swarm-voice's 403: the person is not in the Voice it pool. Keeps the
    established `forbidden` code; `detail.reason` is `voice_pool_refused`."""


class VoicePoolBusy(Conflict):
    """swarm-voice's 409: the Voice it pool account is busy or refused the
    lease. Keeps the established `conflict` code; `detail.reason` is the code
    swarm-voice sent (`voice_pool_busy` or `voice_pool_refused`)."""


#: Either pool refusal; `start` ends the session on both.
VoicePoolUnavailable = (VoicePoolRefused, VoicePoolBusy)


class VoiceSessionLive(Conflict):
    pass


class SpeechFrontRefused(Exception):
    """swarm-voice's 409 `voice_front_refused`: no Claude account for the fast
    front. Never reaches the browser: the session falls back to text."""


class VoiceUnreachable(ApiError):
    status_code = 502
    code = "voice_unreachable"


class VoiceSessionNotLive(Conflict):
    """A turn on a session that is not a live text conversation. Keeps the
    established `conflict` code; `detail.reason` is `voice_session_not_live`."""


class VoiceUsageLimited(RateLimited):
    """The ChatGPT plan's usage limit refused a turn. Keeps the established
    `rate_limited` code; `detail` adds `reason` and `resets_at`."""

    def __init__(self, resets_at: str | None, *, now: datetime) -> None:
        when = _instant(resets_at)
        wait = max(0.0, (when - now).total_seconds()) if when is not None and now.tzinfo else 0.0
        super().__init__(
            "Voice it's ChatGPT plan has reached its usage limit"
            + (f"; it resets at {when.isoformat()}." if when is not None else "; try again later."),
            retry_after_seconds=wait)
        self.detail.update({"reason": USAGE_LIMIT, "resets_at": when.isoformat() if when else None})


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def not_allowed() -> VoiceNotAllowed:
    return VoiceNotAllowed(
        "Voice it is open only to the people this deployment names "
        "(VOICE_IT_PEOPLE, else the platform owner). Nothing was started.")


def unavailable() -> VoiceUnavailable:
    return VoiceUnavailable(
        "Voice it is unavailable: swarm-voice is not deployed here "
        "(SWARM_VOICE_URL is unset). Nothing was started.")


def _refusal_body(raw: bytes) -> tuple[str, str]:
    """(code, message) from swarm-voice's refusal body: `{code, message}`, or
    FastAPI's `{detail: {code, message}}` / `{detail: "<sentence>"}`."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    if isinstance(body, dict) and "detail" in body and "message" not in body:
        body = body["detail"]
    if isinstance(body, str):
        return "", body.strip()[:MAX_REFUSAL_MESSAGE]
    if not isinstance(body, dict):
        return "", ""
    code = body.get("code") if isinstance(body.get("code"), str) else ""
    message = body.get("message") if isinstance(body.get("message"), str) else ""
    return code.strip(), message.strip()[:MAX_REFUSAL_MESSAGE]


def pool_refusal(status: int, raw: bytes, *, mode: str = MODE_LIVE) -> VoicePoolRefused | VoicePoolBusy | None:
    """The browser's refusal for swarm-voice's 403, or its 409 naming a pool
    code, carrying its sentence. None for any other answer (a 409 for the
    voice session already being live is not the pool's, and reads as the
    generic refusal). A busy pool says what was busy by `mode`, the start's:
    only a Live start leases the OpenAI API key."""
    code, message = _refusal_body(raw)
    if status == 403:
        return VoicePoolRefused(
            message or "You are not in the Voice it pool, so no ChatGPT account can be "
            "leased for you. Nothing was started.", detail={"reason": POOL_REFUSED})
    if status != 409 or code not in POOL_REASONS:
        return None
    reason = code
    if code == POOL_REFUSED:
        return VoicePoolBusy(NO_API_KEY_SENTENCE, detail={"reason": reason})
    if mode != MODE_LIVE:
        return VoicePoolBusy(NO_PLAN_SENTENCE, detail={"reason": NO_PLAN_FREE})
    return VoicePoolBusy(message or KEY_IN_USE_SENTENCE, detail={"reason": OPENAI_KEY_IN_USE})


def openai_refusal(raw: bytes) -> VoiceUnreachable | None:
    """The browser's refusal for swarm-voice's 502 `openai_refused`, saying what
    OpenAI answered: its status and its error's code and type. None for any other
    502 body."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        body = body["detail"]
    if not isinstance(body, dict) or body.get("code") != OPENAI_REFUSED:
        return None

    def kept(value: Any) -> str:
        return value if isinstance(value, str) and OPENAI_ERROR_TOKEN.fullmatch(value) else "unreadable"

    status = body.get("openai_status")
    status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
    code, kind = kept(body.get("openai_code")), kept(body.get("openai_type"))
    said = [str(status) if status is not None else "on the sideband", code]
    if kind not in ("unreadable", code):
        said.append(kind)
    return VoiceUnreachable(
        f"OpenAI refused the Live session for the Voice it account ({', '.join(said)}); "
        "the session was ended.",
        detail={"reason": OPENAI_REFUSED, "openai_status": status, "openai_code": code, "openai_type": kind})


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
    #: Kept for the caller's own record; swarm-voice's body has no tenant.
    tenant_id: str
    person: str
    #: The browser's SDP offer; empty for a text session, which has none.
    offer: str
    #: Excluded from the repr: it is the session's credential.
    grant: str
    mode: str = MODE_LIVE
    #: Voice it › Settings' models, read at the start; None leaves swarm-voice
    #: on its pin, and the field out of the body.
    planner_model: str | None = None
    conversation_model: str | None = None
    #: Voice it › Settings' debug mode, read at the start: swarm-voice traces
    #: each planner turn (voice/trace.py) only when it is True.
    debug: bool = False
    #: The second start of a Live session whose next tier is held: swarm-voice
    #: is sent the offer alone and keeps the spent session's grant and models.
    reopen: bool = False

    def __repr__(self) -> str:
        return (f"StartCall(session_id={self.session_id!r}, project_id={self.project_id!r}, "
                f"tenant_id={self.tenant_id!r})")


@dataclass(frozen=True)
class StartAnswer:
    """What swarm-voice's 201 carries besides the session id it echoes."""

    answer: str
    live_session_id: str
    mode: str = MODE_LIVE


@dataclass(frozen=True)
class TurnAnswer:
    """swarm-voice's answer to one text turn."""

    turn_id: str
    reply: str
    cards: list[dict[str, Any]]
    #: The fast front's delegation to the planner, on a speech session.
    delegation_id: str | None = None


@dataclass(frozen=True)
class EnginePick:
    """What a Start runs: Settings' engine, the session's mode, and the
    fallback's fixed code when the mode is text."""

    engine: str
    mode: str
    fallback: str | None = None


def pick_engine(chosen: Mapping[str, Any], pool: Any, person: str, *,
                speech_deployed: bool) -> EnginePick:
    """The engine Voice it › Settings name (`chosen`, settings.effective's
    view, whose default is own speech), with no choice at Start.

    own speech is `speech`, or `text` with `speech_not_deployed` when the
    deployment runs no speech machine; the OpenAI pool is never asked.
    GPT-Live is `live`, or `text` with `no_openai_key` when the broker says
    the pool holds no OpenAI API key; a broker that cannot be asked keeps
    `live`, and swarm-voice's own lease refuses properly."""
    engine = chosen.get("engine")
    engine = engine if engine in ENGINE_MODES else cat.DEFAULTS["engine"]
    if engine == cat.ENGINE_OWN:
        if not speech_deployed:
            return EnginePick(engine, MODE_TEXT, FALLBACK_NOT_DEPLOYED)
        return EnginePick(engine, MODE_SPEECH)
    try:
        answer = pool.voice_pool(person)
    except Exception as exc:  # noqa: BLE001 -- the class name only; the Live path refuses properly
        log.warning("the Voice it pool could not be read for GPT-Live (%s); live", type(exc).__name__)
        return EnginePick(engine, MODE_LIVE)
    reason = answer.get("reason") if isinstance(answer, dict) else None
    if reason == NO_LIVE_ACCOUNT:
        return EnginePick(engine, MODE_TEXT, FALLBACK_NO_OPENAI_KEY)
    return EnginePick(engine, MODE_LIVE)


class VoiceClient(Protocol):
    def start(self, call: StartCall) -> StartAnswer:
        """swarm-voice's answer, or `VoiceUnreachable`, or a pool refusal
        (`VoicePoolRefused`/`VoicePoolBusy`)."""
        ...

    def end(self, session_id: str, *, cause: str) -> bool:
        """True when swarm-voice took the end. Never raises."""
        ...

    def turn(self, session_id: str, text: str) -> TurnAnswer:
        """swarm-voice's answer to a text turn, or `VoiceSessionNotLive`,
        `VoiceUsageLimited` or `VoiceUnreachable`."""
        ...

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        """The planner's spoken line once the front's delegation is done, or
        None (not done in `timeout`, unknown, another session's). Never raises."""
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
            # A refusal's body is swarm-voice's {code, message}: read so the
            # pool's sentence reaches the browser; never logged.
            try:
                raw = exc.read(16 * 1024) if exc.fp is not None else b""
            except OSError:
                raw = b""
            return exc.code, raw or b""

    def _get(self, path: str, timeout: float) -> tuple[int, bytes]:
        bearer = self._token(self._audience, timeout)
        request = urllib.request.Request(self._base + path, method="GET",
                                         headers={"Authorization": f"Bearer {bearer}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - our own internal URL
                return response.status, response.read(256 * 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    def start(self, call: StartCall) -> StartAnswer:
        body = {"voice_session_id": call.session_id, "project_id": call.project_id,
                "person": call.person, "offer": call.offer, "grant": call.grant}
        if call.mode in TURN_MODES:
            # No offer: a text conversation and the fast front have no WebRTC.
            # A Live body is today's, field for field (swarm-voice's `mode`
            # defaults to live).
            body = {"voice_session_id": call.session_id, "project_id": call.project_id,
                    "person": call.person, "grant": call.grant, "mode": call.mode}
        if call.reopen:
            # The reopen carries no grant: swarm-voice keeps the spent session's.
            body = {"voice_session_id": call.session_id, "person": call.person, "offer": call.offer}
        if call.planner_model is not None:
            body["planner_model"] = call.planner_model
        if call.conversation_model is not None:
            body["conversation_model"] = call.conversation_model
        if call.debug:
            body["debug"] = True
        try:
            status, raw = self._post("/internal/sessions", body, START_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice start for session=%s did not reach swarm-voice (%s)",
                        call.session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached; the session was ended.") from None
        if call.mode == MODE_SPEECH and status == 409 and _refusal_body(raw)[0] == FRONT_REFUSED_CODE:
            log.info("swarm-voice found no Claude account for the front of session=%s", call.session_id)
            raise SpeechFrontRefused("swarm-voice could not start the fast front")
        refusal = pool_refusal(status, raw, mode=call.mode) if status in (403, 409) else None
        if refusal is not None:
            log.warning("swarm-voice refused the Voice it pool for session=%s (HTTP %s, %s)",
                        call.session_id, status, refusal.detail.get("reason"))
            raise refusal
        refused = openai_refusal(raw) if status == 502 else None
        if refused is not None:
            log.warning("OpenAI refused the Live session for session=%s (OpenAI HTTP %s, %s, %s)",
                        call.session_id, refused.detail["openai_status"], refused.detail["openai_code"],
                        refused.detail["openai_type"])
            raise refused
        if status != 201:
            log.warning("swarm-voice refused the start for session=%s (HTTP %s)", call.session_id, status)
            raise VoiceUnreachable(f"swarm-voice refused the session (HTTP {status}); it was ended.")
        try:
            started = json.loads(raw.decode("utf-8"))
        except ValueError:
            started = None
        if not isinstance(started, dict) or started.get("voice_session_id") != call.session_id:
            log.warning("swarm-voice answered the start for session=%s for another session", call.session_id)
            raise VoiceUnreachable("swarm-voice answered for another session; the session was ended.")
        if call.mode in TURN_MODES:
            if started.get("mode") != call.mode:
                raise VoiceUnreachable(f"swarm-voice did not start a {call.mode} conversation; "
                                       "the session was ended.")
            return StartAnswer(answer="", live_session_id="", mode=call.mode)
        answer, live_id = started.get("answer"), started.get("live_session_id")
        if not isinstance(answer, str) or not isinstance(live_id, str) or not live_id:
            raise VoiceUnreachable("swarm-voice answered without an SDP answer; the session was ended.")
        return StartAnswer(answer=answer, live_session_id=live_id)

    def turn(self, session_id: str, text: str) -> TurnAnswer:
        path = "/internal/sessions/" + urllib.parse.quote(session_id, safe="") + "/turns"
        try:
            status, raw = self._post(path, {"text": text}, TURN_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice turn for session=%s did not reach swarm-voice (%s)", session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached for this turn.") from None
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("detail"), dict):
            body = body["detail"]
        if status == 404:
            raise VoiceSessionNotLive("This Voice it conversation is no longer live; start a new one.",
                                      detail={"reason": NOT_LIVE})
        if status == 429 and isinstance(body, dict) and body.get("code") == USAGE_LIMIT:
            log.info("the ChatGPT plan's usage limit refused a turn for session=%s", session_id)
            resets = body.get("resets_at")
            raise VoiceUsageLimited(resets if isinstance(resets, str) else None, now=datetime.now(timezone.utc))
        refused = openai_refusal(raw) if status == 502 else None
        if refused is not None:
            log.warning("OpenAI refused a text turn for session=%s (OpenAI HTTP %s, %s)", session_id,
                        refused.detail["openai_status"], refused.detail["openai_code"])
            raise VoiceUnreachable(
                "OpenAI refused this turn on Voice it's ChatGPT plan "
                f"({refused.detail['openai_status'] or 'in the stream'}, {refused.detail['openai_code']}).",
                detail=refused.detail)
        if status != 200 or not isinstance(body, dict):
            log.warning("swarm-voice refused a turn for session=%s (HTTP %s)", session_id, status)
            raise VoiceUnreachable(f"swarm-voice could not answer this turn (HTTP {status}).")
        turn_id, reply, cards = body.get("turn_id"), body.get("reply"), body.get("cards")
        if not isinstance(turn_id, str) or not isinstance(reply, str):
            raise VoiceUnreachable("swarm-voice answered the turn without a reply.")
        delegation = body.get("delegation_id")
        return TurnAnswer(turn_id=turn_id, reply=reply,
                          cards=[c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else [],
                          delegation_id=delegation if isinstance(delegation, str) and delegation else None)

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        wait = max(0.0, min(float(timeout), DELEGATION_WAIT_SECONDS))
        path = ("/delegations/" + urllib.parse.quote(delegation_id, safe="") + "?"
                + urllib.parse.urlencode({"wait": wait}))
        try:
            status, raw = self._get(path, wait + 10.0)
        except (OSError, ValueError) as exc:
            log.warning("voice delegation %s for session=%s did not reach swarm-voice (%s)",
                        delegation_id, session_id, type(exc).__name__)
            return None
        try:
            body = json.loads(raw.decode("utf-8")) if status == 200 and raw else None
        except ValueError:
            body = None
        # Another session's delegation is never spoken here, whatever its id.
        if not isinstance(body, dict) or body.get("voice_session_id") != session_id \
                or body.get("state") != "done":
            return None
        spoken = body.get("spoken")
        return spoken if isinstance(spoken, str) and spoken.strip() else None

    def reopen_voice(self, session_id: str, *, offer: str, cause: str, brief: dict[str, Any] | None,
                     cards: list[str]) -> StartAnswer:
        """swarm-voice's `POST /internal/sessions/{id}/reopen` (THE BROWSER'S
        REOPEN): the answer, or `VoiceSessionNotLive` (not held there),
        `VoiceSessionLive` (nothing to reopen, or a reopen in flight), a pool
        refusal, or `VoiceUnreachable`. Neither body is logged."""
        path = "/internal/sessions/" + urllib.parse.quote(session_id, safe="") + "/reopen"
        body: dict[str, Any] = {"offer": offer, "cause": cause, "cards": cards}
        if brief:
            body["brief"] = brief
        try:
            status, raw = self._post(path, body, START_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            log.warning("voice reopen for session=%s did not reach swarm-voice (%s)",
                        session_id, type(exc).__name__)
            raise VoiceUnreachable("swarm-voice could not be reached to reopen the voice; "
                                   "the session is still paused.") from None
        if status == 404:
            raise VoiceSessionNotLive("swarm-voice no longer holds this Voice it session; start a new one.",
                                      detail={"reason": NOT_LIVE})
        refusal = pool_refusal(status, raw) if status in (403, 409) else None
        if refusal is not None:
            log.warning("swarm-voice refused the Voice it pool on the reopen of session=%s (HTTP %s, %s)",
                        session_id, status, refusal.detail.get("reason"))
            raise refusal
        if status == 409:
            raise VoiceSessionLive("This Voice it session's voice is already open or reopening.",
                                   detail={"session_id": session_id, "reason": "nothing_to_reopen"})
        refused = openai_refusal(raw) if status == 502 else None
        if refused is not None:
            raise refused
        if status != 200:
            log.warning("swarm-voice refused the reopen of session=%s (HTTP %s)", session_id, status)
            raise VoiceUnreachable(f"swarm-voice could not reopen the voice (HTTP {status}); "
                                   "the session is still paused.")
        try:
            answered = json.loads(raw.decode("utf-8"))
        except ValueError:
            answered = None
        if not isinstance(answered, dict) or answered.get("voice_session_id") != session_id:
            raise VoiceUnreachable("swarm-voice answered the reopen for another session.")
        answer, live_id = answered.get("answer"), answered.get("live_session_id")
        if not isinstance(answer, str) or not isinstance(live_id, str) or not live_id:
            raise VoiceUnreachable("swarm-voice answered the reopen without an SDP answer.")
        return StartAnswer(answer=answer, live_session_id=live_id)

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


LIVE_HERE = "You already have a live Voice it session; end it before starting another."


def live_elsewhere() -> str:
    """The 409's words for a session live in another workspace: no id, no
    workspace name (invariant 9)."""
    return ("You already have a live Voice it session in another workspace; switch to it "
            f"to end it, or it ends by itself after {LIVE_STALE_SECONDS // 60} minutes "
            "without activity.")


def last_activity(session: Mapping[str, Any]) -> datetime | None:
    """The newest time the session document records it in use."""
    times = [_aware(session.get(field)) for field in ACTIVITY_FIELDS]
    fallback = session.get("fallback")
    if isinstance(fallback, Mapping):
        times.append(_aware(fallback.get("at")))
    seen = [t for t in times if t is not None]
    return max(seen) if seen else None


def paused_seconds(session: Mapping[str, Any], start: datetime, end: datetime) -> float:
    """The seconds of [start, end] the session's voice was paused (THE PAUSE):
    each closed interval and the open pause, clipped to the span, plus the
    folded `paused_seconds_before`."""
    intervals: list[tuple[datetime | None, datetime | None]] = []
    for pause in session.get("pauses") or ():
        if isinstance(pause, Mapping):
            intervals.append((_aware(pause.get("paused_at")), _aware(pause.get("woke_at"))))
    open_since = _aware(session.get("paused_at"))
    if open_since is not None:
        intervals.append((open_since, end))
    total = 0.0
    for began, woke in intervals:
        if began is None or woke is None:
            continue
        lo, hi = max(began, start), min(woke, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    before = session.get("paused_seconds_before")
    if isinstance(before, (int, float)) and not isinstance(before, bool) and before > 0:
        total += float(before)
    return total


def _closed_pauses(data: Mapping[str, Any], woke_at: datetime) -> dict[str, Any]:
    """The fields that close the open pause at `woke_at`: the interval joins
    `pauses`, the oldest beyond PAUSES_KEPT folding into its seconds."""
    began = _aware(data.get("paused_at"))
    pauses = [p for p in (data.get("pauses") or []) if isinstance(p, Mapping)]
    fields: dict[str, Any] = {"voice_state": VOICE_OPEN, "paused_at": None}
    if began is None:
        return fields
    pauses.append({"paused_at": began, "woke_at": max(woke_at, began)})
    before = data.get("paused_seconds_before")
    folded = float(before) if isinstance(before, (int, float)) and not isinstance(before, bool) else 0.0
    while len(pauses) > PAUSES_KEPT:
        oldest = pauses.pop(0)
        start, stop = _aware(oldest.get("paused_at")), _aware(oldest.get("woke_at"))
        if start is not None and stop is not None and stop > start:
            folded += (stop - start).total_seconds()
        fields["paused_seconds_before"] = folded
    fields["pauses"] = [dict(p) for p in pauses]
    return fields


def open_card_lines(cards: Any, session_id: str) -> list[str]:
    """The session's open cards as one-liners for a reopen's instructions:
    the kind, the title and the summary, quoted data, never configuration."""
    lines: list[str] = []
    for card in cards:
        body = card.get("body") if isinstance(card.get("body"), Mapping) else {}
        if card.get("session_id") != session_id or body.get("state") not in OPEN_CARD_STATES:
            continue
        title, summary = body.get("title"), body.get("summary")
        line = f"{card.get('kind') or 'card'} ({body['state']}): {title if isinstance(title, str) else ''}".rstrip()
        if isinstance(summary, str) and summary.strip():
            line = f"{line} -- {summary.strip()}"
        lines.append(line[:REOPEN_CARD_MAX_CHARS])
    return lines[:REOPEN_CARDS_MAX]


def record_heartbeat(db: Any, *, project_id: str, session_id: str, now: datetime) -> None:
    """The Talk tab is still open: `heartbeat_at`, written when its event
    stream connects (last_activity). Best effort: never fails the stream."""
    try:
        (db.collection(PROJECTS).document(project_id).collection(SESSIONS).document(session_id)
         .update({"heartbeat_at": now}))
    except Exception as exc:  # noqa: BLE001
        log.warning("voice session=%s: heartbeat not recorded (%s)", session_id, type(exc).__name__)


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
                 now: Callable[[], datetime],
                 class_models: Mapping[str, str] | None = None,
                 machine: Any = None) -> None:
        self._db = db
        self._client = client
        self._marker_key = marker_key
        self._now = now
        #: swarm-api's `worker_class_models`; None reads WORKER_CLASS_MODELS,
        #: the variable `Settings.worker_class_models` is parsed from.
        self._class_models = class_models
        #: The speech machine's lifecycle (speech_machine.SpeechMachine), or
        #: None where the deployment runs none: a speech start then falls back.
        self._machine = machine

    def _session_models(self) -> tuple[str | None, str | None]:
        """(planner model, conversation model) for a session starting now,
        each None where the setting leaves swarm-voice on its pin."""
        from .settings import COLLECTION, DOCUMENT, VoiceSettings  # settings imports this module

        pins = cat.configured_class_models() if self._class_models is None else self._class_models
        planner = VoiceSettings(self._db, now=self._now).session_planner_model(pins)
        snap = self._db.collection(COLLECTION).document(DOCUMENT).get()
        stored = (snap.to_dict() or {}).get("conversation_model") if snap.exists else None
        conversation = next((m.model for m in cat.CONVERSATION_MODELS if m.name == stored), None)
        return planner, conversation

    def _session_debug(self) -> bool:
        """Whether a session starting now is traced (Settings' debug mode)."""
        from .settings import VoiceSettings  # settings imports this module

        return VoiceSettings(self._db, now=self._now).session_debug()

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

    def _paused_idle(self, session: Mapping[str, Any]) -> bool:
        """A `live` session paused with nothing delegated running and no
        activity from its person for PAUSED_IDLE_SECONDS (PAUSED_IDLE)."""
        if session.get("state") != LIVE or session.get("voice_state") != VOICE_PAUSED \
                or session.get("work_running") is True:
            return False
        times = [t for t in (_aware(session.get(field)) for field in PAUSED_IDLE_FIELDS) if t is not None]
        return bool(times) and self._now() - max(times) >= timedelta(seconds=PAUSED_IDLE_SECONDS)

    def _stale_cause(self, session: Mapping[str, Any]) -> str | None:
        """Why a `live` session no longer holds its person, or None: a long
        pause (`paused_idle`) before the abandonment (`abandoned`)."""
        if self._paused_idle(session):
            return END_PAUSED_IDLE
        return END_ABANDONED if self._abandoned(session) else None

    def _abandoned(self, session: Mapping[str, Any]) -> bool:
        """A `live` session with no activity for LIVE_STALE_SECONDS. One that
        records no time at all is never judged: every start writes some."""
        if session.get("state") != LIVE:
            return False
        last = last_activity(session)
        return last is not None and self._now() - last >= timedelta(seconds=LIVE_STALE_SECONDS)

    def _holder(self, txn: Any, marker: dict[str, Any] | None) -> tuple[bool, dict[str, Any] | None]:
        """Whether the marker still names a live (or freshly starting)
        session; with the session's data when it names an abandoned `live`
        one, which the caller ends in its transaction (`_end_abandoned`)."""
        if not marker or not marker.get("project_id") or not marker.get("session_id"):
            return False, None
        snap = _snapshot(txn.get(self._session_ref(marker["project_id"], marker["session_id"])))
        if not snap.exists:
            return False, None
        session = snap.to_dict() or {}
        state = session.get("state")
        if state == LIVE:
            return (False, session) if self._stale_cause(session) is not None else (True, None)
        if state == STARTING:
            created = _aware(session.get("created_at"))
            stale = SPEECH_STARTING_STALE_SECONDS if session.get("mode") == MODE_SPEECH \
                else STARTING_STALE_SECONDS
            return created is None or self._now() - created < timedelta(seconds=stale), None
        return False, None

    def _end_abandoned(self, txn: Any, marker: Mapping[str, Any], session: Mapping[str, Any]) -> None:
        """End the abandoned session the marker names, in the caller's
        transaction (its reads done): the fields a person's end writes, with
        its own cause."""
        project_id, session_id = str(marker["project_id"]), str(marker["session_id"])
        cause = self._stale_cause(session) or END_ABANDONED
        txn.update(self._session_ref(project_id, session_id), {"state": ENDED, "ended_at": self._now(), "end_cause": cause,
                         "grant_hash": None})
        self._audit(txn, action=AUDIT_ENDED, tenant_id=str(session.get("tenant_id") or ""),
                    person=str(session.get("person") or ""), project_id=project_id,
                    session_id=session_id, detail={"cause": cause})

    def _tell_abandoned(self, session_id: str, session: Mapping[str, Any]) -> None:
        """swarm-voice ends its side, and a speech session's machine stops,
        the way the end route asks: logged, never blocking."""
        if self._client is None:
            log.warning("abandoned voice session=%s: no swarm-voice to tell", session_id)
            return
        cause = self._stale_cause(session) or END_ABANDONED
        try:
            told = self._client.end(session_id, cause=cause)
            if session.get("mode") == MODE_SPEECH and self._machine is not None:
                holder = self._machine.holder_of(session_id)
                if holder is not None:
                    self._machine.stop(holder, cause=cause)
        except Exception as exc:  # noqa: BLE001 -- best effort: the session is ended here
            log.warning("abandoned voice session=%s: swarm-voice's end failed (%s)",
                        session_id, type(exc).__name__)
            return
        log.info("stale voice session ended session=%s cause=%s voice_told=%s", session_id, cause, told)

    def sweep_abandoned(self) -> list[str]:
        """End every abandoned `live` session a person's marker names, of
        every mode, and free the marker; the ids ended. The sweep route's.
        Every live session is behind its person's marker: a start replaces a
        marker only once its session no longer holds."""
        ended: list[str] = []
        for marker_snap in self._db.collection(LIVE_MARKERS).stream():
            marker_ref = self._db.collection(LIVE_MARKERS).document(marker_snap.id)

            @firestore.transactional
            def _apply(txn: Any) -> tuple[str, dict[str, Any]] | None:
                snap = _snapshot(txn.get(marker_ref))
                marker = snap.to_dict() if snap.exists else None
                _, session = self._holder(txn, marker)
                if marker is None or session is None:
                    return None
                self._end_abandoned(txn, marker, session)
                txn.delete(marker_ref)
                return str(marker["session_id"]), session

            try:
                done = _apply(self._db.transaction())
            except Exception:  # noqa: BLE001 -- one marker's failure never stops the sweep
                log.exception("voice sweep: marker %s failed", marker_snap.id)
                continue
            if done is not None:
                ended.append(done[0])
                self._tell_abandoned(*done)
        return ended

    def start(self, *, tenant_id: str, person: str, offer: str,
              project_id: str | None, mode: str = MODE_LIVE, fallback: str | None = None,
              chosen: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """No credential: swarm-voice leases the pool accounts itself. `mode`
        and `fallback` are `pick_engine`'s; `chosen` is Settings' view, which
        the speech machine's start applies. A text or speech session sends no
        offer. A speech session that cannot start becomes text, with its
        reason (`fallback` in the answer)."""
        if mode not in MODES:
            raise ValueError(f"no Voice it session mode {mode!r}")
        if fallback is not None and (mode != MODE_TEXT or fallback not in FALLBACK_REASONS):
            raise ValueError(f"no Voice it fallback {fallback!r} for mode {mode!r}")
        if mode == MODE_SPEECH and self._machine is None:
            mode, fallback = MODE_TEXT, FALLBACK_NOT_DEPLOYED
        # Read before anything is written, so a failed read leaves nothing held.
        planner_model, conversation_model = self._session_models()
        debug = self._session_debug()
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
            abandoned.clear()
            holds, stale = self._holder(txn, marker)
            if holds:
                here = bool(marker) and marker.get("tenant_id") == tenant_id
                raise VoiceSessionLive(LIVE_HERE if here else live_elsewhere(),
                                       detail=_live_detail(marker, tenant_id))
            if marker is not None and stale is not None:
                # Ended here, in the start's transaction; the marker is
                # replaced below. swarm-voice is told once this commits.
                self._end_abandoned(txn, marker, stale)
                abandoned.append((str(marker["session_id"]), stale))
            now = self._now()
            if new_project:
                txn.set(project_ref, {
                    "tenant_id": tenant_id, "person": person, "title": UNTITLED,
                    "status": "active", "created_at": now, "updated_at": now})
            else:
                txn.update(project_ref, {"updated_at": now})
            fields = {
                "tenant_id": tenant_id, "person": person, "project_id": project_id,
                "state": STARTING, "created_at": now, "grant_hash": grant_hash(grant),
                "mode": mode}
            if fallback is not None:
                fields["fallback"] = {"from": FALLBACK_FROM[fallback], "reason": fallback, "at": now}
            txn.set(session_ref, fields)
            txn.set(marker_ref, {
                "tenant_id": tenant_id, "project_id": project_id,
                "session_id": session_id, "at": now})
            self._audit(txn, action=AUDIT_STARTED, tenant_id=tenant_id, person=person,
                        project_id=project_id, session_id=session_id,
                        detail={"new_project": new_project})

        abandoned: list[tuple[str, dict[str, Any]]] = []
        _open(self._db.transaction())
        for stale_id, stale in abandoned:
            self._tell_abandoned(stale_id, stale)

        def call_for(this_mode: str) -> StartCall:
            return StartCall(session_id=session_id, project_id=project_id, tenant_id=tenant_id,
                             person=person, offer=offer if this_mode == MODE_LIVE else "", grant=grant,
                             mode=this_mode, planner_model=planner_model,
                             conversation_model=conversation_model, debug=debug)

        def close(cause: str) -> None:
            self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                        session_id=session_id, cause=cause)

        on_front = False
        try:
            if mode == MODE_SPEECH:
                fallback = self._start_speech(
                    call_for(MODE_SPEECH), chosen=chosen or {}, tenant_id=tenant_id, close=close)
                if fallback is not None:
                    mode = MODE_TEXT
                    self._session_ref(project_id, session_id).update(
                        {"mode": MODE_TEXT,
                         "fallback": {"from": cat.ENGINE_OWN, "reason": fallback, "at": self._now()}})
                    log.info("voice session %s falls back to text (%s)", session_id, fallback)
                    # A machine fallback keeps the front, which holds the
                    # session's Claude account and takes its text turns: no
                    # second start, so nothing is released and leased again.
                    on_front = fallback != FALLBACK_FRONT_REFUSED
            if mode != MODE_SPEECH and not on_front:
                started = self._client.start(call_for(mode))
                if not isinstance(started, StartAnswer):
                    raise VoiceUnreachable("swarm-voice did not answer the start; the session was ended.")
                if mode == MODE_TEXT and started.mode != MODE_TEXT:
                    raise VoiceUnreachable("swarm-voice did not start a text conversation; the session was ended.")
                if mode == MODE_LIVE and not _valid_answer(started.answer):
                    raise VoiceUnreachable("swarm-voice's answer is not an SDP answer; the session was ended.")
        except VoiceUnreachable:
            close(END_VOICE_UNREACHABLE)
            raise
        except VoicePoolUnavailable:
            close(END_VOICE_POOL_REFUSED)
            raise
        live_fields: dict[str, Any] = {"state": LIVE, "live_at": self._now()}
        if mode == MODE_LIVE:
            live_fields["live_session_id"] = started.live_session_id
        self._session_ref(project_id, session_id).update(live_fields)
        log.info("voice session started tenant=%s project=%s session=%s new_project=%s mode=%s",
                 tenant_id, project_id, session_id, new_project, mode)
        answer = {"project_id": project_id, "session_id": session_id, "state": LIVE,
                  "new_project": new_project, "mode": mode}
        if mode == MODE_LIVE:
            answer["answer_sdp"] = started.answer
        if fallback is not None:
            answer["fallback"] = {"from": FALLBACK_FROM[fallback], "reason": fallback}
        return answer

    def _start_speech(self, call: StartCall, *, chosen: Mapping[str, Any], tenant_id: str,
                      close: Callable[[str], None]) -> str | None:
        """The front at swarm-voice, then the speech machine to RUNNING. None
        when both started; else the fallback's fixed code, with the machine's
        lease released and the front KEPT: it holds the session's Claude
        account and carries the text session, so the fallback never releases
        an account to race for another (live QA, 9 November 2026). A machine
        another session holds, or a failure that is no fallback, ends the
        session and raises."""
        from .speech_machine import MachineInUse, SessionRef, SpeechFallback  # it imports this module

        try:
            started = self._client.start(call)
        except SpeechFrontRefused:
            return FALLBACK_FRONT_REFUSED
        if not isinstance(started, StartAnswer) or started.mode != MODE_SPEECH:
            raise VoiceUnreachable("swarm-voice did not start the speech conversation; the session was ended.")
        session = SessionRef(tenant_id=tenant_id, person=call.person, project_id=call.project_id,
                             session_id=call.session_id)
        try:
            self._machine.start(session, chosen=chosen)
        except SpeechFallback as exc:
            return exc.reason if exc.reason in FALLBACK_REASONS else FALLBACK_NOT_READY
        except MachineInUse:
            close(END_MACHINE_IN_USE)
            self._client.end(call.session_id, cause=END_MACHINE_IN_USE)
            raise
        except Exception:
            close(END_VOICE_UNREACHABLE)
            self._client.end(call.session_id, cause=END_VOICE_UNREACHABLE)
            raise
        return None

    def hold_reopen(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
                    tier: str) -> None:
        """Record the next tier swarm-voice holds for this live Live session.
        `NotFound` when it is not this person's in this tenant; 409 when it is
        not a live Live session."""
        if tier not in REOPEN_TIERS:
            raise ValueError(f"no Voice it tier {tier!r}")
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(session_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person.strip().lower()):
                raise NotFound(f"voice session {session_id!r} not found")
            if data.get("state") != LIVE or (data.get("mode") or MODE_LIVE) != MODE_LIVE:
                raise VoiceSessionNotLive("This Voice it session is not a live Live session; nothing reopens.",
                                          detail={"reason": NOT_LIVE, "state": data.get("state")})
            txn.update(session_ref, {"reopen": {"tier": tier, "at": self._now()}})

        _apply(self._db.transaction())
        log.info("voice session %s: the %s tier is held for the reopen", session_id, tier)

    def reopen(self, *, tenant_id: str, person: str, session_id: str, offer: str) -> dict[str, Any]:
        """The second start of this person's live Live session, while a reopen
        is held: the hold is claimed, the fresh offer goes to swarm-voice and
        its answer comes back. 404 for a session that is not this person's in
        this tenant; 409 `conflict` with no reopen held, as any second start."""
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _claim(txn: Any) -> str:
            snap = _snapshot(txn.get(session_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person):
                raise NotFound(f"voice session {session_id!r} not found")
            held = data.get("reopen") if isinstance(data.get("reopen"), dict) else {}
            at = _aware(held.get("at"))
            fresh = at is not None and self._now() - at <= timedelta(seconds=REOPEN_HOLD_SECONDS)
            if data.get("state") != LIVE or (data.get("mode") or MODE_LIVE) != MODE_LIVE \
                    or held.get("tier") not in REOPEN_TIERS or not fresh:
                raise VoiceSessionLive(LIVE_HERE, detail={"session_id": session_id, "reason": "no_reopen_held"})
            txn.update(session_ref, {"reopen": None})
            return str(held["tier"])

        tier = _claim(self._db.transaction())
        call = StartCall(session_id=session_id, project_id=project_id, tenant_id=tenant_id,
                         person=person, offer=offer, grant="", mode=MODE_LIVE, reopen=True)
        try:
            started = self._client.start(call)
            if not isinstance(started, StartAnswer) or not _valid_answer(started.answer):
                raise VoiceUnreachable("swarm-voice's answer to the reopen is not an SDP answer; "
                                       "the session was ended.")
        except (VoiceUnreachable, VoicePoolRefused, VoicePoolBusy):
            # The talk ends with its reason: the session, its grant, the held
            # tier and any spent Live session swarm-voice still holds.
            self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                        session_id=session_id, cause=END_REOPEN_FAILED)
            self._client.end(session_id, cause=END_REOPEN_FAILED)
            raise
        session_ref.update({"live_session_id": started.live_session_id, "tier": tier,
                            "reopened_at": self._now(), "activity_at": self._now()})
        log.info("voice session reopened tenant=%s project=%s session=%s tier=%s",
                 tenant_id, project_id, session_id, tier)
        return {"project_id": project_id, "session_id": session_id, "state": LIVE,
                "new_project": False, "mode": MODE_LIVE, "answer_sdp": started.answer, "tier": tier}

    def report_voice_state(self, *, tenant_id: str, person: str, project_id: str, session_id: str,
                           state: str, cause: str | None = None) -> dict[str, Any]:
        """swarm-voice's report through the grant (THE PAUSE): `paused`,
        `waking` or `listening`, at this service's clock. `NotFound` when the
        session is not this person's in this tenant; 409 when it is not a live
        Live session. Repeating a report changes nothing. A change of the
        reported state appends one `paused` stream event ({voice_state,
        cause}, stream.py's whitelist) in the same transaction and keeps the
        state as `voice_report`; a repeated report appends none."""
        if state not in VOICE_REPORTS:
            raise ValueError(f"no Voice it voice state {state!r}")
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(session_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person.strip().lower()):
                raise NotFound(f"voice session {session_id!r} not found")
            if data.get("state") != LIVE or (data.get("mode") or MODE_LIVE) != MODE_LIVE:
                raise VoiceSessionNotLive("This Voice it session is not a live Live session; nothing pauses.",
                                          detail={"reason": NOT_LIVE, "state": data.get("state")})
            now = self._now()
            fields: dict[str, Any] = {}
            if state == "paused" and data.get("voice_state") != VOICE_PAUSED:
                # swarm-voice pauses only with a delegation in flight.
                fields = {"voice_state": VOICE_PAUSED, "paused_at": now, "work_running": True}
            elif state == "waking" and cause == "result":
                fields = {"work_running": False, "work_done_at": now}
            elif state == "listening" and data.get("voice_state") == VOICE_PAUSED:
                fields = _closed_pauses(data, now)
                if cause == "result":
                    fields.update({"work_running": False, "work_done_at": now})
            # The stream's `paused` event, once per change of the reported
            # state (`voice_report`, the last one; before any, what
            # voice_state says): its state and a known cause, nothing else.
            last = data.get("voice_report") or ("paused" if data.get("voice_state") == VOICE_PAUSED
                                                else "listening")
            if state != last:
                seq = int(data.get("event_seq") or 0) + 1
                fields.update({"voice_report": state, "event_seq": seq})
                event: dict[str, Any] = {"type": vwork.PAUSED, "voice_state": state}
                if cause in REOPEN_CAUSES:
                    event["cause"] = cause
                txn.set(session_ref.collection(_EVENTS).document(f"{seq:012d}"), {
                    **event, "seq": seq, "tenant_id": tenant_id, "person": data.get("person"),
                    "at": now})
            if fields:
                txn.update(session_ref, fields)
            return {**data, **fields}

        after = _apply(self._db.transaction())
        log.info("voice session %s: swarm-voice reports %s (%s)", session_id, state, cause or "-")
        return {"session_id": session_id, "voice_state": after.get("voice_state") or VOICE_OPEN}

    def reopen_paused(self, *, tenant_id: str, person: str, session_id: str, offer: str,
                      cause: str) -> dict[str, Any]:
        """The browser's reopen of this person's live Live session (THE
        BROWSER'S REOPEN): the fresh offer, the brief and the open cards go to
        swarm-voice and its answer comes back. 404 for a session that is not
        this person's in this tenant; 409 for one that is ended, starting, or
        not a Live session, and for swarm-voice's "nothing to reopen". A
        refusal leaves the session as it was."""
        if cause not in REOPEN_CAUSES:
            raise ValueError(f"no Voice it reopen cause {cause!r}")
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        session_ref = self._session_ref(project_id, session_id)
        snap = _snapshot(session_ref.get())
        data = (snap.to_dict() or {}) if snap.exists else {}
        if not self._mine(data, tenant_id, person):
            raise NotFound(f"voice session {session_id!r} not found")
        if data.get("state") != LIVE or (data.get("mode") or MODE_LIVE) != MODE_LIVE:
            raise VoiceSessionNotLive("This Voice it session is not live; it cannot be reopened.",
                                      detail={"reason": NOT_LIVE, "state": data.get("state"),
                                              "mode": data.get("mode") or MODE_LIVE})
        project_snap = _snapshot(self._project_ref(project_id).get())
        project = (project_snap.to_dict() or {}) if project_snap.exists else {}
        brief = project.get("brief") if self._mine(project, tenant_id, person) else None
        cards = [{"card_id": c.id, **(c.to_dict() or {})}
                 for c in self._project_ref(project_id).collection("cards").stream()]
        lines = open_card_lines([c for c in cards if self._mine(c, tenant_id, person)], session_id)
        reopen = getattr(self._client, "reopen_voice", None)
        if not callable(reopen):
            raise unavailable()
        started = reopen(session_id, offer=offer, cause=cause,
                         brief=brief if isinstance(brief, dict) and brief else None, cards=lines)
        if not isinstance(started, StartAnswer) or not _valid_answer(started.answer):
            raise VoiceUnreachable("swarm-voice's answer to the reopen is not an SDP answer; "
                                   "the session is still paused.")

        @firestore.transactional
        def _woke(txn: Any) -> None:
            current = _snapshot(txn.get(session_ref))
            now_data = (current.to_dict() or {}) if current.exists else {}
            if now_data.get("state") != LIVE:
                return
            now = self._now()
            fields = {"live_session_id": started.live_session_id, "reopened_at": now, "activity_at": now,
                      **_closed_pauses(now_data, now)}
            if cause == "result":
                fields.update({"work_running": False, "work_done_at": now})
            txn.update(session_ref, fields)

        _woke(self._db.transaction())
        log.info("voice session reopened by cause tenant=%s project=%s session=%s cause=%s",
                 tenant_id, project_id, session_id, cause)
        return {"project_id": project_id, "session_id": session_id, "state": LIVE, "voice_state": VOICE_OPEN,
                "mode": MODE_LIVE, "answer_sdp": started.answer, "cause": cause}

    def mark_seen(self, *, tenant_id: str, person: str, session_id: str, seq: int) -> dict[str, Any]:
        """The highest stream sequence this person's main view has shown
        (`seen_seq`): moved only forward and never past `event_seq`; a lower
        one changes nothing. `seen_at` is the person's activity (PAUSED_IDLE).
        404 for a session that is not this person's in this tenant."""
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        session_ref = self._session_ref(project_id, session_id)

        @firestore.transactional
        def _apply(txn: Any) -> tuple[int, bool]:
            snap = _snapshot(txn.get(session_ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person):
                raise NotFound(f"voice session {session_id!r} not found")
            stored = data.get("seen_seq")
            seen = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
            written = data.get("event_seq")
            ceiling = written if isinstance(written, int) and not isinstance(written, bool) else 0
            wanted = min(max(int(seq), 0), ceiling)
            fields: dict[str, Any] = {"seen_at": self._now()} if data.get("state") == LIVE else {}
            moved = wanted > seen
            if moved:
                fields["seen_seq"] = wanted
            if fields:
                txn.update(session_ref, fields)
            return (wanted if moved else seen), moved

        seen, moved = _apply(self._db.transaction())
        return {"session_id": session_id, "seen_seq": seen, "changed": moved}

    def turn(self, *, tenant_id: str, person: str, session_id: str, text: str) -> dict[str, Any]:
        """One spoken turn of a text session, as text: swarm-voice's reply and
        the cards the planner drew. 404 for a session that is not this
        person's in this tenant; 409 (`voice_session_not_live`) for one that is
        ended, still starting, or a Live (WebRTC) one. A speech session's turn
        is the fast front's at swarm-voice, never the text conversation's, and
        carries `delegation_id` when the front delegated. The text and the
        reply are neither logged nor stored here."""
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        snap = _snapshot(self._session_ref(project_id, session_id).get())
        data = (snap.to_dict() or {}) if snap.exists else {}
        if not self._mine(data, tenant_id, person):
            raise NotFound(f"voice session {session_id!r} not found")
        if data.get("state") != LIVE or data.get("mode") not in TURN_MODES:
            raise VoiceSessionNotLive(
                "This Voice it session takes no text turns: it is not a live text or speech conversation.",
                detail={"reason": NOT_LIVE, "state": data.get("state"), "mode": data.get("mode") or MODE_LIVE})
        answered = self._client.turn(session_id, text)
        try:
            # The session's last activity (LIVE_STALE_SECONDS): never fails the turn.
            self._session_ref(project_id, session_id).update({"activity_at": self._now()})
        except Exception as exc:  # noqa: BLE001
            log.warning("voice turn: session=%s activity not recorded (%s)", session_id, type(exc).__name__)
        log.info("voice turn answered tenant=%s project=%s session=%s turn=%s cards=%d",
                 tenant_id, project_id, session_id, answered.turn_id, len(answered.cards))
        said: dict[str, Any] = {"turn_id": answered.turn_id, "reply": answered.reply, "cards": answered.cards}
        delegation = getattr(answered, "delegation_id", None)
        if delegation and data.get("mode") == MODE_TEXT:
            # A text fallback on the front: the text page reads one reply per
            # turn, so the planner's line is waited for and added to it.
            line = self.delegation(session_id, delegation, timeout=DELEGATION_WAIT_SECONDS)
            if line is not None:
                said["reply"] = f"{answered.reply} {line.strip()}".strip()
        elif delegation:
            said["delegation_id"] = delegation
        return said

    def delegation(self, session_id: str, delegation_id: str, *, timeout: float) -> str | None:
        """The planner's spoken line for the fast front's delegation, read
        from swarm-voice's `GET /delegations/{id}` for this session only
        (speech_relay.Turns' `delegation`); None when there is none yet."""
        ask = getattr(self._client, "delegation", None)
        if not callable(ask):
            return None
        line = ask(session_id, delegation_id, timeout=timeout)
        return line if isinstance(line, str) and line.strip() else None

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
            # The grant dies with the session (§3.6): its hash goes, so no
            # presented grant can match it again, whatever the state reads.
            txn.update(session_ref, {"state": ENDED, "ended_at": now, "end_cause": cause,
                                     "grant_hash": None})
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

    def end(self, *, tenant_id: str, person: str, session_id: str,
            cause: str = END_BY_PERSON) -> dict[str, Any]:
        """The person's own end: End (`ended_by_person`) or the holder's
        pagehide end (`tab_closed`), nothing else (PERSON_END_CAUSES)."""
        if cause not in PERSON_END_CAUSES:
            raise ValueError(f"no Voice it end cause {cause!r} a person may name")
        person = person.strip().lower()
        project_id = self._project_of(tenant_id=tenant_id, person=person, session_id=session_id)
        before = self._close(tenant_id=tenant_id, person=person, project_id=project_id,
                             session_id=session_id, cause=cause)
        told = None
        if before is not None:
            told = self._client.end(session_id, cause=cause)
            if before.get("mode") == MODE_SPEECH and self._machine is not None:
                # Stop (§4.5): the machine stops with the session, fenced by
                # the lease's generation; none held is nothing to stop.
                holder = self._machine.holder_of(session_id)
                if holder is not None:
                    self._machine.stop(holder, cause=cause)
            log.info("voice session ended tenant=%s project=%s session=%s cause=%s voice_told=%s",
                     tenant_id, project_id, session_id, cause, told)
        return {"project_id": project_id, "session_id": session_id, "state": ENDED,
                "changed": before is not None, "voice_told": told}
