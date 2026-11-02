"""Contract requests 118-121, the per-agent budget, as applied (BUDGET-CONTRACT, part of #2459).

Accepted by BGD 2026-10-26 (~22:15Z) on PR 2466 and applied by lane
BUDGET-CONTRACT. Each is the request's own wording in
docs/contract-change-requests.md:

  * 118 `EndCause.BUDGET_EXCEEDED = "budget_exceeded"`, written by the worker;
  * 119 a `"budget"` block in `SETTINGS_MAC_FIELDS`, `("unit", "amount")`;
  * 120 `Task.budget` and `WorkflowStep.budget`, with a spec format that
    covers `Task.budget`, signed at that format only when it is set;
  * 121 `EventType.BUDGET_WARNING = "budget_warning"`, written by the worker.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_common import specsign
from swarm_common.models import EndCause, Task, TaskState, WorkflowStep
from swarm_common.specsign import (
    SETTINGS_MAC_FIELDS,
    SpecNotCanonical,
    canonical_settings_block,
    canonical_step_spec,
    signing_format,
)
from swarm_common.states import EventType

REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"

DOC = {
    "tenant_id": "eng",
    "runner_profile": "claude-code",
    "resource_class": "standard",
    "timeout_seconds": 3600,
    "max_attempts": 3,
    "input": {"prompt": "x"},
    "submitted_by": "alice@example.com",
    "metadata": {"dispatch": {"strategy": "direct-pr"}},
}


def test_118_the_end_cause_was_appended_after_child_cascade():
    assert EndCause.BUDGET_EXCEEDED.value == "budget_exceeded"
    assert EndCause("budget_exceeded") is EndCause.BUDGET_EXCEEDED
    # Appended, so no reader's order of the earlier causes moves. Contract
    # request 126 (`fix_loop_exhausted`) was appended after it, 2026-10-28.
    causes = list(EndCause)
    assert causes.index(EndCause.BUDGET_EXCEEDED) == causes.index(EndCause.CHILD_CASCADE) + 1


def test_121_the_warning_is_its_own_event_type():
    assert EventType.BUDGET_WARNING.value == "budget_warning"
    assert EventType("budget_warning") is EventType.BUDGET_WARNING


def test_120_the_fields_default_to_none_and_come_last():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    task = Task(id="task_1", tenant_id="eng", created_at=now, updated_at=now,
                state=TaskState.READY, runner_profile="mock", resource_class="standard",
                input={}, submitted_by="alice@example.com")
    assert task.budget is None and task.to_firestore()["budget"] is None
    assert WorkflowStep(step_id="a", runner_profile="mock", input={}).budget is None
    # Contract request 123 (#1820) appended `platform_refusals` after it, and
    # request 132 the two read-side views; `budget` keeps its position, which
    # is what this holds.
    assert [f.name for f in fields(Task)][-4:] == [
        "budget", "platform_refusals", "attempt_totals", "current_account",
    ]
    assert [f.name for f in fields(WorkflowStep)][-1] == "budget"


def test_120_a_budget_is_signed_at_format_6_and_only_a_budget_is():
    assert specsign.SPEC_FORMAT == 6 and 6 in specsign.SPEC_FORMATS
    # MUTATION: leave `signing_format` at 5 and this is 1.
    assert signing_format({**DOC, "budget": {"usd": 1.0}}) == 6
    assert signing_format({**DOC, "budget": {"tokens": 10}, "model_class": "opus"}) == 6
    assert signing_format(DOC) == 1, "a task with no budget keeps its old format"
    assert signing_format({**DOC, "budget": None}) == 1


def test_120_the_budget_is_inside_format_6s_bytes():
    signed = canonical_step_spec({**DOC, "budget": {"usd": 1.0}}, task_id="task_1", spec_format=6)
    assert json.loads(signed)["budget"] == {"usd": 1.0}
    raised = canonical_step_spec({**DOC, "budget": {"usd": 9.0}}, task_id="task_1", spec_format=6)
    removed = canonical_step_spec(DOC, task_id="task_1", spec_format=6)
    assert signed != raised and signed != removed


@pytest.mark.parametrize("older", [1, 2, 3, 4, 5])
def test_120_a_budget_has_no_canonical_form_below_format_6(older):
    """Adding a budget to a task signed without one must fail verification,
    not slip outside the signed bytes."""
    with pytest.raises(SpecNotCanonical):
        canonical_step_spec({**DOC, "budget": {"usd": 1.0}}, task_id="task_1", spec_format=older)
    # The control: the same document without it is canonical at that format.
    canonical_step_spec(DOC, task_id="task_1", spec_format=older)


def test_120_older_formats_bytes_do_not_change():
    for spec_format in (1, 2, 3, 4, 5):
        assert b'"budget"' not in canonical_step_spec(DOC, task_id="t", spec_format=spec_format)


def test_119_the_budget_block_covers_its_unit_and_amount():
    assert SETTINGS_MAC_FIELDS["budget"] == ("unit", "amount")
    out = canonical_settings_block(tenant_id="eng", block="budget",
                                   fields={"unit": "usd", "amount": 2, "updated_by": "root"})
    assert json.loads(out)["fields"] == {"unit": "usd", "amount": 2}
    forged = canonical_settings_block(tenant_id="eng", block="budget",
                                      fields={"unit": "usd", "amount": 2000})
    assert forged != out


def test_each_request_is_marked_applied():
    text = REQUESTS.read_text()
    for number in (118, 119, 120, 121):
        row = next(line for line in text.splitlines() if line.startswith(f"| {number} |"))
        # MUTATION: leave a row "Not applied" and this names it.
        assert "APPLIED 2026-10-27" in row and "Not applied" not in row, number
        section = text.split(f"\n## {number}. ", 1)[1].split("\n## ", 1)[0]
        # scripts/lib/check-frozen-contract.sh passes a swarm_common diff only on an
        # added line saying "accepted by the owner"; this status line is that line.
        assert section.splitlines()[2].startswith("**Status:** ACCEPTED by the owner (BGD)"), number
