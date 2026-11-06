"""Migration 0002: repository registrations storing the old change-interval
default move to the new one (docs/repo-index.md §3.3; #2524; was
scripts/migrate-index-interval.sh).

`_index_defaults` wrote `index.min_change_interval_minutes = 30` into every
registration made before #2524 raised the default to 120, so changing the
constant moved none of them. BGD, 2026-10-28: a one-time migration, not a
read-time rule. Exactly the integer 30 is moved (a float, a string or a
missing value is not what `_index_defaults` wrote); an owner who chose 30
looks the same and can set it back with
`PATCH /v1/repositories/{repo_id} {"index": {"min_change_interval_minutes": 30}}`.

It runs after the new code serves (`needs-new-code`): the old code would keep
writing 30 into every registration made behind it. Only the one field is
written, conditional on the document's update time.
"""

from __future__ import annotations

from typing import Any

from . import NEEDS_NEW_CODE, Documents, MigrationConflict, Outcome

COLLECTION = "repositories"
OLD_DEFAULT = 30
NEW_DEFAULT = 120
FIELD = "index.min_change_interval_minutes"


def _stores_old_default(data: dict[str, Any]) -> bool:
    index = data.get("index")
    value = index.get("min_change_interval_minutes") if isinstance(index, dict) else None
    return type(value) is int and value == OLD_DEFAULT


class IndexInterval:
    number = 2
    name = "index_interval"
    phase = NEEDS_NEW_CODE
    explain = f"registrations storing the old {OLD_DEFAULT}-minute index interval move to {NEW_DEFAULT}"

    def _rows(self, db: Documents) -> list[tuple[str, Any]]:
        return [(path, update_time) for path, data, update_time in db.list(COLLECTION) if _stores_old_default(data)]

    def pending(self, db: Documents) -> list[str]:
        return [path for path, _ in self._rows(db)]

    def apply(self, db: Documents) -> Outcome:
        changed = refused = 0
        for path, update_time in self._rows(db):
            try:
                db.update(path, {FIELD: NEW_DEFAULT}, update_time=update_time)
                changed += 1
            except MigrationConflict:
                refused += 1
        return Outcome(changed=changed, refused=refused)


MIGRATION = IndexInterval()
