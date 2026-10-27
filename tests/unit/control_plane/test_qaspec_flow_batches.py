"""A target's flows run in batches, several flows a live-browser step (QA-FLOWS-CAP, part of #2136).

BGD, 2026-10-26: a QA target may hold many more flows -- AI Studio's
ai-studio-staging needs 132 read-only frame-state flows -- and a leg's flows
run in BATCHES, several flows per live-browser step, the batch size chosen
when a run starts (`flows_per_step`, 1-30) or by the workspace's default
(`qa_flows_per_step`, 15).

What these hold, against `qaspec.render` alone (the route and the workspace
setting are test_qa_target_flows_per_step.py's):

  * the cap: 150 flows register, 151 are refused with the bound in the message;
  * 132 read-only flows at 15 a step render 9 batch steps, 8 of 15 and 1 of 12,
    each prompt naming its flows in order, each flow ending "STOP and capture"
    with its own `flow-<id>.tar`, beside the capture, review and report steps;
  * a batch step's timeout is the per-flow budget times its flows, held to
    the browser profile's own ceiling;
  * the flow report names every flow's tar, by the batch tar that carries it;
  * a mutating flow is never batched with read-only ones: the gate grants
    writes per step, from the step id, so it keeps its own `flow-<id>` step;
  * `flows_per_step` outside 1-30 is refused, and a leg that would render more
    steps than a workflow holds is refused naming the smallest batch that fits;
  * a 150-flow leg's every step input together stays inside the API's input bound;
  * the cap is stated once: the capture tool never states one of its own.

No credentials, no network.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from swarm_common.config import Settings
from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import qaspec

REPO = Path(__file__).resolve().parents[3]


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a",
        "revision": 4,
        "name": "AI Studio",
        "tenant_id": "tenant-a",
        "base_url": "https://studio.example.com",
        "repository": "https://github.com/example-org/studio",
        "sign_in": {"kind": "none"},
        "screens": [{"id": "home", "path": "/"}],
        "flows": [],
        "environment": "production",
    }
    target.update(over)
    return target


def _flows(n: int, *, goal: str = "reach the frame state", mutating_every: int = 0) -> list[dict[str, Any]]:
    flows = []
    for i in range(1, n + 1):
        flow: dict[str, Any] = {"id": f"frame-{i:03d}", "goal": f"{goal} {i}", "start_path": f"/frames/{i}"}
        if mutating_every and i % mutating_every == 0:
            flow["mutating"] = True
        flows.append(flow)
    return flows


def _steps(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in spec["steps"]}


def _batches(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in spec["steps"] if re.fullmatch(r"flows-\d+", s["step_id"])]


def _named_in_order(prompt: str, ids: list[str]) -> bool:
    at = [prompt.find(f"`{fid}`") for fid in ids]
    return all(i >= 0 for i in at) and at == sorted(at)


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------


def test_the_cap_is_150_flows_a_target() -> None:
    # MUTATION: leave MAX_FLOWS at 10 and this fails.
    assert qaspec.MAX_FLOWS == 150


def test_150_flows_render_and_151_are_refused_naming_the_bound() -> None:
    spec = qaspec.render(_target(flows=_flows(150)), file="none")
    assert sum(1 for _ in _batches(spec)) == 10
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(flows=_flows(151)), file="none")
    # MUTATION: raise the bound, or refuse without saying it, and this fails.
    assert err.value.code == "qa_target_invalid"
    assert "150" in err.value.message


# --------------------------------------------------------------------------
# Batching: 132 flows at 15 a step
# --------------------------------------------------------------------------


def test_132_flows_at_15_a_step_render_nine_batches_of_15_and_one_of_12() -> None:
    flows = _flows(132)
    spec = qaspec.render(_target(flows=flows), file="none", flows_per_step=15)
    batches = _batches(spec)
    # MUTATION: render one step per flow (today's loop) and this is 132.
    assert [s["step_id"] for s in batches] == [f"flows-{k}" for k in range(1, 10)]
    ids = [f["id"] for f in flows]
    chunks = [ids[k:k + 15] for k in range(0, 132, 15)]
    assert [len(c) for c in chunks] == [15] * 8 + [12]
    for step, chunk in zip(batches, chunks):
        prompt = step["prompt"]
        # MUTATION: hand every batch the whole list, or the flows out of order.
        assert _named_in_order(prompt, chunk), step["step_id"]
        others = set(ids) - set(chunk)
        assert not any(f"`{fid}`" in prompt for fid in others), step["step_id"]
        # Each flow ends with its own capture, tar and recording, as one flow's step did.
        assert "STOP and capture" in prompt
        assert "flow-<id>.tar" in prompt and "flows/flow-<id>-*.gif" in prompt
        assert f"{step['step_id']}.tar" in prompt
        # Signs in once for the batch, on the live browser, with the flows' tool.
        assert step["runner_profile"] == qaspec.CAPTURE_PROFILE
        assert step["inputs"] == {"browser_tool": "playwright"}
    steps = _steps(spec)
    # The screens are untouched: capture, review and the report still render.
    assert {"capture-g1", "review-g1", "report", "flow-report"} <= set(steps)
    assert not any(sid.startswith("flow-") and sid != "flow-report" for sid in steps)
    assert len(spec["steps"]) == 9 + 4


def test_every_flow_keeps_its_own_tar_and_report_entry() -> None:
    flows = _flows(132)
    spec = qaspec.render(_target(flows=flows), file="none", flows_per_step=15)
    report = _steps(spec)["flow-report"]
    # MUTATION: stage nothing from a batch, and its flows never reach the report.
    assert report["input_from"] == {f"flows-{k}": f"flows-{k}.tar" for k in range(1, 10)}
    assert report["depends_on"] == [f"flows-{k}" for k in range(1, 10)]
    for flow in flows:
        # MUTATION: name only the batch tars and a missing flow goes unreported.
        assert f"flow-{flow['id']}.tar" in report["prompt"], flow["id"]
    assert "one section per flow" in report["prompt"]
    assert "missing" in report["prompt"]


def test_a_batch_steps_timeout_is_the_per_flow_budget_times_its_flows_held_to_the_profile() -> None:
    ceiling = RUNNER_PROFILES[qaspec.CAPTURE_PROFILE].timeout_seconds
    assert qaspec.FLOW_STEP_CEILING == ceiling
    assert qaspec.FLOW_SECONDS == 2700
    for n, per_step in ((1, 15), (2, 15), (3, 15), (132, 15), (5, 1)):
        spec = qaspec.render(_target(flows=_flows(n)), file="none", flows_per_step=per_step)
        for step in _batches(spec):
            size = len(re.findall(r"\(\d+\) `frame-", step["prompt"]))
            # MUTATION: keep the one-flow 2700 s on a batch of 15, or pass the ceiling.
            assert step["timeout_seconds"] == min(qaspec.FLOW_SECONDS * size, ceiling), (n, size)
    one = _batches(qaspec.render(_target(flows=_flows(1)), file="none"))
    assert one[0]["timeout_seconds"] == 2700


def test_no_flows_render_no_batch_and_no_flow_report() -> None:
    spec = qaspec.render(_target(), file="none", flows_per_step=15)
    # CONTROL: a target with no flows is exactly the screens' run.
    assert _batches(spec) == []
    assert "flow-report" not in _steps(spec)


def test_a_mutating_flow_keeps_its_own_step_and_never_joins_a_batch() -> None:
    flows = _flows(10, mutating_every=5)
    spec = qaspec.render(_target(flows=flows, environment="non_production"), file="none", flows_per_step=15)
    steps = _steps(spec)
    # The gate grants writes from a `flow-<id>` step's flow (`QaTargetIssuer._mutating`):
    # MUTATION: batch the mutating flows with the rest and frame-005 is in flows-1.
    assert {"flow-frame-005", "flow-frame-010"} <= set(steps)
    assert "may submit forms" in steps["flow-frame-005"]["prompt"]
    (batch,) = _batches(spec)
    assert "`frame-005`" not in batch["prompt"] and "`frame-010`" not in batch["prompt"]
    assert "may submit forms" not in batch["prompt"]
    assert "Submit no form" in batch["prompt"]
    report = steps["flow-report"]
    assert report["input_from"] == {"flows-1": "flows-1.tar", "flow-frame-005": "flow-frame-005.tar",
                                    "flow-frame-010": "flow-frame-010.tar"}


def test_the_batch_size_defaults_to_15() -> None:
    assert qaspec.FLOWS_PER_STEP_DEFAULT == 15
    spec = qaspec.render(_target(flows=_flows(31)), file="none")
    # MUTATION: default to one flow a step and this is 31 steps.
    assert len(_batches(spec)) == 3


@pytest.mark.parametrize("value", [0, 31, -1, True, "15", 1.5])
def test_flows_per_step_outside_1_to_30_is_refused(value: Any) -> None:
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(flows=_flows(3)), file="none", flows_per_step=value)
    # MUTATION: accept any integer, or a bool, and this renders.
    assert "flows_per_step" in err.value.message
    assert "1" in err.value.message and "30" in err.value.message


def test_the_bounds_are_1_and_30() -> None:
    assert (qaspec.MIN_FLOWS_PER_STEP, qaspec.MAX_FLOWS_PER_STEP) == (1, 30)
    for value in (1, 30):
        qaspec.render(_target(flows=_flows(3)), file="none", flows_per_step=value)


# --------------------------------------------------------------------------
# Every other bound a 150-flow leg meets
# --------------------------------------------------------------------------


def test_a_leg_over_the_workflow_step_bound_is_refused_naming_the_smallest_batch_that_fits() -> None:
    bound = next(f.default for f in dataclasses.fields(Settings) if f.name == "max_workflow_steps")
    assert qaspec.MAX_WORKFLOW_STEPS == bound == 50
    screens = [{"id": f"s{i}", "path": f"/page/{i}"} for i in range(60)]
    target = _target(flows=_flows(150), screens=screens)
    # 8 captures, 8 reviews, the report, the flow report and the filing: 19
    # steps, so 31 batches at most, so 150 flows need 5 a step.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(target, flows_per_step=4)
    # MUTATION: drop the step check and the API refuses the workflow later, unexplained.
    assert "50" in err.value.message and "flows_per_step" in err.value.message
    assert "at least 5" in err.value.message
    spec = qaspec.render(target, flows_per_step=5)
    assert len(spec["steps"]) == 19 + 30 <= bound
    assert len(qaspec.render(target)["steps"]) == 19 + 10


def test_a_150_flow_leg_fits_the_apis_input_bound() -> None:
    bound = next(f.default for f in dataclasses.fields(Settings) if f.name == "max_input_bytes")
    longest = "g" * 495
    target = _target(flows=_flows(150, goal=longest), widths=list(qaspec.PRESETS))
    for per_step in (5, 15, 30):
        spec = qaspec.render(target, flows_per_step=per_step)
        inputs = [{"prompt": s["prompt"], **(s.get("inputs") or {})} for s in spec["steps"]]
        size = len(json.dumps(inputs).encode())
        # The whole workflow's inputs together (`service.validate_input_size`,
        # "workflow input"): MUTATION: restate every width's recording under
        # every flow and this passes the bound.
        assert size < bound, (per_step, size)


def test_a_leg_whose_prompts_pass_the_input_bound_is_refused_with_the_reason() -> None:
    # 500 two-byte characters a goal: 150 KB of goals before any instruction.
    target = _target(flows=_flows(150, goal="\u00e9" * 495), widths=list(qaspec.PRESETS),
                     screens=[{"id": f"s{i}", "path": f"/page/{i}"} for i in range(60)])
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(target, flows_per_step=5)
    # MUTATION: drop the input check and the submission refuses it as "workflow input", unexplained.
    assert str(qaspec.MAX_INPUT_BYTES) in err.value.message
    assert "flows_per_step" in err.value.message and "goals" in err.value.message


def test_the_number_of_batches_is_the_ceiling_of_flows_over_the_batch_size() -> None:
    for n in (1, 14, 15, 16, 29, 30, 31, 132, 150):
        for per_step in (1, 7, 15, 30):
            if math.ceil(n / per_step) + 4 > qaspec.MAX_WORKFLOW_STEPS:
                continue
            spec = qaspec.render(_target(flows=_flows(n)), file="none", flows_per_step=per_step)
            assert len(_batches(spec)) == math.ceil(n / per_step), (n, per_step)


def test_the_cap_is_stated_once() -> None:
    """docs/mirrored-values.md: a value stated twice needs a test holding the
    two equal. The capture tool (`swarm_qa`) never reads a flow -- a flow runs
    as an agent's prompt -- so it states no flow bound, and qaspec's is the one."""
    path = REPO / "images" / "agent-runtime-browser" / "swarm_qa.py"
    text = path.read_text()
    # MUTATION: restate MAX_FLOWS in swarm_qa.py without a parity test and this fails.
    assert "MAX_FLOWS" not in text
    doc = (REPO / "docs" / "design" / "visual-qa-any-app.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| `flows` |"))
    assert f"at most {qaspec.MAX_FLOWS}" in row
    assert f"{qaspec.MIN_FLOWS_PER_STEP}-{qaspec.MAX_FLOWS_PER_STEP}" in row
    assert f"default {qaspec.FLOWS_PER_STEP_DEFAULT}" in row
