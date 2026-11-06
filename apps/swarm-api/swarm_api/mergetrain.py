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

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES, ParkReason, TaskState

from .errors import ApiError, NotFound
from .forgechecks import GREEN, RED, evaluate, required_status_checks
from .forgewrite import GitHubWriter
from .judgment import settings_mac
from .mergesettings import MergeSettings, MergeSettingsStore
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
#: the step's own two (`MERGE_CONFLICT_ROUNDS_MAX`), so a pull request that
#: conflicts with everything stops holding its place.
MAX_RETURNS = 3
#: A lane claimed for a train that never reached testing -- a tick that
#: died mid-compose -- is abandoned after this long, its riders queued again.
COMPOSE_STALE_SECONDS = 600
#: Lanes stepped per tick, oldest activity first; the rest wait one minute.
MAX_LANES_PER_TICK = 20

_GITHUB_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)


def entry_id_for(tenant_id: str, task_id: str) -> str:
    """`agent_worker.mergetrain.entry_id_for`, restated (held equal by a test)."""
    digest = hashlib.sha256(f"{tenant_id}\n{task_id}".encode("utf-8")).hexdigest()
    return "mte_" + digest[:32]


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


def _order(rider: Rider) -> tuple[Any, int, str]:
    return (rider.enqueued_at, rider.number, rider.entry_id)


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
    #: Whether any lane did something or still has a train: false is a
    #: quiet tenant, which costs nothing until a step registers again.
    active: bool = False
    truncated: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {"lanes": self.lanes, "composed": self.composed, "landed": self.landed,
                "ejected": self.ejected, "returned": self.returned,
                "waiting": self.waiting, "failed": self.failed, "active": self.active,
                "truncated": self.truncated}

    def fail(self, lane: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"lane": lane, "code": code})


# --------------------------------------------------------------------------
# the coordinator
# --------------------------------------------------------------------------

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
    if not lanes:
        return report
    settings = MergeSettingsStore(db, now=ctx.now, mac=settings_mac(ctx.settings)).get(tenant_id)
    held: dict[str, str] = {}

    def token() -> str:
        if "value" not in held:
            held["value"] = ctx.forge_tokens.token_for(tenant)
        return held["value"]

    try:
        for (repository, base), entries in sorted(lanes.items())[:MAX_LANES_PER_TICK]:
            report.lanes += 1
            lane_id = lane_id_for(tenant_id, repository, base)
            try:
                _Lane(ctx, tenant_id, repository, base, lane_id, settings, token, report).step(
                    entries)
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

    # -- the step ---------------------------------------------------------------
    def step(self, entries: list[dict[str, Any]]) -> None:
        lane = self._lane()
        train = lane.get("train")
        if not train:
            self._heal(entries, None)
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

    def _eligible(self, entry: Mapping[str, Any]) -> str | None:
        """None when the entry may ride now; else what to do with it:
        'wait' (its step is not parked on the train yet) or 'ended'."""
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
                        enqueued_at=e.get("enqueued_at") or self.now)
                  for i, e in by_id.items()]
        halves = _halves(lane)
        batch = select(riders, size=self.settings.train_size,
                       next_batch=halves[-1] if halves else [])
        if not batch:
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
                                             "message", "returns")} for e in chosen]
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

    def _reading(self, head_sha: str, branch: str | None = None) -> Any:
        """The CI reading at the train's composed head, on the train's own
        branch: another branch's push suites at that sha are dropped (#2946)."""
        token = self.token()
        required = required_status_checks(self.writer.branch_rules(self.ref, self.base, token))
        return evaluate(required, self.writer.check_runs(self.ref, head_sha, token, head_ref=branch),
                        self.writer.commit_statuses(self.ref, head_sha, token))

    def _test(self, train: Mapping[str, Any]) -> None:
        branch = train.get("branch")
        reading = self._reading(str(train.get("head_sha")),
                                branch if isinstance(branch, str) and branch else None)
        started = train.get("started_at")
        verdict = judge(reading.state, started_at=started if isinstance(started, datetime)
                        else self.now, now=self.now,
                        max_age_minutes=self.settings.max_train_age_minutes)
        if verdict == "pending":
            self.report.waiting += 1
            self._update_lane(train["id"], {"checked_at": self.now})
            return
        if verdict == "green":
            if self._update_lane(train["id"], {"train": {**train, "state": "landing"}}):
                self._land({**train, "state": "landing"})
            return
        failing = ", ".join(reading.failing_names()) or ", ".join(reading.pending) or "no check"
        self._fail(train, "train_red" if verdict == "red" else "train_timed_out", failing)

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

    def _abandon(self, train: Mapping[str, Any]) -> None:
        for rider in train.get("riders") or []:
            self._requeue(rider)
        self._cleanup(train.get("branch"), train.get("pull_request"))
        self._update_lane(train["id"], {"train": None, "last": {
            "id": train["id"], "outcome": "abandoned", "at": self.now}})

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
