"""A flow's capture is faithful to what it records (QA-FLOW-CAPTURE, part of #2688).

The AI Studio session's QA re-run 8 (6 November 2026, target ai-studio-staging,
seven legs) reported three harness defects:

  (a) a flow's screenshot did not show the state its json claimed: a step
      recorded as "twin opened" whose frame shows the closed button;
  (b) Playwright snapshot and click calls failed inside flows on several legs;
  (c) two legs packaged their flow output as flow-1.tar, not the layout.

What these hold, against `qaspec` alone:

  * every flow brief makes each step WAIT for what proves the state it is about
    to record, bounded, before its screenshot, and record that proof in the
    step's json (a);
  * a snapshot or click that fails while the page settles is retried a bounded
    number of times with the reason recorded, and a step fails only after (b):
    the brief carried no such rule, which is the agent's usage, not the tool's;
  * the output layout is stated once (`FLOW_LAYOUT`), every flow brief renders
    the exact bundle commands from it, and the result check
    (`check_flow_output`) normalises a lone stray tar such as flow-1.tar to the
    expected name with a line saying so, refuses an ambiguous one, and flags a
    step json without its proof (c);
  * a fixture flow run whose element appeared late carries its retry, and the
    check counts it.

No credentials, no network.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api import qaspec


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
        "flows": [
            {"id": "twin-open", "goal": "open the twin panel", "start_path": "/frames/1"},
            {"id": "twin-close", "goal": "close the twin panel", "start_path": "/frames/2"},
        ],
        "environment": "non_production",
    }
    target.update(over)
    return target


def _steps(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in spec["steps"]}


def _flow_prompts(tool: str = "playwright") -> dict[str, str]:
    """A batch prompt and a mutating flow's own prompt, both flow briefs."""
    target = _target(flows=_target()["flows"] + [
        {"id": "save-frame", "goal": "save the frame", "start_path": "/frames/3",
         "mutating": True, "writes": "One saved frame named qa-check."}])
    steps = _steps(qaspec.render(target, browser_tool=tool))
    return {"flows-1": steps["flows-1"]["prompt"], "flow-save-frame": steps["flow-save-frame"]["prompt"]}


# --------------------------------------------------------------------------
# (a) wait, then capture, and record the proof
# --------------------------------------------------------------------------


def test_every_flow_brief_waits_for_the_state_before_its_screenshot_and_records_the_proof() -> None:
    for step_id, prompt in _flow_prompts().items():
        # MUTATION: drop `_capture_rule` from a flow prompt and the step
        # screenshots whatever is on screen, as run 8's "twin opened" did.
        assert "before its screenshot" in prompt, step_id
        assert f"at most {qaspec.FLOW_WAIT_SECONDS} s" in prompt, step_id
        assert "browser_wait_for" in prompt, step_id
        assert '`proof`' in prompt and '{"text":' in prompt and '{"element":' in prompt, step_id
        assert '{"missing":' in prompt, step_id


def test_a_jev_flow_brief_waits_too_without_naming_playwright() -> None:
    for step_id, prompt in _flow_prompts("jev").items():
        assert "before its screenshot" in prompt, step_id
        assert "`proof`" in prompt, step_id
        assert "Playwright MCP" not in prompt and "browser_wait_for" not in prompt, step_id


# --------------------------------------------------------------------------
# (b) a settling page is retried, bounded, with the reason
# --------------------------------------------------------------------------


def test_every_flow_brief_retries_a_settling_snapshot_or_click_a_bounded_number_of_times() -> None:
    assert 2 <= qaspec.FLOW_TOOL_TRIES <= 5
    for step_id, prompt in _flow_prompts().items():
        # MUTATION: drop `_retry_rule` and a stale ref after `swarm-handoff
        # device` reloads the page fails the step on its first try.
        assert "browser_snapshot or browser_click" in prompt, step_id
        assert f"at most {qaspec.FLOW_TOOL_TRIES} tries" in prompt, step_id
        assert "`retries`" in prompt and '{"call":' in prompt and '"reason":' in prompt, step_id
        assert "fails only after" in prompt, step_id
        # The gate reloads the page when `device` changes the user agent
        # (DEVICE-RELOAD), so a ref from before it is stale by design.
        assert "never reuse a ref from before it" in prompt, step_id


# --------------------------------------------------------------------------
# (c) the layout, stated once, rendered as the bundle commands
# --------------------------------------------------------------------------


def test_the_layout_is_stated_once_and_every_flow_brief_requires_it() -> None:
    assert qaspec.FLOW_LAYOUT == {
        "json": "flow-{id}.json",
        "recording": "flows/flow-{id}-{width}.gif",
        "screenshots": "screenshots/",
        "flow_tar": "flow-{id}.tar",
        "batch_tar": "{step}.tar",
    }
    prompts = _flow_prompts()
    batch = prompts["flows-1"]
    # MUTATION: say "bundle ... into flows-1.tar" in words again, and an agent
    # writes flow-1.tar, as two of run 8's legs did.
    assert "tar -cf flow-twin-open.tar flow-twin-open.json flows/flow-twin-open-*.gif" in batch
    assert "tar -cf flows-1.tar flow-twin-open.tar flow-twin-close.tar" in batch
    assert "no other name" in batch and "flow-1.tar" in batch
    own = prompts["flow-save-frame"]
    assert "tar -cf flow-save-frame.tar flow-save-frame.json flows/flow-save-frame-*.gif" in own


def test_the_flow_report_applies_the_result_check() -> None:
    report = _steps(qaspec.render(_target()))["flow-report"]["prompt"]
    # MUTATION: drop `_flow_check_line` and the report reads a proofless
    # screenshot as the state its json claims.
    assert "`proof`" in report and "not proven" in report
    assert "`retries`" in report


# --------------------------------------------------------------------------
# The result check
# --------------------------------------------------------------------------


def _proven_run(width: str = "desktop", **step_over: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "did": "clicked Open twin",
        "state": "twin opened",
        "proof": {"element": 'button "Close twin"', "waited_s": 1},
        "screenshot": "screenshots/page-2026-11-06T09-20-01.png",
        "shows": "the twin panel, open",
    }
    step.update(step_over)
    return {"width": width, "goal_reached": True, "steps": [step]}


def _doc(fid: str = "twin-open", *runs: dict[str, Any]) -> dict[str, Any]:
    return {"flow": fid, "runs": list(runs) or [_proven_run()]}


def test_the_layout_as_asked_passes_the_check() -> None:
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open", "twin-close"],
        files=["flows-1.tar", "flow-twin-open.tar", "flow-twin-close.tar", "screenshots/a.png"],
        documents={"twin-open": _doc(), "twin-close": _doc("twin-close")})
    assert check.ok
    assert check.tar == "flows-1.tar"
    assert check.lines == [] and check.renamed is None


def test_a_lone_flow_1_tar_in_place_of_the_batch_tar_is_normalised_with_a_line() -> None:
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open", "twin-close"],
        files=["flow-1.tar", "flow-twin-open.tar", "flow-twin-close.tar"],
        documents={"twin-open": _doc(), "twin-close": _doc("twin-close")})
    # MUTATION: accept any tar silently and the report reads two legs' flows
    # as missing (run 8), or reads a stray tar as the batch with nothing said.
    assert check.tar == "flow-1.tar"
    assert check.renamed == ("flow-1.tar", "flows-1.tar")
    assert check.ok
    assert any("flow-1.tar" in line and "flows-1.tar" in line and "read as" in line for line in check.lines)


def test_two_stray_tars_in_place_of_the_layout_are_refused_naming_both() -> None:
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flow-1.tar", "flows_1.tar"], documents={})
    assert not check.ok and check.tar is None
    assert any("flow-1.tar" in line and "flows_1.tar" in line and "refused" in line for line in check.lines)


def test_no_tar_at_all_is_refused_with_the_expected_name() -> None:
    check = qaspec.check_flow_output("flow-save-frame", ["save-frame"], files=["notes.md"], documents={})
    assert not check.ok
    assert any("flow-save-frame.tar" in line for line in check.lines)


def test_a_flows_own_tar_is_never_read_as_the_batch_tar() -> None:
    # A flow whose id is "1" legitimately writes flow-1.tar: that is its own
    # tar, inside the batch, not a stray batch tar.
    check = qaspec.check_flow_output(
        "flows-1", ["1"], files=["flow-1.tar"], documents={"1": _doc("1")})
    assert not check.ok and check.renamed is None
    assert any("flows-1.tar" in line for line in check.lines)


def test_a_step_json_without_its_proof_is_flagged() -> None:
    bare = _proven_run()
    del bare["steps"][0]["proof"]
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flows-1.tar"], documents={"twin-open": _doc("twin-open", bare)})
    # MUTATION: drop the proof check and run 8's "twin opened" over a closed
    # button passes as proven.
    assert not check.ok
    assert any("twin-open" in line and "desktop" in line and "step 1" in line and "no proof" in line
               for line in check.lines)


def test_a_step_whose_proof_says_missing_is_reported_as_not_proven() -> None:
    run = _proven_run(proof={"missing": 'button "Close twin"', "waited_s": qaspec.FLOW_WAIT_SECONDS})
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flows-1.tar"], documents={"twin-open": _doc("twin-open", run)})
    assert check.ok  # recorded honestly: the report says so, nothing is refused
    assert any("not proven" in line and "Close twin" in line for line in check.lines)


def test_a_flow_with_no_json_is_named() -> None:
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open", "twin-close"], files=["flows-1.tar"], documents={"twin-open": _doc()})
    assert not check.ok
    assert any("twin-close" in line and "flow-twin-close.json" in line for line in check.lines)


def test_a_fixture_run_with_a_slow_element_records_its_retry_and_the_check_counts_it() -> None:
    # The fixture: the Open twin button rendered after the first snapshot, so
    # the first click's ref was stale; the second try, on a fresh snapshot, hit.
    slow = _proven_run(retries=[{"call": "browser_click", "reason": "Ref e42 not found in the current page snapshot"}])
    slow["steps"][0]["proof"] = {"element": 'button "Close twin"', "waited_s": 4}
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flows-1.tar"], documents={"twin-open": _doc("twin-open", slow)})
    assert check.ok
    assert check.retries == {"twin-open": 1}
    assert any("twin-open" in line and "1 retried" in line for line in check.lines)


def test_retries_past_the_bound_or_without_a_reason_are_flagged() -> None:
    many = _proven_run(retries=[{"call": "browser_click", "reason": "Ref not found"}] * (qaspec.FLOW_TOOL_TRIES))
    silent = _proven_run("phone-390", retries=[{"call": "browser_snapshot"}])
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flows-1.tar"],
        documents={"twin-open": _doc("twin-open", many, silent)})
    assert not check.ok
    assert any("desktop" in line and f"more than {qaspec.FLOW_TOOL_TRIES - 1}" in line for line in check.lines)
    assert any("phone-390" in line and "without a reason" in line for line in check.lines)


def test_the_checks_lines_redact_what_they_quote() -> None:
    leaked = "ghp_" + secrets.token_hex(18)
    run = _proven_run(proof={"missing": f"text {leaked}"})
    check = qaspec.check_flow_output(
        "flows-1", ["twin-open"], files=["flows-1.tar"], documents={"twin-open": _doc("twin-open", run)})
    assert leaked not in " ".join(check.lines)
