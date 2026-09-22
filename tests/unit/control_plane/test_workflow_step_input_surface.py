"""The New Workflow screen's per-step `input` and `input_from`, and the seam under them.

THE DEFECT THIS FILE PINS. `SubmitWorkflow.tsx` built each step as exactly
`step_id`, `runner_profile` and `depends_on`. `WorkflowStepCreate.input` is
`Field(default_factory=dict)`, so every step arrived with `input == {}`, and
`run_cli_agent` raises "<profile> requires a non-empty string input.prompt" the
moment it starts. Every workflow that screen ever submitted therefore failed at
every agent step -- not intermittently, deterministically, for any profile
backed by the CLI agent runner -- and it failed AFTER admission, after a
container start and after the tenant's credential had been mounted, so each
dead step cost a slot and a wait before saying so.

Nothing in the platform stops this. The API is right to accept `input == {}`:
`input` is opaque to it, a `generic` step may legitimately carry none, and a
control plane that second-guessed a runner's payload would be the invariant-10
violation in reverse. So the check belongs where the caller still has the
keyboard, and the fact it checks against has to REACH that caller as data --
hence `swarm_api/runnerinputs.py` and the `input_contract` block on
/v1/capacity.

Four seams, one section each:

  1. THE REQUEST BODY. What the form actually builds, asserted in its source and
     then posted at the real API so the shape is known to survive the round
     trip -- including `input_from` reaching `metadata.input_from` as task ids.

  2. THE REFUSAL. The form must stop, not submit. Proven together with the
     measurement that makes it necessary: the API really does accept a step the
     runner will really reject, so "the platform would have caught it" is false.

  3. `input_from` OFFERED ONLY FOR DEPENDENCIES, which is `validate_dag`'s rule
     and not a preference.

  4. THE CATALOGUE BINDING. `runnerinputs.py` restates a worker rule, because
     swarm-api cannot import `agent_worker` -- images/swarm-api/Dockerfile ships
     `apps/common/` and `apps/swarm-api/` and nothing else. A restatement with
     nothing asserting it is a copy waiting to drift, so this section reads the
     worker's own source for the call AND runs `run_cli_agent` against an empty
     payload, which is the only thing that proves the rule is real rather than
     merely written down.

No network, no credentials, no node: the TypeScript is read as text, the same
technique `test_dispatch_ui_surface.py` uses and for the same reason -- a mock
of the UI would agree with whatever the UI happens to do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.runnerinputs import (
    REQUIRED_INPUT_KEYS_BY_MODULE,
    input_contract,
    required_input_keys,
    runner_module,
)

from .conftest import auth_header

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"
RUNNERS = ROOT / "apps/agent-worker/agent_worker/runners"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing UI seam tests: this suite has to pass in a tree that holds only the
#: Python services.
needs_ui = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")


def _src(name: str) -> str:
    return (UI / name).read_text()


def _send_body() -> str:
    """The body of `SubmitWorkflow.tsx`'s `send`, where the request is built.

    Sliced out rather than searched whole-file so a mention of `input` in a
    comment elsewhere in the screen cannot satisfy an assertion about what the
    request carries.
    """
    source = _src("SubmitWorkflow.tsx")
    start = source.index("const send = () => {")
    end = source.index("\n  return (", start)
    return source[start:end]


# --------------------------------------------------------------------------
# 1. The request-body seam
# --------------------------------------------------------------------------

@needs_ui
def test_the_workflow_form_sends_an_input_for_every_step():
    """THE REGRESSION TEST FOR THE BLOCKER.

    `input` is not optional-in-practice here. Omitting it is what produced a
    workflow whose every agent step was dispatched and then refused, so the key
    has to be in the object the form builds -- and it has to be built from the
    step's own parsed input, not from a constant.
    """
    send = _send_body()
    assert "input: parsed.input" in send, (
        "SubmitWorkflow.tsx does not put `input` in the step body. Every step "
        "then arrives with input == {} (WorkflowStepCreate.input is "
        "Field(default_factory=dict)) and run_cli_agent refuses it with "
        "'requires a non-empty string input.prompt'."
    )


@needs_ui
def test_the_workflow_form_sends_input_from_when_a_filename_was_given():
    """The other half: the only way one step's artifact reaches the next.

    Omitted when empty and sent when not, so `{}` is never posted as though it
    were a choice -- but the key must exist in the builder at all, which it did
    not before.
    """
    send = _send_body()
    assert "input_from: staged" in send, (
        "SubmitWorkflow.tsx does not put `input_from` in the step body, so no "
        "workflow built on this screen can stage an upstream artifact."
    )
    assert "stagedArtifacts(s, dependsOn)" in send, (
        "`input_from` must be built from the FILTERED depends_on, so a source "
        "that is not a dependency cannot be sent"
    )


@needs_ui
def test_the_workflow_form_still_sends_the_dispatch_choice():
    """The keys that were already right stay right.

    This screen's body builder was rewritten to add `input`; a rewrite that
    dropped `strategy` would trade one silent default for another.
    """
    send = _send_body()
    for key in ("strategy: dispatch.strategy", "carrier: dispatch.carrier"):
        assert key in send, f"SubmitWorkflow.tsx no longer sends `{key.split(':')[0]}`"
    assert "repo === '' ? {} : { repository_url: repo }" in send


def test_the_workflow_body_the_form_now_builds_is_accepted(client):
    """The exact shape, posted at the real API.

    `WorkflowCreate` is `extra="forbid"`, so a key the form invents is a 422
    naming it. This is the body `send` builds for two steps where the second
    stages a file from the first.
    """
    body = {
        "steps": [
            {
                "step_id": "research",
                "runner_profile": "mock",
                "input": {"prompt": "read the destroy guard and summarise it"},
                "depends_on": [],
            },
            {
                "step_id": "report",
                "runner_profile": "mock",
                "input": {"prompt": "turn the summary into a report"},
                "depends_on": ["research"],
                "input_from": {"research": "summary.md"},
            },
        ],
        "strategy": "collect",
        "carrier": "checkpoints",
    }
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    workflow = response.json()["workflow"]

    steps = {s["step_id"]: s for s in workflow["steps"]}
    assert steps["research"]["input"] == {"prompt": "read the destroy guard and summarise it"}
    assert steps["report"]["input"] == {"prompt": "turn the summary into a report"}
    assert steps["report"]["input_from"] == {"research": "summary.md"}


def test_the_prompt_reaches_the_task_the_worker_will_actually_run(client, db):
    """Not the workflow document -- the TASK, which is what a worker reads.

    `create_workflow` fans the steps out into tasks, and `input` has to survive
    that fan-out. A prompt stored only on the workflow is a prompt the runner
    never sees.
    """
    body = {
        "steps": [
            {
                "step_id": "solo",
                "runner_profile": "mock",
                "input": {"prompt": "the prompt the agent is handed"},
                "depends_on": [],
            }
        ],
    }
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    task_id = response.json()["workflow"]["steps"][0]["task_id"]
    assert task_id, "the step was created without a task id"

    task = client.get(f"/v1/tasks/{task_id}", headers=auth_header("alice"))
    assert task.status_code == 200, task.text
    assert task.json()["task"]["input"] == {"prompt": "the prompt the agent is handed"}


def test_input_from_becomes_metadata_input_from_keyed_by_task_id(client):
    """The translation the worker depends on, measured rather than assumed.

    The form names an upstream STEP; `agent_worker/inputs.py` reads
    `metadata.input_from` keyed by upstream TASK id. If that translation did not
    happen the staging control would be a field that quietly does nothing.
    """
    body = {
        "steps": [
            {"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build"}, "depends_on": []},
            {
                "step_id": "test",
                "runner_profile": "mock",
                "input": {"prompt": "test"},
                "depends_on": ["build"],
                "input_from": {"build": "artifact.tar"},
            },
        ],
    }
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    steps = {s["step_id"]: s for s in response.json()["workflow"]["steps"]}

    test_task = client.get(f"/v1/tasks/{steps['test']['task_id']}", headers=auth_header("alice"))
    assert test_task.status_code == 200, test_task.text
    assert test_task.json()["task"]["metadata"]["input_from"] == {
        steps["build"]["task_id"]: "artifact.tar"
    }


# --------------------------------------------------------------------------
# 2. The refusal seam
# --------------------------------------------------------------------------

def test_the_api_really_does_accept_a_step_the_runner_will_really_refuse(client):
    """WHY THE CHECK HAS TO BE IN THE FORM. Measured, not assumed.

    If the API refused this, the browser would need no rule of its own. It does
    not refuse it, and it is right not to: `input` is opaque to the control
    plane. So an empty input on a `claude-code` step is a 201 followed by a
    dispatched, credentialed, doomed attempt -- which is the cost the form's
    refusal avoids.
    """
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": [{"step_id": "doomed", "runner_profile": "claude-code", "depends_on": []}]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["workflow"]["steps"][0]["input"] == {}


@needs_ui
def test_the_form_refuses_rather_than_submitting_a_step_with_no_required_key():
    """The refusal must come BEFORE the request, not after it.

    A form that posted first and explained afterwards would have created the
    workflow it was trying to prevent. The `not_sent` outcome is therefore
    asserted to be reachable strictly earlier in `send` than `postWorkflow`.
    """
    send = _send_body()
    assert "missingInputKeys(parsed.input, required)" in send, (
        "SubmitWorkflow.tsx does not check the step's input against the keys its "
        "runner requires"
    )
    refused = send.index("kind: 'not_sent'")
    posted = send.index("void postWorkflow(")
    assert refused < posted, (
        "the refusal is decided after the request is sent; nothing was prevented"
    )
    guard = send[refused:posted]
    assert "return" in guard, (
        "`send` records the problems and then posts anyway -- the refusal must "
        "return before the request is built"
    )


@needs_ui
def test_the_form_treats_an_unread_contract_as_unread_and_not_as_permission():
    """An API too old to send `input_contract` must not read as "nothing required".

    That is this repository's standing rule for an absent measurement, and the
    remedies differ: an empty list is a profile that needs nothing, a null is a
    rule nobody could read. Inventing a requirement would refuse valid
    workflows; assuming none silently restores the blocker.
    """
    types = _src("types.ts")
    assert "export function requiredInputKeys" in types, (
        "types.ts has no requiredInputKeys, so the absent case is decided ad hoc "
        "wherever it is needed"
    )
    contract = re.search(r"export function requiredInputKeys.*?\n}", types, re.S)
    assert contract is not None
    assert "return null" in contract.group(0), (
        "requiredInputKeys must return null for an API that did not say, not []"
    )
    send = _send_body()
    assert "required === null ? [] : missingInputKeys" in send, (
        "`send` must apply NO rule when the contract is unread, rather than "
        "inventing one"
    )


@needs_ui
def test_the_form_refuses_a_blank_string_as_loudly_as_a_missing_key():
    """`{"prompt": ""}` fails in exactly the same place `{}` does.

    `run_cli_agent` tests `not prompt.strip()`, so a check for mere presence
    would pass the typo straight through to the identical failure.
    """
    source = _src("SubmitWorkflow.tsx")
    missing = re.search(r"function missingInputKeys\(.*?\n}", source, re.S)
    assert missing is not None, "SubmitWorkflow.tsx has no missingInputKeys"
    body = missing.group(0)
    assert "typeof value !== 'string'" in body, "a non-string value must count as missing"
    assert "value.trim() === ''" in body, (
        "a blank string must count as missing: run_cli_agent refuses it too"
    )


# --------------------------------------------------------------------------
# 3. `input_from` is offered only for dependencies
# --------------------------------------------------------------------------

def test_the_api_refuses_an_input_from_that_is_not_a_dependency(client):
    """The rule the control is shaped around, measured at the API.

    `validate_dag`: "an artifact cannot be staged from a step that may not have
    run yet". The form offers a filename box ONLY for a checked dependency, so
    this 422 is unreachable from the screen rather than merely explained on it.
    """
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "a", "runner_profile": "mock", "input": {}, "depends_on": []},
                {
                    "step_id": "b",
                    "runner_profile": "mock",
                    "input": {},
                    "depends_on": [],
                    "input_from": {"a": "out.txt"},
                },
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert "does not depend on it" in response.text


@needs_ui
def test_the_staging_control_offers_only_the_steps_this_step_depends_on():
    """Offering a non-dependency would offer a 422, and describe a broken workflow."""
    source = _src("SubmitWorkflow.tsx")
    assert "const stageable = step.dependsOn.filter((d) => others.includes(d))" in source, (
        "the staging control is not narrowed to this step's dependencies"
    )
    staged = re.search(r"function stagedArtifacts\(.*?\n}", source, re.S)
    assert staged is not None, "SubmitWorkflow.tsx has no stagedArtifacts"
    assert "for (const source of dependsOn)" in staged.group(0), (
        "input_from must be built by walking depends_on, so a stale filename "
        "cannot be sent for a dependency that was unchecked"
    )


# --------------------------------------------------------------------------
# 4. The catalogue binding
# --------------------------------------------------------------------------

def test_every_profile_in_the_catalogue_gets_an_input_contract(client, db):
    """Served for ALL of them, including the ones that require nothing.

    Serving it only where there is a requirement would make "this API is too old
    to say" and "this profile needs nothing" the same absence, and a form cannot
    tell a missing rule from a satisfied one.
    """
    response = client.get("/v1/capacity", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    profiles = response.json()["runner_profiles"]
    assert set(profiles) == set(RUNNER_PROFILES)
    for name, block in profiles.items():
        assert "input_contract" in block, f"{name} carries no input_contract"
        assert isinstance(block["input_contract"]["required_keys"], list)
    assert profiles["claude-code"]["input_contract"]["required_keys"] == ["prompt"]
    assert profiles["mock"]["input_contract"]["required_keys"] == []


def test_the_contract_is_keyed_off_the_frozen_catalogue_and_not_a_name_list():
    """A new profile pointed at an existing runner must inherit its requirement.

    `runner_module` reads `RunnerProfile.command`, so the profile->module half of
    the answer comes from the frozen catalogue itself. A table of profile NAMES
    would have to be revisited whenever the catalogue grew one, and that revisit
    is the step that gets missed.
    """
    for name, profile in RUNNER_PROFILES.items():
        assert runner_module(profile) == profile.command[-1], name
        assert profile.command[:2] == ("python", "-m"), (
            f"{name} is not started as `python -m <module>`; runner_module would "
            "return None and its requirements would silently become empty"
        )
    assert set(REQUIRED_INPUT_KEYS_BY_MODULE) <= {
        runner_module(p) for p in RUNNER_PROFILES.values()
    }, "runnerinputs names a runner module no profile in the catalogue runs"


def test_the_api_table_matches_the_runners_that_actually_call_run_cli_agent():
    """THE PARITY ASSERTION. swarm-api restates a worker rule; this is the check.

    `run_cli_agent` is the only place the prompt requirement lives, so the set of
    catalogue profiles declared to require `prompt` must be exactly the set whose
    runner module calls it. Read from the worker's source because swarm-api
    cannot import `agent_worker` -- images/swarm-api/Dockerfile copies
    `apps/common/` and `apps/swarm-api/` and nothing else, so the import would
    work here and be an ImportError in production.
    """
    calls_cli_agent = set()
    for name, profile in RUNNER_PROFILES.items():
        module = runner_module(profile)
        assert module is not None, name
        path = RUNNERS / f"{module.rsplit('.', 1)[1]}.py"
        assert path.is_file(), f"{name} names a runner module that is not in {RUNNERS}"
        if re.search(r"\brun_cli_agent\(", path.read_text()):
            calls_cli_agent.add(name)

    declared = {n for n, p in RUNNER_PROFILES.items() if "prompt" in required_input_keys(p)}
    assert declared == calls_cli_agent, (
        "swarm_api/runnerinputs.py has drifted from the runners: it declares "
        f"{sorted(declared)} as requiring a prompt, and the runners that call "
        f"run_cli_agent are {sorted(calls_cli_agent)}"
    )
    assert calls_cli_agent, "no runner calls run_cli_agent; the scan found nothing to compare"


def test_run_cli_agent_really_refuses_an_empty_prompt(tmp_path):
    """The rule itself, executed -- not read, not mocked.

    Everything above is bookkeeping if this sentence is not true. The refusal is
    raised before the credential lookup and before the binary lookup, which is
    why it can be exercised offline with nothing installed.
    """
    from agent_worker.runners.base import RunnerContext, RunnerFailure
    from agent_worker.runners.claude_code import body as claude_code_body

    def ctx(payload: dict[str, object]) -> RunnerContext:
        return RunnerContext(
            work_dir=tmp_path,
            artifacts_dir=tmp_path / "artifacts",
            input_path=tmp_path / "input.json",
            result_path=tmp_path / "result.json",
            quota_path=tmp_path / "quota.json",
            payload=payload,
        )

    for payload in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}):
        with pytest.raises(RunnerFailure) as raised:
            claude_code_body(ctx(dict(payload)))
        assert "input.prompt" in str(raised.value), payload

    # And the contract the API serves is the one that would have prevented it.
    assert input_contract(RUNNER_PROFILES["claude-code"]) == {"required_keys": ["prompt"]}


def test_a_profile_whose_runner_reads_its_input_defensively_requires_nothing():
    """The other direction, which is the one that would annoy people.

    `mock` falls back to "no prompt supplied", `generic` takes a command and
    `browser` takes a url or actions. Declaring a prompt for any of them would
    make the form refuse workflows that run perfectly well.
    """
    for name in ("mock", "generic", "browser"):
        assert required_input_keys(RUNNER_PROFILES[name]) == (), (
            f"{name} is declared as requiring input keys its runner does not demand"
        )
