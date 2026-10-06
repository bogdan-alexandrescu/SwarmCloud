"""The OOM near-miss is judged against the memory the container was SIZED with (#205).

The dispatcher sizes the container from the task's resource class
(`scheduler.dispatch.resource_class_for`): the task's own class when the
catalogue still has it, else the profile's. The worker took the memory limit
behind the near-miss flag -- and the `resource_class` label on the exported
attempt metric, which the sizing decisions read -- from the PROFILE's class
only. A workflow step that narrows `browser` (16 GiB) to `standard` (8 GiB)
was judged against 16 GiB its container never had, so a run at 7.9 GiB of
8 GiB raised no flag.

The CPU limit has used the sized class since #188 (`_cpu_limit`,
`test_cpu_sampler.py`); these tests hold memory to the same rule.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.errors import ExitCode
from agent_worker.metrics import ResourceUsage
from swarm_common.profiles import RESOURCE_CLASSES

from worker_seeds import seed_attempt

GIB = 1024 * 1024 * 1024


class _RecordingSampler:
    """Stands in for `ResourceSampler`, recording the limit it was given."""

    made: list[int] = []

    def __init__(self, *, pid_provider, disk_provider, memory_limit_bytes, **_: object) -> None:
        type(self).made.append(memory_limit_bytes)
        self.usage = ResourceUsage()

    def start(self) -> None:
        pass

    def stop(self) -> ResourceUsage:
        return self.usage


@pytest.fixture
def recorded(monkeypatch) -> list[int]:
    _RecordingSampler.made = []
    monkeypatch.setattr(lifecycle, "ResourceSampler", _RecordingSampler)
    return _RecordingSampler.made


def _started(worker_factory, *, profile: str, task_class: str | None):
    worker, config, exporter = worker_factory(runner_profile=profile)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": config.task_id} | (
        {"resource_class": task_class} if task_class is not None else {}
    )
    worker._start_sampler(SimpleNamespace(pid=None))
    return worker, config, exporter


def test_a_step_narrowed_from_browser_to_standard_is_judged_against_8_gib(
    worker_factory, recorded
):
    """The issue's own pairing: profile `browser`, step class `standard`."""
    assert RESOURCE_CLASSES["browser"].memory_gib == 16
    assert RESOURCE_CLASSES["standard"].memory_gib == 8
    _started(worker_factory, profile="browser", task_class="standard")
    assert recorded == [8 * GIB], recorded


def test_the_profile_class_is_used_when_the_task_names_none(worker_factory, recorded):
    _started(worker_factory, profile="browser", task_class=None)
    assert recorded == [16 * GIB], recorded


def test_a_class_the_catalogue_no_longer_has_falls_back_to_the_profile(
    worker_factory, recorded
):
    """As `resource_class_for` does: the dispatcher sized it with the profile's."""
    _started(worker_factory, profile="browser", task_class="a-class-no-longer-in-the-catalogue")
    assert recorded == [16 * GIB], recorded


def test_the_exported_metric_is_labelled_with_the_sized_class(worker_factory, recorded):
    worker, _, exporter = _started(worker_factory, profile="browser", task_class="standard")
    worker._stop_sampler()
    worker._export_metrics()
    assert exporter.exports, "nothing was exported"
    assert exporter.exports[0][1]["resource_class"] == "standard", exporter.exports[0][1]


def test_the_near_miss_line_names_the_sized_class_and_its_limit(
    worker_factory, recorded, log_stream
):
    worker, _, _ = _started(worker_factory, profile="browser", task_class="standard")
    worker._sampler.usage = ResourceUsage(peak_rss_bytes=int(7.9 * GIB), oom_near_miss=True)
    worker._stop_sampler()

    lines = [line for line in log_stream.getvalue().splitlines() if "OOM NEAR MISS" in line]
    assert lines, log_stream.getvalue()
    assert '"resource_class": "standard"' in lines[0], lines[0]
    assert f'"limit_bytes": {8 * GIB}' in lines[0], lines[0]


def test_a_whole_attempt_sized_larger_than_its_profile_uses_the_larger_limit(
    db, worker_factory, recorded
):
    """End to end through `run()`: the class is read from the task document
    the worker loads, not from anything a test set by hand."""
    seed_attempt(db, task_input={"prompt": "sized", "steps": 1, "sleep_seconds": 0.1})
    db.doc("tasks/task_1")["resource_class"] = "large"  # mock's own class is standard
    worker, _, exporter = worker_factory()
    assert worker.run() == ExitCode.OK

    assert recorded == [RESOURCE_CLASSES["large"].memory_gib * GIB], recorded
    assert exporter.exports[0][1]["resource_class"] == "large", exporter.exports[0][1]
