"""`swarm dispatch --model` records a name. It does not choose one.

THE DEFECT THIS PINS. The flag looked like model selection and was not. The
seam was built at both ends and never executed end to end:

  * `cli.cmd_dispatch` passes `--model` to `SwarmClient.dispatch`;
  * `SwarmClient.dispatch` puts it at the TOP LEVEL of the task payload;
  * `TaskCreate.model` accepts it and `service.create_task` stores it on the
    Task, where `codec.task_to_api` faithfully echoes it back -- so it
    round-trips, which is what made an earlier reading of this call it fine;
  * and then it stops. `scheduler.dispatch.worker_env` builds the worker's
    entire environment and carries identifiers and endpoints ONLY, so no MODEL
    derived from a task ever reaches a container. The worker's `cfg.model`
    comes from the Job definition's own MODEL, and `runners/cliagent.py` reads
    that environment and nothing else -- never `task.model`, and since #226
    never `input.model` either, which the API refuses (#213).

So the flag changed what the record SAID and never what ran.

WHY THIS IS NOT FIXED BY WIRING IT THROUGH. A model chosen per task by the
caller is an execution parameter supplied by the caller, and CONTRACT.md
invariant 10 exists to forbid exactly that: "API callers pick a runner_profile
by name. Never accept images, commands, resource specs or backend parameters
from a caller." The fix available here was to stop the CLI claiming otherwise,
and the help-text half below still holds that: `--model` records, and does
not select.

THE BRAKE, INVERTED ON PURPOSE (#1435, lane MODEL-SCHED). The worker-env
half was a deliberate brake: "If it ever fails because MODEL was wired into
`worker_env`, that is the contract change -- go and get the decision." The
decision was got. The owner's answer to Q2 on 2026-10-17 (PR 1509 and PR
1460; docs/design/model-per-step.md §4.5) is design B: a step names a model
CLASS from the frozen `MODEL_CLASSES`, the profile lists the classes it
accepts, and the platform pins each class's ID in Terraform. So `worker_env`
now carries `MODEL`, and the test asserts that it does, and that its value is
the scheduler's OWN `WORKER_CLASS_MODELS` entry for the step's resolved
class. Invariant 10 still holds, because the caller names a class and never
an ID, and what a caller could have used to name an ID still cannot reach
the environment. Each of these is asserted below:

  * `Task.model`, any string: the class's pin runs and the string is nowhere;
  * `input.model` or any `input` key, and any `metadata` key: none becomes
    `MODEL` or any other variable;
  * a model ID in `model_class`: not a class, so the dispatch raises;
  * a class the profile does not accept: raises, never defaulted;
  * anything for a classless profile: no `MODEL`.
"""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.models import Lease, Task, Tenant
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TaskState

from swarm_mcp.cli import build_parser

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
MODEL = "claude-opus-5"


# -- what the flag tells a person it does ---------------------------------

def _dispatch_model_help() -> str:
    """The `--model` action's own help string, read off the parser.

    Off the ACTION, not out of the rendered page: `--model` also appears in the
    usage line, and matching there passed while the flag carried no help at all.
    """
    parser = build_parser()
    subparsers = [
        action
        for action in parser._actions  # noqa: SLF001 -- argparse exposes no public walk
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    assert subparsers, "the parser no longer has subcommands"
    dispatch = subparsers[0].choices["dispatch"]
    for action in dispatch._actions:  # noqa: SLF001
        if "--model" in action.option_strings:
            return action.help or ""
    raise AssertionError("`swarm dispatch` no longer has a --model flag")


def test_the_dispatch_help_says_model_does_not_select_the_model() -> None:
    help_text = _dispatch_model_help()
    lowered = " ".join(help_text.lower().split())

    assert lowered, "the flag must carry help; a bare `--model` is the lie itself"
    assert "attribution" in lowered, (
        "the help must say the flag is recorded for attribution: " + help_text
    )
    assert "not select" in lowered, (
        "the help must say the flag does NOT select the model the agent runs: "
        + help_text
    )


# -- what the flag actually does on the wire ------------------------------

class _RecordingClient:
    """The transport, stubbed at the one method `dispatch` calls."""

    def __init__(self) -> None:
        self.payload: dict | None = None

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        assert (method, path) == ("POST", "/v1/tasks")
        self.payload = payload
        return {"task": {"id": "task_1"}}


def test_model_travels_as_a_top_level_field_and_never_as_runner_input() -> None:
    from swarm_mcp.client import SwarmClient

    recorder = _RecordingClient()
    SwarmClient.dispatch(
        recorder,  # type: ignore[arg-type]
        prompt="do the thing",
        model=MODEL,
    )

    assert recorder.payload is not None
    assert recorder.payload["model"] == MODEL, "it is sent, and stored, for attribution"
    assert "model" not in recorder.payload["input"], (
        "input.model is refused by the API (claude-code declares no input, "
        "#213), and until #226 the runner read it: putting it there would make "
        "the flag either fail every dispatch or select a model, which is the "
        "contract change this test exists to stop happening by accident"
    )


# -- why it cannot select anything ----------------------------------------

#: terraform/infra/locals.tf `runner_models`, as WORKER_CLASS_MODELS carries
#: it to the scheduler. Test input: the assertions compare the environment to
#: this map, through the settings the scheduler reads it into.
CLASS_MODELS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


def _task(profile_name: str = "claude-code", **fields) -> Task:
    profile = RUNNER_PROFILES[profile_name]
    base = dict(
        id="task_abc123",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile=profile_name,
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        model=MODEL,
        timeout_seconds=1800,
    )
    base.update(fields)
    return Task(**base)


def _env(task: Task, *, class_models: dict | None = None) -> dict[str, str]:
    from control_plane.conftest import PROJECT, scheduler_settings
    from scheduler.dispatch import worker_env

    tenant = Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )
    lease = Lease(
        lease_id="lease_1",
        task_id=task.id,
        attempt_id="att_1",
        tenant_id=task.tenant_id,
        generation=3,
        pools=["global"],
        units=1,
        state=TaskState.LEASED,
        created_at=NOW,
        dispatch_deadline=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=2),
    )
    settings = scheduler_settings(
        worker_class_models=dict(CLASS_MODELS if class_models is None else class_models)
    )
    return worker_env(task=task, lease=lease, tenant=tenant, settings=settings)


def test_worker_environment_carries_the_class_pin_and_not_the_task_s_model() -> None:
    """The inverted brake: MODEL is the scheduler's pin for the step's class."""
    task = _task(model_class="sonnet")
    env = _env(task)

    assert task.model == MODEL, "the task really does name a model"
    assert env["MODEL"] == CLASS_MODELS["sonnet"]
    assert MODEL not in env.values(), "a task's own `model` is attribution, never MODEL"


def test_a_step_naming_no_class_carries_its_profile_s_default_pin() -> None:
    env = _env(_task())
    assert env["MODEL"] == CLASS_MODELS["opus"]
    assert MODEL not in env.values()


def test_a_stored_model_written_past_the_api_selects_nothing() -> None:
    """Any string in `Task.model` -- here another class's real ID."""
    for stored in (CLASS_MODELS["haiku"], "gpt-" + secrets.token_hex(4), ""):
        env = _env(_task(model=stored or None))
        assert env["MODEL"] == CLASS_MODELS["opus"], stored
        if stored:
            assert stored not in env.values(), stored


def test_no_input_or_metadata_key_reaches_the_environment() -> None:
    """`input.model` (the API refuses it, #213; this is a document written past
    the API) and any other key: no MODEL from them, and no new variable."""
    marker = "claude-haiku-5-" + str(10 + secrets.randbelow(80))
    smuggled = {"model": marker, "MODEL": marker, "model_class": "haiku", "x": marker}
    plain = _env(_task())
    with_keys = _env(_task(input=dict(smuggled), metadata=dict(smuggled)))

    assert with_keys == plain
    assert marker not in with_keys.values()


def test_a_model_id_named_as_a_class_raises() -> None:
    from scheduler.dispatch import DispatchError

    with pytest.raises(DispatchError) as raised:
        _env(_task(model_class=CLASS_MODELS["opus"]))
    assert raised.value.code == "model_not_pinned"


def test_a_class_the_profile_does_not_accept_raises_and_is_not_defaulted() -> None:
    """claude-code-review accepts `opus` only."""
    from scheduler.dispatch import DispatchError

    with pytest.raises(DispatchError) as raised:
        _env(_task("claude-code-review", model_class="haiku"))
    assert raised.value.code == "model_not_pinned"


def test_a_classless_profile_gets_no_model() -> None:
    for name, profile in sorted(RUNNER_PROFILES.items()):
        if profile.model_classes or profile.worker_action:
            continue
        env = _env(_task(name))
        assert "MODEL" not in env, name
        assert MODEL not in env.values(), name
