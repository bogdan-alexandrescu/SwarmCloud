"""Contract requests 92-94: the await-input step's frozen contract (HW-CONTRACT, part of #1436).

docs/design/human-steps.md §5 requests A, B and C, accepted by the owner on
2026-10-17 (numbered 85-87 in the design; filed as 92-94 because 85-91 were
taken first). What is held here:

  * `ParkReason.AWAITING_INPUT` exists, and a task parked under it holds no
    capacity (invariant 1): PARKED is pending, never a concurrency state;
  * `RunnerProfile.control_plane` refuses every field that only a runner, a
    worker or a pod would read, and admits an empty `runner_argv`;
  * the `await-input` entry is `control_plane`, and Terraform makes no Job for
    it; it shipped unavailable, and is available since request D (entry 105,
    HW-ENABLE, applied after HW-ACCEPT);
  * the requests are filed in docs/contract-change-requests.md.

MUTATION: drop `"provider"` from the control_plane checks in `__post_init__`
and `test_a_control_plane_profile_refuses_what_only_a_runner_reads[provider]`
fails; drop "await-input" from `profiles_without_a_job` in locals.tf and
`test_terraform_makes_no_job_for_a_control_plane_profile` fails; set
`available=False` again and `test_await_input_is_available_since_request_d`
fails.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.profiles import (
    RUNNER_PROFILES,
    Backend,
    RunnerInput,
    RunnerProfile,
    SecretSlot,
    WorkerAction,
)
from swarm_common.states import (
    CONCURRENCY_STATES,
    PENDING_STATES,
    TERMINAL_STATES,
    ParkReason,
    TaskState,
    holds_capacity,
)

REPO = Path(__file__).resolve().parents[3]
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
REQUESTS = REPO / "docs" / "contract-change-requests.md"


# --------------------------------------------------------------------------
# Request A (92): the park reason
# --------------------------------------------------------------------------


def test_awaiting_input_is_a_park_reason():
    assert ParkReason("AWAITING_INPUT") is ParkReason.AWAITING_INPUT
    assert ParkReason.AWAITING_INPUT.value == "AWAITING_INPUT"


def test_awaiting_input_is_in_no_capacity_set():
    """Invariant 1: the reason rides on PARKED, which costs nothing."""
    reason = ParkReason.AWAITING_INPUT.value
    for states in (CONCURRENCY_STATES, PENDING_STATES, TERMINAL_STATES):
        assert reason not in {s.value for s in states}
    assert TaskState.PARKED in PENDING_STATES
    assert TaskState.PARKED not in CONCURRENCY_STATES
    assert not holds_capacity(TaskState.PARKED)


def test_awaiting_input_is_not_human_required():
    """Design §3.4: a new member, because HUMAN_REQUIRED's readers act on it."""
    assert ParkReason.AWAITING_INPUT is not ParkReason.HUMAN_REQUIRED
    assert ParkReason.AWAITING_INPUT.value != ParkReason.HUMAN_REQUIRED.value


# --------------------------------------------------------------------------
# Request B (93): RunnerProfile.control_plane
# --------------------------------------------------------------------------


def _control_plane(**overrides: Any) -> RunnerProfile:
    fields: dict[str, Any] = {
        "name": "cp-test",
        "image": "agent-runtime-base",
        "resource_class": "standard",
        "backend": Backend.CLOUD_RUN_JOB,
        "runner_argv": (),
        "control_plane": True,
    }
    fields.update(overrides)
    return RunnerProfile(**fields)


def test_a_control_plane_profile_may_have_an_empty_runner_argv():
    profile = _control_plane()
    assert profile.control_plane is True
    assert profile.runner_argv == ()
    assert profile.worker_action is None


def test_an_empty_runner_argv_is_still_refused_on_a_profile_that_runs():
    """The control: request 33's refusal stands for everything else."""
    with pytest.raises(ValueError, match="empty runner_argv"):
        RunnerProfile(
            name="runs-nothing",
            image="agent-runtime-base",
            resource_class="standard",
            backend=Backend.CLOUD_RUN_JOB,
            runner_argv=(),
        )


@pytest.mark.parametrize(
    "field_name,overrides",
    [
        ("runner_argv", {"runner_argv": ("python", "-m", "agent_worker.runners.mock")}),
        ("worker_action", {"worker_action": WorkerAction.MERGE}),
        ("provider", {"provider": "anthropic"}),
        ("secrets", {"secrets": ("ANTHROPIC_API_KEY",)}),
        ("secret_slots", {"secret_slots": {"files": SecretSlot(kinds=("opaque",), means="test")}}),
        ("inputs", {"inputs": {"steps": RunnerInput("integer", minimum=1, maximum=3)}}),
        # GKE_AUTOPILOT, so the refusal is control_plane's, not request 71's.
        ("live_browser", {"live_browser": True, "backend": Backend.GKE_AUTOPILOT}),
    ],
)
def test_a_control_plane_profile_refuses_what_only_a_runner_reads(field_name, overrides):
    with pytest.raises(ValueError, match=rf"control_plane profile .*sets no {field_name}"):
        _control_plane(**overrides)


def test_every_profile_that_runs_is_not_control_plane():
    """The default is False, and no existing profile changed."""
    assert {name for name, p in RUNNER_PROFILES.items() if p.control_plane} == {"await-input", "fix-loop"}
    assert next(f for f in dataclasses.fields(RunnerProfile) if f.name == "control_plane").default is False


# --------------------------------------------------------------------------
# Request C (94): the await-input entry
# --------------------------------------------------------------------------


def test_await_input_is_a_control_plane_profile_that_runs_nothing():
    profile = RUNNER_PROFILES["await-input"]
    assert profile.name == "await-input"
    assert profile.control_plane is True
    assert profile.runner_argv == ()
    assert profile.worker_action is None
    assert profile.provider is None
    assert profile.secrets == ()
    assert dict(profile.secret_slots) == {}
    assert dict(profile.inputs) == {}
    assert profile.live_browser is False


def test_await_input_is_available_since_request_d():
    """Request D (entry 105, HW-ENABLE): `available=True`, `disabled_reason`
    removed, and nothing else about the entry changed."""
    profile = RUNNER_PROFILES["await-input"]
    assert profile.available is True
    assert profile.disabled_reason == ""
    assert profile.control_plane is True


def _profiles_without_a_job() -> set[str]:
    text = LOCALS_TF.read_text(encoding="utf-8")
    match = re.search(r"^\s*profiles_without_a_job\s*=\s*\[([^\]]*)\]", text, re.M)
    assert match, "profiles_without_a_job not found in terraform/infra/locals.tf"
    return set(re.findall(r'"([a-z0-9-]+)"', match.group(1)))


def test_terraform_makes_no_job_for_a_control_plane_profile():
    without = _profiles_without_a_job()
    control_plane = {name for name, p in RUNNER_PROFILES.items() if p.control_plane}
    assert control_plane, "no control_plane profile in the catalogue"
    assert control_plane <= without, sorted(control_plane - without)
    assert "await-input" in without


def test_terraform_mirrors_await_input():
    text = LOCALS_TF.read_text(encoding="utf-8")
    entry = re.search(r'^\s*"await-input"\s*=\s*\{(.*?)\n\s*\}', text, re.M | re.S)
    assert entry, "await-input is not in terraform/infra/locals.tf runner_profiles"
    body = entry.group(1)
    assert re.search(r'image\s*=\s*"agent-runtime-base"', body)
    assert re.search(r'backend\s*=\s*"CLOUD_RUN_JOB"', body)
    assert re.search(r"provider\s*=\s*null", body)
    assert re.search(r"timeout_seconds\s*=\s*300\b", body)


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number,request_letter",
    [("92", "human steps request A"), ("93", "human steps request B"),
     ("94", "human steps request C")],
)
def test_the_requests_are_filed_as_accepted_and_applied(number, request_letter):
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith(f"| {number} |")]
    assert len(rows) == 1, rows
    assert "HW-CONTRACT" in rows[0] and request_letter in rows[0]
    assert "ACCEPTED by the owner 2026-10-17" in rows[0] and "APPLIED" in rows[0]
    assert re.search(rf"^## {number}\. .*\(HW-CONTRACT, {request_letter}\)$", text, re.M)


def test_request_d_is_filed_as_entry_105():
    """The change `test_await_input_is_available_since_request_d` holds has its
    entry, under the design's §5 letter (CLAUDE.md rule 1)."""
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 105 |")]
    assert len(rows) == 1, rows
    assert "HW-ENABLE" in rows[0] and "human steps request D" in rows[0]
    assert re.search(r"^## 105\. .*\(HW-ENABLE, human steps request D\)$", text, re.M)

