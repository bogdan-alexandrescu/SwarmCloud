"""Contract request 108: invariant 7 amended for CPU, `ResourceClass.cpu_limit` (SIZES-CONTRACT-1, part of #1689).

docs/design/runner-sizes.md §5 request RS-1, accepted by the owner on
2026-10-20 (§7: Q1 A, a CPU limit of 2 x the request with the requests
unchanged). What is held here:

  * every class keeps its request and gains a limit, `cpu <= cpu_limit_cores
    <= CPU_BURST_MAX_RATIO * cpu`, at §4.1's numbers (standard 4/8, browser
    4/8, large 8/16);
  * memory and disk have no limit field: one number each, request AND limit,
    so they cannot differ;
  * a limit above 2 x the request, or below it, is refused at construction;
  * the Cloud Run ceiling binds the request, not the limit, so `large` at
    8/16 constructs and a request of 9 is still refused;
  * CONTRACT.md's invariant 7 says CPU may burst and memory may not, and
    profiles.py's docstring no longer forbids every burst;
  * the request is filed in docs/contract-change-requests.md as entry 108.

No cloud, no emulator: the contract is pure. Nothing here renders a pod; the
manifests start reading `cpu_limit_cores` in lane SIZES-SCHED-1.

MUTATION: leave `cpu_limit` unset on `standard` and
`test_every_class_bursts_to_twice_its_request["standard"]` fails; drop the
ratio check from `__post_init__` and
`test_a_limit_above_the_ratio_is_refused` fails; restore invariant 7's
"No bursting" line and `test_invariant_7_text_names_cpu_only` fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from swarm_common import profiles
from swarm_common.profiles import RESOURCE_CLASSES, ResourceClass

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "CONTRACT.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"

#: §4.1, the owner's Q1 answer: requests unchanged, limit twice the request.
EXPECTED = {"standard": (4, 8), "browser": (4, 8), "large": (8, 16)}


def test_the_burst_ratio_is_two():
    # MUTATION: CPU_BURST_MAX_RATIO = 3 (or delete it) and this fails.
    assert profiles.CPU_BURST_MAX_RATIO == 2


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_class_bursts_to_twice_its_request(name):
    rc = RESOURCE_CLASSES[name]
    request, limit = EXPECTED[name]
    # MUTATION: change a class's `cpu` and the request half fails -- Q2 A
    # keeps every request where it was.
    assert rc.cpu == request
    # MUTATION: drop `cpu_limit=` from the class and this reads the request.
    assert rc.cpu_limit_cores == limit
    assert rc.cpu <= rc.cpu_limit_cores <= profiles.CPU_BURST_MAX_RATIO * rc.cpu


def test_the_catalogue_names_exactly_the_three_classes():
    # MUTATION: add a class without a row in EXPECTED and this fails, so a new
    # class cannot skip the burst assertions above.
    assert set(RESOURCE_CLASSES) == set(EXPECTED)


def test_memory_and_disk_have_no_limit_field():
    # MUTATION: add `memory_limit_gib` (or any `*_limit*` but CPU's) and this fails.
    names = {f.name for f in dataclasses.fields(ResourceClass)}
    assert "cpu_limit" in names
    limits = sorted(n for n in names if "limit" in n and n != "cpu_limit")
    assert limits == [], f"memory and disk are request AND limit; found {limits}"


def test_an_unset_limit_equals_the_request():
    # MUTATION: default `cpu_limit_cores` to 2 x cpu and this fails.
    rc = ResourceClass("probe", cpu=2, memory_gib=4, disk_gib=1, units=1)
    assert rc.cpu_limit is None
    assert rc.cpu_limit_cores == 2


def test_a_limit_above_the_ratio_is_refused():
    # MUTATION: remove the upper bound from `__post_init__` and this constructs.
    with pytest.raises(ValueError, match="cpu_limit"):
        ResourceClass("probe", cpu=4, memory_gib=8, disk_gib=4, units=1, cpu_limit=10)


def test_a_limit_below_the_request_is_refused():
    # MUTATION: remove the lower bound from `__post_init__` and this constructs.
    with pytest.raises(ValueError, match="cpu_limit"):
        ResourceClass("probe", cpu=4, memory_gib=8, disk_gib=4, units=1, cpu_limit=3)


def test_a_limit_equal_to_the_request_constructs():
    rc = ResourceClass("probe", cpu=4, memory_gib=8, disk_gib=4, units=1, cpu_limit=4)
    assert rc.cpu_limit_cores == 4


def test_the_cloud_run_ceiling_binds_the_request_not_the_limit():
    # MUTATION: apply the Cloud Run ceiling to `cpu_limit_cores` and `large`
    # at 8/16 is refused at import, so this module does not even load.
    rc = ResourceClass("probe", cpu=8, memory_gib=32, disk_gib=16, units=4, cpu_limit=16)
    assert rc.cpu_limit_cores == 16
    # MUTATION: drop the request's ceiling and a 9-core request constructs.
    with pytest.raises(ValueError, match="ceiling"):
        ResourceClass("probe", cpu=9, memory_gib=32, disk_gib=16, units=4, cpu_limit=9)


def _invariant_7(text: str) -> str:
    match = re.search(r"^7\. (.*?)(?=^8\. )", text, re.M | re.S)
    assert match, "CONTRACT.md has no invariant 7"
    return match.group(1)


def test_invariant_7_text_names_cpu_only():
    seven = _invariant_7(CONTRACT.read_text(encoding="utf-8"))
    # MUTATION: restore "requests == limits. No bursting" and these fail.
    assert "No bursting" not in seven
    assert "Memory and ephemeral storage: requests == limits" in seven
    assert "CPU: request <= limit <= `CPU_BURST_MAX_RATIO` x request" in seven
    assert "GKE" in seven and "Cloud Run renders the request" in seven
    assert "never killed" in seven and "OOM-killed" in seven
    # invariant 8 is named, because Burstable QoS raises the OOM score.
    assert "invariant 8" in seven


def test_profiles_docstring_no_longer_forbids_every_burst():
    doc = profiles.__doc__ or ""
    # MUTATION: restore "requests == limits. Bursting past a request ..." and this fails.
    assert "we never do it" not in doc
    assert "memory and disk: requests == limits" in doc.lower()
    assert "CPU_BURST_MAX_RATIO" in doc


def test_request_rs_1_is_filed_as_entry_108():
    """The change the tests above hold has its entry, under the design's §5
    name (CLAUDE.md rule 1)."""
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 108 |")]
    assert len(rows) == 1, rows
    assert "SIZES-CONTRACT-1" in rows[0] and "RS-1" in rows[0]
    assert "APPLIED" in rows[0]
    assert re.search(r"^## 108\. .*\(SIZES-CONTRACT-1, runner sizes request RS-1, part of #1689\)$", text, re.M)
