"""`swarm follow` and `swarm tail` print one line per start-up step, naming it (STARTUP-API).

`startup_progress` (contract request 74) is one event type for seven steps,
the step in `detail.step`. Printed by type alone, a browser agent's start-up
read as the same words seven times; each line now names its step.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from swarm_mcp import cli
from swarm_mcp import follow as follow_module
from swarm_mcp.follow import event_label

AT = datetime(2026, 10, 13, 9, 0, 0, tzinfo=timezone.utc)
STEPS = ("pod_scheduled", "image_pulling", "image_pulled", "container_started",
         "browser_launching", "browser_ready", "first_frame")


def _step(step: str, n: int) -> dict:
    return {
        "type": "startup_progress",
        "at": (AT + timedelta(seconds=n)).isoformat(),
        "attempt_id": "att_x",
        "generation": 1,
        "detail": {"step": step, "attempt_id": "att_x", "generation": 1},
    }


def test_a_step_is_labelled_with_its_step():
    assert event_label(_step("image_pulled", 0)) == "startup_progress image_pulled"


def test_every_other_event_is_labelled_as_before():
    assert event_label({"type": "dispatched", "detail": {"backend": "GKE_AUTOPILOT"}}) == "dispatched"
    assert event_label({"type": "startup_progress", "detail": "cut to a string"}) == "startup_progress"
    legacy = {"type": "cancelled", "detail": {"phase": "cancel_requested"}}
    assert event_label(legacy) == "cancel_requested"


def test_follow_prints_one_line_per_step():
    report = {
        "tasks": [{
            "task_id": "task_0123456789ab4674b39f", "step_id": None, "read": "ok", "state": "STARTING",
            "terminal": False,
            "events": {"status": "ok", "new": [_step(step, n) for n, step in enumerate(STEPS)]},
            "logs": {"status": "ok", "streams": [], "notes": []},
        }],
        "cursor": {},
        "truncation": [],
    }

    lines = [line for line in follow_module.render(report) if "startup_progress" in line]

    assert [line.rsplit(" ", 1)[1] for line in lines] == list(STEPS)


def test_tail_prints_the_label_not_the_bare_type():
    source = inspect.getsource(cli.cmd_tail)
    assert "event_label(event)" in source
