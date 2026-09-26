"""Workflows, reachable from a Claude Code session.

WHY THIS FILE EXISTS. `server.py` contained the string "workflow" zero times, so
fan-out, `depends_on`, `input_from` artifact staging, the rollup and
`on_step_failure` -- every multi-step feature this platform was built for --
could not be reached from the place a developer actually works. A session could
dispatch N independent agents and join them by hand, which is precisely the
bookkeeping the platform exists to remove.

THE SERVER SIDE IS PROVEN, so this is wiring rather than invention.
`wf_5e5ad3b6f7da4299a839`, submitted 2026-09-22: five independent `claude-code`
steps and a sixth joining all five through `input_from`. All six SUCCEEDED. The
five parallel steps each waited 153s -- admitted in ONE all-or-nothing
transaction, invariant 2 -- and ran 68-92s; the join waited 431s, ran 38s, and
its attempt recorded `cache_read_input_tokens=109629`, which is the proof that
the five staged artifacts really did reach the agent's context.

THE ONE RULE THIS MODULE ADDS, and the reason it is a module rather than four
more branches in `server._call`: a workflow has TWO states and only one of them
is true. `Workflow.state` in Firestore is a cache written once at submission;
`swarm_api.rollup` derives the real one from the step tasks and
`swarm_api.codec.workflow_to_api` serves THAT as `state`, with
`state_source: "derived"` and the cache as `stored_state` beside it. Before that
landed, `wf_bcdc9180e4fb4a209f31` read QUEUED while its three steps were
SUCCEEDED, FAILED and CANCELLED.

THIS PACKAGE DOES NOT DERIVE, and that refusal is deliberate. The derivation is a
precedence table over the frozen state vocabulary and it lives in
`swarm_api.rollup`, which ships inside the API image; `apps/swarm-mcp` depends on
`swarm-common` and on nothing else, on purpose, so it cannot import it -- and
copying the table here would be the restatement this repository keeps deleting.
Worse than usual in this case: the thing the copy would be compared against is
the drift check itself, so the two copies drifting would be reported as the DATA
drifting.

So when the server says it derived, the bridge serves what the server derived.
When the server does not say so, the bridge reports NO workflow state and says
why, naming the route that has to be fixed. It never falls back to
`stored_state`. A bridge that did would print QUEUED into a developer's terminal
directly above six steps that had all finished -- the same contradiction the web
UI was written up for, in a worse place, because a terminal has no second panel
to disagree with it.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import profiles as catalogue
from .client import SwarmClient, SwarmError
from .follow import follow_command

#: What `codec.workflow_to_api` sets on a read that actually looked at the step
#: tasks. Any other value -- including the absence of the field -- means the
#: `state` beside it is the Firestore cache.
DERIVED = "derived"

#: `swarm_api.rollup.UNKNOWN`. Recognised here, never produced here. It is
#: deliberately not a member of the frozen state vocabulary, so it can neither be
#: written to Firestore nor mistaken for a state the platform can be in.
UNKNOWN = "UNKNOWN"

#: The keys one step object may carry. An unknown key is refused rather than
#: dropped: a caller that passed `input` or `image` believing it would be
#: honoured needs to be told it was not, and silently ignoring a key is how
#: invariant 10 would get quietly tested.
#:
#: `inputs` is the runner's DECLARED inputs (#142) -- `sleep_seconds` for the
#: mock, say -- checked per profile by `profiles.check_inputs`. It is not the
#: API's raw `input` object, which stays refused: that would let a caller set
#: any key a runner reads. (`input.model` was one, until #226 made the model the
#: Job's `MODEL`.)
_STEP_KEYS = frozenset(
    {
        "step_id",
        "prompt",
        "runner_profile",
        "inputs",
        "depends_on",
        "input_from",
        "resource_class",
        "timeout_seconds",
        "stage",
    }
)

#: A step key that is READ HERE AND NEVER SENT. `stage` names the group a step
#: is shown under in Claude Code's `/workflows` when the spec is run with
#: `/sc:run`; the platform has no such field (`WorkflowStepCreate` forbids
#: extras) and nothing executes from it. Accepted rather than refused so one
#: spec file serves `/sc:run` and `swarm workflow` alike -- and named here, so
#: it is the one key this module drops knowingly.
DISPLAY_ONLY_STEP_KEYS = frozenset({"stage"})

#: The top-level keys a `swarm workflow` spec may carry -- the file the
#: terminal command reads and the object `swarm_workflow`'s `spec` takes. One
#: reader for both (`read_spec`), so the two cannot come to disagree about what
#: a spec is. An unknown key is refused, as a step's is: a spec carrying
#: `image` or `model` at the top must be told it was not honoured.
SPEC_KEYS = frozenset(
    {
        "steps",
        "strategy",
        "carrier",
        "repository_url",
        "repository_ref",
        "on_step_failure",
        "priority",
        "label",
    }
)

DEFAULT_PROFILE = "claude-code"

#: What a workflow's `strategy` and `carrier` may be, for `swarm workflow
#: --strategy/--carrier` to offer as choices. They are the API's
#: (`swarm_api.validation.DISPATCH_STRATEGIES` / `DISPATCH_CARRIERS`), which
#: this package cannot import -- it depends on swarm-common alone -- so this is
#: a copy, and a copy is only safe while something compares it:
#: `test_the_workflow_choices_are_the_apis` fails the moment they differ.
STRATEGIES = ("collect", "direct-pr", "integrate")
CARRIERS = ("checkpoints", "branches")


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------

def build_steps(raw_steps: Any) -> list[dict[str, Any]]:
    """`WorkflowStepCreate` bodies from the tool's step objects.

    THE PROMPT IS REQUIRED HERE, and that is the whole of this function's
    opinion. `WorkflowStepCreate.input` is `Field(default_factory=dict)`, so the
    API accepts a step with no input at all and every CLI-agent profile then
    fails at runtime -- `runners/cliagent.py` raises "requires a non-empty string
    input.prompt" once the agent has already been admitted, scheduled and
    started. The web UI's New Workflow screen submits exactly that shape, which
    is why every workflow built on that screen fails at every step, totally and
    deterministically, having spent real capacity to get there. A refusal at the
    keyboard costs nothing; the same refusal four minutes later costs a dispatch,
    a lease and a pod.

    INPUT IS THE PROMPT, PLUS WHAT THE PROFILE DECLARES, and nothing else --
    for the same reason `swarm_dispatch` has no raw `input` parameter:
    invariant 10 says a caller names a runner profile and supplies DATA, never
    an image, a command or a resource spec. `prompt` becomes `input.prompt`,
    and a step's `inputs` becomes the rest of `input` only where the frozen
    catalogue's `RunnerProfile.inputs` names them for its profile (#142,
    contract request 25) -- the mock's `sleep_seconds`, for one. swarm-api
    applies the same declaration to every caller. Nothing here reaches the
    runner's argv.

    Everything else about the DAG -- cycles, a dependency naming a step that is
    not in the workflow, an `input_from` whose source is not also a `depends_on`,
    two parents of one step staging the same filename, an absolute or
    traversing filename, the step ceiling -- is checked by
    `swarm_api.validation` and is NOT restated here. That validator is the one with the tests; a second opinion in this file
    would be a second thing to keep in step with it.
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SwarmError("a workflow needs a non-empty list of steps")

    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        where = f"step[{index}]"
        if not isinstance(raw, dict):
            raise SwarmError(f"{where} is not an object")
        unknown = sorted(set(raw) - _STEP_KEYS)
        if unknown:
            # THE WORDING HAS TO AGREE WITH THE LIST BESIDE IT. This said "a
            # resource spec cannot be supplied at all" directly after listing
            # `resource_class` as accepted (#88, SC-F6). What is refused is a
            # spec -- cpu, memory, an image, a command, a backend; what is
            # accepted is a NAME, from the catalogue, which is invariant 10.
            raise SwarmError(
                f"{where} carries {unknown}, which this tool does not send. "
                f"Accepted: {sorted(_STEP_KEYS)}. A step's data goes in `prompt`, "
                "which becomes its `input.prompt`, and in `inputs`, for the inputs "
                "its runner profile declares (`swarm_profiles` lists them); a runner "
                "is chosen by naming a `runner_profile` and a size by naming a "
                "`resource_class`. An image, a command, a backend or cpu and memory "
                "figures are never sent."
            )

        step_id = str(raw.get("step_id") or "").strip()
        if not step_id:
            raise SwarmError(f"{where} has no step_id; every step needs one")
        where = f"step {step_id!r}"

        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise SwarmError(
                f"{where} has no prompt. A step with an empty input reaches the "
                "agent and fails there -- `claude-code requires a non-empty "
                "string input.prompt` -- after the workflow has already been "
                "admitted and dispatched. Give every step its instructions."
            )

        # THE PROFILE NAME IS CHECKED BEFORE THE WHOLE WORKFLOW IS SUBMITTED,
        # and the reason is on the record. On 2026-09-23 a twenty-step run
        # dispatched four `codex` steps and all four failed at the provider,
        # each having been admitted, leased and dispatched first. A workflow is
        # all-or-nothing on admission (invariant 2) but not on validity: the
        # sixteen good steps still ran, so the bad four cost real capacity to
        # learn something the catalogue already knew. Refusing here refuses the
        # SUBMISSION, before any of it is scheduled.
        profile = catalogue.check(str(raw.get("runner_profile") or DEFAULT_PROFILE), where=where)
        step: dict[str, Any] = {
            "step_id": step_id,
            "runner_profile": profile,
            # The prompt, and whatever the profile DECLARES and the step asked
            # for -- checked by name and kind, and refused otherwise (#142).
            # `check_inputs` refuses a `prompt` key, so it cannot replace this one.
            "input": {
                "prompt": prompt,
                **catalogue.check_inputs(profile, raw.get("inputs"), where=where),
            },
        }

        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(
            isinstance(d, str) for d in depends_on
        ):
            raise SwarmError(f"{where}: depends_on must be a list of step ids")
        step["depends_on"] = list(depends_on)

        input_from = raw.get("input_from") or {}
        if not isinstance(input_from, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in input_from.items()
        ):
            raise SwarmError(
                f"{where}: input_from must map an upstream step id to one "
                'artifact filename, e.g. {"research": "research.md"}'
            )
        step["input_from"] = dict(input_from)

        if raw.get("resource_class"):
            # THE NAME ONLY. That it is a class the catalogue holds is checkable
            # here from the frozen contract; that it is no LARGER than the
            # profile's own is `swarm_api.validation`'s rule and is deliberately
            # not restated -- a second opinion about the ceiling is how the two
            # would start disagreeing about which submissions are legal.
            step["resource_class"] = catalogue.check_resource_class(
                str(raw["resource_class"]), where=where
            )
        if raw.get("timeout_seconds") is not None:
            step["timeout_seconds"] = int(raw["timeout_seconds"])
        if raw.get("stage") is not None and not isinstance(raw.get("stage"), str):
            raise SwarmError(f"{where}: stage must be a string -- the group /sc:run shows it under")
        steps.append(step)
    return steps


def _canonical_numbers(value: Any) -> Any:
    """`value` with every integral float made an int, recursively.

    JavaScript has one number type, so `JSON.stringify(3.0)` is `3`, while
    Python's `json.dumps(3.0)` is `3.0`. A relay that writes `3.0` for the 3 a
    spec holds has not changed the spec, and must not fail its digest.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_numbers(item) for item in value]
    return value


def spec_digest(spec: Any) -> str:
    """`fnv1a32:<8 hex>` of a spec, the digest `/sc:run` computes in its script.

    WHY A DIGEST. `/sc:run` cannot call a tool: it hands the spec to an agent
    (`sc:workflow`, haiku) that RETYPES it into `swarm_workflow`. A dropped
    step, two swapped prompts or a "tidied" instruction would be submitted and
    run with no layer noticing. The script computes this digest over the spec
    it was given, the agent passes it beside the spec, and `swarm_workflow`
    refuses before sending anything when the spec it received digests
    differently.

    WHY THIS ONE. It must be computed identically by `plugin/workflows/run.js`,
    which has no crypto, no imports and no `TextEncoder` guarantee: FNV-1a over
    32 bits is a few lines of `Math.imul`. It detects accidents, not an
    adversary, which is the job -- the relay is careless, not hostile.

    THE CANONICAL FORM, which both sides must produce byte for byte: JSON with
    keys sorted, no whitespace (`,` and `:`), non-ASCII characters as
    themselves, encoded as UTF-8 -- `JSON.stringify` of each scalar in
    JavaScript, which escapes exactly what `json.dumps(ensure_ascii=False)`
    escapes. Integral floats are ints (`_canonical_numbers`). Keys sort by code
    point here and by UTF-16 unit in JavaScript, which agree for every key a
    spec has (ASCII). `tests/unit/mcp/test_plugin_agents_and_workflows.py`
    runs run.js's implementation and holds the two equal.
    """
    text = json.dumps(
        _canonical_numbers(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return f"fnv1a32:{fnv1a32(text.encode('utf-8', 'surrogatepass')):08x}"


def fnv1a32(data: bytes) -> int:
    """32-bit FNV-1a: offset basis 0x811C9DC5, prime 0x01000193."""
    value = 0x811C9DC5
    for byte in data:
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def read_spec(document: Any, *, where: str = "the workflow spec") -> dict[str, Any]:
    """A whole `swarm workflow` spec, checked, as `submit`'s keyword arguments.

    Returns `steps` built by `build_steps`, and the spec's `strategy`,
    `carrier`, `repository_url`, `repository_ref`, `on_step_failure`,
    `priority` and `label` as given (None when absent). The caller decides what
    overrides them -- the terminal's flags, or the repository the bridge infers
    from a checkout.
    """
    if not isinstance(document, dict):
        raise SwarmError(f"{where} must be an object with a `steps` list")
    unknown = sorted(set(document) - SPEC_KEYS)
    if unknown:
        raise SwarmError(
            f"{where} carries {unknown}, which a workflow spec does not have. "
            f"Accepted: {sorted(SPEC_KEYS)}. A step's runner is chosen by naming its "
            "`runner_profile`; an image, a command, a backend, a model or a resource "
            "spec is never sent."
        )
    return {
        "steps": build_steps(document.get("steps")),
        "strategy": document.get("strategy"),
        "carrier": document.get("carrier"),
        "repository_url": document.get("repository_url"),
        "repository_ref": document.get("repository_ref"),
        "on_step_failure": document.get("on_step_failure"),
        "priority": document.get("priority"),
        "label": document.get("label"),
    }


def submit(
    client: SwarmClient,
    *,
    steps: list[dict[str, Any]],
    strategy: str | None = None,
    carrier: str | None = None,
    repository_url: str | None = None,
    repository_ref: str | None = None,
    on_step_failure: str | None = None,
    priority: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """`POST /v1/workflows`. Returns the route's envelope, not just the workflow.

    The envelope is kept whole because its second half is the part a caller
    cannot get anywhere else: `dispatch` echoes the strategy and carrier that
    were ACCEPTED -- a submission that named neither gets the defaults back --
    and names which step will open the one pull request under `integrate`.
    Dropping it would make the tool's answer smaller than the server's.
    """
    body: dict[str, Any] = {
        "steps": steps,
        "metadata": {"origin": "swarm-mcp", **({"unit": label} if label else {})},
    }
    if strategy:
        body["strategy"] = strategy
    if carrier:
        body["carrier"] = carrier
    if repository_url:
        body["repository_url"] = repository_url
    if repository_ref:
        body["repository_ref"] = repository_ref
    if on_step_failure:
        body["on_step_failure"] = on_step_failure
    if priority is not None:
        body["priority"] = int(priority)
    return _envelope(client.request("POST", "/v1/workflows", payload=body), "submitting")


def fetch(client: SwarmClient, workflow_id: str) -> dict[str, Any]:
    return _envelope(
        client.request("GET", f"/v1/workflows/{workflow_id}"), f"reading {workflow_id}"
    )


def cancel(client: SwarmClient, workflow_id: str) -> dict[str, Any]:
    """`POST /v1/workflows/{id}/cancel`. Answers a plain report, not a workflow.

    `Store.cancel_workflow` sets `cancel_requested` on the workflow and then
    requests cancellation of every step task that has one, sorting them into
    `tasks_cancelled` and `tasks_already_terminal`. Passed through unchanged: a
    step that had already finished is not a failure of the cancel and must not be
    reported as one.
    """
    data = client.request("POST", f"/v1/workflows/{workflow_id}/cancel", payload={})
    if not isinstance(data, dict):
        raise SwarmError(
            f"cancelling {workflow_id}: the route answered "
            f"{type(data).__name__}, not an object"
        )
    return data


def _envelope(payload: Any, what: str) -> dict[str, Any]:
    """The route's body, checked for the one key everything below reads.

    Named rather than assumed for the reason `unwrap_task` exists: reading an
    envelope that does not contain what you think does not raise -- every field
    comes back absent -- so a workflow with no state, no steps and no tasks looks
    exactly like a workflow that has not started.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow"), dict):
        seen = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SwarmError(f"{what}: the response carried no `workflow` object (saw {seen})")
    return payload


# --------------------------------------------------------------------------
# Reading: the derived state, or none at all
# --------------------------------------------------------------------------

def state_of(workflow: dict[str, Any]) -> dict[str, Any]:
    """The workflow's state as a reader may be given it -- three outcomes.

    1. The server derived and reached an answer: that answer, marked derived.
    2. The server derived and could NOT complete the read (a step task missing,
       or the per-request read budget spent): `UNKNOWN`, with the rollup's own
       reason and the steps it could not read. Not an error -- a partial read
       honestly reported -- but never rounded up to a state.
    3. The server did not derive: NO state, and a sentence naming the defect.
       See this module's docstring for why there is no fallback.
    """
    stored = workflow.get("stored_state")
    source = workflow.get("state_source")
    out: dict[str, Any] = {"stored_state": stored, "state_source": source}

    if source != DERIVED:
        out["state"] = None
        out["state_unavailable_because"] = (
            f"this read did not derive the workflow state (state_source={source!r}), "
            "so the value the response carries is the Firestore cache. That cache "
            "is written once at submission and a workflow whose steps had all "
            "SUCCEEDED still read QUEUED, so the bridge will not report it as a "
            "state. The per-step states below came from the step TASKS and are "
            "live -- read those. To fix the server: GET /v1/workflows/{workflow_id} "
            "must apply swarm_api.rollup and codec.workflow_to_api must set "
            "state_source."
        )
        return out

    rollup = workflow.get("rollup") if isinstance(workflow.get("rollup"), dict) else {}
    out["state"] = workflow.get("state")
    if rollup:
        out["counts"] = rollup.get("counts") or {}
        out["steps_read"] = rollup.get("steps_read")
    if out["state"] == UNKNOWN or (rollup and rollup.get("complete") is False):
        out["state"] = UNKNOWN
        out["state_incomplete_because"] = (
            f"{rollup.get('reason') or 'the derivation did not complete'}. "
            f"unreadable steps: {rollup.get('unreadable_steps') or 'none'}; "
            f"steps with no task: {rollup.get('unstarted_steps') or 'none'}. "
            "This is a partial read, not a workflow in a state called UNKNOWN."
        )
    drift = workflow.get("drift")
    if isinstance(drift, dict) and drift.get("agrees") is False:
        # Reported, never smoothed over. The cache WAS wrong at the moment of
        # this read even though the route repaired it, and that is the fact a
        # caller needs in order to distrust anything that read it earlier.
        out["stored_state_was_wrong"] = True
    return out


def step_rows(
    envelope: dict[str, Any],
    *,
    describe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per step, joined to the task document the same read returned.

    The join is `step.task_id -> task.id`, which is data rather than policy, so
    doing it here restates nothing. What the row says when the join FAILS is the
    part that matters: a step with no task_id and a step whose task was not in
    the response are two different facts, and neither of them is "this step has
    not started". `describe`, when given, adds what the step's task produced --
    commits, patch, pull request, and the reason when there is none.
    """
    tasks: dict[str, dict[str, Any]] = {}
    for task in envelope.get("tasks") or []:
        if isinstance(task, dict) and task.get("id"):
            tasks[str(task["id"])] = task

    rows: list[dict[str, Any]] = []
    for step in envelope["workflow"].get("steps") or []:
        task_id = step.get("task_id")
        row: dict[str, Any] = {
            "step_id": step.get("step_id"),
            "task_id": task_id,
            "runner_profile": step.get("runner_profile"),
            "depends_on": step.get("depends_on") or [],
            "input_from": step.get("input_from") or {},
        }
        if not task_id:
            row["state"] = None
            row["state_unavailable_because"] = (
                "this step carries no task id. Every step created by the current "
                "submission path gets one, so this is malformed data, not a phase "
                "a healthy step passes through."
            )
            rows.append(row)
            continue
        task = tasks.get(str(task_id))
        if task is None:
            row["state"] = None
            row["state_unavailable_because"] = (
                "the workflow read returned no task document for this step. The "
                "step exists and names a task; that task was not in the page this "
                "read loaded, so its state was NOT read -- it is not absent and it "
                "is not queued."
            )
            rows.append(row)
            continue

        row["state"] = task.get("state")
        if task.get("cancel_requested"):
            # The step's own flag, which the state does NOT carry: a step
            # holding capacity stays DISPATCHED or RUNNING, flagged, until its
            # worker releases it. Without this a cancelled workflow showed its
            # steps as plainly RUNNING (#88, SC-F2).
            row["cancel_requested"] = True
        if task.get("park_reason"):
            row["park_reason"] = task["park_reason"]
        if task.get("blocked_by"):
            row["blocked_by"] = task["blocked_by"]
        if task.get("last_error"):
            row["error"] = task["last_error"]
        if describe is not None:
            produced = describe(task)
            produced.pop("task_id", None)
            produced.pop("state", None)
            row["produced"] = produced
        rows.append(row)
    return rows


def report(
    envelope: dict[str, Any],
    *,
    describe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What both read tools answer: the state rule first, then the steps."""
    workflow = envelope["workflow"]
    rows = step_rows(envelope, describe=describe)
    out: dict[str, Any] = {"workflow_id": workflow.get("workflow_id")}
    out.update(state_of(workflow))
    out["steps_total"] = len(rows)
    out["cancel_requested"] = workflow.get("cancel_requested")
    out["on_step_failure"] = workflow.get("on_step_failure")
    if isinstance(envelope.get("dispatch"), dict):
        out["dispatch"] = envelope["dispatch"]
    out["steps"] = rows

    followable = [str(r["task_id"]) for r in rows if r.get("task_id")]
    if followable:
        # The same answer `swarm_dispatch` gives, for the same reason: an MCP
        # tool returns once and cannot stream, so the honest thing to hand back
        # is the command that can. `tail` takes several ids precisely so one
        # background shell follows a whole fan-out.
        #
        # Spelled by `follow.follow_command`, not here. This file used to build
        # the string itself and got `swarm tail`, which is not a command anyone
        # has on their PATH.
        out["follow_live_with"] = follow_command(followable)
    return out
