"""Contract requests 33, 35 and 36 (#295), as the owner accepted them on 2026-10-01.

  * 33: `WorkerAction`, `RunnerProfile.worker_action`, the `merge` profile and
    `EndCause.MERGE_REFUSED`/`MERGE_FAILED`;
  * 35: `WorkerAction.POST_VERDICT`, the `post-verdict` profile and
    `EndCause.VERDICT_REFUSED`/`VERDICT_FAILED`;
  * 36: `RunnerProfile.never_restore_checkpoint` (the owner's name, 2026-09-30,
    over `restore_on_retry`) and the `claude-code-review` profile.

AND THE OWNER'S CONDITION ON ALL THREE: #295 stays disabled for every tenant
until #342 is enforced and the review and merge Apps exist. A catalogue entry
that is `available` is dispatchable the moment it merges, so the three are
held to `available=False` here, with a reason a caller can act on.

WRITTEN TO FAIL BEFORE THE CHANGE, ON ITS ASSERTIONS: everything new is reached
through the module at test time, so on the old contract each case runs and
fails on what it asserts rather than the file failing to import.
"""

from __future__ import annotations

import dataclasses

import pytest

from swarm_common import models, profiles
from swarm_common.profiles import RUNNER_PROFILES, Backend, RunnerProfile

NEW_PROFILES = ("merge", "post-verdict", "claude-code-review")


def _profile(**overrides):
    base = dict(
        name="probe",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
    )
    base.update(overrides)
    return RunnerProfile(**base)


def test_worker_action_is_an_enum_of_the_two_accepted_actions():
    action = getattr(profiles, "WorkerAction", None)
    assert action is not None, "swarm_common.profiles has no WorkerAction (contract request 33)"
    assert [a.value for a in action] == ["merge", "post_verdict"]
    assert issubclass(action, str)


def test_runner_profile_carries_worker_action_and_never_restore_checkpoint_defaulting_off():
    fields = {f.name: f for f in dataclasses.fields(RunnerProfile)}
    assert "worker_action" in fields, "RunnerProfile has no worker_action (contract request 33)"
    assert fields["worker_action"].default is None
    assert "never_restore_checkpoint" in fields, (
        "RunnerProfile has no never_restore_checkpoint (contract request 36)"
    )
    assert fields["never_restore_checkpoint"].default is False
    assert "restore_on_retry" not in fields, "the owner chose never_restore_checkpoint on 2026-09-30"
    # Every profile that existed before #295 is unchanged by both fields.
    for name in ("mock", "generic", "claude-code", "codex", "browser"):
        assert RUNNER_PROFILES[name].worker_action is None, name
        assert RUNNER_PROFILES[name].never_restore_checkpoint is False, name


def test_a_worker_action_with_a_runner_argv_is_refused():
    with pytest.raises(ValueError, match="worker_action"):
        _profile(worker_action=profiles.WorkerAction.MERGE)


def test_an_empty_runner_argv_without_a_worker_action_is_refused():
    with pytest.raises(ValueError, match="runner_argv"):
        _profile(runner_argv=())


def test_a_worker_action_with_an_empty_argv_is_accepted():
    p = _profile(runner_argv=(), worker_action=profiles.WorkerAction.MERGE)
    assert p.worker_action is profiles.WorkerAction.MERGE


def test_merge_is_the_profile_contract_requests_33_and_47_name():
    p = RUNNER_PROFILES["merge"]
    assert p.image == "agent-runtime-base"
    assert p.resource_class == "standard"
    assert p.backend is Backend.CLOUD_RUN_JOB
    assert p.runner_argv == ()
    assert p.worker_action is profiles.WorkerAction.MERGE
    # Contract request 47 (owner, 2026-10-04): the tenant's existing `-git`
    # token, and enabled.
    assert p.provider == "git"
    assert p.available is True
    assert p.disabled_reason is None or p.disabled_reason == ""
    assert p.secrets == ()
    assert p.timeout_seconds == 600
    assert dict(p.inputs) == {}
    assert p.never_restore_checkpoint is False


def test_post_verdict_is_the_profile_contract_request_35_names():
    p = RUNNER_PROFILES["post-verdict"]
    assert p.image == "agent-runtime-base"
    assert p.resource_class == "standard"
    assert p.backend is Backend.CLOUD_RUN_JOB
    assert p.runner_argv == ()
    assert p.worker_action is profiles.WorkerAction.POST_VERDICT
    assert p.provider == "git-review"
    assert p.secrets == ()
    assert p.timeout_seconds == 300
    assert dict(p.inputs) == {}


def test_claude_code_review_is_claude_code_but_its_name_and_never_restore_checkpoint():
    review = RUNNER_PROFILES["claude-code-review"]
    code = RUNNER_PROFILES["claude-code"]
    assert review.never_restore_checkpoint is True
    assert review.worker_action is None
    for field in (
        "image", "resource_class", "backend", "runner_argv", "provider", "secrets",
        "secrets_any_of", "timeout_seconds", "cost_declared", "supports_checkpoint",
        "checkpoint_interval_seconds",
    ):
        assert getattr(review, field) == getattr(code, field), field
    assert dict(review.inputs) == dict(code.inputs)


@pytest.mark.parametrize("name", ("post-verdict", "claude-code-review"))
def test_the_two_app_profiles_stay_disabled(name):
    """Contract request 47 enabled `merge` alone; the other two keep the owner's
    2026-10-01 hold and its reason."""
    p = RUNNER_PROFILES[name]
    assert p.available is False, f"{name} is dispatchable; the owner holds #295 disabled"
    assert "#342" in p.disabled_reason and "#295" in p.disabled_reason, p.disabled_reason


@pytest.mark.parametrize("name", NEW_PROFILES)
def test_checkpointing_stays_on_for_the_three(name):
    # Invariant 8: what never_restore_checkpoint / worker_action skip is the
    # RESTORE. Mandatory periodic checkpointing is unchanged.
    assert RUNNER_PROFILES[name].supports_checkpoint is True


def test_the_four_end_causes_are_in_the_frozen_contract():
    values = [c.value for c in models.EndCause]
    for value in ("merge_refused", "merge_failed", "verdict_refused", "verdict_failed"):
        assert value in values, f"EndCause has no {value}"
    assert models.EndCause("merge_refused").name == "MERGE_REFUSED"
    assert models.EndCause("verdict_failed").name == "VERDICT_FAILED"
