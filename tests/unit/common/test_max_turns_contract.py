"""Contract request 133, the turn cap, as applied (part of #2524 and #2688).

Accepted by the owner 2 November 2026 ("Accept and build"). The
request's own shape in docs/contract-change-requests.md:

  * `RunnerProfile.supports_max_turns` (False by default; True on the six
    profiles whose runner is `agent_worker.runners.claude_code`) and
    `RunnerProfile.max_turns` (None on every profile, so no agent changes);
  * `WorkflowStep.max_turns`, what a step asked, and `Task.max_turns`, what
    it resolved to;
  * spec format 7, format 6's fields plus `max_turns`, at which a task is
    signed only when it has a cap.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from swarm_common import specsign
from swarm_common.models import Task, WorkflowStep
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.specsign import SpecNotCanonical, canonical_step_spec, signing_format

REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"

DOC = {
    "tenant_id": "eng",
    "runner_profile": "claude-code",
    "resource_class": "standard",
    "timeout_seconds": 900,
    "max_attempts": 3,
    "input": {"prompt": "x"},
    "submitted_by": "alice@example.com",
    "metadata": {"dispatch": {"strategy": "direct-pr"}},
}

CLAUDE_CODE_RUNNER = ("python", "-m", "agent_worker.runners.claude_code")


def test_the_claude_code_runners_profiles_support_a_cap_and_no_other_does():
    supported = {name for name, p in RUNNER_PROFILES.items() if p.supports_max_turns}
    assert supported == {
        "claude-code", "claude-code-large", "claude-code-review", "indexer",
        "claude-code-browser", "claude-code-lightpanda", "claude-code-medium",
    }
    # The rule behind the list: exactly the profiles that run the runner that
    # adds `--max-turns`.
    assert supported == {name for name, p in RUNNER_PROFILES.items()
                         if p.runner_argv == CLAUDE_CODE_RUNNER}


def test_no_profile_sets_a_ceiling_so_no_running_agent_changes():
    assert all(p.max_turns is None for p in RUNNER_PROFILES.values())


def test_the_fields_default_to_none_or_false_and_come_last():
    profile = RUNNER_PROFILES["mock"]
    assert profile.supports_max_turns is False and profile.max_turns is None
    # Contract request 136 appends `runtime_capabilities` after them, and
    # contract request 135 `build_resource_class`.
    assert [f.name for f in fields(type(profile))][-4:] == [
        "supports_max_turns", "max_turns", "runtime_capabilities", "build_resource_class"]
    assert [f.name for f in fields(Task)][-1] == "max_turns"
    assert [f.name for f in fields(WorkflowStep)][-1] == "max_turns"
    assert WorkflowStep(step_id="a", runner_profile="mock", input={}).max_turns is None


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "30"])
def test_a_profile_ceiling_is_a_positive_integer(bad):
    with pytest.raises(ValueError, match="max_turns"):
        replace(RUNNER_PROFILES["claude-code"], max_turns=bad)
    assert replace(RUNNER_PROFILES["claude-code"], max_turns=50).max_turns == 50


def test_a_cap_is_signed_at_format_7_and_only_a_cap_is():
    # Format 8 (contract request 137, `metadata.work_contract`) came after it.
    assert specsign.SPEC_FORMATS[6] == 7 and specsign.SPEC_FORMAT > 7
    # MUTATION: leave `signing_format` without format 7 and this is 1.
    assert signing_format({**DOC, "max_turns": 30}) == 7
    assert signing_format({**DOC, "max_turns": 30, "budget": {"usd": 1.0}}) == 7
    assert signing_format(DOC) == 1, "a task with no cap keeps its old format"
    assert signing_format({**DOC, "max_turns": None}) == 1
    assert signing_format({**DOC, "budget": {"usd": 1.0}}) == 6


def test_the_cap_is_inside_format_7s_bytes():
    signed = canonical_step_spec({**DOC, "max_turns": 30}, task_id="task_1", spec_format=7)
    assert json.loads(signed)["max_turns"] == 30
    raised = canonical_step_spec({**DOC, "max_turns": 300}, task_id="task_1", spec_format=7)
    removed = canonical_step_spec(DOC, task_id="task_1", spec_format=7)
    assert signed != raised and signed != removed


@pytest.mark.parametrize("older", [1, 2, 3, 4, 5, 6])
def test_a_cap_has_no_canonical_form_below_format_7(older):
    """Adding a cap to a task signed without one must fail verification."""
    with pytest.raises(SpecNotCanonical):
        canonical_step_spec({**DOC, "max_turns": 30}, task_id="task_1", spec_format=older)
    canonical_step_spec(DOC, task_id="task_1", spec_format=older)


def test_a_cap_rewritten_to_a_float_has_no_canonical_form():
    with pytest.raises(SpecNotCanonical, match="max_turns is a float"):
        canonical_step_spec({**DOC, "max_turns": 30.0}, task_id="task_1", spec_format=7)


def test_older_formats_bytes_do_not_change():
    for spec_format in (1, 2, 3, 4, 5, 6):
        assert b'"max_turns"' not in canonical_step_spec(DOC, task_id="t", spec_format=spec_format)


def test_the_request_is_marked_accepted_and_applied():
    text = REQUESTS.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| 133 |"))
    assert "ACCEPTED by the owner" in row and "APPLIED 2026-11-02" in row
    assert "owner accepted 2 November 2026" in row and "REQUESTED for the owner" not in row
    section = text.split("\n## 133. ", 1)[1].split("\n## ", 1)[0]
    assert section.splitlines()[2].startswith("**Status:** ACCEPTED by the owner")
