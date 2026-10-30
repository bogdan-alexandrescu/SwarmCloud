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

from .state import InstallState, refuse_secrets

COLLECTION = "swarm-install"
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
            }
        ),
    ),
    "swarm-install/{env}/console/github_app": (
        SWARM_API,
        frozenset(
            {"state_hash", "id", "slug", "client_id", "installed_on", "secrets_written_at", "self_check"}
        ),
    ),
    # Fields keyed per account label: owner, link_hash, link_expires, signed_in_at.
    "swarm-install/{env}/console/accounts": (SWARM_API, frozenset()),
    "swarm-install/{env}/console/teams/{id}": (
        SWARM_API,
        frozenset({"group", "workspace", "first_admin", "invites", "progress", "smoke", "grant"}),
    ),
    "swarm-install/{env}/console/finished": (SWARM_API, frozenset({"at", "by"})),
    # The console's two verification runs: `verify_runs` is
    # {"console_task": <task id>, "console_workflow": <workflow id>}, copied
    # into the engine's `state.outputs["verify_runs"]` (DAYONE-SETUP-API-2).
    "swarm-install/{env}/console/verify_runs": (
        SWARM_API,
        frozenset({"verify_runs", "repository", "started_at", "by"}),
    ),
    "swarm-install/{env}/health/latest": (
        SWARM_API_FOR_DOCTOR,
        frozenset({"run_at", "checks", "findings"}),
    ),
}

#: Each stage's entry in `stages[0..15]`.
STAGE_FIELDS = frozenset({"state", "at", "by"})
VERIFY_FIELDS = frozenset(
    {"check", "done", "total", "started", "expected_end", "deadline", "result"}
)


def engine_document(env: str) -> str:
    return f"{COLLECTION}/{env}"


#: The console document holding the console's verification runs' ids.
VERIFY_RUNS = "verify_runs"


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

    def __init__(self, project: str, *, database: str = "(default)", client: Any = None) -> None:
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
