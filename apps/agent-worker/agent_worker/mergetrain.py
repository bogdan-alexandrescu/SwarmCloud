"""The merge step's side of a merge train: its entry (TRAIN-WS, docs/merge-trains.md §5).

WHY. In a workspace that merges in trains, a merge step does not merge its
own pull request. It does everything else it does in direct mode -- the
verdict, the head, conflicts and its fix rounds, its own checks green -- and
then registers the pull request here as ready for a train, and parks.
swarm-api's coordinator (`swarm_api.mergetrain`) composes trains of up to N
ready pull requests per repository, runs the base's required checks on the
composed head, lands each by squash pinned to the head registered here, and
records how each entry ended; the step's next attempt reads that.

WHAT. One document per merge step, `merge_train_entries/{entry_id}`, the id
derived from (tenant, task) so a step's every attempt writes the same one.
States:

  * `queued`   -- ready, waiting for a train (written here);
  * `riding`   -- in the lane's running train (the coordinator);
  * `landed`   -- squash-merged by the train at `head` (the coordinator);
  * `ejected`  -- the train will not land it; `reason`/`detail` say why
                  (the coordinator). The step ends FAILED `train_ejected`;
  * `returned` -- handed back to the step: a conflict with the train, a moved
                  head, or a refused landing. The step re-runs its gate --
                  so a conflict with the new base goes to its conflict fix
                  round as in direct mode -- and registers again.

THE REGISTRATION IS A FENCED TRANSACTION (`ControlPlane._fenced_task`, as the
merge slot's are): a superseded attempt registers nothing (invariant 5).
`register_entry` is its pure rule.

The document is tenant-writable, as every document the worker writes. A
forged entry can at worst ask the coordinator to land, with the tenant's own
`-git` token, a pull request of the tenant's own repository whose composed
head is green -- which that tenant's worker identity, holding the same
token, could already do. The coordinator still re-reads the pull request,
its head and its base, and checks the step is parked on its train wait.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

from swarm_common.admission import _snapshot
from swarm_common.models import utcnow

#: Worker role: get, create and update by id, no list and no delete
#: (`worker_firestore`), which is all an entry needs from this side.
ENTRIES_COLLECTION = "merge_train_entries"

#: The signed `merge_target.merge_mode` values (swarm-api's
#: `validation.MERGE_MODE_*`, held equal by test_merge_train_mode.py).
DIRECT = "direct"
TRAIN = "train"

#: The CI_PENDING park code of a step waiting on its train. swarm-api's CI
#: wake tick leaves these parks alone (`swarm_api.mergewake.MERGE_TRAIN_WAITING`):
#: the coordinator marks them when their entry concludes.
TRAIN_WAITING = "merge_train_waiting"
#: Wakes a train wait may be refunded, counted on their own (`train_waits`)
#: and off the CI clock, as the merge slot's are: a queue several trains
#: long must neither spend the step's attempts nor read as `checks_timeout`.
#: 200 at the 15-minute fallback is two days of waiting.
TRAIN_MAX_WAITS = 200

QUEUED = "queued"
RIDING = "riding"
LANDED = "landed"
EJECTED = "ejected"
RETURNED = "returned"
STATES = (QUEUED, RIDING, LANDED, EJECTED, RETURNED)

#: What a step writes; the coordinator writes the rest.
REGISTERED_FIELDS = ("tenant_id", "task_id", "repository", "repository_url", "base",
                     "number", "head", "title", "message")


class EntryTenantMismatch(RuntimeError):
    """The entry under this step's id names another tenant."""


def entry_id_for(tenant_id: str, task_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\n{task_id}".encode("utf-8")).hexdigest()
    return "mte_" + digest[:32]


def register_entry(existing: Mapping[str, Any] | None, *, fields: Mapping[str, Any],
                   now: Any) -> tuple[dict[str, Any] | None, Mapping[str, Any]]:
    """(the document to write or None, the entry as it now stands).

    * none yet: a queued entry, enqueued now;
    * queued or riding at the same head: unchanged -- it keeps its place,
      and a riding entry is never pulled out of its train from here;
    * landed or ejected at the same head: unchanged -- concluded, and the
      step reads the conclusion;
    * returned, or any state at a NEW head (a fix round pushed one): queued
      again at that head, keeping `enqueued_at` and `returns`.
    """
    if existing is not None and existing.get("tenant_id") != fields.get("tenant_id"):
        raise EntryTenantMismatch(f"merge train entry of tenant {existing.get('tenant_id')!r}")
    base = {key: fields[key] for key in REGISTERED_FIELDS}
    if existing is None:
        new = {**base, "state": QUEUED, "returns": 0, "enqueued_at": now, "updated_at": now}
        return new, new
    same_head = existing.get("head") == fields.get("head")
    state = existing.get("state")
    if same_head and state in (QUEUED, RIDING, LANDED, EJECTED):
        return None, existing
    kept = {key: existing[key] for key in ("enqueued_at", "returns") if key in existing}
    new = {**base, "state": QUEUED, "returns": int(kept.get("returns") or 0),
           "enqueued_at": kept.get("enqueued_at", now), "updated_at": now}
    return new, new


class FirestoreTrainEntries:
    """The step's entry over Firestore, every write fenced on this attempt."""

    def __init__(self, db: Any, *, tenant_id: str,
                 run_transaction: Callable[[Callable[[Any], Any]], Any],
                 fence: Callable[[Any], Any],
                 call_options: Callable[[], Mapping[str, Any]] = dict,
                 clock: Callable[[], Any] = utcnow) -> None:
        self._db = db
        self.tenant_id = tenant_id
        self._run = run_transaction
        self._fence = fence
        self._options = call_options
        self._clock = clock

    def _ref(self, entry_id: str) -> Any:
        return self._db.collection(ENTRIES_COLLECTION).document(entry_id)

    def peek(self, task_id: str) -> Mapping[str, Any] | None:
        """This step's entry as stored, or None. Another tenant's is refused."""
        snap = self._ref(entry_id_for(self.tenant_id, task_id)).get(**dict(self._options()))
        data = snap.to_dict() if snap.exists else None
        if data is not None and data.get("tenant_id") != self.tenant_id:
            raise EntryTenantMismatch(f"merge train entry of tenant {data.get('tenant_id')!r}")
        return data

    def register(self, fields: Mapping[str, Any]) -> Mapping[str, Any]:
        """Write or keep this step's entry (`register_entry`); returns it."""
        fields = {**fields, "tenant_id": self.tenant_id}
        entry_id = entry_id_for(self.tenant_id, str(fields["task_id"]))
        ref = self._ref(entry_id)

        def body(txn: Any) -> Mapping[str, Any]:
            # Every read before any write: Firestore forbids a read after one.
            snap = _snapshot(txn.get(ref, **dict(self._options())))
            self._fence(txn)
            existing = snap.to_dict() if snap.exists else None
            new, entry = register_entry(existing, fields=fields, now=self._clock())
            if new is not None:
                new = {**new, "entry_id": entry_id}
                txn.set(ref, new)
                return new
            return entry

        return self._run(body)
