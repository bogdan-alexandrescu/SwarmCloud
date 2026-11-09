"""The speech machine's lifecycle: its lease, its generation, its start and
its stop (docs/design/voice-it-own-speech.md §4.2, §4.4-§4.8, §5.3, §11.4).

ONE MACHINE, ONE LEASE (§4.6). `voice_speech/machine` is the lease:

    {session_id, tenant_id, project_id, person, generation, state, zone,
     stockouts, started_at, heartbeat_at, activity_at, idle_seconds, sockets,
     detached_at, start_phases, machine, stopped_at, minutes, cause}

`take` reads it and the person's `voice_live` marker in one transaction: the
marker must name the session asking (so a session that is not the person's
live one cannot take the machine), and a lease another session still holds
(`starting` or `running`, heartbeat under HEARTBEAT_STALE_SECONDS old) is 409
"the speech machine is in use". Each take bumps `generation`. Every write the
holder makes afterwards (heartbeat, activity, sockets, readiness, stop) names
the generation it took and the session it took it for, and is refused
(`StaleLease`) once another take has bumped it: a stale holder stops writing
and never stops a machine another session holds (CONTRACT.md invariant 5's
fencing, applied to the machine).

THE MACHINE IS DEMAND ONLY WHILE LEASED (invariant 1). Nothing queues for it:
a second Start while it is held is refused, never waited on. It takes no pool
slot (invariant 3 counts the front's Claude account, which swarm-voice leases
for the session; the machine is a platform service, not an agent).

START (§4.2). With the lease taken, the machine is found by its name in
each of `SWARM_SPEECH_ZONES` (`<prefix><zone letter>`, one name per zone).
Two found (a crash between a delete and a create) are made one: the running
one is kept, else the first in zone order, and the other is deleted. Then, in
the zones' order (owner, 2026-11-09: "deploys in a if capacity available, if
not checks B and then C", revising the 2026-11-05 answer Q1's one zone, one
machine):

  * a STOPPED machine: a machine type that differs from Settings' is set
    (`setMachineType`) and the metadata the boot reads (`swarm-speech-image`,
    `swarm-speech-models`, `swarm-speech-idle-minutes`) is rewritten where it
    differs (`setMetadata`), so a new release's digest (SWARM_SPEECH_IMAGE,
    §5.3) reaches it at the next session and never starts it on its own; then
    `instances.start` in its own zone. A zone with no L4 free
    (`ZONE_RESOURCE_POOL_EXHAUSTED`, with or without details) deletes that
    stopped machine, waits for the deletion, and goes on to the zones after
    its own, wrapping round once, so each zone is tried at most once a Start;
  * NO machine: `instances.insert` from the release's regional template
    (SWARM_SPEECH_TEMPLATE) in the first zone, with Settings' machine type
    and metadata in the create itself; a stockout tries the next zone;
  * a machine already RUNNING (a stop the sweep has not reached) serves as it
    is: its settings stay pending to the next start.

Every stockout is recorded with its zone (an AUDIT_ZONE_STOCKOUT row, and on
the lease's `stockouts`), and the lease's `zone` is the zone the start used.
After the last zone the start is `NoFreeL4`: the lease ends, one AUDIT_NO_L4
row counts the refused start, and the caller falls back to text. Then
`instances.get` every POLL_SECONDS until RUNNING. READY_SECONDS is the budget
of the whole Start, every zone's create and boot inside it: a zone is not
tried once it is spent, and a machine not RUNNING inside it is stopped and
`MachineNotReady`, which says which zones it tried and which it did not.
Each Compute operation (a start, a delete, a create) is waited on for no
longer than what is left of READY_SECONDS (a `Wait`), so a chain of them
never outlasts the budget. The start refreshes the lease's heartbeat while it
waits, between operations and between each operation's wait pieces, so the
sweep never takes a starting machine for an abandoned one; a start whose
lease was taken from it all the same (`StaleLease`) falls back to text and
touches the machine no more. One machine at a time, under the lease: the
reserved address is the template's, and only one instance can hold it.

THE COMPUTE API. `RestCompute` calls Compute Engine's v1 REST API with
swarm-api's own credentials (google-auth's AuthorizedSession, already a
dependency), on the speech machine's names only: exactly the verbs
`swarmSpeechOperator` grants, on the project, conditioned to those names,
their boot disks, the template, the address and the subnet (§6 I1; actAs
on the speech account alone, I2). It is built lazily, so importing this
module needs no credential.

WHAT ENDS THE SESSION (§4.5, §4.8). `end_session` ends the Voice it session
(through the `SessionEnder` the route wires to swarm-voice's end) and stops
the machine, under the holder's generation. `sweep` is the third stop when
swarm-api itself went away: a lease whose heartbeat is older than
HEARTBEAT_STALE_SECONDS is `swept`; a lease with no socket attached for
TAB_CLOSED_SECONDS is `tab_closed`; a running lease past its idle time is
`idle`; and an instance that is not TERMINATED with no live lease is stopped.
Each stop writes one `voice_audit` row with its machine minutes, counted from
the start call (so the boot is in them and the ledger never under-counts).

Nothing here reads or writes a secret: the speech server checks swarm-api's
Google ID token (§3.2), which `speech_relay` mints per socket.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import Conflict
from . import sessions as vs
from . import settings as vsettings
from . import speech_catalogue as cat

log = logging.getLogger(__name__)

COLLECTION = "voice_speech"
LEASE = "machine"

#: The session's mode on this engine (§10.1); sessions.py's MODE_SPEECH once
#: the start lane (L6) adds it, the same word.
MODE_SPEECH = getattr(vs, "MODE_SPEECH", "speech")

# Lease states. Only STARTING and RUNNING hold the machine.
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
HOLDING = frozenset({STARTING, RUNNING})

# Instance statuses Compute Engine reports.
TERMINATED = "TERMINATED"
INSTANCE_RUNNING = "RUNNING"
INSTANCE_STOPPING = "STOPPING"
#: A stopped instance reads TERMINATED; a suspended one is not this design's.
AT_REST = frozenset({TERMINATED, "STOPPED"})

# What ends a session on this engine (§4.8); `ended_by_person` is sessions.py's.
END_BY_PERSON = vs.END_BY_PERSON
END_IDLE = "idle"
END_TAB_CLOSED = "tab_closed"
END_BUDGET_CAP = "budget_cap"
END_SWEPT = "swept"
# Not session ends: the session falls back to text instead (§4.1, §4.7).
FALLBACK_NO_L4 = "no_l4"
FALLBACK_NOT_READY = "not_ready"
FALLBACK_ENGINE_DOWN = "engine_down"
CAUSES = frozenset({END_BY_PERSON, END_IDLE, END_TAB_CLOSED, END_BUDGET_CAP, END_SWEPT,
                    FALLBACK_NO_L4, FALLBACK_NOT_READY, FALLBACK_ENGINE_DOWN})

#: §4.2 step 2: `instances.get` every two seconds until RUNNING.
POLL_SECONDS = 2.0
#: §4.1: the machine is ready in four minutes or the session falls back.
#: `updating speech server` and `fetching model` have their own ten
#: (UPDATE_SECONDS), counted by the relay, which sees those phases.
READY_SECONDS = 240.0
UPDATE_SECONDS = 600.0
#: §4.5: the sweep stops a lease with no heartbeat under two minutes old.
HEARTBEAT_STALE_SECONDS = 120.0
#: §4.4: swarm-api writes `heartbeat_at` every 30 s while a socket is open.
HEARTBEAT_SECONDS = 30.0
#: §4.5: a closed tab is given a minute to reconnect (a reload, a rotation).
TAB_CLOSED_SECONDS = 60.0
#: The idle time when the lease carries none: Settings' default, five minutes.
DEFAULT_IDLE_SECONDS = cat.idle_seconds(cat.DEFAULTS["idle"]) or 300

AUDIT_STOPPED = "voice_speech_machine_stopped"
AUDIT_NO_L4 = "voice_speech_no_l4"
#: One row per zone that answered a stockout, with its zone (§12); a Start
#: refused in every zone also writes one AUDIT_NO_L4, the count.
AUDIT_ZONE_STOCKOUT = "voice_speech_zone_stockout"

# Compute Engine's error codes for a zone with no capacity of the type asked.
STOCKOUT_CODES = frozenset({"ZONE_RESOURCE_POOL_EXHAUSTED",
                            "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"})
#: The code `RestCompute` gives an instance that does not exist (404).
NOT_FOUND = "NOT_FOUND"
#: The code of an operation that outlasted its wait (`Wait`, OPERATION_SECONDS).
TIMEOUT = "TIMEOUT"

META_IMAGE = "swarm-speech-image"
META_MODELS = "swarm-speech-models"
META_IDLE = "swarm-speech-idle-minutes"

# Settings' machine fields a start applies (voice/settings.py MACHINE_FIELDS).
APPLIED_FIELDS = vsettings.MACHINE_FIELDS


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class MachineInUse(Conflict):
    """Another session holds the speech machine (§4.6): a guard, not a queue."""


class StaleLease(Exception):
    """This holder's generation is no longer the lease's: write nothing more,
    touch no machine."""


class SpeechFallback(Exception):
    """The engine cannot start for this session; it becomes a text session
    (§4.1). `reason` is the fallback's word, written to `fallback.reason`."""

    reason = FALLBACK_NOT_READY

    def __init__(self, message: str, *, zone: str = "") -> None:
        super().__init__(message)
        self.zone = zone


class NoFreeL4(SpeechFallback):
    reason = FALLBACK_NO_L4


class MachineNotReady(SpeechFallback):
    reason = FALLBACK_NOT_READY


class ComputeError(Exception):
    """Compute Engine refused or did not answer; `code` is its error code."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Configuration: SWARM_SPEECH_* (terraform/infra/locals.tf)
# --------------------------------------------------------------------------


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(p.strip().lower() for p in raw.split(",") if p.strip()))


def _env(name: str, environ: Mapping[str, str] | None = None) -> str:
    """One stripped variable, from `environ` (the tests') or the process's.

    A module-level reader with the key as its first argument, so the
    environment-parity check (scripts/lib/check-env-parity.sh) sees each read.
    """
    env = os.environ if environ is None else environ
    return env.get(name, "").strip()


@dataclass(frozen=True)
class SpeechConfig:
    """Where the machine is and how it is reached. Empty `address` means the
    engine is not deployed (voice_speech off): no route touches Compute."""

    project: str = ""
    #: The zones the machine is created or started in, in order (Terraform's
    #: voice_speech.zones, owner 2026-11-09).
    zones: tuple[str, ...] = ()
    #: The machine's name up to its zone letter, `<name_prefix>-speech-`; a
    #: name that does not end in "-" is the one machine's whole name.
    instance: str = ""
    #: The regional instance template a create names
    #: (projects/<p>/regions/<r>/instanceTemplates/<name>).
    template: str = ""
    address: str = ""
    #: The speech server's digest the release pinned (§5.3); empty leaves the
    #: instance's metadata as it is.
    image: str = ""
    #: The ID token's audience the speech server checks (§3.2).
    audience: str = "swarm-speech"
    port: int = 8080
    #: The audience and callers the sweep route admits (§4.5).
    sweep_audience: str = ""
    sweep_callers: tuple[str, ...] = ()

    @property
    def zone(self) -> str:
        """The first zone: where a Start with no machine tries first."""
        return self.zones[0] if self.zones else ""

    def name_in(self, zone: str) -> str:
        """The machine's name in `zone`: `<prefix><zone letter>`."""
        if not self.instance.endswith("-"):
            return self.instance
        return self.instance + zone.rsplit("-", 1)[-1]

    @property
    def enabled(self) -> bool:
        return bool(self.address and self.instance and self.zones and self.project
                    and self.template)

    @property
    def stream_url(self) -> str:
        return f"ws://{self.address}:{self.port}/v1/stream"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SpeechConfig":
        env = os.environ if environ is None else environ
        zones = cat.configured_zones(env) if env.get(cat.ZONES_ENV, "").strip() else ()
        return cls(
            project=_env("SWARM_SPEECH_PROJECT", env),
            zones=zones,
            instance=_env("SWARM_SPEECH_INSTANCE", env),
            template=_env("SWARM_SPEECH_TEMPLATE", env),
            address=_env("SWARM_SPEECH_ADDR", env),
            image=_env("SWARM_SPEECH_IMAGE", env),
            audience=_env("SWARM_SPEECH_AUDIENCE", env) or "swarm-speech",
            sweep_audience=_env("SWARM_SPEECH_SWEEP_AUDIENCE", env),
            sweep_callers=_csv(_env("SWARM_SPEECH_SWEEP_CALLERS", env)),
        )


# --------------------------------------------------------------------------
# Compute Engine
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    status: str
    #: The machine type's short name, e.g. "g2-standard-4".
    machine_type: str
    metadata: dict[str, str]
    fingerprint: str
    zone: str = ""


def _no_tick() -> None:
    return None


@dataclass(frozen=True)
class Wait:
    """How long one Compute operation may be waited on (what is left of the
    start's budget), and what runs between the wait's pieces (the lease's
    heartbeat)."""
    seconds: float
    tick: Callable[[], None] = _no_tick


class Compute(Protocol):
    """The speech machine in one zone, by its name there (`name_in`). Every
    call that changes it returns once Compute finished the operation, and
    raises `ComputeError`: a STOCKOUT_CODES code when the zone has no L4,
    TIMEOUT when the operation outlasts its `wait` (or OPERATION_SECONDS)."""

    def get(self, zone: str) -> Instance | None:
        """The machine in `zone`, or None when there is none."""
        ...
    def create(self, zone: str, *, machine_type: str, metadata: Mapping[str, str],
               wait: Wait | None = None) -> None:
        """Create it from the template; Compute starts it."""
        ...
    def delete(self, zone: str, *, wait: Wait | None = None) -> None: ...
    def start(self, zone: str, *, wait: Wait | None = None) -> None: ...
    def stop(self, zone: str) -> None: ...
    def set_machine_type(self, zone: str, machine_type: str, *, wait: Wait | None = None) -> None: ...
    def set_metadata(self, zone: str, items: Mapping[str, str], fingerprint: str, *,
                     wait: Wait | None = None) -> None: ...


class RestCompute:
    """Compute Engine v1 for the speech machine, with swarm-api's credentials."""

    BASE = "https://compute.googleapis.com/compute/v1"
    #: Each operation is waited on in pieces of TIMEOUT_SECONDS at most
    #: (zoneOperations.wait's own limit is two minutes, as long as a stale
    #: heartbeat: a piece that ends with the operation still running is
    #: asked again), up to OPERATION_SECONDS in all, or the `Wait`'s seconds
    #: when they are fewer.
    OPERATION_SECONDS = 300.0
    TIMEOUT_SECONDS = 30.0

    def __init__(self, config: SpeechConfig, *, session: Any = None) -> None:
        self._config = config
        self._session = session

    def _http(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _zone_url(self, zone: str) -> str:
        return f"{self.BASE}/projects/{self._config.project}/zones/{zone}"

    def _url(self, zone: str) -> str:
        return f"{self._zone_url(zone)}/instances/{self._config.name_in(zone)}"

    @staticmethod
    def _error(body: Any, status: int) -> ComputeError:
        errors = []
        if isinstance(body, dict):
            raw = body.get("error")
            if isinstance(raw, dict):
                errors = raw.get("errors") or [raw]
        codes = [str(e.get("code") or e.get("reason") or "") for e in errors if isinstance(e, dict)]
        code = next((c for c in codes if c in STOCKOUT_CODES), codes[0] if codes else str(status))
        if status == 404:
            code = NOT_FOUND
        return ComputeError(f"compute engine answered {status} ({code})", code=code)

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None, *,
              timeout: float | None = None) -> dict[str, Any]:
        response = self._http().request(method, url, json=body,
                                        timeout=self.TIMEOUT_SECONDS if timeout is None else timeout)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            raise self._error(data, response.status_code)
        return data if isinstance(data, dict) else {}

    def _finish(self, zone: str, operation: dict[str, Any], wait: Wait | None) -> None:
        import requests

        seconds = self.OPERATION_SECONDS if wait is None else min(self.OPERATION_SECONDS, wait.seconds)
        deadline = time.monotonic() + seconds
        while operation.get("status") != "DONE":
            left = deadline - time.monotonic()
            if left <= 0:
                raise ComputeError("the compute operation did not finish", code=TIMEOUT)
            if wait is not None:
                wait.tick()
            try:
                operation = self._call(
                    "POST", f"{self._zone_url(zone)}/operations/{operation.get('name', '')}/wait",
                    timeout=min(self.TIMEOUT_SECONDS, left))
            except requests.exceptions.Timeout:
                continue  # the piece ended with the operation still running
        if operation.get("error"):
            raise self._error(operation, 409)

    def get(self, zone: str) -> Instance | None:
        try:
            data = self._call("GET", self._url(zone))
        except ComputeError as exc:
            if exc.code == NOT_FOUND:
                return None
            raise
        meta = data.get("metadata") or {}
        items = {str(i.get("key")): str(i.get("value") or "")
                 for i in meta.get("items") or [] if isinstance(i, dict)}
        return Instance(status=str(data.get("status") or ""),
                        machine_type=str(data.get("machineType") or "").rsplit("/", 1)[-1],
                        metadata=items, fingerprint=str(meta.get("fingerprint") or ""), zone=zone)

    def create(self, zone: str, *, machine_type: str, metadata: Mapping[str, str],
               wait: Wait | None = None) -> None:
        # An insert's metadata replaces the template's whole, so the
        # template's own items (the caller's email, no SSH keys, no serial
        # port) are read and kept, and the start's written over them.
        template = self._call("GET", f"{self.BASE}/{self._config.template}")
        items = {str(i.get("key")): str(i.get("value") or "")
                 for i in ((template.get("properties") or {}).get("metadata") or {}).get("items") or []
                 if isinstance(i, dict)}
        items.update(metadata)
        self._finish(zone, self._call(
            "POST", f"{self._zone_url(zone)}/instances?sourceInstanceTemplate={self._config.template}", {
                "name": self._config.name_in(zone),
                "machineType": f"zones/{zone}/machineTypes/{machine_type}",
                "metadata": {"items": [{"key": k, "value": v} for k, v in sorted(items.items())]}}),
            wait)

    def delete(self, zone: str, *, wait: Wait | None = None) -> None:
        try:
            self._finish(zone, self._call("DELETE", self._url(zone)), wait)
        except ComputeError as exc:
            if exc.code != NOT_FOUND:
                raise

    def start(self, zone: str, *, wait: Wait | None = None) -> None:
        self._finish(zone, self._call("POST", f"{self._url(zone)}/start"), wait)

    def stop(self, zone: str) -> None:
        self._finish(zone, self._call("POST", f"{self._url(zone)}/stop"), None)

    def set_machine_type(self, zone: str, machine_type: str, *, wait: Wait | None = None) -> None:
        self._finish(zone, self._call("POST", f"{self._url(zone)}/setMachineType", {
            "machineType": f"zones/{zone}/machineTypes/{machine_type}"}), wait)

    def set_metadata(self, zone: str, items: Mapping[str, str], fingerprint: str, *,
                     wait: Wait | None = None) -> None:
        self._finish(zone, self._call("POST", f"{self._url(zone)}/setMetadata", {
            "fingerprint": fingerprint,
            "items": [{"key": k, "value": v} for k, v in sorted(items.items())]}), wait)


# --------------------------------------------------------------------------
# What a start writes onto the machine (§5.1 metadata, §11.4)
# --------------------------------------------------------------------------


def models_metadata(chosen: Mapping[str, str]) -> str:
    """`swarm-speech-models` as swarm_speech/models.py's parse_models reads it:
    the chosen catalogue entries' repositories, pinned revisions and checksums.
    Names only ever come from the catalogue, so nothing here is free text."""
    stt = next((m for m in cat.STT_MODELS if m.name == chosen.get("stt_model")), cat.STT_MODELS[0])
    tts = cat.tts(chosen.get("tts_model") or "") or cat.TTS_MODELS[0]
    return json.dumps({
        "stt": {"name": stt.name, "hf": stt.hf, "revision": stt.revision,
                "sha256": {f.path: f.sha256 for f in stt.files}},
        "tts": {"name": tts.name, "hf": tts.hf, "revision": tts.revision,
                "sha256": {f.path: f.sha256 for f in tts.files}, "voices": list(tts.voices)},
    }, sort_keys=True, separators=(",", ":"))


def idle_seconds_of(chosen: Mapping[str, str]) -> int:
    return cat.idle_seconds(chosen.get("idle") or "") or DEFAULT_IDLE_SECONDS


def wanted_metadata(chosen: Mapping[str, str], image: str) -> dict[str, str]:
    wanted = {META_MODELS: models_metadata(chosen),
              META_IDLE: str(max(1, idle_seconds_of(chosen) // 60))}
    if image:
        wanted[META_IMAGE] = image
    return wanted


# --------------------------------------------------------------------------
# The lease
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRef:
    tenant_id: str
    person: str
    project_id: str
    session_id: str


@dataclass(frozen=True)
class Holder:
    """What a lease holder presents with every write."""

    session_id: str
    generation: int


@dataclass(frozen=True)
class Started:
    holder: Holder
    #: Whether this start set a new speech server digest: the boot pulls it
    #: first, so the page reads `updating speech server` until the socket
    #: answers (§5.3).
    image_changed: bool
    #: The machine fields as this start applied them, for `mark_applied`.
    applied: dict[str, str]
    image: str
    #: Seconds from the start call to RUNNING (`start.boot`, §4.2 step 5).
    boot_seconds: float
    idle_seconds: int


class SessionEnder(Protocol):
    def end(self, lease: Mapping[str, Any], cause: str) -> None:
        """End the Voice it session the lease names, with `cause`, and tell
        swarm-voice. Never raises for a session already ended."""
        ...


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _sockets(lease: Mapping[str, Any]) -> dict[str, Any]:
    raw = lease.get("sockets")
    return dict(raw) if isinstance(raw, Mapping) else {}


def live_sockets(lease: Mapping[str, Any], now: datetime) -> list[str]:
    """The sockets that wrote a heartbeat under HEARTBEAT_STALE_SECONDS ago."""
    cutoff = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    return sorted(s for s, at in _sockets(lease).items() if (_aware(at) or cutoff) > cutoff)


def holds(lease: Mapping[str, Any] | None, now: datetime) -> bool:
    """Whether the lease still holds the machine for its session."""
    if not lease or lease.get("state") not in HOLDING:
        return False
    beat = _aware(lease.get("heartbeat_at"))
    return beat is not None and now - beat < timedelta(seconds=HEARTBEAT_STALE_SECONDS)


def due_cause(lease: Mapping[str, Any] | None, now: datetime) -> str | None:
    """Why a held lease must end now, or None: no heartbeat (`swept`), no
    socket for a minute (`tab_closed`), or no activity for its idle time."""
    if not lease or lease.get("state") not in HOLDING:
        return None
    # A tab that closed cleanly is `tab_closed` even once the heartbeat it
    # stopped writing has also gone stale; `swept` is the lease nobody let go.
    detached = _aware(lease.get("detached_at"))
    if detached is not None and not live_sockets(lease, now) \
            and now - detached >= timedelta(seconds=TAB_CLOSED_SECONDS):
        return END_TAB_CLOSED
    if not holds(lease, now):
        return END_SWEPT
    if lease.get("state") == RUNNING:
        activity = _aware(lease.get("activity_at"))
        idle = lease.get("idle_seconds")
        idle = idle if isinstance(idle, int) and idle > 0 else DEFAULT_IDLE_SECONDS
        if activity is not None and now - activity >= timedelta(seconds=idle):
            return END_IDLE
    return None


class SpeechMachine:
    """The machine's lease and lifecycle. Synchronous: the routes run it in
    a thread, as every Firestore call in this service is."""

    def __init__(self, db: Any, compute: Compute, *, config: SpeechConfig,
                 now: Callable[[], datetime], marker_key: str = "",
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 applied: Callable[[Mapping[str, str], str], Any] | None = None) -> None:
        self._db = db
        self._compute = compute
        self.config = config
        self._now = now
        self._marker_key = marker_key
        self._sleep = sleep
        self._clock = clock
        #: Settings' `mark_applied` (voice/settings.py), called at readiness.
        self._applied = applied

    @property
    def _ref(self) -> Any:
        return self._db.collection(COLLECTION).document(LEASE)

    def now(self) -> datetime:
        return self._now()

    def read(self) -> dict[str, Any] | None:
        snap = _snapshot(self._ref.get())
        return (snap.to_dict() or {}) if snap.exists else None

    def _audit(self, txn: Any, action: str, lease: Mapping[str, Any], detail: dict[str, Any]) -> None:
        row = {"action": action, "tenant_id": lease.get("tenant_id"), "by": lease.get("person"),
               "at": self._now(), "project_id": lease.get("project_id"),
               "session_id": lease.get("session_id"), "detail": detail}
        if txn is None:
            self._db.collection(vs.AUDIT).document().set(row)
        else:
            txn.set(self._db.collection(vs.AUDIT).document(), row)

    # -- the take (§4.6)

    def take(self, session: SessionRef, *, idle_seconds: int, machine: str = "") -> Holder:
        ref = self._ref
        marker_ref = self._db.collection(vs.LIVE_MARKERS).document(
            vs.marker_id(self._marker_key, session.person))

        @firestore.transactional
        def _take(txn: Any) -> Holder:
            snap = _snapshot(txn.get(ref))
            marker_snap = _snapshot(txn.get(marker_ref))
            lease = (snap.to_dict() or {}) if snap.exists else {}
            marker = (marker_snap.to_dict() or {}) if marker_snap.exists else {}
            if marker.get("session_id") != session.session_id \
                    or marker.get("project_id") != session.project_id \
                    or marker.get("tenant_id") != session.tenant_id:
                raise vs.VoiceSessionNotLive(
                    "This Voice it session is not the person's live session.",
                    detail={"reason": vs.NOT_LIVE})
            now = self._now()
            if holds(lease, now) and lease.get("session_id") != session.session_id:
                raise MachineInUse("the speech machine is in use",
                                   detail={"reason": "speech_machine_in_use"})
            generation = int(lease.get("generation") or 0) + 1
            txn.set(ref, {
                "session_id": session.session_id, "tenant_id": session.tenant_id,
                "project_id": session.project_id, "person": session.person,
                "generation": generation, "state": STARTING, "zone": self.config.zone,
                "stockouts": [],
                "started_at": now, "heartbeat_at": now, "activity_at": now,
                "idle_seconds": int(idle_seconds), "sockets": {}, "detached_at": None,
                "start_phases": {}, "stopped_at": None, "minutes": None, "cause": None,
                "machine": machine or cat.DEFAULTS["machine"],
            })
            return Holder(session_id=session.session_id, generation=generation)

        return _take(self._db.transaction())

    def _fenced(self, holder: Holder, change: Callable[[dict[str, Any], datetime], dict[str, Any] | None],
                *, holding: bool = True) -> dict[str, Any]:
        """Apply `change` to the lease when it is still this holder's;
        `StaleLease` otherwise. Returns the lease as written."""
        ref = self._ref

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            lease = (snap.to_dict() or {}) if snap.exists else {}
            if lease.get("generation") != holder.generation \
                    or lease.get("session_id") != holder.session_id \
                    or (holding and lease.get("state") not in HOLDING):
                raise StaleLease(f"generation {holder.generation} no longer holds the machine")
            fields = change(lease, self._now())
            if fields:
                txn.update(ref, fields)
                lease = {**lease, **fields}
            return lease

        return _apply(self._db.transaction())

    def beat(self, holder: Holder, *, socket_id: str, activity_at: datetime | None = None) -> dict[str, Any]:
        """The socket's heartbeat, and the last activity it saw (§4.4)."""

        def change(lease: dict[str, Any], now: datetime) -> dict[str, Any]:
            sockets = _sockets(lease)
            sockets[socket_id] = now
            fields: dict[str, Any] = {"heartbeat_at": now, "sockets": sockets, "detached_at": None}
            current = _aware(lease.get("activity_at"))
            if activity_at is not None and (current is None or activity_at > current):
                fields["activity_at"] = activity_at
            return fields

        return self._fenced(holder, change)

    def attach(self, holder: Holder, *, socket_id: str) -> dict[str, Any]:
        return self.beat(holder, socket_id=socket_id)

    def detach(self, holder: Holder, *, socket_id: str) -> dict[str, Any]:
        """A browser socket closed. With none left, `detached_at` starts the
        minute a closed tab is given (§4.5)."""

        def change(lease: dict[str, Any], now: datetime) -> dict[str, Any]:
            sockets = _sockets(lease)
            sockets.pop(socket_id, None)
            fields: dict[str, Any] = {"sockets": sockets}
            if not live_sockets({"sockets": sockets}, now):
                fields["detached_at"] = now
            return fields

        return self._fenced(holder, change)

    def phase(self, holder: Holder, name: str, seconds: float) -> None:
        """One start phase's duration (§4.2 step 5), under the generation."""

        def change(lease: dict[str, Any], _now: datetime) -> dict[str, Any]:
            phases = dict(lease.get("start_phases") or {})
            phases[name] = round(float(seconds), 3)
            return {"start_phases": phases}

        self._fenced(holder, change)

    def ready(self, holder: Holder, started: Started | None) -> None:
        """The server answered ready: the lease runs, and Settings record what
        the machine now carries (§11.4)."""
        self._fenced(holder, lambda _lease, now: {"state": RUNNING, "ready_at": now,
                                                  "activity_at": now})
        if self._applied is not None and started is not None and started.applied:
            try:
                self._applied(started.applied, started.image)
            except Exception as exc:  # noqa: BLE001 -- the session runs; pending stays shown
                log.warning("speech settings could not be marked applied (%s)", type(exc).__name__)

    # -- the start (§4.2)

    def _heartbeat_while_starting(self, holder: Holder, last: list[float]) -> None:
        if self._clock() - last[0] >= HEARTBEAT_SECONDS:
            self._fenced(holder, lambda _lease, now: {"heartbeat_at": now})
            last[0] = self._clock()

    def _release(self, holder: Holder, cause: str) -> None:
        try:
            self._fenced(holder, lambda _lease, now: {"state": STOPPED, "stopped_at": now,
                                                      "cause": cause})
        except StaleLease:
            pass

    def _wait_status(self, holder: Holder, zone: str, want: frozenset[str], deadline: float,
                     beat: list[float]) -> Instance | None:
        while True:
            instance = self._compute.get(zone)
            if instance is not None and instance.status in want:
                return instance
            if self._clock() >= deadline:
                return None
            self._heartbeat_while_starting(holder, beat)
            self._sleep(POLL_SECONDS)

    def _wait(self, holder: Holder, deadline: float, beat: list[float]) -> Wait:
        """What is left of the start's budget for one Compute operation, with
        the lease's heartbeat between the wait's pieces."""
        return Wait(seconds=max(0.0, deadline - self._clock()),
                    tick=lambda: self._heartbeat_while_starting(holder, beat))

    def _ran_out(self, tried: list[str], during: str = "") -> MachineNotReady:
        untried = [z for z in self.config.zones if z not in tried]
        message = "the four-minute start ran out"
        if during:
            message += f" waiting for {during}"
        if tried:
            message += f"; tried {', '.join(tried)}"
        if untried:
            message += f"; {', '.join(untried)} not tried"
        return MachineNotReady(message, zone=tried[-1] if tried else "")

    def _apply(self, instance: Instance, chosen: Mapping[str, str], wait: Callable[[], Wait]) -> bool:
        """Settings onto the stopped machine; True when the image changed."""
        machine = chosen.get("machine") or cat.DEFAULTS["machine"]
        if cat.machine(machine) is not None and instance.machine_type != machine:
            self._compute.set_machine_type(instance.zone, machine, wait=wait())
        wanted = wanted_metadata(chosen, self.config.image)
        changed = {k: v for k, v in wanted.items() if instance.metadata.get(k) != v}
        if changed:
            self._compute.set_metadata(instance.zone, {**instance.metadata, **changed},
                                       instance.fingerprint, wait=wait())
        return META_IMAGE in changed

    def find(self, wait: Callable[[], Wait | None] = lambda: None) -> Instance | None:
        """The one speech machine across the configured zones, or None. Two
        (a crash between a move's delete and its create) are made one: the
        running one is kept, else the first in zone order; the other is
        deleted, and the deletion waited for."""
        found = [i for i in (self._compute.get(z) for z in self.config.zones) if i is not None]
        if len(found) <= 1:
            return found[0] if found else None
        keep = next((i for i in found if i.status == INSTANCE_RUNNING), found[0])
        for other in found:
            if other is not keep:
                log.warning("speech machine found in %s and %s; deleting the one in %s",
                            keep.zone, other.zone, other.zone)
                self._compute.delete(other.zone, wait=wait())
        return keep

    def _record_stockout(self, holder: Holder, session: SessionRef, zone: str) -> None:
        def change(lease: dict[str, Any], now: datetime) -> dict[str, Any]:
            return {"stockouts": [*(lease.get("stockouts") or []), {"zone": zone, "at": now}]}

        self._fenced(holder, change)
        self._audit(None, AUDIT_ZONE_STOCKOUT, asdict(session), {
            "zone": zone, "generation": holder.generation})
        log.info("speech machine: no L4 free in %s (generation %s)", zone, holder.generation)

    def _use_zone(self, holder: Holder, zone: str) -> None:
        self._fenced(holder, lambda _lease, _now: {"zone": zone})

    def _bring_up(self, holder: Holder, session: SessionRef, chosen: Mapping[str, str],
                  deadline: float, beat: list[float], tried: list[str]) -> tuple[str, bool]:
        """Start the stopped machine, or create it, zone by zone (the module's
        START); (the zone it is in, whether its image changed). `NoFreeL4`
        after the last zone; `MachineNotReady` when the budget ends first,
        naming the zones tried and not tried. Every operation is waited on
        inside what is left of the budget, the heartbeat beating meanwhile."""
        zones = list(self.config.zones)

        def wait() -> Wait:
            return self._wait(holder, deadline, beat)

        try:
            return self._bring_up_in_order(holder, session, chosen, deadline, beat, tried,
                                           zones, wait)
        except ComputeError as exc:
            if exc.code != TIMEOUT:
                raise
            raise self._ran_out(tried, "compute engine") from None

    def _bring_up_in_order(self, holder: Holder, session: SessionRef, chosen: Mapping[str, str],
                           deadline: float, beat: list[float], tried: list[str],
                           zones: list[str], wait: Callable[[], Wait]) -> tuple[str, bool]:
        instance = self.find(wait)
        if instance is not None and instance.status == INSTANCE_STOPPING:
            instance = self._wait_status(holder, instance.zone, AT_REST, deadline, beat) or instance
        if instance is not None and instance.status not in AT_REST:
            # Already RUNNING (or booting): it serves as it is.
            self._use_zone(holder, instance.zone)
            return instance.zone, False
        order = zones
        if instance is not None:
            own = instance.zone
            self._use_zone(holder, own)
            image_changed = self._apply(instance, chosen, wait)
            tried.append(own)
            try:
                self._compute.start(own, wait=wait())
                return own, image_changed
            except ComputeError as exc:
                if exc.code not in STOCKOUT_CODES:
                    raise
            self._record_stockout(holder, session, own)
            self._heartbeat_while_starting(holder, beat)
            if self._clock() >= deadline:
                # The machine stays stopped in its zone; the next Start
                # tries it there first.
                raise self._ran_out(tried)
            # Its zone has no L4: the stopped machine leaves it, and the
            # zones after its own are tried, wrapping round once.
            self._compute.delete(own, wait=wait())
            if own in zones:
                at = zones.index(own)
                order = zones[at + 1:] + zones[:at]
        machine = chosen.get("machine") or cat.DEFAULTS["machine"]
        if cat.machine(machine) is None:
            machine = cat.DEFAULTS["machine"]
        metadata = wanted_metadata(chosen, self.config.image)
        for zone in order:
            self._heartbeat_while_starting(holder, beat)
            if self._clock() >= deadline:
                raise self._ran_out(tried)
            self._use_zone(holder, zone)
            tried.append(zone)
            try:
                self._compute.create(zone, machine_type=machine, metadata=metadata, wait=wait())
                return zone, bool(self.config.image)
            except ComputeError as exc:
                if exc.code not in STOCKOUT_CODES:
                    raise
            self._record_stockout(holder, session, zone)
        raise NoFreeL4(f"No L4 free in {', '.join(tried)}.", zone=tried[-1] if tried else "")

    def start(self, session: SessionRef, *, chosen: Mapping[str, str]) -> Started:
        """Take the lease and bring the machine to RUNNING in the first zone
        with a free L4; `NoFreeL4` or `MachineNotReady` (the lease released)
        when it cannot."""
        idle = idle_seconds_of(chosen)
        holder = self.take(session, idle_seconds=idle,
                           machine=str(chosen.get("machine") or ""))
        began = self._clock()
        deadline = began + READY_SECONDS
        beat = [began]
        tried: list[str] = []
        try:
            zone, image_changed = self._bring_up(holder, session, chosen, deadline, beat, tried)
            if self._wait_status(holder, zone, frozenset({INSTANCE_RUNNING}), deadline, beat) is None:
                raise MachineNotReady(f"the speech machine was not running in time in {zone}",
                                      zone=zone)
            boot = self._clock() - began
            applied = {f: str(chosen.get(f) or "") for f in APPLIED_FIELDS}
            if not applied["zone"]:
                applied["zone"] = self.config.zone

            def record(lease: dict[str, Any], _now: datetime) -> dict[str, Any]:
                phases = dict(lease.get("start_phases") or {})
                phases["start.boot"] = round(boot, 3)
                # On the lease, so whichever swarm-api instance the page's audio
                # socket reaches can mark Settings applied at readiness.
                return {"start_phases": phases, "image_changed": image_changed,
                        "applied": applied, "image": self.config.image}

            self._fenced(holder, record)
        except NoFreeL4 as exc:
            self._release(holder, exc.reason)
            self._audit(None, AUDIT_NO_L4, asdict(session), {
                "zone": exc.zone, "zones": list(tried), "generation": holder.generation})
            raise
        except (MachineNotReady, ComputeError) as exc:
            log.warning("speech machine start failed: %s", exc)
            self.stop(holder, cause=FALLBACK_NOT_READY)
            if isinstance(exc, MachineNotReady):
                raise
            raise MachineNotReady("the speech machine could not be started",
                                  zone=tried[-1] if tried else self.config.zone) from None
        except StaleLease:
            # The lease was taken from this start (the sweep, or another
            # session's take): the machine is no longer this holder's to stop
            # or to write about, and the session falls back to text.
            log.warning("speech machine start lost its lease (generation %s)", holder.generation)
            raise MachineNotReady("the speech machine's lease was lost while it started",
                                  zone=tried[-1] if tried else self.config.zone) from None
        log.info("speech machine started in %s generation=%s", zone, holder.generation)
        return Started(holder=holder, image_changed=image_changed, applied=applied,
                       image=self.config.image, boot_seconds=boot, idle_seconds=idle)

    @staticmethod
    def started_of(lease: Mapping[str, Any]) -> Started | None:
        """The start's record from the lease, while the server has not yet
        been seen ready; None afterwards (a rotation, a re-attach)."""
        if lease.get("ready_at") is not None or lease.get("state") != STARTING:
            return None
        applied = lease.get("applied")
        return Started(
            holder=Holder(session_id=str(lease.get("session_id")),
                          generation=int(lease.get("generation") or 0)),
            image_changed=lease.get("image_changed") is True,
            applied=dict(applied) if isinstance(applied, Mapping) else {},
            image=str(lease.get("image") or ""),
            boot_seconds=float((lease.get("start_phases") or {}).get("start.boot") or 0.0),
            idle_seconds=int(lease.get("idle_seconds") or DEFAULT_IDLE_SECONDS))

    # -- the stop (§4.5)

    def stop(self, holder: Holder, *, cause: str) -> bool:
        """Stop the machine for this holder: False, touching nothing, when the
        generation is stale or the lease already stopped."""
        try:
            lease = self._fenced(holder, lambda _lease, _now: {"state": STOPPING, "cause": cause})
        except StaleLease:
            log.info("speech stop refused: generation %s is stale", holder.generation)
            return False
        self._stop_instance(str(lease.get("zone") or self.config.zone))
        now = self._now()
        started = _aware(lease.get("started_at")) or now
        minutes = round(max(0.0, (now - started).total_seconds()) / 60.0, 3)
        machine = cat.machine(str(lease.get("machine") or "")) or cat.machine(cat.DEFAULTS["machine"])
        cost = round(minutes / 60.0 * machine.usd_per_hour, 4) if machine else None
        try:
            self._fenced(holder, lambda _lease, at: {"state": STOPPED, "stopped_at": at,
                                                     "minutes": minutes}, holding=False)
        except StaleLease:
            pass
        self._audit(None, AUDIT_STOPPED, lease, {
            "cause": cause, "generation": holder.generation, "minutes": minutes,
            "usd": cost, "zone": lease.get("zone")})
        log.info("speech machine stopped generation=%s cause=%s minutes=%s",
                 holder.generation, cause, minutes)
        return True

    def _stop_instance(self, zone: str) -> bool:
        try:
            self._compute.stop(zone)
            return True
        except ComputeError as exc:
            # The sweep retries; the VM's own watchdog powers it off after ten
            # minutes with no socket (§4.5 stop 2).
            log.warning("speech machine stop failed (%s)", exc.code)
            return False

    def holder_of(self, session_id: str) -> Holder | None:
        lease = self.read()
        if not lease or lease.get("session_id") != session_id or lease.get("state") not in HOLDING:
            return None
        return Holder(session_id=session_id, generation=int(lease.get("generation") or 0))

    def end_session(self, holder: Holder, *, cause: str, ender: SessionEnder | None) -> bool:
        """End the session this holder serves and stop the machine, fenced."""
        lease = self.read() or {}
        if lease.get("generation") != holder.generation or lease.get("session_id") != holder.session_id \
                or lease.get("state") not in HOLDING:
            return False
        if ender is not None:
            try:
                ender.end(lease, cause)
            except Exception as exc:  # noqa: BLE001 -- the machine still stops
                log.warning("speech session end failed (%s)", type(exc).__name__)
        return self.stop(holder, cause=cause)

    def fall_back(self, session: SessionRef, *, reason: str) -> None:
        """§4.1: the session is not re-opened; it becomes a text session."""
        ref = (self._db.collection(vs.PROJECTS).document(session.project_id)
               .collection(vs.SESSIONS).document(session.session_id))
        ref.update({"mode": vs.MODE_TEXT,
                    "fallback": {"from": "own", "reason": reason, "at": self._now()}})

    # -- the sweep (§4.5 stop 1)

    def sweep(self, ender: SessionEnder | None) -> dict[str, Any]:
        now = self._now()
        lease = self.read()
        report: dict[str, Any] = {"lease": (lease or {}).get("state"), "ended": None,
                                  "stopped": False, "instance": None}
        cause = due_cause(lease, now)
        if lease and cause is not None:
            holder = Holder(session_id=str(lease.get("session_id")),
                            generation=int(lease.get("generation") or 0))
            report["ended"] = cause
            report["stopped"] = self.end_session(holder, cause=cause, ender=ender)
            return report
        try:
            found = [i for i in (self._compute.get(z) for z in self.config.zones) if i is not None]
        except ComputeError as exc:
            report["instance"] = exc.code
            return report
        report["instance"] = found[0].status if found else None
        if holds(lease, now):
            return report
        # No live lease: every machine in a listed zone that is not at rest
        # is stopped (there is one; a crash mid-move may have left two).
        for instance in found:
            if instance.status not in AT_REST and instance.status != INSTANCE_STOPPING:
                report["instance"] = instance.status
                report["stopped"] = self._stop_instance(instance.zone) or report["stopped"]
        if report["stopped"] and lease and lease.get("state") == STOPPING:
            self._ref.update({"state": STOPPED, "stopped_at": now})
        return report


def stockouts(db: Any, *, tenant_id: str | None, session_ids: list[str] | None = None,
              start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    """The starts refused for want of an L4 in every zone: the AUDIT_NO_L4 rows
    `start` wrote, as {session_id, tenant_id, at}, under one tenant or (None)
    every tenant; those of `session_ids`, else those written in [start, end).
    Equality or a range on `at` alone, filtered here, so no composite index.
    A read that fails raises."""
    if session_ids is not None:
        queries = [db.collection(vs.AUDIT)
                   .where(filter=FieldFilter("session_id", "in", session_ids[i:i + 30]))
                   .where(filter=FieldFilter("action", "==", AUDIT_NO_L4))
                   for i in range(0, len(session_ids), 30)]
    else:
        queries = [db.collection(vs.AUDIT)
                   .where(filter=FieldFilter("at", ">=", start))
                   .where(filter=FieldFilter("at", "<", end))]
    rows = []
    for query in queries:
        for snap in query.stream():
            row = snap.to_dict() or {}
            at = _aware(row.get("at"))
            if row.get("action") != AUDIT_NO_L4 or at is None \
                    or (tenant_id is not None and row.get("tenant_id") != tenant_id):
                continue
            rows.append({"session_id": row.get("session_id"),
                         "tenant_id": row.get("tenant_id"), "at": at})
    return rows


def new_socket_id() -> str:
    return "sk_" + secrets.token_hex(8)

