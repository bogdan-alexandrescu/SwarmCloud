"""A step picks its model class by name (contract requests A to D of
docs/design/model-per-step.md §5, part of #1435).

The owner's decisions of 2026-10-17 (PR 1509 and PR 1460): the class names
are in the frozen catalogue and the model IDs in Terraform (Q1); a step names
a class from a closed vocabulary (Q2, design B); `claude-code`,
`claude-code-browser` and `indexer` accept all four classes, `opus` by
default (Q3, R3).

Held here, red before the change:

  * A: `MODEL_CLASSES` is exactly opus, fable, sonnet and haiku, and each
    class admits an ID of its own family at generation 5 or later, and
    nothing else;
  * B: a profile's classes are refused where they mean nothing (a worker
    action, a provider that is not anthropic) or contradict themselves (a
    default outside them, one without the other, a name outside the
    vocabulary); every available claude-code-family profile accepts all four;
    `step_model_class` is the one resolution rule;
  * C: `Task.model_class` and `WorkflowStep.model_class` exist, default to
    None, and reach the stored document;
  * D: `Attempt.model` exists and defaults to None.

Request E (spec format 5 covers `model_class`) is held in test_specsign.py.
"""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import datetime, timezone

import pytest

from swarm_common import profiles
from swarm_common.models import Attempt, Task, WorkflowStep
from swarm_common.profiles import RUNNER_PROFILES, WorkerAction
from swarm_common.states import TaskState

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
ALL_CLASSES = ("opus", "fable", "sonnet", "haiku")
CLAUDE_CODE_RUNNER = ("python", "-m", "agent_worker.runners.claude_code")


# ---------------------------------------------------------------------------
# Request A: the vocabulary
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_the_four_classes_the_owner_named():
    assert tuple(profiles.MODEL_CLASSES) == ALL_CLASSES
    for name, model_class in profiles.MODEL_CLASSES.items():
        assert model_class.name == name
        assert model_class.family == name
        assert model_class.min_major == 5


@pytest.mark.parametrize(
    "name,model_id",
    [
        ("opus", "claude-opus-5-5"),
        ("fable", "claude-fable-5-1"),
        ("fable", "claude-fable-6"),
        ("sonnet", "claude-sonnet-5-5"),
        ("haiku", "claude-haiku-5-5"),
        ("haiku", "claude-haiku-5-5-20261001"),
    ],
)
def test_a_class_admits_its_own_family_at_generation_5_or_later(name, model_id):
    assert profiles.MODEL_CLASSES[name].admits(model_id)


@pytest.mark.parametrize(
    "name,model_id",
    [
        # Another family's pin: the typo that would quietly bill Opus.
        ("fable", "claude-opus-5-5"),
        ("haiku", "claude-opus-5-5"),
        # Below the floor: a re-pin to an older generation.
        ("sonnet", "claude-sonnet-4-6"),
        ("opus", "claude-opus-4"),
        # Not an ID at all, or an ID with a suffix the CLI would read.
        ("haiku", "claude-haiku-5-5[1m]"),
        ("opus", "opus"),
        ("opus", ""),
        ("opus", " claude-opus-5-5"),
        ("haiku", "claude-haiku-5-5-2026100"),
    ],
)
def test_a_class_admits_nothing_else(name, model_id):
    assert not profiles.MODEL_CLASSES[name].admits(model_id)


def test_a_value_that_is_not_a_string_is_never_admitted():
    assert not profiles.MODEL_CLASSES["opus"].admits(None)  # type: ignore[arg-type]
    assert not profiles.MODEL_CLASSES["opus"].admits(5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Request B: each profile's classes, and the one resolution rule
# ---------------------------------------------------------------------------


def test_every_available_claude_code_family_profile_accepts_all_four():
    """Q3: claude-code, claude-code-browser and indexer, by the runner they start, and
    claude-code-large (contract request 113), which is claude-code on the large class, and
    claude-code-lightpanda (contract request 127), claude-code on the Lightpanda image."""
    family = sorted(
        name
        for name, profile in RUNNER_PROFILES.items()
        if profile.runner_argv == CLAUDE_CODE_RUNNER and profile.available
    )
    assert family == [
        "claude-code", "claude-code-browser", "claude-code-large", "claude-code-lightpanda", "indexer",
    ]
    for name in family:
        assert RUNNER_PROFILES[name].model_classes == ALL_CLASSES, name
        assert RUNNER_PROFILES[name].default_model_class == "opus", name


def test_the_retired_review_profile_keeps_only_the_pin_it_has():
    review = RUNNER_PROFILES["claude-code-review"]
    assert review.model_classes == ("opus",)
    assert review.default_model_class == "opus"


def test_a_profile_that_runs_no_claude_model_has_no_classes():
    classed = {
        "claude-code", "claude-code-browser", "claude-code-large", "claude-code-lightpanda", "indexer",
        "claude-code-review",
    }
    unclassed = sorted(set(RUNNER_PROFILES) - classed)
    # The control: the profiles this statement is about exist.
    assert {"codex", "mock", "generic", "browser", "merge", "post-verdict"} <= set(unclassed)
    for name in unclassed:
        assert RUNNER_PROFILES[name].model_classes == (), name
        assert RUNNER_PROFILES[name].default_model_class is None, name


def test_classes_on_a_worker_action_profile_are_refused():
    merge = RUNNER_PROFILES["merge"]
    assert merge.worker_action is WorkerAction.MERGE
    with pytest.raises(ValueError, match="worker_action"):
        replace(merge, provider="anthropic", model_classes=("opus",), default_model_class="opus")


def test_classes_on_a_provider_other_than_anthropic_are_refused():
    with pytest.raises(ValueError, match="anthropic"):
        replace(RUNNER_PROFILES["codex"], model_classes=("opus",), default_model_class="opus")
    with pytest.raises(ValueError, match="anthropic"):
        replace(RUNNER_PROFILES["mock"], model_classes=("opus",), default_model_class="opus")


def test_a_default_outside_the_profiles_own_classes_is_refused():
    with pytest.raises(ValueError, match="default_model_class"):
        replace(RUNNER_PROFILES["claude-code"], model_classes=("opus", "fable"), default_model_class="haiku")


def test_classes_without_a_default_and_a_default_without_classes_are_refused():
    with pytest.raises(ValueError, match="default_model_class"):
        replace(RUNNER_PROFILES["claude-code"], default_model_class=None)
    with pytest.raises(ValueError, match="default_model_class"):
        replace(RUNNER_PROFILES["claude-code"], model_classes=(), default_model_class="opus")


def test_a_class_name_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError, match="MODEL_CLASSES"):
        replace(RUNNER_PROFILES["claude-code"], model_classes=("opus", "gpt"), default_model_class="opus")
    with pytest.raises(ValueError, match="twice"):
        replace(RUNNER_PROFILES["claude-code"], model_classes=("opus", "opus"), default_model_class="opus")


def test_step_model_class_resolves_by_one_rule():
    claude_code = RUNNER_PROFILES["claude-code"]
    assert profiles.step_model_class(claude_code, None) == "opus"
    for name in ALL_CLASSES:
        assert profiles.step_model_class(claude_code, name) == name
    assert profiles.step_model_class(RUNNER_PROFILES["codex"], None) is None
    assert profiles.step_model_class(RUNNER_PROFILES["mock"], None) is None


def test_step_model_class_refuses_a_class_the_profile_does_not_accept():
    with pytest.raises(ValueError, match="opus") as refused:
        profiles.step_model_class(RUNNER_PROFILES["claude-code-review"], "haiku")
    assert "haiku" in str(refused.value)
    with pytest.raises(ValueError, match="no model class"):
        profiles.step_model_class(RUNNER_PROFILES["codex"], "opus")
    with pytest.raises(ValueError, match="MODEL_CLASSES"):
        profiles.step_model_class(RUNNER_PROFILES["claude-code"], "gpt")


# ---------------------------------------------------------------------------
# Requests C and D: the fields
# ---------------------------------------------------------------------------


def _task(**over: object) -> Task:
    return Task(
        id="task_0123456789abcdef",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.QUEUED,
        runner_profile="claude-code",
        resource_class="standard",
        input={"prompt": "summarise the source"},
        submitted_by="alice@example.com",
        **over,  # type: ignore[arg-type]
    )


def test_a_task_names_no_class_by_default():
    task = _task()
    assert task.model_class is None
    assert task.to_firestore()["model_class"] is None


def test_a_tasks_class_reaches_its_stored_document():
    stored = _task(model_class="sonnet").to_firestore()
    assert stored["model_class"] == "sonnet"
    # The model ID stays its own field: a class is a name, never an ID.
    assert stored["model"] is None


def test_a_workflow_step_names_no_class_by_default_and_keeps_one_it_names():
    assert WorkflowStep(step_id="s", runner_profile="claude-code", input={}).model_class is None
    step = WorkflowStep(step_id="s", runner_profile="claude-code", input={}, model_class="fable")
    assert asdict(step)["model_class"] == "fable"


def test_an_attempt_records_no_model_until_its_worker_writes_one():
    attempt = Attempt(
        attempt_id="att_1",
        task_id="task_1",
        tenant_id="eng",
        generation=1,
        lease_id="lease_1",
        backend="GKE_AUTOPILOT",
        created_at=NOW,
    )
    assert attempt.model is None
    assert asdict(replace(attempt, model="claude-sonnet-5-5"))["model"] == "claude-sonnet-5-5"


def test_the_new_fields_come_last_so_no_positional_construction_moves():
    """Every field before them keeps its position."""
    # Contract request 120 (#2459) appends `budget` after `model_class`.
    # Contract request 123 (#1820) appends `platform_refusals` after `budget`.
    # MUTATION: inserting it before `budget` moves budget's position and
    # turns this red.
    assert [f.name for f in fields(Task)][-3:] == ["model_class", "budget", "platform_refusals"]
    assert [f.name for f in fields(WorkflowStep)][-2:] == ["model_class", "budget"]
    # RS-4 (contract request 111) appends the sized_* record after `model`;
    # `model` keeps its position and only RS-4's six fields follow it.
    # MUTATION: inserting an RS-4 field before `model`, or any other field
    # after it, turns this red.
    attempt_names = [f.name for f in fields(Attempt)]
    assert attempt_names[attempt_names.index("model") :] == [
        "model",
        "size_version",
        "sized_cpu_request",
        "sized_cpu_limit",
        "sized_memory_gib",
        "sized_disk_gib",
        "sized_live_split",
    ]
    names = [f.name for f in fields(profiles.RunnerProfile)]
    assert names[-2:] == ["model_classes", "default_model_class"]
