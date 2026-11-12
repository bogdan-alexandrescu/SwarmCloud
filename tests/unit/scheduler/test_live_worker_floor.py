"""Every live-browser pod's worker gets at least LIVE_WORKER_MEMORY_FLOOR_GIB,
sized at admission (#3104).

A claude-code-browser step that runs a Next dev server, a stub API, the
Claude CLI and Playwright was OOMKilled at the worker container's 5 GiB
live-split limit (3 GiB at the code default), and the reconciler fails an
OOM with no retry. The owner chose to give the live worker more memory
(2026-11-12). The frozen table is not edited: `sizes.for_task` raises the
pinned size's memory and the worker's share up to the floor, keeping the
display share and CPU, BEFORE the lease, so these hold:

  * the worker's memory is max(floor, its share) and the size's memory is
    worker + display, for the browser class at the code defaults and at a
    stored version, and for `standard` (a step that shrank);
  * the units charged (`Sized.units`) are `units_for` the raised size:
    13 GiB is 2 units;
  * the attempt's record (`attempt_fields`) is the raised size;
  * the raised size passes `check_size`'s bounds, and one it would refuse
    is kept unraised, logged at ERROR;
  * a pod that is not live, a Cloud Run profile, and `medium`/`large`
    (already above the floor) are sized exactly as before.

Each assertion carries a MUTATION comment naming the change that turns it red.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

from control_plane.test_dispatch_manifests import make_task  # noqa: E402

from swarm_common.models import Task  # noqa: E402
from swarm_common.profiles import (  # noqa: E402
    CLASS_ORDER,
    RESOURCE_CLASSES,
    Backend,
    LiveShare,
    ResourceSize,
    check_size,
    default_sizes,
    units_for,
)

from scheduler import sizes  # noqa: E402
from scheduler.dispatch import (  # noqa: E402
    LIVE_WORKER_MEMORY_FLOOR_GIB,
    live_split_for,
    with_worker_floor,
)

FLOOR = LIVE_WORKER_MEMORY_FLOOR_GIB
GKE = Backend.GKE_AUTOPILOT

#: The deployment's stored browser class when #3104 was filed: 4 cores /
#: 10 GiB, worker 5 GiB / display 5 GiB.
BROWSER_10 = ResourceSize(
    cpu_request=4, cpu_limit=8, memory_gib=10, disk_gib=4,
    live_split=(LiveShare(cpu_request=2, cpu_limit=4, memory_gib=5),
                LiveShare(cpu_request=2, cpu_limit=3, memory_gib=5)),
)
STORED = sizes.SizeVersion(version=7, classes={**default_sizes(), "browser": BROWSER_10})


def _task(resource_class: str) -> Task:
    return dataclasses.replace(make_task("claude-code-browser"), resource_class=resource_class)


def _own_refusals(size: ResourceSize) -> list:
    """`check_size`'s judgement of one size on its own: the same size in
    every class's slot, so the class ORDER (which compares classes, not one
    pod) cannot refuse it and every other bound still applies."""
    return check_size({name: size for name in CLASS_ORDER})


def test_the_floor_is_8_gib():
    # The issue's heartbeats peaked at 5.37 GB; 8 is about 1.5x that.
    assert FLOOR == 8


@pytest.mark.parametrize("version,resource_class,share", [
    (sizes.DEFAULTS, "browser", 3),
    (STORED, "browser", 5),
    (sizes.DEFAULTS, "standard", 4),
], ids=["browser-defaults", "browser-stored", "standard-shrank"])
def test_a_live_size_is_raised_to_the_floor(version, resource_class, share):
    before = sizes.for_task(version, _task(resource_class), backend=GKE, live=False).size
    unraised = live_split_for(RESOURCE_CLASSES[resource_class], before)
    assert unraised[0].memory_gib == share, "the worker's share before the floor"

    sized = sizes.for_task(version, _task(resource_class), backend=GKE, live=True)
    worker, display = sized.live_split
    # MUTATION: drop with_worker_floor from sizes.for_task / sizes._default
    # and the worker keeps its 3, 5 or 4 GiB.
    assert worker.memory_gib == max(FLOOR, share)
    # MUTATION: raise the worker's share without the size's memory and the
    # pod is larger than the units admission charged.
    assert sized.size.memory_gib == worker.memory_gib + display.memory_gib
    # CPU, the display and disk do not move.
    assert display == unraised[1]
    assert (worker.cpu_request, worker.cpu_limit) == (unraised[0].cpu_request, unraised[0].cpu_limit)
    assert (sized.size.cpu_request, sized.size.cpu_limit, sized.size.disk_gib) == (
        before.cpu_request, before.cpu_limit, before.disk_gib)
    assert sized.size.live_split == sized.live_split
    assert sized.version == version.version
    # The raised size breaks no bound.
    assert _own_refusals(sized.size) == []


def test_the_raised_browser_pod_charges_two_units():
    sized = sizes.for_task(sizes.DEFAULTS, _task("browser"), backend=GKE, live=True)
    assert sized.size.memory_gib == 13
    # MUTATION: charge the class's units (1) and the lease pays for half the pod.
    assert sized.units == units_for(sized.size.cpu_request, sized.size.memory_gib) == 2


def test_the_attempt_records_the_raised_size():
    sized = sizes.for_task(STORED, _task("browser"), backend=GKE, live=True)
    fields = sized.attempt_fields()
    # MUTATION: record the version's size while the pod renders the raised
    # one and the record stops describing the pod.
    assert fields["sized_memory_gib"] == 13
    assert fields["sized_live_split"] == {
        "worker": {"cpu_request": 2, "cpu_limit": 4, "memory_gib": FLOOR},
        "display": {"cpu_request": 2, "cpu_limit": 3, "memory_gib": 5},
    }
    assert fields["size_version"] == 7


@pytest.mark.parametrize("resource_class", ["browser", "standard", "medium", "large"])
@pytest.mark.parametrize("version", [sizes.DEFAULTS, STORED], ids=["defaults", "stored"])
def test_a_pod_that_is_not_live_is_unchanged(version, resource_class):
    sized = sizes.for_task(version, _task(resource_class), backend=GKE, live=False)
    expected = (version.classes or default_sizes())[resource_class]
    # MUTATION: apply the floor whether or not the pod is live and every
    # standard and browser pod costs a unit more.
    assert sized.size == expected
    assert sized.live_split is None


@pytest.mark.parametrize("resource_class", ["browser", "standard"])
def test_a_cloud_run_profile_is_unchanged(resource_class):
    sized = sizes.for_task(STORED, _task(resource_class), backend=Backend.CLOUD_RUN_JOB, live=True)
    # Cloud Run fixes resources on the Job and has no sidecar.
    assert sized == sizes.Sized(resource_class=resource_class, version=0,
                                size=default_sizes()[resource_class], live_split=None)


@pytest.mark.parametrize("resource_class", ["medium", "large"])
def test_a_class_already_above_the_floor_is_unchanged(resource_class):
    size = default_sizes()[resource_class]
    split = live_split_for(RESOURCE_CLASSES[resource_class], size)
    assert split[0].memory_gib >= FLOOR
    sized = sizes.for_task(sizes.DEFAULTS, _task(resource_class), backend=GKE, live=True)
    assert sized == sizes.Sized(resource_class=resource_class, version=0, size=size, live_split=split)


def test_with_worker_floor_returns_its_inputs_when_the_worker_meets_it():
    size = default_sizes()["medium"]
    split = live_split_for(RESOURCE_CLASSES["medium"], size)
    raised_size, raised_split = with_worker_floor(size, split)
    assert raised_size is size and raised_split is split


def test_with_worker_floor_moves_memory_only():
    size, split = BROWSER_10, BROWSER_10.live_split
    raised_size, (worker, display) = with_worker_floor(size, split)
    assert worker == dataclasses.replace(split[0], memory_gib=FLOOR)
    assert display is split[1]
    assert raised_size == dataclasses.replace(size, memory_gib=13, live_split=(worker, display))


def test_a_raise_check_size_refuses_keeps_the_unraised_size(caplog):
    """A stored 2-core browser at 8 GiB raised to 14 GiB is 7 GiB a core, past
    MEMORY_PER_CPU_MAX: Autopilot would bill a size nobody chose. Kept as
    stored, logged at ERROR, as a split that leaves no room already is."""
    small = ResourceSize(
        cpu_request=2, cpu_limit=4, memory_gib=8, disk_gib=4,
        live_split=(LiveShare(cpu_request=1, cpu_limit=2, memory_gib=2),
                    LiveShare(cpu_request=1, cpu_limit=2, memory_gib=6)),
    )
    version = sizes.SizeVersion(version=9, classes={**default_sizes(), "browser": small})
    with caplog.at_level(logging.ERROR, logger=sizes.__name__):
        sized = sizes.for_task(version, _task("browser"), backend=GKE, live=True)
    # MUTATION: apply the raise without check_size and the pod is 14 GiB on 2 cores.
    assert sized == sizes.Sized(resource_class="browser", version=9, size=small,
                                live_split=small.live_split)
    assert any(r.levelno == logging.ERROR and "floor" in r.getMessage() for r in caplog.records)
