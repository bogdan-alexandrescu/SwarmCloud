"""The worker reads the size the scheduler RENDERED before the catalogue's (SIZES-WORKER-1, part of #1689).

docs/design/runner-sizes.md §4.4: the scheduler hands the worker
`SWARM_SIZED_CPU_LIMIT` and `SWARM_SIZED_MEMORY_GIB`, platform-derived from
its own reading and never from a task document. Once CPU bursts (limit 8 over
a request of 4) and sizes become an admin setting, the frozen catalogue no
longer says what this container was given: `RESOURCE_CLASSES["standard"]`
reads 4 cores and 8 GiB whatever the scheduler rendered.

So, below the kernel's own word (cgroup `cpu.max`, which still wins: it is
the limit enforced, and the phase-1 canary reads `cpu_limit_source: cgroup`),
the CPU limit comes from the sized environment before the catalogue, still
recorded as `resource_class` -- the frozen `Attempt.cpu_limit_source`
vocabulary has no third word, and "the size the platform gave the class" is
what that word means. The memory limit behind the OOM near-miss comes from
the sized environment the same way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker import metrics
from agent_worker.config import WorkerConfig
from swarm_common.config import Settings
from swarm_common.profiles import RESOURCE_CLASSES

from worker_seeds import seed_attempt

GIB = 1024 * 1024 * 1024

#: The worker's required environment for `WorkerConfig.from_env`, nothing sized.
_BASE_ENV = {
    "TASK_ID": "task_1", "ATTEMPT_ID": "att_1", "LEASE_ID": "lease_1",
    "TENANT_ID": "eng", "GENERATION": "1", "RUNNER_PROFILE": "mock",
    "PROJECT_ID": "swarm-test", "DISABLE_CLOUD_MONITORING": "1",
}


@pytest.fixture
def no_cgroup(monkeypatch, tmp_path: Path) -> None:
    """No `cpu.max`, no `memory.max`: the kernel says nothing, as off a cgroup v2 host."""
    monkeypatch.setattr(metrics, "CGROUP_ROOT", tmp_path / "no-cgroup-here")


def _config_from_env(monkeypatch, tmp_path: Path, **sized: str) -> WorkerConfig:
    for name in ("SWARM_SIZED_CPU_LIMIT", "SWARM_SIZED_MEMORY_GIB"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (_BASE_ENV | {"WORKSPACE_ROOT": str(tmp_path / "ws")} | sized).items():
        monkeypatch.setenv(name, value)
    return WorkerConfig.from_env(Settings.from_env())


# -- the catalogue the assertions are told apart from -------------------------


def test_the_catalogue_differs_from_the_sized_figures_these_tests_use():
    """Without this, a sized 8 and a catalogue 8 would pass for the wrong reason."""
    assert RESOURCE_CLASSES["standard"].cpu == 4
    assert RESOURCE_CLASSES["standard"].memory_gib == 8


# -- the environment is read ---------------------------------------------------


def test_from_env_reads_the_sized_cpu_limit_and_memory(monkeypatch, tmp_path: Path):
    config = _config_from_env(
        monkeypatch, tmp_path, SWARM_SIZED_CPU_LIMIT="8", SWARM_SIZED_MEMORY_GIB="6"
    )
    # MUTATION: drop `sized_cpu_limit=` from `WorkerConfig.from_env` and this is None.
    assert config.sized_cpu_limit == 8.0
    # MUTATION: drop `sized_memory_gib=` from `WorkerConfig.from_env` and this is None.
    assert config.sized_memory_gib == 6.0


def test_from_env_without_the_sized_variables_leaves_them_unset(monkeypatch, tmp_path: Path):
    """A worker dispatched by a scheduler older than SIZES-SCHED-1 reads as today."""
    config = _config_from_env(monkeypatch, tmp_path)
    # MUTATION: default either field to a number in `from_env` and this fails.
    assert config.sized_cpu_limit is None
    assert config.sized_memory_gib is None


@pytest.mark.parametrize("raw", ["eight", "0", "-4", "nan", "inf"])
def test_a_malformed_sized_value_is_ignored_not_fatal(monkeypatch, tmp_path: Path, raw: str):
    """A telemetry figure never fails an attempt: the catalogue is used instead."""
    # MUTATION: parse with `float(raw)` unguarded and "eight" raises; accept any
    # float and "0", "nan" or "inf" becomes a limit no container had.
    config = _config_from_env(
        monkeypatch, tmp_path, SWARM_SIZED_CPU_LIMIT=raw, SWARM_SIZED_MEMORY_GIB=raw
    )
    assert config.sized_cpu_limit is None
    assert config.sized_memory_gib is None
    assert config.memory_limit_bytes_of("standard") == 8 * GIB


# -- memory: `memory_limit_bytes_of` -------------------------------------------


def test_memory_limit_is_the_sized_memory_not_the_catalogues(monkeypatch, tmp_path: Path):
    config = _config_from_env(monkeypatch, tmp_path, SWARM_SIZED_MEMORY_GIB="6")
    # MUTATION: return the catalogue's `memory_gib` first in
    # `memory_limit_bytes_of` and this reads 8 GiB.
    assert config.memory_limit_bytes_of("standard") == 6 * GIB


def test_memory_limit_without_a_sized_memory_is_the_catalogues(monkeypatch, tmp_path: Path):
    config = _config_from_env(monkeypatch, tmp_path)
    # MUTATION: drop the catalogue fallback in `memory_limit_bytes_of` and this raises.
    assert config.memory_limit_bytes_of("standard") == 8 * GIB
    assert config.memory_limit_bytes_of("large") == 32 * GIB


def test_the_near_miss_sampler_is_given_the_sized_memory(db, worker_factory, no_cgroup):
    """The OOM near-miss is judged against what the container was rendered with."""
    seed_attempt(db)
    worker, _, _ = worker_factory(sized_memory_gib=6.0)
    worker._task = {"resource_class": "standard"}
    # MUTATION: make `_memory_limit_bytes` read `RESOURCE_CLASSES` directly and
    # this reads 8 GiB.
    assert worker._memory_limit_bytes() == 6 * GIB


# -- CPU: `_cpu_limit` ---------------------------------------------------------


def test_cpu_limit_without_a_cgroup_is_the_sized_limit(db, worker_factory, no_cgroup):
    seed_attempt(db)
    worker, _, _ = worker_factory(sized_cpu_limit=8.0)
    worker._task = {"resource_class": "standard"}
    # MUTATION: skip `cfg.sized_cpu_limit` in `Worker._cpu_limit` and this is
    # (4.0, "resource_class"), the catalogue's request.
    assert worker._cpu_limit() == (8.0, "resource_class")


def test_the_sized_limit_is_known_before_the_task_is_read(db, worker_factory, no_cgroup):
    """The environment names the container's size; it needs no task document to say it."""
    seed_attempt(db)
    worker, _, _ = worker_factory(sized_cpu_limit=8.0)
    worker._task = None
    # MUTATION: test for the task before the sized limit in `_cpu_limit` and
    # this is (None, None).
    assert worker._cpu_limit() == (8.0, "resource_class")


def test_cpu_limit_without_a_cgroup_or_a_sized_limit_is_the_catalogues(
    db, worker_factory, no_cgroup
):
    seed_attempt(db)
    worker, _, _ = worker_factory()
    worker._task = {"resource_class": "standard"}
    # MUTATION: drop the catalogue fallback in `_cpu_limit` and this is (None, None).
    assert worker._cpu_limit() == (4.0, "resource_class")


def test_the_kernels_cpu_max_wins_over_the_sized_limit(
    db, worker_factory, monkeypatch, tmp_path: Path
):
    """`cpu.max` is the limit enforced: on a cluster that does not burst,
    Autopilot sets the limit to the request, and the record must say so."""
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.max").write_text("400000 100000\n")
    monkeypatch.setattr(metrics, "CGROUP_ROOT", root)
    seed_attempt(db)
    worker, _, _ = worker_factory(sized_cpu_limit=8.0)
    worker._task = {"resource_class": "standard"}
    # MUTATION: read `cfg.sized_cpu_limit` before `cgroup_cpu_limit_cores()` in
    # `_cpu_limit` and this is (8.0, "resource_class").
    assert worker._cpu_limit() == (4.0, "cgroup")


def test_the_attempt_records_the_sized_limit(db, worker_factory, no_cgroup):
    """End to end through `_record_cpu`, onto the attempt document Details reads."""
    from agent_worker.metrics import ResourceUsage

    seed_attempt(db)
    worker, _, _ = worker_factory(sized_cpu_limit=8.0)
    worker._task = {"resource_class": "standard"}
    worker._runner_usage.append(ResourceUsage(cpu_seconds=5.0, cpu_source="proc"))

    worker._record_cpu()

    doc = db.doc("attempts/att_1")
    # MUTATION: as `test_cpu_limit_without_a_cgroup_is_the_sized_limit`: 4.0.
    assert doc["cpu_limit_cores"] == 8.0
    assert doc["cpu_limit_source"] == "resource_class"
