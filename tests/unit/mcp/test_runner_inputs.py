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
nothing -- or something else -- to another: `input.model` IS read by the CLI
runners and would select a model (test_model_flag_is_attribution_only.py). So
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


def test_a_profile_that_declares_no_inputs_refuses_them():
    """`claude-code` reads `input.model`; letting a caller set it is the
    contract change invariant 10 forbids. It declares nothing, so nothing goes."""
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "a", "runner_profile": "claude-code", "prompt": "x",
              "inputs": {"sleep_seconds": 5}}]
        )
    message = str(caught.value)
    assert "claude-code" in message, message
    assert "mock" in message, "the refusal names the profiles that do declare inputs"


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


def test_swarm_dispatch_refuses_an_input_for_a_profile_that_declares_none():
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


def test_only_the_mock_declares_inputs_and_no_declaration_names_execution_detail():
    """The owner's decision on #142: the mock declares its test knobs and every
    other profile declares none (or, for the runners whose work IS their input,
    has not declared yet). And no declaration, anywhere, names an image, a
    command, a resource spec, a backend, a model or a key the mock writes
    platform records with."""
    declaring = sorted(name for name in RUNNER_PROFILES if _declared(name))
    assert declaring == ["mock"], declaring
    for name in RUNNER_PROFILES:
        named = sorted(set(_declared(name)) & set(_NEVER))
        assert not named, f"{name} declares {named}"


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
        for key in declared:
            checked += 1
            assert re.search(rf"""payload(\.get\(|\[)\s*["']{re.escape(key)}["']""", source), (
                f"{name} declares {key!r}, which {path.relative_to(_REPO)} never reads"
            )
    assert checked, "no declared input was checked; the loop ran over nothing"


def test_the_profiles_view_says_what_each_profile_takes():
    """A caller learns the inputs from `swarm_profiles` / `swarm profiles`, not
    from a sentence of prose that can go stale."""
    entries = {entry["name"]: entry for entry in catalogue.catalogue()}
    assert set(entries["mock"]["inputs"]) == set(_declared("mock"))
    assert "inputs" not in entries["claude-code"] or not entries["claude-code"]["inputs"]


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


# -- the profiles whose inputs are not declared yet (#218) ------------------------

#: The profiles the frozen catalogue has not declared inputs for yet
#: (`inputs is None`). What they should declare is open with the owner on #218.
_UNDECLARED = sorted(name for name, p in RUNNER_PROFILES.items() if p.inputs is None)


@pytest.mark.parametrize("name", _UNDECLARED or ["<none>"])
def test_the_bridge_sends_nothing_to_a_profile_that_has_not_declared_yet(name):
    """THE BRIDGE'S OWN SEND POLICY, not a second copy of the rule. The API
    bounds what a caller sends these by size alone; the bridge sends them
    nothing, because it sends only what a declaration names and neither has
    one. Letting `swarm_dispatch` send a browser task's `actions` would be a
    new plugin capability, which is #218's third question for the owner."""
    if name == "<none>":
        pytest.skip("every profile declares its inputs now; #218 is settled")
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps(
            [{"step_id": "a", "runner_profile": name, "prompt": "x",
              "inputs": {"url": "https://example.com"}}]
        )
    message = str(caught.value)
    assert name in message, message
    assert "yet" in message, "the refusal says the profile has not declared, not that it takes nothing"


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


def test_the_plugin_says_which_profiles_the_api_bounds_by_size_alone():
    """The review of #213: the delegate skill said an undeclared key is refused
    "by the bridge, and by the API for every other caller", and the README that
    "the API refuses an undeclared key from every caller" and that "every other
    profile declares none and takes none". For `browser` and `generic`, whose
    inputs are not declared yet, the API takes any key under the size limit, so
    an operator who read either believed those two were policed. Each text
    names every profile not declared yet and says it is bounded by size; the
    claims the review quoted do not come back while one exists."""
    for where, text in _inputs_paragraphs().items():
        for name in _UNDECLARED:
            assert f"`{name}`" in text, (
                f"{where} does not name {name}, whose inputs are not declared yet"
            )
        if _UNDECLARED:
            assert "size" in text, f"{where} does not say what bounds {_UNDECLARED}"
            for claim in (
                "for every other caller",
                "from every caller",
                "Every other profile declares none",
            ):
                assert claim not in text, (
                    f"{where} says {claim!r}, which is false for {_UNDECLARED}"
                )
        else:
            assert "not declared yet" not in text, (
                f"{where} still says a profile has not declared its inputs; none is left"
            )
