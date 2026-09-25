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
the gate is per PROFILE, by name, in one place (`swarm_mcp.profiles`), until
the frozen catalogue can declare a profile's inputs itself (contract request
25). A profile that declares none takes none.

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
#: a simulated credential refusal.
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
    "prompt",
)


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
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_before_it_travels(key, value):
    """The runner would coerce or crash after admission; refused here it costs
    nothing. A bool is not a number, whatever Python thinks."""
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


def test_every_profile_that_declares_inputs_is_in_the_frozen_catalogue():
    """The gate is by NAME, so a renamed profile would keep a declaration that
    admits nothing and a new one would silently take none."""
    stray = sorted(set(catalogue.DECLARED_INPUTS) - set(RUNNER_PROFILES))
    assert not stray, f"inputs are declared for profiles the catalogue does not hold: {stray}"
    assert catalogue.DECLARED_INPUTS, "no profile declares an input; #142 is undone"


def test_every_declared_input_is_one_its_runner_reads():
    """A declared key the runner never reads is accepted and then ignored, which
    is the silent drop the refusals above exist to prevent. The runner is found
    from the catalogue's own `runner_argv` (`python -m agent_worker.runners.X`),
    and its source is read, never imported."""
    checked = 0
    for name, declared in catalogue.DECLARED_INPUTS.items():
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
    assert set(entries["mock"]["inputs"]) == set(catalogue.DECLARED_INPUTS["mock"])
    assert "inputs" not in entries["claude-code"] or not entries["claude-code"]["inputs"]


def test_swarm_profiles_prints_the_declared_inputs(capsys):
    assert cli.cmd_profiles(None, argparse.Namespace(json=False)) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "sleep_seconds" in printed, printed


# -- review of PR #201: no declared input can make a task that never ends ----------


def _runner_source(name: str) -> tuple[Path, str]:
    module = RUNNER_PROFILES[name].runner_argv[-1]
    path = _REPO / "apps" / "agent-worker" / (module.replace(".", "/") + ".py")
    return path, path.read_text()


def _payload_keys(node: ast.AST) -> set[str]:
    """Every `payload.get("k")` / `payload["k"]` key read anywhere under `node`."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "payload"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
        ):
            keys.add(sub.args[0].value)
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "payload"
            and isinstance(sub.slice, ast.Constant)
        ):
            keys.add(sub.slice.value)
    return keys


def _raises(node: ast.If, signal: str) -> bool:
    """Does this `if`'s body raise `signal(...)`?"""
    return any(
        isinstance(sub, ast.Raise)
        and isinstance(sub.exc, ast.Call)
        and isinstance(sub.exc.func, ast.Name)
        and sub.exc.func.id == signal
        for stmt in node.body
        for sub in ast.walk(stmt)
    )


def test_no_declared_input_reaches_the_runners_rate_limit_signal():
    """`quota_exhausted` was declared, and the mock raises its rate limit on
    EVERY attempt: the check runs before any saved state and keeps no count,
    unlike `credential_revoked_times`. A park does not spend an attempt --
    neither `control.park` nor the scheduler's promotion reads
    `retries_exhausted` -- so the task re-leased and re-parked, a new Cloud Run
    execution each time, until someone ran `swarm cancel`. The delegate skill
    told the model `{"quota_exhausted": true}` "parks it", as if once.

    So nothing a caller may send reaches `QuotaExhaustedSignal`: no key read in
    the condition that raises it, nor in the signal it raises (its provider,
    its retry-after). Read from the runner's own source, never imported."""
    checked = 0
    for name, declared in catalogue.DECLARED_INPUTS.items():
        path, source = _runner_source(name)
        branches = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.If) and _raises(node, "QuotaExhaustedSignal")
        ]
        if name == "mock":
            assert branches, (
                f"{path.relative_to(_REPO)} no longer raises a rate limit; re-read this test"
            )
        for branch in branches:
            checked += 1
            reaches = sorted(_payload_keys(branch) & set(declared))
            assert not reaches, (
                f"{name} declares {reaches}, which reach the runner's rate limit at "
                f"{path.relative_to(_REPO)}:{branch.lineno} -- a task that parks on every "
                "attempt and never ends"
            )
    assert checked, "no rate-limit branch was checked; the loop ran over nothing"


@pytest.mark.parametrize("key,value", [("quota_exhausted", True), ("retry_after_seconds", 46)])
def test_the_rate_limit_knobs_are_refused_by_name(key, value):
    """The two keys the finding's session sent. Refused, never dropped."""
    with pytest.raises(SwarmError) as caught:
        catalogue.check_inputs("mock", {key: value})
    assert key in str(caught.value), caught.value


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
    """The skill said `{"quota_exhausted": true}` parks a mock step, and the PR
    said so too. An example is what a model copies, so each one names a key
    `mock` declares -- read from the texts a session actually sees: the
    delegate skill's `inputs` paragraph, the tool schema, the CLI's help."""
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
        undeclared = sorted(shown - set(catalogue.DECLARED_INPUTS["mock"]))
        assert not undeclared, f"{where} shows {undeclared} as inputs, which mock does not declare"
