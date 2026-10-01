"""The no-progress rule judges Cloud Run attempts too, by a per-profile clock.

D5 (gap audit; BUILD_PROMPT_V2 2.9 asks for the no-progress signal on every
runner profile). The owner's 2026-10-01 decision keeps Cloud Run Jobs primary,
so `mock`, `generic`, `claude-code` and `codex` all run there -- and the rule
used to look only at GKE. A heartbeating but wedged Cloud Run agent ran to its
`timeout_seconds`.

S27 (owner decision 2026-10-01): the threshold is per runner profile, every
profile at 1800 s until the owner sets an override.

WHAT THIS FILE PINS, against the real `Reconciler` and `ControlStore` over the
control-plane fake, with the event shapes `agent_worker.control.ControlPlane.emit`
writes (built by the browser-eviction suite's `seed_run`):

  C-1  a RUNNING Cloud Run attempt quiet past the threshold, on measured
       evidence, is FENCED and nothing else in that pass; the next pass
       releases its lease once its worker has exited;
  C-2  any CPU or work-tree movement, and any gap in the evidence, leaves it
       alone;
  C-3  `enable_cloud_run_stall_guard` turns off the Cloud Run half only, and
       `enable_gke_eviction` the GKE half only;
  P-1  `STUCK_AFTER_SECONDS_<PROFILE>` overrides one profile and no other; an
       unknown <PROFILE> is logged and ignored; a bad value for a known one
       fails startup;
  P-2  detect (which attempts are old enough to read) and repair (the clock
       the evidence is judged by) resolve the SAME per-profile value, on both
       backends.

GKE behaviour itself is pinned, unchanged, by test_browser_eviction.py.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest

from swarm_common.config import Settings
from swarm_common.models import utcnow
from swarm_common.profiles import RUNNER_PROFILES

from reconciler import repair
from reconciler.config import ReconcilerConfig, stuck_after_env_name
from reconciler.logs import build_logger
from reconciler.model import ExecutionPhase, ExecutionView
from reconciler.repair import Reconciler
from reconciler.store import ControlStore

from .fakes import FakeFirestore
from .test_browser_eviction import (
    QUIET,
    reconciler_events,
    running_browser_task,
    seed_run,
)
from .test_reconciler_gke_namespaced import (
    CLOUD_RUN,
    ENG,
    ENG_NS,
    REPO,
    FlatBackend,
    RbacBatchApi,
    gke,
    k8s_job,
    log_lines,
    reconciler,
    seed_stranded,
    seed_tenant,
)

PROFILE = "claude-code"


# ---------------------------------------------------------------------------
# A running Cloud Run attempt
# ---------------------------------------------------------------------------


def running_cloud_run_task(db: FakeFirestore, task_id: str, *, profile: str = PROFILE
                           ) -> dict[str, str]:
    """A RUNNING Cloud Run task, 50 minutes in, whose worker heartbeated 10s ago."""
    seed_tenant(db, ENG, record_namespace=True)
    ids = seed_stranded(
        db, task_id, backend=CLOUD_RUN, state="RUNNING", minutes_ago=50,
        heartbeat_seconds_ago=10,
    )
    db.docs[f"tasks/{task_id}"]["runner_profile"] = profile
    return ids


def execution_of(ids: dict[str, str], *, phase: ExecutionPhase = ExecutionPhase.RUNNING
                 ) -> ExecutionView:
    """The execution as `CloudRunBackend.list_executions` attributes it."""
    return ExecutionView(
        name=ids["execution"],
        backend=CLOUD_RUN,
        phase=phase,
        created_at=utcnow() - timedelta(minutes=50),
        task_id=ids["task"],
        attempt_id=ids["attempt"],
        tenant_id=ENG,
        generation=1,
        parent=ids["execution"].rsplit("/executions/", 1)[0],
    )


class CloudRun(FlatBackend):
    """A Cloud Run backend listed in one call, recording every termination."""

    def __init__(self, *executions: ExecutionView) -> None:
        super().__init__(CLOUD_RUN, executions=list(executions))
        self.terminated: list[str] = []

    def set(self, *executions: ExecutionView) -> None:
        self._executions = list(executions)

    def terminate(self, execution: Any) -> bool:
        self.terminated.append(execution.name)
        return True


def fenced(db: FakeFirestore, ids: dict[str, str]) -> bool:
    return db.docs[f"tasks/{ids['task']}"]["current_generation"] == 2


def left_alone(db: FakeFirestore, ids: dict[str, str], backend: CloudRun, report: Any) -> None:
    assert "stuck_no_progress" not in [o.kind for o in report.outcomes], (
        [o.as_dict() for o in report.outcomes]
    )
    assert backend.terminated == []
    task = db.docs[f"tasks/{ids['task']}"]
    assert task["current_generation"] == 1, "a Cloud Run attempt not proven stuck was fenced"
    assert task["state"] == "RUNNING"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None


# ---------------------------------------------------------------------------
# C-1: fenced on measured quiet, then released
# ---------------------------------------------------------------------------


def test_a_cloud_run_attempt_stalled_past_the_threshold_is_fenced_then_released():
    db = FakeFirestore()
    ids = running_cloud_run_task(db, "task_crstall00000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    backend = CloudRun(execution_of(ids))
    rec, stream = reconciler(db, backend)

    first = rec.run_once()

    assert [o.kind for o in first.outcomes] == ["stuck_no_progress"], (
        [o.as_dict() for o in first.outcomes]
    )
    assert fenced(db, ids)
    # Fence ONLY: a cancel SIGTERMs a live worker, whose SIGTERM path parks the
    # task without checking the fence (`repair._repair_stuck`).
    assert backend.terminated == []
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is None
    event = reconciler_events(db, ids["task"], "generation_fenced")[-1]
    assert event["detail"]["finding"] == "stuck_no_progress"
    assert "(threshold 1800s)" in event["detail"]["reason"]
    messages = [line["message"] for line in log_lines(stream)]
    assert repair.FENCED_STALLED_CLOUD_RUN in messages
    assert repair.EVICTED not in messages, "a Cloud Run fence was logged as a GKE eviction"

    # The worker sees the fence, stops the agent and exits 70: the execution
    # fails on its own and the heartbeats stop.
    backend.set(execution_of(ids, phase=ExecutionPhase.FAILED))
    lease = db.docs[f"leases/{ids['lease']}"]
    lease["heartbeat_at"] = utcnow() - timedelta(seconds=300)
    lease["expires_at"] = lease["heartbeat_at"] + timedelta(seconds=120)

    second = rec.run_once()

    task = db.docs[f"tasks/{ids['task']}"]
    assert task["state"] == "READY", [o.as_dict() for o in second.outcomes]
    assert task["current_generation"] == 2, "fenced once, not twice"
    assert db.docs[f"leases/{ids['lease']}"]["released_at"] is not None
    assert backend.terminated == [], "the execution ended itself; nothing to cancel"


# ---------------------------------------------------------------------------
# C-2: movement, or a gap in the evidence, is not a stall
# ---------------------------------------------------------------------------


def test_a_cloud_run_attempt_spending_cpu_is_left_alone():
    """0.4 cores: an agent thinking, compiling, running tests."""
    db = FakeFirestore()
    ids = running_cloud_run_task(db, "task_crbusy00000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.4)
    backend = CloudRun(execution_of(ids))
    CountingStore.reads = []
    rec, _ = reconciler(db, backend, store_class=CountingStore)

    left_alone(db, ids, backend, rec.run_once())
    assert ids["task"] in CountingStore.reads, "never judged: this would pass on any code"


def test_a_cloud_run_attempt_whose_work_tree_changed_is_left_alone():
    db = FakeFirestore()
    ids = running_cloud_run_task(db, "task_credit00000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002,
             work_tree_changes_at=now - timedelta(minutes=10))
    backend = CloudRun(execution_of(ids))
    CountingStore.reads = []
    rec, _ = reconciler(db, backend, store_class=CountingStore)

    left_alone(db, ids, backend, rec.run_once())
    assert ids["task"] in CountingStore.reads, "never judged: this would pass on any code"


@pytest.mark.parametrize("gap", ["events_stopped", "cpu_unmeasured"])
def test_a_cloud_run_attempt_with_gapped_evidence_is_not_judged(gap: str):
    db = FakeFirestore()
    ids = running_cloud_run_task(db, f"task_crgap{gap[:4]}0000000001")
    now = utcnow()
    if gap == "events_stopped":
        seed_run(db, ids, began=now - QUIET, until=now - timedelta(minutes=20), cpu_cores=0.002)
    else:
        seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.0, cpu_measured=False)
    backend = CloudRun(execution_of(ids))
    rec, stream = reconciler(db, backend)

    left_alone(db, ids, backend, rec.run_once())
    assert any(
        line["message"].startswith("not judging an attempt's progress")
        for line in log_lines(stream)
    ), "the attempt was not even read: the gap was never the reason"


# ---------------------------------------------------------------------------
# C-3: two switches, one half each
# ---------------------------------------------------------------------------


def _both_stalled(db: FakeFirestore) -> tuple[dict[str, str], dict[str, str], CloudRun, RbacBatchApi]:
    now = utcnow()
    on_gke = running_browser_task(db, "task_gkestall000000001")
    seed_run(db, on_gke, began=now - QUIET, until=now, cpu_cores=0.002)
    on_run = running_cloud_run_task(db, "task_crstall00000000002")
    seed_run(db, on_run, began=now - QUIET, until=now, cpu_cores=0.002)
    batch = RbacBatchApi(jobs=[k8s_job(task_id=on_gke["task"])], listable={ENG_NS})
    return on_gke, on_run, CloudRun(execution_of(on_run)), batch


def test_the_cloud_run_switch_turns_off_only_the_cloud_run_half():
    db = FakeFirestore()
    on_gke, on_run, backend, batch = _both_stalled(db)
    rec, _ = reconciler(db, gke(batch), backend, enable_cloud_run_stall_guard=False)

    rec.run_once()

    assert fenced(db, on_gke), "the GKE half went off with the Cloud Run switch"
    assert not fenced(db, on_run)


def test_the_gke_switch_turns_off_only_the_gke_half():
    db = FakeFirestore()
    on_gke, on_run, backend, batch = _both_stalled(db)
    rec, _ = reconciler(db, gke(batch), backend, enable_gke_eviction=False)

    rec.run_once()

    assert fenced(db, on_run), "the Cloud Run half went off with the GKE switch"
    assert not fenced(db, on_gke)


def test_the_cloud_run_switch_is_on_by_default_and_read_from_its_own_variable(monkeypatch):
    assert ReconcilerConfig.enable_cloud_run_stall_guard is True
    _clean_env(monkeypatch)
    monkeypatch.setenv("RECONCILER_ENABLE_CLOUD_RUN_STALL_GUARD", "false")
    loaded = ReconcilerConfig.from_env(Settings.from_env())
    assert loaded.enable_cloud_run_stall_guard is False
    assert loaded.enable_gke_eviction is True


# ---------------------------------------------------------------------------
# P-1: per-profile thresholds from the environment
# ---------------------------------------------------------------------------


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("STUCK_AFTER_SECONDS"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PROJECT_ID", "test-project")


def _load(monkeypatch: pytest.MonkeyPatch, **env: str) -> ReconcilerConfig:
    _clean_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return ReconcilerConfig.from_env(Settings.from_env())


def test_an_override_changes_its_profile_and_no_other(monkeypatch):
    loaded = _load(monkeypatch, STUCK_AFTER_SECONDS_CLAUDE_CODE="2400")

    assert stuck_after_env_name("claude-code") == "STUCK_AFTER_SECONDS_CLAUDE_CODE"
    assert loaded.stuck_after_seconds_by_profile == {"claude-code": 2400}
    assert loaded.stuck_after_for("claude-code") == 2400
    for other in RUNNER_PROFILES:
        if other != "claude-code":
            assert loaded.stuck_after_for(other) == 1800, other


def test_with_no_overrides_every_profile_resolves_to_the_global(monkeypatch):
    assert RUNNER_PROFILES, "no profiles: this test would pass vacuously"
    shipped = _load(monkeypatch)
    assert shipped.stuck_after_seconds_by_profile == {}
    assert {shipped.stuck_after_for(p) for p in RUNNER_PROFILES} == {1800}
    assert shipped.stuck_after_for(None) == 1800

    moved = _load(monkeypatch, STUCK_AFTER_SECONDS="900")
    assert {moved.stuck_after_for(p) for p in RUNNER_PROFILES} == {900}


def test_an_override_naming_no_profile_is_logged_once_and_ignored(monkeypatch):
    loaded = _load(monkeypatch, STUCK_AFTER_SECONDS_CLAUDECODE="60")

    assert loaded.stuck_after_seconds_by_profile == {}
    assert loaded.ignored_stuck_overrides == ("STUCK_AFTER_SECONDS_CLAUDECODE",)
    assert {loaded.stuck_after_for(p) for p in RUNNER_PROFILES} == {1800}

    stream = io.StringIO()
    logger = build_logger(stream=stream)
    store = ControlStore(FakeFirestore(), logger=logger)
    Reconciler(store=store, backends=[], config=loaded, logger=logger, hold_releaser=None)
    lines = [
        line for line in log_lines(stream)
        if line["message"].startswith("ignoring a stall threshold override")
    ]
    assert [line["variable"] for line in lines] == ["STUCK_AFTER_SECONDS_CLAUDECODE"]
    assert "60" not in [str(v) for line in lines for v in line.values()]


@pytest.mark.parametrize("value", ["soon", "0", "-30", "1.5"])
def test_a_bad_value_for_a_known_profile_fails_startup(monkeypatch, value: str):
    with pytest.raises(ValueError, match="STUCK_AFTER_SECONDS_CODEX"):
        _load(monkeypatch, STUCK_AFTER_SECONDS_CODEX=value)


def test_no_deployment_sets_a_per_profile_override():
    """Every profile stays at 1800 until the owner sets one (S27)."""
    found = [
        str(path.relative_to(REPO))
        for root in ("terraform", "kubernetes")
        for path in (REPO / root).rglob("*")
        if path.is_file()
        and path.suffix in {".tf", ".tfvars", ".yaml", ".yml", ".json"}
        and "STUCK_AFTER_SECONDS_" in path.read_text(errors="ignore")
    ]
    assert found == []


# ---------------------------------------------------------------------------
# P-2: detect and repair resolve the same per-profile value, on both backends
# ---------------------------------------------------------------------------


class CountingStore(ControlStore):
    """The production store, recording whose progress evidence was read."""

    reads: list[str] = []

    def attempt_events(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
        type(self).reads.append(task_id)
        return super().attempt_events(task_id, attempt_id)


def test_each_backend_is_judged_by_its_own_profiles_threshold():
    """claude-code on Cloud Run at 2400 s, browser on GKE at 2700 s: both quiet
    for 49 minutes, so both fenced -- and each fence names its OWN threshold,
    which is the value `assess` was handed by repair."""
    db = FakeFirestore()
    on_gke, on_run, backend, batch = _both_stalled(db)
    rec, _ = reconciler(
        db, gke(batch), backend,
        stuck_after_seconds_by_profile={"claude-code": 2400, "browser": 2700},
    )

    rec.run_once()

    assert fenced(db, on_run) and fenced(db, on_gke)
    run_reason = reconciler_events(db, on_run["task"], "generation_fenced")[-1]["detail"]["reason"]
    gke_reason = reconciler_events(db, on_gke["task"], "generation_fenced")[-1]["detail"]["reason"]
    assert "(threshold 2400s)" in run_reason
    assert "(threshold 2700s)" in gke_reason


@pytest.mark.parametrize("raised", ["claude-code", "browser"])
def test_a_raised_threshold_keeps_its_profile_unread_and_unfenced(raised: str):
    """Both attempts began 50 minutes (3000 s) ago. A threshold of 3200 s for
    one profile means detect must not even READ that attempt's evidence --
    and repair must not judge it -- while the other profile, at the global
    1800, is read and fenced in the same pass."""
    db = FakeFirestore()
    on_gke, on_run, backend, batch = _both_stalled(db)
    CountingStore.reads = []
    rec, _ = reconciler(
        db, gke(batch), backend, store_class=CountingStore,
        stuck_after_seconds_by_profile={raised: 3200},
    )

    rec.run_once()

    held, other = (on_run, on_gke) if raised == "claude-code" else (on_gke, on_run)
    assert not fenced(db, held)
    assert held["task"] not in CountingStore.reads, "detect read an attempt its threshold excludes"
    assert fenced(db, other), "an override for one profile moved another"
    assert other["task"] in CountingStore.reads


def test_the_progress_verdict_records_the_profile_threshold_applied():
    """Read the evidence the way the pass does, for a Cloud Run attempt whose
    profile has an override, and check what the verdict carries."""
    db = FakeFirestore()
    ids = running_cloud_run_task(db, "task_crverdict000000001")
    now = utcnow()
    seed_run(db, ids, began=now - QUIET, until=now, cpu_cores=0.002)
    rec, _ = reconciler(db, CloudRun(execution_of(ids)),
                        stuck_after_seconds_by_profile={"claude-code": 2100})
    snapshot = rec._store.snapshot()
    report = repair.ReconcileReport(started_at=now, dry_run=False)

    rec._read_progress(snapshot, [execution_of(ids)], report)

    verdict = snapshot.progress[ids["attempt"]]
    assert verdict.stuck
    assert verdict.as_detail()["stuck_after_seconds"] == 2100
