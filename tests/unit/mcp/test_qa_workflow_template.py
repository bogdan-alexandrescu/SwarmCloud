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
* `test_template_report_reads_every_review_and_the_a11y_pass` and
  `test_flow_report_reads_every_flow_and_nothing_else`: the screen findings
  and the flows are two reports (QA-TEMPLATE-SPLIT, part of #1680). Visual QA
  run 2 (`wf_b6e2441b419f423a84eb`, 2026-10-20) lost its whole report to one
  flow: `flow-submit-form` was killed at its timeout, the platform cancels
  every dependant of a failed step, and `report` depended on all ten review,
  flow and a11y steps -- so every finished screen review went unreported.
  `report` now waits only for the reviews and the a11y pass, `flow-report`
  only for the flows; each stages each tarball under the name its producer
  writes, every flow records its GIF with `swarm-handoff record`, the command
  QA-FLOWS shipped, and no step's output goes nowhere.
* `test_flow_submit_form_ends_inside_its_timeout`: run 2's flow navigated
  while its recorder was connecting, the navigation timed out, and one
  `browser_tabs` call then hung for 1800 s and a second until the kill. The
  brief opens the page before recording, stops calling browser tools after one
  times out, caps its screenshots, and carries a budget that ends it with time
  to bundle.
* `test_flow_briefs_name_only_pages_in_the_route_list`: a flow brief names
  each page by its path in `routes.json`, never by a nav trail (QA-038: the
  first run's brief said "Work > Submit a task", a trail the console does not
  draw -- Submit is its own rail item).
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
#: The flows' own report: a flow that fails costs this, never `report`.
FLOW_REPORT = "flow-report"
REPORTS = (REPORT, FLOW_REPORT)
CAPTURE = "capture-"
REVIEW = "review-"
FLOW = "flow-"
#: The step whose tarball only the screen report reads besides the reviews.
A11Y = "a11y-"
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


def _flows(steps: dict[str, dict]) -> list[str]:
    return sorted(s for s in steps if s.startswith(FLOW) and s not in REPORTS)


def test_template_report_reads_every_review_and_the_a11y_pass():
    steps = _steps()
    report = steps[REPORT]
    fed = sorted(s for s in steps if s.startswith(REVIEW) or s.startswith(A11Y))
    # MUTATION: add "flow-submit-form" back to report's depends_on and
    # input_from (run 2's shape, wf_b6e2441b419f423a84eb) and this fails:
    # one killed flow cancelled the report over six finished reviews.
    assert sorted(report["depends_on"]) == fed, "the screen report waits for the reviews and the a11y pass only"
    assert report["input_from"] == {s: f"{s}.tar" for s in fed}, "and stages each one's tarball"
    assert not set(report["depends_on"]) & set(_flows(steps)), "a failed flow must not cancel the screen findings"
    for step_id in fed:
        # The name staged is the name the producer is told to write.
        assert f"{step_id}.tar under $SWARM_ARTIFACTS_DIR" in steps[step_id]["prompt"], step_id
    prompt = report["prompt"]
    assert "report.md" in prompt and "findings.json" in prompt
    # MUTATION: put "then one section per flow with its GIFs embedded" back in
    # report's prompt and this fails: the flows are flow-report's to write.
    assert ".gif" not in prompt.lower() and "flow-submit-form" not in prompt, (
        "the screen report writes no flow section; flow-report does"
    )
    assert FLOW_REPORT in prompt, "report.md says where the flows went"


def test_flow_report_reads_every_flow_and_nothing_else():
    steps = _steps()
    # MUTATION: delete the flow-report step and this fails on the lookup.
    flow_report = steps[FLOW_REPORT]
    flows = _flows(steps)
    assert len(flows) == 3, flows
    # MUTATION: add "review-task" to flow-report's depends_on and this fails:
    # a flows report that waits on a review is cancelled by its failure too.
    assert sorted(flow_report["depends_on"]) == flows, "the flows report waits for the three flows only"
    assert flow_report["input_from"] == {s: f"{s}.tar" for s in flows}, "and stages each one's tarball"
    assert flow_report["runner_profile"] == "claude-code", "it reads tarballs; it needs no browser"
    assert flow_report["model_class"] == "opus", "owner, 2026-10-19 (§7 Q1): opus for the report"
    for step_id in flows:
        prompt = steps[step_id]["prompt"]
        assert f"{step_id}.tar under $SWARM_ARTIFACTS_DIR" in prompt, step_id
        # QA-FLOWS' recorder: a GIF lands at $SWARM_ARTIFACTS_DIR/flows/<name>.gif.
        assert "swarm-handoff record start " in prompt and "swarm-handoff record stop" in prompt, step_id
        assert "flows/" in prompt and ".gif" in prompt, f"{step_id} does not bundle its GIF"
    prompt = flow_report["prompt"]
    # MUTATION: drop "whether its goal was reached" or the flow-submit-form
    # screenshots from flow-report's prompt and this fails.
    for part in ("flows.md", "GIF", "whether its goal was reached", "flow-submit-form screenshot",
                 "$SWARM_ARTIFACTS_DIR"):
        assert part in prompt, f"flow-report's prompt does not ask for {part!r}"

    # No step's output goes nowhere: every step but the two reports feeds a later one.
    consumed = {parent for s in steps.values() for parent in s.get("input_from", {})}
    assert consumed == set(steps) - set(REPORTS)


#: "Your budget is 35 minutes": the minute at which the flow stops and reports.
BUDGET = re.compile(r"Your budget is (\d+) minutes")
#: "at most 12 screenshots per preset".
SCREENSHOT_CAP = re.compile(r"at most (\d+) screenshots per preset")
#: Minutes a flow keeps after its budget to stop the recorder, write and bundle.
WRAP_UP_MINUTES = 10


def test_flow_submit_form_ends_inside_its_timeout():
    step = _steps()["flow-submit-form"]
    prompt = step["prompt"]
    # MUTATION: delete "Your budget is 35 minutes" (run 2's brief, killed at
    # 2700 s with nothing bundled) and this fails on the missing budget.
    budget = BUDGET.search(prompt)
    assert budget, "flow-submit-form carries no explicit budget"
    minutes = int(budget.group(1))
    assert minutes == 35, minutes
    assert (minutes + WRAP_UP_MINUTES) * 60 <= step["timeout_seconds"], (
        f"a {minutes}-minute budget leaves no {WRAP_UP_MINUTES} minutes to bundle inside the timeout"
    )
    assert "`date +%s`" in prompt, "the budget is measured from a recorded start"
    # MUTATION: move "swarm-handoff record start" before the first
    # browser_snapshot (run 2 started the recorder and navigated in the same
    # second; the navigation timed out) and this fails.
    assert prompt.index("browser_snapshot") < prompt.index("swarm-handoff record start"), (
        "the page is opened and seen before the recorder connects"
    )
    # MUTATION: delete the no-retry sentence (run 2 called browser_tabs after
    # its navigation timed out: 1800 s, then again until the kill) and this fails.
    assert "do not call it or any other browser tool again" in prompt
    assert "goal_reached" in prompt, "a stopped flow still says what it reached"
    # MUTATION: raise the cap to 40 and this fails.
    cap = SCREENSHOT_CAP.search(prompt)
    assert cap and int(cap.group(1)) <= 15, "the screenshots are capped per preset"


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


#: A nav trail in prose: "Work > Submit a task", "Capacity > Pools".
NAV_TRAIL = re.compile(r"\b[A-Z][\w-]*(?: [\w-]+)* > [A-Z]")
#: A page named by address: the fixture origin's paths, and a backticked `/path`.
NAMED_PATH = re.compile(re.escape(FIXTURE_ORIGIN) + r"(/[^\s`'\")]*)?|`(/[^`\s]+)`")


def test_flow_briefs_name_only_pages_in_the_route_list():
    # MUTATION: put "open Work > Submit a task" back in flow-submit-form's
    # prompt (the brief of the first run, QA-038) and this fails on the trail.
    routes = json.loads(ROUTES.read_text(encoding="utf-8"))
    paths = {r["path"] for group in routes["groups"].values() for r in group}
    steps = _steps()
    flows = _flows(steps)
    assert flows, "the template has no flow step"
    named_by_flow = {}
    for step_id in flows:
        prompt = steps[step_id]["prompt"]
        assert not NAV_TRAIL.search(prompt), (
            f"{step_id} names a nav trail, {NAV_TRAIL.search(prompt).group(0)!r}...: "
            "name the page by its path in routes.json"
        )
        named = {(a or b or "").rstrip(".,;:") for a, b in NAMED_PATH.findall(prompt)} - {"", "/"}
        assert named <= paths, f"{step_id} names {sorted(named - paths)}, which routes.json does not list"
        named_by_flow[step_id] = named
    # The scripted flow opens one page by name; the goal-driven ones are told
    # not to type addresses, so they name none.
    assert named_by_flow["flow-submit-form"] == {"/submit/task"}, named_by_flow
