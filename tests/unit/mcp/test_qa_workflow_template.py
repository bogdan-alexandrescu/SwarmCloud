"""The console visual-QA workflow template is a spec the platform takes (QA-TEMPLATE, part of #1649).

`docs/qa/console-qa.workflow.json` is the run docs/design/visual-qa.md §4.6
designed and docs/browser-qa.md tells a person to submit: six captures on the
live browser, six reviews on plain `claude-code`, three flows and an
accessibility pass, one report. It is a FILE nobody runs in CI, so the day it
stops being a spec is the day an operator submits it and gets a 422 -- or
worse, a run that sweeps four of six groups and reports a clean console. These
hold it to what reads it:

* `test_template_reads_as_a_spec`: the bridge's own file reader
  (`server._read_spec_file`, which `swarm_workflow_spec` and so
  `/sc:swarmcloud` use), then the body `workflows.submit` would send, read as
  swarm-api reads it: `WorkflowCreate`, the dispatch options, `validate_dag`
  at the API's 50-step limit, and per step the profile, its declared inputs,
  its model class and its timeout.
* `test_template_captures_every_route_group`: one capture and one review step
  per group in `apps/swarm-ui/qa/routes.json`, and nothing else, each capture
  command parsing with `swarm_qa`'s own argument parser and naming presets the
  gate has. A group added to the route list without a step here is a group no
  run looks at.
* `test_template_report_reads_every_review_and_flow`: the report stages every
  review's and flow's tarball, each under the name its producer writes; every
  flow records its GIF with `swarm-handoff record`, the command QA-FLOWS
  shipped; no step's output goes nowhere.
* `test_template_names_only_profiles_inputs_and_classes`: invariant 10. No
  `resource_class`, every `browser_tool` one of the declared choices, every
  `model_class` one of the frozen `MODEL_CLASSES` that its profile takes, and
  `jev` only against the fixture build (owner, 2026-10-19, §7 Q5).
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from agent_worker.livegate.presets import PRESETS
from swarm_api.schemas import WorkflowCreate
from swarm_api.service import SubmissionService
from swarm_api.validation import (
    resolve_dispatch_options,
    resolve_merge_choice,
    validate_dag,
    validate_model_class,
    validate_runner_input,
    validate_runner_profile,
    validate_timeout,
)
from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES
from swarm_mcp import profiles as catalogue
from swarm_mcp import server, workflows

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "docs" / "qa" / "console-qa.workflow.json"
ROUTES = REPO / "apps" / "swarm-ui" / "qa" / "routes.json"
SWARM_QA = REPO / "images" / "agent-runtime-browser" / "swarm_qa.py"

#: swarm-api's `core.max_workflow_steps` default; the design counted 17 of it.
MAX_STEPS = 50
REPORT = "report"
CAPTURE = "capture-"
REVIEW = "review-"
#: The steps whose tarball only the report reads: flows and the a11y pass.
REPORTED_ALONE = ("flow-", "a11y-")
#: Where the capture steps serve the console: `npm run dev` in apps/swarm-ui,
#: the fixture build (`USE_FIXTURES`, apps/swarm-ui/src/api.ts).
FIXTURE_ORIGIN = "http://localhost:5173"


def _template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _steps() -> dict[str, dict]:
    steps = {s["step_id"]: s for s in _template()["steps"]}
    assert len(steps) == len(_template()["steps"]), "two steps share a step_id"
    return steps


def _groups() -> list[str]:
    groups = list(json.loads(ROUTES.read_text(encoding="utf-8"))["groups"])
    assert groups, "routes.json lists no group"
    return groups


def _swarm_qa():
    spec = importlib.util.spec_from_file_location("swarm_qa_for_template", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capture_argv(prompt: str) -> list[str]:
    """The one `python -m swarm_qa ...` command a capture prompt tells the agent to run."""
    commands = re.findall(r"`python -m swarm_qa ([^`]+)`", prompt)
    assert len(commands) == 1, f"a capture step runs exactly one swarm_qa command; found {len(commands)}"
    return shlex.split(commands[0])


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        assert (method, path) == ("POST", "/v1/workflows")
        self.sent.append(payload)
        return {"workflow": {"workflow_id": "wf_template", "steps": []}, "dispatch": {"strategy": "collect"}}


def test_template_reads_as_a_spec():
    # The bridge's file reader, exactly as `swarm_workflow_spec` calls it.
    read = server._read_spec_file(str(TEMPLATE.relative_to(REPO)), base=REPO)
    document = read["spec"]

    # The body `swarm_workflow` would send for it.
    spec = workflows.read_spec(catalogue.spec_without_model_classes(document))
    steps = catalogue.with_model_classes(spec["steps"], document["steps"])
    recorder = _Recorder()
    workflows.submit(
        recorder, steps=steps, strategy=spec["strategy"], carrier=spec["carrier"],
        repository_url=spec["repository_url"], repository_ref=spec["repository_ref"],
        on_step_failure=spec["on_step_failure"], priority=spec["priority"], label=spec["label"],
        title=spec["title"], merge=spec["merge"], after=spec["after"],
        additional_repositories=spec["additional_repositories"],
    )
    (body,) = recorder.sent

    # And that body as swarm-api reads it.
    created = WorkflowCreate.model_validate(body)
    resolve_dispatch_options(strategy=created.strategy, carrier=created.carrier, scale="workflow",
                             repository_url=created.repository_url)
    assert resolve_merge_choice(created.metadata) == "off", "a QA run changes nothing and merges nothing"
    order = validate_dag(SubmissionService._step_specs(created), max_steps=MAX_STEPS)
    assert sorted(order) == sorted(_steps()), "validate_dag ordered a different set of steps"
    for step in created.steps:
        profile = validate_runner_profile(step.runner_profile)
        validate_runner_input(profile, step.input, step_id=step.step_id, repository_url=created.repository_url)
        assert validate_model_class(profile, step.model_class, step_id=step.step_id) == step.model_class
        assert validate_timeout(profile, step.timeout_seconds) == step.timeout_seconds
        assert step.allow_empty_diff is True, f"{step.step_id} changes no file, so an empty diff is its result"
    assert created.on_step_failure == "continue", "one failed flow must not cancel the captures"


def test_template_captures_every_route_group():
    steps = _steps()
    groups = _groups()
    captures = sorted(s for s in steps if s.startswith(CAPTURE))
    reviews = sorted(s for s in steps if s.startswith(REVIEW))
    assert captures == sorted(CAPTURE + g for g in groups), "one capture step per routes.json group, no other"
    assert reviews == sorted(REVIEW + g for g in groups), "one review step per routes.json group, no other"

    parser = _swarm_qa()._parser()
    for group in groups:
        capture, review = steps[CAPTURE + group], steps[REVIEW + group]
        args = parser.parse_args(_capture_argv(capture["prompt"]))
        assert args.command == "capture"
        assert args.group == group
        assert args.routes == str(ROUTES.relative_to(REPO))
        assert args.base == FIXTURE_ORIGIN, "captures run against the fixture build in the same pod"
        presets = args.presets.split(",")
        assert presets == ["desktop", "phone-390"], "every page at 1440 and at 390"
        assert set(presets) <= set(PRESETS), f"a preset the gate does not have: {set(presets) - set(PRESETS)}"
        assert args.themes.split(",") == ["light", "dark"]
        assert args.out == f"$SWARM_ARTIFACTS_DIR/{CAPTURE}{group}"
        assert args.bundle == f"$SWARM_ARTIFACTS_DIR/{CAPTURE}{group}.tar"
        # The capture runs the live browser, by a fixed script, on a cheap class.
        assert capture["runner_profile"] == "claude-code-browser"
        assert capture["inputs"]["browser_tool"] == "playwright"
        # Its review stages exactly that tarball, on the profile with no browser.
        assert review["depends_on"] == [CAPTURE + group]
        assert review["input_from"] == {CAPTURE + group: f"{CAPTURE}{group}.tar"}
        assert review["runner_profile"] == "claude-code"
        assert f"findings-{group}.json" in review["prompt"]
        assert f"{REVIEW}{group}.tar" in review["prompt"]


def test_template_report_reads_every_review_and_flow():
    steps = _steps()
    report = steps[REPORT]
    fed = sorted(s for s in steps if s.startswith(REVIEW) or s.startswith(REPORTED_ALONE))
    assert sorted(report["depends_on"]) == fed, "the report waits for every review, flow and a11y pass"
    assert report["input_from"] == {s: f"{s}.tar" for s in fed}, "and stages each one's tarball"
    for step_id in fed:
        # The name staged is the name the producer is told to write.
        assert f"{step_id}.tar under $SWARM_ARTIFACTS_DIR" in steps[step_id]["prompt"], step_id

    flows = [s for s in steps if s.startswith("flow-")]
    assert len(flows) == 3, flows
    for step_id in flows:
        prompt = steps[step_id]["prompt"]
        # QA-FLOWS' recorder: a GIF lands at $SWARM_ARTIFACTS_DIR/flows/<name>.gif.
        assert "swarm-handoff record start " in prompt and "swarm-handoff record stop" in prompt, step_id
        assert "flows/" in prompt and ".gif" in prompt, f"{step_id} does not bundle its GIF"

    # No step's output goes nowhere: every step but the report feeds a later one.
    consumed = {parent for s in steps.values() for parent in s.get("input_from", {})}
    assert consumed == set(steps) - {REPORT}


def test_template_names_only_profiles_inputs_and_classes():
    for step_id, step in _steps().items():
        assert "resource_class" not in step, f"{step_id}: each profile's own class is the right one"
        profile = RUNNER_PROFILES[step["runner_profile"]]
        inputs = step.get("inputs", {})
        assert set(inputs) <= set(profile.inputs), f"{step_id} sends an input {profile.name} does not declare"
        if "browser_tool" in inputs:
            assert inputs["browser_tool"] in profile.inputs["browser_tool"].choices, step_id
        if inputs.get("browser_tool") == "jev":
            # Owner, 2026-10-19 (§7 Q5): jev only on the fixture build.
            assert FIXTURE_ORIGIN in step["prompt"] and "fixture build" in step["prompt"], step_id
            assert "qa_identity" not in inputs, step_id
        assert step["model_class"] in MODEL_CLASSES, step_id
        assert step["model_class"] in profile.model_classes, f"{step_id}: {profile.name} cannot run it"


@pytest.mark.parametrize("key", ["image", "command", "resources", "model", "env"])
def test_template_carries_no_backend_parameter(key):
    document = _template()
    assert key not in document
    assert all(key not in step for step in document["steps"])
