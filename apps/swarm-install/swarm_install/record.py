"""The install record in Firestore: one writer per document (plan §5.4, D26).

Firestore collection `swarm-install`, document `<env>`, and its
subcollections. **Every document has exactly one writer; a reader never
writes what it reads.** The engine writes `swarm-install/<env>` and nothing
else; it reads the console's documents (written by swarm-api) and copies what
it needs into its authoritative GCS state (`swarm_install.state`).

`WRITERS` is the table itself. SETUP-API-1's
`tests/unit/control_plane/test_install_record_writers.py` imports it and
asserts swarm-api writes exactly its rows, so the table lives once, here.

Within the one writer every write is conditional: a create, or an update with
an `update_time` precondition, so two CLIs cannot lose an update; on a lost
race the writer re-reads and writes again. The engine starts writing at stage
5, once Firestore exists (manifest `other.install-record-firestore`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .state import STEP_DONE, InstallState, refuse_secrets

COLLECTION = "swarm-install"
#: The Firestore database terraform/infra creates (`firestore_database`);
#: that variable refuses "(default)", so the record is never there (N1).
DATABASE = "swarm"
RECORD_SCHEMA_VERSION = 1

ENGINE = "engine"
SWARM_API = "swarm-api"
SWARM_API_FOR_DOCTOR = "swarm-api for swarm-doctor"

#: Document path template -> (its one writer, the fields it may hold).
WRITERS: dict[str, tuple[str, frozenset[str]]] = {
    "swarm-install/{env}": (
        ENGINE,
        frozenset(
            {
                "schema_version",
                "release",
                "region",
                "hostname",
                "stages",
                "lease",
                "verify",
                "hand_off_at",
                "ready_at",
                # The install's sizing (`state.outputs["sizing"]`, lane
                # SIZING-ASK) and the installer's email: Admin › Setup reads
                # them to serve the sizing and to know a one-person install
                # (decision 7) from a team's (DAYONE-SETUP-API-3).
                "sizing",
                "installer",
                # The first team's group stage 8 created (`identity.groups`,
                # `state.outputs["workspace_groups"]`), as `{"team": <email>}`:
                # Admin › Setup's first-team step starts from it rather than
                # asking (N20). None for a one-person install, which has none.
                "workspace_groups",
                # The Desktop client the engine's last bootstrap pass put in
                # IAP's programmatic-clients list, as `{"client_id": <id>}`:
                # Admin › Setup's Sign-in client step reads `applied` from it
                # (issue #2964). None until one is.
                "sign_in_client",
                # When each pass Admin › Setup times began and, once it has,
                # ended (`TIMED_PASSES`, `state.outputs["pass_times"]`): the
                # page shows how long a running pass has taken, since no
                # usual length is measured for either yet.
                "passes",
            }
        ),
    ),
    "swarm-install/{env}/console/github_app": (
        SWARM_API,
        frozenset(
            {"state_hash", "id", "slug", "client_id", "name", "installed_on", "secrets_written_at", "self_check"}
        ),
    ),
    # Fields keyed per account label: owner, link_hash, link_made_at, link_expires, signed_in_at.
    "swarm-install/{env}/console/accounts": (SWARM_API, frozenset()),
    # A collection under the env document: `console/teams/<id>` has five
    # segments, which real Firestore refuses as a document (audit 3, A2).
    "swarm-install/{env}/teams/{id}": (
        SWARM_API,
        frozenset({"group", "workspace", "first_admin", "invites", "progress", "smoke", "grant"}),
    ),
    "swarm-install/{env}/console/finished": (SWARM_API, frozenset({"at", "by"})),
    # A one-person install's first workspace: the installer's personal
    # workspace, its grant and its smoke (decision 7, DAYONE-SETUP-API-3).
    "swarm-install/{env}/console/personal": (
        SWARM_API,
        frozenset({"owner", "workspace", "progress", "grant", "smoke"}),
    ),
    # The console's two verification runs: `verify_runs` is
    # {"console_task": <task id>, "console_workflow": <workflow id>}, copied
    # into the engine's `state.outputs["verify_runs"]` (DAYONE-SETUP-API-2);
    # `retries` is {"console_task": n, "console_workflow": n}, each key's
    # fresh starts (DAYONE-SETUP-API-6).
    "swarm-install/{env}/console/verify_runs": (
        SWARM_API,
        frozenset({"verify_runs", "retries", "repository", "started_at", "by"}),
    ),
    # Admin › Setup's Sign-in client step (issue #2964): the Desktop client's
    # id, when and by whom the console saved it; its secret is in Secret
    # Manager only. The engine's hand-off wait reads it.
    "swarm-install/{env}/console/sign_in_client": (
        SWARM_API,
        frozenset({"client_id", "saved_at", "saved_by"}),
    ),
    "swarm-install/{env}/health/latest": (
        SWARM_API_FOR_DOCTOR,
        frozenset({"run_at", "checks", "findings"}),
    ),
}

#: Each stage's entry in `stages[0..15]`.
STAGE_FIELDS = frozenset({"state", "at", "by"})
#: `detail` is verify's one-line cause, present only while `result` is
#: "failed": the terminal prints it, and Admin › Setup serves it as
#: `engine.verify.detail` so the console's verify step says why too.
VERIFY_FIELDS = frozenset(
    {"check", "done", "total", "started", "expected_end", "deadline", "result", "detail"}
)

#: The passes Admin › Setup times, each by its steps in the order they run:
#: the sign-in pass (bootstrap pass 2, then the latest infra pass, with the
#: saved Desktop client) and infra pass 3 (the GitHub App's ids). Neither has a
#: measured usual length, so the page shows the time since `started_at`.
TIMED_PASSES: dict[str, tuple[str, ...]] = {
    "sign_in_pass": ("handoff.sign-in-bootstrap", "handoff.sign-in-infra"),
    "infra_pass_3": ("github.pass-3",),
}
#: The `state.outputs` key the engine keeps the passes' times under.
PASS_TIMES = "pass_times"
#: Each pass's entry in `passes`: `started_at` when its first step began
#: applying, then `ended_at` and `result` (the step's recorded state: done,
#: failed or paused) once its last step is done or any step of it stops.
PASS_FIELDS = frozenset({"started_at", "ended_at", "result"})


def pass_of(step_id: str) -> str | None:
    """The timed pass a step belongs to, or None."""
    for name, steps in TIMED_PASSES.items():
        if step_id in steps:
            return name
    return None


def pass_started(times: Mapping[str, Any], step_id: str, now: float) -> dict[str, Any] | None:
    """`times` with the step's pass started at `now`, or None when nothing
    changes: the pass is already running (its first step is done and a later
    one applies) or the step is in no timed pass. A pass's first step, or a
    pass with no open start (never started, or ended), starts the clock again."""
    name = pass_of(step_id)
    if name is None:
        return None
    current = times.get(name) if isinstance(times.get(name), Mapping) else {}
    running = bool(current.get("started_at")) and "ended_at" not in current
    if running and step_id != TIMED_PASSES[name][0]:
        return None
    return {**times, name: {"started_at": now}}


def pass_ended(times: Mapping[str, Any], step_id: str, state: str, now: float) -> dict[str, Any] | None:
    """`times` with the step's pass ended at `now` as `state`, or None when
    nothing changes: no open start, or a step that is not the pass's last
    recorded done (the pass runs on)."""
    name = pass_of(step_id)
    if name is None:
        return None
    current = times.get(name) if isinstance(times.get(name), Mapping) else {}
    if not current.get("started_at") or "ended_at" in current:
        return None
    if state == STEP_DONE and step_id != TIMED_PASSES[name][-1]:
        return None
    return {**times, name: {"started_at": current["started_at"], "ended_at": now, "result": state}}


def _passes(value: Any) -> dict[str, dict[str, Any]] | None:
    """`passes` from `state.outputs["pass_times"]`: only the timed passes and
    their known fields; None before any of them started."""
    if not isinstance(value, Mapping):
        return None
    passes = {name: {key: entry[key] for key in PASS_FIELDS if key in entry}
              for name, entry in value.items() if name in TIMED_PASSES and isinstance(entry, Mapping)}
    return passes or None


def engine_document(env: str) -> str:
    return f"{COLLECTION}/{env}"


#: The console document holding the console's verification runs' ids.
VERIFY_RUNS = "verify_runs"
#: The console document holding a one-person install's personal workspace.
PERSONAL = "personal"
#: The console document holding the Desktop client the console's Sign-in
#: client step saved.
SIGN_IN_CLIENT = "sign_in_client"


def console_document(env: str, name: str) -> str:
    return f"{COLLECTION}/{env}/console/{name}"


class RecordConflict(RuntimeError):
    """The engine's document changed between its read and its write."""


class DocumentStore(Protocol):
    """The Firestore calls the record makes, so tests run on a fake.

    `get` returns `(data, update_time)` or `None`. `create` fails with
    `RecordConflict` when the document exists; `update` replaces the given
    top-level fields only when the document's update time is still
    `update_time`, else `RecordConflict`.
    """

    def get(self, path: str) -> tuple[dict[str, Any], Any] | None: ...

    def create(self, path: str, data: dict[str, Any]) -> None: ...

    def update(self, path: str, data: dict[str, Any], *, update_time: Any) -> None: ...


class FirestoreDocuments:
    """`DocumentStore` on a real Firestore database."""

    def __init__(self, project: str, *, database: str = DATABASE, client: Any = None) -> None:
        if client is None:
            from google.cloud import firestore  # lazy: no client at import time

            client = firestore.Client(project=project, database=database)
        self._client = client

    def get(self, path: str) -> tuple[dict[str, Any], Any] | None:
        snapshot = self._client.document(path).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}, snapshot.update_time

    def create(self, path: str, data: dict[str, Any]) -> None:
        from google.api_core import exceptions

        try:
            self._client.document(path).create(data)
        except exceptions.AlreadyExists as exc:
            raise RecordConflict(path) from exc

    def update(self, path: str, data: dict[str, Any], *, update_time: Any) -> None:
        from google.api_core import exceptions

        option = self._client.write_option(last_update_time=update_time)
        try:
            self._client.document(path).update(data, option=option)
        except (exceptions.FailedPrecondition, exceptions.NotFound) as exc:
            raise RecordConflict(path) from exc


def _sizing(value: Any) -> dict[str, Any] | None:
    """`state.outputs["sizing"]`, copied whole; None until stage 0 wrote it."""
    return dict(value) if isinstance(value, Mapping) else None


def _workspace_groups(value: Any) -> dict[str, str] | None:
    """The first team's group from `state.outputs["workspace_groups"]`; None
    before stage 8 recorded it, or with no team group (one person)."""
    team = str(value.get("team") or "") if isinstance(value, Mapping) else ""
    return {"team": team} if team else None


def _sign_in_client(value: Any) -> dict[str, str] | None:
    client = str(value or "")
    return {"client_id": client} if client else None


def open_record(project: str, env: str) -> InstallRecord:
    """The install record of `env` in `project`'s swarm database, the one
    place a verb builds it (no Firestore client before it is called)."""
    return InstallRecord(env, FirestoreDocuments(project))


def stages_of(state: InstallState, stage_of_step: Mapping[str, int]) -> list[dict[str, Any] | None]:
    """`stages[0..15]`: each stage's latest step record, as `{state, at, by}`."""
    stages: list[dict[str, Any] | None] = [None] * 16
    for step_id, record in state.steps.items():
        stage = stage_of_step.get(step_id)
        if stage is None:
            continue
        current = stages[stage]
        if current is None or record.at >= current["at"]:
            stages[stage] = {"state": record.state, "at": record.at, "by": record.by}
    return stages


@dataclass
class InstallRecord:
    """The engine's side of the record: writes `swarm-install/<env>`, reads the rest."""

    env: str
    documents: DocumentStore

    def document_for(
        self,
        state: InstallState,
        stage_of_step: Mapping[str, int],
        *,
        lease: Mapping[str, Any] | None = None,
        verify: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "release": state.release,
            "region": state.region,
            "hostname": state.hostname,
            "stages": stages_of(state, stage_of_step),
            "lease": dict(lease) if lease is not None else None,
            "hand_off_at": state.hand_off_at,
            "ready_at": state.ready_at,
            "sizing": _sizing(state.outputs.get("sizing")),
            "installer": str(state.outputs.get("installer") or "") or None,
            "workspace_groups": _workspace_groups(state.outputs.get("workspace_groups")),
            "sign_in_client": _sign_in_client(state.outputs.get("iap_programmatic_client")),
            "passes": _passes(state.outputs.get(PASS_TIMES)),
        }
        if verify is not None:
            unknown = set(verify) - VERIFY_FIELDS
            if unknown:
                raise ValueError(f"verify fields not in the record's schema: {sorted(unknown)}")
            data["verify"] = dict(verify)
        refuse_secrets("record", data)
        return data

    def write(
        self,
        state: InstallState,
        stage_of_step: Mapping[str, int],
        *,
        lease: Mapping[str, Any] | None = None,
        verify: Mapping[str, Any] | None = None,
        attempts: int = 5,
    ) -> None:
        path = engine_document(self.env)
        data = self.document_for(state, stage_of_step, lease=lease, verify=verify)
        for _ in range(attempts):
            current = self.documents.get(path)
            try:
                if current is None:
                    self.documents.create(path, data)
                else:
                    self.documents.update(path, data, update_time=current[1])
                return
            except RecordConflict:
                continue
        raise RecordConflict(f"{path} kept moving; gave up after {attempts} attempts")

    def read_console(self, name: str) -> dict[str, Any] | None:
        """A console document (written by swarm-api); the engine never writes it."""
        found = self.documents.get(console_document(self.env, name))
        return None if found is None else found[0]
