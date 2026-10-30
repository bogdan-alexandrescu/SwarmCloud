"""The numbered Firestore migrations `swarm upgrade` runs (lane DAYONE-UPGRADE-1,
part of #2688): each is idempotent, each is recorded once, and the schema
number moves once per migration. An in-memory Firestore stands in for the
real one: no credentials, no emulator.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from swarm_install import migrations as mig
from swarm_install.migrations import m0001_workspace_record, m0002_index_interval, m0003_attempt_ends


class MemoryDocuments:
    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = copy.deepcopy(data)
        self.times = {path: 1 for path in self.data}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.moved_before_write: set[str] = set()

    def list(self, collection: str):
        for path in sorted(self.data):
            if path.count("/") == 1 and path.startswith(collection + "/"):
                yield path, copy.deepcopy(self.data[path]), self.times[path]

    def get(self, path: str):
        return (copy.deepcopy(self.data[path]), self.times[path]) if path in self.data else None

    def update(self, path: str, fields: dict[str, Any], *, update_time: Any) -> None:
        if path in self.moved_before_write:
            self.moved_before_write.discard(path)
            self.times[path] += 1
            raise mig.MigrationConflict(path)
        if self.times.get(path) != update_time:
            raise mig.MigrationConflict(path)
        doc = self.data[path]
        for dotted, value in fields.items():
            target = doc
            *parents, leaf = dotted.split(".")
            for name in parents:
                target = target.setdefault(name, {})
            target[leaf] = value
        self.times[path] += 1
        self.writes.append((path, dict(fields)))


SEED = {
    "tenants/eng": {"kind": "team"},
    "workspaces/eng": {"state": "ready"},
    "repositories/r1": {"index": {"min_change_interval_minutes": 30, "paused": False}},
    "repositories/r2": {"index": {"min_change_interval_minutes": 30.0}},
    "repositories/r3": {"index": {"min_change_interval_minutes": 120}},
    "repositories/r4": {"index": {}},
    "tasks/t1": {"current_generation": 3, "updated_at": "2026-10-05T10:00:00Z"},
    "tasks/t2": {"current_generation": 1, "updated_at": "2026-10-05T11:00:00Z"},
    "leases/l1": {"released_at": "2026-10-05T09:30:00Z"},
    # superseded, a later attempt exists: ends at the later attempt's creation
    "attempts/a1": {"task_id": "t1", "generation": 1, "completed_at": None, "error": None,
                    "created_at": "2026-10-05T08:00:00Z"},
    # superseded, the latest attempt below it is at generation 3
    "attempts/a2": {"task_id": "t1", "generation": 2, "completed_at": None, "error": "fenced: x",
                    "lease_id": "l1", "created_at": "2026-10-05T09:00:00Z"},
    "attempts/a3": {"task_id": "t1", "generation": 3, "completed_at": None,
                    "created_at": "2026-10-05T09:45:00Z"},
    # at its task's current generation: never touched
    "attempts/a4": {"task_id": "t2", "generation": 1, "completed_at": None, "created_at": "2026-10-05T10:30:00Z"},
    # task gone: left
    "attempts/a5": {"task_id": "gone", "generation": 1, "completed_at": None},
}


def test_discovered_in_order_with_no_gap() -> None:
    found = mig.discover()
    assert [(m.number, m.name) for m in found] == [(1, "workspace_record"), (2, "index_interval"),
                                                   (3, "attempt_ends")]
    assert {m.phase for m in found} <= set(mig.PHASES)


@pytest.mark.parametrize("migration", mig.discover(), ids=lambda m: m.name)
def test_each_migration_idempotent(migration: mig.Migration) -> None:
    db = MemoryDocuments(SEED)
    migration.apply(db)
    assert migration.pending(db) == []
    after_first = copy.deepcopy(db.data)
    writes = len(db.writes)
    second = migration.apply(db)
    assert second.changed == 0 and len(db.writes) == writes and db.data == after_first


def test_index_interval_moves_only_the_integer_30() -> None:
    db = MemoryDocuments(SEED)
    assert m0002_index_interval.MIGRATION.pending(db) == ["repositories/r1"]
    outcome = m0002_index_interval.MIGRATION.apply(db)
    assert outcome.changed == 1
    assert db.data["repositories/r1"]["index"] == {"min_change_interval_minutes": 120, "paused": False}
    assert db.data["repositories/r2"]["index"]["min_change_interval_minutes"] == 30.0


def test_attempt_ends_closes_only_superseded_attempts() -> None:
    db = MemoryDocuments(SEED)
    assert m0003_attempt_ends.MIGRATION.pending(db) == ["attempts/a1", "attempts/a2"]
    m0003_attempt_ends.MIGRATION.apply(db)
    assert db.data["attempts/a1"]["completed_at"] == "2026-10-05T09:00:00Z"
    assert db.data["attempts/a1"]["error"].startswith("superseded:")
    assert db.data["attempts/a2"]["completed_at"] == "2026-10-05T09:45:00Z"
    assert db.data["attempts/a2"]["error"] == "fenced: x"
    assert "exit_code" not in db.data["attempts/a1"]
    for untouched in ("attempts/a3", "attempts/a4", "attempts/a5"):
        assert db.data[untouched]["completed_at"] is None


def test_workspace_record_stops_on_a_tenant_without_one() -> None:
    db = MemoryDocuments({**SEED, "tenants/u-old": {"kind": "personal"}})
    with pytest.raises(mig.MigrationFailed, match="u-old"):
        m0001_workspace_record.MIGRATION.apply(db)
    assert db.writes == []


def test_schema_version_advanced_once() -> None:
    db = MemoryDocuments(SEED)
    outputs: dict[str, Any] = {}
    records: list[int] = []

    def record(migration: mig.Migration, outcome: mig.Outcome) -> None:
        records.append(migration.number)
        mig.record_into(outputs, migration, outcome, at=float(len(records)), release="2026.11.1")

    found = mig.discover()
    for phase in mig.PHASES:
        mig.run_phase(db, found, phase, outputs=outputs, record=record)
    assert sorted(records) == [1, 2, 3]
    assert outputs[mig.SCHEMA_KEY] == 3
    first = copy.deepcopy(outputs[mig.STATE_KEY])
    for phase in mig.PHASES:
        assert mig.run_phase(db, found, phase, outputs=outputs, record=record) == []
    assert sorted(records) == [1, 2, 3], "a recorded migration never runs again"
    mig.record_into(outputs, found[0], mig.Outcome(changed=9), at=99.0, release="later")
    assert outputs[mig.STATE_KEY] == first and outputs[mig.SCHEMA_KEY] == 3


def test_a_refused_write_is_not_recorded() -> None:
    db = MemoryDocuments(SEED)
    db.moved_before_write.add("repositories/r1")
    outputs: dict[str, Any] = {}
    with pytest.raises(mig.MigrationFailed, match="index_interval"):
        mig.run_phase(db, [m0002_index_interval.MIGRATION], mig.NEEDS_NEW_CODE, outputs=outputs,
                      record=lambda m, o: mig.record_into(outputs, m, o, at=1.0, release="v"))
    assert mig.STATE_KEY not in outputs
    mig.run_phase(db, [m0002_index_interval.MIGRATION], mig.NEEDS_NEW_CODE, outputs=outputs,
                  record=lambda m, o: mig.record_into(outputs, m, o, at=2.0, release="v"))
    assert outputs[mig.STATE_KEY]["0002"]["changed"] == 1
    assert outputs[mig.SCHEMA_KEY] == 0, "0001 is not recorded, so the schema has not reached 1"
