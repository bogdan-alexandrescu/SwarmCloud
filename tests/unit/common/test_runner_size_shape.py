"""Contract request 110: `ResourceSize`, `LiveShare`, `default_sizes` and `effective_size` (SIZES-CONTRACT-2, part of #1689).

docs/design/runner-sizes.md §5 request RS-3, accepted by the owner on
2026-10-20 (§7: Q3 A, Cloud Run profiles stay at the code defaults). One
frozen shape, so the scheduler, swarm-api and the worker read a size version
the same way instead of each restating it. What is held here:

  * `ResourceSize` carries a CPU request and limit, one memory and one disk
    number (request AND limit, invariant 7), and an optional live split of
    two `LiveShare`s, worker then display; `units` is derived, never stored;
  * `default_sizes()` is `RESOURCE_CLASSES`, number for number, plus the
    browser class's live split -- the same split the scheduler renders today
    from `LIVE_DISPLAY_SHARE`, so the default table describes the pods that run;
  * `effective_size` gives a GKE profile the version's size and a Cloud Run
    profile the code default whatever the version says; a version missing a
    class falls back to that class's default; an unresolved AUTO is refused;
  * check-contract-parity.sh compares the UI's bundled unit weights to the
    code DEFAULTS, and says so.

MUTATION: return the version's size for `CLOUD_RUN_JOB` and
`test_a_cloud_run_profile_keeps_the_default` fails; store `units` as a field
and `test_units_is_derived_not_stored` fails; give the default browser split a
3-core display and `test_the_default_split_is_what_the_scheduler_renders`
fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from scheduler.dispatch import LIVE_DISPLAY_SHARE, cpu_burst_ratio
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    Backend,
    LiveShare,
    ResourceSize,
    default_sizes,
    effective_size,
    units_for,
)

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"
PARITY = REPO / "scripts" / "lib" / "check-contract-parity.sh"

#: Version 2 of the design's SIZES-SCHED-2 example: `standard` at 6 / 12 GiB.
RESIZED = ResourceSize(cpu_request=6, cpu_limit=12, memory_gib=12, disk_gib=6)


def test_resource_size_is_the_designs_shape():
    names = [f.name for f in dataclasses.fields(ResourceSize)]
    # MUTATION: add a `memory_limit_gib` and memory could differ from its request.
    assert names == ["cpu_request", "cpu_limit", "memory_gib", "disk_gib", "live_split"]
    assert [f.name for f in dataclasses.fields(LiveShare)] == ["cpu_request", "cpu_limit", "memory_gib"]
    assert ResourceSize(cpu_request=4, cpu_limit=8, memory_gib=8, disk_gib=4).live_split is None


def test_the_shapes_are_frozen():
    size = default_sizes()["standard"]
    # MUTATION: drop `frozen=True` and a reader could resize a pinned size in place.
    with pytest.raises(dataclasses.FrozenInstanceError):
        size.cpu_limit = 16  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        default_sizes()["browser"].live_split[0].cpu_request = 3  # type: ignore[index,misc]


def test_units_is_derived_not_stored():
    assert "units" not in {f.name for f in dataclasses.fields(ResourceSize)}
    assert RESIZED.units == units_for(6, 12) == 2
    assert dataclasses.replace(RESIZED, memory_gib=17).units == 3


@pytest.mark.parametrize("name", sorted(RESOURCE_CLASSES))
def test_default_sizes_are_the_catalogue(name):
    rc = RESOURCE_CLASSES[name]
    size = default_sizes()[name]
    # MUTATION: build a default from anything but RESOURCE_CLASSES and a
    # number here drifts from the class the code still sizes Cloud Run with.
    assert size.cpu_request == rc.cpu
    assert size.cpu_limit == rc.cpu_limit_cores
    assert size.memory_gib == rc.memory_gib
    assert size.disk_gib == rc.disk_gib
    assert size.units == rc.units


def test_default_sizes_name_every_class_and_nothing_else():
    assert set(default_sizes()) == set(RESOURCE_CLASSES)


def test_default_sizes_cannot_be_edited_by_a_reader():
    # MUTATION: return a plain dict and one caller's edit resizes every caller.
    with pytest.raises(TypeError):
        default_sizes()["standard"] = RESIZED  # type: ignore[index]


def test_only_the_browser_class_carries_a_default_split():
    # §4.4: `standard` and `large` have no stored split; the scheduler scales
    # LIVE_DISPLAY_SHARE for them as it does today.
    assert default_sizes()["standard"].live_split is None
    assert default_sizes()["large"].live_split is None
    assert default_sizes()["browser"].live_split == (
        LiveShare(cpu_request=2, cpu_limit=4, memory_gib=3),
        LiveShare(cpu_request=2, cpu_limit=3, memory_gib=5),
    )


def test_the_default_split_is_what_the_scheduler_renders():
    """The frozen default and the renderer's table describe the same pod.

    `dispatch.with_live_display` gives the display LIVE_DISPLAY_SHARE's
    request, memory and limit (capped at the class's ratio) and the worker the
    rest of the class, bursting at the class's ratio. If either side moves
    alone, the default table no longer describes the live pods that run."""
    rc = RESOURCE_CLASSES["browser"]
    share = LIVE_DISPLAY_SHARE["browser"]
    ratio = cpu_burst_ratio(rc)
    display_limit = share.cpu * ratio if share.cpu_limit is None else min(share.cpu_limit, share.cpu * ratio)
    worker, display = default_sizes()["browser"].live_split
    assert display == LiveShare(share.cpu, display_limit, share.memory_gib)
    assert worker == LiveShare(rc.cpu - share.cpu, (rc.cpu - share.cpu) * ratio, rc.memory_gib - share.memory_gib)


def test_a_gke_profile_follows_the_version():
    version = {**default_sizes(), "standard": RESIZED}
    # MUTATION: ignore the version and SIZES-SCHED-2's resize never reaches a pod.
    assert effective_size("standard", version, Backend.GKE_AUTOPILOT) == RESIZED
    assert effective_size("browser", version, Backend.GKE_AUTOPILOT) == default_sizes()["browser"]


def test_a_cloud_run_profile_keeps_the_default():
    version = {**default_sizes(), "standard": RESIZED}
    # Cloud Run fixes resources on the Job, so a Cloud Run profile never follows
    # (§4.7, Q3 A): the lease must charge what the Job runs at.
    assert effective_size("standard", version, Backend.CLOUD_RUN_JOB) == default_sizes()["standard"]


def test_no_version_is_the_code_defaults():
    for backend in (Backend.GKE_AUTOPILOT, Backend.CLOUD_RUN_JOB):
        for name in RESOURCE_CLASSES:
            assert effective_size(name, None, backend) == default_sizes()[name]


def test_a_version_missing_a_class_falls_back_to_its_default():
    version = {"standard": RESIZED}
    assert effective_size("standard", version, Backend.GKE_AUTOPILOT) == RESIZED
    # MUTATION: index the version directly and this raises KeyError.
    assert effective_size("large", version, Backend.GKE_AUTOPILOT) == default_sizes()["large"]


def test_an_unresolved_backend_is_refused():
    # AUTO is a profile's preference, not where it runs: whether a size follows
    # depends on the resolved backend (`resolve_backend`), so guessing is refused.
    with pytest.raises(ValueError, match="resolve_backend"):
        effective_size("standard", None, Backend.AUTO)


def test_an_unknown_class_is_refused():
    with pytest.raises(KeyError):
        effective_size("huge", None, Backend.GKE_AUTOPILOT)


def test_the_parity_check_compares_the_ui_units_to_the_code_defaults():
    text = PARITY.read_text(encoding="utf-8")
    # MUTATION: leave the section reading `RESOURCE_CLASSES[...].units` under
    # its old name and this fails: once an admin can resize a class the bundled
    # copy can only match the defaults, and the check must say that is all it holds.
    assert "default_sizes" in text
    assert "units_py = dict((name, size.units) for name, size in default_sizes().items())" in text
    assert "RESOURCE_UNITS (code defaults)" in text


def test_request_rs_3_is_filed_as_entry_110():
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 110 |")]
    assert len(rows) == 1, rows
    assert "SIZES-CONTRACT-2" in rows[0] and "RS-3" in rows[0]
    assert "APPLIED" in rows[0]
    assert re.search(r"^## 110\. .*\(SIZES-CONTRACT-2, runner sizes request RS-3, part of #1689\)$", text, re.M)
