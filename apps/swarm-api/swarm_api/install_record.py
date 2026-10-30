"""swarm-api's side of the install record (docs/design/day-one-plan.md §5.4).

Firestore collection `swarm-install`, document `<env>` and its
subcollections. **Every document has exactly one writer.** The engine (the
`swarm install` CLI) writes `swarm-install/<env>`; swarm-api reads it and never
writes it. swarm-api writes the console's documents and `health/latest` (on
behalf of `swarm-doctor`), and nothing else under the collection.

`SWARM_API_DOCUMENTS` restates swarm-api's rows of `swarm_install.record.WRITERS`
(DAYONE-ENGINE-1). It is restated, not imported, because swarm-api's image does
not carry `apps/swarm-install` (the engine's Google clients stay out of it);
`tests/unit/control_plane/test_install_record_writers.py` imports both and
fails on any difference, so the table still lives once in effect.

Every write is a Firestore transaction: read, merge, write, retried by the
client on contention, so two swarm-api replicas cannot lose an update.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COLLECTION = "swarm-install"

#: Document path template -> the fields swarm-api may write there. `accounts`
#: is keyed per account label; each label's value holds `ACCOUNT_FIELDS`.
SWARM_API_DOCUMENTS: dict[str, frozenset[str]] = {
    "swarm-install/{env}/console/github_app": frozenset(
        {"state_hash", "id", "slug", "client_id", "installed_on", "secrets_written_at",
         "self_check"}),
    "swarm-install/{env}/console/accounts": frozenset(),
    "swarm-install/{env}/console/teams/{id}": frozenset(
        {"group", "workspace", "first_admin", "invites", "progress", "smoke", "grant"}),
    "swarm-install/{env}/console/finished": frozenset({"at", "by"}),
    #: The console's two verification runs (DAYONE-SETUP-API-2): `verify_runs`
    #: is `{"console_task": <task id>, "console_workflow": <workflow id>}`,
    #: which the engine copies into its state's `outputs["verify_runs"]`.
    "swarm-install/{env}/console/verify_runs": frozenset(
        {"verify_runs", "repository", "started_at", "by"}),
    "swarm-install/{env}/health/latest": frozenset({"run_at", "checks", "findings"}),
}
ACCOUNT_FIELDS = frozenset({"owner", "link_hash", "link_expires", "signed_in_at"})

#: Read only, never written here.
ENGINE_DOCUMENT = "swarm-install/{env}"

GITHUB_APP = "github_app"
ACCOUNTS = "accounts"
FINISHED = "finished"
TEAMS = "teams"
VERIFY_RUNS = "verify_runs"


class RecordFieldRefused(ValueError):
    """A write named a field that is not in its document's row of the table."""


def _template(path: str, env: str) -> str:
    """The table's template for a concrete path, or ValueError."""
    prefix = f"{COLLECTION}/{env}/"
    if not path.startswith(prefix):
        raise RecordFieldRefused(f"{path!r} is not under {COLLECTION}/{env}")
    rest = path[len(prefix):]
    if rest.startswith("console/teams/") and rest.count("/") == 2:
        return "swarm-install/{env}/console/teams/{id}"
    template = "swarm-install/{env}/" + rest
    if template not in SWARM_API_DOCUMENTS:
        raise RecordFieldRefused(f"swarm-api writes no document {path!r}")
    return template


def check_fields(path: str, env: str, data: Mapping[str, Any]) -> None:
    """Refuse a write of any field outside the document's row."""
    template = _template(path, env)
    allowed = SWARM_API_DOCUMENTS[template]
    if template.endswith("/accounts"):
        for label, value in data.items():
            if not isinstance(value, Mapping) or set(value) - ACCOUNT_FIELDS:
                raise RecordFieldRefused(f"accounts.{label}: fields outside {sorted(ACCOUNT_FIELDS)}")
        return
    unknown = set(data) - allowed
    if unknown:
        raise RecordFieldRefused(f"{template}: fields not in its row: {sorted(unknown)}")


class InstallRecord:
    """The record as swarm-api sees it, over a Firestore client (or the fake)."""

    def __init__(self, db: Any, env: str) -> None:
        self._db = db
        self.env = env

    def engine_path(self) -> str:
        return ENGINE_DOCUMENT.format(env=self.env)

    def console_path(self, name: str) -> str:
        return f"{COLLECTION}/{self.env}/console/{name}"

    def team_path(self, team_id: str) -> str:
        return self.console_path(f"{TEAMS}/{team_id}")

    def health_path(self) -> str:
        return f"{COLLECTION}/{self.env}/health/latest"

    def _ref(self, path: str) -> Any:
        collection, _, doc_id = path.rpartition("/")
        return self._db.collection(collection).document(doc_id)

    def read(self, path: str) -> dict[str, Any] | None:
        snapshot = self._ref(path).get()
        if not snapshot.exists:
            return None
        return dict(snapshot.to_dict() or {})

    def read_engine(self) -> dict[str, Any] | None:
        """The engine's document; swarm-api has no method that writes it."""
        return self.read(self.engine_path())

    def teams(self) -> list[tuple[str, dict[str, Any]]]:
        """Every team document, oldest first (`progress.created_at`)."""
        rows = [(snap.id, dict(snap.to_dict() or {}))
                for snap in self._db.collection(self.console_path(TEAMS)).stream()]
        return sorted(rows, key=lambda row: (str((row[1].get("progress") or {})
                                                 .get("created_at") or ""), row[0]))

    def write(self, path: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """Merge `data` into the document at `path` in one transaction; the
        merged document is returned. Each top-level field is replaced whole,
        except `accounts`, whose labels are merged one by one."""
        check_fields(path, self.env, data)
        from google.cloud import firestore  # lazy: nothing built at import time

        ref = self._ref(path)
        merged: dict[str, Any] = {}

        @firestore.transactional
        def _apply(transaction: Any) -> None:
            snapshot = ref.get(transaction=transaction)
            current = dict(snapshot.to_dict() or {}) if snapshot.exists else {}
            merged.clear()
            merged.update(current)
            merged.update(data)
            transaction.set(ref, merged)

        _apply(self._db.transaction())
        return merged


__all__ = [
    "ACCOUNTS",
    "ACCOUNT_FIELDS",
    "COLLECTION",
    "ENGINE_DOCUMENT",
    "FINISHED",
    "GITHUB_APP",
    "InstallRecord",
    "RecordFieldRefused",
    "SWARM_API_DOCUMENTS",
    "TEAMS",
    "VERIFY_RUNS",
    "check_fields",
]
