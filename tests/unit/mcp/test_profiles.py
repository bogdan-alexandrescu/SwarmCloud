"""The runner catalogue as the plugin serves it, and invariant 10 around it.

WHAT THIS FILE IS PROTECTING. Invariant 10: an API caller picks a
`runner_profile` BY NAME and never supplies an image, a command, a resource spec
or a backend. The write paths already honoured it. What did not exist was the
other half of "by name" -- any way for a session to learn what the names ARE.
The `delegate` skill carried them in a sentence of prose, which is a restatement
of the frozen contract in a file no test read.

So two things are asserted here and they pull in opposite directions, which is
why both are needed:

  * The catalogue a session can read must be COMPLETE enough to choose from --
    every name, and for a refused one the reason, because "unknown profile" and
    "this profile is refused, here is why" send a reader to two different places.
  * It must be NARROW enough that the image and the command never appear. Not
    because reading one is itself harmful, but because a field a session can see
    is a field a session will offer to set, and the first request after that is
    a parameter that sets it.

Offline by construction: `swarm_common.profiles` is dataclasses and enums, no
client, no credentials, no network. Nothing here builds a `SwarmClient`.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend
from swarm_mcp import profiles as catalogue
from swarm_mcp.client import SwarmError

#: Every field of `RunnerProfile` that describes HOW a profile executes rather
#: than WHICH profile it is. None of these may reach a caller through this
#: bridge, and the list is spelled out so a new one added to the frozen
#: dataclass has to be considered here rather than inherited silently.
_EXECUTION_DETAIL = ("image", "runner_argv", "secrets", "secrets_any_of", "spot")


def test_every_execution_detail_named_here_is_a_real_runner_profile_field():
    """A name here that is not a field guards nothing, and says it does.

    This list said `command` until contract request 18 renamed the field to
    `runner_argv` (2026-09-24). Left stale, the leak check below would go on
    looking for a key the dataclass no longer has, and a `runner_argv` served
    to a caller would pass it.
    """
    import dataclasses

    from swarm_common.profiles import RunnerProfile

    fields = {f.name for f in dataclasses.fields(RunnerProfile)}
    stale = sorted(set(_EXECUTION_DETAIL) - fields)
    assert not stale, f"_EXECUTION_DETAIL names fields RunnerProfile does not have: {stale}"


def test_the_catalogue_is_the_frozen_one_and_not_a_copy_of_it():
    """Names come from `RUNNER_PROFILES` or the whole exercise is pointless.

    THE MUTATION THIS CATCHES: hardcode a list like
    `["mock", "generic", "claude-code", "browser"]` anywhere in
    `swarm_mcp.profiles` -- which is exactly the shape the `delegate` skill's
    prose had -- and this goes red the moment the frozen catalogue gains or
    loses an entry, which is the day the hardcoded list starts lying.
    """
    served = {entry["name"] for entry in catalogue.catalogue()}
    assert served == set(RUNNER_PROFILES), (
        "the served catalogue and the frozen catalogue disagree about which "
        f"profiles exist: served {sorted(served)}, frozen {sorted(RUNNER_PROFILES)}"
    )
    # Every entry exactly once. A duplicate would let a reader pick the same
    # name twice and read two different sets of facts about it.
    assert len(catalogue.catalogue()) == len(RUNNER_PROFILES)


def test_a_disabled_profile_is_listed_and_carries_its_reason():
    """Disabled, not deleted -- and the reason travels with the flag.

    `RunnerProfile.__post_init__` refuses a disabled profile with no reason, on
    the grounds that "a caller told only that a known profile was refused has
    nothing to act on". Serving the flag without the reason one layer up would
    reintroduce precisely that.
    """
    refused = [e for e in catalogue.catalogue() if not e["available"]]
    assert refused, (
        "no profile is disabled in the frozen catalogue, so this assertion has "
        "nothing to check -- if `codex` was re-enabled, keep the test and pick "
        "the disabled one dynamically rather than deleting the guarantee"
    )
    for entry in refused:
        frozen = RUNNER_PROFILES[entry["name"]]
        assert entry["disabled_reason"] == frozen.disabled_reason, (
            "the reason is quoted from the catalogue VERBATIM; a paraphrase "
            "here is a second wording of the platform's own answer"
        )
        assert entry["disabled_reason"].strip()


def test_the_served_view_never_carries_an_image_a_command_or_a_secret_name():
    """INVARIANT 10, at the surface a session reads.

    THE MUTATION THIS CATCHES: add `"image": profile.image` to
    `profiles.public_profile` -- a one-line change that looks like helpfulness --
    and this goes red. It also goes red on a `secrets` key, because serving the
    variable NAMES invites a session to go looking for the values, and this
    bridge has no business near them.
    """
    for entry in catalogue.catalogue():
        leaked = sorted(set(entry) & set(_EXECUTION_DETAIL))
        assert not leaked, (
            f"{entry['name']} serves execution detail a caller may not name: "
            f"{leaked}. A caller picks a profile BY NAME; the image, the command "
            "and the resource spec follow from it and are not a caller's vocabulary"
        )
        # Belt and braces: no VALUE anywhere in the entry may be an image
        # reference either, whatever key it arrived under.
        for key, value in entry.items():
            if isinstance(value, str):
                assert "agent-runtime" not in value, (
                    f"{entry['name']}.{key} carries an image reference: {value!r}"
                )


def test_the_served_view_is_an_allow_list_with_the_fields_a_chooser_needs():
    """Complete enough to choose from. Each of these answers a real question.

    `backend` is the resolved one, not the declared one: a profile may be
    declared AUTO, and which backend it LANDS on is the answer to "why did this
    one queue behind GKE capacity when the others did not".
    """
    needed = {
        "name",
        "available",
        "backend",
        "resource_class",
        "cpu",
        "memory_gib",
        "workspace_gib",
        "timeout_seconds",
        "needs_provider_credential",
        "provider",
    }
    for entry in catalogue.catalogue():
        missing = sorted(needed - set(entry))
        assert not missing, f"{entry['name']} is missing {missing}"
        assert entry["backend"] in {b.value for b in Backend}
        # AUTO is a declaration, never an answer. `resolve_backend` exists to
        # turn it into one, and serving AUTO here would hand a reader the
        # question back.
        assert entry["backend"] != Backend.AUTO.value, (
            f"{entry['name']} serves an unresolved backend"
        )
        assert entry["resource_class"] in RESOURCE_CLASSES
        assert entry["needs_provider_credential"] is (entry["provider"] is not None)


def test_dispatchable_profiles_are_listed_before_refused_ones():
    """The list is read to make a choice, so a refused name must not sit in the
    middle of the usable ones where a reader picks it before reaching the reason
    it is there."""
    flags = [entry["available"] for entry in catalogue.catalogue()]
    assert flags == sorted(flags, reverse=True), (
        f"usable and refused profiles are interleaved: {flags}"
    )


def test_an_unknown_name_is_refused_and_the_real_names_are_offered():
    """The refusal has to be actionable, which means naming what would work.

    A model that typed `claude` for `claude-code` and read only "invalid
    runner_profile" retries the same guess. One that reads the catalogue picks
    the right name on the next call.
    """
    with pytest.raises(SwarmError) as caught:
        catalogue.check("claude")
    message = str(caught.value)
    assert "claude" in message
    for name in RUNNER_PROFILES:
        assert name in message, f"the refusal does not offer {name}"


def test_a_disabled_name_is_refused_as_refused_and_not_as_unknown():
    """Two different facts needing two different responses.

    "There is no such profile" sends someone hunting for a typo. "This profile
    is known and is refused because the provider rejected the credential" sends
    them to the remedy. The frozen catalogue disables rather than deletes
    specifically to keep those apart; collapsing them here would throw that away.

    THE MUTATION THIS CATCHES: drop the `available` branch from
    `profiles.check`, so a disabled profile passes and is refused by the API four
    minutes later -- which is the 2026-09-23 incident, four codex steps of a
    twenty-step run, each admitted, leased and dispatched before anything said no.
    """
    disabled = [n for n, p in RUNNER_PROFILES.items() if not p.available]
    assert disabled, "nothing is disabled, so this test has nothing to check"
    for name in disabled:
        with pytest.raises(SwarmError) as caught:
            catalogue.check(name)
        message = str(caught.value)
        assert "no runner profile called" not in message, (
            f"{name} is known and refused; reporting it as unknown sends the "
            "reader hunting for a typo that is not there"
        )
        assert RUNNER_PROFILES[name].disabled_reason in message


def test_an_empty_name_is_refused_rather_than_defaulted():
    """An empty string is not a request for the default. The default is applied
    by the caller that has one -- `swarm_dispatch` and `build_steps` both pass
    `or "claude-code"` before calling this -- so a bare empty string reaching
    here means something upstream lost the value, and silently dispatching
    `claude-code` would hide that."""
    for empty in ("", "   ", None, 7):
        with pytest.raises(SwarmError):
            catalogue.check(empty)  # type: ignore[arg-type]


def test_every_dispatchable_name_passes_its_own_check():
    """The two halves of the surface must agree: anything `swarm_profiles`
    offers as usable must be a name `check` accepts. A list that advertises a
    name the dispatch path then refuses is worse than no list."""
    for name in catalogue.dispatchable():
        assert catalogue.check(name) == name


def test_a_resource_class_is_checked_by_name_only():
    """The NAME is checkable from the frozen contract. Whether a class is within
    what the step's profile allows is `swarm_api.validation`'s rule and is
    deliberately not restated -- a second opinion about the ceiling is how the
    two would start disagreeing about which submissions are legal."""
    for name in RESOURCE_CLASSES:
        assert catalogue.check_resource_class(name) == name
    with pytest.raises(SwarmError) as caught:
        catalogue.check_resource_class("standrad")
    assert "standrad" in str(caught.value)
    for name in RESOURCE_CLASSES:
        assert name in str(caught.value)


def test_the_backend_of_an_unknown_profile_is_unknown_and_not_a_default():
    """`describe_task` calls this on whatever profile name a task document
    carries, including one the catalogue no longer holds.

    None means NOT KNOWN, which is the same three-marks rule the rest of this
    plugin is held to. A reader handed CLOUD_RUN_JOB for a profile nobody can
    look up would go and read the wrong service's logs.

    THE MUTATION THIS CATCHES: make `backend_of` fall back to
    `Backend.CLOUD_RUN_JOB.value` for an unknown name -- the tempting default,
    since most profiles are on it -- and this goes red.
    """
    assert catalogue.backend_of("a-profile-that-was-renamed") is None
    assert catalogue.backend_of(None) is None
    assert catalogue.backend_of(42) is None
    for name, profile in RUNNER_PROFILES.items():
        assert catalogue.backend_of(name) is not None
        if profile.backend is not Backend.AUTO:
            assert catalogue.backend_of(name) == profile.backend.value


# --------------------------------------------------------------------------
# The refusal has to happen BEFORE anything is spent
# --------------------------------------------------------------------------
#
# A check that runs after the POST is not a check, it is a second opinion about
# a decision already made. These assert the ordering rather than the message,
# because the ordering is the part that costs money when it is wrong.


class _RefusingClient:
    """A client that fails the test if anything reaches the API at all.

    The assertion is not "an error was raised" -- the API raises one too, four
    minutes and one dispatch later. It is that the network was never touched.
    """

    def __init__(self):
        self.calls: list[str] = []

    def dispatch(self, **kwargs):
        self.calls.append("dispatch")
        raise AssertionError(
            "a refused profile reached the API; the whole point of checking it "
            "locally is that it costs a dispatch, a lease and a pod to find out "
            "there"
        )

    def request(self, method, path, **kwargs):
        self.calls.append(f"{method} {path}")
        raise AssertionError(f"a refused submission reached the API: {method} {path}")


def test_the_dispatch_tool_refuses_a_bad_profile_without_calling_the_api():
    """THE MUTATION THIS CATCHES: move `catalogue.check` from the argument list
    of `client.dispatch` to after the call, or delete it -- and `_RefusingClient`
    raises AssertionError instead of the test passing."""
    from swarm_mcp import server

    client = _RefusingClient()
    with pytest.raises(SwarmError) as caught:
        server._call(client, "swarm_dispatch", {"prompt": "hi", "profile": "claude"})
    assert "claude" in str(caught.value)
    assert client.calls == [], f"the API was called anyway: {client.calls}"


def test_a_workflow_naming_a_disabled_profile_is_refused_at_build_time():
    """The 2026-09-23 incident, in a test.

    Four `codex` steps of a twenty-step run were admitted, leased and dispatched
    before the provider refused them. A workflow is all-or-nothing on admission
    (invariant 2) but not on validity, so the sixteen good steps ran and the bad
    four spent real capacity to learn what the catalogue already knew.

    `build_steps` is where the whole submission is assembled, so a refusal here
    refuses it before any of it is scheduled.
    """
    from swarm_mcp import workflows

    disabled = next(n for n, p in RUNNER_PROFILES.items() if not p.available)
    steps = [
        {"step_id": "good", "prompt": "do the work", "runner_profile": "claude-code"},
        {"step_id": "bad", "prompt": "do the work", "runner_profile": disabled},
    ]
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(steps)
    message = str(caught.value)
    assert "bad" in message, "the refusal must name WHICH step is wrong"
    assert RUNNER_PROFILES[disabled].disabled_reason in message


def test_a_workflow_step_naming_an_unknown_resource_class_is_refused_too():
    """A typo in a named class is the same shape of mistake as a typo in a
    profile name, and the API answers it with a 4xx after the submission has
    travelled."""
    from swarm_mcp import workflows

    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "s", "prompt": "p", "resource_class": "standrad"}]
        )
    assert "standrad" in str(caught.value)


def test_a_valid_workflow_still_builds():
    """The guard must not have made the ordinary case unreachable -- a refusal
    that refuses everything passes every test about refusing."""
    from swarm_mcp import workflows

    steps = workflows.build_steps(
        [
            {"step_id": "research", "prompt": "find out"},
            {
                "step_id": "write",
                "prompt": "write it up",
                "runner_profile": "claude-code",
                "depends_on": ["research"],
                "input_from": {"research": "research.md"},
                "resource_class": "standard",
            },
        ]
    )
    assert [s["step_id"] for s in steps] == ["research", "write"]
    # The default is applied by the caller and survives the check.
    assert steps[0]["runner_profile"] == workflows.DEFAULT_PROFILE
    assert steps[1]["resource_class"] == "standard"


def test_the_profiles_tool_answers_without_a_client_at_all():
    """The one tool that still works when the cluster cannot be reached.

    That is not a curiosity: it is exactly when someone is guessing at a profile
    name, because every other tool is busy failing. `_RefusingClient` proves no
    round trip happens.
    """
    import json

    from swarm_mcp import server

    client = _RefusingClient()
    payload = json.loads(server._call(client, "swarm_profiles", {}))
    assert client.calls == []
    assert {e["name"] for e in payload["profiles"]} == set(RUNNER_PROFILES)


def test_the_profiles_tool_is_registered_and_takes_no_arguments():
    """Registered is not the same as reachable, and reachable is not the same as
    callable -- `swarm_follow` was all three only after `_call` routed it."""
    from swarm_mcp import server

    tool = next((t for t in server.TOOLS if t["name"] == "swarm_profiles"), None)
    assert tool is not None, "swarm_profiles is not in TOOLS, so no host lists it"
    assert tool["inputSchema"].get("required") in (None, []), (
        "a catalogue read takes no arguments; requiring one is a way to get it wrong"
    )
    # The description must not teach the image vocabulary it withholds.
    assert "image" in tool["description"], (
        "the description has to SAY that no image appears and why, or the "
        "absence reads as an oversight and someone helpfully adds one"
    )


def test_the_result_of_a_task_names_its_profile_and_its_backend():
    """A failure that says only `state: FAILED` sends the reader to the logs to
    find out what KIND of thing failed. Which profile ran and which backend it
    landed on are the two facts that separate a bad prompt from a placement
    problem, and the backend is the one a reader cannot derive, because the
    mapping lives in the frozen catalogue rather than on the task.

    THE MUTATION THIS CATCHES: drop either key from `patches.describe_task`.
    """
    from swarm_mcp.patches import describe_task

    described = describe_task(
        {"id": "task_x", "state": "FAILED", "runner_profile": "browser",
         "last_error": "the pod could not be placed"}
    )
    assert described["runner_profile"] == "browser"
    assert described["backend"] == RUNNER_PROFILES["browser"].backend.value
    assert described["error"] == "the pod could not be placed"

    # An old task naming a profile the catalogue no longer holds reports the
    # backend as unknown rather than guessing one.
    stale = describe_task({"id": "task_y", "state": "SUCCEEDED", "runner_profile": "gone"})
    assert stale["runner_profile"] == "gone"
    assert stale["backend"] is None


# --------------------------------------------------------------------------
# "task failed" is not a report
# --------------------------------------------------------------------------


class _AttemptClient:
    """A client that serves one task and its attempt list, and counts reads."""

    def __init__(self, task, attempts=None, raises=False):
        self._task = task
        self._attempts = attempts if attempts is not None else []
        self._raises = raises
        self.attempt_reads = 0

    def task(self, task_id):
        return self._task

    def attempts(self, task_id, *, limit=20):
        self.attempt_reads += 1
        if self._raises:
            raise SwarmError("403: the attempts route refused this caller")
        return self._attempts


def _failed_task(**extra):
    return {"id": "task_x", "state": "FAILED", "runner_profile": "claude-code", **extra}


def test_a_successful_result_costs_no_extra_round_trip():
    """`explain_failure` returns None without asking, so the common read pays
    nothing. THE MUTATION THIS CATCHES: move the state check below the fetch."""
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient({"id": "task_x", "state": "SUCCEEDED"})
    assert explain_failure(client, {"id": "task_x", "state": "SUCCEEDED"}) is None
    assert client.attempt_reads == 0


def test_a_failure_names_the_attempt_the_backend_and_the_exit_code():
    """The four facts a reader needs and the task document does not carry."""
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient(
        _failed_task(last_error="agent exited non-zero", attempt_count=2),
        attempts=[
            {"attempt_id": "att_2", "generation": 2, "backend": "GKE_AUTOPILOT",
             "execution_name": "swarm-job-abc", "exit_code": 137,
             "error": "killed", "oom_near_miss": True, "peak_rss_bytes": 8_000_000},
            {"attempt_id": "att_1", "generation": 1, "backend": "CLOUD_RUN_JOB",
             "exit_code": 1, "error": "first try failed"},
        ],
    )
    failure = explain_failure(client, _failed_task(attempt_count=2))
    last = failure["last_attempt"]
    assert last["attempt_id"] == "att_2"
    assert last["backend"] == "GKE_AUTOPILOT"
    assert last["exit_code"] == 137
    assert last["oom_near_miss"] is True
    assert last["execution_name"] == "swarm-job-abc"

    # The EARLIER attempt is the whole reason the route exists: `result_summary`
    # is written once at terminal state and would have overwritten it.
    assert failure["earlier_attempts"] == [
        {"attempt_id": "att_1", "generation": 1, "exit_code": 1,
         "error": "first try failed"}
    ]


def test_a_missing_exit_code_is_reported_as_unknown_and_never_as_zero():
    """The most expensive possible instance of the three-marks rule.

    0 means the agent exited cleanly. A task that FAILED with no recorded exit
    code did not exit cleanly, so rendering the absence as 0 states the opposite
    of what happened.

    THE MUTATION THIS CATCHES: `"exit_code": last.get("exit_code", 0)` or
    `last.get("exit_code") or 0` -- either of the two ways this is normally
    written wrong.
    """
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient(
        _failed_task(),
        attempts=[{"attempt_id": "att_1", "generation": 1, "exit_code": None}],
    )
    failure = explain_failure(client, _failed_task())
    assert failure["last_attempt"]["exit_code"] is None
    assert "not recorded" in failure["last_attempt"]["exit_code_note"]
    assert "not 0" in failure["last_attempt"]["exit_code_note"]

    # A real 0 must NOT pick up the note -- it is a measurement.
    client = _AttemptClient(
        _failed_task(),
        attempts=[{"attempt_id": "att_1", "generation": 1, "exit_code": 0}],
    )
    failure = explain_failure(client, _failed_task())
    assert failure["last_attempt"]["exit_code"] == 0
    assert "exit_code_note" not in failure["last_attempt"]


def test_no_attempts_at_all_is_a_finding_and_not_an_empty_report():
    """A task can reach FAILED without any agent running -- admission or
    dispatch failed. That is a different investigation, and saying nothing would
    send the reader to read an agent's logs that do not exist."""
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient(_failed_task(), attempts=[])
    failure = explain_failure(client, _failed_task())
    assert failure["attempts"] == []
    assert "before any agent ran" in failure["note"]
    assert "last_attempt" not in failure


def test_an_unreadable_attempts_route_is_reported_rather_than_swallowed():
    """None means "this task did not fail". Returning it because the attempts
    route 403'd would report a failed task as a healthy one -- the exact
    substitution of a failed read for an empty result this repository keeps
    deleting."""
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient(_failed_task(), raises=True)
    failure = explain_failure(client, _failed_task())
    assert failure is not None, "an unreadable route must not read as 'did not fail'"
    assert "UNKNOWN, not absent" in failure["attempts_unreadable"]
    # What IS known from the task survives the failed read.
    assert failure["state"] == "FAILED"


def test_a_task_with_no_id_is_unreadable_rather_than_never_attempted():
    """Two claims that look alike and are not.

    "This task has no attempt records" says an agent never ran, and sends the
    reader to admission and dispatch. Without a task id nothing was ASKED, so
    saying that would be a claim nobody checked. The id being missing is itself
    the defect `task_id_of` exists for -- the API names the field `id`, and
    reading it as `task_id` made every task on the platform look unstarted.
    """
    from swarm_mcp.patches import explain_failure

    client = _AttemptClient({"state": "FAILED"}, attempts=[])
    failure = explain_failure(client, {"state": "FAILED"})
    assert "attempts_unreadable" in failure
    assert "note" not in failure, (
        "a task whose attempts were never asked for must not be reported as one "
        "that never ran"
    )
    assert client.attempt_reads == 0


def test_dead_lettered_counts_as_failed_under_both_spellings():
    """`client.TERMINAL` already carries both defensively, and a failure
    explainer that declined to explain an unrecognised spelling would be the
    worst place to be strict."""
    from swarm_mcp.patches import FAILED_STATES, explain_failure

    assert {"FAILED", "DEAD_LETTERED", "DEAD_LETTER"} <= FAILED_STATES
    for state in ("DEAD_LETTERED", "DEAD_LETTER"):
        task = {"id": "task_x", "state": state}
        client = _AttemptClient(task, attempts=[])
        assert explain_failure(client, task) is not None


def test_the_result_tool_attaches_the_failure_block():
    """Routed, not merely written: a function nothing calls explains nothing."""
    import json

    from swarm_mcp import server

    task = _failed_task(last_error="boom")
    client = _AttemptClient(
        task,
        attempts=[{"attempt_id": "att_1", "generation": 1, "exit_code": 2,
                   "backend": "CLOUD_RUN_JOB", "error": "boom"}],
    )
    payload = json.loads(server._call(client, "swarm_result", {"task_id": "task_x"}))
    assert payload["state"] == "FAILED"
    assert payload["runner_profile"] == "claude-code"
    assert payload["failure"]["last_attempt"]["exit_code"] == 2
    assert client.attempt_reads == 1


def test_a_failed_workflow_step_carries_the_same_failure_block():
    """"Which step failed" was already answerable; "and why" was not.

    A fan-out is where that gap costs most: four failed steps of twenty, each
    reported as a state word, is four investigations with no starting point.
    The step's block comes from the SAME function as a single task's, so the
    two cannot describe one dead agent differently.
    """
    import json

    from swarm_mcp import server, workflows

    # The route's real envelope shape: `step_rows` joins step.task_id to the
    # TASK DOCUMENTS the same read returned, under `tasks` -- it never fetches
    # them one by one. Getting this wrong in a fake is how a test passes against
    # a shape the server does not send.
    bad_task = dict(_failed_task())
    bad_task["id"] = "task_bad"
    bad_task["last_error"] = "boom"
    envelope = {
        "workflow": {
            "workflow_id": "wf_1",
            "state": "FAILED",
            "state_source": workflows.DERIVED,
            "steps": [
                {"step_id": "ok", "task_id": "task_ok"},
                {"step_id": "bad", "task_id": "task_bad"},
            ],
        },
        "tasks": [
            {"id": "task_ok", "state": "SUCCEEDED", "runner_profile": "mock"},
            bad_task,
        ],
    }

    class _WorkflowClient:
        def __init__(self):
            self.attempt_reads = 0

        def request(self, method, path, **kwargs):
            return envelope

        def attempts(self, task_id, *, limit=20):
            assert task_id == "task_bad", f"attempts read for {task_id}"
            self.attempt_reads += 1
            return [{"attempt_id": "att_1", "generation": 1, "exit_code": 9,
                     "backend": "CLOUD_RUN_JOB", "error": "boom"}]

    client = _WorkflowClient()
    report = json.loads(
        server._call(client, "swarm_workflow_result", {"workflow_id": "wf_1"})
    )
    steps = {s["step_id"]: s for s in report["steps"]}
    assert steps["bad"]["produced"]["failure"]["last_attempt"]["exit_code"] == 9
    # The healthy step carries no `failure` key and cost no extra read.
    assert "failure" not in steps["ok"]["produced"]
    assert client.attempt_reads == 1, (
        "the attempts route must be read once, for the failed step only"
    )


def test_the_result_tool_adds_nothing_when_the_task_succeeded():
    """No `failure` key at all on a healthy result -- an empty one would read as
    a failure with no detail."""
    import json

    from swarm_mcp import server

    task = {"id": "task_x", "state": "SUCCEEDED", "runner_profile": "mock"}
    client = _AttemptClient(task)
    payload = json.loads(server._call(client, "swarm_result", {"task_id": "task_x"}))
    assert "failure" not in payload
    assert client.attempt_reads == 0


def test_the_delegation_skill_never_recommends_a_profile_the_platform_refuses():
    """Prose cannot be refused at runtime, so it is checked here.

    The skill tells a session, in words, to use `claude-code` and not to use
    `codex`. Both sentences are true today and both are one catalogue edit from
    being actively harmful -- a skill that recommends a disabled profile sends
    every delegated unit into a refusal, and one that warns off a re-enabled one
    costs the platform a runner nobody will try again.

    This is the same guarantee `test_plugin_skills` makes about tool names,
    applied to the other thing the skill names that lives in code.
    """
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[3]
        / "plugin" / "skills" / "delegate" / "SKILL.md"
    ).read_text()

    assert "`claude-code`" in skill, (
        "the skill no longer names the profile it recommends, so this check has "
        "nothing to anchor to"
    )
    assert RUNNER_PROFILES["claude-code"].available, (
        "the skill tells sessions to use `claude-code` and the catalogue "
        "refuses it -- every delegated unit would be refused on submit"
    )

    # Every profile the skill warns AGAINST must still be one the platform
    # refuses. `codex` is the live case.
    for name, profile in RUNNER_PROFILES.items():
        if f"`{name}` is DISABLED" in skill or f"**`{name}` is DISABLED**" in skill:
            assert not profile.available, (
                f"the skill says {name} is disabled and the catalogue says it is "
                "available -- a usable runner that no session will ever reach for"
            )
