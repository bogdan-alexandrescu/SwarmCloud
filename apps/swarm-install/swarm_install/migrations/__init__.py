"""Numbered, idempotent Firestore migrations, run by `swarm upgrade`
(docs/design/day-one-plan.md §9.3, lane DAYONE-UPGRADE-1; manifest
`other.firestore-migrations`).

Each module `mNNNN_<name>.py` defines `MIGRATION`, whose `number` is its
`NNNN`. The numbers run 1, 2, 3, ... with no gap, so a release can never skip
one. A migration says which slot of the upgrade's order it runs in:

| Phase | When (§9.3 step 6) | Why |
|---|---|---|
| `additive` | after the image copy, before bootstrap | it adds or closes data the running code already tolerates |
| `needs-new-code` | after infra rolled the new code out | the old code would write the old shape again behind it |
| `removes-data` | only after the platform checks are green | a failed image roll is rolled back, and data it removed could not be |

**Idempotent.** `pending(db)` names what is still unmigrated; `apply(db)`
changes only that, each write conditional on the document's update time, so a
second run finds nothing and writes nothing.

**Recorded once.** A migration that ran is recorded in the install state's
`outputs["firestore_migrations"]["NNNN"]` (`{name, at, release, changed}`), and
is never run again; `outputs["firestore_schema"]` is the highest number below
which every migration is recorded. A migration whose `pending` is not empty
after its `apply` is NOT recorded: the upgrade stops on it and the next
`swarm upgrade` resumes there. On a fresh install every migration finds
nothing to do and only records its number.

The record lives in the install state (GCS), not in a new Firestore document:
the install record's one-writer table (`record.WRITERS`) gives the engine
exactly `swarm-install/<env>`.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

ADDITIVE = "additive"
NEEDS_NEW_CODE = "needs-new-code"
REMOVES_DATA = "removes-data"
PHASES = (ADDITIVE, NEEDS_NEW_CODE, REMOVES_DATA)

#: Where the install state records the migrations that ran.
STATE_KEY = "firestore_migrations"
SCHEMA_KEY = "firestore_schema"

_MODULE = re.compile(r"^m(\d{4})_[a-z0-9_]+$")


class MigrationConflict(RuntimeError):
    """A document changed between its read and the migration's write."""


class MigrationFailed(RuntimeError):
    """A migration left documents unmigrated; it is not recorded."""

    def __init__(self, number: int, name: str, why: str) -> None:
        super().__init__(f"migration {number:04d} {name}: {why}")
        self.number = number
        self.name = name


class Documents(Protocol):
    """The Firestore calls a migration makes, so tests run on a fake.

    `list` yields `(path, data, update_time)` for every document of a
    top-level collection; `get` returns `(data, update_time)` or None;
    `update` sets the given fields (dotted paths reach into maps) only while
    the document's update time is still `update_time`, else
    `MigrationConflict`.
    """

    def list(self, collection: str) -> Iterable[tuple[str, dict[str, Any], Any]]: ...

    def get(self, path: str) -> tuple[dict[str, Any], Any] | None: ...

    def update(self, path: str, fields: dict[str, Any], *, update_time: Any) -> None: ...


@dataclass(frozen=True)
class Outcome:
    changed: int
    refused: int = 0
    detail: str = ""


class Migration(Protocol):
    number: int
    name: str
    phase: str
    explain: str

    def pending(self, db: Documents) -> list[str]: ...

    def apply(self, db: Documents) -> Outcome: ...


class FirestoreMigrationDocuments:
    """`Documents` on a real Firestore database (the client is built on first use)."""

    def __init__(self, project: str, *, database: str = "(default)", client: Any = None) -> None:
        self._project = project
        self._database = database
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore  # lazy: no client at import time

            self._client = firestore.Client(project=self._project, database=self._database)
        return self._client

    def list(self, collection: str) -> Iterable[tuple[str, dict[str, Any], Any]]:
        for snapshot in self.client.collection(collection).stream():
            yield f"{collection}/{snapshot.id}", snapshot.to_dict() or {}, snapshot.update_time

    def get(self, path: str) -> tuple[dict[str, Any], Any] | None:
        snapshot = self.client.document(path).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}, snapshot.update_time

    def update(self, path: str, fields: dict[str, Any], *, update_time: Any) -> None:
        from google.api_core import exceptions

        option = self.client.write_option(last_update_time=update_time)
        try:
            self.client.document(path).update(fields, option=option)
        except (exceptions.FailedPrecondition, exceptions.NotFound) as exc:
            raise MigrationConflict(path) from exc


def discover(package: str = __name__) -> list[Migration]:
    """Every migration of `package`, by number; refused unless 1..N with no gap."""
    root = importlib.import_module(package)
    found: list[Migration] = []
    for info in sorted(pkgutil.iter_modules(root.__path__), key=lambda i: i.name):
        match = _MODULE.match(info.name)
        if match is None:
            continue
        migration = importlib.import_module(f"{package}.{info.name}").MIGRATION
        if migration.number != int(match.group(1)):
            raise ValueError(f"{info.name} declares migration {migration.number}")
        if migration.phase not in PHASES:
            raise ValueError(f"{info.name}: phase {migration.phase!r} is not one of {PHASES}")
        found.append(migration)
    numbers = [m.number for m in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"the migrations are numbered {numbers}; they run 1..N with no gap")
    return found


def key(number: int) -> str:
    return f"{number:04d}"


def applied(outputs: Mapping[str, Any]) -> dict[str, Any]:
    return dict(outputs.get(STATE_KEY) or {})


def schema_of(recorded: Mapping[str, Any]) -> int:
    """The highest number below which every migration is recorded."""
    number = 0
    while key(number + 1) in recorded:
        number += 1
    return number


def run_phase(
    db: Documents,
    migrations: Iterable[Migration],
    phase: str,
    *,
    outputs: Mapping[str, Any],
    record: Callable[[Migration, Outcome], None],
    say: Callable[[str], None] = lambda line: None,
) -> list[int]:
    """Run every unrecorded migration of `phase`, in number order; the numbers run.

    Each is recorded (through `record`) only once its `pending` is empty after
    its `apply`; otherwise `MigrationFailed`, and nothing later runs.
    """
    if phase not in PHASES:
        raise ValueError(f"phase {phase!r} is not one of {PHASES}")
    done = applied(outputs)
    ran: list[int] = []
    for migration in sorted(migrations, key=lambda m: m.number):
        if migration.phase != phase or key(migration.number) in done:
            continue
        outcome = migration.apply(db)
        left = migration.pending(db)
        if left:
            raise MigrationFailed(
                migration.number, migration.name,
                f"{len(left)} documents are still unmigrated ({', '.join(left[:5])}"
                f"{', ...' if len(left) > 5 else ''}); {outcome.refused} changed since they were read. "
                "Nothing is recorded; run swarm upgrade again to resume here",
            )
        record(migration, outcome)
        say(f"migration {key(migration.number)} {migration.name}: {outcome.changed} changed"
            + (f" ({outcome.detail})" if outcome.detail else ""))
        ran.append(migration.number)
    return ran


def record_into(state_outputs: dict[str, Any], migration: Migration, outcome: Outcome, *,
                at: float, release: str) -> None:
    """Write one migration's record, once: an existing record is kept as it is."""
    recorded = dict(state_outputs.get(STATE_KEY) or {})
    if key(migration.number) not in recorded:
        recorded[key(migration.number)] = {"name": migration.name, "at": at, "release": release,
                                           "changed": outcome.changed}
    state_outputs[STATE_KEY] = recorded
    state_outputs[SCHEMA_KEY] = schema_of(recorded)


__all__ = [
    "ADDITIVE", "Documents", "FirestoreMigrationDocuments", "Migration", "MigrationConflict", "MigrationFailed",
    "NEEDS_NEW_CODE", "Outcome", "PHASES", "REMOVES_DATA", "SCHEMA_KEY", "STATE_KEY", "applied", "discover",
    "key", "record_into", "run_phase", "schema_of",
]
