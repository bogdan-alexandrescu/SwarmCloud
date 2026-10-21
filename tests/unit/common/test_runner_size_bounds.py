"""Contract request 109: the frozen size bounds, `units_for` and `check_size` (SIZES-CONTRACT-2, part of #1689).

docs/design/runner-sizes.md §5 request RS-2, accepted by the owner on
2026-10-20 (§7). Admins will set runner sizes (lane SIZES-API); the bounds
they may set them within are frozen code, so neither an admin nor a caller
can size a pod outside them (invariants 7 and 10). What is held here, from
§4.2's table:

  * `cpu_request` 0.5 ... 8 in steps of 0.25; `cpu_limit` between the request
    and `CPU_BURST_MAX_RATIO` x the request;
  * `memory_gib` 2 ... 32, an integer; memory per CPU 1 ... 6.5 (Autopilot's
    ratio, outside which it raises a resource and bills it);
  * `disk_gib` 1 ... `memory_gib` - 2, an integer (a tmpfs slice of memory);
  * `standard` <= `browser` <= `large` in every dimension of the request;
  * a live split's worker >= 1 core and 2 GiB, display >= 1 core and 5 GiB,
    their requests and memories summing to the class's;
  * `check_size` takes the whole table, is pure, and returns EVERY refusal;
  * `units_for` reproduces today's 1 / 1 / 4, and every class's `units` is it;
  * a property sweep: whatever `check_size` accepts has a pod request ratio
    inside 1:1 ... 1:6.5.

Each bound is tried at its edge, just inside it and just outside it, and each
assertion looks only at the refusals for the field under test, so a table that
also breaks something else cannot make a bound look enforced.

MUTATION: drop the step check from `check_size` and
`test_cpu_request_bounds[7.1-refused]` fails; compare `cpu_limit` to 3 x the
request and `test_cpu_limit_bounds[8.25-refused]` fails; return after the
first refusal and `test_every_refusal_is_returned` fails; drop the ordering
check and `test_a_smaller_class_larger_than_the_next_is_refused` fails.
"""

from __future__ import annotations

import dataclasses
import random
import re
from pathlib import Path

import pytest

from swarm_common import profiles
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    LiveShare,
    ResourceSize,
    SizeRefusal,
    check_size,
    default_sizes,
    units_for,
)

REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"


def _table(**classes: ResourceSize) -> dict[str, ResourceSize]:
    """The default table with `classes` replaced."""
    table = dict(default_sizes())
    table.update(classes)
    return table


def _size(cpu_request, cpu_limit, memory_gib, disk_gib, live_split=None) -> ResourceSize:
    return ResourceSize(
        cpu_request=cpu_request,
        cpu_limit=cpu_limit,
        memory_gib=memory_gib,
        disk_gib=disk_gib,
        live_split=live_split,
    )


def _split(worker, display):
    return (LiveShare(*worker), LiveShare(*display))


def _refused(refusals: list[SizeRefusal], resource_class: str, field: str) -> list[SizeRefusal]:
    return [r for r in refusals if r.resource_class == resource_class and r.field == field]


def test_the_frozen_bounds_are_the_designs():
    # MUTATION: change any constant and this fails: §4.2's table is the owner's.
    assert profiles.CPU_BURST_MAX_RATIO == 2
    assert (profiles.CPU_REQUEST_MIN, profiles.CPU_REQUEST_MAX, profiles.CPU_REQUEST_STEP) == (0.5, 8, 0.25)
    assert (profiles.MEMORY_GIB_MIN, profiles.MEMORY_GIB_MAX) == (2, 32)
    assert (profiles.MEMORY_PER_CPU_MIN, profiles.MEMORY_PER_CPU_MAX) == (1.0, 6.5)
    assert profiles.LIVE_WORKER_MIN == (1, 2)
    assert profiles.LIVE_DISPLAY_MIN == (1, 5)
    assert profiles.CLASS_ORDER == ("standard", "browser", "large")
    assert set(profiles.CLASS_ORDER) == set(RESOURCE_CLASSES)


@pytest.mark.parametrize(
    "cpu_request,memory_gib,expected",
    [(4, 8, 1), (4, 9, 2), (4.25, 8, 2), (8, 32, 4), (0.5, 2, 1), (8, 16, 2), (6, 12, 2)],
)
def test_units_for(cpu_request, memory_gib, expected):
    # MUTATION: `min` for `max`, or `round` for `ceil`, and a row fails.
    assert units_for(cpu_request, memory_gib) == expected


@pytest.mark.parametrize("name,expected", [("standard", 1), ("browser", 1), ("large", 4)])
def test_units_for_reproduces_todays_classes(name, expected):
    rc = RESOURCE_CLASSES[name]
    # MUTATION: change a class's `units` and both halves fail; the import-time
    # check in profiles.py refuses the module before this runs.
    assert rc.units == expected
    assert units_for(rc.cpu, rc.memory_gib) == expected


def test_the_defaults_pass_every_bound():
    # MUTATION: tighten a bound past today's sizes (memory max 16, say) and
    # `large` is refused here.
    assert check_size(default_sizes()) == []


def test_check_size_is_pure():
    table = _table(standard=_size(4, 9, 8, 4))
    before = dict(table)
    first = check_size(table)
    # MUTATION: cache or mutate anything in check_size and a second call or the
    # table differs.
    assert check_size(table) == first
    assert table == before


@pytest.mark.parametrize(
    "cls,size,refused",
    [
        # the minimum, 0.5 (Autopilot extended run time's floor)
        ("standard", (0.5, 1, 3, 1), False),
        ("standard", (0.75, 1.5, 3, 1), False),
        ("standard", (0.25, 0.5, 2, 1), True),
        ("standard", (0, 0, 3, 1), True),
        # the maximum, 8 (Cloud Run's ceiling binds every class's request)
        ("large", (8, 16, 32, 16), False),
        ("large", (7.75, 15.5, 32, 16), False),
        ("large", (8.25, 16, 32, 16), True),
        # the step, 0.25 (on `large`, so the ordering cannot refuse it too)
        ("large", (7.25, 14.5, 32, 16), False),
        ("large", (7.1, 14.2, 32, 16), True),
    ],
    ids=["0.5", "0.75", "0.25-refused", "0-refused", "8", "7.75", "8.25-refused", "7.25", "7.1-refused"],
)
def test_cpu_request_bounds(cls, size, refused):
    found = _refused(check_size(_table(**{cls: _size(*size)})), cls, "cpu_request")
    assert bool(found) is refused, found


@pytest.mark.parametrize(
    "limit,refused",
    [(8, False), (7.75, False), (8.25, True), (4, False), (4.25, False), (3.75, True)],
    ids=["8", "7.75", "8.25-refused", "4", "4.25", "3.75-refused"],
)
def test_cpu_limit_bounds(limit, refused):
    found = _refused(check_size(_table(standard=_size(4, limit, 8, 4))), "standard", "cpu_limit")
    # MUTATION: drop the lower bound and 3.75 constructs; widen the ratio and
    # 8.25 does.
    assert bool(found) is refused, found


@pytest.mark.parametrize(
    "cls,size,refused",
    [
        ("standard", (0.5, 1, 2, 1), False),
        ("standard", (0.5, 1, 3, 1), False),
        ("standard", (0.5, 1, 1, 1), True),
        ("large", (8, 16, 32, 16), False),
        ("large", (8, 16, 31, 16), False),
        ("large", (8, 16, 33, 16), True),
        ("standard", (4, 8, 8.5, 4), True),
        ("standard", (4, 8, True, 4), True),
    ],
    ids=["2", "3", "1-refused", "32", "31", "33-refused", "fraction-refused", "bool-refused"],
)
def test_memory_bounds(cls, size, refused):
    found = _refused(check_size(_table(**{cls: _size(*size)})), cls, "memory_gib")
    assert bool(found) is refused, found


@pytest.mark.parametrize(
    "cls,size,refused",
    [
        ("standard", (4, 8, 4, 2), False),  # 1:1, the floor
        ("standard", (4, 8, 5, 2), False),
        ("standard", (4, 8, 3, 1), True),  # 1:0.75
        ("large", (4, 8, 26, 16), False),  # 1:6.5, the ceiling
        ("large", (4, 8, 25, 16), False),
        ("large", (4, 8, 27, 16), True),  # 1:6.75
    ],
    ids=["1.0", "1.25", "0.75-refused", "6.5", "6.25", "6.75-refused"],
)
def test_memory_per_cpu_bounds(cls, size, refused):
    found = _refused(check_size(_table(**{cls: _size(*size)})), cls, "memory_per_cpu")
    # MUTATION: drop the ratio check and Autopilot would raise and bill the
    # smaller resource of a 4-core / 27 GiB class; 6.75 constructs.
    assert bool(found) is refused, found


@pytest.mark.parametrize(
    "cls,disk,refused",
    [
        # the ceiling, memory - 2, on `large` (32 GiB) so the ordering is met
        ("large", 30, False),
        ("large", 29, False),
        ("large", 31, True),
        # the floor, 1, on `standard` (8 GiB), the smallest class
        ("standard", 1, False),
        ("standard", 2, False),
        ("standard", 0, True),
        ("standard", 3.5, True),
    ],
    ids=["30", "29", "31-refused", "1", "2", "0-refused", "fraction-refused"],
)
def test_disk_bounds(cls, disk, refused):
    size = _size(8, 16, 32, disk) if cls == "large" else _size(4, 8, 8, disk)
    found = _refused(check_size(_table(**{cls: size})), cls, "disk_gib")
    # MUTATION: let the tmpfs take all of memory and 31 of 32 GiB constructs,
    # leaving the worker and the agent 1 GiB.
    assert bool(found) is refused, found


@pytest.mark.parametrize(
    "field,size",
    [
        ("cpu_request", (4.25, 8, 8, 4)),
        ("memory_gib", (4, 8, 9, 4)),
        ("disk_gib", (4, 8, 8, 5)),
    ],
)
def test_a_smaller_class_larger_than_the_next_is_refused(field, size):
    # `browser` is 4 / 8 GiB / 4 GiB; `standard` one step above it in `field`.
    refusals = check_size(_table(standard=_size(*size)))
    ordering = [r for r in _refused(refusals, "standard", field) if "browser" in r.bound]
    # MUTATION: drop the ordering check and a claude-code-browser step could
    # grow by naming the "smaller" class `standard`.
    assert ordering, refusals


def test_equal_neighbouring_classes_are_accepted():
    # standard == browser is today's catalogue; the order is <=, not <.
    assert default_sizes()["standard"].cpu_request == default_sizes()["browser"].cpu_request
    assert check_size(default_sizes()) == []


def test_a_larger_class_smaller_than_the_one_below_is_refused():
    browser = default_sizes()["browser"]
    large = _size(4, 8, 8, 3)  # disk 3 < browser's 4; cpu and memory equal it
    refusals = check_size(_table(large=large))
    assert browser.disk_gib == 4
    assert [r for r in _refused(refusals, "large", "disk_gib") if "browser" in r.bound], refusals


def test_the_default_live_split_is_accepted():
    assert default_sizes()["browser"].live_split == _split((2, 4, 3), (2, 3, 5))
    assert check_size(_table()) == []


@pytest.mark.parametrize(
    "split,field",
    [
        (((2.25, 4.5, 3), (2, 3, 5)), "live_split.cpu_request"),  # 4.25 != 4
        (((2, 4, 4), (2, 3, 5)), "live_split.memory_gib"),  # 9 != 8
        (((0.75, 1.5, 3), (3.25, 6.5, 5)), "live_split.worker.cpu_request"),
        (((3, 6, 1), (1, 2, 7)), "live_split.worker.memory_gib"),
        (((3.25, 6.5, 3), (0.75, 1.5, 5)), "live_split.display.cpu_request"),
        (((2, 4, 4), (2, 3, 4)), "live_split.display.memory_gib"),
        (((2, 4.25, 3), (2, 3, 5)), "live_split.worker.cpu_limit"),
        (((2, 4, 3), (2, 1.75, 5)), "live_split.display.cpu_limit"),
    ],
    ids=[
        "cpu-sum",
        "memory-sum",
        "worker-cpu-min",
        "worker-memory-min",
        "display-cpu-min",
        "display-memory-min",
        "worker-limit-ratio",
        "display-limit-below-request",
    ],
)
def test_a_live_split_out_of_bounds_is_refused(split, field):
    browser = _size(4, 8, 8, 4, _split(*split))
    refusals = check_size(_table(browser=browser))
    # MUTATION: drop any one live-split check and its row constructs.
    assert _refused(refusals, "browser", field), refusals


def test_live_split_minimums_at_their_edges_are_accepted():
    # worker 1 core / 2 GiB is LIVE_WORKER_MIN; the display gets the rest.
    browser = _size(4, 8, 8, 4, _split((1, 2, 2), (3, 6, 6)))
    assert check_size(_table(browser=browser)) == []
    # display 1 core / 5 GiB is LIVE_DISPLAY_MIN.
    browser = _size(4, 8, 8, 4, _split((3, 6, 3), (1, 2, 5)))
    assert check_size(_table(browser=browser)) == []


def test_every_refusal_is_returned():
    standard = _size(4.1, 9, 8.5, 0)
    refusals = check_size(_table(standard=standard))
    fields = {r.field for r in refusals if r.resource_class == "standard"}
    # MUTATION: return the first refusal and this sees one field.
    assert {"cpu_request", "cpu_limit", "memory_gib", "disk_gib"} <= fields, refusals


def test_a_refusal_names_its_value_and_bound():
    (refusal,) = check_size(_table(standard=_size(4, 12, 8, 4)))
    assert refusal == SizeRefusal(
        resource_class="standard", field="cpu_limit", value=12, bound=refusal.bound
    )
    # The bound is worded as the rule, with the number it works out to.
    assert "2" in refusal.bound and "8" in refusal.bound, refusal.bound
    assert dataclasses.is_dataclass(refusal)


def test_an_unknown_class_is_refused():
    table = _table()
    table["huge"] = _size(8, 16, 32, 16)
    refusals = check_size(table)
    # MUTATION: skip unknown names and an admin has created a class, which
    # every profile, pool and parity check would then miss.
    assert [r for r in refusals if r.resource_class == "huge"], refusals


def test_a_class_the_table_omits_is_checked_as_its_default():
    table = dict(default_sizes())
    del table["browser"]
    assert check_size(table) == []
    # ...and the ordering still holds against the default it stands for.
    table["standard"] = _size(4.25, 8, 8, 4)
    assert [r for r in check_size(table) if "browser" in r.bound], check_size(table)


@pytest.mark.parametrize("value", ["4", None, float("nan"), float("inf")], ids=["string", "none", "nan", "inf"])
def test_a_value_that_is_no_number_is_refused_not_raised(value):
    refusals = check_size(_table(standard=_size(value, 8, 8, 4)))
    # MUTATION: compare without the type check and a string raises TypeError.
    assert _refused(refusals, "standard", "cpu_request"), refusals


def _random_size(rng: random.Random, floor: ResourceSize | None) -> ResourceSize:
    step = profiles.CPU_REQUEST_STEP
    cpu = rng.randint(1, 34) * step  # 0.25 ... 8.5, both edges outside
    if floor is not None and rng.random() < 0.8:
        cpu = max(cpu, floor.cpu_request)
    limit = cpu * rng.choice([0.9, 1, 1.25, 1.5, 2, 2, 2.1])
    # Mostly near Autopilot's ratio, with both edges reachable.
    memory = max(1, int(cpu * rng.uniform(0.75, 7.0)))
    if floor is not None and rng.random() < 0.8:
        memory = max(memory, floor.memory_gib)
    disk = rng.randint(0, max(memory, 1))
    if floor is not None and rng.random() < 0.8:
        disk = max(disk, floor.disk_gib)
    return _size(cpu, limit, memory, disk)


def _random_split(rng: random.Random, size: ResourceSize) -> tuple[LiveShare, LiveShare]:
    step = profiles.CPU_REQUEST_STEP
    worker_cpu = rng.randint(2, max(int(size.cpu_request / step) - 2, 2)) * step
    display_cpu = size.cpu_request - worker_cpu
    display_memory = rng.randint(4, max(size.memory_gib - 1, 4))
    return (
        LiveShare(worker_cpu, worker_cpu * rng.choice([1, 2]), size.memory_gib - display_memory),
        LiveShare(display_cpu, display_cpu * rng.choice([1, 1.5]), display_memory),
    )


def test_whatever_check_size_accepts_is_inside_autopilots_ratio():
    """A sweep over random tables (seeded, so a failure reproduces).

    Autopilot raises the smaller resource of a pod outside 1:1 ... 1:6.5 and
    bills it, so a table the validator accepts must never render one. Each
    accepted class's pod requests are its `cpu_request` and `memory_gib` (a
    live split sums to them), which is what is checked, with the count of
    tables visited, accepted and refused printed so an empty sweep cannot
    pass as a clean one.
    """
    rng = random.Random(1689)
    visited = accepted = 0
    for _ in range(5000):
        table: dict[str, ResourceSize] = {}
        floor = None
        for name in profiles.CLASS_ORDER:
            size = _random_size(rng, floor)
            if name == "browser" and size.cpu_request >= 1 and size.memory_gib >= 6:
                size = dataclasses.replace(size, live_split=_random_split(rng, size))
            table[name] = size
            floor = size
        visited += 1
        if check_size(table):
            continue
        accepted += 1
        for name, size in table.items():
            ratio = size.memory_gib / size.cpu_request
            # MUTATION: drop the memory-per-CPU check from check_size and an
            # accepted table lands outside the ratio here.
            assert 1 <= ratio <= 6.5, (name, size)
            assert size.cpu_request <= size.cpu_limit <= 2 * size.cpu_request, (name, size)
            assert 1 <= size.disk_gib <= size.memory_gib - 2, (name, size)
            assert size.units == units_for(size.cpu_request, size.memory_gib)
            if size.live_split is not None:
                worker, display = size.live_split
                assert worker.cpu_request + display.cpu_request == size.cpu_request
                assert worker.memory_gib + display.memory_gib == size.memory_gib
    print(f"visited {visited} tables: {accepted} accepted, {visited - accepted} refused")
    # The control: the sweep reached both sides of the validator.
    assert visited == 5000
    assert 30 <= accepted <= visited - 30, (visited, accepted)


def test_request_rs_2_is_filed_as_entry_109():
    """The change the tests above hold has its entry, under the design's §5
    name (CLAUDE.md rule 1)."""
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 109 |")]
    assert len(rows) == 1, rows
    assert "SIZES-CONTRACT-2" in rows[0] and "RS-2" in rows[0]
    assert "APPLIED" in rows[0]
    assert re.search(r"^## 109\. .*\(SIZES-CONTRACT-2, runner sizes request RS-2, part of #1689\)$", text, re.M)
