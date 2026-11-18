"""`EventType.STARTUP_PROGRESS` and the one list of start-up steps (STARTUP-API, contract request 74).

The owner, 2026-10-13: a progress bar while a browser agent starts, "Stages +
real pod steps", carried as "One new event type". The steps are written by
three components and served by a fourth, none of which may import the others:
the reconciler (the pod's four), the worker (the browser's three), swarm-api
(all seven, in order) and swarm-mcp (prints them). Each restates its part of
the list; this holds every restatement to the one the lanes agreed, so a step
renamed in one place fails here rather than vanishing from the bar.

No shell, jq or TypeScript copy of `EventType` exists
(`scripts/lib/check-contract-parity.sh` restates none); the last test says so,
so a copy added later is found.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_common.states import EventType

REPO = Path(__file__).resolve().parents[3]

#: The shared shape both lanes code against (the lane brief, 2026-10-13).
STEPS = (
    "pod_scheduled",
    "image_pulling",
    "image_pulled",
    "container_started",
    "browser_launching",
    "browser_ready",
    "first_frame",
)


def test_the_member_is_the_one_the_owner_accepted():
    assert EventType.STARTUP_PROGRESS.value == "startup_progress"
    assert EventType("startup_progress") is EventType.STARTUP_PROGRESS


def test_the_existing_stages_are_unchanged():
    """`lease_acquired`, `dispatched`, `starting`, `running` stay as they are: the bar's first stages."""
    assert [
        EventType.LEASE_ACQUIRED.value,
        EventType.DISPATCHED.value,
        EventType.STARTING.value,
        EventType.RUNNING.value,
    ] == ["lease_acquired", "dispatched", "starting", "running"]


def test_the_reconciler_writes_the_pods_four():
    from reconciler.startsteps import POD_STARTUP_STEPS

    assert POD_STARTUP_STEPS == STEPS[:4]


def test_the_worker_writes_the_browsers_three():
    from agent_worker.handoff import GATE_STARTUP_STEPS

    assert GATE_STARTUP_STEPS == STEPS[4:]


def test_swarm_api_serves_all_seven_after_the_four_stages():
    from swarm_api.startup import ALL_STEPS, STAGE_EVENTS, STARTUP_STEPS

    assert STARTUP_STEPS == STEPS
    assert STAGE_EVENTS == ("lease_acquired", "dispatched", "starting", "running")
    assert ALL_STEPS == STAGE_EVENTS + STEPS


def test_the_contract_request_is_recorded_as_accepted_and_applied():
    text = (REPO / "docs" / "contract-change-requests.md").read_text(encoding="utf-8")
    heading = (
        "## 74. `states.py`: `EventType.STARTUP_PROGRESS`, the browser agent's "
        "start-up steps (STARTUP-API)"
    )
    assert heading in text
    entry = text.split(heading, 1)[1]
    assert "ACCEPTED by the owner 2026-10-13" in entry.split("###", 1)[0]
    assert "APPLIED" in entry.split("###", 1)[0]
    for section in ("### Why", "### The change", "### Rollback", "### What it would break"):
        assert section in entry, section
    assert re.search(r"^\| 74 \|", text, re.MULTILINE), "the index has no row for 74"


def test_no_shell_or_jq_copy_of_the_event_types_exists():
    """If one is added, check-contract-parity.sh must hold it; until then there is nothing to keep in step."""
    copies = []
    for path in [*(REPO / "scripts").rglob("*.sh"), *(REPO / "scripts").rglob("*.jq")]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "human_handoff_requested" in text or "generation_fenced" in text:
            copies.append(str(path.relative_to(REPO)))
    assert copies == [], f"a shell/jq restatement of EventType now exists; add it to the parity check: {copies}"
