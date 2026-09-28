"""What scripts/concurrency-test.sh concludes from one consistent Firestore read.

#175: the suite printed "Leases, not pods, are what counted" and "Backlog cost
nothing while it waited", and asserted neither. Both cases re-checked the global
limit, so a scheduler that counted RUNNING instead of LEASED passed both.

The judgement now lives in `scripts/lib/concurrency-judge.jq`, a pure filter
over one sample -- the pools, the unreleased leases and the tasks, all read at
ONE Firestore `readTime` -- so the part of the suite that decides pass or fail
is tested here, offline, on the exact shapes the suite feeds it. The sample
reads themselves need a deployed platform and stay in `make concurrency-test`.

The states are taken from `swarm_common.states`, not typed here: the suite
passes common.sh's copies, and check-contract-parity.sh holds those to the enum.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_common.states import CONCURRENCY_STATES, PENDING_STATES, TaskState

REPO = Path(__file__).resolve().parents[3]
JUDGE = REPO / "scripts" / "lib" / "concurrency-judge.jq"
SUITE = REPO / "scripts" / "concurrency-test.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

CONCURRENCY = sorted(s.value for s in CONCURRENCY_STATES)
PENDING = sorted(s.value for s in PENDING_STATES)
PRE_RUNNING = sorted(s for s in CONCURRENCY if s != TaskState.RUNNING.value)
GRACE = 30


def _jq(mode: str, stdin: str, *, slurp: bool = False) -> dict:
    assert JUDGE.exists(), f"{JUDGE} is missing: concurrency-test.sh has nothing to judge its samples with"
    args = ["jq", "-c"]
    if slurp:
        args.append("-s")
    args += [
        "-f", str(JUDGE),
        "--arg", "mode", mode,
        "--argjson", "concurrency", json.dumps(CONCURRENCY),
        "--argjson", "pending", json.dumps(PENDING),
        "--argjson", "pre_running", json.dumps(PRE_RUNNING),
        "--argjson", "grace", str(GRACE),
    ]
    proc = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def judge(sample: dict) -> dict:
    return _jq("sample", json.dumps(sample))


def persist(judged: list[dict]) -> dict:
    return _jq("persist", "\n".join(json.dumps(j) for j in judged), slurp=True)


def lease(lid: str, task: str, units: int = 1, pools=("global", "runner:mock")) -> dict:
    return {"id": lid, "task_id": task, "units": units, "pools": list(pools)}


def task(tid: str, state: str, lease_id: str | None) -> dict:
    return {"id": tid, "state": state, "current_lease_id": lease_id}


def pool(pid: str, active: int) -> dict:
    return {"id": pid, "active": active, "hard_limit": 10}


def three_held(at: str = "2026-09-27T12:00:00Z", *, global_active: int = 3) -> dict:
    """Three admitted tasks: one LEASED, one DISPATCHED, one RUNNING."""
    return {
        "at": at,
        "pools": [pool("global", global_active), pool("runner:mock", 3), pool("resource:browser", 0)],
        "leases": [lease("l1", "t1"), lease("l2", "t2"), lease("l3", "t3")],
        "tasks": [
            task("t1", "LEASED", "l1"),
            task("t2", "DISPATCHED", "l2"),
            task("t3", "RUNNING", "l3"),
        ],
    }


# --- invariant 3: the pools count leases, from LEASED -----------------------


def test_pools_that_count_every_unreleased_lease_agree() -> None:
    out = judge(three_held())
    assert out["pool_mismatches"] == []
    assert out["unleased_holders"] == []
    assert out["leases"] == 3
    assert out["holding_tasks"] == 3
    # Two of the three leases belong to tasks that have not reached RUNNING, so
    # this sample is one that could tell LEASED counting from RUNNING counting.
    assert out["pre_running_leases"] == 2


def test_a_scheduler_that_counted_only_running_tasks_is_a_mismatch() -> None:
    """The defect #175 names: pools hold 1 unit while three leases hold 3."""
    out = judge(three_held(global_active=1))
    assert out["pool_mismatches"] == [{"pool": "global", "active": 1, "lease_units": 3}]


def test_units_are_weighted_by_the_lease_not_counted_per_lease() -> None:
    sample = {
        "at": "2026-09-27T12:00:00Z",
        "pools": [pool("resource:browser", 2), pool("global", 2)],
        "leases": [lease("lb", "tb", units=2, pools=("global", "resource:browser"))],
        "tasks": [task("tb", "STARTING", "lb")],
    }
    assert judge(sample)["pool_mismatches"] == []
    sample["pools"][0]["active"] = 1
    assert judge(sample)["pool_mismatches"] == [
        {"pool": "resource:browser", "active": 1, "lease_units": 2}
    ]


def test_capacity_held_with_no_lease_behind_it_is_a_mismatch() -> None:
    sample = three_held()
    sample["pools"][2]["active"] = 2  # resource:browser, which no lease names
    assert judge(sample)["pool_mismatches"] == [
        {"pool": "resource:browser", "active": 2, "lease_units": 0}
    ]


def test_a_holding_task_without_an_unreleased_lease_is_reported() -> None:
    sample = three_held()
    sample["tasks"].append(task("t9", "RUNNING", "l9"))
    out = judge(sample)
    assert [h["task"] for h in out["unleased_holders"]] == ["t9"]
    assert out["holding_tasks"] == 4


def test_a_sample_with_every_lease_running_cannot_tell_leased_from_running() -> None:
    sample = three_held()
    for t in sample["tasks"]:
        t["state"] = "RUNNING"
    assert judge(sample)["pre_running_leases"] == 0


# --- invariant 1: the backlog holds nothing --------------------------------


@pytest.mark.parametrize("state", PENDING)
def test_a_lease_held_by_a_waiting_task_is_reported(state: str) -> None:
    sample = three_held()
    sample["leases"].append(lease("l4", "t4"))
    sample["tasks"].append(task("t4", state, "l4"))
    sample["pools"][0]["active"] = 4
    sample["pools"][1]["active"] = 4
    out = judge(sample)
    assert [(b["lease"], b["state"]) for b in out["backlog_leases"]] == [("l4", state)]
    assert out["pool_mismatches"] == []


def test_a_waiting_task_with_no_lease_costs_nothing() -> None:
    sample = three_held()
    sample["tasks"].append(task("t5", "QUEUED", None))
    out = judge(sample)
    assert out["backlog_leases"] == []
    assert out["unleased_holders"] == []


# --- across samples: the two known two-transaction gaps get a grace ---------


def _backlog_sample(at: str) -> dict:
    sample = three_held(at)
    sample["leases"].append(lease("l4", "t4"))
    sample["tasks"].append(task("t4", "PARKED", "l4"))
    sample["pools"][0]["active"] = 4
    sample["pools"][1]["active"] = 4
    return sample


def test_a_park_that_releases_within_the_grace_is_not_a_violation() -> None:
    """The worker parks first and releases in a second transaction, by design."""
    judged = [judge(_backlog_sample("2026-09-27T12:00:00Z")), judge(_backlog_sample("2026-09-27T12:00:05Z"))]
    out = persist(judged)
    assert out["backlog_leases"] == []


def test_a_waiting_task_that_keeps_its_lease_past_the_grace_is_a_violation() -> None:
    judged = [judge(_backlog_sample("2026-09-27T12:00:00Z")), judge(_backlog_sample("2026-09-27T12:00:40Z"))]
    out = persist(judged)
    assert [b["lease"] for b in out["backlog_leases"]] == ["l4"]
    assert out["backlog_leases"][0]["seconds"] == 40


def test_a_holder_without_a_lease_past_the_grace_is_a_violation() -> None:
    def s(at: str) -> dict:
        sample = three_held(at)
        sample["tasks"].append(task("t9", "RUNNING", "l9"))
        return sample

    assert persist([judge(s("2026-09-27T12:00:00Z")), judge(s("2026-09-27T12:00:10Z"))])["unleased_holders"] == []
    late = persist([judge(s("2026-09-27T12:00:00Z")), judge(s("2026-09-27T12:01:00Z"))])
    assert [h["task"] for h in late["unleased_holders"]] == ["t9"]


# --- the suite asserts from the judge, not from the global limit ------------


def test_the_suite_asserts_what_its_case_titles_claim() -> None:
    """Each claim is asserted from the judge's output, not the global limit again."""
    text = SUITE.read_text()
    assert "concurrency-judge.jq" in text
    assert "readTime" in text, "the sample must be one consistent read, not four separate ones"
    for key in ("pool_mismatches", "unleased_holders", "backlog_leases", "pre_running_leases"):
        assert key in text, f"concurrency-test.sh never reads the judge's {key}"
    # The old Backlog case compared the peak holding count with the global limit,
    # which is the concurrency check over again.
    assert "peak tasks holding capacity vs global limit" not in text
