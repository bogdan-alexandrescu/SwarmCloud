"""The merge train coordinator: batch ready pull requests, test once, land each (TRAIN-WS).

docs/merge-trains.md §4-§5. In a workspace whose effective merge mode for a
repository is `train` (`mergesettings`), a merge step that has done every
check it does in direct mode registers its pull request in
`merge_train_entries` and parks (`agent_worker.mergetrain`). This module is
the other side: ONE LANE PER (tenant, repository, base branch), at most one
train running in it, advanced on the per-tenant `merge_wake` tick
(`routes.admin.wake_merges`, every minute) -- no loop, no pod, no poll
between ticks. A waiting train is two kinds of Firestore document and a
branch on GitHub; it holds no capacity and creates no infrastructure demand
(invariant 1).

THE RULES ARE PURE AND PORTED from the repository-level GitHub-Actions train
(PR 1294, `scripts/merge-train.sh`: select / judge / plan):

  * `select`: up to N ready pull requests, oldest registration first; a half
    left by a bisection rides first; one pull request rides once.
  * `judge`: the composed head's required checks -- green, red, or pending,
    and pending past the workspace's max train age is `timed_out`.
  * `bisect` / `on_failure`: a red (or timed-out) train of one ejects it; of
    several, ejects nobody and puts every rider back in the queue in its old
    place; its first half (rounded up) rides next, then its second, each
    split again if red (the lane's `halves` stack). A single culprit is
    found in about log2(N) further trains and is the only one ejected.

THE LANE, step by step (`_Lane`):

  1. COMPOSE. Eligible entries are queued ones whose step is PARKED on
     CI_PENDING with `merge_wait.code == merge_train_waiting` at the same
     pull request and head, in the entry's repository. A step that ended is
     dropped (`step_ended`); one not parked yet is left for the next tick.
     Each pull request is re-read: merged, closed, moved head or another
     base returns it to its step. The lane is CLAIMED in a transaction
     (`train` set, `seq` bumped) before anything is written to GitHub, so two
     ticks never compose two trains. Then a `swarm-train/<lane>-<seq>` branch
     is made at the base's head and each pull request's head merged into it
     in order (`POST /merges`). A conflict with the train RETURNS that pull
     request to its step -- whose gate then finds any conflict with the new
     base and spends a conflict fix round, as in direct mode -- and after
     `MAX_RETURNS` returns ejects it. A DRAFT pull request from the branch
     makes the base's pull-request checks run on the composed head.
  2. TEST. Each tick reads the base's required checks at the composed head
     (`forgechecks.evaluate`, the CI wake tick's reading) and `judge`s it.
  3. LAND (green). Each pull request is squash-merged in order, pinned to the
     head it was registered and tested at (`sha`), its own title and the
     step's provenance as the commit. A refusal returns that one and puts
     the rest back in the queue. After the last, the base's tree is read and
     compared with the tested tree (`tree_verified`): the squash sequence
     must equal what CI saw, and the lane records whether it did.
  4. FAIL (red / timed out): `on_failure`'s plan, the draft closed and the
     branch deleted.

Every entry that concludes -- LANDED, EJECTED, RETURNED -- has its step's
park MARKED (`mergewake._record`, guarded on the step still being parked at
that head), and the scheduler promotes it on its next drain: the step reads
its own outcome and ends SUCCEEDED or FAILED `train_ejected`.

WHAT IT GAINED FROM THE SCRIPT (docs/design/merging-without-a-laptop.md
§6-§8, lanes 5-8):

  * GATHER. A train composes once `gather_min` riders are eligible, or once
    the oldest has waited `gather_wait_minutes`; a bisected half rides at
    once (`_compose`).
  * PARITY. A red train re-runs its failed Actions runs ONCE before it
    bisects (`_retry`); while the base's own required checks are red at its
    head a red train is HELD, not bisected, up to `base_red_wait_minutes`; a
    required check with no run `required_start_minutes` after compose fails
    the train `required_check_not_started`, naming it; the context only the
    train produces (`issueci.TRAIN_CONTEXT`) is left out of the composed
    head's judging and POSTED at each rider's head just before it lands.
  * BOARDING. In a registered repository whose effective mode is `train`, an
    open, non-draft, non-fork pull request on the lane's base that carries
    `ready` -- last labelled by an account with write or higher, green at its
    head and mergeable -- boards as a `label` entry, after every merge-step
    entry, in label order (`_board_labelled`). One that cannot is told why,
    once per head and reason; an ejected one is told why and loses `ready`.
  * COEXISTENCE. While the repository's own `MERGE_TRAIN` is on
    (`issueci.repository_train_on`) the lane composes and lands nothing,
    abandons a train it had, and reports `repository_train_on`: one train
    per base, the repository's own switch wins.
  * THE LANDING CHECK. A rider is merged only when the train's required
    checks were read green at the tested head (`green_head`, else read again
    now), and never onto a base whose required checks are red at its head.

CREDENTIAL. The tenant's own forge token (`ctx.forge_tokens.token_for`),
the one the CI wake tick on the same route reads with, read once per tick,
held only for it, never logged. Only that tenant's entries are read
(invariant 9): every query filters on the tenant and every document read is
checked again.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES, ParkReason, TaskState

from .errors import ApiError, NotFound
from .forgechecks import (
    GREEN, RED, evaluate, failed_workflow_runs, not_started, required_status_checks,
    without_contexts,
)
from .forgewrite import (
    ISSUES_READ, ISSUES_WRITE, RULES_READ, ForgeWriteNotFound, GitHubWriter,
)
from .issueci import TRAIN_CONTEXT, repository_train_on
from .judgment import settings_mac
from .mergesettings import TRAIN, MergeSettings, MergeSettingsStore, effective_settings
from .mergewake import MERGE_TRAIN_WAITING, MERGE_WAIT_METADATA_KEY, WAKE_MARKER, _record
from .repositories import parse_repository
from .validation import IssueRef

log = logging.getLogger(__name__)

#: `agent_worker.mergetrain`'s names, held equal by test_merge_train_rules.py.
ENTRIES_COLLECTION = "merge_train_entries"
TRAIN_WAITING = MERGE_TRAIN_WAITING
QUEUED = "queued"
RIDING = "riding"
LANDED = "landed"
EJECTED = "ejected"
RETURNED = "returned"

#: One document per lane: (tenant, repository, base). Written here only.
LANES_COLLECTION = "merge_train_lanes"
#: The train branch's prefix. Not `swarm/`, which is a task's own branch.
BRANCH_PREFIX = "swarm-train/"
#: Returns before a pull request that keeps conflicting with the train is
#: ejected instead: three is one conflict fix round's worth of tries after
#: the step's own budget of two SPENT rounds (`MERGE_CONFLICT_ROUNDS_MAX`;
#: rounds that moved the head do not count, #3073), so a pull request that
#: conflicts with everything stops holding its place.
MAX_RETURNS = 3
#: A lane claimed for a train that never reached testing -- a tick that
#: died mid-compose -- is abandoned after this long, its riders queued again.
COMPOSE_STALE_SECONDS = 600
#: Lanes stepped per tick, oldest activity first; the rest wait one minute.
MAX_LANES_PER_TICK = 20
#: A red reading this soon after the train's one re-run is the old runs,
#: not yet replaced: it is read as pending.
RETRY_SETTLE_SECONDS = 90
#: The label a person boards a pull request with, as on the repository-level
#: train (`scripts/merge-train.sh`).
READY_LABEL = "ready"
#: An entry's source: a merge step registered it, or `ready` boarded it.
SOURCE_STEP = "step"
SOURCE_LABEL = "label"
#: Collaborator permissions that may board a pull request by labelling it:
#: GitHub reports `maintain` as `write`, and `triage` as `read`.
BOARDING_PERMISSIONS = frozenset({"admin", "write"})
#: The pages of a pull request's events read to find who labelled it last.
MAX_EVENT_PAGES = 5
#: The hidden marker of what the train tells a pull request.
TELL_MARKER = "<!-- swarm-merge-train:"
#: What the lane reports while the repository's own train is on.
REPOSITORY_TRAIN_ON = "repository_train_on"

_GITHUB_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)


def entry_id_for(tenant_id: str, task_id: str) -> str:
    """`agent_worker.mergetrain.entry_id_for`, restated (held equal by a test)."""
    digest = hashlib.sha256(f"{tenant_id}\n{task_id}".encode("utf-8")).hexdigest()
    return "mte_" + digest[:32]


def label_entry_id_for(tenant_id: str, repository: str, number: int) -> str:
    """A `ready` pull request's entry: one per (tenant, repository, number),
    never a task id's (a task id has no `#`)."""
    key = f"{tenant_id}\n{repository.lower()}\n#{int(number)}"
    return "mte_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def lane_id_for(tenant_id: str, repository: str, base: str) -> str:
    key = f"{tenant_id}\n{repository.lower()}\n{base}"
    return "mtl_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# the pure rules (ported from PR 1294's select / judge / plan)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rider:
    entry_id: str
    number: int
    head: str
    enqueued_at: Any
    #: 0 for a merge step's entry, 1 for a labelled pull request: every
    #: merge-step entry boards before any labelled one (§6.2 B).
    rank: int = 0


def _order(rider: Rider) -> tuple[int, Any, int, str]:
    return (rider.rank, rider.enqueued_at, rider.number, rider.entry_id)


def select(queued: Sequence[Rider], *, size: int,
           next_batch: Sequence[str] = ()) -> list[Rider]:
    """The next train: a bisected half first, else up to `size`, oldest first."""
    by_id = {r.entry_id: r for r in queued}
    half = [by_id[i] for i in next_batch if i in by_id]
    candidates = half if half else sorted(queued, key=_order)
    picked: list[Rider] = []
    numbers: set[int] = set()
    for rider in candidates:
        if rider.number in numbers:
            continue
        numbers.add(rider.number)
        picked.append(rider)
        if len(picked) >= max(1, size):
            break
    return picked


def judge(reading: str, *, started_at: datetime, now: datetime, max_age_minutes: int) -> str:
    """green / red / pending, and pending past the max age is timed_out."""
    if reading == GREEN:
        return "green"
    if reading == RED:
        return "red"
    if now - started_at >= timedelta(minutes=max_age_minutes):
        return "timed_out"
    return "pending"


def bisect(batch: Sequence[Rider]) -> tuple[list[Rider], list[Rider]]:
    """(first half, rounded up; the rest)."""
    cut = math.ceil(len(batch) / 2)
    return list(batch[:cut]), list(batch[cut:])


@dataclass(frozen=True)
class FailurePlan:
    eject: list[str]
    #: The first half: rides next.
    next_batch: list[str]
    #: The rest: rides after `next_batch` and anything it splits into.
    later: list[str]
    requeue: list[str]


def on_failure(batch: Sequence[Rider]) -> FailurePlan:
    """A red or timed-out train: eject a lone rider, else bisect.

    The lane keeps the halves on a stack (`halves`, the top last): `later`
    is pushed, then `next_batch`, and each compose pops the top. So after a
    red half is split down to its culprit, the half beside it rides next on
    its own, rather than the whole queue being re-formed and re-tested."""
    if len(batch) == 1:
        return FailurePlan(eject=[batch[0].entry_id], next_batch=[], later=[], requeue=[])
    first, rest = bisect(batch)
    return FailurePlan(eject=[], next_batch=[r.entry_id for r in first],
                       later=[r.entry_id for r in rest], requeue=[r.entry_id for r in batch])


def _halves(lane: Mapping[str, Any]) -> list[list[str]]:
    """The lane's untested halves, the top last. Stored as `[{ids: [...]}]`:
    Firestore holds no array directly inside an array."""
    stored = lane.get("halves")
    if not isinstance(stored, list):
        return []
    return [[str(i) for i in item.get("ids") or []] for item in stored
            if isinstance(item, Mapping)]


def _stored_halves(halves: list[list[str]]) -> list[dict[str, list[str]]]:
    return [{"ids": list(ids)} for ids in halves if ids]


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

@dataclass
class TrainReport:
    lanes: int = 0
    composed: int = 0
    landed: int = 0
    ejected: int = 0
    returned: int = 0
    waiting: int = 0
    failed: int = 0
    #: Labelled pull requests boarded this tick.
    boarded: int = 0
    #: Lanes held because the repository's own train is on.
    repository_train_on: int = 0
    #: Whether any lane did something or still has a train: false is a
    #: quiet tenant, which costs nothing until a step registers again.
    active: bool = False
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {"lanes": self.lanes, "composed": self.composed, "landed": self.landed,
                "ejected": self.ejected, "returned": self.returned,
                "waiting": self.waiting, "failed": self.failed, "active": self.active,
                "boarded": self.boarded, "repository_train_on": self.repository_train_on,
                "truncated": self.truncated}

    def fail(self, lane: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"lane": lane, "code": code})


# --------------------------------------------------------------------------
# the coordinator
# --------------------------------------------------------------------------

def _parse_time(value: Any) -> datetime | None:
    """An ISO 8601 time GitHub wrote, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _repo_of_url(url: Any) -> str | None:
    matched = _GITHUB_URL.match(url) if isinstance(url, str) else None
    return f"{matched.group(1)}/{matched.group(2)}".lower() if matched else None


def _entries(db: Any, tenant_id: str, state: str, limit: int) -> list[dict[str, Any]]:
    query = (db.collection(ENTRIES_COLLECTION)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .where(filter=FieldFilter("state", "==", state))
             .limit(limit))
    # The filter again, in the application: another tenant's entry is never
    # read with this tenant's token.
    return [d for d in (s.to_dict() or {} for s in query.stream())
            if d.get("tenant_id") == tenant_id and d.get("state") == state]


def tick_tenant(ctx: Any, tenant_id: str, *, limit: int) -> TrainReport:
    """One tick over `tenant_id`'s train lanes; see the module docstring."""
    tenant = ctx.store.get_tenant(tenant_id)
    if tenant is None:
        raise NotFound(f"tenant {tenant_id!r} not found")
    db = ctx.store.db
    report = TrainReport()
    queued = _entries(db, tenant_id, QUEUED, limit + 1)
    riding = _entries(db, tenant_id, RIDING, limit + 1)
    report.truncated = len(queued) > limit or len(riding) > limit
    lanes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in queued[:limit] + riding[:limit]:
        repository, base = entry.get("repository"), entry.get("base")
        if isinstance(repository, str) and isinstance(base, str) and repository and base:
            lanes.setdefault((repository.lower(), base), []).append(entry)
    store = MergeSettingsStore(db, now=ctx.now, mac=settings_mac(ctx.settings))
    settings = store.get(tenant_id)
    # The registered repositories, each with its own effective settings; one
    # whose effective mode is `train` has a lane on its default branch even
    # with no entry, so that a `ready` pull request can board it (§6.2 B).
    registered: dict[str, dict[str, Any]] = {}
    for registration in store.registrations(tenant_id):
        name = f"{registration.get('owner')}/{registration.get('repo')}".lower()
        if registration.get("tenant_id") == tenant_id and registration.get("owner") \
                and registration.get("repo"):
            registered[name] = registration
    boarding: set[tuple[str, str]] = set()
    for name, registration in registered.items():
        base = registration.get("default_branch")
        if effective_settings(settings, registration).mode == TRAIN \
                and isinstance(base, str) and base:
            lanes.setdefault((name, base), [])
            boarding.add((name, base))
    if not lanes:
        return report
    held: dict[str, str] = {}

    def token() -> str:
        if "value" not in held:
            held["value"] = ctx.forge_tokens.token_for(tenant)
        return held["value"]

    try:
        for (repository, base), entries in sorted(lanes.items())[:MAX_LANES_PER_TICK]:
            report.lanes += 1
            lane_id = lane_id_for(tenant_id, repository, base)
            lane_settings = effective_settings(settings, registered.get(repository))
            try:
                _Lane(ctx, tenant_id, repository, base, lane_id, lane_settings, token,
                      report).step(entries, board=(repository, base) in boarding)
            except Exception as exc:
                code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                log.warning("merge train lane=%s tenant=%s: not advanced (%s)",
                            lane_id, tenant_id, code)
                report.fail(lane_id, code)
    finally:
        held.clear()
    return report


class _Lane:
    def __init__(self, ctx: Any, tenant_id: str, repository: str, base: str, lane_id: str,
                 settings: MergeSettings, token: Any, report: TrainReport) -> None:
        self.ctx = ctx
        self.db = ctx.store.db
        self.writer: GitHubWriter = ctx.forge_writer
        self.tenant_id = tenant_id
        self.repository = repository
        self.base = base
        self.lane_id = lane_id
        self.settings = settings
        self.token = token
        self.report = report
        self.now = ctx.now()
        owner, repo = parse_repository(repository)
        self.ref = IssueRef(owner=owner, repo=repo, number=0)
        self._rules: list[Any] | None = None
        self._open: list[dict[str, Any]] | None = None

    # -- documents ------------------------------------------------------------
    def _lane_ref(self) -> Any:
        return self.db.collection(LANES_COLLECTION).document(self.lane_id)

    def _lane(self) -> dict[str, Any]:
        snap = self._lane_ref().get()
        data = snap.to_dict() if snap.exists else None
        if data is not None and data.get("tenant_id") != self.tenant_id:
            raise NotFound(f"merge train lane {self.lane_id!r} is not this tenant's")
        return data or {"lane_id": self.lane_id, "tenant_id": self.tenant_id,
                        "repository": self.repository, "base": self.base, "seq": 0,
                        "train": None, "halves": [], "last": None}

    def _update_lane(self, expect_train: str | None, patch: Mapping[str, Any]) -> bool:
        """Apply `patch` to the lane while its running train is `expect_train`."""
        ref = self._lane_ref()
        transaction = self.db.transaction()
        fresh = self._lane()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else dict(fresh)
            if data.get("tenant_id") != self.tenant_id:
                return False
            if ((data.get("train") or {}).get("id")) != expect_train:
                return False
            txn.set(ref, {**data, **patch, "updated_at": self.now})
            return True

        return bool(_apply(transaction))

    def _set_entry(self, entry_id: str, *, expect: Sequence[str], head: str,
                   patch: Mapping[str, Any]) -> bool:
        """Move one entry, guarded on its tenant, its state and its head: a
        step that re-registered at a new head meanwhile wins."""
        ref = self.db.collection(ENTRIES_COLLECTION).document(entry_id)
        transaction = self.db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if (data is None or data.get("tenant_id") != self.tenant_id
                    or data.get("state") not in expect or data.get("head") != head):
                return False
            txn.update(ref, {**patch, "updated_at": self.now})
            return True

        return bool(_apply(transaction))

    def _conclude(self, rider: Mapping[str, Any], state: str, *, reason: str | None = None,
                  detail: str = "", extra: Mapping[str, Any] | None = None,
                  expect: Sequence[str] = (RIDING,), returns: int | None = None) -> None:
        """An entry's outcome, and its step's park marked so it reads it."""
        patch: dict[str, Any] = {"state": state, **(extra or {})}
        if reason is not None:
            patch["reason"] = reason
            patch["detail"] = detail[:500]
        if returns is not None:
            patch["returns"] = returns
        if not self._set_entry(rider["entry_id"], expect=expect, head=rider["head"],
                               patch=patch):
            return
        if state == LANDED:
            self.report.landed += 1
        elif state == EJECTED:
            self.report.ejected += 1
        elif state == RETURNED:
            self.report.returned += 1
        if rider.get("source") == SOURCE_LABEL:
            # No step reads it: the pull request is told, and loses `ready`.
            if state == EJECTED:
                self._eject_labelled(rider, reason or "ejected", detail)
            return
        _record(self.db, self.tenant_id, str(rider.get("task_id")), rider["head"],
                {"checked_at": self.now, WAKE_MARKER: self.now,
                 "wake_reason": f"train_{state}"})

    def _return(self, rider: Mapping[str, Any], reason: str, detail: str = "", *,
                expect: Sequence[str] = (RIDING,), counts: bool = False) -> None:
        returns = int(rider.get("returns") or 0) + (1 if counts else 0)
        if counts and returns >= MAX_RETURNS:
            self._conclude(rider, EJECTED, reason=reason, detail=detail, expect=expect,
                           returns=returns)
            return
        self._conclude(rider, RETURNED, reason=reason, detail=detail, expect=expect,
                       returns=returns if counts else None)

    def _requeue(self, rider: Mapping[str, Any]) -> None:
        self._set_entry(rider["entry_id"], expect=(RIDING,), head=rider["head"],
                        patch={"state": QUEUED, "train_id": None})

    # -- the forge reads -------------------------------------------------------
    def _required(self) -> list[Any]:
        """The base's required checks, read once per lane per tick from its
        rules, never from a stored list: a change applies on the next tick."""
        if self._rules is None:
            self._rules = required_status_checks(
                self.writer.branch_rules(self.ref, self.base, self.token()))
        return self._rules

    def _train_posts(self) -> bool:
        """The base requires the context only the train produces."""
        return any(check.context == TRAIN_CONTEXT for check in self._required())

    def _judged(self) -> list[Any]:
        """The required checks a head is judged by: the train's own context
        left out, since the train posts it itself just before landing."""
        return without_contexts(self._required(), [TRAIN_CONTEXT])

    def _read_at(self, sha: str, branch: str | None) -> tuple[Any, list[Any], list[Any]]:
        token = self.token()
        runs = self.writer.check_runs(self.ref, sha, token, head_ref=branch)
        statuses = self.writer.commit_statuses(self.ref, sha, token)
        return evaluate(self._judged(), runs, statuses), runs, statuses

    def _base_red(self) -> bool:
        """The base's own required checks are red at its head: every train
        is red then, and nothing lands on it."""
        base_sha = self.writer.branch_head(self.ref, self.base, self.token())
        if not base_sha:
            return False
        reading, _runs, _statuses = self._read_at(base_sha, self.base)
        return reading.state == RED

    def _repository_train_on(self) -> bool:
        return repository_train_on(self.writer, self.ref, self.token(), self._required())

    # -- the step ---------------------------------------------------------------
    def step(self, entries: list[dict[str, Any]], *, board: bool = False) -> None:
        lane = self._lane()
        train = lane.get("train")
        if board and not train and not entries and not self._ready_pulls():
            # An idle lane of a registered repository: one list call a tick.
            return
        if self._repository_train_on():
            self._hand_to_repository(lane, train)
            return
        if lane.get("status") == REPOSITORY_TRAIN_ON and not train:
            self._update_lane(None, {"status": None})
        if not train:
            self._heal(entries, None)
            if board:
                self._board_labelled(lane, entries)
            self._compose(lane, [e for e in entries if e.get("state") == QUEUED])
            return
        self.report.active = True
        self._heal(entries, train.get("id"))
        state = train.get("state")
        if state == "composing":
            started = train.get("started_at")
            if isinstance(started, datetime) and \
                    self.now - started >= timedelta(seconds=COMPOSE_STALE_SECONDS):
                self._abandon(train)
            else:
                self.report.waiting += 1
        elif state == "testing":
            self._test(train)
        elif state == "landing":
            self._land(train)

    def _heal(self, entries: list[dict[str, Any]], train_id: str | None) -> None:
        """A RIDING entry of no running train -- its tick died between the
        claim and the conclusion -- is queued again."""
        for entry in entries:
            if entry.get("state") == RIDING and entry.get("train_id") != train_id:
                self._requeue(entry)
                entry["state"] = QUEUED

    # -- boarding a `ready` pull request (§6.2 B) -------------------------------
    def _board_labelled(self, lane: Mapping[str, Any], entries: list[dict[str, Any]]) -> None:
        """Board each open `ready` pull request on this base that no merge
        step registered and that may board; tell one that may not why, once
        per head and reason. A queued labelled entry whose pull request lost
        `ready` is returned. One list call per lane per tick; the reads of a
        candidate are made only while it is not already queued at its head."""
        mine = {str(e.get("entry_id")): e for e in entries}
        steps = {e.get("number") for e in entries if e.get("source") != SOURCE_LABEL}
        told = {str(k): v for k, v in (lane.get("told") or {}).items()}
        ready: set[int] = set()
        for raw in self._ready_pulls():
            number = raw["number"]
            ready.add(number)
            head = (raw.get("head") or {}).get("sha")
            if number in steps or not isinstance(head, str) or not head \
                    or (raw.get("base") or {}).get("ref") != self.base:
                continue
            entry_id = label_entry_id_for(self.tenant_id, self.repository, number)
            current = mine.get(entry_id)
            if current is not None and current.get("head") == head:
                continue
            verdict, detail, labelled_at = self._may_board(raw, number, head)
            if verdict == "wait":
                continue
            if verdict is not None:
                self._tell(told, number, head, verdict, detail)
                continue
            entry = self._write_label_entry(entry_id, raw, number, head, labelled_at)
            if entry is not None:
                self.report.boarded += 1
                entries.append(entry)
        for entry in list(entries):
            if entry.get("source") == SOURCE_LABEL and entry.get("state") == QUEUED \
                    and entry.get("number") not in ready:
                self._conclude(entry, RETURNED, reason="not_ready",
                               detail="it no longer carries `ready`", expect=(QUEUED,))
                entries.remove(entry)
        kept = {k: v for k, v in told.items() if k.isdigit() and int(k) in ready}
        if kept != (lane.get("told") or {}):
            self._update_lane(None, {"told": kept})

    def _ready_pulls(self) -> list[dict[str, Any]]:
        """The open pull requests that carry `ready`: one list call (the
        first page, the most recently updated), read once per lane per tick."""
        if self._open is None:
            self._open = []
            for raw in self.writer.pulls_page(self.ref, "open", 1, self.token()):
                number = raw.get("number")
                labels = {label.get("name") for label in raw.get("labels") or []
                          if isinstance(label, Mapping)}
                if isinstance(number, int) and not isinstance(number, bool) \
                        and READY_LABEL in labels:
                    self._open.append(raw)
        return self._open

    def _may_board(self, raw: Mapping[str, Any], number: int,
                   head: str) -> tuple[str | None, str, datetime]:
        """(None, "", labelled at) when the pull request may board; ("wait",
        ...) while a read is not settled; else (reason, sentence, ...)."""
        if raw.get("draft"):
            return "draft", "it is a draft", self.now
        head_repo = ((raw.get("head") or {}).get("repo") or {}).get("full_name")
        base_repo = ((raw.get("base") or {}).get("repo") or {}).get("full_name")
        if not isinstance(head_repo, str) or not isinstance(base_repo, str) \
                or head_repo.lower() != base_repo.lower():
            return "fork", "its head is in another repository (a fork)", self.now
        labelled = self._last_ready_label(number)
        if labelled is None:
            return "wait", "", self.now
        login, labelled_at = labelled
        if self._permission(login) not in BOARDING_PERMISSIONS:
            return ("labeller_not_writer",
                    "`ready` was last added by an account without write access to this "
                    "repository; someone with write access must add it", labelled_at)
        head_ref = (raw.get("head") or {}).get("ref")
        reading = self._reading(head, head_ref if isinstance(head_ref, str) else None)
        if reading.state == RED:
            return ("checks_red", "required checks are red at its head: "
                    + (", ".join(reading.failing_names()) or "a check"), labelled_at)
        if reading.state != GREEN and reading.pending:
            return "wait", "", labelled_at
        pull = self.writer.read_pull(self.ref, number, self.token())
        if pull.head_sha != head or pull.state != "open" or pull.mergeable is None:
            return "wait", "", labelled_at
        if pull.mergeable is False:
            return "not_mergeable", "it conflicts with its base", labelled_at
        return None, "", labelled_at

    def _last_ready_label(self, number: int) -> tuple[str, datetime] | None:
        """Who last added `ready` to pull request `number`, and when, from
        its issue events; None when it was never added or was taken off since."""
        what = f"the events of {self.repository}#{int(number)}"
        added: tuple[str, datetime] | None = None
        removed: datetime | None = None
        for page in range(1, MAX_EVENT_PAGES + 1):
            data = self.writer._call(
                "GET", self.writer._url(self.ref, f"issues/{int(number)}/events"
                                                  f"?per_page=100&page={page}"),
                self.token(), what, needs=ISSUES_READ)
            if not isinstance(data, list):
                break
            for event in data:
                if not isinstance(event, Mapping):
                    continue
                label = event.get("label")
                if not isinstance(label, Mapping) or label.get("name") != READY_LABEL:
                    continue
                at = _parse_time(event.get("created_at"))
                if at is None:
                    continue
                actor = (event.get("actor") or {}).get("login")
                if event.get("event") == "labeled" and isinstance(actor, str) and actor \
                        and (added is None or at >= added[1]):
                    added = (actor, at)
                elif event.get("event") == "unlabeled" and (removed is None or at > removed):
                    removed = at
            if len(data) < 100:
                break
        if added is None or (removed is not None and removed >= added[1]):
            return None
        return added

    def _permission(self, login: str) -> str:
        """The account's permission on the repository (the collaborator-
        permission read): `admin`, `write`, `read` or `none`. Not a
        collaborator is `none`."""
        try:
            data = self.writer._call(
                "GET", self.writer._url(self.ref, f"collaborators/{quote(login, safe='')}"
                                                  "/permission"),
                self.token(), f"a collaborator's permission on {self.repository}",
                needs=RULES_READ)
        except ForgeWriteNotFound:
            return "none"
        value = data.get("permission") if isinstance(data, Mapping) else None
        return value if isinstance(value, str) else "none"

    def _write_label_entry(self, entry_id: str, raw: Mapping[str, Any], number: int,
                           head: str, labelled_at: datetime) -> dict[str, Any] | None:
        """The labelled pull request's entry, QUEUED; None when it is queued
        or riding already, or was ejected at this very head."""
        ref = self.db.collection(ENTRIES_COLLECTION).document(entry_id)
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        transaction = self.db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any] | None:
            snap = _snapshot(txn.get(ref))
            data = snap.to_dict() if snap.exists else None
            if data is not None:
                if data.get("tenant_id") != self.tenant_id:
                    return None
                if data.get("state") in (QUEUED, RIDING):
                    return None
                if data.get("state") == EJECTED and data.get("head") == head:
                    return None
            same = data is not None and data.get("head") == head
            entry = {
                "entry_id": entry_id, "tenant_id": self.tenant_id, "task_id": None,
                "source": SOURCE_LABEL, "repository": self.repository, "base": self.base,
                "number": number, "head": head, "title": title[:256],
                "message": f"Landed by the SwarmCloud merge train of {self.base}",
                "state": QUEUED, "train_id": None,
                "returns": int((data or {}).get("returns") or 0) if same else 0,
                "enqueued_at": labelled_at, "updated_at": self.now,
            }
            txn.set(ref, entry)
            return entry

        return _apply(transaction)

    def _tell(self, told: dict[str, Any], number: int, head: str, reason: str,
              detail: str) -> None:
        """Say once per head and reason why a `ready` pull request cannot
        board (the script's `tell_skip`)."""
        key = str(number)
        if told.get(key) == f"{head}:{reason}":
            return
        told[key] = f"{head}:{reason}"
        self._comment(number, f"{TELL_MARKER}skip:{reason}:{head} -->\n"
                              f"**Not boarded on the merge train of `{self.base}`**: {detail}. "
                              "It boards on its own once this is resolved.")

    def _eject_labelled(self, rider: Mapping[str, Any], reason: str, detail: str) -> None:
        """An ejected labelled pull request: one comment giving the reason,
        and `ready` removed, so it does not board again until relabelled."""
        number = int(rider["number"])
        self._comment(number, f"{TELL_MARKER}eject:{reason}:{rider['head']} -->\n"
                              f"**Ejected from the merge train of `{self.base}`** "
                              f"({reason}): {detail}. `ready` was removed; add it again "
                              "once this is fixed.")
        try:
            self.writer._call(
                "DELETE", self.writer._url(self.ref, f"issues/{number}/labels/{READY_LABEL}"),
                self.token(), f"the {READY_LABEL} label of {self.repository}#{number}",
                needs=ISSUES_WRITE, ok=(200, 204))
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.info("merge train lane=%s: %s not removed from #%d (%s)", self.lane_id,
                     READY_LABEL, number, code)

    def _comment(self, number: int, body: str) -> None:
        """Best effort: a comment that fails is logged, never fatal."""
        try:
            self.writer.create_comment(
                IssueRef(owner=self.ref.owner, repo=self.ref.repo, number=number),
                body, self.token())
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.info("merge train lane=%s: comment on #%d failed (%s)", self.lane_id,
                     number, code)

    def _hand_to_repository(self, lane: Mapping[str, Any], train: Any) -> None:
        """The repository's own train is on (§8 A): compose and land nothing.
        A train this lane had is abandoned -- its riders queued again -- so
        two trains never compose on one base."""
        self.report.repository_train_on += 1
        if train:
            self._abandon(train, outcome=REPOSITORY_TRAIN_ON)
        if lane.get("status") != REPOSITORY_TRAIN_ON:
            self._update_lane(None, {"status": REPOSITORY_TRAIN_ON, "checked_at": self.now})

    def _eligible(self, entry: Mapping[str, Any]) -> str | None:
        """None when the entry may ride now; else what to do with it:
        'wait' (its step is not parked on the train yet) or 'ended'."""
        if entry.get("source") == SOURCE_LABEL:
            # No step: `_board_labelled` re-read it this tick, and the
            # compose below reads its pull request again.
            return None
        snap = self.db.collection("tasks").document(str(entry.get("task_id"))).get()
        task = snap.to_dict() if snap.exists else None
        if task is None or task.get("tenant_id") != self.tenant_id:
            return "ended"
        state = task.get("state")
        if state in {s.value for s in TERMINAL_STATES}:
            return "ended"
        if task.get("runner_profile") != "merge":
            return "ended"
        if _repo_of_url(task.get("repository_url")) != self.repository:
            return "ended"
        wait = (task.get("metadata") or {}).get(MERGE_WAIT_METADATA_KEY) or {}
        if (state != TaskState.PARKED.value
                or task.get("park_reason") != ParkReason.CI_PENDING.value
                or wait.get("code") != TRAIN_WAITING
                or wait.get("pull_request") != entry.get("number")
                or wait.get("head") != entry.get("head")):
            return "wait"
        return None

    def _compose(self, lane: Mapping[str, Any], queued: list[dict[str, Any]]) -> None:
        by_id: dict[str, dict[str, Any]] = {}
        for entry in queued:
            verdict = self._eligible(entry)
            if verdict == "ended":
                self._conclude(entry, EJECTED, reason="step_ended",
                               detail="the merge step that registered it has ended",
                               expect=(QUEUED,))
            elif verdict is None:
                by_id[str(entry["entry_id"])] = entry
        riders = [Rider(entry_id=i, number=int(e["number"]), head=str(e["head"]),
                        enqueued_at=e.get("enqueued_at") or self.now,
                        rank=1 if e.get("source") == SOURCE_LABEL else 0)
                  for i, e in by_id.items()]
        halves = _halves(lane)
        batch = select(riders, size=self.settings.train_size,
                       next_batch=halves[-1] if halves else [])
        if not batch:
            return
        if not (halves and halves[-1]) and self._gathering(riders):
            self.report.active = True
            self.report.waiting += 1
            return
        self.report.active = True
        chosen: list[dict[str, Any]] = []
        for rider in batch:
            entry = by_id[rider.entry_id]
            pull = self.writer.read_pull(self.ref, rider.number, self.token())
            if pull.merged:
                self._return(entry, "already_merged", expect=(QUEUED,))
            elif pull.state != "open":
                self._return(entry, "closed", "the pull request is closed", expect=(QUEUED,))
            elif pull.head_sha != rider.head:
                self._return(entry, "head_moved",
                             f"its head is {pull.head_sha}, not {rider.head}", expect=(QUEUED,))
            elif pull.base_ref != self.base:
                self._return(entry, "base_changed",
                             f"it now targets {pull.base_ref}", expect=(QUEUED,))
            else:
                chosen.append(entry)
        if not chosen:
            return
        seq = int(lane.get("seq") or 0) + 1
        train_id = f"{self.lane_id}-{seq}"
        branch = f"{BRANCH_PREFIX}{self.lane_id[4:16]}-{seq}"
        ride = [{key: e.get(key) for key in ("entry_id", "task_id", "number", "head", "title",
                                             "message", "returns", "source")} for e in chosen]
        train = {"id": train_id, "seq": seq, "state": "composing", "branch": branch,
                 "riders": ride, "started_at": self.now}
        if not self._update_lane(None, {"seq": seq, "train": train,
                                        "halves": _stored_halves(halves[:-1])}):
            return
        riders_in: list[dict[str, Any]] = []
        for rider in ride:
            if self._set_entry(rider["entry_id"], expect=(QUEUED,), head=rider["head"],
                               patch={"state": RIDING, "train_id": train_id}):
                riders_in.append(rider)
        base_sha = self.writer.branch_head(self.ref, self.base, self.token())
        if base_sha is None:
            raise NotFound(f"the base branch {self.base} of {self.repository} is not there")
        self.writer.create_branch(self.ref, branch, base_sha, self.token())
        included: list[dict[str, Any]] = []
        for rider in riders_in:
            merged = self.writer.merge_into_branch(
                self.ref, branch=branch, head=rider["head"], token=self.token(),
                message=f"Merge #{rider['number']} into merge train {seq} of {self.base}")
            if merged.conflict:
                self._return(rider, "train_conflict",
                             f"#{rider['number']} conflicts with the train ahead of it: "
                             f"{merged.message}", counts=True)
            else:
                included.append(rider)
        if not included:
            self._cleanup(branch, None)
            self._update_lane(train_id, {"train": None, "last": {
                "id": train_id, "outcome": "empty", "at": self.now}})
            return
        head_sha = self.writer.branch_head(self.ref, branch, self.token()) or base_sha
        tree_sha = self.writer.commit_tree(self.ref, head_sha, self.token())
        numbers = ", ".join(f"#{r['number']}" for r in included)
        pull, _ = self.writer.open_pull(
            self.ref, head=branch, base=self.base, draft=True, token=self.token(),
            title=f"Merge train {seq} of {self.base}: {numbers}",
            body=(f"SwarmCloud merge train {train_id}: {numbers}, composed on {self.base} "
                  f"at {base_sha}. A draft so that {self.base}'s required checks run on the "
                  "composed head; it is never merged itself. Each pull request lands on its "
                  "own, squashed at the head tested here. docs/merge-trains.md"),
        )
        self.report.composed += 1
        self._update_lane(train_id, {"train": {
            **train, "state": "testing", "riders": included, "base_sha": base_sha,
            "head_sha": head_sha, "tree_sha": tree_sha, "pull_request": pull.number,
        }})

    def _gathering(self, riders: Sequence[Rider]) -> bool:
        """Fewer than `gather_min` riders, the oldest boarded less than
        `gather_wait_minutes` ago: wait for company (§7). A lone change is
        never held longer than the wait."""
        if len({r.number for r in riders}) >= self.settings.gather_min:
            return False
        stamps = [r.enqueued_at for r in riders if isinstance(r.enqueued_at, datetime)]
        if len(stamps) < len(riders):
            return False
        return self.now - min(stamps) < timedelta(minutes=self.settings.gather_wait_minutes)

    def _reading(self, head_sha: str, branch: str | None = None) -> Any:
        """The CI reading at the train's composed head, on the train's own
        branch: another branch's push suites at that sha are dropped (#2946)."""
        return self._read_at(head_sha, branch)[0]

    def _test(self, train: Mapping[str, Any]) -> None:
        branch = train.get("branch")
        head_sha = str(train.get("head_sha"))
        reading, runs, statuses = self._read_at(
            head_sha, branch if isinstance(branch, str) and branch else None)
        started = train.get("started_at")
        started_at = started if isinstance(started, datetime) else self.now
        state = reading.state
        retried = train.get("retried_at")
        if state == RED and isinstance(retried, datetime) \
                and self.now - retried < timedelta(seconds=RETRY_SETTLE_SECONDS):
            state = "pending"
        verdict = judge(state, started_at=started_at, now=self.now,
                        max_age_minutes=self.settings.max_train_age_minutes)
        if verdict == "pending":
            missing = not_started(self._judged(), runs, statuses)
            if missing and self.now - started_at >= timedelta(
                    minutes=self.settings.required_start_minutes):
                self._fail(train, "required_check_not_started",
                           f"{', '.join(missing)} never started")
                return
            self.report.waiting += 1
            self._update_lane(train["id"], {"checked_at": self.now})
            return
        if verdict == "green":
            landing = {**train, "state": "landing", "green_head": head_sha}
            if self._update_lane(train["id"], {"train": landing}):
                self._land(landing)
            return
        failing = ", ".join(reading.failing_names()) or ", ".join(reading.pending) or "no check"
        if verdict == "red":
            if self._retry(train, reading):
                return
            if self._hold_for_base(train):
                return
        self._fail(train, "train_red" if verdict == "red" else "train_timed_out", failing)

    def _retry(self, train: Mapping[str, Any], reading: Any) -> bool:
        """Re-run a red train's failed Actions runs ONCE before bisecting it
        (the script's `retry_failed_runs`): a flaky check must not eject a
        culprit-free train. False when it was retried already, names no
        Actions run, or GitHub refused the re-run."""
        if train.get("retried_at") is not None:
            return False
        run_ids = failed_workflow_runs(reading)
        if not run_ids:
            return False
        try:
            for run_id in run_ids:
                self.writer.rerun_failed_jobs(self.ref, run_id, self.token())
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.info("merge train %s tenant=%s: re-run refused (%s)", train["id"],
                     self.tenant_id, code)
            return False
        self.report.waiting += 1
        self._update_lane(train["id"], {"train": {**train, "retried_at": self.now,
                                                  "retried_runs": run_ids}})
        return True

    def _hold_for_base(self, train: Mapping[str, Any]) -> bool:
        """While the base's own required checks are red at its head, a red
        train is held, not bisected, up to `base_red_wait_minutes` (the
        script's `main_red_gate`): a red base makes every train red."""
        if self.settings.base_red_wait_minutes <= 0 or not self._base_red():
            return False
        since = train.get("base_red_since")
        since = since if isinstance(since, datetime) else self.now
        if self.now - since >= timedelta(minutes=self.settings.base_red_wait_minutes):
            return False
        self.report.waiting += 1
        self._update_lane(train["id"], {"train": {**train, "base_red_since": since},
                                        "status": "base_red"})
        return True

    def _fail(self, train: Mapping[str, Any], reason: str, failing: str) -> None:
        riders = list(train.get("riders") or [])
        numbers = ", ".join(f"#{r['number']}" for r in riders)
        plan = on_failure([Rider(entry_id=r["entry_id"], number=int(r["number"]),
                                 head=r["head"], enqueued_at=None) for r in riders])
        for rider in riders:
            if rider["entry_id"] in plan.eject:
                self._conclude(rider, EJECTED, reason=reason,
                               detail=(f"{failing} on merge train {train.get('seq')} "
                                       f"({numbers}) at {train.get('head_sha')}"))
            else:
                self._requeue(rider)
        self._cleanup(train.get("branch"), train.get("pull_request"))
        halves = _halves(self._lane())
        if plan.next_batch:
            halves += [plan.later, plan.next_batch]
        self._update_lane(train["id"], {"train": None, "halves": _stored_halves(halves), "last": {
            "id": train["id"], "outcome": reason, "numbers": [r["number"] for r in riders],
            "failing": failing, "at": self.now}})

    def _land(self, train: Mapping[str, Any]) -> None:
        riders = list(train.get("riders") or [])
        if not self._may_land(train):
            return
        landed: list[int] = []
        stopped = False
        for index, rider in enumerate(riders):
            current = self.db.collection(ENTRIES_COLLECTION).document(rider["entry_id"]).get()
            entry = current.to_dict() if current.exists else {}
            if entry.get("state") == LANDED and entry.get("train_id") == train["id"]:
                landed.append(rider["number"])
                continue
            pull = self.writer.read_pull(self.ref, int(rider["number"]), self.token())
            if pull.merged and pull.head_sha == rider["head"]:
                # A landing whose answer was lost: it stands.
                self._conclude(rider, LANDED, extra={"train_id": train["id"],
                                                     "landed_at": self.now})
                landed.append(rider["number"])
                continue
            if self._train_posts():
                self._post_train_context(rider, train)
            answer = self.writer.merge_pull(
                self.ref, int(rider["number"]), sha=rider["head"], token=self.token(),
                title=f"{rider.get('title') or 'Merge'} (#{rider['number']})",
                message=str(rider.get("message") or ""))
            if answer.merged:
                self._conclude(rider, LANDED, extra={"train_id": train["id"],
                                                     "merge_commit": answer.sha,
                                                     "landed_at": self.now})
                landed.append(rider["number"])
                continue
            # Counted: a base that refuses every second landing (a strict
            # up-to-date policy) must not bounce a pull request forever.
            self._return(rider, "land_refused",
                         f"GitHub refused to land #{rider['number']}: {answer.message}",
                         counts=True)
            for rest in riders[index + 1:]:
                self._requeue(rest)
            stopped = True
            break
        tree_verified: bool | None = None
        if not stopped:
            base_head = self.writer.branch_head(self.ref, self.base, self.token())
            tree_verified = bool(base_head) and self.writer.commit_tree(
                self.ref, base_head, self.token()) == train.get("tree_sha")
            if not tree_verified:
                log.warning("merge train %s tenant=%s: %s's tree after landing is not the "
                            "tested tree", train["id"], self.tenant_id, self.base)
        self._cleanup(train.get("branch"), train.get("pull_request"))
        self._update_lane(train["id"], {"train": None, "last": {
            "id": train["id"], "outcome": "partial" if stopped else "landed",
            "numbers": [r["number"] for r in riders], "landed": landed,
            "tree_verified": tree_verified, "at": self.now}})

    def _may_land(self, train: Mapping[str, Any]) -> bool:
        """THE LANDING CHECK. A rider is merged only when the train's
        required checks were read green at its tested head; a train in
        `landing` with no such reading is read again now, and one that is
        not green lands nothing and queues its riders again. Nothing lands
        while the base's own required checks are red at its head: the train
        waits up to `base_red_wait_minutes`, then is abandoned."""
        head_sha = train.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha:
            self._abandon(train, outcome="not_read_green")
            return False
        if train.get("green_head") != head_sha:
            branch = train.get("branch")
            reading = self._reading(head_sha, branch if isinstance(branch, str) and branch
                                    else None)
            if reading.state != GREEN:
                log.warning("merge train %s tenant=%s: in landing, but its checks are not "
                            "green at %s; nothing lands", train["id"], self.tenant_id,
                            head_sha)
                self._abandon(train, outcome="not_read_green")
                return False
        if self._base_red():
            since = train.get("base_red_since")
            since = since if isinstance(since, datetime) else self.now
            if self.now - since >= timedelta(minutes=self.settings.base_red_wait_minutes):
                self._abandon(train, outcome="base_red")
            else:
                self.report.waiting += 1
                self._update_lane(train["id"], {"train": {**train, "base_red_since": since},
                                                "status": "base_red"})
            return False
        return True

    def _post_train_context(self, rider: Mapping[str, Any], train: Mapping[str, Any]) -> None:
        """The required context only the train produces, posted at the
        rider's head just before it lands, as `land_train` does: the train's
        own judge read every other required check green at the tested head."""
        self.writer._call(
            "POST", self.writer._url(self.ref, f"statuses/{rider['head']}"), self.token(),
            f"the {TRAIN_CONTEXT} status at {self.repository}@{str(rider['head'])[:12]}",
            needs="statuses: write", ok=(200, 201),
            payload={"state": "success", "context": TRAIN_CONTEXT,
                     "description": f"Tested in SwarmCloud merge train {train.get('seq')} "
                                    f"of {self.base}"[:140]})

    def _abandon(self, train: Mapping[str, Any], *, outcome: str = "abandoned") -> None:
        for rider in train.get("riders") or []:
            self._requeue(rider)
        self._cleanup(train.get("branch"), train.get("pull_request"))
        self._update_lane(train["id"], {"train": None, "last": {
            "id": train["id"], "outcome": outcome, "at": self.now}})

    def _cleanup(self, branch: Any, pull_request: Any) -> None:
        """Close the draft and delete the branch. Best effort: a leftover
        branch costs nothing and the next train's has another name."""
        try:
            if isinstance(pull_request, int):
                self.writer.close_pull(self.ref, pull_request, self.token())
            if isinstance(branch, str) and branch.startswith(BRANCH_PREFIX):
                self.writer.delete_branch(self.ref, branch, self.token())
        except Exception as exc:
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.warning("merge train lane=%s: cleanup of %s failed (%s)",
                        self.lane_id, branch, code)
