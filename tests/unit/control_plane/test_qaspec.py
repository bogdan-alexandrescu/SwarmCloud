"""The visual-QA renderer and findings validator (docs/design/visual-qa-any-app.md
§4; lane VQ-1 SPEC of docs/design/agent-autonomy.md §6, part of #1933).

`swarm_api.qaspec` renders a QA target into a workflow spec and checks the
report's findings. It is pure: no Firestore, no client, no credential. Held
here, row by row of the lane's test column:

  * a rendered spec passes `swarm_mcp.workflows.read_spec`, the one reader of
    a spec the terminal, the plugin and the launch share;
  * it names no console path, `localhost` or `npm`: the console template's
    fixture build is gone, the deployed app is `--base`;
  * screens are grouped at most 8 by 8;
  * an `own_login` target's captures carry `--keep-storage`;
  * a `mutating` flow on a `production` target is refused;
  * every step has `allow_empty_diff` and the workflow `merge: off`;
  * no step's input carries a target field (§1.1: the target travels as
    swarm-api-written metadata, never in the input);
  * a finding without evidence fails validation.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from swarm_mcp.profiles import check_spec_model_classes, spec_without_model_classes
from swarm_mcp.workflows import read_spec

from swarm_api import qaspec


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a",
        "revision": 4,
        "name": "Pricing site",
        "tenant_id": "tenant-a",
        "base_url": "https://app.example.com",
        "repository": "https://github.com/example-org/pricing-site",
        "sign_in": {"kind": "none"},
        "screens": [
            {"id": "home", "path": "/"},
            {"id": "pricing", "path": "/pricing", "ready": "main h1", "segments": 3},
        ],
        "flows": [],
        "environment": "production",
    }
    target.update(over)
    return target


def _screens(n: int) -> list[dict[str, Any]]:
    return [{"id": f"s{i}", "path": f"/page/{i}"} for i in range(n)]


def _own_login() -> dict[str, Any]:
    return {
        "kind": "own_login",
        "login_path": "/login",
        "signed_in_selector": "nav [data-user]",
    }


def _steps(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in spec["steps"]}


def _all_text(spec: dict[str, Any]) -> str:
    return json.dumps(spec)


# --------------------------------------------------------------------------
# The rendered spec is a spec
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        _target(),
        _target(sign_in=_own_login(), screens=_screens(60)),
        _target(
            environment="non_production",
            flows=[
                {"id": "checkout", "goal": "buy the cheapest plan", "start_path": "/pricing", "mutating": True},
                {"id": "find-docs", "goal": "find the API reference", "start_path": "/"},
            ],
        ),
    ],
    ids=["public", "own-login-60", "flows"],
)
def test_a_rendered_spec_passes_the_shared_spec_reader(target: dict[str, Any]) -> None:
    spec = qaspec.render(target)
    # MUTATION: render a step key build_steps does not list (say `qa_target`)
    # and read_spec refuses it.
    built = read_spec(spec_without_model_classes(spec))
    assert [s["step_id"] for s in built["steps"]] == [s["step_id"] for s in spec["steps"]]
    # MUTATION: give a capture step `model_class: "fable-x"` and the profile
    # check refuses it.
    check_spec_model_classes(spec)
    # MUTATION: render `merge: "on"` or drop `strategy` and these fail.
    assert built["merge"] == "off"
    assert built["strategy"] == "collect"
    assert built["on_step_failure"] == "continue"
    assert built["repository_url"] == target["repository"]


def test_the_discovery_spec_passes_the_shared_spec_reader() -> None:
    spec = qaspec.render_discovery(_target(screens=None, discover={"max_pages": 30, "max_depth": 2}))
    built = read_spec(spec_without_model_classes(spec))
    check_spec_model_classes(spec)
    (step,) = built["steps"]
    # MUTATION: drop the caps from the discover command and this fails.
    assert "swarm_qa discover --base https://app.example.com --max-pages 30 --max-depth 2" in step["input"]["prompt"]
    assert step["runner_profile"] == "claude-code-browser"
    assert built["merge"] == "off"


def test_discovery_is_capped_at_sixty_pages_and_depth_three() -> None:
    # MUTATION: raise MAX_DISCOVER_PAGES above 60 and the first is accepted.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render_discovery(_target(discover={"max_pages": 61, "max_depth": 2}))
    assert err.value.code == "qa_target_invalid"
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render_discovery(_target(discover={"max_pages": 10, "max_depth": 4}))


def test_the_steps_are_the_designs_shapes() -> None:
    spec = qaspec.render(
        _target(flows=[{"id": "find-docs", "goal": "find the API reference", "start_path": "/"}])
    )
    steps = _steps(spec)
    assert set(steps) == {"capture-g1", "review-g1", "flow-find-docs", "flow-report", "report", "file"}
    # MUTATION: run captures on claude-code and this fails.
    assert steps["capture-g1"]["runner_profile"] == "claude-code-browser"
    assert steps["capture-g1"]["model_class"] == "haiku"
    assert steps["review-g1"]["runner_profile"] == "claude-code"
    assert steps["review-g1"]["model_class"] == "opus"
    # MUTATION: stage the review from anything but its capture tar and this fails.
    assert steps["review-g1"]["input_from"] == {"capture-g1": "capture-g1.tar"}
    assert steps["review-g1"]["depends_on"] == ["capture-g1"]
    assert steps["flow-find-docs"]["runner_profile"] == "claude-code-browser"
    assert steps["flow-find-docs"]["model_class"] == "sonnet"
    assert steps["flow-find-docs"]["inputs"] == {"browser_tool": "jev"}
    # MUTATION: stage fewer than every capture and review tar into the report.
    assert steps["report"]["input_from"] == {
        "capture-g1": "capture-g1.tar",
        "review-g1": "review-g1.tar",
    }
    # A failed flow never costs the screen findings: the report does not wait
    # on any flow.
    assert not [d for d in steps["report"]["depends_on"] if d.startswith("flow")]
    assert steps["flow-report"]["input_from"] == {"flow-find-docs": "flow-find-docs.tar"}
    assert steps["file"]["input_from"] == {"report": "findings.json"}
    assert steps["file"]["model_class"] == "sonnet"


def test_report_only_renders_no_file_step() -> None:
    # MUTATION: ignore `file="none"` and a `file` step appears.
    spec = qaspec.render(_target(), file="none")
    assert "file" not in _steps(spec)
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(), file="everything")


# --------------------------------------------------------------------------
# Nothing of the console template survives
# --------------------------------------------------------------------------


def test_the_spec_names_no_console_path_localhost_or_npm() -> None:
    text = _all_text(qaspec.render(
        _target(sign_in=_own_login(), flows=[{"id": "f", "goal": "g", "start_path": "/"}])
    ))
    # MUTATION: copy the console capture prompt's `npm ci` / `npm run dev`
    # line, or its `--routes apps/swarm-ui/qa/routes.json`, and this fails.
    for word in ("localhost", "npm", "apps/swarm-ui", "5173", "bogdan-alexandrescu/SwarmCloud"):
        assert word not in text, word
    assert "https://app.example.com" in text


def test_the_capture_command_is_the_generic_one() -> None:
    spec = qaspec.render(_target(widths=["desktop", "iphone-15"], themes=["dark"]))
    prompt = _steps(spec)["capture-g1"]["prompt"]
    # MUTATION: drop `--settle generic` and the console's pending-read wait
    # runs on someone else's app.
    assert "--settle generic" in prompt
    assert "--base https://app.example.com" in prompt
    assert "--presets desktop,iphone-15 --themes dark" in prompt
    assert "--axe" in prompt
    # The clock stays unfrozen unless the target sets one.
    assert "--clock" not in prompt
    assert "--keep-storage" not in prompt
    # The route list the step writes is the capture tool's format, this group's.
    routes = qaspec.route_list(qaspec.group_screens(_target()["screens"]))
    assert json.dumps(routes, indent=2, sort_keys=True) in prompt
    assert "clock" not in routes
    # MUTATION: write the route list without the default ready selector.
    assert routes["groups"]["g1"][0] == {"id": "home", "path": "/", "ready": "body"}
    assert routes["format"] == 1


def test_a_target_clock_reaches_the_route_list_and_the_flag() -> None:
    spec = qaspec.render(_target(clock="2026-10-19T12:00:00Z"))
    prompt = _steps(spec)["capture-g1"]["prompt"]
    assert "--clock" in prompt
    assert '"clock": "2026-10-19T12:00:00Z"' in prompt


def test_a_sub_path_target_discovers_at_its_path_and_captures_at_its_origin() -> None:
    # Discovery writes route paths relative to the ORIGIN (`/todomvc/...`), so
    # capture must load them against the origin, or `/todomvc/todomvc/` is what
    # it opens. Decided 2026-10-22 (QA-ORIGIN): qaspec passes the origin to capture.
    target = _target(base_url="https://app.example.com/sub", discover={"max_pages": 30, "max_depth": 2})
    discover = _steps(qaspec.render_discovery(target))["discover"]["prompt"]
    # MUTATION: pass `_origin(base)` to discover as well and this fails.
    assert "swarm_qa discover --base https://app.example.com/sub " in discover
    capture = _steps(qaspec.render(target))["capture-g1"]["prompt"]
    # MUTATION: pass the full `base` (path included) to `capture --base` and this fails.
    assert "--base https://app.example.com --presets" in capture
    assert "--base https://app.example.com/sub" not in capture


def test_a_no_path_target_discovers_and_captures_at_the_same_origin() -> None:
    target = _target(base_url="https://app.example.com:8443", discover={"max_pages": 30, "max_depth": 2})
    discover = _steps(qaspec.render_discovery(target))["discover"]["prompt"]
    capture = _steps(qaspec.render(target))["capture-g1"]["prompt"]
    # MUTATION: drop the port when deriving the capture origin and this fails.
    assert "swarm_qa discover --base https://app.example.com:8443 " in discover
    assert "--base https://app.example.com:8443 --presets" in capture


def test_an_own_login_targets_captures_carry_keep_storage() -> None:
    spec = qaspec.render(_target(sign_in=_own_login(), screens=_screens(20)))
    captures = [s for s in spec["steps"] if s["step_id"].startswith("capture-")]
    assert len(captures) == 3
    for step in captures:
        # MUTATION: render `--keep-storage` only for the first group, or only
        # for `none`, and this fails.
        assert "--keep-storage" in step["prompt"], step["step_id"]
        # The sign-in is the gate's: the agent is told what to run when it
        # sees the login page again, never a credential.
        assert "swarm-handoff sign-in" in step["prompt"]
    public = qaspec.render(_target())
    assert all("--keep-storage" not in s["prompt"] for s in public["steps"])
    assert all("swarm-handoff sign-in" not in s["prompt"] for s in public["steps"])


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 8, 9, 17, 33, 60])
def test_screens_group_at_most_eight_by_eight(n: int) -> None:
    groups = qaspec.group_screens(_screens(n))
    # MUTATION: set MAX_GROUP_SCREENS to 9, or MAX_GROUPS to 9, and this fails.
    assert len(groups) <= 8
    assert all(1 <= len(screens) <= 8 for _, screens in groups)
    assert [s["id"] for _, g in groups for s in g] == [f"s{i}" for i in range(n)]
    assert [name for name, _ in groups] == [f"g{i + 1}" for i in range(len(groups))]
    assert len(groups) == -(-n // 8)
    spec = qaspec.render(_target(screens=_screens(n)))
    captures = [s for s in spec["steps"] if s["step_id"].startswith("capture-")]
    assert len(captures) == len(groups)


def test_more_than_sixty_screens_is_refused() -> None:
    # MUTATION: drop the 60-screen cap and 61 renders as 8 groups.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(screens=_screens(61)))
    assert err.value.code == "qa_target_too_many_screens"


def test_discovered_screens_are_used_when_none_are_listed() -> None:
    spec = qaspec.render(_target(screens=None, discovered=_screens(3)))
    assert '"path": "/page/2"' in _steps(spec)["capture-g1"]["prompt"]
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(screens=None))
    assert err.value.code == "qa_target_no_screens"


def test_a_subset_of_screens_runs_only_those() -> None:
    spec = qaspec.render(_target(screens=_screens(12)), screens=["s3", "s11"])
    text = _steps(spec)["capture-g1"]["prompt"]
    assert "/page/3" in text and "/page/11" in text and "/page/4" not in text
    # MUTATION: ignore an unknown name in the subset and it runs everything.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(), screens=["nope"])


# --------------------------------------------------------------------------
# Safety (§3) as the renderer can hold it
# --------------------------------------------------------------------------


def test_a_mutating_flow_on_a_production_target_is_refused() -> None:
    flow = {"id": "checkout", "goal": "buy a plan", "start_path": "/pricing", "mutating": True}
    # MUTATION: check `environment != "production"` against the wrong field,
    # or default `environment` to non_production, and this renders.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(flows=[flow]))
    assert err.value.code == "qa_mutating_flow_on_production"
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render({k: v for k, v in _target(flows=[flow]).items() if k != "environment"})
    spec = qaspec.render(_target(flows=[flow], environment="non_production"))
    assert "may submit forms" in _steps(spec)["flow-checkout"]["prompt"]


def test_a_read_only_flow_is_told_it_is_read_only() -> None:
    spec = qaspec.render(_target(flows=[{"id": "look", "goal": "find pricing", "start_path": "/"}]))
    prompt = _steps(spec)["flow-look"]["prompt"]
    # MUTATION: render the mutating wording for every flow.
    assert "Submit no form" in prompt
    assert "may submit forms" not in prompt
    # One recording per width.
    assert "swarm-handoff record start flow-look-desktop" in prompt
    assert "swarm-handoff record start flow-look-phone-390" in prompt


def test_exclusions_are_merged_with_the_defaults_and_named() -> None:
    spec = qaspec.render(_target(exclusions={"paths": ["/admin/*"], "controls": ["Archive"]}))
    for step in spec["steps"]:
        if step["runner_profile"] != "claude-code-browser":
            continue
        # MUTATION: replace the defaults with the target's list.
        for word in ("/logout", "/signout", "/admin/*", "delete", "cancel subscription", "Archive"):
            assert word in step["prompt"], (step["step_id"], word)
    # MUTATION: stop checking screens against the exclusions.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(screens=[{"id": "out", "path": "/logout"}]))
    assert err.value.code == "qa_screen_excluded"
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(screens=[{"id": "a", "path": "/admin/users"}],
                              exclusions={"paths": ["/admin/*"]}))


@pytest.mark.parametrize(
    "base_url",
    [
        "http://app.example.com",
        "https://user@app.example.com",
        "https://app.example.com/?x=1",
        "https://app.example.com/#top",
        "https://10.0.0.8",
        "https://[::1]",
        "https://app.example.com/ `id`",
        "ftp://app.example.com",
        "https://",
    ],
)
def test_a_base_url_that_is_not_a_plain_https_origin_is_refused(base_url: str) -> None:
    # MUTATION: accept any scheme, or let whitespace and backticks through to
    # the prompt's command line.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(base_url=base_url))
    assert err.value.code == "qa_target_invalid"


@pytest.mark.parametrize(
    "screen",
    [
        {"id": "x", "path": "https://evil.example.net/"},
        {"id": "x", "path": "//evil.example.net/"},
        {"id": "x", "path": "/a b"},
        {"id": "x", "path": "/a`b`"},
        {"id": "Bad Id", "path": "/"},
        {"id": "a--b", "path": "/"},
        {"id": "x", "path": "/", "segments": 13},
        {"id": "x", "path": "/", "ready": "a\nb"},
    ],
)
def test_an_unsafe_screen_is_refused(screen: dict[str, Any]) -> None:
    # MUTATION: accept a path that is not origin-relative and the capture
    # leaves the approved host.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(screens=[screen]))


def test_repeated_screen_ids_are_refused() -> None:
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(screens=[{"id": "a", "path": "/"}, {"id": "a", "path": "/b"}]))


@pytest.mark.parametrize("width", ["desktop-4k", "1440", ""])
def test_an_unknown_preset_is_refused(width: str) -> None:
    # MUTATION: pass widths through unchecked and the gate refuses at run time.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(widths=[width]))


def test_the_preset_names_are_the_gates() -> None:
    from agent_worker.livegate.presets import PRESETS

    # MUTATION: add a preset here the gate does not have.
    assert set(qaspec.PRESETS) == set(PRESETS)


# --------------------------------------------------------------------------
# §1.1: the target is never in a step's input
# --------------------------------------------------------------------------


def test_no_steps_input_carries_a_target_field() -> None:
    target = _target(sign_in=_own_login(), environment="non_production",
                     flows=[{"id": "f", "goal": "g", "start_path": "/", "mutating": True}])
    spec = qaspec.render(target)
    built = read_spec(spec_without_model_classes(spec))
    for raw, step in zip(spec["steps"], built["steps"]):
        # MUTATION: add `inputs: {"qa_target": ...}` to a browser step and
        # this fails (and the profile's declared inputs refuse it).
        assert set(raw.get("inputs") or {}) <= {"browser_tool"}, raw["step_id"]
        assert set(step["input"]) <= {"prompt", "browser_tool"}, raw["step_id"]
        for key in raw:
            assert "target" not in key, (raw["step_id"], key)
    assert "qa_target" not in spec and "target" not in spec


def test_the_metadata_names_the_target_and_its_browser_steps() -> None:
    spec = qaspec.render(_target(flows=[{"id": "f", "goal": "g", "start_path": "/"}]))
    # MUTATION: stamp the review step too, or drop the revision.
    assert qaspec.qa_target_metadata(_target()) == {"id": "qat_3f9a", "revision": 4}
    assert qaspec.browser_steps(spec) == ["capture-g1", "flow-f"]


def test_no_credential_value_is_ever_rendered() -> None:
    import secrets

    value = secrets.token_hex(16)
    target = _target(sign_in={**_own_login(), "password": value, "username": value},
                     credential={"set": True, "version": 3, "value": value})
    # MUTATION: interpolate the sign_in block into a prompt and this fails.
    assert value not in _all_text(qaspec.render(target))


# --------------------------------------------------------------------------
# Every step changes nothing
# --------------------------------------------------------------------------


def test_every_step_allows_an_empty_diff_and_the_workflow_never_merges() -> None:
    spec = qaspec.render(_target(sign_in=_own_login(), screens=_screens(30),
                                 flows=[{"id": "f", "goal": "g", "start_path": "/"}]))
    # MUTATION: drop allow_empty_diff from any one step builder.
    assert all(s.get("allow_empty_diff") is True for s in spec["steps"])
    assert spec["merge"] == "off"
    assert all("Change no file in the repository" in s["prompt"] for s in spec["steps"])


def test_the_review_reads_the_apps_design_docs() -> None:
    spec = qaspec.render(_target(design_docs=["STYLE.md", "docs/ui/**"]))
    prompt = _steps(spec)["review-g1"]["prompt"]
    # MUTATION: render the default globs whatever the target says.
    assert "STYLE.md" in prompt and "docs/ui/**" in prompt
    default = _steps(qaspec.render(_target()))["review-g1"]["prompt"]
    assert "DESIGN.md" in default and "docs/design*/**" in default


def test_the_file_step_carries_the_filing_rules() -> None:
    spec = qaspec.render(_target(file={"min_severity": "major", "max_issues": 5,
                                       "labels": ["qa"], "evidence": "console_link"}))
    prompt = _steps(spec)["file"]["prompt"]
    assert "visual-qa:qat_3f9a:" in prompt
    assert "at most 5" in prompt
    assert "major" in prompt
    assert '"qa"' in prompt
    # VQ-Q3: a console link by default; the branch only when opted in.
    assert "git_push_branch" not in prompt
    branch = _steps(qaspec.render(_target(file={"evidence": "branch"})))["file"]["prompt"]
    assert "git_push_branch" in branch and "swarm/" in branch
    # MUTATION: let max_issues above the broker's 30 through.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(file={"max_issues": 31}))
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(file={"evidence": "upload"}))


def test_render_does_not_mutate_the_target() -> None:
    target = _target(sign_in=_own_login(), flows=[{"id": "f", "goal": "g", "start_path": "/"}])
    before = copy.deepcopy(target)
    qaspec.render(target)
    assert target == before


# --------------------------------------------------------------------------
# The findings validator (§4.4)
# --------------------------------------------------------------------------


def _finding(**over: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "g2-004",
        "title": "The pricing table overflows its card at 390 in dark mode",
        "severity": "major",
        "screen": "pricing",
        "path": "/pricing",
        "width": "phone-390",
        "theme": "dark",
        "evidence": ["g2-004.png", "g2/pricing--phone-390--dark--s2.png"],
        "actual": "The third column is cut off at the card's right edge.",
        "expected": "The table wraps or scrolls inside its card, as at 1440.",
        "steps_to_reproduce": ["Open /pricing at 390 px wide", "Set the system theme to dark"],
        "also_seen": [{"width": "desktop", "theme": "dark"}],
    }
    finding.update(over)
    return finding


def test_a_whole_finding_is_accepted() -> None:
    checked = qaspec.validate_findings({"format": 1, "findings": [_finding()]})
    assert [f["id"] for f in checked.accepted] == ["g2-004"]
    assert checked.refused == []


@pytest.mark.parametrize("evidence", [None, [], [""], ["../etc/passwd"], ["/abs.png"], "g2-004.png"])
def test_a_finding_without_evidence_fails_validation(evidence: Any) -> None:
    finding = _finding()
    if evidence is None:
        del finding["evidence"]
    else:
        finding["evidence"] = evidence
    checked = qaspec.validate_findings({"format": 1, "findings": [finding]})
    # MUTATION: treat a missing or empty evidence list as fine and this fails.
    assert checked.accepted == []
    (refused,) = checked.refused
    assert refused["id"] == "g2-004"
    assert any("evidence" in p for p in refused["problems"])


def test_evidence_the_staged_tars_do_not_hold_fails_validation() -> None:
    finding = _finding()
    # MUTATION: skip the availability check and the finding is accepted.
    checked = qaspec.validate_findings({"format": 1, "findings": [finding]},
                                       available={"g2-004.png"})
    assert checked.accepted == []
    assert "g2/pricing--phone-390--dark--s2.png" in " ".join(checked.refused[0]["problems"])
    ok = qaspec.validate_findings({"format": 1, "findings": [finding]},
                                  available=set(finding["evidence"]))
    assert len(ok.accepted) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"severity": "critical"},
        {"title": ""},
        {"path": "pricing"},
        {"width": "desktop-4k"},
        {"theme": "sepia"},
        {"actual": ""},
        {"expected": None},
        {"steps_to_reproduce": []},
        {"also_seen": [{"width": "nope", "theme": "dark"}]},
    ],
)
def test_a_finding_missing_what_an_issue_needs_is_refused(over: dict[str, Any]) -> None:
    # MUTATION: drop any one of the field checks and its case is accepted.
    checked = qaspec.validate_findings({"format": 1, "findings": [_finding(**over)]})
    assert checked.accepted == []
    assert checked.refused and checked.refused[0]["problems"]


def test_findings_about_the_run_pass_through_apart() -> None:
    document = {
        "format": 1,
        "findings": [_finding()],
        "about_run": [{"id": "run-1", "title": "review-g3.tar was not staged",
                       "actual": "the report found no review-g3.tar"}],
    }
    checked = qaspec.validate_findings(document)
    assert len(checked.accepted) == 1
    # MUTATION: file run findings as app issues and this fails.
    assert [f["id"] for f in checked.about_run] == ["run-1"]


def test_repeated_finding_ids_are_refused() -> None:
    checked = qaspec.validate_findings({"format": 1, "findings": [_finding(), _finding()]})
    assert len(checked.accepted) == 1
    assert len(checked.refused) == 1


@pytest.mark.parametrize("document", [[], {"format": 2, "findings": []}, {"format": 1}, "x"])
def test_a_document_that_is_not_findings_is_refused(document: Any) -> None:
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.validate_findings(document)
    assert err.value.code == "qa_findings_invalid"


def test_the_refusals_redact_what_they_quote() -> None:
    # A finding that quotes a credential-shaped run from the page is refused
    # with its problems; nothing it quotes is echoed back unredacted.
    token = "ghp_" + "x" * 36
    checked = qaspec.validate_findings({"format": 1, "findings": [_finding(severity=token)]})
    assert token not in json.dumps(checked.refused)


# --------------------------------------------------------------------------
# Start clicks (§2.2; BGD, 2026-10-23: a chooser before the IdP's form)
# --------------------------------------------------------------------------

#: AI Studio's chooser: a Staging | Production toggle, then "Sign in".
_CHOOSER = [{"text": "Staging", "css": "button"}, {"text": "Sign in"}]


def _with_clicks(clicks: Any, **over: Any) -> dict[str, Any]:
    return _target(sign_in={**_own_login(), "start_clicks": clicks}, **over)


def test_valid_start_clicks_are_accepted_trimmed_and_in_order() -> None:
    clicks = qaspec.start_clicks(_with_clicks([{"text": "  Staging ", "css": "button"}, {"text": "Sign in"}]))
    # MUTATION: drop the strip in `start_clicks` (or reorder) and this fails.
    assert clicks == _CHOOSER
    # MUTATION: require start clicks on every own-login target and this raises.
    assert qaspec.start_clicks(_target(sign_in=_own_login())) == []
    qaspec.render(_with_clicks(_CHOOSER))


@pytest.mark.parametrize("css", ["button:has-text('Sign in')", "text=Sign in", "div >> button", "button:visible"])
def test_playwright_selector_syntax_is_refused_with_why(css: str) -> None:
    with pytest.raises(qaspec.QaSpecError) as refused:
        qaspec.start_clicks(_with_clicks([{"text": "Sign in", "css": css}]))
    # MUTATION: drop `_PLAYWRIGHT_SELECTOR` from `start_clicks` and a selector the gate cannot run is stored.
    assert "CDP" in refused.value.message and "Playwright" in refused.value.message


@pytest.mark.parametrize("clicks", [[{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}], [],
                                    "Sign in", [{"text": "x" * 61}], [{"text": "Sign in", "css": "b" * 201}],
                                    [{"text": "Sign in", "selector": "button"}]])
def test_more_than_three_or_a_malformed_start_click_is_refused(clicks: Any) -> None:
    # MUTATION: raise MAX_START_CLICKS (or drop a bound in `start_clicks`) and this fails.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.start_clicks(_with_clicks(clicks))


@pytest.mark.parametrize("text,exclusions", [("Log out", None), ("Logout", None), ("Delete account", None),
                                             ("Buy now", None), ("Archive workspace", {"controls": ["archive"]})])
def test_a_start_click_naming_an_exclusion_word_is_refused(text: str, exclusions: Any) -> None:
    over = {"exclusions": exclusions} if exclusions else {}
    with pytest.raises(qaspec.QaSpecError) as refused:
        qaspec.start_clicks(_with_clicks([{"text": text}], **over))
    # MUTATION: skip the exclusion-word check in `start_clicks` (or read only the defaults) and this fails.
    assert "excluded control" in refused.value.message


def test_start_clicks_on_a_public_target_are_refused() -> None:
    target = _target(sign_in={"kind": "none", "start_clicks": [{"text": "Sign in"}]})
    # MUTATION: drop the own_login check in `start_clicks` and a public target carries clicks.
    with pytest.raises(qaspec.QaSpecError) as refused:
        qaspec.start_clicks(target)
    assert "own_login" in refused.value.message
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(target)


def test_the_capture_flow_and_discovery_prompts_name_the_start_clicks_in_order_only_when_set() -> None:
    flows = [{"id": "f", "goal": "g", "start_path": "/"}]
    chooser = "If your tab shows the sign-in chooser, click 'Staging' then 'Sign in'; the sign-in completes"
    spec = qaspec.render(_with_clicks(_CHOOSER, flows=flows))
    browser = [s for s in spec["steps"] if (s.get("inputs") or {}).get("browser_tool")]
    assert {s["step_id"].split("-")[0] for s in browser} == {"capture", "flow"}
    for step in browser:
        # MUTATION: leave the clicks out of `_sign_in_note` (or a caller's call) and this fails.
        assert chooser in step["prompt"], step["step_id"]
        assert "Never type into a login form" in step["prompt"]
    discovery = qaspec.render_discovery(_with_clicks(_CHOOSER, screens=None,
                                                     discover={"max_pages": 5, "max_depth": 1}))
    assert chooser in discovery["steps"][0]["prompt"]
    plain = qaspec.render(_target(sign_in=_own_login(), flows=flows))
    # MUTATION: write the chooser sentence for every own-login target and this fails.
    assert all("chooser" not in s["prompt"] for s in plain["steps"])
    assert all("chooser" not in s["prompt"] for s in qaspec.render(_target())["steps"])
