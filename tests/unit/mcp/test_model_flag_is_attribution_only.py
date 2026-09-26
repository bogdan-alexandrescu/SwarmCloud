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
from a caller." Changing it is an owner decision and belongs in
docs/contract-change-requests.md, not in a patch. The fix available here is to
stop the CLI claiming otherwise, and these tests hold both halves in place: the
help text says what the flag does, and the transport still does it.

`test_worker_environment_carries_no_model_for_a_task_that_names_one` is
therefore a DELIBERATE brake. If it ever fails because MODEL was wired into
`worker_env`, that is the contract change -- go and get the decision, and
update the help text in the same commit.
"""

from __future__ import annotations

import argparse
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

def _task_with_model() -> Task:
    profile = RUNNER_PROFILES["claude-code"]
    return Task(
        id="task_abc123",
        tenant_id="eng",
        created_at=NOW,
        updated_at=NOW,
        state=TaskState.LEASED,
        runner_profile="claude-code",
        resource_class=profile.resource_class,
        input={},
        submitted_by="alice@saga.xyz",
        provider=profile.provider,
        model=MODEL,
        timeout_seconds=1800,
    )


def test_worker_environment_carries_no_model_for_a_task_that_names_one() -> None:
    from control_plane.conftest import PROJECT, scheduler_settings
    from scheduler.dispatch import worker_env

    task = _task_with_model()
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

    env = worker_env(task=task, lease=lease, tenant=tenant, settings=scheduler_settings())

    assert task.model == MODEL, "the task really does name a model"
    assert "MODEL" not in env, (
        "a caller-chosen model in the execution environment is an execution "
        "parameter from a caller -- CONTRACT.md invariant 10. If this is meant "
        "to change, raise it in docs/contract-change-requests.md and update the "
        "`--model` help text in the same commit"
    )
    assert MODEL not in env.values(), "not under another name either"
