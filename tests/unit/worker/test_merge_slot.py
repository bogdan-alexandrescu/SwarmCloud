"""The per-repository merge slot: merge steps against one base land one at a time (#295).

Measured 2026-10-10: 16 merge steps against one `main` that requires an
up-to-date branch ran at once; each landing merge put the rest behind, and 7
refused `behind_too_often`. `agent_worker.mergeslot` gives each (tenant,
repository, base) one slot: a step takes it once its checks are green,
waits for it PARKED (holding nothing) in submission order, and gives it back
on every exit -- or loses it to the lease when it crashes.

`Repo` below is GitHub as the merge step sees it, in Python behind
`merge.ForgeMerger`: a base that moves on every merge, a strict up-to-date
rule (a merge onto an old base is GitHub's 405 "out of date"), update-branch
as GitHub makes it, and CI that takes `CI_SECONDS` of a fake clock at every
new head. `Driver` is the rest of the platform as far as a merge step can
tell: the park the lifecycle writes, swarm-api's wake tick (which marks a CI
park once its checks settle, and skips a slot wait), and the scheduler,
which wakes a park on its marker or its fallback instant. The driver runs
due steps in REVERSE submission order, so the order the merges land in is
the slot's, not the driver's.

WITHOUT THE CHANGE: every step updates its branch the moment it reads it
behind, each merge puts the rest behind again, and the N-step case ends in
`behind_too_often` refusals and out-of-order merges; with the slot store
removed (`merge_slots=None`) the merge refuses `merge_slot_unavailable`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import merge, mergeslot, post_verdict
from agent_worker.errors import ExitCode, FencedError
from swarm_common.models import EndCause
from swarm_common.states import EventType, ParkReason, TaskState

from fake_github import fresh_token
from fakes import FakeFirestore, FakeTransactionRunner
from merge_world import LogCapture

TENANT = "eng"
BASE = "main"
CI_SECONDS = 20 * 60
T0 = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
_SHAS = count(1)


def _sha() -> str:
    return f"{next(_SHAS):040x}"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# GitHub, behind `merge.ForgeMerger`
# ---------------------------------------------------------------------------


@dataclass
class Pull:
    number: int
    head: str
    based_on: str
    red: bool = False
    state: str = "open"
    merged: bool = False
    merge_commit: str | None = None


class Repo:
    """One repository whose `main` requires `ci` green on an up-to-date branch."""

    def __init__(self, full_name: str, clock: Clock) -> None:
        self.full_name = full_name
        self.clock = clock
        self.tip = _sha()
        self.on_main = {self.tip}
        self.pulls: dict[int, Pull] = {}
        self.commits: dict[str, merge.CommitFacts] = {}
        self.ci: dict[str, tuple[datetime, str]] = {}
        self.merged: list[int] = []
        self.updates: list[int] = []
        #: Every merge call: (number, whether the branch was up to date).
        self.merge_calls: list[tuple[int, bool]] = []

    # -- what a test sets up -------------------------------------------
    def open(self, number: int, *, ci_seconds: float = CI_SECONDS, red: bool = False,
             behind: bool = False) -> str:
        head = _sha()
        based = self.tip
        if behind:
            # Opened on a base that has moved on since.
            self.tip = _sha()
            self.on_main.add(self.tip)
        self.pulls[number] = Pull(number, head, based, red=red)
        self.ci[head] = (self.clock() + timedelta(seconds=ci_seconds),
                         "failure" if red else "success")
        return head

    def land_elsewhere(self) -> None:
        """Someone outside the slot merges into main (a person, by hand)."""
        self.tip = _sha()
        self.on_main.add(self.tip)

    # -- ForgeMerger ------------------------------------------------------
    def can_push(self) -> bool:
        return True

    def default_branch(self) -> str:
        return BASE

    def pull_request(self, number: int) -> merge.PullRequestFacts:
        pull = self.pulls[number]
        return merge.PullRequestFacts(
            number=number, state=pull.state, merged=pull.merged, draft=False,
            title=f"Pull request {number} lands its change", head_sha=pull.head,
            head_ref=f"feature/{number}", head_repo=self.full_name, base_ref=BASE,
            base_repo=self.full_name, mergeable=True, merge_commit_sha=pull.merge_commit,
            mergeable_state="behind" if pull.based_on != self.tip else "clean",
        )

    def required_checks(self, branch: str) -> merge.RequiredChecks:
        return merge.RequiredChecks(checks=(forge_mod.RequiredCheck("ci", None),),
                                    protected=True)

    def checks_at(self, sha: str) -> list[merge.CheckFacts]:
        ready, conclusion = self.ci[sha]
        if self.clock() < ready:
            return [merge.CheckFacts("ci", "in_progress", None)]
        return [merge.CheckFacts("ci", "completed", conclusion)]

    def settled(self, sha: str) -> bool:
        return sha in self.ci and self.clock() >= self.ci[sha][0]

    def commit(self, sha: str) -> merge.CommitFacts:
        return self.commits[sha]

    def on_base(self, base: str, sha: str) -> bool:
        return sha in self.on_main

    def update_branch(self, number: int, *, expected_head_sha: str) -> merge.UpdateAnswer:
        pull = self.pulls[number]
        if pull.head != expected_head_sha:
            return merge.UpdateAnswer(422, "expected head sha didn't match current head ref.")
        new = _sha()
        self.commits[new] = merge.CommitFacts(new, (pull.head, self.tip),
                                              merge.GITHUB_COMMITTER_EMAIL, True)
        pull.head, pull.based_on = new, self.tip
        self.ci[new] = (self.clock() + timedelta(seconds=CI_SECONDS),
                        "failure" if pull.red else "success")
        self.updates.append(number)
        return merge.UpdateAnswer(202, "Updating pull request branch.")

    def merge(self, number: int, *, sha: str, title: str, message: str) -> merge.MergeAnswer:
        pull = self.pulls[number]
        up_to_date = pull.based_on == self.tip
        self.merge_calls.append((number, up_to_date))
        if pull.head != sha:
            return merge.MergeAnswer(False, 409, message="Head branch was modified.")
        if not up_to_date:
            return merge.MergeAnswer(False, 405, message="Head branch is out of date")
        self.tip = _sha()
        self.on_main.add(self.tip)
        pull.merged, pull.state, pull.merge_commit = True, "closed", self.tip
        self.merged.append(number)
        return merge.MergeAnswer(True, 200, sha=self.tip)

    def enqueue(self, number: int, *, node_id: str, expected_head_sha: str):
        raise AssertionError("no merge queue here")

    def queue_state(self, number: int):
        raise AssertionError("no merge queue here")

    def comment(self, number: int, body: str) -> bool:
        return True

    def closing_issues(self, number: int) -> merge.ClosingReferences:
        return merge.ClosingReferences()

    def close_issue(self, number: int, *, comment: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# The platform around a merge step
# ---------------------------------------------------------------------------


@dataclass
class Step:
    task_id: str
    repo: Repo
    number: int
    pinned: str
    outcomes: list[post_verdict.ActionOutcome] = field(default_factory=list)


class Driver:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = Clock()
        self.db = FakeFirestore()
        self.repos: dict[str, Repo] = {}
        self.steps: list[Step] = []
        self.log = LogCapture()
        self.attempts = 0
        self.crashed: set[str] = set()
        monkeypatch.setattr(merge, "merger_for", self._merger_for)

    def _merger_for(self, url: str | None, **_kw: Any) -> Repo:
        full = (url or "").removeprefix("https://github.com/").removesuffix(".git")
        return self.repos[full]

    def repo(self, full_name: str) -> Repo:
        return self.repos.setdefault(full_name, Repo(full_name, self.clock))

    def submit(self, repo: Repo, number: int, **opened: Any) -> Step:
        """A `merge_pr` step for pull request `number`, submitted now."""
        pinned = repo.open(number, **opened)
        task_id = f"task_{repo.full_name.split('/')[1]}_{number}"
        self.db.seed(f"tasks/{task_id}", {
            "id": task_id, "tenant_id": TENANT, "state": TaskState.READY.value,
            "created_at": self.clock() + timedelta(milliseconds=len(self.steps)),
            "metadata": {},
        })
        step = Step(task_id, repo, number, pinned)
        self.steps.append(step)
        return step

    def slots(self) -> mergeslot.FirestoreMergeSlots:
        return mergeslot.FirestoreMergeSlots(
            self.db, tenant_id=TENANT, run_transaction=FakeTransactionRunner(self.db).run,
            fence=lambda _txn: None, clock=self.clock)

    def doc(self, step: Step) -> dict[str, Any]:
        return self.db.doc(f"tasks/{step.task_id}")

    def context(self, step: Step) -> post_verdict.ActionContext:
        def fetch(task_id: str) -> dict[str, Any]:
            return self.db.doc(f"tasks/{task_id}")

        return post_verdict.ActionContext(
            tenant_id=TENANT, task_id=step.task_id, attempt_id=f"att_{self.attempts}",
            workflow_id="wf_merge", store=None,  # type: ignore[arg-type]
            dispatch={"merge_target": {"number": step.number, "head_sha": step.pinned}},
            fetch_upstream=fetch, verify_upstream=lambda *_a, **_k: None,
            read_app_key=lambda: pytest.fail("the merge read an App key"),
            environ={}, recheck=lambda: False, reap=lambda: (), unprotected=None,
            scrub=lambda text: text, log=self.log, read_git_token=fresh_token,
            repository_url=f"https://github.com/{step.repo.full_name}.git",
            merge_slots=self.slots(),
        )

    def attempt(self, step: Step) -> post_verdict.ActionOutcome:
        """One attempt, ended the way the lifecycle ends it."""
        self.attempts += 1
        doc = self.doc(step)
        doc["state"] = TaskState.RUNNING.value
        outcome = merge.run_merge(self.context(step))
        step.outcomes.append(outcome)
        if outcome.ci_wait is not None:
            wait = dict(outcome.ci_wait)
            wait["parked_at"] = self.clock()
            doc.update(state=TaskState.PARKED.value, park_reason=ParkReason.CI_PENDING.value,
                       next_eligible_at=self.clock() + timedelta(
                           seconds=merge.MERGE_CI_FALLBACK_SECONDS))
            doc["metadata"] = {"merge_wait": wait}
        else:
            doc.update(state=outcome.state.value, park_reason=None)
        return outcome

    def due(self, step: Step) -> bool:
        doc = self.doc(step)
        if step.task_id in self.crashed:
            return False
        if doc["state"] == TaskState.READY.value:
            return True
        if doc["state"] != TaskState.PARKED.value:
            return False
        wait = doc["metadata"].get("merge_wait") or {}
        if wait.get("wake_requested_at") or doc["next_eligible_at"] <= self.clock():
            return True
        # swarm-api's wake tick: a CI park is marked once its checks settle;
        # a slot wait is left to the release (`mergewake.MERGE_SLOT_WAIT`).
        return wait.get("code") != mergeslot.MERGE_SLOT_WAIT and step.repo.settled(wait["head"])

    def run(self, *, until: timedelta, tick: float = 60) -> None:
        end = self.clock() + until
        while self.clock() < end:
            for step in reversed(self.steps):
                if self.due(step):
                    self.attempt(step)
            if all(self.doc(s)["state"] in {TaskState.SUCCEEDED.value, TaskState.FAILED.value}
                   for s in self.steps if s.task_id not in self.crashed):
                return
            self.clock.advance(tick)

    def slot(self, repo: Repo) -> dict[str, Any]:
        return self.db.doc(
            f"{mergeslot.MERGE_SLOTS_COLLECTION}/{mergeslot.slot_id(TENANT, repo.full_name, BASE)}")

    def codes(self, step: Step) -> list[str]:
        return [(o.ci_wait or {}).get("code") or o.state.value for o in step.outcomes]


@pytest.fixture
def driver(monkeypatch) -> Driver:
    return Driver(monkeypatch)


def _refusals(step: Step) -> list[str]:
    return [o.summary["refusal"]["code"] for o in step.outcomes if "refusal" in o.summary]


# ---------------------------------------------------------------------------
# The cases the lane names
# ---------------------------------------------------------------------------


N = 8


def test_n_merge_steps_against_one_repository_land_serially_in_submission_order(driver):
    repo = driver.repo("octo-org/widget-shop")
    # The first was opened on an old base: it takes the slot green and must
    # update, so every other step reads its checks green while the slot is
    # held, and queues. The rest are green five minutes in.
    steps = [driver.submit(repo, 1, ci_seconds=0, behind=True)]
    steps += [driver.submit(repo, n, ci_seconds=300) for n in range(2, N + 1)]

    driver.run(until=timedelta(hours=12))

    assert repo.merged == list(range(1, N + 1)), "the merges landed out of submission order"
    for step in steps:
        assert driver.doc(step)["state"] == TaskState.SUCCEEDED.value, (step.number,
                                                                        driver.codes(step))
        assert _refusals(step) == [], (step.number, _refusals(step))
    # One catch-up update each, made while holding the slot; never a merge
    # attempted onto an old base, never a second update.
    assert sorted(repo.updates) == list(range(1, N + 1))
    assert all(up_to_date for _, up_to_date in repo.merge_calls), repo.merge_calls
    # The waiters parked on the slot and were woken by its handoff.
    for step in steps[1:]:
        assert mergeslot.MERGE_SLOT_WAIT in driver.codes(step), driver.codes(step)
    slot = driver.slot(repo)
    assert slot["holder"] is None and slot["waiters"] == []
    assert slot["generation"] == N


def test_a_waiter_does_not_spin_it_wakes_on_the_handoff_or_the_fallback(driver):
    repo = driver.repo("octo-org/widget-shop")
    driver.submit(repo, 1, ci_seconds=0, behind=True)
    last = None
    for n in range(2, N + 1):
        last = driver.submit(repo, n, ci_seconds=300)
    driver.run(until=timedelta(hours=12))
    assert last is not None
    waits = driver.codes(last).count(mergeslot.MERGE_SLOT_WAIT)
    # The last waiter queued behind N-1 merges of one CI run each; it may
    # wake once per fallback, and not once per tick.
    behind = (N - 1) * CI_SECONDS
    assert waits <= behind // merge.MERGE_CI_FALLBACK_SECONDS + N, waits
    assert len(last.outcomes) < behind // 60 / 4


def test_the_handoff_marks_the_next_waiters_park_for_the_scheduler(driver):
    repo = driver.repo("octo-org/widget-shop")
    first = driver.submit(repo, 1, ci_seconds=0, behind=True)
    second = driver.submit(repo, 2, ci_seconds=0)
    driver.attempt(first)
    assert driver.codes(first) == ["branch_updated"]
    driver.attempt(second)
    assert driver.codes(second) == [mergeslot.MERGE_SLOT_WAIT]
    assert driver.slot(repo)["waiters"][0]["task_id"] == second.task_id

    driver.clock.advance(CI_SECONDS)
    driver.attempt(first)
    assert repo.merged == [1]

    wait = driver.doc(second)["metadata"]["merge_wait"]
    assert wait["wake_requested_at"] == driver.clock()
    assert wait["wake_reason"] == mergeslot.SLOT_WAKE_REASON
    assert driver.slot(repo)["holder"]["task_id"] == second.task_id
    assert first.outcomes[-1].summary["merge_slot"]["handed_to"] == second.task_id


def test_a_crashed_holder_frees_the_slot_when_its_lease_runs_out(driver):
    repo = driver.repo("octo-org/widget-shop")
    holder = driver.submit(repo, 1, ci_seconds=0, behind=True)
    waiter = driver.submit(repo, 2, ci_seconds=0)
    driver.attempt(holder)
    assert driver.slot(repo)["holder"]["task_id"] == holder.task_id
    # The holder's worker dies mid-attempt: its task is still RUNNING, and
    # nothing of it ever runs again.
    driver.doc(holder)["state"] = TaskState.RUNNING.value
    driver.crashed.add(holder.task_id)

    driver.run(until=timedelta(seconds=mergeslot.MERGE_SLOT_LEASE_SECONDS - 120))
    assert repo.merged == [], "the waiter merged while the crashed holder's lease was live"
    assert set(driver.codes(waiter)) == {mergeslot.MERGE_SLOT_WAIT}

    driver.run(until=timedelta(hours=2))
    assert repo.merged == [2]
    assert driver.doc(waiter)["state"] == TaskState.SUCCEEDED.value
    assert driver.clock() >= T0 + timedelta(seconds=mergeslot.MERGE_SLOT_LEASE_SECONDS)
    taken = waiter.outcomes[-1].summary["merge_slot"]
    assert taken["generation"] == 2 and taken["released"] == "succeeded"


def test_a_holder_whose_task_ended_frees_the_slot_at_once(driver):
    repo = driver.repo("octo-org/widget-shop")
    holder = driver.submit(repo, 1, ci_seconds=0, behind=True)
    waiter = driver.submit(repo, 2, ci_seconds=0)
    driver.attempt(holder)
    driver.doc(holder)["state"] = TaskState.DEAD_LETTERED.value
    driver.crashed.add(holder.task_id)

    driver.attempt(waiter)
    assert driver.codes(waiter) == ["SUCCEEDED"], "the waiter waited on a holder that had ended"
    assert repo.merged == [2]
    assert waiter.outcomes[-1].summary["merge_slot"]["generation"] == 2


def test_a_red_head_releases_the_slot_and_fails_cleanly(driver):
    repo = driver.repo("octo-org/widget-shop")
    red = driver.submit(repo, 1, ci_seconds=0, behind=True, red=False)
    waiter = driver.submit(repo, 2, ci_seconds=0)
    driver.attempt(red)
    driver.attempt(waiter)
    # The update's CI comes back red.
    repo.ci[repo.pulls[1].head] = (driver.clock() + timedelta(seconds=CI_SECONDS), "failure")
    driver.clock.advance(CI_SECONDS)

    outcome = driver.attempt(red)

    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "checks_failed"
    assert outcome.summary["merge_slot"]["released"] == "checks_failed"
    assert repo.merged == [] and 1 not in [n for n, _ in repo.merge_calls]
    slot = driver.slot(repo)
    assert slot["holder"]["task_id"] == waiter.task_id
    assert driver.doc(waiter)["metadata"]["merge_wait"]["wake_requested_at"] is not None

    driver.run(until=timedelta(hours=2))
    assert repo.merged == [2]


def test_two_repositories_do_not_block_each_other(driver):
    shop = driver.repo("octo-org/widget-shop")
    docs = driver.repo("octo-org/widget-docs")
    held = driver.submit(shop, 1, ci_seconds=0, behind=True)
    other = driver.submit(docs, 1, ci_seconds=0)
    driver.attempt(held)
    assert driver.slot(shop)["holder"]["task_id"] == held.task_id

    outcome = driver.attempt(other)

    assert outcome.state is TaskState.SUCCEEDED, driver.codes(other)
    assert docs.merged == [1] and shop.merged == []
    assert mergeslot.slot_id(TENANT, shop.full_name, BASE) != mergeslot.slot_id(
        TENANT, docs.full_name, BASE)
    # The control: a second step in the SAME repository does wait.
    same = driver.submit(shop, 2, ci_seconds=0)
    assert driver.codes(same) == [] and driver.attempt(same).ci_wait["code"] == (
        mergeslot.MERGE_SLOT_WAIT)


def test_a_base_moved_under_a_held_slot_is_updated_again_and_counted(driver):
    repo = driver.repo("octo-org/widget-shop")
    step = driver.submit(repo, 1, ci_seconds=0, behind=True)
    driver.attempt(step)
    driver.clock.advance(CI_SECONDS)
    repo.land_elsewhere()
    outcome = driver.attempt(step)
    assert outcome.ci_wait["code"] == "branch_updated"
    assert outcome.summary["updates_while_held"] == 1
    driver.run(until=timedelta(hours=1))
    assert repo.merged == [1]


def test_cancelled_releases_the_slot(driver):
    repo = driver.repo("octo-org/widget-shop")
    holder = driver.submit(repo, 1, ci_seconds=0, behind=True)
    waiter = driver.submit(repo, 2, ci_seconds=0)
    driver.attempt(holder)
    driver.attempt(waiter)
    ctx = driver.context(holder)
    ctx.recheck = lambda: True
    outcome = merge.run_merge(ctx)
    assert outcome.state is TaskState.CANCELLED
    assert driver.slot(repo)["holder"]["task_id"] == waiter.task_id


def test_a_merge_with_no_slot_store_refuses_before_the_token(driver):
    repo = driver.repo("octo-org/widget-shop")
    step = driver.submit(repo, 1, ci_seconds=0)
    ctx = driver.context(step)
    ctx.merge_slots = None
    ctx.read_git_token = lambda: pytest.fail("the token was read")
    outcome = merge.run_merge(ctx)
    assert outcome.summary["refusal"]["code"] == "merge_slot_unavailable"
    assert repo.merged == []


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


def _me(task_id: str, minute: int = 0) -> mergeslot.Claimant:
    return mergeslot.Claimant(task_id, f"att_{task_id}", T0 + timedelta(minutes=minute))


def test_waiters_are_taken_in_submission_order_not_arrival_order():
    doc, held = mergeslot.acquire(None, _me("a"), tenant_id=TENANT, repository="o/r",
                                  base=BASE, now=T0, updates=0, ended=set())
    assert isinstance(held, mergeslot.Held)
    for task, minute in (("late", 9), ("early", 1), ("middle", 5)):
        doc, waiting = mergeslot.acquire(doc, _me(task, minute), tenant_id=TENANT,
                                         repository="o/r", base=BASE, now=T0, updates=0,
                                         ended=set())
        assert isinstance(waiting, mergeslot.Waiting)
    assert [w["task_id"] for w in doc["waiters"]] == ["early", "middle", "late"]
    doc, handed = mergeslot.release(doc, _me("a"), now=T0, reason="merged", ended={"early"},
                                    generation=held.generation)
    assert handed == "middle", "an ended waiter was handed the slot"
    assert [w["task_id"] for w in doc["waiters"]] == ["late"]


def test_a_stale_generation_neither_releases_nor_hands_on():
    doc, held = mergeslot.acquire(None, _me("a"), tenant_id=TENANT, repository="o/r",
                                  base=BASE, now=T0, updates=0, ended=set())
    assert isinstance(held, mergeslot.Held)
    unchanged, handed = mergeslot.release(doc, _me("a"), now=T0, reason="merged", ended=set(),
                                          generation=held.generation + 1)
    assert unchanged is None and handed is None


def test_a_superseded_attempt_writes_no_slot():
    """Invariant 5: every slot transaction is fenced on the attempt's task."""
    db = FakeFirestore()

    def fence(_txn):
        raise FencedError(1, 2, "superseded")

    slots = mergeslot.FirestoreMergeSlots(db, tenant_id=TENANT,
                                          run_transaction=FakeTransactionRunner(db).run,
                                          fence=fence, clock=lambda: T0)
    use = mergeslot.SlotUse(slots, _me("a"), "o/r", task_state=lambda _t: None, base=BASE)
    with pytest.raises(FencedError):
        use.acquire(updates=0)
    assert not [p for p in db.documents if p.startswith(mergeslot.MERGE_SLOTS_COLLECTION)]


def test_another_tenants_slot_is_never_read():
    db = FakeFirestore()
    slot = mergeslot.slot_id(TENANT, "o/r", BASE)
    db.seed(f"{mergeslot.MERGE_SLOTS_COLLECTION}/{slot}", {"tenant_id": "other"})
    slots = mergeslot.FirestoreMergeSlots(db, tenant_id=TENANT,
                                          run_transaction=FakeTransactionRunner(db).run,
                                          fence=lambda _t: None, clock=lambda: T0)
    with pytest.raises(mergeslot.SlotTenantMismatch):
        slots.peek(slot)


def test_the_slot_wait_code_is_the_one_swarm_apis_wake_tick_skips():
    from swarm_api import mergewake

    assert mergeslot.MERGE_SLOT_WAIT == mergewake.MERGE_SLOT_WAIT
    assert mergeslot._MERGE_WAIT_KEY == mergewake.MERGE_WAIT_METADATA_KEY
    assert mergeslot._WAKE_MARKER == mergewake.WAKE_MARKER


# ---------------------------------------------------------------------------
# The park, through the production worker
# ---------------------------------------------------------------------------


@pytest.fixture
def no_runner(monkeypatch):
    from agent_worker import lifecycle
    from fakes import ExplodingChildProcess

    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)


def test_a_parked_waiter_holds_no_capacity(db, worker_factory, monkeypatch, no_runner):
    """Invariant 1: a step waiting for the slot is PARKED with no lease, its
    attempt refunded on the slot-wait counter, off the CI clock."""
    from worker_seeds import seed_attempt

    seed_attempt(db, runner_profile="merge", task_input={"prompt": "merge"})
    task = db.doc("tasks/task_1")
    task["workflow_id"] = "wf_1"
    task["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                     "merge_target": {"pull_request": "task_int"}},
                        "merge_wait": {"code": "checks_pending", "wakes": 3,
                                       "first_parked_at": T0}}
    slot = mergeslot.slot_id("eng", "octo-org/widget-shop", BASE)
    holder = {"task_id": "task_other", "generation": 1, "expires_at": T0 + timedelta(days=9),
              "submitted_at": T0}
    db.seed(f"{mergeslot.MERGE_SLOTS_COLLECTION}/{slot}",
            {"tenant_id": "eng", "generation": 1, "holder": holder, "waiters": []})
    seen: list[Any] = []

    def run(ctx):
        seen.append(ctx.merge_slots)
        use = mergeslot.SlotUse(ctx.merge_slots, mergeslot.Claimant("task_1", "att_1", T0),
                                "octo-org/widget-shop", task_state=lambda _t: None, base=BASE)
        waiting = use.acquire(updates=0)
        return merge._slot_wait(merge._Run(ctx), waiting, head="c" * 40, number=41, base=BASE)

    monkeypatch.setattr(merge, "run_merge", run)
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.PARKED

    assert isinstance(seen[0], mergeslot.FirestoreMergeSlots)
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.CI_PENDING.value
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert task["attempt_count"] == 0, "a slot wait spent an attempt"
    wait = task["metadata"]["merge_wait"]
    assert wait["code"] == mergeslot.MERGE_SLOT_WAIT
    assert wait["slot_waits"] == 1 and wait["wakes"] == 3, "a slot wait spent a CI wake"
    assert wait["first_parked_at"] is None, "a slot wait ran the CI clock"
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked[-1]["detail"]["code"] == mergeslot.MERGE_SLOT_WAIT
    waiters = db.doc(f"{mergeslot.MERGE_SLOTS_COLLECTION}/{slot}")["waiters"]
    assert [w["task_id"] for w in waiters] == ["task_1"]
