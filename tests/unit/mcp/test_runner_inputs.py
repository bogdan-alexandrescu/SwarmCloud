"""A caller may send a runner's DECLARED inputs, and nothing else (#142).

WHAT WAS MISSING. The mock runner reads `sleep_seconds`, `steps`, `fail`,
`artifact_text` and the rest from its task's `input`
(`agent_worker/runners/mock.py`), and that is how the cancel, failure and park
paths are exercised without a provider. The plugin could not send any of them:
`swarm workflow` refused a step carrying `sleep_seconds` or an `input` object,
`swarm dispatch` had no input flag, and so every mock step sent through the CLI
slept the 1 s default -- too short to be caught RUNNING and cancelled. The QA
run that found it had to POST the DAG by hand.

WHAT MAY BE SENT. Invariant 10 is unchanged: a caller names a `runner_profile`
and supplies DATA, never an image, a command, a resource spec or a backend
parameter. Runner inputs are data, but they are read by one runner and mean
nothing -- or something else -- to another: `input.model` WAS read by the CLI
runners and selected a model (test_model_flag_is_attribution_only.py; since
#226 the model is the Job's MODEL and the runner reads no `input.model`). So
the gate is per PROFILE, and it is the frozen catalogue's own
`RunnerProfile.inputs` (contract request 25, accepted by the owner on #142,
2026-09-25). Until then the bridge kept a table of its own keyed by profile
name; the API enforces the same declaration now, for every caller, so a second
copy here would be two answers to one question. A profile that declares none
takes none.

Offline: the transport is a recorder; nothing is sent anywhere.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES
from swarm_mcp import cli, server, workflows
from swarm_mcp import profiles as catalogue
from swarm_mcp.client import SwarmClient, SwarmError

_REPO = Path(__file__).resolve().parents[3]

#: What a caller may never name, whatever a profile declares: execution detail
#: (invariant 10), the model the CLI runners would select, and the mock's own
#: keys that write platform records -- spend figures, a provider's identity,
#: a simulated credential refusal, the text and the reset time of a simulated
#: rate limit -- and `attempt_count`, which the lifecycle writes from the task
#: document and the mock bounds its park by: a count a caller could set is a
#: bound a caller could lift.
_NEVER = (
    "image",
    "command",
    "runner_argv",
    "resources",
    "resource_class",
    "cpu",
    "memory_gib",
    "backend",
    "model",
    "spend",
    "provider",
    "credential_revoked_times",
    "credential_detail",
    "quota_detail",
    "reset_at",
    "attempt_count",
    "prompt",
)


def _declared(name: str) -> dict:
    """What the frozen catalogue declares for `name`; `{}` for none."""
    return dict(RUNNER_PROFILES[name].inputs or {})


class _Recorder:
    """The transport, stubbed at `request`, behind the real `dispatch`."""

    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        if path == "/v1/workflows":
            steps = [
                {"step_id": s["step_id"], "task_id": f"task_{s['step_id']}", "depends_on": []}
                for s in (payload or {}).get("steps", [])
            ]
            return {"workflow": {"workflow_id": "wf_1", "steps": steps}}
        return {"task": {"id": "task_1", "state": "QUEUED"}}


class _Refusing(_Recorder):
    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        raise AssertionError(f"a refused submission reached the API: {method} {path}")


# -- a workflow step -----------------------------------------------------------


def test_a_mock_step_carries_its_declared_inputs():
    """The QA case: a 120 s mock step that `swarm cancel` can catch RUNNING."""
    steps = workflows.build_steps(
        [
            {
                "step_id": "slow",
                "runner_profile": "mock",
                "prompt": "wait",
                "inputs": {"sleep_seconds": 120, "steps": 3, "fail": False,
                           "artifact_text": "done"},
            }
        ]
    )
    assert steps[0]["input"] == {
        "prompt": "wait",
        "sleep_seconds": 120,
        "steps": 3,
        "fail": False,
        "artifact_text": "done",
    }


def test_a_key_claude_code_does_not_declare_is_refused():
    """`claude-code` read `input.model` until #226; letting a caller set it was
    the contract change invariant 10 forbids. It declares `issue` and nothing
    else (contract request 28, #265), so the mock's knobs do not go either."""
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "a", "runner_profile": "claude-code", "prompt": "x",
              "inputs": {"sleep_seconds": 5}}]
        )
    message = str(caught.value)
    assert "claude-code" in message, message
    assert "sleep_seconds" in message, message
    assert "issue" in message, "the refusal offers what claude-code does declare"


@pytest.mark.parametrize("key", _NEVER)
def test_an_undeclared_key_is_refused_and_the_declared_ones_are_offered(key):
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "a", "runner_profile": "mock", "prompt": "x", "inputs": {key: "x"}}]
        )
    message = str(caught.value)
    assert key in message, message
    assert "sleep_seconds" in message, "the refusal lists what the profile does declare"


@pytest.mark.parametrize(
    "key,value",
    [
        ("sleep_seconds", "long"),
        ("sleep_seconds", -1),
        ("sleep_seconds", True),
        ("steps", 0),
        ("steps", 1.5),
        ("fail", "yes"),
        ("exit_code", 300),
        ("artifact_name", "../escape.txt"),
        ("artifact_name", "nested/out.txt"),
        ("artifact_name", ""),
        ("artifact_text", 7),
        ("quota_exhausted", "yes"),
        ("retry_after_seconds", 0),
        ("retry_after_seconds", 3601),
        ("retry_after_seconds", 90.5),
        # json.loads reads NaN and Infinity, and every comparison with NaN is
        # False, so a bound written as `value < minimum` lets it through.
        ("retry_after_seconds", float("nan")),
        ("retry_after_seconds", float("inf")),
        ("sleep_seconds", float("nan")),
        ("sleep_seconds", float("inf")),
        ("cpu_burn_seconds", float("-inf")),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_before_it_travels(key, value):
    """The runner would coerce or crash after admission; refused here it costs
    nothing. A bool is not a number, whatever Python thinks, and neither NaN
    nor Infinity is one a runner can sleep for or a worker can wait out."""
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "a", "runner_profile": "mock", "prompt": "x", "inputs": {key: value}}]
        )
    message = str(caught.value)
    assert key in message, message
    assert "must be" in message, message


def test_the_cli_workflow_spec_sends_a_steps_inputs(tmp_path, capsys):
    spec = tmp_path / "wf.json"
    spec.write_text(json.dumps({
        "steps": [{"step_id": "slow", "runner_profile": "mock", "prompt": "wait",
                   "inputs": {"sleep_seconds": 120}}]
    }))
    recorder = _Recorder()
    args = cli.build_parser().parse_args(["workflow", str(spec)])

    assert cli.cmd_workflow(recorder, args) == cli.EXIT_OK

    (_, path, payload), = recorder.sent
    assert path == "/v1/workflows"
    assert payload["steps"][0]["input"] == {"prompt": "wait", "sleep_seconds": 120}


# -- a single dispatch -----------------------------------------------------------


def test_swarm_dispatch_sends_input_flags_typed_by_the_declaration(capsys):
    """`--input KEY=VALUE`, repeatable. A number is a number and a string stays
    a string, by what the profile declares -- not by what JSON happens to parse."""
    recorder = _Recorder()
    args = cli.build_parser().parse_args(
        ["dispatch", "wait", "--profile", "mock",
         "--input", "sleep_seconds=120",
         "--input", "fail=true",
         "--input", "artifact_text=hello world",
         "--input", "fail_message=123"]
    )

    assert cli.cmd_dispatch(recorder, args) == cli.EXIT_OK

    (_, path, payload), = recorder.sent
    assert path == "/v1/tasks"
    assert payload["input"] == {
        "prompt": "wait",
        "sleep_seconds": 120,
        "fail": True,
        "artifact_text": "hello world",
        "fail_message": "123",
    }


def test_swarm_dispatch_refuses_an_input_claude_code_does_not_declare():
    args = cli.build_parser().parse_args(
        ["dispatch", "x", "--profile", "claude-code", "--input", "model=opus"]
    )
    with pytest.raises(SwarmError) as caught:
        cli.cmd_dispatch(_Refusing(), args)
    assert "claude-code" in str(caught.value)


def test_the_dispatch_tool_sends_declared_inputs():
    recorder = _Recorder()

    server._call(recorder, "swarm_dispatch",
                 {"prompt": "wait", "profile": "mock", "inputs": {"sleep_seconds": 120}})

    (_, _, payload), = recorder.sent
    assert payload["input"] == {"prompt": "wait", "sleep_seconds": 120}


def test_the_dispatch_tool_refuses_undeclared_inputs_without_calling_the_api():
    with pytest.raises(SwarmError) as caught:
        server._call(_Refusing(), "swarm_dispatch",
                     {"prompt": "x", "profile": "mock", "inputs": {"image": "evil:latest"}})
    assert "image" in str(caught.value)


def test_the_workflow_tool_sends_a_steps_inputs():
    recorder = _Recorder()

    server._call(recorder, "swarm_workflow", {"steps": [
        {"step_id": "slow", "runner_profile": "mock", "prompt": "wait",
         "inputs": {"sleep_seconds": 120}},
    ]})

    (_, _, payload), = recorder.sent
    assert payload["steps"][0]["input"] == {"prompt": "wait", "sleep_seconds": 120}


def test_the_tool_schemas_offer_inputs_and_still_no_execution_detail():
    tools = {t["name"]: t["inputSchema"] for t in server.TOOLS}
    dispatch = tools["swarm_dispatch"]["properties"]
    step = tools["swarm_workflow"]["properties"]["steps"]["items"]["properties"]
    for where, properties in (("swarm_dispatch", dispatch), ("swarm_workflow step", step)):
        assert "inputs" in properties, f"{where} cannot send a runner's declared inputs"
        assert properties["inputs"]["type"] == "object"
        leaked = sorted({"image", "command", "args", "cpu", "memory", "backend", "input"} & set(properties))
        assert not leaked, f"{where} offers {leaked}"


# -- the one place, and what it is held to ----------------------------------------


def test_the_bridge_keeps_no_table_of_its_own():
    """THE MIRRORED COPY IS THE DEFECT. The bridge's `DECLARED_INPUTS` was the
    declaration until contract request 25 put it in the frozen catalogue, where
    the API reads it too. Kept, it would be a second answer to "what may a
    caller send", and the day the two disagreed the plugin would refuse what
    the API accepts, or send what the API refuses."""
    assert not hasattr(catalogue, "DECLARED_INPUTS"), (
        "swarm_mcp.profiles still carries its own input table; the frozen "
        "catalogue's RunnerProfile.inputs is the one declaration"
    )
    assert not hasattr(catalogue, "InputSpec"), (
        "swarm_mcp.profiles still defines its own input type; "
        "swarm_common.profiles.RunnerInput is the one"
    )
    for name in RUNNER_PROFILES:
        assert catalogue.declared_inputs(name) == _declared(name), name
    assert _declared("mock"), "the mock declares no input; #142 is undone"


def test_the_declaring_profiles_and_no_declaration_names_execution_detail():
    """The owner's decisions on #142, on contract request 28 (#265, accepted
    2026-09-28) and on contract request 32 (#218): the mock declares its test
    knobs, `claude-code` and `codex` declare `issue` and only it, and
    `browser` and `generic` declare what their runners read. And no
    declaration, anywhere, names an image, a command, a resource spec, a
    backend, a model or a key the mock writes platform records with.

    ONE NAMED EXEMPTION, not a hole in the guard: `generic` declares
    `command`, the name of an entry in its runner's closed catalogue, whose
    argv the platform owns (request 32, Question 2). `command` stays in
    `_NEVER`; the exemption is `generic` alone, and only in the one shape
    `test_generic_command_is_exactly_the_runners_catalogue` holds it to.
    Every other profile declaring `command` is still flagged here."""
    declaring = sorted(name for name in RUNNER_PROFILES if _declared(name))
    assert declaring == [
        "browser", "claude-code", "claude-code-review", "codex", "generic",
        "indexer", "mock",
    ], declaring
    # claude-code-review is claude-code under its own Job (contract request 36);
    # indexer is claude-code on the indexer image (contract request 48).
    for name in ("claude-code", "claude-code-review", "codex", "indexer"):
        assert set(_declared(name)) == {"issue"}, (name, _declared(name))
    for name in RUNNER_PROFILES:
        named = set(_declared(name)) & set(_NEVER)
        if name == "generic":
            named -= {"command"}
        assert not named, f"{name} declares {sorted(named)}"


def test_no_profile_is_left_undeclared():
    """Request 32 retired the `inputs is None` amendment to request 25: every
    profile's `inputs` is a mapping, and `{}` is the declaration "the prompt
    and nothing else"."""
    undeclared = sorted(name for name, p in RUNNER_PROFILES.items() if p.inputs is None)
    assert not undeclared, f"{undeclared} still declare no inputs at all"


def test_the_catalogue_refuses_command_on_any_other_profile_or_in_any_other_shape():
    """`RunnerProfile.__post_init__` enforces the exemption at construction,
    so a declaration that widens it cannot even be built."""
    import dataclasses

    from swarm_common.profiles import RunnerInput

    commands = tuple(sorted(_generic_commands()))
    for name in ("mock", "claude-code", "browser"):
        with pytest.raises(ValueError):
            dataclasses.replace(
                RUNNER_PROFILES[name],
                inputs={"command": RunnerInput("string", choices=commands)},
            )
    generic = RUNNER_PROFILES["generic"]
    for wrong in (
        RunnerInput("string"),
        RunnerInput("argument"),
        RunnerInput("string", choices=commands[:-1]),
        RunnerInput("string", choices=(*commands, "sh")),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(generic, inputs={**generic.inputs, "command": wrong})


def test_every_declared_input_is_one_its_runner_reads():
    """A declared key the runner never reads is accepted and then ignored, which
    is the silent drop the refusals above exist to prevent. The runner is found
    from the catalogue's own `runner_argv` (`python -m agent_worker.runners.X`),
    and its source is read, never imported."""
    checked = 0
    for name in RUNNER_PROFILES:
        declared = _declared(name)
        if not declared:
            continue
        module = RUNNER_PROFILES[name].runner_argv[-1]
        path = _REPO / "apps" / "agent-worker" / (module.replace(".", "/") + ".py")
        source = path.read_text()
        if "run_cli_agent(" in source:
            # claude-code and codex hand their payload to the one CLI runner,
            # which is where every key of theirs is read.
            source += (path.parent / "cliagent.py").read_text()
        # The generic runner hands its whole payload to `resolve_limits`,
        # which reads the four limits by name (`runners/limits.py`).
        limits = _limit_keys() if "resolve_limits(payload" in source else set()
        for key in declared:
            checked += 1
            assert key in limits or re.search(
                rf"""payload(\.get\(|\[)\s*["']{re.escape(key)}["']""", source
            ), f"{name} declares {key!r}, which {path.relative_to(_REPO)} never reads"
    assert checked, "no declared input was checked; the loop ran over nothing"


def test_the_profiles_view_says_what_each_profile_takes():
    """A caller learns the inputs from `swarm_profiles` / `swarm profiles`, not
    from a sentence of prose that can go stale."""
    entries = {entry["name"]: entry for entry in catalogue.catalogue()}
    assert set(entries["mock"]["inputs"]) == set(_declared("mock"))
    assert set(entries["claude-code"]["inputs"]) == {"issue"}


def test_swarm_profiles_prints_the_declared_inputs(capsys):
    assert cli.cmd_profiles(None, argparse.Namespace(json=False)) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "sleep_seconds" in printed, printed
    assert "quota_exhausted" in printed, printed


# -- the bounded park (owner decision on #142, 2026-09-25) --------------------------


def test_the_park_knobs_are_declared_now_that_the_park_is_bounded():
    """Withheld by the review of PR #201 because the mock raised its rate limit
    on EVERY attempt and a park does not spend one, so a step sent
    `{"quota_exhausted": true}` re-leased and re-parked until `swarm cancel`.
    The mock now parks the task's first attempt only, by the `attempt_count`
    admission writes in the lease's own transaction
    (tests/unit/worker/test_mock_bounded_park.py proves the bound holds even
    when the park's checkpoint fails), so the owner declared both."""
    assert catalogue.check_inputs(
        "mock", {"quota_exhausted": True, "retry_after_seconds": 60}
    ) == {"quota_exhausted": True, "retry_after_seconds": 60}


@pytest.mark.parametrize("value", [1, 46, 3600, 90.0])
def test_retry_after_seconds_takes_one_second_to_one_hour(value):
    out = catalogue.check_inputs("mock", {"retry_after_seconds": value})
    assert out == {"retry_after_seconds": int(value)}
    assert isinstance(out["retry_after_seconds"], int)


def test_the_park_knobs_travel_on_a_dispatch():
    recorder = _Recorder()
    args = cli.build_parser().parse_args(
        ["dispatch", "park", "--profile", "mock",
         "--input", "quota_exhausted=true",
         "--input", "retry_after_seconds=120"]
    )

    assert cli.cmd_dispatch(recorder, args) == cli.EXIT_OK

    (_, _, payload), = recorder.sent
    assert payload["input"] == {
        "prompt": "park", "quota_exhausted": True, "retry_after_seconds": 120,
    }


def _worker_exit_codes() -> dict[str, int]:
    """`EXIT_*` in agent_worker/runners/base.py: the codes the worker reads a
    runner's exit by. Read from the source, as the runner is above."""
    path = _REPO / "apps" / "agent-worker" / "agent_worker" / "runners" / "base.py"
    codes: dict[str, int] = {}
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("EXIT_")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            codes[node.targets[0].id] = node.value.value
    return codes


def _read_as_something_else() -> list[tuple[str, int]]:
    """Every exit code the worker reads as anything but a plain failure."""
    codes = _worker_exit_codes()
    assert codes.get("EXIT_FAILED") == 1, codes
    return sorted((name, code) for name, code in codes.items() if name != "EXIT_FAILED")


def test_the_worker_still_reads_the_codes_this_file_is_about():
    """Guards the parametrisation below against reading nothing."""
    names = {name for name, _ in _read_as_something_else()}
    expected = {"EXIT_OK", "EXIT_QUOTA_EXHAUSTED", "EXIT_CREDENTIAL_REVOKED", "EXIT_TERMINATED"}
    assert expected <= names, names


@pytest.mark.parametrize("name,code", _read_as_something_else())
def test_a_failure_cannot_exit_with_a_code_the_worker_reads_as_something_else(name, code):
    """`fail` with `exit_code` exits the mock with that code, and the worker
    decides what the attempt WAS from it: 77 is a rate limit -- with no
    quota.json it invents one for provider "unknown" and parks, the same loop
    as above -- 143 records the task CANCELLED, 78 is the refused-credential
    code, and 0, beside the result.json the mock writes, records SUCCEEDED. A
    failure on purpose that reads as any of those is not a failure."""
    with pytest.raises(SwarmError) as caught:
        catalogue.check_inputs("mock", {"fail": True, "exit_code": code})
    message = str(caught.value)
    assert "exit_code" in message, f"{name}: {message}"
    assert str(code) in message, f"{name}: {message}"


@pytest.mark.parametrize("code", [1, 2, 42, 76, 79, 142, 255])
def test_a_failure_may_still_exit_with_an_ordinary_code(code):
    assert catalogue.check_inputs("mock", {"fail": True, "exit_code": code}) == {
        "exit_code": code,
        "fail": True,
    }


#: `{"key": ...}` in an example, and `--input key=...` (lowercase, so the
#: usage line's `--input KEY=VALUE` metavar is not read as a key).
_EXAMPLE_KEY = re.compile(r"""\{\s*\\?["'](?P<a>\w+)\\?["']\s*:|--input\s+(?P<b>[a-z_]+)=""")


def _examples(text: str) -> set[str]:
    return {m["a"] or m["b"] for m in _EXAMPLE_KEY.finditer(text)}


def _subcommand(name: str) -> argparse.ArgumentParser:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("swarm has no subcommands")


def test_every_input_the_bridge_or_the_skill_shows_is_one_that_is_declared():
    """During #201 the skill said `{"quota_exhausted": true}` parks a mock
    step while the key was not declared, and the PR said so too. An example is
    what a model copies, so each one names a key `mock` declares -- read from
    the texts a session actually sees: the delegate skill's `inputs`
    paragraph, the tool schema, the CLI's help."""
    skill = (_REPO / "plugin" / "skills" / "delegate" / "SKILL.md").read_text()
    paragraph = [p for p in re.split(r"\n\s*\n", skill) if p.startswith("`inputs`")]
    assert len(paragraph) == 1, "the delegate skill's `inputs` paragraph moved"
    # Sentences end at a full stop before a capital, so `e.g. {...}` stays whole.
    epilog = [s for s in re.split(r"\.\s+(?=[A-Z])", cli._workflow_epilog()) if "`inputs`" in s]
    assert len(epilog) == 1, "the workflow help's `inputs` sentence moved"
    flag = [a for a in _subcommand("dispatch")._actions if a.dest == "inputs"]
    assert len(flag) == 1, "swarm dispatch has no --input"
    texts = {
        "delegate SKILL.md": paragraph[0],
        "the inputs schema": server._INPUTS_SCHEMA["description"],
        "swarm workflow --help": epilog[0],
        "swarm dispatch --input's help": flag[0].help,
    }
    for where, text in texts.items():
        shown = _examples(text)
        assert shown, f"{where} shows no example any more; this test reads nothing there"
        undeclared = sorted(shown - set(_declared("mock")))
        assert not undeclared, f"{where} shows {undeclared} as inputs, which mock does not declare"


# -- browser and generic declare (contract request 32, #218) ---------------------


def test_the_bridge_sends_a_browser_task_its_url_and_actions():
    """#220: every browser task dispatched through the plugin failed after it
    started, with "browser runner needs input.url or at least one action",
    because the bridge sends only what a declaration names and `browser`
    declared nothing. It declares now, so the bridge sends them, unchanged."""
    recorder = _Recorder()
    actions = [{"type": "goto", "url": "https://example.com/a"}, {"type": "screenshot", "name": "a.png"}]

    server._call(recorder, "swarm_dispatch", {
        "prompt": "look", "profile": "browser",
        "inputs": {"url": "https://example.com", "actions": actions},
    })

    (_, _, payload), = recorder.sent
    assert payload["input"] == {"prompt": "look", "url": "https://example.com", "actions": actions}


def test_swarm_dispatch_keeps_a_url_and_an_argument_as_typed_text():
    """Question 3 of request 32: `--input target=123` is the make target
    "123", not the number 123 -- which the `argument` kind would refuse."""
    recorder = _Recorder()
    args = cli.build_parser().parse_args(
        ["dispatch", "build", "--profile", "generic",
         "--input", "command=make",
         "--input", "target=123",
         "--input", "working_directory=2024",
         "--input", "timeout_seconds=60"]
    )

    assert cli.cmd_dispatch(recorder, args) == cli.EXIT_OK

    (_, _, payload), = recorder.sent
    assert payload["input"] == {
        "prompt": "build", "command": "make", "target": "123",
        "working_directory": "2024", "timeout_seconds": 60,
    }


def test_swarm_dispatch_reads_a_list_input_as_json():
    recorder = _Recorder()
    args = cli.build_parser().parse_args(
        ["dispatch", "look", "--profile", "browser",
         "--input", "url=https://example.com",
         "--input", 'actions=[{"type":"screenshot"}]']
    )

    assert cli.cmd_dispatch(recorder, args) == cli.EXIT_OK

    (_, _, payload), = recorder.sent
    assert payload["input"] == {
        "prompt": "look", "url": "https://example.com", "actions": [{"type": "screenshot"}],
    }


@pytest.mark.parametrize(
    "profile,inputs,named",
    [
        ("generic", {"paths": ["tests"]}, "command"),
        ("generic", {"command": "sh"}, "command"),
        ("generic", {"command": "pytest", "argv": ["-x"]}, "argv"),
        ("generic", {"command": "make", "target": "-rf"}, "target"),
        ("generic", {"command": "make", "target": "all\n"}, "target"),
        ("generic", {"command": "pytest", "timeout_seconds": 0}, "timeout_seconds"),
        ("browser", {"url": "http://169.254.169.254/computeMetadata/v1/"}, "url"),
        ("browser", {"actions": [{"type": "goto", "url": "http://10.0.0.1/"}]}, "actions[0].url"),
        ("browser", {"actions": [{"type": "click"}]}, "actions[0]"),
        ("browser", {"actions": [{"type": "Goto", "url": "https://example.com"}]}, "actions[0]"),
        ("browser", {"actions": [{"type": "wait", "seconds": 600}]}, "actions[0].seconds"),
        ("browser", {"timeout_ms": 0}, "timeout_ms"),
        ("browser", {"user_agent": "x\r\nX-Injected: 1"}, "user_agent"),
    ],
)
def test_the_bridge_refuses_what_the_declaration_refuses_before_it_travels(profile, inputs, named):
    """Each of these was admitted, leased and started before request 32, and
    failed (or ran something else) inside the pod. Refused here, it costs
    nothing and never reaches the API."""
    with pytest.raises(SwarmError) as caught:
        server._call(_Refusing(), "swarm_dispatch", {"prompt": "x", "profile": profile, "inputs": inputs})
    assert named in str(caught.value), str(caught.value)


def test_a_generic_dispatch_with_no_inputs_is_refused_for_its_missing_command():
    """`command` is required. No `inputs` at all is not a way around that."""
    with pytest.raises(SwarmError) as caught:
        server._call(_Refusing(), "swarm_dispatch", {"prompt": "x", "profile": "generic"})
    message = str(caught.value)
    assert "command" in message, message
    assert "does not declare" not in message, "a missing required key is not an undeclared one"


# -- the restatements request 32 creates, held to the runners' source ----------
#
# The frozen catalogue cannot import the worker, so each number and name
# below is a copy -- the same kind of copy as the mock's exit codes, and held
# the same way: the runner's source is READ, never imported.

_RUNNERS = _REPO / "apps" / "agent-worker" / "agent_worker" / "runners"
_CONFIG = _REPO / "apps" / "agent-worker" / "agent_worker" / "config.py"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _constant(node: ast.AST) -> object:
    """A literal, or a `*`/`+` of literals such as `32 * 1024 * 1024`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _constant(node.left), _constant(node.right)
        return left * right if isinstance(node.op, ast.Mult) else left + right
    return ast.literal_eval(node)


def _assigned(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    raise AssertionError(f"no module-level {name} to compare against")


def _generic_commands() -> set[str]:
    """The keys of `GENERIC_COMMANDS` in runners/generic.py."""
    table = _assigned(_module(_RUNNERS / "generic.py"), "GENERIC_COMMANDS")
    assert isinstance(table, ast.Dict), "GENERIC_COMMANDS is no longer a dict literal"
    keys = {ast.literal_eval(key) for key in table.keys}
    assert keys, "read no command from GENERIC_COMMANDS"
    return keys


def _class_defaults(path: Path, class_name: str) -> dict[str, object]:
    """A class's field defaults that are literals; any other default is left out."""
    for node in _module(path).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            defaults: dict[str, object] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value:
                    try:
                        defaults[item.target.id] = _constant(item.value)
                    except (ValueError, TypeError):
                        continue
            return defaults
    raise AssertionError(f"{path.name} has no class {class_name}")


def _limit_keys() -> set[str]:
    """The caller-lowerable limits `resolve_limits` reads (runners/limits.py)."""
    body = re.search(
        r"def resolve_limits\(.*?\n    return ", (_RUNNERS / "limits.py").read_text(), re.S
    )
    assert body, "limits.py has no resolve_limits"
    keys = set(re.findall(r'"([a-z_]+)":\s*(?:float\()?ceilings\.', body.group(0)))
    assert keys == {"timeout_seconds", "grace_seconds", "max_stdout_bytes", "max_stderr_bytes"}, keys
    return keys


def test_generic_command_is_exactly_the_runners_catalogue():
    command = _declared("generic")["command"]
    assert command.kind == "string" and command.required, command
    assert set(command.choices) == _generic_commands(), (
        f"generic's command offers {sorted(command.choices)}; the runner's catalogue is "
        f"{sorted(_generic_commands())}"
    )


def test_the_argument_kind_is_the_generic_runners_argument_rule():
    """`_ARGUMENT` in the catalogue restates `_ARGUMENT_SAFE` in the runner."""
    from swarm_common import profiles

    call = _assigned(_module(_RUNNERS / "generic.py"), "_ARGUMENT_SAFE")
    assert isinstance(call, ast.Call) and call.args, "_ARGUMENT_SAFE is no longer re.compile(...)"
    runner = ast.literal_eval(call.args[0])
    # The runner anchors with ^...$ and must use fullmatch; the catalogue uses
    # fullmatch unanchored. The rule between the anchors is the one compared.
    assert runner.removeprefix("^").removesuffix("$") == profiles._ARGUMENT.pattern, (
        runner, profiles._ARGUMENT.pattern,
    )


def test_the_list_bounds_are_the_runners():
    tree = _module(_RUNNERS / "browser.py")
    assert _declared("browser")["actions"].maximum == _constant(_assigned(tree, "MAX_ACTIONS"))
    defaults = _class_defaults(_RUNNERS / "generic.py", "GenericCommand")
    assert _declared("generic")["paths"].maximum == defaults["max_arguments"]
    # `paths` is read for `pytest` only, which must keep the default bound.
    table = _assigned(_module(_RUNNERS / "generic.py"), "GENERIC_COMMANDS")
    pytest_entry = next(v for k, v in zip(table.keys, table.values) if ast.literal_eval(k) == "pytest")
    assert not any(kw.arg == "max_arguments" for kw in pytest_entry.keywords), (
        "GENERIC_COMMANDS['pytest'] sets its own max_arguments; the catalogue's bound is the default's"
    )


def _action_branches() -> dict[str, ast.If]:
    """Each `kind == "<type>"` branch of `body()` in runners/browser.py."""
    branches: dict[str, ast.If] = {}
    for node in ast.walk(_module(_RUNNERS / "browser.py")):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "kind"
            and isinstance(node.test.ops[0], ast.Eq)
            and isinstance(node.test.comparators[0], ast.Constant)
        ):
            branches[node.test.comparators[0].value] = node
    assert len(branches) == 8, sorted(branches)
    return branches


def _fields_read(branch: ast.If) -> set[str]:
    """`action["x"]` and `action.get("x", ...)` inside one branch's own body."""
    fields: set[str] = set()
    for statement in branch.body:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "action"
                and isinstance(node.slice, ast.Constant)
            ):
                fields.add(node.slice.value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "action"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                fields.add(node.args[0].value)
    return fields


def test_the_eight_action_shapes_are_the_runners():
    """Each declared shape takes exactly the fields its branch of `body()`
    reads, and the eight `type`s are the branches."""
    variants = _declared("browser")["actions"].items.variants
    branches = _action_branches()
    assert set(variants) == set(branches), (sorted(variants), sorted(branches))
    for shape, branch in branches.items():
        assert set(variants[shape]) == _fields_read(branch), (
            f"the {shape!r} action declares {sorted(variants[shape])}; the runner reads "
            f"{sorted(_fields_read(branch))}"
        )


def test_the_wait_ceiling_is_the_runners_clamp():
    clamp = [
        node.args[1].value
        for node in ast.walk(_action_branches()["wait"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "min"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    ]
    assert clamp, "the wait branch no longer clamps with min(..., N)"
    seconds = _declared("browser")["actions"].items.variants["wait"]["seconds"]
    assert seconds.maximum == clamp[0], (seconds.maximum, clamp)


def test_the_generic_limit_ceilings_are_what_the_worker_exports():
    """Each of the four ceilings is the value the worker exports by default,
    so a caller may only lower the platform's own."""
    declared = _declared("generic")
    config = _class_defaults(_CONFIG, "WorkerConfig")
    assert set(declared) >= _limit_keys()
    assert declared["timeout_seconds"].maximum == RUNNER_PROFILES["generic"].timeout_seconds
    assert declared["grace_seconds"].maximum == config["termination_grace_seconds"]
    assert declared["max_stdout_bytes"].maximum == config["max_stdout_bytes"]
    assert declared["max_stderr_bytes"].maximum == config["max_stderr_bytes"]
    for key in _limit_keys():
        assert declared[key].minimum == 1, f"{key}: the runner reads 0 or less as not asked"


def _inputs_paragraphs() -> dict[str, str]:
    """The paragraph each of the plugin's two texts gives to runner inputs."""
    skill = (_REPO / "plugin" / "skills" / "delegate" / "SKILL.md").read_text()
    readme = (_REPO / "plugin" / "README.md").read_text()
    found = {
        "delegate SKILL.md": [p for p in re.split(r"\n\s*\n", skill) if p.startswith("`inputs`")],
        "plugin/README.md": [
            p for p in re.split(r"\n\s*\n", readme) if p.startswith("**Beside the prompt")
        ],
    }
    for where, paragraphs in found.items():
        assert len(paragraphs) == 1, f"{where}'s runner-inputs paragraph moved"
    return {where: " ".join(paragraphs[0].split()) for where, paragraphs in found.items()}


def test_the_plugin_says_every_profile_declares():
    """The review of #213 held both texts to naming every profile that had
    not declared yet and saying the API bounded it by size alone. Request 32
    left none, so neither text may still say a profile has not declared, and
    both name `browser` and `generic` among the ones that do."""
    for where, text in _inputs_paragraphs().items():
        assert "not declared yet" not in text and "declared their inputs yet" not in text, (
            f"{where} still says a profile has not declared its inputs; none is left"
        )
        for name in ("browser", "generic"):
            assert f"`{name}`" in text, f"{where} does not say what {name} declares"
